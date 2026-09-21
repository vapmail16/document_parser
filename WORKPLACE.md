# Running at the workplace with an on-prem Qwen model

The pipeline is model-agnostic: the only model-specific part is one entry in `models.yaml`. The
on-prem adapter talks to any OpenAI-compatible server (vLLM or Ollama), so switching from Claude to
Qwen is configuration — but do the steps below in order; step 5 catches most problems in one call.

## 1. Get the code

Clone the repository. It contains the code, configuration, test data, golden sets and the POC results
(run records, raw model responses, extractions, reports, published records, OCR). `.gitignore` keeps out:

| Not in the repository | Why |
|---|---|
| `.env` | the API key for the hosted-model baseline — hosted models are not allowed in production |
| `.venv/`, caches, OS files | rebuilt / irrelevant on the target machine |
| page images, model-response cache, degraded test PDF | large and regenerable (`cae ingest`, `cae degrade --seed 0`) |

The results were produced with a hosted model (Claude Opus 5) on a public SEC exhibit; their
provenance records say so, and the golden set records that it was AI-verified. Check your
organisation's policy for bringing in such artefacts; the code runs without them.

A zip copy can still be made with `uv run python scripts/export_bundle.py`, which also scans every
file for API keys, e-mail addresses and home-directory paths before writing it.

## 2. Install (offline-capable)

```bash
# Python 3.11+. From an internal mirror, hash-pinned, WITHOUT the frontier SDK:
pip install --require-hashes -r requirements-prod.txt && pip install --no-deps -e .
#   or, if uv is available:  uv sync        (no --extra frontier)
python scripts/check_licences.py            # licence gate (spec §5)
pytest                                      # all tests, networking disabled
```

Tesseract OCR must be installed on the machine (`tesseract --version`; 5.x). It is the independent
witness for grounding — the pipeline will not run without it.

Set production mode so no frontier model can ever be called:

```bash
export CAE_ENVIRONMENT=prod
```

## 3. Serve Qwen

Examples — use whatever your platform team provides; the pipeline only needs the URL and model name.

```bash
# vLLM (flags differ between versions — check `vllm serve --help`)
vllm serve Qwen/Qwen2.5-VL-7B-Instruct --max-model-len 32768
#   -> api_base http://<gpu-host>:8000/v1, model_name Qwen/Qwen2.5-VL-7B-Instruct

# Ollama
ollama pull qwen2.5vl:7b
#   -> api_base http://localhost:11434/v1, model_name qwen2.5vl:7b
```

`GET <api_base>/models` lists the exact model name to put in `models.yaml`.

## 4. Configure `models.yaml`

In the `onprem_candidate` entry set `model_name`, `api_base`, `licence` (for the exact model and
size you use), and `enabled: true`. Set `default_model: onprem_candidate` at the top. If the endpoint
needs a token, put it in an environment variable named `ONPREM_API_KEY` (or change `api_key_env`).

## 5. Smoke test — one call

```bash
cae check-model onprem_candidate
```

It sends one synthetic page (a date, an amount, a small pricing grid) and checks that the model
returns the JSON envelope with those values. If it fails:

| Symptom | Change in `models.yaml` |
|---|---|
| `api_error ... response_format` / 400 about schema | `structured_output_style: vllm_guided_json` (older vLLM), else `prompt_only` |
| `invalid_output` | try `vllm_guided_json`; `prompt_only` is the last resort (weakest) |
| `truncated` | raise `max_output_tokens` (must fit the server's `--max-model-len`) or tile more (`max_tiles_per_page`) |
| error about image size / too many tokens | lower `image.max_pixels` / `max_long_edge_px` — pages are tiled, not squashed |
| values misread | raise `image.max_pixels` if the server allows (small print needs ~190+ effective DPI) |

## 6. Run the sample first, then score it

The 16 pages that carry the golden values:

```bash
cae extract data/sec/albemarle-2022-AR.pdf --document-id albemarle --version-label 2022-AR \
    --effective-date 2022-10-28 --pages 1,14,15,17,18,22,24,32,33,41,42,108,110,141,150,154

cae eval <RUN_ID> --golden data/golden/albemarle-2022-AR.draft_reviewed.csv --allow-ai-verified
cae compare <CLAUDE_RUN_ID> <QWEN_RUN_ID> --golden data/golden/albemarle-2022-AR.draft_reviewed.csv \
    --allow-ai-verified --model-a frontier --model-b onprem_candidate      # Claude results are in the bundle
```

Claude baseline on these pages (clean render, prompt v2, pass 2 with dictionary v3) to compare against:
labelled recall 100%, precision on golden keys 98.8%, auto-accepted precision 100% (43/43), 0 missed
errors. Claude clean run id: `20260921T094008Z-67a88b`.

Then the whole document: the same `cae extract` without `--pages`.

## 7. What to watch with a small on-prem model

- **Auto-accept threshold.** `AUTO_ACCEPT_CONFIDENCE = 0.85` (grounding.py) was set on Claude. A
  smaller model's self-reported confidence is calibrated differently. Do not trust auto-acceptance
  until `cae eval` shows auto-accepted precision on the golden set; raise the threshold if needed.
- **More review, not more errors.** Grounding verifies every value against OCR regardless of model,
  so a weaker model mostly means more items in `cae review export`, not wrong auto-accepted values.
  Watch the "unverified by grounding" and "auto-accepted precision" lines of `cae eval`.
- **JSON reliability.** Invalid output is retried once; persistent failures mark the page `failed` —
  visible in `assemble` output and the review export. Many failed pages = change
  `structured_output_style` or the model.
- **Throughput.** 211 pages × ~2 bands × 2 passes ≈ 850 calls. Set `concurrency` to what the GPU
  server sustains.
- **Pass 2 naming.** Pass 2 asks the model to answer in dictionary keys; small models follow this less
  reliably. `cae reconcile` output shows how many pass-2 values were confirmed / disagreed / new.

## 8. Before anyone relies on the numbers

- A named person completes the golden-set checklist (the current golden set is AI-verified only).
- The dictionary owner signs off snapshots v2 and v3 and reviews `dictionary/proposals/`.
- `cae export-audit --run-id <RUN_ID>` produces the governance bundle for the run.
- Real customer agreements only once governance has classified the system (spec §16).
