"""Data contracts (spec §10).

Conventions:
- Pages are 1-based.
- Bounding boxes are (x0, y0, x1, y1) as fractions (0..1) of the page image width/height,
  so they are independent of rasterisation DPI.
- Monetary and numeric amounts are Decimal and serialise to JSON strings — never float.
"""

from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Confidence = Annotated[float, Field(ge=0.0, le=1.0)]
Fraction = Annotated[float, Field(ge=0.0, le=1.0)]
PageNo = Annotated[int, Field(ge=1)]


class Contract(BaseModel):
    """Base for all contracts: reject unknown keys so schema drift fails loudly."""

    model_config = ConfigDict(extra="forbid")


# --- Grounding ---------------------------------------------------------------


class GroundingStatus(str, Enum):
    not_checked = "not_checked"
    verified = "verified"  # quote found on cited page AND value derivable from quote
    not_found = "not_found"  # possible hallucination -> always routed to review


class SourceRef(Contract):
    page: PageNo
    source_text: str = Field(min_length=1)  # verbatim supporting quote — required
    region_id: str | None = None
    bbox: tuple[Fraction, Fraction, Fraction, Fraction] | None = None  # from the OCR match, not the VLM
    grounding: GroundingStatus = GroundingStatus.not_checked
    match_score: Confidence | None = None  # quote <-> OCR similarity

    @model_validator(mode="after")
    def _bbox_ordered(self) -> "SourceRef":
        if self.bbox is not None:
            x0, y0, x1, y1 = self.bbox
            if x0 > x1 or y0 > y1:
                raise ValueError(f"bbox must be (x0, y0, x1, y1) with x0<=x1, y0<=y1, got {self.bbox}")
        return self


# --- Values ------------------------------------------------------------------


class ValueType(str, Enum):
    text = "text"
    money = "money"
    percentage = "percentage"
    bps = "bps"
    ratio = "ratio"
    date = "date"
    duration = "duration"
    integer = "integer"
    boolean = "boolean"
    table_ref = "table_ref"  # e.g. Applicable Margin defined by a pricing grid
    term_ref = "term_ref"  # value is another defined term


_AMOUNT_TYPES = {ValueType.money, ValueType.percentage, ValueType.bps, ValueType.ratio,
                 ValueType.integer, ValueType.duration}


class NormalisedValue(Contract):
    type: ValueType
    amount: Decimal | None = None
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")  # ISO 4217; None if ambiguous ("$")
    unit: str | None = None  # "%", "bps", "x", "days", "months", ...
    date_value: date | None = None
    bool_value: bool | None = None
    text: str | None = None
    ref_id: str | None = None  # table_id or defined term

    @model_validator(mode="after")
    def _slot_for_type(self) -> "NormalisedValue":
        required = (
            "amount" if self.type in _AMOUNT_TYPES
            else "date_value" if self.type is ValueType.date
            else "bool_value" if self.type is ValueType.boolean
            else "ref_id" if self.type in (ValueType.table_ref, ValueType.term_ref)
            else "text"
        )
        if getattr(self, required) is None:
            raise ValueError(f"NormalisedValue of type {self.type.value!r} requires {required!r}")
        return self


# --- Fields ------------------------------------------------------------------


class AgreementSignals(Contract):
    pass1_pass2_agree: bool | None = None
    ocr_agree: bool | None = None  # == grounding verified
    second_model_agree: bool | None = None  # eval / dual-model runs
    arithmetic_check: bool | None = None  # table/sum checks where applicable


class ExtractedField(Contract):
    field_id: str  # unique within the extraction
    name: str  # raw surface form as returned
    canonical_name: str | None = None
    match_method: Literal["exact", "alias", "defined_term", "fuzzy", "embedding", "none"] = "none"
    match_score: Confidence | None = None
    instance_key: str | None = None  # disambiguates repeats: tranche / lender / test date
    clause_id: str | None = None
    raw_value: str | None  # as printed
    value: NormalisedValue | None = None  # deterministic normalisation of raw_value
    model_confidence: Confidence  # model self-report — weak signal on its own
    agreement: AgreementSignals = Field(default_factory=AgreementSignals)
    routing_score: Confidence | None = None  # computed in Stage 8 (spec §15)
    source: SourceRef  # grounding — required
    vision_noise_flag: bool = False
    origin: Literal["model", "human"] = "model"
    extraction_pass: Literal[1, 2] = 1  # 1: open-ended; 2: schema-hinted sweep (spec §8)
    review_status: Literal["auto_accepted", "needs_review", "confirmed", "corrected", "rejected"] = "needs_review"
    review_reasons: list[str] = []  # why a reviewer sees it: "value: …", "mapping: …"

    @property
    def hallucination_flag(self) -> bool:
        return self.source.grounding is GroundingStatus.not_found

    @model_validator(mode="after")
    def _auto_accept_requires_grounding(self) -> "ExtractedField":
        # Grounding is a hard gate (spec §15): nothing is auto-accepted unless verified.
        if self.review_status == "auto_accepted" and self.source.grounding is not GroundingStatus.verified:
            raise ValueError("auto_accepted requires source.grounding == verified")
        return self


# --- Tables ------------------------------------------------------------------


class TableCell(Contract):
    row: Annotated[int, Field(ge=0)]
    col: Annotated[int, Field(ge=0)]
    row_span: Annotated[int, Field(ge=1)] = 1
    col_span: Annotated[int, Field(ge=1)] = 1
    is_header: bool = False
    raw_value: str | None
    value: NormalisedValue | None = None
    model_confidence: Confidence
    source: SourceRef | None = None


class TableCheck(Contract):
    name: str  # e.g. "commitments_sum_to_facility_amount"
    passed: bool
    detail: str | None = None


class Table(Contract):
    table_id: str
    caption: str | None = None
    clause_id: str | None = None
    pages: list[PageNo] = Field(min_length=1)  # multi-page tables are stitched into one
    n_rows: Annotated[int, Field(ge=0)]
    n_cols: Annotated[int, Field(ge=0)]
    cells: list[TableCell]
    checks: list[TableCheck] = []

    @model_validator(mode="after")
    def _cells_in_bounds(self) -> "Table":
        for c in self.cells:
            if c.row + c.row_span > self.n_rows or c.col + c.col_span > self.n_cols:
                raise ValueError(
                    f"{self.table_id}: cell ({c.row},{c.col}) span ({c.row_span},{c.col_span}) "
                    f"exceeds table size {self.n_rows}x{self.n_cols}"
                )
        return self


# --- Document structure ------------------------------------------------------


class Party(Contract):
    name: str
    role: str  # Borrower, Administrative Agent, Lender, Guarantor, ...
    source: SourceRef
    canonical_roles: list[str] = []  # dictionary keys the role maps to (set by reconciliation)


class DefinedTerm(Contract):
    term: str
    definition: str | None
    definition_location: SourceRef | None
    references: list[str] = []  # other defined terms / table_ids the definition points at
    members: list[str] = []  # collective terms: "Borrowers" = the Company + the Belgian Borrower
    usage_locations: list[SourceRef] = []
    usage_headings: list[str] = []  # sub-clause headings it is used under, e.g. "Cross-Default"
    from_ocr: bool = False  # definition read from OCR of a page the model did not extract: linking only


class Clause(Contract):
    clause_id: str  # stable WITHIN a version: hierarchical path + content hash
    number: str | None  # as printed, e.g. "6.12(b)" — NOT used for cross-version identity
    heading: str | None
    text: str | None
    page_range: tuple[PageNo, PageNo]
    fields: list[ExtractedField] = []
    tables: list[Table] = []

    @model_validator(mode="after")
    def _page_range_ordered(self) -> "Clause":
        if self.page_range[0] > self.page_range[1]:
            raise ValueError(f"{self.clause_id}: page_range start after end {self.page_range}")
        return self


class Section(Contract):
    section_id: str
    title: str | None
    clauses: list[Clause]


class PageCoverage(Contract):
    page: PageNo
    # not_extracted: deliberately outside this run's page selection (e.g. a sample) — explicit, never silent
    status: Literal["ok", "low_coverage", "failed", "blank", "not_extracted"]
    ocr_token_count: Annotated[int, Field(ge=0)]
    covered_token_ratio: Fraction
    tiles: Annotated[int, Field(ge=1)] = 1
    attempts: Annotated[int, Field(ge=1)] = 1
    text_layer: Literal["none", "untrusted", "trusted"] = "none"


class Provenance(Contract):
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_count: Annotated[int, Field(ge=1)]
    pipeline_version: str
    model_id: str  # key in models.yaml
    model_params: dict[str, Any]
    prompt_versions: dict[str, str]  # {"pass1_extraction": "v1", ...}
    dictionary_version: Annotated[int, Field(ge=1)]
    ocr_engine: str
    run_id: str
    extracted_at: datetime


class AmendmentInstruction(Contract):
    target_ref: str  # as printed: 'Section 6.12(b)', 'definition of "Applicable Margin"'
    operation: Literal["replace", "delete", "insert", "restate", "other"]
    old_text: str | None
    new_text: str | None
    source: SourceRef


class DocumentExtraction(Contract):
    document_id: str
    version_label: str
    effective_date: date | None
    execution_status: Literal["executed", "draft", "specimen", "unknown"] = "unknown"
    document_kind: Literal["original", "restatement", "amendment", "unknown"] = "unknown"
    parties: list[Party]
    sections: list[Section]
    defined_terms: list[DefinedTerm]
    flat_fields: list[ExtractedField]
    amendment_instructions: list[AmendmentInstruction] = []
    page_coverage: list[PageCoverage]
    provenance: Provenance

    @model_validator(mode="after")
    def _no_silently_dropped_pages(self) -> "DocumentExtraction":
        # Spec §1: content is never silently dropped — every page has a coverage record.
        pages = sorted(p.page for p in self.page_coverage)
        expected = list(range(1, self.provenance.page_count + 1))
        if pages != expected:
            raise ValueError(
                f"page_coverage must have exactly one record per page 1..{self.provenance.page_count}"
            )
        ids = [f.field_id for f in self.flat_fields]
        if len(ids) != len(set(ids)):
            raise ValueError("flat_fields contain duplicate field_id values")
        return self


# --- Dictionary (spec §9) ----------------------------------------------------


class Alias(Contract):
    surface_form: str
    status: Literal["approved", "pending_review"] = "pending_review"
    learned_via: Literal["seed", "human", "fuzzy", "embedding"]
    first_seen_document: str | None = None

    @model_validator(mode="after")
    def _no_auto_approved_aliases(self) -> "Alias":
        # Poisoning guard (spec §9): machine-learned aliases are never approved without a human.
        if self.learned_via in ("fuzzy", "embedding") and self.status == "approved":
            raise ValueError(
                f"alias {self.surface_form!r} learned via {self.learned_via} cannot be approved "
                "directly; a human approval must re-record it as learned_via='human'"
            )
        return self


class InstanceRule(Contract):
    """One part of a repeatable field's instance_key, derived from the extracted field name.

    kind="level":  "Level 3" / "Pricing Level 3"   -> "level3"
    kind="choice": first choice whose terms occur in the name -> its key (else `default`)
    kind="slug":   the name's parenthesised qualifier, or the value, as a slug ("bank_of_america_n_a")
    Parts are joined with "|" in order; if a part without a default cannot be resolved the
    instance_key stays unset (and the mapping is not used for golden-key scoring).
    """

    part: str
    kind: Literal["level", "choice", "slug"]
    choices: dict[str, list[str]] = {}
    slug_from: Literal["qualifier", "value"] = "qualifier"
    default: str | None = None


class FieldDictionaryEntry(Contract):
    canonical_name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    display_name: str
    aliases: list[Alias] = []
    business_meaning: str | None = None
    credit_risk_relevance: str | None = None
    downstream_use: str | None = None
    typical_location: str | None = None
    data_type: ValueType | None = None
    repeatable: bool = False  # may occur multiple times -> instance_key required
    instance_rules: list[InstanceRule] = []  # how to derive instance_key for repeatable fields
    value_guidance: str | None = None  # what the VALUE is, for the pass-2 prompt ("the grace period, not the heading")
    value_is_name: bool = False  # the value is the instance's own name (a lender, a rate option) — no label-echo check
    core: bool = False  # expected for this doc type -> coverage check + acceptance
    status: Literal["approved", "pending_review"] = "pending_review"
    first_seen: date
    last_seen: date
    version: Annotated[int, Field(ge=1)] = 1

    def surface_forms(self, approved_only: bool = True) -> list[str]:
        forms = [self.display_name]
        forms += [a.surface_form for a in self.aliases if a.status == "approved" or not approved_only]
        return forms


def normalise_surface_form(s: str) -> str:
    """Casefold and collapse punctuation/whitespace: 'Applicable  Margin' == 'applicable margin'."""
    return " ".join("".join(ch if ch.isalnum() else " " for ch in s.casefold()).split())


class DictionarySnapshot(Contract):
    """Immutable, versioned dictionary state. A run pins exactly one (spec §9)."""

    version: Annotated[int, Field(ge=1)]
    parent_version: int | None = None
    created_at: datetime
    notes: str | None = None
    entries: list[FieldDictionaryEntry]
    # Surface forms that must never be merged, even if they score as near-identical.
    confusable_pairs: list[tuple[str, str]] = []

    @model_validator(mode="after")
    def _consistent(self) -> "DictionarySnapshot":
        names = [e.canonical_name for e in self.entries]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(f"duplicate canonical_name(s): {sorted(dupes)}")

        owner: dict[str, str] = {}
        for e in self.entries:
            for form in e.surface_forms(approved_only=False):
                key = normalise_surface_form(form)
                if key in owner and owner[key] != e.canonical_name:
                    raise ValueError(
                        f"surface form {form!r} maps to both {owner[key]!r} and {e.canonical_name!r}"
                    )
                owner[key] = e.canonical_name

        for a, b in self.confusable_pairs:
            ka, kb = normalise_surface_form(a), normalise_surface_form(b)
            if ka == kb:
                raise ValueError(f"confusable pair has identical members: {a!r}")
            if ka in owner and kb in owner and owner[ka] == owner[kb]:
                raise ValueError(
                    f"confusable pair {a!r} / {b!r} are both surface forms of {owner[ka]!r}"
                )
        return self

    def entry(self, canonical_name: str) -> FieldDictionaryEntry:
        for e in self.entries:
            if e.canonical_name == canonical_name:
                return e
        raise KeyError(canonical_name)

    @property
    def core_fields(self) -> list[FieldDictionaryEntry]:
        return [e for e in self.entries if e.core]


# --- Human review (spec §14) -------------------------------------------------


class ReviewRecord(Contract):
    review_id: str
    document_id: str | None
    version_label: str | None
    target_kind: Literal["field", "table_cell", "clause_alignment", "dictionary_entry", "alias", "page"]
    target_id: str
    reason_flagged: str  # not_found / low routing_score / proposed mapping / low_coverage / ...
    decision: Literal["confirmed", "corrected", "rejected"]
    old_value: str | None
    new_value: str | None
    reviewer: str
    reviewed_at: datetime
    notes: str | None = None


# --- Diff (spec §11) ---------------------------------------------------------


class ClauseAlignment(Contract):
    from_clause_id: str | None
    to_clause_id: str | None
    status: Literal["matched", "added", "removed"]
    score: Confidence | None
    text_changed: bool = False
    low_confidence_alignment: bool = False


class FieldChange(Contract):
    canonical_name: str  # unmapped fields are diffed by raw name within aligned clauses
    instance_key: str | None
    from_clause_id: str | None
    to_clause_id: str | None
    old_value: NormalisedValue | None
    new_value: NormalisedValue | None
    old_source: SourceRef | None  # audit: show both sides
    new_source: SourceRef | None
    change_type: Literal["added", "removed", "modified", "unchanged"]
    confidence: Confidence  # min(routing_score) of the two sides
    low_confidence_change: bool


class CellChange(Contract):
    row_header: str | None  # cells aligned by header labels, not indices
    col_header: str | None
    old_value: NormalisedValue | None
    new_value: NormalisedValue | None
    change_type: Literal["added", "removed", "modified"]


class TableChange(Contract):
    from_table_id: str | None
    to_table_id: str | None
    cell_changes: list[CellChange]


class VersionDiff(Contract):
    document_id: str
    from_version: str
    to_version: str
    from_provenance: Provenance
    to_provenance: Provenance
    comparability_warnings: list[str]  # different model / prompt / dictionary versions between sides
    clause_alignments: list[ClauseAlignment]
    changes: list[FieldChange]
    table_changes: list[TableChange]


# --- Ground truth & evaluation (spec §13) ------------------------------------


class GoldenField(Contract):
    document_id: str
    version_label: str
    canonical_name: str
    instance_key: str | None = None
    expected_value: NormalisedValue | None  # None == field is expected to be absent
    source_page: PageNo
    labelled_by: str | None = None
    notes: str | None = None

    @property
    def key(self) -> str:
        return f"{self.canonical_name}|{self.instance_key or ''}"


class GoldenTable(Contract):
    document_id: str
    version_label: str
    caption: str | None
    pages: list[PageNo] = Field(min_length=1)
    cells: list[tuple[int, int, str | None]]  # (row, col, expected raw value)


class EvalResult(Contract):
    model_id: str
    document_id: str
    input_variant: Literal["clean", "degraded"]
    provenance: Provenance
    n_golden: Annotated[int, Field(ge=0)]
    recall: Fraction  # golden fields found with the correct value
    precision_on_golden_keys: Fraction  # of extracted fields mapped to a golden key, share correct
    audited_precision: Fraction | None  # sampled human audit of open-ended (non-golden) fields
    table_cell_accuracy: Fraction | None
    hallucination_rate: Fraction  # share of extracted fields with grounding == not_found
    ci95: dict[str, tuple[float, float]]  # Wilson intervals per metric
    calibration_error: float | None  # routing_score vs observed correctness
    cost_per_page: float | None
    latency_per_doc_s: float | None
    per_field: dict[str, bool]  # key = GoldenField.key


# --- Ingest, rasterise & run records (spec §6 Stages 0-1) --------------------


class PageImage(Contract):
    page: PageNo
    path: str  # relative to the store root
    width_px: Annotated[int, Field(gt=0)]
    height_px: Annotated[int, Field(gt=0)]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    text_layer_chars: Annotated[int, Field(ge=0)]  # embedded PDF text; 0 for scans (text-layer probe, §17)


class PageManifest(Contract):
    """Rasterised pages for one source PDF at one render config. Cached by (source hash, config)."""

    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    dpi: Annotated[int, Field(ge=300)]
    grayscale: bool
    page_count: Annotated[int, Field(ge=1)]
    pages: list[PageImage]

    @model_validator(mode="after")
    def _complete(self) -> "PageManifest":
        if [p.page for p in self.pages] != list(range(1, self.page_count + 1)):
            raise ValueError("manifest must list every page 1..page_count in order")
        return self


class StageRecord(Contract):
    stage: str
    status: Literal["ok", "failed", "skipped"]
    started_at: datetime
    finished_at: datetime
    detail: str | None = None


class RunRecord(Contract):
    """One pipeline run over one input. Re-runs are new records; nothing is overwritten (spec §6 Stage 9)."""

    run_id: str
    document_id: str
    version_label: str
    effective_date: date | None
    input_variant: Literal["clean", "degraded"] = "clean"
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_filename: str
    page_count: Annotated[int, Field(ge=1)]
    manifest_path: str  # relative to the store root
    dictionary_version: Annotated[int, Field(ge=1)]
    pipeline_version: str
    created_at: datetime
    stages: list[StageRecord] = []


# --- VLM output envelope (spec §8) -------------------------------------------
# What the model returns per page/tile. Fixed shape, open field names. Kept separate from
# the document contracts above: the envelope is raw model output, the pipeline turns it into
# grounded, normalised ExtractedField / Table / Clause objects.


class EnvelopeBlock(Contract):
    kind: Literal["heading", "clause", "other"]
    number: str | None = None
    heading: str | None = None
    text: str
    continues_from_previous: bool = False
    continues_on_next: bool = False


class EnvelopeField(Contract):
    name: str
    raw_value: str
    source_text: str
    confidence: float
    block_index: int | None = None


class EnvelopeCell(Contract):
    row: int
    col: int
    row_span: int = 1
    col_span: int = 1
    is_header: bool = False
    raw_value: str | None = None


class EnvelopeTable(Contract):
    caption: str | None = None
    continues_from_previous: bool = False
    continues_on_next: bool = False
    cells: list[EnvelopeCell]


class EnvelopeParty(Contract):
    name: str
    role: str
    source_text: str


class PageEnvelope(Contract):
    blocks: list[EnvelopeBlock]
    fields: list[EnvelopeField]
    tables: list[EnvelopeTable]
    parties: list[EnvelopeParty]
