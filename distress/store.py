"""
store.py - the JSON files that carry state between GitHub Actions runs.

Everything lives in data/distress/ and is committed back to the repo by the
workflow, so the next run picks up where the last one stopped and you have a
plain-file audit trail of every record that was ever pulled.

    clerk_records.json   doc_id  -> recorded document
    court_cases.json     case_id -> court case (open tax suits, probate, divorce)
    matches.json         "clerk:<doc_id>" / "court:<case_id>" -> parcel match
    parcels.json         geo_id  -> HCAD parcel (owner, address, value, legal)
    tax_status.json      geo_id  -> latest tax-site check for matched parcels
    state.json           scraper progress (per-keyword done-through dates, canary)
"""

from __future__ import annotations

import json
import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("DISTRESS_DATA_DIR", "data/distress"))

FILES = ("clerk_records", "court_cases", "matches", "parcels", "tax_status", "state")


def load(name: str) -> dict:
    path = DATA_DIR / f"{name}.json"
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def save(name: str, data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / f"{name}.json"
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, sort_keys=True, ensure_ascii=False)
        f.write("\n")
    tmp.replace(path)
