"""
cad.py - HCAD (TrueProdigy) lookups and record-to-parcel matching.

Same public, anonymous API that subdivision_crawler.py uses (see the header
there for the token handshake). The advanced search also accepts:

    {"ownerName": {"operator": "begins", "value": "BERRONES JOSE"}}

and every hit carries geoID, owner name, situs street, city, legal description
and market value. That's enough to match public records to parcels:

    clerk heirship:  grantee "BERRONES JOSE GUADALUPE DECEASED",
                     legal "EXPRESSWAY HEIGHTS 3 Lot: 7 Block: 11"
    HCAD owner hit:  "BERRONES JOSE G & SAN JUANA",
                     legal "EXPRESSWAY HEIGHTS UT NO. 3 BLK 11 LOT 7"
                     -> E8500-03-011-0007-00   (confirmed live)

MATCH CONFIDENCE
-----------------
  high    lot (and block, when both sides have one) agree AND the subdivision
          names overlap - or the parcel was found by legal description alone
  medium  the name search returned exactly one plausible parcel
  low     several plausible parcels and nothing to tell them apart - kept for
          the "needs review" list but NOT used for scoring
"""

from __future__ import annotations

import time
from dataclasses import dataclass, asdict

import requests

from .names import (Legal, Person, parse_cad_legal, subdivision_similarity, norm_space)

TRUEPRODIGY_BASE = "https://prod-container.trueprodigyapi.com"
TOKEN_URL = f"{TRUEPRODIGY_BASE}/trueprodigy/cadpublic/auth/token"
CONFIG_URL = f"{TRUEPRODIGY_BASE}/public/config/propertysearchadvanced"
SEARCH_URL = f"{TRUEPRODIGY_BASE}/public/property/advancedsearch"
SKIP_PROP_TYPES = {"P", "MN", "BPP"}   # business personal property, minerals


@dataclass
class Parcel:
    geo_id: str
    pid: str
    owner: str
    address: str
    city: str
    legal: str
    market_value: float
    prop_type: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Match:
    geo_id: str
    confidence: str      # high | medium | low
    method: str          # name+legal | legal | name
    person: str          # whose name matched ("" for legal-only)
    n_candidates: int

    def to_dict(self) -> dict:
        return asdict(self)


class CadClient:
    def __init__(self, year: str, delay: float = 0.4, timeout: int = 30):
        self.year = year
        self.delay = delay
        self.timeout = timeout
        self.s = requests.Session()
        self._token = None
        self._token_at = 0.0
        self._subdivisions: list[dict] | None = None
        self._cache: dict[str, list[Parcel]] = {}
        self.parcels: dict[str, Parcel] = {}
        self.calls = 0

    def _auth(self) -> str:
        if self._token is None or time.time() - self._token_at > 240:
            r = self.s.post(TOKEN_URL, json={"office": "Hidalgo"}, timeout=self.timeout)
            r.raise_for_status()
            self._token = r.json()["user"]["token"]
            self._token_at = time.time()
        return self._token

    def _search(self, criteria: dict) -> list[Parcel]:
        key = repr(sorted(criteria.items()))
        if key in self._cache:
            return self._cache[key]
        body = {"advanced": True, "pYear": {"operator": "=", "value": self.year}, "sortOrder": "geoID", **criteria}
        out: list[Parcel] = []
        for attempt in range(3):
            try:
                r = self.s.post(SEARCH_URL, json=body, timeout=self.timeout,
                                headers={"Authorization": self._auth(), "Content-Type": "application/json"})
                self.calls += 1
                if r.status_code == 204 or not r.text.strip():
                    break
                r.raise_for_status()
                for p in r.json().get("results", []):
                    if (p.get("propType") or "").upper() in SKIP_PROP_TYPES or not p.get("geoID"):
                        continue
                    try:
                        mv = float(p.get("marketValue") or 0)
                    except ValueError:
                        mv = 0.0
                    parcel = Parcel(
                        geo_id=p["geoID"], pid=str(p.get("pid") or ""),
                        owner=norm_space(p.get("name") or p.get("displayName") or ""),
                        address=norm_space(p.get("streetPrimary") or ""), city=norm_space(p.get("city") or ""),
                        legal=norm_space(p.get("legalDescription") or ""), market_value=mv,
                        prop_type=p.get("propType") or "",
                    )
                    out.append(parcel)
                    self.parcels[parcel.geo_id] = parcel
                break
            except requests.RequestException:
                self._token = None
                time.sleep(2 * (attempt + 1))
        time.sleep(self.delay)
        self._cache[key] = out
        return out

    def by_owner(self, begins: str) -> list[Parcel]:
        return self._search({"ownerName": {"operator": "begins", "value": begins}})

    def subdivisions(self) -> list[dict]:
        if self._subdivisions is None:
            r = self.s.get(CONFIG_URL, headers={"Authorization": self._auth()}, timeout=self.timeout)
            r.raise_for_status()
            results = r.json()["results"]
            fields = results.values() if isinstance(results, dict) else results
            f = next(f for f in fields if f.get("id") == "abstractSubdivisionName")
            self._subdivisions = f["codefile"]
        return self._subdivisions

    def by_subdivision(self, code_name: str) -> list[Parcel]:
        return self._search({"abstractSubdivisionName": {"operator": "in", "value": [code_name]}})


# ---------------------------------------------------------------- matching

def _legal_fit(parcel: Parcel, legal: Legal | None) -> tuple[bool, float]:
    """(lot/block agree, subdivision similarity)"""
    if not legal or not legal.lot:
        return False, 0.0
    pl = parse_cad_legal(parcel.legal)
    lot_ok = bool(pl.lot) and pl.lot == legal.lot
    block_ok = (not legal.block or not pl.block) or pl.block == legal.block
    return (lot_ok and block_ok), subdivision_similarity(pl.subdivision, legal.subdivision)


def _name_fit(parcel: Parcel, person: Person) -> bool:
    toks = parcel.owner.upper().replace(",", " ").split()
    if not toks:
        return False
    surname_ok = person.last.split()[0] in toks[:2] or any(a.split()[0] in toks[:2] for a in person.alt_lasts)
    first_ok = person.first in toks[:4]
    if not (surname_ok and first_ok):
        return False
    # middle-initial conflict: "JOSE G" on record vs "JOSE ANGEL" on the parcel
    if person.middle:
        try:
            nxt = toks[toks.index(person.first) + 1]
        except (ValueError, IndexError):
            return True
        if nxt not in {"&", "AND", "ET"} and len(nxt) >= 1 and nxt[0] != person.middle[0]:
            return False
    return True


def match_record(cad: CadClient, people: list[Person], legal: Legal | None) -> tuple[Match | None, list[str]]:
    """Find the parcel a record is about. Returns (best match or None, low-confidence candidate geo IDs)."""
    name_hits: dict[str, tuple[Parcel, Person]] = {}
    for person in people:
        for q in person.queries():
            for parcel in cad.by_owner(q):
                if _name_fit(parcel, person):
                    name_hits.setdefault(parcel.geo_id, (parcel, person))
            if name_hits:
                break

    # 1) name + legal description agree
    if legal and legal.lot:
        confirmed = []
        for geo, (parcel, person) in name_hits.items():
            ok, sim = _legal_fit(parcel, legal)
            if ok and (sim >= 0.25 or not legal.subdivision):
                confirmed.append((sim, parcel, person))
        if confirmed:
            confirmed.sort(key=lambda t: -t[0])
            _, parcel, person = confirmed[0]
            return Match(parcel.geo_id, "high", "name+legal", person.last_first, len(name_hits)), []

    # 2) legal description alone (heirs often record before HCAD changes the owner name)
    if legal and legal.lot and legal.subdivision:
        subs = sorted(cad.subdivisions(),
                      key=lambda c: -subdivision_similarity(c.get("codeDescription", ""), legal.subdivision))
        for code in subs[:3]:
            if subdivision_similarity(code.get("codeDescription", ""), legal.subdivision) < 0.5:
                break
            fits = [p for p in cad.by_subdivision(code["codeName"]) if _legal_fit(p, legal)[0]]
            if len(fits) == 1:
                return Match(fits[0].geo_id, "high", "legal", "", 1), []

    # 3) name only
    if len(name_hits) == 1:
        geo, (parcel, person) = next(iter(name_hits.items()))
        return Match(geo, "medium", "name", person.last_first, 1), []
    if name_hits and len(people) > 1:
        # Co-owners break ties: "BERRONES JOSE G & SAN JUANA" beats "BERRONES JOSE ANGEL"
        # when the case also names San Juana Berrones.
        def co_owner_hits(parcel: Parcel, person: Person) -> int:
            owner = " " + parcel.owner.upper() + " "
            return sum(1 for other in people if other is not person and other.first
                       and f" {other.first} " in owner)
        ranked = sorted(((co_owner_hits(p, per), geo, per) for geo, (p, per) in name_hits.items()), reverse=True)
        if ranked[0][0] > 0 and (len(ranked) == 1 or ranked[1][0] < ranked[0][0]):
            _, geo, per = ranked[0]
            return Match(geo, "medium", "name+co-owner", per.last_first, len(name_hits)), []
    if name_hits:
        return None, list(name_hits.keys())[:5]
    return None, []
