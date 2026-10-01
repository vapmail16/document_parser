# Credit Agreement Extraction Pipeline

Turns scanned credit / facility agreements into structured, source-grounded data: every extracted value
carries the page and a verbatim quote it came from, is checked against an independent OCR of that page,
and is mapped to a key in a versioned field dictionary. Values that cannot be verified go to human review
instead of being accepted. Versions of an agreement can be stored, queried as of a date, and diffed.

- Design and rationale: [`credit-agreement-extraction-HANDOVER.md`](credit-agreement-extraction-HANDOVER.md) ("the spec")
- Running with an on-prem model (Qwen via vLLM / Ollama): [`WORKPLACE.md`](WORKPLACE.md)
- What was measured during the proof of concept: [`docs/POC_RESULTS.md`](docs/POC_RESULTS.md)
- How it was built, step by step (decisions, issues, learnings): [`docs/BUILD_JOURNAL.md`](docs/BUILD_JOURNAL.md)

## Status

Proof of concept, all 17 build milestones of spec §20 implemented, plus a JSON data-product layer
served by a query register over REST and MCP. 136 automated tests.

| Validated | Not yet validated |
|---|---|
| Every stage run on a real 211-page agreement (public SEC exhibit), clean and degraded scan; extraction on a 16-page sample with a hosted frontier model | A run with an on-prem model — the adapter is tested against a simulated vLLM/Ollama server only |
| On the sample: every auto-accepted value correct (43/43 clean, 42/42 degraded); grounding never verified a value that was not on the page | The full 211-page extraction |
| Commitment schedule reconciles ($1.5bn; shares sum to 100%); pricing grid 20/20 cells | Accuracy against a **human**-verified golden set — the current set is AI-verified (see [Evaluation](#evaluation)) |

## How it works

Each stage is a separate module with typed inputs and outputs (`src/credit_extract/contracts.py`).
Only stages 3 and 7 call a model; everything else is deterministic.

| # | Stage | Module | What it does |
|---|---|---|---|
| 0 | Ingest | `ingest.py`, `store.py` | Stores the PDF by SHA-256, pins a dictionary snapshot, writes a write-once run record |
| 1 | Rasterise | `ingest.py` | Every page to a 300 DPI image; records any embedded text layer |
| 2 | OCR | `ocr.py` | Tesseract on every page — the independent witness for grounding and completeness |
| 3 | Extract | `pass1.py`, `pass2.py`, `gateway.py`, `tiling.py` | Pass 1: open-ended extraction per page band. Pass 2: the model is asked which *known* dictionary fields are present. Pages are split into bands so small print is not downscaled |
| 4 | Assemble | `assemble.py` | Strips headers/footers, merges band overlaps, stitches clauses across pages, builds sections/clauses/tables, per-page completeness against OCR |
| 5 | Defined terms | `defined_terms.py` | Definitions, collective terms ("Borrowers"), usages across all pages |
| 6 | Reconcile | `reconcile.py` | Field names to dictionary keys and instances (exact → alias → defined term → fuzzy), type and confusable-name gates, pass-1/pass-2 agreement |
| 7 | Enrich | `enrich.py` | Drafts business meanings for proposed new dictionary entries (pending human approval) |
| 8 | Ground & route | `grounding.py` | Quote found on the page **and** value derivable from it → auto-accept; otherwise review |
| 9 | Persist | `lineage.py` | Write-once versioned records per (document, version); as-of queries |
| 10 | Diff | `diff.py` | Content-aligned clause diff (renumbering-safe), field and table changes, comparability warnings |
| 11 | Hand-off | `handoff.py` | `emit()` of accepted values to a downstream tool; governance audit bundle |

Human review (`review.py`) and dictionary approval (`approve.py`) close the loop: reviewer decisions
become audit records, new document revisions, alias proposals and golden-set rows.

## Guard rails

- **No hosted model in production.** `CAE_ENVIRONMENT=prod` refuses any model not marked `prod_allowed`;
  hosted (frontier) models can never be `prod_allowed`. The production install contains no provider SDK.
- **Grounding is a hard gate.** Nothing is auto-accepted unless its quote is on the cited page and the
  value is in both the quote and the page text; a single differing digit is never verified.
- **Nothing is dropped silently.** Every page gets a coverage record (`ok`, `low_coverage`, `failed`,
  `blank`, `not_extracted`); failed pages and failed table checks go to the review export.
- **Nothing is overwritten.** Inputs, run records, dictionary snapshots and published revisions are
  write-once; prompt versions and pass-2 sweeps are kept side by side.
- **The dictionary cannot be poisoned by fuzzy matches.** Fuzzy mappings are always reviewed and only
  *propose* aliases; confusable pairs can never merge; only a named human can approve aliases or entries.
- **Offline and permissive.** Dependencies are MIT/BSD/Apache, hash-pinned, checked by a licence gate;
  tests run with networking disabled.

## Setup

```bash
# Production / on-prem (no hosted-model SDK)
pip install --require-hashes -r requirements-prod.txt && pip install --no-deps -e .
export CAE_ENVIRONMENT=prod

# Development (adds the Anthropic SDK for the frontier comparison baseline)
uv sync --extra frontier

pytest                                   # all tests, networking disabled
python scripts/check_licences.py         # fails on GPL/AGPL/unknown licences
```

Requires Python 3.11+ and Tesseract 5.x on the PATH. Model configuration: see [Configuration](#configuration)
and [`WORKPLACE.md`](WORKPLACE.md).

## Usage

```bash
cae check-model onprem_candidate                     # one-call smoke test of a configured model

cae extract agreement.pdf --document-id DOC --version-label V --effective-date YYYY-MM-DD \
    [--pages 1,15,17-18] [--model KEY] [--skip-pass2]  # the whole pipeline, stages 0-9

cae review export RUN_ID --out review.csv            # fill in decision (confirm/correct/reject) + reviewer
cae review ingest RUN_ID review.csv
cae dict approve var/store/runs/RUN_ID/reviews/BATCH.json --approver "Full Name"

cae eval RUN_ID --golden data/golden/FILE.csv        # human-verified rows only (--allow-ai-verified adds labelled AI rows)
cae compare RUN_A RUN_B --golden FILE.csv            # two models side by side on the same golden rows
cae history DOC ; cae asof DOC --field applicable_margin --on 2023-06-30
cae diff DOC --from-version V1 --to-version V2
cae emit DOC --version-label V --out handoff.json    # accepted values only, each with page + quote
cae export-audit --run-id RUN_ID                     # governance bundle with sha256 manifest

cae products build handoff.json --out var/products   # entity model + JSON data products
cae serve --products var/products                    # REST on http://127.0.0.1:8765
cae mcp --products var/products                      # the same queries as MCP tools
```

Individual stages can also be run on their own: `ingest`, `ocr`, `pass1`, `pass2`, `assemble`, `ground`,
`reconcile`, `publish`, `degrade` (test-only scan simulation), `dict list|show|versions|enrich|add-entries`.
`cae --help` lists everything.

## Configuration

| What | Where | Notes |
|---|---|---|
| Models | `models.yaml` | The only place model names appear. `provider: openai_compatible` for vLLM/Ollama, `anthropic` for the frontier baseline. Image budget, tiling, structured-output style, concurrency per model |
| Field dictionary | `dictionary/snapshots/vNNNN.json` | Immutable snapshots; a run pins one. v1 seed core set, v2 instance rules, v3 value guidance. New entries/aliases only via `dict add-entries` / `dict approve` |
| Dictionary proposals | `dictionary/proposals/` | AI-drafted meanings awaiting a human decision |
| Prompts | `prompts/<name>/vN.md` | Versioned; never edit a released version — add `vN+1`. Versions used are recorded in provenance |
| Settings | env vars `CAE_*` or `.env` | `CAE_ENVIRONMENT` (poc/prod), `CAE_STORE_DIR`, `CAE_RASTERISE_DPI`, `CAE_RASTERISE_GRAYSCALE` |
| Thresholds | `grounding.py`, `reconcile.py`, `assemble.py`, `diff.py` | Module-level constants, e.g. `AUTO_ACCEPT_CONFIDENCE` — calibrate per model with `cae eval` |

## Repository layout

| Path | Contents |
|---|---|
| `src/credit_extract/` | The pipeline (see [How it works](#how-it-works)); `cli.py` is the `cae` command. `products/` is the entity model, data products, query register, REST API, and MCP server |
| `tests/` | Unit and end-to-end tests (fake models; no network) |
| `scripts/` | Licence gate, golden-set drafting, leak-scanned zip export (`export_bundle.py`) |
| `prompts/`, `dictionary/`, `models.yaml` | Versioned configuration (above) |
| `data/sec/` | Public test agreement (SEC EDGAR), SHA-256 checksums, a text reference of pages 1–10 |
| `data/golden/` | Golden sets: template, text-layer draft, reviewed version |
| `var/store/` | Processed sample: run records, page images, OCR, raw model responses, extractions, reports, published records, source PDFs |
| `var/products/` | Entity model and JSON data products built from the accepted Albemarle handoff |
| `var/handoff/`, `var/audit/` | Accepted hand-off payload and governance bundle |
| `docs/POC_RESULTS.md`, `docs/BUILD_JOURNAL.md` | Measurements taken during the POC; how it was built, step by step |

The processed Albemarle store is in the repository so the extracted values, page images, and data
products can be inspected without re-running the model. `.env` (API keys) is not committed.
The two degraded PDFs are stored with Git LFS because each is larger than GitHub's 100 MB file limit.

## Evaluation

The golden set is the only basis for an accuracy claim. Each row records who drafted and who verified it,
and `cae eval` reports by verification tier:

- **human** — `verified_by` is a named person: counts, numbers are quotable.
- **ai** — verified by a model: counted only with `--allow-ai-verified`, and every report says
  "AI-VERIFIED — not valid for governance sign-off". An AI checking a model's output is not independent.
- **unverified** — counted only with `--include-unverified` (PROVISIONAL).

The current Albemarle set (`data/golden/albemarle-2022-AR.draft_reviewed.csv`, 69 rows) is AI-verified.
Reviewer decisions from `cae review ingest` produce human-verified rows automatically.

Metrics: labelled recall (value under the right key), precision on golden keys, **auto-accepted
precision** (the values no human sees — the number governance needs), grid-cell accuracy, and the share
of values grounding could not confirm, each with a 95% Wilson interval. `cae compare` adds a paired
McNemar test between two runs.

## Data products

Accepted handoff values are turned into an entity model and four JSON products. JSON is the store, so
the products are not tied to a database. REST and MCP read them only through a query register.

| Piece | Module | Role |
|---|---|---|
| Entity model and products | `products/model.py` | Facility, parties, commitments, terms, and the links between them. Products: `facility_terms`, `party_roles`, `lender_commitments`, `field_facts` |
| Query operators | `products/query_layer.py` | `top_n` and time buckets (`year`, `quarter`, `month`). No business meaning |
| Query register | `products/register.py` | The only questions the API will answer. Each entry names a product, a SQL template, and which operator runs after the SQL |
| Access | `products/access.py` | Loads the JSON into SQLite and runs one registered template. Parameters are bound. A request cannot send SQL |
| REST | `products/rest.py` | `GET /health`, `GET /products`, `GET /products/{name}`, `GET /entity-models`, `GET /queries`, `POST /queries/{name}` |
| MCP | `products/mcp_server.py` | The same queries as tools, plus `list_products` and `get_entity_model` |

The Albemarle sample is already built at `var/products/albemarle/2022-AR/r003/`. It has 12 lender
commitments that sum to 1,500,000,000, maturity 2027-10-28, and borrowers Albemarle Corporation and
Albemarle Europe SRL. Fifty-three unreviewed values were left out of the products.

```bash
cae serve --products var/products
# largest three commitments
curl -s -X POST http://127.0.0.1:8765/queries/top_lenders \
  -H 'Content-Type: application/json' \
  -d '{"params":{"document_id":"albemarle","version_label":"2022-AR","n":3}}'
# commitments bucketed by the agreement effective date
curl -s -X POST http://127.0.0.1:8765/queries/commitments_by_period \
  -H 'Content-Type: application/json' \
  -d '{"params":{"document_id":"albemarle","grain":"year"}}'
```

Registered queries: `facility_terms`, `party_roles`, `lender_commitments`, `top_lenders`,
`commitments_by_period`, `field_facts`.

To add a question, append a `QuerySpec` in `register.py`, or drop a JSON file of the same shape in a
directory and start the server with `--queries that/directory`. No new HTTP route and no new MCP
function. The template must be a single `SELECT` of one product. `top_n` and `time_agg` are declared
on the entry and run after the SQL.

On-prem, point MCP at `cae mcp --products var/products` and point other systems at `cae serve`.
Both call the same register. Rebuild products after a new accepted handoff with `cae products build`.

## Known limitations

- Thresholds (grounding tolerance, auto-accept confidence, coverage) were tuned on the 16-page sample
  and a hosted model; re-check them on unseen pages and on the on-prem model before relying on them.
- The embedding tier of name reconciliation (spec §9) is not implemented; exact / alias / defined-term /
  fuzzy tiers are.
- Amendment documents are classified and their instructions can be extracted, but instructions are not
  applied to prior versions (spec §11, out of scope for the POC).
- Dictionary snapshots v2–v3, the 15 drafted meanings in `dictionary/proposals/`, and two mapping
  questions (net vs total leverage; facility fee vs commitment fee) await the dictionary owner.
