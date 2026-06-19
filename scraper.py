"""
scraper.py – Core scraping logic for hornbach.de.

Handles:
  • Navigating category / search pages
  • Extracting product data from listing cards
  • Drilling into product detail pages (PDP) for richer info
  • Pagination across multiple pages
  • Rate limiting and retry logic
"""

import re
import time
import random
import logging
from typing import List, Optional
from urllib.parse import urljoin

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout
from bs4 import BeautifulSoup
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from config import cfg
from models import Product
from browser import BrowserManager, handle_cookie_consent, handle_market_selection

logger = logging.getLogger("hornbach_scraper.scraper")


# ─────────────────────────────────────────────────────────────────────────────
# Delay helper
# ─────────────────────────────────────────────────────────────────────────────

def _human_delay():
    """Sleep for a random interval to mimic human browsing behaviour."""
    delay = random.uniform(cfg.min_delay, cfg.max_delay)
    logger.debug("Sleeping %.1fs…", delay)
    time.sleep(delay)


# ─────────────────────────────────────────────────────────────────────────────
# HTML parsing helpers
# ─────────────────────────────────────────────────────────────────────────────

def _extract_products_from_html(html: str, base_url: str, category: str = "") -> List[Product]:
    """
    Parse the product listing HTML and extract Product objects from
    product cards.

    Uses multiple CSS selectors to be resilient against minor site changes.
    """
    soup = BeautifulSoup(html, "lxml")
    products: List[Product] = []

    # Primary selectors for product cards
    # Hornbach uses a mix of data-testid, data-tn, and dynamic classes.
    cards = soup.select('[data-testid="article-card"], [data-tn="product-card"], .article-card, .product-card')
    
    if not cards:
        # Fallback: Look for the parent container of the article title links
        # Many products are wrapped in <div> tags without clear classes but with stable children
        title_links = soup.select('a[data-testid="article-title"]')
        for link in title_links:
            # The product card is typically a parent or grandparent div
            card = link.find_parent("div", limit=5)
            if card and card not in cards:
                cards.append(card)
    
    if not cards:
        # Last resort: Look for any list item or article with product-like structure
        cards = soup.select("article.product, li.product-list__item, [data-testid^='product-']")

    logger.info("Found %d product cards on page.", len(cards))

    for card in cards:
        try:
            product = _parse_product_card(card, base_url, category)
            if product and product.name:
                products.append(product)
                logger.debug("  ✓ %s", product)
        except Exception as exc:
            logger.warning("Failed to parse a product card: %s", exc)

    return products


def _parse_product_card(card, base_url: str, category: str) -> Optional[Product]:
    """Parse a single product card BeautifulSoup element into a Product."""
    product = Product(category=category)

    # ── Product name ─────────────────────────────────────────────────────
    name_el = (
        card.select_one('[data-testid="article-title"]')
        or card.select_one('[data-tn="product-card-title"]')
        or card.select_one("h2, h3")
        or card.select_one(".product-card__title, .product-title")
    )
    if name_el:
        product.name = name_el.get_text(strip=True)

    # ── Price ────────────────────────────────────────────────────────────
    price_el = (
        card.select_one('[data-testid="price"]')
        or card.select_one(".price-price__price")
        or card.select_one('[data-tn="product-card-price"]')
        or card.select_one(".product-price, .price")
    )
    if price_el:
        raw_price = price_el.get_text(strip=True)
        # Clean price text: "29,95 €" or "29.95"
        price_match = re.search(r"[\d.,]+", raw_price)
        if price_match:
            # Handle German format: 1.234,56 -> 1234.56
            val = price_match.group()
            if "," in val and "." in val:
                val = val.replace(".", "").replace(",", ".")
            elif "," in val:
                val = val.replace(",", ".")
            product.price = val
        # Detect currency
        if "€" in raw_price or "EUR" in raw_price:
            product.currency = "EUR"

    # ── Product URL ──────────────────────────────────────────────────────
    link_el = (
        card.select_one('a[data-testid="article-title"]')
        or card.select_one('[data-tn="product-card-body"]')
        or card.select_one("a[href*='/p/']")
        or card.select_one("a")
    )
    if link_el and link_el.get("href"):
        product.product_url = urljoin(base_url, link_el["href"])

    # ── Image URL ────────────────────────────────────────────────────────
    img_el = card.select_one("img")
    if img_el:
        product.image_url = (
            img_el.get("src")
            or img_el.get("data-src")
            or img_el.get("data-lazy-src")
            or img_el.get("data-srcset")
        )
        if product.image_url and not product.image_url.startswith("http"):
            product.image_url = urljoin(base_url, product.image_url)

    # ── Availability ─────────────────────────────────────────────────────
    avail_el = (
        card.select_one('[data-testid="availability"]')
        or card.select_one('[data-tn="availability"]')
        or card.select_one(".availability, .stock-status")
    )
    if avail_el:
        product.availability = avail_el.get_text(strip=True)

    # ── Article number ───────────────────────────────────────────────────
    art_el = (
        card.select_one('[data-testid="article-number"]')
        or card.select_one(".article-number")
        or card.select_one('[data-tn="article-number"]')
    )
    if art_el:
        product.article_number = art_el.get_text(strip=True).replace("Art. ", "")

    return product


def _extract_product_detail(html: str, base_url: str = "") -> dict:
    """
    Parse a product detail page (PDP) and return supplementary fields
    that may not be available on listing cards.
    """
    soup = BeautifulSoup(html, "lxml")
    detail: dict = {}

    # Description
    desc_el = (
        soup.select_one("#article-details-accordion")
        or soup.select_one(".description-list")
        or soup.select_one('[data-tn="product-description"]')
        or soup.select_one(".product-description")
    )
    if desc_el:
        detail["description"] = desc_el.get_text(separator=" ", strip=True)[:2000]

    # More precise availability from PDP
    avail_el = (
        soup.select_one(".js-stock-availability")
        or soup.select_one('[data-tn="stock-status"]')
        or soup.select_one(".availability-info")
    )
    if avail_el:
        detail["availability"] = avail_el.get_text(strip=True)

    # Article number (often cleaner on PDP)
    art_el = soup.select_one("span.article-number, [data-tn='article-number']")
    if art_el:
        detail["article_number"] = art_el.get_text(strip=True)

    return detail


def _extract_spec_tables(html: str) -> dict:
    """
    Extract specification tables from a Hornbach product detail page (PDP).

    Confirmed real Hornbach PDP structure (2026-04-04):
      <section id="attribute">
        <ul>
          <li>
            <div>Tiefe</div>          ← key
            <div>600 mm</div>         ← value
          </li>
          ...
        </ul>
      </section>

    Falls back to dl, table, and data-testid patterns for resilience.

    Returns:
        dict: {spec_label: spec_value} pairs, whitespace and linebreaks trimmed.
    """
    soup = BeautifulSoup(html, "lxml")
    specs: dict = {}

    # ── Pattern A (PRIMARY): section#attribute ul li ───────────────────────
    # This is the confirmed structure on hornbach.de PDPs (2026-04-04)
    attr_section = soup.find("section", id="attribute")
    if attr_section:
        for li in attr_section.find_all("li"):
            # Get direct-child divs only (not nested ones)
            divs = li.find_all("div", recursive=False)
            if len(divs) >= 2:
                key = _clean_text(divs[0].get_text())
                val = _clean_text(divs[1].get_text())
                if key and val:
                    specs[key] = val
            elif len(divs) == 0:
                # Some li may have direct text nodes as pairs
                children = [c for c in li.children
                            if hasattr(c, "get_text") and c.get_text(strip=True)]
                if len(children) >= 2:
                    key = _clean_text(children[0].get_text())
                    val = _clean_text(children[1].get_text())
                    if key and val and key != val:
                        specs[key] = val

    # ── Pattern B: data-testid / data-tn attribute rows ──────────────────
    if not specs:
        for row in soup.select(
            "[data-testid='spec-row'], [data-tn='spec-row'], "
            "[data-testid='attribute-row'], [data-testid='product-attribute'], "
            "[data-testid='attribute-item']"
        ):
            label_el = (
                row.select_one("[data-testid='spec-label'], [data-testid='attribute-label'], "
                               "[data-testid='attribute-name']")
                or row.select_one(".spec-label, .attribute-name, .key")
            )
            value_el = (
                row.select_one("[data-testid='spec-value'], [data-testid='attribute-value'], "
                               "[data-testid='attribute-val']")
                or row.select_one(".spec-value, .attribute-value, .value")
            )
            if label_el and value_el:
                key = _clean_text(label_el.get_text())
                val = _clean_text(value_el.get_text())
                if key and val:
                    specs[key] = val

    # ── Pattern C: <dl> definition list ──────────────────────────────────
    if not specs:
        dl_containers = soup.select(
            "dl.product-specs, dl.article-specs, dl.specifications, "
            "[data-testid='spec-list'] dl, .product-details dl, .article-attributes dl"
        )
        if not dl_containers:
            # Only use bare <dl> if we still have nothing
            dl_containers = soup.find_all("dl")

        for dl in dl_containers:
            dt_list = dl.find_all("dt")
            dd_list = dl.find_all("dd")
            for dt, dd in zip(dt_list, dd_list):
                key = _clean_text(dt.get_text())
                val = _clean_text(dd.get_text())
                if key and val:
                    specs[key] = val

    # ── Pattern D: <table> fallback ───────────────────────────────────────
    # Only use tables from clearly-named product-spec containers,
    # NOT bare tables (to avoid picking up store-hours tables etc.)
    if not specs:
        spec_tables = soup.select(
            "table.product-data-table, table.specs-table, table.technical-data, "
            "[data-testid='spec-table'] table, [data-testid='technical-data'] table, "
            ".technical-specifications table, .article-attributes table"
        )
        for table in spec_tables:
            for row in table.find_all("tr"):
                cells = row.find_all(["td", "th"])
                if len(cells) >= 2:
                    key = _clean_text(cells[0].get_text())
                    val = _clean_text(cells[1].get_text())
                    if key and val:
                        specs[key] = val

    return specs


def _clean_text(text: str) -> str:
    """Strip whitespace, line breaks, and multiple spaces from text."""
    if not text:
        return ""
    import re as _re
    text = text.replace("\n", " ").replace("\r", " ").replace("\t", " ")
    text = _re.sub(r"\s{2,}", " ", text)
    return text.strip()


# ─────────────────────────────────────────────────────────────────────────────
# Main scraper class
# ─────────────────────────────────────────────────────────────────────────────

class HornbachScraper:
    """
    High-level scraper that orchestrates browser, navigation, parsing,
    pagination, and optional detail-page crawling.

    Usage:
        scraper = HornbachScraper()
        products = scraper.scrape_category("/c/badarmaturen/S1429/")
    """

    def __init__(self, fetch_details: bool = False, use_proxy: bool = True):
        """
        Args:
            fetch_details: If True, visit each product's detail page for
                           richer descriptions. Slower but more complete.
            use_proxy:     Whether to use a proxy (from config/pool).
        """
        self.fetch_details = fetch_details
        self.use_proxy = use_proxy
        self.products: List[Product] = []  # Store partial results
        self._bm: Optional[BrowserManager] = None
        self._page: Optional[Page] = None

    # ── Context manager ──────────────────────────────────────────────────

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.stop()

    def start(self):
        """Initialise the browser and create a page."""
        self._bm = BrowserManager(use_proxy=self.use_proxy)
        self._bm.start()
        self._page = self._bm.new_page()
        logger.info("Scraper started.")

    def stop(self):
        """Shut down the browser."""
        if self._bm:
            self._bm.stop()
        logger.info("Scraper stopped.")

    # ── Public API ───────────────────────────────────────────────────────

    def scrape_category(
        self,
        category_path: str,
        category_name: str = "",
        max_pages: int = 0,
    ) -> List[Product]:
        """
        Scrape all products from a category listing page, handling pagination.

        Args:
            category_path: Path like "/c/badarmaturen/S1429/"
            category_name: Human-readable category name for tagging.
            max_pages:     Max pages to scrape (0 = use config value).

        Returns:
            List of Product objects.
        """
        max_pages = max_pages or cfg.max_pages
        all_products: List[Product] = []
        page_num = 1
        base = cfg.base_url.rstrip("/")

        while True:
            # Build URL with pagination
            url = f"{base}{category_path}"
            if page_num > 1:
                separator = "&" if "?" in url else "?"
                url = f"{url}{separator}page={page_num}"

            logger.info("━━━ Page %d: %s", page_num, url)

            # Navigate with retry
            html = self._navigate_and_get_html(url)
            if not html:
                logger.error("Failed to load page %d. Stopping.", page_num)
                break

            # Handle cookie consent and market selection on first page
            if page_num == 1:
                handle_cookie_consent(self._page)
                handle_market_selection(self._page)
                # Re-grab HTML after modals might have reloaded or changed the DOM
                html = self._page.content()

            # Parse products from listing
            products = _extract_products_from_html(html, base, category_name)

            if not products:
                logger.info("No products found on page %d – end of listing.", page_num)
                break

            # Optionally enrich each product with detail page data
            if self.fetch_details:
                products = self._enrich_products(products)

            all_products.extend(products)
            logger.info(
                "Page %d: %d products (total so far: %d)",
                page_num, len(products), len(all_products),
            )

            # Check if we've reached the max page limit
            if max_pages and page_num >= max_pages:
                logger.info("Reached max pages limit (%d). Stopping.", max_pages)
                break

            # Check for "next page" indicator before continuing
            if not self._has_next_page():
                logger.info("No next page found. Scraping complete.")
                break

            page_num += 1
            _human_delay()

        logger.info(
            "Category '%s' complete: %d products scraped.",
            category_name or category_path, len(all_products),
        )
        return all_products

    def scrape_search(
        self,
        query: str,
        max_pages: int = 0,
    ) -> List[Product]:
        """
        Scrape products from a search results page.

        Args:
            query:     Search term (e.g. "bohrmaschine").
            max_pages: Max pages to scrape.
        """
        search_path = f"/s/{query}/"
        return self.scrape_category(
            category_path=search_path,
            category_name=f"Search: {query}",
            max_pages=max_pages,
        )

    def scrape_multiple_categories(
        self,
        categories: List[dict] = None,
        max_pages: int = 0,
        discover: bool = False,
    ) -> List[Product]:
        """
        Scrape multiple categories sequentially.

        Args:
            categories: List of dicts with keys "path" and "name". 
            max_pages:  Max pages per category.
            discover:   If True, automatically find categories on site first.
        """
        if discover:
            categories = self.discover_categories()

        if not categories:
            logger.warning("No categories to scrape.")
            return []

        # Reset internal product store for this run
        self.products = []

        for i, cat in enumerate(categories, 1):
            logger.info(
                "═══ Category %d/%d: %s ═══",
                i, len(categories), cat.get("name", cat["path"]),
            )
            batch = self.scrape_category(
                category_path=cat["path"],
                category_name=cat.get("name", ""),
                max_pages=max_pages,
            )
            self.products.extend(batch)

            # Delay between categories
            if i < len(categories):
                _human_delay()

        return self.products
    def discover_categories(self) -> List[dict]:
        """
        Navigate to the Sortiment page and discover all category paths.
        
        Returns:
            List[dict]: Each dict has 'path' and 'name'.
        """
        sortiment_url = f"{cfg.base_url}/c/"
        logger.info("🔍 Discovering categories from: %s", sortiment_url)
        
        try:
            self._navigate_and_get_html(sortiment_url)
            
            # Allow some time for JS to render the category list
            # The selector a[href^="/c/"] is for all category links
            self._page.wait_for_selector('a[href^="/c/"]', timeout=10000)
            
            # Extract all category links
            links = self._page.query_selector_all('a[href^="/c/"]')
            
            discovered = []
            seen_paths = set()
            
            for link in links:
                href = link.get_attribute("href")
                title = link.get_attribute("title") or link.inner_text().strip()
                
                # Sanitize path: must start with /c/ and not be just /c/
                if not href or href == "/c/" or href == "/c":
                    continue
                
                # Deduplicate
                if href in seen_paths:
                    continue
                
                # Filter out some non-category links if any
                if any(x in href.lower() for x in ["impressum", "datenschutz", "kontakt"]):
                    continue
                
                seen_paths.add(href)
                discovered.append({
                    "path": href,
                    "name": title if title else href.split("/")[-2].replace("-", " ").title()
                })
                
            logger.info("✨ Discovered %d categories.", len(discovered))
            return discovered
            
        except Exception as exc:
            logger.error("Failed to discover categories: %s", exc)
            return []

    # ── Private helpers ──────────────────────────────────────────────────

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=4, max=30),
        retry=retry_if_exception_type((PlaywrightTimeout, Exception)),
        reraise=True,
    )
    def _navigate_and_get_html(self, url: str) -> Optional[str]:
        """
        Navigate to a URL with retries and return the page HTML.

        Uses tenacity for exponential backoff on failures.
        """
        try:
            response = self._page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=30000,
            )

            if response and response.status >= 400:
                logger.warning(
                    "HTTP %d for %s — may need to rotate proxy.",
                    response.status, url,
                )
                if response.status in (403, 406, 429):
                    # Try rotating proxy
                    if self._bm:
                        self._bm.rotate_proxy()
                        self._page = self._bm.new_page()
                    raise Exception(f"HTTP {response.status} – rotating proxy")

            # Scroll down to trigger lazy loading
            self._page.evaluate("window.scrollBy(0, 1000)")
            time.sleep(1)

            # Wait for any typical product card or title
            try:
                self._page.wait_for_selector(
                    '[data-testid="article-card"], [data-testid="article-title"], [data-tn="product-card"]',
                    timeout=10000,
                )
            except PlaywrightTimeout:
                logger.debug("Typical product selectors not found – site may use different structure or page is empty.")

            return self._page.content()

        except PlaywrightTimeout as exc:
            logger.warning("Timeout navigating to %s: %s", url, exc)
            raise
        except Exception as exc:
            logger.warning("Error navigating to %s: %s", url, exc)
            raise

    def _has_next_page(self) -> bool:
        """Check whether the current page has a 'next' pagination link."""
        try:
            next_btn = self._page.locator(
                'a[data-tn="pagination-next"], '
                'a[rel="next"], '
                'button[aria-label="Nächste Seite"], '
                '.pagination__next:not(.pagination__next--disabled)'
            ).first
            return next_btn.is_visible(timeout=3000)
        except Exception:
            return False

    def _enrich_products(self, products: List[Product]) -> List[Product]:
        """
        Visit each product's detail page to fill in description,
        availability, and other fields not available on the listing.
        """
        enriched: List[Product] = []
        for product in products:
            if not product.product_url:
                enriched.append(product)
                continue

            try:
                logger.debug("  ↳ Fetching detail: %s", product.product_url)
                _human_delay()

                html = self._navigate_and_get_html(product.product_url)
                if html:
                    detail = _extract_product_detail(html, cfg.base_url)
                    if detail.get("description"):
                        product.description = detail["description"]
                    if detail.get("availability"):
                        product.availability = detail["availability"]
                    if detail.get("article_number"):
                        product.article_number = detail["article_number"]
            except Exception as exc:
                logger.warning(
                    "Could not enrich product '%s': %s", product.name, exc
                )

            enriched.append(product)

        return enriched
