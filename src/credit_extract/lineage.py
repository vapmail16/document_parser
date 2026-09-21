"""Versioned document records & as-of queries (spec §6 Stage 9, §11). No model involved.

records/<document_id>/<version_label>/r<NNN>.json   write-once revisions of the final document
records/<document_id>/index.json                    derived index (rebuilt on every publish)

Nothing is overwritten: a re-extraction or a review round of the same version is a new revision.
As-of answers "what did the margin look like as of version N / date D?" from the latest revision
of the version in force.
"""

from datetime import date, datetime, timezone

from pydantic import BaseModel

from .contracts import DocumentExtraction, ExtractedField
from .store import Store


class RevisionInfo(BaseModel):
    version_label: str
    revision: int
    effective_date: date | None
    input_variant: str
    published_at: datetime
    run_id: str
    model_id: str
    prompt_versions: dict[str, str]
    dictionary_version: int
    source_sha256: str
    fields: int
    reviewed: int  # fields a human confirmed / corrected / rejected
    path: str


class DocumentIndex(BaseModel):
    document_id: str
    revisions: list[RevisionInfo]

    def latest(self, version_label: str, variant: str | None = None) -> RevisionInfo | None:
        rs = [r for r in self.revisions if r.version_label == version_label and (variant is None or r.input_variant == variant)]
        return max(rs, key=lambda r: r.revision) if rs else None

    def version_in_force(self, on: date) -> str | None:
        dated = sorted({(r.effective_date, r.version_label) for r in self.revisions if r.effective_date and r.effective_date <= on})
        return dated[-1][1] if dated else None


def _dir(store: Store, document_id: str):
    return store.root / "records" / document_id


def load_index(store: Store, document_id: str) -> DocumentIndex:
    path = _dir(store, document_id) / "index.json"
    if not path.exists():
        return DocumentIndex(document_id=document_id, revisions=[])
    return DocumentIndex.model_validate_json(path.read_text())


def publish(store: Store, doc: DocumentExtraction, input_variant: str, now: datetime | None = None) -> RevisionInfo:
    now = now or datetime.now(timezone.utc)
    index = load_index(store, doc.document_id)
    n = 1 + max((r.revision for r in index.revisions if r.version_label == doc.version_label), default=0)
    path = _dir(store, doc.document_id) / doc.version_label / f"r{n:03d}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as f:  # write-once
        f.write(doc.model_dump_json(indent=1))
    info = RevisionInfo(
        version_label=doc.version_label, revision=n, effective_date=doc.effective_date, input_variant=input_variant,
        published_at=now, run_id=doc.provenance.run_id, model_id=doc.provenance.model_id,
        prompt_versions=doc.provenance.prompt_versions, dictionary_version=doc.provenance.dictionary_version,
        source_sha256=doc.provenance.source_sha256, fields=len(doc.flat_fields),
        reviewed=sum(f.review_status in ("confirmed", "corrected", "rejected") for f in doc.flat_fields),
        path=store.rel(path))
    index.revisions.append(info)
    (_dir(store, doc.document_id) / "index.json").write_text(index.model_dump_json(indent=1))
    return info


class AsOfAnswer(BaseModel):
    document_id: str
    version_label: str
    revision: int
    effective_date: date | None
    fields: list[ExtractedField]


def as_of(store: Store, document_id: str, canonical_name: str, *, on: date | None = None,
          version_label: str | None = None, variant: str | None = None) -> AsOfAnswer | None:
    index = load_index(store, document_id)
    label = version_label or (index.version_in_force(on) if on else None)
    if label is None and on is None:
        label = max(index.revisions, key=lambda r: (r.effective_date or date.min, r.published_at)).version_label \
            if index.revisions else None
    if label is None:
        return None
    rev = index.latest(label, variant)
    if rev is None:
        return None
    doc = DocumentExtraction.model_validate_json(store.abs(rev.path).read_text())
    fields = [f for f in doc.flat_fields if f.canonical_name == canonical_name and f.review_status != "rejected"]
    return AsOfAnswer(document_id=document_id, version_label=label, revision=rev.revision,
                      effective_date=rev.effective_date, fields=fields)
