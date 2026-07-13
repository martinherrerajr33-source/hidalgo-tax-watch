"""
hidalgo_tax_checker.py

Core engine for checking Hidalgo County property tax delinquency status
against the county's public tax lookup (actweb.acttax.com).

HOW THIS MAPS TO WHAT YOU SEE ON SCREEN
----------------------------------------
The tax site (https://actweb.acttax.com/act_webdev/hidalgo/index.jsp) has two
search fields that both work, for two different kinds of ID - confirmed live
against the real site:

  "Account No."       (searchby=4) - accepts your GEO ID directly, dashes
                                      and all (e.g. C1622-00-000-0015-00),
                                      or a plain account number
                                      (e.g. A100002000000100).
  "CAD Reference No." (searchby=5) - accepts PropID, the plain numeric ID
                                      from the Hidalgo CAD property search
                                      (hidalgo.prodigycad.com), e.g. 109048.
                                      Does NOT accept the dashed GEO ID.

So if you already have GEO IDs (from CAD, DealMachine, wherever), you don't
need to go get PropID at all - just feed the GEO ID straight in. This module
auto-detects which one you handed it (anything with a letter in it is
treated as a GEO ID/account number; pure digits are treated as a PropID) and
routes to the right search field, with fallbacks if the first guess is wrong.

Example, same parcel, three different-looking IDs (all confirmed live):
    CAD portal GEO ID           : A1000-02-000-0001-00  -> Account No. field
    CAD portal Ref ID           : 160212                  -> neither field
    CAD portal PropID           : 109048                  -> CAD Reference No. field
    ACT tax account number      : A100002000000100        -> Account No. field

THE SITE ITSELF IS STATELESS
-----------------------------
No login, no session cookies. Two simple HTTP calls per property:
  1. POST to showlist.jsp with the CAD reference -> get back the account number(s)
  2. GET showdetail2.jsp?can=<account_number> -> get back the actual balance detail

This means we don't need a browser/Playwright for this site - plain
`requests` is enough, which is faster and more reliable to run on a
schedule (e.g. GitHub Actions).
"""

from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

import requests

BASE_URL = "https://actweb.acttax.com/act_webdev/hidalgo"
SEARCH_URL = f"{BASE_URL}/showlist.jsp"
DETAIL_URL = f"{BASE_URL}/showdetail2.jsp"

# Confirmed against the live form:
#   searchby=4 is "Account No."       - accepts GEO ID / account number, dashes and all
#   searchby=5 is "CAD Reference No." - accepts PropID (the CAD portal's plain numeric ID)
SEARCHBY_ACCOUNT_NO = "4"
SEARCHBY_CAD_REF = "5"

# GEO IDs / account numbers always start with a letter (e.g. "C1622-00-000-0015-00",
# "A100002000000100"). PropID / CAD Reference No. is always plain digits (e.g. "109048").
# We use this to auto-route each input to the right search field.
_HAS_LETTER_RE = re.compile(r"[A-Za-z]")

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
}

MONEY_RE = r"\$?([\d,]+(?:\.\d{2})?)"


def _to_float(money_str: Optional[str]) -> float:
    """'$1,234.56' / '1,234.56' / None / 'Not Received' -> float (0.0 on anything unparseable)."""
    if not money_str:
        return 0.0
    cleaned = money_str.replace("$", "").replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def _clean_text(html: str) -> str:
    """Turn <br> into newlines then strip remaining tags, for regex-friendly text."""
    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return text.strip()


@dataclass
class TaxRecord:
    cad_reference: str
    account_number: Optional[str] = None
    found: bool = False
    match_count: int = 0
    owner_name: str = ""
    owner_address: str = ""
    property_site_address: str = ""
    legal_description: str = ""
    current_tax_levy: float = 0.0
    current_amount_due: float = 0.0
    prior_year_amount_due: float = 0.0
    total_amount_due: float = 0.0
    last_payment_amount: str = ""
    last_payment_date: str = ""
    active_lawsuits: str = ""
    gross_value: float = 0.0
    land_value: float = 0.0
    improvement_value: float = 0.0
    exemptions: str = ""
    status: str = "unknown"  # delinquent | current_due | paid_up | not_found | error
    error: str = ""
    checked_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    # carried through from a CAD export, if the input CSV had these columns
    source_owner: str = ""
    source_address: str = ""
    source_city: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class HidalgoTaxChecker:
    def __init__(self, delay_seconds: float = 0.8, timeout: int = 20, max_retries: int = 3):
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self.delay_seconds = delay_seconds
        self.timeout = timeout
        self.max_retries = max_retries

    # ---------- low-level requests ----------

    def _post_with_retry(self, url: str, data: dict) -> requests.Response:
        last_exc = None
        for attempt in range(self.max_retries):
            try:
                resp = self.session.post(url, data=data, timeout=self.timeout)
                resp.raise_for_status()
                return resp
            except requests.RequestException as exc:
                last_exc = exc
                time.sleep(1.5 * (attempt + 1))
        raise last_exc

    def _get_with_retry(self, url: str, params: dict) -> requests.Response:
        last_exc = None
        for attempt in range(self.max_retries):
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
                resp.raise_for_status()
                return resp
            except requests.RequestException as exc:
                last_exc = exc
                time.sleep(1.5 * (attempt + 1))
        raise last_exc

    def _polite_wait(self):
        # small random jitter so requests aren't perfectly periodic
        time.sleep(self.delay_seconds + random.uniform(0, 0.4))

    # ---------- step 1: search by GEO ID / account no., or by PropID / CAD reference ----------

    def _raw_search(self, criteria: str, searchby: str) -> tuple[list[str], int]:
        payload = {
            "criteria": criteria,
            "searchby": searchby,
            "subcriteria": "",
            "subsearchby": "3",
            "submit": "Search",
        }
        resp = self._post_with_retry(SEARCH_URL, payload)
        html = resp.text

        if "found no records" in html.lower():
            return [], 0

        match = re.search(r"There are (\d+) match", html)
        match_count = int(match.group(1)) if match else 0

        account_numbers = re.findall(r"showdetail2\.jsp\?can=([A-Za-z0-9]+)", html)
        seen = set()
        deduped = []
        for a in account_numbers:
            if a not in seen:
                seen.add(a)
                deduped.append(a)
        return deduped, match_count or len(deduped)

    def _search_account_numbers(self, property_id: str) -> tuple[list[str], int]:
        """
        Auto-routes based on the shape of the input:
          - has a letter  -> GEO ID / account number -> search "Account No." (searchby=4)
          - digits only   -> PropID                   -> search "CAD Reference No." (searchby=5)
        Falls back to stripping dashes, and then to the other search field, before
        giving up - some accounts don't follow the usual pattern.
        """
        looks_like_geo_or_account = bool(_HAS_LETTER_RE.search(property_id))
        primary_searchby = SEARCHBY_ACCOUNT_NO if looks_like_geo_or_account else SEARCHBY_CAD_REF

        account_numbers, match_count = self._raw_search(property_id, primary_searchby)
        if account_numbers:
            return account_numbers, match_count

        # Fallback 1: same field, dashes stripped (covers any edge case where a
        # particular account doesn't tolerate them, even though most do)
        if "-" in property_id:
            self._polite_wait()
            account_numbers, match_count = self._raw_search(property_id.replace("-", ""), primary_searchby)
            if account_numbers:
                return account_numbers, match_count

        # Fallback 2: try the other search field entirely, in case the input's
        # shape guessed wrong (e.g. a numeric-only account number)
        other_searchby = SEARCHBY_CAD_REF if primary_searchby == SEARCHBY_ACCOUNT_NO else SEARCHBY_ACCOUNT_NO
        self._polite_wait()
        return self._raw_search(property_id, other_searchby)

    # ---------- step 2: pull the actual balance detail ----------

    def _get_detail(self, account_number: str) -> dict:
        resp = self._get_with_retry(DETAIL_URL, {"can": account_number})
        text = _clean_text(resp.text)

        def grab(label: str, stop_labels: list[str]) -> str:
            stop_pattern = "|".join(re.escape(s) for s in stop_labels)
            pattern = rf"{re.escape(label)}\s*:?\s*(.*?)(?:\n(?:{stop_pattern})|\Z)" if stop_labels else rf"{re.escape(label)}\s*:?\s*(.*)"
            m = re.search(pattern, text, re.DOTALL)
            return m.group(1).strip() if m else ""

        def grab_money(label: str) -> float:
            m = re.search(rf"{re.escape(label)}\s*:?\s*" + MONEY_RE, text)
            return _to_float(m.group(1)) if m else 0.0

        owner_address = grab("Address:", ["Property Site Address"])
        property_site_address = grab("Property Site Address:", ["Legal Description"])
        legal_description = grab("Legal Description:", ["Current Tax Levy"])
        last_payment_amount = grab("Last Payment Amount for Current Year Taxes:", ["Last Payment Date"])
        last_payment_date = grab("Last Payment Date for Current Year Taxes:", ["Active Lawsuits"])
        active_lawsuits = grab("Active Lawsuits:", ["Pending Credit Card"])
        exemptions = grab("Exemptions:", ["Exemption and Tax Rate", "Taxes Due Detail"])

        appraisal_district_number_m = re.search(r"Appraisal District Number:\s*(\S+)", text)

        return {
            "owner_name": owner_address.split("\n")[0].strip() if owner_address else "",
            "owner_address": " ".join(line.strip() for line in owner_address.split("\n")[1:] if line.strip()),
            "property_site_address": " ".join(line.strip() for line in property_site_address.split("\n") if line.strip()),
            "legal_description": " ".join(line.strip() for line in legal_description.split("\n") if line.strip()),
            "current_tax_levy": grab_money("Current Tax Levy:"),
            "current_amount_due": grab_money("Current Amount Due:"),
            "prior_year_amount_due": grab_money("Prior Year Amount Due:"),
            "total_amount_due": grab_money("Total Amount Due:"),
            "last_payment_amount": last_payment_amount,
            "last_payment_date": last_payment_date,
            "active_lawsuits": active_lawsuits,
            "gross_value": grab_money("Gross Value:"),
            "land_value": grab_money("Land Value:"),
            "improvement_value": grab_money("Improvement Value:"),
            "exemptions": exemptions,
            "appraisal_district_number": appraisal_district_number_m.group(1) if appraisal_district_number_m else "",
        }

    # ---------- public entry point ----------

    def check(self, cad_reference: str, source_owner: str = "", source_address: str = "", source_city: str = "") -> TaxRecord:
        cad_reference = str(cad_reference).strip()
        record = TaxRecord(cad_reference=cad_reference, source_owner=source_owner,
                            source_address=source_address, source_city=source_city)
        if not cad_reference:
            record.status = "error"
            record.error = "empty CAD reference / PropID"
            return record

        try:
            account_numbers, match_count = self._search_account_numbers(cad_reference)
            record.match_count = match_count
            self._polite_wait()

            if not account_numbers:
                record.found = False
                record.status = "not_found"
                return record

            # If multiple accounts share a CAD reference (rare - e.g. land + mobile home),
            # we check the first and flag it so it gets a human look.
            account_number = account_numbers[0]
            record.account_number = account_number
            record.found = True

            detail = self._get_detail(account_number)
            record.owner_name = detail["owner_name"]
            record.owner_address = detail["owner_address"]
            record.property_site_address = detail["property_site_address"]
            record.legal_description = detail["legal_description"]
            record.current_tax_levy = detail["current_tax_levy"]
            record.current_amount_due = detail["current_amount_due"]
            record.prior_year_amount_due = detail["prior_year_amount_due"]
            record.total_amount_due = detail["total_amount_due"]
            record.last_payment_amount = detail["last_payment_amount"]
            record.last_payment_date = detail["last_payment_date"]
            record.active_lawsuits = detail["active_lawsuits"]
            record.gross_value = detail["gross_value"]
            record.land_value = detail["land_value"]
            record.improvement_value = detail["improvement_value"]
            record.exemptions = detail["exemptions"]

            if record.prior_year_amount_due > 0:
                record.status = "delinquent"
            elif record.current_amount_due > 0:
                record.status = "current_due"
            else:
                record.status = "paid_up"

            if match_count > 1:
                record.status += "_multi_match"

            self._polite_wait()

        except Exception as exc:  # noqa: BLE001 - we want to record *any* failure and keep going
            record.status = "error"
            record.error = str(exc)

        return record
