# Handover Spec — Credit Agreement Extraction & Versioning Pipeline

**Owner:** Vikkas Arun Pareek
**Audience:** engineers operating and extending the pipeline
**Status:** Build spec for POC → production-ready pipeline
**Last updated:** 21 Sep 2026 (Rev 2)

### Rev 2 change log (design review incorporated)
- **Grounding made implementable:** added an independent OCR stage + verbatim `source_text`; grounding is tri-state, tolerant of normalisation (§6 Stage 2, §15).
- **Licence fix:** `pymupdf` (AGPL) → `pypdfium2`; added licence/offline table and a CI licence gate (§5).
- **Dictionary poisoning fix:** fuzzy/embedding matches no longer auto-create aliases (§9).
- **Eval made runnable and meaningful:** seed core dictionary at milestone 1, metric definitions, sample-size floor, confidence intervals (§13, §20).
- **Parity risk moved forward:** a real local VLM and degraded-scan inputs are in the first vertical slice (§3, §19, §20).
- **New stage:** cross-page assembly + completeness check (§6 Stage 4). Execution-status detection moved here.
- **Extraction robustness:** fixed output envelope, tiling, retry/truncation policy (§8).
- **Confidence:** routing uses agreement signals + calibration, not model self-report alone (§15).
- **Diff:** explicit clause-alignment algorithm, table diffs, source refs on both sides, comparability warnings; amendments are extracted but **not auto-applied** in the POC (§11).
- **Contracts reworked:** typed values, instance keys, richer tables, provenance, review records (§10).
- **Text-layer probe** defined (§17). Persistence moved early in the build order (§20).

---

## 1. Goal

Build a document-intelligence pipeline that takes a credit / facility agreement (image-based, OCR-required PDF in production) and:

1. Extracts **everything extractable** — parties, dates, defined terms, clauses, tables, financial terms (facility amount, margin/pricing, maturity, covenants, events of default, etc.) — into structured JSON key-value form.
2. Reconciles every document against a growing **field dictionary** so field names are normalised, new fields are captured, and expected fields are gap-checked.
3. Enriches each field with **business-contextual meaning** (what it means, why it matters for credit risk, how it's used downstream) — curated once, reused across documents.
4. Attaches **per-field / per-cell confidence** and **source grounding** so low-confidence or unverifiable extractions route to human review.
5. Handles **versioning**: given two or more versions of the same agreement (Original → A&R → 2nd A&R …), produces a structured, audit-friendly diff of what changed.
6. Hands the structured output to a **downstream tool** (integration out of scope; expose a clean JSON output + an `emit()` hook).

This is a credit-risk use case. Auditability, traceability, source-grounding, and not silently dropping content matter more than raw speed. **The human-review loop is part of the product, not a nicety (see §14).** "Not silently dropping content" is enforced mechanically by the completeness check (§6 Stage 4), not left as an aspiration.

---

## 2. Where a model is (and isn't) used

Two kinds of model appear: **generative** (VLM/LLM — swappable, the parity risk) and **non-generative ML** (OCR, embeddings — fixed, local, but still models for governance inventory purposes, §16).

| Step | Model? | Notes |
|---|---|---|
| Rasterise PDF → page images | **No** | Deterministic (`pypdfium2`). CPU only. |
| Independent OCR of page images | **Non-generative ML** | Local OCR engine → word-level text + boxes. Used for grounding, completeness, and the text-layer probe — **never** as the extraction source of truth. |
| Extract text + tables + structure from page image | **Yes — VLM** | Vision-language model reads layout + text + tables together. This is the swappable model (frontier for demo → on-prem SLM for prod). |
| Assemble pages → document tree, completeness check | **No** | Deterministic stitching + OCR-token coverage. |
| Defined-term resolution | **No** | Pattern + string matching over assembled clause text. |
| Reconcile extracted fields against the dictionary | **Non-generative ML** | Exact/alias matching is deterministic; fuzzy is `rapidfuzz`; the semantic tier uses a local embedding model. |
| Business-context enrichment of a field | **Yes — LLM/SLM** | Cheap text-only pass. Proposes business meaning; human approves; stored once in the dictionary. |
| Source grounding / value verification | **No** (consumes OCR output) | Deterministic check of the quoted `source_text` against the OCR text of the cited page. |
| Version diff (restatements) | **No** (embeddings optional in alignment) | Structured JSON diff over aligned clauses/fields/tables. |
| Amendment-instruction extraction | **Yes — VLM** | Same extraction path; instructions are captured as fields, **not applied** (§11). |

Generative steps: extraction and enrichment only.

---

## 3. Hard constraints (these shape the architecture)

| Constraint | Implication |
|---|---|
| **Production runs on the organisation's network. No public/frontier LLM may see real customer agreements.** | Extraction + enrichment models must be swappable to a **self-hosted SLM/VLM** served on-prem (vLLM/Ollama). No SaaS document-AI in the prod path. |
| **Demo/POC may use a public sample document.** | For the POC we use public SEC agreements and may use a frontier model to establish an accuracy ceiling — **but the pipeline must be model-agnostic** so frontier→SLM is a config change, not a rewrite. |
| **Production docs are image-based (scanned) PDFs.** | Assume no reliable text layer. Rasterise pages to images and use the vision path. Test on **degraded** images, not just clean renders (§19). |
| **Air-gapped / restricted network in prod.** | Permissive-licence (MIT/Apache/BSD) dependencies that run fully offline. **Enforced, not just preferred** — see the licence/offline table and CI gate in §5. |
| **Credit-risk model governance applies (see §16).** | How this system is classified (model vs assist) determines the approval path. Settle it early. |

### The parity risk (design principle #1)
Biggest risk: demo works on a frontier model, then the on-prem SLM underperforms and it fails in production. **Mitigate structurally:**
- All model access goes through **one gateway interface** (LiteLLM). Frontier and SLM are config entries.
- Support running **two models side-by-side on the same input** and reporting a comparison (`eval` mode, §12). The demo must show the SLM candidate *next to* the frontier model so any gap is visible before sign-off.
- **A real local open-weights VLM is wired in the first vertical slice (§20, milestone 4) — not a placeholder.** The first parity reading happens at milestone 8, not at the end.
- **Both models are measured on clean *and* degraded page images (§19).** Clean HTML→PDF renders are not scans; a ceiling measured only on them is optimistic twice over.

---

## 4. Framework & model-abstraction decision

- **Model gateway: one `extract()` seam with a provider adapter per model (Rev 2.1, 21 Sep 2026).** Model choice is config-driven (`models.yaml` `provider` + `model_name`). This is the seam that de-risks frontier→SLM migration. The frontier adapter uses the official Anthropic SDK directly (native structured outputs, `effort`, typed stop reasons); the on-prem adapter will target vLLM/Ollama's OpenAI-compatible endpoint (LiteLLM remains an option there). *Rev 2 required LiteLLM; changed because an OpenAI-style shim hides the frontier model's structured-output and stop-reason semantics, which the failure policy (§8) depends on.*
- **Orchestration: plain Python for the POC.** The pipeline is largely linear. Do **not** pull in LangGraph/LangChain yet.
- **LangGraph seam (optional, later):** Structure the pipeline as discrete, independently callable stages with typed I/O so LangGraph can be layered on **without a rewrite** when we need retry loops, low-confidence re-extraction, human-in-the-loop review gates, or amendment-vs-restatement branching. Keep stages pure and side-effect-light.

---

## 5. Recommended tech stack

- **Language:** Python 3.11+
- **Model gateway:** `litellm`
- **PDF → image rasterisation:** `pypdfium2`. *(Not `pymupdf` — AGPL. Not `pdf2image`/poppler — GPL.)*
- **Independent OCR:** behind an `OcrEngine` interface. POC default: Tesseract (CPU, fully offline, word-level boxes). Evaluate PaddleOCR / docTR if accuracy on noisy scans is insufficient for grounding.
- **On-prem serving (prod):** vLLM (preferred) or Ollama — both expose OpenAI-compatible endpoints LiteLLM targets directly.
- **Field matching / normalisation:** `rapidfuzz` (fuzzy) + `sentence-transformers` (semantic similarity), both local and free.
- **Clause alignment (diff):** `rapidfuzz` + embeddings + `scipy.optimize.linear_sum_assignment`.
- **Image degradation (test only):** Pillow/OpenCV transforms (skew, blur, noise, JPEG, contrast loss, stamps).
- **Schema / validation:** `pydantic` v2 for all data contracts (§10).
- **Dictionary + golden-set + lineage store:** start with versioned JSON/SQLite; keep the interface abstract so it can move to a proper store later.
- **Config:** `pydantic-settings` + `models.yaml`.
- **CLI:** `typer`. **Tests:** `pytest`.

### Licence & offline posture (enforced)

| Dependency | Licence | Offline note |
|---|---|---|
| `pypdfium2` | Apache-2.0 / BSD-3 | Bundled binary; no network. |
| Tesseract + `pytesseract` | Apache-2.0 | Language data shipped with the image. |
| `litellm` | MIT (core) | Set `LITELLM_LOCAL_MODEL_COST_MAP=True` (otherwise fetches a cost map from GitHub); disable telemetry/callbacks. |
| `sentence-transformers` | Apache-2.0 (check the **weights'** licence separately) | Vendor the model weights; set `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`. |
| `rapidfuzz`, `pydantic`, `typer` | MIT | — |
| `scipy`, `opencv-python`, `Pillow` | BSD / Apache-2.0 / HPND | — |
| Open-weights VLM | **Varies by model and size** | Check per model; not every size in a family is Apache-2.0. |

- **CI licence gate:** run `pip-licenses` (or equivalent) in CI; fail the build on GPL/AGPL/unknown.
- **Supply chain:** pin exact versions **with hashes** (`--require-hashes`), install from an internal mirror. The gateway sits on every model call and holds credentials — treat it as security-sensitive.
- **Offline test:** one CI job runs the full pipeline (local model stub) with networking disabled. Anything that phones home fails there, not in prod.
- Keep a separate `requirements-prod.txt` excluding any frontier-only SDKs.

---

## 6. Pipeline architecture

Stages in order. Each is an independently callable module with typed I/O. **Persistence is a cross-cutting service available from milestone 2** (content-addressed blobs + run records); Stage 9 is the final versioned-record write, not the first time anything is saved.

**Stage 0 — Ingest**
- Accept a PDF path + `document_id` + `version_label` + optional `effective_date`.
- Store raw input under a content-addressed path (SHA-256) for lineage.
- Pin the **dictionary snapshot version** for this run (§9) and record it in provenance.

**Stage 1 — Rasterise (no model)**
- Convert each page to an image at **300 DPI** (clause text is small). This is the archival/OCR resolution.
- **What the VLM sees is a separate concern:** most VLMs downscale large images, which makes 300 DPI clause text illegible if a full page is sent as one image. Per-model image limits and tiling live in `models.yaml` (§18) and are applied in Stage 3.
- Run the **text-layer probe** (§17) and record the result per page.
- Output: ordered page images + page metadata.
- *POC note:* SEC test docs are clean text/HTML. Convert to PDF then rasterise anyway — this exercises the exact image→VLM path production will use. Also produce a **degraded variant** of each test doc (§19).

**Stage 2 — Independent OCR (non-generative, local)**
- Run the OCR engine over every page image → word-level tokens with boxes, stored per page.
- Purpose: (a) the independent text source grounding checks against (§15); (b) the denominator for the completeness check (Stage 4); (c) cut-line selection for tiling; (d) the text-layer probe comparison.
- OCR output is **never** used as the extraction result. It is the second witness.

**Stage 3 — VLM extraction (model)** — see §8 for the two-pass detail
- For each page (or tile, where the page exceeds the model's image budget), call the configured VLM with the image and an **open-ended extraction prompt**, returning the **fixed output envelope** (§8).
- **Every field value must carry a source location and a verbatim `source_text` quote** — this is the anti-hallucination anchor (§15). Model-supplied bounding boxes are optional and untrusted; the authoritative box comes from matching the quote against OCR.
- **Tables are extracted as structured data** (header cells, spans, rows/cells as JSON), not flattened to prose. Financial tables (pricing grids, amortisation schedules) are the highest-error area.
- Each page result carries `continues_from_previous` / `continues_on_next` markers for the first/last block and for tables, to support assembly.
- Malformed/truncated output follows the retry policy in §8. A page is never silently skipped.

**Stage 4 — Assemble & completeness check (no model)**
- **Stitch** per-page results into `document → sections → clauses → fields[]` plus the **flat field list**: merge clauses that continue across page boundaries; merge multi-page tables (continuation markers, repeated header rows) into one `Table` with `pages[]`; de-duplicate tile overlaps by OCR position; validate clause-numbering sequence against OCR text and flag gaps.
- **Completeness:** per page, compute `covered_token_ratio` = share of OCR tokens accounted for by extracted clause text, table cells, and field quotes. Below threshold → re-extract with finer tiling → still low → page status `low_coverage` and routed to review. Pages with zero output are `failed`, blank/signature-only pages are `blank`. The per-page coverage report is persisted with the extraction.
- **Document status:** derive executed vs draft/specimen and signature-block presence from the extracted content (signature pages, "EXECUTION VERSION"/"DRAFT" markers). Extraction from a draft is not the same as from an executed agreement — record it.
- **Document kind:** classify `restatement` vs `amendment` (title/structure heuristic) — drives §11 branching.

**Stage 5 — Defined-term resolution (no model)** — credit-agreement-specific, do not skip
- These documents are heavily self-referential: "Applicable Margin", "Permitted Indebtedness", etc. are *defined once* (usually a definitions section) and used throughout.
- Build a **defined-terms map** from the assembled definitions section (`"Term" means …` patterns, plus inline `(the "Term")` definitions), then link each in-text use back to its definition. The business value of a term lives in its definition, not the surface mention.
- Where a definition points at a table (e.g. Applicable Margin → pricing grid) or at other defined terms, record the reference so the field's value can be a `table_ref` / `term_ref` (§10) rather than a lossy string.
- Output: each defined term resolved to `{term, definition, definition_location, references[], usages[]}`.

**Stage 6 — Reconcile against field dictionary** — see §9
- Match each extracted field to a **canonical field** (exact → approved alias → fuzzy `rapidfuzz` → embedding `sentence-transformers`), recording `match_method` and `match_score`.
- Known variant ("Interest Margin" ≈ "Applicable Margin") → map to canonical name.
- Fuzzy/embedding matches are **proposals**: the mapping is used for this document, flagged for review, and the alias enters the dictionary as `pending_review` — never auto-approved (§9).
- Genuinely new field → dictionary proposal as a `pending_review` entry.
- Assign an **`instance_key`** where a canonical field repeats (per tranche, per lender, per test date).
- Run a **coverage check**: were the `core` fields expected for this doc type found? Flag gaps (e.g. a missing covenant).

**Stage 7 — Business-context enrichment (model, reused)** — see §9
- For any canonical field lacking an approved business meaning, run a cheap **text-only LLM/SLM pass** proposing: business meaning, credit-risk relevance, typical downstream use.
- Store the meaning **in the dictionary against the canonical field, not per-document** — stable across documents, curated once, human-approvable, reused for free.

**Stage 8 — Grounding & confidence routing (no model)** — see §15
- Verify each field's `source_text` against the OCR text of the cited page, then verify the normalised value is derivable from the quote. Result is tri-state: `verified | not_found | not_checked`. `not_found` → possible hallucination, routed to review **regardless of model confidence**.
- Run **table arithmetic checks** where applicable (commitments sum to facility amount; amortisation sums to principal; grid monotonicity).
- Compute a **`routing_score`** from agreement signals + calibrated confidence (§15). Route sub-threshold, unverified, low-coverage, and proposed-mapping fields to human review. Separate **vision/OCR-noise** flags from genuine low-confidence semantic extraction.

**Stage 9 — Persist**
- Store versioned extraction JSON keyed by `(document_id, version_label)` with full **provenance** (source hash, model id + params, prompt versions, dictionary version, pipeline version, OCR engine).
- Keep **full lineage** — never overwrite prior versions; re-extractions of the same version are new runs, not replacements. Must answer "what did the margin look like as of version N / date D?"

**Stage 10 — Version diff (no model)** — only when ≥2 versions exist. See §11.

**Stage 11 — Hand-off**
- Expose the final structured JSON via a clean `emit(payload)` function/callback. Downstream integration out of scope.

---

## 7. Difficulty & realistic accuracy (set expectations)

- **Difficulty: medium.** Engineering is mature-parts assembly (rasterise → OCR + VLM → JSON → diff). The hard 20% is table accuracy, cross-page assembly, clause alignment for the diff, defined-term resolution, and the frontier→SLM parity gap.
- **The figures below are planning assumptions, not measurements.** They must not be quoted to governance or the requester as results; only §13 numbers may be.
  - Clean digital PDF, frontier VLM: ~90–95%+ on well-defined fields.
  - Scanned/image PDF (prod case), frontier VLM: ~85–92% on clear fields; **complex financial tables ~70–85%** (main error source).
  - On-prem SLM: typically **5–15 points below** frontier — the reason `eval` mode exists.
  - Open-ended "extract everything" is inherently fuzzier than a fixed schema.
- **Correct framing for governance and the requester:** not "95% accurate autopilot" but **"high-confidence, source-grounded fields auto-accepted; everything below threshold or unverifiable goes to human review."** That is the only defensible posture for credit-risk data.

---

## 8. Extraction design — two-pass (important)

Keep the **extraction prompt** and the **field dictionary** strictly separate. Do not feed the dictionary into Pass 1 — a checklist makes the model stop hunting once boxes are ticked, defeating "extract everything."

- **Pass 1 — open-ended (always).** Prompt: *"Extract every field, value, party, date, defined term, clause, and table you find in this page. Do not limit yourself to any predefined list. For each field include the value as printed, a verbatim quote of the supporting text, confidence (0–1), and source location."*
- **Pass 2 — schema-hinted sweep (optional, recommended).** Feed the canonical field list **from the pinned dictionary snapshot**: *"Are any of these known fields present that the first pass missed or phrased differently?"*
- Merge Pass 1 + Pass 2, de-duplicate. **Record whether the two passes agree on each value** — that agreement is a routing signal (§15). Then hand to assembly → defined-term resolution → reconciliation.

### Output envelope (fixed shape, open names)
"JSON-only" prompting is not sufficient for small models. Use **structured outputs / guided decoding** (LiteLLM `response_format`; vLLM guided JSON) with a **fixed envelope schema** whose *field names remain open*:

```json
{
  "blocks":  [{"kind": "heading|clause|other", "number": "6.12(b)", "heading": "...", "text": "...",
               "continues_from_previous": false, "continues_on_next": true}],
  "fields":  [{"name": "...", "raw_value": "...", "source_text": "...", "confidence": 0.0, "block_index": 0}],
  "tables":  [{"caption": "...", "continues_from_previous": false, "continues_on_next": false,
               "cells": [{"row": 0, "col": 0, "row_span": 1, "col_span": 1, "is_header": true, "raw_value": "..."}]}],
  "parties": [{"name": "...", "role": "...", "source_text": "..."}]
}
```

The schema constrains the *shape*; it does not enumerate fields, so "extract everything" is preserved.

### Image budget & tiling
- `models.yaml` declares per model: max image long-edge / pixel budget, max output tokens, structured-output support.
- If a 300-DPI page exceeds the model's budget, split into overlapping horizontal bands (~10% overlap). Choose cut lines at OCR whitespace gaps so tables and paragraphs are not sliced. Overlap duplicates are removed in Stage 4.

### Failure policy (a page is never silently dropped)
1. Invalid/unparseable JSON → retry once with the same input. *(Do not hard-code `temperature=0`: current Claude models reject sampling params with a 400. Sampling/effort settings are per-model in `models.yaml`.)*
2. Truncated output (hit max tokens) → re-run with finer tiling.
3. Still failing → page status `failed`, routed to review, surfaced in the run summary and in `emit()` output.
- All attempts (prompt version, params, raw response) are persisted for audit.

### Prompt scaffolding to implement (versioned templates in `/prompts/`)
- **Extraction (Pass 1):** open-ended, envelope-constrained, confidence + `source_text` per field, tables as structured cells.
- **Schema-hint (Pass 2):** inject canonical field names + short descriptions; presence/values only, same envelope.
- **Enrichment (Stage 7):** given field name + example values + surrounding clause text → `{business_meaning, credit_risk_relevance, downstream_use}`.
- Every template has a version id; the ids used in a run are recorded in provenance.

---

## 9. Field dictionary (the memory layer)

A persistent, versioned registry that turns one-off extraction into a compounding asset — makes document N+1 consistent, cheaper, and gap-checked.

Each entry: `canonical_name`, `aliases[]` (each with its own status), `business_meaning`, `credit_risk_relevance`, `downstream_use`, `typical_location`, `data_type`/expected format, `core` (expected for coverage/acceptance), `status` (`approved` | `pending_review`), `first_seen`, `last_seen`, `version`.

### Seed dictionary (milestone 1)
Start with a hand-written **core set of ~20–30 canonical fields** (facility amount, borrower, administrative agent, maturity date, applicable margin, commitment fee, financial covenants, etc.), all `approved`, flagged `core`. This (a) gives the golden set stable keys before reconciliation exists, (b) makes the coverage check meaningful from day one, and (c) is the concrete answer to the "target taxonomy" and acceptance-criteria questions (§21, §23).

### Reconciliation rules (poisoning-safe)
Order: exact → **approved** alias → fuzzy → embedding.

| Match tier | Mapping applied to this document? | Dictionary effect |
|---|---|---|
| Exact / approved alias | Yes, automatically | `last_seen` updated |
| Fuzzy / embedding ≥ threshold | Yes, but field flagged `needs_review` with `match_method` + score | Surface form proposed as alias with status **`pending_review`** |
| Below threshold | No (`canonical_name = None`) | New entry proposed as `pending_review` |

**Why:** credit agreements are full of near-identical names with different meanings — *Consolidated EBITDA* vs *Consolidated Adjusted EBITDA*, *Total Leverage Ratio* vs *Senior Secured Leverage Ratio*, *Base Rate Margin* vs *SOFR Margin*. Embeddings score these very high. If a fuzzy match auto-created an alias, one wrong match would become a permanent *exact* match. Only a human approval promotes an alias.

Maintain a small **"confusable pairs" negative list** (pairs that must never be merged); a match across a confusable pair is always rejected to `pending_review`.

### Snapshots & reproducibility
- The dictionary is versioned as immutable **snapshots**. A run pins one snapshot at Stage 0; all matching and Pass 2 hints use it.
- A run never mutates the dictionary in place. It emits **proposals** (new entries, pending aliases, proposed meanings); applying approved proposals creates the next snapshot.
- Consequence: re-running the same document with the same model, prompts, and snapshot is reproducible, and `eval` comparisons are apples-to-apples.

---

## 10. Data contracts (pydantic)

Illustrative — refine as you build. Amounts are `Decimal` (serialised as strings), never `float`.

```python
Confidence = Annotated[float, Field(ge=0.0, le=1.0)]

class GroundingStatus(str, Enum):
    not_checked = "not_checked"
    verified = "verified"           # quote found on cited page AND value derivable from quote
    not_found = "not_found"         # possible hallucination → always routed to review

class SourceRef(BaseModel):
    page: int                       # 1-based
    source_text: str                # verbatim supporting quote — required
    region_id: str | None = None
    bbox: tuple[float, float, float, float] | None = None   # from the OCR match, not trusted from the VLM
    grounding: GroundingStatus = GroundingStatus.not_checked
    match_score: float | None = None                        # quote ↔ OCR similarity

class ValueType(str, Enum):
    text = "text"; money = "money"; percentage = "percentage"; bps = "bps"; ratio = "ratio"
    date = "date"; duration = "duration"; integer = "integer"; boolean = "boolean"
    table_ref = "table_ref"         # e.g. Applicable Margin defined by a pricing grid
    term_ref = "term_ref"           # value is another defined term

class NormalisedValue(BaseModel):
    type: ValueType
    amount: Decimal | None = None   # money / percentage / bps / ratio / integer / duration
    currency: str | None = None     # ISO 4217
    unit: str | None = None         # "%", "bps", "x", "days", "months", ...
    date_value: date | None = None
    bool_value: bool | None = None
    text: str | None = None
    ref_id: str | None = None       # table_id or defined term

class AgreementSignals(BaseModel):
    pass1_pass2_agree: bool | None = None
    ocr_agree: bool | None = None           # == grounding verified
    second_model_agree: bool | None = None  # eval / dual-model runs
    arithmetic_check: bool | None = None    # table/sum checks where applicable

class ExtractedField(BaseModel):
    field_id: str                   # unique within the extraction
    name: str                       # raw surface form as returned
    canonical_name: str | None = None
    match_method: Literal["exact", "alias", "fuzzy", "embedding", "none"] = "none"
    match_score: float | None = None
    instance_key: str | None = None # disambiguates repeats: tranche / lender / test date
    clause_id: str | None = None
    raw_value: str | None           # as printed
    value: NormalisedValue | None   # deterministic normalisation of raw_value
    model_confidence: Confidence    # model self-report — weak signal on its own
    agreement: AgreementSignals = AgreementSignals()
    routing_score: Confidence | None = None   # computed in Stage 8 (§15)
    source: SourceRef               # grounding — required
    vision_noise_flag: bool = False
    origin: Literal["model", "human"] = "model"
    review_status: Literal["auto_accepted", "needs_review", "confirmed", "corrected"] = "needs_review"
    # "hallucination flag" == (source.grounding == not_found); no separate boolean

class TableCell(BaseModel):
    row: int
    col: int
    row_span: int = 1
    col_span: int = 1
    is_header: bool = False
    raw_value: str | None
    value: NormalisedValue | None = None
    model_confidence: Confidence
    source: SourceRef | None = None

class TableCheck(BaseModel):
    name: str                       # e.g. "commitments_sum_to_facility_amount"
    passed: bool
    detail: str | None = None

class Table(BaseModel):
    table_id: str
    caption: str | None = None
    clause_id: str | None = None
    pages: list[int]                # multi-page tables are stitched into one
    n_rows: int
    n_cols: int
    cells: list[TableCell]
    checks: list[TableCheck] = []

class Party(BaseModel):
    name: str
    role: str                       # Borrower, Administrative Agent, Lender, Guarantor, ...
    source: SourceRef

class DefinedTerm(BaseModel):
    term: str
    definition: str | None
    definition_location: SourceRef | None
    references: list[str] = []      # other defined terms / table_ids the definition points at
    usage_locations: list[SourceRef] = []

class Clause(BaseModel):
    clause_id: str                  # stable WITHIN a version: hierarchical path + content hash
    number: str | None              # as printed, e.g. "6.12(b)" — NOT used for cross-version identity
    heading: str | None
    text: str | None
    page_range: tuple[int, int]
    fields: list[ExtractedField]
    tables: list[Table] = []

class Section(BaseModel):
    section_id: str
    title: str | None
    clauses: list[Clause]

class PageCoverage(BaseModel):
    page: int
    status: Literal["ok", "low_coverage", "failed", "blank"]
    ocr_token_count: int
    covered_token_ratio: float
    tiles: int = 1
    attempts: int = 1
    text_layer: Literal["none", "untrusted", "trusted"] = "none"

class Provenance(BaseModel):
    source_sha256: str
    page_count: int
    pipeline_version: str
    model_id: str
    model_params: dict[str, Any]
    prompt_versions: dict[str, str]     # {"pass1": "v3", "pass2": "v1", ...}
    dictionary_version: int
    ocr_engine: str
    run_id: str
    extracted_at: datetime

class DocumentExtraction(BaseModel):
    document_id: str
    version_label: str
    effective_date: date | None
    execution_status: Literal["executed", "draft", "specimen", "unknown"] = "unknown"
    document_kind: Literal["original", "restatement", "amendment", "unknown"] = "unknown"
    parties: list[Party]
    sections: list[Section]
    defined_terms: list[DefinedTerm]
    flat_fields: list[ExtractedField]
    amendment_instructions: list["AmendmentInstruction"] = []
    page_coverage: list[PageCoverage]
    provenance: Provenance

# --- Dictionary ---
class Alias(BaseModel):
    surface_form: str
    status: Literal["approved", "pending_review"] = "pending_review"
    learned_via: Literal["seed", "human", "fuzzy", "embedding"]
    first_seen_document: str | None = None

class FieldDictionaryEntry(BaseModel):
    canonical_name: str
    aliases: list[Alias] = []
    business_meaning: str | None = None
    credit_risk_relevance: str | None = None
    downstream_use: str | None = None
    typical_location: str | None = None
    data_type: ValueType | None = None
    core: bool = False              # expected for this doc type → coverage check + acceptance
    status: Literal["approved", "pending_review"] = "pending_review"
    first_seen: date
    last_seen: date
    version: int = 1

# --- Human review (§14) ---
class ReviewRecord(BaseModel):
    review_id: str
    document_id: str | None
    version_label: str | None
    target_kind: Literal["field", "table_cell", "clause_alignment", "dictionary_entry", "alias", "page"]
    target_id: str
    reason_flagged: str             # not_found / low routing_score / proposed mapping / low_coverage / ...
    decision: Literal["confirmed", "corrected", "rejected"]
    old_value: str | None
    new_value: str | None
    reviewer: str
    reviewed_at: datetime
    notes: str | None = None

# --- Amendments (§11) ---
class AmendmentInstruction(BaseModel):
    target_ref: str                 # as printed: 'Section 6.12(b)', 'definition of "Applicable Margin"'
    operation: Literal["replace", "delete", "insert", "restate", "other"]
    old_text: str | None
    new_text: str | None
    source: SourceRef

# --- Diff (§11) ---
class ClauseAlignment(BaseModel):
    from_clause_id: str | None
    to_clause_id: str | None
    status: Literal["matched", "added", "removed"]
    score: float | None
    text_changed: bool = False
    low_confidence_alignment: bool = False

class FieldChange(BaseModel):
    canonical_name: str             # unmapped fields are diffed by raw name within aligned clauses
    instance_key: str | None
    from_clause_id: str | None
    to_clause_id: str | None
    old_value: NormalisedValue | None
    new_value: NormalisedValue | None
    old_source: SourceRef | None    # audit: show both sides
    new_source: SourceRef | None
    change_type: Literal["added", "removed", "modified", "unchanged"]
    confidence: Confidence          # min(routing_score) of the two sides
    low_confidence_change: bool

class CellChange(BaseModel):
    row_header: str | None          # cells aligned by header labels, not indices
    col_header: str | None
    old_value: NormalisedValue | None
    new_value: NormalisedValue | None
    change_type: Literal["added", "removed", "modified"]

class TableChange(BaseModel):
    from_table_id: str | None
    to_table_id: str | None
    cell_changes: list[CellChange]

class VersionDiff(BaseModel):
    document_id: str
    from_version: str
    to_version: str
    from_provenance: Provenance
    to_provenance: Provenance
    comparability_warnings: list[str]   # different model / prompt / dictionary versions between sides
    clause_alignments: list[ClauseAlignment]
    changes: list[FieldChange]
    table_changes: list[TableChange]

# --- Ground truth & evaluation (§13) ---
class GoldenField(BaseModel):
    document_id: str
    version_label: str
    canonical_name: str
    instance_key: str | None = None
    expected_value: NormalisedValue | None
    source_page: int

class GoldenTable(BaseModel):
    document_id: str
    version_label: str
    caption: str | None
    pages: list[int]
    cells: list[tuple[int, int, str | None]]    # (row, col, expected raw value)

class EvalResult(BaseModel):
    model_id: str
    document_id: str
    input_variant: Literal["clean", "degraded"]
    provenance: Provenance
    n_golden: int
    recall: float                       # golden fields found with the correct value
    precision_on_golden_keys: float     # of extracted fields mapped to a golden key, share correct
    audited_precision: float | None     # sampled human audit of open-ended (non-golden) fields
    table_cell_accuracy: float | None
    hallucination_rate: float           # share of extracted fields with grounding == not_found
    ci95: dict[str, tuple[float, float]]    # Wilson intervals per metric
    calibration_error: float | None     # routing_score vs observed correctness
    cost_per_page: float | None
    latency_per_doc_s: float | None
    per_field: dict[str, bool]          # key = f"{canonical_name}|{instance_key or ''}"
```

---

## 11. Versioning & diff logic

Versions are a **known lineage** — identity is given, not detected.

- **Diff at the structured level, not raw text.** Align clauses by content identity and fields by `(canonical_name, instance_key)`, then compare **normalised** values. Survives vision noise and page-shift.
- **Normalise before diffing** (whitespace, currency/number formatting, casing) — this is the same deterministic normaliser that produces `NormalisedValue`, so artefacts don't register as real changes.
- **Tables are diffed too.** A pricing-grid change is the most common amendment. Align tables by caption/clause, align cells by **row/column header labels** (not indices), emit `TableChange`.
- **Both sides carry source refs** (`old_source`, `new_source`) so a reviewer can open both pages. That is what "audit-friendly" means here.
- **Comparability warnings:** if the two extractions used different models, prompt versions, or dictionary snapshots, say so in the diff — some "changes" may be extraction drift. Preferred practice: re-extract both versions under the same provenance before diffing.
- **Flag low-confidence changes separately** (`low_confidence_change`), and low-confidence *alignments* (`low_confidence_alignment`).
- **Capture effective/execution date per version** as a first-class field.

### Clause alignment algorithm (renumbering-safe)
`clause_id` is stable only *within* a version. Cross-version identity comes from alignment, never from clause numbers (a restatement may renumber everything).

1. Align **sections** by title similarity.
2. Within aligned sections (then globally for leftovers), score every clause pair:
   `score = w1·heading_similarity (rapidfuzz) + w2·body_similarity (embedding or TF-IDF cosine) + w3·defined_term_overlap (Jaccard)`.
3. Solve one-to-one assignment (`linear_sum_assignment`) with a **minimum-score floor**; unmatched → `added` / `removed`.
4. Record the score on every alignment. Scores in the grey band → `low_confidence_alignment` → human review.
5. Field/table diffs run only inside matched clauses; fields in added/removed clauses are reported as added/removed.

Stress-test explicitly on the IEC restatement series, and with a synthetic test that renumbers and reorders clauses without changing content (expected diff: empty).

### Amendment vs restatement
- **Restatement** = full re-extract + diff (above). This is the POC's diff path.
- **Amendment** (delta document: *"Section 6.12(b) is hereby amended by deleting '3.50' and substituting '4.00'"*) — **POC scope: extract, don't apply.** The amendment is run through the normal pipeline; its operative instructions are captured as `AmendmentInstruction` records and shown alongside the prior version. Reliably *applying* instructions to prior structured JSON is a hard, model-assisted problem and needs its own human-review gate — deferred (future LangGraph node; see §22, §23).
- The IEC series ends with a First Amendment — use it to exercise instruction extraction, and the four restatements to exercise the diff.

---

## 12. Run modes (CLI)

- `extract` — full pipeline on one document → versioned JSON (+ dictionary **proposals**). `--dictionary-version` to pin a snapshot (default: latest).
- `diff` — two `(document_id, version_label)` pairs → structured diff (fields, tables, alignments, comparability warnings).
- `eval` — **run two models on the same document (clean + degraded variants) → comparison report** against the golden set (§13) with confidence intervals, calibration, cost/latency. The parity check; must appear in the demo.
- `dict` — inspect / review / approve dictionary entries, pending aliases, and business meanings; apply approved proposals → new snapshot.
- `review` — export flagged (low-confidence / unverified / proposed-mapping / low-coverage) items for human review and re-ingest corrections as `ReviewRecord`s (§14).
- `export-audit` — bundle governance artefacts for a run or date range (§16).

---

## 13. Evaluation & ground truth (do this early — you cannot claim accuracy without it)

- **No accuracy number is real without labelled ground truth.** Build a **golden set** on the SEC test docs, keyed by the **seed dictionary's canonical names** (§9) so it works before reconciliation exists.
- **Size matters.** 50 labelled fields gives roughly ±8 points (95% CI) at 90% accuracy — too coarse to resolve the 5–15 point frontier↔SLM gap this harness exists to measure. **Target ≥ 300 labelled field instances** across ≥ 6 documents (≈ ±3.5 points), plus **5–10 tables labelled cell-by-cell** (`GoldenTable`). Start with ~50 to get the harness working; grow to the floor before quoting numbers.
- **Model comparison is paired:** both models are scored on the same golden fields; report the paired difference (McNemar) and Wilson CIs, not two independent percentages.

### Metric definitions (open-ended extraction makes these non-obvious)
- **Recall** — share of golden fields extracted with the correct value.
- **Precision on golden keys** — of extracted fields that map to a golden `(canonical_name, instance_key)`, share with the correct value.
- **Audited precision** — open-ended fields *outside* the golden set are neither TP nor FP by construction. Estimate their precision by human audit of a random sample (e.g. 50 per model per release).
- **Value equality** — compared on `NormalisedValue` (typed, exact for money/dates/percentages; normalised fuzzy match for free text with a stated threshold).
- **Table cell accuracy** — share of golden cells matched at the right (row, col) with the correct value.
- **Hallucination rate** — share of extracted fields with `grounding == not_found`. Report alongside the share of those confirmed wrong on review (grounding false-alarm rate).
- **Calibration** — does `routing_score ≥ t` actually deliver the promised precision? This is what justifies the auto-accept threshold.
- **Cost/page and latency/doc** per model.

- Every metric is reported per model × input variant (**clean** and **degraded**, §19).
- Track metrics over time as prompts/models change (guard against regressions). Eval runs pin the dictionary snapshot.
- Golden-set labelling is unglamorous but is the single highest-leverage task for credibility. Budget for it.

---

## 14. Human-review workflow (this IS the product, not out of scope)

- Confidence/grounding routing only delivers value if flagged fields have somewhere to go and someone to act.
- Minimum viable loop: **export flagged items → human corrects → corrections write back** to (a) the document record (`origin = "human"`, `review_status = corrected|confirmed`) and (b) the field dictionary (alias approvals, approved meanings, new entries → next snapshot).
- **Every decision is a persisted `ReviewRecord`** (who, when, why flagged, old → new). These are governance artefacts (§16) *and* the labelled data that calibrates routing (§15) and grows the golden set.
- What gets routed: `grounding == not_found`; sub-threshold `routing_score`; fuzzy/embedding dictionary mappings; failed table checks; `low_coverage` / `failed` pages; low-confidence clause alignments.
- The correction feedback is what makes the system improve over time — treat it as core, not a footnote.
- For the POC a simple table/CSV export + re-ingest is enough; note a proper review UI as a fast-follow.

---

## 15. Hallucination grounding & confidence (credit-risk critical)

- VLMs confidently invent values — a plausible margin that isn't there, a subtly wrong date. **High model confidence does not mean correct.** Confidence scores alone will not catch this.
- **Hard rule:** every field value must be **verifiable at its cited source location**. "Show me where you got this" (page + quote) is non-negotiable for anything near a credit decision. Grounding is a validation gate, not optional metadata.

### How grounding actually works (image-only PDFs have no text to check against)
The check needs a **second, independent witness**: the OCR output from Stage 2.

1. **Quote check** — fuzzy-match the field's `source_text` against the OCR text of the cited page (± adjacent page for boundary cases). Tolerance is tuned to OCR noise (`match_score` recorded). On a match, take the **bbox from the OCR tokens** — model-supplied boxes are untrusted.
2. **Value check** — confirm the normalised value is derivable from the quote using the deterministic normaliser: `"$50,000,000"`, `"50,000,000 Dollars"`, and `"Fifty Million Dollars ($50,000,000)"` must all verify against `money(50000000, USD)`.
3. Both pass → `verified`. Quote not found, or value not derivable from it → `not_found` → review, regardless of confidence. Not run → `not_checked` (never treated as verified).

- Where the text-layer probe marks a page's text layer `trusted` (§17), use it instead of OCR as the witness — it is strictly better text.
- Track the grounding **false-alarm rate** (flagged `not_found` but actually correct — usually OCR noise). If it is high, the OCR engine, not the VLM, is the problem.

### Routing score (don't trust self-reported confidence alone)
Model self-reported confidence — per-cell especially — is weakly calibrated. `routing_score` combines:
- grounding status (**hard gate** — no auto-accept without `verified`);
- **agreement signals**: Pass 1 vs Pass 2, second model (where run), table arithmetic checks;
- dictionary match tier (fuzzy/embedding mappings never auto-accept);
- `model_confidence` as one weak input.

Start with transparent rule-based tiers. Once the golden set and `ReviewRecord`s provide enough labels, **calibrate** the score against observed correctness and set the auto-accept threshold to hit the agreed precision (§21). Report calibration in `eval`.

---

## 16. Model governance & classification (longest pole — frame it now)

- This is a bank. Anything touching credit-risk data or decisions can trigger **model validation, documentation, ongoing monitoring, and formal sign-off** (SR 11-7 / internal model-risk equivalents). POCs that ignore this stall at the production gate.
- **Get the classification agreed up front:** is this a *model* in the governance sense, or a *productivity assist that a human always validates*? The answer changes the entire approval path, documentation burden, and monitoring requirements.
- **Model inventory — list every model, not just the VLM:** extraction VLM, enrichment LLM/SLM, **OCR engine**, **embedding model**. The latter two are fixed and local but will still be asked about.
- Keep the artefacts governance will ask for from day one: prompt versions, model versions + params, dictionary snapshot versions, eval results (with CIs), data lineage, page-coverage reports, raw model responses, `ReviewRecord`s. `Provenance` (§10) plus `export-audit` (§12) make them persisted and exportable.

---

## 17. Cost & latency

- A 300-page doc with per-page VLM calls (more with tiling and Pass 2) is real GPU time and cost, ×many documents.
- Levers:
  - **Batch** page calls.
  - **Cache** extraction results. Cache key = `hash(page image) + model_id + model params + prompt version + tiling config` — a content hash alone would serve stale results after a prompt or model change.
  - Process only changed pages on a known version (OCR-text page hashes identify unchanged pages cheaply).
  - Run Pass 2 only on pages where Pass 1 coverage or the dictionary coverage check suggests something was missed.
- **Text-layer probe (defined):** in Stage 1, check whether the PDF page has an embedded text layer; in Stage 2, compare it with the OCR output. High agreement → `trusted`; present but divergent (bad OCR layer baked into the scan) → `untrusted`; absent → `none`.
  - A `trusted` text layer is used as the **grounding witness** (better than OCR) — always on.
  - Routing `trusted` pages to a cheaper text-only extraction path is a **cost optimisation that is off by default**, and may only be enabled after `eval` shows no quality loss (tables/layout are the risk). Production scans will mostly be `none`/`untrusted`, so do not design around it.
- Measure cost/page and latency/doc in the `eval` harness so the prod economics are known before scale-up.

---

## 18. Model-agnostic design (day one)

- Single interface: `extract(images, prompt, model_id) -> ExtractionResult` over LiteLLM. Same for enrichment.
- `models.yaml` defines each model: id, provider, endpoint, params, **max image long-edge / pixel budget, max output tokens, structured-output support, tiling overrides**. Entries: a frontier model (POC ceiling), an on-prem open-weights VLM served via vLLM (pick a current one and **check the licence for the specific size**), optionally a lighter baseline.
- **POC model choices (development):** frontier slot = `claude-opus-5` (vision, structured outputs, 1M context); cost fallback for bulk iteration = `claude-sonnet-5`; on-prem slot = a current open-weights VLM via Ollama/vLLM until the allow-list is known. Per-model quirks live in `models.yaml`, not in code: Claude models take **no** `temperature`/`top_p` (400), use adaptive thinking + `effort` instead, and can return `stop_reason: "refusal"` — the gateway must check stop reason before parsing, and treat `max_tokens` as the truncation signal (§8).
- No model name hard-coded outside `models.yaml`.
- OCR, assembly, defined-term resolution, reconciliation, dictionary, grounding, diff, and hand-off must all be model-independent.

---

## 19. Test data (public, real, versioned)

Source: **SEC EDGAR**. Exhibits are mostly clean HTML/text — convert to PDF and rasterise to simulate the production image-PDF case.

**Degraded variants (required):** a pristine HTML→PDF render is not a scan. For every test doc also generate a degraded copy — slight skew/rotation, blur, Gaussian + salt-and-pepper noise, JPEG compression, reduced contrast, occasional stamp/handwriting overlay, optional 200-DPI downsample — with a fixed seed so runs are reproducible. All eval metrics are reported on both variants. If real scanned credit agreements can be sourced publicly, add them.

**Versioned lineage (best for diff testing) — IEC Electronics Corp ↔ M&T Bank:**
- Amended & Restated — 16 Dec 2009 · Second A&R — 30 Jul 2010 · Third A&R — 17 Dec 2010 · Fourth A&R — 18 Jan 2013 · First Amendment to Fourth A&R — 15 May 2013
- Restatements exercise the diff (§11); the First Amendment exercises amendment-instruction extraction.

Filing index: `https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0000049728&type=10-K`

**Three standalone agreements:**
- Albemarle A&R Credit Agreement (2022): `https://www.sec.gov/Archives/edgar/data/915913/000091591322000167/exhibit1010930202210q.htm`
- Bank of America A&R Credit Agreement (2014): `https://www.sec.gov/Archives/edgar/data/922863/000119312514398826/d817814dex101.htm`
- Mobivity A&R Credit Facility Agreement (2022): `https://www.sec.gov/Archives/edgar/data/1447380/000149315223028625/ex10-1.htm`

**More versioned pairs:** SEC EDGAR full-text search at `https://efts.sec.gov` — search `"Second Amended and Restated Credit Agreement"` and follow the filer's exhibit history.

**Synthetic v2 for a clean demo:** take one downloaded agreement, produce a lightly edited "v2" (change a margin, relax a covenant, add a clause, **change a pricing-grid cell**), run both through the pipeline, show the diff. Add a second synthetic that only **renumbers/reorders** clauses — expected diff: empty.

> Respect SEC fair-access rules: set a descriptive `User-Agent` header **with contact details** and rate-limit downloads. SEC returns 403 to undeclared automated clients, so the links above have not been machine-verified — confirm them manually at milestone 2 and commit the downloaded files (with hashes) so tests don't depend on the network.

---

## 20. Build order / milestones

1. **Skeleton + contracts** — repo, config, `models.yaml`, pydantic models, CLI stubs, prompt templates, **seed core dictionary (~20–30 fields, §9)**, CI licence gate + offline test job (§5).
2. **Ingest + rasterise + minimal persistence** — PDF → 300 DPI images on the SEC test docs; content-addressed store + run records (everything after this depends on it); degraded-variant generator.
3. **Independent OCR + text-layer probe** — word-level tokens/boxes per page.
4. **LiteLLM gateway** — one `extract()` over LiteLLM; wire one frontier model **and one real local open-weights VLM** (Ollama/vLLM). Envelope schema, image budget/tiling, retry policy.
5. **Pass 1 extraction (single doc)** — open-ended, envelope-constrained, per-field confidence + `source_text`, tables — end-to-end on one agreement. Exact/approved-alias matching against the seed dictionary (enough to key the golden set).
6. **Assemble + completeness** — cross-page stitching, multi-page tables, page-coverage report, execution status.
7. **Grounding gate** — quote check + value check against OCR; tri-state status; deterministic normaliser.
8. **Golden set + eval harness** — start ~50 fields, grow to the ≥300 floor (§13); recall / precision / hallucination with CIs; **both models, clean + degraded** → first parity reading. *(Do this before trusting any output.)*

   **Ship 1–8 as a working, *measured* vertical slice on one document first, then broaden.**

9. **Full reconciliation** — fuzzy + embedding tiers, proposals (never auto-alias), confusable-pairs list, `instance_key`, coverage check, dictionary snapshots.
10. **Defined-term resolution** — including table/term references.
11. **Pass 2 schema-hinted sweep** — merge with Pass 1; record pass agreement.
12. **Enrichment pass + `dict` review mode.**
13. **Human-review loop** — `review` export + write-back, `ReviewRecord`s; routing-score tiers, then calibration.
14. **Versioned lineage store** — full `(document_id, version_label)` history, as-of queries.
15. **Version diff** — alignment algorithm, field + table diffs, comparability warnings, on the IEC restatements and both synthetics; amendment-instruction extraction on the IEC First Amendment.
16. **`eval` mode, full report** — paired frontier-vs-SLM comparison, calibration, cost/latency.
17. **Hand-off hook + `export-audit` + docs.**

---

## 21. Acceptance criteria (agree with the requester before building)

Define the greenlight bar up front, e.g.: *"Extracts the [N] `core` dictionary fields at ≥ [X]% precision (lower bound of the 95% CI) on [Z] sample agreements, clean and degraded; auto-accepted fields meet ≥ [P]% observed precision; zero silently dropped pages; all sub-threshold/unverified fields routed to human review; frontier↔SLM gap ≤ [G] points on the paired comparison; a working version diff (fields + tables) on the IEC restatements with an empty diff on the renumbering synthetic."* Without an agreed bar the POC can succeed technically and still get a "so what". Include: which core fields are mandatory (the seed dictionary is the proposal), the acceptable frontier↔SLM gap, and what governance needs to see.

---

## 22. Out of scope

- The downstream tool that consumes the output (expose clean JSON + `emit()` only).
- Choosing/procuring the final production SLM (design accepts whatever is on the organisation's allow-list).
- Real customer agreements (POC uses public SEC docs only).
- Auto-*detecting* that two arbitrary documents are versions of each other (version identity is given).
- **Auto-*applying* amendment instructions to a prior version** (POC extracts the instructions and presents them; application is a later, human-gated stage — §11).
- A production-grade review UI (POC uses export + re-ingest; note as fast-follow).

---

## 23. Open questions for the requester

- Which self-hosted VLM/SLM(s) are/will be on the organisation's allow-list, and is on-prem GPU + a serving stack (vLLM) already available?
- **Is an OCR engine and an embedding model on the allow-list?** Grounding (§15) and semantic reconciliation (§9) depend on them; both need to be in the model inventory (§16).
- **Governance classification:** is this a "model" (full model-risk validation) or a human-validated assist? (Determines the whole approval path — §16.)
- Amendment vs restatement: will prod always receive full restated docs, or also delta amendments? If deltas are common, applying amendments (§11, §22) moves up the roadmap.
- Confidence + grounding thresholds for human-review routing — who sets them, and what auto-accept precision is required; is there a review UI or just a flagged-fields export?
- Dictionary governance — who approves `pending_review` fields, aliases, and business meanings? Who owns the confusable-pairs list?
- Acceptance criteria — the specific fields, accuracy bar, and demo scope that constitute success (§21).
- Target field taxonomy — is the seed core dictionary (§9) the right guaranteed set on top of open extraction?
- What do real production scans look like (DPI, fax artefacts, stamps, handwriting)? Needed to make the degraded test variants (§19) representative.
