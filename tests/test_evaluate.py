import csv
from datetime import datetime, timezone

from credit_extract.contracts import (Clause, DocumentExtraction, ExtractedField, PageCoverage, Party, Provenance,
                                      Section, SourceRef, Table, TableCell)
from credit_extract.evaluate import evaluate, load_golden, wilson
from credit_extract.normalise import normalise

HEADER = ["document_id", "version_label", "canonical_name", "instance_key", "expected_value", "source_page",
          "source_quote", "drafted_by", "verified_by", "verification", "notes"]


def golden_csv(tmp_path, rows):
    path = tmp_path / "golden.csv"
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        for r in rows:
            w.writerow(["doc", "v1", *r])
    return path


def grid() -> Table:
    rows = [["Pricing Level", "Applicable Rate for Term SOFR Loans", "Applicable Rate for Base Rate Loans"],
            ["1", "0.910%", "0.000%"], ["2", "1.025%", "0.025%"]]
    return Table(table_id="t1", pages=[2], n_rows=3, n_cols=3,
                 cells=[TableCell(row=r, col=c, raw_value=v, model_confidence=1.0)
                        for r, row in enumerate(rows) for c, v in enumerate(row)])


def doc(tables=(), fields=(), clause_text="") -> DocumentExtraction:
    return DocumentExtraction(
        document_id="doc", version_label="v1", effective_date=None,
        parties=[Party(name="BANK OF AMERICA, N.A.", role="Administrative Agent",
                       source=SourceRef(page=1, source_text="BANK OF AMERICA, N.A., as Administrative Agent"))],
        sections=[Section(section_id="s0", title=None, clauses=[
            Clause(clause_id="c1", number=None, heading=None, text=clause_text, page_range=(2, 2),
                   tables=list(tables))])],
        defined_terms=[], flat_fields=list(fields),
        page_coverage=[PageCoverage(page=p, status="ok" if p <= 2 else "not_extracted", ocr_token_count=10,
                                    covered_token_ratio=1.0) for p in (1, 2, 3)],
        provenance=Provenance(source_sha256="a" * 64, page_count=3, pipeline_version="0", model_id="m",
                              model_params={}, prompt_versions={"pass1_extraction": "v2"}, dictionary_version=1,
                              ocr_engine="x", run_id="r", extracted_at=datetime.now(timezone.utc)))


def test_only_verified_rows_count_unless_provisional(tmp_path):
    path = golden_csv(tmp_path, [("a", "", "1", 1, "q", "c", "", "", ""),
                                 ("b", "", "2", 1, "q", "c", "me", "ok", ""),
                                 ("c", "", "3", 1, "q", "c", "me", "wrong", "")])
    assert [r.canonical_name for r in load_golden(path)] == ["b"]
    assert [r.canonical_name for r in load_golden(path, include_unverified=True)] == ["a", "b"]


def test_grid_rows_scored_by_cell_position_not_value(tmp_path):
    # 0.910% expected in the Base Rate column: the value exists in the table, but in the wrong cell.
    rows = load_golden(golden_csv(tmp_path, [
        ("applicable_margin", "level1|term_sofr", "0.910%", 2, "q", "c", "me", "ok", ""),
        ("applicable_margin", "level2|base_rate", "0.025%", 2, "q", "c", "me", "ok", ""),
        ("applicable_margin", "level1|base_rate", "0.910%", 2, "q", "c", "me", "ok", ""),
    ]))
    r = evaluate(doc(tables=[grid()]), rows, "g.csv", False, "clean")
    assert [x.cell_check for x in r.results] == ["right_cell", "right_cell", "wrong_cell"]
    assert r.grid_cell_accuracy == round(2 / 3, 4)


def test_structured_vs_text_only_vs_missed_vs_not_extracted(tmp_path):
    f = ExtractedField(field_id="f1", name="Aggregate Commitments", raw_value="$1,500,000,000",
                       value=normalise("$1,500,000,000"), model_confidence=0.9, review_status="needs_review",
                       source=SourceRef(page=2, source_text="is $1,500,000,000"))
    rows = load_golden(golden_csv(tmp_path, [
        ("total_commitments", "", "$1,500,000,000", 2, "q", "c", "me", "ok", ""),     # field
        ("administrative_agent", "", "BANK OF AMERICA, N.A.", 1, "q", "c", "me", "ok", ""),  # party
        ("governing_law", "", "the law of the State of New York", 2, "q", "c", "me", "ok", ""),  # text only
        ("maturity_date", "", "October 28, 2027", 2, "q", "c", "me", "ok", ""),       # missed
        ("lender_commitment", "boa", "$170,000,000", 3, "q", "c", "me", "ok", ""),    # page not extracted
        ("security", "", "", 2, "", "c", "me", "ok", "absent"),                       # absent: pending
    ]))
    r = evaluate(doc(fields=[f], clause_text="governed by the law of the State of New York"), rows, "g", False, "clean")
    assert [x.status for x in r.results] == ["found", "found", "text_only", "missed", "page_not_extracted",
                                             "absent_pending"]
    assert r.rows_scored == 4 and r.value_recall == 0.5 and r.text_only == 1


def test_wilson_interval():
    lo, hi = wilson(86, 86)
    assert 0.95 < lo < 0.96 and hi == 1.0
    assert wilson(0, 0) == (0.0, 1.0)


def test_ai_verified_rows_need_explicit_opt_in(tmp_path):
    path = golden_csv(tmp_path, [("a", "", "1", 1, "q", "c", "Claude", "ok", ""),
                                 ("b", "", "2", 1, "q", "c", "Jane Smith", "ok", "")])
    assert [r.canonical_name for r in load_golden(path)] == ["b"]
    assert [r.canonical_name for r in load_golden(path, allow_ai_verified=True)] == ["a", "b"]
