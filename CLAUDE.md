# rieko-tracker — InSinkErator Italy Price Tracker

Daily price/stock tracker for InSinkErator food-waste disposals across Italian
retailers. A GitHub Actions cron (07:00 UTC) runs `scraper.py`, commits the
data files, and `index.html` (GitHub Pages) renders the dashboard from the
Google Sheets "Latest" tab plus the JSON artifacts in this repo.

## Commands

```bash
python3 scraper.py                 # full run: scrape all sites, write CSV/JSON, upload to Sheets
python3 -c "import scraper; print(scraper.scrape_yeppon())"   # test one site
```

Sheets upload is skipped unless `GOOGLE_SHEET_ID` is set (CI sets it from
secrets; locally it defaults to empty, so local runs only write files).

## Architecture (scraper.py)

- **Extraction strategy per site, cheapest reliable channel first:**
  - *Shopify stores* (climaconvenienza, caldaiemurali, bricobravo, yeppon):
    `scrape_shopify()` merges `/collections/tritarifiuti/products.json` with
    `/search/suggest.json` (limit 10/query) — no HTML parsing. The `available`
    field gives a definitive `in_stock`/`out_of_stock`.
  - *WooCommerce* (tritarifiutidomesticoservice): Store API
    `/wp-json/wc/store/v1/products?search=…`; prices come in minor units
    (`currency_minor_unit`), stock from `is_in_stock`.
  - *PrestaShop* (lineadaincasso, opportunitycommerce, kelsostore): shared
    `_parse_prestashop_cards()` HTML parser.
  - *Magento/HTML* (trovaincasso, pentoleprofessionali, vieffetrade, mk2shop,
    amazon): per-site `_parse_*()` functions.
- **Playwright fallback**: any site returning 0 products via requests gets ONE
  headless-Chromium retry (`PLAYWRIGHT_FALLBACKS` maps site → url/selector/
  parser). `_looks_blocked()` detects challenge pages so "blocked" is
  distinguished from "empty" (reachable, no matching products). No CAPTCHA
  solving or proxies — still-blocked sites are logged and skipped.
- **Registry**: `SCRAPERS` list of `(fn, site_name, apply_retry)`;
  `TYPICAL_COUNTS` drives the low-count warning in the run summary.
- **Accessory filter**: `is_accessory(name, price)` — an accessory word alone
  is not enough: names containing a disposal word (e.g. "Dissipatore … con
  tappo salvaposate") are bundles, kept when priced ≥ €150
  (`_ACCESSORY_PRICE_CEILING`). index.html mirrors this logic
  (`DISPOSAL_RE`/`ACCESSORY_PRICE_CEILING`) — keep the two in sync.

## Data files (committed by CI)

| file | purpose |
|---|---|
| `prices.csv` | append-only rows: date, site, product_name, price_eur, url, stock_status |
| `site_health.json` | per-site last_run / last_success / product_count / status — rendered as the "data health" panel |
| `stock_state.json` | last known in/out-of-stock per `site\|product_name` (previous-run baseline) |
| `stock_history.json` | append-only in/out-of-stock transitions — rendered as per-model "Stock history" |

`stock_state.json` is seeded from `prices.csv` history on first run
(`_seed_stock_from_csv`), so deleting it regenerates state AND re-derives
historical transitions. `stock_status` of `unknown` (HTML sites with no
signal) is treated as `in_stock` for history purposes.

`.gitignore` blocks `*.json` (protects `service_account.json` — a real
credential, never commit it) with explicit `!` exceptions for the three data
files above. Whitelist any new JSON artifact there AND in the workflow's
`git add` line.

## Site quirks

- **climaconvenienza.it / caldaiemurali.it** share one Shopify catalog
  (identical prices; caldaiemurali lists a couple more models). Both tracked.
- **yeppon.it**: HTML is Cloudflare-403'd but its Shopify JSON endpoints
  respond normally.
- **vieffetrade.com**: only works via Playwright; uses Adobe Commerce
  `ds-sdk-product-item` widgets; price cells contain old+discount+final price,
  `_last_price()` takes the final one.
- **unieuro.it / eprice.it / leroymerlin.it**: hard-blocked (SPA-404 / Akamai /
  DataDome) even via Playwright → status "blocked".
- **mediaworld.it / mk2shop.com**: reachable, but currently list no
  InSinkErator products → status "empty".
- **official** (insinkerator.com/it-it): redirects to a distributor page for
  rieko.it, which has no e-commerce → status "no_shop". Rows would be labelled
  site "official" if a shop ever appears.
- **plumbingshop.it**: NXDOMAIN since 2026-05; kept for auto-recovery.

## Gotchas

- Local dev machine runs Python 3.9 — don't use 3.10+ syntax (match, `X | Y`
  type unions).
- `parse_price` handles Italian formats ("1.299,99"); Shopify/Woo JSON prices
  are plain decimals and pass through fine.
- Amazon search includes non-InSinkErator items; the dashboard filters to
  InSinkErator-branded names client-side.
- index.html loads prices from Google Sheets (`Latest` tab) but
  health/history from repo-relative JSON — both must be deployed together.
