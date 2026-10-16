"""
score.py - the 0-100 distress score.

Each parcel collects signals (clerk filings, court cases, tax status). The
score is built from the strongest signal in each *group* - so a probate case
and an heirship affidavit on the same dead owner don't double count - plus a
bonus for every additional group stacked on the same parcel, because that's
where the real motivation is (a dead owner AND back taxes AND a weedy-lot
lien beats any one of those alone).

    score = sum(best weight per group, aged)            group weights below
          + 5 per extra distinct group (max +15)        stacking
          + 10 if a court case names the owner deceased
          + 5  if the parcel has no homestead exemption (absentee / not owner-occupied)
          capped at 100

Older signals count for less: 60% after 1 year, 40% after 3 years. An open
tax suit or a current delinquency never ages - it's still true today.
"""

from __future__ import annotations

from datetime import date, datetime

# category -> (group, weight, label)
CATEGORIES: dict[str, tuple[str, int, str]] = {
    "tax_suit":           ("tax",         30, "Open tax suit"),
    "tax_delinquent":     ("tax",         20, "Delinquent taxes"),
    "pre_foreclosure":    ("foreclosure", 30, "Pre-foreclosure"),
    "probate":            ("death",       25, "Probate case"),
    "heirship":           ("death",       25, "Affidavit of heirship"),
    "will_probate":       ("death",       20, "Will / probate filing"),
    "lis_pendens":        ("lawsuit",     20, "Lis pendens"),
    "bankruptcy":         ("bankruptcy",  15, "Bankruptcy"),
    "code_lien":          ("code",        15, "City / code lien"),
    "tax_lien":           ("liens",       12, "IRS / state tax lien"),
    "tax_loan":           ("tax",         15, "Property-tax loan (tax lien transfer)"),
    "judgment":           ("liens",       10, "Judgment"),
    "child_support_lien": ("liens",       10, "Child support lien"),
    "mechanics_lien":     ("liens",        8, "Mechanic's lien"),
    "other_lien":         ("liens",        6, "Other lien"),
    "divorce":            ("divorce",     10, "Divorce"),
}

NEVER_AGES = {"tax_suit", "tax_delinquent"}


def _parse_date(s: str) -> date | None:
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime((s or "")[:10], fmt).date()
        except ValueError:
            continue
    return None


def age_factor(category: str, when: str, today: date) -> float:
    if category in NEVER_AGES:
        return 1.0
    d = _parse_date(when)
    if not d:
        return 1.0
    days = (today - d).days
    if days > 3 * 365:
        return 0.4
    if days > 365:
        return 0.6
    return 1.0


def tax_weight(prior_due: float) -> int:
    base = CATEGORIES["tax_delinquent"][1]
    if prior_due >= 15000:
        return base + 10
    if prior_due >= 5000:
        return base + 5
    return base


def score_parcel(signals: list[dict], tax: dict | None, today: date) -> tuple[int, list[dict]]:
    """Returns (score 0-100, breakdown rows for the dashboard)."""
    best: dict[str, tuple[float, str]] = {}   # group -> (points, label)
    deceased = False
    for sig in signals:
        cat = sig["category"]
        if cat not in CATEGORIES:
            continue
        group, weight, label = CATEGORIES[cat]
        if cat == "tax_delinquent":
            weight = tax_weight(float(sig.get("amount") or 0))
        pts = weight * age_factor(cat, sig.get("date", ""), today)
        if pts > best.get(group, (0, ""))[0]:
            best[group] = (pts, label)
        if sig.get("deceased"):
            deceased = True

    breakdown = [{"label": label, "points": round(pts)} for _, (pts, label) in
                 sorted(best.items(), key=lambda kv: -kv[1][0])]
    total = sum(p for p, _ in best.values())

    extra = max(0, len(best) - 1)
    if extra:
        bonus = min(15, 5 * extra)
        total += bonus
        breakdown.append({"label": f"Stacked: {len(best)} signal types", "points": bonus})
    if deceased:
        total += 10
        breakdown.append({"label": "Owner named deceased", "points": 10})
    if tax and tax.get("found") and not tax.get("homestead"):
        total += 5
        breakdown.append({"label": "No homestead (absentee)", "points": 5})

    return min(100, round(total)), breakdown
