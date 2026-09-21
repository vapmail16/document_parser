"""Human-review loop (spec §14). No model involved.

export  -> a CSV of everything routed to review (fields, failed/low-coverage pages, failed table checks)
ingest  -> the reviewer's decisions become ReviewRecords (audit), a NEW reviewed revision of the
           document (never overwritten), dictionary alias proposals from confirmed mappings, and
           golden-set rows from confirmed/corrected values (verified_by = the reviewer).

Decisions: confirm | correct | reject. A correction may change the value and/or the key.
"""

import csv
import hashlib
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel

from .contracts import DocumentExtraction, ExtractedField, ReviewRecord
from .evaluate import is_ai_verifier
from .normalise import normalise

COLUMNS = ["item_id", "kind", "page", "name", "canonical_name", "instance_key", "value", "source_quote",
           "reasons", "decision", "corrected_value", "corrected_canonical_name", "corrected_instance_key",
           "reviewer", "notes"]
DECISIONS = {"confirm": "confirmed", "correct": "corrected", "reject": "rejected"}


def export_rows(doc: DocumentExtraction) -> list[dict[str, str]]:
    rows = []
    for f in doc.flat_fields:
        if f.review_status != "needs_review":
            continue
        rows.append(dict(item_id=f.field_id, kind="field", page=str(f.source.page), name=f.name,
                         canonical_name=f.canonical_name or "", instance_key=f.instance_key or "",
                         value=f.raw_value or "", source_quote=f.source.source_text,
                         reasons="; ".join(f.review_reasons) or "needs review"))
    for c in doc.page_coverage:
        if c.status in ("failed", "low_coverage"):
            rows.append(dict(item_id=f"page:{c.page}", kind="page", page=str(c.page),
                             reasons=f"page {c.status} (coverage {c.covered_token_ratio:.0%}, {c.attempts} attempts)"))
    for s in doc.sections:
        for cl in s.clauses:
            for t in cl.tables:
                for ch in t.checks:
                    if not ch.passed:
                        rows.append(dict(item_id=f"{t.table_id}:{ch.name}", kind="table_check", page=str(t.pages[0]),
                                         name=t.caption or t.table_id, reasons=f"{ch.name}: {ch.detail}"))
    return [{k: r.get(k, "") for k in COLUMNS} for r in rows]


def write_export(doc: DocumentExtraction, path: Path) -> int:
    rows = export_rows(doc)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    return len(rows)


class AliasProposal(BaseModel):
    canonical_name: str
    surface_form: str
    approved_by: str  # the reviewer who confirmed or corrected the mapping
    source: str  # review batch id


class IngestResult(BaseModel):
    batch_id: str
    reviewer_is_ai: bool
    records: list[ReviewRecord]
    alias_proposals: list[AliasProposal]
    golden_rows: list[dict[str, str]]
    skipped: list[str]  # rows without a decision, or referring to unknown items
    errors: list[str]


def ingest(doc: DocumentExtraction, path: Path, now: datetime | None = None) -> tuple[DocumentExtraction, IngestResult]:
    now = now or datetime.now(timezone.utc)
    rows = list(csv.DictReader(path.open(newline="")))
    batch = f"{now:%Y%m%dT%H%M%SZ}-{hashlib.sha256(path.read_bytes()).hexdigest()[:6]}"
    by_id = {f.field_id: f for f in doc.flat_fields}
    records, aliases, golden, skipped, errors = [], [], [], [], []
    reviewers = set()
    for i, r in enumerate(rows, start=2):  # line numbers as a spreadsheet shows them
        decision = (r.get("decision") or "").strip().casefold()
        if not decision:
            skipped.append(f"line {i}: no decision")
            continue
        if decision not in DECISIONS:
            errors.append(f"line {i}: decision must be confirm, correct or reject, got {decision!r}")
            continue
        reviewer = (r.get("reviewer") or "").strip()
        if not reviewer:
            errors.append(f"line {i}: reviewer is required")
            continue
        reviewers.add(reviewer)
        item = r["item_id"]
        f = by_id.get(item)
        if r.get("kind") == "field" and f is None:
            skipped.append(f"line {i}: unknown field {item}")
            continue
        new_value = (r.get("corrected_value") or "").strip() or None
        new_key = (r.get("corrected_canonical_name") or "").strip() or None
        new_inst = (r.get("corrected_instance_key") or "").strip() or None
        if decision == "correct" and f is not None and not (new_value or new_key or new_inst):
            errors.append(f"line {i}: 'correct' needs a corrected value, key or instance")
            continue
        records.append(ReviewRecord(
            review_id=f"{batch}-{len(records) + 1:04d}", document_id=doc.document_id, version_label=doc.version_label,
            target_kind="field" if f is not None else ("page" if item.startswith("page:") else "table_cell"),
            target_id=item, reason_flagged=r.get("reasons") or "", decision=DECISIONS[decision],
            old_value=(f.raw_value if f else None),
            new_value=(new_value if decision == "correct" else None), reviewer=reviewer, reviewed_at=now,
            notes=(r.get("notes") or None)))
        if f is None:
            continue
        by_id[item] = _apply(f, decision, new_value, new_key, new_inst)
        kept = by_id[item]
        # A human-confirmed fuzzy mapping, or a human-chosen key, is evidence for an alias.
        if decision in ("confirm", "correct") and kept.canonical_name and (f.match_method == "fuzzy" or new_key):
            aliases.append(AliasProposal(canonical_name=kept.canonical_name or "", surface_form=f.name,
                                         approved_by=reviewer, source=batch))
        if decision in ("confirm", "correct") and kept.canonical_name:
            golden.append(dict(document_id=doc.document_id, version_label=doc.version_label,
                               canonical_name=kept.canonical_name, instance_key=kept.instance_key or "",
                               expected_value=kept.raw_value or "", source_page=str(kept.source.page),
                               source_quote=kept.source.source_text, drafted_by=f"review {batch}",
                               verified_by=reviewer, verification="corrected" if decision == "correct" else "ok",
                               notes=r.get("notes") or ""))
    reviewed = doc.model_copy(update={"flat_fields": [by_id[f.field_id] for f in doc.flat_fields]})
    return reviewed, IngestResult(batch_id=batch, reviewer_is_ai=any(is_ai_verifier(x) for x in reviewers),
                                  records=records, alias_proposals=[a for a in aliases if a.canonical_name],
                                  golden_rows=golden, skipped=skipped, errors=errors)


def _apply(f: ExtractedField, decision: str, value: str | None, key: str | None, inst: str | None) -> ExtractedField:
    if decision == "confirm":
        return f.model_copy(update={"review_status": "confirmed"})
    if decision == "reject":
        # Never deleted: kept, marked rejected, so the audit trail and downstream consumers see it.
        return f.model_copy(update={"review_status": "rejected"})
    update = {"review_status": "corrected", "origin": "human"}
    if value is not None:
        update |= {"raw_value": value, "value": normalise(value)}
    if key is not None:
        update |= {"canonical_name": key, "match_method": "exact"}
    if inst is not None:
        update |= {"instance_key": inst}
    return f.model_copy(update=update)

