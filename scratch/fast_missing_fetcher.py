#!/usr/bin/env python3
"""
fast_missing_fetcher.py – Fetch ONLY truly missing product pages from hornbach.de.

Strategy:
  1. Load ALL existing article_numbers from every CSV in output_local/specs/
     (IMPORTANT: Only count records as 'seen' if they have a Name and Price).
  2. Read missing_urls.txt, filter out already-fetched URLs (by article number).
  3. Fetch remaining URLs concurrently (20 workers) with retry + backoff.
  4. Extract specs + category from Apollo JSON state embedded in every PDP.
  5. Append to correct per-category CSV (no overwrites, no duplicates).

Run:
    git clone https://github.com/usmanmanii/scraper.git && cd scraper
    source venv/bin/activate
    python scratch/fast_missing_fetcher.py
"""

import asyncio
import csv
import json
import logging
import os
import re
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import httpx
from bs4 import BeautifulSoup

# ── Project imports ───────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent))
from scraper import _extract_spec_tables, _clean_text
from models import Product
from config import cfg

# ─────────────────────────────────────────────────────────────────────────────
# Paths & tunables
# ─────────────────────────────────────────────────────────────────────────────
PROJECT_ROOT      = Path(__file__).parent.parent
OUTPUT_DIR        = PROJECT_ROOT / "output_local" / "specs"
MISSING_URLS_FILE = PROJECT_ROOT / "missing_urls.txt"
LOG_FILE          = PROJECT_ROOT / "fast_fetcher.log"

MAX_CONCURRENT    = 20          # parallel HTTP requests
RETRY_COUNT       = 3
TIMEOUT           = 30.0
BATCH_FLUSH       = 50          # write to disk every N products

# ── Category name → slug map from product-info.csv ───────────────────────────
# (We pre-build this to ensure slugs match existing filenames.)
PRODUCT_INFO_CSV  = PROJECT_ROOT / "product-info.csv"

# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
logger = logging.getLogger("fast_fetcher")

# ─────────────────────────────────────────────────────────────────────────────
# Slug helper  (must match existing filenames)
# ─────────────────────────────────────────────────────────────────────────────
def slugify(name: str) -> str:
    umlaut_map = {
        "ä": "ae", "ö": "oe", "ü": "ue",
        "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss",
    }
    for char, rep in umlaut_map.items():
        name = name.replace(char, rep)
    name = name.lower()
    name = re.sub(r"[&,\s]+", "_", name)
    name = re.sub(r"[^\w_]", "", name)
    name = re.sub(r"_+", "_", name).strip("_")
    return name


# ─────────────────────────────────────────────────────────────────────────────
# Load category map from product-info.csv
# ─────────────────────────────────────────────────────────────────────────────
def load_category_map() -> Dict[str, str]:
    """Return {category_name_lower: slug} for all categories in product-info.csv."""
    cat_map: Dict[str, str] = {}
    if not PRODUCT_INFO_CSV.exists():
        return cat_map
    with open(PRODUCT_INFO_CSV, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header_found = False
        for row in reader:
            if not row or len(row) < 2:
                continue
            if not header_found:
                if row[0].strip().lower() == "name" and "url" in row[1].strip().lower():
                    header_found = True
                continue
            name = row[0].strip()
            if name and name.lower() not in ("name",):
                cat_map[name.lower()] = slugify(name)
    return cat_map


# ─────────────────────────────────────────────────────────────────────────────
# Load already-fetched article numbers from all CSVs
# ─────────────────────────────────────────────────────────────────────────────
def load_existing_article_numbers() -> Set[str]:
    """
    Scan every CSV in output_local/specs/ and collect all article_numbers.
    Only mark as 'seen' if it has both Name and Price.
    """
    seen: Set[str] = set()
    if not OUTPUT_DIR.exists():
        return seen
    for csv_path in OUTPUT_DIR.glob("*.csv"):
        try:
            with open(csv_path, "r", encoding="utf-8", errors="ignore") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    art = (row.get("article_number") or "").strip()
                    name = (row.get("name") or "").strip()
                    price = (row.get("price") or "").strip()
                    if art and name and price:
                        seen.add(art)
        except Exception as e:
            logger.warning(f"Could not read {csv_path.name}: {e}")
    logger.info(f"Loaded {len(seen):,} COMPLETE article numbers from CSVs.")
    return seen


# ─────────────────────────────────────────────────────────────────────────────
# Extract category from Apollo state JSON (most reliable method)
# ─────────────────────────────────────────────────────────────────────────────
def extract_category_from_page(html: str) -> str:
    """
    Extract category from page. Priority:
      1. Apollo JSON __ARTICLE_DETAIL_APOLLO_STATE__ breadcrumbs
      2. Breadcrumb DOM elements
      3. URL path segment
    Returns the human-readable category name (not slug).
    """
    try:
        soup = BeautifulSoup(html, "lxml")

        # ── 1. Apollo state (most reliable) ──────────────────────────────
        for script in soup.find_all("script"):
            content = script.string or ""
            if "__ARTICLE_DETAIL_APOLLO_STATE__" not in content:
                continue
            parts = content.split("=", 1)
            if len(parts) < 2:
                continue
            raw = parts[1].strip()
            try:
                decoder = json.JSONDecoder()
                state, _ = decoder.raw_decode(raw)
            except json.JSONDecodeError:
                continue

            # Collect Breadcrumb objects, skip root "Sortiment"
            breadcrumbs = [
                v for v in state.values()
                if isinstance(v, dict)
                and v.get("__typename") == "Breadcrumb"
                and v.get("name", "").lower() not in ("sortiment", "")
                and v.get("id") != "R000000"
            ]
            if breadcrumbs:
                # Sort by URL length ascending → first real top-level category
                breadcrumbs.sort(key=lambda x: len(x.get("url", "")))
                return breadcrumbs[0]["name"]

        # ── 2. DOM breadcrumb fallback ────────────────────────────────────
        bc_items = soup.select(
            'nav[aria-label="Breadcrumb"] ol li, '
            'nav[aria-label="breadcrumb"] li, '
            '.breadcrumb li, '
            '[data-testid="breadcrumb"] li, '
            '[aria-label="Breadcrumb"] li'
        )
        for item in bc_items[1:]:  # skip "Home"
            text = item.get_text(strip=True)
            if text and text.lower() not in ("sortiment", "home", "hornbach"):
                return text

    except Exception as e:
        logger.debug(f"Category extraction error: {e}")

    return ""  # caller will handle empty → uncategorized


# ─────────────────────────────────────────────────────────────────────────────
# Parse a PDP page into a Product
# ─────────────────────────────────────────────────────────────────────────────
def parse_pdp(html: str, url: str) -> Optional[Product]:
    try:
        soup = BeautifulSoup(html, "lxml")

        # Article number (primary: DOM element; fallback: URL)
        article_number = ""
        art_el = soup.select_one(
            '[data-testid="article-number"], span.article-number, [data-tn="article-number"]'
        )
        if art_el:
            raw = art_el.get_text(strip=True)
            article_number = re.sub(r"^Art\.?\s*", "", raw).strip()
        if not article_number:
            m = re.search(r"/(\d{5,10})/?(?:\?|$)", url)
            if m:
                article_number = m.group(1)

        if not article_number:
            return None  # cannot identify product

        # Product name
        name = ""
        name_el = soup.select_one(
            'h1[data-testid="article-title"], h1.article-title, h1'
        )
        if name_el:
            name = name_el.get_text(strip=True)

        # Price
        # NOTE: Hornbach uses data-testid="prices" (plural) on the live site.
        # The accessible <span> inside contains e.g. "Preis — 39,95 € * pro ST"
        # The aria-hidden <span> inside contains just "39,95 €*" – easier to parse.
        price = ""
        price_el = soup.select_one(
            '[data-testid="prices"], '   # live site (plural)
            '[data-testid="price"], '    # older / fallback
            '.price-price__price'
        )
        if price_el:
            raw = price_el.get_text(separator=" ", strip=True).replace("\xa0", "")
            m = re.search(r"[\d.,]+", raw)
            if m:
                val = m.group()
                if "," in val and "." in val:
                    val = val.replace(".", "").replace(",", ".")
                elif "," in val:
                    val = val.replace(",", ".")
                price = val

        # Image
        image_url = ""
        img_el = soup.select_one(
            '[data-testid="article-image"] img, '
            '[data-testid*="image"] img, '
            '.hb-product-image img, .product-image img'
        )
        if img_el:
            image_url = (
                img_el.get("src")
                or img_el.get("data-src")
                or img_el.get("data-lazy-src")
                or ""
            )

        # Availability
        availability = ""
        avail_el = soup.select_one(
            '[data-testid="availability"], .js-stock-availability'
        )
        if avail_el:
            availability = avail_el.get_text(strip=True)

        # Category
        category = extract_category_from_page(html)

        # Specs
        specs = _extract_spec_tables(html)

        return Product(
            name=name,
            price=price,
            currency="EUR",
            product_url=url,
            image_url=image_url,
            availability=availability,
            article_number=article_number,
            category=category,
            specs=specs,
        )
    except Exception as e:
        logger.error(f"parse_pdp error ({url}): {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Thread-safe incremental CSV writer
# ─────────────────────────────────────────────────────────────────────────────
class CSVStore:
    """
    Manages one CSV file per category. Thread-safe via a lock.
    Reads the existing header before appending so no columns are lost.
    Can handle new spec columns by buffering and flushing periodically.
    """
    BASE_COLUMNS = [
        "article_number", "name", "price", "currency",
        "product_url", "image_url", "availability", "category",
    ]

    def __init__(self):
        self._lock = threading.Lock()
        self._buffers: Dict[str, List[Dict]] = {}          # slug → pending rows
        self._headers: Dict[str, List[str]] = {}           # slug → field names
        self._known_articles: Set[str] = set()             # runtime dedup guard

    def is_known(self, article_number: str) -> bool:
        return article_number in self._known_articles

    def add(self, product: Product, cat_slug: str):
        """Buffer a product row. Flush if buffer is large enough."""
        with self._lock:
            if product.article_number in self._known_articles:
                return  # second-level runtime dedup

            self._known_articles.add(product.article_number)

            row = {
                "article_number": product.article_number,
                "name": product.name,
                "price": product.price,
                "currency": product.currency,
                "product_url": product.product_url,
                "image_url": product.image_url,
                "availability": product.availability,
                "category": product.category,
            }
            row.update(product.specs)

            if cat_slug not in self._buffers:
                self._buffers[cat_slug] = []
                self._headers[cat_slug] = self._read_existing_header(cat_slug)

            # Track new spec columns
            for k in row:
                if k not in self._headers[cat_slug]:
                    self._headers[cat_slug].append(k)

            self._buffers[cat_slug].append(row)

            if len(self._buffers[cat_slug]) >= BATCH_FLUSH:
                self._flush_locked(cat_slug)

    def _read_existing_header(self, cat_slug: str) -> List[str]:
        """Read header row from existing CSV, or return base columns."""
        csv_path = OUTPUT_DIR / f"{cat_slug}.csv"
        if csv_path.exists():
            try:
                with open(csv_path, "r", encoding="utf-8") as f:
                    reader = csv.reader(f)
                    header = next(reader, None)
                    if header:
                        return header
            except Exception:
                pass
        return list(self.BASE_COLUMNS)

    def _flush_locked(self, cat_slug: str):
        """Write buffered rows to disk. Must be called with lock held."""
        rows = self._buffers.get(cat_slug, [])
        if not rows:
            return
        csv_path = OUTPUT_DIR / f"{cat_slug}.csv"
        fieldnames = self._headers[cat_slug]
        csv_exists = csv_path.exists()
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        with open(csv_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            if not csv_exists:
                writer.writeheader()
            writer.writerows(rows)
        self._buffers[cat_slug] = []

    def flush_all(self):
        """Flush all remaining buffers to disk."""
        with self._lock:
            for slug in list(self._buffers.keys()):
                self._flush_locked(slug)


# ─────────────────────────────────────────────────────────────────────────────
# Async fast fetcher
# ─────────────────────────────────────────────────────────────────────────────
class FastFetcher:
    def __init__(self, urls: List[str], cat_map: Dict[str, str], store: CSVStore):
        self.urls = urls
        self.cat_map = cat_map          # {name_lower: slug}
        self.store = store
        self.semaphore = asyncio.Semaphore(MAX_CONCURRENT)
        self.success = 0
        self.failed = 0

        # Proxy
        proxy = cfg.proxy_url
        if not proxy and cfg.proxy_pool:
            proxy = cfg.proxy_pool[0]
        self.proxy = proxy

        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
        }

    def _resolve_category_slug(self, product: Product) -> str:
        """
        Map the product's category name to a slug that matches an existing CSV.
        Falls back to 'uncategorized' if no match found.
        """
        cat_lower = product.category.lower().strip()
        if not cat_lower:
            return "uncategorized"

        # Exact match
        if cat_lower in self.cat_map:
            return self.cat_map[cat_lower]

        # Partial match (longest match wins)
        best_slug = None
        best_len = 0
        for name_l, slug in self.cat_map.items():
            if name_l in cat_lower or cat_lower in name_l:
                if len(name_l) > best_len:
                    best_len = len(name_l)
                    best_slug = slug

        return best_slug or slugify(product.category) or "uncategorized"

    async def fetch_one(self, client: httpx.AsyncClient, url: str) -> bool:
        async with self.semaphore:
            for attempt in range(RETRY_COUNT):
                try:
                    resp = await client.get(url, timeout=TIMEOUT, follow_redirects=True)
                    if resp.status_code == 200:
                        product = parse_pdp(resp.text, url)
                        if product:
                            cat_slug = self._resolve_category_slug(product)
                            self.store.add(product, cat_slug)
                            logger.info(f"✓ {product.article_number} → {cat_slug}.csv  ({product.name[:50]})")
                            return True
                        else:
                            logger.warning(f"✗ parse failed: {url}")
                            return False
                    elif resp.status_code == 404:
                        logger.warning(f"✗ 404: {url}")
                        return False
                    else:
                        logger.warning(f"! HTTP {resp.status_code}: {url} (attempt {attempt+1})")
                except httpx.TimeoutException:
                    logger.warning(f"! Timeout: {url} (attempt {attempt+1})")
                except Exception as e:
                    logger.warning(f"! Error: {url} → {e} (attempt {attempt+1})")

                if attempt < RETRY_COUNT - 1:
                    await asyncio.sleep(2 ** attempt)

            self.failed += 1
            return False

    async def run(self):
        logger.info(f"Starting fetch of {len(self.urls):,} URLs with {MAX_CONCURRENT} workers…")
        proxy_cfg = self.proxy if self.proxy else None
        async with httpx.AsyncClient(headers=self.headers, proxy=proxy_cfg) as client:
            tasks = [self.fetch_one(client, url) for url in self.urls]
            results = await asyncio.gather(*tasks, return_exceptions=False)

        self.success = sum(1 for r in results if r is True)
        self.store.flush_all()
        logger.info(
            f"Done. Success: {self.success:,} | Failed/Skipped: {self.failed:,} "
            f"| Total: {len(self.urls):,}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
async def main():
    if not MISSING_URLS_FILE.exists():
        logger.error(f"missing_urls.txt not found at: {MISSING_URLS_FILE}")
        return

    # ── Step 1: Load category map ─────────────────────────────────────────
    cat_map = load_category_map()
    logger.info(f"Loaded {len(cat_map)} categories from product-info.csv")

    # ── Step 2: Load already-scraped article numbers ──────────────────────
    existing_articles = load_existing_article_numbers()

    # Pre-populate the runtime store with existing articles so we don't duplicate
    store = CSVStore()
    store._known_articles = set(existing_articles)

    # ── Step 3: Filter missing_urls.txt to genuinely missing ones ─────────
    with open(MISSING_URLS_FILE, "r", encoding="utf-8") as f:
        raw_lines = [line.strip() for line in f if line.strip()]

    def normalize_url(u: str) -> str:
        """
        Strip mirror artifacts like /index.html/, /index.html.tmp/ etc.
        from URLs so they resolve correctly on the live site.
        """
        # Remove /index.html/, /index.html.tmp/ and trailing variants
        u = re.sub(r"/index\.html(\.tmp)?/?$", "/", u)
        u = re.sub(r"/index\.html(\.tmp)?/", "/", u)
        # Ensure clean trailing slash for category-style paths
        if not u.endswith("/") and re.search(r"/\d{5,10}$", u):
            u += "/"
        return u

    # Normalize + deduplicate URLs
    seen_norm: Set[str] = set()
    all_urls: List[str] = []
    for raw in raw_lines:
        norm = normalize_url(raw)
        if norm not in seen_norm:
            seen_norm.add(norm)
            all_urls.append(norm)

    logger.info(f"After normalization: {len(all_urls):,} unique URLs (was {len(raw_lines):,})")

    todo_urls: List[str] = []
    skipped = 0
    for url in all_urls:
        # Extract article number from URL to check existence
        m = re.search(r"/(\d{5,10})/?(?:\?|$)", url)
        if m and m.group(1) in existing_articles:
            skipped += 1
            continue
        todo_urls.append(url)

    logger.info(
        f"Total in missing_urls.txt : {len(all_urls):,}\n"
        f"  Already in CSVs (skip)  : {skipped:,}\n"
        f"  To fetch (genuinely new): {len(todo_urls):,}"
    )

    if not todo_urls:
        logger.info("Nothing to fetch – all URLs already exist in CSVs. ✓")
        return

    # ── Step 4: Fetch ─────────────────────────────────────────────────────
    fetcher = FastFetcher(todo_urls, cat_map, store)
    await fetcher.run()


if __name__ == "__main__":
    asyncio.run(main())
