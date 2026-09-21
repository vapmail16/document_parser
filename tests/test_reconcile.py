from pathlib import Path

from credit_extract.contracts import ExtractedField, GroundingStatus, SourceRef
from credit_extract.dictionary import load_snapshot
from credit_extract.normalise import normalise
from credit_extract.reconcile import base_name, map_field, party_roles, route

SNAP = load_snapshot(Path(__file__).resolve().parents[1] / "dictionary" / "snapshots", 2)


def fld(name, raw, verified=True, conf=0.95) -> ExtractedField:
    return ExtractedField(field_id="f", name=name, raw_value=raw, value=normalise(raw), model_confidence=conf,
                          source=SourceRef(page=1, source_text=raw,
                                           grounding=GroundingStatus.verified if verified else GroundingStatus.not_found))


def mapped(name, raw, section=None):
    m = map_field(fld(name, raw), section, SNAP)
    return m.method, m.canonical_name, m.instance_key


def test_exact_alias_and_base_name():
    assert mapped("Maturity Date", "October 28, 2027") == ("exact", "maturity_date", None)
    assert mapped("Aggregate Commitments", "$1,500,000,000") == ("alias", "total_commitments", None)
    assert base_name("Applicable Rate for Term SOFR Loans (Level 1)") == "applicable rate for term sofr loan"


def test_grid_fields_get_level_and_rate_instances():
    assert mapped("Applicable Rate for Base Rate Loans (Level 3)", "0.125%") == \
        ("fuzzy", "applicable_margin", "level3|base_rate")
    assert mapped("Applicable Rate for Term SOFR Loans (Pricing Level II)", "1.025%")[2] == "level2|term_sofr"


def test_type_gate_blocks_wrong_kind_of_value():
    # "Maturity Date extension notice" carries a duration, not a date: never maturity_date via fuzzy.
    assert mapped("Maturity Date extension notice period", "30 days")[1] != "maturity_date"


def test_one_word_forms_do_not_swallow_longer_names():
    for name, raw in [("Applicant Borrower reference", "Section 2.14"),
                      ("Minimum Collateral Amount (L/C Obligations)", "100% of the Outstanding Amount"),
                      ("Non-Extending Lender cross-reference", "Section 2.15")]:
        assert mapped(name, raw)[0] == "none", name


def test_party_fields_never_take_cross_references():
    assert mapped("Agent Parties", "Section 11.02")[1] != "administrative_agent"


def test_confusable_partner_never_mapped():
    assert mapped("Consolidated Adjusted EBITDA", "an amount equal to ...")[1] != "consolidated_ebitda_definition"
    assert mapped("Maximum Senior Secured Leverage Ratio", "2.50 to 1.00")[1] != "max_total_leverage_ratio"


def test_section_context_maps_events_of_default_only_with_a_real_instance():
    section = "ARTICLE IX.\nEVENTS OF DEFAULT AND REMEDIES"
    assert mapped("Other Defaults cure period", "thirty days", section) == \
        ("fuzzy", "event_of_default", "other_defaults_grace")
    assert mapped("Anti-Corruption Legislation", "FCPA", section)[0] == "none"


def test_routing_fuzzy_and_unresolved_instance_go_to_review():
    f = fld("Applicable Rate for Base Rate Loans (Level 3)", "0.125%")
    r = route(f, map_field(f, None, SNAP))
    assert r.review_status == "needs_review" and any(x.startswith("mapping: fuzzy") for x in r.review_reasons)
    f = fld("Level 5 Applicable Rate (column 3)", "1.375%")
    r = route(f, map_field(f, None, SNAP))
    assert "mapping: instance of repeatable applicable_margin not determined" in r.review_reasons
    f = fld("Maturity Date", "October 28, 2027")
    assert route(f, map_field(f, None, SNAP)).review_status == "auto_accepted"
    f = fld("Maturity Date", "October 28, 2027", verified=False)
    assert route(f, map_field(f, None, SNAP)).review_reasons == ["value: not verified at source"]


def test_party_roles():
    assert party_roles("Administrative Agent and Swing Line Lender", SNAP) == ["administrative_agent"]
    assert party_roles("Lender", SNAP) == ["lender"]
    assert party_roles("Syndication Agent", SNAP) == []


def test_schedule_rows_become_labelled_fields():
    from datetime import datetime, timezone

    from credit_extract.contracts import (Clause, DocumentExtraction, PageCoverage, Provenance, Section, Table,
                                          TableCell)
    from credit_extract.reconcile import reconcile

    rows = [["Lender", "Commitment", "Pro Rata Share"], ["Bank of America, N.A.", "$170,000,000.00", "11.3%"],
            ["Total", "$1,500,000,000.00", "100%"]]
    t = Table(table_id="t2", pages=[1], n_rows=3, n_cols=3,
              cells=[TableCell(row=r, col=c, raw_value=v, model_confidence=1.0,
                               source=SourceRef(page=1, source_text=v, grounding=GroundingStatus.verified))
                     for r, row in enumerate(rows) for c, v in enumerate(row)])
    doc = DocumentExtraction(
        document_id="d", version_label="v", effective_date=None, parties=[], defined_terms=[], flat_fields=[],
        sections=[Section(section_id="s", title=None, clauses=[
            Clause(clause_id="c", number=None, heading=None, text="Schedule 2.01", page_range=(1, 1), tables=[t])])],
        page_coverage=[PageCoverage(page=1, status="ok", ocr_token_count=5, covered_token_ratio=1.0)],
        provenance=Provenance(source_sha256="a" * 64, page_count=1, pipeline_version="0", model_id="m",
                              model_params={}, prompt_versions={}, dictionary_version=2, ocr_engine="x", run_id="r",
                              extracted_at=datetime.now(timezone.utc)))
    out, _ = reconcile(doc, SNAP)
    got = {(f.canonical_name, f.instance_key, f.raw_value) for f in out.flat_fields if f.canonical_name}
    assert ("lender_commitment", "bank_of_america_n_a", "$170,000,000.00") in got
    assert ("lender", "bank_of_america_n_a", "Bank of America, N.A.") in got
    assert not any("Total" in f.name for f in out.flat_fields)  # totals are checked, not extracted


def test_party_fields_never_take_dates():
    assert mapped("Administrative Agent Fee Letter date", "October 5, 2022")[1] != "administrative_agent"


def test_value_that_repeats_its_label_goes_to_review():
    f = fld("event_of_default (non-payment)", "Non-Payment")
    r = route(f, map_field(f, None, SNAP))
    assert r.review_status == "needs_review" and "value: repeats the field's own label, not a value" in r.review_reasons
    f = fld("event_of_default (non-payment)", "five Business Days")
    assert "value: repeats the field's own label, not a value" not in route(f, map_field(f, None, SNAP)).review_reasons
