#!/usr/bin/env python3
"""
fast_parallel_fetcher.py – High-Performance Missing Product Fetcher
====================================================================

Fetches all missing product data not yet in output_local/specs/ CSVs.

Pipeline:
  Phase 1  – Crawl category listing pages with async HTTP to collect ALL
              product URLs (fast: just HTML parsing, no browser).
  Phase 2  – Compare against URLs already in CSVs → build "missing" list.
  Phase 3  – Fetch all missing product detail pages concurrently with
              N_WORKERS async workers and extract all fields + specs.
  Phase 4  – Append new rows to category CSVs atomically (no duplicates).

Run (single instance, all categories):
    git clone https://github.com/usmanmanii/scraper.git && cd scraper
    source venv/bin/activate
    python scratch/fast_parallel_fetcher.py

Run only specific categories (comma-separated partial match):
    python scratch/fast_parallel_fetcher.py --categories "Garten,Bad"

Run N parallel worker processes (recommended for max speed):
    python scratch/fast_parallel_fetcher.py --workers 30

Show progress / dry-run (no CSV writes):
    python scratch/fast_parallel_fetcher.py --dry-run

IMPORTANT: A German IP / proxy is required. Configure in .env:
    PROXY_URL=http://user:pass@german_proxy:port
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
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse

import httpx
from bs4 import BeautifulSoup

# ── Project path ──────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

OUTPUT_DIR   = PROJECT_ROOT / "output_local" / "specs"
CSV_INFO     = PROJECT_ROOT / "product-info.csv"
LOG_FILE     = PROJECT_ROOT / "fast_fetcher.log"
CRAWL_CACHE  = PROJECT_ROOT / ".crawl_cache.json"   # persisted listing URL cache
BASE_URL     = "https://www.hornbach.de"

# ── Tunable constants ──────────────────────────────────────────────────────────
N_LISTING_WORKERS  = 20    # concurrent workers for listing-page crawl
N_PRODUCT_WORKERS  = 30    # concurrent workers for product detail pages
RETRY_COUNT        = 3
TIMEOUT            = 25.0
CHECKPOINT_EVERY   = 200   # flush CSV after every N new rows
CACHE_MAX_AGE_SECS = 86_400  # 24 h – re-crawl listing pages after this

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
logger = logging.getLogger("fast_fetcher")

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
    """Read proxy from .env / environment."""
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
    proxy = os.getenv("PROXY_URL") or ""
    if not proxy:
        # also check PROXY_POOL
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
    """Fetch URL, return HTML string or None on failure."""
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
                        f"HTTP 406 (Not Acceptable) – hornbach.de requires a German IP!\n"
                        f"  Set PROXY_URL=http://user:pass@german_proxy:port in your .env file.\n"
                        f"  URL: {url}"
                    )
                    return None  # No point retrying without a proxy change
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
                err = str(e) or type(e).__name__
                logger.debug(f"Error attempt {attempt+1}: {url} → {err}")
                await asyncio.sleep(2 ** attempt)
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# Crawl-cache helpers  (skip re-crawling listing pages on repeated runs)
# ═══════════════════════════════════════════════════════════════════════════════

import time as _time

def _load_crawl_cache() -> Dict:
    """
    Load .crawl_cache.json.
    Returns dict:  {category_name: [[url, name, article_number], ...], ...}
    Returns empty dict on any error or if cache is absent.
    """
    if not CRAWL_CACHE.exists():
        return {}
    try:
        age = _time.time() - CRAWL_CACHE.stat().st_mtime
        if age > CACHE_MAX_AGE_SECS:
            logger.info(
                f"🕐 Crawl cache is {age/3600:.1f}h old (limit {CACHE_MAX_AGE_SECS/3600:.0f}h) "
                "– will re-crawl listing pages."
            )
            return {}
        with open(CRAWL_CACHE, "r", encoding="utf-8") as f:
            data = json.load(f)
        logger.info(
            f"⚡ Loaded crawl cache ({len(data)} categories) – skipping listing crawl."
        )
        return data
    except Exception as e:
        logger.warning(f"Could not read crawl cache: {e}")
        return {}


def _save_crawl_cache(cache: Dict):
    """Persist crawl cache to disk (best-effort)."""
    try:
        with open(CRAWL_CACHE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
        logger.info(f"💾 Crawl cache saved → {CRAWL_CACHE.name}")
    except Exception as e:
        logger.warning(f"Could not save crawl cache: {e}")


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
    """Parse product-info.csv → list of {name, url, path, total_products, total_pages}."""
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
    """Return the set of product_url values already present in a CSV."""
    urls = set()
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
    """Return the set of article_number values already present in a CSV."""
    arts = set()
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
# Phase 1 – Collect all product URLs from listing pages
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_product_links_from_html(html: str, base_url: str = BASE_URL) -> List[Tuple[str, str, str]]:
    """
    Extract (full_url, name, article_number) tuples from a listing page HTML.
    Returns deduplicated list.
    """
    soup = BeautifulSoup(html, "lxml")
    results: List[Tuple[str, str, str]] = []
    seen: Set[str] = set()

    # Primary: confirmed Hornbach selector
    for a in soup.select('a[data-testid="article-title"]'):
        href = (a.get("href") or "").strip()
        if not href:
            continue
        full_url = urljoin(base_url, href) if not href.startswith("http") else href
        # Normalise: strip trailing slash & query params for dedup
        full_url = full_url.split("?")[0].rstrip("/")
        if full_url in seen:
            continue
        seen.add(full_url)
        name = (a.get("title") or a.get_text(strip=True))
        art_m = re.search(r"/(\d{5,10})/?$", href)
        article_number = art_m.group(1) if art_m else ""
        results.append((full_url, name, article_number))

    # Fallback: any /p/ links
    if not results:
        for a in soup.select('a[href*="/p/"]'):
            href = (a.get("href") or "").strip()
            if not href:
                continue
            full_url = urljoin(base_url, href) if not href.startswith("http") else href
            full_url = full_url.split("?")[0].rstrip("/")
            if full_url in seen:
                continue
            seen.add(full_url)
            art_m = re.search(r"/(\d{5,10})/?$", href)
            article_number = art_m.group(1) if art_m else ""
            name = (a.get("title") or a.get_text(strip=True))
            results.append((full_url, name, article_number))

    return results


def _extract_total_pages_from_html(html: str) -> int:
    """Try to determine the total number of pages from listing HTML."""
    soup = BeautifulSoup(html, "lxml")
    # Look for max page number in pagination links
    max_page = 1
    for a in soup.select('[data-testid*="pagination"] a, a[href*="page="]'):
        href = a.get("href") or ""
        m = re.search(r"[?&]page=(\d+)", href)
        if m:
            p = int(m.group(1))
            if p > max_page:
                max_page = p
    return max_page


async def collect_category_urls(
    client: httpx.AsyncClient,
    cat: Dict,
    sem: asyncio.Semaphore,
) -> List[Tuple[str, str, str]]:
    """
    Crawl all listing pages for a category.
    Returns list of (url, name, article_number) tuples.
    """
    name = cat["name"]
    path = cat["path"]
    total_pages = cat.get("total_pages", 0)
    base = BASE_URL.rstrip("/")

    all_products: List[Tuple[str, str, str]] = []
    seen_urls: Set[str] = set()

    logger.info(f"[{name}] Collecting URLs ({total_pages} pages expected)…")

    # Build all page URLs upfront
    if total_pages > 0:
        page_urls = [f"{base}{path}"] + [f"{base}{path}?page={p}" for p in range(2, total_pages + 1)]
    else:
        # Unknown page count – fetch first page to discover
        first_html = await _fetch_html(client, f"{base}{path}", sem)
        if not first_html:
            logger.warning(f"[{name}] Failed to fetch first listing page")
            return []
        products = _extract_product_links_from_html(first_html)
        all_products.extend(products)
        discovered = _extract_total_pages_from_html(first_html)
        if discovered > 1:
            page_urls = [f"{base}{path}?page={p}" for p in range(2, discovered + 1)]
        else:
            return products
        page_urls = page_urls  # remaining pages

    # Fetch all pages in parallel batches
    async def fetch_page(page_url: str):
        html = await _fetch_html(client, page_url, sem)
        if html:
            return _extract_product_links_from_html(html)
        return []

    tasks = [fetch_page(u) for u in page_urls]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for result in results:
        if isinstance(result, Exception):
            logger.warning(f"[{name}] Page fetch error: {result}")
            continue
        if isinstance(result, list):
            for item in result:
                if item[0] not in seen_urls:
                    seen_urls.add(item[0])
                    all_products.append(item)

    logger.info(f"[{name}] Collected {len(all_products)} product URLs")
    return all_products


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 3 – Extract product fields from detail page HTML
# ═══════════════════════════════════════════════════════════════════════════════

def extract_product_data(html: str, url: str, name: str, article_number: str, category: str) -> Optional[Dict]:
    """
    Parse a product detail page (PDP) and return a flat dict with all fields.
    Returns None if no meaningful data found.
    """
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

        # ── article_number ─────────────────────────────────────────────────
        art_el = soup.select_one(
            '[data-testid="article-number"], span.article-number, [data-tn="article-number"]'
        )
        if art_el:
            raw = art_el.get_text(strip=True)
            result["article_number"] = re.sub(r"^Art\.?\s*", "", raw).strip()
        if not result["article_number"]:
            m = re.search(r"/(\d{5,10})/?(?:\?|$)", url)
            if m:
                result["article_number"] = m.group(1)

        # ── name ───────────────────────────────────────────────────────────
        name_el = soup.select_one(
            'h1[data-testid="article-title"], '
            '[data-testid="article-headline"], '
            'h1.article-title, h1'
        )
        if name_el:
            result["name"] = name_el.get_text(strip=True)

        # ── price ──────────────────────────────────────────────────────────
        for price_sel in [
            '[data-testid="prices"]',
            '[data-testid="price"]',
            '.price-price__price',
            'span[aria-label*="Preis"]',
            '[class*="price"]',
        ]:
            price_el = soup.select_one(price_sel)
            if price_el:
                raw = price_el.get_text(separator=" ", strip=True).replace("\xa0", "")
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

        # ── image_url ──────────────────────────────────────────────────────
        for img_sel in [
            '[data-testid="main-image"] img',
            '[data-testid="article-image"] img',
            '[data-testid*="image"] img',
            '.hb-product-image img',
            '.product-image img',
            'img[data-testid*="image"]',
        ]:
            img_el = soup.select_one(img_sel)
            if img_el:
                result["image_url"] = (
                    img_el.get("src") or img_el.get("data-src") or
                    img_el.get("data-lazy-src") or ""
                )
                if result["image_url"]:
                    break

        # ── availability (try Apollo state JSON first) ─────────────────────
        for script in soup.find_all("script"):
            content = script.string or ""
            if "__ARTICLE_DETAIL_APOLLO_STATE__" not in content:
                continue
            try:
                parts = content.split("=", 1)
                if len(parts) < 2:
                    continue
                decoder = json.JSONDecoder()
                state, _ = decoder.raw_decode(parts[1].strip())
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

        # DOM fallback for availability
        if not result["availability"]:
            avail_el = soup.select_one(
                '[data-testid="availability"], '
                '.js-stock-availability, '
                '[class*="availability"], '
                '[class*="stock-status"]'
            )
            if avail_el:
                result["availability"] = avail_el.get_text(strip=True)

        # ── specs (section#attribute ul li) ───────────────────────────────
        specs: Dict[str, str] = {}
        attr_section = soup.find("section", id="attribute")
        if attr_section:
            for li in attr_section.find_all("li"):
                divs = li.find_all("div", recursive=False)
                if len(divs) >= 2:
                    key = _clean(divs[0].get_text())
                    val = _clean(divs[1].get_text())
                    if key and val:
                        specs[key] = val

        # Fallback: data-testid spec rows
        if not specs:
            for row_el in soup.select(
                "[data-testid='spec-row'], [data-testid='attribute-row'], "
                "[data-testid='product-attribute'], [data-testid='attribute-item']"
            ):
                label_el = row_el.select_one(
                    "[data-testid*='label'], [data-testid*='name'], .spec-label, .attribute-name"
                )
                value_el = row_el.select_one(
                    "[data-testid*='value'], .spec-value, .attribute-value"
                )
                if label_el and value_el:
                    key = _clean(label_el.get_text())
                    val = _clean(value_el.get_text())
                    if key and val:
                        specs[key] = val

        # Fallback: dl definition list
        if not specs:
            for dl in soup.select("dl.product-specs, dl.article-specs, dl.specifications, .product-details dl"):
                for dt, dd in zip(dl.find_all("dt"), dl.find_all("dd")):
                    key = _clean(dt.get_text())
                    val = _clean(dd.get_text())
                    if key and val:
                        specs[key] = val

        result.update(specs)

        # Return only if we have something useful
        if result["name"] or result["article_number"] or specs:
            return result
        return None

    except Exception as e:
        logger.error(f"extract_product_data error ({url}): {e}")
        return None


def _clean(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\n", " ").replace("\r", " ").replace("\t", " ")
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 4 – Thread-safe CSV appender
# ═══════════════════════════════════════════════════════════════════════════════

class CSVAppender:
    """
    Thread-safe, incremental CSV appender.
    Loads existing headers + rows on init, buffers new rows in memory,
    flushes to disk atomically via temp file → rename.
    """

    BASE_COLUMNS = [
        "article_number", "name", "price", "currency",
        "product_url", "image_url", "availability", "category",
    ]

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
            # Track existing spec keys
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
            # Track any new spec keys
            for key in row_dict:
                if key not in self.BASE_COLUMNS and key not in self._seen_spec_keys:
                    self._spec_keys.append(key)
                    self._seen_spec_keys.add(key)
            self._new_rows.append(row_dict)

    def pending_count(self) -> int:
        with self._lock:
            return len(self._new_rows)

    def flush(self) -> int:
        """Write all rows (existing + new) to disk atomically. Returns count of new rows written."""
        with self._lock:
            if not self._new_rows:
                return 0
            rows_to_write = self._new_rows[:]
            new_count = len(rows_to_write)

        # Build merged fieldnames: existing + any new spec keys not already there
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
            # Update in-memory state
            self._existing_rows = all_rows
            self._existing_fieldnames = all_columns
            with self._lock:
                self._new_rows = self._new_rows[new_count:]  # remove flushed rows
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
# Main Orchestrator
# ═══════════════════════════════════════════════════════════════════════════════

class FastParallelFetcher:

    def __init__(self, category_filter: Optional[List[str]] = None, dry_run: bool = False,
                 n_product_workers: int = N_PRODUCT_WORKERS,
                 n_listing_workers: int = N_LISTING_WORKERS):
        self.category_filter = [c.strip() for c in category_filter] if category_filter else None
        self.dry_run = dry_run
        self.n_product_workers = n_product_workers
        self.n_listing_workers = n_listing_workers
        self.proxy = _get_proxy()
        self.appenders: Dict[str, CSVAppender] = {}
        self._total_fetched = 0
        self._total_failed = 0
        self._lock = threading.Lock()

        if self.proxy:
            logger.info(f"🌐 Using proxy: {self.proxy[:40]}…")
        else:
            logger.warning(
                "⚠  No PROXY_URL set in .env – hornbach.de requires a German IP!\n"
                "   Set PROXY_URL=http://user:pass@host:port in your .env file."
            )

    def _get_appender(self, category_name: str) -> CSVAppender:
        if category_name not in self.appenders:
            slug = slugify(category_name)
            csv_path = OUTPUT_DIR / f"{slug}.csv"
            self.appenders[category_name] = CSVAppender(csv_path)
        return self.appenders[category_name]

    async def _fetch_and_process_product(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        url: str,
        name: str,
        article_number: str,
        category: str,
        counter: List[int],
        total: int,
    ):
        html = await _fetch_html(client, url, sem)
        if not html:
            with self._lock:
                self._total_failed += 1
            return

        data = extract_product_data(html, url, name, article_number, category)
        if not data:
            with self._lock:
                self._total_failed += 1
            logger.debug(f"No data extracted: {url}")
            return

        if not self.dry_run:
            appender = self._get_appender(category)
            appender.add(data)

            # Checkpoint flush
            if appender.pending_count() >= CHECKPOINT_EVERY:
                appender.flush()

        with self._lock:
            self._total_fetched += 1
            counter[0] += 1
            done = counter[0]
            if done % 100 == 0 or done == total:
                logger.info(
                    f"  [{category[:25]}] {done}/{total}  "
                    f"(total ok={self._total_fetched} fail={self._total_failed})"
                )

    async def run(self, refresh_cache: bool = False):
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

        # Load categories
        categories = load_categories(CSV_INFO)
        if not categories:
            logger.error("No categories found in product-info.csv")
            sys.exit(1)

        # Apply category filter
        if self.category_filter:
            categories = [
                c for c in categories
                if any(f.lower() in c["name"].lower() for f in self.category_filter)
            ]
            if not categories:
                logger.error(f"No categories matched filter: {self.category_filter}")
                sys.exit(1)

        logger.info(f"📋 Processing {len(categories)} categories")

        # ── Phase 1+2: Collect all listing URLs, filter missing ──────────────
        #
        # Optimisation: if a fresh crawl cache exists (and --refresh-cache was
        # not passed), skip re-crawling listing pages entirely and use the
        # cached URL lists.  This saves hundreds of HTTP requests on every
        # re-run after the first one.
        #
        crawl_cache: Dict = {} if refresh_cache else _load_crawl_cache()

        cats_needing_crawl = [
            cat for cat in categories
            if cat["name"] not in crawl_cache
        ]
        cats_from_cache = [
            cat for cat in categories
            if cat["name"] in crawl_cache
        ]

        cat_results_map: Dict[str, List] = {}

        # Use cache for categories that are already there
        for cat in cats_from_cache:
            # Stored as list-of-lists; convert back to list-of-tuples
            cat_results_map[cat["name"]] = [
                tuple(item) for item in crawl_cache[cat["name"]]
            ]

        # Crawl only the categories not in cache
        if cats_needing_crawl:
            logger.info(
                f"🌐 Crawling listing pages for {len(cats_needing_crawl)} "
                f"categor{'y' if len(cats_needing_crawl)==1 else 'ies'} "
                f"(+{len(cats_from_cache)} from cache)…"
            )
            listing_sem = asyncio.Semaphore(self.n_listing_workers)
            async with _build_client(self.proxy) as listing_client:
                tasks = [
                    collect_category_urls(listing_client, cat, listing_sem)
                    for cat in cats_needing_crawl
                ]
                results = await asyncio.gather(*tasks, return_exceptions=True)

            for cat, result in zip(cats_needing_crawl, results):
                if isinstance(result, Exception):
                    logger.error(f"[{cat['name']}] URL collection failed: {result}")
                    cat_results_map[cat["name"]] = []
                else:
                    cat_results_map[cat["name"]] = result
                    # Update cache entry so it gets persisted below
                    crawl_cache[cat["name"]] = [list(item) for item in result]

            # Persist updated cache
            _save_crawl_cache(crawl_cache)
        else:
            logger.info("⚡ All categories served from crawl cache – no listing crawl needed.")

        # Rebuild cat_results in original category order for the rest of the logic
        cat_results = [cat_results_map.get(cat["name"], []) for cat in categories]

        # Build: category → list of missing (url, name, article_number)
        all_missing: List[Tuple[str, str, str, str]] = []  # (category, url, name, art)
        for cat, result in zip(categories, cat_results):
            if isinstance(result, Exception):
                # Should not normally happen since we handle above, but be safe
                logger.error(f"[{cat['name']}] URL collection failed: {result}")
                continue
            cat_name = cat["name"]
            csv_path = OUTPUT_DIR / f"{slugify(cat_name)}.csv"
            existing_urls = load_existing_urls(csv_path)
            existing_arts = load_existing_article_numbers(csv_path)

            missing_for_cat = []
            for (url, name, art) in result:
                if url in existing_urls:
                    continue
                if art and art in existing_arts:
                    continue
                missing_for_cat.append((cat_name, url, name, art))

            fetched = len(existing_urls)
            logger.info(
                f"[{cat_name}] {fetched:,} already fetched, "
                f"{len(missing_for_cat):,} missing"
            )
            all_missing.extend(missing_for_cat)

        if not all_missing:
            logger.info("✅ All products already fetched! Nothing to do.")
            return

        logger.info(f"\n{'='*60}")
        logger.info(f"📦 Total missing products to fetch: {len(all_missing):,}")
        logger.info(f"   Workers: {self.n_product_workers}  |  Dry-run: {self.dry_run}")
        logger.info(f"{'='*60}\n")

        if self.dry_run:
            logger.info("DRY RUN – no CSV writes will happen.")
            # Print per-category summary
            by_cat: Dict[str, int] = defaultdict(int)
            for (cat, url, name, art) in all_missing:
                by_cat[cat] += 1
            for cat, cnt in sorted(by_cat.items()):
                logger.info(f"  {cat:<45} {cnt:>6} missing")
            return

        # ── Phase 3: Fetch all missing product pages concurrently ────────────
        product_sem = asyncio.Semaphore(self.n_product_workers)
        counter = [0]
        total = len(all_missing)

        async with _build_client(self.proxy) as product_client:
            tasks = [
                self._fetch_and_process_product(
                    product_client, product_sem,
                    url, name, art, cat,
                    counter, total,
                )
                for (cat, url, name, art) in all_missing
            ]
            await asyncio.gather(*tasks)

        # ── Phase 4: Final flush all CSVs ────────────────────────────────────
        logger.info("\n💾 Flushing all CSV files…")
        total_written = 0
        for cat_name, appender in self.appenders.items():
            written = appender.flush()
            total_written += written

        logger.info(f"\n{'='*60}")
        logger.info(f"✅ Done!")
        logger.info(f"   Successfully fetched : {self._total_fetched:,}")
        logger.info(f"   Failed               : {self._total_failed:,}")
        logger.info(f"   New rows written     : {total_written:,}")
        logger.info(f"{'='*60}")


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

def parse_args():
    p = argparse.ArgumentParser(
        description="🚀 Fast parallel fetcher for missing Hornbach product specs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Fetch ALL missing products (all categories)
  python scratch/fast_parallel_fetcher.py

  # Only specific categories
  python scratch/fast_parallel_fetcher.py --categories "Garten,Bad"

  # Show what would be fetched without writing CSVs
  python scratch/fast_parallel_fetcher.py --dry-run

  # Use more workers (faster but more likely to get rate-limited)
  python scratch/fast_parallel_fetcher.py --workers 50
        """,
    )
    p.add_argument(
        "--categories", "-c", default="",
        help="Comma-separated partial category name filter (default: all)"
    )
    p.add_argument(
        "--workers", "-w", type=int, default=N_PRODUCT_WORKERS,
        help=f"Number of parallel product fetch workers (default: {N_PRODUCT_WORKERS})"
    )
    p.add_argument(
        "--listing-workers", type=int, default=N_LISTING_WORKERS,
        help=f"Number of parallel listing-page workers (default: {N_LISTING_WORKERS})"
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be fetched without writing to CSVs"
    )
    p.add_argument(
        "--refresh-cache", action="store_true",
        help=(
            "Force re-crawl of all listing pages even if a fresh crawl cache exists. "
            f"Cache is automatically invalidated after {CACHE_MAX_AGE_SECS//3600}h."
        )
    )
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    cat_filter = [c.strip() for c in args.categories.split(",") if c.strip()] if args.categories else None

    fetcher = FastParallelFetcher(
        category_filter=cat_filter,
        dry_run=args.dry_run,
        n_product_workers=args.workers,
        n_listing_workers=args.listing_workers,
    )
    asyncio.run(fetcher.run(refresh_cache=args.refresh_cache))
