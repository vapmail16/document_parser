"""`eval` mode — two runs side by side (spec §3 parity risk, §12, §13). No model involved.

Built for frontier vs on-prem SLM on the same document: both scored on the SAME golden rows, a
paired test on the rows they disagree about (McNemar, exact), field-level agreement between the two
extractions, and cost / latency per page from the runs' own reports.
"""

from math import comb

from pydantic import BaseModel

from .contracts import DocumentExtraction
from .evaluate import EvalReport, GoldenRow, evaluate
from .normalise import normalise, same_value
from .store import Store


class RunSide(BaseModel):
    run_id: str
    model_key: str
    input_variant: str
    labelled_recall: float | None
    auto_accepted_precision: float | None
    precision_on_golden_keys: float | None
    grid_cell_accuracy: float | None
    unverified_rate: float
    cost_usd: float
    cost_per_page_usd: float | None
    mean_latency_per_call_s: float | None
    pages_extracted: int


class Comparison(BaseModel):
    a: RunSide
    b: RunSide
    golden_rows_paired: int
    a_only_right: int  # golden rows A labels correctly and B does not
    b_only_right: int
    mcnemar_p: float | None  # exact two-sided; small p = the recall difference is unlikely to be chance
    shared_keys: int
    keys_same_value: int
    keys_different_value: list[str]
    note: str


def mcnemar_exact(b: int, c: int) -> float | None:
    n = b + c
    if n == 0:
        return None
    k = min(b, c)
    return min(1.0, 2 * sum(comb(n, i) for i in range(k + 1)) / 2 ** n)


def _cost(store: Store, run_id: str, doc: DocumentExtraction) -> tuple[float, float | None, float | None, int]:
    """Cost of producing THIS extraction: every model call recorded for the pass-1 / pass-2 prompt
    versions it was built from (retries and failed pages included), not whatever else was run."""
    from .pass1 import PagePass1, pass1_dir
    from .pass2 import pass2_dir

    model, pv = doc.provenance.model_id, doc.provenance.prompt_versions
    dirs = [pass1_dir(store, run_id, model, pv["pass1_extraction"])]
    if "pass2_schema_hint" in pv:
        dirs.append(pass2_dir(store, run_id, model, pv["pass1_extraction"], pv["pass2_schema_hint"]))
    cost, latencies, pages = 0.0, [], set()
    for d in dirs:
        for f in d.glob("p*.json"):
            r = PagePass1.model_validate_json(f.read_text())
            pages.add(r.page)
            for a in r.attempts:
                cost += a.response.usage.cost_usd or 0.0
                latencies.append(a.response.latency_s)
    per_page = round(cost / len(pages), 4) if pages else None
    return round(cost, 4), per_page, (round(sum(latencies) / len(latencies), 1) if latencies else None), len(pages)


def _side(store: Store, run_id: str, doc: DocumentExtraction, e: EvalReport, variant: str) -> RunSide:
    cost, per_page, latency, pages = _cost(store, run_id, doc)
    return RunSide(run_id=run_id, model_key=doc.provenance.model_id, input_variant=variant,
                   labelled_recall=e.labelled_recall, auto_accepted_precision=e.auto_accepted_precision,
                   precision_on_golden_keys=e.precision_on_golden_keys, grid_cell_accuracy=e.grid_cell_accuracy,
                   unverified_rate=e.unverified_rate, cost_usd=cost, cost_per_page_usd=per_page,
                   mean_latency_per_call_s=latency, pages_extracted=pages)


def compare(store: Store, run_a: str, doc_a: DocumentExtraction, run_b: str, doc_b: DocumentExtraction,
            golden: list[GoldenRow], golden_name: str, provisional: bool) -> Comparison:
    va, vb = store.load_run(run_a).input_variant, store.load_run(run_b).input_variant
    ea = evaluate(doc_a, golden, golden_name, provisional, va)
    eb = evaluate(doc_b, golden, golden_name, provisional, vb)
    ra = {r.key: r.label_ok for r in ea.results if r.label_ok is not None}
    rb = {r.key: r.label_ok for r in eb.results if r.label_ok is not None}
    paired = sorted(set(ra) & set(rb))
    a_only = sum(ra[k] and not rb[k] for k in paired)
    b_only = sum(rb[k] and not ra[k] for k in paired)

    def keyed(d):
        out = {}
        for f in d.flat_fields:
            if f.canonical_name and f.review_status != "rejected":
                out.setdefault((f.canonical_name, f.instance_key), []).append(f.value or normalise(f.raw_value))
        return out
    ka, kb = keyed(doc_a), keyed(doc_b)
    shared = sorted(set(ka) & set(kb), key=lambda k: (k[0], k[1] or ""))
    same = [k for k in shared if any(x and y and same_value(x, y) for x in ka[k] for y in kb[k])]
    diff = [f"{k[0]}|{k[1] or ''}" for k in shared if k not in same]
    same_model = doc_a.provenance.model_id == doc_b.provenance.model_id
    note = ("same model on both sides: this compares INPUTS (e.g. clean vs degraded), not models"
            if same_model else "different models on the same golden rows: the frontier ↔ on-prem parity check")
    return Comparison(a=_side(store, run_a, doc_a, ea, va), b=_side(store, run_b, doc_b, eb, vb),
                      golden_rows_paired=len(paired), a_only_right=a_only, b_only_right=b_only,
                      mcnemar_p=mcnemar_exact(a_only, b_only), shared_keys=len(shared), keys_same_value=len(same),
                      keys_different_value=diff, note=note)
