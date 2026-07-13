import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import hidalgo_tax_checker as htc  # noqa: E402
from test_parser import (  # noqa: E402
    SEARCH_HTML_ONE_MATCH, DETAIL_HTML_PAID_UP, DETAIL_HTML_DELINQUENT, SEARCH_HTML_NO_MATCH,
)

RESPONSES = {
    "109048": (
        SEARCH_HTML_ONE_MATCH.replace("1564064", "109048").replace("R332097000007401", "A100002000000100"),
        DETAIL_HTML_DELINQUENT.replace("R332097000007401", "A100002000000100"),
    ),
    "1564064": (SEARCH_HTML_ONE_MATCH, DETAIL_HTML_PAID_UP),
}


class FakeResponse:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


def fake_post_with_retry(self, url, data):
    cad_ref = data.get("criteria")
    search_html, _ = RESPONSES.get(cad_ref, (SEARCH_HTML_NO_MATCH, ""))
    return FakeResponse(search_html)


def fake_get_with_retry(self, url, params):
    can = params.get("can")
    for search_html, detail_html in RESPONSES.values():
        if can in search_html:
            return FakeResponse(detail_html)
    return FakeResponse("")


htc.HidalgoTaxChecker._post_with_retry = fake_post_with_retry
htc.HidalgoTaxChecker._get_with_retry = fake_get_with_retry
htc.HidalgoTaxChecker._polite_wait = lambda self: None  # skip sleeps in test

import run_batch  # noqa: E402

sys.argv = [
    "run_batch.py",
    "--input", str(Path(__file__).resolve().parent.parent / "input" / "test_run.csv"),
    "--dashboard-dir", str(Path(__file__).resolve().parent.parent / "dashboard"),
    "--output-dir", str(Path(__file__).resolve().parent.parent / "output"),
]
run_batch.main()

# ---- verify output ----
import json  # noqa: E402
data = json.loads((Path(__file__).resolve().parent.parent / "dashboard" / "data.json").read_text())
print("\n--- data.json summary ---")
print(json.dumps(data["summary"], indent=2))
assert data["summary"]["total_checked"] == 3
assert data["summary"]["delinquent_count"] == 1
assert data["summary"]["not_found_count"] == 1
statuses = sorted(p["status"] for p in data["properties"])
print("statuses:", statuses)
assert statuses == ["delinquent", "not_found", "paid_up"]
print("\nEnd-to-end pipeline test PASSED.")
