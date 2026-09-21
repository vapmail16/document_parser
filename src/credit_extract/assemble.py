"""Stage 4 — assemble & completeness (spec §6). No model involved.

Per-band model output -> one DocumentExtraction:
  1. page furniture (browser/print headers & footers, page numbers) found from OCR margins
     across all pages and stripped;
  2. band overlaps merged: continued blocks joined, duplicated blocks/table rows/fields removed;
  3. clauses stitched across page boundaries using the continuation markers;
  4. clauses grouped into sections (ARTICLE headings); fields and tables attached;
  5. completeness: every page gets a coverage record against its OCR tokens — nothing silent.
"""

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher

from pydantic import BaseModel

from . import __version__
from .config import Settings
from .contracts import (Clause, DocumentExtraction, EnvelopeField, EnvelopeTable,
                        ExtractedField, PageCoverage, PageManifest, Party, Provenance, Section, SourceRef,
                        StageRecord, Table, TableCell)
from .grounding import Witness, locate
from .normalise import normalise, same_value, text_key
from .ocr import OcrPage
from .pass1 import PagePass1, latest_pass1_version, pass1_dir
from .store import Store

MARGIN = 0.06  # top/bottom fraction of the page where furniture lives
FURNITURE_MIN_SHARE = 0.3  # a margin line on >= 30% of pages is furniture
FURNITURE_SIMILARITY = 0.8
COVERAGE_MIN_CONF = 0.5  # OCR tokens below this are ignored (scan speckle)
LOW_COVERAGE = 0.80
BLANK_MAX_WORDS = 8

_PAGE_NO = re.compile(r"^\W*(\d{1,4}|[ivxlc]{1,6}|\d{1,4}\s*/\s*\d{1,4}|page\s+\d{1,4}(\s+of\s+\d{1,4})?)\W*$",
                      re.IGNORECASE)


def _mask(s: str) -> str:
    return re.sub(r"\d", "#", text_key(s))


def _words(s: str) -> list[str]:
    return [w for w in (t.strip(".,;:()[]\"'") for t in text_key(s).split()) if w]


# --- 1. furniture ----------------------------------------------------------------


def _ocr_lines(page: OcrPage) -> list[tuple[str, float, list[int]]]:
    """(text, y-centre, token indices) per OCR line."""
    lines: dict[int, list[int]] = {}
    for i, t in enumerate(page.tokens):
        lines.setdefault(t.line, []).append(i)
    out = []
    for idx in lines.values():
        toks = [page.tokens[i] for i in idx]
        y = sum((t.bbox[1] + t.bbox[3]) / 2 for t in toks) / len(toks)
        out.append((" ".join(t.text for t in toks), y, idx))
    return out


def detect_furniture(ocr_pages: list[OcrPage]) -> list[str]:
    """Masked margin lines that recur across pages (digits masked, so page counters match)."""
    clusters: list[list] = []  # [representative masked text, page set]
    for page in ocr_pages:
        for text, y, _ in _ocr_lines(page):
            if MARGIN < y < 1 - MARGIN or len(text) < 4:
                continue
            m = _mask(text)
            for c in clusters:
                if SequenceMatcher(None, m, c[0]).ratio() >= FURNITURE_SIMILARITY:
                    c[1].add(page.page)
                    break
            else:
                clusters.append([m, {page.page}])
    need = max(2, FURNITURE_MIN_SHARE * len(ocr_pages))
    return [c[0] for c in clusters if len(c[1]) >= need]


def is_furniture(text: str, furniture: list[str]) -> bool:
    if _PAGE_NO.match(text):
        return True
    m = _mask(text)
    return any(SequenceMatcher(None, m, f).ratio() >= FURNITURE_SIMILARITY or (len(m) >= 8 and m in f)
               for f in furniture)


def strip_furniture(text: str, furniture: list[str]) -> str:
    kept = [ln for ln in text.splitlines() if ln.strip() and not is_furniture(ln, furniture)]
    return "\n".join(kept)


# --- 2-3. merging --------------------------------------------------------------------


@dataclass
class Blk:
    kind: str
    number: str | None
    heading: str | None
    text: str
    pages: list[int]
    cont_prev: bool
    cont_next: bool


@dataclass
class Fld:
    raw: EnvelopeField
    page: int
    block: int | None  # index into the document-level block list


@dataclass
class Tbl:
    caption: str | None
    pages: list[int]
    rows: list[list[tuple[int, int, int, bool, str | None]]]  # per row: (col, row_span, col_span, header, value)
    cont_prev: bool
    cont_next: bool


@dataclass
class Assembly:
    blocks: list[Blk] = field(default_factory=list)
    fields: list[Fld] = field(default_factory=list)
    tables: list[Tbl] = field(default_factory=list)
    parties: list[tuple[str, str, str, int]] = field(default_factory=list)  # name, role, quote, page


def join_overlap(a: str, b: str) -> str:
    """Concatenate, dropping the longest run of words that ends `a` and starts `b`."""
    aw, bw = a.split(), b.split()
    ka, kb = [text_key(w) for w in aw], [text_key(w) for w in bw]
    for k in range(min(len(aw), len(bw), 120), 2, -1):
        if ka[-k:] == kb[:k]:
            return " ".join(aw + bw[k:])
    return f"{a} {b}"


def _contained(small: str, big: str) -> bool:
    s, b = text_key(small), text_key(big)
    return bool(s) and (s in b or (len(s) > 20 and SequenceMatcher(None, s, b).find_longest_match().size >= 0.9 * len(s)))


def _table_rows(t: EnvelopeTable) -> list[list[tuple[int, int, int, bool, str | None]]]:
    rows: dict[int, list] = {}
    for c in t.cells:
        rows.setdefault(c.row, []).append((c.col, c.row_span, c.col_span, c.is_header, c.raw_value))
    out = [sorted(rows[r]) for r in sorted(rows)]
    return [r for r in out if any((v or "").strip() for *_, v in r)]  # drop fully empty rows


def _row_key(row) -> tuple:
    return tuple(text_key(v) for *_, v in row)


def _merge_table(a: Tbl, b: Tbl) -> None:
    seen = {_row_key(r) for r in a.rows}
    a.rows += [r for r in b.rows if _row_key(r) not in seen]
    a.pages = sorted(set(a.pages + b.pages))
    a.cont_next = b.cont_next


def merge_page(result: PagePass1, furniture: list[str], ocr: OcrPage | None, height_frac_of,
               asm: Assembly) -> None:
    """Fold one page's bands into the document assembly."""
    page = result.page
    ocr_w = Witness.from_ocr(ocr) if ocr else None
    prev_band_fields: list[Fld] = []
    prev_band_y1: float | None = None
    prev_band_text = ""
    for bi, att in enumerate(result.bands):
        env = att.response.envelope
        local_to_global: dict[int, int | None] = {}
        for li, b in enumerate(env.blocks):
            text = strip_furniture(b.text, furniture)
            if not text.strip():
                local_to_global[li] = None
                continue
            last = asm.blocks[-1] if asm.blocks else None
            first_in_band = all(v is None for v in local_to_global.values())
            # bi > 0: previous band of this page; bi == 0: last block of the immediately preceding page
            adjacent = last is not None and (bi > 0 or last.pages[-1] == page - 1)
            if adjacent and bi > 0 and first_in_band and _contained(text, prev_band_text):
                local_to_global[li] = len(asm.blocks) - 1  # overlap duplicate of the previous band
                continue
            if adjacent and first_in_band and (last.cont_next or b.continues_from_previous):
                last.text = join_overlap(last.text, text)
                last.cont_next = b.continues_on_next
                if page not in last.pages:
                    last.pages.append(page)
                local_to_global[li] = len(asm.blocks) - 1
                continue
            asm.blocks.append(Blk(b.kind, b.number, b.heading, text, [page], b.continues_from_previous,
                                  b.continues_on_next))
            local_to_global[li] = len(asm.blocks) - 1

        # Tables: merge a continuation into the previous open table (across bands or pages).
        for t in env.tables:
            rows = _table_rows(t)
            if not rows:
                continue
            tbl = Tbl(t.caption, [page], rows, t.continues_from_previous, t.continues_on_next)
            if asm.tables and (asm.tables[-1].cont_next or tbl.cont_prev) and \
                    (page - asm.tables[-1].pages[-1]) <= 1:
                _merge_table(asm.tables[-1], tbl)
            else:
                asm.tables.append(tbl)

        # Fields: drop one-to-one duplicates of the previous band that lie in the overlap.
        absorbed: set[int] = set()
        band_fields = []
        for f in env.fields:
            if is_furniture(f.source_text, furniture) or is_furniture(f.raw_value, furniture):
                continue  # e.g. the print URL extracted as a "Source URL" field
            if bi > 0 and _is_overlap_dup(f, prev_band_fields, absorbed, ocr_w, prev_band_y1):
                continue
            fld = Fld(f, page, local_to_global.get(f.block_index) if f.block_index is not None else None)
            asm.fields.append(fld)
            band_fields.append(fld)
        prev_band_fields = band_fields
        prev_band_y1 = height_frac_of(att.band.y1)
        prev_band_text = " ".join([b.text for b in env.blocks] +
                                  [" ".join(c.raw_value or "" for c in t.cells) for t in env.tables])

        for p in env.parties:
            if not any(text_key(p.name) == text_key(q[0]) for q in asm.parties):
                asm.parties.append((p.name, p.role, p.source_text, page))


def _is_overlap_dup(f: EnvelopeField, prev: list[Fld], absorbed: set[int], ocr_w: Witness | None,
                    prev_y1: float | None) -> bool:
    if ocr_w is not None and prev_y1 is not None:
        m = locate(f.source_text, ocr_w)
        if m and m.score >= 0.8 and m.bbox and (m.bbox[1] + m.bbox[3]) / 2 > prev_y1 + 0.01:
            return False  # located below the previous band: genuinely new
    v = normalise(f.raw_value)
    for i, g in enumerate(prev):
        if i in absorbed:
            continue
        gv = normalise(g.raw.raw_value)
        if v is not None and gv is not None and same_value(v, gv) and \
                SequenceMatcher(None, text_key(f.source_text), text_key(g.raw.source_text)).ratio() >= 0.5:
            absorbed.add(i)
            return True
    return False


# --- 4. structure ---------------------------------------------------------------------

_ARTICLE = re.compile(r"^\s*article\b", re.IGNORECASE)


def _clause_id(section_idx: int, clause_idx: int, text: str) -> str:
    return f"s{section_idx:02d}.c{clause_idx:04d}-{hashlib.sha1(text_key(text).encode()).hexdigest()[:8]}"


def build_structure(asm: Assembly) -> tuple[list[Section], dict[int, str]]:
    """Sections from ARTICLE headings; one clause per non-heading block. Returns block->clause_id."""
    sections: list[tuple[str | None, list[Clause]]] = [(None, [])]
    block_clause: dict[int, str] = {}
    pending_heading: list[int] = []  # heading block indices waiting for their clause
    heading_text: str | None = None
    for i, b in enumerate(asm.blocks):
        if b.kind == "heading":
            if _ARTICLE.match(b.text) or _ARTICLE.match(b.number or ""):
                sections.append((b.text.strip(), []))
                heading_text = None
            else:
                heading_text = b.text.strip()
            pending_heading.append(i)
            continue
        clauses = sections[-1][1]
        cid = _clause_id(len(sections) - 1, len(clauses), b.text)
        clauses.append(Clause(clause_id=cid, number=b.number, heading=b.heading or heading_text, text=b.text,
                              page_range=(min(b.pages), max(b.pages))))
        block_clause[i] = cid
        for h in pending_heading:
            block_clause[h] = cid
        pending_heading, heading_text = [], None
    out = [Section(section_id=f"s{i:02d}", title=title or ("front matter" if i == 0 else None), clauses=cl)
           for i, (title, cl) in enumerate(sections) if cl]
    return out, block_clause


def _attach_table(t: Table, sections: list[Section]) -> str | None:
    header = set(_words(" ".join(c.raw_value or "" for c in t.cells if c.row == 0)))
    best, best_score = None, -1.0
    for s in sections:
        for c in s.clauses:
            if c.page_range[0] <= t.pages[0] <= c.page_range[1] or c.page_range[1] == t.pages[0] - 1:
                words = set(_words(c.text or ""))
                score = len(header & words) / (len(header) or 1)
                if re.search(r"(below|following|as follows)\W*:\s*$", c.text or "", re.I):
                    score += 1.0  # "... as set forth below:" introduces the table that follows it
                if score > best_score:
                    best, best_score = c.clause_id, score
    return best


# --- 5. completeness ------------------------------------------------------------------


def page_words_ocr(page: OcrPage, furniture: list[str]) -> list[str]:
    keep: list[str] = []
    for text, _, idx in _ocr_lines(page):
        if is_furniture(text, furniture):
            continue
        for i in idx:
            t = page.tokens[i]
            keep += [w for w in _words(t.text) if t.conf >= COVERAGE_MIN_CONF and any(ch.isalnum() for ch in w)]
    return keep


def coverage(ocr_words: list[str], extracted_text: str) -> float:
    from collections import Counter

    have = Counter(_words(extracted_text))
    want = Counter(ocr_words)
    return sum((want & have).values()) / (sum(want.values()) or 1)


# --- entry point ----------------------------------------------------------------------


class AssemblyReport(BaseModel):
    run_id: str
    model_key: str
    prompt_version: str
    furniture: list[str]
    pages_extracted: int
    pages_by_status: dict[str, int]
    low_coverage_pages: list[int]
    blocks: int
    clauses: int
    sections: int
    fields: int
    tables: int
    parties: int
    execution_status: str
    document_kind: str


_AMENDMENT = re.compile(r"\b(amendment no\.?|(first|second|third|fourth|fifth|sixth) amendment)\b", re.I)


def classify(cover_text: str) -> tuple[str, str]:
    t = text_key(cover_text)
    status = ("executed" if "execution version" in t or "execution copy" in t
              else "draft" if re.search(r"\bdraft\b", t) else "specimen" if "specimen" in t else "unknown")
    kind = ("amendment" if _AMENDMENT.search(t) else "restatement" if "amended and restated" in t
            else "original" if "credit agreement" in t or "facility agreement" in t else "unknown")
    return status, kind


def run_assemble(store: Store, settings: Settings, run_id: str, model_key: str,
                 prompt_version: str | None = None, pass2: str | None = None) -> tuple[DocumentExtraction, AssemblyReport]:
    t0 = datetime.now(timezone.utc)
    run = store.load_run(run_id)
    manifest = PageManifest.model_validate_json(store.abs(run.manifest_path).read_text())
    ocr_pages: list[OcrPage] = []
    for p in manifest.pages:
        found = sorted((store.root / "ocr" / p.sha256).glob("*.json")) if (store.root / "ocr" / p.sha256).exists() else []
        if not found:
            raise FileNotFoundError(f"page {p.page} has no OCR; run `cae ocr {run_id}` first (completeness needs it)")
        ocr_pages.append(OcrPage.model_validate_json(found[0].read_text()))

    version = prompt_version or latest_pass1_version(store, run_id, model_key)
    src = pass1_dir(store, run_id, model_key, version)
    results = {r.page: r for r in (PagePass1.model_validate_json(f.read_text()) for f in sorted(src.glob("p*.json")))}
    if not results:
        raise FileNotFoundError(f"no pass-1 output for model {model_key!r}; run `cae pass1 {run_id}` first")

    furniture = detect_furniture(ocr_pages)
    asm = Assembly()
    for page_no in sorted(results):
        r = results[page_no]
        if r.status != "ok":
            continue
        if asm.blocks and page_no - 1 not in results:
            asm.blocks[-1].cont_next = False  # never stitch across a gap in the page selection
            if asm.tables:
                asm.tables[-1].cont_next = False
        h = manifest.pages[page_no - 1].height_px
        merge_page(r, furniture, ocr_pages[page_no - 1], lambda y, h=h: y / h, asm)

    sections, block_clause = build_structure(asm)

    fields: list[ExtractedField] = []
    per_page_count: dict[int, int] = {}
    for f in asm.fields:
        n = per_page_count[f.page] = per_page_count.get(f.page, 0) + 1
        fields.append(ExtractedField(
            field_id=f"p{f.page:04d}-f{n:03d}", name=f.raw.name, raw_value=f.raw.raw_value,
            value=normalise(f.raw.raw_value), model_confidence=min(1.0, max(0.0, f.raw.confidence)),
            clause_id=block_clause.get(f.block) if f.block is not None else None,
            source=SourceRef(page=f.page, source_text=f.raw.source_text or f.raw.raw_value or "?"),
        ))

    p2_fields, p2_version = _pass2_fields(store, run_id, model_key, version, furniture, pass2)
    fields += p2_fields

    tables: list[Table] = []
    for i, t in enumerate(asm.tables):
        cells = [TableCell(row=r, col=col, row_span=rs, col_span=cs, is_header=hd, raw_value=v,
                           value=normalise(v), model_confidence=1.0)
                 for r, row in enumerate(t.rows) for col, rs, cs, hd, v in row]
        n_rows = max((c.row + c.row_span for c in cells), default=0)
        n_cols = max((c.col + c.col_span for c in cells), default=0)
        tables.append(Table(table_id=f"t{i + 1:03d}-p{t.pages[0]:04d}", caption=t.caption, pages=t.pages,
                            n_rows=n_rows, n_cols=n_cols, cells=cells))

    by_clause: dict[str, list[ExtractedField]] = {}
    for f in fields:
        if f.clause_id:
            by_clause.setdefault(f.clause_id, []).append(f)
    tables_by_clause: dict[str, list[Table]] = {}
    for t in tables:
        cid = _attach_table(t, sections)
        if cid:
            tables_by_clause.setdefault(cid, []).append(t.model_copy(update={"clause_id": cid}))
    sections = [s.model_copy(update={"clauses": [
        c.model_copy(update={"fields": by_clause.get(c.clause_id, []), "tables": tables_by_clause.get(c.clause_id, [])})
        for c in s.clauses]}) for s in sections]

    # Completeness against OCR, per page.
    text_by_page: dict[int, list[str]] = {}
    for b in asm.blocks:
        for p in b.pages:
            text_by_page.setdefault(p, []).append(b.text)
    for t in asm.tables:
        for p in t.pages:
            text_by_page.setdefault(p, []).append(" ".join(v or "" for row in t.rows for *_, v in row))
    for f in asm.fields:
        text_by_page.setdefault(f.page, []).append(f"{f.raw.source_text} {f.raw.raw_value}")
    for name, role, quote, p in asm.parties:
        text_by_page.setdefault(p, []).append(f"{name} {role} {quote}")

    layer = {p["page"]: p["text_layer"] for p in _ocr_report_pages(store, run_id)}
    coverage_records = []
    for p in manifest.pages:
        words = page_words_ocr(ocr_pages[p.page - 1], furniture)
        r = results.get(p.page)
        attempts = len(r.attempts) if r else 1
        tiles = r.bands[0].bands_on_page if r and r.bands else 1
        if r is None:
            status, ratio = "not_extracted", 0.0
        elif r.status != "ok":
            status, ratio = "failed", 0.0
        elif len(words) <= BLANK_MAX_WORDS:
            status, ratio = "blank", 1.0
        else:
            ratio = coverage(words, " ".join(text_by_page.get(p.page, [])))
            status = "ok" if ratio >= LOW_COVERAGE else "low_coverage"
        coverage_records.append(PageCoverage(page=p.page, status=status, ocr_token_count=len(words),
                                             covered_token_ratio=round(ratio, 4), tiles=tiles, attempts=attempts,
                                             text_layer=layer.get(p.page, "none")))

    exec_status, kind = classify(" ".join(t.text for t in ocr_pages[0].tokens[:400]) + " " +
                                 " ".join(b.text for b in asm.blocks if 1 in b.pages))
    spec = settings.model(model_key)
    any_result = next(iter(results.values()))
    doc = DocumentExtraction(
        document_id=run.document_id, version_label=run.version_label, effective_date=run.effective_date,
        execution_status=exec_status, document_kind=kind,
        parties=[Party(name=n, role=r, source=SourceRef(page=p, source_text=q or n)) for n, r, q, p in asm.parties],
        sections=sections, defined_terms=[], flat_fields=fields, page_coverage=coverage_records,
        provenance=Provenance(
            source_sha256=run.source_sha256, page_count=manifest.page_count, pipeline_version=__version__,
            model_id=model_key, model_params={"model_name": spec.model_name, **spec.params},
            prompt_versions={"pass1_extraction": any_result.prompt_version,
                             **({"pass2_schema_hint": p2_version} if p2_version else {})},
            dictionary_version=run.dictionary_version, ocr_engine=ocr_pages[0].engine, run_id=run_id,
            extracted_at=datetime.now(timezone.utc)),
    )
    statuses: dict[str, int] = {}
    for c in coverage_records:
        statuses[c.status] = statuses.get(c.status, 0) + 1
    report = AssemblyReport(
        run_id=run_id, model_key=model_key, prompt_version=version, furniture=furniture, pages_extracted=len(results),
        pages_by_status=statuses, low_coverage_pages=[c.page for c in coverage_records if c.status == "low_coverage"],
        blocks=len(asm.blocks), clauses=sum(len(s.clauses) for s in sections), sections=len(sections),
        fields=len(fields), tables=len(tables), parties=len(asm.parties), execution_status=exec_status,
        document_kind=kind)
    store.save_json(f"runs/{run_id}/extraction-{model_key}-{version}.json", doc)
    store.save_json(f"reports/{run_id}/assemble-{model_key}-{version}.json", report)
    store.append_stage(run_id, StageRecord(
        stage=f"assemble:{model_key}:{version}", status="ok", started_at=t0, finished_at=datetime.now(timezone.utc),
        detail=f"{report.clauses} clauses, {report.fields} fields, {report.tables} tables; pages {statuses}"))
    return doc, report


def _pass2_fields(store: Store, run_id: str, model_key: str, pass1_version: str,
                  furniture: list[str], pass2_tag_wanted: str | None = None) -> tuple[list[ExtractedField], str | None]:
    """Fields from the latest pass-2 sweep over these pass-1 bands (if one was run), and its prompt version."""
    base = store.root / "runs" / run_id / "pass2" / model_key
    sweeps = [d for d in base.glob(f"{pass1_version}+v*") if d.is_dir()] if base.exists() else []
    if pass2_tag_wanted:
        sweeps = [d for d in sweeps if d.name == f"{pass1_version}+{pass2_tag_wanted}"]
    # latest = highest (prompt version, dictionary version)
    nums = lambda d: tuple(int(x) for x in re.findall(r"\d+", d.name.split("+", 1)[1]))
    sweeps.sort(key=nums)
    if not sweeps:
        return [], None
    out, seen = [], set()
    for f in sorted(sweeps[-1].glob("p*.json")):
        r = PagePass1.model_validate_json(f.read_text())
        if r.status != "ok":
            continue
        n = 0
        for b in r.bands:
            for e in b.response.envelope.fields:
                key = (r.page, text_key(e.name), text_key(e.raw_value))
                if key in seen or is_furniture(e.source_text, furniture):
                    continue  # band-overlap duplicate
                seen.add(key)
                n += 1
                out.append(ExtractedField(
                    field_id=f"p{r.page:04d}-s{n:03d}", name=e.name, raw_value=e.raw_value,
                    value=normalise(e.raw_value), model_confidence=min(1.0, max(0.0, e.confidence)),
                    extraction_pass=2, source=SourceRef(page=r.page, source_text=e.source_text or e.raw_value or "?")))
    return out, sweeps[-1].name.split("+", 1)[1]


def _ocr_report_pages(store: Store, run_id: str) -> list[dict]:
    import json

    path = store.root / "reports" / run_id / "ocr.json"
    return json.loads(path.read_text())["pages"] if path.exists() else []
