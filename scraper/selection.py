"""
Какие машины с encar берём в bn-auto.

Все — с 2020 года выпуска, популярных и премиальных марок. Из них 75% —
до 160 л.с. (проходные по утильсбору), остальные 25% — любой мощности.

Мощность encar в списке не показывает и в поиске не фильтрует, поэтому она
оценивается по двигателю из названия и точному объёму из API encar:
атмосферный бензин/газ до 2.0 л, турбобензин до 1.4 л, дизель и гибрид без
турбины до 1.6 л — это до 160 л.с. Электромобили и машины, мощность которых не оценить, не берём.
Правила — оценка, а не паспорт машины.
"""

import os
import re

MIN_YEAR = int(os.environ.get("ENCAR_MIN_YEAR") or "2020")

MASS_BRANDS = {
    "Hyundai", "Kia", "Chevrolet", "Renault", "KGM",
    "Toyota", "Honda", "Nissan", "Volkswagen", "MINI", "Ford", "Peugeot",
}
PREMIUM_BRANDS = {
    "BMW", "Mercedes-Benz", "Audi", "Porsche", "Lexus", "Genesis",
    "Land Rover", "Volvo", "Tesla", "Jaguar", "Cadillac", "Lincoln", "Maserati", "Bentley",
}

# Сколько машин собирать за прогон (по умолчанию 300): доля ENCAR_SHARE_160
# (0.75) — до 160 л.с., остальное — любой мощности.
_TOTAL = int(os.environ.get("ENCAR_TOTAL") or "300")
_LE160 = round(_TOTAL * float(os.environ.get("ENCAR_SHARE_160") or "0.75"))
QUOTAS = {"le160": _LE160, "other": _TOTAL - _LE160}
# Какую часть каждой группы берём из импорта (остальное добирают корейские
# марки): импорт до 160 л.с. встречается реже, поэтому его доля там меньше.
IMPORT_SHARE = {"le160": 0.3, "other": 0.7}

# Поиск encar: базовые условия списка + год выпуска от MIN_YEAR.
# CarType.Y — корейские марки, CarType.N — импорт. Если такой фильтр вернёт
# пустую страницу (синтаксис поиска encar не документирован), парсер
# откатится на BASE_ACTION и отфильтрует машины сам — см. scraper.main().
BASE_ACTION = "(And.Hidden.N._.MultiViewHidden.N.)"
_YEAR = f"Year.range({MIN_YEAR}00..)"
PROFILES = [
    {"name": "импорт с 2020 г.", "import": True,
     "action": f"(And.Hidden.N._.MultiViewHidden.N._.CarType.N._.{_YEAR}.)"},
    {"name": "корейские марки с 2020 г.", "import": False,
     "action": f"(And.Hidden.N._.MultiViewHidden.N._.CarType.Y._.{_YEAR}.)"},
]

# Модели, где распространённые версии мощнее 160 л.с., а слова
# «турбо» в названии на encar часто нет (Equinox 1.5T — 170 л.с., Tivoli
# 1.5T — 163 л.с.). Сверяется по английскому названию модели из API encar.
GT160_MODELS = {
    "Carnival", "Palisade", "Staria", "Sorento", "Santa Fe", "Santafe", "Grandeur", "K8", "K9",
    "Mohave", "Stinger", "Rexton", "Torres", "Tivoli", "Korando", "Actyon",
    "Equinox", "Traverse", "Tahoe", "Colorado", "Camaro", "Impala",
}

_TURBO = re.compile(r"터보|T-?GDi|\d\.\dT\b|TSI|TFSI|turbo|турбо", re.I)
_DIESEL = re.compile(r"디젤|diesel|CRDi|VGT|dCi|дизель", re.I)
# «가솔린+전기» — так encar пишет топливо гибрида: это гибрид, а не электромобиль
_HYBRID = re.compile(r"하이브리드|hybrid|HEV|\+\s*전기|гибрид", re.I)
_ELECTRIC = re.compile(r"전기|일렉트릭|\bEV\d*\b|electric|электро", re.I)

# Модели, которые не считаем «до 160» даже по API: почти все версии
# мощнее 160 л.с. или электромобили (по корейскому названию в списке encar)
_KO_EXCLUDE = re.compile(
    r"카니발|팰리세이드|스타리아|쏘렌토|싼타페|그랜저|모하비|스팅어|렉스턴|토레스|티볼리|코란도|액티언|"
    r"이쿼녹스|트래버스|타호|콜로라도|\bK[89]\b|아이오닉\s?[5-9]|일렉트릭|\bEV\d"
)
_LITERS = re.compile(r"(?<![\d.])(\d\.\d)(?![\d])")


def power_class(text: str, displacement: int | None = None) -> str | None:
    """'le160' / 'gt160' / None (не хватает данных, чтобы судить; электромобиль)."""
    text = text or ""
    if _ELECTRIC.search(text) and not _HYBRID.search(text):
        return None
    cc = displacement
    if not cc:
        m = _LITERS.search(text)
        cc = round(float(m.group(1)) * 1000) if m else None
    if not cc:
        return None
    turbo, diesel, hybrid = bool(_TURBO.search(text)), bool(_DIESEL.search(text)), bool(_HYBRID.search(text))
    if hybrid and turbo:
        return "gt160"
    if turbo and not diesel:
        return "le160" if cc <= 1400 else "gt160"
    if diesel or hybrid:
        return "le160" if cc <= 1600 else "gt160"
    return "le160" if cc <= 2000 else "gt160"


def eligible(brand: str | None, year: int | None) -> bool:
    """Подходит ли машина по году и марке (мощность — отдельно)."""
    return bool(year and year >= MIN_YEAR and brand in MASS_BRANDS | PREMIUM_BRANDS)


def classify(power: str | None, model: str | None = None, title: str | None = None) -> str | None:
    """Группа машины: 'le160', 'gt160' или None — мощность не оценить (такие не берём).
    Модели, где обычно мощнее 160 л.с., в «до 160» не попадают."""
    if power == "le160" and ((model and model in GT160_MODELS) or (title and _KO_EXCLUDE.search(title))):
        return "gt160"
    return power
