import hashlib

import numpy as np
import pypdfium2 as pdfium
from PIL import Image

from credit_extract.degrade import DegradeProfile, degrade_page, degrade_pdf
from credit_extract.ingest import ingest, rasterise
from credit_extract.store import Store, sha256_file


def test_ingest_stores_rasterises_and_records(tmp_path, scanned_pdf):
    store = Store(tmp_path / "store")
    run = ingest(store, scanned_pdf, "doc", "v1", dictionary_version=1)

    assert run.page_count == 3
    assert store.blob_path(run.source_sha256).exists()
    assert store.load_run(run.run_id) == run
    assert [s.stage for s in run.stages] == ["ingest", "rasterise"]

    manifest = rasterise(store, run.source_sha256)  # served from cache
    assert [p.page for p in manifest.pages] == [1, 2, 3]
    for p in manifest.pages:
        data = store.abs(p.path).read_bytes()
        assert hashlib.sha256(data).hexdigest() == p.sha256
        assert p.text_layer_chars == 0  # scan: no text layer
        with Image.open(store.abs(p.path)) as img:
            assert img.size == (p.width_px, p.height_px)
            # 4.25x5.5in at 300 DPI; pdfium may round by a pixel
            assert abs(p.width_px - 1275) <= 1 and abs(p.height_px - 1650) <= 1
            assert img.mode == "L"


def test_reingest_is_content_addressed_and_never_overwrites(tmp_path, scanned_pdf):
    store = Store(tmp_path / "store")
    a = ingest(store, scanned_pdf, "doc", "v1", dictionary_version=1)
    copy = tmp_path / "renamed.pdf"
    copy.write_bytes(scanned_pdf.read_bytes())
    b = ingest(store, copy, "doc", "v1", dictionary_version=1)

    assert a.source_sha256 == b.source_sha256
    assert a.run_id != b.run_id  # re-runs are new records
    assert len(list((store.root / "blobs").rglob("*.pdf"))) == 1
    assert len(list((store.root / "runs").glob("*.json"))) == 2


def test_text_layer_probe_detects_embedded_text(tmp_path, text_pdf):
    store = Store(tmp_path / "store")
    run = ingest(store, text_pdf, "doc", "v1", dictionary_version=1)
    manifest = rasterise(store, run.source_sha256)
    assert manifest.pages[0].text_layer_chars > 0


def test_degrade_page_is_deterministic_and_size_preserving():
    img = Image.new("L", (600, 800), 255)
    profile = DegradeProfile(stamp_prob=1.0)
    a = degrade_page(img, np.random.default_rng([7, 0]), profile, dpi=300)
    b = degrade_page(img, np.random.default_rng([7, 0]), profile, dpi=300)
    c = degrade_page(img, np.random.default_rng([8, 0]), profile, dpi=300)
    assert a.size == img.size
    assert a.tobytes() == b.tobytes()
    assert a.tobytes() != c.tobytes()


def test_degraded_pdf_is_image_only_and_reproducible(tmp_path, text_pdf):
    out1, out2 = tmp_path / "d1.pdf", tmp_path / "d2.pdf"
    assert degrade_pdf(text_pdf, out1, seed=3) == 1
    degrade_pdf(text_pdf, out2, seed=3)

    store = Store(tmp_path / "store")
    run = ingest(store, out1, "doc", "v1", dictionary_version=1, input_variant="degraded")
    manifest = rasterise(store, run.source_sha256)
    assert manifest.pages[0].text_layer_chars == 0  # text layer gone, like a real scan

    r1 = pdfium.PdfDocument(out1)[0].render(scale=1).to_pil().tobytes()
    r2 = pdfium.PdfDocument(out2)[0].render(scale=1).to_pil().tobytes()
    assert r1 == r2
    assert sha256_file(out1) != sha256_file(text_pdf)
