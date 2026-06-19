#!/usr/bin/env python3
"""
fetch_remaining.py – Complete the remaining 30% of product data
================================================================

The .crawl_cache.json only captured ~50% of the actual product URLs
because listing-page crawls were incomplete on previous runs.

This script:
  1. Re-crawls ALL listing pages for every category (ignores stale cache)
  2. Compares discovered URLs against existing CSV rows
  3. Builds a queue of ONLY the missing URLs (~125K products)
  4. Fetches them all concurrently and appends to category CSVs

Run:
    git clone https://github.com/usmanmanii/scraper.git && cd scraper
    source venv/bin/activate
    python scratch/fetch_remaining.py

Options:
    --workers 40          # product-fetch concurrency (default 30)
    --listing-workers 30  # listing-page crawl concurrency (default 20)
    --dry-run             # show what would be fetched without writing CSVs
    --categories "Garten" # only process specific categories
"""

import argparse
import asyncio
import csv
import json
import logging
import os
import re
import sys
import threading
import time as _time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import httpx
from bs4 import BeautifulSoup

# ── Project path ──────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

OUTPUT_DIR  = PROJECT_ROOT / "output_local" / "specs"
CSV_INFO    = PROJECT_ROOT / "product-info.csv"
LOG_FILE    = PROJECT_ROOT / "fetch_remaining.log"
CRAWL_CACHE = PROJECT_ROOT / ".crawl_cache_full.json"   # separate full cache
BASE_URL    = "https://www.hornbach.de"

# ── Tunable constants ──────────────────────────────────────────────────────────
N_LISTING_WORKERS  = 20
N_PRODUCT_WORKERS  = 30
RETRY_COUNT        = 4
TIMEOUT            = 30.0
CHECKPOINT_EVERY   = 150   # flush CSV after every N new rows
CACHE_MAX_AGE_SECS = 3_600  # 1 hour – always re-crawl if older

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
logger = logging.getLogger("fetch_remaining")


# ═══════════════════════════════════════════════════════════════════════════════
# HTTP helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _make_headers(extra: Optional[Dict] = None) -> Dict[str, str]:
    h = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control": "no-cache",
        "DNT": "1",
    }
    if extra:
        h.update(extra)
    return h


def _get_proxy() -> Optional[str]:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
    proxy = os.getenv("PROXY_URL") or ""
    if not proxy:
        pool = os.getenv("PROXY_POOL", "")
        if pool:
            proxy = pool.split(",")[0].strip()
    return proxy or None


def _build_client(proxy: Optional[str] = None) -> httpx.AsyncClient:
    kwargs = dict(
        headers=_make_headers(),
        timeout=TIMEOUT,
        follow_redirects=True,
    )
    if proxy:
        kwargs["proxy"] = proxy
    return httpx.AsyncClient(**kwargs)


async def _fetch_html(client: httpx.AsyncClient, url: str, sem: asyncio.Semaphore) -> Optional[str]:
    async with sem:
        for attempt in range(RETRY_COUNT):
            try:
                resp = await client.get(url)
                if resp.status_code == 200:
                    return resp.text
                if resp.status_code == 404:
                    logger.debug(f"404: {url}")
                    return None
                if resp.status_code == 406:
                    logger.error(
                        f"HTTP 406 – hornbach.de requires a German IP!\n"
                        f"  Set PROXY_URL=http://user:pass@german_proxy:port in .env\n"
                        f"  URL: {url}"
                    )
                    return None
                if resp.status_code in (429, 503):
                    wait = 5 * (attempt + 1)
                    logger.warning(f"Rate limit {resp.status_code} – sleeping {wait}s  {url}")
                    await asyncio.sleep(wait)
                    continue
                logger.warning(f"HTTP {resp.status_code}: {url}")
            except httpx.TimeoutException:
                logger.debug(f"Timeout attempt {attempt+1}: {url}")
                await asyncio.sleep(2 ** attempt)
            except Exception as e:
                logger.debug(f"Error attempt {attempt+1}: {url} → {e}")
                await asyncio.sleep(2 ** attempt)
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# Utilities
# ═══════════════════════════════════════════════════════════════════════════════

def slugify(name: str) -> str:
    umlaut_map = {
        "ä": "ae", "ö": "oe", "ü": "ue",
        "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss",
    }
    for char, replacement in umlaut_map.items():
        name = name.replace(char, replacement)
    name = name.lower()
    name = re.sub(r"[&,\s]+", "_", name)
    name = re.sub(r"[^\w_]", "", name)
    name = re.sub(r"_+", "_", name).strip("_")
    return name


def load_categories(csv_path: Path) -> List[Dict]:
    categories = []
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header_found = False
        for row in reader:
            if not row or len(row) < 2:
                continue
            if not header_found:
                if row[0].strip().lower() == "name":
                    header_found = True
                continue
            name = row[0].strip()
            url  = row[1].strip()
            if not name or not url.startswith("http"):
                continue
            total_products = int(row[2].strip()) if len(row) > 2 and row[2].strip().isdigit() else 0
            total_pages    = int(row[3].strip()) if len(row) > 3 and row[3].strip().isdigit() else 0
            path = url.replace(BASE_URL, "").rstrip("/") + "/"
            categories.append({
                "name": name,
                "url": url,
                "path": path,
                "total_products": total_products,
                "total_pages": total_pages,
            })
    return categories


def load_existing_urls(csv_path: Path) -> Set[str]:
    urls: Set[str] = set()
    if not csv_path.exists():
        return urls
    try:
        with open(csv_path, "r", encoding="utf-8", errors="ignore") as f:
            for row in csv.DictReader(f):
                url = (row.get("product_url") or "").strip()
                if url:
                    urls.add(url)
    except Exception as e:
        logger.warning(f"Could not read {csv_path.name}: {e}")
    return urls


def load_existing_article_numbers(csv_path: Path) -> Set[str]:
    arts: Set[str] = set()
    if not csv_path.exists():
        return arts
    try:
        with open(csv_path, "r", encoding="utf-8", errors="ignore") as f:
            for row in csv.DictReader(f):
                art = (row.get("article_number") or "").strip()
                if art:
                    arts.add(art)
    except Exception as e:
        logger.warning(f"Could not read {csv_path.name}: {e}")
    return arts


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 1 – Full listing-page crawl (ALL pages per category)
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_product_links(html: str, base_url: str = BASE_URL) -> List[Tuple[str, str, str]]:
    """Extract (url, name, article_number) from a listing page."""
    soup = BeautifulSoup(html, "lxml")
    results: List[Tuple[str, str, str]] = []
    seen: Set[str] = set()

    # 1. Broad selector for <a> tags
    for a in soup.select('a[data-testid="article-title"], a[href*="/p/"], a[href*="/conf/"]'):
        href = (a.get("href") or "").strip()
        if not href:
            continue
        full_url = (href if href.startswith("http") else f"{base_url}{href}").split("?")[0].rstrip("/")
        if full_url in seen:
            continue
        seen.add(full_url)
        name = a.get("title") or a.get_text(strip=True)
        m = re.search(r"/(\d{5,10})/?$", full_url)
        art = m.group(1) if m else ""
        results.append((full_url, name, art))

    # 2. Aggressive regex scan (Catches lazy-loaded items hidden in JSON/Script blocks)
    # Search for anything that looks like a product URL path (/p/name/123 or /conf/name/123)
    # This is critical for catching the 'missing 50%' on pages with 72 products.
    paths = re.findall(r'/(?:p|conf)/[^/"\'\s?#]+/(?:\d{5,10})/?', html)
    for p in paths:
        full_url = f"{base_url.rstrip('/')}{p}".rstrip("/")
        if full_url in seen:
            continue
        seen.add(full_url)
        
        # For these, we don't have a name yet, but the fetcher will get it from the PDP
        m = re.search(r"/(\d{5,10})/?$", full_url)
        results.append((full_url, "", m.group(1) if m else ""))

    return results


def _detect_total_pages(html: str) -> int:
    """Parse pagination to find max page number."""
    soup = BeautifulSoup(html, "lxml")
    max_page = 1
    for a in soup.select('[data-testid*="pagination"] a, a[href*="page="]'):
        m = re.search(r"[?&]page=(\d+)", a.get("href", ""))
        if m:
            max_page = max(max_page, int(m.group(1)))
    # Also check aria / text-based page numbers
    for el in soup.select('[aria-label*="Seite"], [aria-label*="page"]'):
        m = re.search(r"\d+", el.get("aria-label", ""))
        if m:
            max_page = max(max_page, int(m.group()))
    return max_page


async def crawl_category_full(
    client: httpx.AsyncClient,
    cat: Dict,
    sem: asyncio.Semaphore,
) -> List[Tuple[str, str, str]]:
    """
    Crawl ALL listing pages for a category.
    Uses total_pages from product-info.csv (ground truth), but also
    auto-discovers if product-info.csv value seems too low.
    """
    name = cat["name"]
    path = cat["path"]
    expected_pages = cat.get("total_pages", 0)
    base = BASE_URL.rstrip("/")
    first_url = f"{base}{path}"

    all_products: List[Tuple[str, str, str]] = []
    seen_urls: Set[str] = set()

    # Always fetch page 1 to detect actual page count
    logger.info(f"[{name}] Fetching page 1 / {expected_pages} expected pages…")
    first_html = await _fetch_html(client, first_url, sem)
    if not first_html:
        logger.warning(f"[{name}] Failed to fetch first listing page!")
        return []

    for item in _extract_product_links(first_html):
        if item[0] not in seen_urls:
            seen_urls.add(item[0])
            all_products.append(item)

    detected_pages = _detect_total_pages(first_html)
    # Trust the larger of the two (product-info.csv vs detected)
    total_pages = max(expected_pages, detected_pages)
    if total_pages < 1:
        total_pages = 1

    logger.info(f"[{name}] {total_pages} total pages (expected={expected_pages}, detected={detected_pages})")

    if total_pages <= 1:
        return all_products

    # Fetch remaining pages concurrently
    page_urls = [f"{base}{path}?page={p}" for p in range(2, total_pages + 1)]

    async def fetch_page(purl: str):
        html = await _fetch_html(client, purl, sem)
        return _extract_product_links(html) if html else []

    tasks = [fetch_page(u) for u in page_urls]
    batch_size = 50  # process in batches to avoid memory spikes
    for i in range(0, len(tasks), batch_size):
        batch_results = await asyncio.gather(*tasks[i:i+batch_size], return_exceptions=True)
        for res in batch_results:
            if isinstance(res, list):
                for item in res:
                    if item[0] not in seen_urls:
                        seen_urls.add(item[0])
                        all_products.append(item)

    logger.info(f"[{name}] ✓ Collected {len(all_products):,} unique product URLs")
    return all_products


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 3 – Extract product fields from detail page
# ═══════════════════════════════════════════════════════════════════════════════

def _clean(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\n", " ").replace("\r", " ").replace("\t", " ")
    return re.sub(r"\s{2,}", " ", text).strip()


def extract_product_data(html: str, url: str, name: str, article_number: str, category: str) -> Optional[Dict]:
    try:
        soup = BeautifulSoup(html, "lxml")
        result = {
            "article_number": article_number,
            "name": name,
            "price": "",
            "currency": "EUR",
            "product_url": url,
            "image_url": "",
            "availability": "",
            "category": category,
        }

        # Try to find category if missing or generic
        if not result["category"] or result["category"] == "Pending":
            # 1. Try Breadcrumbs
            bc = soup.select('nav[aria-label="Breadcrumb"] li, .breadcrumb li, [data-testid="breadcrumb"] li')
            if bc and len(bc) > 1:
                # The second to last item is usually the leaf category
                result["category"] = bc[-2].get_text(strip=True)
            
            # 2. Try Apollo state if breadcrumb failed
            if not result["category"] or result["category"] == "Pending":
                for script in soup.find_all("script"):
                    if "__ARTICLE_DETAIL_APOLLO_STATE__" in (script.string or ""):
                        m = re.search(r'"categoryName":"([^"]+)"', script.string)
                        if m:
                            result["category"] = m.group(1)
                            break

        # article_number
        art_el = soup.select_one('[data-testid="article-number"], span.article-number, [data-tn="article-number"]')
        if art_el:
            result["article_number"] = re.sub(r"^Art\.?\s*", "", art_el.get_text(strip=True)).strip()
        if not result["article_number"]:
            m = re.search(r"/(\d{5,10})/?(?:\?|$)", url)
            if m:
                result["article_number"] = m.group(1)

        # name
        name_el = soup.select_one('h1[data-testid="article-title"], [data-testid="article-headline"], h1.article-title, h1')
        if name_el:
            result["name"] = name_el.get_text(strip=True)

        # price
        for sel in ['[data-testid="prices"]', '[data-testid="price"]', '.price-price__price',
                    'span[aria-label*="Preis"]', '[class*="price"]']:
            el = soup.select_one(sel)
            if el:
                raw = el.get_text(separator=" ", strip=True).replace("\xa0", "")
                m = re.search(r"\d[\d.,]*", raw)
                if m:
                    val = m.group()
                    if "," in val and "." in val:
                        val = val.replace(".", "").replace(",", ".")
                    elif "," in val:
                        val = val.replace(",", ".")
                    try:
                        if float(val) > 0:
                            result["price"] = val
                            break
                    except ValueError:
                        pass

        # image_url
        for sel in ['[data-testid="main-image"] img', '[data-testid="article-image"] img',
                    '[data-testid*="image"] img', '.hb-product-image img', '.product-image img']:
            el = soup.select_one(sel)
            if el:
                result["image_url"] = el.get("src") or el.get("data-src") or el.get("data-lazy-src") or ""
                if result["image_url"]:
                    break

        # availability (Apollo state JSON first)
        for script in soup.find_all("script"):
            content = script.string or ""
            if "__ARTICLE_DETAIL_APOLLO_STATE__" not in content:
                continue
            try:
                parts = content.split("=", 1)
                if len(parts) < 2:
                    continue
                state, _ = json.JSONDecoder().raw_decode(parts[1].strip())
                for v in state.values():
                    if not isinstance(v, dict):
                        continue
                    for key in ("availabilityStatus", "stockStatus", "availability"):
                        if v.get(key):
                            result["availability"] = str(v[key])
                            break
                    if result["availability"]:
                        break
            except Exception:
                pass
            if result["availability"]:
                break

        if not result["availability"]:
            av = soup.select_one('[data-testid="availability"], .js-stock-availability, [class*="availability"], [class*="stock-status"]')
            if av:
                result["availability"] = av.get_text(strip=True)

        # specs
        specs: Dict[str, str] = {}
        attr_section = soup.find("section", id="attribute")
        if attr_section:
            for li in attr_section.find_all("li"):
                divs = li.find_all("div", recursive=False)
                if len(divs) >= 2:
                    k = _clean(divs[0].get_text())
                    v = _clean(divs[1].get_text())
                    if k and v:
                        specs[k] = v

        if not specs:
            for row_el in soup.select("[data-testid='spec-row'], [data-testid='attribute-row'], [data-testid='product-attribute'], [data-testid='attribute-item']"):
                lbl = row_el.select_one("[data-testid*='label'], [data-testid*='name'], .spec-label, .attribute-name")
                val = row_el.select_one("[data-testid*='value'], .spec-value, .attribute-value")
                if lbl and val:
                    k = _clean(lbl.get_text())
                    v = _clean(val.get_text())
                    if k and v:
                        specs[k] = v

        if not specs:
            for dl in soup.select("dl.product-specs, dl.article-specs, dl.specifications, .product-details dl"):
                for dt, dd in zip(dl.find_all("dt"), dl.find_all("dd")):
                    k = _clean(dt.get_text())
                    v = _clean(dd.get_text())
                    if k and v:
                        specs[k] = v

        result.update(specs)

        if result["name"] or result["article_number"] or specs:
            return result
        return None

    except Exception as e:
        logger.error(f"extract_product_data error ({url}): {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 4 – Thread-safe CSV appender
# ═══════════════════════════════════════════════════════════════════════════════

class CSVAppender:
    BASE_COLUMNS = ["article_number", "name", "price", "currency", "product_url", "image_url", "availability", "category"]

    def __init__(self, csv_path: Path):
        self.csv_path = csv_path
        self._lock = threading.Lock()
        self._existing_fieldnames: List[str] = []
        self._existing_rows: List[Dict] = []
        self._new_rows: List[Dict] = []
        self._spec_keys: List[str] = []
        self._seen_spec_keys: Set[str] = set()
        self._load()

    def _load(self):
        if not self.csv_path.exists():
            self._existing_fieldnames = list(self.BASE_COLUMNS)
            return
        try:
            with open(self.csv_path, "r", encoding="utf-8", errors="ignore") as f:
                reader = csv.DictReader(f)
                self._existing_fieldnames = list(reader.fieldnames or self.BASE_COLUMNS)
                self._existing_rows = list(reader)
            for fn in self._existing_fieldnames:
                if fn not in self.BASE_COLUMNS and fn not in self._seen_spec_keys:
                    self._spec_keys.append(fn)
                    self._seen_spec_keys.add(fn)
            logger.info(f"  ↳ Loaded {len(self._existing_rows):,} existing rows from {self.csv_path.name}")
        except Exception as e:
            logger.warning(f"Could not load {self.csv_path.name}: {e}")
            self._existing_fieldnames = list(self.BASE_COLUMNS)

    def add(self, row_dict: Dict):
        with self._lock:
            for key in row_dict:
                if key not in self.BASE_COLUMNS and key not in self._seen_spec_keys:
                    self._spec_keys.append(key)
                    self._seen_spec_keys.add(key)
            self._new_rows.append(row_dict)

    def pending_count(self) -> int:
        with self._lock:
            return len(self._new_rows)

    def flush(self) -> int:
        with self._lock:
            if not self._new_rows:
                return 0
            rows_to_write = self._new_rows[:]
            new_count = len(rows_to_write)

        all_columns = list(self._existing_fieldnames)
        for key in self._spec_keys:
            if key not in all_columns:
                all_columns.append(key)

        all_rows = self._existing_rows + rows_to_write
        tmp_path = self.csv_path.with_suffix(".tmp")
        try:
            with open(tmp_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=all_columns, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(all_rows)
            tmp_path.replace(self.csv_path)
            self._existing_rows = all_rows
            self._existing_fieldnames = all_columns
            with self._lock:
                self._new_rows = self._new_rows[new_count:]
            logger.info(f"  💾 {self.csv_path.name}: +{new_count} rows → {len(all_rows):,} total")
            return new_count
        except Exception as e:
            logger.error(f"Failed to flush {self.csv_path.name}: {e}")
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass
            return 0


# ═══════════════════════════════════════════════════════════════════════════════
# Crawl cache (full, separate from the original .crawl_cache.json)
# ═══════════════════════════════════════════════════════════════════════════════

def _load_full_cache(force_refresh: bool = False) -> Dict:
    if force_refresh or not CRAWL_CACHE.exists():
        return {}
    try:
        age = _time.time() - CRAWL_CACHE.stat().st_mtime
        if age > CACHE_MAX_AGE_SECS:
            logger.info(f"🕐 Full crawl cache is {age/3600:.1f}h old – will re-crawl.")
            return {}
        with open(CRAWL_CACHE, "r", encoding="utf-8") as f:
            data = json.load(f)
        total_urls = sum(len(v) for v in data.values())
        logger.info(f"⚡ Loaded full crawl cache ({len(data)} categories, {total_urls:,} URLs total)")
        return data
    except Exception as e:
        logger.warning(f"Could not read full crawl cache: {e}")
        return {}


def _save_full_cache(cache: Dict):
    try:
        with open(CRAWL_CACHE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
        total_urls = sum(len(v) for v in cache.values())
        logger.info(f"💾 Full crawl cache saved → {CRAWL_CACHE.name} ({total_urls:,} URLs)")
    except Exception as e:
        logger.warning(f"Could not save full crawl cache: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# Main orchestrator
# ═══════════════════════════════════════════════════════════════════════════════

class RemainingFetcher:

    def __init__(
        self,
        category_filter: Optional[List[str]] = None,
        dry_run: bool = False,
        n_product_workers: int = N_PRODUCT_WORKERS,
        n_listing_workers: int = N_LISTING_WORKERS,
        force_refresh: bool = False,
    ):
        self.category_filter = [c.strip() for c in category_filter] if category_filter else None
        self.dry_run = dry_run
        self.n_product_workers = n_product_workers
        self.n_listing_workers = n_listing_workers
        self.force_refresh = force_refresh
        self.proxy = _get_proxy()
        self.appenders: Dict[str, CSVAppender] = {}
        self._total_fetched = 0
        self._total_failed = 0
        self._lock = threading.Lock()

        if self.proxy:
            logger.info(f"🌐 Using proxy: {self.proxy[:40]}…")
        else:
            logger.warning(
                "⚠  No PROXY_URL set – hornbach.de requires a German IP!\n"
                "   Set PROXY_URL=http://user:pass@host:port in .env"
            )

    def _get_appender(self, cat_name: str) -> CSVAppender:
        if cat_name not in self.appenders:
            csv_path = OUTPUT_DIR / f"{slugify(cat_name)}.csv"
            self.appenders[cat_name] = CSVAppender(csv_path)
        return self.appenders[cat_name]

    async def _process_product(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        url: str,
        name: str,
        art: str,
        cat: str,
        counter: List[int],
        total: int,
    ):
        html = await _fetch_html(client, url, sem)
        if not html:
            with self._lock:
                self._total_failed += 1
            return

        data = extract_product_data(html, url, name, art, cat)
        if not data:
            with self._lock:
                self._total_failed += 1
            logger.debug(f"No data: {url}")
            return

        if not self.dry_run:
            appender = self._get_appender(cat)
            appender.add(data)
            if appender.pending_count() >= CHECKPOINT_EVERY:
                appender.flush()

        with self._lock:
            self._total_fetched += 1
            counter[0] += 1
            done = counter[0]
            if done % 500 == 0 or done == total:
                pct = done / total * 100
                logger.info(
                    f"  Progress: {done:,}/{total:,} ({pct:.1f}%)  "
                    f"ok={self._total_fetched:,}  fail={self._total_failed:,}"
                )

    async def run(self):
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

        # Load categories
        categories = load_categories(CSV_INFO)
        if not categories:
            logger.error("No categories found in product-info.csv")
            sys.exit(1)

        if self.category_filter:
            categories = [
                c for c in categories
                if any(f.lower() in c["name"].lower() for f in self.category_filter)
            ]
            if not categories:
                logger.error(f"No categories matched filter: {self.category_filter}")
                sys.exit(1)

        logger.info(f"📋 {len(categories)} categories to process")

        # ── Phase 1: Collect all product URLs ───────────────────────────────
        cache = _load_full_cache(force_refresh=self.force_refresh)
        cats_needing_crawl = [c for c in categories if c["name"] not in cache]
        cats_from_cache    = [c for c in categories if c["name"] in cache]

        cat_url_map: Dict[str, List] = {}

        for cat in cats_from_cache:
            cat_url_map[cat["name"]] = [tuple(x) for x in cache[cat["name"]]]
            logger.info(f"[{cat['name']}] ⚡ {len(cat_url_map[cat['name']]):,} URLs from cache")

        if cats_needing_crawl:
            logger.info(
                f"\n🌐 Crawling listing pages for {len(cats_needing_crawl)} "
                f"categor{'y' if len(cats_needing_crawl)==1 else 'ies'} "
                f"(+{len(cats_from_cache)} from cache)…\n"
            )
            listing_sem = asyncio.Semaphore(self.n_listing_workers)
            async with _build_client(self.proxy) as listing_client:
                tasks = [crawl_category_full(listing_client, cat, listing_sem) for cat in cats_needing_crawl]
                results = await asyncio.gather(*tasks, return_exceptions=True)

            for cat, result in zip(cats_needing_crawl, results):
                if isinstance(result, Exception):
                    logger.error(f"[{cat['name']}] crawl failed: {result}")
                    cat_url_map[cat["name"]] = []
                else:
                    cat_url_map[cat["name"]] = result
                    cache[cat["name"]] = [list(x) for x in result]

            _save_full_cache(cache)
        else:
            logger.info("⚡ All categories served from cache")

        # ── Phase 2: Build missing queue ─────────────────────────────────────
        logger.info("\n🔍 Comparing collected URLs against existing CSV rows…")
        all_missing: List[Tuple[str, str, str, str]] = []  # (cat, url, name, art)
        
        global_existing_urls: Set[str] = set()
        global_existing_arts: Set[str] = set()

        for cat in categories:
            cat_name = cat["name"]
            csv_path = OUTPUT_DIR / f"{slugify(cat_name)}.csv"
            existing_urls = load_existing_urls(csv_path)
            existing_arts = load_existing_article_numbers(csv_path)
            
            # Populate global sets for deduplicating external URL lists
            global_existing_urls.update(existing_urls)
            global_existing_arts.update(existing_arts)

            all_discovered = cat_url_map.get(cat_name, [])

            missing_for_cat = []
            for (url, name, art) in all_discovered:
                if url in existing_urls:
                    continue
                if art and art in existing_arts:
                    continue
                missing_for_cat.append((cat_name, url, name, art))

            pct_done = len(existing_urls) / max(cat["total_products"], 1) * 100
            logger.info(
                f"  [{cat_name}]  discovered={len(all_discovered):,}  "
                f"existing={len(existing_urls):,}  missing={len(missing_for_cat):,}  "
                f"({pct_done:.1f}% done)"
            )
            all_missing.extend(missing_for_cat)

        # ── Step 2.5: Inject missing_urls.txt ────────────────────────────────
        MISSING_FILE = PROJECT_ROOT / "missing_urls.txt"
        if MISSING_FILE.exists():
            logger.info(f"\n📂 Loading additional targets from {MISSING_FILE.name}…")
            external_urls = set()
            try:
                with open(MISSING_FILE, "r") as f:
                    for line in f:
                        u = line.strip().split("?")[0].rstrip("/")
                        if u.startswith("http") and u not in global_existing_urls:
                            # Also check article number if possible
                            m = re.search(r"/(\d{5,10})/?$", u)
                            if m and m.group(1) in global_existing_arts:
                                continue
                            external_urls.add(u)
            except Exception as e:
                logger.warning(f"Failed to read {MISSING_FILE.name}: {e}")

            if external_urls:
                added_ext = 0
                for u in external_urls:
                    # Deduplicate against already queued
                    if any(u == m[1] for m in all_missing):
                        continue
                    
                    # Extract art number for queue if possible
                    m = re.search(r"/(\d{5,10})/?$", u)
                    art = m.group(1) if m else ""
                        
                    all_missing.append(("Pending", u, "", art))
                    added_ext += 1
                logger.info(f"  ↳ Added {added_ext:,} unique URLs from {MISSING_FILE.name}")

        if not all_missing:
            logger.info("\n✅ Nothing missing – all products already fetched!")
            return

        logger.info(f"\n{'='*65}")
        logger.info(f"📦 Total products to fetch: {len(all_missing):,}")
        logger.info(f"   Product workers : {self.n_product_workers}")
        logger.info(f"   Dry-run         : {self.dry_run}")
        logger.info(f"{'='*65}\n")

        if self.dry_run:
            by_cat: Dict[str, int] = defaultdict(int)
            for (cat, url, name, art) in all_missing:
                by_cat[cat] += 1
            logger.info("DRY RUN – per-category breakdown:")
            for cat, cnt in sorted(by_cat.items()):
                logger.info(f"  {cat:<50} {cnt:>7,} missing")
            return

        # ── Phase 3: Fetch all missing products ───────────────────────────────
        product_sem = asyncio.Semaphore(self.n_product_workers)
        counter = [0]
        total = len(all_missing)

        async with _build_client(self.proxy) as product_client:
            tasks = [
                self._process_product(
                    product_client, product_sem,
                    url, name, art, cat,
                    counter, total,
                )
                for (cat, url, name, art) in all_missing
            ]
            await asyncio.gather(*tasks)

        # ── Phase 4: Final flush ──────────────────────────────────────────────
        logger.info("\n💾 Flushing all CSV files…")
        total_written = 0
        for cat_name, appender in self.appenders.items():
            total_written += appender.flush()

        logger.info(f"\n{'='*65}")
        logger.info(f"✅ Done!")
        logger.info(f"   Successfully fetched : {self._total_fetched:,}")
        logger.info(f"   Failed               : {self._total_failed:,}")
        logger.info(f"   New rows written     : {total_written:,}")
        logger.info(f"{'='*65}")


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

def parse_args():
    p = argparse.ArgumentParser(
        description="🚀 Fetch remaining ~30% of Hornbach product specs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Fetch ALL remaining products (recommended first run)
  python scratch/fetch_remaining.py --refresh

  # Use more workers for speed
  python scratch/fetch_remaining.py --refresh --workers 40 --listing-workers 30

  # Only specific categories
  python scratch/fetch_remaining.py --categories "Garten,Innendeko" --refresh

  # Dry-run to see what would be fetched
  python scratch/fetch_remaining.py --dry-run --refresh

  # Subsequent runs (uses cache, only fetches newly discovered missing)
  python scratch/fetch_remaining.py
        """,
    )
    p.add_argument("--categories", "-c", default="", help="Comma-separated partial category name filter")
    p.add_argument("--workers", "-w", type=int, default=N_PRODUCT_WORKERS,
                   help=f"Product fetch workers (default: {N_PRODUCT_WORKERS})")
    p.add_argument("--listing-workers", type=int, default=N_LISTING_WORKERS,
                   help=f"Listing-page crawl workers (default: {N_LISTING_WORKERS})")
    p.add_argument("--dry-run", action="store_true", help="Show what would be fetched, no CSV writes")
    p.add_argument("--refresh", action="store_true",
                   help="Force full re-crawl of ALL listing pages (ignore cache). Use on first run!")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    cat_filter = [c.strip() for c in args.categories.split(",") if c.strip()] if args.categories else None

    fetcher = RemainingFetcher(
        category_filter=cat_filter,
        dry_run=args.dry_run,
        n_product_workers=args.workers,
        n_listing_workers=args.listing_workers,
        force_refresh=args.refresh,
    )
    asyncio.run(fetcher.run())
