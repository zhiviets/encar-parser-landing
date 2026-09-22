
import json
import os
import re
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

from brand_map import extract_brand_model




START_URL = (
    "https://car.encar.com/list/car?page=1"
    "&search=%7B%22type%22%3A%22car%22%2C%22action%22%3A%22(And.Hidden.N._.MultiViewHidden.N.)%22,"
    "%22toggle%22%3A%7B%7D,%22layer%22%3A%22%22,%22sort%22%3A%22MobileModifiedDate%22%7D"
)


# Раз в неделю можно забирать больше, чем одну страницу — регулируется без
# правки кода переменной окружения ENCAR_MAX_PAGES (по умолчанию 3 страницы).
MAX_PAGES = int(os.environ.get("ENCAR_MAX_PAGES", "3"))

# Куда пушим данные в bn-auto. Без этих переменных скрипт просто
# сохранит cars.json локально, как раньше — пуш не обязателен.
BN_AUTO_URL = os.environ.get("BN_AUTO_URL", "").rstrip("/")
BN_AUTO_IMPORT_TOKEN = os.environ.get("BN_AUTO_IMPORT_TOKEN", "")

# Опциональный прокси — GitHub Actions запускается из дата-центра, и Encar
# может блокировать такие IP («подозрительный трафик» + капча). Без этих
# переменных браузер просто ходит напрямую, как раньше.
#
# ВАЖНО: если у прокси есть логин/пароль — используйте схему "http://",
# не "socks5://". Авторизация в SOCKS5-прокси не поддерживается ни в
# Chromium, ни в Firefox (ограничение самих браузеров, не Playwright) —
# падает с "Browser does not support socks5 proxy authentication".
# Прокси без пароля (IP уже привязан на стороне провайдера) может
# использовать socks5:// как обычно.
PROXY_SERVER = os.environ.get("PROXY_SERVER") or None  # например "http://109.237.105.248:8000"
PROXY_USERNAME = os.environ.get("PROXY_USERNAME") or None
PROXY_PASSWORD = os.environ.get("PROXY_PASSWORD") or None


OUT_PATH = Path(__file__).resolve().parents[1] / "site" / "data" / "cars.json"
OUT_PATH.parent.mkdir(parents=True, exist_ok=True)




def slow_human_pause(sec: float = 1.0):
    time.sleep(sec)

def _to_int(s: str) -> int | None:
    if not s:
        return None
    digits = re.sub(r"[^\d]", "", s)
    return int(digits) if digits else None

def _year_from_korean(s: str) -> int | None:
    """
    Примеры: '21/04식', '2019식' -> 2021 / 2019
    Берём первые 2–4 цифры. Если 2-значный — считаем как 2000+.
    """
    m = re.search(r"(\d{2,4})", s or "")
    if not m:
        return None
    y = int(m.group(1))
    return 2000 + y if y < 100 else y

def _external_id(link: str) -> str | None:
    m = re.search(r"/detail/(\d+)", link or "")
    return m.group(1) if m else None


def _norm_url(u: str) -> str:
    if not u:
        return u
    if u.startswith("//"):
        return "https:" + u
    if u.startswith("/"):
        return "https://car.encar.com" + u
    return u




def scrape_list(page, url: str) -> list[dict]:
    page.goto(url, wait_until="networkidle")
    slow_human_pause(1.2)

    try:
        page.wait_for_selector('div[class^="ItemBigImage_item__"]', timeout=15000)
    except Exception:
        # Селектор не появился — сохраняем скриншот и HTML, чтобы понять,
        # что реально вернул сайт серверу GitHub Actions (блокировка по
        # региону/IP, капча, изменившаяся вёрстка — по картинке видно сразу,
        # а из песочницы, где писался этот код, зайти на encar.com нельзя).
        debug_dir = Path(__file__).resolve().parent / "debug"
        debug_dir.mkdir(exist_ok=True)
        page.screenshot(path=str(debug_dir / "list_page.png"), full_page=True)
        (debug_dir / "list_page.html").write_text(page.content(), encoding="utf-8")
        print(f"Селектор карточек не найден. Заголовок страницы: {page.title()!r}")
        print(f"Диагностика сохранена в {debug_dir}")
        raise

    items = page.locator('div[class^="ItemBigImage_item__"]')
    n = min(items.count(), 30)  

    cars: list[dict] = []

    for i in range(n):
        card = items.nth(i)
        try:
            
            href = ""
            link = card.locator('a[class^="ItemBigImage_link_item__"]')
            if link.count():
                href = _norm_url(link.first.get_attribute("href") or "")

            
            img = ""
            img_loc = card.locator('div[class^="CarPhotoSwiper_swiper_wrap__"] img')
            if img_loc.count() == 0:
                img_loc = card.locator("img")
            if img_loc.count():
                img = _norm_url(
                    img_loc.first.get_attribute("src")
                    or img_loc.first.get_attribute("data-src")
                    or ""
                )

           
            brand, model = None, None
            title_node = card.locator('strong[class^="ItemBigImage_name__"]')
            if title_node.count():
                lines = [x.strip() for x in title_node.first.inner_text().splitlines() if x.strip()]
                if lines:
                    brand = lines[0]
                if len(lines) > 1:
                    model = lines[1]

          
            year, mileage_km = None, None
            info_items = card.locator('ul[class^="ItemBigImage_info__"] > li')
            if info_items.count() >= 1:
                year = _year_from_korean(info_items.nth(0).inner_text().strip())
            if info_items.count() >= 2:
                mileage_km = _to_int(info_items.nth(1).inner_text().strip())

          
            price_krw = None
            price_num = card.locator('div[class^="ItemBigImage_price_area__"] span[class^="ItemBigImage_num__"]')
            if price_num.count():
                man = _to_int(price_num.first.inner_text().strip())
                if man is not None:
                    price_krw = man * 10_000

           
            if (not brand or not model) and img_loc.count():
                alt = img_loc.first.get_attribute("alt") or ""
                parts = [p for p in alt.split() if p.strip()]
                if parts:
                    brand = brand or parts[0]
                    if not model and len(parts) > 1:
                        model = " ".join(parts[1:3])

           
            if href and img:
                cars.append({
                    "brand": brand,
                    "model": model,
                    "year": year,
                    "mileage_km": mileage_km,
                    "price_krw": price_krw,
                    "image": img,
                    "link": href,
                })

        except Exception:
            
            continue

    return cars




def main():
    with sync_playwright() as p:
        # Firefox не умеет авторизацию (логин/пароль) в SOCKS5-прокси —
        # Playwright падает с "Browser does not support socks5 proxy
        # authentication". Chromium это поддерживает.
        browser = p.chromium.launch(headless=True, slow_mo=80)

        proxy = None
        if PROXY_SERVER:
            proxy = {"server": PROXY_SERVER}
            if PROXY_USERNAME:
                proxy["username"] = PROXY_USERNAME
            if PROXY_PASSWORD:
                proxy["password"] = PROXY_PASSWORD
            print(f"Используем прокси: {PROXY_SERVER}")

        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 900},
            locale="ko-KR",
            proxy=proxy,
        )
        page = context.new_page()

        all_cars: list[dict] = []
        for page_no in range(1, MAX_PAGES + 1):
            url = re.sub(r"page=\d+", f"page={page_no}", START_URL)
            all_cars.extend(scrape_list(page, url))
            slow_human_pause(1.0)

        
        cleaned = []
        for c in all_cars:
            if c.get("image") and c.get("link"):
                raw_title = c.get("brand") or ""
                brand_en, model_guess, _ = extract_brand_model(raw_title)
                cleaned.append({
                    "external_id": _external_id(c.get("link")),
                    "brand": c.get("brand"),
                    "model": c.get("model"),
                    "brand_en": brand_en,
                    "model_guess": model_guess,
                    "title": raw_title,
                    "year": c.get("year"),
                    "mileage_km": c.get("mileage_km"),
                    "price_krw": c.get("price_krw"),
                    "image": c.get("image"),
                    "link": c.get("link"),
                })

        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(OUT_PATH, "w", encoding="utf-8") as f:
            json.dump({"updated_at": int(time.time()), "cars": cleaned}, f, ensure_ascii=False, indent=2)

        print(f"Saved {len(cleaned)} cars -> {OUT_PATH}")
        browser.close()

    push_to_bn_auto(cleaned)


def push_to_bn_auto(cars: list[dict]):
    """Отправить объявления в bn-auto. Без настроенных переменных — просто пропустить."""
    if not BN_AUTO_URL or not BN_AUTO_IMPORT_TOKEN:
        print("BN_AUTO_URL / BN_AUTO_IMPORT_TOKEN не заданы — пуш в bn-auto пропущен.")
        return

    import requests

    listings = [
        {
            "external_id": c["external_id"],
            "make": c.get("brand_en"),
            "model": c.get("model_guess"),
            "title": c.get("title"),
            "year": c.get("year"),
            "mileage_km": c.get("mileage_km"),
            "price_value": c.get("price_krw"),
            "photo_url": c.get("image"),
            "source_url": c.get("link"),
        }
        for c in cars
        if c.get("external_id")
    ]
    if not listings:
        print("Нет объявлений с external_id — нечего пушить в bn-auto.")
        return

    resp = requests.post(
        f"{BN_AUTO_URL}/api/live-listings/import",
        json={"source": "encar", "listings": listings},
        headers={"Authorization": f"Bearer {BN_AUTO_IMPORT_TOKEN}"},
        timeout=30,
    )
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code >= 400:
        print(f"Пуш в bn-auto не удался: HTTP {resp.status_code} {data}")
        resp.raise_for_status()
    print(f"Пуш в bn-auto: {data.get('stats')}, пропущено {data.get('skipped', 0)}")


if __name__ == "__main__":
    main()
