"""The whole pipeline end to end with a fake on-prem model: no network, no API spend."""
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from credit_extract.cli import app
from credit_extract.config import get_settings
from credit_extract.contracts import EnvelopeBlock, EnvelopeField, PageEnvelope
from credit_extract.gateway import ModelResponse, Usage

REPO = Path(__file__).resolve().parents[1]


class FakeQwen:
    def __init__(self, settings, key):
        self.spec = settings.load_models().models[key].model_copy(update={"enabled": True})

    def extract(self, images, prompt):
        if "Known fields" in prompt:  # pass 2
            fields = [EnvelopeField(name="applicable_margin (Level 1, Term SOFR)", raw_value="2.25%",
                                    source_text="The Applicable Margin shall be 2.25% per annum.", confidence=0.9)]
            env = PageEnvelope(blocks=[], fields=fields, tables=[], parties=[])
        else:
            env = PageEnvelope(
                blocks=[EnvelopeBlock(kind="clause", text="The Applicable Margin shall be 2.25% per annum.")],
                fields=[EnvelopeField(name="Applicable Margin", raw_value="2.25%", block_index=0,
                                      source_text="The Applicable Margin shall be 2.25% per annum.", confidence=0.95)],
                tables=[], parties=[])
        return ModelResponse(model_key="onprem_candidate", envelope=env, stop_reason="stop",
                             usage=Usage(input_tokens=100, output_tokens=50))


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")
def test_extract_end_to_end_with_fake_onprem_model(tmp_path, monkeypatch, scanned_pdf):
    monkeypatch.setenv("CAE_ROOT", str(REPO))
    monkeypatch.setenv("CAE_STORE_DIR", str(tmp_path / "store"))
    monkeypatch.setenv("CAE_ENVIRONMENT", "prod")  # frontier models refused; on-prem allowed
    get_settings.cache_clear()
    from credit_extract import config, pass1, pass2

    orig_model = config.Settings.model
    monkeypatch.setattr(config.Settings, "model",
                        lambda self, key: orig_model(self, key) if key != "onprem_candidate"
                        else self.load_models().models[key].model_copy(update={"enabled": True}))
    monkeypatch.setattr(pass1, "get_adapter", lambda s, k: FakeQwen(s, k))
    monkeypatch.setattr(pass2, "get_adapter", lambda s, k: FakeQwen(s, k))
    try:
        r = CliRunner().invoke(app, ["extract", str(scanned_pdf), "--document-id", "doc", "--version-label", "v1",
                                     "--model", "onprem_candidate", "--ocr-workers", "1"])
        assert r.exit_code == 0, r.output + repr(r.exception)
        assert "[8/8] publish" in r.output and "3/3 pages ok" in r.output
        assert "verified at source" in r.output
        refused = CliRunner().invoke(app, ["extract", str(scanned_pdf), "--document-id", "doc", "--version-label", "v1",
                                           "--model", "frontier"])
        assert refused.exit_code != 0 and "not prod_allowed" in str(refused.exception)
    finally:
        get_settings.cache_clear()
