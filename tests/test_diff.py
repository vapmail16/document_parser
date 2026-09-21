from credit_extract.contracts import Clause, Section, Table, TableCell
from credit_extract.diff import diff
from tests.test_evaluate import doc as make_doc
from tests.test_review import fld

CLAUSES = [
    ("1.01", "Defined Terms", "“Applicable Rate” means the percentages per annum set forth below based on the Debt Rating."),
    ("2.05", "Prepayments", "The Borrowers may, upon notice to the Administrative Agent, voluntarily prepay Loans in whole or in part."),
    ("8.06", "Financial Covenant", "Permit the Consolidated Leverage Ratio as of the end of any fiscal quarter to be greater than 3.50 to 1.0."),
]


def grid(level1="0.910%") -> Table:
    rows = [["Pricing Level", "Term SOFR Loans"], ["1", level1], ["2", "1.025%"]]
    return Table(table_id="t1", pages=[2], n_rows=3, n_cols=2,
                 cells=[TableCell(row=r, col=c, raw_value=v, model_confidence=1.0)
                        for r, row in enumerate(rows) for c, v in enumerate(row)])


def version(label, clauses, margin="0.910%", leverage="3.50 to 1.0"):
    cl = [Clause(clause_id=f"{label}-{i}", number=n, heading=h, text=t, page_range=(2, 2),
                 tables=[grid(margin)] if i == 0 else []) for i, (n, h, t) in enumerate(clauses)]
    fields = [fld("f1", "Applicable Rate (Level 1)", margin, canonical="applicable_margin", status="confirmed"),
              fld("f2", "Maximum Consolidated Leverage Ratio", leverage, canonical="max_total_leverage_ratio",
                  status="confirmed")]
    fields = [f.model_copy(update={"instance_key": "level1|term_sofr" if f.field_id == "f1" else "base"}) for f in fields]
    d = make_doc(fields=fields)
    return d.model_copy(update={"version_label": label, "sections": [Section(section_id="s", title=None, clauses=cl)]})


def test_renumbered_and_reordered_restatement_has_no_changes():
    renumbered = [("1.01", CLAUSES[0][1], CLAUSES[0][2]), ("10.06", CLAUSES[2][1], CLAUSES[2][2]),
                  ("3.05", CLAUSES[1][1], CLAUSES[1][2])]
    d = diff(version("v1", CLAUSES), version("v2", renumbered))
    assert all(a.status == "matched" and not a.text_changed for a in d.clause_alignments)
    assert all(c.change_type == "unchanged" for c in d.changes) and not d.table_changes


def test_margin_change_and_relaxed_covenant_are_reported_with_sources():
    edited = [CLAUSES[0], CLAUSES[1], ("8.06", "Financial Covenant", CLAUSES[2][2].replace("3.50", "4.00"))]
    d = diff(version("v1", CLAUSES), version("v2", edited, margin="1.000%", leverage="4.00 to 1.0"))
    changed = {c.canonical_name: c for c in d.changes if c.change_type == "modified"}
    assert set(changed) == {"applicable_margin", "max_total_leverage_ratio"}
    assert str(changed["max_total_leverage_ratio"].new_value.amount) == "4"
    assert changed["applicable_margin"].old_source and changed["applicable_margin"].new_source
    assert [(cc.row_header, cc.change_type) for t in d.table_changes for cc in t.cell_changes] == [("1", "modified")]
    assert sum(a.text_changed for a in d.clause_alignments) == 1


def test_added_clause_and_comparability_warning():
    extra = CLAUSES + [("2.18", "Sustainability Adjustments", "The Applicable Rate shall be adjusted by up to 0.05% per annum based on KPI performance.")]
    b = version("v2", extra)
    b = b.model_copy(update={"provenance": b.provenance.model_copy(update={"model_id": "onprem_candidate"})})
    d = diff(version("v1", CLAUSES), b)
    assert [a.status for a in d.clause_alignments].count("added") == 1
    assert any("different model" in w for w in d.comparability_warnings)
