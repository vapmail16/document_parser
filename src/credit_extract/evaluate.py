"""Evaluation against the golden set (spec §13). No model involved.

What can be scored before field-name reconciliation (milestone 9) exists:
  value recall     — was the expected value extracted on its page (±1), by any field or table cell?
  cell accuracy    — for pricing-grid rows, is the value in the right cell (level row x rate column)?
  auto-accept rate — of the values found, how many passed grounding and were auto-accepted?
Label correctness for non-table fields ("is this value attached to the right field name?") and
precision on golden keys need the canonical mapping, and are reported as pending until then.

Only human-verified golden rows count, unless include_unverified is set — then the report is
marked PROVISIONAL and must not be quoted.
"""

import csv
import math
import re
from collections import Counter
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from .contracts import DocumentExtraction, GroundingStatus, Table
from .grounding import value_in
from .normalise import normalise, same_value, text_key

VERIFIED = {"ok", "corrected"}
# Verification by a model is a cross-check, not ground truth: when the system under test is also a
# model, shared mistakes score as correct. Such rows count only when explicitly allowed.
AI_VERIFIERS = ("claude", "gpt", "chatgpt", "gemini", "llm", "ai", "model")


def is_ai_verifier(name: str) -> bool:
    n = name.strip().casefold()
    return any(n == a or n.startswith(a + " ") or a in n.split() for a in AI_VERIFIERS) or "claude" in n

# Pricing-grid rows: instance "level{N}|{rate}" -> header words identifying the column.
_GRID_COLUMNS = {
    "term_sofr": "term sofr loans", "base_rate": "base rate loans",
    "letter_of_credit_fee": "letter of credit fee", "facility_fee": "facility fee",
}


class GoldenRow(BaseModel):
    document_id: str
    version_label: str
    canonical_name: str
    instance_key: str
    expected_value: str
    source_page: int
    source_quote: str
    verified_by: str
    verification: str
    notes: str

    @property
    def key(self) -> str:
        return f"{self.canonical_name}|{self.instance_key}"


def verification_tier(row: GoldenRow) -> str:
    if row.verification.strip().casefold() not in VERIFIED:
        return "unverified"
    return "ai" if is_ai_verifier(row.verified_by) else "human"


def load_golden(path: Path, include_unverified: bool = False, allow_ai_verified: bool = False) -> list[GoldenRow]:
    with path.open(newline="") as f:
        rows = [GoldenRow(**{k: (r.get(k) or "") for k in GoldenRow.model_fields if k != "source_page"},
                          source_page=int(r["source_page"])) for r in csv.DictReader(f)]
    rows = [r for r in rows if r.verification.strip().casefold() != "wrong"]
    allowed = {"human"} | ({"ai"} if allow_ai_verified or include_unverified else set()) | \
        ({"unverified"} if include_unverified else set())
    return [r for r in rows if verification_tier(r) in allowed]


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4))


class RowResult(BaseModel):
    key: str
    page: int
    expected: str
    # found: captured as structured data (field, table cell or party)
    # text_only: transcribed in clause text but not captured as a key-value — counts as missed
    status: Literal["found", "text_only", "missed", "page_not_extracted", "absent_pending"]
    matched: str | None = None  # field id / table cell that carried the value
    matched_name: str | None = None
    cell_check: Literal["right_cell", "wrong_cell", "not_a_grid_row", "no_table"] = "not_a_grid_row"
    auto_accepted: bool | None = None
    # Value found AND carried under the right dictionary key + instance (needs reconciliation).
    # Grid rows also count when the value sits in the right table cell.
    label_ok: bool | None = None
    labelled_by: str | None = None


class EvalReport(BaseModel):
    run_id: str
    model_key: str
    prompt_version: str
    input_variant: str
    golden_file: str
    provisional: bool  # True when unverified rows were used — do not quote
    verification_tiers: dict[str, int]  # rows used, by who verified them: human / ai / unverified
    rows_scored: int
    rows_not_extracted: int
    rows_absent_pending: int
    value_recall: float
    value_recall_ci95: tuple[float, float]
    grid_cells: int
    grid_cell_accuracy: float | None
    grid_cell_accuracy_ci95: tuple[float, float] | None
    found_auto_accepted: float | None  # share of found values that grounding auto-accepted
    text_only: int  # read and transcribed, but not captured as key-value data
    reconciled: bool  # extraction carries dictionary mappings (label metrics available)
    labelled_recall: float | None  # golden values found under the right key+instance
    labelled_recall_ci95: tuple[float, float] | None
    precision_on_golden_keys: float | None  # of fields mapped to a golden key, share with the right value
    precision_on_golden_keys_ci95: tuple[float, float] | None
    precision_fields: int  # denominator of the above
    wrong_values_on_golden_keys: list[str]
    # The number governance needs: of mapped fields the pipeline AUTO-ACCEPTED, how many are right.
    auto_accepted_precision: float | None
    auto_accepted_precision_ci95: tuple[float, float] | None
    auto_accepted_fields: int
    wrong_auto_accepted: list[str]
    # Share of extracted fields that grounding could not confirm. An UPPER BOUND on hallucination:
    # on noisy scans most of these are correct values OCR failed to confirm (see grounding --truth).
    unverified_rate: float
    pending: list[str]
    results: list[RowResult]


def _grid_cell(tables: list[Table], row: GoldenRow) -> tuple[str, str | None]:
    """Check a grid row's value sits at (level row, rate column). Returns (check, cell value)."""
    level, _, rate = row.instance_key.partition("|")
    rate = rate or ("facility_fee" if row.canonical_name == "commitment_fee_rate" else "")
    header_words = _GRID_COLUMNS.get(rate)
    if not level.startswith("level") or header_words is None:
        return "not_a_grid_row", None
    candidates = [t for t in tables if row.source_page in t.pages]
    if not candidates:
        return "no_table", None
    for t in candidates:
        header = {c.col: text_key(c.raw_value) for c in t.cells if c.row == 0}
        cols = [col for col, h in header.items() if header_words in h]
        # "Applicable Rate for Base Rate Loans" must not match the Term SOFR column, and vice versa.
        cols = [c for c in cols if not (rate == "term_sofr" and "base rate" in header[c])]
        level_rows = [c.row for c in t.cells if c.col == 0 and text_key(c.raw_value) == level[len("level"):]]
        if not cols or not level_rows:
            continue
        cell = next((c for c in t.cells if c.row == level_rows[0] and c.col == cols[0]), None)
        if cell is None:
            return "wrong_cell", None
        expected = normalise(row.expected_value)
        got = normalise(cell.raw_value)
        ok = expected is not None and got is not None and same_value(expected, got)
        return ("right_cell" if ok else "wrong_cell"), cell.raw_value
    return "no_table", None


def _structured_elsewhere(doc: DocumentExtraction, tables: list[Table], expected, pages: set[int]) -> str | None:
    """A value captured as a party or a table cell (e.g. a rate type in a grid header)."""
    if expected is None:
        return None
    for party in doc.parties:
        if party.source.page in pages and value_in(expected, party.name):
            return f"party {party.name} ({party.role})"
    for t in tables:
        if set(t.pages) & pages:
            for c in t.cells:
                if c.raw_value and value_in(expected, c.raw_value):
                    return f"table {t.table_id} cell ({c.row},{c.col})"
    return None


def _ikey(k: str | None) -> str:
    """Instance keys compared in slug form: 'bank_of_america__n_a' == 'bank_of_america_n_a'."""
    return "|".join(re.sub(r"_+", "_", p).strip("_") for p in (k or "").casefold().split("|"))


def _key_matches(g: GoldenRow, canonical: str | None, instance: str | None) -> bool:
    return canonical == g.canonical_name and (not g.instance_key or _ikey(instance) == _ikey(g.instance_key))


def _labelled(doc: DocumentExtraction, g: GoldenRow) -> tuple[bool, str | None]:
    expected = normalise(g.expected_value)
    if expected is None:
        return False, None
    pages = {g.source_page - 1, g.source_page, g.source_page + 1}
    for f in doc.flat_fields:
        if f.source.page in pages and _key_matches(g, f.canonical_name, f.instance_key) and f.raw_value and (
                same_value(expected, f.value or normalise(f.raw_value)) or value_in(expected, f.raw_value)
                or (f.value is not None and f.value.text and value_in(expected, f.value.text))):
            return True, f"field {f.field_id} {f.name!r}"
    for p in doc.parties:
        if g.canonical_name in p.canonical_roles and p.source.page in pages and value_in(expected, p.name):
            return True, f"party {p.name} ({p.role})"
    return False, None


def _precision(doc: DocumentExtraction, golden: list[GoldenRow], pages: set[int],
               auto_only: bool = False) -> tuple[int, list[str]]:
    """Fields mapped to a golden (canonical, instance) key on extracted pages: is the value right?
    auto_only: only fields the pipeline auto-accepted — the ones no human looks at."""
    ok, bad = 0, []
    for f in doc.flat_fields:
        if f.source.page not in pages or not f.canonical_name:
            continue
        if auto_only and f.review_status != "auto_accepted":
            continue
        rows = [g for g in golden if g.expected_value.strip() and _key_matches(g, f.canonical_name, f.instance_key)]
        if not rows:
            continue
        v = f.value or normalise(f.raw_value)
        if any(v is not None and (same_value(normalise(g.expected_value), v) or
                                  value_in(normalise(g.expected_value), f.raw_value or "") or
                                  (v.text is not None and value_in(normalise(g.expected_value), v.text))) for g in rows):
            ok += 1
        else:
            bad.append(f"{f.field_id} {f.name!r} = {f.raw_value!r} mapped to "
                       f"{f.canonical_name}|{f.instance_key}; golden {[g.expected_value for g in rows][:3]}")
    return ok, bad


def evaluate(doc: DocumentExtraction, golden: list[GoldenRow], golden_file: str, provisional: bool,
             input_variant: str) -> EvalReport:
    extracted_pages = {c.page for c in doc.page_coverage if c.status in ("ok", "low_coverage", "blank")}
    tables = [t for s in doc.sections for c in s.clauses for t in c.tables]
    results: list[RowResult] = []
    for g in golden:
        if g.document_id != doc.document_id or g.version_label != doc.version_label:
            continue
        base = dict(key=g.key, page=g.source_page, expected=g.expected_value)
        if not g.expected_value.strip():
            results.append(RowResult(status="absent_pending", **base))
            continue
        if g.source_page not in extracted_pages:
            results.append(RowResult(status="page_not_extracted", **base))
            continue
        expected = normalise(g.expected_value)
        pages = {g.source_page - 1, g.source_page, g.source_page + 1}
        match = None
        for f in doc.flat_fields:
            if f.source.page in pages and f.raw_value and expected is not None and (
                    same_value(expected, f.value or normalise(f.raw_value)) or value_in(expected, f.raw_value)):
                match = f
                break
        cell_check, cell_value = _grid_cell(tables, g)
        if cell_check in ("right_cell", "wrong_cell"):
            # Grid values repeat (0.910% is both a margin and an L/C fee), so a value match anywhere
            # proves nothing: grid rows are scored only by position in the extracted table.
            results.append(RowResult(status="found" if cell_check == "right_cell" else "missed",
                                     matched=f"table cell {cell_value}", cell_check=cell_check, **base))
        elif match is None:
            where = _structured_elsewhere(doc, tables, expected, pages)
            if where:
                results.append(RowResult(status="found", matched=where, cell_check=cell_check, **base))
            elif any(p in pages and value_in(expected, c.text or "")
                     for s in doc.sections for c in s.clauses for p in range(c.page_range[0], c.page_range[1] + 1)):
                results.append(RowResult(status="text_only", cell_check=cell_check, **base))
            else:
                results.append(RowResult(status="missed", cell_check=cell_check, **base))
        else:
            results.append(RowResult(status="found", matched=match.field_id, matched_name=match.name,
                                     cell_check=cell_check, auto_accepted=match.review_status == "auto_accepted",
                                     **base))

    reconciled = any(f.match_method != "none" for f in doc.flat_fields) or any(p.canonical_roles for p in doc.parties)
    by_key = {g.key: g for g in golden if g.document_id == doc.document_id and g.version_label == doc.version_label}
    for r in results:
        if r.status in ("page_not_extracted", "absent_pending"):
            continue
        g = by_key[r.key]
        r.label_ok, r.labelled_by = _labelled(doc, g) if reconciled else (None, None)
        if r.label_ok is False and r.cell_check == "right_cell":
            r.label_ok, r.labelled_by = True, "table cell position"

    scored = [r for r in results if r.status in ("found", "text_only", "missed")]
    found = [r for r in scored if r.status == "found"]
    grid = [r for r in scored if r.cell_check in ("right_cell", "wrong_cell", "no_table")]
    grid_ok = [r for r in grid if r.cell_check == "right_cell"]
    accepted = [r for r in found if r.auto_accepted is not None]
    on_pages = [f for f in doc.flat_fields if f.source.page in extracted_pages]
    labelled = [r for r in scored if r.label_ok is not None]
    prec_ok, prec_bad = _precision(doc, list(by_key.values()), extracted_pages) if reconciled else (0, [])
    prec_n = prec_ok + len(prec_bad)
    auto_ok, auto_bad = _precision(doc, list(by_key.values()), extracted_pages, auto_only=True) if reconciled else (0, [])
    auto_n = auto_ok + len(auto_bad)
    return EvalReport(
        auto_accepted_precision=round(auto_ok / auto_n, 4) if auto_n else None,
        auto_accepted_precision_ci95=wilson(auto_ok, auto_n) if auto_n else None,
        auto_accepted_fields=auto_n, wrong_auto_accepted=auto_bad,
        reconciled=reconciled,
        labelled_recall=round(sum(r.label_ok for r in labelled) / len(labelled), 4) if labelled else None,
        labelled_recall_ci95=wilson(sum(r.label_ok for r in labelled), len(labelled)) if labelled else None,
        precision_on_golden_keys=round(prec_ok / prec_n, 4) if prec_n else None,
        precision_on_golden_keys_ci95=wilson(prec_ok, prec_n) if prec_n else None,
        precision_fields=prec_n, wrong_values_on_golden_keys=prec_bad,
        run_id=doc.provenance.run_id, model_key=doc.provenance.model_id,
        prompt_version=doc.provenance.prompt_versions.get("pass1_extraction", "?"), input_variant=input_variant,
        golden_file=golden_file, provisional=provisional,
        verification_tiers=dict(Counter(verification_tier(g) for g in golden)), rows_scored=len(scored),
        rows_not_extracted=sum(r.status == "page_not_extracted" for r in results),
        rows_absent_pending=sum(r.status == "absent_pending" for r in results),
        value_recall=round(len(found) / len(scored), 4) if scored else 0.0,
        value_recall_ci95=wilson(len(found), len(scored)),
        grid_cells=len(grid),
        grid_cell_accuracy=round(len(grid_ok) / len(grid), 4) if grid else None,
        grid_cell_accuracy_ci95=wilson(len(grid_ok), len(grid)) if grid else None,
        found_auto_accepted=round(sum(r.auto_accepted for r in accepted) / len(accepted), 4) if accepted else None,
        text_only=sum(r.status == "text_only" for r in results),
        unverified_rate=round(sum(f.source.grounding is GroundingStatus.not_found for f in on_pages)
                                 / len(on_pages), 4) if on_pages else 0.0,
        pending=([] if reconciled else ["label metrics: run `cae reconcile` first"]) +
                ["absent-field checks (asserting a field is NOT in the document)"],
        results=results,
    )
