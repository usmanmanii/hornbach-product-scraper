#!/usr/bin/env python3
"""
local_spec_scraper.py – Hornbach Local Mirror Specification Scraper

This script extracts product information from a local mirror of www.hornbach.de.
It follows the structure and logic of the original spec_scraper.py but
works entirely offline using the filesystem.
"""

import csv
import logging
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from bs4 import BeautifulSoup

# Add current directory to path so we can import from the project
sys.path.append(str(Path(__file__).parent))

from models import Product
from scraper import _extract_spec_tables, _clean_text

# ─────────────────────────────────────────────────────────────────────────────
# Configuration & Paths
# ─────────────────────────────────────────────────────────────────────────────

PROJECT_BASE = Path(__file__).parent
MIRROR_BASE = PROJECT_BASE / "www.hornbach.de"
CSV_INFO_PATH = PROJECT_BASE / "product-info.csv"
OUTPUT_DIR = PROJECT_BASE / "output_local" / "specs"

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(PROJECT_BASE / "local_scraper.log", encoding='utf-8')
    ]
)
logger = logging.getLogger("local_scraper")

# ─────────────────────────────────────────────────────────────────────────────
# Utilities
# ─────────────────────────────────────────────────────────────────────────────

def slugify(name: str) -> str:
    """Convert a category name to a safe ASCII filename slug."""
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
    """Parse product-info.csv and return list of categories."""
    categories = []
    if not csv_path.exists():
        logger.error(f"Category info file not found: {csv_path}")
        return []
        
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
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
            url = row[1].strip()
            if not name or not url or not url.startswith("http"):
                continue
            
            # Extract relative path from URL - try to get the root category path
            # Example: /c/bad-sanitaer/S474/ -> /c/bad-sanitaer/
            root_path_match = re.search(r'(.*/c/[^/]+)', url)
            if root_path_match:
                path = root_path_match.group(1).replace("https://www.hornbach.de", "").rstrip("/") + "/"
            else:
                path = url.replace("https://www.hornbach.de", "").rstrip("/") + "/"
                
            categories.append({
                "name": name,
                "url": url,
                "path": path
            })
    return categories

# ─────────────────────────────────────────────────────────────────────────────
# CSV Writer
# ─────────────────────────────────────────────────────────────────────────────

class CategoryCSVWriter:
    """Writes products to a per-category CSV file incrementally."""
    
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
        self.spec_keys: List[str] = []
        self._seen_keys: set = set()

    def add(self, product: Product):
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
        for key in product.specs:
            if key not in self._seen_keys:
                self.spec_keys.append(key)
                self._seen_keys.add(key)
        row.update(product.specs)
        self.rows.append(row)

    def save(self):
        """Write all buffered rows to disk."""
        if not self.rows:
            return
            
        all_columns = self.BASE_COLUMNS + self.spec_keys
        try:
            with open(self.csv_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=all_columns, extrasaction="ignore")
                writer.writeheader()
                for row in self.rows:
                    writer.writerow(row)
            logger.info(f"Saved {len(self.rows)} products to {self.csv_path}")
        except Exception as e:
            logger.error(f"Failed to save CSV {self.csv_path}: {e}")

# ─────────────────────────────────────────────────────────────────────────────
# Local Scraper Engine
# ─────────────────────────────────────────────────────────────────────────────

class LocalScraper:
    def __init__(self):
        self._processed_products = set()

    def find_local_file(self, path: str) -> Optional[Path]:
        """Find a local html file for a given relative web path."""
        # Convert full URL to relative path if needed
        if path.startswith("http"):
            path = path.replace("https://www.hornbach.de", "").replace("http://www.hornbach.de", "")
            
        # Ensure path starts with / and is relative to mirror base
        rel_path = path.lstrip("/").rstrip("/")
        if not rel_path:
            return None
            
        base_dir = MIRROR_BASE / rel_path
        
        # Priority 1: direct index.html.tmp or index.html in the directory
        options = [
            base_dir / "index.html.tmp",
            base_dir / "index.html",
        ]
        
        # Priority 2: In case the folder is named with .html.tmp (some mirror tools do this)
        options.append(Path(str(base_dir) + ".html.tmp"))
        options.append(Path(str(base_dir) + ".html"))
        
        # Priority 3: If rel_path itself ends in a file-like segment
        if rel_path.endswith(".html") or rel_path.endswith(".html.tmp"):
            options.insert(0, MIRROR_BASE / rel_path)
            
        for opt in options:
            if opt.is_file():
                return opt
                
        # Wildcard search in the directory for any .html.tmp file
        if base_dir.is_dir():
            for f in base_dir.glob("*.html.tmp"):
                return f
            for f in base_dir.glob("index*.html*"):
                return f
                
        return None

    def get_listing_products(self, category_dir: Path) -> List[Tuple[str, str]]:
        """Extract product links from a local listing file."""
        products = []
        # Find all index files in the category dir and its subdirectories recursively
        for html_file in category_dir.rglob("index*.html*"):
            try:
                with open(html_file, 'r', encoding='utf-8', errors='ignore') as f:
                    soup = BeautifulSoup(f.read(), 'lxml')
                    
                # Using selectors from scraper.py
                links = soup.select('a[data-testid="article-title"]')
                if not links:
                    links = soup.select('a[href*="/p/"]')
                    
                for a in links:
                    href = a.get("href", "")
                    # Match /p/ or /conf/ links
                    m = re.search(r'/(p|conf)/', href)
                    if m:
                        prefix = m.group(1)
                        # Clean href: resolve relative paths and remove params
                        clean_href = f"/{prefix}/" + href.split(f"/{prefix}/")[1].split("?")[0]
                        clean_href = clean_href.rstrip("/")
                        name = a.get_text(strip=True) or a.get("title", "")
                        products.append((clean_href, name))
            except Exception as e:
                logger.error(f"Error reading listing file {html_file}: {e}")
                
        return list(set(products)) # Deduplicate

    def scrape_product(self, product_path: str, name: str, category_name: str) -> Optional[Product]:
        """Parse a local product detail page."""
        local_file = self.find_local_file(product_path)
        
        # If not found by direct path, try searching by article number
        if not local_file:
            art_match = re.search(r'/(\d{5,10})/?$', product_path)
            if art_match:
                article_id = art_match.group(1)
                p_dirs = [MIRROR_BASE / "p", MIRROR_BASE / "conf"]
                for p_dir in p_dirs:
                    if p_dir.is_dir():
                        # Look for any folder named exactly article_id within p/ or conf/
                        for match in p_dir.glob(f"*/{article_id}"):
                            if match.is_dir():
                                local_file = self.find_local_file(str(match.relative_to(MIRROR_BASE)))
                                if local_file:
                                    break
                    if local_file:
                        break

        if not local_file:
            logger.warning(f"  ✗ Local file not found for: {product_path}")
            return None

        try:
            with open(local_file, 'r', encoding='utf-8', errors='ignore') as f:
                html = f.read()
            
            soup = BeautifulSoup(html, 'lxml')
            
            # Extract specs using existing project logic
            specs = _extract_spec_tables(html)
            
            # Extract other fields
            price = ""
            price_el = soup.select_one('[data-testid="price"], .price-price__price')
            if price_el:
                raw = price_el.get_text(separator=" ", strip=True)
                m = re.search(r"[\d.,]+", raw.replace("\xa0", ""))
                if m:
                    price = m.group().replace(",", ".") if "," in m.group() else m.group()

            image_url = ""
            img_el = soup.select_one('[data-testid*="image"] img, .product-image img')
            if img_el:
                image_url = img_el.get("src") or img_el.get("data-src") or ""

            availability = ""
            avail_el = soup.select_one('[data-testid="availability"], .js-stock-availability')
            if avail_el:
                availability = avail_el.get_text(strip=True)

            article_number = ""
            art_el = soup.select_one('[data-testid="article-number"], span.article-number')
            if art_el:
                article_number = art_el.get_text(strip=True).replace("Art. ", "").strip()
            
            if not article_number:
                art_match = re.search(r'/(\d{5,10})/?$', product_path)
                if art_match:
                    article_number = art_match.group(1)

            return Product(
                name=name,
                price=price,
                currency="EUR",
                product_url="https://www.hornbach.de/" + product_path.lstrip("/"),
                image_url=image_url,
                availability=availability,
                article_number=article_number,
                category=category_name,
                specs=specs
            )
        except Exception as e:
            logger.error(f"  ✗ Error parsing product {product_path}: {e}")
            return None

    def run(self):
        """Main execution loop."""
        categories = load_categories(CSV_INFO_PATH)
        if not categories:
            logger.error("No categories loaded. Exiting.")
            return

        logger.info(f"Starting local scrape for {len(categories)} categories.")
        
        for cat in categories:
            name = cat["name"]
            path = cat["path"]
            logger.info(f"═ Processing Category: {name}")
            
            local_cat_dir = MIRROR_BASE / path.lstrip("/")
            if not local_cat_dir.is_dir():
                logger.warning(f"  ! Category directory not found: {local_cat_dir}")
                continue
                
            product_links = self.get_listing_products(local_cat_dir)
            logger.info(f"  ↳ Found {len(product_links)} products in local listing.")
            
            writer = CategoryCSVWriter(OUTPUT_DIR / f"{slugify(name)}.csv")
            
            for p_path, p_name in product_links:
                if p_path in self._processed_products:
                    continue
                
                logger.info(f"    → {p_name[:50]}")
                product = self.scrape_product(p_path, p_name, name)
                if product:
                    writer.add(product)
                    # self._processed_products.add(p_path) # Commented out to allow same product in multiple categories if needed
                    
            writer.save()

if __name__ == "__main__":
    if not MIRROR_BASE.exists():
        print(f"ERROR: Mirror base directory not found at {MIRROR_BASE}")
        sys.exit(1)
        
    scraper = LocalScraper()
    scraper.run()
