# Field Mapping & Disambiguation — Design Discussion

12 Oct 2026 · Vikkas Arun Pareek

## Purpose and context

The design is sound in principle: values get their meaning from definitions and quoted evidence, not from labels or bulk examples. It does not guarantee 100% accuracy and has specific gaps, listed in [Gaps and action list](#gaps-and-action-list).

This is a record of a design discussion held on 12 Oct 2026, prompted by a colleague's review. It captures each question as asked and the full answer given, so that none of the reasoning is lost.

**System under discussion:** the credit-agreement extraction pipeline in this repo (`src/credit_extract/`). It takes a scanned PDF, splits it into page bands and runs two AI passes. Pass 1 extracts everything; pass 2 looks for the known dictionary fields. It then checks each quote against an independent OCR (grounding) and maps names to the field dictionary (reconcile). Values go either to auto-acceptance or to human review. Only accepted values are published as JSON data products (`field_facts`, `facility_terms`, `party_roles`, `lender_commitments`).

**Test basis for all figures quoted:** one agreement (Albemarle A&R Credit Agreement 2022, 211 pages), 15–16 sampled pages and 66 golden values. The golden set was checked by an AI, not a person. Source: [`POC_RESULTS.md`](POC_RESULTS.md).

**Discussion state:** no code was changed during the discussion. Everything below is assessment and recommendation.

## Q1 — Can the design tell what a value means?

**Answer in one line:** partly. The design reads each value in the context of its clause and has several safeguards against mislabelling. It does not reach 100%, and it has gaps in exactly the areas the colleague raised.

### The question (as asked, summarised)

The colleague said the problem is harder than it looks. The pipeline goes PDF → pages → JSON → key-value pairs → business dictionary. But:

- **Start date:** how do we know whether a start date is the amortisation start date or the repayment start date? What is the context behind it?
- **Rate mentioned many times:** if a rate column appears 45 times in a document, how do we know which rate is meant?
- **Document type:** how do we know what type of document this is? (Later clarified: this was an example from a different project, not a core concern.)
- **Downstream risk:** key-value outputs feed downstream systems, so values must be 100% accurate. Can the AI identify which field an ambiguous value really belongs to?

The colleague's own past-project technique was deliberately not named, to avoid biasing the assessment.

### Key distinction: two separate questions

1. **Is the value really on the page?** This is answered well. Every value must be quoted word for word, and the quote is checked against an independent OCR. Measured missed errors: 0.
2. **Is the value labelled correctly** (maturity vs repayment start)? Grounding does nothing for this. It is handled by a separate set of safeguards, which work but are not airtight.

### How the design handles each example

**Which start date?**

- Pass 1 records the name the document uses, a verbatim quote and the clause it came from (`prompts/pass1_extraction/v2.md`). The AI reads the clause, not just the date.
- The dictionary keeps similar dates as separate entries: `agreement_date`, `closing_date`, `effective_date`, `maturity_date`.
- Reconcile (`reconcile.py`) auto-accepts only exact or approved-alias matches. Fuzzy matches always go to review.
- A type gate stops a duration being stored as a date. A confusable gate stops look-alike terms being mapped to each other.
- **Gap:** no entries for repayment or amortisation start date, availability period or first payment date. These would be proposed as new entries, not mislabelled. That is safe, but they are not captured.

**Which rate?**

- Dictionary `instance_rules` split the applicable margin by pricing level (Level 1–5) and by type (base rate, term SOFR, LC fee, facility fee). Each gets its own key.
- The pricing grid is extracted as a table and linked to its definition. Result: 20/20 grid values in the right cell.
- Pass 2 re-reads with the dictionary in view. Where the passes disagree, both values go to review.
- An undetermined instance goes to review rather than being guessed.
- **Gap:** no facility or tranche dimension. "Tranche" is mentioned at `contracts.py:115`, but no rule uses it. Albemarle has one revolver, so Term Loan A vs Revolver margin has never been tested. This is the most likely source of mislabelling.

**Document type**

- The weakest point. `assemble.py:352` only classifies executed vs not, and original vs amended-and-restated, from the cover page. Prompts and dictionary assume every input is a credit agreement. Amendments are the risky case: an amended value could look like a current term.

**Only 100%-accurate values downstream**

- The rule: auto-send only values that pass every check; everything else goes to a person. Only auto-accepted values reach the data products. For example, `facility_terms.json` has 7 rows, and no pricing-grid values are in it yet because they await review.
- Measured: 100% of 43 auto-accepted values correct (clean) and 100% of 42 (degraded).
- Caveats when quoting this: the statistical lower bound is about 92%; one agreement and 15 pages; AI-checked golden set; thresholds tuned on the same pages.
- Cost of the caution: only about half of values are auto-accepted. The rest need a reviewer.

### Code issue found

`products/model.py:252`: `facility_terms` groups by key and value but ignores the instance. Once repeatable fields such as margin levels are accepted, the instances would show as false conflicts or lose their labels in that product. `field_facts` keeps the instance correctly.

### Suggested message to the colleague

- **Right approach:** reading in context with AI, quoting evidence, a dictionary with instance rules, two-pass comparison, uncertain values to a person.
- **Not yet proven:** multi-tranche deals, amendments, non-credit-agreement documents, and accuracy checked by a person across several documents.
- Nobody should promise 100%. What can be promised is that every value sent downstream has a quote and page reference, and anything uncertain is held for a person.
- **Decisive test:** run a multi-tranche agreement and one amendment. Have a person check auto-accepted values for labelling (right key, instance and facility), not just the right value.

## Q2 — How do we map hundreds of document fields to 10 target fields?

**Answer in one line:** start from the 10 target fields and search for each one against a written definition, rather than extracting everything and sorting it afterwards. Let the AI decide meaning and let code convert format.

### The question (as asked, summarised)

The colleague's real concerns:

1. **Date formats:** `12 - Oct - 2026`; "twelfth of Oct twenty twenty six"; "5th Friday every quarter".
2. **Rate wording:** a rate stated as "8 basis margin shift".
3. **Many date types:** amortisation, repayment start, maturity, term end, and others.
4. **The core issue:** a target system needs, say, 10 fields. A document may contain hundreds. Every document is worded differently (different law, terms, verbatim language). How do we know which document value is which target field, and be sure it is correct?

### How the current code handles the examples

Checked against the value normaliser, `src/credit_extract/normalise.py`.

| Example | What happens today | Safe? |
| --- | --- | --- |
| `12 - Oct - 2026` | Not recognised as a date. Only full month names ("October 12, 2026", "12 October 2026") and `M/D/Y` work. Short months and hyphens are not handled. | Yes. Kept as text; a text value is blocked from a date field and goes to review. Nothing wrong is loaded, but the value is missed. |
| "twelfth of October twenty twenty six" | Not recognised. | Safe, but missed. |
| "5th Friday every quarter" | Not recognised. It is a rule, not a date, and there is no type for recurring schedules. | Safe, but missed. |
| "8 basis margin shift" | "8 basis" not recognised; only "basis points" and "bps" are. It is also a change to the margin, not the margin itself, and nothing in the dictionary represents a change. | Partly. It won't load as basis points. But a correctly written "8 bps" shift could be put under the margin field, which would be wrong. |
| 5 kinds of dates | Only agreement, closing, effective and maturity dates exist. Repayment start, amortisation and term end are missing, as are rules to tell similar dates apart. | Unknown values are proposed as new entries, not loaded. Main risk: "term end date" matched to `maturity_date`. |

The safety net works: odd formats are blocked, not loaded wrongly. But it mostly catches problems after the fact and does not capture these cases.

### The solution: work backwards from the target system

**1. Define each target field precisely, owned by the business.** Each dictionary entry holds:

- **Definition:** e.g. "Repayment start date = the date of the first scheduled principal instalment. Not the first interest payment, not maturity."
- **Data type and target format:** ISO date, basis points, money plus currency, and so on.
- **What it is not:** look-alike fields and the test that separates them. "Maturity = final repayment of everything; term end = end of the availability or commitment period; amortisation start = first scheduled principal reduction."
- **Typical wordings:** phrases different documents use, growing as documents are reviewed and approved.

This turns "the AI guesses what this date is" into "the AI checks this date against a written definition". The dictionary already has the structure (`value_guidance`, `confusable_pairs`, aliases). It needs entries for the 10 target fields and owner sign-off.

**2. Search for the target fields specifically.** Pass 1 extracts everything. Pass 2 is given the dictionary and asked, for each known field, whether it appears, under what wording, with a quote. Agreement between passes on the same evidence raises trust; disagreement goes to review. For this use case, pass 2 runs against the 10 target fields.

**3. Use the document's own definitions.** Agreements define their terms ("'Maturity Date' means…", "'Repayment Date' means each date set out in Schedule 4"). The code already collects these (`defined_terms.py`). Rule: a value maps to a target field because the document's definition matches the field's definition, not because the label looks similar. This is what handles varying wording: each document explains its own terms.

**4. AI decides the meaning; code converts the format.**

- AI: "this clause sets the repayment start date; the value as written is 'twelfth of October twenty twenty six'", with an exact quote.
- AI also suggests the converted value, `2026-10-12`.
- Code independently converts the written value, after the normaliser is extended for short months, written-out dates and ordinals, and checks that both agree. Disagreement or no conversion means review.
- Rules and relative dates ("5th Friday every quarter", "3 months after the Closing Date") get a **schedule** type. It stores the rule as written plus calculated dates that point to the field they depend on, marked *calculated*. Calculated values are not loaded without review until proven reliable.
- Margin changes ("8 bps step-up/shift") get their own entry with a direction and a trigger, separate from the margin itself.

**5. Check fields against each other.** This is the strongest protection against wrong-field errors, and the design barely does it yet. Today it only checks that lender commitments sum to the total. Checks to add:

- closing date ≤ repayment start ≤ maturity date
- amortisation start ≥ closing date
- term end ≤ maturity
- margin falls within the pricing grid's range
- repayment schedule sums to the loan amount

If start and maturity are swapped, these checks fail and the record goes to review.

**6. Exactly one value per field, or hold it back.** Before loading, each field must have exactly one checked value per facility. Zero or two different values means a person decides. The pipeline must never quietly pick one. Today `facility_terms` marks `conflict = 1` but still publishes the row, so the loader would have to block it. This should become a hard stop.

**7. Earn automatic loading one field at a time.** Measure, per target field, the precision of values loaded without review, against reference answers checked by a person across many varied documents. A field is loaded automatically only once it clears an agreed bar. Otherwise it always goes through a quick review showing the quote and page.

- Simple fields (agreement date, governing law) will clear the bar early.
- Rule-based dates and margin changes may always need a person to confirm.

### Position to give the colleague

We can't promise the AI is 100% right. We can promise that every loaded value has a precise field definition, a quote and page, an agreeing second reading, a code check of the converted value and passing cross-field checks. Anything that fails is held back for a person. The target system only receives values that passed every check or that a person approved.

- **Exists today:** both passes, dictionary structure, matching to the document's own definitions, type and confusable gates, review routing.
- **Missing for the 10-field scenario:** listed in [Gaps and action list](#gaps-and-action-list).

## Q3 — Chunking and 1,000 examples per field?

**Answer in one line:** not as the way to teach the model. Meaning should come from definitions. But the colleague is right that many labelled examples are needed, to test the system rather than to teach it.

### The question (as asked, summarised)

The colleague proposed (1) chunking, and (2) giving the AI around 1,000 examples of what a maturity date looks like, so it can recognise one. The user's view: this is not good design, and none of the earlier answers pointed to it.

### Chunking

- We already chunk: each page is split into bands and sent separately (`tiling.py`, `pass1.py`).
- Chunking only manages how much text the model takes at once. It does not tell you what a value means.
- It creates the very problem being worried about. "Repayment starts on the Repayment Date" may be in one chunk while "Repayment Date means…" is 40 pages away in another.
- What matters is how meaning is reconnected across chunks. The design does this through:
    - **defined-term resolution:** finding each definition across all pages and linking it to its uses;
    - **section context:** e.g. a value in Article IX is read as an event of default;
    - **the two-pass check:** pass 2 searches every band for the known fields.

### 1,000 examples as a teaching method: the wrong approach

- **Most examples would be easy ones.** 950 variations of "the Maturity Date is…" teach nothing. Errors happen on hard cases: term end vs maturity, termination date vs maturity in a revolver with extension options, a maturity that "springs" earlier if other debt isn't refinanced. Examples don't explain the rule that separates these. A written definition plus a "this is not that" test does.
- **It is how pre-LLM systems were built.** Older extraction models needed thousands of labelled examples because they couldn't read a definition. Current models can apply "the maturity date is the date all amounts are finally due" and quote the clause that shows it.
- **It doesn't keep up with change.** New wording or document types need more examples. Changing a definition is a one-line, reviewer-approved edit.
- **Expensive and hard to audit.** 1,000 examples in every prompt costs a lot. "It looked like example #612" is a weaker audit trail than "it matches definition X; here is the quote".

### Where the colleague is right

1. **Hundreds or thousands of labelled examples are needed to test the system.** Current figures rest on one agreement and 66 AI-checked values; this is the weakest part of the design. Defensible accuracy claims need reference answers checked by people, across many documents and every target field.
2. **A few well-chosen examples help.** Five to ten per field is enough, mainly look-alikes: "this is the maturity date; this similar-looking one is term end, because…". These fit in the dictionary's `value_guidance` and confusable pairs.
3. **Examples work as a similarity check.** `reconcile.py` already has an unbuilt slot ("Embedding tier: deferred"). Approved past matches could be stored and used to suggest a mapping for new wording. Suggestions would go to review like fuzzy matches, never be auto-accepted.
4. **A smaller on-prem model may need more examples.** `models.yaml` includes an on-prem candidate. A weaker model benefits more from few-shot examples than Opus does. The planned on-prem parity test should settle this, not argument.

### Recommendation

Keep meaning coming from definitions, the document's defined terms, two independent passes, checks and human review. Bring the colleague on board by using examples where they work best:

- a large person-checked example set, to measure accuracy;
- a few look-alike examples per field, in the dictionary;
- later, a similarity check that only suggests matches.

**Settle it with data.** Take 20–30 documents with person-checked reference answers. Run definitions-only against definitions-plus-examples on the same 10 target fields. Whichever gives higher precision on auto-loaded values, with fewer going to review, wins.

## Gaps and action list

The gaps raised across the three questions are listed below, with code references. None has been worked on yet.

| # | Gap | Where in code | Risk if left |
| --- | --- | --- | --- |
| 1 | No dictionary entries for the 10 target fields with definitions and "what it is not" tests; missing repayment start, amortisation start, term end, availability period, first payment date | `dictionary/snapshots/v0003.json` | Values missed, or look-alikes such as term end matched to maturity |
| 2 | No facility or tranche dimension in instance rules | `contracts.py:115`, dictionary `instance_rules` | Term Loan A vs Revolver values mislabelled; untested |
| 3 | Normaliser misses short months, hyphenated dates, written-out dates, ordinals, "basis" without "points" | `normalise.py` | Valid values blocked and missed (safe, but lost recall) |
| 4 | No schedule or relative-date type ("5th Friday every quarter", "3 months after Closing Date") | `contracts.py` value types, `normalise.py` | Rule-based dates cannot be captured |
| 5 | No margin-change entry (step-up/shift with direction and trigger) | dictionary | A shift could be stored as the margin itself (wrong value) |
| 6 | Cross-field checks limited to commitments summing to the total | `contracts.py` checks, `reconcile.py` | Swapped or mislabelled dates pass unnoticed |
| 7 | `facility_terms` publishes rows with `conflict = 1`; no hard "exactly one value" stop | `products/model.py:252` | Downstream loader could take an ambiguous value |
| 8 | `facility_terms` groups by key and value, ignoring instance | `products/model.py:252` | Repeatable fields show false conflicts or lose labels |
| 9 | Document classification only covers executed vs not and original vs amended-and-restated | `assemble.py:352` | Amendments or other document types processed as a full credit agreement |
| 10 | Golden set is one agreement, 66 values, checked by AI; thresholds tuned on the same pages | `data/golden/`, `docs/POC_RESULTS.md` | Accuracy claims not valid for sign-off |
| 11 | No per-field auto-accept bar | `grounding.py`, `reconcile.py` | All fields treated alike regardless of measured reliability |
| 12 | Embedding/similarity tier deferred | `reconcile.py` | New wordings rely on fuzzy matching only |

### Next steps

- [ ] Agree the 10 target fields with the target-system owner, and write a definition, data type, target format and look-alike test for each
- [ ] Build a person-checked reference set across 20–30 varied documents, including multi-tranche deals and amendments
- [ ] Extend the normaliser for the date and basis-points formats above
- [ ] Add schedule/relative-date and margin-change types
- [ ] Add cross-field consistency checks
- [ ] Make "exactly one checked value per field per facility" a hard stop before loading
- [ ] Fix `facility_terms` grouping to include the instance
- [ ] Run definitions-only vs definitions-plus-examples on the reference set
- [ ] Set a per-field auto-accept bar from measured precision
- [ ] Run the on-prem parity test to see whether a smaller model needs few-shot examples

## Glossary

| Term | Meaning |
| --- | --- |
| Band | A horizontal slice of a page image, sent to the model as one chunk |
| Pass 1 | Open-ended extraction of everything on a band; never sees the dictionary |
| Pass 2 | Schema-hinted sweep: given the dictionary, reports which known fields appear on a band |
| Grounding | Checking that a value's verbatim quote appears in an independent OCR of the page |
| Reconcile | Mapping extracted names to dictionary keys: exact → approved alias → defined term → fuzzy → section context |
| Field dictionary | Versioned list of known fields with meaning, data type, aliases, value guidance and instance rules |
| Instance rules | How a repeatable field is split into instances, e.g. margin by pricing level and rate type |
| Type gate | Blocks a value whose type doesn't fit the field (a duration never becomes a date) |
| Confusable gate | Blocks mapping a term to its look-alike partner ("Consolidated Adjusted EBITDA" never becomes "Consolidated EBITDA") |
| Defined-term resolution | Finding "X means…" definitions across all pages and linking them to where X is used |
| Auto-accepted | Passed every check; published without human review |
| Needs review | Failed or skipped a check; held for a person |
| Golden set | Reference answers used to measure accuracy |
| Labelled recall | Share of golden values found under the right dictionary key and instance |
| Auto-accepted precision | Share of auto-accepted values that are correct |
