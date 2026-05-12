"""
InSinkErator Italy Price Tracker
Scrapes prices from unieuro.it, eprice.it, mediaworld.it, trovaincasso.it,
pentoleprofessionali.it, leroymerlin.it, bricobravo.com, plumbingshop.it,
lineadaincasso.it, opportunitycommerce.com, climaconvenienza.it, and amazon.it,
then saves results to CSV and Google Sheets.
"""

import csv
import json
import logging
import os
import random
import re
import time
from datetime import date
from pathlib import Path
from typing import Optional

import gspread
import requests
from bs4 import BeautifulSoup
from google.oauth2.service_account import Credentials

# ── Configuration ──────────────────────────────────────────────────────────────

CSV_FILE = "prices.csv"
CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "service_account.json")
SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "")
REQUEST_DELAY_MIN = 2
REQUEST_DELAY_MAX = 4
RETRY_MAX = 3
RETRY_DELAY_MIN = 5
RETRY_DELAY_MAX = 10

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
]

# Expected product counts per site — used for the end-of-run warning.
# Sites not listed here are known SPA/blocked and always return 0.
TYPICAL_COUNTS = {
    "trovaincasso.it": 10,
    "pentoleprofessionali.it": 8,
    "bricobravo.com": 8,
    "lineadaincasso.it": 29,
    "opportunitycommerce.com": 18,
    "climaconvenienza.it": 8,
    "amazon.it": 44,
}

BASE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
    # Omit Accept-Encoding — requests/urllib3 advertises gzip+deflate and auto-decompresses.
    # Advertising brotli without the brotli package installed yields garbled responses.
    "Referer": "https://www.google.it/",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

TODAY = date.today().isoformat()


# ── Helpers ────────────────────────────────────────────────────────────────────

def _random_ua() -> str:
    return random.choice(USER_AGENTS)


def _make_session(ua: Optional[str] = None) -> requests.Session:
    session = requests.Session()
    session.headers.update({**BASE_HEADERS, "User-Agent": ua or _random_ua()})
    return session


def get(url: str, session: Optional[requests.Session] = None, **kwargs) -> Optional[requests.Response]:
    """GET with shared headers and basic error handling."""
    try:
        requester = session or requests
        if session is None:
            kwargs.setdefault("headers", {**BASE_HEADERS, "User-Agent": _random_ua()})
        resp = requester.get(url, timeout=15, **kwargs)
        resp.raise_for_status()
        return resp
    except requests.RequestException as exc:
        log.error("Failed to fetch %s: %s", url, exc)
        return None


def parse_price(raw: str) -> Optional[float]:
    """
    Convert Italian-formatted price strings to float.
    Handles formats like "29,99 €", "1.299,99", "1299.99".
    """
    cleaned = raw.strip()
    cleaned = re.sub(r"[€$\s]", "", cleaned)
    if "." in cleaned and "," in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    cleaned = re.sub(r"[^\d.]", "", cleaned)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


_ACCESSORY_WORDS = re.compile(
    r"\b(tappo|guarnizione|accessori[oa]|ricambio|filtro|flangia|coperchio|kit"
    r"|splash|stopper|seal|spare\s*part|ricambi|1975)\b",
    re.IGNORECASE,
)

_OUT_OF_STOCK_RE = re.compile(
    r"esaurit[oa]|non\s+disponibile|fuori\s+produzione|prodotto\s+non\s+disponibile"
    r"|sold\s+out|out\s+of\s+stock|temporaneamente\s+non\s+disponibile"
    r"|non\s+in\s+stock|disponibilit[àa]\s*:\s*0",
    re.IGNORECASE,
)


def is_accessory(name: str) -> bool:
    return bool(_ACCESSORY_WORDS.search(name))


def detect_stock_status(card) -> str:
    """
    Detect stock availability from a BeautifulSoup card element.
    Returns 'out_of_stock', or 'unknown' when no signal is found.
    """
    text = card.get_text(" ", strip=True)
    if _OUT_OF_STOCK_RE.search(text):
        return "out_of_stock"

    # Disabled add-to-cart button is a strong out-of-stock signal
    for btn in card.select("button, a[class*='add'], a[class*='cart'], a[class*='aggiungi']"):
        btn_text = btn.get_text(strip=True).lower()
        if any(kw in btn_text for kw in ("aggiungi", "carrello", "add to cart", "buy")):
            classes = " ".join(btn.get("class", []))
            if btn.get("disabled") is not None or "disabled" in classes or "grayed" in classes:
                return "out_of_stock"

    return "unknown"


def _select_first(soup, *selectors):
    """Try CSS selectors in order, return the first non-empty result list."""
    for sel in selectors:
        found = soup.select(sel)
        if found:
            return found
    return []


def make_row(site: str, name: str, price_raw: str, url: str, stock_status: str = "unknown") -> Optional[dict]:
    """Build a result dict; returns None if the price cannot be parsed or the product is an accessory."""
    if is_accessory(name):
        log.debug("Skipping accessory: %s", name.strip())
        return None
    price = parse_price(price_raw)
    if price is None or price <= 0:
        return None
    return {
        "date": TODAY,
        "site": site,
        "product_name": name.strip(),
        "price_eur": price,
        "url": url,
        "stock_status": stock_status,
    }


def _scrape_with_retry(fn, site_name: str) -> tuple[list[dict], str]:
    """
    Call fn up to RETRY_MAX+1 times, rotating User-Agent on each retry.
    Returns (results, status) where status is 'success', 'retried', or 'failed'.
    """
    for attempt in range(RETRY_MAX + 1):
        if attempt > 0:
            delay = random.uniform(RETRY_DELAY_MIN, RETRY_DELAY_MAX)
            log.warning(
                "  [%s] retry %d/%d — waiting %.1fs, rotating UA…",
                site_name, attempt, RETRY_MAX, delay,
            )
            time.sleep(delay)
        try:
            results = fn()
            if results:
                return results, ("retried" if attempt > 0 else "success")
        except Exception as exc:
            log.error("  [%s] attempt %d raised: %s", site_name, attempt + 1, exc)

    return [], "failed"


# ── Scrapers ───────────────────────────────────────────────────────────────────

def scrape_unieuro() -> list[dict]:
    """
    unieuro.it — search for "insinkerator".
    The site is an Ionic/Angular SPA; product data is loaded client-side, so
    static scraping returns 0 results. A headless browser (e.g. Playwright) is
    required for reliable extraction.
    """
    site = "unieuro.it"
    url = "https://www.unieuro.it/online/search/?q=insinkerator"
    log.info("Scraping %s …", site)

    resp = get(url)
    if resp is None:
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    results = []

    for card in soup.select(
        "[data-product-id], .product-card, .product-tile, "
        "[class*='product-item'], [class*='ProductCard']"
    ):
        name_tag = card.select_one(
            ".product-name, .product-title, h2 a, h3 a, [class*='name'] a"
        )
        price_tag = card.select_one(
            ".price .value, [class*='price'] .value, [data-price], "
            "span.price, [class*='price-box']"
        )
        link_tag = card.select_one("a[href]")

        if not (name_tag and price_tag):
            continue

        product_url = link_tag["href"] if link_tag and link_tag.get("href") else url
        if product_url.startswith("/"):
            product_url = "https://www.unieuro.it" + product_url

        row = make_row(site, name_tag.get_text(), price_tag.get_text(), product_url)
        if row:
            results.append(row)

    if not results:
        log.warning(
            "unieuro.it returned 0 products — the site is a client-side SPA. "
            "A headless browser (e.g. Playwright) is required."
        )
    log.info("  → %d products found", len(results))
    return results


def scrape_eprice() -> list[dict]:
    """
    eprice.it — search for "insinkerator".
    The site is protected by Akamai bot detection; static requests receive a 403.
    A headless browser (e.g. Playwright) is required.
    """
    site = "eprice.it"
    url = "https://www.eprice.it/it/s/insinkerator/"
    log.info("Scraping %s …", site)

    resp = get(url)
    if resp is None:
        log.warning(
            "eprice.it blocked the request (Akamai). "
            "A headless browser is required."
        )
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    results = []

    for card in soup.select(
        ".product-item, .product-thumb, li[class*='product'], "
        "[class*='product-card'], article.product"
    ):
        name_tag = card.select_one(
            "h2 a, h3 a, .product-name a, a.product-item-link, "
            "[class*='name'] a, [itemprop='name']"
        )
        price_tag = card.select_one(
            "span.price, .product-price, [class*='price'] span, "
            "[itemprop='price'], .price-box .price"
        )
        link_tag = card.select_one("a[href]")

        if not (name_tag and price_tag):
            continue

        product_url = link_tag["href"] if link_tag and link_tag.get("href") else url
        if product_url.startswith("/"):
            product_url = "https://www.eprice.it" + product_url

        row = make_row(site, name_tag.get_text(), price_tag.get_text(), product_url)
        if row:
            results.append(row)

    if not results:
        log.warning(
            "eprice.it returned 0 products — likely blocked by Akamai. "
            "A headless browser is required."
        )
    log.info("  → %d products found", len(results))
    return results


def scrape_mediaworld() -> list[dict]:
    """
    mediaworld.it — search for "insinkerator".
    The site is protected by Cloudflare; static requests receive a JS challenge.
    A headless browser (e.g. Playwright) is required.
    """
    site = "mediaworld.it"
    url = "https://www.mediaworld.it/it/search.html?q=insinkerator"
    log.info("Scraping %s …", site)

    resp = get(url)
    if resp is None:
        log.warning(
            "mediaworld.it blocked the request (Cloudflare). "
            "A headless browser is required."
        )
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    results = []

    for card in soup.select(
        "[data-testid*='product'], .product-item, article[class*='product'], "
        "[class*='ProductCard'], [class*='product-card']"
    ):
        name_tag = card.select_one(
            "h2, h3, [class*='title'], [class*='name'], "
            "[data-testid*='title'], [data-testid*='name']"
        )
        price_tag = card.select_one(
            "[class*='price'], [data-testid*='price'], "
            "span.price, .price-box"
        )
        link_tag = card.select_one("a[href]")

        if not (name_tag and price_tag):
            continue

        product_url = link_tag["href"] if link_tag and link_tag.get("href") else url
        if product_url.startswith("/"):
            product_url = "https://www.mediaworld.it" + product_url

        row = make_row(site, name_tag.get_text(), price_tag.get_text(), product_url)
        if row:
            results.append(row)

    if not results:
        log.warning(
            "mediaworld.it returned 0 products — likely a Cloudflare JS challenge. "
            "A headless browser is required."
        )
    log.info("  → %d products found", len(results))
    return results


def scrape_trovaincasso() -> list[dict]:
    """
    trovaincasso.it — category page filtered to InSinkErator brand.
    Cards: article / li.item  Name: a.product-item-link  Price: span.price
    Fallback card selectors: li.item, .products-list li
    """
    site = "trovaincasso.it"
    url = (
        "https://www.trovaincasso.it/elettrodomestici-da-incasso"
        "/tritarifiuti/brand/insinkerator.html"
    )
    log.info("Scraping %s …", site)

    resp = get(url)
    if resp is None:
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    results = []

    cards = _select_first(
        soup,
        "article, .product-item, li.item",
        "li.item",
        ".products-list li, .category-products li",
    )

    for card in cards:
        name_tag = card.select_one(
            "a.product-item-link, .product-name a, h2 a, h3 a, [class*='name'] a, strong a"
        )
        price_tag = card.select_one(
            "span.price, [class*='price'] span, .price-box .price"
        )
        link_tag = card.select_one("a[href]")

        if not (name_tag and price_tag):
            continue

        product_url = link_tag["href"] if link_tag and link_tag.get("href") else url
        if product_url.startswith("/"):
            product_url = "https://www.trovaincasso.it" + product_url

        stock = detect_stock_status(card)
        row = make_row(site, name_tag.get_text(), price_tag.get_text(), product_url, stock)
        if row:
            results.append(row)

    log.info("  → %d products found", len(results))
    return results


def scrape_pentoleprofessionali() -> list[dict]:
    """
    pentoleprofessionali.it — Magento brand page for InSinkErator.
    Items: li.product-item  Name: .product-grid-item__name-link
    Price: [data-price-type="finalPrice"]  URL: a.product-grid-item__link
    Fallback card selectors: li.product-grid-item, .products-grid li
    """
    site = "pentoleprofessionali.it"
    url = "https://www.pentoleprofessionali.it/it/marchi-trattati/insinkerator"
    log.info("Scraping %s …", site)

    resp = get(url)
    if resp is None:
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    results = []

    cards = _select_first(
        soup,
        "li.product-item",
        "li.product-grid-item",
        ".products-grid li, ol.products li",
    )

    for card in cards:
        name_tag = (
            card.select_one(".product-grid-item__name-link")
            or card.select_one("a.product-item-link")
            or card.select_one(".product-item-name a, strong.product-item-name a")
        )
        # finalPrice span holds the displayed price (discounted when applicable)
        price_tag = (
            card.select_one("[data-price-type='finalPrice']")
            or card.select_one("span.price")
            or card.select_one(".price-box .price")
        )
        link_tag = (
            card.select_one("a.product-grid-item__link")
            or card.select_one("a.product-item-link")
            or card.select_one("a[href]")
        )

        if not (name_tag and price_tag):
            continue

        product_url = link_tag["href"] if link_tag and link_tag.get("href") else url

        stock = detect_stock_status(card)
        row = make_row(site, name_tag.get_text(), price_tag.get_text(), product_url, stock)
        if row:
            results.append(row)

    log.info("  → %d products found", len(results))
    return results


def scrape_leroymerlin() -> list[dict]:
    """
    leroymerlin.it — search for "insinkerator".
    The site is protected by DataDome; static requests typically receive a 403.
    Attempts the scrape anyway and returns results if DataDome lets the request
    through; logs a warning otherwise.
    """
    site = "leroymerlin.it"
    url = "https://www.leroymerlin.it/ricerca?q=insinkerator"
    log.info("Scraping %s …", site)

    session = _make_session()
    session.headers.update({
        "Referer": "https://www.google.it/",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "cross-site",
    })

    resp = get(url, session=session)
    if resp is None:
        log.warning(
            "leroymerlin.it blocked the request (DataDome). "
            "A headless browser (e.g. Playwright) is required."
        )
        return []

    if resp.status_code == 403 or "datadome" in resp.headers.get("server", "").lower():
        log.warning(
            "leroymerlin.it: DataDome challenge page returned. "
            "A headless browser is required for this site."
        )
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    results = []

    for card in soup.select(
        "[data-test='product-cell'], .product-cell, "
        "[class*='ProductCard'], [class*='product-card']"
    ):
        name_tag = card.select_one(
            "[data-test='product-cell-title'], .product-cell__title, "
            "[class*='product-title'], h2, h3"
        )
        price_tag = card.select_one(
            "[data-test='product-cell-price'], .product-cell__price, "
            ".price--final, [class*='price']"
        )
        link_tag = card.select_one(
            "a[data-test='product-cell-link'], a[href*='/p/'], a[href]"
        )

        if not (name_tag and price_tag):
            continue

        product_url = link_tag["href"] if link_tag and link_tag.get("href") else url
        if product_url.startswith("/"):
            product_url = "https://www.leroymerlin.it" + product_url

        row = make_row(site, name_tag.get_text(), price_tag.get_text(), product_url)
        if row:
            results.append(row)

    if not results:
        log.warning(
            "leroymerlin.it returned 0 products — likely blocked by DataDome. "
            "A headless browser (e.g. Playwright) is required."
        )
    log.info("  → %d products found", len(results))
    return results


def scrape_bricobravo() -> list[dict]:
    """
    bricobravo.com — Shopify-based store; search for "insinkerator".
    Cards: .product-card  Name: .product-card__title a span
    Price: .f-price-item--sale / .f-price-item--regular
    Fallback card selectors: .card-product, [class*='product-item']
    """
    site = "bricobravo.com"
    url = "https://www.bricobravo.com/search?q=insinkerator"
    log.info("Scraping %s …", site)

    resp = get(url)
    if resp is None:
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    results = []

    cards = _select_first(
        soup,
        ".product-card",
        ".card-product, [class*='product-card']",
        "[class*='product-item']",
    )

    for card in cards:
        name_tag = card.select_one(".product-card__title a span, .product-card__title a")
        if not name_tag:
            name_tag = card.select_one("h3 a, h2 a, [class*='title'] a")

        # Prefer sale price when active, else regular price
        price_tag = (
            card.select_one(".f-price__sale .f-price-item--sale")
            or card.select_one(".f-price-item--regular")
            or card.select_one("[class*='price']")
        )
        link_tag = card.select_one(".product-card__title a[href], a[href]")

        if not (name_tag and price_tag):
            continue

        price_text = price_tag.get_text(strip=True)
        if not price_text:
            alt = card.select_one(".f-price-item--regular")
            if alt:
                price_text = alt.get_text(strip=True)
        if not price_text:
            continue

        product_url = link_tag["href"] if link_tag and link_tag.get("href") else url
        if product_url.startswith("/"):
            product_url = "https://www.bricobravo.com" + product_url
        product_url = product_url.split("?")[0]

        stock = detect_stock_status(card)
        row = make_row(site, name_tag.get_text(), price_text, product_url, stock)
        if row:
            results.append(row)

    log.info("  → %d products found", len(results))
    return results


def scrape_plumbingshop() -> list[dict]:
    """
    plumbingshop.it — Magento-style store; search for "insinkerator".
    NOTE: as of 2026-05, the domain returns NXDOMAIN (DNS not found).
    The scraper is included so it activates automatically if the site comes back.
    """
    site = "plumbingshop.it"
    url = "https://www.plumbingshop.it/catalogsearch/result/?q=insinkerator"
    log.info("Scraping %s …", site)

    resp = get(url)
    if resp is None:
        log.warning(
            "plumbingshop.it is unreachable (NXDOMAIN as of 2026-05). "
            "The domain may be down or renamed."
        )
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    results = []

    for card in soup.select("li.product-item, .product-item"):
        name_tag = card.select_one(
            "a.product-item-link, .product-item-link, "
            "[class*='product-name'] a, h2 a, h3 a"
        )
        price_tag = card.select_one(
            "[data-price-type='finalPrice'], span.price, "
            ".price-box .price, [class*='price'] span"
        )
        link_tag = card.select_one("a.product-item-link, a[href]")

        if not (name_tag and price_tag):
            continue

        product_url = link_tag["href"] if link_tag and link_tag.get("href") else url
        if product_url.startswith("/"):
            product_url = "https://www.plumbingshop.it" + product_url

        row = make_row(site, name_tag.get_text(), price_tag.get_text(), product_url)
        if row:
            results.append(row)

    log.info("  → %d products found", len(results))
    return results


def scrape_lineadaincasso() -> list[dict]:
    """
    lineadaincasso.it — PrestaShop; search for "insinkerator".
    Cards: article.product-miniature  Name: .product-title a
    Price: span.price  URL: .product-title a[href]
    Fallback card selectors: .js-product-miniature, .product-miniature
    """
    site = "lineadaincasso.it"
    url = "https://www.lineadaincasso.it/cerca?s=insinkerator"
    log.info("Scraping %s …", site)

    resp = get(url)
    if resp is None:
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    results = []

    cards = _select_first(
        soup,
        "article.product-miniature",
        ".js-product-miniature",
        ".product-miniature, .product-container",
    )

    for card in cards:
        name_tag = (
            card.select_one(".product-title a")
            or card.select_one("h3 a, h2 a, [class*='product-title'] a")
        )
        price_tag = (
            card.select_one("span.price")
            or card.select_one("[itemprop='price'], .content_price .price, .price")
        )

        if not (name_tag and price_tag):
            continue

        product_url = name_tag.get("href", url)

        stock = detect_stock_status(card)
        row = make_row(site, name_tag.get_text(), price_tag.get_text(), product_url, stock)
        if row:
            results.append(row)

    log.info("  → %d products found", len(results))
    return results


def scrape_opportunitycommerce() -> list[dict]:
    """
    opportunitycommerce.com — PrestaShop; search for "insinkerator".
    Cards: article.product-miniature  Name: .product-title a
    Price: [itemprop='price']  URL: a[href] (first link in card)
    Fallback card selectors: .js-product-miniature, .product-miniature
    """
    site = "opportunitycommerce.com"
    url = "https://www.opportunitycommerce.com/it/ricerca?controller=search&s=insinkerator"
    log.info("Scraping %s …", site)

    resp = get(url)
    if resp is None:
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    results = []

    cards = _select_first(
        soup,
        "article.product-miniature",
        ".js-product-miniature",
        ".product-miniature, .product-container",
    )

    for card in cards:
        name_tag = (
            card.select_one(".product-title a")
            or card.select_one("h3 a, h2 a, [class*='product-title'] a")
        )
        price_tag = (
            card.select_one("[itemprop='price']")
            or card.select_one("span.price, .price")
        )
        link_tag = card.select_one("a[href]")

        if not (name_tag and price_tag):
            continue

        product_url = link_tag["href"] if link_tag and link_tag.get("href") else url

        stock = detect_stock_status(card)
        row = make_row(site, name_tag.get_text(), price_tag.get_text(), product_url, stock)
        if row:
            results.append(row)

    log.info("  → %d products found", len(results))
    return results


def scrape_climaconvenienza() -> list[dict]:
    """
    climaconvenienza.it — Shopify; search for "insinkerator".
    Cards: product-card (custom element)  Name: a.js-prod-link[aria-label]
    Price: .price__current  URL: a.js-prod-link[href]
    Fallback card selectors: .product-card, .grid__item
    """
    site = "climaconvenienza.it"
    url = "https://www.climaconvenienza.it/search?q=insinkerator&type=product"
    log.info("Scraping %s …", site)

    resp = get(url)
    if resp is None:
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    results = []

    cards = _select_first(
        soup,
        "product-card",
        ".product-card, .grid__item",
        "[class*='card-product'], li.product",
    )

    for card in cards:
        name_link = (
            card.select_one("a.js-prod-link")
            or card.select_one("a[class*='prod'], a[class*='product']")
            or card.select_one("h3 a, h2 a")
        )
        price_tag = (
            card.select_one(".price__current")
            or card.select_one(".price__sale, [class*='price']")
        )

        if not (name_link and price_tag):
            continue

        name = name_link.get("aria-label", "") or name_link.get_text(strip=True)
        price_text = price_tag.get_text(strip=True)

        product_url = name_link.get("href", url)
        if product_url.startswith("/"):
            product_url = "https://www.climaconvenienza.it" + product_url

        stock = detect_stock_status(card)
        row = make_row(site, name, price_text, product_url, stock)
        if row:
            results.append(row)

    log.info("  → %d products found", len(results))
    return results


def scrape_vieffetrade() -> list[dict]:
    """
    vieffetrade.com — Magento 2 SPA; requires JavaScript to render product listings.
    Static requests receive a JS-required notice; a headless browser is needed.
    """
    site = "vieffetrade.com"
    url = "https://www.vieffetrade.com/catalogsearch/result/?q=insinkerator"
    log.info("Scraping %s …", site)

    resp = get(url)
    if resp is None:
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    results = []

    for card in soup.select("li.product-item, .product-item"):
        name_tag = card.select_one(
            "a.product-item-link, .product-item-link, [class*='product-name'] a"
        )
        price_tag = card.select_one(
            "[data-price-type='finalPrice'], span.price, .price-box .price"
        )
        link_tag = card.select_one("a[href]")

        if not (name_tag and price_tag):
            continue

        product_url = link_tag["href"] if link_tag and link_tag.get("href") else url
        if product_url.startswith("/"):
            product_url = "https://www.vieffetrade.com" + product_url

        row = make_row(site, name_tag.get_text(), price_tag.get_text(), product_url)
        if row:
            results.append(row)

    if not results:
        log.warning(
            "vieffetrade.com returned 0 products — the site is a Magento 2 SPA. "
            "A headless browser (e.g. Playwright) is required."
        )
    log.info("  → %d products found", len(results))
    return results


def scrape_yeppon() -> list[dict]:
    """
    yeppon.it — protected by Cloudflare; static requests receive a 403 challenge.
    A headless browser (e.g. Playwright) is required.
    """
    site = "yeppon.it"
    url = "https://www.yeppon.it/search?q=insinkerator"
    log.info("Scraping %s …", site)

    resp = get(url)
    if resp is None:
        log.warning(
            "yeppon.it blocked the request (Cloudflare). "
            "A headless browser is required."
        )
        return []

    if resp.status_code == 403 or "challenge" in resp.text[:500].lower():
        log.warning(
            "yeppon.it: Cloudflare challenge page returned. "
            "A headless browser is required for this site."
        )
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    results = []

    for card in soup.select(
        ".product-card, [class*='product-item'], article[class*='product'], li.product"
    ):
        name_tag = card.select_one(
            "h2, h3, [class*='title'], [class*='name'], a[class*='product']"
        )
        price_tag = card.select_one("[class*='price'], span.price")
        link_tag = card.select_one("a[href]")

        if not (name_tag and price_tag):
            continue

        product_url = link_tag["href"] if link_tag and link_tag.get("href") else url
        if product_url.startswith("/"):
            product_url = "https://www.yeppon.it" + product_url

        row = make_row(site, name_tag.get_text(), price_tag.get_text(), product_url)
        if row:
            results.append(row)

    if not results:
        log.warning(
            "yeppon.it returned 0 products — likely blocked by Cloudflare. "
            "A headless browser is required."
        )
    log.info("  → %d products found", len(results))
    return results


def scrape_amazon() -> list[dict]:
    """
    amazon.it — search results for "insinkerator tritarifiuti".
    Amazon's search results render server-side for the first page.
    """
    site = "amazon.it"
    url = "https://www.amazon.it/s?k=insinkerator+tritarifiuti"
    log.info("Scraping %s …", site)

    session = _make_session()
    session.headers.update({
        "Connection": "keep-alive",
        "Referer": "https://www.amazon.it/",
    })

    try:
        resp = session.get(url, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.error("Failed to fetch %s: %s", site, exc)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    results = []

    # Try primary selector, then fallbacks
    items = _select_first(
        soup,
        "[data-component-type='s-search-result']",
        ".s-result-item[data-asin]:not([data-asin=''])",
        ".sg-col-inner .a-section[data-asin]",
    )

    for item in items:
        name_tag = item.select_one("h2 a span, h2 span")
        # Price is split into whole and fraction parts
        whole = item.select_one(".a-price-whole")
        fraction = item.select_one(".a-price-fraction")

        if not (name_tag and whole):
            continue

        price_raw = whole.get_text()
        if fraction:
            price_raw = price_raw.rstrip(",.") + "," + fraction.get_text()

        asin = item.get("data-asin", "")
        if asin:
            product_url = f"https://www.amazon.it/dp/{asin}"
        else:
            link_tag = item.select_one("h2 a[href]")
            product_url = link_tag["href"] if link_tag and link_tag.get("href") else url
            if product_url.startswith("/"):
                product_url = "https://www.amazon.it" + product_url

        stock = detect_stock_status(item)
        row = make_row(site, name_tag.get_text(), price_raw, product_url, stock)
        if row:
            results.append(row)

    log.info("  → %d products found", len(results))
    return results


# ── Output: CSV ────────────────────────────────────────────────────────────────

FIELDNAMES = ["date", "site", "product_name", "price_eur", "url", "stock_status"]


def _migrate_csv_add_stock_status() -> None:
    """Add stock_status column (defaulting to 'unknown') to an existing CSV."""
    log.info("Migrating %s to add stock_status column…", CSV_FILE)
    old_path = Path(CSV_FILE)
    tmp_path = Path(CSV_FILE + ".tmp")

    with open(old_path, newline="", encoding="utf-8") as fin, \
         open(tmp_path, "w", newline="", encoding="utf-8") as fout:
        reader = csv.DictReader(fin)
        writer = csv.DictWriter(fout, fieldnames=FIELDNAMES)
        writer.writeheader()
        for row in reader:
            row.setdefault("stock_status", "unknown")
            writer.writerow({f: row.get(f, "") for f in FIELDNAMES})

    tmp_path.replace(old_path)
    log.info("CSV migration complete.")


def save_to_csv(products: list[dict]) -> None:
    """Append today's rows to prices.csv, migrating schema if needed."""
    if Path(CSV_FILE).exists():
        with open(CSV_FILE, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            if reader.fieldnames and "stock_status" not in reader.fieldnames:
                _migrate_csv_add_stock_status()

        with open(CSV_FILE, "a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=FIELDNAMES)
            writer.writerows(products)
    else:
        with open(CSV_FILE, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=FIELDNAMES)
            writer.writeheader()
            writer.writerows(products)

    log.info("Saved %d rows to %s", len(products), CSV_FILE)


# ── Output: Google Sheets ──────────────────────────────────────────────────────

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def _get_or_create_worksheet(spreadsheet, title: str, rows: int = 1000, cols: int = 10):
    """Return an existing worksheet by title, or create it."""
    try:
        return spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=title, rows=rows, cols=cols)


def upload_to_sheets(products: list[dict]) -> None:
    """
    Upload results to Google Sheets:
    - 'Raw Data' tab: append all rows (one per run).
    - 'Latest' tab: overwrite with only today's scrape.
    """
    if not SHEET_ID:
        log.warning("GOOGLE_SHEET_ID not set — skipping Sheets upload.")
        return

    creds_source = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if creds_source:
        info = json.loads(creds_source)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    elif Path(CREDENTIALS_FILE).exists():
        creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES)
    else:
        log.error(
            "No Google credentials found. Set GOOGLE_CREDENTIALS_JSON env var "
            "or place service_account.json in the project root."
        )
        return

    try:
        client = gspread.authorize(creds)
        spreadsheet = client.open_by_key(SHEET_ID)
    except Exception as exc:
        log.error("Could not open Google Sheet %s: %s", SHEET_ID, exc)
        return

    rows_as_lists = [list(p.values()) for p in products]

    # ── Raw Data tab: append ──────────────────────────────────────────────────
    raw_ws = _get_or_create_worksheet(spreadsheet, "Raw Data")
    if raw_ws.row_count == 1 and not raw_ws.get_all_values():
        raw_ws.append_row(FIELDNAMES)
    raw_ws.append_rows(rows_as_lists, value_input_option="USER_ENTERED")
    log.info("Appended %d rows to 'Raw Data' tab.", len(products))

    # ── Latest tab: overwrite ─────────────────────────────────────────────────
    latest_ws = _get_or_create_worksheet(spreadsheet, "Latest")
    latest_ws.clear()
    latest_ws.append_row(FIELDNAMES)
    latest_ws.append_rows(rows_as_lists, value_input_option="USER_ENTERED")
    log.info("Overwrote 'Latest' tab with %d rows.", len(products))


# ── End-of-run summary ─────────────────────────────────────────────────────────

def _print_run_summary(summary: list[dict]) -> None:
    col_w = 32
    print()
    print("=" * 68)
    print("  END-OF-RUN SUMMARY")
    print("=" * 68)
    print(f"  {'SITE':<{col_w}} {'FOUND':>6}  {'STATUS':<10}  NOTE")
    print("  " + "-" * 64)

    total_found = 0
    total_sites = 0
    warned = False

    for row in summary:
        site     = row["site"]
        found    = row["found"]
        status   = row["status"]
        typical  = TYPICAL_COUNTS.get(site)

        total_found += found
        if found > 0:
            total_sites += 1

        status_label = {"success": "OK", "retried": "RETRIED", "failed": "FAILED"}.get(status, status)

        note = ""
        if typical:
            if found == 0:
                note = f"EXPECTED ~{typical} — check selector or network"
                warned = True
            elif found < typical * 0.5:
                note = f"LOW (got {found}, typical ~{typical})"
                warned = True

        print(f"  {site:<{col_w}} {found:>6}  {status_label:<10}  {note}")

    print("=" * 68)
    print(f"  Total: {total_found} products from {total_sites} site(s)")
    if warned:
        print("  ⚠  One or more sites returned fewer products than expected.")
    print("=" * 68)
    print()


# ── Scraper registry ───────────────────────────────────────────────────────────

# (function, site_name, apply_retry)
# apply_retry=False for known SPA/bot-blocked sites that always return 0 —
# retrying them just burns time without any chance of recovery.
SCRAPERS: list[tuple] = [
    (scrape_unieuro,              "unieuro.it",              False),
    (scrape_eprice,               "eprice.it",               False),
    (scrape_mediaworld,           "mediaworld.it",           False),
    (scrape_trovaincasso,         "trovaincasso.it",         True),
    (scrape_pentoleprofessionali, "pentoleprofessionali.it", True),
    (scrape_leroymerlin,          "leroymerlin.it",          False),
    (scrape_bricobravo,           "bricobravo.com",          True),
    (scrape_plumbingshop,         "plumbingshop.it",         False),
    (scrape_lineadaincasso,       "lineadaincasso.it",       True),
    (scrape_opportunitycommerce,  "opportunitycommerce.com", True),
    (scrape_climaconvenienza,     "climaconvenienza.it",     True),
    (scrape_vieffetrade,          "vieffetrade.com",         False),
    (scrape_yeppon,               "yeppon.it",               False),
    (scrape_amazon,               "amazon.it",               True),
]


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    all_products: list[dict] = []
    run_summary: list[dict] = []

    for fn, site_name, do_retry in SCRAPERS:
        if do_retry:
            results, status = _scrape_with_retry(fn, site_name)
        else:
            try:
                results = fn()
                status = "success" if results else "failed"
            except Exception as exc:
                log.error("Unhandled error in %s: %s", fn.__name__, exc)
                results, status = [], "failed"

        run_summary.append({"site": site_name, "found": len(results), "status": status})
        all_products.extend(results)
        time.sleep(random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX))

    if not all_products:
        log.warning("No products collected — check scraper selectors.")
    else:
        save_to_csv(all_products)
        upload_to_sheets(all_products)

    _print_run_summary(run_summary)
    log.info("Done. Total products collected: %d", len(all_products))


if __name__ == "__main__":
    main()
