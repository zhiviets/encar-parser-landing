"""
Какие машины с encar берём в bn-auto.

Две категории, обе — с 2020 года выпуска и любой мощности:
  * «массовые» — популярные марки (Hyundai, Kia, Toyota…);
  * «премиум» — BMW, Mercedes, Audi и т.п.

Ограничение «массовые только до 160 л.с.» можно включить переменной
ENCAR_LIMIT_160=1. Мощность encar в списке не показывает и в поиске не
фильтрует, поэтому тогда она оценивается по двигателю из названия и точному
объёму из API encar: атмосферный бензин/газ до 2.0 л, турбобензин до 1.4 л,
дизель и гибрид без турбины до 1.6 л — это до 160 л.с. Электромобили при
этом не берём. Правила — оценка, а не паспорт машины.
"""

import os
import re

MIN_YEAR = int(os.environ.get("ENCAR_MIN_YEAR") or "2020")
# Массовые марки только до 160 л.с. — по умолчанию выключено: берём любую мощность
LIMIT_160 = os.environ.get("ENCAR_LIMIT_160") == "1"

MASS_BRANDS = {
    "Hyundai", "Kia", "Chevrolet", "Renault", "KGM",
    "Toyota", "Honda", "Nissan", "Volkswagen", "MINI", "Ford", "Peugeot",
}
PREMIUM_BRANDS = {
    "BMW", "Mercedes-Benz", "Audi", "Porsche", "Lexus", "Genesis",
    "Land Rover", "Volvo", "Tesla", "Jaguar", "Cadillac", "Lincoln", "Maserati", "Bentley",
}

# Сколько машин каждой категории собирать за прогон (всего по умолчанию 300)
QUOTAS = {
    "mass": int(os.environ.get("ENCAR_QUOTA_MASS") or "180"),
    "premium": int(os.environ.get("ENCAR_QUOTA_PREMIUM") or "120"),
}

# Поиск encar: базовые условия списка + год выпуска от MIN_YEAR.
# CarType.Y — корейские марки, CarType.N — импорт. Если такой фильтр вернёт
# пустую страницу (синтаксис поиска encar не документирован), парсер
# откатится на BASE_ACTION и отфильтрует машины сам — см. scraper.main().
BASE_ACTION = "(And.Hidden.N._.MultiViewHidden.N.)"
_YEAR = f"Year.range({MIN_YEAR}00..)"
PROFILES = [
    {"name": "корейские марки с 2020 г.", "bucket": "mass",
     "action": f"(And.Hidden.N._.MultiViewHidden.N._.CarType.Y._.{_YEAR}.)"},
    {"name": "импорт с 2020 г.", "bucket": "premium",
     "action": f"(And.Hidden.N._.MultiViewHidden.N._.CarType.N._.{_YEAR}.)"},
]

# Модели массовых марок, где распространённые версии мощнее 160 л.с., а слова
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

# Модели массовых марок, которые не проверяем даже по API: почти все версии
# мощнее 160 л.с. или электромобили (по корейскому названию в списке encar)
_KO_EXCLUDE = re.compile(
    r"카니발|팰리세이드|스타리아|쏘렌토|싼타페|그랜저|모하비|스팅어|렉스턴|토레스|티볼리|코란도|액티언|"
    r"이쿼녹스|트래버스|타호|콜로라도|\bK[89]\b|아이오닉\s?[5-9]|일렉트릭|\bEV\d"
)
_LITERS = re.compile(r"(?<![\d.])(\d\.\d)(?![\d])")


def power_class(text: str, displacement: int | None = None) -> str | None:
    """'le160' / 'gt160' / None (не хватает данных, чтобы судить)."""
    text = text or ""
    if _ELECTRIC.search(text) and not _HYBRID.search(text):
        return "gt160"
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


def bucket_for(brand: str | None, year: int | None, power: str | None, final: bool,
               model: str | None = None, title: str | None = None) -> str | None:
    """Категория машины или None, если не берём.

    final=False — предварительная проверка по карточке списка: машину с
    неизвестной мощностью оставляем до уточнения по API. final=True —
    окончательная: массовой машине нужна подтверждённая оценка «до 160».
    """
    if not year or year < MIN_YEAR:
        return None
    if brand in PREMIUM_BRANDS:
        return "premium"
    if brand in MASS_BRANDS:
        if not LIMIT_160:
            return "mass"
        if model and model in GT160_MODELS:
            return None
        if title and _KO_EXCLUDE.search(title):
            return None
        if power == "le160" or (power is None and not final):
            return "mass"
    return None
