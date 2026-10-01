# POC results — Albemarle A&R Credit Agreement (2022)

Chronological record of what was measured during the proof of concept, kept as it was reported.
Public SEC exhibit (211 pages), 16-page sample, clean render and a degraded-scan variant. Model:
Claude Opus 5 (hosted; public documents only). Earlier tables are superseded by later ones — the last
table in each section is current. **All accuracy figures use a golden set that was drafted from the
PDF text and verified by an AI, not a person: they are not valid for governance sign-off.**

### Early results — Albemarle A&R Credit Agreement (2022), 211 pages, Opus 5

10-page sample (cover, definitions, pricing grid, covenants, events of default, governing law,
signatures), each page sent as 2 bands at ~275 effective DPI, `effort: medium`. Directional only —
not an accuracy claim until the golden set exists (spec §13).

| | Clean render | Degraded scan |
|---|---|---|
| Pages ok / failed | 10 / 0 | 10 / 0 |
| Fields extracted | 79 | 95 |
| Quotes found verbatim in the true text | 79/79 (100%) | 93/95 (97.9%) — both misses are table-row quotes, values correct |
| Pricing grid (20 values) | 20/20 correct | 20/20 correct |
| Cost | $1.55 ($0.155/page) | $1.61 ($0.161/page) |
| Projected full document | ~$33 | ~$34 |
| Latency | ~15 s per band; 93 s for 10 pages at concurrency 4 | ~18 s per band |

Independent OCR (Tesseract 5.5.3, all 211 pages, ~2 min at 8 workers):

| | Clean render | Degraded scan |
|---|---|---|
| Word-F1 vs true text | 0.948 mean (vs embedded text layer) | 0.912 mean; 135/211 pages ≥ 0.95 |
| Mean token confidence | 0.93 | 0.87 |
| Worst pages | near-blank pages (footer only) | same: speckle read as junk tokens at ~0.2–0.3 confidence |

Implication: completeness (milestone 6) must drop low-confidence OCR tokens and classify
footer-only pages as `blank`, or they look like dropped content. 39 pages score 0.5–0.9 on the
degraded scan and should be inspected when the grounding gate (milestone 7) is built.

Assembly (milestone 6) on the sample: 2 browser header/footer patterns learned from OCR margins and
stripped; band-overlap duplicates removed (fields, blocks, the repeated grid row); pricing grid is
one 6×6 table attached to the "Applicable Rate means …" definition; every sampled page ≥ 98.9%
OCR-token coverage; classified `executed` / `restatement` from the cover page.

Grounding (milestone 7), scored against the true text of the clean PDF:

| | Clean render | Degraded scan |
|---|---|---|
| Fields | 72 | 83 |
| Verified at source → auto-accepted | 70 | 68 |
| Routed to human review | 2 | 15 (6 labelled "likely OCR noise") |
| **Missed errors** (verified, but not truly on the page) | **0** | **0** |
| False alarms (true, but sent to review) | 0 | 13 (16% of true fields) |
| Pricing-grid cells found on the page | 36/36 | 30/36 |

Both clean-render rejections are real model errors: a value paraphrased from its quote
("Exhibit 10.1" from "EX-10.1") and a Governing Law value longer than its quote. Degraded-scan
false alarms are OCR losing characters the model read correctly (a wrapped table row, "7.11" read
as "zu", a signature). Caveat: grounding thresholds were tuned on these same 10 pages — the rates
must be re-measured on unseen pages before they are quoted.

Prompt v2 (`raw_value` must be an exact substring of `source_text`), same 10 pages:

| | v1 clean | v2 clean | v1 degraded | v2 degraded |
|---|---|---|---|---|
| Verified / fields | 70/72 | **86/86** | 68/83 | 70/82 |
| Model errors ("value not in quote") | 2 | **0** | 2 | **0** |
| False alarms (OCR noise) | 0 | 0 | 13 | 12 |
| Missed errors | 0 | 0 | 0 | 0 |

The v2 run used 3 bands on 8 pages because of a since-fixed tiling bug (the third band was a
footer strip); cost $1.78 per 10 pages instead of ~$1.55. All 211 pages now plan exactly 2 bands.

### Golden set & eval (milestone 8)

`data/golden/albemarle-2022-AR.draft.csv` — 68 candidate rows drafted from the PDF's own text (never
from model output) by `scripts/draft_golden_albemarle.py`, which checks every quote is on its page
and every value is inside its quote. **Drafted rows are not ground truth**: a person must check each
against the PDF and set `verification` to `ok`, `corrected` (after fixing the value) or `wrong`, and
put their name in `verified_by`. Rows marked `REVIEW MAPPING` in `notes` need a dictionary decision
(net vs total leverage; facility fee vs commitment fee).

```bash
uv run cae eval RUN_ID --golden data/golden/albemarle-2022-AR.draft.csv                     # verified rows only
uv run cae eval RUN_ID --golden data/golden/albemarle-2022-AR.draft.csv --include-unverified  # PROVISIONAL
```

PROVISIONAL result (unverified rows — not quotable), prompt v2, 10-page sample:

| | Clean | Degraded |
|---|---|---|
| Golden values on extracted pages | 37 (29 more on pages not yet extracted) | 37 |
| Value recall (captured as field / cell / party) | 97.3% (95% CI 86–99.5%) | 97.3% |
| Pricing-grid values in the right cell | 20/20 | 20/20 |
| Found values auto-accepted by grounding | 100% | 88.9% |
| Only miss | EBITDA definition: transcribed, not captured as a field | same |

After reconciliation (milestone 9) and extracting the 6 remaining golden pages, AI-VERIFIED golden set
(`albemarle-2022-AR.draft_reviewed.csv`, reviewed by Claude, not a person — not valid for sign-off):

| All 66 scorable golden values, 16 extracted pages | Clean | Degraded |
|---|---|---|
| Value found (field / table cell / party) | 98.5% | 98.5% |
| Pricing-grid values in the right cell | 20/20 | 20/20 |
| **Labelled recall** (value under the right dictionary key + instance) | **86.2%** (95% CI 76–93%) | **84.6%** (74–91%) |
| **Precision on golden keys** (mapped fields with the right value) | **100%** of 54 (93–100%) | **100%** of 53 (93–100%) |
| Schedule 2.01: commitments sum to $1.5bn, shares to 100% | pass | pass |
| Missed errors (grounding verified a value not on the page) | 0 | 0 |

Page 24 could not be extracted: the API's output filter blocked the lower half ("Output blocked by
content filtering policy", request ids in `var/store/runs/*/pass1/frontier/v2/p0024.json`) on benign
text — the "Defaulting Lender" definition. Recorded as a failed page and routed to review, not retried
or worked around. Its golden row (default rate) is excluded from the scores above.

After Pass 2 (milestone 11: `cae pass2 RUN --dictionary-version 2`, then assemble → ground →
`reconcile --dictionary-version 3` → eval), same AI-VERIFIED golden set:

| 65 golden values on 15 extracted pages | Clean | Degraded |
|---|---|---|
| Labelled recall (value under the right key) | **95.4%** (was 86.2%) | **98.5%** (was 84.6%) |
| Precision on golden keys (all mapped fields) | 89.0% of 82 | 88.6% of 88 |
| **Auto-accepted precision** (mapped fields no human reviews) | **95.2%** of 42 | **95.1%** of 41 |
| Pass 2 vs pass 1 | 54 confirmed, 9 disagree, 28 new | 42 confirmed, 8 disagree, 33 new |
| Missed errors (grounding) | 0 | 0 |
| Cost of the pass-2 sweep | $1.79 | $1.77 |

Of the 2 auto-accepted "errors", one is a golden-set gap, not an extraction error: ALBEMARLE EUROPE SRL is
the Belgian Borrower (p14) and the golden set lists only one borrower. The other is real: the agent
given as "Bank of America" (", N.A." dropped). Pass-2 values that are wrong (event-of-default headings
instead of grace periods, "SONIA" for a rate option, a letter-of-credit sub-limit as facility type,
jurisdiction as governing law) all reached the review queue — via the pass-1/pass-2 disagreement check,
low model confidence, or the label-echo gate. Dictionary v3 adds `value_guidance` so pass 2 is told
what each value is; re-running pass 2 with it (~$3.5 both variants) is the expected fix.

Label misses and where they get fixed: 5 benchmark-rate names exist only in the grid header, and the
EBITDA definition only as clause text (→ Pass 2 schema-hinted sweep, milestone 11); the borrower is
introduced as "the Company" (→ defined-term resolution, milestone 10); "Leverage Ratio step-up"
fits both leverage entries, so it is deliberately left unmapped; the cross-default Threshold Amount is
defined on p42, outside the Events of Default article (→ defined-term resolution links it).

Reconciliation rules (`src/credit_extract/reconcile.py`): exact name → approved alias → fuzzy, and
section context for repeatable fields (e.g. Article IX → event_of_default). Every candidate passes
a type gate (a duration never becomes a date; a party is never a "Section 11.02" cross-reference)
and a confusable gate (dictionary `confusable_pairs`). Instance keys come from `instance_rules` in
dictionary v2. Fuzzy mappings always go to review and only *propose* pending aliases — until a
reviewer approves those aliases, the ~21 fuzzy pricing-grid mappings per document stay in the
review queue, which is why only 56% of found values are auto-accepted on the clean render.

Diff noise test (milestone 15): the clean and degraded extractions of the SAME version diffed against each
other — 119 clauses matched, 0 table-cell changes, 69 fields unchanged, 4 field "changes", all flagged
low-confidence (pass-2 naming noise on benchmark rates / an event-of-default header).

Cost (both passes, measured from recorded calls): $0.28 per page -> ~$59 per 211-page variant.

Pass 2 re-run with dictionary v3 value guidance ($3.62), plus two free fixes — definitions read from the
OCR of all 211 pages, and party values that are defined terms resolved to the legal name they stand for
("Bank of America" -> "Bank of America, N.A.", per the p19 definition). AI-VERIFIED golden set:

| 66 golden values, 15 pages | Clean | Degraded |
|---|---|---|
| Labelled recall | **100%** (95% CI 94.5–100%) | 95.5% (87.5–98.4%) |
| Precision on golden keys | **98.8%** of 81 | **97.6%** of 84 |
| **Auto-accepted precision** | **100%** of 43 (91.8–100%) | **100%** of 42 (91.6–100%) |
| Missed errors | 0 | 0 |

Remaining wrong mapped values (all in the review queue, none auto-accepted): "SONIA" given for the
Alternative Currency Daily Rate option (golden-set question: rate name or underlying benchmark?), and on
the degraded scan "NEW YORK STATE" for governing law. The degraded scan's 3 unlabelled benchmark-rate
names were labelled in the previous sweep — run-to-run variation in what pass 2 reports.

Enrichment (milestone 12): `cae dict enrich RUN --dry-run` shows the filtered candidates (89 proposed new
names -> 31 after dropping cross-references, signatures, metadata and per-lender duplicates); 15 meanings
drafted for $0.18 into `dictionary/proposals/enrichment-*.json`, status pending_review. Nothing is applied
until a named person runs `cae dict add-entries FILE --approver "Full Name"` (AI approvers are refused).


## Current summary (after pass 2 with dictionary v3 and defined-term name resolution)

| 66 golden values, 15 extracted pages | Clean | Degraded |
|---|---|---|
| Labelled recall (value under the right dictionary key) | 100% (95% CI 94.5–100%) | 95.5% (87.5–98.4%) |
| Precision on golden keys | 98.8% of 81 | 97.6% of 84 |
| Auto-accepted precision | 100% of 43 (91.8–100%) | 100% of 42 (91.6–100%) |
| Missed errors (grounding verified a value not on the page) | 0 | 0 |
| Extraction cost, both passes | $0.28 / page | $0.28 / page |

Page 24 could not be extracted with the hosted model: its output filter blocked the lower half of the
page ("Output blocked by content filtering policy") on benign text. Recorded as a failed page.

Total API spend for the POC: about $16 (291 calls), computed from recorded token counts at list price.

## Runs not yet done

| Run | Why | Approx. cost |
|---|---|---|
| Full 211 pages, both passes | whole-document accuracy, completeness, and the unseen-page check of grounding thresholds | ~$59 per variant |
| A second agreement version (IEC series or synthetic v2) | real version diff | ~$59 per version at full length; ~$5 for a sampled synthetic |
| On-prem Qwen run | the parity check (milestone 16) | on-prem; see WORKPLACE.md |

## Needs owner sign-off

- `dictionary/snapshots/v0001.json` — the seed core fields, aliases and business meanings are a draft.
- `models.yaml` — values marked `VERIFY`, and the choice of on-prem candidate model.
