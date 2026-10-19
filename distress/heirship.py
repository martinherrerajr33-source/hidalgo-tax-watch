"""
heirship.py - read an Affidavit of Heirship and pull out who actually inherits.

The clerk's index only names the AFFIANT (grantor) and the DECEDENT (grantee).
The affiant is often NOT an heir - Texas wants a disinterested witness, so it's
frequently a neighbor, family friend, pastor or in-law. The heirs (surviving
spouse, children, other relatives) are only named inside the document, so we
OCR the page images and read the standard sections:

    1. Affiant.  My name is Eva Maria Mireles ... I am the daughter-in-law of ...
    4. Surviving Spouse.  Decedent was married to San Juana Ginez Berrones, who
       survived him. Her residence address is 2009 Dallas Dr, Weslaco, TX 78596.
    5. Children.  ... identified as the parents of the following children:
       Nato Berrones -- Date of birth: 10/03/1962 -- Current address: ...
    6. Other Children ... none

Layouts vary (attorney-drafted affidavits read like prose), so every rule here
is a heuristic; anything we can't place stays out rather than being guessed.
"""

from __future__ import annotations

import re

from .names import norm_space

# relationships that make the affiant an heir themselves (or a spouse of one, who
# can still steer the family)
HEIR_RELATIONS = re.compile(
    r"\b(son|daughter|child|wife|husband|spouse|widow|widower|grand(?:son|daughter|child)|"
    r"mother|father|parent|brother|sister|sibling|nephew|niece|heir)\b", re.I)
IN_LAW = re.compile(r"-in-law|\bin law\b|step", re.I)
NON_FAMILY = re.compile(r"\b(neighbou?r|friend|acquaint\w*|pastor|minister|co-?worker|employer|caregiver|"
                        r"not related|no relation|unrelated|disinterested)\b", re.I)

_U = r"A-Za-zÀ-ÖØ-öø-ÿ"
_NAME = (r"([A-ZÀ-Þ][" + _U + r".'\-]+(?:\s+(?:[A-ZÀ-Þ][" + _U + r".'\-]+|de|De|del|Del|la|La|los|Los|y|Y)){1,5})")
_STOP_WORDS = {"Date", "Birth", "Current", "Address", "Decedent", "Deceased", "Texas", "County", "State",
               "The", "This", "Affiant", "Children", "Child", "None", "Residence", "Hidalgo", "Street",
               "Avenue", "Drive", "Road", "Dr", "St", "Ave", "Rd", "Apt", "Unit", "Of", "And", "Who", "Her",
               "His", "Their", "Was", "Is", "Name", "My", "I", "Am", "Married", "Spouse", "Surviving"}
_ADDR = re.compile(r"(\d{2,6}\s+[A-Za-z0-9 .'#-]{3,40}?,?\s+[A-Za-z .'-]{3,25},?\s+(?:TX|Texas|Tx)\.?,?\s*\d{5})")


def _clean_name(s: str) -> str:
    s = norm_space(re.sub(r"[^" + _U + r".'\- ]", " ", s or ""))
    toks = [t.rstrip(".,") if not re.fullmatch(r"(Jr|Sr|[A-Z])\.", t) else t
            for t in s.split() if t.strip(".") and t.strip(".") not in _STOP_WORDS]
    # trim trailing lowercase words that leaked in ("Berrones who survived")
    while toks and toks[-1][0].islower() and toks[-1].lower() not in ("de", "del", "la", "y"):
        toks.pop()
    return " ".join(toks) if 2 <= len(toks) <= 6 else ""


def _fold(name: str) -> frozenset:
    import unicodedata
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return frozenset(w for w in re.sub(r"[^A-Za-z ]", " ", plain).upper().split() if len(w) > 1)


def _section(text: str, start_pat: str, end_pats: str) -> str:
    m = re.search(start_pat, text, re.I)
    if not m:
        return ""
    rest = text[m.end():]
    e = re.search(end_pats, rest, re.I)
    return rest[: e.start()] if e else rest[:1500]


def _relationship(text: str) -> str:
    m = re.search(r"\bI\s+am\s+(?:the\s+|a\s+)?([a-z][a-z\- ]{2,40}?)\s+(?:of|to|for)\s+(?:the\s+)?(?:Decedent|deceased|"
                  r"[A-Z])", text)
    if m:
        return norm_space(m.group(1)).lower()
    m = NON_FAMILY.search(text[:2500])
    if m:
        return m.group(1).lower()
    return ""


def parse_affidavit(text: str, decedent: str = "") -> dict:
    """OCR text of an affidavit of heirship -> who's who.

    Returns {"affiant": {name, relationship, is_heir}, "heirs": [{name, relation, address}],
             "return_to": {name, address}}
    """
    t = text.replace("—", "-").replace("–", "-")
    flat = norm_space(t)
    out = {"affiant": {}, "heirs": [], "return_to": {}}
    dec_last = (decedent.split() or [""])[0].upper()

    # ---- affiant
    aff = ""
    for pat in (r"My\s+name\s+is\s+" + _NAME, r"personally\s+appeared\s+" + _NAME, _NAME + r"\s*\(\s*[\"“']?Affiant"):
        m = re.search(pat, flat)
        if m:
            aff = _clean_name(m.group(1))
            if aff:
                break
    aff_block = _section(t, r"Affiant\b", r"\n\s*2\.|Decedent\.|\bDecedent\s*\(")
    rel = _relationship(aff_block or flat[:2500])
    is_heir = bool(rel) and bool(HEIR_RELATIONS.search(rel)) and not IN_LAW.search(rel) and not NON_FAMILY.search(rel)
    out["affiant"] = {"name": aff, "relationship": rel, "is_heir": is_heir}

    heirs: dict[str, dict] = {}

    def add(name: str, relation: str, address: str = ""):
        n = _clean_name(name)
        if not n or (aff and n.upper() == aff.upper()):
            return
        if decedent and _fold(n) == _fold(decedent):
            return
        k = n.upper()
        if k not in heirs:
            heirs[k] = {"name": n, "relation": relation, "address": address}
        elif address and not heirs[k]["address"]:
            heirs[k]["address"] = address

    # ---- surviving spouse
    sp = ""
    for start in (r"Surviving\s+Spouse", r"\d\.\s*(?:Marriage|Marital\s+History|Spouse)"):
        sp = _section(t, start, r"\n\s*\d\.\s|Children\.|\bChildren\b")
        if sp:
            break
    for m in re.finditer(r"(?:married\s+to|spouse\s+(?:is|was)|survived\s+by)\s+" + _NAME, sp or flat):
        if re.search(r"(?:who|which)\s+(?:pre)?deceased|predeceased", (sp or flat)[m.end(): m.end() + 60], re.I):
            continue
        a = _ADDR.search((sp or flat)[m.end(): m.end() + 250])
        add(m.group(1), "surviving spouse", norm_space(a.group(1)) if a else "")
        break

    # ---- children (and other heirs listed the same way)
    for sec_pat, rel_name in ((r"\bChildren\b", "child"), (r"Other\s+Children|Other\s+Heirs|Heirs\s+at\s+Law", "heir")):
        sec = _section(t, sec_pat, r"\n\s*\d{1,2}\.\s+[A-Z][a-z]+|Real\s+Property|Legal\s+description|AFFIANT'?S\s+OATH")
        if not sec:
            continue
        lines = [ln.strip() for ln in sec.splitlines()]
        for i, line in enumerate(lines):
            m = re.match(_NAME + r"\s*[-,:]*\s*(?:Date\s+of\s+birth|DOB|born|age\b|\(|Current)", line)
            if not m:
                m = re.match(r"^(?:[a-z]\)|\d\)|[-•*])\s*" + _NAME + r"\s*$", line)
            if m:
                a = _ADDR.search(line) or _ADDR.search(" ".join(lines[i + 1:i + 2]))
                add(m.group(1), rel_name, norm_space(a.group(1)) if a else "")
        # prose: "the following children: A, B and C"
        pm = re.search(r"following\s+(?:children|heirs)\s*[:,]\s*([^.]{5,400})\.", norm_space(sec))
        if pm:
            for piece in re.split(r",|\band\b", pm.group(1)):
                add(piece, rel_name)

    # ---- fallback: family members sharing the decedent's surname
    if not heirs and dec_last:
        for m in re.finditer(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z.]+){0,2}\s+" + re.escape(dec_last.title()) + r")\b", flat):
            add(m.group(1), "family (named in affidavit)")

    out["heirs"] = list(heirs.values())[:12]

    # ---- cover page "Record and Return To"
    m = re.search(r"Record\s+and\s+Return\s+To:?\s*\n+\s*([^\n]{3,60})\n\s*([^\n]{3,60})\n\s*([^\n]{3,60})", t, re.I)
    if m and not re.search(r"Original\s+Returned", m.group(1), re.I):
        addr = norm_space(m.group(2) + ", " + m.group(3))
        if not re.search(r"Original\s+Returned", addr, re.I):
            out["return_to"] = {"name": _clean_name(m.group(1)) or norm_space(m.group(1)), "address": addr}
    return out
