"""
Какие машины с encar берём в bn-auto.

Все — с 2010 года выпуска, любых марок, по возможности все модели (см.
coverage.py). Из них 75% — до 160 л.с. (проходные по утильсбору),
остальные 25% — любой мощности.

Мощность encar в списке не показывает и в поиске не фильтрует, поэтому она
оценивается по двигателю из названия и точному объёму из API encar:
атмосферный бензин/газ до 2.0 л, турбобензин до 1.4 л, дизель и гибрид без
турбины до 1.6 л — это до 160 л.с. Электромобили и гибриды мощнее — в группе «любой мощности»; машины, мощность
которых не оценить (нет объёма двигателя), не берём.
Правила — оценка, а не паспорт машины.
"""

import os
import re

MIN_YEAR = int(os.environ.get("ENCAR_MIN_YEAR") or "2010")

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

# Доли по годам выпуска: 60% — 2022–2024, 15% — 2025–2026, 15% — 2017–2021, 10% — 2010–2016.
# Порядок — приоритет: при выборе машины модели сначала смотрим первую группу.
YEAR_BANDS = [("2022–2024", 2022, 2024, 0.60), ("2025–2026", 2025, 2026, 0.15), ("2017–2021", 2017, 2021, 0.15),
              ("2010–2016", 2010, 2016, 0.10)]


def year_band(year: int | None) -> str | None:
    for name, lo, hi, _ in YEAR_BANDS:
        if year and lo <= year <= hi:
            return name
    return None


def catalog_have(items, kinds=("le160", "gt160")) -> dict:
    """Состав каталога: {(класс мощности, годы): машин} — по опубликованным полным машинам с
    сайта (мощность и «электро» — как их считает сайт; электромобили — в «любой мощности»)."""
    have = {}
    for i in items:
        if not (i.get("complete", True) and i.get("published")):
            continue
        power = i.get("power") or 0
        if not power and not i.get("electric"):
            continue
        kind = kinds[1] if power > 160 or i.get("electric") else kinds[0]
        band = year_band(int(i["year"]) if i.get("year") else None)
        if band:
            have[(kind, band)] = have.get((kind, band), 0) + 1
    return have


def run_wants(total: int, have: dict, share: float, kinds=("le160", "gt160")) -> dict:
    """Сколько машин каждой клетки (класс мощности, годы) взять за прогон, чтобы доли (share до
    160 л.с., YEAR_BANDS) держались для каталога целиком: прошлый перекос выправляется,
    переполненные клетки в этот прогон не берём."""
    final = sum(have.values()) + total
    need = {(k, name): max(0, round(final * (share if k == kinds[0] else 1 - share) * w) - have.get((k, name), 0))
            for k in kinds for name, _, _, w in YEAR_BANDS}
    s = sum(need.values())
    if s > total:
        exact = {c: v * total / s for c, v in need.items()}
        # Округление с сохранением суммы: целые части, остаток — самым большим дробным
        need = {c: int(v) for c, v in exact.items()}
        for c in sorted(exact, key=lambda c: exact[c] - need[c], reverse=True)[:total - sum(need.values())]:
            need[c] += 1
    return need


# Поиск encar: базовые условия списка + год выпуска от MIN_YEAR.
# CarType.Y — корейские марки, CarType.N — импорт. Если такой фильтр вернёт
# пустую страницу (синтаксис поиска encar не документирован), парсер
# откатится на BASE_ACTION и отфильтрует машины сам — см. scraper.main().
BASE_ACTION = "(And.Hidden.N._.MultiViewHidden.N.)"
_YEAR = f"Year.range({MIN_YEAR}00..)"
PROFILES = [
    {"name": f"импорт с {MIN_YEAR} г.", "import": True,
     "action": f"(And.Hidden.N._.MultiViewHidden.N._.CarType.N._.{_YEAR}.)"},
    {"name": f"корейские марки с {MIN_YEAR} г.", "import": False,
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
# Спортивные марки — любые их машины мощнее 160 л.с. (Porsche 718 2.0 — 300 л.с.)
_SPORT_MAKES = re.compile(r"포르쉐|페라리|람보르기니|맥라렌|애스턴|벤틀리|롤스로이스|마세라티|로터스|"
                          r"porsche|ferrari|lamborghini|mclaren|aston|bentley|rolls|maserati|lotus", re.I)
# Спортивные версии: AMG, BMW M2–M8 и X3 M…, Audi RS/S, GTI, JCW («M 스포츠» — пакет, не версия)
_PERFORMANCE = re.compile(r"\bAMG\b|\bM[2-8]\b|\bX[3-7]\s?M\b|\bRS\s?\d|\bS[3-8]\b|\bSQ\d|\bGTI\b|\bJCW\b|존쿠퍼웍스", re.I)
# Марки, у которых моторы больше 1,6 л — турбо мощнее 160 л.с. (BMW 420i 2.0 — 184 л.с.)
_TURBO_MAKES = re.compile(r"BMW|벤츠|아우디|폭스바겐|볼보|미니|랜드로버|재규어|캐딜락|링컨|제네시스|"
                          r"mercedes|benz|audi|volkswagen|volvo|\bmini\b|land rover|jaguar|cadillac|lincoln|genesis", re.I)


def power_class(text: str, displacement: int | None = None) -> str | None:
    """'le160' / 'gt160' / None (не хватает данных, чтобы судить).
    Электромобиль — 'gt160': он идёт в группу «любой мощности»."""
    text = text or ""
    if _ELECTRIC.search(text) and not _HYBRID.search(text):
        return "gt160"
    cc = displacement
    if not cc:
        m = _LITERS.search(text)
        cc = round(float(m.group(1)) * 1000) if m else None
    if not cc:
        return None
    if _SPORT_MAKES.search(text) or _PERFORMANCE.search(text):
        return "gt160"
    if cc > 1600 and _TURBO_MAKES.search(text):
        return "gt160"
    turbo, diesel, hybrid = bool(_TURBO.search(text)), bool(_DIESEL.search(text)), bool(_HYBRID.search(text))
    if hybrid and turbo:
        return "gt160"
    if turbo and not diesel:
        return "le160" if cc <= 1400 else "gt160"
    if diesel or hybrid:
        return "le160" if cc <= 1600 else "gt160"
    return "le160" if cc <= 2000 else "gt160"


def eligible(brand: str | None, year: int | None) -> bool:
    """Подходит ли машина по году (мощность — отдельно). Марка — любая, лишь бы
    распознана (иначе на сайте вместо марки будет «?»)."""
    return bool(year and year >= MIN_YEAR and brand)


def classify(power: str | None, model: str | None = None, title: str | None = None) -> str | None:
    """Группа машины: 'le160', 'gt160' или None — мощность не оценить (такие не берём).
    Модели, где обычно мощнее 160 л.с., в «до 160» не попадают."""
    if power == "le160" and ((model and model in GT160_MODELS) or (title and _KO_EXCLUDE.search(title))):
        return "gt160"
    return power
