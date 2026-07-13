#!/usr/bin/env python3
"""
run_batch.py - batch-check a list of properties for tax delinquency and
refresh the dashboard.

USAGE
-----
    python run_batch.py --input input/geo_ids.csv

Your input CSV just needs ONE required column identifying the property -
either your GEO ID or a PropID both work, and the checker auto-detects
which one it's looking at. Column name matching is case-insensitive and
flexible - any of these work:
    Geo ID, GEO ID, PropID, Prop ID, CAD Reference, CAD Reference No,
    CAD Ref, Appraisal District Number, Account No, Account Number

NOTE - the CAD portal's "Ref ID" column (a plain number that looks similar
to PropID, e.g. 160212) is a DIFFERENT ID space from both of the above and
won't match - see hidalgo_tax_checker.py header comment for the full
explanation of which ID is which.

Optional columns, if present, get carried straight through to the dashboard
so you have owner/address context without a second lookup:
    Owner Name, Property Address, City

OUTPUT
------
    dashboard/data.json    <- powers the live dashboard (dashboard/index.html)
    output/results.csv     <- flat CSV, one row per property, for CRM import
"""

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from hidalgo_tax_checker import HidalgoTaxChecker

COLUMN_ALIASES = {
    "geo id": "cad_reference",
    "geoid": "cad_reference",
    "geo_id": "cad_reference",
    "propid": "cad_reference",
    "prop id": "cad_reference",
    "cad reference": "cad_reference",
    "cad reference no": "cad_reference",
    "cad reference no.": "cad_reference",
    "cad ref": "cad_reference",
    "cad_reference": "cad_reference",
    "appraisal district number": "cad_reference",
    "account no": "cad_reference",
    "account no.": "cad_reference",
    "account number": "cad_reference",
    "owner name": "owner",
    "owner": "owner",
    "property address": "address",
    "address": "address",
    "city": "city",
}
# NOTE: the field is still internally called "cad_reference" for backward
# compatibility, but it now just means "whatever ID identifies the property -
# GEO ID, account number, or PropID" - hidalgo_tax_checker.py auto-routes it.


def load_input_rows(csv_path: Path) -> list[dict]:
    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ValueError("Input CSV appears to be empty.")

        field_map = {}
        for original in reader.fieldnames:
            key = original.strip().lower()
            if key in COLUMN_ALIASES:
                field_map[original] = COLUMN_ALIASES[key]

        if "cad_reference" not in field_map.values():
            ref_id_present = any("ref id" in c.lower() or "refid" in c.lower() for c in reader.fieldnames)
            hint = (
                "\n\nI see a 'Ref ID' style column, but that's a different ID space "
                "from what the tax site needs - use a GEO ID or PropID column instead."
                if ref_id_present else ""
            )
            raise ValueError(
                f"Couldn't find a GEO ID / PropID / Account No. column in {csv_path.name}. "
                f"Columns found: {reader.fieldnames}{hint}"
            )

        rows = []
        for raw_row in reader:
            row = {}
            for original, value in raw_row.items():
                mapped_key = field_map.get(original)
                if mapped_key:
                    row[mapped_key] = (value or "").strip()
            if row.get("cad_reference"):
                rows.append(row)
        return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", required=True, help="Path to input CSV of properties to check")
    parser.add_argument("--dashboard-dir", default="dashboard", help="Where to write data.json")
    parser.add_argument("--output-dir", default="output", help="Where to write results.csv")
    parser.add_argument("--delay", type=float, default=0.8, help="Seconds to wait between requests (be polite to the county server)")
    parser.add_argument("--limit", type=int, default=None, help="Only process the first N rows (useful for testing)")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    rows = load_input_rows(input_path)
    if args.limit:
        rows = rows[: args.limit]

    if not rows:
        print("No valid rows with a PropID/CAD Reference found in the input CSV.", file=sys.stderr)
        sys.exit(1)

    print(f"Checking {len(rows)} propert{'y' if len(rows)==1 else 'ies'} against the Hidalgo tax site...")

    checker = HidalgoTaxChecker(delay_seconds=args.delay)
    results = []
    for i, row in enumerate(rows, start=1):
        cad_ref = row["cad_reference"]
        record = checker.check(
            cad_ref,
            source_owner=row.get("owner", ""),
            source_address=row.get("address", ""),
            source_city=row.get("city", ""),
        )
        results.append(record)
        status_note = record.status
        print(f"  [{i}/{len(rows)}] {cad_ref} -> {status_note}")

    # ---- write dashboard/data.json ----
    dashboard_dir = Path(args.dashboard_dir)
    dashboard_dir.mkdir(parents=True, exist_ok=True)

    delinquent = [r for r in results if r.status.startswith("delinquent")]
    total_owed = sum(r.total_amount_due for r in results)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total_checked": len(results),
            "delinquent_count": len(delinquent),
            "not_found_count": sum(1 for r in results if r.status == "not_found"),
            "error_count": sum(1 for r in results if r.status == "error"),
            "total_amount_due": round(total_owed, 2),
            "total_delinquent_amount": round(sum(r.total_amount_due for r in delinquent), 2),
        },
        "properties": [r.to_dict() for r in results],
    }

    data_json_path = dashboard_dir / "data.json"
    data_json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nWrote {data_json_path}")

    # ---- write output/results.csv ----
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    results_csv_path = output_dir / "results.csv"

    fieldnames = [
        "cad_reference", "account_number", "status", "owner_name", "source_owner",
        "property_site_address", "source_address", "source_city", "legal_description",
        "current_tax_levy", "current_amount_due", "prior_year_amount_due", "total_amount_due",
        "last_payment_amount", "last_payment_date", "active_lawsuits",
        "gross_value", "land_value", "improvement_value", "exemptions",
        "match_count", "error", "checked_at",
    ]
    with results_csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for r in results:
            writer.writerow(r.to_dict())
    print(f"Wrote {results_csv_path}")

    print(
        f"\nSummary: {len(results)} checked | {len(delinquent)} delinquent "
        f"| ${payload['summary']['total_delinquent_amount']:,.2f} total delinquent owed"
    )


if __name__ == "__main__":
    main()
