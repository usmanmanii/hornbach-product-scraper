#!/usr/bin/env python3
"""
spec_scraper.py – Hornbach.de Specification Scraper

Scrapes ALL categories from product-info.csv, visits every product detail
page (PDP), extracts specification tables, and saves one CSV per category.

Features:
  • Resume from last stopped position (state.json)
  • Retry queue for failed products (retry_queue.json)
  • Per-category CSV with dynamic spec columns
  • Graceful SIGINT handling (Ctrl+C saves state, exits cleanly)

Usage:
  # Full run (all categories, resumes automatically)
  python spec_scraper.py

  # Drain retry queue only
  python spec_scraper.py --retry

  # Single category (by name from product-info.csv)
  python spec_scraper.py --category "Bad&Sanitär"

  # Limit pages per category (for testing)
  python spec_scraper.py --pages 2

  # Disable proxy
  python spec_scraper.py --no-proxy
"""

import argparse
import csv
import json
import logging
import os
import re
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout

from browser import BrowserManager, handle_cookie_consent, handle_market_selection
from config import cfg, setup_logging
from scraper import _extract_products_from_html, _extract_spec_tables, _human_delay
from models import Product

logger = logging.getLogger("hornbach_scraper.spec_scraper")

# ─────────────────────────────────────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent
STATE_FILE = BASE_DIR / "state.json"
RETRY_FILE = BASE_DIR / "retry_queue.json"
OUTPUT_DIR = BASE_DIR / "output" / "specs"
CSV_DIR = BASE_DIR / "product-info.csv"

# ─────────────────────────────────────────────────────────────────────────────
# Categories loader
# ─────────────────────────────────────────────────────────────────────────────

def load_categories(csv_path: Path) -> List[Dict]:
    """
    Parse product-info.csv and return list of {name, url, products, pages}.
    The CSV has this structure:
        Name,URL,Products,Pages
        Bad&Sanitär,https://...,42282,588
    """
    categories = []
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header_found = False
        for row in reader:
            if not row or len(row) < 2:
                continue
            # Skip until we hit the header row
            if not header_found:
                if row[0].strip().lower() == "name" and "url" in row[1].strip().lower():
                    header_found = True
                continue
            name = row[0].strip()
            url = row[1].strip()
            if not name or not url or not url.startswith("http"):
                continue
            products_count = int(row[2].strip()) if len(row) > 2 and row[2].strip().isdigit() else 0
            pages_count = int(row[3].strip()) if len(row) > 3 and row[3].strip().isdigit() else 0
            # Extract path from full URL (e.g. /c/bad-sanitaer/S474/)
            path = url.replace(cfg.base_url, "").rstrip("/") + "/"
            categories.append({
                "name": name,
                "url": url,
                "path": path,
                "total_products": products_count,
                "total_pages": pages_count,
            })
    return categories


def slugify(name: str) -> str:
    """Convert a category name to a safe ASCII filename slug."""
    # Replace German umlauts first so filenames stay readable
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


# ─────────────────────────────────────────────────────────────────────────────
# State Manager
# ─────────────────────────────────────────────────────────────────────────────

class StateManager:
    """
    Persists scraping progress to state.json so runs can be resumed.

    State structure:
    {
      "version": 1,
      "categories": {
        "Bad&Sanitär": {
          "status": "pending|in_progress|done",
          "current_page": 1,
          "scraped_urls": ["https://..."],
          "products_scraped": 0,
          "products_failed": 0
        }
      },
      "last_updated": "2026-04-03T21:00:00"
    }
    """

    def __init__(self, path: Path):
        self.path = path
        self.state: Dict = {"version": 1, "categories": {}, "last_updated": ""}
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self.state = json.load(f)
                logger.info("📂 Loaded state from %s", self.path)
            except Exception as e:
                logger.warning("Could not load state file, starting fresh: %s", e)

    def save(self):
        self.state["last_updated"] = datetime.now().isoformat()
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error("Failed to save state: %s", e)

    def get_category(self, name: str) -> Dict:
        return self.state["categories"].get(name, {
            "status": "pending",
            "current_page": 1,
            "scraped_urls": [],
            "products_scraped": 0,
            "products_failed": 0,
        })

    def update_category(self, name: str, **kwargs):
        cat = self.get_category(name)
        cat.update(kwargs)
        self.state["categories"][name] = cat
        self.save()

    def mark_url_scraped(self, category_name: str, url: str):
        cat = self.get_category(category_name)
        if url not in cat["scraped_urls"]:
            cat["scraped_urls"].append(url)
        cat["products_scraped"] = len(cat["scraped_urls"])
        self.state["categories"][category_name] = cat

    def is_url_scraped(self, category_name: str, url: str) -> bool:
        return url in self.get_category(category_name).get("scraped_urls", [])

    def is_category_done(self, category_name: str) -> bool:
        return self.get_category(category_name).get("status") == "done"

    def get_resume_page(self, category_name: str) -> int:
        return self.get_category(category_name).get("current_page", 1)


# ─────────────────────────────────────────────────────────────────────────────
# Retry Queue
# ─────────────────────────────────────────────────────────────────────────────

class RetryQueue:
    """
    Manages failed product URLs in retry_queue.json.

    Each entry:
    {
      "category": "Bad&Sanitär",
      "product_url": "https://...",
      "product_name": "...",
      "article_number": "...",
      "attempts": 1,
      "last_error": "Timeout"
    }
    """

    MAX_ATTEMPTS = 5

    def __init__(self, path: Path):
        self.path = path
        self.queue: List[Dict] = []
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self.queue = json.load(f)
                logger.info("🔁 Retry queue: %d items loaded", len(self.queue))
            except Exception as e:
                logger.warning("Could not load retry queue: %s", e)

    def save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.queue, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error("Failed to save retry queue: %s", e)

    def add(self, category: str, product_url: str, product_name: str = "",
            article_number: str = "", error: str = ""):
        # Skip if already at max attempts
        for entry in self.queue:
            if entry["product_url"] == product_url:
                entry["attempts"] += 1
                entry["last_error"] = error
                self.save()
                return
        self.queue.append({
            "category": category,
            "product_url": product_url,
            "product_name": product_name,
            "article_number": article_number,
            "attempts": 1,
            "last_error": error,
        })
        self.save()

    def pop_all_for_category(self, category: str) -> List[Dict]:
        """Return and remove all retryable items for a category."""
        retryable = [e for e in self.queue
                     if e["category"] == category and e["attempts"] < self.MAX_ATTEMPTS]
        self.queue = [e for e in self.queue if e not in retryable]
        return retryable

    def pop_all(self) -> List[Dict]:
        """Return and remove all retryable items."""
        retryable = [e for e in self.queue if e["attempts"] < self.MAX_ATTEMPTS]
        self.queue = [e for e in self.queue
                      if e["attempts"] >= self.MAX_ATTEMPTS]
        return retryable

    def count(self) -> int:
        return len(self.queue)


# ─────────────────────────────────────────────────────────────────────────────
# Category CSV Writer
# ─────────────────────────────────────────────────────────────────────────────

class CategoryCSVWriter:
    """
    Writes products to a per-category CSV file incrementally.

    Since spec keys vary per product, we buffer all rows in memory and
    flush to disk with a final header when done (or on checkpoint).

    For very large categories, we use an append strategy with a two-pass
    approach: first collect all spec keys, then write the header + rows.
    """

    # Base columns always present in every CSV
    BASE_COLUMNS = [
        "article_number",
        "name",
        "price",
        "currency",
        "product_url",
        "image_url",
        "availability",
        "category",
    ]

    def __init__(self, csv_path: Path):
        self.csv_path = csv_path
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        self.rows: List[Dict] = []
        self.spec_keys: List[str] = []  # Ordered list of unique spec keys
        self._seen_keys: set = set()
        # Load existing rows if resuming
        self._load_existing()

    def _load_existing(self):
        """Load existing rows from CSV if resuming a previous run."""
        if not self.csv_path.exists():
            return
        try:
            with open(self.csv_path, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                if reader.fieldnames:
                    for key in reader.fieldnames:
                        if key not in self.BASE_COLUMNS and key not in self._seen_keys:
                            self.spec_keys.append(key)
                            self._seen_keys.add(key)
                for row in reader:
                    self.rows.append(dict(row))
            logger.info(
                "  ↳ Resumed CSV '%s': %d existing rows, %d spec keys",
                self.csv_path.name, len(self.rows), len(self.spec_keys)
            )
        except Exception as e:
            logger.warning("Could not load existing CSV '%s': %s", self.csv_path, e)

    def add(self, product: "Product"):
        """Add a product row to the buffer."""
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
        # Track new spec keys (preserve insertion order)
        for key in product.specs:
            if key not in self._seen_keys:
                self.spec_keys.append(key)
                self._seen_keys.add(key)
        row.update(product.specs)
        self.rows.append(row)

    def flush(self):
        """Write all buffered rows to disk with current header."""
        all_columns = self.BASE_COLUMNS + self.spec_keys
        try:
            with open(self.csv_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=all_columns, extrasaction="ignore")
                writer.writeheader()
                for row in self.rows:
                    writer.writerow(row)
            logger.debug("  💾 Flushed %d rows to %s", len(self.rows), self.csv_path.name)
        except Exception as e:
            logger.error("Failed to flush CSV '%s': %s", self.csv_path, e)

    @property
    def row_count(self) -> int:
        return len(self.rows)


# ─────────────────────────────────────────────────────────────────────────────
# Core scraping engine
# ─────────────────────────────────────────────────────────────────────────────

class SpecScraper:
    """
    Orchestrates the full specification scrape:
      1. Iterate categories from product-info.csv
      2. Paginate listing pages to get product URLs
      3. Visit each PDP to extract spec tables
      4. Write incremental per-category CSVs
      5. Track progress in state.json, failures in retry_queue.json
    """

    # How often to save state (every N products)
    CHECKPOINT_INTERVAL = 25

    def __init__(self, max_pages: int = 0, use_proxy: bool = True, start_page: int = 0, end_page: int = 0):
        self.max_pages = max_pages or cfg.max_pages or 0
        self.use_proxy = use_proxy
        self.start_page = start_page
        self.end_page = end_page
        self._bm: Optional[BrowserManager] = None
        self._page: Optional[Page] = None
        self._running = True  # Set to False on SIGINT
        self.state = StateManager(STATE_FILE)
        self.retry_q = RetryQueue(RETRY_FILE)
        # Writers keyed by category name
        self._writers: Dict[str, CategoryCSVWriter] = {}

    # ── Lifecycle ────────────────────────────────────────────────────────

    def start(self):
        self._bm = BrowserManager(use_proxy=self.use_proxy)
        self._bm.start()
        self._page = self._bm.new_page()
        logger.info("🚀 SpecScraper started.")

    def stop(self):
        # Flush all writers on exit
        for name, writer in self._writers.items():
            logger.info("  💾 Flushing CSV for '%s' (%d rows)…", name, writer.row_count)
            writer.flush()
        self.state.save()
        self.retry_q.save()
        if self._bm:
            self._bm.stop()
        logger.info("🛑 SpecScraper stopped.")

    def _get_writer(self, category_name: str) -> CategoryCSVWriter:
        if category_name not in self._writers:
            slug = slugify(category_name)
            csv_path = OUTPUT_DIR / f"{slug}.csv"
            self._writers[category_name] = CategoryCSVWriter(csv_path)
        return self._writers[category_name]

    # ── Navigation ───────────────────────────────────────────────────────

    def _navigate(self, url: str, wait_selector: Optional[str] = None) -> Optional[str]:
        """Navigate to URL, return page HTML or None on failure."""
        for attempt in range(1, cfg.max_retries + 1):
            try:
                resp = self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
                if resp and resp.status >= 400:
                    if resp.status in (403, 406, 429) and self._bm:
                        logger.warning("HTTP %d – rotating proxy…", resp.status)
                        self._bm.rotate_proxy()
                        self._page = self._bm.new_page()
                    raise Exception(f"HTTP {resp.status}")

                # Scroll to trigger lazy loads
                self._page.evaluate("window.scrollBy(0, 800)")
                time.sleep(0.8)

                if wait_selector:
                    try:
                        self._page.wait_for_selector(wait_selector, timeout=8000)
                    except PlaywrightTimeout:
                        pass  # Selector may not exist on this page

                return self._page.content()
            except Exception as exc:
                logger.warning("  ⚠ Attempt %d/%d failed for %s: %s",
                               attempt, cfg.max_retries, url, exc)
                if attempt < cfg.max_retries:
                    time.sleep(3 * attempt)
        return None

    def _has_next_page(self) -> bool:
        """Check for next page using confirmed live selector.
        
        Hornbach lazy-loads pagination via JS, so we must scroll to the
        bottom of the page first to trigger it before checking visibility.
        """
        try:
            # Scroll to bottom to trigger lazy-loaded pagination controls
            self._page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(1.0)  # Wait for lazy-load JS to execute
            # Also scroll back slightly to ensure pagination renders
            self._page.evaluate("window.scrollBy(0, -200)")
            time.sleep(0.3)

            locator = self._page.locator(
                'a[data-testid="pagination-bar-next-button"], '
                'a[data-tn="pagination-next"], '
                'a[rel="next"], '
                'button[aria-label="nächste Seite"], '
                'button[aria-label="Nächste Seite"], '
                '[data-testid="pagination-bar-next-button"]'
            ).first
            visible = locator.is_visible(timeout=4000)
            if not visible:
                # Last resort: check for higher page numbers in URL pattern links
                html = self._page.content()
                from bs4 import BeautifulSoup as _BS
                soup = _BS(html, "lxml")
                # Look for any pagination link with page= param or a next-style button
                pagination_links = soup.select(
                    '[data-testid*="pagination"], [data-tn*="pagination"], '
                    '.pagination a, nav[aria-label*="Seite"] a, '
                    'a[href*="page="]'
                )
                logger.debug("  Pagination elements found: %d", len(pagination_links))
                # If there are pagination links with future pages, there's a next page
                has_numbered = any(
                    'page=' in (el.get('href') or '') for el in pagination_links
                )
                return has_numbered
            return visible
        except Exception as e:
            logger.debug("_has_next_page error: %s", e)
            return False

    # ── Product URL extraction from listing pages ─────────────────────────

    def _get_product_urls_from_html(self, html: str) -> List[Tuple[str, str, str]]:
        """
        Extract (product_url, name, article_number) from a listing page HTML.
        Returns list of tuples.

        Confirmed selectors from live page inspection (2026-04-04):
          - Card:    li[data-testid="article-card"]
          - Title:   a[data-testid="article-title"]
          - Art. #:  extracted from the URL's trailing numeric segment
        """
        from bs4 import BeautifulSoup
        from urllib.parse import urljoin
        import re as _re
        soup = BeautifulSoup(html, "lxml")
        results = []
        seen = set()

        # Primary: confirmed stable selector from live inspection
        for a in soup.select('a[data-testid="article-title"]'):
            href = a.get("href", "") or ""
            if not href:
                continue
            full_url = urljoin(cfg.base_url, href) if not href.startswith("http") else href
            if full_url in seen:
                continue
            seen.add(full_url)

            # Name from title attribute (more complete) or text
            name = a.get("title", "").strip() or a.get_text(strip=True)

            # Article number: last numeric segment in URL
            # e.g. /p/ecoflow-solarmodul.../10473855/  →  10473855
            art_match = _re.search(r'/(\d{5,10})/?$', href)
            article_number = art_match.group(1) if art_match else ""

            results.append((full_url, name, article_number))

        # Fallback: any /p/ links if primary found nothing
        if not results:
            for a in soup.select('a[href*="/p/"]'):
                href = a.get("href", "") or ""
                if not href:
                    continue
                full_url = urljoin(cfg.base_url, href) if not href.startswith("http") else href
                if full_url in seen:
                    continue
                seen.add(full_url)
                art_match = _re.search(r'/(\d{5,10})/?$', href)
                article_number = art_match.group(1) if art_match else ""
                name = a.get("title", "").strip() or a.get_text(strip=True)
                results.append((full_url, name, article_number))

        logger.info("  ↳ Found %d product URLs on listing page", len(results))
        return results

    # ── PDP spec extraction ───────────────────────────────────────────────

    def _scrape_product_specs(
        self, url: str, name: str, article_number: str, category: str
    ) -> Optional[Product]:
        """Visit a product detail page and extract all spec data."""
        html = self._navigate(url, wait_selector="section#attribute, section#article-details-accordion")
        if not html:
            return None

        # Click "MEHR ANZEIGEN" / "Mehr anzeigen" button inside #attribute section
        # to expand all hidden specification rows before grabbing HTML
        try:
            mehr_btn = self._page.locator(
                "section#attribute button, "
                "section#attribute [role='button'], "
                "button:has-text('Mehr anzeigen'), "
                "button:has-text('MEHR ANZEIGEN'), "
                "button:has-text('Alle Eigenschaften'), "
                "[data-testid='show-more-attributes']"
            ).first
            if mehr_btn.is_visible(timeout=2000):
                mehr_btn.click()
                time.sleep(0.5)
                html = self._page.content()
        except Exception:
            pass  # Button may not exist; proceed with current HTML

        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")

        # Extract specs from confirmed section#attribute structure
        specs = _extract_spec_tables(html)

        # ── Price ──────────────────────────────────────────────────────────
        price = ""
        # Try multiple price selectors (Hornbach uses dynamic class names)
        for price_sel in [
            '[data-testid="price"]',
            '[data-testid="product-price"]',
            '.price-price__price',
            'span[aria-label*="Preis"]',
            '.price',
        ]:
            price_el = soup.select_one(price_sel)
            if price_el:
                raw = price_el.get_text(separator=" ", strip=True)
                m = re.search(r"[\d.,]+", raw.replace("\xa0", ""))
                if m:
                    val = m.group()
                    if "," in val and "." in val:
                        val = val.replace(".", "").replace(",", ".")
                    elif "," in val:
                        val = val.replace(",", ".")
                    price = val
                    break

        # ── Image ──────────────────────────────────────────────────────────
        image_url = ""
        for img_sel in [
            '[data-testid="product-image"] img',
            '[data-testid="article-image"] img',
            '.product-image img',
            '.article-image img',
            'img[data-testid*="image"]',
        ]:
            img_el = soup.select_one(img_sel)
            if img_el:
                image_url = img_el.get("src") or img_el.get("data-src") or ""
                if image_url:
                    break

        # ── Availability ───────────────────────────────────────────────────
        availability = ""
        for avail_sel in [
            '[data-testid="availability"]',
            '[data-testid="stock-status"]',
            '.js-stock-availability',
            '[data-tn="stock-status"]',
            '.availability-info',
        ]:
            avail_el = soup.select_one(avail_sel)
            if avail_el:
                availability = avail_el.get_text(strip=True)
                break

        # ── Article number (PDP is most reliable) ─────────────────────────
        for art_sel in [
            '[data-testid="article-number"]',
            "[data-tn='article-number']",
            "span.article-number",
        ]:
            art_el = soup.select_one(art_sel)
            if art_el:
                article_number = art_el.get_text(strip=True).replace("Art. ", "").strip()
                break
        # Final fallback: parse from URL
        if not article_number:
            art_match = re.search(r'/(\d{5,10})/?$', url)
            if art_match:
                article_number = art_match.group(1)

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

    # ── Category scraping ─────────────────────────────────────────────────

    def scrape_category(self, category: Dict):
        """Scrape one full category: paginate listing → visit each PDP."""
        name = category["name"]
        path = category["path"]

        if self.state.is_category_done(name):
            logger.info("⏭  Category '%s' already complete, skipping.", name)
            return

        writer = self._get_writer(name)
        resume_page = self.state.get_resume_page(name)
        self.state.update_category(name, status="in_progress")

        page_num = self.start_page if self.start_page > 0 else resume_page
        base = cfg.base_url.rstrip("/")
        first_page_loaded = False
        products_this_run = 0

        logger.info("═" * 60)
        logger.info("📦 Category: %s (resuming from page %d)", name, page_num)
        logger.info("   URL: %s%s", base, path)

        while self._running:
            # Build paginated URL
            url = f"{base}{path}"
            if page_num > 1:
                sep = "&" if "?" in url else "?"
                url = f"{url}{sep}page={page_num}"

            logger.info("  ━ Listing page %d: %s", page_num, url)

            # Navigate to listing page
            try:
                resp = self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
                if resp and resp.status >= 400:
                    logger.error("  ✗ HTTP %d for listing page %d", resp.status, page_num)
                    break
            except Exception as exc:
                logger.error("  ✗ Failed to navigate to listing page %d: %s", page_num, exc)
                break

            # Handle cookie/market popups on FIRST page only (before content renders)
            if not first_page_loaded:
                try:
                    handle_cookie_consent(self._page)
                    handle_market_selection(self._page)
                except Exception:
                    pass
                first_page_loaded = True

            # Progressive scroll: trigger lazy-loaded product cards AND pagination
            # Hornbach uses lazy-loading for both product cards and pagination controls
            try:
                # Step 1: scroll partway to load initial products
                self._page.evaluate("window.scrollBy(0, 800)")
                time.sleep(1.0)
                # Step 2: wait for product cards to appear
                self._page.wait_for_selector(
                    'a[data-testid="article-title"], li[data-testid="article-card"]',
                    timeout=12000,
                )
                # Step 3: scroll to bottom to trigger lazy-loaded pagination
                self._page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(1.5)
                # Step 4: scroll back up a bit so page is stable
                self._page.evaluate("window.scrollTo(0, 0)")
                time.sleep(0.5)
            except PlaywrightTimeout:
                logger.warning("  ⚠ Product cards did not appear on page %d", page_num)
            except Exception as ex:
                logger.debug("  Scroll/wait error: %s", ex)

            html = self._page.content()
            if not html:
                logger.error("  ✗ Empty HTML on listing page %d.", page_num)
                break

            # Get product URLs from this listing page
            product_tuples = self._get_product_urls_from_html(html)
            if not product_tuples:
                logger.info("  ⚑ No products on page %d — end of category.", page_num)
                break

            logger.info("  ↳ %d products queued for page %d", len(product_tuples), page_num)

            # Scrape each product's PDP
            for product_url, product_name, article_number in product_tuples:
                if not self._running:
                    break

                # Skip already-scraped URLs
                if self.state.is_url_scraped(name, product_url):
                    logger.debug("    ⏭ Already scraped: %s", product_url)
                    continue

                logger.info("    → %s", product_name[:60] or product_url)

                try:
                    _human_delay()
                    product = self._scrape_product_specs(
                        product_url, product_name, article_number, name
                    )
                    if product and (product.specs or product.name):
                        writer.add(product)
                        self.state.mark_url_scraped(name, product_url)
                        products_this_run += 1
                        spec_count = len(product.specs)
                        logger.info(
                            "    ✓ %s | specs=%d | price=%s",
                            (product.name or product_url)[:55], spec_count, product.price
                        )
                    else:
                        logger.warning("    ✗ No data extracted for: %s", product_url)
                        self.retry_q.add(name, product_url, product_name, article_number,
                                         "No data extracted")

                except Exception as exc:
                    logger.warning("    ✗ Error scraping %s: %s", product_url, exc)
                    self.retry_q.add(name, product_url, product_name, article_number, str(exc))
                    self.state.update_category(
                        name, products_failed=self.state.get_category(name).get("products_failed", 0) + 1
                    )

                # Checkpoint: save state + flush CSV every N products
                if products_this_run % self.CHECKPOINT_INTERVAL == 0 and products_this_run > 0:
                    logger.info("  💾 Checkpoint: saving state & flushing CSV…")
                    self.state.save()
                    writer.flush()
                    self.retry_q.save()

            # Update current page in state
            self.state.update_category(name, current_page=page_num)

            # Pagination check
            if self.max_pages and page_num >= self.max_pages:
                logger.info("  ⑊ Reached max pages limit (%d).", self.max_pages)
                break
                
            if self.end_page > 0 and page_num >= self.end_page:
                logger.info("  ⑊ Reached end page limit (%d).", self.end_page)
                break

            # !! KEY FIX: After visiting PDPs, browser is on the LAST product page.
            # We must navigate BACK to the listing page before checking pagination,
            # otherwise _has_next_page() runs on a PDP (no pagination there → always False).
            listing_url = f"{base}{path}"
            if page_num > 1:
                sep = "&" if "?" in listing_url else "?"
                listing_url = f"{listing_url}{sep}page={page_num}"
            logger.info("  ↩ Returning to listing page %d to check pagination…", page_num)
            try:
                self._page.goto(listing_url, wait_until="domcontentloaded", timeout=30000)
                # Scroll to bottom to trigger lazy-loaded pagination
                self._page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(2.0)
                self._page.evaluate("window.scrollBy(0, -100)")
                time.sleep(0.5)
            except Exception as nav_exc:
                logger.warning("  ⚠ Could not navigate back to listing: %s", nav_exc)

            if not self._has_next_page():
                logger.info("  ⑊ No next page — category complete.")
                break

            page_num += 1
            _human_delay()

        # Final flush and mark done (only if fully completed without interrupt)
        writer.flush()
        if self._running:
            self.state.update_category(name, status="done", current_page=page_num)
            logger.info("✅ Category '%s' complete: %d rows in CSV.", name, writer.row_count)
        else:
            logger.info("⏸  Category '%s' paused at page %d. Will resume next run.", name, page_num)

    # ── Retry queue draining ──────────────────────────────────────────────

    def drain_retry_queue(self):
        """Process all items in the retry queue."""
        items = self.retry_q.pop_all()
        if not items:
            logger.info("✅ Retry queue is empty. Nothing to do.")
            return

        logger.info("🔁 Draining retry queue: %d items", len(items))
        processed = 0

        for entry in items:
            if not self._running:
                # Put unprocessed items back
                for unprocessed in items[processed:]:
                    self.retry_q.queue.append(unprocessed)
                break

            category = entry["category"]
            product_url = entry["product_url"]
            product_name = entry.get("product_name", "")
            article_number = entry.get("article_number", "")

            logger.info("  🔁 Retrying: %s", product_name or product_url)

            try:
                _human_delay()
                product = self._scrape_product_specs(
                    product_url, product_name, article_number, category
                )
                if product and (product.specs or product.name):
                    writer = self._get_writer(category)
                    writer.add(product)
                    self.state.mark_url_scraped(category, product_url)
                    logger.info("    ✓ Retry succeeded: %s | specs=%d",
                                product.name[:50], len(product.specs))
                else:
                    logger.warning("    ✗ Still no data for: %s", product_url)
                    self.retry_q.add(category, product_url, product_name, article_number,
                                     "Still no data after retry")
            except Exception as exc:
                logger.warning("    ✗ Retry failed: %s – %s", product_url, exc)
                self.retry_q.add(category, product_url, product_name, article_number, str(exc))

            processed += 1

        # Final flush
        for name, writer in self._writers.items():
            writer.flush()
        self.retry_q.save()
        logger.info("🔁 Retry pass complete. %d items processed.", processed)

    # ── Main run ─────────────────────────────────────────────────────────

    def run(self, categories: List[Dict], retry_only: bool = False):
        """Run the full scrape orchestration."""
        if retry_only:
            self.drain_retry_queue()
            return

        total = len(categories)
        for i, cat in enumerate(categories, 1):
            if not self._running:
                logger.info("⛔ Interrupted. Stopping after current category.")
                break
            logger.info("╔ %d/%d: %s", i, total, cat["name"])
            self.scrape_category(cat)
            if self._running and i < total:
                _human_delay()


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(
        description="🦏 Hornbach.de Specification Scraper",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python spec_scraper.py                            # scrape all categories
  python spec_scraper.py --retry                    # drain retry queue
  python spec_scraper.py --category "Bad&Sanitär"  # single category
  python spec_scraper.py --pages 3 --no-proxy       # test run
        """,
    )
    parser.add_argument("--retry", action="store_true",
                        help="Drain retry_queue.json only (no new scraping)")
    parser.add_argument("--category", "-c", default="",
                        help="Scrape only this category (exact name from product-info.csv)")
    parser.add_argument("--pages", "-p", type=int, default=0,
                        help="Max listing pages per category (0 = all)")
    parser.add_argument("--start-page", type=int, default=0,
                        help="Start from this page number (overrides resumed state)")
    parser.add_argument("--end-page", type=int, default=0,
                        help="Stop after this page number")
    parser.add_argument("--no-proxy", action="store_true",
                        help="Disable proxy usage")
    parser.add_argument("--reset", action="store_true",
                        help="Reset state for given category (or all if no --category)")
    parser.add_argument("--force", action="store_true",
                        help="Force re-scrape: reset state for the given --category before running (shorthand for --reset + --category)")
    return parser.parse_args()


def main():
    setup_logging()
    args = parse_args()

    logger.info("=" * 60)
    logger.info("🦏 Hornbach.de Specification Scraper")
    logger.info("=" * 60)

    # Load categories
    categories = load_categories(CSV_DIR)
    if not categories:
        logger.error("No categories found in product-info.csv. Exiting.")
        sys.exit(1)
    logger.info("📋 Loaded %d categories from product-info.csv", len(categories))

    # Filter to single category if requested
    if args.category:
        categories = [c for c in categories if c["name"] == args.category]
        if not categories:
            logger.error("Category '%s' not found in product-info.csv", args.category)
            sys.exit(1)

    scraper = SpecScraper(
        max_pages=args.pages, 
        use_proxy=not args.no_proxy,
        start_page=args.start_page,
        end_page=args.end_page
    )

    # Handle state reset
    if args.reset or args.force:
        target = args.category or "ALL"
        logger.warning("🔄 Resetting state for: %s", target)
        if args.category:
            scraper.state.update_category(args.category, status="pending",
                                          current_page=1, scraped_urls=[],
                                          products_scraped=0, products_failed=0)
            logger.info("✅ State reset for category '%s'", args.category)
            # Also delete the old CSV to avoid duplicates on re-scrape
            slug = slugify(args.category)
            old_csv = OUTPUT_DIR / f"{slug}.csv"
            if old_csv.exists():
                old_csv.unlink()
                logger.info("🗑  Deleted old CSV '%s' to avoid duplicates", old_csv.name)
        else:
            scraper.state.state["categories"] = {}
            scraper.state.save()
            # Delete all CSVs
            for f in OUTPUT_DIR.glob("*.csv"):
                f.unlink()
                logger.info("🗑  Deleted old CSV '%s'", f.name)
            logger.info("✅ Full state + CSV reset done")

    # Graceful SIGINT handler
    def _sigint_handler(sig, frame):
        logger.warning("\n⛔ Interrupted! Saving state and flushing CSVs…")
        scraper._running = False

    signal.signal(signal.SIGINT, _sigint_handler)
    signal.signal(signal.SIGTERM, _sigint_handler)

    # Proxy check
    proxies = cfg.get_all_proxies()
    if proxies:
        logger.info("📡 Proxy pool: %d proxy(ies)", len(proxies))
    else:
        logger.warning("⚠  No proxy configured. hornbach.de requires a German IP!")

    # Run
    try:
        scraper.start()
        scraper.run(categories, retry_only=args.retry)
    except KeyboardInterrupt:
        logger.warning("\n⛔ KeyboardInterrupt caught.")
        scraper._running = False
    except Exception as exc:
        logger.error("Fatal error: %s", exc, exc_info=True)
    finally:
        scraper.stop()

    # Summary
    logger.info("=" * 60)
    logger.info("📊 Run Summary")
    logger.info("   Retry queue: %d items pending", scraper.retry_q.count())
    logger.info("   Output CSVs: %s", OUTPUT_DIR)
    for name, writer in scraper._writers.items():
        logger.info("   %-40s %d rows", name + ":", writer.row_count)
    logger.info("=" * 60)

    # ── Missing Products Report ───────────────────────────────────────────
    _generate_missing_report(scraper, categories)

    logger.info("🏁 Done!")


def _generate_missing_report(scraper: "SpecScraper", categories: List[Dict]):
    """
    Write output/specs/missing_report.csv after every run.

    Columns: category, status, attempts, product_name, product_url, error

    status values:
      fetched  – successfully scraped (in state scraped_urls)
      failed   – in retry_queue.json (attempted but errored)
    """
    report_path = OUTPUT_DIR / "missing_report.csv"
    rows = []

    # Fetched: URLs stored in state's scraped_urls list
    for cat in categories:
        cat_name = cat["name"]
        cat_state = scraper.state.get_category(cat_name)
        for url in cat_state.get("scraped_urls", []):
            rows.append({
                "category": cat_name,
                "product_url": url,
                "product_name": "",
                "status": "fetched",
                "error": "",
                "attempts": 1,
            })

    # Failed: items sitting in retry queue
    for entry in scraper.retry_q.queue:
        rows.append({
            "category": entry.get("category", ""),
            "product_url": entry.get("product_url", ""),
            "product_name": entry.get("product_name", ""),
            "status": "failed",
            "error": entry.get("last_error", ""),
            "attempts": entry.get("attempts", 1),
        })

    if not rows:
        logger.info("📋 No products to report yet.")
        return

    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        with open(report_path, "w", newline="", encoding="utf-8") as f:
            writer_csv = csv.DictWriter(
                f,
                fieldnames=["category", "status", "attempts", "product_name", "product_url", "error"],
            )
            writer_csv.writeheader()
            # failed first so they're easy to spot at the top
            rows.sort(key=lambda r: (0 if r["status"] == "failed" else 1, r["category"]))
            writer_csv.writerows(rows)

        failed = sum(1 for r in rows if r["status"] == "failed")
        fetched = sum(1 for r in rows if r["status"] == "fetched")
        logger.info("📋 Missing report written → %s", report_path)
        logger.info("   ✅ Fetched : %d", fetched)
        logger.info("   ❌ Failed  : %d  (retry with: ./run_scraper.sh --retry)", failed)
    except Exception as e:
        logger.error("Failed to write missing report: %s", e)


if __name__ == "__main__":
    main()

