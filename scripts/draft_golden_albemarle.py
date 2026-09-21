"""Draft candidate golden rows for Albemarle 2022 A&R from the PDF's own text layer (spec §13).

Drafted from the document text only — never from model extractions — so the golden set stays
independent of the system under test. Every row is self-checked: its quote must occur on the
cited page and its value must occur inside its quote.

Drafted rows are NOT ground truth until a human sets `verification` (ok / corrected / wrong)
after checking each row against the PDF.

Usage:  uv run python scripts/draft_golden_albemarle.py
"""

import csv
import sys
from pathlib import Path

from credit_extract.normalise import text_key
from credit_extract.ocr import text_layer

PDF = Path("data/sec/albemarle-2022-AR.pdf")
OUT = Path("data/golden/albemarle-2022-AR.draft.csv")
DOC, VER = "albemarle", "2022-AR"

# (canonical_name, instance_key, expected_value, page, quote, note)
ROWS: list[tuple[str, str, str, int, str, str]] = [
    ("borrower", "albemarle_corporation", "ALBEMARLE CORPORATION", 1, "ALBEMARLE CORPORATION, (the “Company”)",
     "the Company"),
    ("borrower", "albemarle_europe_srl", "ALBEMARLE EUROPE SRL", 14,
     "ALBEMARLE EUROPE SRL, a société à responsabilité limitée organized under the laws of Belgium (the “Belgian Borrower”",
     "the Belgian Borrower, named in the preamble (missed in the first draft)"),
    ("administrative_agent", "", "BANK OF AMERICA, N.A.", 1,
     "BANK OF AMERICA, N.A., as Administrative Agent and Swing Line Lender", ""),
    ("agreement_date", "", "October 28, 2022", 1, "Dated as of October 28, 2022", ""),
    ("maturity_date", "", "October 28, 2027", 33, "“Maturity Date” means the later of (a) October 28, 2027",
     "extendable under Section 2.15"),
    ("facility_type", "", "revolving credit facility", 14,
     "The Company has requested that the Lenders provide a revolving credit facility", ""),
    ("total_commitments", "", "$1,500,000,000", 15,
     "The amount of the Aggregate Commitment in effect on the Closing Date is ONE BILLION FIVE HUNDRED MILLION DOLLARS ($1,500,000,000)", ""),
    ("letter_of_credit_sublimit", "", "$100,000,000", 32,
     "“Letter of Credit Sublimit” means the lesser of (a) ONE HUNDRED MILLION DOLLARS ($100,000,000)", "non-core"),
    ("swingline_sublimit", "", "$150,000,000", 41,
     "“Swing Line Sublimit” means the lesser of (a) ONE HUNDRED FIFTY MILLION DOLLARS ($150,000,000)", "non-core"),
    ("default_rate", "", "2%", 24, "the Applicable Rate, if any, applicable to Base Rate Loans plus (iii) 2% per annum",
     "non-core; margin over the otherwise applicable rate"),
    ("governing_law", "", "THE LAW OF THE STATE OF NEW YORK", 141,
     "THIS AGREEMENT SHALL BE GOVERNED BY, AND CONSTRUED IN ACCORDANCE WITH, THE LAW OF THE STATE OF NEW YORK", ""),
    ("consolidated_ebitda_definition", "", "an amount equal to the sum of (a) Consolidated Net Income for such period plus", 22,
     "“Consolidated EBITDA” means, for any period, for the Consolidated Group, an amount equal to the sum of (a) Consolidated Net Income for such period plus",
     "definition opening only; add-backs follow"),
    ("max_total_leverage_ratio", "base", "3.50 to 1.0", 108,
     "Permit the Consolidated Leverage Ratio as of the end of any fiscal quarter of the Company to be greater than 3.50 to 1.0",
     "REVIEW MAPPING: agreement tests a NET leverage ratio (Funded Debt minus Unrestricted Cash, p23); body numbers it 11.06, TOC says 8.06"),
    ("max_total_leverage_ratio", "acquisition_step_up", "0.50:1.00", 108,
     "shall increase by 0.50:1.00 for four consecutive fiscal quarters",
     "increase for 4 quarters after an Acquisition funded by > $500,000,000 of Funded Debt, at the Company's election"),
    ("min_interest_coverage_ratio", "", "", 108, "", "ABSENT: the only financial covenant is the leverage ratio"),
    ("security", "", "", 24, "", "ABSENT: unsecured facility, no collateral provisions (Debt Rating refers to senior unsecured debt, p24)"),
    ("event_of_default", "non_payment_grace", "five Business Days", 110,
     "within five Business Days after the same becomes due, any interest on any Loan", "principal: no grace"),
    ("event_of_default", "specific_covenants", "Section 7.03, 7.05 or 7.11 or Article VIII", 110,
     "contained in any of Section 7.03, 7.05 or 7.11 or Article VIII", "no grace period"),
    ("event_of_default", "other_defaults_grace", "thirty days", 110, "such failure continues for thirty days", ""),
    ("event_of_default", "cross_default_threshold", "$100,000,000", 42,
     "“Threshold Amount” means ONE HUNDRED MILLION DOLLARS ($100,000,000)", "cross-default (e) on p111 refers to the Threshold Amount"),
]

# Rate types covered by each pricing-grid column (header text on p17).
BENCHMARKS = [
    ("term_sofr", "Term SOFR"), ("term_sofr_daily", "Term SOFR Daily Floating Rate"),
    ("alt_currency_daily", "Alternative Currency Daily Rate"), ("alt_currency_term", "Alternative Currency Term Rate"),
    ("base_rate", "Base Rate"),
]
for key, name in BENCHMARKS:
    ROWS.append(("benchmark_rate", key, name, 17, f"{name} Loans", "rate option named in the pricing grid header"))

GRID = [  # level, debt rating, term SOFR & alt currency, base rate, L/C fee, facility fee
    (1, "A-/A3/A- or better", "0.910%", "0.000%", "0.910%", "0.090%"),
    (2, "BBB+/Baa1/BBB+", "1.025%", "0.025%", "1.025%", "0.100%"),
    (3, "BBB/Baa2/BBB", "1.125%", "0.125%", "1.125%", "0.125%"),
    (4, "BBB-/Baa3/BBB-", "1.200%", "0.200%", "1.200%", "0.175%"),
    (5, "worse than or equal to BB+/Ba1/BB+ or unrated", "1.375%", "0.375%", "1.375%", "0.250%"),
]
for level, rating, sofr, base, lc, fee in GRID:
    quote = f"{level} {rating} {sofr} {base} {lc} {fee}"
    ROWS += [
        ("applicable_margin", f"level{level}|term_sofr", sofr, 17, quote,
         "column also covers Term SOFR Daily Floating, Alternative Currency Daily and Term Rate Loans"),
        ("applicable_margin", f"level{level}|base_rate", base, 17, quote, ""),
        ("applicable_margin", f"level{level}|letter_of_credit_fee", lc, 17, quote, ""),
        ("commitment_fee_rate", f"level{level}", fee, 17, quote,
         "REVIEW MAPPING: agreement charges a Facility Fee (on the whole commitment), not an unused-commitment fee"),
    ]

COMMITMENTS = [
    ("Bank of America, N.A.", "$170,000,000.00"), ("JPMorgan Chase Bank, N.A.", "$170,000,000.00"),
    ("HSBC Bank USA, National Association", "$170,000,000.00"), ("Mizuho Bank, Ltd.", "$170,000,000.00"),
    ("Banco Santander, S.A., New York Branch", "$120,000,000.00"), ("Goldman Sachs Bank USA", "$120,000,000.00"),
    ("MUFG Bank, Ltd.", "$120,000,000.00"), ("Sumitomo Mitsui Banking Corporation", "$120,000,000.00"),
    ("Truist Bank", "$120,000,000.00"), ("U.S. Bank National Association", "$120,000,000.00"),
    ("Bank of China, New York Branch", "$50,000,000.00"), ("The Northern Trust Company", "$50,000,000.00"),
]
for lender, amount in COMMITMENTS:
    key = "".join(ch if ch.isalnum() else "_" for ch in lender.casefold()).strip("_")
    ROWS.append(("lender", key, lender, 154, f"{lender} {amount}", "Schedule 2.01"))
    ROWS.append(("lender_commitment", key, amount, 154, f"{lender} {amount}", "Schedule 2.01"))


def main() -> int:
    pages = [text_key(p) for p in text_layer(PDF)]
    errors = []
    for name, key, value, page, quote, _ in ROWS:
        if not quote:
            continue  # asserted-absent rows
        if text_key(quote) not in pages[page - 1]:
            errors.append(f"{name}|{key}: quote not on page {page}: {quote[:60]!r}")
        elif text_key(value) not in text_key(quote):
            errors.append(f"{name}|{key}: value {value!r} not inside its quote")
    if errors:
        print("Self-check FAILED:\n  " + "\n  ".join(errors))
        return 1

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["document_id", "version_label", "canonical_name", "instance_key", "expected_value",
                    "source_page", "source_quote", "drafted_by", "verified_by", "verification", "notes"])
        for name, key, value, page, quote, note in ROWS:
            w.writerow([DOC, VER, name, key, value, page, quote, "text-layer draft (Claude)", "", "", note])
    print(f"wrote {OUT}: {len(ROWS)} rows, all quotes found on their pages")
    return 0


if __name__ == "__main__":
    sys.exit(main())
