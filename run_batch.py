#!/usr/bin/env python3
"""
run_batch.py - check a list of properties for tax delinquency and refresh the
dashboard.  Incremental, parallel and time-boxed so it keeps working as the
list grows to tens of thousands of rows.

USAGE
    python run_batch.py --input input/geo_ids.csv [--workers 6] [--max-minutes 200] [--max-age-days 3]

HOW IT WORKS
  * output/results.csv doubles as the cache of previous results.
  * Work order: properties never checked (or errored) first, then the stalest
    results older than --max-age-days, oldest first.
  * Stops taking new work at --max-minutes; everything else keeps its previous
    result, so the dashboard always shows the full list.
  * Without --max-minutes / --workers it behaves like the old sequential script.

OUTPUT
    dashboard/data.json   <- powers the dashboard
    output/results.csv    <- flat CSV (also the cache)
"""

import argparse
import csv
import json
import re
import sys
import threading
import time
from dataclasses import fields as dc_fields
from datetime import datetime, timezone
from pathlib import Path

from hidalgo_tax_checker import HidalgoTaxChecker, TaxRecord

COLUMN_ALIASES = {
    "geo id": "cad_reference", "geoid": "cad_reference", "geo_id": "cad_reference",
    "propid": "cad_reference", "prop id": "cad_reference",
    "cad reference": "cad_reference", "cad reference no": "cad_reference",
    "cad reference no.": "cad_reference", "cad ref": "cad_reference",
    "cad_reference": "cad_reference", "appraisal district number": "cad_reference",
    "account no": "cad_reference", "account no.": "cad_reference",
    "account number": "cad_reference",
    "owner name": "owner", "owner": "owner",
    "property address": "address", "address": "address", "city": "city",
}

# Geo ID glued to the previous line (missing newline from an old upload).
_GLUED_GEO_RE = re.compile(r"(?<=[^\n])(?=[A-Z0-9]{5}-[0-9A-Z]{2}-[0-9A-Z]{3}-[0-9A-Z]{4}-[0-9A-Z]{2},)")

FLOAT_FIELDS = {"current_tax_levy", "current_amount_due", "prior_year_amount_due",
                "total_amount_due", "gross_value", "land_value", "improvement_value"}
RESULT_COLUMNS = [
    "cad_reference", "account_number", "status", "owner_name", "source_owner",
    "property_site_address", "source_address", "source_city", "legal_description",
    "current_tax_levy", "current_amount_due", "prior_year_amount_due", "total_amount_due",
    "last_payment_amount", "last_payment_date", "active_lawsuits",
    "gross_value", "land_value", "improvement_value", "exemptions",
    "match_count", "error", "checked_at",
]
TAXRECORD_FIELDS = {f.name for f in dc_fields(TaxRecord)}


def load_input_rows(csv_path: Path) -> list[dict]:
    text = csv_path.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    text = _GLUED_GEO_RE.sub("\n", text)
    reader = csv.DictReader(text.split("\n"))
    if not reader.fieldnames:
        raise ValueError("Input CSV appears to be empty.")
    field_map = {o: COLUMN_ALIASES[o.strip().lower()] for o in reader.fieldnames
                 if o and o.strip().lower() in COLUMN_ALIASES}
    if "cad_reference" not in field_map.values():
        raise ValueError(f"No GEO ID / PropID column found in {csv_path.name}. Columns: {reader.fieldnames}")
    rows, seen = [], set()
    for raw in reader:
        row = {field_map[o]: (v or "").strip() for o, v in raw.items() if o in field_map}
        ref = row.get("cad_reference")
        if ref and ref not in seen:
            seen.add(ref)
            rows.append(row)
    return rows


def load_cache(results_csv: Path) -> dict:
    cache = {}
    if not results_csv.exists():
        return cache
    with results_csv.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            ref = r.get("cad_reference")
            if not ref:
                continue
            kwargs = {}
            for k, v in r.items():
                if k not in TAXRECORD_FIELDS or v is None:
                    continue
                if k in FLOAT_FIELDS:
                    try:
                        v = float(v or 0)
                    except ValueError:
                        v = 0.0
                elif k == "match_count":
                    try:
                        v = int(v or 0)
                    except ValueError:
                        v = 0
                kwargs[k] = v
            try:
                rec = TaxRecord(**kwargs)
            except TypeError:
                continue
            rec.found = bool(rec.account_number)
            cache[ref] = rec
    return cache


def parse_ts(ts: str) -> float:
    try:
        return datetime.fromisoformat(ts).timestamp()
    except (ValueError, TypeError):
        return 0.0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", required=True)
    ap.add_argument("--dashboard-dir", default="dashboard")
    ap.add_argument("--output-dir", default="output")
    ap.add_argument("--delay", type=float, default=0.8)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--max-minutes", type=float, default=None)
    ap.add_argument("--max-age-days", type=float, default=3.0)
    args = ap.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)
    rows = load_input_rows(input_path)
    if args.limit:
        rows = rows[: args.limit]
    if not rows:
        print("No valid rows found in the input CSV.", file=sys.stderr)
        sys.exit(1)

    results_csv_path = Path(args.output_dir) / "results.csv"
    cache = load_cache(results_csv_path)
    now = time.time()
    stale_before = now - args.max_age_days * 86400

    fresh_rows = [r for r in rows if r["cad_reference"] not in cache
                  or cache[r["cad_reference"]].status in ("error", "unknown")]
    stale_rows = sorted(
        (r for r in rows if r["cad_reference"] in cache
         and cache[r["cad_reference"]].status not in ("error", "unknown")
         and parse_ts(cache[r["cad_reference"]].checked_at) < stale_before),
        key=lambda r: parse_ts(cache[r["cad_reference"]].checked_at))
    work = fresh_rows + stale_rows
    print(f"{len(rows)} properties in list | {len(fresh_rows)} new/errored | "
          f"{len(stale_rows)} stale (> {args.max_age_days}d) | {len(rows)-len(work)} fresh in cache")

    deadline = now + args.max_minutes * 60 if args.max_minutes else None
    lock = threading.Lock()
    state = {"i": 0, "done": 0}
    fresh_results: dict[str, TaxRecord] = {}

    def worker():
        checker = HidalgoTaxChecker(delay_seconds=args.delay)
        while True:
            with lock:
                if state["i"] >= len(work) or (deadline and time.time() >= deadline):
                    return
                row = work[state["i"]]
                state["i"] += 1
            rec = checker.check(row["cad_reference"], source_owner=row.get("owner", ""),
                                source_address=row.get("address", ""), source_city=row.get("city", ""))
            with lock:
                fresh_results[row["cad_reference"]] = rec
                state["done"] += 1
                if state["done"] % 200 == 0:
                    print(f"  checked {state['done']}/{len(work)}", flush=True)

    threads = [threading.Thread(target=worker) for _ in range(max(1, args.workers))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print(f"Checked {state['done']} of {len(work)} queued this run "
          f"({len(work)-state['done']} left for the next run).")

    results = []
    for row in rows:
        ref = row["cad_reference"]
        rec = fresh_results.get(ref)
        if rec is None:
            rec = cache.get(ref)
            if rec is not None:  # refresh source info from the input list
                rec.source_owner = row.get("owner", "") or rec.source_owner
                rec.source_address = row.get("address", "") or rec.source_address
                rec.source_city = row.get("city", "") or rec.source_city
        if rec is not None and rec.status not in ("unknown",):
            results.append(rec)

    dashboard_dir = Path(args.dashboard_dir)
    dashboard_dir.mkdir(parents=True, exist_ok=True)
    delinquent = [r for r in results if r.status.startswith("delinquent")]
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total_checked": len(results),
            "delinquent_count": len(delinquent),
            "not_found_count": sum(1 for r in results if r.status == "not_found"),
            "error_count": sum(1 for r in results if r.status == "error"),
            "total_amount_due": round(sum(r.total_amount_due for r in results), 2),
            "total_delinquent_amount": round(sum(r.total_amount_due for r in delinquent), 2),
        },
        "properties": [],
    }
    for r in results:
        d = r.to_dict()
        d["legal_description"] = (d.get("legal_description") or "")[:200]
        d["exemptions"] = (d.get("exemptions") or "")[:120]
        payload["properties"].append(d)
    (dashboard_dir / "data.json").write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")

    results_csv_path.parent.mkdir(parents=True, exist_ok=True)
    with results_csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=RESULT_COLUMNS, extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        for r in results:
            d = r.to_dict()
            d["legal_description"] = (d.get("legal_description") or "")[:200]
            w.writerow(d)

    print(f"\nSummary: {len(results)} on dashboard | {len(delinquent)} delinquent "
          f"| ${payload['summary']['total_delinquent_amount']:,.2f} total delinquent owed")


if __name__ == "__main__":
    main()
