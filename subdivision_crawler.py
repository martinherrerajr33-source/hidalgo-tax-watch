"""
subdivision_crawler.py

Gradual, hands-off crawler that works through every subdivision/abstract code
in Hidalgo CAD's public property-search index, pulls the property list for
each one directly from the CAD's own (public, anonymous) API, checks each
new property's tax-delinquency status against actweb.acttax.com using the
exact same logic as hidalgo_tax_checker.py, and appends only the
delinquent / current_due properties to input/geo_ids.csv.

This replaces the manual "export .xlsx from the CAD portal, upload it"
workflow with something that can run unattended on a schedule, a handful of
subdivisions/properties at a time, until it has eventually covered the whole
county.

HOW IT TALKS TO THE CAD PORTAL
-------------------------------
hidalgo.prodigycad.com (Hidalgo County Appraisal District's public property
search) is a single-page app backed by prod-container.trueprodigyapi.com
(a "TrueProdigy" CAD platform). The API allows fully anonymous, public
access - confirmed live:

  1. POST /trueprodigy/cadpublic/auth/token   body: {"office": "Hidalgo"}
     -> {"user": {"token": "<JWT>"}}
     A JWT scoped to office=Hidalgo, valid ~5 minutes, no credentials needed.

  2. GET /public/config/propertysearchadvanced   header: Authorization: <JWT>
     -> {"results": {"0": {...}, "1": {...}, ...}}
     Field "1" (id: "abstractSubdivisionName") has a "codefile" array with
     every subdivision/abstract code in the county (~11,839 entries as of
     2026), each shaped {"codeName": "...", "codeDescription": "..."}.
     codeName is what you feed back into the search below. In practice it's
     almost always the property's Geo ID prefix (first 5 characters) + "00",
     e.g. Geo IDs starting "N0400-" belong to codeName "N040000"
     ("NELCO - SDN"); Geo IDs starting "A2100-" belong to "A210000"
     ("ALAMO TOWNSITE - SPA"). Confirmed against real subdivisions already
     tracked in this project.

  3. POST /public/property/advancedsearch   header: Authorization: <JWT>
     body: {"advanced": true, "pYear": {"operator": "=", "value": "<year>"},
            "sortOrder": "geoID",
            "abstractSubdivisionName": {"operator": "in", "value": ["<codeName>"]}}
     -> {"results": [{"geoID": "...", "name": "...", "streetPrimary": "...",
                       "city": ..., ...}, ...]}
     Returns every property in that subdivision/abstract in one shot (no
     pagination cap seen up to 930 results for Alamo Townsite) or HTTP 204
     with no body if the code has zero properties for that year (many of
     the ~11,839 codes are legacy/unused abstract codes with nothing on
     them for the current roll year).

PROGRESS TRACKING
------------------
input/subdivision_progress.json keeps the list of codeNames already
processed (so a code is never re-queried/re-checked once it's been fully
handled) plus some running counters. On first run it also seeds itself with
every codeName derivable from Geo IDs already present in input/geo_ids.csv
(prefix + "00"), so subdivisions already covered by earlier manual batches
aren't pointlessly re-crawled.

BUDGET PER RUN
--------------
Each run processes whole subdivisions (never stops partway through one) up
to a properties-checked budget (--max-properties, default 400) so a daily
scheduled run finishes in a reasonable time and doesn't hammer either the
CAD portal or the county tax site. At ~2 seconds per property checked
(two HTTP calls + politeness delay, per hidalgo_tax_checker.py), 400
properties is roughly 15-20 minutes.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

from hidalgo_tax_checker import HidalgoTaxChecker

TRUEPRODIGY_BASE = "https://prod-container.trueprodigyapi.com"
TOKEN_URL = f"{TRUEPRODIGY_BASE}/trueprodigy/cadpublic/auth/token"
CONFIG_URL = f"{TRUEPRODIGY_BASE}/public/config/propertysearchadvanced"
SEARCH_URL = f"{TRUEPRODIGY_BASE}/public/property/advancedsearch"

OFFICE = "Hidalgo"
TOKEN_MAX_AGE_SECONDS = 240  # refresh a bit before the ~300s expiry

GEO_IDS_CSV = os.path.join("input", "geo_ids.csv")
PROGRESS_JSON = os.path.join("input", "subdivision_progress.json")

CSV_HEADER = ["Geo ID", "Owner Name", "Property Address", "City"]


class TokenManager:
    def __init__(self):
        self._token = None
        self._obtained_at = 0.0

    def get(self) -> str:
        now = time.time()
        if self._token is None or (now - self._obtained_at) > TOKEN_MAX_AGE_SECONDS:
            resp = requests.post(
                TOKEN_URL,
                json={"office": OFFICE},
                timeout=20,
            )
            resp.raise_for_status()
            self._token = resp.json()["user"]["token"]
            self._obtained_at = now
        return self._token


def fetch_subdivision_codes(tokens: TokenManager) -> list[dict]:
    """Returns the full list of {"codeName": ..., "codeDescription": ...} entries."""
    resp = requests.get(CONFIG_URL, headers={"Authorization": tokens.get()}, timeout=30)
    resp.raise_for_status()
    cfg = resp.json()
    results = cfg["results"]
    # "results" has been observed as both a JSON array and as an object keyed by
    # stringified indices ("0", "1", ...) depending on how it's serialized -
    # normalize to an iterable of field dicts either way.
    fields = results.values() if isinstance(results, dict) else results
    field = next((f for f in fields if f.get("id") == "abstractSubdivisionName"), None)
    if field is None:
        raise RuntimeError(
            "Could not find 'abstractSubdivisionName' field in propertysearchadvanced "
            "config - the CAD API layout may have changed."
        )
    return field["codefile"]


def search_subdivision(tokens: TokenManager, code_name: str, year: str) -> list[dict]:
    """Returns the list of property dicts for one subdivision/abstract code."""
    resp = requests.post(
        SEARCH_URL,
        headers={"Authorization": tokens.get(), "Content-Type": "application/json"},
        json={
            "advanced": True,
            "pYear": {"operator": "=", "value": year},
            "sortOrder": "geoID",
            "abstractSubdivisionName": {"operator": "in", "value": [code_name]},
        },
        timeout=30,
    )
    if resp.status_code == 204:
        return []
    resp.raise_for_status()
    if not resp.text.strip():
        return []
    return resp.json().get("results", [])


def load_existing_geo_ids() -> set[str]:
    ids = set()
    if os.path.exists(GEO_IDS_CSV):
        with open(GEO_IDS_CSV, newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            next(reader, None)  # header
            for row in reader:
                if row:
                    ids.add(row[0].strip())
    return ids


def derive_code_from_geo_id(geo_id: str) -> str | None:
    geo_id = geo_id.strip()
    if len(geo_id) < 5:
        return None
    return geo_id[:5] + "00"


def load_progress(existing_geo_ids: set[str]) -> dict:
    if os.path.exists(PROGRESS_JSON):
        with open(PROGRESS_JSON, encoding="utf-8") as f:
            progress = json.load(f)
        progress.setdefault("processed_codes", [])
        progress.setdefault("total_properties_checked", 0)
        progress.setdefault("total_added", 0)
        progress.setdefault("seeded_from_existing_csv", False)
    else:
        progress = {
            "processed_codes": [],
            "total_properties_checked": 0,
            "total_added": 0,
            "seeded_from_existing_csv": False,
        }

    if not progress["seeded_from_existing_csv"]:
        derived = {derive_code_from_geo_id(g) for g in existing_geo_ids}
        derived.discard(None)
        combined = set(progress["processed_codes"]) | derived
        progress["processed_codes"] = sorted(combined)
        progress["seeded_from_existing_csv"] = True
        print(
            f"Seeded progress file with {len(derived)} subdivision codes derived "
            f"from {len(existing_geo_ids)} already-tracked Geo IDs."
        )

    return progress


def save_progress(progress: dict) -> None:
    progress["last_run_utc"] = datetime.now(timezone.utc).isoformat()
    os.makedirs(os.path.dirname(PROGRESS_JSON), exist_ok=True)
    with open(PROGRESS_JSON, "w", encoding="utf-8") as f:
        json.dump(progress, f, indent=2)


def append_rows_to_csv(rows: list[list[str]]) -> None:
    if not rows:
        return
    file_exists = os.path.exists(GEO_IDS_CSV)
    os.makedirs(os.path.dirname(GEO_IDS_CSV), exist_ok=True)
    with open(GEO_IDS_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(CSV_HEADER)
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-properties", type=int, default=400,
                         help="Budget of new properties to tax-check this run (default 400).")
    parser.add_argument("--year", default="2026", help="Appraisal year to query (default 2026).")
    parser.add_argument("--max-subdivisions", type=int, default=None,
                         help="Optional hard cap on number of subdivisions to process this run, "
                              "regardless of property budget (mainly for testing).")
    args = parser.parse_args()

    tokens = TokenManager()
    existing_geo_ids = load_existing_geo_ids()
    progress = load_progress(existing_geo_ids)
    processed_set = set(progress["processed_codes"])

    print(f"Already tracked Geo IDs: {len(existing_geo_ids)}")
    print(f"Subdivision codes already processed: {len(processed_set)}")

    print("Fetching full subdivision/abstract code list from CAD portal...")
    all_codes = fetch_subdivision_codes(tokens)
    print(f"Total subdivision/abstract codes in county index: {len(all_codes)}")

    remaining = [c for c in all_codes if c["codeName"] not in processed_set]
    print(f"Remaining unprocessed codes: {len(remaining)}")

    if not remaining:
        print("Nothing left to crawl - the full county index has been processed.")
        save_progress(progress)
        return

    checker = HidalgoTaxChecker()
    new_rows: list[list[str]] = []
    properties_checked_this_run = 0
    subdivisions_processed_this_run = 0
    subdivisions_with_new_matches = 0

    for code in remaining:
        if properties_checked_this_run >= args.max_properties:
            print(f"Hit property budget ({args.max_properties}) - stopping for this run.")
            break
        if args.max_subdivisions is not None and subdivisions_processed_this_run >= args.max_subdivisions:
            print(f"Hit subdivision cap ({args.max_subdivisions}) - stopping for this run.")
            break

        code_name = code["codeName"]
        description = code.get("codeDescription") or ""

        try:
            properties = search_subdivision(tokens, code_name, args.year)
        except requests.RequestException as exc:
            print(f"  [{code_name}] request failed, will retry another day: {exc}")
            continue  # do NOT mark as processed - try again next run

        subdivisions_processed_this_run += 1
        processed_set.add(code_name)

        new_in_this_code = [p for p in properties if p.get("geoID") and p["geoID"] not in existing_geo_ids]
        if not properties:
            continue
        if not new_in_this_code:
            continue

        print(f"  [{code_name}] {description!r}: {len(properties)} properties, "
              f"{len(new_in_this_code)} not yet tracked - checking delinquency...")

        matches_here = 0
        for prop in new_in_this_code:
            if properties_checked_this_run >= args.max_properties:
                break
            geo_id = prop["geoID"]
            record = checker.check(
                geo_id,
                source_owner=prop.get("name") or "",
                source_address=prop.get("streetPrimary") or "",
                source_city=prop.get("city") or "",
            )
            properties_checked_this_run += 1
            base_status = record.status.replace("_multi_match", "")
            if base_status in ("delinquent", "current_due"):
                new_rows.append([
                    geo_id,
                    prop.get("name") or "",
                    prop.get("streetPrimary") or "",
                    prop.get("city") or "",
                ])
                existing_geo_ids.add(geo_id)
                matches_here += 1

        if matches_here:
            subdivisions_with_new_matches += 1
            print(f"    -> {matches_here} delinquent/current_due, added to geo_ids.csv")

    progress["processed_codes"] = sorted(processed_set)
    progress["total_properties_checked"] += properties_checked_this_run
    progress["total_added"] += len(new_rows)

    append_rows_to_csv(new_rows)
    save_progress(progress)

    print("---")
    print(f"Subdivisions processed this run: {subdivisions_processed_this_run}")
    print(f"Subdivisions with new delinquent/current_due matches: {subdivisions_with_new_matches}")
    print(f"Properties tax-checked this run: {properties_checked_this_run}")
    print(f"New rows appended to geo_ids.csv: {len(new_rows)}")
    print(f"Total subdivision codes processed so far: {len(processed_set)} / {len(all_codes)}")
    remaining_after = len(all_codes) - len(processed_set)
    print(f"Remaining subdivision codes for future runs: {remaining_after}")


if __name__ == "__main__":
    main()
