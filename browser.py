"""
browser.py – Playwright browser manager with proxy & stealth support.

Manages the browser lifecycle, proxy rotation, cookie consent handling,
and provides page-level helpers used by the scraper.
"""

import random
import logging
from typing import Optional

from playwright.sync_api import sync_playwright, Browser, BrowserContext, Page
from fake_useragent import UserAgent

from config import cfg

logger = logging.getLogger("hornbach_scraper.browser")

# ── User-Agent rotation ─────────────────────────────────────────────────
_ua = UserAgent(browsers=["chrome", "firefox", "edge"])


def _random_user_agent() -> str:
    """Return a random realistic desktop User-Agent string."""
    return _ua.random


def _build_proxy_dict(proxy_url: str) -> dict:
    """
    Convert a proxy URL string into the dict format Playwright expects.
    
    Warning: Chromium (default) does NOT support SOCKS5 authentication 
    at the browser level. If auth is required, use an HTTP proxy instead.
    """
    from urllib.parse import urlparse

    parsed = urlparse(proxy_url)
    scheme = parsed.scheme.lower()
    
    proxy = {"server": f"{scheme}://{parsed.hostname}:{parsed.port}"}
    
    if parsed.username:
        if "socks" in scheme:
            logger.error(
                "❌ Chromium does not support SOCKS5 with authentication! "
                "The browser will likely fail to launch. "
                "Please use an HTTP proxy for authenticated connections or a "
                "SOCKS5 proxy without a username/password."
            )
        proxy["username"] = parsed.username
    if parsed.password:
        proxy["password"] = parsed.password

    logger.info("Using proxy: %s://%s:%s", scheme, parsed.hostname, parsed.port)
    return proxy


class BrowserManager:
    """
    Encapsulates Playwright browser setup with proxy and stealth options.

    Usage:
        with BrowserManager() as bm:
            page = bm.new_page()
            page.goto("https://www.hornbach.de")
    """

    def __init__(self, proxy_url: Optional[str] = None, use_proxy: bool = True):
        self._pw = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        
        if not use_proxy:
            self._proxy_url = None
            logger.info("Proxy usage disabled explicitly.")
        else:
            self._proxy_url = proxy_url or self._pick_proxy()

    # ── Proxy selection ──────────────────────────────────────────────────

    @staticmethod
    def _pick_proxy() -> Optional[str]:
        """Pick a random proxy from the configured pool."""
        proxies = cfg.get_all_proxies()
        if proxies:
            chosen = random.choice(proxies)
            logger.debug("Selected proxy from pool: %s", chosen)
            return chosen
        logger.warning("No proxy configured – requests will use your local IP.")
        return None

    # ── Context manager ──────────────────────────────────────────────────

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()

    # ── Lifecycle ────────────────────────────────────────────────────────

    def start(self):
        """Launch the browser with proxy and stealth settings."""
        self._pw = sync_playwright().start()

        launch_kwargs = {
            "headless": cfg.headless,
        }

        # Detect if we need Firefox (Chromium fails on SOCKS5 auth)
        engine = "chromium"
        if self._proxy_url:
            parsed = _build_proxy_dict(self._proxy_url)
            launch_kwargs["proxy"] = parsed
            
            # If it's socks+auth, swap to firefox
            from urllib.parse import urlparse
            pu = urlparse(self._proxy_url)
            if "socks" in pu.scheme.lower() and pu.username:
                engine = "firefox"
                logger.info("📡 SOCKS5 Auth detected – using Firefox engine for compatibility.")

        if engine == "firefox":
            self._browser = self._pw.firefox.launch(**launch_kwargs)
        else:
            self._browser = self._pw.chromium.launch(**launch_kwargs)

        # Create context with stealth-enhancing options
        user_agent = _random_user_agent()
        self._context = self._browser.new_context(
            user_agent=user_agent,
            viewport={"width": 1920, "height": 1080},
            locale="de-DE",
            timezone_id="Europe/Berlin",
            # Extra HTTP headers to look more like a real browser
            extra_http_headers={
                "Accept-Language": "de-DE,de;q=0.9,en;q=0.5",
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;"
                    "q=0.9,image/avif,image/webp,*/*;q=0.8"
                ),
                "DNT": "1",
                "Upgrade-Insecure-Requests": "1",
            },
        )

        logger.info(
            "Browser started (headless=%s, user_agent=%s…)",
            cfg.headless,
            user_agent[:50],
        )

    def stop(self):
        """Gracefully close the browser and Playwright."""
        if self._context:
            self._context.close()
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()
        logger.info("Browser stopped.")

    # ── Page helpers ─────────────────────────────────────────────────────

    def new_page(self) -> Page:
        """Create a new browser page within the current context."""
        if not self._context:
            raise RuntimeError("BrowserManager has not been started.")
        page = self._context.new_page()
        return page

    def rotate_proxy(self):
        """
        Rotate to a different proxy by restarting the browser context.
        Useful if the current IP gets blocked.
        """
        old = self._proxy_url
        proxies = cfg.get_all_proxies()
        if len(proxies) <= 1:
            logger.warning("Only one proxy available – cannot rotate.")
            return

        # Pick a *different* proxy
        candidates = [p for p in proxies if p != old]
        self._proxy_url = random.choice(candidates)
        logger.info("Rotating proxy: %s → %s", old, self._proxy_url)

        # Restart context with new proxy
        self.stop()
        self.start()


# ── Cookie consent helper ────────────────────────────────────────────────

def handle_cookie_consent(page: Page, timeout: int = 5000):
    """
    Dismiss the cookie consent banner if it appears.

    Hornbach shows a privacy modal with the button text "ALLE AKZEPTIEREN"
    (uppercase) on first visit.
    """
    try:
        # The real site uses uppercase "ALLE AKZEPTIEREN"
        accept_btn = page.locator(
            "button:has-text('ALLE AKZEPTIEREN'), "
            "button:has-text('Alle akzeptieren'), "
            "button:has-text('Akzeptieren'), "
            "button:has-text('Accept All'), "
            "[data-tn='cookie-banner-accept-all']"
        ).first

        accept_btn.wait_for(state="visible", timeout=timeout)
        accept_btn.click()
        logger.info("Cookie consent accepted.")
        page.wait_for_timeout(1000)
    except Exception:
        logger.debug("No cookie consent banner found (or already dismissed).")


def handle_market_selection(page: Page, timeout: int = 3000):
    """
    Handle the market/store selection dialog that appears on first visit.

    Hornbach asks "Ist [City] Dein richtiger Markt?" – we confirm the
    default or close the dialog so it doesn't block scraping.
    """
    try:
        # Try to confirm the suggested market
        confirm_btn = page.locator(
            "button:has-text('Ja'), "
            "button:has-text('Bestätigen'), "
            "button:has-text('Übernehmen'), "
            "[data-tn='market-confirm']"
        ).first

        confirm_btn.wait_for(state="visible", timeout=timeout)
        confirm_btn.click()
        logger.info("Market selection confirmed.")
        page.wait_for_timeout(500)
    except Exception:
        # If no confirm button, try to close the dialog
        try:
            close_btn = page.locator(
                "button[aria-label='Schließen'], "
                "button[aria-label='Close'], "
                ".modal-close, "
                "[data-tn='modal-close']"
            ).first
            close_btn.wait_for(state="visible", timeout=1000)
            close_btn.click()
            logger.info("Market selection dialog closed.")
        except Exception:
            logger.debug("No market selection dialog found.")
