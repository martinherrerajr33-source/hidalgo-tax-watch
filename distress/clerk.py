"""
clerk.py - Hidalgo County Clerk official public records scraper
(hidalgo.tx.publicsearch.us, the Kofile/Neumo "PublicSearch" platform).

HOW THE PORTAL WORKS (confirmed live, Oct 2026)
------------------------------------------------
This one is NOT a plain HTTP site. The results page is a single-page app
that runs every search over a websocket (wss://.../ws, "@kofile/FETCH_DOCUMENTS"
messages) and renders the table in the browser. So we drive a real headless
Chromium with Playwright and read the rendered table - same idea as a person
using the site, one search at a time.

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


def results_url(keyword: str, department: str, start: date, end: date, offset: int = 0, limit: int = 250) -> str:
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


_EXTRACT_JS = """
() => {
  const body = document.body ? document.body.innerText : '';
  const noResults = /No Results Found/i.test(body);
  const m = body.match(/(\\d[\\d,]*)-(\\d[\\d,]*) of (\\d[\\d,]*) results/);
  const rows = [...document.querySelectorAll('table tbody tr')].map(tr => {
    const cell = c => { const td = tr.querySelector('td.' + c); return td ? td.innerText.trim() : ''; };
    const cb = tr.querySelector('input[id^="table-checkbox-"]');
    return {
      doc_id: cb ? cb.id.replace('table-checkbox-', '') : '',
      grantor: cell('col-3'), grantee: cell('col-4'), doc_type: cell('col-5'),
      recorded: cell('col-6'), doc_number: cell('col-7'), legal: cell('col-9'),
    };
  }).filter(r => r.doc_type || r.doc_number);
  const loading = /Loading Search Results/i.test(document.title);
  const timedOut = /timed out/i.test(body);
  return {noResults, loading, timedOut, rows,
          from: m ? +m[1].replace(/,/g,'') : null, to: m ? +m[2].replace(/,/g,'') : null,
          total: m ? +m[3].replace(/,/g,'') : null};
}
"""


class ClerkScraper:
    """One Playwright browser, reused for every search in a run."""

    def __init__(self, min_gap: float = 15.0, max_gap: float = 25.0, page_timeout: float = 75.0,
                 headless: bool = True, log=print):
        self.min_gap, self.max_gap = min_gap, max_gap
        self.page_timeout = page_timeout
        self.headless = headless
        self.log = log
        self.searches = 0
        self._pw = self._browser = self._page = None
        self._last_search_at = 0.0
        self.debug: list[dict] = []          # one line per search - committed so failures can be diagnosed
        self.debug_dir = None                # set to a Path to also save page text/screenshots

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
        return self

    def __exit__(self, *exc):
        try:
            if self._browser:
                self._browser.close()
        finally:
            if self._pw:
                self._pw.stop()

    # ---- low level ----------------------------------------------------

    def _pace(self):
        gap = random.uniform(self.min_gap, self.max_gap)
        wait = self._last_search_at + gap - time.time()
        if wait > 0:
            time.sleep(wait)
        self._last_search_at = time.time()

    def certified_through(self) -> date | None:
        """The portal only has documents indexed up to this date."""
        self._page.goto(BASE + "/", wait_until="domcontentloaded", timeout=60000)
        try:
            self._page.wait_for_selector("text=Certified through", timeout=30000)
        except Exception:  # noqa: BLE001
            pass
        txt = self._page.inner_text("body")
        m = re.search(r"Certified through\s+(\d{2}/\d{2}/\d{4})", txt)
        return datetime.strptime(m.group(1), "%m/%d/%Y").date() if m else None

    def _load(self, url: str) -> dict:
        self._pace()
        self.searches += 1
        self._page.goto(url, wait_until="domcontentloaded", timeout=60000)
        deadline = time.time() + self.page_timeout
        state = {}
        while time.time() < deadline:
            time.sleep(1.5)
            state = self._page.evaluate(_EXTRACT_JS)
            if state["rows"] and state["total"] is not None:
                # table can paint before every row is in - one more beat
                time.sleep(1.0)
                state = self._page.evaluate(_EXTRACT_JS)
                break
            if state["noResults"] or state["timedOut"]:
                break
        else:
            state["timedOut"] = True
        self._note(url, state)
        return state

    def _note(self, url: str, state: dict) -> None:
        body = ""
        try:
            body = self._page.inner_text("body")[:600]
        except Exception:  # noqa: BLE001
            pass
        entry = {"url": url, "title": self._page.title(), "rows": len(state.get("rows", [])),
                 "total": state.get("total"), "noResults": state.get("noResults"),
                 "timedOut": state.get("timedOut"), "body": body}
        self.debug.append(entry)
        if self.debug_dir and len(self.debug) <= 3:
            try:
                self.debug_dir.mkdir(parents=True, exist_ok=True)
                self._page.screenshot(path=str(self.debug_dir / f"search_{len(self.debug)}.png"), full_page=False)
            except Exception:  # noqa: BLE001
                pass

    # ---- one keyword / one window --------------------------------------

    def search_window(self, keyword: str, department: str, start: date, end: date) -> tuple[list[ClerkRecord], int]:
        """All rows for one keyword in one date window. Returns (records kept, raw row count).

        Raises Throttled if the portal times out twice.
        """
        kept: list[ClerkRecord] = []
        offset, limit, raw = 0, 250, 0
        while True:
            url = results_url(keyword, department, start, end, offset, limit)
            state = self._load(url)
            if state.get("timedOut"):
                self.log(f"    {keyword} {start}..{end}: timed out, cooling down and retrying")
                time.sleep(60)
                state = self._load(url)
                if state.get("timedOut"):
                    raise Throttled(f"{keyword} {start}..{end} timed out twice")
            rows = state["rows"]
            raw += len(rows)
            for r in rows:
                cat = categorize(r["doc_type"], r["grantor"], r["grantee"])
                if not cat:
                    continue
                kept.append(ClerkRecord(
                    doc_id=r["doc_id"], doc_number=r["doc_number"], doc_type=r["doc_type"], category=cat,
                    recorded=r["recorded"], grantor=r["grantor"], grantee=r["grantee"], legal=r["legal"],
                    department=department, keyword=keyword,
                ))
            total, to = state.get("total"), state.get("to")
            if not rows or total is None or to is None or to >= total:
                break
            offset = to  # the page tells us how many it actually showed
        return kept, raw

    def is_honest(self, canary: dict | None) -> bool:
        """Re-run a search we know has results. Empty => we're being throttled."""
        canary = canary or DEFAULT_CANARY
        start = date.fromisoformat(canary["start"])
        end = date.fromisoformat(canary["end"])
        url = results_url(canary["keyword"], canary.get("department", "RP"), start, end)
        state = self._load(url)
        ok = bool(state["rows"])
        self.log(f"    canary {canary['keyword']} {start}..{end}: {'ok' if ok else 'EMPTY - throttled'}")
        return ok
