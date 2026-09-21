"""Stage 8 — grounding & confidence routing (spec §6, §15). No model involved.

A field is `verified` only if BOTH hold:
  1. quote check — the model's verbatim `source_text` is found in an independent witness of the
     cited page (the trusted PDF text layer if the probe trusts it, else OCR), within tolerance;
  2. value check — the normalised value is derivable from the quote AND from the witness text
     it matched. The second half matters: a quote can match at 0.95 while one digit differs.
Anything else is `not_found` and routed to review, regardless of model confidence.
"""

import re
from collections import Counter
from decimal import Decimal
from dataclasses import dataclass
from difflib import SequenceMatcher

from pydantic import BaseModel

from .contracts import (DocumentExtraction, ExtractedField, GroundingStatus, NormalisedValue, SourceRef,
                        TableCheck, ValueType)
from .normalise import find_values, normalise, same_value, text_key
from .ocr import OcrPage

QUOTE_MIN_SCORE = 0.82  # char similarity of quote vs witness window; tolerates OCR noise
BAG_MIN_RECALL = 0.9  # out-of-order fallback: share of quote words found within a 3n-word window
NOISE_BELOW = 0.95  # verified, but the witness disagreed in places -> vision_noise_flag
TEXT_VALUE_RECALL = 0.9  # share of a text value's words that must appear in the quote
AUTO_ACCEPT_CONFIDENCE = 0.85

_WORD = re.compile(r"[^\s]+")


def _words(s: str) -> list[str]:
    return [w.strip(".,;:()[]\"'") for w in _WORD.findall(text_key(s))] or []


@dataclass
class Witness:
    """Words of a page from one source, optionally with OCR bboxes per word."""
    kind: str  # "ocr" | "text_layer"
    words: list[str]
    bboxes: list[tuple[float, float, float, float]] | None = None

    @classmethod
    def from_ocr(cls, page: OcrPage, min_conf: float = 0.3) -> "Witness":
        toks = [t for t in page.tokens if t.conf >= min_conf]
        pairs = [(w, t.bbox) for t in toks for w in _words(t.text)[:1] if w]
        return cls("ocr", [w for w, _ in pairs], [b for _, b in pairs])

    @classmethod
    def from_text(cls, text: str) -> "Witness":
        return cls("text_layer", [w for w in _words(text) if w])


@dataclass
class Match:
    score: float
    start: int
    end: int
    witness: Witness
    mode: str = "sequence"  # "bag": words found out of order within a short window (wrapped table rows)

    @property
    def text(self) -> str:
        return " ".join(self.witness.words[self.start:self.end])

    def padded_text(self, pad: int = 3) -> str:
        """The match plus a few words either side — best windows can stop one noisy word short."""
        return " ".join(self.witness.words[max(0, self.start - pad):self.end + pad])

    @property
    def bbox(self) -> tuple[float, float, float, float] | None:
        if not self.witness.bboxes or self.end <= self.start:
            return None
        bs = self.witness.bboxes[self.start:self.end]
        return (min(b[0] for b in bs), min(b[1] for b in bs), max(b[2] for b in bs), max(b[3] for b in bs))


def locate(quote: str, witness: Witness, top_k: int = 12) -> Match | None:
    """Best fuzzy window for `quote` in the witness words. Word-overlap prefilter, then char ratio."""
    q = [w for w in _words(quote) if w]
    words = witness.words
    if not q or not words:
        return None
    n = len(q)
    qset = Counter(q)
    joined_q = " ".join(q)

    # Sliding word-overlap count for windows of length n.
    def overlap(start: int) -> int:
        return sum((Counter(words[start:start + n]) & qset).values())

    def ratio(s: int, length: int) -> Match:
        window = words[s:s + length]
        return Match(SequenceMatcher(None, joined_q, " ".join(window), autojunk=False).ratio(),
                     s, s + len(window), witness)

    starts = range(max(1, len(words) - n + 1))
    coarse = sorted((ratio(s, n) for s in sorted(starts, key=overlap, reverse=True)[:top_k]),
                    key=lambda m: m.score, reverse=True)[:3]
    best = coarse[0]
    slack = max(2, n // 5)
    for c in coarse:  # refine start and length around the best coarse windows
        for s2 in range(max(0, c.start - slack), min(len(words) - 1, c.start + slack) + 1):
            for length in range(max(1, n - slack), n + slack + 1):
                m = ratio(s2, length)
                if m.score > best.score:
                    best = m
    if best.score < QUOTE_MIN_SCORE and n >= 3:
        # Table rows whose label wraps over several lines are read by OCR in a different order.
        # Accept the quote if (nearly) all its words occur within a window of 3n words.
        size = min(len(words), 3 * n)
        for s in range(max(1, len(words) - size + 1)):
            recall = sum((Counter(words[s:s + size]) & qset).values()) / n
            if recall > best.score and recall >= BAG_MIN_RECALL:
                best = Match(recall, s, s + size, witness, mode="bag")
    return best


def value_in(value: NormalisedValue, text: str, fuzzy_letters: bool = False) -> bool:
    """Is `value` derivable from `text`?

    Words carrying digits must always match exactly — that is where a wrong value hides.
    With fuzzy_letters (witness is OCR), purely alphabetic words may match approximately.
    """
    if value.type is ValueType.text:
        want, have = _words(value.text or ""), _words(text)
        if not want:
            return False
        have_set = set(have)
        # OCR punctuation noise inside references ("7,03(c" for "7.03(c)"): compare alphanumerics only.
        # Text path only — typed amounts/percentages go through find_values and stay exact.
        alnum = lambda s: "".join(ch for ch in s if ch.isalnum())
        have_alnum = {alnum(h) for h in have} if fuzzy_letters else set()
        hits = 0
        for w in want:
            if w in have_set:
                hits += 1
            elif any(ch.isdigit() for ch in w):
                if fuzzy_letters and alnum(w) in have_alnum:
                    hits += 1
                    continue
                return False
            elif fuzzy_letters and any(SequenceMatcher(None, w, h).ratio() >= 0.8 for h in have):
                hits += 1
        return hits / len(want) >= TEXT_VALUE_RECALL
    if value.type is ValueType.integer:
        return str(value.amount) in _words(text)
    return any(same_value(value, v) for v in find_values(text))


# Characters OCR commonly confuses. Used only to label a miss as likely OCR noise — never to verify.
_CONFUSABLE = str.maketrans({"l": "1", "i": "1", "|": "1", "o": "0", "s": "5", "z": "2", "b": "8", "g": "9"})


def ocr_confusable_match(value: NormalisedValue, text: str) -> bool:
    raw = text_key(value.text if value.type is ValueType.text else str(value.amount or value.date_value or ""))
    return bool(raw) and raw.translate(_CONFUSABLE) in text_key(text).translate(_CONFUSABLE)


class FieldGrounding(BaseModel):
    field_id: str
    page: int
    status: GroundingStatus
    witness: str | None
    match_mode: str | None
    quote_score: float | None
    value_in_quote: bool
    value_in_witness: bool
    likely_ocr_noise: bool  # not verified, but the value matches up to OCR-confusable characters
    reason: str


def ground_field(field: ExtractedField, witnesses: list[Witness]) -> FieldGrounding:
    value = field.value or normalise(field.raw_value)
    best: Match | None = None
    for w in witnesses:
        m = locate(field.source.source_text, w)
        if m and (best is None or m.score > best.score):
            best = m
    in_quote = value is not None and value_in(value, field.source.source_text)
    in_witness = value is not None and best is not None and \
        value_in(value, best.padded_text(), fuzzy_letters=best.witness.kind == "ocr")
    ok_quote = best is not None and best.score >= QUOTE_MIN_SCORE
    status = GroundingStatus.verified if (ok_quote and in_quote and in_witness) else GroundingStatus.not_found
    noise = (status is GroundingStatus.not_found and in_quote and best is not None and best.witness.kind == "ocr"
             and value is not None and ocr_confusable_match(value, best.text))
    reason = ("ok" if status is GroundingStatus.verified
              else "value not in quote" if not in_quote
              else "likely OCR noise" if noise
              else "quote not found on page" if not ok_quote
              else "value differs from page text")
    return FieldGrounding(field_id=field.field_id, page=field.source.page, status=status,
                          witness=best.witness.kind if best else None, match_mode=best.mode if best else None,
                          quote_score=round(best.score, 3) if best else None,
                          value_in_quote=in_quote, value_in_witness=in_witness, likely_ocr_noise=noise,
                          reason=reason)


def route(field: ExtractedField, g: FieldGrounding) -> ExtractedField:
    """Rule-based routing tiers (spec §15). Calibrated scoring replaces this once labels exist."""
    source = field.source.model_copy(update={"grounding": g.status, "match_score": g.quote_score})
    verified = g.status is GroundingStatus.verified
    noise = g.likely_ocr_noise or (verified and g.witness == "ocr" and (g.quote_score or 0) < NOISE_BELOW)
    score = field.model_confidence if verified else 0.0
    # Grounding is a hard gate; fuzzy/embedding dictionary mappings never auto-accept.
    auto = verified and score >= AUTO_ACCEPT_CONFIDENCE and field.match_method not in ("fuzzy", "embedding")
    return field.model_copy(update={
        "source": source, "routing_score": round(score, 3), "vision_noise_flag": noise,
        "agreement": field.agreement.model_copy(update={"ocr_agree": verified}),
        "review_status": "auto_accepted" if auto else "needs_review",
    })


# --- stage entry point -----------------------------------------------------------


class GroundingReport(BaseModel):
    run_id: str
    model_key: str
    prompt_version: str
    fields: int
    verified: int
    not_found: int
    auto_accepted: int
    needs_review: int
    vision_noise: int
    by_reason: dict[str, int]
    by_witness: dict[str, int]
    table_cells_grounded: str
    table_checks_failed: list[str]
    review_queue: list[FieldGrounding]
    # Only with a truth reference (e.g. the clean original of a degraded scan):
    truth: str | None = None
    confusion: dict[str, int] | None = None  # verified|not_found x true|false
    missed_errors: list[str] | None = None  # verified but not truly on the page: the dangerous cell


def truly_present(field: ExtractedField, truth_words: list[Witness]) -> bool:
    """Ground truth for grounding: the quote is on the true page text and carries the value."""
    value = field.value or normalise(field.raw_value)
    alnum = lambda s: "".join(ch for ch in text_key(s) if ch.isalnum())
    for w in truth_words:
        m = locate(field.source.source_text, w)
        if m and m.score >= 0.9 and value is not None and (
                value_in(value, m.text) or
                # PDF text layers drop spaces ("NATIONALASSOCIATION"); for the TRUTH side only, compare
                # letters and digits. This never makes the grounding gate itself more lenient.
                (value.type is ValueType.text and alnum(value.text or "") in alnum(m.padded_text()))):
            return True
    return False


def run_grounding(store, run_id: str, model_key: str, truth_pdf=None,
                  prompt_version: str | None = None) -> tuple[DocumentExtraction, GroundingReport]:
    import json
    from datetime import datetime, timezone

    from .contracts import PageManifest, StageRecord
    from .ocr import text_layer

    t0 = datetime.now(timezone.utc)
    from .pass1 import latest_pass1_version

    version = prompt_version or latest_pass1_version(store, run_id, model_key)
    doc = DocumentExtraction.model_validate_json(
        (store.root / "runs" / run_id / f"extraction-{model_key}-{version}.json").read_text())
    run = store.load_run(run_id)
    manifest = PageManifest.model_validate_json(store.abs(run.manifest_path).read_text())
    ocr_report = store.root / "reports" / run_id / "ocr.json"
    trusted = {p["page"] for p in json.loads(ocr_report.read_text())["pages"] if p["text_layer"] == "trusted"} \
        if ocr_report.exists() else set()
    layer = text_layer(store.blob_path(run.source_sha256)) if trusted else []

    def witnesses_for(page: int) -> list[Witness]:
        out = []
        for p in (page, page - 1, page + 1):  # cited page first; adjacent pages for boundary quotes
            if not 1 <= p <= manifest.page_count:
                continue
            sha = manifest.pages[p - 1].sha256
            files = sorted((store.root / "ocr" / sha).glob("*.json"))
            if files:
                out.append(Witness.from_ocr(OcrPage.model_validate_json(files[0].read_text())))
            if p in trusted:
                out.append(Witness.from_text(layer[p - 1]))
        return out

    cache: dict[int, list[Witness]] = {}
    groundings: dict[str, FieldGrounding] = {}
    routed: dict[str, ExtractedField] = {}
    for f in doc.flat_fields:
        ws = cache.setdefault(f.source.page, witnesses_for(f.source.page))
        g = ground_field(f, ws)
        groundings[f.field_id] = g
        routed[f.field_id] = route(f, g)

    sections = [s.model_copy(update={"clauses": [
        c.model_copy(update={"fields": [routed[f.field_id] for f in c.fields]}) for c in s.clauses]})
        for s in doc.sections]
    doc = doc.model_copy(update={"flat_fields": [routed[f.field_id] for f in doc.flat_fields],
                                 "sections": sections})
    table_pages = {p for s in doc.sections for c in s.clauses for t in c.tables for p in t.pages}
    doc = ground_tables(doc, {p: cache.setdefault(p, witnesses_for(p))[:2] for p in table_pages})

    fields = doc.flat_fields
    gs = list(groundings.values())
    count = lambda xs: {k: sum(1 for x in xs if x == k) for k in sorted(set(xs))}
    checks = [(t.table_id, ch) for s in doc.sections for c in s.clauses for t in c.tables for ch in t.checks]
    grounded_cells = [ch.detail for _, ch in checks if ch.name == "cells_grounded"]
    report = GroundingReport(
        run_id=run_id, model_key=model_key, prompt_version=version, fields=len(fields),
        verified=sum(g.status is GroundingStatus.verified for g in gs),
        not_found=sum(g.status is GroundingStatus.not_found for g in gs),
        auto_accepted=sum(f.review_status == "auto_accepted" for f in fields),
        needs_review=sum(f.review_status == "needs_review" for f in fields),
        vision_noise=sum(f.vision_noise_flag for f in fields),
        by_reason=count([g.reason for g in gs]), by_witness=count([g.witness or "none" for g in gs]),
        table_cells_grounded="; ".join(grounded_cells) or "no tables",
        table_checks_failed=[f"{tid}:{ch.name} ({ch.detail})" for tid, ch in checks if not ch.passed],
        review_queue=[groundings[f.field_id] for f in fields if f.review_status == "needs_review"],
    )

    if truth_pdf is not None:
        truth_text = text_layer(truth_pdf)
        conf = {"verified&true": 0, "verified&false": 0, "not_found&true": 0, "not_found&false": 0}
        missed = []
        for f in fields:
            p = f.source.page
            tw = [Witness.from_text(truth_text[q - 1]) for q in (p, p - 1, p + 1) if 1 <= q <= len(truth_text)]
            true = truly_present(f, tw)
            status = "verified" if f.source.grounding is GroundingStatus.verified else "not_found"
            conf[f"{status}&{'true' if true else 'false'}"] += 1
            if status == "verified" and not true:
                missed.append(f"{f.field_id} {f.name} = {f.raw_value!r}")
        report.truth, report.confusion, report.missed_errors = truth_pdf.name, conf, missed

    store.save_json(f"runs/{run_id}/extraction-{model_key}-{version}.grounded.json", doc)
    store.save_json(f"reports/{run_id}/grounding-{model_key}-{version}.json", report)
    store.append_stage(run_id, StageRecord(
        stage=f"ground:{model_key}:{version}", status="ok", started_at=t0, finished_at=datetime.now(timezone.utc),
        detail=f"{report.verified}/{report.fields} verified; {report.auto_accepted} auto-accepted; "
               f"{report.needs_review} to review"))
    return doc, report


# --- tables --------------------------------------------------------------------


def _is_level_grid(table) -> bool:
    """Pricing grids: first column is a level (1, 2, 3 / I, II / Level 1 ...)."""
    first = [text_key(c.raw_value) for c in table.cells if c.col == 0 and not c.is_header and c.row > 0]
    return len(first) >= 3 and all(re.fullmatch(r"(level\s*)?([0-9]+|[ivx]+)", f) for f in first)


def _totals_add_up(table) -> TableCheck | None:
    """Rows above a 'Total' row must sum to it, per numeric column (e.g. commitments = facility amount)."""
    total_rows = [c.row for c in table.cells if c.col == 0 and text_key(c.raw_value).startswith("total")]
    if not total_rows:
        return None
    total_row = total_rows[-1]
    cols: dict[int, dict[int, NormalisedValue]] = {}
    for c in table.cells:
        v = c.value or normalise(c.raw_value)
        if c.row > 0 and v is not None and v.type in (ValueType.money, ValueType.percentage) and v.amount is not None:
            cols.setdefault(c.col, {})[c.row] = v
    checked, bad = 0, []
    for col, by_row in cols.items():
        if total_row not in by_row or len(by_row) < 3:
            continue
        parts = sum(v.amount for r, v in by_row.items() if r < total_row)
        total = by_row[total_row].amount
        tolerance = Decimal("0.0000001") * len(by_row) if by_row[total_row].type is ValueType.percentage else 0
        checked += 1
        if abs(parts - total) > tolerance + Decimal("0.01"):
            bad.append(f"col {col}: rows sum to {parts}, total says {total}")
    if not checked:
        return None
    return TableCheck(name="rows_sum_to_total", passed=not bad,
                      detail=f"{checked} columns checked" + (f"; {'; '.join(bad)}" if bad else ""))


def _numeric_columns_monotonic(table) -> TableCheck | None:
    if not _is_level_grid(table):
        return None
    cols: dict[int, list[tuple[int, NormalisedValue]]] = {}
    for c in table.cells:
        v = c.value or normalise(c.raw_value)
        if not c.is_header and v is not None and v.type in (ValueType.percentage, ValueType.bps, ValueType.money):
            cols.setdefault(c.col, []).append((c.row, v))
    checked, bad = 0, []
    for col, vals in cols.items():
        if len(vals) < 3:
            continue
        seq = [v.amount for _, v in sorted(vals, key=lambda t: t[0])]
        checked += 1
        if not (all(a <= b for a, b in zip(seq, seq[1:])) or all(a >= b for a, b in zip(seq, seq[1:]))):
            bad.append(col)
    if not checked:
        return None
    return TableCheck(name="numeric_columns_monotonic", passed=not bad,
                      detail=f"{checked} columns checked" + (f"; non-monotonic columns {bad}" if bad else ""))


def ground_tables(doc: DocumentExtraction, witnesses_by_page: dict[int, list[Witness]]) -> DocumentExtraction:
    """Each non-empty cell's value must appear on the table's page(s); plus arithmetic checks."""
    sections = []
    for section in doc.sections:
        clauses = []
        for clause in section.clauses:
            tables = []
            for t in clause.tables:
                texts = [" ".join(w.words) for p in t.pages for w in witnesses_by_page.get(p, [])]
                cells, found, total = [], 0, 0
                for c in t.cells:
                    if not (c.raw_value or "").strip():
                        cells.append(c)
                        continue
                    total += 1
                    v = c.value or normalise(c.raw_value)
                    hit = v is not None and any(value_in(v, tx) for tx in texts)
                    found += hit
                    cells.append(c.model_copy(update={"value": v, "source": SourceRef(
                        page=t.pages[0], source_text=c.raw_value,
                        grounding=GroundingStatus.verified if hit else GroundingStatus.not_found)}))
                checks = [ch for ch in t.checks
                          if ch.name not in ("cells_grounded", "numeric_columns_monotonic", "rows_sum_to_total")]
                checks.append(TableCheck(name="cells_grounded", passed=found == total, detail=f"{found}/{total} cells"))
                t2 = t.model_copy(update={"cells": cells})
                for check in (_numeric_columns_monotonic(t2), _totals_add_up(t2)):
                    if check is not None:
                        checks.append(check)
                tables.append(t2.model_copy(update={"checks": checks}))
            clauses.append(clause.model_copy(update={"tables": tables}))
        sections.append(section.model_copy(update={"clauses": clauses}))
    return doc.model_copy(update={"sections": sections})
