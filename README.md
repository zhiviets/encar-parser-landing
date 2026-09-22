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

### Local tuning

- `ENCAR_MAX_PAGES` (default `3`) — how many listing pages to scrape per run.
- `BN_AUTO_URL` / `BN_AUTO_IMPORT_TOKEN` — same as above, for a local test push.
