"""
Best-effort normalisation of ENCAR's Korean listing titles into a
Latin brand name + a short model guess.

ENCAR's card title is one blob of text, e.g.
    "벤츠 GLC-클래스 X254 GLC300 4MATIC 아방가르드"
There is no clean brand/model split in the DOM, and a full translation
would need a translation API this project doesn't have. Instead we:
  1. match a known Korean brand name at the start of the title and map
     it to its Latin name;
  2. guess the model from the first Latin/alphanumeric token left in
     the remainder (Korean listings almost always keep the model code
     in Latin, e.g. "K5", "GLC300", "X3", "Tucson").
This is an approximation, not a translation — trim/grade words stay
in Korean. Good enough to show a recognisable card title.
"""

import re

# Ordered longest-key-first so "쉐보레(GM대우)" matches before "쉐보레" alone.
BRAND_MAP = {
    "쉐보레(GM대우)": "Chevrolet",
    "GM대우": "Chevrolet",
    "쉐보레": "Chevrolet",
    "KG모빌리티(쌍용)": "KGM",
    "쌍용자동차": "KGM",
    "쌍용": "KGM",
    "르노코리아": "Renault",
    "르노삼성": "Renault",
    "르노": "Renault",
    "현대": "Hyundai",
    "기아": "Kia",
    "제네시스": "Genesis",
    "벤츠": "Mercedes-Benz",
    "아우디": "Audi",
    "폭스바겐": "Volkswagen",
    "토요타": "Toyota",
    "렉서스": "Lexus",
    "혼다": "Honda",
    "닛산": "Nissan",
    "포드": "Ford",
    "볼보": "Volvo",
    "포르쉐": "Porsche",
    "미니": "MINI",
    "지프": "Jeep",
    "캐딜락": "Cadillac",
    "페라리": "Ferrari",
    "람보르기니": "Lamborghini",
    "마세라티": "Maserati",
    "벤틀리": "Bentley",
    "롤스로이스": "Rolls-Royce",
    "테슬라": "Tesla",
    "푸조": "Peugeot",
    "시트로엥": "Citroen",
    "피아트": "Fiat",
    "링컨": "Lincoln",
    "재규어": "Jaguar",
    "랜드로버": "Land Rover",
    "BMW": "BMW",
    "MINI": "MINI",
}

# Ordered by key length descending so multi-syllable brands match first
_BRAND_KEYS = sorted(BRAND_MAP.keys(), key=len, reverse=True)

# Common non-model Latin tokens that show up in ENCAR titles and would
# otherwise be picked up as a false "model" guess.
_SKIP_TOKENS = {"WD", "4WD", "2WD", "AWD", "RWD", "FWD", "4MATIC", "TFSI", "TDI", "GDI"}


def extract_brand_model(raw_title: str):
    """Return (brand, model_guess, remainder) from a raw ENCAR title."""
    raw_title = (raw_title or "").strip()
    if not raw_title:
        return None, None, raw_title

    brand = None
    remainder = raw_title
    for key in _BRAND_KEYS:
        if raw_title.startswith(key):
            brand = BRAND_MAP[key]
            remainder = raw_title[len(key):].strip()
            break

    if brand is None:
        # Brand not in the map (rare/import marque) — leave it for a human to fill in.
        return None, None, raw_title

    # Предпочитаем токен с цифрой (GLC300, X254, K5) — это почти всегда код
    # модели, а не название комплектации/трима (RS, GT и т.п.).
    first_token = None
    first_digit_token = None
    for token in re.findall(r"[A-Za-z][A-Za-z0-9\-]{1,}", remainder):
        token = token.strip("-")
        if not token or token.upper() in _SKIP_TOKENS:
            continue
        if first_token is None:
            first_token = token
        if first_digit_token is None and any(ch.isdigit() for ch in token):
            first_digit_token = token
            break

    model = first_digit_token or first_token
    return brand, model, remainder
