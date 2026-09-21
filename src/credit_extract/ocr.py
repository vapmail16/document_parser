"""Stage 2 — independent OCR (spec §6) and the text-layer probe (spec §17).

OCR is the second witness: grounding checks model quotes against it, completeness counts its
tokens, tiling picks cut lines from its whitespace. It is never the extraction result.
"""

import csv
import io
import re
import shutil
import subprocess
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path
from typing import Literal, Protocol

import pypdfium2 as pdfium

from .contracts import Contract, PageImage
from .store import Store


class OcrToken(Contract):
    text: str
    conf: float  # engine confidence 0..1
    bbox: tuple[float, float, float, float]  # fractions of page width/height (x0, y0, x1, y1)
    block: int
    line: int  # line index, unique within the page


class OcrPage(Contract):
    page: int
    image_sha256: str
    engine: str  # "tesseract 5.x.y"
    tokens: list[OcrToken]

    @property
    def text(self) -> str:
        lines: dict[int, list[str]] = {}
        for t in self.tokens:
            lines.setdefault(t.line, []).append(t.text)
        return "\n".join(" ".join(ws) for ws in lines.values())


class OcrEngine(Protocol):
    name: str

    def ocr(self, image_path: Path) -> list[OcrToken]: ...


class TesseractEngine:
    """Tesseract via its CLI (Apache-2.0, fully offline). No Python binding dependency."""

    def __init__(self, lang: str = "eng", psm: int = 3, binary: str = "tesseract"):
        self.binary = shutil.which(binary) or binary
        self.lang, self.psm = lang, psm
        self.name = f"tesseract {_tesseract_version(self.binary)} lang={lang} psm={psm}"

    def ocr(self, image_path: Path) -> list[OcrToken]:
        out = subprocess.run(
            [self.binary, str(image_path), "stdout", "-l", self.lang, "--psm", str(self.psm), "tsv"],
            check=True, capture_output=True, text=True,
        ).stdout
        rows = list(csv.DictReader(io.StringIO(out), delimiter="\t", quoting=csv.QUOTE_NONE))
        page_row = next(r for r in rows if r["level"] == "1")
        w, h = float(page_row["width"]), float(page_row["height"])
        tokens, line_ids = [], {}
        for r in rows:
            if r["level"] != "5" or not r["text"].strip() or float(r["conf"]) < 0:
                continue
            key = (r["block_num"], r["par_num"], r["line_num"])
            line = line_ids.setdefault(key, len(line_ids))
            x, y, bw, bh = (float(r[k]) for k in ("left", "top", "width", "height"))
            tokens.append(OcrToken(
                text=r["text"].strip(), conf=float(r["conf"]) / 100, block=int(r["block_num"]), line=line,
                bbox=(x / w, y / h, min(1.0, (x + bw) / w), min(1.0, (y + bh) / h)),
            ))
        return tokens


@lru_cache
def _tesseract_version(binary: str) -> str:
    out = subprocess.run([binary, "--version"], check=True, capture_output=True, text=True)
    first = (out.stdout or out.stderr).splitlines()[0]
    return first.split()[-1].lstrip("v")


def ocr_pages(store: Store, pages: list[PageImage], engine: OcrEngine, workers: int = 1) -> list[OcrPage]:
    """OCR each page image, in page order. Cached by (image hash, engine string)."""

    def one(p: PageImage) -> OcrPage:
        cache = store.root / "ocr" / p.sha256 / (re.sub(r"[^A-Za-z0-9.=-]+", "_", engine.name) + ".json")
        if cache.exists():
            return OcrPage.model_validate_json(cache.read_text())
        page = OcrPage(page=p.page, image_sha256=p.sha256, engine=engine.name,
                       tokens=engine.ocr(store.abs(p.path)))
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_suffix(".tmp")
        tmp.write_text(page.model_dump_json() + "\n")
        tmp.rename(cache)
        return page

    if workers <= 1:
        return [one(p) for p in pages]
    with ThreadPoolExecutor(max_workers=workers) as pool:  # tesseract is a subprocess: threads suffice
        return list(pool.map(one, pages))


# --- text-layer probe (spec §17) ---------------------------------------------

_WORD_RE = re.compile(r"[A-Za-z0-9$%.,()/-]+")


def words(text: str) -> Counter[str]:
    """Bag of normalised words: casefolded, trailing punctuation stripped."""
    out: Counter[str] = Counter()
    for w in _WORD_RE.findall(text.casefold()):
        w = w.strip(".,()")
        if w:
            out[w] += 1
    return out


def word_f1(reference: str, candidate: str) -> float:
    """Order-insensitive word-bag F1. 1.0 == same words; robust to reading-order differences."""
    ref, cand = words(reference), words(candidate)
    if not ref and not cand:
        return 1.0
    overlap = sum((ref & cand).values())
    if not overlap:
        return 0.0
    p, r = overlap / sum(cand.values()), overlap / sum(ref.values())
    return 2 * p * r / (p + r)


def text_layer(pdf_path: Path) -> list[str]:
    pdf = pdfium.PdfDocument(pdf_path)
    try:
        out = []
        for i in range(len(pdf)):
            page = pdf[i]
            tp = page.get_textpage()
            out.append(tp.get_text_range())
            tp.close()
            page.close()
        return out
    finally:
        pdf.close()


TRUSTED_F1 = 0.90  # below this, an embedded text layer disagrees too much with what is on the page


def probe(layer_text: str, ocr: OcrPage) -> tuple[Literal["none", "untrusted", "trusted"], float | None]:
    """Classify a page's embedded text layer against independent OCR."""
    if not layer_text.strip():
        return "none", None
    f1 = word_f1(layer_text, ocr.text)
    return ("trusted" if f1 >= TRUSTED_F1 else "untrusted"), f1


# --- stage entry point -------------------------------------------------------


class OcrPageSummary(Contract):
    page: int
    tokens: int
    mean_conf: float | None
    text_layer: Literal["none", "untrusted", "trusted"]
    text_layer_f1: float | None  # embedded text layer vs this OCR
    reference_f1: float | None  # reference text vs this OCR (OCR accuracy estimate)


class OcrReport(Contract):
    run_id: str
    engine: str
    reference: str | None  # filename whose text layer served as reference
    pages: list[OcrPageSummary]

    def mean(self, attr: str) -> float | None:
        vals = [getattr(p, attr) for p in self.pages if getattr(p, attr) is not None]
        return sum(vals) / len(vals) if vals else None


def run_ocr(store: Store, run_id: str, engine: OcrEngine, *, workers: int = 1,
            reference_pdf: Path | None = None) -> OcrReport:
    """Stage 2 over one run. `reference_pdf` (e.g. the clean original of a degraded variant)
    provides a text layer to measure OCR accuracy against; pages must correspond 1:1."""
    from datetime import datetime, timezone

    from .contracts import PageManifest, StageRecord

    t0 = datetime.now(timezone.utc)
    run = store.load_run(run_id)
    manifest = PageManifest.model_validate_json(store.abs(run.manifest_path).read_text())
    ocr = ocr_pages(store, manifest.pages, engine, workers=workers)
    layers = text_layer(store.blob_path(run.source_sha256))
    reference = text_layer(reference_pdf) if reference_pdf else None
    if reference is not None and len(reference) != len(ocr):
        raise ValueError(f"reference has {len(reference)} pages, run has {len(ocr)}")

    summaries = []
    for i, page in enumerate(ocr):
        status, layer_f1 = probe(layers[i], page)
        summaries.append(OcrPageSummary(
            page=page.page, tokens=len(page.tokens),
            mean_conf=(sum(t.conf for t in page.tokens) / len(page.tokens)) if page.tokens else None,
            text_layer=status, text_layer_f1=layer_f1,
            reference_f1=word_f1(reference[i], page.text) if reference is not None else None,
        ))
    report = OcrReport(run_id=run_id, engine=engine.name,
                       reference=reference_pdf.name if reference_pdf else None, pages=summaries)
    store.save_json(f"reports/{run_id}/ocr.json", report)

    trusted = sum(1 for s in summaries if s.text_layer == "trusted")
    ref = report.mean("reference_f1")
    store.append_stage(run_id, StageRecord(
        stage="ocr", status="ok", started_at=t0, finished_at=datetime.now(timezone.utc),
        detail=f"{engine.name}; {sum(s.tokens for s in summaries)} tokens; text layer trusted on "
               f"{trusted}/{len(summaries)} pages" + (f"; mean reference word-F1 {ref:.3f}" if ref else ""),
    ))
    return report
