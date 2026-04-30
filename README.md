# InSinkErator Italy Price Tracker

Scrapes prices for InSinkErator waste-disposal units from five Italian retail
and price-comparison sites, saves results to a local CSV, and syncs to Google
Sheets automatically every day via GitHub Actions.

## Sites scraped

| Site | Type |
|---|---|
| trovaprezzi.it | Price comparison |
| trovaincasso.it | Specialist appliance retailer |
| pentoleprofessionali.it | Professional kitchenware |
| leroymerlin.it | Home improvement retail |
| amazon.it | Marketplace |

---

## Local setup

### 1. Clone and install

```bash
git clone <your-repo-url>
cd rieko-tracker
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Set up a Google Cloud service account

You need a service account with Sheets + Drive access to write to Google Sheets.
If you only want the CSV output, skip steps 2–4.

1. Go to [Google Cloud Console](https://console.cloud.google.com/) and create (or select) a project.
2. Enable these two APIs:
   - **Google Sheets API**
   - **Google Drive API**
3. Navigate to **IAM & Admin → Service Accounts → Create Service Account**.
   - Give it any name (e.g. `price-tracker`).
   - No special roles are required at project level.
4. Open the new service account, go to **Keys → Add Key → Create new key → JSON**.
   Download the file and save it as **`service_account.json`** in the project root.
   > **Never commit this file.** It is already listed in `.gitignore`.

### 3. Share your Google Sheet with the service account

1. Create a new Google Sheet (or open an existing one).
2. Copy the Sheet ID from the URL:
   `https://docs.google.com/spreadsheets/d/**<SHEET_ID>**/edit`
3. Click **Share** and add the service account's email address
   (found in `service_account.json` under `"client_email"`) with **Editor** access.

### 4. Configure environment variables

Create a `.env` file or export the variables in your shell:

```bash
export GOOGLE_SHEET_ID="your_sheet_id_here"
export GOOGLE_CREDENTIALS_FILE="service_account.json"   # default; can omit
```

Alternatively, set `GOOGLE_CREDENTIALS_JSON` to the raw JSON string of your
service account file (used by GitHub Actions — see below).

---

## Running the scraper

```bash
python scraper.py
```

Results are appended to `prices.csv` and uploaded to Google Sheets (two tabs):
- **Raw Data** — every row ever scraped, appended daily.
- **Latest** — only today's prices, overwritten each run.

## Generating a report

```bash
python report.py
```

Prints to stdout:
- Cheapest price per model today
- Average price per model across all historical data
- Most price-competitive site (ranked by average premium over the cheapest price)

---

## GitHub Actions (automated daily run)

The workflow in `.github/workflows/scraper.yml` runs `scraper.py` every day at
**07:00 UTC** and commits the updated `prices.csv` back to the repository.

### Required secrets

Go to your repository → **Settings → Secrets and variables → Actions** and add:

| Secret | Value |
|---|---|
| `GOOGLE_CREDENTIALS_JSON` | Paste the **entire JSON content** of your `service_account.json` file |
| `GOOGLE_SHEET_ID` | Your Google Sheet ID (from the URL) |

The workflow also needs **write permission** to push the updated CSV.
Go to **Settings → Actions → General → Workflow permissions** and select
**"Read and write permissions"**.

---

## Project structure

```
rieko-tracker/
├── scraper.py              # main scraper
├── report.py               # price analysis report
├── prices.csv              # output data (git-tracked)
├── requirements.txt
├── service_account.json    # NOT committed — add to .gitignore
├── README.md
└── .github/
    └── workflows/
        └── scraper.yml     # daily automation
```

## Notes on scraper reliability

- Site HTML structures change over time. If a scraper returns 0 results for a
  site, inspect the page source and update the CSS selectors in `scraper.py`.
- Amazon.it has stronger anti-scraping measures. If it consistently returns 0
  results, consider switching to the
  [Product Advertising API](https://affiliate-program.amazon.com/gp/advertising/api/detail/main.html)
  or a headless browser (Playwright/Selenium).
- A 2-second delay is added between each site to avoid rate-limiting.
