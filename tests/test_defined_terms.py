from datetime import datetime, timezone
from pathlib import Path

from credit_extract.contracts import (Clause, DocumentExtraction, ExtractedField, PageCoverage, Party, Provenance,
                                      Section, SourceRef)
from credit_extract.defined_terms import resolve
from credit_extract.dictionary import load_snapshot
from credit_extract.normalise import normalise
from credit_extract.ocr import OcrPage, OcrToken
from credit_extract.reconcile import map_field, reconcile

SNAP = load_snapshot(Path(__file__).resolve().parents[1] / "dictionary" / "snapshots", 3)
PREAMBLE = ("This CREDIT AGREEMENT is entered into among ALBEMARLE CORPORATION, a Virginia corporation (the “Company”), "
            "ALBEMARLE EUROPE SRL, a Belgian company (the “Belgian Borrower” and together with the Company, "
            "collectively, the “Borrowers”), and BANK OF AMERICA, N.A., as Administrative Agent.")
DEFS = "“Threshold Amount” means ONE HUNDRED MILLION DOLLARS ($100,000,000). “Type” means a Loan type."


def ocr(page: int, text: str) -> OcrPage:
    return OcrPage(page=page, image_sha256="0" * 64, engine="fake",
                   tokens=[OcrToken(text=w, conf=0.9, bbox=(0, 0, 0.1, 0.1), block=1, line=0) for w in text.split()])


def doc(fields=(), parties=()) -> DocumentExtraction:
    return DocumentExtraction(
        document_id="d", version_label="v", effective_date=None, parties=list(parties), defined_terms=[],
        flat_fields=list(fields),
        sections=[Section(section_id="s", title=None, clauses=[
            Clause(clause_id="c1", number=None, heading=None, text=PREAMBLE, page_range=(1, 1)),
            Clause(clause_id="c2", number=None, heading=None, text=DEFS, page_range=(2, 2))])],
        page_coverage=[PageCoverage(page=p, status="ok", ocr_token_count=5, covered_token_ratio=1.0) for p in (1, 2, 3)],
        provenance=Provenance(source_sha256="a" * 64, page_count=3, pipeline_version="0", model_id="m", model_params={},
                              prompt_versions={}, dictionary_version=3, ocr_engine="x", run_id="r",
                              extracted_at=datetime.now(timezone.utc)))


PAGES = [ocr(1, PREAMBLE), ocr(2, DEFS),
         ocr(3, "(e) Cross-Default. (i) The Company fails to pay Indebtedness of more than the Threshold Amount; or")]


def test_definitions_inline_terms_collectives_and_usages():
    d = resolve(doc(), PAGES)
    terms = {t.term: t for t in d.defined_terms}
    assert terms["Company"].definition == "ALBEMARLE CORPORATION"
    assert terms["Threshold Amount"].definition.startswith("“Threshold Amount” means ONE HUNDRED MILLION")
    assert set(terms["Borrowers"].members) == {"Company", "Belgian Borrower"}
    assert "Cross-Default" in terms["Threshold Amount"].usage_headings
    assert any(u.page == 3 for u in terms["Company"].usage_locations)


def test_company_party_resolves_to_borrower():
    party = Party(name="ALBEMARLE CORPORATION", role="Company", source=SourceRef(page=1, source_text="ALBEMARLE"))
    out, _ = reconcile(resolve(doc(parties=[party]), PAGES), SNAP)
    assert out.parties[0].canonical_roles == ["borrower"]


def test_threshold_amount_links_to_cross_default_for_review():
    f = ExtractedField(field_id="f", name="Threshold Amount", raw_value="$100,000,000",
                       value=normalise("$100,000,000"), model_confidence=0.95,
                       source=SourceRef(page=2, source_text="“Threshold Amount” means ($100,000,000)"))
    m = map_field(f, None, SNAP, resolve(doc(fields=[f]), PAGES))
    assert (m.canonical_name, m.instance_key, m.method) == ("event_of_default", "cross_default_threshold", "fuzzy")


def test_party_value_that_is_a_defined_term_resolves_to_the_legal_name():
    from credit_extract.defined_terms import entity_name
    from credit_extract.contracts import DefinedTerm

    t = DefinedTerm(term="Bank of America", definition="“Bank of America” means Bank of America, N.A. and its successors.",
                    definition_location=None)
    assert entity_name(t) == "Bank of America, N.A."
    pages = PAGES + [ocr(4, "“Bank of America” means Bank of America, N.A. and its successors. “Base Rate” means ...")]
    f = ExtractedField(field_id="f", name="administrative_agent", raw_value="Bank of America",
                       value=normalise("Bank of America"), model_confidence=0.95,
                       source=SourceRef(page=2, source_text="means Bank of America in its capacity as agent"))
    out, _ = reconcile(resolve(doc(fields=[f]), pages), SNAP)
    got = next(x for x in out.flat_fields if x.field_id == "f")
    assert got.raw_value == "Bank of America" and got.value.text == "Bank of America, N.A."
    assert next(t for t in out.defined_terms if t.term == "Bank of America").from_ocr
