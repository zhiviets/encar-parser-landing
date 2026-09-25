
import base64
import io
import json
import os
from datetime import date
import random
import re
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

from brand_map import extract_brand_model
import encar_ru
import coverage
import selection

# Фото храним в базе bn-auto (диск ограничен): WebP до 960 px и до 150 КБ —
# карточка ~70–120 КБ вместо ~300–800 КБ JPEG.
MAX_PHOTO_BYTES = 150 * 1024
PHOTO_MAX_WIDTH = 960
PHOTO_QUALITY = 72




def list_url(action: str, page_no: int) -> str:
    """Страница списка encar с заданным условием поиска (как в адресной строке сайта)."""
    from urllib.parse import quote
    search = {"type": "car", "action": action, "toggle": {}, "layer": "", "sort": "MobileModifiedDate"}
    return f"https://car.encar.com/list/car?page={page_no}&search=" + quote(
        json.dumps(search, ensure_ascii=False, separators=(",", ":")), safe="(),."
    )


# Сколько машин собирать — квоты в selection.QUOTAS (по расписанию 750 до
# 160 л.с. + 250 любой мощности = 1000). Страницы листаются, пока квота не
# наберётся, но не больше ENCAR_MAX_PAGES на поиск (машин до 160 л.с. среди
# новых машин меньшинство — страниц нужно много). Это запасной путь, если поиск
# API encar (coverage.py — все модели) не ответил.
MAX_PAGES = int(os.environ.get("ENCAR_MAX_PAGES") or "80")
# Порция машин между паузами и длина паузы в минутах
BATCH = int(os.environ.get("ENCAR_BATCH") or "100")
BATCH_PAUSE = float(os.environ.get("ENCAR_BATCH_PAUSE") or "1.5")
# Заполнение каталога: пока машин с полной информацией на сайте меньше FILL_TARGET — каждый
# прогон добавляет до FILL_PER_RUN новых порциями по BATCH с паузой BATCH_PAUSE (1–2 мин) и в
# конце запускает следующий. Потом — обновление два раза в неделю (UPDATE_DAYS, 0 — понедельник,
# первый прогон дня): до UPDATE_NEW новых порциями по UPDATE_BATCH с паузой UPDATE_PAUSE минут.
# ENCAR_TOTAL > 0 (ручной запуск) — ровно столько новых.
FILL_TARGET = int(os.environ.get("ENCAR_FILL_TARGET") or "5500")
FILL_PER_RUN = int(os.environ.get("ENCAR_FILL_PER_RUN") or "1000")
UPDATE_DAYS = {int(d) for d in (os.environ.get("ENCAR_UPDATE_DAYS") or "0,3").split(",") if d.strip()}
UPDATE_NEW = int(os.environ.get("ENCAR_UPDATE_NEW") or "600")
UPDATE_BATCH = int(os.environ.get("ENCAR_UPDATE_BATCH") or "150")
UPDATE_PAUSE = float(os.environ.get("ENCAR_UPDATE_PAUSE") or "30")
# Сколько машин с сайта, не встреченных в поиске, проверить по API (продаётся ли ещё)
VERIFY_LIMIT = int(os.environ.get("ENCAR_VERIFY") or "300")
# Через столько минут после старта новые порции не начинаем: GitHub обрывает прогон через
# 6 ч (timeout 355 мин), а оборванный прогон не запускает следующий. Порция — до ~30 мин.
RUN_MINUTES = float(os.environ.get("ENCAR_RUN_MINUTES") or "300")
# Машину с сайта, которой не нашлась точная мощность на drom.ru, открываем снова не раньше чем через
# столько дней (каталог drom.ru дополняется, разбор улучшается)
DROM_RETRY_DAYS = 14
STARTED = time.time()


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
ACCEPT_LANGUAGE = "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7"
ENCAR_VEHICLE_API = "https://api.encar.com/v1/readside/vehicle/{id}"
ENCAR_VEHICLE_INCLUDE = "ADVERTISEMENT,CATEGORY,CONDITION,CONTACT,MANAGE,OPTIONS,PHOTOS,SPEC,PARTNERSHIP,CENTER,VIEW"
OUT_PATH.parent.mkdir(parents=True, exist_ok=True)




def slow_human_pause(sec: float = 1.0):
    time.sleep(sec)


def human_pause(low: float, high: float):
    """Случайная пауза: ровный ритм запросов — первый признак бота."""
    time.sleep(random.uniform(low, high))


class Blocked(Exception):
    """Сайт начал отвечать капчей / 403 / 429 — дальше не долбим."""

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

    # Листаем страницу постепенно, как человек, — заодно догружаются карточки
    for _ in range(random.randint(4, 7)):
        page.mouse.wheel(0, random.randint(500, 1100))
        page.wait_for_timeout(random.randint(350, 900))

    items = page.locator('div[class^="ItemBigImage_item__"]')
    n = min(items.count(), 100)

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
    known_all = fetch_known()
    total = run_size(known_all)
    if not total:
        return
    with sync_playwright() as p:
        # Firefox не умеет авторизацию (логин/пароль) в SOCKS5-прокси —
        # Playwright падает с "Browser does not support socks5 proxy
        # authentication". Chromium это поддерживает.
        browser = p.chromium.launch(
            headless=True,
            slow_mo=80,
            # Без этого флага Chromium сам сообщает сайту, что им управляет программа
            args=["--disable-blink-features=AutomationControlled"],
        )

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
            timezone_id="Asia/Seoul",
            extra_http_headers={"Accept-Language": ACCEPT_LANGUAGE},
            proxy=proxy,
        )
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
        page = context.new_page()

        session = http_session()
        # Машины с полной информацией заново не собираем; неполные — как новые (обновятся)
        known = {k: v for k, v in known_all.items() if v.get("complete", True)}
        le160_share = float(os.environ.get("ENCAR_SHARE_160") or "0.75")
        selection.QUOTAS.update(le160=round(total * le160_share), other=total - round(total * le160_share))
        pacer = Pacer()

        # Точная мощность — каталог drom.ru; его таблицы дорисовываются скриптом, поэтому браузер
        import drom_specs
        power_counts = {"found": 0, "none": 0}
        drom_browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        drom = drom_specs.DromCatalog(
            str(Path(__file__).resolve().parent / "drom_cache.json"),
            drom_specs.playwright_fetcher(drom_browser.new_context(user_agent=UA, locale="ru-RU")),
            max_requests=int(os.environ.get("DROM_MAX_PAGES") or "300"))

        # Порциями по ходу отбора: набралось BATCH выбранных машин — детали, фото, отправка на
        # сайт — пауза BATCH_PAUSE минут, отбор продолжается. Машины появляются на сайте сразу,
        # а не после отбора всей тысячи (он уточняет мощность по API и идёт долго).
        kept, buf = [], []
        state = {"batches": 0, "option_codes": None, "late": 0}

        def process(chunk, last=False):
            if not chunk:
                return
            if state["option_codes"] is None:
                state["option_codes"] = capture_network_sample(page, chunk[0].get("link")) if chunk[0].get("link") else {}
            state["batches"] += 1
            print(f"=== Порция {state['batches']}: {len(chunk)} машин, {(time.time() - STARTED) / 60:.0f} мин от старта ===")
            enrich_with_details(session, chunk, known, pacer)
            chunk = [c for c in chunk if still_ok(c)]
            add_drom_power(chunk, drom, power_counts)
            drom.save()
            push_to_bn_auto(session, chunk, known, state["option_codes"])
            kept.extend(chunk)
            if not last:
                pause = BATCH_PAUSE * random.uniform(0.7, 1.3)
                print(f"Пауза {pause:.1f} мин, чтобы не нагружать encar")
                time.sleep(pause * 60)
                pacer.blocked = False   # после паузы даём encar ещё шанс

        def on_take(car):
            if time.time() - STARTED > RUN_MINUTES * 60:
                state["late"] += 1      # время прогона вышло — машина уйдёт в следующий прогон
                return
            buf.append(car)
            if len(buf) >= BATCH:
                chunk = buf[:]
                buf.clear()
                process(chunk)

        cars, touched = None, []
        if os.environ.get("ENCAR_ALL_MODELS", "1") == "1":
            # Все модели — через поиск API encar; не вышло — общий список на сайте, как раньше
            try:
                cars, touched = coverage.collect_all_models(session, known, pacer, parse_encar_detail, power_of,
                                                            _save_debug, total=total, on_take=on_take,
                                                            touched_out=touched)
            except Exception as error:
                print(f"Перебор моделей через поиск encar не удался ({error}) — берём общий список")
                if state["batches"] or buf:
                    cars = []           # часть уже отправлена — общий список не нужен
        if cars is None and total:
            le160, other, _ = collect_cars(page, session, known, pacer)
            cars = le160 + other
            for car in cars:
                on_take(car)
        process(buf[:], last=True)
        buf.clear()
        if state["late"]:
            print(f"Прошло {RUN_MINUTES:g} мин — {state['late']} выбранных машин в следующий прогон")

        # Машины с сайта, встреченные в поиске: отметка «ещё в продаже» (старым — VIN и привод),
        # затем давно не встречавшиеся — проверка по API. После новых: при заполнении важнее новые.
        backfill_left = int(os.environ.get("ENCAR_BACKFILL") or "150")
        if touched:
            print(f"=== Машины с сайта, встреченные в поиске: {len(touched)} ===")
            tried = drom.cache.setdefault("power_tried", {})
            backfill_left -= backfill_known(session, touched, known, pacer, limit=backfill_left, tried=tried)
            add_drom_power([c for c in touched if c.get("backfill")], drom, power_counts, tried)
            push_to_bn_auto(session, touched, known, state["option_codes"] or {})
        seen = {c["external_id"] for c in touched} | {c["external_id"] for c in (cars or [])}
        push_to_bn_auto(session, verify_known(session, known_all, seen, pacer), known_all)
        drom.save()
        drom_browser.close()
        browser.close()
    print(f"Мощность с drom.ru: найдена у {power_counts['found']}, не найдена у {power_counts['none']} "
          f"(совпадения: {drom.stats}, страниц drom.ru {drom.requests})")

    if not kept:
        return
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        landing = [{k: v for k, v in c.items() if k != "detail"} for c in kept]
        json.dump({"updated_at": int(time.time()), "cars": landing}, f, ensure_ascii=False, indent=2)
    print(f"Saved {len(kept)} cars -> {OUT_PATH}")


def run_size(known_all: dict) -> int:
    """Сколько новых машин добавить: вручную (ENCAR_TOTAL); до заполнения каталога (FILL_TARGET) —
    по FILL_PER_RUN за прогон; потом в дни обновления (первый прогон дня) — UPDATE_NEW порциями
    по UPDATE_BATCH с паузой UPDATE_PAUSE; 0 — сейчас ничего не нужно."""
    global BATCH, BATCH_PAUSE
    manual = int(os.environ.get("ENCAR_TOTAL") or "0")
    if manual:
        return manual
    good = sum(1 for i in known_all.values() if i.get("complete") and i.get("published"))
    if good < FILL_TARGET:
        n = min(FILL_PER_RUN, FILL_TARGET - good)
        print(f"Заполнение каталога: на сайте {good} из {FILL_TARGET} — добавим {n}")
        # Каталог ещё не заполнен — workflow сразу запустит следующий прогон (без остановки)
        open(Path(__file__).resolve().parent / "continue_fill", "w").close()
        return n
    now = time.gmtime()
    if now.tm_wday in UPDATE_DAYS and now.tm_hour < 8:
        BATCH, BATCH_PAUSE = UPDATE_BATCH, UPDATE_PAUSE
        print(f"Каталог заполнен ({good}) — обновление: до {UPDATE_NEW} новых, порции по {BATCH} с паузой {BATCH_PAUSE:g} мин")
        return UPDATE_NEW
    print(f"Каталог заполнен ({good}), сейчас не время обновления — прогон окончен")
    return 0


def verify_known(session, known_all: dict, seen: set, pacer) -> list[dict]:
    """Машины с сайта, не встреченные в поиске и не обновлявшиеся неделю: спрашиваем API encar.
    В продаже — отметка «ещё в продаже»; снята — не трогаем, через 30 дней сайт её скроет."""
    todo = [(k, i) for k, i in known_all.items() if k not in seen and (i.get("seen_days") or 0) >= 7]
    todo.sort(key=lambda x: -(x[1].get("seen_days") or 0))
    out = []
    for key, info in todo[:VERIFY_LIMIT]:
        if pacer.blocked:
            break
        detail = pacer.detail(session, key)
        status = ((detail or {}).get("advertisement") or {}).get("status")
        if detail and status in (None, "ADVERTISE"):
            out.append({"external_id": key, "link": info.get("url") or f"https://fem.encar.com/cars/detail/{key}"})
    print(f"Проверено по API машин с сайта, не встреченных в поиске: {min(len(todo), VERIFY_LIMIT)} из {len(todo)}, "
          f"в продаже {len(out)}")
    return out


def complete_listing(x: dict) -> bool:
    """Полная информация: фото, цена, год, марка, модель и то, по чему считается таможня —
    объём (мощность сайт оценит по нему), у электромобилей — мощность с drom.ru."""
    spec = x.get("spec") or {}
    engine = spec.get("Мощность, л.с.") if spec.get("Топливо") == "электро" else spec.get("Объём, см³")
    return bool(x.get("photo_url") and x.get("price_value") and x.get("year") and x.get("make") and x.get("model")
                and engine)


class Pacer:
    """Темп запросов к API encar и предохранитель от блокировки.

    Случайная пауза после каждого запроса и длинный перерыв каждые ~25.
    На 403/429/капчу — пауза 90–150 с и одна повторная попытка, потом
    blocked=True и запросы к API больше не идут: лучше отправить в bn-auto
    то, что собрано, чем спалить IP прокси.
    """

    def __init__(self):
        self.count = 0
        self.next_break = random.randint(25, 40)
        self.blocked = False
        self.saved = 0

    def _tick(self):
        self.count += 1
        if self.count >= self.next_break:
            # Темп человека: перерыв 1–2,5 мин каждые 25–40 объявлений, между ними 2–5 с
            human_pause(60, 150)
            self.next_break = self.count + random.randint(25, 40)
        else:
            human_pause(2.0, 5.0)

    def detail(self, session, vehicle_id: str):
        if self.blocked:
            return None
        try:
            try:
                data = fetch_encar_detail(session, vehicle_id)
            except Blocked as reason:
                print(f"encar притормозил нас ({reason}) — пауза 90–150 с и одна попытка")
                human_pause(90, 150)
                try:
                    data = fetch_encar_detail(session, vehicle_id)
                except Blocked as again:
                    print(f"Снова блок ({again}) — останавливаем запросы к encar, отправляем собранное")
                    self.blocked = True
                    return None
        finally:
            self._tick()
        if data and self.saved < 3:
            _save_debug(f"vehicle_{vehicle_id}.json", data)
            self.saved += 1
        return data


def _card_to_car(c: dict) -> dict | None:
    """Карточка списка → машина с категорией, либо None, если не подходит."""
    if not (c.get("image") and c.get("link")):
        return None
    raw_title = c.get("brand") or ""
    brand_en, model_guess, _ = extract_brand_model(raw_title)
    if not selection.eligible(brand_en, c.get("year")):
        return None
    return {
        "external_id": _external_id(c.get("link")),
        # Оценка мощности по названию: «gt160» — сразу ясно, что мощнее
        "power": selection.power_class(raw_title),
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
    }


def iter_candidates(page, profile: dict, seen: set):
    """Кандидаты со страниц поиска профиля; если фильтр поиска пуст — из общего списка."""
    yielded = 0
    attempts = [(profile["action"], profile["name"]),
                (selection.BASE_ACTION, profile["name"] + ", общий список")]
    for n, (action, label) in enumerate(attempts):
        if n == 1:
            if yielded:
                return
            print(f"[{profile['name']}] фильтр поиска ничего не дал — берём общий список и фильтруем сами")
        for page_no in range(1, MAX_PAGES + 1):
            try:
                found = scrape_list(page, list_url(action, page_no))
            except Exception as error:
                print(f"[{label}] страница {page_no} не загрузилась ({error})")
                break
            if not found:
                break
            fresh = [car for car in map(_card_to_car, found) if car and car["external_id"] not in seen]
            print(f"[{label}] страница {page_no}: подходящих по году и марке {len(fresh)}")
            for car in fresh:
                seen.add(car["external_id"])
                yielded += 1
                yield car
            human_pause(4, 9)


def power_of(model, text, cc) -> str | None:
    """'le160' / 'gt160' / None — мощность не оценить."""
    try:
        cc = int(float(cc)) if cc else None
    except (TypeError, ValueError):
        cc = None
    return selection.classify(selection.power_class(text, cc), model=model, title=text)


def collect_cars(page, session, known: dict, pacer: Pacer):
    """Набрать квоты по списку поиска: 75% до 160 л.с., остальное — любой мощности.

    Мощность машины, которая может оказаться «до 160», проверяем по API
    encar, как только она встретилась в списке (эти данные потом идут в
    объявление — второй раз не запрашиваем). Уже известные bn-auto машины
    проверяем по их сохранённым данным — у encar ничего не запрашиваем.
    Из импорта берём только долю selection.IMPORT_SHARE каждой группы.
    """
    seen = set()
    picked = {"le160": [], "other": []}
    quota = selection.QUOTAS
    checked = dropped = 0
    first_link = None

    for profile in selection.PROFILES:
        cap = {b: round(quota[b] * selection.IMPORT_SHARE[b]) if profile["import"] else quota[b] for b in quota}
        taken = {b: 0 for b in quota}

        def need(bucket):
            return len(picked[bucket]) < quota[bucket] and taken[bucket] < cap[bucket]

        if not (need("le160") or need("other")):
            continue
        for car in iter_candidates(page, profile, seen):
            first_link = first_link or car["link"]
            # Мощнее 160 по названию — сразу «любой мощности»; остальных уточняем по данным
            # encar. Машину, мощность которой не оценить (нет объёма), не берём — см. still_ok()
            bucket = "other"
            if car["power"] != "gt160" and need("le160"):
                power = None
                info = known.get(car["external_id"])
                if info:
                    power = power_of(info.get("model"), f"{info.get('text') or ''} {car['title']}", info.get("cc"))
                elif not pacer.blocked:
                    detail = pacer.detail(session, car["external_id"])
                    if detail:
                        checked += 1
                        d = parse_encar_detail(detail, car["external_id"])
                        car["detail"] = d
                        power = power_of(d.get("model"), f"{d.get('power_text') or ''} {car['title']}",
                                         d.get("displacement"))
                        dropped += power != "le160"
                bucket = {"le160": "le160", "gt160": "other"}.get(power)
            elif car["external_id"] in known:
                info = known[car["external_id"]]
                if not power_of(info.get("model"), f"{info.get('text') or ''} {car['title']}", info.get("cc")):
                    bucket = None   # уже на сайте, но мощность не оценить (нет объёма) — больше не обновляем
            if bucket and need(bucket):
                car["bucket"] = bucket
                picked[bucket].append(car)
                taken[bucket] += 1
            if not (need("le160") or need("other")) or (pacer.blocked and not need("other")):
                break
        print(f"[{profile['name']}] итого: {len(picked['le160'])} до 160 л.с., {len(picked['other'])} любой мощности")

    print(f"Собрано: {len(picked['le160'])} до 160 л.с. (проверено по API {checked}, мощнее или без оценки {dropped}), "
          f"{len(picked['other'])} любой мощности")
    return picked["le160"], picked["other"], first_link


KOREAN_MAKES = {"Hyundai", "Kia", "Genesis", "Chevrolet", "Renault", "KGM"}
JAPANESE_MAKES = {"Toyota", "Lexus", "Honda", "Nissan", "Infiniti", "Mazda", "Subaru", "Mitsubishi", "Suzuki", "Acura"}


def drom_markets(make: str) -> list[str]:
    """Рынки drom.ru по очереди: корейские марки — Корея; импорт — Корея, потом рынок
    страны марки (японские — Япония), потом Европа и США."""
    if make in KOREAN_MAKES:
        return ["south-korea"]
    if make in JAPANESE_MAKES:
        return ["south-korea", "japan", "usa", "europe"]
    return ["south-korea", "europe", "usa"]


def add_drom_power(cars: list[dict], drom, counts: dict, tried: dict | None = None):
    """Точная мощность комплектации с drom.ru — в характеристики новых машин («Мощность, л.с.»).
    tried — день последней неудачной попытки по машине (см. backfill_known)."""
    import drom_specs
    today = date.today().toordinal()
    for c in cars:
        d = c.get("detail") or {}
        spec = d.get("spec")
        if not spec or spec.get("Мощность, л.с.") or not d.get("make") or not d.get("model") or not d.get("year"):
            if tried is not None and not (spec or {}).get("Мощность, л.с."):
                tried[c["external_id"]] = today      # искать нечем — не открывать её снова каждый прогон
            continue
        found = drom.power({
            "make": d["make"], "model": d["model"],
            "markets": drom_markets(d["make"]),
            "year": d["year"], "month": d.get("month"), "cc": d.get("displacement"),
            "fuel": drom_specs.norm_fuel(spec.get("Топливо")), "drive": drom_specs.norm_drive(spec.get("Привод")),
            "trans": drom_specs.norm_trans(spec.get("Трансмиссия")),
            "trim": f"{spec.get('Модификация') or ''} {spec.get('Комплектация') or ''}",
        })
        if found:
            spec["Мощность, л.с."] = str(found["hp"])
            if found.get("hp_total"):
                spec["Суммарная мощность гибрида, л.с."] = str(found["hp_total"])
            counts["found"] += 1
            if tried is not None:
                tried.pop(c["external_id"], None)
        else:
            counts["none"] += 1
            if tried is not None:
                tried[c["external_id"]] = today


def still_ok(car: dict) -> bool:
    """После API перепроверяем год выпуска, марку и что мощность вообще можно оценить
    (машины без объёма двигателя не берём; электромобили — в группе «любой мощности»)."""
    d = car.get("detail")
    if not d:
        return True
    if not power_of(d.get("model"), f"{d.get('power_text') or ''} {car['title']}", d.get("displacement")):
        return False
    return selection.eligible(d.get("make") or car.get("brand_en"), d.get("year") or car.get("year"))


def http_session():
    """requests-сессия с теми же заголовками и прокси, что и у браузера."""
    import requests
    from urllib.parse import quote, urlsplit

    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": ACCEPT_LANGUAGE})
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


OPTION_MARKERS = ("선루프", "썬루프", "\\uc120\\ub8e8\\ud504", "\\uc36c\\ub8e8\\ud504")
_OPTION_PATTERNS = [
    re.compile(r'optionCd["\']?\s*:\s*["\'](\d{3})["\']\s*,\s*["\']?optionName["\']?\s*:\s*["\']([^"\']+)["\']'),
    re.compile(r'["\']?(?:code|cd)["\']?\s*:\s*["\'](\d{3})["\']\s*,\s*["\']?(?:name|nm|text|label)["\']?\s*:\s*["\']([^"\']+)["\']'),
    re.compile(r'["\']?(?:name|nm|text|label)["\']?\s*:\s*["\']([^"\']+)["\']\s*,\s*["\']?(?:code|cd)["\']?\s*:\s*["\'](\d{3})["\']'),
    re.compile(r'["\'](\d{3})["\']\s*:\s*["\']([^"\']*[가-힣][^"\']*)["\']'),
]
_UNICODE_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")


def extract_option_codes(text: str) -> dict:
    """Найти в JS/HTML сайта таблицу «код опции → название». Пусто, если не нашлась."""
    if "\\u" in text:
        text = _UNICODE_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), text)
    best = {}
    for i, pattern in enumerate(_OPTION_PATTERNS):
        found = {}
        for m in pattern.finditer(text):
            code, name = (m.group(2), m.group(1)) if i == 2 else (m.group(1), m.group(2))
            if encar_ru.HANGUL.search(name) and len(name) <= 40:
                found.setdefault(code, name)
        if len(found) > len(best) and any("루프" in n for n in found.values()):
            best = found
    return best if len(best) >= 20 else {}


def capture_network_sample(page, url: str) -> dict:
    """Открыть одну карточку как посетитель и сохранить всё полезное для диагностики.

    Возвращает справочник «код опции → название», если нашёлся в JS/HTML
    сайта (в API encar опции идут только кодами).
    """
    DEBUG_DIR.mkdir(exist_ok=True)
    responses = []
    handler = lambda r: responses.append(r)
    page.on("response", handler)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        page.wait_for_timeout(6000)
        for _ in range(10):
            page.mouse.wheel(0, random.randint(700, 1200))
            page.wait_for_timeout(random.randint(500, 1000))
        page.wait_for_timeout(2000)
        (DEBUG_DIR / "detail_page.html").write_text(page.content(), encoding="utf-8")
    except Exception as error:
        print(f"Сетевой лог карточки не собран: {error}")
    finally:
        page.remove_listener("response", handler)

    sample = []
    option_codes = {}
    n_sources = 0
    sources = []
    for r in responses:
        if "encar" not in r.url:
            continue
        ctype = r.headers.get("content-type") or ""
        try:
            if "json" in ctype:
                body = r.text()
                sample.append({"url": r.url, "status": r.status, "body": body[:400_000]})
            elif "javascript" in ctype or "html" in ctype or "text/plain" in ctype:
                body = r.text()
            else:
                continue
        except Exception:
            continue
        if any(m in body for m in OPTION_MARKERS):
            n_sources += 1
            sources.append(r.url)
            (DEBUG_DIR / f"options_source_{n_sources}.txt").write_text(r.url + "\n\n" + body[:5_000_000], encoding="utf-8")
            codes = extract_option_codes(body)
            if len(codes) > len(option_codes):
                option_codes = codes
    try:
        html = (DEBUG_DIR / "detail_page.html").read_text(encoding="utf-8")
        codes = extract_option_codes(html)
        if len(codes) > len(option_codes):
            option_codes = codes
    except OSError:
        pass

    _save_debug("network_sample.json", sample)
    _save_debug("option_codes.json", {"sources": sources, "codes": option_codes})
    print(f"Сетевой лог карточки: {len(sample)} JSON-ответов, источников с опциями: {n_sources}, "
          f"справочник опций: {len(option_codes)} кодов")
    return option_codes


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
        except Exception as error:
            print(f"API encar {vehicle_id}: {error}")
            continue
        if resp.status_code in (403, 429):
            raise Blocked(f"HTTP {resp.status_code}")
        if resp.status_code == 200:
            try:
                return resp.json()
            except ValueError:
                # Вместо JSON пришла HTML-страница — это капча/блокировка
                raise Blocked("вместо JSON пришла страница (капча?)")
        print(f"API encar {vehicle_id}: HTTP {resp.status_code}")
    return None


def _translate_generation(category: dict) -> str | None:
    name = category.get("modelName") or ""
    group_ko = category.get("modelGroupName")
    group_en = category.get("modelGroupEnglishName")
    if group_ko and group_en:
        name = name.replace(group_ko, encar_ru.model_name(group_en))
    return encar_ru.clean_latin(name)


ENCAR_PHOTO_BASE = "https://ci.encar.com/carpicture"


def parse_encar_detail(detail: dict, vehicle_id: str) -> dict:
    """Всё, что удалось достать из API encar; каждое поле необязательно."""
    category = detail.get("category") or {}
    spec = detail.get("spec") or {}
    contact = detail.get("contact") or {}

    out = {}
    out["make"] = encar_ru.make_name(category.get("manufacturerEnglishName"))
    out["model"] = encar_ru.model_name(category.get("modelGroupEnglishName"))
    electric = spec.get("fuelName") == "전기"

    ym = re.sub(r"\D", "", str(category.get("yearMonth") or ""))
    if len(ym) >= 4:
        out["year"] = int(ym[:4])
    if len(ym) >= 6 and 1 <= int(ym[4:6]) <= 12:
        out["month"] = int(ym[4:6])
    if spec.get("mileage") is not None:
        out["mileage_km"] = _to_int(str(spec.get("mileage")))

    mileage = out.get("mileage_km")
    grade_text = " ".join(str(x) for x in (
        category.get("gradeEnglishName"), category.get("gradeName"),
        category.get("gradeDetailEnglishName"), category.get("gradeDetailName"),
    ) if x)
    ru_spec = {
        "Лот": f"№{vehicle_id}",
        "VIN": encar_ru.vin(detail.get("vin")),
        "Локация продавца": encar_ru.region(contact.get("address")),
        "Выпуск": encar_ru.release(category.get("yearMonth")),
        "Класс": encar_ru.lookup(encar_ru.BODY, spec.get("bodyName")),
        "Поколение": _translate_generation(category),
        "Модификация": category.get("gradeEnglishName") or encar_ru.clean_latin(category.get("gradeName")),
        "Комплектация": category.get("gradeDetailEnglishName") or encar_ru.clean_latin(category.get("gradeDetailName")),
        "Трансмиссия": encar_ru.lookup(encar_ru.TRANSMISSION, spec.get("transmissionName")),
        "Привод": encar_ru.drive(grade_text, out["make"], out["model"]),
        # У электромобилей в displacement лежит не объём двигателя
        "Объём, см³": str(spec["displacement"]) if spec.get("displacement") and not electric else None,
        "Пробег": f"{mileage:,}".replace(",", " ") + " км" if mileage else None,
        "Топливо": encar_ru.lookup(encar_ru.FUEL, spec.get("fuelName")),
        "Цвет": encar_ru.lookup(encar_ru.COLOR, spec.get("colorName")),
        "Мест": str(spec["seatCount"]) if spec.get("seatCount") else None,
    }
    out["spec"] = {k: v for k, v in ru_spec.items() if v}

    out["displacement"] = None if electric else (_to_int(str(spec.get("displacement") or "")) or None)
    out["power_text"] = " ".join(str(x) for x in (
        category.get("modelName"), category.get("gradeName"), category.get("gradeDetailName"),
        category.get("gradeEnglishName"), spec.get("fuelName"),
    ) if x)

    options = detail.get("options")
    if isinstance(options, dict):
        out["options"] = {k: v for k, v in options.items() if isinstance(v, list)}

    # Главное фото объявления — файл «_001»: его encar ставит на обложку и в
    # поиск. Если его нет — первое наружное по порядку.
    photos = [ph for ph in (detail.get("photos") or []) if isinstance(ph, dict) and ph.get("path")]
    photos.sort(key=lambda ph: (not str(ph["path"]).endswith("_001.jpg"), ph.get("type") != "OUTER",
                                str(ph.get("code") or "")))
    out["photo_paths"] = [ph["path"] for ph in photos]
    if photos:
        out["photo"] = ENCAR_PHOTO_BASE + photos[0]["path"]
    return out


def main_photo_url(car: dict, d: dict) -> str | None:
    """То же фото, что encar показывает на карточке в поиске, но в полном размере."""
    m = re.search(r"(\d+_\d{3})\.jpg", car.get("image") or "")
    if m:
        for path in d.get("photo_paths") or []:
            if path.endswith(m.group(1) + ".jpg"):
                return ENCAR_PHOTO_BASE + path
    return d.get("photo")


def fetch_known() -> dict:
    """Лоты, которые уже есть в bn-auto с фото и характеристиками: id → марка, модель, объём, описание."""
    if not BN_AUTO_URL or not BN_AUTO_IMPORT_TOKEN:
        return {}
    import requests
    try:
        resp = requests.get(
            f"{BN_AUTO_URL}/api/live-listings/known",
            params={"source": "encar", "all": "1"},
            headers={"Authorization": f"Bearer {BN_AUTO_IMPORT_TOKEN}"},
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        items = {str(i["id"]): i for i in data.get("items") or []}
        for vid in data.get("ids") or []:
            items.setdefault(str(vid), {})
        good = sum(1 for i in items.values() if i.get("complete", True))
        print(f"На сайте: {len(items)}, с полной информацией {good} — у encar их заново не запрашиваем, "
              f"неполные ({len(items) - good}) обновим, если встретятся")
        return items
    except Exception as error:
        print(f"Список известных лотов не получен ({error}) — запрашиваем всё")
        return {}


def enrich_with_details(session, cars: list[dict], known: dict, pacer: Pacer):
    """Дополнить машины без данных характеристиками, опциями и фото из API encar."""
    todo = [c for c in cars if not c.get("detail") and c["external_id"] not in known]
    print(f"Нужны детали для {len(todo)} машин из {len(cars)}")
    ok = 0
    for c in todo:
        if pacer.blocked:
            c["blocked"] = True
            continue
        detail = pacer.detail(session, c["external_id"])
        if not detail:
            continue
        try:
            c["detail"] = parse_encar_detail(detail, c["external_id"])
            ok += 1
        except Exception as error:
            print(f"Не разобраны детали {c['external_id']}: {error}")
    print(f"Детали из API encar: {ok} из {len(todo)}")


def backfill_known(session, cars: list[dict], known: dict, pacer: Pacer, limit: int | None = None,
                   tried: dict | None = None) -> int:
    """Машины, сохранённые до появления VIN и привода, дополняем по API и
    заменяем фото на главное (раньше бралось первое попавшееся — бывало сзади).
    Так же — машины, у которых мощность только оценена: после API им ищется точная
    мощность на drom.ru (add_drom_power). Не нашлась — такую машину снова не открываем
    DROM_RETRY_DAYS дней (tried: id → день попытки).

    Не больше ENCAR_BACKFILL за прогон, чтобы не нагружать encar: остальные
    дополнятся в следующие прогоны.
    """
    if limit is None:
        limit = int(os.environ.get("ENCAR_BACKFILL") or "150")
    tried = tried if tried is not None else {}
    today = date.today().toordinal()

    def wanted(c):
        info = known.get(c["external_id"])
        if not info or c.get("detail"):
            return False
        return not info.get("has_vin") or (
            info.get("exact_power") is False and today - tried.get(c["external_id"], 0) > DROM_RETRY_DAYS)

    # Без VIN — первыми
    todo = sorted((c for c in cars if wanted(c)), key=lambda c: bool(known[c["external_id"]].get("has_vin")))
    todo = todo[:max(limit, 0)]
    if not todo:
        return 0
    ok = 0
    for c in todo:
        if pacer.blocked:
            break
        detail = pacer.detail(session, c["external_id"])
        if not detail:
            continue
        try:
            c["detail"] = parse_encar_detail(detail, c["external_id"])
            c["backfill"] = True
            ok += 1
        except Exception as error:
            print(f"Не разобраны детали {c['external_id']}: {error}")
    print(f"Дополнено по API у машин с сайта (VIN, привод, точная мощность): {ok} из {len(todo)}")
    return len(todo)


def _compress_to_data_url(image_bytes: bytes) -> str | None:
    from PIL import Image

    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except Exception:
        return None

    # WebP: при том же качестве на ~40 % легче JPEG. 960 px по ширине хватает
    # для страницы объявления; тяжёлые снимки сжимаем сильнее, затем уменьшаем.
    quality, max_width = PHOTO_QUALITY, PHOTO_MAX_WIDTH
    while True:
        resized = img
        if resized.width > max_width:
            resized = resized.resize((max_width, max(1, round(resized.height * max_width / resized.width))), Image.LANCZOS)
        buf = io.BytesIO()
        resized.save(buf, format="WEBP", quality=quality, method=6)
        data = buf.getvalue()
        if len(data) <= MAX_PHOTO_BYTES or max_width <= 640:
            return "data:image/webp;base64," + base64.b64encode(data).decode("ascii")
        if quality > 55:
            quality -= 8
        else:
            max_width = int(max_width * 0.85)


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


def push_to_bn_auto(session, cars: list[dict], known: dict, option_codes: dict | None = None):
    """Отправить объявления в bn-auto. Без настроенных переменных — просто пропустить."""
    if not BN_AUTO_URL or not BN_AUTO_IMPORT_TOKEN:
        print("BN_AUTO_URL / BN_AUTO_IMPORT_TOKEN не заданы — пуш в bn-auto пропущен.")
        return

    import requests

    listings = []
    for c in cars:
        if not c.get("external_id"):
            continue
        if c["external_id"] in known and not c.get("backfill"):
            # Уже есть с фото и характеристиками — обновляем только цену,
            # пробег и отметку «ещё в продаже», сайт лишний раз не трогаем.
            listings.append({
                "external_id": c["external_id"],
                "price_value": c.get("price_krw"),
                "mileage_km": c.get("mileage_km"),
                "source_url": c.get("link"),
            })
            continue
        d = c.get("detail") or {}
        opts = d.get("options")
        if option_codes and opts and isinstance(opts.get("standard"), list):
            # Названия опций по-корейски — bn-auto сопоставит их с русским списком
            opts["names"] = [option_codes[code] for code in opts["standard"] if code in option_codes]
        photo = None
        if not c.get("blocked"):
            photo = fetch_photo_data_url(session, main_photo_url(c, d)) or fetch_photo_data_url(session, c.get("image"))
            human_pause(0.6, 1.8)
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
    full_count = sum(1 for x in listings if "make" in x)
    listings = [x for x in listings if "make" not in x or complete_listing(x)]
    if full_count > sum(1 for x in listings if "make" in x):
        print(f"Не отправлены без фото, цены или объёма: {full_count - sum(1 for x in listings if 'make' in x)}")
    if not listings:
        return

    # С фото внутри пачка из сотни машин весит десятки МБ — такие шлём по 10.
    # Уже известные машины (только цена и пробег) — по 200 за раз.
    light = [x for x in listings if "make" not in x]
    full = [x for x in listings if "make" in x]
    batches = [light[i:i + 200] for i in range(0, len(light), 200)]
    batches += [full[i:i + 10] for i in range(0, len(full), 10)]
    start = 0
    for batch in batches:
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
        start += len(batch)


if __name__ == "__main__":
    main()
