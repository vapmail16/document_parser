from pathlib import Path

import pytest

from credit_extract.config import ModelSpec, Settings
from credit_extract.contracts import PageEnvelope, PageManifest
from credit_extract.gateway import ModelResponse, Usage
from credit_extract.ingest import ingest
from credit_extract.pass1 import extract_page
from credit_extract.store import Store

REPO = Path(__file__).resolve().parents[1]
EMPTY = PageEnvelope(blocks=[], fields=[], tables=[], parties=[])


class FakeAdapter:
    """Scripted responses: a list of failure names (or None for success), consumed in order."""

    def __init__(self, script, long_edge=2576, max_pixels=None):
        self.spec = ModelSpec(role="frontier", provider="anthropic", model_name="fake", prod_allowed=False,
                              supports_sampling_params=False, structured_output=True, max_output_tokens=100,
                              image={"max_long_edge_px": long_edge, "max_pixels": max_pixels},
                              tiling={"max_tiles_per_page": 1}, price_in_per_mtok=1, price_out_per_mtok=1)
        self.script, self.calls = list(script), []

    def extract(self, images, prompt):
        self.calls.append((images[0].size, prompt))
        failure = self.script.pop(0) if self.script else None
        return ModelResponse(model_key="fake", failure=failure, envelope=None if failure else EMPTY,
                             usage=Usage(input_tokens=10, output_tokens=5, cost_usd=0.001))


@pytest.fixture
def setup(tmp_path, scanned_pdf):
    store = Store(tmp_path / "store")
    run = ingest(store, scanned_pdf, "doc", "v1", dictionary_version=1)
    manifest = PageManifest.model_validate_json(store.abs(run.manifest_path).read_text())
    return store, Settings(root=REPO), manifest


def test_success_single_band_then_cached(setup):
    store, settings, manifest = setup
    adapter = FakeAdapter([None])
    r = extract_page(store, settings, adapter, "fake", manifest, 1)
    assert r.status == "ok" and len(r.bands) == 1 and len(adapter.calls) == 1
    assert "page 1, the whole page" in adapter.calls[0][1]

    again = extract_page(store, settings, adapter, "fake", manifest, 1)
    assert again.status == "ok" and again.bands[0].cached and len(adapter.calls) == 1
    assert again.cost_usd == 0


def test_invalid_output_retried_once(setup):
    store, settings, manifest = setup
    adapter = FakeAdapter(["invalid_output", None])
    r = extract_page(store, settings, adapter, "fake", manifest, 2)
    assert r.status == "ok" and len(r.attempts) == 2


def test_truncation_escalates_to_more_bands(setup):
    store, settings, manifest = setup
    # Small budget so the page can be split; first call truncates, then both bands succeed.
    adapter = FakeAdapter(["truncated", None, None], long_edge=1400, max_pixels=1_400_000)
    r = extract_page(store, settings, adapter, "fake", manifest, 1)
    assert r.status == "ok"
    assert len(r.bands) == 2 and r.bands[0].bands_on_page == 2
    assert "band 1 of 2" in adapter.calls[1][1] and "band 2 of 2" in adapter.calls[2][1]


def test_persistent_failure_is_recorded_not_dropped(setup):
    store, settings, manifest = setup
    adapter = FakeAdapter(["api_error", "api_error"])
    r = extract_page(store, settings, adapter, "fake", manifest, 3)
    assert r.status == "failed" and r.bands == [] and len(r.attempts) == 2
    assert not list((store.root / "cache").rglob("*.json")) if (store.root / "cache").exists() else True


@pytest.mark.parametrize("failure", ["refusal", "blocked"])
def test_refusal_and_blocked_output_are_not_retried(setup, failure):
    store, settings, manifest = setup
    adapter = FakeAdapter([failure])
    r = extract_page(store, settings, adapter, "fake", manifest, 3)
    assert r.status == "failed" and len(adapter.calls) == 1
