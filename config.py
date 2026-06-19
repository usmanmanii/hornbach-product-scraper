"""
config.py – Centralised configuration loaded from .env file.

All scraper settings are managed here so that the rest of the codebase
can simply `from config import cfg`.
"""

import os
import logging
from dataclasses import dataclass, field
from typing import List, Optional

from dotenv import load_dotenv

# Load environment variables from .env file in the project root
load_dotenv()


@dataclass
class ScraperConfig:
    """Immutable configuration container for the Hornbach scraper."""

    # ── Proxy ────────────────────────────────────────────────────────────
    proxy_url: Optional[str] = os.getenv("PROXY_URL")
    proxy_pool: List[str] = field(default_factory=lambda: [
        p.strip()
        for p in os.getenv("PROXY_POOL", "").split(",")
        if p.strip()
    ])

    # ── Google Sheets ────────────────────────────────────────────────────
    google_service_account_file: str = os.getenv(
        "GOOGLE_SERVICE_ACCOUNT_FILE", "credentials/service_account.json"
    )
    google_sheet_id: str = os.getenv("GOOGLE_SHEET_ID", "")
    google_worksheet_name: str = os.getenv("GOOGLE_WORKSHEET_NAME", "Products")

    # ── Hornbach ─────────────────────────────────────────────────────────
    base_url: str = os.getenv("HORNBACH_BASE_URL", "https://www.hornbach.de")

    # ── Rate limiting / stealth ──────────────────────────────────────────
    min_delay: float = float(os.getenv("MIN_DELAY", "2.0"))
    max_delay: float = float(os.getenv("MAX_DELAY", "5.0"))
    max_pages: int = int(os.getenv("MAX_PAGES", "0"))
    max_retries: int = int(os.getenv("MAX_RETRIES", "3"))
    headless: bool = os.getenv("HEADLESS", "true").lower() == "true"

    # ── Logging ──────────────────────────────────────────────────────────
    log_level: str = os.getenv("LOG_LEVEL", "INFO")

    def get_all_proxies(self) -> List[str]:
        """Return a flat list of all available proxies (single + pool)."""
        proxies = list(self.proxy_pool)
        if self.proxy_url and self.proxy_url not in proxies:
            proxies.insert(0, self.proxy_url)
        return proxies


# ── Singleton config instance ────────────────────────────────────────────
cfg = ScraperConfig()


def setup_logging() -> logging.Logger:
    """Configure and return the root logger for the scraper."""
    log_format = (
        "%(asctime)s │ %(levelname)-8s │ %(name)-22s │ %(message)s"
    )
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format=log_format,
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logger = logging.getLogger("hornbach_scraper")
    return logger
