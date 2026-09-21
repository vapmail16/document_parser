import shutil
from pathlib import Path

import pytest

from credit_extract.ingest import ingest
from credit_extract.ocr import OcrPage, OcrToken, TesseractEngine, probe, run_ocr, word_f1
from credit_extract.store import Store


class FakeEngine:
    name = "fake 1.0"

    def __init__(self, text: str):
        self.text, self.calls = text, 0

    def ocr(self, image_path: Path) -> list[OcrToken]:
        self.calls += 1
        return [OcrToken(text=w, conf=0.9, bbox=(0.1, 0.1, 0.2, 0.12), block=1, line=0)
                for w in self.text.split()]


def page(text: str) -> OcrPage:
    return OcrPage(page=1, image_sha256="0" * 64, engine="fake",
                   tokens=FakeEngine(text).ocr(Path("x")))


def test_word_f1_is_order_insensitive_and_punctuation_tolerant():
    assert word_f1("The Applicable Margin is 2.25%.", "applicable margin the is 2.25%") == 1.0
    assert word_f1("", "") == 1.0
    assert word_f1("alpha beta", "gamma delta") == 0.0
    assert 0.5 < word_f1("the margin is 2.25%", "the margin is 2.75%") < 1.0


def test_probe_classifies_text_layer():
    assert probe("", page("anything")) == ("none", None)
    status, f1 = probe("Total Commitments $50,000,000", page("Total Commitments $50,000,000"))
    assert status == "trusted" and f1 == 1.0
    status, _ = probe("Total Commitments $50,000,000", page("Something else entirely here"))
    assert status == "untrusted"


def test_run_ocr_caches_and_logs_stage(tmp_path, text_pdf):
    store = Store(tmp_path / "store")
    run = ingest(store, text_pdf, "doc", "v1", dictionary_version=1)
    engine = FakeEngine("Total Commitments: $50,000,000")

    report = run_ocr(store, run.run_id, engine, reference_pdf=text_pdf)
    assert report.pages[0].text_layer == "trusted"
    assert report.pages[0].reference_f1 == 1.0
    assert (store.root / "reports" / run.run_id / "ocr.json").exists()

    run_ocr(store, run.run_id, engine)
    assert engine.calls == 1  # second run served from the per-image cache
    assert [s.stage for s in store.load_run(run.run_id).stages] == ["ingest", "rasterise", "ocr", "ocr"]


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")
def test_tesseract_reads_a_rendered_page(tmp_path, scanned_pdf):
    store = Store(tmp_path / "store")
    run = ingest(store, scanned_pdf, "doc", "v1", dictionary_version=1)
    report = run_ocr(store, run.run_id, TesseractEngine())
    first = store.root / "ocr"
    assert report.pages[0].tokens > 5
    cached = next(first.rglob("*.json"))
    text = OcrPage.model_validate_json(cached.read_text()).text.casefold()
    assert "applicable" in text and "margin" in text
    for t in OcrPage.model_validate_json(cached.read_text()).tokens:
        x0, y0, x1, y1 = t.bbox
        assert 0 <= x0 <= x1 <= 1 and 0 <= y0 <= y1 <= 1
