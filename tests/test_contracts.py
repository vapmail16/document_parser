from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from credit_extract.contracts import (
    Alias,
    DocumentExtraction,
    ExtractedField,
    GroundingStatus,
    NormalisedValue,
    PageCoverage,
    PageEnvelope,
    Provenance,
    SourceRef,
    Table,
    TableCell,
    ValueType,
)


def src(**kw) -> SourceRef:
    return SourceRef(page=kw.pop("page", 3), source_text="aggregate principal amount of $50,000,000", **kw)


def field(**kw) -> ExtractedField:
    defaults = dict(field_id="f1", name="Total Commitments", raw_value="$50,000,000", model_confidence=0.9,
                    source=src())
    return ExtractedField(**(defaults | kw))


def provenance(page_count: int = 2) -> Provenance:
    return Provenance(
        source_sha256="a" * 64, page_count=page_count, pipeline_version="0.1.0", model_id="frontier",
        model_params={}, prompt_versions={"pass1_extraction": "v1"}, dictionary_version=1,
        ocr_engine="tesseract", run_id="r1", extracted_at=datetime(2026, 9, 21, tzinfo=timezone.utc),
    )


def test_money_serialises_as_string_not_float():
    v = NormalisedValue(type=ValueType.money, amount=Decimal("50000000.00"), currency="USD")
    assert '"amount":"50000000.00"' in v.model_dump_json()
    assert NormalisedValue.model_validate_json(v.model_dump_json()) == v


def test_value_requires_slot_for_its_type():
    with pytest.raises(ValidationError, match="requires 'amount'"):
        NormalisedValue(type=ValueType.money, currency="USD")
    with pytest.raises(ValidationError, match="requires 'date_value'"):
        NormalisedValue(type=ValueType.date, text="18 Jan 2013")


def test_confidence_bounded():
    with pytest.raises(ValidationError):
        field(model_confidence=1.2)


def test_source_text_required_and_nonempty():
    with pytest.raises(ValidationError):
        SourceRef(page=1, source_text="")


def test_bbox_must_be_ordered_fractions():
    assert src(bbox=(0.1, 0.2, 0.5, 0.3)).bbox == (0.1, 0.2, 0.5, 0.3)
    with pytest.raises(ValidationError):
        src(bbox=(0.5, 0.2, 0.1, 0.3))
    with pytest.raises(ValidationError):
        src(bbox=(0.1, 0.2, 1.5, 0.3))


def test_auto_accept_requires_verified_grounding():
    with pytest.raises(ValidationError, match="auto_accepted requires"):
        field(review_status="auto_accepted")
    ok = field(review_status="auto_accepted", source=src(grounding=GroundingStatus.verified))
    assert not ok.hallucination_flag


def test_hallucination_flag_derived_from_grounding():
    assert field(source=src(grounding=GroundingStatus.not_found)).hallucination_flag


def test_unknown_keys_rejected():
    with pytest.raises(ValidationError):
        SourceRef(page=1, source_text="x", confidence=0.9)


def test_table_cells_within_bounds():
    cell = dict(raw_value="2.25%", model_confidence=0.8)
    Table(table_id="t1", pages=[4, 5], n_rows=2, n_cols=2, cells=[TableCell(row=1, col=1, **cell)])
    with pytest.raises(ValidationError, match="exceeds table size"):
        Table(table_id="t1", pages=[4], n_rows=2, n_cols=2, cells=[TableCell(row=1, col=1, col_span=2, **cell)])


def test_machine_learned_alias_cannot_be_approved():
    Alias(surface_form="Interest Margin", learned_via="fuzzy")  # pending by default
    with pytest.raises(ValidationError, match="cannot be approved"):
        Alias(surface_form="Interest Margin", learned_via="embedding", status="approved")


def _doc(coverage_pages, fields=()) -> DocumentExtraction:
    return DocumentExtraction(
        document_id="iec", version_label="4th-AR", effective_date=date(2013, 1, 18),
        parties=[], sections=[], defined_terms=[], flat_fields=list(fields),
        page_coverage=[PageCoverage(page=p, status="ok", ocr_token_count=10, covered_token_ratio=1.0)
                       for p in coverage_pages],
        provenance=provenance(page_count=2),
    )


def test_every_page_needs_a_coverage_record():
    _doc([1, 2])
    with pytest.raises(ValidationError, match="one record per page"):
        _doc([1])
    with pytest.raises(ValidationError, match="one record per page"):
        _doc([1, 2, 2])


def test_duplicate_field_ids_rejected():
    with pytest.raises(ValidationError, match="duplicate field_id"):
        _doc([1, 2], fields=[field(), field()])


def test_envelope_schema_is_closed():
    schema = PageEnvelope.model_json_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"blocks", "fields", "tables", "parties"}
