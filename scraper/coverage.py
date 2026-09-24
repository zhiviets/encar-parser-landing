"""
Все модели encar: хотя бы по одной машине каждой модели с MIN_YEAR года.

Марки и модели берём из фасетов поиска API encar — тех же счётчиков, из
которых сайт строит фильтры «제조사 → 모델». По каждой модели запрашиваем
ENCAR_PER_MODEL свежих объявлений (одним запросом) и выбираем:
  1) по машине на модель — до 160 л.с., если такая есть;
  2) добор до ENCAR_TOTAL так, чтобы машин до 160 л.с. было не меньше 75%,
     а по годам — 70% 2022–2024, 15% 2025–2026, 15% 2017–2021.
Если моделей мощнее 160 л.с. слишком много, «до 160» добираются сверх
ENCAR_TOTAL — доля важнее общего числа.
"""

import math
import os
import random
import time
from urllib.parse import quote

import selection
from brand_map import extract_brand_model

SEARCH_API = "https://api.encar.com/search/car/list/general"
PER_MODEL = int(os.environ.get("ENCAR_PER_MODEL") or "30")
# Сколько машин модели без объёма в названии уточнять по API при выборе «до 160»
RESOLVE_PER_MODEL = 6
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


def _search(session, q: str, count: int = 0, inav: bool = False) -> dict:
    url = (f"{SEARCH_API}?count=true&q={quote(q, safe='(),._')}"
           f"&sr={quote(f'|ModifiedDate|0|{count}', safe='|')}")
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
SEARCH_BREAK_EVERY = 100
SEARCH_BREAK_MIN = float(os.environ.get("ENCAR_SEARCH_BREAK") or "3")
_searches = {"n": 0}


def _pause():
    _searches["n"] += 1
    if _searches["n"] % SEARCH_BREAK_EVERY == 0:
        print(f"  запросов к поиску {_searches['n']} — перерыв {SEARCH_BREAK_MIN:g} мин")
        time.sleep(SEARCH_BREAK_MIN * 60)
    time.sleep(random.uniform(1.0, 2.5))


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
        "image": image,
        "link": f"https://fem.encar.com/cars/detail/{vid}",
    }


def model_groups(session, save_debug) -> list[tuple[str, str, str, bool]]:
    """(CarType, марка, модель, фильтр по году понят) — все модели с MIN_YEAR года."""
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
            groups = _unique(_facets(data.get("iNav"), "ModelGroup"))
            out += [(cartype, maker, g, with_year) for g, _ in groups]
            print(f"  {maker}: моделей {len(groups)}")
    return out


def _resolver(session, known: dict, pacer, parse_encar_detail, power_of):
    def resolve(car: dict):
        """Мощность машины без объёма в названии — по данным encar (или сайта, если машина там уже есть)."""
        info = known.get(car["external_id"])
        if info:
            return power_of(info.get("model"), f"{info.get('text') or ''} {car['title']}", info.get("cc"))
        if pacer.blocked:
            return None
        detail = pacer.detail(session, car["external_id"])
        if not detail:
            return None
        d = parse_encar_detail(detail, car["external_id"])
        car["detail"] = d
        return power_of(d.get("model"), f"{d.get('power_text') or ''} {car['title']}", d.get("displacement"))

    return resolve


def pick(groups: dict, total: int, share: float, resolve) -> list[dict]:
    """По машине на модель, затем добор по кругу по моделям: «до 160» — пока их не
    станет share, мощных — пока их не больше остального; внутри каждой группы —
    по долям лет выпуска selection.YEAR_BANDS (70% 2022–2024 и т.д.)."""
    picked, used = [], set()
    count = {}
    resolved = {"n": 0}
    per_group = {}
    rank = {name: i for i, (name, *_) in enumerate(selection.YEAR_BANDS)}

    def power(car, key):
        """Мощность; машины без объёма в названии уточняем по API (не больше RESOLVE_PER_MODEL на модель)."""
        if car.get("power") is None and not car.get("_resolved") and per_group.get(key, 0) < RESOLVE_PER_MODEL:
            car["_resolved"] = True
            per_group[key] = per_group.get(key, 0) + 1
            resolved["n"] += 1
            car["power"] = resolve(car)
        return car.get("power")

    def take(car):
        car["bucket"] = "le160" if car["power"] == "le160" else "other"
        picked.append(car)
        used.add(id(car))
        k = (car["power"], selection.year_band(car["year"]))
        count[k] = count.get(k, 0) + 1

    def total_of(kind):
        return sum(v for (k, _), v in count.items() if k == kind)

    for key, cars in groups.items():
        # Порядок внутри модели: сначала 2022–2024, потом 2025–2026, потом старше; уже на сайте — первыми
        cars.sort(key=lambda c: rank.get(selection.year_band(c["year"]), 9))
        best = (next((c for c in cars if c.get("power") == "le160"), None)
                or next((c for c in cars if power(c, key) == "le160"), None)
                or next((c for c in cars if c.get("power") == "gt160"), None))
        if best:
            take(best)
    covered = len(picked)

    def fill(kind, want_band, need):
        pos = {k: 0 for k in groups}
        progress = True
        while need() and progress:
            progress = False
            for key, cars in groups.items():
                if not need():
                    break
                i = pos[key]
                while i < len(cars):
                    c = cars[i]
                    i += 1
                    if id(c) in used or (want_band and selection.year_band(c["year"]) != want_band):
                        continue
                    if power(c, key) == kind:
                        take(c)
                        progress = True
                        break
                pos[key] = i

    def fill_kind(kind, target):
        for name, _, _, w in selection.YEAR_BANDS:
            want = round(target * w)
            fill(kind, name, lambda: count.get((kind, name), 0) < want and total_of(kind) < target)
        fill(kind, None, lambda: total_of(kind) < target)   # в какой-то группе лет машин не хватило

    ratio = (1 - share) / share if share else 0
    fill_kind("le160", max(round(total * share), math.ceil(total_of("gt160") / ratio) if ratio else 0))
    fill_kind("gt160", min(total - total_of("le160"), math.floor(total_of("le160") * ratio)))
    years = {name: sum(v for (_, b), v in count.items() if b == name) for name, *_ in selection.YEAR_BANDS}
    print(f"Выбрано: моделей с машиной {covered} из {len(groups)}, всего {len(picked)} — до 160 л.с. "
          f"{total_of('le160')}, мощнее {total_of('gt160')} (уточнено по API encar {resolved['n']}); по годам: "
          + ", ".join(f"{k} — {v}" for k, v in years.items()))
    return picked


def collect_all_models(session, known: dict, pacer, parse_encar_detail, power_of, save_debug) -> list[dict]:
    """parse_encar_detail, power_of, save_debug — из scraper.py (он запускается как __main__)."""
    groups_list = model_groups(session, save_debug)
    if not groups_list:
        raise SearchBlocked("поиск encar не отдал список моделей")
    print(f"Моделей всего: {len(groups_list)} — по каждой смотрим до {PER_MODEL} объявлений")
    groups, seen = {}, set()
    for n, (cartype, maker, group, with_year) in enumerate(groups_list, 1):
        _pause()
        try:
            data = _search(session, _query(cartype, maker, group, with_year), count=PER_MODEL)
        except SearchBlocked as error:
            print(f"Поиск encar закрылся ({error}) на модели {n}/{len(groups_list)} — выбираем из собранного")
            break
        cars = []
        for r in data.get("SearchResults") or []:
            car = _to_car(r)
            if not car or car["external_id"] in seen or not selection.eligible(car["brand_en"], car["year"]):
                continue
            seen.add(car["external_id"])
            cars.append(car)
        if cars:
            # Машины, которые уже на сайте, — первыми (в своей группе лет, см. pick):
            # иначе каждый прогон брал бы новые объявления и сайт разрастался бы
            cars.sort(key=lambda c: c["external_id"] not in known)
            groups[(maker, group)] = cars
        if n % 50 == 0:
            print(f"  просмотрено моделей {n}/{len(groups_list)}, с подходящими машинами {len(groups)}")
    share = float(os.environ.get("ENCAR_SHARE_160") or "0.75")
    total = sum(selection.QUOTAS.values())
    return pick(groups, total, share, _resolver(session, known, pacer, parse_encar_detail, power_of))
