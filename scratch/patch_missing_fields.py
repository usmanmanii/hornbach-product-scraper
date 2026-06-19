#!/usr/bin/env python3
"""
patch_missing_fields.py – Patch missing field values in existing CSV records.

What it does:
  1. Scans every CSV in output_local/specs/ for rows where any of the
     patchable base fields is empty: price, name, image_url, availability,
     article_number.
  2. Builds a minimal fetch-list (one URL per row that needs patching).
  3. Fetches those product pages concurrently (20 workers).
  4. For each fetched page, updates ONLY the empty fields – never overwrites
     fields that already have a value, never adds or removes rows.
  5. Writes the patched CSV back in-place atomically (temp file → rename).

Fields patched:
  • price        – was broken (selector was "price" not "prices")
  • availability – was never populated (JS-rendered; often empty even now)
  • name         – fixes the ~0.8 % of rows with blank names
  • image_url    – fixes the handful of rows with missing images
  • article_number – fixes the 5 rows with blank article numbers

Run:
    git clone https://github.com/usmanmanii/scraper.git && cd scraper
    source venv/bin/activate
    python scratch/patch_missing_fields.py
"""

import asyncio
import csv
import json
import logging
import os
import re
import sys
import tempfile
import threading
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Set

import httpx
from bs4 import BeautifulSoup

# ── Project imports ────────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent))

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent.parent
OUTPUT_DIR   = PROJECT_ROOT / "output_local" / "specs"
LOG_FILE     = PROJECT_ROOT / "patch_missing.log"

MAX_CONCURRENT = 10    # reduced from 20 – avoids server-side throttling/connection resets
RETRY_COUNT    = 3
TIMEOUT        = 30.0

# Fields we will patch if empty (never overwrite an existing value)
PATCHABLE = ["price", "name", "image_url", "availability", "article_number"]

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
logger = logging.getLogger("patch_fields")

# ─────────────────────────────────────────────────────────────────────────────
# Step 1 – Audit CSVs, collect rows that need patching
# ─────────────────────────────────────────────────────────────────────────────
def audit_csvs() -> Dict[Path, Dict[str, Dict]]:
    """
    Returns:
        {
          csv_path: {
            product_url: { field_name: True, ... }   # fields that are empty
          }
        }
    """
    needs: Dict[Path, Dict[str, Dict]] = {}
    total_rows = 0
    total_needing = 0

    for csv_path in sorted(OUTPUT_DIR.glob("*.csv")):
        file_needs: Dict[str, Dict] = {}
        try:
            with open(csv_path, "r", encoding="utf-8", errors="ignore") as f:
                for row in csv.DictReader(f):
                    total_rows += 1
                    url = (row.get("product_url") or "").strip()
                    if not url:
                        continue  # can't fetch without a URL
                    missing = {
                        field
                        for field in PATCHABLE
                        if not (row.get(field) or "").strip()
                    }
                    if missing:
                        file_needs[url] = missing
                        total_needing += 1
        except Exception as e:
            logger.warning(f"Could not read {csv_path.name}: {e}")

        if file_needs:
            needs[csv_path] = file_needs

    logger.info(
        f"Audit complete: {total_rows:,} total rows, "
        f"{total_needing:,} need patching across {len(needs)} files."
    )
    # Per-field summary
    field_counts: Dict[str, int] = defaultdict(int)
    for file_needs in needs.values():
        for missing_set in file_needs.values():
            for f in missing_set:
                field_counts[f] += 1
    for field in PATCHABLE:
        cnt = field_counts[field]
        logger.info(f"  {field:<20} missing in {cnt:,} rows")

    return needs

# ─────────────────────────────────────────────────────────────────────────────
# Step 2 – Extract fields from a fetched PDP page
# ─────────────────────────────────────────────────────────────────────────────
def extract_fields(html: str, url: str) -> Dict[str, str]:
    """Returns a dict of field → value for all patchable fields found."""
    result: Dict[str, str] = {}
    try:
        soup = BeautifulSoup(html, "lxml")

        # ── article_number ────────────────────────────────────────────────
        art_el = soup.select_one(
            '[data-testid="article-number"], span.article-number, [data-tn="article-number"]'
        )
        if art_el:
            raw = art_el.get_text(strip=True)
            result["article_number"] = re.sub(r"^Art\.?\s*", "", raw).strip()
        if not result.get("article_number"):
            m = re.search(r"/(\d{5,10})/?(?:\?|$)", url)
            if m:
                result["article_number"] = m.group(1)

        # ── name ──────────────────────────────────────────────────────────
        name_el = soup.select_one(
            'h1[data-testid="article-title"], h1.article-title, h1'
        )
        if name_el:
            result["name"] = name_el.get_text(strip=True)

        # ── price ─────────────────────────────────────────────────────────
        # Hornbach uses data-testid="prices" (plural) on the live site
        price_el = soup.select_one(
            '[data-testid="prices"], '
            '[data-testid="price"], '
            '.price-price__price'
        )
        if price_el:
            raw = price_el.get_text(separator=" ", strip=True).replace("\xa0", "")
            # Require at least one leading digit so we never capture a bare "."
            m = re.search(r"\d[\d.,]*", raw)
            if m:
                val = m.group()
                # Normalise German decimal format: 1.234,56 → 1234.56
                if "," in val and "." in val:
                    val = val.replace(".", "").replace(",", ".")
                elif "," in val:
                    val = val.replace(",", ".")
                # Sanity check: must parse as a positive float
                try:
                    if float(val) > 0:
                        result["price"] = val
                except ValueError:
                    pass

        # ── image_url ─────────────────────────────────────────────────────
        img_el = soup.select_one(
            '[data-testid="main-image"] img, '
            '[data-testid="article-image"] img, '
            '[data-testid*="image"] img, '
            '.hb-product-image img, .product-image img'
        )
        if img_el:
            result["image_url"] = (
                img_el.get("src")
                or img_el.get("data-src")
                or img_el.get("data-lazy-src")
                or ""
            )

        # ── availability ──────────────────────────────────────────────────
        # Availability is JS-rendered; try Apollo state as best-effort
        for script in soup.find_all("script"):
            content = script.string or ""
            if "__ARTICLE_DETAIL_APOLLO_STATE__" not in content:
                continue
            parts = content.split("=", 1)
            if len(parts) < 2:
                continue
            try:
                decoder = json.JSONDecoder()
                state, _ = decoder.raw_decode(parts[1].strip())
                for v in state.values():
                    if not isinstance(v, dict):
                        continue
                    # Look for stock/availability info
                    for key in ("availabilityStatus", "stockStatus", "availability"):
                        if v.get(key):
                            result["availability"] = str(v[key])
                            break
                    if result.get("availability"):
                        break
            except (json.JSONDecodeError, Exception):
                pass

        # DOM fallback for availability
        if not result.get("availability"):
            avail_el = soup.select_one(
                '[data-testid="availability"], '
                '[data-testid="stock"], '
                '.js-stock-availability, '
                '[class*="availability"], '
                '[class*="stock-status"]'
            )
            if avail_el:
                result["availability"] = avail_el.get_text(strip=True)

    except Exception as e:
        logger.error(f"extract_fields error ({url}): {e}")

    return result

# ─────────────────────────────────────────────────────────────────────────────
# Step 3 – Async fetcher
# ─────────────────────────────────────────────────────────────────────────────
class PatchFetcher:
    def __init__(self, url_to_files: Dict[str, List[Path]]):
        """
        url_to_files: { product_url: [csv_path, ...] }
        (one URL may theoretically appear in multiple files – handle it)
        """
        self.url_to_files = url_to_files
        self.semaphore = asyncio.Semaphore(MAX_CONCURRENT)
        self.results: Dict[str, Dict[str, str]] = {}   # url → extracted fields
        self.lock = threading.Lock()
        self.success = 0
        self.failed = 0

        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
        }

    async def fetch_one(self, client: httpx.AsyncClient, url: str):
        async with self.semaphore:
            for attempt in range(RETRY_COUNT):
                try:
                    resp = await client.get(url, timeout=TIMEOUT, follow_redirects=True)
                    if resp.status_code == 200:
                        fields = extract_fields(resp.text, url)
                        with self.lock:
                            self.results[url] = fields
                            self.success += 1
                        logger.info(
                            f"✓ {url.split('/')[-2]}  "
                            f"price={fields.get('price','—')}  "
                            f"avail={fields.get('availability','—')[:30]}"
                        )
                        return
                    elif resp.status_code == 404:
                        logger.warning(f"✗ 404: {url}")
                        self.failed += 1
                        return
                    else:
                        logger.warning(f"! HTTP {resp.status_code}: {url} (attempt {attempt+1})")
                except httpx.TimeoutException:
                    logger.warning(f"! Timeout: {url} (attempt {attempt+1})")
                except Exception as e:
                    # str(e) can be empty for connection-reset errors; include type name
                    err_msg = str(e) or type(e).__name__
                    logger.warning(f"! Error: {url} → {err_msg} (attempt {attempt+1})")

                if attempt < RETRY_COUNT - 1:
                    await asyncio.sleep(2 ** attempt)

            self.failed += 1

    async def run(self):
        urls = list(self.url_to_files.keys())
        logger.info(f"Fetching {len(urls):,} pages with {MAX_CONCURRENT} workers…")
        async with httpx.AsyncClient(headers=self.headers) as client:
            await asyncio.gather(*[self.fetch_one(client, u) for u in urls])
        logger.info(
            f"Fetch done – success: {self.success:,} | failed: {self.failed:,}"
        )

# ─────────────────────────────────────────────────────────────────────────────
# Step 4 – Patch CSVs in-place
# ─────────────────────────────────────────────────────────────────────────────
def patch_csv(
    csv_path: Path,
    file_needs: Dict[str, Set[str]],
    fetched: Dict[str, Dict[str, str]],
) -> int:
    """
    Read csv_path, patch empty fields from fetched data, write back atomically.
    Returns number of rows actually updated.
    """
    rows: List[Dict] = []
    fieldnames: List[str] = []

    try:
        with open(csv_path, "r", encoding="utf-8", errors="ignore", newline="") as f:
            reader = csv.DictReader(f)
            fieldnames = list(reader.fieldnames or [])
            rows = list(reader)
    except Exception as e:
        logger.error(f"Cannot read {csv_path.name}: {e}")
        return 0

    updated = 0
    for row in rows:
        url = (row.get("product_url") or "").strip()
        if url not in file_needs:
            continue
        new_vals = fetched.get(url, {})
        if not new_vals:
            continue

        changed = False
        for field in file_needs[url]:
            # Only fill if still empty AND we have a new value
            if not (row.get(field) or "").strip() and (new_vals.get(field) or "").strip():
                row[field] = new_vals[field]
                changed = True

        if changed:
            updated += 1

    # Write back atomically via temp file
    try:
        tmp_path = csv_path.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        tmp_path.replace(csv_path)
        logger.info(f"  ↳ {csv_path.name}: {updated} rows patched")
    except Exception as e:
        logger.error(f"Cannot write {csv_path.name}: {e}")
        return 0

    return updated

# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
async def main():
    if not OUTPUT_DIR.exists():
        logger.error(f"Output directory not found: {OUTPUT_DIR}")
        return

    # 1. Audit
    needs = audit_csvs()  # {csv_path: {url: set(missing_fields)}}
    if not needs:
        logger.info("All records are complete. Nothing to patch. ✓")
        return

    # 2. Build unique URL → [csv_paths] map (avoid fetching same URL twice)
    url_to_files: Dict[str, List[Path]] = defaultdict(list)
    for csv_path, file_needs in needs.items():
        for url in file_needs:
            url_to_files[url].append(csv_path)

    logger.info(f"Unique URLs to fetch: {len(url_to_files):,}")

    # 3. Fetch
    fetcher = PatchFetcher(url_to_files)
    await fetcher.run()

    if not fetcher.results:
        logger.error("No pages fetched successfully. Aborting patch.")
        return

    # 4. Patch each CSV
    logger.info("Patching CSV files…")
    total_patched = 0
    for csv_path, file_needs in needs.items():
        patched = patch_csv(csv_path, file_needs, fetcher.results)
        total_patched += patched

    logger.info(
        f"\n{'─'*50}\n"
        f"Done. Total rows patched: {total_patched:,}\n"
        f"Log: {LOG_FILE}\n"
        f"{'─'*50}"
    )


if __name__ == "__main__":
    asyncio.run(main())
