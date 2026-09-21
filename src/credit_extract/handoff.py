"""Stage 11 — hand-off (spec §6) and governance artefact export (spec §16). No model involved.

emit(payload, sink): the one integration seam for the downstream tool. The payload carries only
values the pipeline stands behind by default (auto-accepted or human-confirmed/corrected), each with
its source page and quote; `include_pending` adds unreviewed values, clearly marked.
"""

import hashlib
import json
import zipfile
from collections.abc import Callable
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel

from .contracts import DocumentExtraction, NormalisedValue, Provenance
from .store import Store

TRUSTED = {"auto_accepted", "confirmed", "corrected"}


class HandoffValue(BaseModel):
    key: str
    instance: str | None
    value: NormalisedValue | None
    raw_value: str | None
    page: int
    quote: str
    grounding: str
    status: str  # auto_accepted | confirmed | corrected | needs_review (only with include_pending)
    origin: str  # model | human


class HandoffPayload(BaseModel):
    document_id: str
    version_label: str
    revision: int
    effective_date: date | None
    execution_status: str
    document_kind: str
    parties: list[dict[str, str | list[str]]]
    values: list[HandoffValue]
    withheld_pending_review: int  # values not sent because nobody has accepted them yet
    provenance: Provenance
    emitted_at: datetime


def build_payload(doc: DocumentExtraction, revision: int, include_pending: bool = False) -> HandoffPayload:
    keep = [f for f in doc.flat_fields if f.canonical_name and
            (f.review_status in TRUSTED or (include_pending and f.review_status == "needs_review"))]
    withheld = sum(1 for f in doc.flat_fields if f.canonical_name and f.review_status == "needs_review") \
        if not include_pending else 0
    return HandoffPayload(
        document_id=doc.document_id, version_label=doc.version_label, revision=revision,
        effective_date=doc.effective_date, execution_status=doc.execution_status, document_kind=doc.document_kind,
        parties=[{"name": p.name, "role": p.role, "keys": p.canonical_roles} for p in doc.parties if p.canonical_roles],
        values=[HandoffValue(key=f.canonical_name, instance=f.instance_key, value=f.value, raw_value=f.raw_value,
                             page=f.source.page, quote=f.source.source_text, grounding=f.source.grounding.value,
                             status=f.review_status, origin=f.origin) for f in keep],
        withheld_pending_review=withheld, provenance=doc.provenance, emitted_at=datetime.now(timezone.utc))


def emit(payload: HandoffPayload, sink: Callable[[HandoffPayload], None]) -> None:
    """The downstream integration point: register any callable (queue publisher, API client, file writer)."""
    sink(payload)


def file_sink(path: Path) -> Callable[[HandoffPayload], None]:
    def write(p: HandoffPayload) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(p.model_dump_json(indent=1))
    return write


# --- governance export -----------------------------------------------------------

def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def export_audit(store: Store, run_id: str, repo_root: Path, out: Path) -> dict:
    """Zip every artefact governance asks for about one run, with a sha256 manifest.
    Excludes secrets (.env) and page images (listed by hash in the page manifest instead)."""
    run = store.load_run(run_id)
    files: dict[str, bytes] = {}

    def add(arc: str, path: Path) -> None:
        if path.is_file():
            files[arc] = path.read_bytes()

    add("run/run.json", store.root / "runs" / f"{run_id}.json")
    add("run/stages.jsonl", store.root / "runs" / f"{run_id}.stages.jsonl")
    add("run/page_manifest.json", store.abs(run.manifest_path))
    for p in sorted((store.root / "reports" / run_id).glob("*.json")):
        add(f"reports/{p.name}", p)
    run_dir = store.root / "runs" / run_id
    for p in sorted(run_dir.rglob("*.json")):
        rel = p.relative_to(run_dir).as_posix()
        add(f"run/{rel}", p)  # extractions, raw model responses (pass1/pass2), review batches

    used_prompts: set[tuple[str, str]] = set()
    used_dicts: set[int] = {run.dictionary_version}
    for p in run_dir.glob("extraction-*.json"):
        prov = json.loads(p.read_text()).get("provenance", {})
        used_prompts |= set(prov.get("prompt_versions", {}).items())
        used_dicts.add(prov.get("dictionary_version", run.dictionary_version))
    for name, version in sorted(used_prompts):
        add(f"prompts/{name}/{version}.md", repo_root / "prompts" / name / f"{version}.md")
    for v in sorted(used_dicts):
        add(f"dictionary/v{v:04d}.json", repo_root / "dictionary" / "snapshots" / f"v{v:04d}.json")
    for name in ("models.yaml", "requirements-prod.txt", "uv.lock", "pyproject.toml"):
        add(f"config/{name}", repo_root / name)

    manifest = {"run_id": run_id, "document_id": run.document_id, "version_label": run.version_label,
                "source_sha256": run.source_sha256, "created_at": datetime.now(timezone.utc).isoformat(),
                "excluded": [".env (secrets)", "page images (hashes in run/page_manifest.json)"],
                "files": {k: _sha(v) for k, v in sorted(files.items())}}
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for k, v in sorted(files.items()):
            z.writestr(k, v)
        z.writestr("MANIFEST.json", json.dumps(manifest, indent=1, default=_json_default))
    return manifest


def _json_default(o):
    if isinstance(o, (Decimal, date, datetime)):
        return str(o)
    raise TypeError(type(o))
