"""
Name and legal-description helpers shared by the scrapers and the matcher.

Two naming conventions show up in Hidalgo records:
  - County Clerk index and HCAD owner names are LAST FIRST MIDDLE
    ("BERRONES JOSE GUADALUPE DECEASED", "BERRONES JOSE G & SAN JUANA")
  - Odyssey court case styles are First Middle Last
    ("Alberto Rendon", "Thelma Edith Martinez, a/k/a Thelma E. Martinez")
Everything gets normalized to upper-case LAST FIRST before matching.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Parties that are never the property owner we want to call
_ENTITY_RE = re.compile(
    r"\b("
    r"LLC|L\.L\.C|INC|CORP|CORPORATION|COMPANY|CO\b|LTD|LP|L\.P|PLLC|"
    r"BANK|BANCO|N\.A|NATIONAL ASSOC|MORTGAGE|LENDING|LOAN|FINANCIAL|FINANCE|CREDIT|"
    r"FEDERAL|SAVINGS|CAPITAL|SERVICING|SERVICES|INVESTMENT|HOLDINGS|PROPERTIES|"
    r"CITY|COUNTY|STATE|TEXAS|UNITED STATES|U\.S\.|USA|IRS|INTERNAL REVENUE|DEPARTMENT|"
    r"COMMISSION|COMPTROLLER|ATTORNEY GENERAL|DISTRICT|ISD|SCHOOL|COLLEGE|UNIVERSITY|"
    r"IRRIGATION|DRAINAGE|EMERGENCY SERVICES|AUTHORITY|HOSPITAL|CLINIC|MEDICAL|"
    r"ASSOCIATION|ASSN|HOA|OWNERS|CHURCH|MINISTR|SECRETARY|ADMINISTRATOR OF|"
    r"TRUSTEE|SUBSTITUTE|UNKNOWN|HEIRS|PUBLIC|PUBLISHING|ELECTRIC|UTILIT|"
    r"CONSTRUCTION|BUILDERS|ROOFING|PLUMBING|SUPPLY|MATERIALS|LUMBER|HOME DEPOT|"
    r"MIDLAND|PORTFOLIO|RECOVERY|COLLECTION|ACCEPTANCE|DISCOVER|CAPITAL ONE|AMEX|"
    r"RELX|WORKFORCE|ESTATE OF"
    r")\b",
    re.I,
)

_SUFFIXES = {"JR", "SR", "II", "III", "IV", "V"}
_DECEASED_RE = re.compile(r"\(?\bDECEASED\b\)?|\bDEC'D\b|\bDECD\b", re.I)
_AKA_RE = re.compile(r"\b(A/K/A|AKA|F/K/A|FKA|N/K/A|D/B/A|DBA)\b.*$", re.I)
_ETAL_RE = re.compile(r"\bET\s*AL\b|\bET\s*UX\b|\bET\s*VIR\b", re.I)


def norm_space(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def is_entity(name: str) -> bool:
    return bool(_ENTITY_RE.search(name or ""))


def strip_deceased(name: str) -> tuple[str, bool]:
    deceased = bool(_DECEASED_RE.search(name or ""))
    cleaned = _DECEASED_RE.sub(" ", name or "")
    return norm_space(cleaned), deceased


def _clean_tokens(name: str) -> list[str]:
    import unicodedata
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    name = _AKA_RE.sub("", name)
    name = _ETAL_RE.sub("", name)
    name = re.sub(r"[^A-Za-z&' -]", " ", name)
    toks = [t for t in norm_space(name).upper().split(" ") if t]
    return [t for t in toks if t not in _SUFFIXES]


@dataclass
class Person:
    """A person named on a record, normalized to LAST FIRST."""
    raw: str
    last: str
    first: str
    middle: str = ""
    deceased: bool = False
    alt_lasts: list[str] = field(default_factory=list)  # other plausible surnames (compound Hispanic names)

    @property
    def last_first(self) -> str:
        return norm_space(f"{self.last} {self.first}")

    def queries(self) -> list[str]:
        """HCAD 'ownerName begins' queries to try, best first."""
        qs = [self.last_first]
        for alt in self.alt_lasts:
            qs.append(norm_space(f"{alt} {self.first}"))
        out = []
        for q in qs:
            if q and q not in out and len(q.split()) >= 2:
                out.append(q)
        return out

    def to_dict(self) -> dict:
        return {"raw": self.raw, "last": self.last, "first": self.first, "middle": self.middle,
                "deceased": self.deceased, "alt_lasts": self.alt_lasts}


def person_from_last_first(raw: str) -> Person | None:
    """'BERRONES JOSE GUADALUPE DECEASED' -> Person(last=BERRONES, first=JOSE, ...).

    Clerk-index and HCAD names. Anything after '&' (a co-owner) is dropped.
    """
    if not raw or is_entity(raw):
        return None
    name, deceased = strip_deceased(raw)
    name = name.split("&")[0]
    toks = _clean_tokens(name)
    if len(toks) < 2:
        return None
    return Person(raw=raw, last=toks[0], first=toks[1], middle=" ".join(toks[2:]), deceased=deceased)


def person_from_first_last(raw: str) -> Person | None:
    """'Thelma Edith Martinez' / 'Adelina P. (Deceased) Alamia' / 'Rendon, Alberto' -> Person.

    Odyssey style. A comma means it's already 'Last, First'.
    """
    if not raw or is_entity(raw):
        return None
    name, deceased = strip_deceased(raw)
    if "," in name:
        last_part, first_part = name.split(",", 1)
        lt, ft = _clean_tokens(last_part), _clean_tokens(first_part)
        if not lt or not ft:
            return None
        return Person(raw=raw, last=" ".join(lt), first=ft[0], middle=" ".join(ft[1:]), deceased=deceased,
                      alt_lasts=[lt[0]] if len(lt) > 1 else [])
    toks = _clean_tokens(name)
    if len(toks) >= 3 and len(toks[-1]) == 1:
        toks = toks[:-1]   # stray trailing initial
    if len(toks) < 2:
        return None
    first = toks[0]
    last = toks[-1]
    alts = []
    if len(toks) >= 3 and len(toks[-2]) > 1:
        # "Maria Del Rosario Gonzalez Gray" - father's surname is usually 2nd-to-last
        alts.append(toks[-2])
        if toks[-2] in {"DE", "DEL", "LA", "LOS", "DELA"} and len(toks) >= 4:
            alts.append(" ".join(toks[-2:]))
    return Person(raw=raw, last=last, first=first, middle=" ".join(toks[1:-1]), deceased=deceased, alt_lasts=alts)


def split_case_parties(side: str) -> list[str]:
    """Split one side of an Odyssey style ('A, B, a/k/a C, D Jr.') into party names."""
    parts = [norm_space(p) for p in (side or "").split(",")]
    out: list[str] = []
    for p in parts:
        if not p:
            continue
        if re.match(r"^(a/k/a|aka|f/k/a|d/b/a)\b", p, re.I):
            continue  # alias of the previous party
        if p.upper().rstrip(".") in _SUFFIXES and out:
            continue  # 'Benigno Lozano, Jr.'
        out.append(p)
    return out


# ---------------------------------------------------------------- legal descriptions

_STOP_SUBD = {"SUBD", "SUBDIVISION", "SUBDUCTION", "SUB", "SDN", "SWL", "SPA", "ADDN", "ADDITION", "ADD",
              "UT", "UNIT", "NO", "THE", "OF", "PH", "PHASE", "SEC", "SECTION", "REPLAT", "AMENDED",
              "AMD", "ESTATES", "EST", "TOWNSITE", "ORIGINAL", "ORIG", "TR", "TRACT", "CITY"}


@dataclass
class Legal:
    subdivision: str = ""
    lot: str = ""
    block: str = ""

    def to_dict(self) -> dict:
        return {"subdivision": self.subdivision, "lot": self.lot, "block": self.block}


def _num(s: str) -> str:
    s = (s or "").strip().upper()
    return s.lstrip("0") or ("0" if s else "")


def parse_clerk_legal(text: str) -> Legal:
    """'Subdivision- Name: EXPRESSWAY HEIGHTS 3 Lot: 7 Block: 11, Reference - 18 / 14'"""
    t = norm_space(text).upper()
    leg = Legal()
    m = re.search(r"NAME:\s*(.+?)(?=\s+LOT:|\s+BLOCK:|,|$)", t)
    if m:
        leg.subdivision = m.group(1).strip()
    m = re.search(r"LOT:\s*([A-Z0-9-]+)", t)
    if m:
        leg.lot = _num(m.group(1))
    m = re.search(r"BLOCK:\s*([A-Z0-9-]+)", t)
    if m:
        leg.block = _num(m.group(1))
    return leg


def parse_cad_legal(text: str) -> Legal:
    """'EXPRESSWAY HEIGHTS UT NO. 3 BLK 11 LOT 7' / 'EL CHARRO LOT 4 BLK 11'"""
    t = norm_space(text).upper()
    leg = Legal()
    m = re.search(r"\bLOTS?\s+([A-Z0-9-]+)", t)
    if m:
        leg.lot = _num(m.group(1))
    m = re.search(r"\b(?:BLK|BLOCK)\s+([A-Z0-9-]+)", t)
    if m:
        leg.block = _num(m.group(1))
    sub = re.split(r"\b(?:LOTS?|BLK|BLOCK)\b", t)[0]
    leg.subdivision = sub.strip(" ,")
    return leg


def subdivision_tokens(name: str) -> set[str]:
    t = re.sub(r"[^A-Z0-9 ]", " ", (name or "").upper())
    return {w for w in t.split() if w and w not in _STOP_SUBD}


def subdivision_similarity(a: str, b: str) -> float:
    ta, tb = subdivision_tokens(a), subdivision_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)
