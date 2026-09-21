"""Stage 0 (ingest) and Stage 1 (rasterise) — spec §6. No model involved."""

import hashlib
import io
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal

import pypdfium2 as pdfium

from . import __version__
from .contracts import PageImage, PageManifest, RunRecord, StageRecord
from .store import Store, new_run_id


def rasterise(store: Store, sha: str, dpi: int = 300, grayscale: bool = True) -> PageManifest:
    """Render every page of a stored PDF to PNG. Cached by (source hash, dpi, colour mode)."""
    if cached := store.load_manifest(sha, dpi, grayscale):
        return cached

    out_dir = store.pages_dir(sha, dpi, grayscale)
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf = pdfium.PdfDocument(store.blob_path(sha))
    try:
        pages = []
        for i in range(len(pdf)):
            page = pdf[i]
            textpage = page.get_textpage()
            text_chars = len(textpage.get_text_range().strip())
            image = page.render(scale=dpi / 72, grayscale=grayscale).to_pil()
            textpage.close()
            page.close()

            buf = io.BytesIO()
            image.save(buf, format="PNG", dpi=(dpi, dpi))
            data = buf.getvalue()
            path = out_dir / f"p{i + 1:04d}.png"
            path.write_bytes(data)
            pages.append(PageImage(
                page=i + 1, path=store.rel(path), width_px=image.width, height_px=image.height,
                sha256=hashlib.sha256(data).hexdigest(), text_layer_chars=text_chars,
            ))
    finally:
        pdf.close()

    if not pages:
        raise ValueError(f"PDF {sha} has no pages")
    manifest = PageManifest(source_sha256=sha, dpi=dpi, grayscale=grayscale, page_count=len(pages), pages=pages)
    store.save_manifest(manifest)
    return manifest


def ingest(
    store: Store,
    pdf_path: Path,
    document_id: str,
    version_label: str,
    *,
    dictionary_version: int,
    effective_date: date | None = None,
    input_variant: Literal["clean", "degraded"] = "clean",
    dpi: int = 300,
    grayscale: bool = True,
) -> RunRecord:
    """Store the input by content hash, rasterise it, and write a run record."""
    t0 = datetime.now(timezone.utc)
    sha = store.put_pdf(pdf_path)
    t1 = datetime.now(timezone.utc)
    manifest = rasterise(store, sha, dpi=dpi, grayscale=grayscale)
    t2 = datetime.now(timezone.utc)

    scanned = sum(1 for p in manifest.pages if p.text_layer_chars == 0)
    run = RunRecord(
        run_id=new_run_id(t0), document_id=document_id, version_label=version_label,
        effective_date=effective_date, input_variant=input_variant, source_sha256=sha,
        source_filename=pdf_path.name, page_count=manifest.page_count,
        manifest_path=store.rel(store.pages_dir(sha, dpi, grayscale) / "manifest.json"),
        dictionary_version=dictionary_version, pipeline_version=__version__, created_at=t0,
        stages=[
            StageRecord(stage="ingest", status="ok", started_at=t0, finished_at=t1, detail=f"sha256={sha}"),
            StageRecord(stage="rasterise", status="ok", started_at=t1, finished_at=t2,
                        detail=f"{manifest.page_count} pages at {dpi} DPI; {scanned} with no text layer"),
        ],
    )
    store.save_run(run)
    return run
