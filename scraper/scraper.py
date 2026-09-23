
import base64
import io
import json
import os
import re
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

from brand_map import extract_brand_model
import encar_ru

# Чуть меньше лимита bn-auto (900 КБ, см. server/photo.js в bn-auto) —
# запас на накладные расходы base64.
MAX_PHOTO_BYTES = 850 * 1024




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
# Диагностика (сырые ответы API, сетевой лог одной страницы) — выгружается
# артефактом GitHub Actions, в репозиторий не попадает (.gitignore).
DEBUG_DIR = Path(__file__).resolve().parent / "debug"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
ENCAR_VEHICLE_API = "https://api.encar.com/v1/readside/vehicle/{id}"
ENCAR_VEHICLE_INCLUDE = "ADVERTISEMENT,CATEGORY,CONDITION,CONTACT,MANAGE,OPTIONS,PHOTOS,SPEC,PARTNERSHIP,CENTER,VIEW"
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
    # "networkidle" ждёт полной тишины в сети полсекунды — на сайтах с
    # постоянными фоновыми запросами (аналитика, чаты) это условие может
    # вообще не наступить, особенно через прокси. Дальше всё равно идёт
    # wait_for_selector на реальный контент — он и есть настоящий сигнал
    # готовности, "domcontentloaded" тут просто быстрее и надёжнее.
    page.goto(url, wait_until="domcontentloaded", timeout=45_000)
    slow_human_pause(1.2)

    try:
        page.wait_for_selector('div[class^="ItemBigImage_item__"]', timeout=25000)
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
                # Картинки на encar лениво подгружаются: пока фото не попало
                # в видимую область, в src стоит служебная заглушка
                # (прозрачный trans.gif), а настоящая ссылка — в data-src.
                # Поэтому сначала смотрим data-src и только потом src, а не
                # наоборот — иначе почти всегда достаётся пустышка.
                data_src = img_loc.first.get_attribute("data-src") or ""
                src = img_loc.first.get_attribute("src") or ""
                candidate = data_src if data_src and "trans.gif" not in data_src else src
                if "trans.gif" in candidate:
                    candidate = ""
                img = _norm_url(candidate)

           
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
            user_agent=UA,
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

        if cleaned:
            capture_network_sample(page, cleaned[0]["link"])
        browser.close()

    session = http_session()
    enrich_with_details(session, cleaned)
    push_to_bn_auto(session, cleaned)


def http_session():
    """requests-сессия с теми же заголовками и прокси, что и у браузера."""
    import requests
    from urllib.parse import quote, urlsplit

    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    if PROXY_SERVER:
        parts = urlsplit(PROXY_SERVER)
        auth = ""
        if PROXY_USERNAME:
            auth = f"{quote(PROXY_USERNAME, safe='')}:{quote(PROXY_PASSWORD or '', safe='')}@"
        proxy_url = f"{parts.scheme}://{auth}{parts.netloc}"
        s.proxies = {"http": proxy_url, "https": proxy_url}
    return s


def _save_debug(name: str, data):
    DEBUG_DIR.mkdir(exist_ok=True)
    with open(DEBUG_DIR / name, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def capture_network_sample(page, url: str):
    """Сохранить все JSON-ответы encar при открытии одной карточки.

    Нужно, чтобы увидеть реальную структуру API (в том числе справочник
    опций комплектации) — из песочницы, где писался код, encar недоступен.
    """
    responses = []
    handler = lambda r: responses.append(r)
    page.on("response", handler)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        page.wait_for_timeout(8000)
        page.mouse.wheel(0, 4000)
        page.wait_for_timeout(3000)
    except Exception as error:
        print(f"Сетевой лог карточки не собран: {error}")
    finally:
        page.remove_listener("response", handler)

    sample = []
    for r in responses:
        try:
            if "encar.com" not in r.url or "json" not in (r.headers.get("content-type") or ""):
                continue
            sample.append({"url": r.url, "status": r.status, "body": r.text()[:400_000]})
        except Exception:
            continue
    _save_debug("network_sample.json", sample)
    print(f"Сетевой лог карточки: {len(sample)} JSON-ответов -> {DEBUG_DIR / 'network_sample.json'}")


def fetch_encar_detail(session, vehicle_id: str):
    headers = {
        "Referer": "https://fem.encar.com/",
        "Origin": "https://fem.encar.com",
        "Accept": "application/json, text/plain, */*",
    }
    url = ENCAR_VEHICLE_API.format(id=vehicle_id)
    for params in ({"include": ENCAR_VEHICLE_INCLUDE}, None):
        try:
            resp = session.get(url, params=params, headers=headers, timeout=20)
            if resp.status_code == 200:
                return resp.json()
            print(f"API encar {vehicle_id}: HTTP {resp.status_code}")
        except Exception as error:
            print(f"API encar {vehicle_id}: {error}")
    return None


def _translate_generation(category: dict) -> str | None:
    name = category.get("modelName") or ""
    group_ko = category.get("modelGroupName")
    group_en = category.get("modelGroupEnglishName")
    if group_ko and group_en:
        name = name.replace(group_ko, group_en)
    return encar_ru.clean_latin(name)


def parse_encar_detail(detail: dict, vehicle_id: str) -> dict:
    """Всё, что удалось достать из API encar; каждое поле необязательно."""
    category = detail.get("category") or {}
    spec = detail.get("spec") or {}
    contact = detail.get("contact") or {}

    out = {}
    out["make"] = category.get("manufacturerEnglishName") or None
    out["model"] = category.get("modelGroupEnglishName") or None

    ym = re.sub(r"\D", "", str(category.get("yearMonth") or ""))
    if len(ym) >= 4:
        out["year"] = int(ym[:4])
    if spec.get("mileage") is not None:
        out["mileage_km"] = _to_int(str(spec.get("mileage")))

    mileage = out.get("mileage_km")
    ru_spec = {
        "Лот": f"№{vehicle_id}",
        "Локация продавца": encar_ru.region(contact.get("address")),
        "Выпуск": encar_ru.release(category.get("yearMonth")),
        "Класс": encar_ru.lookup(encar_ru.BODY, spec.get("bodyName")),
        "Поколение": _translate_generation(category),
        "Модификация": category.get("gradeEnglishName") or encar_ru.clean_latin(category.get("gradeName")),
        "Комплектация": category.get("gradeDetailEnglishName") or encar_ru.clean_latin(category.get("gradeDetailName")),
        "Трансмиссия": encar_ru.lookup(encar_ru.TRANSMISSION, spec.get("transmissionName")),
        "Объём, см³": str(spec["displacement"]) if spec.get("displacement") else None,
        "Пробег": f"{mileage:,}".replace(",", " ") + " км" if mileage else None,
        "Топливо": encar_ru.lookup(encar_ru.FUEL, spec.get("fuelName")),
        "Цвет": encar_ru.lookup(encar_ru.COLOR, spec.get("colorName")),
        "Мест": str(spec["seatCount"]) if spec.get("seatCount") else None,
    }
    out["spec"] = {k: v for k, v in ru_spec.items() if v}

    options = detail.get("options")
    if isinstance(options, dict):
        out["options"] = {k: v for k, v in options.items() if isinstance(v, list)}

    photos = detail.get("photos") or []
    outer = [ph for ph in photos if isinstance(ph, dict) and ph.get("path")]
    outer.sort(key=lambda ph: (ph.get("type") != "OUTER", str(ph.get("code") or "")))
    if outer:
        out["photo"] = "https://ci.encar.com/carpicture" + outer[0]["path"]
    return out


def enrich_with_details(session, cars: list[dict]):
    """Дополнить каждую машину данными из API encar (характеристики, опции, фото)."""
    ok = 0
    saved = 0
    for i, c in enumerate(cars, start=1):
        if not c.get("external_id"):
            continue
        detail = fetch_encar_detail(session, c["external_id"])
        if not detail:
            continue
        if saved < 3:
            _save_debug(f"vehicle_{c['external_id']}.json", detail)
            saved += 1
        try:
            c["detail"] = parse_encar_detail(detail, c["external_id"])
            ok += 1
        except Exception as error:
            print(f"Не разобраны детали {c['external_id']}: {error}")
        time.sleep(0.3)
    print(f"Детали из API encar: {ok} из {len(cars)}")


def _compress_to_data_url(image_bytes: bytes) -> str | None:
    from PIL import Image

    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except Exception:
        return None

    quality = 82
    max_width = 1000
    while True:
        resized = img
        if resized.width > max_width:
            ratio = max_width / resized.width
            resized = resized.resize((max_width, max(1, int(resized.height * ratio))))
        buf = io.BytesIO()
        resized.save(buf, format="JPEG", quality=quality)
        data = buf.getvalue()
        if len(data) <= MAX_PHOTO_BYTES or (quality <= 40 and max_width <= 480):
            return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")
        if quality > 40:
            quality -= 12
        else:
            max_width = int(max_width * 0.8)


def fetch_photo_data_url(session, image_url: str | None) -> str | None:
    """Скачать фото и вернуть его как сжатый data-URL.

    Прямая ссылка на CDN encar не отдаёт картинку при запросе с чужого
    сайта (защита от хотлинков по Referer) — в браузере bn-auto она
    показывается сломанной. Скрипт сам скачивает фото с правильным
    Referer и кладёт в bn-auto уже готовым изображением, а не ссылкой.
    """
    if not image_url:
        return None

    headers = {
        "Referer": "https://fem.encar.com/",
        "Accept": "image/webp,image/apng,image/*,*/*;q=0.8",
    }
    try:
        resp = session.get(image_url, headers=headers, timeout=20)
        resp.raise_for_status()
        return _compress_to_data_url(resp.content)
    except Exception as error:
        print(f"Не удалось скачать фото {image_url}: {error}")
        return None


def push_to_bn_auto(session, cars: list[dict]):
    """Отправить объявления в bn-auto. Без настроенных переменных — просто пропустить."""
    if not BN_AUTO_URL or not BN_AUTO_IMPORT_TOKEN:
        print("BN_AUTO_URL / BN_AUTO_IMPORT_TOKEN не заданы — пуш в bn-auto пропущен.")
        return

    import requests

    listings = []
    for c in cars:
        if not c.get("external_id"):
            continue
        d = c.get("detail") or {}
        photo = fetch_photo_data_url(session, d.get("photo")) or fetch_photo_data_url(session, c.get("image"))
        listings.append({
            "external_id": c["external_id"],
            "make": d.get("make") or c.get("brand_en"),
            "model": d.get("model") or c.get("model_guess"),
            "title": c.get("title"),
            "year": d.get("year") or c.get("year"),
            "mileage_km": d.get("mileage_km") or c.get("mileage_km"),
            "price_value": c.get("price_krw"),
            "photo_url": photo,
            "spec": d.get("spec"),
            "options": d.get("options"),
            "source_url": c.get("link"),
        })
    if not listings:
        print("Нет объявлений с external_id — нечего пушить в bn-auto.")
        return

    # С фото внутри пачка из сотни машин весит десятки МБ — шлём по 10.
    batch_size = 10
    for start in range(0, len(listings), batch_size):
        batch = listings[start:start + batch_size]
        resp = requests.post(
            f"{BN_AUTO_URL}/api/live-listings/import",
            json={"source": "encar", "listings": batch},
            headers={"Authorization": f"Bearer {BN_AUTO_IMPORT_TOKEN}"},
            timeout=60,
        )
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code >= 400:
            print(f"Пуш в bn-auto не удался: HTTP {resp.status_code} {data}")
            resp.raise_for_status()
        print(f"Пуш в bn-auto [{start + 1}–{start + len(batch)}]: {data.get('stats')}, пропущено {data.get('skipped', 0)}")


if __name__ == "__main__":
    main()
