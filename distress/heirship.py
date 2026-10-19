"""
heirship.py - read an Affidavit of Heirship and pull out who actually inherits.

The clerk's index only names the AFFIANT (grantor) and the DECEDENT (grantee).
The affiant is often NOT an heir - Texas wants a disinterested witness, so it's
frequently a neighbor, family friend, pastor or in-law. The heirs (surviving
spouse, children, parents, siblings) are only named inside the document, so we
OCR the page images and read them out.

Layouts seen in Hidalgo filings (Oct 2026 samples):
  * numbered State Bar form:  "4. Surviving Spouse. Decedent was married to X, who survived him"
                              "5. Children ... X -- Date of birth: .. -- Current address: .."
  * "Name:/Address:" blocks:  "a. Name: Ruben De Leon / Address: 1900 W Mimosa Dr., Weslaco, TX"
                              "12. The following were the heirs of the Decedent: a. Name: .."
  * notary-shop prose:        "Decedent's Father was Juan Casares Jr", "Grantee: Juan Casares JR."
                              "Grantee Mailing Address: 315 N. Gonzalez Progreso, TX 78579"
  * affiant blocks:           "Affiant: Sebastian Barron / Affiant's Address: .. /
                               Affiant's Relationship to Decedent: Family friend for 35 years"
                              "We, Sebastian Barron and Roberto Garcia, are personally familiar.."
  * cover page:               "Record and Return To:" (two-column OCR, merged with clerk info)

Every rule is a heuristic; a name we can't place is left out rather than guessed.
"""

from __future__ import annotations

import re
import unicodedata

from .names import norm_space

HEIR_RELATIONS = re.compile(
    r"\b(son|daughter|child|wife|husband|spouse|widow|widower|grand(?:son|daughter|child)|"
    r"mother|father|parent|brother|sister|sibling|nephew|niece|heir)\b", re.I)
IN_LAW = re.compile(r"-in-law|\bin law\b|\bstep", re.I)
NON_FAMILY = re.compile(r"\b(neighbou?r|friend|acquaint\w*|pastor|minister|co-?worker|employer|caregiver|"
                        r"not related|no relation|unrelated|disinterested|known (?:them|the family|decedent))", re.I)
ENTITY = re.compile(r"\b(LLC|L\.L\.C|INC|CORP|CORPORATION|COMPANY|TITLE|ABSTRACT|LAW|LAWYER|ATTORNEY|PLLC|P\.C|"
                    r"NOTARY|SIMPLIFILE|ERECORDING|E-RECORDING|CSC|SERVICE|SERVICES|PARTNERS|GROUP|BANK|"
                    r"MORTGAGE|LENDING|FIRM|OFFICE|ASSOCIATES|FNA|TRUST|ESCROW|DOCUMENT|RECEIPT)\b", re.I)

_U = "A-Za-zÀ-ÖØ-öø-ÿ"
_NAME = (r"([A-ZÀ-Þ][" + _U + r".'\-]+(?:\s+(?:[A-ZÀ-Þ][" + _U + r".'\-]+|de|De|del|Del|la|La|los|Los|y|Y)){1,5})")
_STOP_WORDS = {"Date", "Birth", "Current", "Address", "Decedent", "Decedents", "Deceased", "Texas", "County",
               "State", "The", "This", "Affiant", "Affiants", "Children", "Child", "None", "Residence", "Hidalgo",
               "Name", "Names", "My", "I", "Am", "Married", "Spouse", "Surviving", "Of", "And", "Who", "Her",
               "His", "Their", "Was", "Is", "Mailing", "Grantee", "Father", "Mother", "Other", "Parent",
               "Aka", "Estate", "Mr", "Mrs", "Ms"}
_ADDR = re.compile(r"(\d{1,6}\s+[A-Za-z0-9 .'#,-]{3,45}?,?\s+[A-Za-z .'-]{3,25},?\s+(?:TX|Texas|Tx|Fexas)\.?,?\s*\d{5})")
_SUFFIX = re.compile(r"^(Jr|Sr|II|III|IV)\.?$", re.I)
_PARTICLES = ("de", "del", "la", "los", "y")


def _fold(name: str) -> frozenset:
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return frozenset(w for w in re.sub(r"[^A-Za-z ]", " ", plain).upper().split() if len(w) > 1)


def _clean_name(s: str) -> str:
    s = re.split(r"\b(?:aka|a/k/a|f/k/a)\b", s or "", flags=re.I)[0]
    s = norm_space(re.sub(r"[^" + _U + r".'\- ]", " ", s))
    toks = []
    for t in s.split():
        if toks and (_SUFFIX.match(toks[-1].strip(".")) or (toks[-1].endswith(".") and len(toks[-1]) > 2)):
            break   # "Antonio Perez. We declare" / "Juan Casares Jr. Decedent's" - the name ended
        bare = t.strip(".,")
        if not bare or bare in _STOP_WORDS or bare.capitalize() in _STOP_WORDS:
            continue
        if _SUFFIX.match(bare):
            toks.append("Jr." if bare.lower() == "jr" else bare.upper())
        else:
            toks.append(t.rstrip(",") if t.endswith(".") and len(bare) > 1 and bare[0].isupper() and
                        not (len(bare) == 1) else bare)
    while toks and toks[-1][0].islower() and toks[-1].lower() not in _PARTICLES:
        toks.pop()
    while toks and toks[-1].lower() in _PARTICLES:
        toks.pop()   # a line break cut "De La Rosa" short - don't keep a dangling particle
    if toks and toks[0].lower() in _PARTICLES:
        return ""
    if toks and toks[-1].endswith(".") and toks[-1] != "Jr.":
        toks[-1] = toks[-1].rstrip(".")
    name = " ".join(toks)
    if not (2 <= len(toks) <= 6) or ENTITY.search(name):
        return ""
    if name.replace("Jr.", "").isupper():
        name = " ".join(w if _SUFFIX.match(w.strip(".")) else
                        (w.lower() if w.lower() in _PARTICLES else w.capitalize()) for w in name.split())
    return name


def _address_after(lines: list[str], i: int, span: int = 3) -> str:
    """An address on line i (after a label) or wrapped onto the next line(s)."""
    chunk = " ".join(re.sub(r"^(?:Affiant'?s\s+)?(?:Current\s+)?(?:Mailing\s+)?Address:?\s*", "", ln, flags=re.I)
                     for ln in lines[i:i + span])
    m = _ADDR.search(chunk)
    return norm_space(m.group(1)).replace("Fexas", "Texas") if m else ""


def parse_affidavit(text: str, decedent: str = "") -> dict:
    """OCR text -> {"affiants": [{name, relationship, is_heir, address}], "affiant": first affiant,
                    "heirs": [{name, relation, address}], "return_to": {name, address}}"""
    t = (text.replace("—", "-").replace("–", "-").replace("’", "'")
             .replace("“", '"').replace("”", '"').replace("{", "("))
    lines = [norm_space(ln) for ln in t.splitlines()]
    lines = [ln for ln in lines if ln]
    flat = " ".join(lines)
    dec = _fold(decedent)

    # ------------------------------------------------------------ affiants
    affiants: dict[frozenset, dict] = {}

    def add_affiant(name: str, rel: str = "", address: str = ""):
        n = _clean_name(name)
        if not n or _fold(n) == dec:
            return
        a = affiants.setdefault(_fold(n), {"name": n, "relationship": "", "address": ""})
        a["relationship"] = a["relationship"] or rel
        a["address"] = a["address"] or address

    for pat in (r"My\s+[Nn]ame\s+is\s+" + _NAME,
                r"personally\s+appeared\s+" + _NAME + r"(?:\s*,?\s*and\s+" + _NAME + r")?",
                r"(?:Our\s+names\s+are|We,)\s+" + _NAME + r",?\s+and\s+" + _NAME):
        for m in re.finditer(pat, flat):
            for g in m.groups():
                if g:
                    add_affiant(g)
    for i, ln in enumerate(lines):
        m = re.match(r"Affiant\s*:\s*" + _NAME, ln)
        if m:
            rel = ""
            for nxt in lines[i + 1:i + 4]:
                rm = re.match(r"Affiant'?s\s+Relationship(?:\s+to\s+(?:the\s+)?Decedent)?\s*:\s*(.+)", nxt, re.I)
                if rm:
                    rel = rm.group(1)
            add_affiant(m.group(1), rel, _address_after(lines, i + 1))
        m = re.match(_NAME + r"\s*,\s*Affiant\b", ln)
        if m:
            add_affiant(m.group(1), "", _address_after(lines, i + 1))

    # relationship stated in prose ("I am the daughter-in-law of ...", "Family friend", "known them 20 years")
    rel_text = ""
    m = re.search(r"\b(?:I|We)\s+(?:am|are)\s+(?:the\s+|a\s+)?([a-z][a-z\- ]{2,40}?)\s+(?:of|to|for)\s+(?:the\s+)?"
                  r"(?:Decedent|deceased|[A-Z])", flat)
    if m and not re.search(r"familiar|personally|knowledge|aware", m.group(1)):
        rel_text = norm_space(m.group(1)).lower()
    if not rel_text:
        m = NON_FAMILY.search(flat[:5000])
        rel_text = m.group(1).lower() if m else ""
    for a in affiants.values():
        a["relationship"] = norm_space(a["relationship"] or rel_text).lower()
        if re.fullmatch(r"known (?:them|the family|decedent)|disinterested", a["relationship"]):
            a["relationship"] = "knows the family - not a relative"
        r = a["relationship"]
        a["is_heir"] = bool(r) and bool(HEIR_RELATIONS.search(r)) and not IN_LAW.search(r) and not NON_FAMILY.search(r)

    # people named only as "having knowledge of the family" are witnesses, not heirs
    witnesses = set(affiants)
    m = re.search(r"following\s+persons?\s+ha(?:s|ve)\s+(?:knowledge|acknowledged)[^:]*:?\s*(.{0,200})", flat, re.I)
    if m:
        for g in re.findall(_NAME, m.group(1)):
            witnesses.add(_fold(g))
    for m in re.finditer(r"(?:acknowledged|sworn\s+to)\s+before\s+me[^.]{0,60}?\bby\s+" + _NAME, flat):
        witnesses.add(_fold(m.group(1)))

    # ------------------------------------------------------------ heirs
    heirs: dict[frozenset, dict] = {}

    def add_heir(name: str, relation: str, address: str = ""):
        n = _clean_name(name)
        if not n:
            return
        f = _fold(n)
        if not f or f == dec or f in witnesses:
            return
        if dec and f < dec:
            return   # a shortened form of the decedent's own name
        cur = heirs.get(f)
        if cur is None:
            heirs[f] = {"name": n, "relation": relation, "address": address}
        else:
            cur["address"] = cur["address"] or address
            if cur["relation"] in ("heir", "family (named in affidavit)") and relation not in ("heir",):
                cur["relation"] = relation

    def context(i: int) -> str:
        """Heading above a 'Name:' entry tells us who it is."""
        for back in range(i - 1, max(-1, i - 30), -1):
            h = lines[back]
            if re.search(r"Name\s+of\s+Other\s+Parent", h, re.I):
                continue
            if re.search(r"heirs?\s+of\s+(?:the\s+)?Decedent|were\s+the\s+heirs|heirs\s+at\s+law", h, re.I):
                return "heir"
            if re.search(r"\bchild(?:ren)?\b", h, re.I):
                return "child"
            if re.search(r"sibling|brother|sister", h, re.I):
                return "sibling"
            if re.search(r"\bparents?\b|\bfather\b|\bmother\b", h, re.I):
                return "parent"
            if re.search(r"Affiant|knowledge\s+regarding", h, re.I):
                return "skip"
        return "heir"

    for i, ln in enumerate(lines):
        m = re.match(r"(?:[a-z0-9ivx]{1,4}[.)]\s*)?Name\s*:\s*(.+)$", ln, re.I)
        if m and not re.match(r"Name\s+of\s+Other", ln, re.I):
            ctx = context(i)
            if ctx == "skip":
                continue
            addr = ""
            for j in range(i + 1, min(len(lines), i + 6)):
                if re.match(r"(?:Current\s+)?(?:Mailing\s+)?Address", lines[j], re.I):
                    addr = _address_after(lines, j)
                    break
                if re.match(r"(?:[a-z0-9ivx]{1,4}[.)]\s*)?Name\s*:", lines[j], re.I):
                    break
            add_heir(m.group(1), ctx, addr)

    # State Bar form: "Nato Berrones -- Date of birth: ... -- Current address:" (address may wrap)
    for i, ln in enumerate(lines):
        m = re.match(_NAME + r"\s*[-,:]*\s*(?:Date\s+of\s+birth|DOB\b|born\b)", ln)
        if m:
            add_heir(m.group(1), "child", _address_after(lines, i, 2))

    # surviving spouse - only if the marriage didn't end in death or divorce
    for m in re.finditer(r"(?:married\s+to|marriage\s+was\s+to|survived\s+by\s+(?:his|her)\s+(?:wife|husband|spouse),?)\s+"
                         + _NAME, flat):
        after = flat[m.end(): m.end() + 220]
        if re.search(r"ended|death\s+of|deceased|predeceased|divorce|\bdied\b", after[:160], re.I):
            continue
        a = _ADDR.search(after)
        add_heir(m.group(1), "surviving spouse", norm_space(a.group(1)) if a else "")

    # parents / named grantee (notary-shop style)
    for m in re.finditer(r"Decedent'?s\s+(Father|Mother)\s+(?:was|is)\s*:?\s*" + _NAME, flat, re.I):
        after = flat[m.end(): m.end() + 60]
        if not re.search(r"deceased|predeceased|\bdied\b", after, re.I):
            add_heir(m.group(2), m.group(1).lower())
    m = re.search(r"\bGrantee\s*:\s*" + _NAME, flat)
    if m:
        a = re.search(r"Grantee\s+Mailing\s+Address\s*:\s*(.{5,80}?\d{5})", flat)
        add_heir(m.group(1), "named grantee", norm_space(a.group(1)) if a else "")

    # prose list: "the following children: A, B and C."
    for pm in re.finditer(r"following\s+(children|heirs)\s*[:,]\s*([^.]{5,400})\.", flat):
        for piece in re.split(r",|\band\b", pm.group(2)):
            add_heir(piece, "child" if pm.group(1) == "children" else "heir")

    # last resort: relatives sharing the decedent's surname
    if not heirs and decedent:
        surname = re.escape(decedent.split()[0].title())
        for m in re.finditer(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z.]+){0,2}\s+" + surname + r"(?:\s+[A-Z][a-z]+)?)\b", flat):
            add_heir(m.group(1), "family (named in affidavit)")

    # ------------------------------------------------------------ return-to
    return_to = {}
    left = re.compile(r"^(?:File Information:?|Document No:?\s*\d+|Receipt No:?\s*\d+|Recorded On:.*?(?:AM|PM)|"
                      r"Deputy Clerk:\s*[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?|Station:\s*\S+)\s*", re.I)
    for i, ln in enumerate(lines):
        if re.search(r"(?:Record(?:ed)?\s+and\s+Return\s+To|Returned\s+to|RETURN\s+TO)\s*:?\s*$", ln, re.I):
            vals = []
            for nxt in lines[i + 1:i + 7]:
                v = left.sub("", nxt).strip()
                v = re.sub(r"\s*original\s+returned\s+to\s+customer\s*", " ", v, flags=re.I).strip()
                if not v or re.search(r"THIS PAGE|STATE OF TEXAS|hereby certify|AFFIDAVIT|Date:|Name:", v, re.I):
                    continue
                vals.append(v)
            name = _clean_name(vals[0]) if vals else ""
            if name:
                return_to = {"name": name, "address": norm_space(", ".join(vals[1:3]))}
                break
    first = next(iter(affiants.values()), {"name": "", "relationship": rel_text, "is_heir": False, "address": ""})
    return {"affiants": list(affiants.values()), "affiant": first,
            "heirs": list(heirs.values())[:12], "return_to": return_to}
