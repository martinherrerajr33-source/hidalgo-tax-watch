"""
clerk.py - Hidalgo County Clerk official public records scraper
(hidalgo.tx.publicsearch.us, the Kofile/Neumo "PublicSearch" platform).

HOW THE PORTAL WORKS (confirmed live, Oct 2026)
------------------------------------------------
This one is NOT a plain HTTP site. The results page is a single-page app
that runs every search over a websocket (wss://.../ws, "@kofile/FETCH_DOCUMENTS"
messages). We drive a real headless Chromium with Playwright, trigger each
search inside the app, and read the FETCH_DOCUMENTS_FULFILLED websocket
message directly - not the rendered table, which on a fresh page load can
show "No Results Found" even though the data arrived (confirmed 10/15/2026).
That message also carries structured lot / block / subdivision fields and the
total hit count (meta.numRecords) used for paging.

A quick search is just a URL:
    /results?department=RP&limit=250&offset=0
            &recordedDateRange=20261012,20261013
            &searchOcrText=false&searchType=quickSearch&searchValue=heirship
The search value matches indexed words, including the document type, so
"heirship" pulls every AFFIDVT HEIRSHIP in the window. Rows come back as:
    GRANTOR | GRANTEE | DOC TYPE | RECORDED DATE | DOC NUMBER | BOOK/VOL/PAGE | LEGAL DESCRIPTION
e.g. MIRELES EVA MARIA | BERRONES JOSE GUADALUPE DECEASED | AFFIDVT HEIRSHIP |
     10/13/2026 | 2026-3796238 | --/--/-- |
     Subdivision- Name: EXPRESSWAY HEIGHTS 3 Lot: 7 Block: 11

Each keyword search brings back some noise (a grantor named WILL, a vendor's
lien on a deed) so every row is re-checked against the doc-type rules in
DOC_RULES before it's kept.

THE THROTTLING PROBLEM
-----------------------
After a burst of searches the backend starts timing out, and the page shows
"No Results Found" instead of an error. A naive scraper would read that as
"nothing was filed" and silently skip the day. So:
  * searches are spaced out (~15-25 s apart),
  * an empty result is retried once after a cool-down, and
  * then a canary search (a window we already know has filings) is run. If
    the canary also comes back empty, the portal is throttling us: the run
    stops and the window is NOT marked done, so the next run retries it.
"""

from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass, asdict, field
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode

BASE = "https://hidalgo.tx.publicsearch.us"

# keyword -> (department, how many days per search window)
# Busy keywords get small windows so one search stays well under the page size.
KEYWORDS = {
    "heirship":    ("RP", 7),
    "will":        ("RP", 7),
    "probate":     ("RP", 7),
    "testament":   ("RP", 14),
    "pendens":     ("RP", 7),
    "judgment":    ("RP", 7),
    "judgement":   ("RP", 7),
    "lien":        ("RP", 1),
    "trustee":     ("RP", 3),
    "bankruptcy":  ("RP", 14),
    "divorce":     ("RP", 14),
    "foreclosure": ("FC", 7),
}

# Documents that end or transfer a problem rather than create one - never a signal.
_NOT_A_SIGNAL = re.compile(
    r"^(VOID|COR |C C |REL\b|RELEASE|RELEASED|P/REL|P/RE |PARTIAL REL|MEM REL|AMD REL|DISCH|DISCHARGE|"
    r"WAIVER|SUBORDINAT|TRANSFER|ASGMT|ASSIGN|P/ASGMT|CONVYNCE|RECONVEY|EXTENSN|AEOLE|CEOLE|MEOLE|"
    r"RENEW|RENEWAL|AR&ELE|MODIFY|MOD |MEM MOD|RATIF|ROLE|REINSTATE|BOND|MEM BOND|REQUEST|REQ )"
    r"|DEED|SALE DEE|TRUSTEE DEE|TRUSTEES DEE|SHERIFFS|CONSTABLES|VENDORS|EQUITY LIEN|HOMESTEAD LIEN|LIEN-VOLUNTARY|LANDLORDS|HOSPITAL|MEDICAL|CLINIC|"
    r"RENTAL LIEN|R/W LIEN|RIGHT OF WAY|INHER|ESTATE TAX REC|ESTATE TAX LET|NO INHER|ETLE|ETLT|"
    r"RELEASE OF CHIL|TAX AGREE|TAX PAYM|TAX REDEMPTION|AUTHORIZATION",
    re.I,
)

# (category, doc-type regex) - first match wins
DOC_RULES: list[tuple[str, re.Pattern]] = [
    ("heirship",           re.compile(r"HEIR", re.I)),
    ("will_probate",       re.compile(r"\bWILL\b|PROBATE|LETTERS? (OF )?TEST|LETTER TESTAMEN|EXECUTORS|"
                                      r"LETTERS OF ADMI|MUNIMENT|ESTATE DOCUMENT|^ED$|DEATH CERT", re.I)),
    ("pre_foreclosure",    re.compile(r"FORECL|FORCL|APPOINT\w* (FOR )?SUB|APPOINT TRUSTEE|APPOINTMENT OF SUB|"
                                      r"MEM APPOINT TRU", re.I)),
    ("lis_pendens",        re.compile(r"LIS P", re.I)),
    ("code_lien",          re.compile(r"MOWING|PAVING|ASSESSMENT LIEN|ASSMT LIEN|^ALE$|DEMOLI|WEED|NUISANCE", re.I)),
    ("tax_lien",           re.compile(r"FEDERAL TAX|FED TAX|FEDERAL LIEN|STATE TAX L|TAX LIEN|FEDERAL A/J", re.I)),
    ("child_support_lien", re.compile(r"CHILD SUP|^CSL$", re.I)),
    ("judgment",           re.compile(r"JUDG", re.I)),
    ("mechanics_lien",     re.compile(r"MECH|MATERIAL|LABOR LIEN|^MLE$|^ML$|^MLAW$", re.I)),
    ("bankruptcy",         re.compile(r"BANKRU", re.I)),
    ("divorce",            re.compile(r"DIVORCE|DECREE", re.I)),
    ("other_lien",         re.compile(r"LIEN|^LAI$|^LAV$", re.I)),
]

_GOV_GRANTOR = re.compile(r"^(CITY|TOWN|VILLAGE) OF\b|\bCITY OF\b", re.I)


def categorize(doc_type: str, grantor: str = "", grantee: str = "") -> str | None:
    dt = (doc_type or "").strip()
    if not dt or _NOT_A_SIGNAL.search(dt):
        return None
    for cat, rx in DOC_RULES:
        if rx.search(dt):
            # A plain lien filed BY a city is almost always a weedy-lot /
            # demolition / code-enforcement lien.
            if cat == "other_lien" and _GOV_GRANTOR.search(grantor or ""):
                return "code_lien"
            return cat
    return None


@dataclass
class ClerkRecord:
    doc_id: str
    doc_number: str
    doc_type: str
    category: str
    recorded: str        # MM/DD/YYYY
    grantor: str
    grantee: str
    legal: str
    department: str
    keyword: str
    first_seen: str = field(default_factory=lambda: datetime.now(timezone.utc).date().isoformat())

    @property
    def url(self) -> str:
        return f"{BASE}/doc/{self.doc_id}"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["url"] = self.url
        return d


# Confirmed live 10/15/2026: 11 heirship affidavits recorded 10/12-10/13/2026.
# Used until a run finds a busier window of its own.
DEFAULT_CANARY = {"keyword": "heirship", "department": "RP", "start": "2026-10-12", "end": "2026-10-13"}


class Throttled(RuntimeError):
    """The portal stopped answering honestly - stop and retry next run."""


def results_url(keyword: str, department: str, start: date, end: date, offset: int = 0, limit: int = 50) -> str:
    q = {
        "department": department,
        "limit": str(limit),
        "offset": str(offset),
        "recordedDateRange": f"{start:%Y%m%d},{end:%Y%m%d}",
        "searchOcrText": "false",
        "searchType": "quickSearch",
        "searchValue": keyword,
    }
    return f"{BASE}/results?{urlencode(q)}"


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "").strip()


def _join(v) -> str:
    if isinstance(v, list):
        return "; ".join(x.strip() for x in v if isinstance(x, str) and x.strip())
    return (v or "").strip() if isinstance(v, str) else ""


def rows_from_payload(data: dict) -> list[dict]:
    """FETCH_DOCUMENTS_FULFILLED payload.data -> flat rows (same shape the table shows)."""
    out = []
    by_hash = data.get("byHash") or {}
    for doc_id in data.get("byOrder") or []:
        d = by_hash.get(str(doc_id)) or by_hash.get(doc_id) or {}
        grantees = [p.get("name", "") for p in d.get("parties", []) if p.get("type") == "grantee" and p.get("name")]
        grantors = [p.get("name", "") for p in d.get("parties", []) if p.get("type") == "grantor" and p.get("name")]
        out.append({
            "doc_id": str(d.get("docId") or doc_id),
            "doc_number": d.get("docNumber") or d.get("documentNumber") or "",
            "doc_type": _strip_tags(d.get("docType") or "") or d.get("docTypeCode", ""),
            "doc_type_code": d.get("docTypeCode", ""),
            "recorded": d.get("recordedDate", ""),
            "grantor": "; ".join(dict.fromkeys(grantors)) or _join(d.get("grantor")),
            "grantee": "; ".join(dict.fromkeys(grantees)) or _join(d.get("grantee")),
            "legal": _join(d.get("legalDescription")),
        })
    return out


class ClerkScraper:
    """One Playwright browser, reused for every search in a run.

    The page's own table is unreliable on a fresh load (it can paint "No Results
    Found" even when the data arrives), so we listen to the websocket and read the
    FETCH_DOCUMENTS_FULFILLED message directly. Each search is triggered inside the
    already-loaded app (history.pushState + popstate), exactly as clicking a new
    search does - one websocket, one app session for the whole run.
    """

    def __init__(self, min_gap: float = 15.0, max_gap: float = 25.0, page_timeout: float = 75.0,
                 headless: bool = True, log=print):
        self.min_gap, self.max_gap = min_gap, max_gap
        self.page_timeout = page_timeout
        self.headless = headless
        self.log = log
        self.searches = 0
        self._pw = self._browser = self._page = None
        self._last_search_at = 0.0
        self._frames: list[dict] = []
        self.debug: list[dict] = []          # one line per search - committed so failures can be diagnosed
        self.debug_dir = None

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)
        ctx = self._browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
            viewport={"width": 1366, "height": 900},
        )
        self._page = ctx.new_page()
        self._page.on("websocket", self._on_ws)
        return self

    def __exit__(self, *exc):
        try:
            if self._browser:
                self._browser.close()
        finally:
            if self._pw:
                self._pw.stop()

    def _on_ws(self, ws):
        def on_frame(payload):
            text = payload if isinstance(payload, str) else (payload or b"").decode("utf-8", "ignore")
            if "FETCH_DOCUMENTS" not in text:
                return
            try:
                msg = json.loads(text)
            except ValueError:
                return
            t = msg.get("type", "")
            if "FETCH_DOCUMENTS_FULFILLED" in t or "FETCH_DOCUMENTS_REJECTED" in t:
                self._frames.append(msg)
        ws.on("framereceived", on_frame)

    # ---- low level ----------------------------------------------------

    def _pace(self):
        gap = random.uniform(self.min_gap, self.max_gap)
        wait = self._last_search_at + gap - time.time()
        if wait > 0:
            time.sleep(wait)
        self._last_search_at = time.time()

    def certified_through(self) -> date | None:
        """Loads the app (opening its websocket) and reads the 'Certified through' date."""
        self._page.goto(BASE + "/", wait_until="domcontentloaded", timeout=60000)
        try:
            self._page.wait_for_selector("text=Certified through", timeout=30000)
        except Exception:  # noqa: BLE001
            pass
        txt = self._page.inner_text("body")
        m = re.search(r"Certified through\s+(\d{2}/\d{2}/\d{4})", txt)
        return datetime.strptime(m.group(1), "%m/%d/%Y").date() if m else None

    # Typing a search into the page's own form is what makes the portal answer.
    # Loading a /results?... URL directly (or pushing it into the app's history)
    # only works for searches this browser has already run - brand-new ones come
    # back as "No Results Found" (confirmed 10/16/2026 in a normal browser: the
    # same advanced search returned 27 rows when submitted from the form and 0
    # when loaded by URL). So every search goes through the Quick Search form.

    def _wait_frame(self, n_before: int) -> dict:
        deadline = time.time() + self.page_timeout
        while time.time() < deadline:
            self._page.wait_for_timeout(500)
            new = self._frames[n_before:]
            ok = [f for f in new if "FULFILLED" in f.get("type", "")]
            if ok:
                payload = ok[-1].get("payload") or {}
                rows = rows_from_payload(payload.get("data") or {})
                total = (payload.get("meta") or {}).get("numRecords", len(rows))
                return {"rows": rows, "total": total, "timedOut": False, "rejected": ""}
            bad = [f for f in new if "REJECTED" in f.get("type", "")]
            if bad:
                err = bad[-1].get("payload") or {}
                return {"rows": [], "total": None, "timedOut": True, "rejected": str(err.get("errors") or "rejected")[:200]}
        return {"rows": [], "total": None, "timedOut": True, "rejected": ""}

    def _dismiss_popups(self) -> None:
        for sel in ('[role=dialog] button:has-text("Close")', 'button[aria-label="Close"]', 'button:has-text("No Thanks")'):
            try:
                loc = self._page.locator(sel)
                if loc.count() and loc.first.is_visible():
                    loc.first.click(timeout=2000)
            except Exception:  # noqa: BLE001
                pass

    def _fill(self, selector: str, value: str) -> None:
        loc = self._page.locator(selector).first
        loc.click(timeout=10000)
        loc.press("Control+A")
        loc.press("Backspace")
        loc.type(value, delay=40)

    def _ui_search(self, keyword: str, department: str, start: date, end: date) -> dict:
        """Run one quick search through the form and collect every page of results."""
        self._pace()
        self.searches += 1
        label = f"{keyword} {start:%m/%d/%Y}-{end:%m/%d/%Y}"
        self._page.goto(BASE + "/", wait_until="domcontentloaded", timeout=60000)
        self._page.wait_for_selector("#basicSearchInputBox, input[aria-label^='Search for']", timeout=30000)
        self._page.wait_for_timeout(1500)
        self._dismiss_popups()
        if department != "RP":
            try:
                self._page.locator("button[aria-haspopup=listbox]").first.click(timeout=5000)
                self._page.locator("[role=option]", has_text="Foreclosures").first.click(timeout=5000)
            except Exception:  # noqa: BLE001
                self.log(f"    could not switch department to {department}")
        self._fill("#basicSearchInputBox, input[aria-label^='Search for']", keyword)
        self._fill("input[aria-label='Starting Recorded Date']", start.strftime("%m/%d/%Y"))
        self._fill("input[aria-label='Ending Recorded Date']", end.strftime("%m/%d/%Y"))
        self._page.keyboard.press("Escape")   # close the date picker
        n = len(self._frames)
        self._page.locator("button[type=submit]", has_text="Search").first.click(timeout=10000)
        state = self._wait_frame(n)
        all_rows = list(state["rows"])
        # further pages: the results page's own "next page" control
        pages = 1
        while not state["timedOut"] and state["total"] and len(all_rows) < state["total"] and pages < 40:
            nxt = self._page.locator("button[aria-label*='next' i], a[aria-label*='next' i]").first
            try:
                if not nxt.count() or nxt.is_disabled():
                    break
                n = len(self._frames)
                self._page.wait_for_timeout(int(random.uniform(3000, 6000)))
                nxt.click(timeout=10000)
            except Exception:  # noqa: BLE001
                break
            page_state = self._wait_frame(n)
            if page_state["timedOut"] or not page_state["rows"]:
                break
            all_rows.extend(page_state["rows"])
            pages += 1
        state["rows"] = all_rows
        state["pages"] = pages
        self._note(label, state)
        return state

    def _note(self, url: str, state: dict) -> None:
        types = {}
        for r in state.get("rows", []):
            types[r.get("doc_type", "")] = types.get(r.get("doc_type", ""), 0) + 1
        self.debug.append({"search": url, "types": types, "rows": len(state.get("rows", [])), "total": state.get("total"),
                           "timedOut": state.get("timedOut"), "rejected": state.get("rejected", ""),
                           "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})

    # ---- one keyword / one window --------------------------------------

    def search_window(self, keyword: str, department: str, start: date, end: date) -> tuple[list[ClerkRecord], int]:
        """All rows for one keyword in one date window. Returns (records kept, raw row count).

        Raises Throttled if the portal times out / rejects twice.
        """
        kept: list[ClerkRecord] = []
        state = self._ui_search(keyword, department, start, end)
        if state.get("timedOut"):
            self.log(f"    {keyword} {start}..{end}: no answer ({state.get('rejected') or 'timeout'}), cooling down")
            time.sleep(60)
            state = self._ui_search(keyword, department, start, end)
            if state.get("timedOut"):
                raise Throttled(f"{keyword} {start}..{end}: {state.get('rejected') or 'timed out'} twice")
        rows = state["rows"]
        if state.get("total") and len(rows) < state["total"]:
            self.log(f"    {keyword} {start}..{end}: only {len(rows)} of {state['total']} rows came back")
        for r in rows:
            cat = categorize(r["doc_type"], r["grantor"], r["grantee"]) or \
                  categorize(r.get("doc_type_code", ""), r["grantor"], r["grantee"])
            if not cat:
                continue
            kept.append(ClerkRecord(
                doc_id=r["doc_id"], doc_number=r["doc_number"], doc_type=r["doc_type"], category=cat,
                recorded=r["recorded"], grantor=r["grantor"], grantee=r["grantee"], legal=r["legal"],
                department=department, keyword=keyword,
            ))
        return kept, len(rows)

    def is_honest(self, canary: dict | None) -> bool:
        """Re-run a search we know has results. Empty => we're being throttled."""
        canary = canary or DEFAULT_CANARY
        start = date.fromisoformat(canary["start"])
        end = date.fromisoformat(canary["end"])
        state = self._ui_search(canary["keyword"], canary.get("department", "RP"), start, end)
        ok = bool(state["rows"])
        self.log(f"    canary {canary['keyword']} {start}..{end}: {'ok' if ok else 'EMPTY - throttled'}")
        return ok
