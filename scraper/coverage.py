"""
Все модели encar: хотя бы по одной машине каждой модели с MIN_YEAR года.

Марки и модели берём из фасетов поиска API encar — тех же счётчиков, из
которых сайт строит фильтры «제조사 → 모델». По каждой модели запрашиваем
ENCAR_PER_MODEL свежих объявлений (одним запросом) и выбираем:
  1) по машине на модель — до 160 л.с., если такая есть;
  2) добор до ENCAR_TOTAL так, чтобы машин до 160 л.с. было не меньше ENCAR_SHARE_160 (35%),
     а по годам — 60% 2022–2024, 15% 2025–2026, 15% 2017–2021, 10% 2010–2016.
Если моделей мощнее 160 л.с. слишком много, «до 160» добираются сверх
ENCAR_TOTAL — доля важнее общего числа.
"""

import json
import re
import os
import random
import time
from pathlib import Path
from urllib.parse import quote

import selection
import wanted as wanted_mod
from brand_map import extract_brand_model

SEARCH_API = "https://api.encar.com/search/car/list/general"
# Сколько свежих объявлений модели смотреть (страницами по PAGE): при заполнении каталога
# новые машины модели ищутся глубже первых десятков
PER_MODEL = int(os.environ.get("ENCAR_PER_MODEL") or "100")
# Разнообразие: не больше стольких машин одной модели за прогон — отдельно до 160 л.с. и мощнее,
# чтобы доля 75/25 сохранялась
PER_MODEL_RUN = {"le160": int(os.environ.get("ENCAR_PER_MODEL_LE160") or "3"),
                 "gt160": int(os.environ.get("ENCAR_PER_MODEL_OTHER") or "2")}
PAGE = 50
# Сколько машин модели без объёма в названии уточнять по API при выборе «до 160»
RESOLVE_PER_MODEL = 6
# Через столько минут после старта отбор больше не уточняет мощность (лимит GitHub — 6 ч)
RUN_MINUTES = float(os.environ.get("ENCAR_RUN_MINUTES") or "300")
# Доля прогона под модели из справочника сайта, которых на сайте мало (wanted.py)
WANTED_SHARE = float(os.environ.get("ENCAR_WANTED_SHARE") or "0.5")
STARTED = time.time()
HEADERS = {
    "Referer": "https://www.encar.com/",
    "Origin": "https://www.encar.com",
    "Accept": "application/json, text/plain, */*",
}


class SearchBlocked(Exception):
    pass


def _query(cartype: str, maker: str | None = None, group: str | None = None, with_year: bool = True) -> str:
    """Выражение поиска encar: «(And.Hidden.N._.(C.CarType.Y._.Manufacturer.현대.)_.Year.range(201700..).)»."""
    if group:
        cond = f"(C.CarType.{cartype}._.(C.Manufacturer.{maker}._.ModelGroup.{group}.))"
    elif maker:
        cond = f"(C.CarType.{cartype}._.Manufacturer.{maker}.)"
    else:
        cond = f"CarType.{cartype}."
    parts = ["Hidden.N.", cond]
    if with_year:
        parts.append(f"Year.range({selection.MIN_YEAR}00..).")
    return "(And." + "_.".join(parts) + ")"


def _search(session, q: str, count: int = 0, inav: bool = False, offset: int = 0) -> dict:
    url = (f"{SEARCH_API}?count=true&q={quote(q, safe='(),._')}"
           f"&sr={quote(f'|ModifiedDate|{offset}|{count}', safe='|')}")
    if inav:
        url += "&inav=" + quote("|Metadata|Sort", safe="|")
    for attempt in (1, 2):
        try:
            resp = session.get(url, headers=HEADERS, timeout=30)
        except Exception as error:
            if attempt == 2:
                raise
            print(f"  поиск encar: {error} — ещё раз")
            time.sleep(random.uniform(5, 10))
            continue
        if resp.status_code in (403, 429):
            if attempt == 2:
                raise SearchBlocked(f"HTTP {resp.status_code}")
            print(f"  поиск encar притормозил (HTTP {resp.status_code}) — пауза 90–150 с")
            time.sleep(random.uniform(90, 150))
            continue
        resp.raise_for_status()
        try:
            return resp.json()
        except ValueError:
            raise SearchBlocked("вместо JSON пришла страница (капча?)")
    return {}


# Каждые SEARCH_BREAK_EVERY запросов к поиску — перерыв SEARCH_BREAK_MIN минут
SEARCH_BREAK_EVERY = 120
SEARCH_BREAK_MIN = float(os.environ.get("ENCAR_SEARCH_BREAK") or "1.5")
_searches = {"n": 0}


def _pause():
    _searches["n"] += 1
    if _searches["n"] % SEARCH_BREAK_EVERY == 0:
        print(f"  запросов к поиску {_searches['n']} — перерыв {SEARCH_BREAK_MIN:g} мин")
        time.sleep(SEARCH_BREAK_MIN * 60)
    time.sleep(random.uniform(0.8, 2.0))


def _facets(node, name: str, out: list | None = None) -> list[tuple[str, int]]:
    """Значения фасета (марки, модели) со счётчиками — где бы в дереве iNav он ни лежал."""
    out = [] if out is None else out
    if isinstance(node, dict):
        if node.get("Name") == name and isinstance(node.get("Facets"), list):
            for f in node["Facets"]:
                if isinstance(f, dict) and f.get("Value") and (f.get("Count") or 0) > 0:
                    out.append((str(f["Value"]), int(f.get("Count") or 0)))
        for v in node.values():
            _facets(v, name, out)
    elif isinstance(node, list):
        for v in node:
            _facets(v, name, out)
    return out


def _unique(pairs):
    seen, out = set(), []
    for value, count in pairs:
        if value not in seen:
            seen.add(value)
            out.append((value, count))
    return out


def _to_car(r: dict) -> dict | None:
    """Объявление из поиска API → машина в том же виде, что и карточка со страницы списка."""
    vid = str(r.get("Id") or "").strip()
    if not vid:
        return None
    title = " ".join(str(r.get(k) or "") for k in ("Manufacturer", "Model", "Badge", "BadgeDetail", "FuelType")).split()
    title = " ".join(title)
    year = None
    if str(r.get("FormYear") or "")[:4].isdigit():
        year = int(str(r["FormYear"])[:4])
    elif r.get("Year"):
        year = int(float(r["Year"])) // 100
    photo = str(r.get("Photo") or "")
    image = None
    if photo:
        image = ("https://ci.encar.com" + photo) if photo.startswith("/carpicture") else ("https://ci.encar.com/carpicture" + photo)
        if image.endswith("_"):
            image += "001.jpg"
    brand_en, model_guess, _ = extract_brand_model(title)
    price = r.get("Price")
    return {
        "external_id": vid,
        "power": selection.classify(selection.power_class(title), title=title),
        "brand": r.get("Manufacturer"),
        "model": r.get("Model"),
        "brand_en": brand_en,
        "model_guess": model_guess,
        "title": title,
        "year": year,
        "mileage_km": int(float(r["Mileage"])) if r.get("Mileage") is not None else None,
        "price_krw": int(float(price) * 10_000) if price else None,
        # Для шкалы цены: похожие — та же модель (с поколением), версия мотора (Badge) и год
        "similar": (r.get("Manufacturer"), r.get("Model"), r.get("Badge"), year),
        "image": image,
        "link": f"https://fem.encar.com/cars/detail/{vid}",
    }


COMMERCIAL = re.compile(r"포터|봉고|마이티|카운티|쏠라티|트럭|버스|라보|파맥스|메가|엑시언트|트라고|노부스|프리마|"
                        r"에어로|유니버스|그랜버드|porter|bongo|truck|bus", re.I)


# Разнообразие каталога: сколько объявлений модели на encar (массовость) — сначала самые массовые;
# имя модели на сайте ↔ модель encar (по машинам сайта, встреченным в поиске); лимиты — с сайта (/known)
POPULARITY = {}
GROUP_NAME = {}      # (марка, модель encar) → (марка, модель на сайте)
NAME_GROUP = {}      # (марка, модель на сайте) → (марка, модель encar)
MIX = {"limits": None}


def model_groups(session, save_debug) -> list[tuple[str, str, str, bool]]:
    """(CarType, марка, модель, фильтр по году понят) — все модели с MIN_YEAR года, самые массовые первыми."""
    out = []
    for cartype in ("Y", "N"):   # Y — корейские марки, N — импорт
        with_year = True
        data = _search(session, _query(cartype), inav=True)
        makers = _unique(_facets(data.get("iNav"), "Manufacturer"))
        if not makers:
            # Фильтр по году в таком виде не понят — берём все годы, год отсеем сами
            with_year = False
            _pause()
            data = _search(session, _query(cartype, with_year=False), inav=True)
            makers = _unique(_facets(data.get("iNav"), "Manufacturer"))
        print(f"encar {'корейские марки' if cartype == 'Y' else 'импорт'}: марок {len(makers)}"
              + ("" if with_year else " (без фильтра по году)"))
        if not makers:
            save_debug(f"search_inav_{cartype}.json", data)
        for maker, _ in makers:
            _pause()
            data = _search(session, _query(cartype, maker, with_year=with_year), inav=True)
            # Грузовики и автобусы не берём — только легковые, минивэны, пикапы
            groups = [(g, n) for g, n in _unique(_facets(data.get("iNav"), "ModelGroup")) if not COMMERCIAL.search(g)]
            out += [(cartype, maker, g, with_year) for g, _ in groups]
            POPULARITY.update({(maker, g): n for g, n in groups})
            print(f"  {maker}: моделей {len(groups)}")
    out.sort(key=lambda x: -POPULARITY.get((x[1], x[2]), 0))
    return out


# Класс мощности машин, чьи данные уже запрашивали у encar: id → класс (None — мощность не нашлась). Данные машины
# не меняются, поэтому повторно их не запрашиваем (раньше каждый прогон заново). Файл коммитится вместе с drom_cache.json
POWER_CACHE = Path(__file__).resolve().parent / "power_cache.json"


def _load_power_cache() -> dict:
    try:
        return json.loads(POWER_CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_power_cache(cache: dict) -> None:
    POWER_CACHE.write_text(json.dumps(cache, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def _resolver(session, known: dict, pacer, parse_encar_detail, power_of):
    cache = _load_power_cache()
    print(f"Кэш класса мощности encar: {len(cache)} машин (повторно не запрашиваем)")

    def resolve(car: dict):
        """Мощность машины без объёма в названии — по данным encar (или сайта, если машина там уже есть)."""
        info = known.get(car["external_id"])
        if info:
            return power_of(info.get("model"), f"{info.get('text') or ''} {car['title']}", info.get("cc"))
        key = str(car["external_id"])
        if key in cache:
            return cache[key]
        if pacer.blocked:
            return None
        detail = pacer.detail(session, car["external_id"])
        if not detail:
            return None
        d = parse_encar_detail(detail, car["external_id"])
        car["detail"] = d
        cls = power_of(d.get("model"), f"{d.get('power_text') or ''} {car['title']}", d.get("displacement"))
        cache[key] = cls
        _save_power_cache(cache)
        return cls

    return resolve


KINDS = ("le160", "gt160")


class MixGuard:
    """Лимиты разнообразия: не больше MIX["limits"] = [на модель-год, на модель] машин — вместе с теми,
    что уже на сайте (have_names — {(марка, модель на сайте, год): машин})."""

    def __init__(self, have_names: dict):
        self.limits = MIX["limits"] or [10 ** 6, 10 ** 6]
        self.year, self.model = {}, {}
        for (make, model, year), n in have_names.items():
            self.year[(make, model, year)] = self.year.get((make, model, year), 0) + n
            self.model[(make, model)] = self.model.get((make, model), 0) + n

    def _name(self, key):
        return GROUP_NAME.get(key) or key

    def allows(self, car, key) -> bool:
        name = self._name(key)
        return (self.year.get((*name, car.get("year")), 0) < self.limits[0]
                and self.model.get(name, 0) < self.limits[1])

    def site_year(self, car, key) -> int:
        return self.year.get((*self._name(key), car.get("year")), 0)

    def take(self, car, key):
        name = self._name(key)
        self.year[(*name, car.get("year"))] = self.year.get((*name, car.get("year")), 0) + 1
        self.model[name] = self.model.get(name, 0) + 1


def pick(groups: dict, total: int, share: float, resolve, on_site: dict | None = None, on_take=None,
         have: dict | None = None, have_names: dict | None = None, wanted=None) -> list[dict]:
    """По машине на модель, затем добор по кругу по моделям. Доли — share до 160 л.с. и доли лет
    selection.YEAR_BANDS — для каталога целиком (have — состав сайта, см. selection.run_wants);
    клетки «класс × годы» набираются вперемешку, чтобы и оборванный по времени прогон держал доли.

    Модели, которых на сайте меньше (on_site), идут первыми; по машине без очереди
    получают только модели, которых на сайте ещё нет.

    on_take(car) — сразу для каждой выбранной машины (отправка на сайт порциями по ходу
    отбора: отбор уточняет мощность по API в темпе человека и идёт долго). Через
    RUN_MINUTES от старта мощность больше не уточняется — отбор заканчивается."""
    on_site = on_site or {}
    # Сначала моделей, которых на сайте меньше, и среди них — самые массовые на encar
    order = sorted(groups, key=lambda k: (on_site.get(k, 0), -POPULARITY.get(k, 0), random.random()))
    groups = {k: groups[k] for k in order}
    picked, used, count, per_group, taken = [], set(), {}, {}, {}
    resolved = {"n": 0}
    mix = MixGuard(have_names or {})
    rank = {name: i for i, (name, *_) in enumerate(selection.YEAR_BANDS)}
    want = selection.run_wants(total, have or {}, share, KINDS)
    print("Нужно за прогон: " + ", ".join(f"{'до 160' if k == 'le160' else 'мощнее'} {b} — {v}"
                                            for (k, b), v in want.items()))

    def power(car, key):
        """Мощность; машины без объёма в названии уточняем по API (не больше RESOLVE_PER_MODEL на модель)."""
        if time.time() - STARTED > RUN_MINUTES * 60:
            return car.get("power")
        if car.get("power") is None and not car.get("_resolved") and per_group.get(key, 0) < RESOLVE_PER_MODEL:
            car["_resolved"] = True
            per_group[key] = per_group.get(key, 0) + 1
            resolved["n"] += 1
            car["power"] = resolve(car)
        return car.get("power")

    def fill_ratio(kind, car):
        cell = (kind, selection.year_band(car["year"]))
        return count.get(cell, 0) / want[cell] if want.get(cell) else 9

    def total_of(kind):
        return sum(v for (k, _), v in count.items() if k == kind)

    def kind_want(kind):
        return sum(v for (k, _), v in want.items() if k == kind)

    def take(car, key):
        mix.take(car, key)
        taken[(key, car["power"])] = taken.get((key, car["power"]), 0) + 1
        car["bucket"] = "le160" if car["power"] == "le160" else "other"
        picked.append(car)
        used.add(id(car))
        k = (car["power"], selection.year_band(car["year"]))
        count[k] = count.get(k, 0) + 1
        if on_take:
            on_take(car)

    for key, cars in groups.items():
        # Порядок внутри модели: сначала годы, которых на сайте нет (или меньше), среди них — самые
        # массовые на encar (больше объявлений этого года), потом по долям лет
        per_year = {}
        for c in cars:
            per_year[c["year"]] = per_year.get(c["year"], 0) + 1
        cars.sort(key=lambda c: (mix.site_year(c, key), -per_year.get(c["year"], 0), rank.get(selection.year_band(c["year"]), 9)))
        # …и годы по очереди: по машине каждого года, потом по второй — больше разных лет модели
        nth, seq = {}, []
        for c in cars:
            nth[c["year"]] = nth.get(c["year"], 0) + 1
            seq.append((nth[c["year"]], len(seq), c))
        cars[:] = [c for *_, c in sorted(seq, key=lambda x: (x[0], x[1]))]
    # Модели из справочника сайта, которых на сайте меньше 3 машин (wanted.py), — в первую очередь, до нужного
    # числа (не больше WANTED_SHARE прогона); мощность — как обычно (по названию или по API)
    from_ref = 0
    if wanted:
        cap = max(1, int(total * WANTED_SHARE))
        for key, cars in groups.items():
            if from_ref >= cap or len(picked) >= total:
                break
            make, model = GROUP_NAME.get(key) or (cars[0].get("brand_en"), cars[0].get("model_guess"))
            for c in cars:
                if wanted.need(make, model) <= 0 or from_ref >= cap or len(picked) >= total:
                    break
                if id(c) in used or not mix.allows(c, key):
                    continue
                if power(c, key) in KINDS:
                    take(c, key)
                    wanted.took(make, model)
                    from_ref += 1
        print(f"Из справочника сайта (моделей мало на сайте): {from_ref}")
    for key, cars in groups.items():
        # По машине — только моделям, которых на сайте ещё нет, и в пределах долей: класс и годы —
        # из клетки, набранной меньше всего; сначала машины с известной мощностью (без запроса к API)
        if on_site.get(key) or len(picked) >= total:
            continue
        for kind in sorted(KINDS, key=lambda k: total_of(k) / max(kind_want(k), 1)):
            fits = sorted((c for c in cars if id(c) not in used and fill_ratio(kind, c) < 1 and mix.allows(c, key)),
                          key=lambda c: fill_ratio(kind, c))
            best = (next((c for c in fits if c.get("power") == kind), None)
                    or next((c for c in fits if c.get("power") is None and power(c, key) == kind), None))
            if best:
                take(best, key)
                break
    covered = len(picked)

    def candidates(kind, band):
        """Следующая машина класса kind и лет band — по кругу моделей, по машине с модели за круг."""
        pos = {k: 0 for k in groups}
        progress = True
        while progress:
            progress = False
            for key, cars in groups.items():
                if taken.get((key, kind), 0) >= PER_MODEL_RUN[kind]:
                    continue
                i = pos[key]
                while i < len(cars):
                    c = cars[i]
                    i += 1
                    if id(c) in used or (band and selection.year_band(c["year"]) != band) or not mix.allows(c, key):
                        continue
                    if power(c, key) == kind:
                        pos[key] = i
                        progress = True
                        yield c, key
                        break
                pos[key] = i

    # Клетки вперемешку: каждый раз — та, что набрана меньше всего относительно нужного
    open_cells = {cell: candidates(*cell) for cell, v in want.items() if v > 0}
    while open_cells and len(picked) < total:
        cell = min(open_cells, key=lambda c: count.get(c, 0) / want[c])
        nxt = next(open_cells[cell], None) if count.get(cell, 0) < want[cell] else None
        if nxt is None:
            del open_cells[cell]
            continue
        take(*nxt)
    # Каких-то лет не хватило — добираем машинами тех же классов других лет, которых каталогу
    # ещё не хватает (переполненные годы — нет: иначе, когда время прогона вышло, сюда уходили
    # все машины с известной мощностью — 76 машин 2017–2021 при нужных 0)
    for kind in KINDS:
        more = candidates(kind, None)
        while len(picked) < total and total_of(kind) < kind_want(kind):
            nxt = next(more, None)
            if nxt is None:
                break
            if want.get((kind, selection.year_band(nxt[0]["year"]))):
                take(*nxt)
    years = {name: sum(v for (_, b), v in count.items() if b == name) for name, *_ in selection.YEAR_BANDS}
    print(f"Выбрано: новых моделей {covered} (на сайте нет {sum(1 for k in groups if not on_site.get(k))} из {len(groups)}), всего {len(picked)} — до 160 л.с. "
          f"{total_of('le160')}, мощнее {total_of('gt160')} (уточнено по API encar {resolved['n']}); по годам: "
          + ", ".join(f"{k} — {v}" for k, v in years.items()))
    return picked


MIN_SIMILAR = 4


def price_stats(prices: list) -> dict:
    """{n, lo, mid, hi}: от 10 цен — 10-й и 90-й процентили, иначе самая низкая и самая высокая."""
    p = sorted(prices)
    at = lambda q: p[round((len(p) - 1) * q)]
    wide = len(p) >= 10
    return {"n": len(p), "lo": at(0.1) if wide else p[0], "mid": at(0.5), "hi": at(0.9) if wide else p[-1]}


PRICES = {}   # группа похожих → цены объявлений encar (собираются при обходе моделей)


def similar_prices(group, year, badge=None) -> list:
    """Цены похожих объявлений: та же модель, версия мотора и год; мало (меньше MIN_SIMILAR) — шире:
    любая версия мотора, соседние годы (±1, потом ±2). group — (марка, модель encar)."""
    if not (group and year):
        return []
    levels = ([[("b", *group, badge, year)]] if badge else []) + [
        [("y", *group, year)],
        [("y", *group, year + d) for d in (-1, 0, 1)],
        [("y", *group, year + d) for d in range(-2, 3)],
    ]
    for keys in levels:
        prices = [p for k in keys for p in PRICES.get(k, [])]
        if len(prices) >= MIN_SIMILAR:
            return prices
    return []


def stats_key(group, year) -> str | None:
    return f"{group[0]}|{group[1]}|{year}" if group and year else None


def attach_price_stats(cars: list):
    """Шкала цены на сайте: цены похожих объявлений encar → car["price_stats"] (и stats_key — группа)."""
    for c in cars:
        if c.get("price_krw") and c.get("_group") and c.get("year"):
            g, badge = c["_group"], (c.get("similar") or (None,) * 3)[2]
            PRICES.setdefault(("b", *g, badge, c["year"]), []).append(c["price_krw"])
            PRICES.setdefault(("y", *g, c["year"]), []).append(c["price_krw"])
    done = 0
    for c in cars:
        prices = similar_prices(c.get("_group"), c.get("year"), (c.get("similar") or (None,) * 3)[2])
        if prices:
            c["price_stats"] = price_stats(prices)
            c["stats_key"] = stats_key(c["_group"], c["year"])
            done += 1
    print(f"Статистика цен: у {done} из {len(cars)} машин {MIN_SIMILAR}+ похожих объявлений")


STATS_DAYS = int(os.environ.get("ENCAR_STATS_DAYS") or "10")


def stale_stats(known_all: dict, seen: set) -> list[dict]:
    """Машины сайта, не встреченные в этом обходе, у которых шкалы нет или она старше STATS_DAYS дней, —
    свежая статистика по ценам, собранным при обходе (модель на сайте → модель encar)."""
    out, lack = [], 0
    for vid, i in known_all.items():
        if vid in seen or not i.get("published") or not i.get("url"):
            continue
        if i.get("has_gauge") and (i.get("stats_days") or 0) < STATS_DAYS and i.get("stats_days") is not None:
            continue
        group = NAME_GROUP.get((i.get("make"), i.get("model")))
        try:
            year = int(i.get("year") or 0)
        except ValueError:
            year = 0
        prices = similar_prices(group, year)
        if not prices:
            lack += 1
            continue
        out.append({"external_id": vid, "link": i["url"], "price_stats": price_stats(prices),
                    "stats_key": stats_key(group, year)})
    print(f"Шкала цены машинам сайта, не встреченным в обходе: обновлена у {len(out)}, нет похожих у {lack}")
    return out


def collect_all_models(session, known: dict, pacer, parse_encar_detail, power_of, save_debug,
                       total: int | None = None, on_take=None, touched_out: list | None = None) -> tuple[list[dict], list[dict]]:
    """(новые машины, машины с сайта, встреченные в поиске).

    known — машины с сайта с полной информацией: их заново не выбираем, только отмечаем
    «ещё в продаже». parse_encar_detail, power_of, save_debug — из scraper.py."""
    groups_list = model_groups(session, save_debug)
    if not groups_list:
        raise SearchBlocked("поиск encar не отдал список моделей")
    print(f"Моделей всего: {len(groups_list)} — по каждой смотрим до {PER_MODEL} объявлений")
    groups, seen, touched, on_site = {}, set(), [], {}
    stop = False
    for n, (cartype, maker, group, with_year) in enumerate(groups_list, 1):
        cars, on = [], 0
        for offset in range(0, PER_MODEL, PAGE):
            _pause()
            try:
                data = _search(session, _query(cartype, maker, group, with_year),
                               count=min(PAGE, PER_MODEL - offset), offset=offset)
            except SearchBlocked as error:
                print(f"Поиск encar закрылся ({error}) на модели {n}/{len(groups_list)} — выбираем из собранного")
                stop = True
                break
            except Exception as error:
                # Одна модель не ищется (HTTP 400 на особом названии, сбой сети) — пропускаем её,
                # а не бросаем весь перебор: раньше из-за одной модели Volkswagen прогон уходил
                # в медленный запасной путь (листать страницы сайта)
                print(f"  модель {group} ({maker}) не ищется: {str(error)[:120]} — пропускаем")
                break
            results = data.get("SearchResults") or []
            for r in results:
                car = _to_car(r)
                if not car or car["external_id"] in seen or not selection.eligible(car["brand_en"], car["year"]):
                    continue
                seen.add(car["external_id"])
                car["_group"] = (maker, group)
                if car["external_id"] in known:
                    touched.append(car)
                    on += 1
                    info = known.get(car["external_id"]) or {}
                    if info.get("make") and info.get("model"):
                        GROUP_NAME[(maker, group)] = (info["make"], info["model"])
                        NAME_GROUP[(info["make"], info["model"])] = (maker, group)
                else:
                    cars.append(car)
            if len(results) < min(PAGE, PER_MODEL - offset):
                break
        if cars:
            groups[(maker, group)] = cars
            on_site[(maker, group)] = on
        if stop:
            break
        if n % 50 == 0:
            print(f"  просмотрено моделей {n}/{len(groups_list)}, с новыми машинами {len(groups)}, "
                  f"машин с сайта встречено {len(touched)}")
    attach_price_stats(touched + [c for cars in groups.values() for c in cars])
    # Новые машины — только со шкалой цены продаж (4+ похожих объявлений encar): без неё на сайт не берём
    before = sum(map(len, groups.values()))
    groups = {k: [c for c in cars if c.get("price_stats")] for k, cars in groups.items()}
    groups = {k: cars for k, cars in groups.items() if cars}
    print(f"Новые машины со шкалой цены: {sum(map(len, groups.values()))} из {before}")
    share = float(os.environ.get("ENCAR_SHARE_160") or "0.35")
    total = sum(selection.QUOTAS.values()) if total is None else total
    if not total:
        return [], touched
    if touched_out is not None:
        touched_out.extend(touched)   # машины с сайта известны до отбора — если отбор оборвётся
    have = selection.catalog_have(known.values(), KINDS)
    print("На сайте по годам: " + ", ".join(f"{name} — {sum(v for (_, b), v in have.items() if b == name)}"
                                            for name, *_ in selection.YEAR_BANDS)
          + f"; до 160 л.с. {sum(v for (k, _), v in have.items() if k == 'le160')}, "
            f"мощнее {sum(v for (k, _), v in have.items() if k == 'gt160')}")
    have_names = {}
    for i in known.values():
        if i.get("published") and i.get("complete", True) and i.get("make"):
            k = (i["make"], i.get("model"), int(i["year"]) if str(i.get("year") or "").isdigit() else None)
            have_names[k] = have_names.get(k, 0) + 1
    wanted = wanted_mod.load(os.environ.get("BN_AUTO_URL", ""), os.environ.get("BN_AUTO_IMPORT_TOKEN", ""), "encar")
    return pick(groups, total, share, _resolver(session, known, pacer, parse_encar_detail, power_of), on_site,
                on_take, have, have_names, wanted=wanted), touched
