"""
Справочник Drom для парсеров BN-Auto (Корея, Китай, Япония).

Читает тот же файл, что и сайт: bn-auto/server/data/drom-index.json.gz (его собирает
scripts/build-drom-index.mjs). Логика поиска — порт dromMatch() из server/dromSpecs.js, чтобы
парсер и сайт всегда приходили к одной и той же мощности.

Формат: {"Рынок|марка|модель": [[год с, год по, объём см³, л.с., 30-мин мощность, суммарная мощность,
топливо, привод, комплектация], ...]}. Ключи — в нижнем регистре без пробелов и знаков (norm()).
Топливо: p — бензин, d — дизель, e — электро, h — гибрид, s — последовательный гибрид.

Подставляем только однозначное совпадение: подходят комплектации с разной мощностью — машину не
трогаем (сайт её всё равно не покажет). Мощность никогда не оценивается по названию.

Файл ищется так: переменная DROM_INDEX_PATH, затем drom-index.json.gz рядом с этим модулем,
затем ../bn-auto/server/data/drom-index.json.gz (если репозиторий bn-auto лежит рядом).

Проверка из cmd:  python drom_index.py --stats
                  python drom_index.py Корея Hyundai Grandeur 2020 petrol 2359
"""

import gzip
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MARKETS = ("Корея", "Япония", "Китай")
LE160 = "le160"
GT160 = "gt160"

# Хвосты названий кузова и версии, которых нет в названии модели у Drom: «Passat Variant», «Corolla Touring»
BODY_WORDS = re.compile(
    r"(variant|touring|hatchback|fastback|sportback|sedan|wagon|coupe|cabriolet|cabrio|hybrid|phev|ev|sport|avant|estate)$")

# Марка у площадки → какие марки есть у Drom (Renault в Корее у Drom — Renault Samsung, KGM — SsangYong и т. д.)
MAKE_ALIASES = {
    "renault": ["renaultsamsung", "renaultkorea", "renault"],
    "renaultsamsung": ["renaultsamsung", "renaultkorea", "renault"],
    "kgm": ["kgmobility", "ssangyong", "kgm"],
    "kgmobility": ["kgmobility", "ssangyong", "kgm"],
    "ssangyong": ["ssangyong", "kgmobility"],
    "chevrolet": ["chevrolet", "gmdaewoo", "daewoo"],
    "mercedes": ["mercedesbenz"],
    "benz": ["mercedesbenz"],
    "greatwall": ["greatwall", "haval"],
}

_FUEL_WORDS = (
    # (признак, код) — первый подошедший; порядок важен: «гибрид (бензин + электро)» — это гибрид, а не электро
    (("водород", "수소", "hydrogen"), "e"),
    (("гибрид", "하이브리드", "hybrid", "hev", "phev", "+전기", "плагин"), "h"),
    (("электро", "전기", "electric", "纯电"), "e"),
    (("дизел", "디젤", "diesel", "柴油"), "d"),
)


def norm(value) -> str:
    """Как norm() в dromSpecs.js: нижний регистр, только латиница, цифры и кириллица."""
    return re.sub(r"[^a-z0-9а-я]+", "", str(value or "").lower())


def fuel_code(text) -> str | None:
    """Топливо площадки → код справочника (p, d, e, h). None — топливо неизвестно."""
    low = str(text or "").strip().lower()
    if not low:
        return None
    if low in ("p", "d", "e", "h", "s"):
        return low
    for words, code in _FUEL_WORDS:
        if any(w in low for w in words):
            return code
    return "p"  # бензин, газ (LPG), бензин + газ — по мощности это бензиновый двигатель


def drive_name(text) -> str | None:
    """«полный»/«4wd»/«AWD» → «Полный» (как в справочнике); None — не определить."""
    low = str(text or "").strip().lower()
    if not low:
        return None
    if any(k in low for k in ("полн", "4wd", "awd", "四驱", "4х4", "4x4")):
        return "Полный"
    if any(k in low for k in ("задн", "rwd", "后驱")):
        return "Задний"
    if any(k in low for k in ("перед", "fwd", "前驱")):
        return "Передний"
    return None


def model_keys(make, model) -> list[str]:
    """Варианты ключа модели: как есть, с маркой впереди («Mazda 3» → mazda3), без хвоста кузова."""
    m = norm(model)
    keys = [m, norm(make) + m]
    cur = m
    for _ in range(2):
        if BODY_WORDS.search(cur) and len(cur) > 4:
            cur = BODY_WORDS.sub("", cur)
            keys += [cur, norm(make) + cur]
        else:
            break
    return list(dict.fromkeys(keys))


class DromIndex:
    def __init__(self, path: str | os.PathLike | None = None):
        self.path = Path(path) if path else find_index_path()
        with gzip.open(self.path, "rt", encoding="utf-8") as f:
            self.data = json.load(f)
        # Модели, которых нет в справочнике: «Корея|Hyundai|Foo» → сколько раз спросили (для доработки)
        self.missing: dict[str, int] = {}
        self.stats = {"ok": 0, "ambiguous": 0, "none": 0}

    # ---------- поиск ----------

    def rows(self, country: str, make, model) -> list | None:
        """Комплектации модели на рынке страны (рынки других стран не берём: спецификации отличаются)."""
        if country not in MARKETS:
            return None
        make_norm = norm(make)
        makes = list(dict.fromkeys(MAKE_ALIASES.get(make_norm, []) + [make_norm]))
        for mk in makes:
            for key in model_keys(mk, model):
                rows = self.data.get(f"{country}|{mk}|{key}")
                if rows:
                    return rows
        return None

    def _filter(self, rows, year, fuel, cc, drive, power):
        c = [r for r in rows
             if (not year or r[0] <= year <= r[1]) and (not fuel or r[6] == fuel or (fuel in "hs" and r[6] in "hs"))]
        if cc and fuel != "e":
            c = [r for r in c if r[2] and abs(r[2] - cc) <= 60]
        if drive and len(c) > 1:
            by_drive = [r for r in c if str(r[7]).lower() == drive.lower()]
            if by_drive:
                c = by_drive
        if power and len(c) > 1:
            by_power = [r for r in c if abs(r[3] - power) <= 2]
            if by_power:
                c = by_power
        return c

    def match(self, country: str, make, model, year=None, fuel=None, cc=None, drive=None, power=None) -> dict:
        """→ {"status": "none"|"ambiguous"|"ok", "n": комплектаций, и при ok: hp, power30, total, cc, drive}.
        Те же правила, что у dromMatch() на сайте: объём ±60 см³, однозначная мощность (hp, 30-мин, суммарная)."""
        fuel = fuel_code(fuel) or "p"
        rows = self.rows(country, make, model)
        if rows is None:
            self.missing[f"{country}|{make}|{model}"] = self.missing.get(f"{country}|{make}|{model}", 0) + 1
            self.stats["none"] += 1
            return {"status": "none", "n": 0, "reason": "model"}
        cc = int(cc) if cc else None
        c = self._filter(rows, year, fuel, cc, drive_name(drive), power)
        if not c:
            self.stats["none"] += 1
            return {"status": "none", "n": 0, "reason": "trim"}
        same = lambda i: all(r[i] == c[0][i] for r in c)
        if not c[0][3] or not same(3) or not same(4) or not same(5):
            self.stats["ambiguous"] += 1
            return {"status": "ambiguous", "n": len(c)}
        self.stats["ok"] += 1
        return {"status": "ok", "n": len(c), "hp": c[0][3], "power30": c[0][4] or None, "total": c[0][5] or None,
                "cc": c[0][2] or None if same(2) else None,
                "drive": c[0][7] if same(7) else None, "fuel": fuel}

    # ---------- класс мощности для квот ----------

    @staticmethod
    def _class(hp: int, fuel: str) -> str:
        # Электромобили — всегда «любой мощности» (так же их считает каталог сайта, см. selection.catalog_have)
        return GT160 if fuel == "e" or hp > 160 else LE160

    def power_class(self, country: str, make, model, year=None, fuel=None, cc=None, drive=None, power=None) -> str | None:
        """'le160' / 'gt160' / None. Строго: нужна однозначная мощность (как у сайта) — иначе None."""
        m = self.match(country, make, model, year, fuel, cc, drive, power)
        return self._class(m["hp"], m["fuel"]) if m["status"] == "ok" else None

    def power_band(self, country: str, make, model, year=None, fuel=None) -> str | None:
        """Предварительный класс по марке, модели, году и топливу (объёма ещё нет — карточку не открывали):
        все комплектации ≤160 л.с. → 'le160', все мощнее → 'gt160', разные → None (уточнит карточка)."""
        fuel = fuel_code(fuel)
        rows = self.rows(country, make, model)
        if rows is None:
            return None
        c = self._filter(rows, year, fuel, None, None, None)
        if not c:
            return None
        if fuel == "e" or all(r[6] == "e" for r in c):
            return GT160
        classes = {self._class(r[3], r[6]) for r in c if r[3]}
        return classes.pop() if len(classes) == 1 else None

    def has_model(self, country: str, make, model) -> bool:
        return self.rows(country, make, model) is not None


_cache: dict[str, DromIndex] = {}


def find_index_path() -> Path:
    candidates = []
    if os.environ.get("DROM_INDEX_PATH"):
        candidates.append(Path(os.environ["DROM_INDEX_PATH"]))
    candidates += [HERE / "drom-index.json.gz", HERE.parent / "drom-index.json.gz",
                   HERE.parent.parent / "bn-auto" / "server" / "data" / "drom-index.json.gz"]
    for p in candidates:
        if p.is_file():
            return p
    raise FileNotFoundError("drom-index.json.gz не найден: положите файл рядом со скриптом или задайте DROM_INDEX_PATH "
                            "(файл лежит в репозитории bn-auto: server/data/drom-index.json.gz)")


def load(path=None) -> DromIndex:
    key = str(path or "")
    if key not in _cache:
        _cache[key] = DromIndex(path)
    return _cache[key]


def _main(argv):
    index = load()
    if not argv or argv[0] == "--stats":
        per = {}
        for key, rows in index.data.items():
            market = key.split("|")[0]
            s = per.setdefault(market, {"моделей": 0, "комплектаций": 0, "до 160 л.с.": 0})
            s["моделей"] += 1
            s["комплектаций"] += len(rows)
            s["до 160 л.с."] += sum(1 for r in rows if r[3] and r[3] <= 160 and r[6] != "e")
        print(f"Справочник: {index.path}")
        for market, s in per.items():
            print(f"  {market}: {s}")
        return
    country, make, model, year, fuel, *rest = argv
    cc = int(rest[0]) if rest else None
    print(json.dumps(index.match(country, make, model, int(year), fuel, cc), ensure_ascii=False))


if __name__ == "__main__":
    _main(sys.argv[1:])
