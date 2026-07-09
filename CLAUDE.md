# rieko-tracker — InSinkErator Italy Price Tracker

Daily price/stock tracker for InSinkErator products (disposals + hot-water
taps) across Italian retailers. A GitHub Actions cron (07:00 UTC) runs
`scraper.py`, commits the data files, and `index.html` (GitHub Pages) renders
the dashboard entirely from the repo-relative JSON artifacts — it has no
Google Sheets dependency (Sheets upload still happens, for spreadsheet users).

## Commands

```bash
python3 scraper.py                 # full run: scrape all sites, write CSV/JSON, upload to Sheets
python3 -c "import scraper; print(scraper.scrape_yeppon())"   # test one site
python3 -m http.server 8080        # then open http://localhost:8080/index.html to view the dashboard
```

Sheets upload is skipped unless `GOOGLE_SHEET_ID` is set (CI sets it from
secrets; locally it defaults to empty, so local runs only write files).

## Architecture (scraper.py)

- **Extraction strategy per site, cheapest reliable channel first:**
  - *Shopify stores* (climaconvenienza, caldaiemurali, bricobravo, yeppon):
    `scrape_shopify()` merges `/collections/{tritarifiuti,insinkerator}/products.json`
    with `/search/suggest.json` (limit 10/query) — no HTML parsing. The
    `available` field gives a definitive `in_stock`/`out_of_stock`.
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
- **Row filter (`make_row`)**: drops rows whose name doesn't contain
  "insinkerator" (every site is queried for the brand, so unbranded hits are
  other vendors), accessories per `is_accessory`, and unidentified items
  priced < €150 (spare parts sold by article number only).
- **Accessory filter**: `is_accessory(name, price)` — an accessory word alone
  is not enough: names containing a disposal word (e.g. "Dissipatore … con
  tappo salvaposate") are bundles, kept when priced ≥ €150
  (`_ACCESSORY_PRICE_CEILING`).
- **Model canonicalization — single source of truth**: `canonical_model(name)`
  maps retailer names to canonical models via `ARTICLE_MODELS` (InSinkErator
  article numbers, checked first — e.g. trovaincasso labels article 1974550
  "Evolution Plus 550 EC" but it IS the Premium 550 EC) then ordered
  `MODEL_NAME_RULES` regexes. Categories: disposer / tap / other.
  `MODEL_SPECS` holds the dashboard spec lines. index.html does NOT re-derive
  models — it reads the `model`/`category` fields from `latest.json`,
  `price_history.json` and `stock_history.json`.

## Data files (committed by CI)

| file | purpose |
|---|---|
| `prices.csv` | one row per listing per day: date, site, product_name, price_eur, url, stock_status. Same-day re-runs REPLACE that day's rows for the scraped sites (idempotent), not append |
| `latest.json` | today's rows + `model`/`category` + `MODEL_SPECS` — the dashboard's primary data |
| `price_history.json` | per model → per site → [[date, min price], …]; REBUILT from prices.csv every run (rule improvements retroactively fix history); excludes out-of-stock listings |
| `site_health.json` | per-site last_run / last_success / product_count / status — the "data health" panel |
| `stock_state.json` | last known in/out-of-stock per `site\|product_name` (previous-run baseline) |
| `stock_history.json` | append-only in/out-of-stock transitions with `model` field (backfilled every run) — per-model "Storico disponibilità" |

`stock_state.json` is seeded from `prices.csv` history on first run
(`_seed_stock_from_csv`), so deleting it regenerates state AND re-derives
historical transitions. `stock_status` of `unknown` (HTML sites with no
signal) is treated as `in_stock` for history purposes.

`.gitignore` blocks `*.json` (protects `service_account.json` — a real
credential, never commit it) with explicit `!` exceptions for the data files
above. Whitelist any new JSON artifact there AND in the workflow's
`git add` line.

## Dashboard (index.html)

- Renders ONLY from repo-relative JSON (`latest.json`, `price_history.json`,
  `stock_history.json`, `site_health.json`) — no Sheets, no external data.
- Sections by category; models sorted by popularity (# sites listing today,
  then # listings, then price). Cards: per-site price rows with stock badges
  ("Esaurito dal <date>", "Esaurito ovunque" banner when no site has stock),
  a cheapest-price sparkline (per-site series forward-filled ≤7 days so
  missed scrapes don't fake price changes), and recent stock transitions.
- Colors follow the dataviz reference palette (validated); site→slot map in
  `SITE_SLOTS` — first 8 sites by popularity get hues, the rest neutral gray
  (site names are always printed beside marks). Dark mode via
  `prefers-color-scheme`.

## Site quirks

- **climaconvenienza.it / caldaiemurali.it** share one Shopify catalog
  (identical prices; caldaiemurali lists a couple more models). Both tracked.
- **yeppon.it**: HTML is Cloudflare-403'd but its Shopify JSON endpoints
  respond normally.
- **vieffetrade.com**: only works via Playwright; uses Adobe Commerce
  `ds-sdk-product-item` widgets; price cells contain old+discount+final price,
  `_last_price()` takes the final one.
- **lineadaincasso.it**: card titles are truncated ("Insinkerator 1975556…") —
  article numbers carry the model mapping.
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
- Amazon search returns many non-InSinkErator items — filtered out at scrape
  time by the brand check in `make_row` (typical branded count ~13).
- Product dicts must keep exactly the `FIELDNAMES` keys (csv.DictWriter and
  the Sheets upload both break on extras) — `model`/`category` are attached
  only at JSON-write time (`_dashboard_row`).
