"""Stage 10 — structured version diff (spec §11). No model involved.

Clauses are aligned by CONTENT (heading, body, defined terms) with a one-to-one assignment, never by
clause number — restatements renumber. Fields are compared by (dictionary key, instance) on
normalised values, tables by row/column header labels. Differences between the two extractions'
model, prompts or dictionary are reported as comparability warnings: some "changes" may be drift.
"""

import re

import numpy as np
from rapidfuzz import fuzz
from scipy.optimize import linear_sum_assignment

from .contracts import (CellChange, Clause, ClauseAlignment, DocumentExtraction, ExtractedField, FieldChange,
                        GroundingStatus, Table, TableChange, VersionDiff)
from .normalise import normalise, same_value, text_key

W_HEADING, W_BODY, W_TERMS = 0.3, 0.5, 0.2
ALIGN_FLOOR = 0.55  # below: not the same clause (added / removed)
ALIGN_CONFIDENT = 0.75  # between floor and this: aligned, but flagged low-confidence
TEXT_SAME = 97  # rapidfuzz ratio at or above which clause text counts as unchanged
LOW_CONFIDENCE = 0.85

_TERM = re.compile(r"[“\"]([A-Z][^”\"]{1,60})[”\"]")
_NUMBERS = re.compile(r"\d+(?:[.,]\d+)*")
_FOOTNOTE = re.compile(r"[\u00b9\u00b2\u00b3\u2070-\u2079*†‡]+|(?<=[a-z])\d{1,2}$")


def _label(s: str | None) -> str:
    """Header/row label for cell alignment: footnote markers dropped ("Pro Rata Share¹" == "Pro Rata Share1")."""
    return _FOOTNOTE.sub("", text_key(s)).strip()


def _clauses(doc: DocumentExtraction) -> list[Clause]:
    return [c for s in doc.sections for c in s.clauses if len(text_key(c.text)) >= 20]


def _score(a: Clause, b: Clause) -> float:
    ha, hb = text_key(a.heading or (a.text or "")[:80]), text_key(b.heading or (b.text or "")[:80])
    heading = fuzz.ratio(ha, hb) / 100
    body = fuzz.token_set_ratio(text_key(a.text)[:3000], text_key(b.text)[:3000]) / 100
    ta, tb = set(_TERM.findall(a.text or "")), set(_TERM.findall(b.text or ""))
    terms = len(ta & tb) / len(ta | tb) if ta | tb else body
    return W_HEADING * heading + W_BODY * body + W_TERMS * terms


def align_clauses(a: list[Clause], b: list[Clause]) -> list[ClauseAlignment]:
    if not a or not b:
        return [ClauseAlignment(from_clause_id=c.clause_id, to_clause_id=None, status="removed", score=None) for c in a] + \
            [ClauseAlignment(from_clause_id=None, to_clause_id=c.clause_id, status="added", score=None) for c in b]
    scores = np.array([[_score(x, y) for y in b] for x in a])
    rows, cols = linear_sum_assignment(-scores)
    out, used_a, used_b = [], set(), set()
    for i, j in zip(rows, cols):
        s = float(scores[i, j])
        if s < ALIGN_FLOOR:
            continue
        used_a.add(i)
        used_b.add(j)
        ta, tb = text_key(a[i].text), text_key(b[j].text)
        # Wording may drift slightly between extractions; numbers may not: one changed figure
        # ("3.50" -> "4.00") is a changed clause however similar the rest is.
        changed = fuzz.ratio(ta, tb) < TEXT_SAME or _NUMBERS.findall(ta) != _NUMBERS.findall(tb)
        out.append(ClauseAlignment(from_clause_id=a[i].clause_id, to_clause_id=b[j].clause_id, status="matched",
                                   score=round(s, 3), text_changed=changed, low_confidence_alignment=s < ALIGN_CONFIDENT))
    out += [ClauseAlignment(from_clause_id=a[i].clause_id, to_clause_id=None, status="removed", score=None)
            for i in range(len(a)) if i not in used_a]
    out += [ClauseAlignment(from_clause_id=None, to_clause_id=b[j].clause_id, status="added", score=None)
            for j in range(len(b)) if j not in used_b]
    return out


def _keyed(doc: DocumentExtraction) -> dict[tuple[str, str | None], list[ExtractedField]]:
    out: dict[tuple[str, str | None], list[ExtractedField]] = {}
    for f in doc.flat_fields:
        if f.canonical_name and f.review_status != "rejected" and (f.value or normalise(f.raw_value)) is not None:
            out.setdefault((f.canonical_name, f.instance_key), []).append(f)
    return out


def _conf(fs: list[ExtractedField]) -> float:
    best = max(fs, key=lambda f: (f.review_status in ("confirmed", "corrected"), f.routing_score or 0))
    if best.review_status in ("confirmed", "corrected"):
        return 1.0
    return best.routing_score if best.routing_score is not None else 0.5


def _field_changes(a: DocumentExtraction, b: DocumentExtraction) -> list[FieldChange]:
    ka, kb = _keyed(a), _keyed(b)
    changes = []
    for key in sorted(set(ka) | set(kb), key=lambda k: (k[0], k[1] or "")):
        fa, fb = ka.get(key, []), kb.get(key, [])
        va = [f.value or normalise(f.raw_value) for f in fa]
        vb = [f.value or normalise(f.raw_value) for f in fb]
        if fa and fb:
            common = next(((x, y) for x in range(len(fa)) for y in range(len(fb)) if same_value(va[x], vb[y])), None)
            kind = "unchanged" if common else "modified"
            ia, ib = common or (0, 0)
        else:
            kind, ia, ib = ("removed", 0, None) if fa else ("added", None, 0)
        old = fa[ia] if ia is not None else None
        new = fb[ib] if ib is not None else None
        conf = min(_conf(fa) if fa else 1.0, _conf(fb) if fb else 1.0)
        unverified = any(f.source.grounding is not GroundingStatus.verified for f in (old, new) if f)
        changes.append(FieldChange(
            canonical_name=key[0], instance_key=key[1],
            from_clause_id=old.clause_id if old else None, to_clause_id=new.clause_id if new else None,
            old_value=va[ia] if ia is not None else None, new_value=vb[ib] if ib is not None else None,
            old_source=old.source if old else None, new_source=new.source if new else None,
            change_type=kind, confidence=round(conf, 3),
            low_confidence_change=kind != "unchanged" and (conf < LOW_CONFIDENCE or unverified)))
    return changes


def _tables(doc: DocumentExtraction) -> list[Table]:
    return [t for s in doc.sections for c in s.clauses for t in c.tables]


def _cells_by_label(t: Table) -> dict[tuple[str, str], str | None]:
    header = {c.col: _label(c.raw_value) for c in t.cells if c.row == 0}
    labels = {c.row: _label(c.raw_value) for c in t.cells if c.col == 0 and c.row > 0}
    return {(labels.get(c.row, str(c.row)), header.get(c.col, str(c.col))): c.raw_value
            for c in t.cells if c.row > 0 and c.col > 0}


def _table_changes(a: DocumentExtraction, b: DocumentExtraction) -> list[TableChange]:
    ta, tb = _tables(a), _tables(b)
    head = lambda t: " ".join(text_key(c.raw_value) for c in t.cells if c.row == 0)
    out, used = [], set()
    for x in ta:
        best = max(((fuzz.token_set_ratio(head(x), head(y)), j) for j, y in enumerate(tb) if j not in used), default=None)
        if best is None or best[0] < 80:
            out.append(TableChange(from_table_id=x.table_id, to_table_id=None, cell_changes=[]))
            continue
        used.add(best[1])
        y = tb[best[1]]
        ca, cb = _cells_by_label(x), _cells_by_label(y)
        cells = []
        for k in sorted(set(ca) | set(cb)):
            old, new = ca.get(k), cb.get(k)
            vo, vn = normalise(old), normalise(new)
            if old is not None and new is not None and vo is not None and vn is not None and same_value(vo, vn):
                continue
            cells.append(CellChange(row_header=k[0], col_header=k[1], old_value=vo, new_value=vn,
                                    change_type="modified" if old and new else "removed" if old else "added"))
        if cells:
            out.append(TableChange(from_table_id=x.table_id, to_table_id=y.table_id, cell_changes=cells))
    out += [TableChange(from_table_id=None, to_table_id=y.table_id, cell_changes=[])
            for j, y in enumerate(tb) if j not in used]
    return out


def comparability(a: DocumentExtraction, b: DocumentExtraction) -> list[str]:
    pa, pb = a.provenance, b.provenance
    warnings = []
    for label, x, y in [("model", pa.model_id, pb.model_id), ("prompt versions", pa.prompt_versions, pb.prompt_versions),
                        ("dictionary version", pa.dictionary_version, pb.dictionary_version),
                        ("OCR engine", pa.ocr_engine, pb.ocr_engine), ("pipeline version", pa.pipeline_version, pb.pipeline_version)]:
        if x != y:
            warnings.append(f"different {label}: {x} vs {y} — some changes may be extraction drift")
    pages_a = {c.page for c in a.page_coverage if c.status == "ok"}
    pages_b = {c.page for c in b.page_coverage if c.status == "ok"}
    if pages_a != pages_b and (len(pages_a) < pa.page_count or len(pages_b) < pb.page_count):
        warnings.append(f"different extracted pages ({len(pages_a)} vs {len(pages_b)}) — added/removed may just be coverage")
    return warnings


def diff(a: DocumentExtraction, b: DocumentExtraction) -> VersionDiff:
    return VersionDiff(
        document_id=a.document_id, from_version=a.version_label, to_version=b.version_label,
        from_provenance=a.provenance, to_provenance=b.provenance, comparability_warnings=comparability(a, b),
        clause_alignments=align_clauses(_clauses(a), _clauses(b)),
        changes=[c for c in _field_changes(a, b)], table_changes=_table_changes(a, b))
