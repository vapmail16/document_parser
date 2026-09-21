"""Stage 7 — business-context enrichment (spec §6, §9). Model: text-only, cheap.

Drafts {business_meaning, credit_risk_relevance, downstream_use} for PROPOSED new dictionary entries.
Output is a proposal file for a human dictionary owner — never applied to a snapshot automatically.
Candidates are filtered deterministically first so no money is spent on cross-references,
signatures, document metadata or per-lender duplicates of existing fields.
"""

import re
from datetime import datetime, timezone

from pydantic import BaseModel

from .config import Settings
from .contracts import DictionarySnapshot, DocumentExtraction, FieldDictionaryEntry
from .gateway import get_adapter
from .normalise import normalise, text_key
from .prompts import load_prompt
from .reconcile import base_name, key_form

PROMPT_NAME = "enrichment"
_REFERENCE_VALUE = re.compile(r"^\W*(sections?|articles?|exhibits?|schedules?|clauses?|rule|publication|part)\b", re.I)
_NOISE_NAME = re.compile(r"\b(cross reference|reference|section|exhibit|schedule|signatory|title of|form of|form|"
                         r"cusip|document type|page|provision|legislation|directive|statute|handbook)\b")


class EnrichmentOut(BaseModel):
    business_meaning: str
    credit_risk_relevance: str
    downstream_use: str


class Candidate(BaseModel):
    canonical_name: str
    display_name: str
    examples: list[str]
    page: int
    clause_excerpt: str
    seen: int


class Proposal(BaseModel):
    entry: FieldDictionaryEntry  # status pending_review — for a human to approve, edit or drop
    evidence: Candidate
    model_key: str
    prompt_version: str
    cost_usd: float | None
    failure: str | None = None


class EnrichmentBatch(BaseModel):
    created_at: datetime
    document_id: str
    version_label: str
    dictionary_version: int
    candidates_considered: int
    proposals: list[Proposal]
    cost_usd: float


def candidates(doc: DocumentExtraction, snap: DictionarySnapshot, limit: int) -> tuple[list[Candidate], int]:
    party_words = [key_form(p.name) for p in doc.parties if len(p.name) > 5]
    known = {e.canonical_name for e in snap.entries}
    clause_text = {c.clause_id: c.text or "" for s in doc.sections for c in s.clauses}
    groups: dict[str, list] = {}
    for f in doc.flat_fields:
        if f.canonical_name or not f.raw_value:
            continue
        b = re.sub(r"(?<=[a-z])\d{1,2}\b", "", base_name(f.name)).strip()  # footnote marks: "share1"
        if not b or _NOISE_NAME.search(b) or _REFERENCE_VALUE.match(f.raw_value):
            continue
        # Party named in the concept itself ("Bank of America, N.A. Commitment") = an instance of an existing
        # field. Named only in the qualifier ("Pro Rata Share (Bank of America, N.A.)") = a new concept's instance.
        if any(pw and pw in b for pw in party_words):
            continue
        groups.setdefault(b, []).append(f)
    # Fold near-duplicates into the shorter name ("debt rating s p moody s fitch" -> "debt rating"), but only
    # when they share an actual value — "alternative currency daily rate floor" is not "alternative currency".
    for b in sorted(groups, key=len):
        if b not in groups:
            continue
        values = {text_key(f.raw_value) for f in groups[b]}
        for longer in [x for x in groups if x != b and x.startswith(b + " ")]:
            if values & {text_key(f.raw_value) for f in groups[longer]}:
                groups[b] += groups.pop(longer)
    total = len(groups)
    out = []
    for b, fs in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        slug = re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", b)).strip("_")
        if not slug or slug in known:
            continue
        f = fs[0]
        excerpt = (clause_text.get(f.clause_id) or f.source.source_text)[:1500]
        out.append(Candidate(canonical_name=slug, display_name=f.name.split("(")[0].strip(),
                             examples=list(dict.fromkeys(x.raw_value for x in fs))[:3], page=f.source.page,
                             clause_excerpt=excerpt, seen=len(fs)))
        if len(out) >= limit:
            break
    return out, total


def enrich(doc: DocumentExtraction, snap: DictionarySnapshot, settings: Settings, model_key: str,
           limit: int = 15) -> EnrichmentBatch:
    template = load_prompt(settings.path(settings.prompts_dir), PROMPT_NAME)
    adapter = get_adapter(settings, model_key)
    cands, total = candidates(doc, snap, limit)
    today = datetime.now(timezone.utc).date()
    proposals = []
    for c in cands:
        prompt = template.render(field_name=c.display_name, example_values="\n".join(f"- {x}" for x in c.examples),
                                 clause_text=c.clause_excerpt)
        resp, out = adapter.complete(prompt, EnrichmentOut)
        v = normalise(c.examples[0])
        entry = FieldDictionaryEntry(
            canonical_name=c.canonical_name, display_name=c.display_name,
            business_meaning=out.business_meaning if out else None,
            credit_risk_relevance=out.credit_risk_relevance if out else None,
            downstream_use=out.downstream_use if out else None,
            typical_location=f"p{c.page} of {doc.document_id} {doc.version_label}", data_type=v.type if v else None,
            status="pending_review", first_seen=today, last_seen=today)
        proposals.append(Proposal(entry=entry, evidence=c, model_key=model_key, prompt_version=template.version,
                                  cost_usd=resp.usage.cost_usd, failure=resp.failure))
    return EnrichmentBatch(created_at=datetime.now(timezone.utc), document_id=doc.document_id,
                           version_label=doc.version_label, dictionary_version=snap.version,
                           candidates_considered=total, proposals=proposals,
                           cost_usd=round(sum(p.cost_usd or 0 for p in proposals), 4))
