import csv

from credit_extract.contracts import ExtractedField, GroundingStatus, SourceRef
from credit_extract.normalise import normalise
from credit_extract.review import COLUMNS, export_rows, ingest
from tests.test_evaluate import doc as make_doc


def fld(fid, name, raw, status="needs_review", canonical=None, method="none"):
    return ExtractedField(field_id=fid, name=name, raw_value=raw, value=normalise(raw), model_confidence=0.9,
                          canonical_name=canonical, match_method=method, review_status=status,
                          review_reasons=["mapping: fuzzy match"] if method == "fuzzy" else [],
                          source=SourceRef(page=2, source_text=raw, grounding=GroundingStatus.verified))


def test_export_lists_review_items_only():
    d = make_doc(fields=[fld("f1", "Applicable Rate (Level 1)", "0.910%", canonical="applicable_margin", method="fuzzy"),
                         fld("f2", "Maturity Date", "October 28, 2027", status="auto_accepted")])
    rows = export_rows(d)
    assert [r["item_id"] for r in rows] == ["f1"] and set(rows[0]) == set(COLUMNS)


def test_ingest_applies_decisions_and_feeds_dictionary_and_golden(tmp_path):
    d = make_doc(fields=[fld("f1", "Applicable Rate (Level 1)", "0.910%", canonical="applicable_margin", method="fuzzy"),
                         fld("f2", "Agent Fee", "$50,000"), fld("f3", "Noise", "x")])
    path = tmp_path / "r.csv"
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerow({"item_id": "f1", "kind": "field", "decision": "confirm", "reviewer": "Jane Smith"})
        w.writerow({"item_id": "f2", "kind": "field", "decision": "correct", "corrected_value": "$55,000",
                    "corrected_canonical_name": "agency_fee", "reviewer": "Jane Smith"})
        w.writerow({"item_id": "f3", "kind": "field", "decision": "reject", "reviewer": "Jane Smith"})
    out, r = ingest(d, path)
    status = {f.field_id: (f.review_status, f.raw_value, f.canonical_name) for f in out.flat_fields}
    assert status == {"f1": ("confirmed", "0.910%", "applicable_margin"), "f2": ("corrected", "$55,000", "agency_fee"),
                      "f3": ("rejected", "x", None)}  # rejected is kept, never deleted
    assert [(a.canonical_name, a.surface_form) for a in r.alias_proposals] == \
        [("applicable_margin", "Applicable Rate (Level 1)"), ("agency_fee", "Agent Fee")]
    assert [g["verification"] for g in r.golden_rows] == ["ok", "corrected"]
    assert len(r.records) == 3 and not r.reviewer_is_ai and not r.errors


def test_ingest_rejects_rows_without_reviewer_or_with_bad_decision(tmp_path):
    d = make_doc(fields=[fld("f1", "X", "1")])
    path = tmp_path / "r.csv"
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerow({"item_id": "f1", "kind": "field", "decision": "confirm"})
        w.writerow({"item_id": "f1", "kind": "field", "decision": "maybe", "reviewer": "Jane"})
        w.writerow({"item_id": "f1", "kind": "field", "decision": "correct", "reviewer": "Jane"})
    _, r = ingest(d, path)
    assert len(r.errors) == 3
