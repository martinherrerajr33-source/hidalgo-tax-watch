# Hidalgo Tax Watch

Automated tax delinquency checker for Hidalgo County properties. Give it a
list of Geo IDs, it checks each one against the county's public tax lookup,
and publishes a live dashboard of who's behind on their taxes.

Replaces the manual workflow of pulling a property, opening
`actweb.acttax.com`, typing in the Geo ID, and eyeballing the balance - one
at a time.

## Which ID goes where (confirmed live against the real site)

The tax site has two search fields that both work, for two different kinds
of ID:

| You have...                                  | Example                | Use this search field |
|-----------------------------------------------|-------------------------|------------------------|
| **Geo ID** (from CAD, DealMachine, etc.)       | `C1622-00-000-0015-00`  | "Account No." — takes the dashes and all |
| **Account Number** (plain, no dashes)          | `A100002000000100`      | "Account No." |
| **PropID** (from the CAD portal's export)      | `109048`                | "CAD Reference No." |

The CAD portal's "**Ref ID**" column (e.g. `160212`) is a different ID space
from all three of the above and won't match either field.

**This tool auto-detects which one you're feeding it** - anything with a
letter in it is treated as a Geo ID/account number, pure digits are treated
as a PropID - and routes to the right field automatically, with a fallback
to the other field if the first guess comes back empty. So in practice: just
put in whatever ID you already have on hand. You don't need to go get PropID
from the CAD portal unless that's just what you happen to have.

## What's in here

- `hidalgo_tax_checker.py` - the scraper/parser. Plain `requests`, no
  browser needed (the tax site turned out to be fully stateless - no login,
  no session cookies).
- `run_batch.py` - CLI that reads a CSV of Geo IDs, checks each one, and
  writes `dashboard/data.json` + `output/results.csv`.
- `dashboard/index.html` - the live dashboard. Single file, no build step.
- `.github/workflows/check_delinquency.yml` - runs the check daily, and
  publishes the dashboard to GitHub Pages automatically.
- `input/geo_ids_template.csv` - example of the input format.

## Getting a list of properties in

Two ways, both land in the same `input/geo_ids.csv`:

1. **From the CAD portal (pull by area):** go to
   [hidalgo.prodigycad.com/property-search](https://hidalgo.prodigycad.com/property-search),
   search by street/subdivision/owner (the free-text search doesn't filter
   by zip code directly, since zip isn't part of the indexed address
   string - search by street name, subdivision, or city instead), then hit
   **Export as Excel**. Save/convert it to CSV, keep the `GEO ID` column
   (or `PropID` - either works), and save as `input/geo_ids.csv`.
2. **From any other list you already have:** just build a CSV with a
   `Geo ID` column. Optional `Owner Name`, `Property Address`, `City`
   columns get carried straight into the dashboard.

See `input/geo_ids_template.csv` for the exact shape.

## One-time setup

1. Create a new GitHub repo and push this folder to it.
2. In the repo, go to **Settings → Pages → Source**, and set it to
   **GitHub Actions** (not "Deploy from a branch").
3. Copy `input/geo_ids_template.csv` to `input/geo_ids.csv` and put your
   real properties in it (delete the example rows).
4. Commit/push. The workflow runs automatically (see below), and your
   dashboard will be live at `https://<you>.github.io/<repo-name>/`.

## Running it

- **Automatically:** the workflow runs every morning on its own (edit the
  `cron` line in `.github/workflows/check_delinquency.yml` if you want a
  different time - it's in UTC).
- **On demand, no computer needed:** edit `input/geo_ids.csv` right in the
  GitHub web UI (or drag-and-drop a replacement file) and commit - that
  push automatically kicks off a fresh check.
- **On demand, manual button:** go to the **Actions** tab in your repo →
  "Check Tax Delinquency" → **Run workflow**.
- **Locally, for testing:**
  ```bash
  pip install -r requirements.txt
  python run_batch.py --input input/geo_ids.csv
  # then open dashboard/index.html via a local server, e.g.:
  cd dashboard && python -m http.server 8000
  # visit http://localhost:8000
  ```
  (Opening `index.html` directly as a `file://` URL won't work - browsers
  block `fetch()` of local files. Always serve it, even locally.)

## How "delinquent" is decided

Each property comes back tagged:

- **Delinquent** - `Prior Year Amount Due` > $0 (behind on taxes from a
  previous year - this is the actionable list).
- **Current Due** - this year's taxes aren't paid yet, but nothing's owed
  from prior years.
- **Paid Up** - `Total Amount Due` is $0.
- **Not Found** - neither search field matched. Usually means a typo, a
  brand-new/exempt parcel, or (rarely) a property whose account number
  doesn't follow the usual Geo-ID-derived pattern (some mobile home,
  personal property, or mineral-interest accounts are numbered
  independently).
- **Error** - the request failed (network hiccup, site hiccup); it'll
  retry automatically on the next run.

## Being a good neighbor to the county's server

This hits a small public-sector server, not a CDN. `run_batch.py` waits
~0.8-1.2 seconds between each property by default (`--delay` flag to
adjust) and retries transient failures with backoff. If you're running a
batch of a few thousand at once, consider splitting it across a couple of
scheduled runs rather than firing it all in one go.

## Extending this

- Want GoHighLevel-ready export? `output/results.csv` already has one row
  per property - point a GHL CSV import at it, or add a step to the
  workflow to push straight into your existing Podio/GHL pipeline.
- Want this merged with your existing distressed-property scoring/stacking
  logic? `run_batch.py` is a plain function call
  (`HidalgoTaxChecker().check(geo_id)`) - easy to import into another
  script rather than running standalone.
