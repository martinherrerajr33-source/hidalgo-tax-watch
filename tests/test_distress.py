"""Offline tests for the distress-watch parsers, using rows captured from the live portals (Oct 2026)."""

from datetime import date

from distress.clerk import categorize, results_url
from distress.court import parse_results, build_case, record_count
from distress.names import (parse_clerk_legal, parse_cad_legal, person_from_last_first,
                            person_from_first_last, split_case_parties, subdivision_similarity)
from distress.cad import _legal_fit, _name_fit, Parcel
from distress.score import score_parcel

ODYSSEY_ROW = (
    '<table><tr><td nowrap="true" valign="top"><a href="CaseDetail.aspx?CaseID=5071026" style="color: blue">'
    'T-1086-26-B</a></td><td valign="top">Hidalgo County, Hidalgo County Emergency Services District # 05, '
    'La Joya Independent School District, South Texas Independent School District, Hidalgo County Drainage '
    'District # 01, South Texas College, Hidalgo County Emergency Services District # 06 VS. Alberto Rendon, '
    'Alda Rendon, James S. Bates</td><td valign="top" nowrap="true"><div>09/01/2026</div><div>93rd District '
    'Court</div></td><td valign="top" nowrap="true"><div>Tax Cases (OCA)</div><div>Filed</div></td></tr></table>'
    '<span>Record Count:</span>&nbsp;<b>117</b>'
)


def test_odyssey_row_parse():
    rows = parse_results(ODYSSEY_ROW)
    assert len(rows) == 1
    r = rows[0]
    assert r["case_id"] == "5071026"
    assert r["case_number"] == "T-1086-26-B"
    assert r["filed"] == "09/01/2026"
    assert r["court"] == "93rd District Court"
    assert r["case_type"] == "Tax Cases (OCA)"
    assert r["status"] == "Filed"
    assert record_count(ODYSSEY_ROW) == 117


def test_tax_suit_defendants():
    case = build_case(parse_results(ODYSSEY_ROW)[0], "tax_suit")
    names = [(p["last"], p["first"]) for p in case.parties]
    assert ("RENDON", "ALBERTO") in names
    assert ("RENDON", "ALDA") in names
    assert ("BATES", "JAMES") in names
    assert "Hidalgo County" in case.plaintiffs


def test_tax_suit_deceased_and_aka():
    row = {"case_id": "1", "case_number": "T-1", "filed": "09/08/2026", "court": "x", "case_type": "Tax Cases (OCA)",
           "status": "Filed",
           "style": "Hidalgo County, City of Elsa VS. Santiago Perez (Deceased), Oralia Perez (Deceased), "
                    "Thelma Edith Martinez, a/k/a Thelma E. Martinez, Benigno Lozano, Jr."}
    case = build_case(row, "tax_suit")
    raws = [p["raw"] for p in case.parties]
    assert len(raws) == 4                       # alias and "Jr." are not separate people
    assert case.any_deceased
    perez = next(p for p in case.parties if p["first"] == "SANTIAGO")
    assert perez["last"] == "PEREZ" and perez["deceased"]


def test_probate_and_divorce_styles():
    p = build_case({"case_id": "2", "case_number": "P1-44264", "filed": "10/14/2026", "court": "Probate Court #1",
                    "case_type": "Determination of Heirship (OCA)", "status": "Filed",
                    "style": "Estate of Gumercindo Delgado Deceased"}, "probate")
    assert p.parties[0]["last"] == "DELGADO" and p.parties[0]["first"] == "GUMERCINDO" and p.parties[0]["deceased"]
    d = build_case({"case_id": "3", "case_number": "F-3981-26-I", "filed": "10/15/2026", "court": "398th",
                    "case_type": "Divorce - No Children (OCA)", "status": "Filed",
                    "style": "IN THE MATTER OF THE MARRIAGE OF SEAN BENNETT BEALL VS. ANNALIZZA TIJERINA"}, "divorce")
    assert {(x["last"], x["first"]) for x in d.parties} == {("BEALL", "SEAN"), ("TIJERINA", "ANNALIZZA")}


def test_compound_surname_alternatives():
    p = person_from_first_last("Maria Del Rosario Gonzalez Gray")
    assert p.last == "GRAY" and "GONZALEZ" in p.alt_lasts
    assert "GONZALEZ MARIA" in p.queries()


def test_clerk_categories():
    assert categorize("AFFIDVT HEIRSHIP") == "heirship"
    assert categorize("MOWING LIEN") == "code_lien"
    assert categorize("LIEN", grantor="CITY OF MCALLEN") == "code_lien"
    assert categorize("LIEN", grantor="ACME ROOFING") == "other_lien"
    assert categorize("REL MOWING LIEN") is None
    assert categorize("RELEASE OF LIEN") is None
    assert categorize("DEED W/VENDORS LIEN") is None
    assert categorize("LIS PENDENS") == "lis_pendens"
    assert categorize("REL LIS PENDENS") is None
    assert categorize("FEDERAL TAX LIEN") == "tax_lien"
    assert categorize("ABSTRACT OF JUDGEMEN") == "judgment"
    assert categorize("APPOINTMENT FOR SUB TRUSTEE") == "pre_foreclosure"
    assert categorize("NOTICE OF FORECLOSURE") == "pre_foreclosure"
    assert categorize("FORECLOSURE SALE DEE") is None
    assert categorize("SUB TRUSTEE DEED") is None
    assert categorize("WILL") == "will_probate"
    assert categorize("LETTERS OF TESTAMENT") == "will_probate"
    assert categorize("WARRANTY DEED") is None
    assert categorize("TRANSFER TX LIEN") == "tax_loan"
    assert categorize("TRANSFER OF LIEN") is None
    assert categorize("APPOINT SUB TTEE") == "pre_foreclosure"
    assert categorize("REL OF JUDGEMENT") is None


def test_legal_descriptions_match():
    clerk = parse_clerk_legal("Subdivision-  Name: EXPRESSWAY HEIGHTS 3 Lot: 7 Block: 11")
    cad = parse_cad_legal("EXPRESSWAY HEIGHTS UT NO. 3 BLK 11 LOT 7")
    assert (clerk.lot, clerk.block) == ("7", "11")
    assert (cad.lot, cad.block) == ("7", "11")
    assert subdivision_similarity(clerk.subdivision, cad.subdivision) >= 0.5
    parcel = Parcel("E8500-03-011-0007-00", "172984", "BERRONES JOSE G & SAN JUANA", "2009 DALLAS DR", "",
                    "EXPRESSWAY HEIGHTS UT NO. 3 BLK 11 LOT 7", 102139, "R")
    ok, sim = _legal_fit(parcel, clerk)
    assert ok and sim >= 0.5
    person = person_from_last_first("BERRONES JOSE GUADALUPE DECEASED")
    assert person.deceased and person.last_first == "BERRONES JOSE"
    assert _name_fit(parcel, person)          # middle initial G agrees
    other = Parcel("M4659-00-000-0024-00", "534741", "BERRONES JOSE ANGEL", "5000 QUALE DR", "",
                   "MINNESOTA HEIGHTS LOT 24", 205452, "R")
    assert not _name_fit(other, person)       # ANGEL != GUADALUPE


def test_entities_are_not_people():
    assert person_from_last_first("CITY OF MCALLEN") is None
    assert person_from_last_first("WELLS FARGO BANK N A") is None
    assert split_case_parties("A B, a/k/a C D, E F, Jr.") == ["A B", "E F"]


def test_score_stacks_and_caps():
    today = date(2026, 10, 15)
    sigs = [
        {"category": "tax_suit", "date": "09/01/2026"},
        {"category": "heirship", "date": "10/13/2026", "deceased": True},
        {"category": "probate", "date": "10/14/2026"},
        {"category": "code_lien", "date": "01/01/2020"},
    ]
    s, breakdown = score_parcel(sigs, {"found": True, "homestead": False}, today)
    # tax 30 + death 25 + code 15*0.4 (6y old) + stack 2 extra groups 10 + deceased 10 + absentee 5 = 86
    assert s == 86
    one, _ = score_parcel([{"category": "divorce", "date": "10/01/2026"}], None, today)
    assert one == 10
    many = [{"category": c, "date": "10/01/2026"} for c in
            ("tax_suit", "pre_foreclosure", "probate", "lis_pendens", "code_lien", "bankruptcy")]
    assert score_parcel(many, None, today)[0] == 100


def test_results_url():
    u = results_url("heirship", "RP", date(2026, 10, 12), date(2026, 10, 13))
    assert "recordedDateRange=20261012%2C20261013" in u and "searchValue=heirship" in u


def test_rows_from_ws_payload():
    from distress.clerk import rows_from_payload
    data = {"byOrder": [331587830], "byHash": {"331587830": {
        "docId": 331587830, "docNumber": "2026-3796238", "docType": "AFFIDVT <em>HEIRSHIP</em>",
        "docTypeCode": "AFFIDVT HEIRSHI", "recordedDate": "10/13/2026",
        "grantor": ["MIRELES EVA MARIA", "BERRONES JOSE GUADALUPE DECEASED", ""],
        "grantee": ["BERRONES JOSE GUADALUPE DECEASED", ""],
        "legalDescription": ["Subdivision-  Name: EXPRESSWAY HEIGHTS 3 Lot: 7 Block: 11", ""],
        "parties": [{"name": "MIRELES EVA MARIA", "type": "grantor"},
                    {"name": "BERRONES JOSE GUADALUPE DECEASED", "type": "grantor"},
                    {"name": "BERRONES JOSE GUADALUPE DECEASED", "type": "grantee"},
                    {"type": "grantor", "overflowMsg": ""}]}}}
    (r,) = rows_from_payload(data)
    assert r["doc_type"] == "AFFIDVT HEIRSHIP" and categorize(r["doc_type"]) == "heirship"
    assert r["grantee"] == "BERRONES JOSE GUADALUPE DECEASED"
    assert r["grantor"] == "MIRELES EVA MARIA; BERRONES JOSE GUADALUPE DECEASED"
    assert r["legal"].endswith("Lot: 7 Block: 11") and r["recorded"] == "10/13/2026"


PROBATE_DETAIL = '''<div>Party Information</div><table>
<tr><th id="PIr01">Decedent</th><th id="PIr11">Delgado, Gumercindo</th><th>Attorneys</th></tr>
<tr><td headers="PIr01 PIr11">&nbsp;</td></tr>
<tr><th id="PIr02">Applicant</th><th id="PIr12">Delgado, Maria Elena</th><th>Lead Attorneys</th></tr>
<tr><td headers="PIr02 PIr12">1205 W Elm Ave<br>McAllen, TX 78501</td><td>JOHN Q SMITH<br>Retained<br>956-555-1234(W)</td></tr>
</table><div>Events & Orders of the Court</div>'''


def test_case_parties_and_contacts():
    from distress.court import parse_case_parties
    import run_distress as rd
    parties = parse_case_parties(PROBATE_DETAIL)
    applicant = next(p for p in parties if p["role"] == "Applicant")
    assert applicant["address"] == "1205 W Elm Ave, McAllen, TX 78501"
    assert applicant["attorney_phone"] == "(956) 555-1234"
    srcs = [
        ("court", {"kind": "probate", "case_number": "P1-1", "parties": [], "detail_parties": parties}),
        ("clerk", {"category": "heirship", "doc_number": "2026-1",
                   "grantor": "DELGADO ROSA; DELGADO GUMERCINDO DECEASED", "grantee": "DELGADO GUMERCINDO DECEASED"}),
    ]
    contacts, deceased = rd.contacts_for({"owner": "DELGADO GUMERCINDO"}, None, srcs)
    # affidavit not read yet: the signer is only an affiant, ranked last
    assert [c["role"] for c in contacts] == ["Estate representative", "Affiant"]
    assert contacts[0]["name"] == "Maria Elena Delgado"
    assert deceased == ["Gumercindo Delgado"]     # never offered as a contact


AFFIDAVIT_TEXT = open(__file__.replace("test_distress.py", "fixtures/affidavit_berrones.txt"), encoding="utf-8").read()


def test_affidavit_heirs_not_affiant():
    from distress.heirship import parse_affidavit
    import run_distress as rd
    a = parse_affidavit(AFFIDAVIT_TEXT, "BERRONES JOSE GUADALUPE")
    assert a["affiant"]["name"] == "Eva Maria Mireles"
    assert a["affiant"]["relationship"] == "daughter-in-law" and not a["affiant"]["is_heir"]
    names = {h["name"]: h for h in a["heirs"]}
    assert names["San Juana Ginez Berrones"]["relation"] == "surviving spouse"
    assert names["Nato Berrones"]["address"].startswith("306 N. Bob Garcia")
    assert "José Guadalupe Berrones Jr." in names          # the son, not the decedent
    rec = {"category": "heirship", "doc_number": "2026-3796238", "affidavit": a,
           "grantor": "MIRELES EVA MARIA; BERRONES JOSE GUADALUPE DECEASED", "grantee": "BERRONES JOSE GUADALUPE DECEASED"}
    contacts, deceased = rd.contacts_for({"owner": "BERRONES JOSE G & SAN JUANA"}, None, [("clerk", rec)])
    assert contacts[0]["role"] == "Heir"
    assert any(c["name"].endswith("Jr.") for c in contacts)   # the son isn't mistaken for his late father
    assert contacts[-1]["name"] == "Eva Maria Mireles" and contacts[-1]["role"] == "Affiant"
    assert "Jose Berrones" in deceased
