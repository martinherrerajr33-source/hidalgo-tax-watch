"""
Offline sanity tests for hidalgo_tax_checker.py's parsing logic.

These fixtures reproduce the exact HTML structure and label text confirmed
against the live site (via manual browser inspection), so we can validate
the regex/parsing logic without hitting the network every time.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hidalgo_tax_checker import _clean_text, _to_float, HidalgoTaxChecker  # noqa: E402

# Reconstructs the confirmed real structure for account R332097000007401
# (CAD reference 1564064, SMITH ADA M, 1222 N Cesar Chavez Rd Lot 74, Alamo)
# — a "paid up" (non-delinquent) example.
DETAIL_HTML_PAID_UP = """
<html><body>
<h3>Begin a New Search Go to Your Portfolio</h3>
<h3><b>Account Number:</b>  R332097000007401</h3>
<h3><b>Appraisal District Number:</b>  1564064</h3>
<h3><b>Address:</b> <br>
SMITH ADA M<br>1222 N CESAR CHAVEZ RD LOT 74<br>ALAMO, TX  78516 </h3>
<h3><b>Property Site Address:</b><br>
1222 N CESAR CHAVEZ RD<br>78516 </h3>
<h3><b>Legal Description:</b><br>
ROADRUNNER M/H PARK,SPACE 74 14X66<br>SENTRY, LABEL# TRA0040313, SERIAL#<br>SM8781/ NEW ACCT 2024 </h3>
<h3><b>Current Tax Levy:</b>  $459.50</h3>
<h3><b>Current Amount Due:</b>  $0.00</h3>
<h3><b>Prior Year Amount Due:</b> $0.00</h3>
<h3><b>Total Amount Due:</b> $0.00</h3>
<h3><b>Last Payment Amount for Current Year Taxes:</b><br>$510.05</h3>
<h3><b>Last Payment Date for Current Year Taxes:</b><br>04/01/2026</h3>
<h3><b>Active Lawsuits:</b> None</h3>
<h3><b>Gross Value:</b> $23,794</h3>
<h3><b>Land Value:</b> $0</h3>
<h3><b>Improvement Value:</b> $23,794</h3>
<h3><b>Capped Value:</b> $0</h3>
<h3><b>Agricultural Value:</b> $0</h3>
<h3><b>Exemptions:</b><br>None</h3>
</body></html>
"""

# Synthetic delinquent example (same shape, invented numbers) to confirm
# the "delinquent" classification path.
DETAIL_HTML_DELINQUENT = DETAIL_HTML_PAID_UP.replace(
    "<h3><b>Prior Year Amount Due:</b> $0.00</h3>",
    "<h3><b>Prior Year Amount Due:</b> $1,208.44</h3>",
).replace(
    "<h3><b>Total Amount Due:</b> $0.00</h3>",
    "<h3><b>Total Amount Due:</b> $1,208.44</h3>",
)

SEARCH_HTML_ONE_MATCH = """
<html><body>
<p>The following is the result of your CAD Reference search for "1564064"</p>
<p>There are 1 matches.</p>
<table>
<tr><td><a href="showdetail2.jsp?can=R332097000007401">R332097000007401</a></td></tr>
</table>
</body></html>
"""

SEARCH_HTML_NO_MATCH = """
<html><body>
<p>Your search found no records, please try again</p>
</body></html>
"""


def make_checker_with_fake_transport(search_html, detail_html):
    checker = HidalgoTaxChecker(delay_seconds=0)

    class FakeResponse:
        def __init__(self, text):
            self.text = text

        def raise_for_status(self):
            pass

    def fake_post(url, data=None, timeout=None):
        return FakeResponse(search_html)

    def fake_get(url, params=None, timeout=None):
        return FakeResponse(detail_html)

    checker.session.post = fake_post
    checker.session.get = fake_get
    return checker


def test_to_float():
    assert _to_float("$1,234.56") == 1234.56
    assert _to_float("1,234.56") == 1234.56
    assert _to_float("Not Received") == 0.0
    assert _to_float(None) == 0.0
    assert _to_float("") == 0.0


def test_clean_text_handles_br():
    html = "<h3><b>Address:</b><br>Line1<br>Line2</h3>"
    cleaned = _clean_text(html)
    assert "Line1" in cleaned and "Line2" in cleaned
    assert "\n" in cleaned


def test_paid_up_record():
    checker = make_checker_with_fake_transport(SEARCH_HTML_ONE_MATCH, DETAIL_HTML_PAID_UP)
    record = checker.check("1564064", source_owner="SMITH ADA M")
    assert record.found is True
    assert record.account_number == "R332097000007401"
    assert record.status == "paid_up"
    assert record.current_tax_levy == 459.50
    assert record.prior_year_amount_due == 0.0
    assert record.total_amount_due == 0.0
    assert record.gross_value == 23794.0
    assert "SMITH ADA M" in record.owner_name
    assert "1222 N CESAR CHAVEZ" in record.property_site_address
    print("PASS: paid_up record parses correctly ->", record.status, record.owner_name)


def test_delinquent_record():
    checker = make_checker_with_fake_transport(SEARCH_HTML_ONE_MATCH, DETAIL_HTML_DELINQUENT)
    record = checker.check("1564064")
    assert record.status == "delinquent"
    assert record.prior_year_amount_due == 1208.44
    assert record.total_amount_due == 1208.44
    print("PASS: delinquent record classifies correctly -> $%.2f prior year due" % record.prior_year_amount_due)


def test_not_found_record():
    checker = make_checker_with_fake_transport(SEARCH_HTML_NO_MATCH, "")
    record = checker.check("00000000")
    assert record.found is False
    assert record.status == "not_found"
    print("PASS: not_found record classifies correctly")


def test_empty_input():
    checker = HidalgoTaxChecker()
    record = checker.check("")
    assert record.status == "error"
    print("PASS: empty input handled as error, not a crash")


def test_geo_id_routes_to_account_no_field():
    """A GEO ID (has letters/dashes) should be searched via searchby=4 (Account No.), not 5."""
    checker = HidalgoTaxChecker(delay_seconds=0)
    calls = []

    def fake_post(url, data=None, timeout=None):
        calls.append(data["searchby"])
        return type("R", (), {"text": SEARCH_HTML_ONE_MATCH.replace("1564064", "601618")
                               .replace("R332097000007401", "C162200000001500"),
                               "raise_for_status": lambda self=None: None})()

    def fake_get(url, params=None, timeout=None):
        return type("R", (), {"text": DETAIL_HTML_PAID_UP, "raise_for_status": lambda self=None: None})()

    checker.session.post = fake_post
    checker.session.get = fake_get
    checker.check("C1622-00-000-0015-00")
    assert calls[0] == "4", f"expected first attempt on searchby=4 (Account No.), got {calls[0]}"
    print("PASS: GEO ID with dashes routes to Account No. field (searchby=4)")


def test_numeric_propid_routes_to_cad_ref_field():
    """A pure-digit PropID should be searched via searchby=5 (CAD Reference No.)."""
    checker = HidalgoTaxChecker(delay_seconds=0)
    calls = []

    def fake_post(url, data=None, timeout=None):
        calls.append(data["searchby"])
        return type("R", (), {"text": SEARCH_HTML_ONE_MATCH, "raise_for_status": lambda self=None: None})()

    def fake_get(url, params=None, timeout=None):
        return type("R", (), {"text": DETAIL_HTML_PAID_UP, "raise_for_status": lambda self=None: None})()

    checker.session.post = fake_post
    checker.session.get = fake_get
    checker.check("109048")
    assert calls[0] == "5", f"expected first attempt on searchby=5 (CAD Reference No.), got {calls[0]}"
    print("PASS: numeric PropID routes to CAD Reference No. field (searchby=5)")


def test_falls_back_to_other_field_when_first_guess_fails():
    """If the primary field guess comes back empty, it should try the other field before giving up."""
    checker = HidalgoTaxChecker(delay_seconds=0)
    calls = []

    def fake_post(url, data=None, timeout=None):
        calls.append(data["searchby"])
        # first attempt (searchby=4, our GEO ID guess) fails; second attempt (searchby=5) succeeds
        if len(calls) == 1:
            return type("R", (), {"text": SEARCH_HTML_NO_MATCH, "raise_for_status": lambda self=None: None})()
        return type("R", (), {"text": SEARCH_HTML_ONE_MATCH, "raise_for_status": lambda self=None: None})()

    def fake_get(url, params=None, timeout=None):
        return type("R", (), {"text": DETAIL_HTML_PAID_UP, "raise_for_status": lambda self=None: None})()

    checker.session.post = fake_post
    checker.session.get = fake_get
    # No dash here, so this exercises the cross-field fallback directly
    # rather than the dash-stripping fallback.
    record = checker.check("Z1234ABC")
    assert calls == ["4", "5"], f"expected fallback sequence ['4','5'], got {calls}"
    assert record.found is True
    print("PASS: falls back to the other search field when the first guess comes back empty")


if __name__ == "__main__":
    test_to_float()
    test_clean_text_handles_br()
    test_paid_up_record()
    test_delinquent_record()
    test_not_found_record()
    test_empty_input()
    test_geo_id_routes_to_account_no_field()
    test_numeric_propid_routes_to_cad_ref_field()
    test_falls_back_to_other_field_when_first_guess_fails()
    print("\nAll offline parser tests passed.")
