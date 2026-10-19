"""
contacts.py - who to call about a property.

Every source names people, but in different roles. For a wholesale deal on a
distressed property the best point of contact is whoever can actually sign or
steer a sale - so contacts are ranked:

  1  Estate representative   applicant / executor / administrator on a probate case
  2  Heir                    signed an affidavit of heirship (they're the family member handling it)
  3  Owner of record         HCAD / tax-office owner, if not known to be deceased
  4  Co-owner / defendant    living people named in the tax suit (often the heirs)
  5  Spouse                  party to a divorce case
  6  Named on a filing       person named on a lien / lis pendens / judgment

People known to be deceased are never offered as a contact; they're listed
separately so you know whose estate it is.
"""

from __future__ import annotations

import re

from .names import person_from_first_last, person_from_last_first, is_entity, norm_space

ESTATE_ROLES = re.compile(r"APPLICANT|EXECUT|ADMINISTRAT|PERSONAL REP|PETITIONER|INDEPENDENT", re.I)
DECEDENT_ROLES = re.compile(r"DECEDENT|DECEASED|ESTATE OF", re.I)

ROLE_RANK = {
    "Estate representative": 1,
    "Heir": 2,
    "Owner of record": 3,
    "Co-owner / tax suit defendant": 4,
    "Spouse": 5,
    "Named on filing": 6,
}


def _key(last: str, first: str) -> str:
    return f"{(last or '').split(' ')[0]}|{first or ''}".upper()


def _pretty(first: str, middle: str, last: str) -> str:
    parts = [first, middle, last]
    return " ".join(p.title() for p in parts if p).strip()


class ContactBook:
    def __init__(self):
        self.people: dict[str, dict] = {}
        self.deceased: dict[str, str] = {}

    def mark_deceased(self, person) -> None:
        if person:
            self.deceased[_key(person.last, person.first)] = _pretty(person.first, "", person.last)

    def add(self, person, role: str, source: str, address: str = "", attorney: str = "", attorney_phone: str = ""):
        if not person:
            return
        if person.deceased:
            self.mark_deceased(person)
            return
        k = _key(person.last, person.first)
        cur = self.people.get(k)
        entry = {
            "name": _pretty(person.first, person.middle, person.last),
            "role": role, "rank": ROLE_RANK.get(role, 9), "source": source,
            "address": norm_space(address), "attorney": attorney, "attorney_phone": attorney_phone,
        }
        if cur is None or entry["rank"] < cur["rank"]:
            if cur:  # keep any address/attorney we already had
                entry["address"] = entry["address"] or cur["address"]
                entry["attorney"] = entry["attorney"] or cur["attorney"]
                entry["attorney_phone"] = entry["attorney_phone"] or cur["attorney_phone"]
            self.people[k] = entry
        else:
            cur["address"] = cur["address"] or entry["address"]
            cur["attorney"] = cur["attorney"] or entry["attorney"]
            cur["attorney_phone"] = cur["attorney_phone"] or entry["attorney_phone"]

    def result(self) -> tuple[list[dict], list[str]]:
        living = [p for k, p in self.people.items() if k not in self.deceased]
        living.sort(key=lambda p: (p["rank"], not p["address"], p["name"]))
        return living, sorted(set(self.deceased.values()))


def split_owner_names(owner: str) -> list:
    """'BERRONES JOSE G & SAN JUANA' -> [Person(BERRONES JOSE G), Person(BERRONES SAN JUANA)]"""
    if not owner or is_entity(owner):
        return []
    parts = [p.strip() for p in re.split(r"\s*&\s*|\s+AND\s+", owner) if p.strip()]
    out = []
    first = person_from_last_first(parts[0]) if parts else None
    if first:
        out.append(first)
    for extra in parts[1:]:
        toks = extra.split()
        p = person_from_last_first(extra) if len(toks) >= 3 else None
        if p is None and first and toks:
            p = person_from_last_first(f"{first.last} {extra}")
        if p:
            out.append(p)
    return out


def split_index_names(field: str) -> list:
    """Clerk index field 'PEREZ ALBERTA DELA ROSA; VILLANUEVA JAIME PEREZ DECEASED' -> people (LAST FIRST)."""
    out = []
    for n in re.split(r"\s*;\s*|\n", field or ""):
        p = person_from_last_first(n)
        if p:
            out.append(p)
    return out


def person_from_court_name(name: str):
    """Odyssey detail names are 'Last, First Middle'; styles are 'First Last'."""
    return person_from_first_last(name)
