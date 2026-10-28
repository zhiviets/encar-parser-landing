"""
Запуск парсера Encar с подгонкой под справочник Drom (тот же файл, что на сайте).

    cd scraper
    python run_drom.py

Что меняется по сравнению с `python scraper.py` (остальной код — прокси, паузы, порции, фото,
шкала цен — тот же):

1. Мощность — только из справочника Drom (drom_index.py), а не оценка по объёму и названию.
   Машина берётся, если справочник однозначно называет её мощность: марка, модель, год, объём,
   топливо и привод совпали с одной комплектацией (или со всеми, у которых мощность одна).
   Моделей, которых нет в справочнике, в каталог не попадают; список таких — в конце лога.
2. Доли каталога: 70% машин до 160 л.с. и 30% мощнее; годы — 15% 2010–2018, 10% 2019–2021,
   65% 2022–2025, 10% 2026. Любую долю можно поменять переменными ниже.
3. Объём каталога: ENCAR_FILL_TARGET (по умолчанию 40000 машин), на модель за прогон больше машин.

Без BN_AUTO_URL / BN_AUTO_IMPORT_TOKEN парсер ничего не отправляет (сухой прогон: cars.json локально).

Переменные (необязательные):
    ENCAR_SHARE_160   доля машин до 160 л.с. (по умолчанию 0.70)
    ENCAR_YEAR_SHARES доли лет «2010-2018:0.15,2019-2021:0.10,2022-2025:0.65,2026-2026:0.10»
    ENCAR_FILL_TARGET сколько машин держать в каталоге (по умолчанию 40000)
    DROM_INDEX_PATH   путь к drom-index.json.gz (по умолчанию — рядом со скриптом)
"""

import os
import re
import sys

# Эти значения читаются при импорте scraper / coverage, поэтому — до него
os.environ.setdefault("ENCAR_SHARE_160", "0.70")
os.environ.setdefault("ENCAR_FILL_TARGET", "40000")
os.environ.setdefault("ENCAR_PER_MODEL_LE160", "6")
os.environ.setdefault("ENCAR_PER_MODEL_OTHER", "3")
os.environ.setdefault("ENCAR_MIN_YEAR", "2010")

import drom_index  # noqa: E402
import selection  # noqa: E402
import coverage  # noqa: E402
import scraper  # noqa: E402

COUNTRY = "Корея"
INDEX = drom_index.load()


def _year_bands():
    spec = os.environ.get("ENCAR_YEAR_SHARES") or "2010-2018:0.15,2019-2021:0.10,2022-2025:0.65,2026-2026:0.10"
    bands = []
    for part in spec.split(","):
        m = re.fullmatch(r"\s*(\d{4})-(\d{4}):([\d.]+)\s*", part)
        if not m:
            raise SystemExit(f"ENCAR_YEAR_SHARES: не понял «{part}» (нужно 2010-2018:0.15)")
        lo, hi, share = int(m.group(1)), int(m.group(2)), float(m.group(3))
        bands.append((f"{lo}–{hi}" if lo != hi else str(lo), lo, hi, share))
    total = sum(b[3] for b in bands)
    if abs(total - 1) > 0.01:
        raise SystemExit(f"ENCAR_YEAR_SHARES: доли дают {total:.2f}, а должны 1.00")
    # Порядок — приоритет при выборе машины: сначала самые нужные (большие доли)
    bands.sort(key=lambda b: -b[3])
    return bands


# Мутируем список на месте: selection.year_band() и coverage.pick() читают selection.YEAR_BANDS
selection.YEAR_BANDS[:] = _year_bands()

def _fuel_ko(value):
    text = str(value or "")
    if text == "전기":
        return "e"
    if "하이브리드" in text or "+전기" in text:
        return "h"
    if "디젤" in text:
        return "d"
    return "p" if text else None


# ---------- 1. класс мощности при просмотре поиска: по справочнику, без оценки по названию ----------

_orig_to_car = coverage._to_car


def _to_car(r):
    car = _orig_to_car(r)
    if car:
        # Объёма в списке поиска нет: предварительный класс по марке, модели, году и топливу.
        # None — комплектации разной мощности: машину откроет карточка (API) и уточнит по объёму
        car["power"] = INDEX.power_band(COUNTRY, car.get("brand_en"), car.get("model_guess"), car.get("year"),
                                        _fuel_ko(r.get("FuelType")))
    return car


coverage._to_car = _to_car


# ---------- 2. уточнение по карточке: строгое совпадение со справочником ----------

def class_for_detail(d: dict) -> str | None:
    spec = d.get("spec") or {}
    return INDEX.power_class(COUNTRY, d.get("make"), d.get("model"), d.get("year"), spec.get("Топливо"),
                             d.get("displacement"), spec.get("Привод"))


def _digits(value):
    return int(re.sub(r"\D", "", str(value or "")) or 0) or None


def class_for_known(info: dict) -> str | None:
    """Машина уже на сайте: мощность известна сайту (hp) — иначе справочник по данным сайта."""
    kind = str(info.get("engine_type") or "")
    fuel = "e" if "electric" in kind and "hybrid" not in kind else "h" if "hybrid" in kind else "d" if "diesel" in kind else "p"
    hp = _digits(info.get("hp"))
    if hp and info.get("exact_power") is not False:
        return INDEX._class(hp, fuel)
    return INDEX.power_class(COUNTRY, info.get("make"), info.get("model"), _digits(info.get("year")), fuel,
                             _digits(info.get("cc")))


def _resolver(session, known, pacer, parse_encar_detail, power_of):
    def resolve(car):
        info = known.get(car["external_id"])
        if info:
            return class_for_known(info)
        if pacer.blocked:
            return None
        detail = pacer.detail(session, car["external_id"])
        if not detail:
            return None
        d = parse_encar_detail(detail, car["external_id"])
        car["detail"] = d
        return class_for_detail(d)

    return resolve


coverage._resolver = _resolver


# ---------- 3. после карточки: машина должна однозначно найтись в справочнике ----------

def still_ok(car: dict) -> bool:
    d = car.get("detail")
    if not d:
        return True
    return class_for_detail(d) is not None and selection.eligible(d.get("make") or car.get("brand_en"),
                                                                  d.get("year") or car.get("year"))


scraper.still_ok = still_ok


# ---------- 4. мощность в характеристики машины — из справочника ----------

def add_drom_power(cars, drom, counts, tried=None):
    from datetime import date
    today = date.today().toordinal()
    for c in cars:
        d = c.get("detail") or {}
        spec = d.get("spec")
        if not spec or spec.get("Мощность, л.с.") or not d.get("make") or not d.get("model") or not d.get("year"):
            if tried is not None and not (spec or {}).get("Мощность, л.с."):
                tried[c["external_id"]] = today
            continue
        m = INDEX.match(COUNTRY, d["make"], d["model"], d["year"], spec.get("Топливо"), d.get("displacement"),
                        spec.get("Привод"))
        if m["status"] != "ok":
            counts["none"] += 1
            if tried is not None:
                tried[c["external_id"]] = today
            continue
        spec["Мощность, л.с."] = str(m["hp"])
        if m.get("power30") and m["fuel"] in "ehs":
            spec["30-минутная мощность, л.с."] = str(m["power30"])
        if m.get("total") and m["fuel"] in "hs":
            spec["Суммарная мощность гибрида, л.с."] = str(m["total"])
        if m.get("drive") and not spec.get("Привод"):
            spec["Привод"] = m["drive"].lower()
        counts["found"] += 1
        if tried is not None:
            tried.pop(c["external_id"], None)


scraper.add_drom_power = add_drom_power


def main():
    print(f"Справочник Drom: {INDEX.path.name}, доля до 160 л.с. {os.environ['ENCAR_SHARE_160']}, "
          f"годы: " + ", ".join(f"{n} — {w:.0%}" for n, _, _, w in selection.YEAR_BANDS))
    try:
        scraper.main()
    finally:
        s = INDEX.stats
        print(f"Справочник: однозначно {s['ok']}, неоднозначно {s['ambiguous']}, не найдено {s['none']}")
        if INDEX.missing:
            top = sorted(INDEX.missing.items(), key=lambda x: -x[1])[:40]
            print("Моделей нет в справочнике (марка+модель, сколько раз) — для доработки справочника:")
            for key, n in top:
                print(f"  {key}: {n}")


if __name__ == "__main__":
    sys.exit(main())
