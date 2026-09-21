"""Stage 3, Pass 2 — schema-hinted sweep (spec §8).

For each band pass 1 extracted, ask the model which KNOWN dictionary fields are in the image,
named by their dictionary key (plus qualifier for repeatable fields). Pass 1 stays open-ended and
never sees the dictionary; pass 2 only fills gaps and gives a second reading to compare against.

Same bands as pass 1 (so both passes saw identical pixels), same cache, same audit records.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from PIL import Image
from pydantic import BaseModel

from .config import Settings
from .contracts import DictionarySnapshot, FieldDictionaryEntry, PageManifest, StageRecord
from .gateway import ModelResponse, get_adapter
from .pass1 import BandAttempt, PagePass1, _band_image, _cache_key, latest_pass1_version, pass1_dir
from .prompts import load_prompt
from .store import Store

PROMPT_NAME = "pass2_schema_hint"


def _qualifier_hint(entry: FieldDictionaryEntry) -> str:
    parts = []
    for r in entry.instance_rules:
        if r.kind == "level":
            parts.append("pricing level, e.g. Level 2")
        elif r.kind == "choice":
            opts = [terms[0] for terms in r.choices.values()]
            parts.append("one of: " + ", ".join(opts) + (f" (omit if {r.default})" if r.default else ""))
        else:
            parts.append("the lender's name as printed" if r.slug_from == "qualifier" else "not needed")
    return f" Qualifier: {'; '.join(p for p in parts if p != 'not needed')}." if any(
        p != "not needed" for p in parts) else ""


def known_fields_text(snap: DictionarySnapshot) -> str:
    lines = []
    for e in snap.entries:
        meaning = (e.business_meaning or "").rstrip(".")
        guidance = f" Value: {e.value_guidance.rstrip('.')}." if e.value_guidance else ""
        lines.append(f"- {e.canonical_name} — {e.display_name}: {meaning}.{guidance}{_qualifier_hint(e)}")
    return "\n".join(lines)


class Pass2Summary(BaseModel):
    run_id: str
    model_key: str
    prompt_version: str
    pass1_prompt_version: str
    dictionary_version: int
    pages: list[int]
    ok: int
    failed: list[int]
    calls: int
    cached_calls: int
    fields_reported: int
    cost_usd: float


def sweep_page(store: Store, settings: Settings, adapter, model_key: str, manifest: PageManifest,
               p1: PagePass1, snap: DictionarySnapshot, prompt_version: str | None) -> PagePass1:
    template = load_prompt(settings.path(settings.prompts_dir), PROMPT_NAME, prompt_version)
    known = known_fields_text(snap)
    page_img = Image.open(store.abs(manifest.pages[p1.page - 1].path))
    page_img.load()
    attempts, good = [], []
    for b in p1.bands:
        img = _band_image(page_img, b.band)
        already = "\n".join(f"- {f.name}" for f in b.response.envelope.fields) or "(none)"
        where = "the whole page" if b.bands_on_page == 1 else f"band {b.band.index + 1} of {b.bands_on_page}, top to bottom"
        prompt = template.render(known_fields=known, already_extracted=already) + \
            f"\n\nThis image: page {p1.page}, {where}."
        key = _cache_key(img, adapter.spec, prompt)
        cache = store.root / "cache" / "extract" / f"{key}.json"
        for _ in range(2):
            if cache.exists():
                att = BandAttempt(band=b.band, bands_on_page=b.bands_on_page, cache_key=key, cached=True,
                                  response=ModelResponse.model_validate_json(cache.read_text()))
            else:
                resp = adapter.extract([img], prompt)
                att = BandAttempt(band=b.band, bands_on_page=b.bands_on_page, cache_key=key, cached=False,
                                  response=resp)
                if resp.ok:
                    cache.parent.mkdir(parents=True, exist_ok=True)
                    cache.write_text(resp.model_dump_json())
            attempts.append(att)
            if att.response.ok or att.response.failure in ("truncated", "refusal", "blocked"):
                break
        if not att.response.ok:
            return PagePass1(page=p1.page, model_key=model_key, prompt_version=template.version,
                             prompt_sha256=template.sha256, status="failed", bands=[], attempts=attempts)
        good.append(att)
    return PagePass1(page=p1.page, model_key=model_key, prompt_version=template.version,
                     prompt_sha256=template.sha256, status="ok", bands=good, attempts=attempts)


def pass2_tag(prompt_version: str, dictionary_version: int) -> str:
    # The field list (and its value guidance) comes from the dictionary, so it is part of the sweep's identity.
    return f"{prompt_version}+d{dictionary_version}"


def pass2_dir(store: Store, run_id: str, model_key: str, pass1_version: str, tag: str):
    """runs/<run>/pass2/<model>/<pass-1 version>+<pass-2 prompt>+d<dictionary>: never overwritten."""
    return store.root / "runs" / run_id / "pass2" / model_key / f"{pass1_version}+{tag}"


def run_pass2(store: Store, settings: Settings, run_id: str, model_key: str, snap: DictionarySnapshot,
              pages: list[int] | None = None, prompt_version: str | None = None,
              pass1_version: str | None = None) -> Pass2Summary:
    t0 = datetime.now(timezone.utc)
    run = store.load_run(run_id)
    manifest = PageManifest.model_validate_json(store.abs(run.manifest_path).read_text())
    p1_version = pass1_version or latest_pass1_version(store, run_id, model_key)
    p1_results = {r.page: r for r in (PagePass1.model_validate_json(f.read_text())
                                      for f in sorted(pass1_dir(store, run_id, model_key, p1_version).glob("p*.json")))}
    targets = [p for p in (pages or sorted(p1_results)) if p in p1_results and p1_results[p].status == "ok"]
    version = load_prompt(settings.path(settings.prompts_dir), PROMPT_NAME, prompt_version).version
    tag = pass2_tag(version, snap.version)
    out_dir = pass2_dir(store, run_id, model_key, p1_version, tag)
    out_dir.mkdir(parents=True, exist_ok=True)
    adapter = get_adapter(settings, model_key)

    def one(p: int) -> PagePass1:
        r = sweep_page(store, settings, adapter, model_key, manifest, p1_results[p], snap, version)
        (out_dir / f"p{p:04d}.json").write_text(r.model_dump_json(indent=1))
        return r

    with ThreadPoolExecutor(max_workers=adapter.spec.concurrency) as pool:
        results = list(pool.map(one, targets))
    fresh = [a for r in results for a in r.attempts if not a.cached]
    summary = Pass2Summary(
        run_id=run_id, model_key=model_key, prompt_version=version, pass1_prompt_version=p1_version,
        dictionary_version=snap.version, pages=targets, ok=sum(r.status == "ok" for r in results),
        failed=[r.page for r in results if r.status == "failed"], calls=sum(len(r.attempts) for r in results),
        cached_calls=sum(a.cached for r in results for a in r.attempts),
        fields_reported=sum(len(b.response.envelope.fields) for r in results for b in r.bands),
        cost_usd=round(sum(a.response.usage.cost_usd or 0 for a in fresh), 4))
    store.save_json(f"reports/{run_id}/pass2-{model_key}-{p1_version}+{tag}-{t0:%Y%m%dT%H%M%SZ}.json", summary)
    store.append_stage(run_id, StageRecord(
        stage=f"pass2:{model_key}:{p1_version}+{tag}", status="ok" if not summary.failed else "failed",
        started_at=t0, finished_at=datetime.now(timezone.utc),
        detail=f"{summary.ok}/{len(targets)} pages; {summary.fields_reported} known-field reports; "
               f"dictionary v{snap.version}; ${summary.cost_usd:.2f}"))
    return summary
