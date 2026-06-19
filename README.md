# Hornbach.de Product Scraper

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![Playwright](https://img.shields.io/badge/browser-Playwright-green.svg)](https://playwright.dev/python/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**Open-source Python web scraper for [hornbach.de](https://www.hornbach.de)** — extract product listings, prices, specifications, and availability from Germany's leading DIY & home-improvement retailer. Built with **Playwright** for JavaScript-rendered pages, **proxy rotation** for geo-restricted access, and optional **Google Sheets** export for e-commerce data pipelines.

> **Keywords:** hornbach scraper · python web scraper · playwright scraper · e-commerce product scraper · DIY retailer data extraction · german proxy scraper · product specification scraper · google sheets export · price monitoring · competitive intelligence · retail data mining

---

## Table of Contents

- [Features](#features)
- [Who Is This For?](#who-is-this-for)
- [Project Structure](#project-structure)
- [Quick Start](#quick-start)
- [Proxy Setup](#proxy-setup)
- [Google Sheets API Setup](#google-sheets-api-setup)
- [CLI Reference](#cli-reference)
- [Data Fields](#data-fields)
- [Configuration Reference](#configuration-reference)
- [Anti-Detection Measures](#anti-detection-measures)
- [Utility Scripts](#utility-scripts)
- [Troubleshooting](#troubleshooting)
- [Legal & Responsible Use](#legal--responsible-use)
- [Contributing](#contributing)
- [License](#license)

---

## Features

- **Playwright browser automation** — handles JavaScript-rendered product pages and dynamic content
- **Proxy support** — SOCKS5 and HTTP proxies for Germany-geo-restricted access
- **Proxy rotation** — automatic failover across a proxy pool on HTTP 403/429
- **User-Agent rotation** — randomized browser fingerprints per session
- **Cookie consent handling** — automatic GDPR banner dismissal
- **Category & search scraping** — navigate product listings or search results
- **Pagination** — automatically follows all listing pages
- **Product detail enrichment** — visit each PDP for full descriptions and spec tables
- **Specification scraper** — per-category CSV export with dynamic spec columns (`spec_scraper.py`)
- **Google Sheets integration** — batch upload via service account
- **CSV & JSON export** — local backup of all scraped data
- **Rate limiting** — configurable random delays between requests
- **Retry logic** — exponential backoff on failures
- **Resume support** — checkpoint state for long-running spec scrapes
- **Parallel workers** — high-throughput async fetchers for large catalogs
- **Clean architecture** — modular codebase with separate concerns

---

## Who Is This For?

| Use case | How this project helps |
| --- | --- |
| **Price monitoring** | Track Hornbach product prices over time via CSV or Sheets |
| **Market research** | Export full category catalogs with specs for analysis |
| **E-commerce data pipelines** | Feed product data into databases, BI tools, or ML models |
| **Competitive intelligence** | Compare DIY/home-improvement product assortments |
| **Learning web scraping** | Reference implementation for Playwright + proxy + anti-bot patterns |

---

## Project Structure

```
scraper/
├── main.py                  # CLI entry point (category/search scraping)
├── spec_scraper.py          # Full spec-table scraper (per-category CSVs)
├── local_spec_scraper.py    # Offline scraper for local hornbach.de mirrors
├── config.py                # Configuration loaded from .env
├── models.py                # Product data model
├── browser.py               # Playwright browser manager (proxy, stealth)
├── scraper.py               # Core scraping & parsing logic
├── sheets.py                # Google Sheets API integration
├── requirements.txt         # Python dependencies
├── .env.example             # Example environment configuration
├── product-info.csv.example # Example category list for spec scrapers
├── run_local_scraper.sh     # Runner for local mirror scraper
├── run_parallel.sh          # Parallel spec_scraper launcher
├── run_fetch_remaining.sh   # Background process manager for fetch_remaining
├── scratch/                 # Utility scripts for gap-filling & backfills
│   ├── fast_missing_fetcher.py
│   ├── patch_missing_fields.py
│   ├── fetch_remaining.py
│   ├── compare_all.py
│   └── ...
├── LICENSE
└── README.md
```

---

## Quick Start

### 1. Clone & Install

```bash
git clone https://github.com/usmanmanii/scraper.git
cd scraper

# Create a virtual environment
python3 -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Install Playwright browsers
playwright install chromium
```

### 2. Configure Environment

```bash
# Copy the example env file
cp .env.example .env

# Edit with your values
nano .env   # or use any text editor
```

**Required settings (for live scraping):**

| Variable | Description |
| --- | --- |
| `PROXY_URL` | German SOCKS5 or HTTP proxy ([Proxy Setup](#proxy-setup)) |
| `GOOGLE_SHEET_ID` | Your Google Sheet ID ([Google Sheets Setup](#google-sheets-api-setup)) |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | Path to service account credentials JSON |

> Google Sheets is optional — use `--no-sheets` to export locally only.

### 3. Set Up Categories (spec scraper)

For `spec_scraper.py` and utility scripts, copy the example category file:

```bash
cp product-info.csv.example product-info.csv
# Edit product-info.csv with your target categories, URLs, and page counts
```

### 4. Run the Scraper

```bash
# Scrape a category
python main.py --category "/c/elektrowerkzeuge/S1196/" --name "Power Tools"

# Search for products
python main.py --search "bohrmaschine" --pages 3

# Scrape all predefined categories
python main.py --all

# Scrape without Google Sheets (local only)
python main.py --search "akkuschrauber" --no-sheets

# Scrape without a proxy (if you have a German VPN)
python main.py --search "bohrmaschine" --no-proxy

# Scrape with full product details
python main.py --category "/c/farben/S882/" --details --pages 2

# Full specification scraper (all categories from product-info.csv)
python spec_scraper.py

# Offline extraction from a local mirror
./run_local_scraper.sh
```

---

## Proxy Setup

**hornbach.de is geo-restricted to Germany.** Route requests through a German IP.

### Option A: SOCKS5 Proxy

```env
PROXY_URL=socks5://username:password@proxy-host:1080
```

### Option B: HTTP/HTTPS Proxy

```env
PROXY_URL=http://username:password@proxy-host:8080
```

### Option C: Proxy Rotation Pool

```env
PROXY_URL=socks5://u1:p1@host1:1080
PROXY_POOL=socks5://u1:p1@host1:1080,http://u2:p2@host2:8080,socks5://u3:p3@host3:1080
```

### Where to Get German Proxies

| Provider | Type | Notes |
| --- | --- | --- |
| **Bright Data** | Residential | Best for avoiding blocks |
| **Oxylabs** | Datacenter/Residential | German IPs available |
| **SmartProxy** | Rotating | Good for large-scale scraping |
| **ProxyMesh** | HTTP | Budget-friendly |
| **NordVPN SOCKS5** | SOCKS5 | If you have a NordVPN subscription |

### Using a VPN Instead

If you're connected to a German VPN, leave `PROXY_URL` empty:

```env
PROXY_URL=
```

---

## Google Sheets API Setup

### Step 1: Create a Google Cloud Project

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project (or select an existing one)
3. Enable the **Google Sheets API** and **Google Drive API**:
   - Navigate to **APIs & Services → Library**
   - Search for "Google Sheets API" → Click **Enable**
   - Search for "Google Drive API" → Click **Enable**

### Step 2: Create a Service Account

1. Go to **APIs & Services → Credentials**
2. Click **Create Credentials → Service Account**
3. Give it a name (e.g., `hornbach-scraper`)
4. Click **Done**
5. Click on the new service account → **Keys** tab
6. Click **Add Key → Create new key → JSON**
7. Save the downloaded file as `credentials/service_account.json`:

```bash
mkdir -p credentials
mv ~/Downloads/your-project-*.json credentials/service_account.json
```

### Step 3: Create a Google Sheet

1. Go to [Google Sheets](https://sheets.google.com) and create a new spreadsheet
2. Copy the **Sheet ID** from the URL:
   ```
   https://docs.google.com/spreadsheets/d/THIS_IS_THE_SHEET_ID/edit
   ```
3. **Share the spreadsheet** with the service account email (found in the JSON file under `client_email`):
   - Click **Share** → enter the service account email → give **Editor** access

### Step 4: Update `.env`

```env
GOOGLE_SERVICE_ACCOUNT_FILE=credentials/service_account.json
GOOGLE_SHEET_ID=your_sheet_id_here
GOOGLE_WORKSHEET_NAME=Products
```

---

## CLI Reference

```
usage: main.py [-h] (--category CATEGORY | --search SEARCH | --all)
               [--name NAME] [--pages PAGES] [--details] [--no-sheets]
               [--clear] [--output-format {csv,json,both}] [--no-proxy]

Hornbach.de Product Scraper

options:
  --category, -c   Category URL path (e.g. /c/badarmaturen/S1429/)
  --search, -s     Search query (e.g. "bohrmaschine")
  --all, -a        Scrape all predefined categories
  --name, -n       Category name label
  --pages, -p      Max pages per category (0 = unlimited)
  --details, -d    Fetch detail pages for richer data (slower)
  --no-sheets      Skip Google Sheets upload
  --clear          Clear sheet before writing
  --output-format  csv, json, or both (default: both)
  --no-proxy       Disable proxy usage explicitly
```

---

## Data Fields

Each product record contains:

| Field | Description | Example |
| --- | --- | --- |
| `name` | Product title | Bosch PST 650 Stichsäge |
| `price` | Numeric price | 59.95 |
| `currency` | Currency code | EUR |
| `product_url` | Full URL to product page | https://www.hornbach.de/p/... |
| `description` | Product description (from detail page) | Leistungsstarke Stichsäge… |
| `image_url` | Product image URL | https://…/image.jpg |
| `availability` | Stock/availability status | Online bestellbar |
| `article_number` | Hornbach article number | 8633015 |
| `category` | Category label | Power Tools |
| `specs` | Key-value spec table (spec scraper) | Material: Stahl, Gewicht: 2.1 kg |

---

## Configuration Reference

All settings in `.env`:

| Variable | Default | Description |
| --- | --- | --- |
| `PROXY_URL` | *(empty)* | Primary proxy URL |
| `PROXY_POOL` | *(empty)* | Comma-separated proxy list |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | `credentials/service_account.json` | Path to SA JSON key |
| `GOOGLE_SHEET_ID` | *(empty)* | Target spreadsheet ID |
| `GOOGLE_WORKSHEET_NAME` | `Products` | Worksheet tab name |
| `HORNBACH_BASE_URL` | `https://www.hornbach.de` | Base URL |
| `MIN_DELAY` | `2.0` | Min delay between requests (seconds) |
| `MAX_DELAY` | `5.0` | Max delay between requests (seconds) |
| `MAX_PAGES` | `0` | Global max pages (0 = unlimited) |
| `MAX_RETRIES` | `3` | Max retries per request |
| `HEADLESS` | `true` | Run browser in headless mode |
| `LOG_LEVEL` | `INFO` | Logging verbosity |

---

## Anti-Detection Measures

The scraper implements several techniques to reduce blocking risk:

1. **Random User-Agent rotation** — each browser session uses a different realistic UA
2. **German locale headers** — `Accept-Language: de-DE`, timezone `Europe/Berlin`
3. **Human-like delays** — random waits between requests (configurable)
4. **Proxy rotation** — automatic switchover on HTTP 403/429
5. **Cookie consent handling** — proper GDPR banner dismissal
6. **Realistic viewport** — 1920×1080 resolution
7. **Exponential backoff** — retries with increasing delays on failures

---

## Utility Scripts

These scripts live in `scratch/` and complement the main scraper. Run them after the main scraper has collected data.

### 1. `scratch/fast_missing_fetcher.py` — Fetch Missing Products

Fetches product pages listed in `missing_urls.txt` that are not yet in your CSVs.

```bash
source venv/bin/activate
python scratch/fast_missing_fetcher.py
```

### 2. `scratch/patch_missing_fields.py` — Backfill Empty Fields

Patches incomplete rows (e.g. missing `price`, `image_url`) without re-scraping everything.

```bash
python scratch/patch_missing_fields.py
```

### 3. `scratch/fetch_remaining.py` — Full Coverage Fetch

Re-crawls all listing pages, discovers every product URL, and fetches missing products.

```bash
# First run (full listing re-crawl)
./run_fetch_remaining.sh start

# Resume after stop/crash
./run_fetch_remaining.sh resume

# Check status and logs
./run_fetch_remaining.sh status
./run_fetch_remaining.sh logs

# Coverage report
./run_fetch_remaining.sh check
```

**CLI options:**

```
--categories "Garten,Bad"   Only process specific categories
--workers 30                Parallel product-fetch workers (default: 30)
--listing-workers 25        Parallel listing-page workers (default: 20)
--dry-run                   Show missing counts without writing CSVs
--refresh                   Force full re-crawl of listing pages
```

### 4. `scratch/compare_all.py` — Coverage Report

Compares scraped CSV row counts against expected totals in `product-info.csv`.

```bash
python scratch/compare_all.py
```

### Recommended order of operations

```
1. python main.py --all                              # initial full scrape
2. python scratch/fast_missing_fetcher.py            # fill gaps from missing_urls.txt
3. python scratch/patch_missing_fields.py            # backfill empty fields
4. ./run_fetch_remaining.sh start                    # discover & fetch remaining products
5. python scratch/compare_all.py                     # verify coverage
```

---

## Troubleshooting

### "HTTP 403 / 406 – Access Denied"

Your proxy is not in Germany, or the IP is blacklisted. Try a different German proxy.

### "No products found on page"

The site may have changed its HTML structure. Check the CSS selectors in `scraper.py`.

### "Spreadsheet not found"

Ensure the Google Sheet is shared with the service account email (Editor access).

### "Playwright browser not installed"

```bash
playwright install chromium
```

### "No categories found in product-info.csv"

Copy and edit the example file:

```bash
cp product-info.csv.example product-info.csv
```

### Debugging mode

Set `HEADLESS=false` in `.env` to watch the browser:

```env
HEADLESS=false
LOG_LEVEL=DEBUG
```

---

## Legal & Responsible Use

This project is provided for **educational and research purposes**. Before scraping any website:

- Read and comply with [hornbach.de Terms of Service](https://www.hornbach.de/)
- Respect `robots.txt` and rate limits
- Do not overload servers — use the built-in delays and reasonable worker counts
- Do not use scraped data in ways that violate copyright or consumer protection laws
- You are solely responsible for how you use this software

---

## Contributing

Contributions are welcome! To get started:

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/my-improvement`)
3. Commit your changes with a clear message
4. Open a Pull Request

Please keep changes focused and include a brief description of what you changed and why.

web-scraper
python
playwright
e-commerce
hornbach
product-scraper
web-scraping
price-monitoring
google-sheets
retail-data

---

## License

[MIT License](LICENSE) — use freely, but always scrape responsibly.
