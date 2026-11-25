"""
subdivision_crawler.py

Hands-off crawler that works through every subdivision/abstract code in
Hidalgo CAD's public property-search index, pulls the property list for each
one from the CAD's public API, checks each property's tax status against
actweb.acttax.com (same logic as hidalgo_tax_checker.py), and appends only
the delinquent / current_due properties to input/geo_ids.csv.

CAD API (anonymous, public):
  POST /trueprodigy/cadpublic/auth/token  {"office": "Hidalgo"} -> JWT (~5 min)
  GET  /public/config/propertysearchadvanced -> field "abstractSubdivisionName"
       has a "codefile" list of every subdivision/abstract code (~11.8k)
  POST /public/property/advancedsearch -> every property for a code
       (HTTP 204 = code has no properties)

RUN LIMITS
  --max-minutes N     stop gracefully after N minutes (used by the overnight
                      workflow; each GitHub job may run 6h max)
  --max-properties N  stop after N properties have been tax-checked

PROGRESS (input/subdivision_progress.json)
  processed_codes : codes fully checked - never revisited
  partial         : {code: next_index} for a code that was interrupted
                    part-way (time/budget ran out); resumed first next run
Progress and new leads are flushed to disk every FLUSH_EVERY properties so a
killed job loses almost nothing.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import time
from datetime import datetime, timezone

import requests

from hidalgo_tax_checker import HidalgoTaxChecker

TRUEPRODIGY_BASE = "https://prod-container.trueprodigyapi.com"
TOKEN_URL = f"{TRUEPRODIGY_BASE}/trueprodigy/cadpublic/auth/token"
CONFIG_URL = f"{TRUEPRODIGY_BASE}/public/config/propertysearchadvanced"
SEARCH_URL = f"{TRUEPRODIGY_BASE}/public/property/advancedsearch"

OFFICE = "Hidalgo"
TOKEN_MAX_AGE_SECONDS = 240

GEO_IDS_CSV = os.path.join("input", "geo_ids.csv")
PROGRESS_JSON = os.path.join("input", "subdivision_progress.json")
CSV_HEADER = ["Geo ID", "Owner Name", "Property Address", "City"]

FLUSH_EVERY = 50          # properties between disk flushes
MAX_CONSEC_ERRORS = 5     # abort the run if the tax site keeps erroring

# A Geo ID glued onto the end of the previous line (missing newline from an
# old manual upload) - split it back onto its own line.
_GLUED_GEO_RE = re.compile(r"(?<=[^\n])(?=[A-Z0-9]{5}-[0-9A-Z]{2}-[0-9A-Z]{3}-[0-9A-Z]{4}-[0-9A-Z]{2},)")


class TokenManager:
    def __init__(self):
        self._token = None
        self._obtained_at = 0.0

    def get(self) -> str:
        now = time.time()
        if self._token is None or (now - self._obtained_at) > TOKEN_MAX_AGE_SECONDS:
            resp = requests.post(TOKEN_URL, json={"office": OFFICE}, timeout=20)
            resp.raise_for_status()
            self._token = resp.json()["user"]["token"]
            self._obtained_at = now
        return self._token


def fetch_subdivision_codes(tokens: TokenManager) -> list[dict]:
    resp = requests.get(CONFIG_URL, headers={"Authorization": tokens.get()}, timeout=30)
    resp.raise_for_status()
    results = resp.json()["results"]
    fields = results.values() if isinstance(results, dict) else results
    field = next((f for f in fields if f.get("id") == "abstractSubdivisionName"), None)
    if field is None:
        raise RuntimeError("'abstractSubdivisionName' not found in propertysearchadvanced config - CAD API may have changed.")
    return field["codefile"]


def search_subdivision(tokens: TokenManager, code_name: str, year: str) -> list[dict]:
    last_exc = None
    for attempt in range(3):
        try:
            resp = requests.post(
                SEARCH_URL,
                headers={"Authorization": tokens.get(), "Content-Type": "application/json"},
                json={
                    "advanced": True,
                    "pYear": {"operator": "=", "value": year},
                    "sortOrder": "geoID",
                    "abstractSubdivisionName": {"operator": "in", "value": [code_name]},
                },
                timeout=60,
            )
            if resp.status_code == 204 or not resp.text.strip():
                return []
            resp.raise_for_status()
            return resp.json().get("results", [])
        except requests.RequestException as exc:
            last_exc = exc
            time.sleep(2 * (attempt + 1))
    raise last_exc


def read_geo_csv_text() -> str:
    if not os.path.exists(GEO_IDS_CSV):
        return ""
    with open(GEO_IDS_CSV, encoding="utf-8-sig", newline="") as f:
        return _GLUED_GEO_RE.sub("\n", f.read().replace("\r\n", "\n"))


def load_existing_geo_ids() -> set[str]:
    ids = set()
    lines = read_geo_csv_text().split("\n")
    for row in csv.reader(lines[1:]):
        if row and row[0].strip():
            ids.add(row[0].strip())
    return ids


def append_rows_to_csv(rows: list[list[str]]) -> None:
    if not rows:
        return
    os.makedirs(os.path.dirname(GEO_IDS_CSV), exist_ok=True)
    exists = os.path.exists(GEO_IDS_CSV) and os.path.getsize(GEO_IDS_CSV) > 0
    needs_newline = False
    if exists:
        with open(GEO_IDS_CSV, "rb") as f:
            f.seek(-1, os.SEEK_END)
            needs_newline = f.read(1) not in (b"\n", b"\r")
    with open(GEO_IDS_CSV, "a", newline="", encoding="utf-8") as f:
        if needs_newline:
            f.write("\n")
        w = csv.writer(f, lineterminator="\n")
        if not exists:
            w.writerow(CSV_HEADER)
        w.writerows(rows)


def derive_code_from_geo_id(geo_id: str) -> str | None:
    geo_id = geo_id.strip()
    return geo_id[:5] + "00" if len(geo_id) >= 5 else None


def load_progress(existing_geo_ids: set[str]) -> dict:
    progress = {"processed_codes": [], "partial": {}, "total_properties_checked": 0,
                "total_added": 0, "seeded_from_existing_csv": False}
    if os.path.exists(PROGRESS_JSON):
        with open(PROGRESS_JSON, encoding="utf-8") as f:
            progress.update(json.load(f))
    progress.setdefault("partial", {})
    if not progress["seeded_from_existing_csv"]:
        derived = {derive_code_from_geo_id(g) for g in existing_geo_ids}
        derived.discard(None)
        progress["processed_codes"] = sorted(set(progress["processed_codes"]) | derived)
        progress["seeded_from_existing_csv"] = True
    return progress


def save_progress(progress: dict) -> None:
    progress["last_run_utc"] = datetime.now(timezone.utc).isoformat()
    os.makedirs(os.path.dirname(PROGRESS_JSON), exist_ok=True)
    with open(PROGRESS_JSON, "w", encoding="utf-8") as f:
        json.dump(progress, f, indent=2)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--max-properties", type=int, default=None)
    ap.add_argument("--max-minutes", type=float, default=None)
    ap.add_argument("--year", default="2026")
    ap.add_argument("--max-subdivisions", type=int, default=None)
    args = ap.parse_args()
    if args.max_properties is None and args.max_minutes is None:
        args.max_properties = 400

    start = time.time()
    deadline = start + args.max_minutes * 60 if args.max_minutes else None

    def out_of_budget(checked: int) -> bool:
        if args.max_properties is not None and checked >= args.max_properties:
            return True
        return deadline is not None and time.time() >= deadline

    tokens = TokenManager()
    existing = load_existing_geo_ids()
    progress = load_progress(existing)
    done = set(progress["processed_codes"])
    partial: dict = progress["partial"]
    print(f"Tracked Geo IDs: {len(existing)} | codes done: {len(done)} | partial: {len(partial)}")

    all_codes = fetch_subdivision_codes(tokens)
    by_name = {c["codeName"]: c for c in all_codes}
    # resume interrupted codes first, then everything not yet done
    queue = [by_name[c] for c in partial if c in by_name and c not in done]
    queue += [c for c in all_codes if c["codeName"] not in done and c["codeName"] not in partial]
    print(f"Total codes: {len(all_codes)} | to do: {len(queue)}")

    checker = HidalgoTaxChecker()
    pending_rows: list[list[str]] = []
    checked = added = codes_done_run = since_flush = consec_err = 0
    aborted = False

    def flush():
        nonlocal pending_rows, since_flush
        append_rows_to_csv(pending_rows)
        progress["processed_codes"] = sorted(done)
        save_progress(progress)
        pending_rows = []
        since_flush = 0

    for code in queue:
        if out_of_budget(checked) or aborted:
            break
        if args.max_subdivisions is not None and codes_done_run >= args.max_subdivisions:
            break
        name = code["codeName"]
        try:
            props = search_subdivision(tokens, name, args.year)
        except requests.RequestException as exc:
            print(f"  [{name}] search failed, retry next run: {exc}")
            continue

        start_idx = int(partial.get(name, 0))
        finished = True
        for idx in range(start_idx, len(props)):
            prop = props[idx]
            gid = prop.get("geoID")
            if not gid or gid in existing:
                continue
            if out_of_budget(checked):
                partial[name] = idx
                finished = False
                break
            rec = checker.check(gid, source_owner=prop.get("name") or "",
                                source_address=prop.get("streetPrimary") or "",
                                source_city=prop.get("city") or "")
            if rec.status == "error":
                consec_err += 1
                partial[name] = idx          # retry this property next time
                finished = False
                time.sleep(20)
                if consec_err >= MAX_CONSEC_ERRORS:
                    print("Tax site keeps erroring - stopping this run early.")
                    aborted = True
                break
            consec_err = 0
            checked += 1
            since_flush += 1
            if rec.status.replace("_multi_match", "") in ("delinquent", "current_due"):
                pending_rows.append([gid, prop.get("name") or "", prop.get("streetPrimary") or "",
                                     prop.get("city") or ""])
                existing.add(gid)
                added += 1
            if since_flush >= FLUSH_EVERY:
                if finished:
                    partial[name] = idx + 1
                flush()
        if finished:
            partial.pop(name, None)
            done.add(name)
            codes_done_run += 1
        elif name not in partial:
            partial[name] = start_idx
        if props:
            print(f"  [{name}] {len(props)} props | finished={finished} | run totals checked={checked} added={added}")

    progress["total_properties_checked"] += checked
    progress["total_added"] += added
    progress["partial"] = partial
    flush()

    print("---")
    print(f"Minutes elapsed: {(time.time()-start)/60:.1f}")
    print(f"Subdivisions completed: {codes_done_run} | in progress: {len(partial)}")
    print(f"Properties checked: {checked} | new leads: {added}")
    print(f"Codes done overall: {len(done)} / {len(all_codes)}")


if __name__ == "__main__":
    main()
