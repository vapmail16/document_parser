"""CLI (spec §12). Run modes not yet built exit with code 2 and name their milestone."""

from datetime import datetime

from PIL import Image
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from . import __version__
from .config import get_settings
from .assemble import run_assemble
from .compare import compare as run_compare
from .degrade import degrade_pdf
from .enrich import EnrichmentBatch, candidates as enrich_candidates, enrich as run_enrich
from .diff import diff as run_diff
from .grounding import run_grounding
from .handoff import HandoffPayload, build_payload, emit, export_audit, file_sink
from .approve import add_entries as add_dictionary_entries, approve as approve_aliases
from .dictionary import list_versions, load_snapshot, save_snapshot
from .ingest import ingest as run_ingest
from .lineage import as_of, load_index, publish as publish_record
from .ocr import TesseractEngine, run_ocr
from .contracts import DocumentExtraction, PageManifest
from .defined_terms import resolve as resolve_terms
from .ocr import OcrPage
from .evaluate import evaluate, is_ai_verifier, load_golden
from .pass1 import latest_pass1_version, run_pass1
from .pass2 import run_pass2
from .reconcile import reconcile as run_reconcile
from .review import IngestResult, ingest as review_ingest_file, write_export as write_review_export
from .prompts import PROMPT_NAMES, available_versions
from .products.mcp_server import serve_stdio
from .products.model import write_bundle
from .products.rest import serve as serve_products
from .products.access import open_access
from .store import Store

app = typer.Typer(no_args_is_help=True, add_completion=False, help=__doc__)
dict_app = typer.Typer(no_args_is_help=True, help="Inspect / review the field dictionary.")
review_app = typer.Typer(no_args_is_help=True, help="Human-review export and re-ingest (spec §14).")
app.add_typer(dict_app, name="dict")
app.add_typer(review_app, name="review")


def _not_built(milestone: int) -> NoReturn:
    typer.echo(f"not implemented yet — milestone {milestone} (spec §20)", err=True)
    raise typer.Exit(code=2)


@app.command()
def version() -> None:
    """Print the pipeline version."""
    typer.echo(__version__)


@app.command()
def models() -> None:
    """List models configured in models.yaml."""
    settings = get_settings()
    cfg = settings.load_models()
    typer.echo(f"environment: {settings.environment}   default: {cfg.default_model}   eval_pair: {list(cfg.eval_pair)}")
    for key, spec in cfg.models.items():
        state = "enabled" if spec.enabled else "disabled"
        prod = "prod-allowed" if spec.prod_allowed else "POC only"
        typer.echo(f"  {key:<18} {spec.role:<9} {state:<9} {prod:<13} {spec.provider}:{spec.model_name}")


@app.command()
def prompts() -> None:
    """List prompt templates and their versions."""
    prompts_dir = get_settings().path(get_settings().prompts_dir)
    for name in PROMPT_NAMES:
        typer.echo(f"  {name:<20} {', '.join(available_versions(prompts_dir, name)) or '(none)'}")


@app.command()
def ingest(
    pdf: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    document_id: Annotated[str, typer.Option()],
    version_label: Annotated[str, typer.Option()],
    effective_date: Annotated[datetime | None, typer.Option(formats=["%Y-%m-%d"])] = None,
    variant: Annotated[str, typer.Option(help="clean | degraded")] = "clean",
    dictionary_version: Annotated[int | None, typer.Option(help="pin a snapshot; default latest")] = None,
) -> None:
    """Stages 0-1: store the PDF by content hash and rasterise every page."""
    if variant not in ("clean", "degraded"):
        raise typer.BadParameter("variant must be 'clean' or 'degraded'")
    settings = get_settings()
    snap = load_snapshot(settings.path(settings.dictionary_dir), dictionary_version)
    run = run_ingest(
        Store(settings.path(settings.store_dir)), pdf, document_id, version_label,
        dictionary_version=snap.version, effective_date=effective_date.date() if effective_date else None,
        input_variant=variant, dpi=settings.rasterise_dpi, grayscale=settings.rasterise_grayscale,
    )
    typer.echo(f"run {run.run_id}: {run.page_count} pages, sha256 {run.source_sha256[:12]}…, dictionary v{snap.version}")
    for stage in run.stages:
        typer.echo(f"  {stage.stage:<10} {stage.status:<4} {stage.detail or ''}")


@app.command()
def ocr(
    run_id: Annotated[str, typer.Argument()],
    workers: Annotated[int, typer.Option(help="parallel tesseract processes")] = 4,
    reference: Annotated[Path | None, typer.Option(exists=True, dir_okay=False,
                         help="PDF whose text layer is ground truth (e.g. clean original of a degraded run)")] = None,
) -> None:
    """Stage 2: independent OCR + text-layer probe for an ingested run."""
    settings = get_settings()
    report = run_ocr(Store(settings.path(settings.store_dir)), run_id, TesseractEngine(),
                     workers=workers, reference_pdf=reference)
    counts = {k: sum(1 for p in report.pages if p.text_layer == k) for k in ("trusted", "untrusted", "none")}
    typer.echo(f"{report.engine}: {len(report.pages)} pages; text layer {counts}")
    typer.echo(f"  mean OCR confidence {report.mean('mean_conf'):.3f}")
    if (f1 := report.mean("text_layer_f1")) is not None:
        typer.echo(f"  mean text-layer vs OCR word-F1 {f1:.3f}")
    if (ref := report.mean("reference_f1")) is not None:
        worst = sorted((p for p in report.pages if p.reference_f1 is not None), key=lambda p: p.reference_f1)[:5]
        typer.echo(f"  mean reference vs OCR word-F1 {ref:.3f}; worst pages: "
                   + ", ".join(f"p{p.page}={p.reference_f1:.2f}" for p in worst))


def _parse_pages(spec: str | None) -> list[int] | None:
    if not spec:
        return None
    pages: list[int] = []
    for part in spec.split(","):
        a, _, b = part.strip().partition("-")
        pages += list(range(int(a), int(b or a) + 1))
    return sorted(set(pages))


@app.command()
def pass1(
    run_id: Annotated[str, typer.Argument()],
    model: Annotated[str | None, typer.Option(help="models.yaml key; default default_model")] = None,
    pages: Annotated[str | None, typer.Option(help="e.g. 1,15,17-18; default all pages")] = None,
    prompt_version: Annotated[str | None, typer.Option(help="pass-1 prompt version, e.g. v1; default latest")] = None,
) -> None:
    """Stage 3 Pass 1: open-ended VLM extraction (spends API credit on frontier models)."""
    settings = get_settings()
    key = model or settings.load_models().default_model
    s = run_pass1(Store(settings.path(settings.store_dir)), settings, run_id, key, _parse_pages(pages), prompt_version)
    typer.echo(f"{key} (prompt {s.prompt_version}): {s.ok}/{len(s.pages)} pages ok; {s.calls} calls ({s.cached_calls} cached); "
               f"{s.input_tokens:,} in / {s.output_tokens:,} out tokens")
    typer.echo(f"  cost ${s.cost_usd:.2f} (${s.cost_per_page_usd:.3f}/page -> ~${s.projected_doc_cost_usd:.2f} "
               f"for the whole document); {s.mean_latency_per_call_s}s per call")
    if s.failed:
        typer.echo(f"  FAILED pages: {s.failed}", err=True)
        raise typer.Exit(code=1)


@app.command()
def pass2(
    run_id: Annotated[str, typer.Argument()],
    model: Annotated[str | None, typer.Option(help="models.yaml key; default default_model")] = None,
    pages: Annotated[str | None, typer.Option(help="default: every page pass 1 extracted")] = None,
    dictionary_version: Annotated[int | None, typer.Option(
        help="dictionary snapshot for the field list; default = the version the run pinned")] = None,
) -> None:
    """Stage 3 Pass 2: schema-hinted sweep over pass-1 bands (spends API credit on frontier models)."""
    settings = get_settings()
    store = Store(settings.path(settings.store_dir))
    key = model or settings.load_models().default_model
    snap = load_snapshot(settings.path(settings.dictionary_dir), dictionary_version or store.load_run(run_id).dictionary_version)
    s = run_pass2(store, settings, run_id, key, snap, _parse_pages(pages))
    typer.echo(f"{key} (pass-2 prompt {s.prompt_version} over pass-1 {s.pass1_prompt_version}, dictionary v{s.dictionary_version}): "
               f"{s.ok}/{len(s.pages)} pages ok; {s.calls} calls ({s.cached_calls} cached); "
               f"{s.fields_reported} known-field reports; ${s.cost_usd:.2f}")
    if s.failed:
        typer.echo(f"  FAILED pages: {s.failed}", err=True)
        raise typer.Exit(code=1)


@app.command()
def assemble(
    run_id: Annotated[str, typer.Argument()],
    model: Annotated[str | None, typer.Option(help="models.yaml key; default default_model")] = None,
    prompt_version: Annotated[str | None, typer.Option(help="pass-1 prompt version, e.g. v1; default latest")] = None,
    pass2: Annotated[str | None, typer.Option(help="pass-2 sweep to merge, e.g. v1+d2; default latest")] = None,
) -> None:
    """Stage 4: stitch pass-1 output into one document; per-page completeness (no model)."""
    settings = get_settings()
    key = model or settings.load_models().default_model
    _, r = run_assemble(Store(settings.path(settings.store_dir)), settings, run_id, key, prompt_version, pass2)
    typer.echo(f"[prompt {r.prompt_version}] {r.document_kind}, {r.execution_status}: {r.sections} sections, {r.clauses} clauses, "
               f"{r.fields} fields, {r.tables} tables, {r.parties} parties")
    typer.echo(f"  pages {r.pages_by_status}; furniture patterns stripped: {len(r.furniture)}")
    if r.low_coverage_pages:
        typer.echo(f"  LOW COVERAGE (routed to review): {r.low_coverage_pages}")


@app.command()
def ground(
    run_id: Annotated[str, typer.Argument()],
    model: Annotated[str | None, typer.Option(help="models.yaml key; default default_model")] = None,
    truth: Annotated[Path | None, typer.Option(exists=True, dir_okay=False,
                     help="PDF with the true text (e.g. clean original) to score grounding decisions")] = None,
    prompt_version: Annotated[str | None, typer.Option(help="pass-1 prompt version, e.g. v1; default latest")] = None,
) -> None:
    """Stage 8: verify every value at its source; route to auto-accept or review (no model)."""
    settings = get_settings()
    key = model or settings.load_models().default_model
    _, r = run_grounding(Store(settings.path(settings.store_dir)), run_id, key, truth_pdf=truth,
                         prompt_version=prompt_version)
    typer.echo(f"[prompt {r.prompt_version}] {r.verified}/{r.fields} fields verified at source; {r.auto_accepted} auto-accepted, "
               f"{r.needs_review} to human review ({r.vision_noise} flagged OCR-noise)")
    typer.echo(f"  not verified because: {r.by_reason}")
    typer.echo(f"  witness used: {r.by_witness}; table cells grounded: {r.table_cells_grounded}")
    if r.table_checks_failed:
        typer.echo(f"  table checks FAILED: {r.table_checks_failed}")
    if r.confusion:
        c = r.confusion
        typer.echo(f"  vs truth ({r.truth}): {c}")
        typer.echo(f"  missed errors (verified but not truly on page): {len(r.missed_errors)}")
        for m in r.missed_errors[:10]:
            typer.echo(f"    {m}")


@app.command()
def reconcile(
    run_id: Annotated[str, typer.Argument()],
    model: Annotated[str | None, typer.Option(help="models.yaml key; default default_model")] = None,
    prompt_version: Annotated[str | None, typer.Option(help="pass-1 prompt version; default latest")] = None,
    dictionary_version: Annotated[int | None, typer.Option(
        help="dictionary snapshot; default = the version the run pinned at ingest")] = None,
) -> None:
    """Stage 6: map field names to dictionary keys; propose (never apply) aliases and new entries."""
    settings = get_settings()
    store = Store(settings.path(settings.store_dir))
    key = model or settings.load_models().default_model
    version = prompt_version or latest_pass1_version(store, run_id, key)
    pinned = store.load_run(run_id).dictionary_version
    snap = load_snapshot(settings.path(settings.dictionary_dir), dictionary_version or pinned)
    src = store.root / "runs" / run_id / f"extraction-{key}-{version}.grounded.json"
    manifest = PageManifest.model_validate_json(store.abs(store.load_run(run_id).manifest_path).read_text())
    ocr_pages = [OcrPage.model_validate_json(next((store.root / "ocr" / p.sha256).glob("*.json")).read_text())
                 for p in manifest.pages]
    doc = resolve_terms(DocumentExtraction.model_validate_json(src.read_text()), ocr_pages)  # Stage 5
    doc, r = run_reconcile(doc, snap)
    store.save_json(f"runs/{run_id}/extraction-{key}-{version}.final.json", doc)
    store.save_json(f"reports/{run_id}/reconcile-{key}-{version}.json", r)
    if snap.version != pinned:
        typer.echo(f"note: run pinned dictionary v{pinned}; reconciled with v{snap.version} (recorded in provenance)")
    if r.pass2_agree or r.pass2_disagree or r.pass2_new:
        typer.echo(f"  pass 2: {r.pass2_agree} pass-1 values confirmed, {r.pass2_disagree} disagreements (to review), "
                   f"{r.pass2_new} known fields only pass 2 found")
    typer.echo(f"  defined terms: {len(doc.defined_terms)} "
               f"({sum(bool(t.members) for t in doc.defined_terms)} collective, "
               f"{sum(bool(t.usage_locations) for t in doc.defined_terms)} with usages found across all pages)")
    typer.echo(f"[dictionary v{snap.version}] {r.fields} fields: {r.by_method}; "
               f"{r.type_or_confusable_rejections} candidates blocked by type/confusable gates")
    typer.echo(f"  core fields found: {len(r.mapped_core)}/{len(r.mapped_core) + len(r.missing_core)}; "
               f"not found on extracted pages: {r.missing_core}")
    typer.echo(f"  routing: {r.auto_accepted} auto-accepted, {r.needs_review} to review {r.review_reasons}")
    typer.echo(f"  proposals (pending, not applied): {len(r.alias_proposals)} aliases, "
               f"{len(r.new_entry_proposals)} new entries")


@app.command()
def degrade(
    pdf: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    out: Annotated[Path, typer.Argument(dir_okay=False)],
    seed: Annotated[int, typer.Option()] = 0,
) -> None:
    """Write a degraded, image-only copy of a PDF for testing (spec §19)."""
    n = degrade_pdf(pdf, out, seed=seed, dpi=get_settings().rasterise_dpi)
    typer.echo(f"wrote {out} ({n} pages, seed {seed})")


@app.command(name="check-model")
def check_model(key: Annotated[str, typer.Argument(help="models.yaml key, e.g. onprem_candidate")]) -> None:
    """One-call smoke test: does the model read a page image and return the JSON envelope? (spec §18)"""
    from PIL import ImageDraw, ImageFont

    from .gateway import get_adapter
    from .prompts import load_prompt

    settings = get_settings()
    spec = settings.load_models().models[key]
    if not spec.enabled:
        typer.echo(f"note: {key} is disabled in models.yaml; testing it anyway")
        spec = spec.model_copy(update={"enabled": True})
    img = Image.new("L", (1400, 700), 255)
    d = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=30)
    lines = ["“Maturity Date” means October 28, 2027.", "“Aggregate Commitments” means $1,500,000,000.",
             "Pricing Level   Term SOFR Loans   Base Rate Loans", "1               0.910%            0.000%",
             "2               1.025%            0.025%"]
    for i, line in enumerate(lines):
        d.text((40, 40 + i * 110), line, fill=0, font=font)
    from .gateway import AnthropicAdapter, OpenAICompatibleAdapter
    adapter = (AnthropicAdapter if spec.provider == "anthropic" else OpenAICompatibleAdapter)(settings, key, spec)
    prompt = load_prompt(settings.path(settings.prompts_dir), "pass1_extraction").render() + "\n\nThis image: page 1, the whole page."
    r = adapter.extract([img], prompt)
    typer.echo(f"{key} ({spec.provider}:{spec.model_name}) -> {'OK' if r.ok else 'FAILED: ' + str(r.failure)}"
               f"  stop={r.stop_reason}  {r.usage.input_tokens} in / {r.usage.output_tokens} out  {r.latency_s:.1f}s")
    if not r.ok:
        typer.echo(f"  detail: {(r.detail or '')[:400]}")
        typer.echo(f"  raw: {(r.raw_text or '')[:400]}")
        raise typer.Exit(code=1)
    values = " | ".join(f.raw_value for f in r.envelope.fields)
    checks = {v: v in values for v in ("October 28, 2027", "$1,500,000,000", "0.910%", "0.025%")}
    typer.echo(f"  {len(r.envelope.fields)} fields, {len(r.envelope.tables)} tables; expected values read: {checks}")
    if not all(checks.values()):
        raise typer.Exit(code=1)


@app.command()
def extract(
    pdf: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    document_id: Annotated[str, typer.Option()],
    version_label: Annotated[str, typer.Option()],
    effective_date: Annotated[datetime | None, typer.Option(formats=["%Y-%m-%d"])] = None,
    model: Annotated[str | None, typer.Option(help="models.yaml key; default default_model")] = None,
    pages: Annotated[str | None, typer.Option(help="e.g. 1,15,17-18; default all pages")] = None,
    dictionary_version: Annotated[int | None, typer.Option(help="default: latest snapshot")] = None,
    variant: Annotated[str, typer.Option(help="clean | degraded (test inputs)")] = "clean",
    skip_pass2: Annotated[bool, typer.Option(help="pass 1 only (cheaper / faster)")] = False,
    ocr_workers: Annotated[int, typer.Option()] = 4,
) -> None:
    """Full pipeline on one document: ingest -> OCR -> pass 1 -> pass 2 -> assemble -> ground ->
    defined terms + reconcile -> publish. Everything a reviewer or governance needs is kept per stage."""
    settings = get_settings()
    store = Store(settings.path(settings.store_dir))
    key = model or settings.load_models().default_model
    settings.model(key)  # fail fast: disabled, or not allowed in this environment
    snap = load_snapshot(settings.path(settings.dictionary_dir), dictionary_version)

    typer.echo(f"[1/8] ingest + rasterise ({settings.rasterise_dpi} DPI)")
    run = run_ingest(store, pdf, document_id, version_label, dictionary_version=snap.version,
                     effective_date=effective_date.date() if effective_date else None, input_variant=variant,
                     dpi=settings.rasterise_dpi, grayscale=settings.rasterise_grayscale)
    typer.echo(f"      run {run.run_id}: {run.page_count} pages")
    typer.echo("[2/8] OCR (the independent witness)")
    run_ocr(store, run.run_id, TesseractEngine(), workers=ocr_workers)
    typer.echo(f"[3/8] pass 1 with {key}")
    s1 = run_pass1(store, settings, run.run_id, key, _parse_pages(pages))
    typer.echo(f"      {s1.ok}/{len(s1.pages)} pages ok; failed {s1.failed or 'none'}; ${s1.cost_usd:.2f}")
    if not skip_pass2:
        typer.echo(f"[4/8] pass 2 (dictionary v{snap.version})")
        s2 = run_pass2(store, settings, run.run_id, key, snap, _parse_pages(pages))
        typer.echo(f"      {s2.ok}/{len(s2.pages)} pages; {s2.fields_reported} known-field reports; ${s2.cost_usd:.2f}")
    typer.echo("[5/8] assemble")
    _, a = run_assemble(store, settings, run.run_id, key)
    typer.echo(f"      {a.clauses} clauses, {a.fields} fields, {a.tables} tables; pages {a.pages_by_status}")
    typer.echo("[6/8] ground")
    _, g = run_grounding(store, run.run_id, key)
    typer.echo(f"      {g.verified}/{g.fields} verified at source")
    typer.echo(f"[7/8] defined terms + reconcile (dictionary v{snap.version})")
    version = latest_pass1_version(store, run.run_id, key)
    grounded = DocumentExtraction.model_validate_json(
        (store.root / "runs" / run.run_id / f"extraction-{key}-{version}.grounded.json").read_text())
    manifest = PageManifest.model_validate_json(store.abs(run.manifest_path).read_text())
    ocr_pages = [OcrPage.model_validate_json(next((store.root / "ocr" / p.sha256).glob("*.json")).read_text())
                 for p in manifest.pages]
    doc, r = run_reconcile(resolve_terms(grounded, ocr_pages), snap)
    store.save_json(f"runs/{run.run_id}/extraction-{key}-{version}.final.json", doc)
    store.save_json(f"reports/{run.run_id}/reconcile-{key}-{version}.json", r)
    typer.echo(f"      {r.auto_accepted} auto-accepted, {r.needs_review} to review; core fields found "
               f"{len(r.mapped_core)}/{len(r.mapped_core) + len(r.missing_core)}")
    typer.echo("[8/8] publish")
    info = publish_record(store, doc, variant)
    typer.echo(f"      {document_id} {version_label} r{info.revision:03d} -> {info.path}")
    typer.echo(f"next: cae review export {run.run_id}  |  cae eval {run.run_id} --golden ...  |  "
               f"cae export-audit --run-id {run.run_id}")


@app.command(name="publish")
def publish_cmd(
    run_id: Annotated[str, typer.Argument()],
    model: Annotated[str | None, typer.Option()] = None,
    prompt_version: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Stage 9: publish the latest reviewed revision as a write-once versioned record."""
    settings = get_settings()
    store = Store(settings.path(settings.store_dir))
    key = model or settings.load_models().default_model
    version = prompt_version or latest_pass1_version(store, run_id, key)
    src, doc = _final_doc(store, run_id, key, version)
    info = publish_record(store, doc, store.load_run(run_id).input_variant)
    typer.echo(f"{doc.document_id} {info.version_label} revision {info.revision} ({info.input_variant}) "
               f"<- {src.name}: {info.fields} fields, {info.reviewed} human-reviewed -> {info.path}")


@app.command()
def history(document_id: Annotated[str, typer.Argument()]) -> None:
    """List published versions and revisions of a document."""
    idx = load_index(Store(get_settings().path(get_settings().store_dir)), document_id)
    if not idx.revisions:
        typer.echo(f"nothing published for {document_id!r}", err=True)
        raise typer.Exit(code=1)
    for r in sorted(idx.revisions, key=lambda r: (r.effective_date or datetime.min.date(), r.version_label, r.revision)):
        typer.echo(f"  {r.version_label:<14} r{r.revision:03d} effective {r.effective_date}  {r.input_variant:<9} "
                   f"run {r.run_id}  model {r.model_id} prompts {r.prompt_versions} dict v{r.dictionary_version}  "
                   f"{r.fields} fields ({r.reviewed} reviewed)")


@app.command()
def asof(
    document_id: Annotated[str, typer.Argument()],
    field: Annotated[str, typer.Option(help="dictionary key, e.g. applicable_margin")],
    on: Annotated[datetime | None, typer.Option(formats=["%Y-%m-%d"], help="the version in force on this date")] = None,
    version_label: Annotated[str | None, typer.Option()] = None,
    variant: Annotated[str | None, typer.Option(help="clean | degraded; default the latest revision")] = None,
) -> None:
    """What did a field look like as of a date or version? (spec §6 Stage 9)"""
    a = as_of(Store(get_settings().path(get_settings().store_dir)), document_id, field,
              on=on.date() if on else None, version_label=version_label, variant=variant)
    if a is None:
        typer.echo("no published version matches", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"{a.document_id} {a.version_label} r{a.revision:03d} (effective {a.effective_date}): {len(a.fields)} values")
    for f in a.fields:
        typer.echo(f"  {f.instance_key or '-':<28} {f.raw_value!r:<34} p{f.source.page}  {f.review_status}")


@app.command()
def diff(
    document_id: Annotated[str, typer.Argument()],
    from_version: Annotated[str, typer.Option()],
    to_version: Annotated[str, typer.Option()],
    from_revision: Annotated[int | None, typer.Option(help="default: latest revision")] = None,
    to_revision: Annotated[int | None, typer.Option(help="default: latest revision")] = None,
    show_unchanged: Annotated[bool, typer.Option()] = False,
) -> None:
    """Structured diff between two published versions (spec §11)."""
    store = Store(get_settings().path(get_settings().store_dir))
    idx = load_index(store, document_id)

    def pick(label, rev):
        rs = [r for r in idx.revisions if r.version_label == label and (rev is None or r.revision == rev)]
        if not rs:
            typer.echo(f"no published revision {label} r{rev}", err=True)
            raise typer.Exit(code=1)
        r = max(rs, key=lambda r: r.revision)
        return r, DocumentExtraction.model_validate_json(store.abs(r.path).read_text())

    ra, a = pick(from_version, from_revision)
    rb, b = pick(to_version, to_revision)
    d = run_diff(a, b)
    store.save_json(f"records/{document_id}/diffs/{from_version}.r{ra.revision}..{to_version}.r{rb.revision}.json", d)
    kinds = {k: sum(c.change_type == k for c in d.changes) for k in ("added", "removed", "modified", "unchanged")}
    al = {k: sum(x.status == k for x in d.clause_alignments) for k in ("matched", "added", "removed")}
    typer.echo(f"{document_id}: {from_version} r{ra.revision} ({ra.input_variant}) -> {to_version} r{rb.revision} ({rb.input_variant})")
    for w in d.comparability_warnings:
        typer.echo(f"  WARNING {w}")
    typer.echo(f"  clauses {al}; {sum(x.text_changed for x in d.clause_alignments)} matched clauses with changed text; "
               f"{sum(x.low_confidence_alignment for x in d.clause_alignments)} low-confidence alignments")
    typer.echo(f"  fields {kinds}; {sum(c.low_confidence_change for c in d.changes)} changes flagged low-confidence")
    typer.echo(f"  tables: {sum(len(t.cell_changes) for t in d.table_changes)} cell changes")
    for c in d.changes:
        if c.change_type != "unchanged" or show_unchanged:
            fmt = lambda v: None if v is None else (v.text if v.type.value == "text" else
                                                    str(v.amount if v.amount is not None else v.date_value))
            flag = " (low confidence)" if c.low_confidence_change else ""
            typer.echo(f"    {c.change_type:<9} {c.canonical_name}|{c.instance_key or ''}: "
                       f"{fmt(c.old_value)!r} -> {fmt(c.new_value)!r}{flag}")
    for t in d.table_changes:
        for cc in t.cell_changes:
            typer.echo(f"    table {t.from_table_id}->{t.to_table_id} [{cc.row_header} | {cc.col_header}] {cc.change_type}")


@app.command(name="eval")
def eval_(
    run_id: Annotated[str, typer.Argument()],
    golden: Annotated[Path, typer.Option(exists=True, dir_okay=False, help="golden-set CSV")],
    model: Annotated[str | None, typer.Option(help="models.yaml key; default default_model")] = None,
    prompt_version: Annotated[str | None, typer.Option(help="pass-1 prompt version; default latest")] = None,
    include_unverified: Annotated[bool, typer.Option(help="also score unverified draft rows (PROVISIONAL)")] = False,
    allow_ai_verified: Annotated[bool, typer.Option(help="also score rows verified by an AI (labelled in the report)")] = False,
) -> None:
    """Score a grounded extraction against the golden set (spec §13)."""
    settings = get_settings()
    store = Store(settings.path(settings.store_dir))
    key = model or settings.load_models().default_model
    version = prompt_version or latest_pass1_version(store, run_id, key)
    base = store.root / "runs" / run_id / f"extraction-{key}-{version}"
    final = base.with_name(base.name + ".final.json")  # reconciled; falls back to grounded-only
    doc = DocumentExtraction.model_validate_json(
        (final if final.exists() else base.with_name(base.name + ".grounded.json")).read_text())
    rows = load_golden(golden, include_unverified, allow_ai_verified)
    if not rows:
        ai = len(load_golden(golden, allow_ai_verified=True))
        hint = f" ({ai} rows are AI-verified: pass --allow-ai-verified to score them, labelled)" if ai else ""
        typer.echo(f"no human-verified golden rows{hint}", err=True)
        raise typer.Exit(code=1)
    r = evaluate(doc, rows, golden.name, include_unverified, store.load_run(run_id).input_variant)
    store.save_json(f"reports/{run_id}/eval-{key}-{version}{'-provisional' if r.provisional else ''}.json", r)
    if r.provisional:
        typer.echo("PROVISIONAL — includes rows no one has verified; do not quote these numbers")
    elif r.verification_tiers.get("ai"):
        typer.echo(f"AI-VERIFIED — {r.verification_tiers['ai']} of {sum(r.verification_tiers.values())} rows were "
                   "verified by an AI, not a person; not valid for governance sign-off")
    typer.echo(f"[{key} prompt {version}, {r.input_variant}] {r.rows_scored} golden values on extracted pages "
               f"({r.rows_not_extracted} on pages not extracted, {r.rows_absent_pending} absent-checks pending)")
    lo, hi = r.value_recall_ci95
    typer.echo(f"  value recall      {r.value_recall:.1%}  (95% CI {lo:.1%}–{hi:.1%})")
    if r.grid_cell_accuracy is not None:
        lo, hi = r.grid_cell_accuracy_ci95
        typer.echo(f"  grid cell accuracy {r.grid_cell_accuracy:.1%} of {r.grid_cells} cells  (95% CI {lo:.1%}–{hi:.1%})")
    if r.labelled_recall is not None:
        lo, hi = r.labelled_recall_ci95
        typer.echo(f"  labelled recall   {r.labelled_recall:.1%}  (95% CI {lo:.1%}–{hi:.1%})  value under the right key")
    if r.precision_on_golden_keys is not None:
        lo, hi = r.precision_on_golden_keys_ci95
        typer.echo(f"  precision on golden keys {r.precision_on_golden_keys:.1%} of {r.precision_fields} mapped fields "
                   f"(95% CI {lo:.1%}–{hi:.1%})")
        for w in r.wrong_values_on_golden_keys:
            typer.echo(f"    WRONG {w}")
    if r.auto_accepted_precision is not None:
        lo, hi = r.auto_accepted_precision_ci95
        typer.echo(f"  AUTO-ACCEPTED precision {r.auto_accepted_precision:.1%} of {r.auto_accepted_fields} "
                   f"(95% CI {lo:.1%}–{hi:.1%})  <- wrong values no human would see")
        for w in r.wrong_auto_accepted:
            typer.echo(f"    WRONG & AUTO-ACCEPTED {w}")
    if r.found_auto_accepted is not None:
        typer.echo(f"  found values auto-accepted by grounding: {r.found_auto_accepted:.1%}")
    typer.echo(f"  unverified by grounding (upper bound on hallucination): {r.unverified_rate:.1%}")
    for m in (x for x in r.results if x.status in ("missed", "text_only") or x.label_ok is False):
        label = ("IN TEXT ONLY" if m.status == "text_only" else "MISSED" if m.status == "missed"
                 else "FOUND, WRONG/NO LABEL")
        typer.echo(f"  {label} {m.key} = {m.expected!r} (p{m.page})")
    typer.echo("  pending: " + "; ".join(r.pending))


@app.command(name="compare")
def compare_cmd(
    run_a: Annotated[str, typer.Argument()],
    run_b: Annotated[str, typer.Argument()],
    golden: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    allow_ai_verified: Annotated[bool, typer.Option()] = False,
    model_a: Annotated[str | None, typer.Option()] = None,
    model_b: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Two runs side by side on the same golden rows — the frontier vs on-prem parity check (spec §12)."""
    settings = get_settings()
    store = Store(settings.path(settings.store_dir))
    docs = []
    for run, m in ((run_a, model_a), (run_b, model_b)):
        key = m or settings.load_models().default_model
        docs.append(_final_doc(store, run, key, latest_pass1_version(store, run, key))[1])
    rows = load_golden(golden, allow_ai_verified=allow_ai_verified)
    c = run_compare(store, run_a, docs[0], run_b, docs[1], rows, golden.name, False)
    store.save_json(f"reports/compare/{run_a}..{run_b}.json", c)
    typer.echo(f"NOTE: {c.note}")
    if any(is_ai_verifier(r.verified_by) for r in rows):
        typer.echo("AI-VERIFIED golden rows — not valid for governance sign-off")
    pct = lambda v: "-" if v is None else f"{v:.1%}"
    typer.echo(f"{'':<34}{'A':>14}{'B':>14}")
    for label, fa, fb in [("run", c.a.run_id[-13:], c.b.run_id[-13:]), ("model", c.a.model_key, c.b.model_key),
                          ("input", c.a.input_variant, c.b.input_variant),
                          ("labelled recall", pct(c.a.labelled_recall), pct(c.b.labelled_recall)),
                          ("auto-accepted precision", pct(c.a.auto_accepted_precision), pct(c.b.auto_accepted_precision)),
                          ("precision on golden keys", pct(c.a.precision_on_golden_keys), pct(c.b.precision_on_golden_keys)),
                          ("grid cell accuracy", pct(c.a.grid_cell_accuracy), pct(c.b.grid_cell_accuracy)),
                          ("unverified by grounding", pct(c.a.unverified_rate), pct(c.b.unverified_rate)),
                          ("cost (extraction)", f"${c.a.cost_usd:.2f}", f"${c.b.cost_usd:.2f}"),
                          ("cost per page", f"${c.a.cost_per_page_usd}", f"${c.b.cost_per_page_usd}"),
                          ("latency per call (s)", str(c.a.mean_latency_per_call_s), str(c.b.mean_latency_per_call_s))]:
        typer.echo(f"  {label:<32}{fa:>14}{fb:>14}")
    p = "n/a" if c.mcnemar_p is None else f"{c.mcnemar_p:.3f}"
    typer.echo(f"  paired on {c.golden_rows_paired} golden rows: A-only right {c.a_only_right}, "
               f"B-only right {c.b_only_right}, McNemar exact p = {p}")
    typer.echo(f"  shared keys {c.shared_keys}: same value {c.keys_same_value}, different {len(c.keys_different_value)}")


@app.command(name="export-audit")
def export_audit_cmd(
    run_id: Annotated[str, typer.Option()],
    out: Annotated[Path | None, typer.Option(help="zip to write; default var/audit/<run_id>.zip")] = None,
) -> None:
    """Bundle governance artefacts for a run into a zip with a sha256 manifest (spec §16)."""
    settings = get_settings()
    store = Store(settings.path(settings.store_dir))
    target = out or settings.path(Path("var/audit")) / f"{run_id}.zip"
    m = export_audit(store, run_id, settings.path(Path(".")), target)
    typer.echo(f"wrote {target}: {len(m['files'])} files (manifest with sha256 of each); excluded: {m['excluded']}")


@app.command(name="emit")
def emit_cmd(
    document_id: Annotated[str, typer.Argument()],
    version_label: Annotated[str, typer.Option()],
    revision: Annotated[int | None, typer.Option(help="default: latest")] = None,
    out: Annotated[Path | None, typer.Option(help="write the payload here; default stdout")] = None,
    include_pending: Annotated[bool, typer.Option(help="also send values nobody has accepted yet (marked)")] = False,
) -> None:
    """Stage 11: hand a published version to the downstream tool via emit()."""
    store = Store(get_settings().path(get_settings().store_dir))
    idx = load_index(store, document_id)
    rs = [r for r in idx.revisions if r.version_label == version_label and (revision is None or r.revision == revision)]
    if not rs:
        typer.echo("no such published revision", err=True)
        raise typer.Exit(code=1)
    r = max(rs, key=lambda r: r.revision)
    doc = DocumentExtraction.model_validate_json(store.abs(r.path).read_text())
    payload = build_payload(doc, r.revision, include_pending)
    emit(payload, file_sink(out) if out else (lambda p: typer.echo(p.model_dump_json(indent=1))))
    if out:
        typer.echo(f"emitted {len(payload.values)} values ({payload.withheld_pending_review} withheld pending review) -> {out}")


@dict_app.command("list")
def dict_list(
    version: Annotated[int | None, typer.Option(help="snapshot version; default latest")] = None,
    core_only: Annotated[bool, typer.Option("--core")] = False,
) -> None:
    """List dictionary entries."""
    snap = load_snapshot(get_settings().path(get_settings().dictionary_dir), version)
    entries = snap.core_fields if core_only else snap.entries
    typer.echo(f"dictionary v{snap.version}: {len(snap.entries)} entries, {len(snap.core_fields)} core")
    for e in entries:
        flags = ("core " if e.core else "     ") + ("rep " if e.repeatable else "    ")
        typer.echo(f"  {e.canonical_name:<36} {flags} {e.status:<15} {(e.data_type.value if e.data_type else '-'):<11} {e.display_name}")


@dict_app.command("show")
def dict_show(
    canonical_name: str,
    version: Annotated[int | None, typer.Option()] = None,
) -> None:
    """Show one dictionary entry as JSON."""
    snap = load_snapshot(get_settings().path(get_settings().dictionary_dir), version)
    try:
        typer.echo(snap.entry(canonical_name).model_dump_json(indent=2, exclude_none=True))
    except KeyError:
        typer.echo(f"no entry {canonical_name!r} in v{snap.version}", err=True)
        raise typer.Exit(code=1)


@dict_app.command("versions")
def dict_versions() -> None:
    """List dictionary snapshot versions."""
    typer.echo(" ".join(f"v{v}" for v in list_versions(get_settings().path(get_settings().dictionary_dir))))


@dict_app.command("enrich")
def dict_enrich(
    run_id: Annotated[str, typer.Argument()],
    limit: Annotated[int, typer.Option(help="max new entries to draft (each is one model call)")] = 15,
    model: Annotated[str | None, typer.Option()] = None,
    dictionary_version: Annotated[int | None, typer.Option()] = None,
    dry_run: Annotated[bool, typer.Option(help="list the candidates only; no model calls")] = False,
) -> None:
    """Stage 7: AI-drafted business meanings for proposed new entries -> a pending proposal file (spends credit)."""
    settings = get_settings()
    store = Store(settings.path(settings.store_dir))
    key = model or settings.load_models().default_model
    _, doc = _final_doc(store, run_id, key, latest_pass1_version(store, run_id, key))
    snap = load_snapshot(settings.path(settings.dictionary_dir), dictionary_version)
    if dry_run:
        cands, total = enrich_candidates(doc, snap, limit)
        typer.echo(f"{total} candidate names after filtering; top {len(cands)}:")
        for c in cands:
            typer.echo(f"  {c.canonical_name:<44} seen {c.seen}  e.g. {c.examples[0][:50]!r}")
        return
    b = run_enrich(doc, snap, settings, key, limit)
    out = settings.path(Path("dictionary/proposals")) / f"enrichment-{b.created_at:%Y%m%dT%H%M%SZ}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(b.model_dump_json(indent=1))
    for p in b.proposals:
        typer.echo(f"  {p.entry.canonical_name:<40} {(p.entry.business_meaning or p.failure or '')[:90]}")
    typer.echo(f"{len(b.proposals)} proposals (pending, NOT applied) -> {out}; cost ${b.cost_usd:.2f}")


@dict_app.command("add-entries")
def dict_add_entries(
    proposals: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="dictionary/proposals/enrichment-*.json")],
    approver: Annotated[str, typer.Option(help="the named person accepting these entries")],
    only: Annotated[str | None, typer.Option(help="comma-separated canonical names; default all")] = None,
) -> None:
    """Accept AI-drafted entries (edit the proposal file first if needed) -> next dictionary snapshot."""
    settings = get_settings()
    d = settings.path(settings.dictionary_dir)
    batch = EnrichmentBatch.model_validate_json(proposals.read_text())
    wanted = {x.strip() for x in only.split(",")} if only else None
    entries = [p.entry for p in batch.proposals if p.entry.business_meaning and (wanted is None or p.entry.canonical_name in wanted)]
    try:
        new = add_dictionary_entries(load_snapshot(d), entries, approver)
    except (PermissionError, ValueError) as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1)
    typer.echo(f"wrote {save_snapshot(d, new)}: {new.notes}")


@dict_app.command("approve")
def dict_approve(
    batch: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="runs/<run>/reviews/<batch>.json")],
    approver: Annotated[str, typer.Option(help="the named person approving these aliases")],
    dry_run: Annotated[bool, typer.Option(help="show what would change; write nothing")] = False,
) -> None:
    """Approve reviewer-confirmed mappings as aliases -> writes the next dictionary snapshot."""
    settings = get_settings()
    d = settings.path(settings.dictionary_dir)
    snap = load_snapshot(d)
    result = IngestResult.model_validate_json(batch.read_text())
    try:
        new, r = approve_aliases(snap, result.alias_proposals, approver)
    except PermissionError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1)
    for a in r.added:
        typer.echo(f"  + {a}")
    for a in r.rejected:
        typer.echo(f"  ! {a}")
    typer.echo(f"{len(r.added)} to add, {len(r.already_known)} already known, {len(r.rejected)} rejected")
    if dry_run or not r.added:
        typer.echo("nothing written" + (" (dry run)" if dry_run else ""))
        return
    typer.echo(f"wrote {save_snapshot(d, new)} (v{new.version}; v{snap.version} unchanged)")


def _final_doc(store: Store, run_id: str, key: str, version: str) -> tuple[Path, DocumentExtraction]:
    """Latest document revision: the newest reviewed revision, else the reconciled extraction."""
    base = store.root / "runs" / run_id
    reviewed = sorted(base.glob(f"extraction-{key}-{version}.reviewed-*.json"))
    path = reviewed[-1] if reviewed else base / f"extraction-{key}-{version}.final.json"
    return path, DocumentExtraction.model_validate_json(path.read_text())


@review_app.command("export")
def review_export(
    run_id: Annotated[str, typer.Argument()],
    out: Annotated[Path, typer.Option(help="CSV to write")] = Path("review.csv"),
    model: Annotated[str | None, typer.Option()] = None,
    prompt_version: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Export everything routed to review as a CSV (fill in decision / reviewer, then ingest)."""
    settings = get_settings()
    store = Store(settings.path(settings.store_dir))
    key = model or settings.load_models().default_model
    version = prompt_version or latest_pass1_version(store, run_id, key)
    src, doc = _final_doc(store, run_id, key, version)
    n = write_review_export(doc, out)
    typer.echo(f"wrote {out}: {n} items to review (from {src.name}). Decisions: confirm / correct / reject.")


@review_app.command("ingest")
def review_ingest(
    run_id: Annotated[str, typer.Argument()],
    path: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    model: Annotated[str | None, typer.Option()] = None,
    prompt_version: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Apply reviewer decisions: audit records, a new reviewed revision, alias proposals, golden rows."""
    settings = get_settings()
    store = Store(settings.path(settings.store_dir))
    key = model or settings.load_models().default_model
    version = prompt_version or latest_pass1_version(store, run_id, key)
    src, doc = _final_doc(store, run_id, key, version)
    reviewed, r = review_ingest_file(doc, path)
    if r.errors:
        typer.echo("NOT INGESTED — fix these rows first:\n  " + "\n  ".join(r.errors), err=True)
        raise typer.Exit(code=1)
    n = len(list((store.root / "runs" / run_id).glob(f"extraction-{key}-{version}.reviewed-*.json"))) + 1
    store.save_json(f"runs/{run_id}/extraction-{key}-{version}.reviewed-{n:03d}.json", reviewed)
    store.save_json(f"runs/{run_id}/reviews/{r.batch_id}.json", r)
    counts = {d: sum(x.decision == d for x in r.records) for d in ("confirmed", "corrected", "rejected")}
    typer.echo(f"batch {r.batch_id}: {counts}; revision {n:03d} written (previous revisions kept)")
    typer.echo(f"  {len(r.alias_proposals)} alias proposals -> `cae dict approve`; "
               f"{len(r.golden_rows)} golden rows in runs/{run_id}/reviews/{r.batch_id}.json")
    if r.reviewer_is_ai:
        typer.echo("  note: reviewer looks like an AI — its golden rows count only as AI-verified, "
                   "and its alias proposals cannot be approved into the dictionary")
    if r.skipped:
        typer.echo(f"  skipped {len(r.skipped)} rows without a decision")


products_app = typer.Typer(no_args_is_help=True, help="Entity model and JSON data products from an accepted handoff.")
app.add_typer(products_app, name="products")


@products_app.command("build")
def products_build(
    handoff: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="Handoff JSON from cae emit")],
    out: Annotated[Path, typer.Option(help="Directory the JSON products are written under")] = Path("var/products"),
) -> None:
    """Build the entity model and data products for one accepted handoff."""
    payload = HandoffPayload.model_validate_json(handoff.read_text())
    dest = write_bundle(payload, out)
    typer.echo(f"wrote entity model and products for {payload.document_id} {payload.version_label} "
               f"r{payload.revision:03d} -> {dest}")


@app.command()
def serve(
    products: Annotated[Path, typer.Option(help="Directory written by cae products build")] = Path("var/products"),
    host: Annotated[str, typer.Option()] = "127.0.0.1",
    port: Annotated[int, typer.Option()] = 8765,
    queries: Annotated[Path | None, typer.Option(help="Extra query-register JSON files")] = None,
) -> None:
    """Serve the data products over HTTP. Queries are the register; the body cannot contain SQL."""
    access, entities = open_access(products, queries)
    typer.echo(f"serving {len(access.products)} products, {len(access.queries)} queries on http://{host}:{port}")
    serve_products(access, entities, host, port)


@app.command()
def mcp(
    products: Annotated[Path, typer.Option(help="Directory written by cae products build")] = Path("var/products"),
    queries: Annotated[Path | None, typer.Option(help="Extra query-register JSON files")] = None,
) -> None:
    """Serve the same query register as MCP tools on stdin/stdout."""
    access, entities = open_access(products, queries)
    serve_stdio(access, entities)
