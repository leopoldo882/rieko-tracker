"""
InSinkErator Price Report
Reads prices.csv and prints:
  - Cheapest price per model today
  - Average price per model across all sites
  - Most price-competitive site overall
"""

import csv
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

CSV_FILE = "prices.csv"
TODAY = date.today().isoformat()


def load_rows(filepath: str) -> list[dict]:
    if not Path(filepath).exists():
        print(f"[error] {filepath} not found — run scraper.py first.")
        sys.exit(1)

    rows = []
    with open(filepath, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                row["price_eur"] = float(row["price_eur"])
                rows.append(row)
            except (ValueError, KeyError):
                continue  # skip malformed rows
    return rows


def normalize(name: str) -> str:
    """Lowercase + strip for grouping product names that may differ slightly."""
    return name.strip().lower()


def report(filepath: str = CSV_FILE) -> None:
    rows = load_rows(filepath)
    today_rows = [r for r in rows if r["date"] == TODAY]

    if not today_rows:
        print(f"No data found for today ({TODAY}) in {filepath}.")
        print(f"Total rows in file: {len(rows)}")
        return

    # ── 1. Cheapest price per model today ─────────────────────────────────────
    cheapest: dict[str, dict] = {}
    for r in today_rows:
        key = normalize(r["product_name"])
        if key not in cheapest or r["price_eur"] < cheapest[key]["price_eur"]:
            cheapest[key] = r

    print(f"\n{'='*70}")
    print(f"  InSinkErator Price Report — {TODAY}")
    print(f"{'='*70}")

    print(f"\n{'─'*70}")
    print("  CHEAPEST PRICE PER MODEL TODAY")
    print(f"{'─'*70}")
    for key in sorted(cheapest):
        r = cheapest[key]
        print(f"  {r['product_name'][:45]:<45}  €{r['price_eur']:>8.2f}  ({r['site']})")

    # ── 2. Average price per model across all sites (all history) ─────────────
    model_prices: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        model_prices[normalize(r["product_name"])].append(r["price_eur"])

    print(f"\n{'─'*70}")
    print("  AVERAGE PRICE PER MODEL (ALL HISTORY)")
    print(f"{'─'*70}")
    for key in sorted(model_prices):
        prices = model_prices[key]
        avg = sum(prices) / len(prices)
        # Use the most recent display name for this key
        display = next(
            r["product_name"] for r in reversed(rows) if normalize(r["product_name"]) == key
        )
        print(f"  {display[:45]:<45}  €{avg:>8.2f}  (n={len(prices)})")

    # ── 3. Most competitive site today ────────────────────────────────────────
    # "Most competitive" = site whose prices are on average lowest relative to
    # the cheapest available price for each model it lists.
    site_deltas: dict[str, list[float]] = defaultdict(list)
    for r in today_rows:
        key = normalize(r["product_name"])
        best = cheapest[key]["price_eur"]
        delta_pct = (r["price_eur"] - best) / best * 100
        site_deltas[r["site"]].append(delta_pct)

    print(f"\n{'─'*70}")
    print("  MOST COMPETITIVE SITES TODAY")
    print(f"  (avg % above the cheapest available price per model)")
    print(f"{'─'*70}")
    ranked = sorted(
        site_deltas.items(),
        key=lambda kv: sum(kv[1]) / len(kv[1]),
    )
    for i, (site, deltas) in enumerate(ranked, 1):
        avg_delta = sum(deltas) / len(deltas)
        label = " ← most competitive" if i == 1 else ""
        print(f"  {i}. {site:<30}  +{avg_delta:>5.1f}% avg premium{label}")

    print(f"\n{'='*70}\n")


if __name__ == "__main__":
    filepath = sys.argv[1] if len(sys.argv) > 1 else CSV_FILE
    report(filepath)
