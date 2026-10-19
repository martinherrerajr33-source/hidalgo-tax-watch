"""
court.py - Hidalgo County Odyssey public access (pa.co.hidalgo.tx.us) scraper.

HOW THE PORTAL WORKS (confirmed live, Oct 2026)
------------------------------------------------
Classic Tyler Odyssey "Records Inquiry". No captcha, no login. It is an
ASP.NET WebForms site, so every search is:

  1. GET  default.aspx                       -> sets the .ASPXFORMSPUBLICACCESS cookie
  2. POST Search.aspx?ID=200 (NodeID, NodeDesc)  -> picks "All Courts" for the session
     (this is what the site's own LaunchSearch() javascript does - skipping it
     makes every search bounce to ErrorOccured.aspx)
  3. GET  Search.aspx?ID=200                 -> fresh __VIEWSTATE / __EVENTVALIDATION
  4. POST Search.aspx?ID=200 with the form   -> results table

Results rows look like:
    <tr><td><a href="CaseDetail.aspx?CaseID=5071026">T-1086-26-B</a></td>
        <td>Hidalgo County, ... VS. Alberto Rendon, Alda Rendon, James S. Bates</td>
        <td><div>09/01/2026</div><div>93rd District Court</div></td>
        <td><div>Tax Cases (OCA)</div><div>Filed</div></td></tr>

THE THREE SEARCHES WE RUN
--------------------------
  * Tax suits  - party search, business name "HIDALGO COUNTY", Open cases only,
                 by filed-date window. The county is a plaintiff on essentially
                 every delinquent-tax suit (with the school/drainage/ESD
                 districts), case numbers T-####-YY-X, type "Tax Cases (OCA)".
  * Probate    - case number wildcard "P*" sorted newest first. Odyssey caps a
                 result set at 200 rows, which is ~1-2 months of probate
                 filings, so a daily run never misses any.
  * Divorce    - case number wildcard "F*" newest first, kept only when the
                 case type says Divorce. 200 rows is ~a week, fine for a daily run.
"""

from __future__ import annotations

import html as html_module
import random
import re
import time
from dataclasses import dataclass, asdict, field
from datetime import date, datetime, timedelta, timezone

import requests

from .names import person_from_first_last, split_case_parties, norm_space, is_entity

BASE = "https://pa.co.hidalgo.tx.us"
ALL_COURTS_NODES = ("100,110,120,130,140,150,155,157,160,161,165,166,200,210,220,230,240,250,"
                    "260,270,280,222,290,295,296,297,215,216,217,218,221,224,227,228,229")

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
}

_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S | re.I)
_DIV_RE = re.compile(r"<div[^>]*>(.*?)</div>", re.S | re.I)
_CASEID_RE = re.compile(r'CaseDetail\.aspx\?CaseID=(\d+)[^>]*>(.*?)</a>', re.S | re.I)
_HIDDEN_RE = re.compile(r'<input[^>]+type="hidden"[^>]*>', re.I)


def _text(fragment: str) -> str:
    t = re.sub(r"<[^>]+>", " ", fragment)
    return norm_space(html_module.unescape(t).replace("\xa0", " "))


@dataclass
class CourtCase:
    case_id: str
    case_number: str
    style: str
    filed: str           # MM/DD/YYYY
    court: str
    case_type: str
    status: str
    kind: str            # tax_suit | probate | divorce
    plaintiffs: list[str] = field(default_factory=list)
    parties: list[dict] = field(default_factory=list)   # Person.to_dict() for the people we'd match
    any_deceased: bool = False
    first_seen: str = field(default_factory=lambda: datetime.now(timezone.utc).date().isoformat())
    last_seen: str = field(default_factory=lambda: datetime.now(timezone.utc).date().isoformat())

    @property
    def url(self) -> str:
        return f"{BASE}/CaseDetail.aspx?CaseID={self.case_id}"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["url"] = self.url
        return d


def parse_results(page_html: str) -> list[dict]:
    """Pull the case rows out of an Odyssey results page."""
    rows = []
    for row_html in _ROW_RE.findall(page_html):
        m = _CASEID_RE.search(row_html)
        if not m:
            continue
        cells = _CELL_RE.findall(row_html)
        if len(cells) < 4:
            continue
        filed_divs = [_text(d) for d in _DIV_RE.findall(cells[-2])]
        type_divs = [_text(d) for d in _DIV_RE.findall(cells[-1])]
        rows.append({
            "case_id": m.group(1),
            "case_number": _text(m.group(2)),
            "style": _text(cells[1]),
            "filed": filed_divs[0] if filed_divs else _text(cells[-2])[:10],
            "court": filed_divs[1] if len(filed_divs) > 1 else "",
            "case_type": type_divs[0] if type_divs else _text(cells[-1]),
            "status": type_divs[1] if len(type_divs) > 1 else "",
        })
    return rows


def record_count(page_html: str) -> int | None:
    m = re.search(r"Record Count:\s*(\d+)", _text(page_html))
    return int(m.group(1)) if m else None


def _split_style(style: str) -> tuple[str, str]:
    parts = re.split(r"\s+VS\.?\s+|\s+V\.\s+", style, maxsplit=1, flags=re.I)
    if len(parts) == 2:
        return parts[0], parts[1]
    return style, ""


def build_case(row: dict, kind: str) -> CourtCase:
    style = row["style"]
    plaintiffs: list[str] = []
    people = []
    if kind == "tax_suit":
        left, right = _split_style(style)
        plaintiffs = split_case_parties(left)
        for name in split_case_parties(right):
            p = person_from_first_last(name)
            if p:
                people.append(p)
    elif kind == "probate":
        m = re.search(r"ESTATE OF\s+(.+?)(?:,?\s*(?:DECEASED|DEC'D)\b|$)", style, re.I)
        if m:
            p = person_from_first_last(m.group(1))
            if p:
                p.deceased = True
                people.append(p)
    elif kind == "divorce":
        s = re.sub(r"^IN THE MATTER OF THE\s*MARRIAGE OF\s*", "", style, flags=re.I)
        for name in re.split(r"\s+(?:VS\.?|AND)\s+", s, flags=re.I):
            p = person_from_first_last(name)
            if p:
                people.append(p)
    return CourtCase(
        case_id=row["case_id"], case_number=row["case_number"], style=style,
        filed=row["filed"], court=row["court"], case_type=row["case_type"], status=row["status"],
        kind=kind, plaintiffs=plaintiffs, parties=[p.to_dict() for p in people],
        any_deceased=any(p.deceased for p in people) or bool(re.search(r"DECEASED|UNKNOWN HEIRS", style, re.I)),
    )


def ca_bundle_with_missing_intermediates(host: str) -> str:
    """The Odyssey server doesn't send its intermediate certificate. Browsers fetch it
    themselves (from the URL inside the certificate, "AIA"); Python doesn't, so every
    request fails with CERTIFICATE_VERIFY_FAILED. Do the same thing a browser does:
    read the server's certificate, download its issuer(s) from the AIA URL, and add
    them to the normal trusted bundle. Verification stays fully on.
    """
    import socket
    import ssl
    import tempfile
    import urllib.request

    import certifi
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from cryptography.x509.oid import AuthorityInformationAccessOID, ExtensionOID

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE   # only to READ the leaf certificate; nothing is sent
    with socket.create_connection((host, 443), timeout=30) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as tls:
            der = tls.getpeercert(binary_form=True)
    cert = x509.load_der_x509_certificate(der)
    extra = []
    for _ in range(3):
        try:
            aia = cert.extensions.get_extension_for_oid(ExtensionOID.AUTHORITY_INFORMATION_ACCESS).value
        except x509.ExtensionNotFound:
            break
        urls = [d.access_location.value for d in aia if d.access_method == AuthorityInformationAccessOID.CA_ISSUERS]
        if not urls:
            break
        data = urllib.request.urlopen(urls[0], timeout=30).read()
        try:
            issuer = x509.load_der_x509_certificate(data)
        except ValueError:
            issuer = x509.load_pem_x509_certificate(data)
        extra.append(issuer.public_bytes(serialization.Encoding.PEM))
        if issuer.issuer == issuer.subject:
            break
        cert = issuer
    with open(certifi.where(), "rb") as f:
        bundle = f.read()
    tmp = tempfile.NamedTemporaryFile("wb", suffix=".pem", delete=False)
    tmp.write(bundle + b"\n" + b"".join(extra))
    tmp.close()
    return tmp.name


class OdysseyClient:
    def __init__(self, delay_seconds: float = 2.0, timeout: int = 60, max_retries: int = 3):
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        self.delay = delay_seconds
        self.timeout = timeout
        self.max_retries = max_retries
        self._ready = False

    def _wait(self):
        time.sleep(self.delay + random.uniform(0, 0.8))

    def _req(self, method: str, url: str, **kw) -> requests.Response:
        last = None
        for attempt in range(self.max_retries):
            try:
                r = self.s.request(method, url, timeout=self.timeout, **kw)
                r.raise_for_status()
                if "ErrorOccured.aspx" in r.url or "Public Access Error" in r.text:
                    raise RuntimeError("Odyssey returned its error page")
                return r
            except Exception as exc:  # noqa: BLE001
                last = exc
                self._ready = False
                time.sleep(4 * (attempt + 1))
                self._open_session()
        raise last

    def _open_session(self):
        self.s.cookies.clear()
        try:
            self.s.get(f"{BASE}/default.aspx", timeout=self.timeout)
        except requests.exceptions.SSLError:
            self.s.verify = ca_bundle_with_missing_intermediates("pa.co.hidalgo.tx.us")
            self.s.get(f"{BASE}/default.aspx", timeout=self.timeout)
        self.s.post(f"{BASE}/Search.aspx?ID=200",
                    data={"NodeID": ALL_COURTS_NODES, "NodeDesc": "All Courts"}, timeout=self.timeout)
        self._ready = True

    def _search(self, fields: dict) -> str:
        if not self._ready:
            self._open_session()
        form_page = self._req("GET", f"{BASE}/Search.aspx?ID=200").text
        data = {}
        for tag in _HIDDEN_RE.findall(form_page):
            name = re.search(r'name="([^"]+)"', tag)
            val = re.search(r'value="([^"]*)"', tag)
            if name:
                data[name.group(1)] = html_module.unescape(val.group(1)) if val else ""
        data.setdefault("NodeID", ALL_COURTS_NODES)
        data.setdefault("NodeDesc", "All Courts")
        data.update(fields)
        data["SearchSubmit"] = "Search"
        r = self._req("POST", f"{BASE}/Search.aspx?ID=200", data=data)
        self._wait()
        return r.text

    # ---- search flavours -------------------------------------------------

    def business_party_search(self, business: str, filed_after: date, filed_before: date,
                              status: str = "open") -> list[dict]:
        status_fields = {
            "open": {"CaseStatusType": "1", "StatusType": "true", "AllStatusTypes": "false"},
            "all": {"CaseStatusType": "0", "StatusType": "true", "AllStatusTypes": "true"},
        }[status]
        page = self._search({
            "SearchBy": "1", "PartySearchMode": "BusinessName", "LastName": business,
            "DateFiledOnAfter": filed_after.strftime("%m/%d/%Y"),
            "DateFiledOnBefore": filed_before.strftime("%m/%d/%Y"),
            "SortBy": "fileddate", "SearchType": "PARTY", "SearchMode": "BUSINESSNAME",
            "NameTypeKy": "DBA", "BaseConnKy": "DF", **status_fields,
        })
        return parse_results(page)

    def case_number_search(self, pattern: str, newest_first: bool = True) -> list[dict]:
        page = self._search({
            "SearchBy": "0", "CaseSearchMode": "CaseNumber", "CaseSearchValue": pattern,
            "CaseStatusType": "0", "SortBy": "fileddaterev" if newest_first else "fileddate",
            "SearchType": "CASE", "SearchMode": "CASENUMBER", "StatusType": "true", "AllStatusTypes": "true",
        })
        return parse_results(page)


# ---- high-level jobs -------------------------------------------------------

ODYSSEY_ROW_CAP = 200


def fetch_open_tax_suits(client: OdysseyClient, start: date, end: date, window_days: int = 31,
                         log=print) -> list[CourtCase]:
    """Every OPEN tax suit filed between start and end, walked in date windows.

    A window that comes back at the 200-row cap is split in half and re-run so
    nothing gets truncated.
    """
    out: dict[str, CourtCase] = {}
    windows = []
    d = start
    while d <= end:
        w_end = min(d + timedelta(days=window_days - 1), end)
        windows.append((d, w_end))
        d = w_end + timedelta(days=1)

    while windows:
        a, b = windows.pop(0)
        rows = client.business_party_search("HIDALGO COUNTY", a, b, status="open")
        if len(rows) >= ODYSSEY_ROW_CAP and a < b:
            mid = a + (b - a) // 2
            windows[:0] = [(a, mid), (mid + timedelta(days=1), b)]
            log(f"  tax suits {a}..{b}: hit the {ODYSSEY_ROW_CAP}-row cap, splitting")
            continue
        kept = 0
        for row in rows:
            if "TAX" not in row["case_type"].upper():
                continue
            out[row["case_id"]] = build_case(row, "tax_suit")
            kept += 1
        log(f"  tax suits filed {a}..{b}: {kept} open")
    return list(out.values())


def fetch_recent(client: OdysseyClient, kind: str, log=print) -> list[CourtCase]:
    pattern, type_filter = {
        "probate": ("P*", None),
        "divorce": ("F*", "DIVORCE"),
    }[kind]
    rows = client.case_number_search(pattern, newest_first=True)
    cases = []
    for row in rows:
        if type_filter and type_filter not in row["case_type"].upper():
            continue
        if kind == "probate" and not re.search(r"ESTATE|HEIRSHIP|WILL|ADMINISTRATION|PROBATE",
                                               row["case_type"] + " " + row["style"], re.I):
            continue
        cases.append(build_case(row, kind))
    log(f"  {kind}: {len(rows)} newest cases scanned, {len(cases)} kept")
    return cases


# ---- case detail: who's on the case --------------------------------------
#
# Odyssey's CaseDetail.aspx "Party Information" table lays each party out as
#   <tr><th id="PIr01">Applicant</th><th id="PIr11">Garcia, Maria</th> ... <th>Attorneys</th></tr>
#   <tr><td headers="PIr01 PIr11">123 MAIN ST<br>MCALLEN, TX 78501</td> <td>JOHN DOE Retained 956-555-1234(W)</td></tr>
# The parser below keys off those ids, and falls back to plain text if the
# layout differs. Applicants / executors / administrators on a probate case are
# the people who control the estate - the best point of contact for an heir deal.

_PARTY_ROLE_RE = re.compile(
    r'<th[^>]*id="(PIr0\d+)"[^>]*>(.*?)</th>\s*<th[^>]*id="(PIr1\d+)"[^>]*>(.*?)</th>(.*?)(?=<th[^>]*id="PIr0\d+"|</table>)',
    re.S | re.I)
_PHONE_RE = re.compile(r"\(?\b(\d{3})\)?[-. ]?(\d{3})[-. ]?(\d{4})\b")
_ROLE_WORDS = ("applicant", "executor", "executrix", "administrator", "administratrix", "personal representative",
               "petitioner", "respondent", "defendant", "plaintiff", "decedent", "deceased", "heir", "guardian",
               "ward", "proposed ward", "trustee", "beneficiary", "devisee", "independent")


def _cell_lines(fragment: str) -> list[str]:
    t = re.sub(r"<br\s*/?>", "\n", fragment, flags=re.I)
    t = re.sub(r"</(td|tr|div|p)>", "\n", t, flags=re.I)
    t = html_module.unescape(re.sub(r"<[^>]+>", " ", t)).replace("\xa0", " ")
    return [norm_space(x) for x in t.split("\n") if norm_space(x)]


def parse_case_parties(page_html: str) -> list[dict]:
    """CaseDetail.aspx -> [{role, name, address, attorney, attorney_phone}]"""
    i = page_html.find("Party Information")
    if i < 0:
        return []
    j = page_html.find("Events & Orders", i)
    block = page_html[i: j if j > 0 else i + 60000]
    parties = []
    for _, role_html, _, name_html, rest in _PARTY_ROLE_RE.findall(block):
        role = _text(role_html)
        name = _text(name_html)
        if not name or not role:
            continue
        # cells after the name row: first the party's own details (address), then attorney cells
        tds = re.findall(r"<td[^>]*>(.*?)</td>", rest, re.S | re.I)
        address_lines, attorney, phone = [], "", ""
        for td in tds:
            lines = _cell_lines(td)
            if not lines:
                continue
            joined = " ".join(lines)
            if re.search(r"\b(Retained|Court Appointed|Pro Se|Lead Attorney)\b", joined, re.I):
                attorney = attorney or lines[0]
                m = _PHONE_RE.search(joined)
                if m and not phone:
                    phone = f"({m.group(1)}) {m.group(2)}-{m.group(3)}"
            elif not address_lines and re.search(r"\d", joined) and not re.search(r"\b(DOB|SID|Race|Male|Female)\b", joined):
                address_lines = [x for x in lines if not re.match(r"^(Height|Weight)", x)]
        parties.append({"role": role, "name": name, "address": ", ".join(address_lines)[:160],
                        "attorney": attorney[:80], "attorney_phone": phone})
    return parties


def fetch_case_parties(client: "OdysseyClient", case_id: str) -> tuple[list[dict], str]:
    if not client._ready:
        client._open_session()
    r = client._req("GET", f"{BASE}/CaseDetail.aspx", params={"CaseID": case_id})
    client._wait()
    return parse_case_parties(r.text), r.text
