#!/usr/bin/env python3
"""
run_distress.py - Hidalgo Distress Watch runner.

    python run_distress.py clerk  [--budget 120] [--backfill-days 90]
    python run_distress.py court  [--full-taxsuits]
    python run_distress.py build  [--tax-budget 400] [--match-budget 1500]
    python run_distress.py all    (clerk, court, build in order)

clerk  - pulls new recorded documents (heirships, wills/probate, lis pendens,
         liens incl. city mowing/paving liens, judgments, sub-trustee
         appointments, foreclosure notices, bankruptcies, divorces) from the
         County Clerk portal, picking up where the last run stopped.
court  - pulls open tax suits (all years, full re-walk weekly so closed ones
         drop off) plus the newest probate and divorce filings from Odyssey.
build  - matches every record to an HCAD parcel, refreshes tax status for
         matched parcels, scores each parcel 0-100, and writes
         dashboard/distress.json for the dashboard.

All state is in data/distress/*.json (see distress/store.py).
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone

from distress import store
from distress.names import person_from_last_first, Person, parse_clerk_legal
from distress.score import CATEGORIES, score_parcel

TODAY = datetime.now(timezone.utc).date()


def log(msg: str) -> None:
    print(msg, flush=True)


# ============================================================== clerk

def run_clerk(budget: int, backfill_days: int) -> None:
    from distress.clerk import ClerkScraper, KEYWORDS, Throttled

    records = store.load("clerk_records")
    state = store.load("state")
    cstate = state.setdefault("clerk", {})
    done = cstate.setdefault("done_through", {})
    canary = cstate.get("canary")
    added = 0

    from pathlib import Path
    with ClerkScraper(log=log) as sc:
        sc.debug_dir = store.DATA_DIR / "debug"
        certified = sc.certified_through() or (TODAY - timedelta(days=2))
        log(f"Clerk portal certified through {certified}")
        default_start = certified - timedelta(days=backfill_days)

        searches_since_canary = 99
        status = "ok"
        while sc.searches < budget:
            # always work on the keyword that is furthest behind
            pending = []
            for kw, (dept, win) in KEYWORDS.items():
                start = date.fromisoformat(done[kw]) + timedelta(days=1) if kw in done else default_start
                if start <= certified:
                    pending.append((start, kw, dept, win))
            if not pending:
                log("Clerk: every keyword is caught up to the certified date.")
                break
            pending.sort()
            start, kw, dept, win = pending[0]
            end = min(start + timedelta(days=win - 1), certified)

            try:
                kept, raw = sc.search_window(kw, dept, start, end)
            except Throttled as exc:
                log(f"  STOP: portal throttling ({exc}). Will resume next run.")
                status = "throttled"
                break

            if raw == 0:
                if searches_since_canary > 4:
                    if not sc.is_honest(canary):
                        log("  STOP: canary search came back empty - portal is throttling. Will resume next run.")
                        status = "throttled"
                        break
                    searches_since_canary = 0
            elif raw >= 3:
                canary = {"keyword": kw, "department": dept, "start": start.isoformat(), "end": end.isoformat()}
            searches_since_canary += 1

            new = 0
            for rec in kept:
                key = rec.doc_id or rec.doc_number
                if key and key not in records:
                    records[key] = rec.to_dict()
                    new += 1
            added += new
            done[kw] = end.isoformat()
            log(f"  {kw:<11} {start}..{end}: {raw} rows, {len(kept)} signals, {new} new")

            if sc.searches % 10 == 0:   # checkpoint so a crash doesn't lose work
                cstate["canary"] = canary
                store.save("clerk_records", records)
                store.save("state", state)

        cstate["canary"] = canary
        cstate["certified_through"] = certified.isoformat()
        cstate["last_run"] = datetime.now(timezone.utc).isoformat()
        cstate["last_status"] = status
        cstate["last_searches"] = sc.searches
        cstate["last_debug"] = sc.debug[-25:]

    store.save("clerk_records", records)
    store.save("state", state)
    log(f"Clerk: {sc.searches} searches, {added} new records, {len(records)} total. Status: {status}")


# ============================================================== clerk exports

EXPORT_DIR = "input/clerk_exports"
_EXPORT_COLS = {
    "grantor": "grantor", "grantors": "grantor", "grantor name": "grantor",
    "grantee": "grantee", "grantees": "grantee", "grantee name": "grantee",
    "doc type": "doc_type", "document type": "doc_type", "instrument type": "doc_type", "type": "doc_type",
    "recorded date": "recorded", "recorded": "recorded", "record date": "recorded", "file date": "recorded",
    "doc number": "doc_number", "document number": "doc_number", "instrument number": "doc_number",
    "doc #": "doc_number", "doc no": "doc_number", "instrument #": "doc_number",
    "legal description": "legal", "legal": "legal", "legal desc": "legal",
}


def import_clerk_exports() -> int:
    """Read CSVs exported from the clerk portal ('Export all Results') into clerk_records.

    The portal returns empty results to automated browsers, so clerk filings come
    in this way: run the searches in a normal browser, export, and drop the CSV in
    input/clerk_exports/ (GitHub web upload works - it triggers a refresh).
    """
    import csv
    from pathlib import Path
    from distress.clerk import categorize

    files = sorted(Path(EXPORT_DIR).glob("*.csv"))
    if not files:
        return 0
    records = store.load("clerk_records")
    added = 0
    for f in files:
        with f.open(newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            cols = {c: _EXPORT_COLS.get(re.sub(r"\s+", " ", (c or "").strip().lower())) for c in reader.fieldnames or []}
            if "doc_type" not in cols.values() or "doc_number" not in cols.values():
                log(f"  {f.name}: skipped - no Doc Type / Doc Number columns (found {reader.fieldnames})")
                continue
            for raw in reader:
                row = {"grantor": "", "grantee": "", "doc_type": "", "recorded": "", "doc_number": "", "legal": ""}
                for c, key in cols.items():
                    if key and raw.get(c):
                        row[key] = (row[key] + "; " if row[key] else "") + raw[c].strip()
                d = row["recorded"][:10]
                for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
                    try:
                        row["recorded"] = datetime.strptime(d, fmt).strftime("%m/%d/%Y")
                        break
                    except ValueError:
                        continue
                cat = categorize(row["doc_type"], row["grantor"], row["grantee"])
                if not cat or not row["doc_number"] or row["doc_number"] in records:
                    continue
                records[row["doc_number"]] = {
                    "doc_id": "", "doc_number": row["doc_number"], "doc_type": row["doc_type"], "category": cat,
                    "recorded": row["recorded"], "grantor": row["grantor"], "grantee": row["grantee"],
                    "legal": row["legal"], "department": "RP", "keyword": "export",
                    "first_seen": TODAY.isoformat(), "url": "",
                }
                added += 1
        log(f"  {f.name}: read")
    store.save("clerk_records", records)
    log(f"Clerk exports: {added} new signal records from {len(files)} file(s)")
    return added


# ============================================================== court

def run_court(full_taxsuits: bool) -> None:
    from distress.court import OdysseyClient, fetch_open_tax_suits, fetch_recent

    cases = store.load("court_cases")
    state = store.load("state")
    cst = state.setdefault("court", {})
    client = OdysseyClient()

    last_full = cst.get("taxsuits_full_walk")
    need_full = full_taxsuits or not last_full or (TODAY - date.fromisoformat(last_full)).days >= 7

    if need_full:
        log("Court: full walk of open tax suits (2000 -> today)")
        found = fetch_open_tax_suits(client, date(2000, 1, 1), TODAY, window_days=92, log=log)
        open_ids = {c.case_id for c in found}
        closed = 0
        for cid, c in cases.items():
            if c.get("kind") == "tax_suit" and c.get("open", True) and cid not in open_ids:
                c["open"] = False
                c["closed_detected"] = TODAY.isoformat()
                closed += 1
        log(f"  {closed} previously-open tax suits are no longer open")
        cst["taxsuits_full_walk"] = TODAY.isoformat()
    else:
        log("Court: open tax suits filed in the last 60 days")
        found = fetch_open_tax_suits(client, TODAY - timedelta(days=60), TODAY, window_days=31, log=log)

    for c in found:
        d = c.to_dict()
        d["open"] = True
        prev = cases.get(c.case_id)
        if prev:
            d["first_seen"] = prev.get("first_seen", d["first_seen"])
        cases[c.case_id] = d

    for kind in ("probate", "divorce"):
        try:
            for c in fetch_recent(client, kind, log=log):
                d = c.to_dict()
                prev = cases.get(c.case_id)
                if prev:
                    d["first_seen"] = prev.get("first_seen", d["first_seen"])
                cases[c.case_id] = d
        except Exception as exc:  # noqa: BLE001
            log(f"  {kind}: failed ({exc}) - will retry next run")

    cst["last_run"] = datetime.now(timezone.utc).isoformat()
    store.save("court_cases", cases)
    store.save("state", state)
    n_open = sum(1 for c in cases.values() if c.get("kind") == "tax_suit" and c.get("open"))
    log(f"Court: {len(cases)} cases on file, {n_open} open tax suits")


# ============================================================== build

_HOMESTEAD_RE = re.compile(r"\bHS\b|HOMESTEAD|HOMESTD|\bHMSTD\b", re.I)
_LAWSUIT_NONE = re.compile(r"^\s*(NO|NONE|N/A)?\s*$", re.I)


def clerk_people(rec: dict) -> list[Person]:
    """Who on this document is probably the owner - best guess first."""
    cat = rec["category"]
    grantor = [person_from_last_first(n) for n in re.split(r"\s*;\s*|\n", rec.get("grantor", "")) if n]
    grantee = [person_from_last_first(n) for n in re.split(r"\s*;\s*|\n", rec.get("grantee", "")) if n]
    grantor = [p for p in grantor if p]
    grantee = [p for p in grantee if p]
    if cat in ("heirship", "code_lien", "tax_lien", "child_support_lien", "judgment", "mechanics_lien",
               "other_lien", "lis_pendens"):
        ordered = grantee + grantor
    else:
        ordered = grantor + grantee
    # deceased people first - for death records they're the owner of record
    ordered.sort(key=lambda p: not p.deceased)
    return ordered


def court_people(case: dict) -> list[Person]:
    out = []
    for d in case.get("parties", []):
        out.append(Person(raw=d["raw"], last=d["last"], first=d["first"], middle=d.get("middle", ""),
                          deceased=d.get("deceased", False), alt_lasts=d.get("alt_lasts", [])))
    return out


def run_build(tax_budget: int, match_budget: int, year: str) -> None:
    from distress.cad import CadClient, match_record
    from hidalgo_tax_checker import HidalgoTaxChecker

    clerk = store.load("clerk_records")
    court = store.load("court_cases")
    matches = store.load("matches")
    parcels = store.load("parcels")
    taxes = store.load("tax_status")

    cad = CadClient(year=year)

    # ---- 1. match records to parcels --------------------------------
    todo = []
    for doc_id, rec in clerk.items():
        todo.append((f"clerk:{doc_id}", lambda r=rec: (clerk_people(r), parse_clerk_legal(r.get("legal", "")))))
    for cid, case in court.items():
        if case.get("kind") == "tax_suit" and not case.get("open", True):
            continue
        todo.append((f"court:{cid}", lambda c=case: (court_people(c), None)))

    def needs_match(ref: str) -> bool:
        m = matches.get(ref)
        if not m:
            return True
        if m.get("confidence") in ("high", "medium"):
            return False
        # retry the misses every 2 weeks - HCAD owner names change
        return (TODAY - date.fromisoformat(m.get("tried", "2000-01-01"))).days >= 14

    pending = [t for t in todo if needs_match(t[0])]
    log(f"Build: {len(pending)} records to match ({len(todo)} total)")
    for i, (ref, get) in enumerate(pending[:match_budget], 1):
        people, legal = get()
        try:
            m, cands = match_record(cad, people, legal)
        except Exception as exc:  # noqa: BLE001
            log(f"  match {ref} failed: {exc}")
            continue
        entry = {"tried": TODAY.isoformat(), "candidates": cands}
        if m:
            entry.update(m.to_dict())
        else:
            entry["confidence"] = "low" if cands else "none"
        matches[ref] = entry
        if i % 100 == 0:
            log(f"  matched {i}/{min(len(pending), match_budget)} ({cad.calls} HCAD calls)")
            store.save("matches", matches)
    for geo, p in cad.parcels.items():
        parcels[geo] = p.to_dict()
    store.save("matches", matches)
    store.save("parcels", parcels)

    # ---- 2. gather signals per parcel ------------------------------
    signals: dict[str, list[dict]] = {}

    def add(geo: str, sig: dict):
        signals.setdefault(geo, []).append(sig)

    for doc_id, rec in clerk.items():
        m = matches.get(f"clerk:{doc_id}", {})
        if m.get("confidence") not in ("high", "medium"):
            continue
        add(m["geo_id"], {
            "category": rec["category"], "source": "clerk", "date": rec["recorded"],
            "label": CATEGORIES[rec["category"]][2], "detail": rec["doc_type"],
            "ref": rec.get("doc_number", ""), "url": rec.get("url", ""),
            "parties": f'{rec.get("grantor","")} → {rec.get("grantee","")}',
            "deceased": bool(re.search(r"DECEASED", rec.get("grantee", "") + rec.get("grantor", ""), re.I))
                        and rec["category"] in ("heirship", "will_probate"),
            "confidence": m["confidence"],
        })
    for cid, case in court.items():
        if case.get("kind") == "tax_suit" and not case.get("open", True):
            continue
        m = matches.get(f"court:{cid}", {})
        if m.get("confidence") not in ("high", "medium"):
            continue
        cat = case["kind"]
        add(m["geo_id"], {
            "category": cat, "source": "court", "date": case["filed"],
            "label": CATEGORIES[cat][2], "detail": f'{case["case_type"]} · {case["court"]}',
            "ref": case["case_number"], "url": case.get("url", ""), "parties": case["style"][:240],
            "deceased": bool(case.get("any_deceased")), "confidence": m["confidence"],
        })

    # ---- 3. tax status for matched parcels -------------------------
    stale = [g for g in signals if g not in taxes or
             (TODAY - date.fromisoformat(taxes[g].get("checked", "2000-01-01"))).days >= 7]
    stale.sort(key=lambda g: g in taxes)   # never-checked first
    if stale:
        log(f"Build: tax-checking {min(len(stale), tax_budget)} of {len(stale)} parcels")
        checker = HidalgoTaxChecker()
        for g in stale[:tax_budget]:
            r = checker.check(g)
            if r.status == "error":
                continue
            taxes[g] = {
                "checked": TODAY.isoformat(), "found": r.found, "status": r.status,
                "prior_year_due": r.prior_year_amount_due, "total_due": r.total_amount_due,
                "active_lawsuits": r.active_lawsuits, "exemptions": r.exemptions,
                "homestead": bool(_HOMESTEAD_RE.search(r.exemptions or "")),
                "owner_mailing": r.owner_address, "owner_name": r.owner_name,
            }
        store.save("tax_status", taxes)

    for g, t in taxes.items():
        if g not in signals:
            continue
        if t.get("prior_year_due", 0) > 0:
            add(g, {"category": "tax_delinquent", "source": "tax", "date": t["checked"],
                    "label": "Delinquent taxes", "detail": f'${t["prior_year_due"]:,.0f} prior-year due',
                    "amount": t["prior_year_due"], "ref": "", "url": "", "parties": "", "deceased": False,
                    "confidence": "high"})
        lawsuits = t.get("active_lawsuits") or ""
        if not _LAWSUIT_NONE.match(lawsuits) and not any(s["category"] == "tax_suit" for s in signals[g]):
            add(g, {"category": "tax_suit", "source": "tax", "date": t["checked"],
                    "label": "Open tax suit", "detail": f"Tax office: {lawsuits[:80]}",
                    "ref": "", "url": "", "parties": "", "deceased": False, "confidence": "high"})

    # ---- 4. score + write dashboard --------------------------------
    props = []
    for g, sigs in signals.items():
        p = parcels.get(g, {})
        t = taxes.get(g)
        score, breakdown = score_parcel(sigs, t, TODAY)
        sigs.sort(key=lambda s: s.get("date", ""), reverse=True)
        props.append({
            "geo_id": g, "pid": p.get("pid", ""), "owner": p.get("owner", ""),
            "address": p.get("address", ""), "city": p.get("city", ""),
            "legal": p.get("legal", ""), "market_value": p.get("market_value", 0),
            "score": score, "breakdown": breakdown, "signals": sigs,
            "tax": t or None,
            "categories": sorted({s["category"] for s in sigs}),
        })
    props.sort(key=lambda r: (-r["score"], r["geo_id"]))

    tax_suits = []
    for cid, case in court.items():
        if case.get("kind") != "tax_suit" or not case.get("open", True):
            continue
        m = matches.get(f"court:{cid}", {})
        geo = m.get("geo_id") if m.get("confidence") in ("high", "medium") else None
        p = parcels.get(geo, {}) if geo else {}
        defendants = ", ".join(x["raw"] for x in case.get("parties", []))
        tax_suits.append({
            "case_number": case["case_number"], "url": case.get("url", ""), "filed": case["filed"],
            "court": case["court"], "status": case["status"], "case_type": case["case_type"],
            "defendants": defendants or case["style"].split(" VS. ")[-1][:200],
            "plaintiffs": case.get("plaintiffs", [])[:8],
            "deceased": bool(case.get("any_deceased")),
            "geo_id": geo, "pid": p.get("pid", ""), "match": m.get("confidence", "none"),
            "address": p.get("address", ""), "city": p.get("city", ""),
            "market_value": p.get("market_value", 0),
            "total_due": (taxes.get(geo) or {}).get("total_due") if geo else None,
        })
    tax_suits.sort(key=lambda r: datetime.strptime(r["filed"], "%m/%d/%Y") if r["filed"] else datetime.min,
                   reverse=True)

    # recent filings feed (last 45 days), matched or not - so nothing is invisible
    feed = []
    cutoff = TODAY - timedelta(days=45)
    for doc_id, rec in clerk.items():
        try:
            d = datetime.strptime(rec["recorded"], "%m/%d/%Y").date()
        except ValueError:
            continue
        if d < cutoff:
            continue
        m = matches.get(f"clerk:{doc_id}", {})
        feed.append({"date": rec["recorded"], "source": "Clerk", "category": rec["category"],
                     "label": CATEGORIES[rec["category"]][2], "doc_type": rec["doc_type"],
                     "ref": rec.get("doc_number", ""), "url": rec.get("url", ""),
                     "parties": f'{rec.get("grantor","")} → {rec.get("grantee","")}', "legal": rec.get("legal", ""),
                     "geo_id": m.get("geo_id"), "match": m.get("confidence", "pending"),
                     "candidates": m.get("candidates", [])})
    for cid, case in court.items():
        if case.get("kind") == "tax_suit":
            continue
        try:
            d = datetime.strptime(case["filed"], "%m/%d/%Y").date()
        except ValueError:
            continue
        if d < cutoff:
            continue
        m = matches.get(f"court:{cid}", {})
        feed.append({"date": case["filed"], "source": "Court", "category": case["kind"],
                     "label": CATEGORIES[case["kind"]][2], "doc_type": case["case_type"],
                     "ref": case["case_number"], "url": case.get("url", ""), "parties": case["style"][:240],
                     "legal": "", "geo_id": m.get("geo_id"), "match": m.get("confidence", "pending"),
                     "candidates": m.get("candidates", [])})
    feed.sort(key=lambda r: datetime.strptime(r["date"], "%m/%d/%Y"), reverse=True)

    state = store.load("state")
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "properties": len(props),
            "score_70_plus": sum(1 for p in props if p["score"] >= 70),
            "score_50_plus": sum(1 for p in props if p["score"] >= 50),
            "stacked": sum(1 for p in props if len({CATEGORIES[c][0] for c in p["categories"]}) >= 2),
            "open_tax_suits": len(tax_suits),
            "clerk_records": len(clerk),
            "court_cases": len(court),
            "match_rate": round(100 * sum(1 for m in matches.values() if m.get("confidence") in ("high", "medium"))
                                / max(1, len(matches))),
            "clerk_certified_through": state.get("clerk", {}).get("certified_through"),
            "clerk_status": state.get("clerk", {}).get("last_status"),
            "clerk_done_through": state.get("clerk", {}).get("done_through", {}),
        },
        "categories": {k: {"group": v[0], "weight": v[1], "label": v[2]} for k, v in CATEGORIES.items()},
        "properties": props,
        "tax_suits": tax_suits,
        "feed": feed,
    }
    import json
    from pathlib import Path
    out = Path("dashboard/distress.json")
    out.write_text(json.dumps(payload, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    log(f"Build: {len(props)} scored parcels, {len(tax_suits)} open tax suits, {len(feed)} recent filings "
        f"-> {out} ({out.stat().st_size/1e6:.1f} MB)")


# ============================================================== main

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("job", choices=["clerk", "court", "build", "all"],
                    help="all = court + clerk CSV exports + build. The clerk scraper only runs "
                         "when asked for by name (the portal blanks automated searches).")
    ap.add_argument("--budget", type=int, default=120, help="max clerk searches this run")
    ap.add_argument("--backfill-days", type=int, default=90, help="how far back the clerk scraper starts")
    ap.add_argument("--full-taxsuits", action="store_true", help="force a full walk of open tax suits")
    ap.add_argument("--tax-budget", type=int, default=400, help="max tax-site checks this run")
    ap.add_argument("--match-budget", type=int, default=1500, help="max records matched this run")
    ap.add_argument("--year", default=str(TODAY.year), help="HCAD appraisal year")
    args = ap.parse_args()

    t0 = time.time()
    failed = []
    if args.job in ("court", "all"):
        try:
            run_court(args.full_taxsuits)
        except Exception as exc:  # noqa: BLE001
            import traceback
            log(f"COURT JOB FAILED: {exc!r}")
            st = store.load("state")
            st.setdefault("court", {})["last_error"] = traceback.format_exc()[-2500:]
            st["court"]["last_error_at"] = datetime.now(timezone.utc).isoformat()
            store.save("state", st)
            failed.append("court")
    if args.job in ("all", "build"):
        import_clerk_exports()
    if args.job == "clerk":
        try:
            run_clerk(args.budget, args.backfill_days)
        except Exception as exc:  # noqa: BLE001
            log(f"CLERK JOB FAILED: {exc!r}")
            failed.append("clerk")
    if args.job in ("build", "all"):
        run_build(args.tax_budget, args.match_budget, args.year)
    log(f"Done in {(time.time()-t0)/60:.1f} min" + (f" - failed: {', '.join(failed)}" if failed else ""))
    if failed and args.job != "all":
        sys.exit(1)


if __name__ == "__main__":
    main()
