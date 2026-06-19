#!/usr/bin/env python3
"""
main.py – Hornbach.de Web Scraper Entry Point

This is the main script that ties together all modules:
  • config  – reads settings from .env
  • browser – manages Playwright with proxy & stealth
  • scraper – navigates pages and parses products
  • sheets  – writes results to Google Sheets

Usage:
    # Scrape a single category
    python main.py --category "/c/badarmaturen/S1429/" --name "Bathroom Faucets"

    # Scrape by search query
    python main.py --search "bohrmaschine"

    # Scrape multiple categories defined in the script
    python main.py --all

    # Scrape without uploading to Google Sheets (CSV only)
    python main.py --search "akkuschrauber" --no-sheets

    # Scrape with detail page enrichment (slower, more data)
    python main.py --search "werkzeug" --details

    # Limit pages
    python main.py --category "/c/elektrowerkzeuge/S1196/" --pages 3
"""

import argparse
import csv
import json
import os
import sys
import logging
from datetime import datetime
from typing import List

from config import cfg, setup_logging
from models import Product
from scraper import HornbachScraper
from sheets import GoogleSheetsClient

logger = logging.getLogger("hornbach_scraper.main")

# ─────────────────────────────────────────────────────────────────────────────
# Predefined categories to scrape (used with --all flag)
# ─────────────────────────────────────────────────────────────────────────────

CATEGORIES = [
    {"path": "/c/elektrowerkzeuge/S1196/", "name": "Power Tools"},
    {"path": "/c/handwerkzeuge/S1195/", "name": "Hand Tools"},
    {"path": "/c/badarmaturen/S1429/", "name": "Bathroom Faucets"},
    {"path": "/c/farben/S882/", "name": "Paints"},
    {"path": "/c/bodenbelaege/S1168/", "name": "Flooring"},
    {"path": "/c/leuchten-lampen/S1332/", "name": "Lighting"},
    {"path": "/c/garten/S200/", "name": "Garden & Outdoor"},
    {"path": "/c/baustoffe/S863/", "name": "Building Materials"},
    {"path": "/c/kuechen/S1059/", "name": "Kitchens"},
]


# ─────────────────────────────────────────────────────────────────────────────
# CSV export
# ─────────────────────────────────────────────────────────────────────────────

def save_to_csv(products: List[Product], filename: str = ""):
    """Save products to a CSV file as a local backup."""
    if not filename:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"output/hornbach_products_{timestamp}.csv"

    # Ensure output directory exists
    os.makedirs(os.path.dirname(filename) or "output", exist_ok=True)

    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(Product.header_row())
        for p in products:
            writer.writerow(p.to_row())

    logger.info("💾 Saved %d products to %s", len(products), filename)
    return filename


def save_to_json(products: List[Product], filename: str = ""):
    """Save products to a JSON file."""
    if not filename:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"output/hornbach_products_{timestamp}.json"

    os.makedirs(os.path.dirname(filename) or "output", exist_ok=True)

    data = [p.to_dict() for p in products]
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    logger.info("💾 Saved %d products to %s", len(products), filename)
    return filename


# ─────────────────────────────────────────────────────────────────────────────
# Google Sheets upload
# ─────────────────────────────────────────────────────────────────────────────
def save_to_excel(products: List[Product], filename: str = ""):
    """Save products to an Excel (.xlsx) file using pandas."""
    try:
        import pandas as pd
    except ImportError:
        logger.error("pandas not found. Install it with 'pip install pandas openpyxl'")
        return None

    if not filename:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"output/hornbach_products_{timestamp}.xlsx"

    os.makedirs(os.path.dirname(filename) or "output", exist_ok=True)

    # Convert to DataFrame
    df = pd.DataFrame([p.to_dict() for p in products])
    
    # Reorder columns to match header_row
    headers = Product.header_row()
    mapping = {
        "name": headers[0],
        "price": headers[1],
        "currency": headers[2],
        "product_url": headers[3],
        "description": headers[4],
        "image_url": headers[5],
        "availability": headers[6],
        "article_number": headers[7],
        "category": headers[8],
    }
    df = df.rename(columns=mapping)

    # Ensure column order matches Product.header_row()
    df = df[headers]

    df.to_excel(filename, index=False)
    logger.info("💾 Saved %d products to %s", len(products), filename)
    return filename


# ─────────────────────────────────────────────────────────────────────────────
# Google Sheets upload
# ─────────────────────────────────────────────────────────────────────────────

def upload_to_sheets(products: List[Product], clear_existing: bool = False):
    """Upload products to Google Sheets using the configured service account."""
    if not cfg.google_sheet_id:
        logger.error(
            "GOOGLE_SHEET_ID not set in .env – skipping Sheets upload. "
            "See README.md for setup instructions."
        )
        return

    if not os.path.exists(cfg.google_service_account_file):
        logger.error(
            "Service account file not found at '%s'. "
            "See README.md for Google Sheets API setup.",
            cfg.google_service_account_file,
        )
        return

    try:
        client = GoogleSheetsClient()
        client.connect()
        client.write_products(products, clear_existing=clear_existing)
        logger.info(
            "✅ Successfully uploaded %d products to Google Sheets.", len(products)
        )
    except Exception as exc:
        logger.error("Failed to upload to Google Sheets: %s", exc)
        logger.info("Data was saved to CSV as a backup.")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(
        description="🦏 Hornbach.de Product Scraper",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py --category "/c/badarmaturen/S1429/" --name "Bathroom Faucets"
  python main.py --search "bohrmaschine" --pages 5
  python main.py --all --details --no-sheets
        """,
    )

    # Scraping mode (mutually exclusive)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--category", "-c",
        help="Category URL path to scrape (e.g. /c/badarmaturen/S1429/)",
    )
    mode.add_argument("--search", "-s", help="Search query to scrape")
    mode.add_argument(
        "--all", "-a",
        action="store_true",
        help="Scrape all predefined categories",
    )
    mode.add_argument(
        "--discover",
        action="store_true",
        help="Automatically discover all categories from the site",
    )

    # Options
    parser.add_argument(
        "--name", "-n",
        default="",
        help="Category name label (used with --category)",
    )
    parser.add_argument(
        "--pages", "-p",
        type=int,
        default=0,
        help="Max pages to scrape per category (0 = unlimited)",
    )
    parser.add_argument(
        "--details", "-d",
        action="store_true",
        help="Fetch product detail pages for richer data (slower)",
    )
    parser.add_argument(
        "--no-sheets",
        action="store_true",
        help="Skip Google Sheets upload (CSV/JSON only)",
    )
    parser.add_argument(
        "--clear",
        action="store_true",
        help="Clear existing sheet data before writing",
    )
    parser.add_argument(
        "--output-format",
        choices=["csv", "json", "excel", "all"],
        default="all",
        help="Local output format (default: all)",
    )
    parser.add_argument(
        "--no-proxy",
        action="store_true",
        help="Disable proxy usage (even if configured in .env)",
    )

    return parser.parse_args()


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    """Main entry point for the Hornbach scraper."""
    setup_logging()
    args = parse_args()

    logger.info("=" * 60)
    logger.info("🦏 Hornbach.de Product Scraper")
    logger.info("=" * 60)

    # Validate proxy configuration
    proxies = cfg.get_all_proxies()
    if proxies:
        logger.info("📡 Proxy configured: %d proxy(ies) available", len(proxies))
    else:
        logger.warning(
            "⚠️  No proxy configured! hornbach.de requires a German IP. "
            "Set PROXY_URL in your .env file."
        )

    products: List[Product] = []

    # ── Scrape ───────────────────────────────────────────────────────────
    try:
        with HornbachScraper(fetch_details=args.details, use_proxy=not args.no_proxy) as scraper:
            if args.category:
                products = scraper.scrape_category(
                    category_path=args.category,
                    category_name=args.name,
                    max_pages=args.pages,
                )
            elif args.search:
                products = scraper.scrape_search(
                    query=args.search,
                    max_pages=args.pages,
                )
            else:
                # Use discover or predefined categories
                products = scraper.scrape_multiple_categories(
                    categories=CATEGORIES if args.all else None,
                    max_pages=args.pages,
                    discover=args.discover,
                )
    except KeyboardInterrupt:
        logger.info("\n⛔ Scraping interrupted by user. Saving partial results…")
        # Attempt to recover results from the scraper instance if possible
        if 'scraper' in locals():
            products = scraper.products
    except Exception as exc:
        logger.error("Scraping failed: %s", exc, exc_info=True)

    # ── Save results ─────────────────────────────────────────────────────
    if not products:
        logger.warning("No products were scraped. Check your proxy and URLs.")
        sys.exit(1)

    logger.info("━" * 60)
    logger.info("📊 Scraped %d products total.", len(products))

    # Save locally
    if args.output_format in ("csv", "all"):
        save_to_csv(products)
    if args.output_format in ("json", "all"):
        save_to_json(products)
    if args.output_format in ("excel", "all"):
        save_to_excel(products)

    # Upload to Google Sheets
    if not args.no_sheets:
        upload_to_sheets(products, clear_existing=args.clear)
    else:
        logger.info("Skipping Google Sheets upload (--no-sheets flag).")

    logger.info("🏁 Done!")


if __name__ == "__main__":
    main()
