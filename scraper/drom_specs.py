"""
Точная мощность из каталога drom.ru.

Машина (марка, модель, рынок, год, объём, топливо, привод, КПП, название
комплектации) сопоставляется с группой комплектаций на странице поколения
drom.ru: «1.5 л, бензин, 106 л.с., параллельный гибрид, 122 л.с., вариатор
(CVT), полный привод (4WD)». Мощность берётся, только если совпадение
однозначное: все подходящие группы дают одну мощность — или их различило
название комплектации. Иначе — None (сайт оставит оценку со знаком «≈»).

Страницы открываются в браузере (таблицы комплектаций drom.ru дорисовывает
скриптом) и запоминаются в кэше (drom_cache.json в репозитории): страница
поколения не меняется, список поколений модели перечитывается раз в месяц.
Этот файл одинаковый в dongchedi_parser и encar-parser-landing.
"""

from contextlib import contextmanager
import json
import random
import re
import time
from datetime import date

BASE = "https://www.drom.ru/catalog/"
MARKETS = {"china": "Китай", "south-korea": "Южная Корея", "japan": "Япония", "europe": "Европа", "usa": "США"}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")
GEN_LIST_TTL_DAYS = 30
# Версия разбора: при повышении страницы, разобранные старым разбором впустую (поколения без групп,
# списки моделей марок), перечитываются
CACHE_VERSION = 3

# Марки, которые на drom.ru называются иначе, чем у нас (остальные ищутся по названию
# на странице каталога drom.ru)
BRAND_ALIASES = {
    "mercedes-benz": "mercedes-benz", "land rover": "land_rover", "li auto": "lixiang", "lynk & co": "lynk_co",
    "baic bj": "baic", "great wall": "great_wall", "rolls-royce": "rolls-royce", "aston martin": "aston_martin",
    "alfa romeo": "alfa_romeo", "chery fengyun": "chery", "fangchengbao": "fangchengbao", "trumpchi": "gac",
    # Названия encar
    "renault-koreasamsung": "renault_samsung", "renault samsung": "renault_samsung",
    "kg_mobility_ssangyong": "ssang_yong", "kg mobility": "ssang_yong", "kgm": "ssang_yong", "ssangyong": "ssang_yong",
    "citroen-ds": "citroen",
}
# Модели марки бывают и под другим названием марки на drom.ru: Renault Samsung — и Renault
# (Arkana, Grand Koleos), SsangYong — и KG Mobility (Torres), Citroen — и DS
BRAND_FALLBACK = {"renault_samsung": ["renault"], "ssang_yong": ["kg_mobility"], "citroen": ["ds"]}
# Модели, которые на drom.ru называются иначе: (марка, модель) → адрес модели на drom.ru
MODEL_ALIASES = {
    ("geely", "xingyue l"): "geely/monjaro",
    ("geely", "monjaro"): "geely/monjaro",
    ("mercedes-benz", "c-class"): "mercedes-benz/c-class",
    ("mercedes-benz", "e-class"): "mercedes-benz/e-class",
    ("mercedes-benz", "s-class"): "mercedes-benz/s-class",
    ("mercedes-benz", "g-class"): "mercedes-benz/g-class",
    ("bmw", "3 series"): "bmw/3-series",
    ("bmw", "5 series"): "bmw/5-series",
    ("bmw", "7 series"): "bmw/7-series",
    ("toyota", "land cruiser prado"): "toyota/land_cruiser_prado",
    ("hyundai", "santa fe"): "hyundai/santa_fe",
    ("hyundai", "santafe"): "hyundai/santa_fe",
    ("tesla", "model 3"): "tesla/model_3",
    ("tesla", "model y"): "tesla/model_y",
    ("kg_mobility_ssangyong", "tiboli"): "ssang_yong/tivoli",
    ("volkswagen", "beatle"): "volkswagen/beetle",
    ("hyundai", "maxcruz"): "hyundai/maxcruze",
    ("chevrolet", "surburban"): "chevrolet/suburban",
    ("mini", "cooper"): "mini/hatch",
    ("mini", "cooper convertible"): "mini/cabrio",
    ("mini", "coupe"): "mini/coupe-model",
    ("mini", "mini"): "mini/hatch",
    # goo-net пишет иначе, чем drom.ru
    ("honda", "stepwagon"): "honda/stepwgn",
    ("honda", "stepwagon spada"): "honda/stepwgn",
    ("honda", "honda e"): "honda/e",
    ("toyota", "86"): "toyota/gt_86",
    ("toyota", "grmn yaris"): "toyota/gr_yaris",
    ("toyota", "avevsis wagon"): "toyota/avensis",
    ("toyota", "avevsis"): "toyota/avensis",
    # goo-net: фургон и универсал NV200 — «NV200VANETTE VAN/WAGON», на drom.ru — «NV200»
    ("nissan", "nv200vanette"): "nissan/nv200",
    ("nissan", "nv200vanette van"): "nissan/nv200",
    ("nissan", "nv200vanette wagon"): "nissan/nv200",
}

# Хвосты названия модели, без которых модель ищется на drom.ru («Crown Hybrid» → «Crown»)
_MODEL_TAILS = {"hybrid", "custom", "phv", "phev", "plugin", "epower", "ev", "gr", "sport", "sports", "turbo", "diesel",
                "wagon", "van", "touring", "cross", "hv",
                # goo-net пишет кузов в названии модели: «Hijet Truck», «Carry Track», «Hijet Cargo»
                "truck", "track", "cargo", "bus", "cabriolet"}

JAPANESE_MAKES = {"toyota", "lexus", "nissan", "honda", "mazda", "subaru", "mitsubishi", "suzuki", "daihatsu"}
AWD_WORDS = {"xdrive", "4matic", "quattro", "4motion"}

LEVELS = {"Базовая", "Предмаксимальная", "Максимальная", "Средняя", "Спортивная", "Оптимальная", "Комфорт"}
PERIOD_RE = re.compile(r"^(\d{2})\.(\d{4})\s*-\s*(?:(\d{2})\.(\d{4})|н\.в\.)$")
HP_RE = re.compile(r"^(\d{2,4})\s*л\.с\.$")


def _norm(text: str) -> str:
    return re.sub(r"[\s_\-·.()（）]", "", (text or "").lower())


# ---------- разбор страниц drom.ru ----------

def text_lines(text: str) -> list[str]:
    """Текст страницы из браузера (page.inner_text) → строки; ячейки таблицы — отдельными строками."""
    return [cell.strip() for line in (text or "").splitlines() for cell in line.split("\t") if cell.strip()]


# Электромобили — без объёма, мощность первой: «170 л.с., электричество, редуктор, задний привод»
HEADER_START = re.compile(r"^(\d+(?:\.\d)?\s*л,|\d{2,4}\s*л\.с\.,\s*электр|электр)", re.I)


def parse_header(text: str) -> dict | None:
    """«1.5 л, бензин, 106 л.с., параллельный гибрид, 122 л.с., вариатор (CVT), полный привод (4WD)»."""
    if "л.с." not in text or "привод" not in text or not HEADER_START.match(text) or len(text) > 200:
        return None
    g = {"text": text, "liters": None, "fuel": None, "hp": None, "hybrid": None, "hp_total": None,
         "trans": None, "drive": None}
    after_hybrid = False
    for tok in (t.strip() for t in text.split(",")):
        low = tok.lower()
        m = re.match(r"^(\d+(?:\.\d)?)\s*л$", low)
        if m:
            g["liters"] = float(m.group(1))
        elif HP_RE.match(low):
            hp = int(HP_RE.match(low).group(1))
            if after_hybrid and g["hp"] is not None:
                # После «гибрид» бывает и суммарная мощность (Honda Fit: 106, суммарная 122),
                # и мощность одного электромотора (Tucson: 180 и 60) — суммарная больше ДВС
                g["hp_total"] = hp if hp > g["hp"] else None
            elif g["hp"] is None:
                g["hp"] = hp
        elif "гибрид" in low:
            g["hybrid"] = low
            after_hybrid = True
        elif "привод" in low:
            g["drive"] = "4wd" if ("полный" in low or "4wd" in low) else "rwd" if "задний" in low else "fwd"
        elif low in ("бензин", "дизель", "электро", "газ", "газ/бензин") or "электр" in low:
            g["fuel"] = "electric" if "электр" in low else low
        elif any(k in low for k in ("акпп", "мкпп", "вариатор", "робот", "редуктор", "cvt", "dct")):
            g["trans"] = ("cvt" if ("вариатор" in low or "cvt" in low) else "manual" if "мкпп" in low
                          else "robot" if ("робот" in low or "dct" in low) else "reducer" if "редуктор" in low
                          else "auto")
    return g if g["hp"] else None


def _ym(month: str, year: str):
    return int(year) * 100 + int(month)


def trim_links(html: str) -> dict:
    """Ссылки на страницы комплектаций на странице поколения: название комплектации → номер (441739)."""
    out = {}
    for num, inner in re.findall(r'href="(?:https://www\.drom\.ru)?/catalog/[a-z0-9_\-~]+/[a-z0-9_\-~]+/(\d{4,})/"[^>]*>(.*?)</a>',
                                 html or "", re.S):
        name = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", inner)).strip()
        if name:
            out.setdefault(name, num)
    return out


def parse_generation(page_text: str, html: str | None = None) -> dict:
    """Поколение (текст страницы из браузера): рынок, период выпуска и группы комплектаций с мощностью.
    Таблицу комплектаций drom.ru дорисовывает скриптом — в HTML простого запроса её нет."""
    lines = text_lines(page_text)
    text = "\n".join(lines)
    market = re.search(r"Рынок сбыта:\s*([^.\n]+)", text)
    period = re.search(r"\((\d{2})\.(\d{4})\s*-\s*(?:(\d{2})\.(\d{4})|н\.в\.)\)", text)
    gen = {"market": market.group(1).strip() if market else None,
           "from": _ym(period.group(1), period.group(2)) if period else None,
           "to": _ym(period.group(3), period.group(4)) if period and period.group(3) else None,
           "groups": []}
    group = None
    for i, ln in enumerate(lines):
        header = parse_header(ln)
        if header:
            group = {**header, "engine": None, "body": [], "trims": []}
            gen["groups"].append(group)
            continue
        if group is None:
            continue
        if ln.startswith("Сравнение ") or ln.startswith("Отзывы "):
            group = None
            continue
        if ln == "Двигатель:" and i + 1 < len(lines):
            group["engine"] = lines[i + 1]
        elif ln == "Кузов:":
            j = i + 1
            while j < len(lines):
                parts = [x.strip() for x in lines[j].split(",") if x.strip()]
                if not parts or not all(re.match(r"^[A-Z0-9][A-Z0-9\-]{1,14}$", x) for x in parts):
                    break
                group["body"] += parts
                j += 1
        m = PERIOD_RE.match(ln)
        if m:
            name = lines[i - 1] if lines[i - 1] not in LEVELS else lines[i - 2]
            group["trims"].append({"name": name, "from": _ym(m.group(1), m.group(2)),
                                   "to": _ym(m.group(3), m.group(4)) if m.group(3) else None})
    if html is not None:
        ids = trim_links(html)
        for g in gen["groups"]:
            for t in g["trims"]:
                if ids.get(t["name"]):
                    t["id"] = ids[t["name"]]
        gen["ids"] = True
    return gen


# Строки страницы комплектации drom.ru → (раздел, название на сайте, единица)
TECH_FIELDS = [
    ("Динамика", "Время разгона 0-100 км/ч, с", "Разгон 0–100 км/ч", "с"),
    ("Динамика", "Максимальная скорость, км/ч", "Максимальная скорость", "км/ч"),
    ("Двигатель", "Максимальная мощность, л.с. (кВт) при об./мин.", "Мощность", "power"),
    ("Двигатель", "Максимальный крутящий момент, Н*м (кг*м) при об./мин.", "Крутящий момент", "torque"),
    ("Двигатель", "Марка двигателя", "Модель двигателя", ""),
    ("Двигатель", "Тип двигателя", "Тип двигателя", ""),
    ("Двигатель", "Нагнетатель", "Наддув", ""),
    ("Двигатель", "Используемое топливо", "Топливо", ""),
    ("Двигатель", "Экологический тип двигателя", "Экокласс", ""),
    # Электромобили и гибриды: 30-минутная мощность — по ней считается утильсбор (пиковая выше)
    ("Электро", "Электродвигатель: 30-минутная мощность, л.с.", "30-минутная мощность", "л.с."),
    ("Электро", "Электродвигатель: мощность, кВт", "Мощность электромотора", "кВт"),
    ("Электро", "Электродвигатель: крутящий момент, Нм", "Крутящий момент электромотора", "Н·м"),
    ("Электро", "Ёмкость батареи, кВт*ч", "Ёмкость батареи", "кВт·ч"),
    ("Электро", "Запас хода на электротяге в км", "Запас хода", "км"),
    ("Электро", "Запас хода на электротяге, км", "Запас хода", "км"),
    ("Электро", "Запас хода, км", "Запас хода", "км"),
    ("Электро", "Максимальная мощность быстрой зарядки, кВт", "Быстрая зарядка", "кВт"),
    ("Электро", "Максимальная мощность медленной зарядки, кВт", "Медленная зарядка", "кВт"),
    ("Расход топлива", "Расход топлива в смешанном цикле, л/100 км", "Смешанный цикл", "л/100 км"),
    ("Расход топлива", "Расход топлива в городском цикле, л/100 км", "Город", "л/100 км"),
    ("Расход топлива", "Расход топлива за городом, л/100 км", "Трасса", "л/100 км"),
    ("Размеры и масса", "Габариты кузова (Д x Ш x В), мм", "Габариты (Д × Ш × В)", "мм"),
    ("Размеры и масса", "Колесная база, мм", "Колёсная база", "мм"),
    ("Размеры и масса", "Клиренс (высота дорожного просвета), мм", "Клиренс", "мм"),
    ("Размеры и масса", "Масса, кг", "Масса", "кг"),
    ("Размеры и масса", "Объем топливного бака, л", "Топливный бак", "л"),
    ("Размеры и масса", "Объем багажника, л", "Багажник", "л"),
    ("Размеры и масса", "Число мест", "Мест", ""),
    ("Ходовая часть", "Передняя подвеска", "Передняя подвеска", ""),
    ("Ходовая часть", "Задняя подвеска", "Задняя подвеска", ""),
    ("Ходовая часть", "Передние тормоза", "Передние тормоза", ""),
    ("Ходовая часть", "Задние тормоза", "Задние тормоза", ""),
    ("Ходовая часть", "Передние колеса", "Шины", ""),
    ("Ходовая часть", "Минимальный радиус разворота, м", "Радиус разворота", "м"),
]
def _label_key(text: str) -> str:
    """Название строки для сравнения: без регистра, «ё» как «е», без лишних пробелов и двоеточия в конце."""
    return re.sub(r"\s+", " ", (text or "").lower().replace("ё", "е")).strip().rstrip(":").strip()


_TECH_LABELS = {_label_key(src): (group, name, unit) for group, src, name, unit in TECH_FIELDS}
TRIM_VERSION = 2   # 2 — строки электромобиля (30-минутная мощность и др.); старые записи кэша перечитываются


def parse_trim(page_text: str) -> dict | None:
    """Страница комплектации: {"name": «1.6 G CVT Smart», "groups": [[раздел, [[название, значение], …]], …]}."""
    lines = text_lines(page_text)
    name = next((lines[i + 1] for i, ln in enumerate(lines[:-1]) if ln == "Название комплектации"), None)
    got = {}
    for i, ln in enumerate(lines[:-1]):
        ln = _label_key(ln)
        if ln not in _TECH_LABELS or ln in got:
            continue
        value = lines[i + 1].strip()
        if not value or value == "—" or _label_key(value) in _TECH_LABELS or len(value) > 120:
            continue
        group, label, unit = _TECH_LABELS[ln]
        m = re.fullmatch(r"([\d.,]+)\s*\(([\d.,]+)\)\s*(?:/\s*([\d\s–-]+))?", value)
        if unit in ("power", "torque"):
            if m:
                a, b, rpm = m.group(1), m.group(2), (m.group(3) or "").strip()
                value = (f"{a} л.с. ({b} кВт)" if unit == "power" else f"{a} Н·м") + (f" при {rpm} об/мин" if rpm else "")
            got[ln] = (group, label, value)
            continue
        if " x " in value:
            value = value.replace(" x ", " × ")
        if unit and not value.endswith(unit):
            value = f"{value.replace('.', ',')} {unit}" if re.fullmatch(r"[\d.,]+", value) else f"{value} {unit}"
        got[ln] = (group, label, value)
    if len(got) < 3:
        return None
    groups = []
    for group, src, _, _ in TECH_FIELDS:
        if _label_key(src) in got:
            g, label, value = got[_label_key(src)]
            if not groups or groups[-1][0] != g:
                groups.append([g, []])
            if label not in [r[0] for r in groups[-1][1]]:
                groups[-1][1].append([label, value])
    return {"name": name, "groups": groups, "v": TRIM_VERSION}


# ---------- каталог с кэшем ----------

def playwright_fetcher(browser_context):
    """fetch(url) → (html, текст страницы) через браузер: drom.ru дорисовывает таблицы скриптом."""
    page = browser_context.new_page()

    def fetch(url):
        resp = page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        if resp is not None and resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}")
        page.wait_for_timeout(2500)
        return page.content(), page.inner_text("body")

    return fetch


class DromCatalog:
    def __init__(self, cache_path: str, fetch, max_requests: int = 300, log=print):
        """fetch(url) → (html, текст страницы) — см. playwright_fetcher()."""
        self.path = cache_path
        self.fetch = fetch
        self.log = log
        self.max_requests = max_requests
        self.requests = 0
        try:
            with open(cache_path, encoding="utf-8") as f:
                self.cache = json.load(f)
        except (OSError, ValueError):
            self.cache = {}
        for key in ("brands", "models", "gen_lists", "gens", "trims", "auctions"):
            self.cache.setdefault(key, {})
        version = self.cache.get("version", 1)
        if version < 2:
            self.cache["models"] = {}       # старый разбор терял ссылки на модели с разметкой внутри
        if version < 3:
            # и группы электромобилей («170 л.с., электричество, …») — такие поколения были пустыми
            self.cache["gens"] = {k: g for k, g in self.cache["gens"].items() if g.get("groups")}
        self.cache["version"] = CACHE_VERSION
        self.stats = {"exact": 0, "by_trim": 0, "ambiguous": 0, "no_model": 0, "no_match": 0}
        self.missing = {}   # модели, которых не нашлось на drom.ru: «марка модель» → сколько раз

    def save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.cache, f, ensure_ascii=False, indent=0, sort_keys=True)

    def _get(self, url: str):
        """(html, текст) или None; не больше max_requests страниц за прогон."""
        if self.requests >= self.max_requests:
            return None
        self.requests += 1
        time.sleep(random.uniform(2.0, 4.0))
        try:
            return self.fetch(url)
        except Exception as error:
            message = str(error).splitlines()[0][:120]
            if "HTTP 404" in message:
                # Страницы нет (например, рынка «Южная Корея» у импортной модели) — запоминаем
                # пустой: в кэше она больше не открывается
                return "", ""
            self.log(f"  drom.ru: {url} — {message}")
            return None

    def _links(self, html: str, prefix: str) -> dict:
        """Ссылки вида prefix<slug>/ с текстом: нормализованное название → slug."""
        out = {}
        for slug, name in re.findall(rf'href="(?:https://www\.drom\.ru)?/catalog/{re.escape(prefix)}([a-z0-9_\-~]+)/"[^>]*>([^<]{{1,60}})</a>', html):
            out.setdefault(_norm(name), slug)
            out.setdefault(_norm(slug), slug)
        # Ссылки, у которых внутри разметка (картинка, <span>), — хотя бы по адресу
        for slug in re.findall(rf'href="(?:https://www\.drom\.ru)?/catalog/{re.escape(prefix)}([a-z0-9_\-~]+)/"', html):
            out.setdefault(_norm(slug), slug)
        return out

    def brand_slug(self, make: str) -> str | None:
        key = _norm(make)
        if make.lower() in BRAND_ALIASES:
            return BRAND_ALIASES[make.lower()]
        if not self.cache["brands"]:
            got = self._get(BASE)
            if got:
                self.cache["brands"] = self._links(got[0], "")
        return self.cache["brands"].get(key)

    def model_path(self, make: str, model: str) -> str | None:
        alias = MODEL_ALIASES.get((make.lower(), model.lower()))
        if alias:
            return alias
        brand = self.brand_slug(make)
        if not brand:
            return None
        # «X2 (F39)» → «X2»; «4-Series» и «4 Series» — одно. Нет такой модели — без хвоста версии:
        # «Crown Hybrid» → «Crown», «N-BOX Custom» → «N-BOX» (goo-net пишет версию в названии модели)
        words = re.sub(r"\s*\(.*?\)", "", model).split()
        names = [_norm(" ".join(words))]
        while len(words) > 1 and _norm(words[-1]) in _MODEL_TAILS:
            words = words[:-1]
            names.append(_norm(" ".join(words)))
            alias = MODEL_ALIASES.get((make.lower(), " ".join(words).lower()))
            if alias:
                return alias
        for b in [brand] + BRAND_FALLBACK.get(brand, []):
            if b not in self.cache["models"]:
                got = self._get(f"{BASE}{b}/")
                if got is None:
                    continue
                self.cache["models"][b] = self._links(got[0], f"{b}/")
            slug = next((self.cache["models"][b][n] for n in names if n in self.cache["models"][b]), None)
            if not slug:
                # Версия модели в названии без пробела или с лишним словом: «Crown Athlete», «Corollasport» —
                # самая длинная модель drom.ru, с которой начинается название (не короче 4 букв)
                full = names[0]
                prefix = max((k for k in self.cache["models"][b] if len(k) >= 4 and full.startswith(k)), key=len, default=None)
                slug = self.cache["models"][b][prefix] if prefix else None
            if slug:
                return f"{b}/{slug}"
        return None

    def auction_lots(self, make: str, model: str, year: int, pages: int = 3) -> list[dict] | None:
        """Проданные лоты японских аукционов модели за год (drom.ru/world/japan/<марка>/<модель>/):
        [{"year", "grade", "cc", "mileage_km", "score", "body", "price_jpy"}]. Кэш на AUCTION_TTL_DAYS
        дней; None — модели нет на drom.ru или страницы не открылись (лимит запросов)."""
        path = self.model_path(make, model)
        if not path or not year:
            return None
        key = f"{path}|{year}"
        today = date.today().toordinal()
        entry = self.cache["auctions"].get(key)
        if entry and today - entry.get("day", 0) <= AUCTION_TTL_DAYS:
            return entry["lots"]
        lots, seen = [], set()
        for n in range(1, pages + 1):
            page = "" if n == 1 else f"page{n}/"
            got = self._get(f"https://www.drom.ru/world/japan/{path}/{page}?yearFrom={year}&yearTo={year}")
            if got is None:
                return entry["lots"] if entry else None
            new = [x for x in parse_auction(got[1]) if x["lot"] not in seen]
            seen.update(x["lot"] for x in new)
            lots += new
            if len(new) < 10:
                break
        self.cache["auctions"][key] = {"day": today, "lots": [{k: v for k, v in x.items() if k != "lot"} for x in lots]}
        return self.cache["auctions"][key]["lots"]

    def known_missing(self, make: str, model: str) -> bool:
        """Модели точно нет на drom.ru (список моделей марки уже в кэше, а её там нет) —
        например, грузовики и автобусы: их нет в каталоге легковых. Без запросов."""
        with self.cached_only():
            if self.model_path(make, model):
                return False
            brand = self.brand_slug(make)
        brands = [brand] + BRAND_FALLBACK.get(brand, []) if brand else []
        if not brands or not all(b in self.cache["models"] for b in brands):
            return False
        # Lexus «RX» — на drom.ru rx350, rx450h…: модель, с которой начинается чьё-то название, есть
        name = _norm(re.sub(r"\s*\(.*?\)", "", model))
        return not any(k.startswith(name) for b in brands for k in self.cache["models"][b])

    def generations(self, path: str, market: str, year: int | None = None) -> list[dict]:
        """Поколения модели на рынке; с year — только начавшиеся в [year-12, year+1]
        (год начала — в адресе g_2020_16433), чтобы не открывать страницы старых поколений."""
        key = f"{path}/{market}"
        entry = self.cache["gen_lists"].get(key)
        today = date.today().toordinal()
        if not entry or today - entry.get("day", 0) > GEN_LIST_TTL_DAYS:
            got = self._get(f"{BASE}{path}/{market}/")
            if got is None:
                return []
            urls = sorted(set(re.findall(rf'/catalog/{re.escape(path)}/(g_\d+_\d+)/', got[0])))
            # g_2025_23434 и g_202509_23434 — одно поколение (номер тот же)
            by_id = {}
            for u in urls:
                by_id.setdefault(u.rsplit("_", 1)[1], u)
            entry = {"day": today, "gens": sorted(by_id.values())}
            self.cache["gen_lists"][key] = entry
        gens = []
        for g in entry["gens"]:
            start = int(g.split("_")[1][:4])
            if year and not (year - 12 <= start <= year + 1):
                continue
            gkey = f"{path}/{g}"
            if gkey not in self.cache["gens"]:
                got = self._get(f"{BASE}{gkey}/")
                if got is None:
                    continue
                self.cache["gens"][gkey] = parse_generation(got[1], got[0])
            self.cache["gens"][gkey]["key"] = gkey
            gens.append(self.cache["gens"][gkey])
        return [g for g in gens if g.get("market") == MARKETS.get(market, market)]

    def power(self, car: dict) -> dict | None:
        """car: make, model, market (china/south-korea/japan) или markets [по очереди], year, month?, cc, fuel (petrol/diesel/
        hybrid/electric/phev), drive (fwd/rwd/4wd)?, trans (auto/manual/cvt/robot)?, trim?, body?.
        Без cc комплектацию ищем по дате, топливу, приводу и названию — объём тогда берём
        из найденной группы («liters», если он у всех подходящих групп один).
        → {"hp", "hp_total", "liters", "source", "trim"} или None."""
        path = self.model_path(car["make"], car["model"]) if car.get("make") and car.get("model") else None
        num = re.search(r"(\d{3}[a-z]*\+?)", car.get("trim") or "", re.I)
        if not path and num and (car.get("make") or "").lower() == "lexus":
            # На drom.ru модели Lexus — по мотору: «NX» + «300h» из комплектации → nx300h
            path = self.model_path(car["make"], f"{car['model']}{num.group(1)}")
        if not path:
            self.stats["no_model"] += 1
            key = f"{car.get('make')} {car.get('model')}"
            self.missing[key] = self.missing.get(key, 0) + 1
            return None
        if not car.get("year"):
            # Без года поколение и комплектацию не выбрать
            self.stats["no_match"] += 1
            return None
        ym = car["year"] * 100 + (car.get("month") or 6)
        # Без месяца (только год) период комплектации сверяем по году
        lo_hi = (lambda t: (t["from"] // 100 * 100 + 1, (t["to"] or 999999) // 100 * 100 + 12)) if not car.get("month") \
            else (lambda t: (t["from"], t["to"] or 999999))
        cands = []
        for relax in (False, True):
            if cands:
                break
            car = {**car, "_relax": relax}
            cands = self._candidates(path, car, ym, lo_hi)
        self.last_candidates = [(g["text"], [t["name"] for t in trims][:3]) for g, trims in cands]
        words = set(re.findall(r"[a-z0-9]+", (car.get("trim") or "").lower())) - {"l", "t", "at", "mt"}
        return self._choose(car, cands, words)

    def _candidates(self, path, car, ym, lo_hi):
        """Группы комплектаций, подходящие машине по дате, объёму, топливу, приводу, коробке."""
        cands = []
        # Рынки по очереди (импорт в Корее — «south-korea», потом «europe»): берём первый, где нашлось.
        # Сначала комплектации, чей период выпуска точно включает дату машины; нет таких — ±1 год
        for market in car.get("markets") or [car["market"]]:
            gens = self.generations(path, market, car["year"])
            for tol in (0, 100):
                for gen in gens:
                    for g in gen["groups"]:
                        trims = [t for t in g["trims"] if lo_hi(t)[0] - tol <= ym <= lo_hi(t)[1] + tol] or (
                            [] if g["trims"] else [None])
                        if trims and _fits(g, car):
                            g["gen_key"] = gen.get("key")
                            cands.append((g, [t for t in trims if t]))
                if cands:
                    break
            if cands:
                break
        return cands

    def _choose(self, car, cands, words):
        """Мощность из подходящих групп: одна — берём; несколько — уточняем, иначе None."""

        def result(hp, total):
            # Комплектация для технических характеристик: из групп с этой мощностью — та, чьё
            # название больше всего совпадает с комплектацией машины
            best, ref, liters = -1, None, set()
            for g, trims in cands:
                if (g["hp"], g.get("hp_total")) != (hp, total):
                    continue
                liters.add(g["liters"])
                for t in trims:
                    tw = set(re.findall(r"[a-z0-9]+", t["name"].lower()))
                    # «xDrive» в комплектации drom.ru, а у машины его нет — это другая машина
                    score = len(words & tw) - len((tw & AWD_WORDS) - words)
                    if score > best:
                        best, ref = score, {"gen": g.get("gen_key"), "name": t["name"], "id": t.get("id")}
            return {"hp": hp, "hp_total": total if total and total > hp else None, "source": "drom", "trim": ref,
                    "liters": liters.pop() if len(liters) == 1 else None}

        # Код мотора Audi / VW в комплектации («35 TFSI», «40 TDI») должен совпасть с комплектацией
        # drom.ru: A4 2020 «35 TFSI» — 150 л.с., а не 190 л.с. у «40 TFSI» того же объёма
        code = re.search(r"\b(\d{2})\s*(TFSI|TDI|TSI)\b", car.get("trim") or "", re.I)
        if code and cands:
            key = f"{code.group(1)}{code.group(2).lower()}"
            named = lambda t: {f"{a}{b.lower()}" for x in t for a, b in re.findall(r"\b(\d{2})\s*(TFSI|TDI|TSI)\b", x["name"], re.I)}
            if any(named(t) for _, t in cands):
                cands = [(g, t) for g, t in cands if key in named(t)]
        # Кей-кары (до 660 см³): турбо — всегда 64 л.с., атмосферные — меньше. Турбо известно
        # из объявления — оставляем только группы с такой мощностью (Tanto 0.66: 58 и 64 л.с.;
        # Dayz Highway Star Turbo — не 49 л.с. атмосферной версии, даже если турбо на drom.ru нет)
        if car.get("turbo") is not None and car.get("cc") and car["cc"] <= 700 and cands:
            kei = [(g, t) for g, t in cands if (g["hp"] >= 60) == bool(car["turbo"])]
            if len(kei) != len(cands):
                self.stats["by_turbo"] = self.stats.get("by_turbo", 0) + 1
            cands = kei
        hps = {(g["hp"], g.get("hp_total")) for g, _ in cands}
        if len(hps) == 1:
            self.stats["exact"] += 1
            return result(*hps.pop())
        if not cands:
            self.stats["no_match"] += 1
            return None
        # Несколько мощностей — уточняем по коробке (вариатор / робот / автомат / механика:
        # Fit 2020 — робот 110 л.с. и вариатор 98 л.с.) и по турбо (Harrier 2.0 и 2.0 Turbo)
        def narrow(keep):
            nonlocal cands
            left = [(g, t) for g, t in cands if keep(g, t)]
            if left and len(left) < len(cands):
                cands = left
            return {(g["hp"], g.get("hp_total")) for g, _ in cands}
        if car.get("trans"):
            hps = narrow(lambda g, t: not g["trans"] or g["trans"] == car["trans"])
            if len(hps) == 1:
                self.stats["by_trans"] = self.stats.get("by_trans", 0) + 1
                return result(*hps.pop())
        # «Turbo» в названии комплектации — признак турбо только у японских марок (Harrier 2.0 Turbo);
        # у Porsche, BMW и других турбо все версии, «Turbo» — отдельная модификация
        if car.get("turbo") is not None and (car.get("make") or "").lower() in JAPANESE_MAKES:
            turbo_named = lambda t: any("turbo" in x["name"].lower() for x in t)
            hps = narrow(lambda g, t: turbo_named(t) == bool(car["turbo"]))
            if len(hps) == 1:
                self.stats["by_turbo"] = self.stats.get("by_turbo", 0) + 1
                return result(*hps.pop())
        # Одинаковый объём, разная мощность — различаем по названию комплектации
        scored = []
        for g, trims in cands:
            best = max((len(words & set(re.findall(r"[a-z0-9]+", t["name"].lower()))) for t in trims), default=0)
            if car.get("body") and car["body"] in g["body"]:
                best += 10
            scored.append((best, g))
        top = max(s for s, _ in scored)
        hps = {(g["hp"], g.get("hp_total")) for s, g in scored if s == top}
        if top > 0 and len(hps) == 1:
            self.stats["by_trim"] += 1
            return result(*hps.pop())
        self.stats["ambiguous"] += 1
        return None

    @contextmanager
    def cached_only(self):
        """Внутри — только то, что уже в кэше, без новых страниц drom.ru."""
        limit, self.max_requests = self.max_requests, self.requests
        try:
            yield self
        finally:
            self.max_requests = limit

    def tech(self, trim: dict | None) -> dict | None:
        """Технические характеристики комплектации (trim — из результата power()) со страницы
        комплектации drom.ru: {"name", "groups", "url"} или None. Страница комплектации не меняется —
        кэш навсегда; номер комплектации в старом кэше поколения — перечитываем страницу поколения."""
        if not trim or not trim.get("gen"):
            return None
        gkey, tid = trim["gen"], trim.get("id")
        gen = self.cache["gens"].get(gkey)
        if not tid and gen and not gen.get("ids"):
            got = self._get(f"{BASE}{gkey}/")
            if got is None:
                return None
            fresh = parse_generation(got[1], got[0])
            fresh["key"] = gkey
            self.cache["gens"][gkey] = fresh
            tid = next((t.get("id") for g in fresh["groups"] for t in g["trims"] if t["name"] == trim["name"]), None)
        if not tid:
            return None
        cached = self.cache["trims"].get(tid)
        # Старая запись электромобиля/гибрида — без строк электромотора: перечитать страницу
        stale = cached is not None and cached.get("v", 1) < TRIM_VERSION and "Электр" in json.dumps(cached, ensure_ascii=False)
        if tid not in self.cache["trims"] or stale:
            got = self._get(f"{BASE}{gkey.rsplit('/', 1)[0]}/{tid}/")
            if got is None:
                return None
            self.cache["trims"][tid] = parse_trim(got[1]) or {}
        data = self.cache["trims"][tid]
        if not data.get("groups"):
            return None
        return {**data, "url": f"{BASE}{gkey.rsplit('/', 1)[0]}/{tid}/"}


AUCTION_TTL_DAYS = 7
_AUC_HEAD = re.compile(r"^(.+),\s*((?:19|20)\d{2})$")
_AUC_PRICE = re.compile(r"^([\d\s\u00a0\u202f]+)\s*JP¥$")


def _num_text(text: str) -> int | None:
    digits = re.sub(r"\D", "", text or "")
    return int(digits) if digits else None


def parse_auction(page_text: str) -> list[dict]:
    """Текст страницы статистики аукционов drom.ru → проданные лоты. Лот: «Honda Fit, 2018»,
    комплектация, «Объем 1 500 см³», «КПП …», «Кузов GP5», «Пробег 131 000 км», «Оценка 3.5»,
    «732 000 JP¥», …, «Лот 60155»."""
    lines = [ln.strip() for ln in page_text.split("\n") if ln.strip()]
    lots, cur = [], None
    for i, ln in enumerate(lines):
        head = _AUC_HEAD.match(ln)
        if head and i + 1 < len(lines) and not lines[i + 1].startswith(("Объем", "Пробег")):
            cur = {"year": int(head.group(2)), "grade": lines[i + 1]}
            continue
        if cur is None:
            continue
        label, _, value = ln.partition("\t")
        if not value:
            parts = ln.split(None, 1)
            label, value = (parts[0], parts[1]) if len(parts) == 2 else (ln, "")
        if label == "Объем":
            cur["cc"] = _num_text(value)
        elif label == "Пробег":
            cur["mileage_km"] = _num_text(value)
        elif label == "Оценка":
            cur["score"] = value.strip()
        elif label == "Кузов":
            cur["body"] = value.strip()
        elif _AUC_PRICE.match(ln):
            cur["price_jpy"] = _num_text(ln)
        elif ln.startswith("Лот ") and cur.get("price_jpy"):
            cur["lot"] = ln[4:].strip() + "|" + str(cur.get("price_jpy"))
            lots.append(cur)
            cur = None
    return lots


def norm_fuel(text: str | None) -> str | None:
    """«бензин», «гибрид (бензин + электро)», «Бензин», «Гибрид», «Электро», «Последовательный гибрид…» → код."""
    low = (text or "").lower()
    if not low:
        return None
    if any(k in low for k in ("подключ", "plug", "phev", "dm-i", "dm-p")):
        return "phev"
    if "гибрид" in low or "hybrid" in low:
        return "hybrid"
    if "электр" in low:
        return "electric"
    if "дизел" in low:
        return "diesel"
    if "газ" in low or "lpg" in low:
        return "lpg"
    if "бензин" in low:
        return "petrol"
    return None


def norm_trans(text: str | None) -> str | None:
    low = (text or "").lower()
    return ("manual" if ("механ" in low or "мкпп" in low or "手动" in low) else "cvt" if ("вариатор" in low or "cvt" in low)
            else "robot" if ("робот" in low or "dct" in low) else "auto" if ("автомат" in low or "акпп" in low or "自动" in low)
            else None)


def norm_drive(text: str | None) -> str | None:
    low = (text or "").lower()
    return ("4wd" if ("полный" in low or "4wd" in low or "awd" in low or "四驱" in low
                     or re.search(r"xdrive|4matic|quattro|4motion", low)) else "rwd" if ("задний" in low or "后驱" in low)
            else "fwd" if ("передний" in low or "前驱" in low) else None)


def _fits(g: dict, car: dict) -> bool:
    fuel = car.get("fuel")
    if fuel == "electric":
        if g["fuel"] != "electric":
            return False
    else:
        if g["fuel"] == "electric":
            return False
        # Запасной поиск (_relax): объём ±0,1 л — продавец пишет 2200 см³ вместо 2267 (на drom.ru 2.3 л)
        if car.get("cc") and g["liters"] is not None and \
                abs(g["liters"] - round(car["cc"] / 1000, 1)) > (0.15 if car.get("_relax") else 0.05):
            return False
        if fuel == "diesel" and g["fuel"] != "дизель":
            return False
        if fuel == "lpg" and "газ" not in (g["fuel"] or ""):
            return False
        # Бензин — без гибридов; в запасном поиске — и мягкий гибрид (eK X: у продавца «бензин»)
        if fuel == "petrol" and (g["fuel"] in ("дизель", "газ") or (g["hybrid"] and not (car.get("_relax") and "подзаряж" not in g["hybrid"] and "подключ" not in g["hybrid"]))):
            return False
        if fuel in ("hybrid", "phev") and not g["hybrid"]:
            return False
        plug = bool(g["hybrid"]) and any(k in g["hybrid"] for k in ("подключ", "подзаряж", "plug", "phev"))
        if fuel == "phev" and g["hybrid"] and not plug:
            return False
        if fuel == "hybrid" and plug:
            return False
    if car.get("drive") and g["drive"] and car["drive"] != g["drive"]:
        return False
    if car.get("trans") and g["trans"]:
        # «автомат» у продавца бывает и вариатором, и роботом — отсекаем только механику
        if (car["trans"] == "manual") != (g["trans"] == "manual"):
            return False
    return True
