"""
InSinkErator Italy Price Tracker

Scrapes prices from Italian retailers via the cheapest reliable channel per
site — Shopify /products.json + search-suggest JSON, the WooCommerce Store
API, or HTML parsing — with a one-shot headless-Chromium (Playwright)
fallback for sites that block plain requests. Results go to prices.csv and
Google Sheets; per-site health lands in site_health.json and stock-status
transitions in stock_history.json (read by index.html).
"""

import csv
import json
import logging
import os
import random
import re
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from urllib.parse import quote

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
# Counts observed 2026-07-09 with the brand + accessory filters in place.
TYPICAL_COUNTS = {
    "trovaincasso.it": 5,
    "pentoleprofessionali.it": 6,
    "bricobravo.com": 4,
    "lineadaincasso.it": 17,
    "opportunitycommerce.com": 9,
    "climaconvenienza.it": 5,
    "caldaiemurali.it": 5,
    "tritarifiutidomesticoservice.it": 4,
    "kelsostore.it": 4,
    "yeppon.it": 5,
    "vieffetrade.com": 22,
    "amazon.it": 13,
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


def get_json(url: str, session: Optional[requests.Session] = None) -> Optional[dict]:
    """GET a JSON endpoint with shared headers; returns None on any failure."""
    resp = get(url, session=session)
    if resp is None:
        return None
    try:
        return resp.json()
    except ValueError as exc:
        log.error("Non-JSON response from %s: %s", url, exc)
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
    r"\b(tappo|guarnizione|accessor\w*|ricambio|filtro|flangia|coperchio|kit"
    r"|splash|stopper|seal|spare\s*part|ricambi|1975"
    r"|copertura|spazzolino|detersivo|paraspruzzi|collarino|adattatore|adapter"
    r"|interruttore|blancocare|pulsante|cca-00|water\s*filter|f[\s-]?1000"
    r"|throat\s*guard|deflettore|chiave|overload|rubber\s*boot|dispenser\s+di\s+sapone"
    r"|\d+\s*pezzi)\b",
    re.IGNORECASE,
)

_OUT_OF_STOCK_RE = re.compile(
    r"esaurit[oa]|non\s+disponibile|fuori\s+produzione|prodotto\s+non\s+disponibile"
    r"|sold\s+out|out\s+of\s+stock|temporaneamente\s+non\s+disponibile"
    r"|non\s+in\s+stock|disponibilit[àa]\s*:\s*0",
    re.IGNORECASE,
)


# Words that identify a real disposal unit (not an accessory). Product names
# like "Dissipatore ecologico con tappo salvaposate" mention accessories that
# are bundled in — they must NOT be filtered out.
_DISPOSAL_WORDS = re.compile(
    r"dissipatore|tritarifiuti|tritatutto|trituratore|disposer|food\s*waste"
    r"|smaltimento\s+(?:dei\s+)?rifiuti|eliminatore|evolution|badger"
    r"|lc[\s-]?50|erogatore",
    re.IGNORECASE,
)

# Below this price an item that mentions an accessory word is assumed to BE an
# accessory even if the name also mentions the disposal it fits.
_ACCESSORY_PRICE_CEILING = 150.0


def is_accessory(name: str, price: Optional[float] = None) -> bool:
    """
    True when the name looks like an accessory/spare part.
    A name that also contains a disposal keyword is treated as a real product
    (a disposal bundled with accessories) when its price is above the
    accessory ceiling — or when no price is known.
    """
    if not _ACCESSORY_WORDS.search(name):
        return False
    if _DISPOSAL_WORDS.search(name):
        return price is not None and price < _ACCESSORY_PRICE_CEILING
    return True


# ── Model canonicalization ─────────────────────────────────────────────────────
#
# Maps messy retailer product names to one canonical model per real product,
# so the dashboard can group listings across sites. Article numbers are the
# most reliable signal and are checked first; name patterns are ordered so the
# most specific match wins. This is the single source of truth — index.html
# consumes the pre-computed "model"/"category" fields from latest.json and
# price_history.json instead of re-deriving them.

CAT_DISPOSER = "disposer"
CAT_TAP = "tap"
CAT_OTHER = "other"

# InSinkErator article number → (canonical model, category)
ARTICLE_MODELS = {
    "1974100": ("Evolution Plus 1000 EC", CAT_DISPOSER),
    "1974750": ("Evolution Plus 750 EC", CAT_DISPOSER),
    "1974700": ("Premium 700 EC", CAT_DISPOSER),
    # trovaincasso labels 1974550 "Evolution Plus 550 EC" — same article as
    # everyone else's Premium 550 EC, so the article match must win.
    "1974550": ("Premium 550 EC", CAT_DISPOSER),
    # 1976xxx = SR (SoundSeal + pneumatic switch) variants; 197x1xx = ex names
    "1976100": ("Evolution Plus 1000 SR", CAT_DISPOSER),
    "1976750": ("Evolution Plus 750 SR", CAT_DISPOSER),
    "1976700": ("Premium 700 SR", CAT_DISPOSER),
    "1976550": ("Premium 550 SR", CAT_DISPOSER),
    "1976460": ("Standard 460", CAT_DISPOSER),
    "1971100": ("Evolution Plus 750 EC", CAT_DISPOSER),   # ex Evolution 100
    "1972046": ("M 46", CAT_DISPOSER),
    "1972056": ("M 56", CAT_DISPOSER),
    "1975078": ("LC-50 Professional", CAT_DISPOSER),
    "1975525": ("3N1 Touch", CAT_TAP),
    "1975520": ("GN1100", CAT_TAP),
    "1975518": ("HC1100", CAT_TAP),
    "1975522": ("H3300", CAT_TAP),
    "1975521": ("HC3300", CAT_TAP),
    # Tap + NeoTank (+ filter) bundle articles
    "1975541": ("GN1100 kit NeoTank", CAT_TAP),
    "1975542": ("HC1100 kit NeoTank", CAT_TAP),
    "1975543": ("H3300 kit NeoTank", CAT_TAP),
    "1975532": ("H3300 kit NeoTank", CAT_TAP),
    "1975544": ("HC3300 kit NeoTank", CAT_TAP),
    "1975545": ("HC3300 kit NeoTank", CAT_TAP),
    "1975557": ("4N1 Touch", CAT_TAP),
    "1975556": ("NeoTank", CAT_TAP),
}

# Ordered (pattern, model, category) rules applied to the lowercased name.
# First match wins; keep ex-model aliases and SR variants before the broader
# numeric patterns they would otherwise fall into.
MODEL_NAME_RULES = [
    (r"lc[\s.-]?50", "LC-50 Professional", CAT_DISPOSER),
    (r"evolution\s*200\b", "Evolution Plus 1000 EC", CAT_DISPOSER),   # ex name
    (r"evolution\s*100\b", "Evolution Plus 750 EC", CAT_DISPOSER),    # ex name
    (r"1000\s*sr", "Evolution Plus 1000 SR", CAT_DISPOSER),
    (r"\b1000\b", "Evolution Plus 1000 EC", CAT_DISPOSER),
    (r"750\s*sr", "Evolution Plus 750 SR", CAT_DISPOSER),
    (r"\b750\b", "Evolution Plus 750 EC", CAT_DISPOSER),
    (r"\b700\b|ise\s*66", "Premium 700 EC", CAT_DISPOSER),
    (r"550\s*sr", "Premium 550 SR", CAT_DISPOSER),
    (r"\b550\b|ise\s*56", "Premium 550 EC", CAT_DISPOSER),
    (r"\b460\b|460\s*sr|460sr|ise\s*46", "Standard 460", CAT_DISPOSER),
    (r"serie\s*m.*\b46\b|\bm\s*46\b|modello\s*46\b|mod\.?\s*46\b", "M 46", CAT_DISPOSER),
    (r"serie\s*m.*\b56\b|\bm\s*56\b|modello\s*56\b|mod\.?\s*56\b", "M 56", CAT_DISPOSER),
    (r"badger", "Badger 5", CAT_DISPOSER),
    (r"evo\s*cvr\s*cntrl|cover\s*control", "Evolution Cover Control", CAT_DISPOSER),
    (r"power\s*0[.,]?75|power\s*750", "Power 0.75 HP", CAT_DISPOSER),
    (r"pro\s*750|pro750", "PRO750", CAT_DISPOSER),
    (r"hc[\s.-]?1100", "HC1100", CAT_TAP),
    (r"gn[\s.-]?1100", "GN1100", CAT_TAP),
    (r"hc[\s.-]?3300", "HC3300", CAT_TAP),
    (r"h[\s.-]?3300", "H3300", CAT_TAP),
    (r"4\s*n\s*1|4in1|4\s*in\s*1", "4N1 Touch", CAT_TAP),
    (r"3\s*n\s*1|3in1|3\s*in\s*1", "3N1 Touch", CAT_TAP),
    (r"hot\s*250|\bh250", "HOT250", CAT_TAP),
    (r"neo\s*tank", "NeoTank", CAT_TAP),
]
_MODEL_NAME_RULES = [(re.compile(p), m, c) for p, m, c in MODEL_NAME_RULES]

# Human-readable spec line per canonical model (shown on the dashboard).
MODEL_SPECS = {
    "Evolution Plus 1000 EC": "1,00 HP · 4 stadi di triturazione · motore a induzione",
    "Evolution Plus 1000 SR": "1,00 HP · 4 stadi · SoundSeal · pulsante pneumatico",
    "Evolution Plus 750 EC": "0,75 HP · 3 stadi di triturazione · motore a induzione",
    "Evolution Plus 750 SR": "0,75 HP · 3 stadi · SoundSeal",
    "Premium 700 EC": "0,75 HP · 2 stadi di triturazione",
    "Premium 700 SR": "0,75 HP · 2 stadi · SoundSeal",
    "Premium 550 EC": "0,55 HP · 2 stadi di triturazione",
    "Premium 550 SR": "0,55 HP · 2 stadi · SoundSeal",
    "Standard 460": "0,55 HP · entry level · alimentazione continua",
    "M 46": "Serie M · entry level",
    "M 56": "Serie M · entry level",
    "LC-50 Professional": "professionale · uso commerciale leggero",
    "Evolution Cover Control": "batch feed · avvio a coperchio",
    "Power 0.75 HP": "0,75 HP · Serie Power",
    "PRO750": "0,75 HP · Serie Pro",
    "Badger 5": "0,50 HP · alimentazione continua",
    "GN1100": "erogatore acqua bollente 98 °C",
    "HC1100": "erogatore acqua bollente + fredda filtrata",
    "H3300": "erogatore acqua bollente 98 °C",
    "HC3300": "erogatore acqua bollente + fredda filtrata",
    "4N1 Touch": "miscelatore 4-in-1: bollente, fredda, miscelata",
    "3N1 Touch": "miscelatore 3-in-1 con acqua bollente",
    "HOT250": "erogatore istantaneo acqua calda",
    "NeoTank": "serbatoio sottolavello per erogatori",
    "GN1100 kit NeoTank": "erogatore GN1100 + NeoTank + filtro",
    "HC1100 kit NeoTank": "erogatore HC1100 + NeoTank + filtro",
    "H3300 kit NeoTank": "erogatore H3300 + NeoTank + filtro",
    "HC3300 kit NeoTank": "erogatore HC3300 + NeoTank + filtro",
}

_ARTICLE_RE = re.compile(r"\b(19\d{5})\b")
_TAP_HINT_RE = re.compile(r"erogatore|acqua\s+(?:calda|bollente)|dispenser|distributor", re.IGNORECASE)


def canonical_model(name: str) -> tuple:
    """
    (model, category) for a product name; model is None when the product is
    InSinkErator-branded but doesn't match any known model (category is then
    a best guess used to shelve it under "other products").
    """
    lower = name.lower()
    for art in _ARTICLE_RE.findall(lower):
        if art in ARTICLE_MODELS:
            return ARTICLE_MODELS[art]
    for pattern, model, cat in _MODEL_NAME_RULES:
        if pattern.search(lower):
            return model, cat
    if _TAP_HINT_RE.search(lower):
        return None, CAT_TAP
    if _DISPOSAL_WORDS.search(lower):
        return None, CAT_DISPOSER
    return None, CAT_OTHER


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
    """
    Build a result dict; returns None when the price cannot be parsed, the
    name isn't InSinkErator-branded (every tracked site is queried for
    "insinkerator", so unbranded hits are other vendors' products), or the
    product is an accessory — including unidentifiable items priced below the
    accessory ceiling (mounting kits, filters, spare parts sold by article
    number only).
    """
    price = parse_price(price_raw)
    if price is None or price <= 0:
        return None
    if "insinkerator" not in name.lower():
        log.debug("Skipping non-InSinkErator item: %s", name.strip())
        return None
    if is_accessory(name, price):
        log.debug("Skipping accessory: %s", name.strip())
        return None
    model, _cat = canonical_model(name)
    if model is None and price < _ACCESSORY_PRICE_CEILING:
        log.debug("Skipping unidentified low-price item (likely accessory): %s", name.strip())
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

SHOPIFY_SUGGEST_QUERIES = ("insinkerator", "tritarifiuti insinkerator")


def scrape_shopify(domain: str, site: Optional[str] = None,
                   collections: tuple = ("tritarifiuti", "insinkerator")) -> list[dict]:
    """
    Generic Shopify store scraper using public JSON endpoints (no HTML parsing):
    - /collections/<handle>/products.json?limit=250&page=N — full product data
      with per-variant "available" flags.
    - /search/suggest.json — catches InSinkErator items outside the collections.
    Both are merged and deduplicated by product URL. The "available" field
    drives stock_status directly (in_stock / out_of_stock).
    """
    site = site or domain
    base = f"https://www.{domain}"
    log.info("Scraping %s (Shopify JSON) …", site)

    by_url: dict[str, dict] = {}

    def add(name: str, price_raw: str, url: str, available: bool) -> None:
        url = url.split("?")[0]
        stock = "in_stock" if available else "out_of_stock"
        row = make_row(site, name, price_raw, url, stock)
        if row:
            by_url[url] = row

    # 1) Collection endpoints: complete variant data, paginated
    for coll in collections:
        for page in range(1, 11):
            data = get_json(f"{base}/collections/{coll}/products.json?limit=250&page={page}")
            if not data:
                break
            products = data.get("products", [])
            if not products:
                break
            for p in products:
                blob = f"{p.get('title', '')} {p.get('vendor', '')}".lower()
                if "insinkerator" not in blob:
                    continue
                variants = p.get("variants") or []
                prices = [float(v["price"]) for v in variants if v.get("price")]
                if not prices:
                    continue
                available = any(v.get("available") for v in variants)
                add(p["title"], str(min(prices)), f"{base}/products/{p['handle']}", available)
            if len(products) < 250:
                break

    # 2) Search-suggest endpoint: up to 10 hits per query, includes availability
    for q in SHOPIFY_SUGGEST_QUERIES:
        data = get_json(
            f"{base}/search/suggest.json?q={quote(q)}"
            "&resources[type]=product&resources[limit]=10"
        )
        if not data:
            continue
        try:
            products = data["resources"]["results"].get("products", [])
        except (KeyError, TypeError):
            continue
        for p in products:
            title = p.get("title", "")
            if "insinkerator" not in f"{title} {p.get('vendor', '')}".lower():
                continue
            url = p.get("url", "")
            if url.startswith("/"):
                url = base + url
            price_raw = str(p.get("price", ""))
            if not price_raw:
                continue
            key = url.split("?")[0]
            if key in by_url:
                continue
            add(title, price_raw, url, bool(p.get("available", True)))

    results = list(by_url.values())
    log.info("  → %d products found", len(results))
    return results


UNIEURO_URL = "https://www.unieuro.it/online/search/?q=insinkerator"


def _parse_unieuro(soup) -> list[dict]:
    site = "unieuro.it"
    url = UNIEURO_URL
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

    return results


def scrape_unieuro() -> list[dict]:
    """
    unieuro.it — search for "insinkerator".
    The site is an Ionic/Angular SPA; product data is loaded client-side, so
    static scraping returns 0 results and the Playwright fallback kicks in.
    """
    site = "unieuro.it"
    log.info("Scraping %s …", site)

    resp = get(UNIEURO_URL)
    if resp is None:
        return []

    results = _parse_unieuro(BeautifulSoup(resp.text, "html.parser"))
    if not results:
        log.warning("unieuro.it returned 0 products via requests (client-side SPA).")
    log.info("  → %d products found", len(results))
    return results


EPRICE_URL = "https://www.eprice.it/it/s/insinkerator/"


def _parse_eprice(soup) -> list[dict]:
    site = "eprice.it"
    url = EPRICE_URL
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

    return results


def scrape_eprice() -> list[dict]:
    """
    eprice.it — search for "insinkerator".
    The site is protected by Akamai bot detection; static requests receive a
    403, after which the Playwright fallback kicks in.
    """
    site = "eprice.it"
    log.info("Scraping %s …", site)

    resp = get(EPRICE_URL)
    if resp is None:
        log.warning("eprice.it blocked the request (Akamai).")
        return []

    results = _parse_eprice(BeautifulSoup(resp.text, "html.parser"))
    if not results:
        log.warning("eprice.it returned 0 products via requests (Akamai).")
    log.info("  → %d products found", len(results))
    return results


MEDIAWORLD_URL = "https://www.mediaworld.it/it/search.html?q=insinkerator"


def _parse_mediaworld(soup) -> list[dict]:
    site = "mediaworld.it"
    url = MEDIAWORLD_URL
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

    return results


def scrape_mediaworld() -> list[dict]:
    """
    mediaworld.it — search for "insinkerator".
    The site is protected by Cloudflare; static requests receive a JS
    challenge, after which the Playwright fallback kicks in.
    """
    site = "mediaworld.it"
    log.info("Scraping %s …", site)

    resp = get(MEDIAWORLD_URL)
    if resp is None:
        log.warning("mediaworld.it blocked the request (Cloudflare).")
        return []

    results = _parse_mediaworld(BeautifulSoup(resp.text, "html.parser"))
    if not results:
        log.warning("mediaworld.it returned 0 products via requests (Cloudflare).")
    log.info("  → %d products found", len(results))
    return results


TROVAINCASSO_URL = (
    "https://www.trovaincasso.it/elettrodomestici-da-incasso"
    "/tritarifiuti/brand/insinkerator.html"
)


def _parse_trovaincasso(soup) -> list[dict]:
    site = "trovaincasso.it"
    url = TROVAINCASSO_URL
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

    return results


def scrape_trovaincasso() -> list[dict]:
    """
    trovaincasso.it — category page filtered to InSinkErator brand.
    Cards: article / li.item  Name: a.product-item-link  Price: span.price
    Fallback card selectors: li.item, .products-list li
    """
    site = "trovaincasso.it"
    log.info("Scraping %s …", site)

    resp = get(TROVAINCASSO_URL)
    if resp is None:
        return []

    results = _parse_trovaincasso(BeautifulSoup(resp.text, "html.parser"))
    log.info("  → %d products found", len(results))
    return results


PENTOLE_URL = "https://www.pentoleprofessionali.it/it/marchi-trattati/insinkerator"


def _parse_pentoleprofessionali(soup) -> list[dict]:
    site = "pentoleprofessionali.it"
    url = PENTOLE_URL
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

    return results


def scrape_pentoleprofessionali() -> list[dict]:
    """
    pentoleprofessionali.it — Magento brand page for InSinkErator.
    Items: li.product-item  Name: .product-grid-item__name-link
    Price: [data-price-type="finalPrice"]  URL: a.product-grid-item__link
    Fallback card selectors: li.product-grid-item, .products-grid li
    """
    site = "pentoleprofessionali.it"
    log.info("Scraping %s …", site)

    resp = get(PENTOLE_URL)
    if resp is None:
        return []

    results = _parse_pentoleprofessionali(BeautifulSoup(resp.text, "html.parser"))
    log.info("  → %d products found", len(results))
    return results


LEROYMERLIN_URL = "https://www.leroymerlin.it/ricerca?q=insinkerator"


def _parse_leroymerlin(soup) -> list[dict]:
    site = "leroymerlin.it"
    url = LEROYMERLIN_URL
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

    return results


def scrape_leroymerlin() -> list[dict]:
    """
    leroymerlin.it — search for "insinkerator".
    The site is protected by DataDome; static requests typically receive a 403.
    Attempts the scrape anyway; the Playwright fallback kicks in otherwise.
    """
    site = "leroymerlin.it"
    log.info("Scraping %s …", site)

    session = _make_session()
    session.headers.update({
        "Referer": "https://www.google.it/",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "cross-site",
    })

    resp = get(LEROYMERLIN_URL, session=session)
    if resp is None:
        log.warning("leroymerlin.it blocked the request (DataDome).")
        return []

    if resp.status_code == 403 or "datadome" in resp.headers.get("server", "").lower():
        log.warning("leroymerlin.it: DataDome challenge page returned.")
        return []

    results = _parse_leroymerlin(BeautifulSoup(resp.text, "html.parser"))
    if not results:
        log.warning("leroymerlin.it returned 0 products via requests (DataDome).")
    log.info("  → %d products found", len(results))
    return results


def scrape_bricobravo() -> list[dict]:
    """bricobravo.com — Shopify store; scraped via public JSON endpoints."""
    return scrape_shopify("bricobravo.com")


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

    results = _parse_prestashop_cards(BeautifulSoup(resp.text, "html.parser"), site, url)
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

    results = _parse_prestashop_cards(BeautifulSoup(resp.text, "html.parser"), site, url)
    log.info("  → %d products found", len(results))
    return results


def scrape_climaconvenienza() -> list[dict]:
    """climaconvenienza.it — Shopify store; scraped via public JSON endpoints."""
    return scrape_shopify("climaconvenienza.it")


def scrape_caldaiemurali() -> list[dict]:
    """
    caldaiemurali.it — Shopify store; scraped via public JSON endpoints.
    NOTE: shares a catalog with climaconvenienza.it (same products/prices),
    but stocks a few extra InSinkErator models, so both are tracked.
    """
    return scrape_shopify("caldaiemurali.it")


def scrape_tritarifiutidomesticoservice() -> list[dict]:
    """
    tritarifiutidomesticoservice.it — WooCommerce; uses the public Store API
    (/wp-json/wc/store/v1/products) instead of HTML parsing. Prices come in
    minor units with an explicit currency_minor_unit; is_in_stock drives
    stock_status directly.
    """
    site = "tritarifiutidomesticoservice.it"
    url = (
        "https://tritarifiutidomesticoservice.it/wp-json/wc/store/v1/products"
        "?search=insinkerator&per_page=100"
    )
    log.info("Scraping %s (WooCommerce Store API) …", site)

    data = get_json(url)
    if not isinstance(data, list):
        return []

    results = []
    for p in data:
        prices = p.get("prices") or {}
        raw = prices.get("price")
        if not raw:
            continue
        minor_unit = int(prices.get("currency_minor_unit", 2))
        price = int(raw) / (10 ** minor_unit)
        stock = "in_stock" if p.get("is_in_stock") else "out_of_stock"
        row = make_row(site, p.get("name", ""), str(price), p.get("permalink", url), stock)
        if row:
            results.append(row)

    log.info("  → %d products found", len(results))
    return results


def scrape_kelsostore() -> list[dict]:
    """
    kelsostore.it — PrestaShop; search for "insinkerator".
    Cards: article.product-miniature  Name: .product-title a
    Price: [itemprop='price'] / span.price
    """
    site = "kelsostore.it"
    url = "https://kelsostore.it/it/ricerca?controller=search&s=insinkerator"
    log.info("Scraping %s …", site)

    resp = get(url)
    if resp is None:
        return []

    results = _parse_prestashop_cards(BeautifulSoup(resp.text, "html.parser"), site, url)
    log.info("  → %d products found", len(results))
    return results


def _parse_prestashop_cards(soup, site: str, fallback_url: str) -> list[dict]:
    """Shared PrestaShop product-miniature parser (kelsostore-style themes)."""
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
        if not (name_tag and price_tag):
            continue

        product_url = name_tag.get("href") or fallback_url

        stock = detect_stock_status(card)
        row = make_row(site, name_tag.get_text(), price_tag.get_text(), product_url, stock)
        if row:
            results.append(row)

    return results


MK2SHOP_URL = "https://www.mk2shop.com/it/catalogsearch/result/?q=insinkerator"


def _parse_mk2shop(soup) -> list[dict]:
    """Magento (theme-pearl) search results parser."""
    site = "mk2shop.com"
    url = MK2SHOP_URL
    results = []

    for card in soup.select("li.product-item, .product-item"):
        name_tag = card.select_one(
            "a.product-item-link, .product-item-link, [class*='product-name'] a"
        )
        price_tag = card.select_one(
            "[data-price-type='finalPrice'], span.price, .price-box .price"
        )
        if not (name_tag and price_tag):
            continue
        product_url = name_tag.get("href") or url
        if product_url.startswith("/"):
            product_url = "https://www.mk2shop.com" + product_url
        row = make_row(site, name_tag.get_text(), price_tag.get_text(), product_url,
                       detect_stock_status(card))
        if row:
            results.append(row)

    return results


def scrape_mk2shop() -> list[dict]:
    """
    mk2shop.com — protected by a Cloudflare managed challenge
    (cf-mitigated: challenge); static requests receive a 403.
    Handled by the Playwright fallback when available.
    """
    site = "mk2shop.com"
    log.info("Scraping %s …", site)

    resp = get(MK2SHOP_URL)
    if resp is None:
        log.warning("mk2shop.com blocked the request (Cloudflare challenge).")
        return []

    results = _parse_mk2shop(BeautifulSoup(resp.text, "html.parser"))
    log.info("  → %d products found", len(results))
    return results


def scrape_official() -> list[dict]:
    """
    insinkerator.com/it-it — the official InSinkErator site. As of 2026-07 the
    it-it locale redirects to /worldwide/italy, a distributor page pointing to
    rieko.it (a brochure site with no e-commerce). No prices are published, so
    this scraper reports 0 products; it stays registered so it activates
    automatically if an official Italian shop ever appears. Rows (if any) are
    labelled with site "official".
    """
    site = "official"
    url = "https://www.insinkerator.com/it-it"
    log.info("Scraping %s (%s) …", site, url)

    resp = get(url)
    if resp is None:
        return []

    if "/worldwide" in resp.url or "404" in resp.url:
        log.info(
            "  insinkerator.com/it-it redirects to %s — no official Italian "
            "shop exists; nothing to scrape.", resp.url,
        )
        return []

    # A real it-it shop appeared: attempt a generic product-card parse
    soup = BeautifulSoup(resp.text, "html.parser")
    results = []
    for card in soup.select("[class*='product-card'], [class*='product-item'], li.product"):
        name_tag = card.select_one("h2, h3, [class*='title'], [class*='name']")
        price_tag = card.select_one("[class*='price']")
        link_tag = card.select_one("a[href]")
        if not (name_tag and price_tag):
            continue
        product_url = link_tag["href"] if link_tag and link_tag.get("href") else url
        if product_url.startswith("/"):
            product_url = "https://www.insinkerator.com" + product_url
        row = make_row(site, name_tag.get_text(), price_tag.get_text(), product_url,
                       detect_stock_status(card))
        if row:
            results.append(row)

    log.info("  → %d products found", len(results))
    return results


VIEFFETRADE_URL = "https://www.vieffetrade.com/catalogsearch/result/?q=insinkerator"

_PRICE_TOKEN_RE = re.compile(r"\d[\d.,]*\s*€")


def _last_price(text: str) -> str:
    """
    Extract the final (displayed) price from a cell that may contain several,
    e.g. "554,54 €-29 %393,72 €" → "393,72 €" (the discounted price is last).
    """
    tokens = _PRICE_TOKEN_RE.findall(text)
    return tokens[-1] if tokens else text


def _parse_vieffetrade(soup) -> list[dict]:
    site = "vieffetrade.com"
    url = VIEFFETRADE_URL
    results = []

    # Adobe Commerce storefront-search widgets (current), then legacy Magento
    for card in soup.select("div.ds-sdk-product-item"):
        name_tag = card.select_one(".ds-sdk-product-item__name")
        price_tag = card.select_one(".ds-sdk-product-item__price")
        link_tag = card.find_parent("a") or card.select_one("a[href]")

        if not (name_tag and price_tag):
            continue

        product_url = (link_tag.get("href") or url) if link_tag else url
        if product_url.startswith("//"):
            product_url = "https:" + product_url
        elif product_url.startswith("/"):
            product_url = "https://www.vieffetrade.com" + product_url

        row = make_row(site, name_tag.get_text(),
                       _last_price(price_tag.get_text(" ", strip=True)), product_url)
        if row:
            results.append(row)

    if results:
        return results

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

    return results


def scrape_vieffetrade() -> list[dict]:
    """
    vieffetrade.com — Magento 2 SPA; requires JavaScript to render product
    listings, so the Playwright fallback handles it.
    """
    site = "vieffetrade.com"
    log.info("Scraping %s …", site)

    resp = get(VIEFFETRADE_URL)
    if resp is None:
        return []

    results = _parse_vieffetrade(BeautifulSoup(resp.text, "html.parser"))
    if not results:
        log.warning("vieffetrade.com returned 0 products via requests (Magento 2 SPA).")
    log.info("  → %d products found", len(results))
    return results


YEPPON_URL = "https://www.yeppon.it/search?q=insinkerator"


def _parse_yeppon(soup) -> list[dict]:
    """Shopify theme: div.product-card with the name in a[aria-label]."""
    site = "yeppon.it"
    results = []

    for card in soup.select("div.product-card"):
        link_tag = card.select_one("a[aria-label][href]")
        price_tag = card.select_one("[class*='price']")

        if not (link_tag and price_tag):
            continue

        product_url = link_tag["href"].split("?")[0]
        if product_url.startswith("/"):
            product_url = "https://www.yeppon.it" + product_url

        row = make_row(site, link_tag["aria-label"],
                       _last_price(price_tag.get_text(" ", strip=True)),
                       product_url, detect_stock_status(card))
        if row:
            results.append(row)

    return results


def scrape_yeppon() -> list[dict]:
    """
    yeppon.it — Shopify store behind Cloudflare. The HTML search page returns
    a 403 challenge to static requests, but the public Shopify JSON endpoints
    respond normally, so those are used (the Playwright fallback handles the
    HTML page if the JSON endpoints ever get blocked too).
    """
    return scrape_shopify("yeppon.it")


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


# ── Playwright fallback ────────────────────────────────────────────────────────
#
# For any site whose scraper returns 0 products via requests, retry ONCE with
# headless Chromium (realistic viewport + UA, Italian locale) and re-run the
# same HTML parser on the rendered page. No CAPTCHA solving, no proxies —
# if the site still blocks us it is reported as "blocked" and skipped.

PLAYWRIGHT_VIEWPORT = {"width": 1440, "height": 900}
PLAYWRIGHT_NAV_TIMEOUT_MS = 45_000
PLAYWRIGHT_SELECTOR_TIMEOUT_MS = 15_000

# site → (url, selector that indicates the product grid rendered, parser)
PLAYWRIGHT_FALLBACKS: dict[str, tuple] = {
    "unieuro.it":              (UNIEURO_URL, "[data-product-id], .product-card, [class*='product-item'], [class*='ProductCard']", _parse_unieuro),
    "eprice.it":               (EPRICE_URL, ".product-item, [class*='product-card'], article.product", _parse_eprice),
    "mediaworld.it":           (MEDIAWORLD_URL, "[data-testid*='product'], [class*='product-card'], [class*='ProductCard']", _parse_mediaworld),
    "leroymerlin.it":          (LEROYMERLIN_URL, "[data-test='product-cell'], [class*='product-card'], [class*='ProductCard']", _parse_leroymerlin),
    "vieffetrade.com":         (VIEFFETRADE_URL, "li.product-item, .product-item", _parse_vieffetrade),
    "yeppon.it":               (YEPPON_URL, ".product-card, [class*='product-item'], li.product", _parse_yeppon),
    "mk2shop.com":             (MK2SHOP_URL, "article.product-miniature, [class*='product-'], li.product", _parse_mk2shop),
    "trovaincasso.it":         (TROVAINCASSO_URL, "article, li.item", _parse_trovaincasso),
    "pentoleprofessionali.it": (PENTOLE_URL, "li.product-item, li.product-grid-item", _parse_pentoleprofessionali),
    "lineadaincasso.it":       ("https://www.lineadaincasso.it/cerca?s=insinkerator",
                                "article.product-miniature, .js-product-miniature",
                                lambda soup: _parse_prestashop_cards(soup, "lineadaincasso.it", "https://www.lineadaincasso.it/cerca?s=insinkerator")),
    "opportunitycommerce.com": ("https://www.opportunitycommerce.com/it/ricerca?controller=search&s=insinkerator",
                                "article.product-miniature, .js-product-miniature",
                                lambda soup: _parse_prestashop_cards(soup, "opportunitycommerce.com", "https://www.opportunitycommerce.com/it/ricerca?controller=search&s=insinkerator")),
    "kelsostore.it":           ("https://kelsostore.it/it/ricerca?controller=search&s=insinkerator",
                                "article.product-miniature, .js-product-miniature",
                                lambda soup: _parse_prestashop_cards(soup, "kelsostore.it", "https://kelsostore.it/it/ricerca?controller=search&s=insinkerator")),
}


def _render_with_playwright(url: str, wait_selector: str) -> Optional[str]:
    """Render a page in headless Chromium and return its HTML, or None."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.warning("Playwright is not installed — skipping browser fallback.")
        return None

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                context = browser.new_context(
                    user_agent=USER_AGENTS[1],
                    viewport=PLAYWRIGHT_VIEWPORT,
                    locale="it-IT",
                    timezone_id="Europe/Rome",
                )
                page = context.new_page()
                page.goto(url, timeout=PLAYWRIGHT_NAV_TIMEOUT_MS,
                          wait_until="domcontentloaded")
                try:
                    page.wait_for_selector(
                        wait_selector, timeout=PLAYWRIGHT_SELECTOR_TIMEOUT_MS)
                except Exception:
                    # Grid never appeared — give client-side JS a last moment,
                    # then parse whatever rendered.
                    page.wait_for_timeout(3_000)
                return page.content()
            finally:
                browser.close()
    except Exception as exc:
        log.error("Playwright rendering failed for %s: %s", url, exc)
        return None


_BLOCKED_PAGE_RE = re.compile(
    r"access denied|attention required|just a moment|pardon our interruption"
    r"|request unsuccessful|verify you are human|non sei un robot|captcha",
    re.IGNORECASE,
)


def _looks_blocked(soup) -> bool:
    """Heuristic: rendered page is a bot-challenge / access-denied page."""
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    body = soup.body.get_text(" ", strip=True)[:1500] if soup.body else ""
    if _BLOCKED_PAGE_RE.search(f"{title} {body}"):
        return True
    # An (almost) empty body means the anti-bot layer served a hollow shell
    return len(body) < 200


def _playwright_fallback(site: str) -> tuple[list[dict], bool]:
    """
    Retry a 0-product site once via headless Chromium.
    Returns (results, rendered): rendered=False means the page could not be
    fetched / was a challenge page (truly blocked), while rendered=True with
    no results means the site is reachable but lists no matching products.
    """
    cfg = PLAYWRIGHT_FALLBACKS.get(site)
    if not cfg:
        return [], False
    url, selector, parser = cfg
    log.info("  [%s] 0 products via requests — retrying with headless Chromium …", site)
    html = _render_with_playwright(url, selector)
    if not html:
        return [], False
    soup = BeautifulSoup(html, "html.parser")
    results = parser(soup)
    if not results and _looks_blocked(soup):
        return [], False
    log.info("  [%s] Playwright fallback → %d products", site, len(results))
    return results, True


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
    """
    Save today's rows to prices.csv, migrating schema if needed.
    Re-runs on the same day replace that day's rows for the sites just
    scraped (instead of appending duplicates), so multiple runs stay
    idempotent while other sites' rows are preserved.
    """
    scraped_sites = {p["site"] for p in products}

    if Path(CSV_FILE).exists():
        with open(CSV_FILE, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            if reader.fieldnames and "stock_status" not in reader.fieldnames:
                _migrate_csv_add_stock_status()

        with open(CSV_FILE, newline="", encoding="utf-8") as fh:
            kept = [
                row for row in csv.DictReader(fh)
                if not (row.get("date") == TODAY and row.get("site") in scraped_sites)
            ]
    else:
        kept = []

    tmp_path = Path(CSV_FILE + ".tmp")
    with open(tmp_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDNAMES)
        writer.writeheader()
        for row in kept:
            writer.writerow({f: row.get(f, "") for f in FIELDNAMES})
        writer.writerows(products)
    tmp_path.replace(Path(CSV_FILE))

    log.info("Saved %d rows to %s (%d previous rows kept)", len(products), CSV_FILE, len(kept))


# ── Dashboard data files ───────────────────────────────────────────────────────
#
# index.html renders entirely from repo-relative JSON committed by CI:
#   latest.json        — today's rows with canonical model/category attached
#   price_history.json — per model per site, daily minimum price series
# Both are regenerated on every run (price history is re-derived from
# prices.csv, so improved canonicalization rules retroactively fix history).

LATEST_FILE = "latest.json"
PRICE_HISTORY_FILE = "price_history.json"


def _dashboard_row(p: dict) -> dict:
    model, category = canonical_model(p["product_name"])
    row = dict(p)
    row["model"] = model
    row["category"] = category
    return row


def write_latest(products: list[dict]) -> None:
    """Write today's enriched rows for the dashboard."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    payload = {
        "generated": now,
        "date": TODAY,
        "specs": MODEL_SPECS,
        "rows": [_dashboard_row(p) for p in products],
    }
    Path(LATEST_FILE).write_text(
        json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8"
    )
    log.info("Dashboard data written to %s (%d rows)", LATEST_FILE, len(products))


def write_price_history() -> None:
    """
    Rebuild price_history.json from prices.csv: for every canonical model,
    per site, the daily minimum price — compact series for the dashboard's
    price-trend charts.
    """
    if not Path(CSV_FILE).exists():
        return

    # model -> site -> {date: min price}
    series: dict = {}
    with open(CSV_FILE, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            name = row.get("product_name", "")
            if "insinkerator" not in name.lower():
                continue
            try:
                price = float(row["price_eur"])
            except (KeyError, ValueError):
                continue
            if price <= 0 or is_accessory(name, price):
                continue
            # a sold-out listing's price is not a real offer
            if row.get("stock_status") == "out_of_stock":
                continue
            model, _cat = canonical_model(name)
            if model is None:
                continue
            day = row.get("date", "")
            site = row.get("site", "")
            if not (day and site):
                continue
            per_day = series.setdefault(model, {}).setdefault(site, {})
            if day not in per_day or price < per_day[day]:
                per_day[day] = price

    payload = {
        model: {
            site: sorted(per_day.items())
            for site, per_day in sites.items()
        }
        for model, sites in series.items()
    }
    Path(PRICE_HISTORY_FILE).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    n_points = sum(len(days) for sites in series.values() for days in sites.values())
    log.info("Price history written to %s (%d models, %d points)",
             PRICE_HISTORY_FILE, len(series), n_points)


# ── Per-site health tracking ───────────────────────────────────────────────────

SITE_HEALTH_FILE = "site_health.json"


def _load_json_file(path: str, default):
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        log.error("Could not read %s (%s) — starting fresh.", path, exc)
        return default


def update_site_health(run_summary: list[dict]) -> None:
    """
    Persist per-site health to site_health.json:
    last_run / last_success timestamps, product count and run status.
    last_success survives failed runs so the dashboard can show staleness.
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    health = _load_json_file(SITE_HEALTH_FILE, {})
    sites = health.get("sites", {})

    for row in run_summary:
        entry = sites.get(row["site"], {})
        entry["last_run"] = now
        entry["status"] = row["status"]
        entry["product_count"] = row["found"]
        if row["found"] > 0:
            entry["last_success"] = now
        sites[row["site"]] = entry

    Path(SITE_HEALTH_FILE).write_text(
        json.dumps({"last_run": now, "sites": sites}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    log.info("Site health written to %s", SITE_HEALTH_FILE)


# ── Stock-status history ───────────────────────────────────────────────────────

STOCK_STATE_FILE = "stock_state.json"
STOCK_HISTORY_FILE = "stock_history.json"


def _effective_stock(status: str) -> str:
    """
    Collapse stock_status to a binary signal for history tracking.
    A product listed with a price and no out-of-stock marker ('unknown')
    counts as in stock — that matches how the dashboard treats it.
    """
    return "out_of_stock" if status == "out_of_stock" else "in_stock"


def _seed_stock_from_csv() -> tuple[dict, list[dict]]:
    """
    First run only: reconstruct state + transition history from prices.csv,
    so the history starts populated instead of empty.
    """
    state: dict[str, str] = {}
    history: list[dict] = []
    if not Path(CSV_FILE).exists():
        return state, history

    with open(CSV_FILE, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            key = f"{row.get('site', '')}|{row.get('product_name', '')}"
            new = _effective_stock(row.get("stock_status", "unknown"))
            old = state.get(key)
            if old is not None and old != new:
                history.append({
                    "product_name": row.get("product_name", ""),
                    "site": row.get("site", ""),
                    "old_status": old,
                    "new_status": new,
                    "timestamp": f"{row.get('date', '')}T00:00:00+00:00",
                })
            state[key] = new

    log.info("Seeded stock state from %s: %d products, %d historical transitions.",
             CSV_FILE, len(state), len(history))
    return state, history


def update_stock_history(products: list[dict]) -> None:
    """
    Compare each product's current stock status to the previous run and append
    a record to stock_history.json for every in/out-of-stock transition.
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if Path(STOCK_STATE_FILE).exists():
        state = _load_json_file(STOCK_STATE_FILE, {})
        history = _load_json_file(STOCK_HISTORY_FILE, [])
    else:
        state, history = _seed_stock_from_csv()

    changes = 0
    for p in products:
        key = f"{p['site']}|{p['product_name']}"
        new = _effective_stock(p["stock_status"])
        old = state.get(key)
        if old is not None and old != new:
            history.append({
                "product_name": p["product_name"],
                "site": p["site"],
                "old_status": old,
                "new_status": new,
                "timestamp": now,
            })
            changes += 1
        state[key] = new

    # Attach/refresh the canonical model on every record so the dashboard can
    # group transitions per model without re-deriving names client-side.
    for rec in history:
        rec["model"] = canonical_model(rec.get("product_name", ""))[0]

    Path(STOCK_STATE_FILE).write_text(
        json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    Path(STOCK_HISTORY_FILE).write_text(
        json.dumps(history, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    log.info("Stock history updated: %d transition(s) this run (%d total).",
             changes, len(history))


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

        status_label = {
            "success": "OK",
            "retried": "RETRIED",
            "failed": "FAILED",
            "playwright": "BROWSER",
            "blocked": "BLOCKED",
            "empty": "EMPTY",
            "no_shop": "NO SHOP",
        }.get(status, status)

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

# Sites that legitimately have nothing to sell (no e-commerce at all);
# 0 products there is expected, not a failure.
NO_SHOP_SITES = {"official"}

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
    (scrape_caldaiemurali,        "caldaiemurali.it",        True),
    (scrape_tritarifiutidomesticoservice, "tritarifiutidomesticoservice.it", True),
    (scrape_kelsostore,           "kelsostore.it",           True),
    (scrape_mk2shop,              "mk2shop.com",             False),
    (scrape_official,             "official",                False),
    (scrape_vieffetrade,          "vieffetrade.com",         False),
    (scrape_yeppon,               "yeppon.it",               True),
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

        # One headless-Chromium retry for sites that requests couldn't crack
        if not results and site_name in PLAYWRIGHT_FALLBACKS:
            try:
                results, rendered = _playwright_fallback(site_name)
            except Exception as exc:
                log.error("  [%s] Playwright fallback raised: %s", site_name, exc)
                results, rendered = [], False
            if results:
                status = "playwright"
            elif rendered:
                status = "empty"
                log.info("  [%s] reachable via browser but no products listed.", site_name)
            else:
                status = "blocked"
                log.warning("  [%s] still blocked after Playwright attempt.", site_name)
        elif not results and site_name in NO_SHOP_SITES:
            status = "no_shop"

        run_summary.append({"site": site_name, "found": len(results), "status": status})
        all_products.extend(results)
        time.sleep(random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX))

    if not all_products:
        log.warning("No products collected — check scraper selectors.")
    else:
        save_to_csv(all_products)
        update_stock_history(all_products)
        write_latest(all_products)
        write_price_history()
        upload_to_sheets(all_products)

    update_site_health(run_summary)
    _print_run_summary(run_summary)
    log.info("Done. Total products collected: %d", len(all_products))


if __name__ == "__main__":
    main()
