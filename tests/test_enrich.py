from pathlib import Path

from credit_extract.dictionary import load_snapshot
from credit_extract.enrich import candidates
from tests.test_evaluate import doc as make_doc
from tests.test_review import fld

SNAP = load_snapshot(Path(__file__).resolve().parents[1] / "dictionary" / "snapshots", 3)


def test_candidate_filter_skips_references_signatures_and_party_instances():
    d = make_doc(fields=[fld("f1", "Pro Rata Share1 (Bank of America, N.A.)", "11.333333333%"),
                         fld("f2", "Pro Rata Share (Truist Bank)", "8.000000000%"),
                         fld("f3", "Assignment consent section", "Section 11.07(b)"),
                         fld("f4", "Signatory - Mizuho Bank, Ltd.", "Donna DeMagistris"),
                         fld("f5", "BANK OF AMERICA, N.A. Commitment", "$170,000,000.00"),
                         fld("f6", "Debt Rating (Level 1)", "A-/A3/A- or better"),
                         fld("f7", "Debt Rating S&P/Moody's/Fitch (Level 1)", "A-/A3/A- or better"),
                         fld("f8", "Maturity Date", "October 28, 2027", canonical="maturity_date")])
    cands, _ = candidates(d, SNAP, limit=10)
    assert {c.canonical_name: c.seen for c in cands} == {"debt_rating": 2, "pro_rata_share": 2}
