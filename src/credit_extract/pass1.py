"""Stage 3, Pass 1 — open-ended VLM extraction per page/band (spec §6, §8).

Failure policy (a page is never silently dropped):
  invalid output / API error -> retry the band once
  truncated (max_tokens)     -> re-plan the page with twice as many bands, up to 2 escalations
  still failing              -> page status "failed", recorded and surfaced
"""

import hashlib
import io
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Literal

from PIL import Image
from pydantic import BaseModel

from .config import ModelSpec, Settings
from .contracts import PageManifest, StageRecord
from .gateway import ModelResponse, get_adapter
from .ocr import OcrPage
from .prompts import load_prompt
from .store import Store
from .tiling import Band, plan_bands

PROMPT_NAME = "pass1_extraction"
MAX_ESCALATIONS = 2


class BandAttempt(BaseModel):
    band: Band
    bands_on_page: int
    cache_key: str
    cached: bool
    response: ModelResponse


class PagePass1(BaseModel):
    page: int
    model_key: str
    prompt_version: str
    prompt_sha256: str
    status: Literal["ok", "failed"]
    bands: list[BandAttempt]  # the successful set covering the page, in order
    attempts: list[BandAttempt]  # every call made, including failures (audit)

    @property
    def cost_usd(self) -> float:
        return sum(a.response.usage.cost_usd or 0 for a in self.attempts if not a.cached)


class Pass1Summary(BaseModel):
    run_id: str
    model_key: str
    prompt_version: str
    pages: list[int]
    ok: int
    failed: list[int]
    calls: int
    cached_calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    cost_per_page_usd: float
    projected_doc_cost_usd: float
    mean_latency_per_call_s: float


def _band_image(page: Image.Image, band: Band) -> Image.Image:
    return page.crop((0, band.y0, page.width, band.y1))


def _cache_key(img: Image.Image, spec: ModelSpec, prompt: str) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    h = hashlib.sha256(buf.getvalue())
    h.update(json.dumps({"model": spec.model_name, "params": spec.params, "max_out": spec.max_output_tokens},
                        sort_keys=True).encode())
    h.update(prompt.encode())
    return h.hexdigest()


def _cached_ocr(store: Store, image_sha: str) -> OcrPage | None:
    found = sorted((store.root / "ocr" / image_sha).glob("*.json")) if (store.root / "ocr" / image_sha).exists() else []
    return OcrPage.model_validate_json(found[0].read_text()) if found else None


def extract_page(store: Store, settings: Settings, adapter, model_key: str, manifest: PageManifest,
                 page_no: int, prompt_version: str | None = None) -> PagePass1:
    spec: ModelSpec = adapter.spec
    template = load_prompt(settings.path(settings.prompts_dir), PROMPT_NAME, prompt_version)
    base_prompt = template.render()
    meta = manifest.pages[page_no - 1]
    page_img = Image.open(store.abs(meta.path))
    page_img.load()
    ocr = _cached_ocr(store, meta.sha256)

    attempts: list[BandAttempt] = []
    max_tiles = spec.tiling.max_tiles_per_page
    for _ in range(MAX_ESCALATIONS + 1):
        tiled = spec.model_copy(update={"tiling": spec.tiling.model_copy(update={"max_tiles_per_page": max_tiles})})
        bands = plan_bands(page_img.width, page_img.height, tiled, dpi=manifest.dpi, ocr=ocr)
        good: list[BandAttempt] = []
        truncated = False
        for band in bands:
            img = _band_image(page_img, band)
            where = "the whole page" if len(bands) == 1 else f"band {band.index + 1} of {len(bands)}, top to bottom"
            prompt = f"{base_prompt}\n\nThis image: page {page_no}, {where}."
            key = _cache_key(img, spec, prompt)
            cache = store.root / "cache" / "extract" / f"{key}.json"

            for retry in range(2):
                if cache.exists():
                    att = BandAttempt(band=band, bands_on_page=len(bands), cache_key=key, cached=True,
                                      response=ModelResponse.model_validate_json(cache.read_text()))
                else:
                    resp = adapter.extract([img], prompt)
                    att = BandAttempt(band=band, bands_on_page=len(bands), cache_key=key, cached=False, response=resp)
                    if resp.ok:
                        cache.parent.mkdir(parents=True, exist_ok=True)
                        cache.write_text(resp.model_dump_json())
                attempts.append(att)
                if att.response.ok or att.response.failure in ("truncated", "refusal", "blocked"):
                    break
            if att.response.ok:
                good.append(att)
            else:
                truncated = truncated or att.response.failure == "truncated"
                break

        if len(good) == len(bands):
            return PagePass1(page=page_no, model_key=model_key, prompt_version=template.version,
                             prompt_sha256=template.sha256, status="ok", bands=good, attempts=attempts)
        if not truncated:
            break
        max_tiles *= 2

    return PagePass1(page=page_no, model_key=model_key, prompt_version=template.version,
                     prompt_sha256=template.sha256, status="failed", bands=[], attempts=attempts)


def pass1_dir(store: Store, run_id: str, model_key: str, prompt_version: str):
    return store.root / "runs" / run_id / "pass1" / model_key / prompt_version


def latest_pass1_version(store: Store, run_id: str, model_key: str) -> str:
    base = store.root / "runs" / run_id / "pass1" / model_key
    versions = sorted((int(d.name[1:]) for d in base.glob("v*") if d.is_dir() and d.name[1:].isdigit()))
    if not versions:
        raise FileNotFoundError(f"no pass-1 output for model {model_key!r}; run `cae pass1 {run_id}` first")
    return f"v{versions[-1]}"


def run_pass1(store: Store, settings: Settings, run_id: str, model_key: str,
              pages: list[int] | None = None, prompt_version: str | None = None) -> Pass1Summary:
    t0 = datetime.now(timezone.utc)
    run = store.load_run(run_id)
    manifest = PageManifest.model_validate_json(store.abs(run.manifest_path).read_text())
    pages = pages or list(range(1, manifest.page_count + 1))
    bad = [p for p in pages if not 1 <= p <= manifest.page_count]
    if bad:
        raise ValueError(f"pages out of range 1..{manifest.page_count}: {bad}")

    adapter = get_adapter(settings, model_key)
    version = load_prompt(settings.path(settings.prompts_dir), PROMPT_NAME, prompt_version).version
    out_dir = pass1_dir(store, run_id, model_key, version)  # one directory per prompt version: never overwritten
    out_dir.mkdir(parents=True, exist_ok=True)

    def one(p: int) -> PagePass1:
        result = extract_page(store, settings, adapter, model_key, manifest, p, version)
        (out_dir / f"p{p:04d}.json").write_text(result.model_dump_json(indent=1))
        return result

    with ThreadPoolExecutor(max_workers=adapter.spec.concurrency) as pool:
        results = list(pool.map(one, pages))

    fresh = [a for r in results for a in r.attempts if not a.cached]
    cost = sum(a.response.usage.cost_usd or 0 for a in fresh)
    summary = Pass1Summary(
        run_id=run_id, model_key=model_key, prompt_version=version, pages=pages,
        ok=sum(r.status == "ok" for r in results), failed=[r.page for r in results if r.status == "failed"],
        calls=sum(len(r.attempts) for r in results), cached_calls=sum(a.cached for r in results for a in r.attempts),
        input_tokens=sum(a.response.usage.input_tokens for a in fresh),
        output_tokens=sum(a.response.usage.output_tokens for a in fresh),
        cost_usd=round(cost, 4),
        cost_per_page_usd=round(cost / len(pages), 4) if fresh else 0.0,
        projected_doc_cost_usd=round(cost / len(pages) * manifest.page_count, 2) if fresh else 0.0,
        mean_latency_per_call_s=round(sum(a.response.latency_s for a in fresh) / len(fresh), 1) if fresh else 0.0,
    )
    store.save_json(f"reports/{run_id}/pass1-{model_key}-{version}-{t0:%Y%m%dT%H%M%SZ}.json", summary)  # one per invocation
    store.append_stage(run_id, StageRecord(
        stage=f"pass1:{model_key}:{version}", status="ok" if not summary.failed else "failed",
        started_at=t0, finished_at=datetime.now(timezone.utc),
        detail=f"{summary.ok}/{len(pages)} pages ok; {summary.calls} calls ({summary.cached_calls} cached); "
               f"${summary.cost_usd:.2f}" + (f"; failed pages {summary.failed}" if summary.failed else ""),
    ))
    return summary
