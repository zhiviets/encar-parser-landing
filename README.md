# ENCAR Parser & Landing 🚗

Daily updated scraper and responsive landing page for used cars from [Encar.com](https://www.encar.com).  
The project collects **brand, model, year, mileage, price, and photo** of cars and publishes them as a simple adaptive landing page.

---

## 🔗 Live Demo
👉 [View site on GitHub Pages](https://OfUndefiend.github.io/encar-parser-landing/)

---

## Features
- Scraper for [Encar.com](https://www.encar.com)  
- Extracts key details: **brand, model, year, mileage, price, image, link**  
- Best-effort Korean→Latin brand/model guess (`brand_en`/`model_guess` — see `scraper/brand_map.py`; the raw Korean title is always kept as `title`, so nothing is lost even where the guess misses)
- Saves data into [`site/data/cars.json`](site/data/cars.json)  
- Responsive landing page (`site/index.html`)  
- Automated weekly update via **GitHub Actions**, which also pushes the batch into the [bn-auto](https://github.com/zhiviets/bn-auto) live-listings catalog (`POST /api/live-listings/import`) when `BN_AUTO_URL`/`BN_AUTO_IMPORT_TOKEN` are configured  
- Mobile & desktop friendly  

---

## Local Setup

Clone the repository:
```bash
git clone https://github.com/OfUndefiend/encar-parser-landing.git
cd encar-parser-landing
```
## 1. Run the scraper
```bash

cd scraper
python -m venv .venv
.venv\Scripts\activate   # Windows
pip install -r requirements.txt
python scraper.py


Result is saved into ../site/data/cars.json.
```
## 2. Start local server for landing page
```bash
cd ../site
python -m http.server 8000


Then open: http://localhost:8000
```
---
## Automation (CI/CD)

This repo includes a GitHub Actions workflow (`.github/workflows/scrape.yml`):

- Runs the scraper weekly (Mondays, 03:00 UTC) and on manual dispatch
- Commits fresh data to `site/data/cars.json` — GitHub Pages picks it up automatically
- Pushes the same batch to bn-auto's live-listings catalog, if these repo secrets are set:
  - `BN_AUTO_URL` — e.g. `https://bn-auto.up.railway.app`
  - `BN_AUTO_IMPORT_TOKEN` — must match `SCRAPER_IMPORT_TOKEN` on the bn-auto server
  Without these secrets the workflow still runs fine, it just skips the push.

### What gets scraped

`scraper/coverage.py` and `scraper/selection.py` decide which cars go to bn-auto: cars from 2017
on, of any brand, **every model at least once**. Makes and models come from the facets of the
encar search API (the same counters the site's "manufacturer → model" filters use); for every
model the freshest `ENCAR_PER_MODEL` listings are fetched in one request. Then:

1. one car per model — up to 160 hp if the model has one;
2. more cars round-robin across models until `ENCAR_TOTAL`, with at least 75 % up to 160 hp
   (the preferential Russian recycling fee) and by model year 70 % 2022–2024, 15 % 2025–2026,
   15 % 2017–2021 (`selection.YEAR_BANDS`). If too many models only exist above 160 hp, more
   "up to 160" cars are added beyond `ENCAR_TOTAL` — the share wins over the total.

Cars already on the site are preferred within a model, so the catalogue doesn't grow run after
run. Encar doesn't expose horsepower, so it's estimated from the engine: naturally aspirated
petrol/LPG up to 2.0 L, turbo petrol up to 1.4 L, diesel and non-turbo hybrids up to 1.6 L.
EVs and larger hybrids count as "any power"; cars with no engine size to estimate from are
skipped. If the search API doesn't answer, the scraper falls back to paging through the
listing on the site.

### Being gentle with encar (avoiding IP bans)

- Cars already stored in bn-auto with photo and specs are not requested again — only their
  price and "still listed" mark are refreshed (`GET /api/live-listings/known`).
- Random pauses between pages and requests, a longer break every ~25 requests, human-like
  scrolling, `navigator.webdriver` hidden, Korean locale/timezone.
- On 403/429/captcha: one long pause and a single retry, then requests stop and whatever was
  collected is pushed.

### Local tuning

- `ENCAR_TOTAL` (default `300`) — cars per run. The workflow runs on Mondays and Thursdays with
  `1000`; a manual run takes the "total" input (default `1000`).
- `ENCAR_SHARE_160` (default `0.75`) — share of cars up to 160 hp.
- `ENCAR_BATCH` (default `100`) / `ENCAR_BATCH_PAUSE` (default `10`, minutes) — cars are detailed,
  photographed and pushed in batches with a pause between them; the model search takes a
  `ENCAR_SEARCH_BREAK`-minute break (default `3`) every 100 requests.
- `ENCAR_PER_MODEL` (default `30`) — listings fetched per model to choose from.
- `ENCAR_ALL_MODELS=0` — skip the per-model search, page through the listing instead.
- `ENCAR_MIN_YEAR` (default `2017`) — oldest model year.
- `ENCAR_MAX_PAGES` (default `80`) — max listing pages per search.
- `BN_AUTO_URL` / `BN_AUTO_IMPORT_TOKEN` — same as above, for a local test push.
