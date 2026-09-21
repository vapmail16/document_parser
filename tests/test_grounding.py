from credit_extract.contracts import ExtractedField, GroundingStatus, SourceRef
from credit_extract.grounding import Witness, ground_field, locate, route, value_in
from credit_extract.normalise import normalise

PAGE = ("“Applicable Rate” means, from time to time, the following percentages per annum. "
        "Level 5 worse than or equal to BB+/Ba1/BB+ or unrated 1.375% 0.375% 1.375% 0.250%. "
        "The Aggregate Commitments are $1,500,000,000 on the Closing Date. "
        "Notice pursuant to Section 7.03(c) shall be given.")


def fld(raw, quote, conf=0.95, page=17) -> ExtractedField:
    return ExtractedField(field_id="f", name="n", raw_value=raw, value=normalise(raw), model_confidence=conf,
                          source=SourceRef(page=page, source_text=quote))


def text_witness(text=PAGE) -> list[Witness]:
    return [Witness.from_text(text)]


def ocr_witness(text: str) -> list[Witness]:
    w = Witness.from_text(text)
    w.kind = "ocr"
    return [w]


def test_verified_when_quote_and_value_are_on_the_page():
    g = ground_field(fld("$1,500,000,000", "The Aggregate Commitments are $1,500,000,000"), text_witness())
    assert g.status is GroundingStatus.verified


def test_one_wrong_digit_is_never_verified():
    # Quote copied faithfully, value misread by the model: the value check catches it.
    g = ground_field(fld("$1,800,000,000", "The Aggregate Commitments are $1,500,000,000"), text_witness())
    assert g.status is GroundingStatus.not_found and g.reason == "value not in quote"
    # Quote AND value both altered: quote still matches at ~0.97, but the page text disagrees.
    g = ground_field(fld("1.875%", "unrated 1.875% 0.375%"), text_witness())
    assert g.status is GroundingStatus.not_found


def test_fabricated_quote_not_found():
    g = ground_field(fld("2.00%", "The Applicable Margin shall be 2.00% per annum at all times"), text_witness())
    assert g.status is GroundingStatus.not_found and g.reason == "quote not found on page"


def test_ocr_punctuation_noise_tolerated_for_references_only():
    noisy = ocr_witness(PAGE.replace("7.03(c)", "7,03(c").replace("1,500,000,000", "1.500,000,000"))
    assert ground_field(fld("Section 7.03(c)", "Notice pursuant to Section 7.03(c) shall be given"),
                        noisy).status is GroundingStatus.verified
    # Money must still match exactly: OCR's "1.500,000,000" does not confirm $1,500,000,000.
    g = ground_field(fld("$1,500,000,000", "The Aggregate Commitments are $1,500,000,000"), noisy)
    assert g.status is GroundingStatus.not_found


def test_ocr_confusable_labelled_but_not_verified():
    noisy = ocr_witness(PAGE.replace("BB+/Ba1/BB+", "BB+/Bal/BB+"))
    g = ground_field(fld("BB+/Ba1/BB+", "worse than or equal to BB+/Ba1/BB+ or unrated"), noisy)
    assert g.status is GroundingStatus.not_found and g.likely_ocr_noise


def test_wrapped_table_row_found_out_of_order():
    # OCR reads a wrapped row label interleaved with the rate cells.
    ocr = ocr_witness("5 worse than or equal 1.375% 0.375% to BB+/Ba1/BB+ 1.375% 0.250% or unrated")
    m = locate("5 worse than or equal to BB+/Ba1/BB+ or unrated 1.375% 0.375% 1.375% 0.250%", ocr[0])
    assert m.score >= 0.82


def test_value_in_text_requires_digit_words_exactly():
    assert value_in(normalise("Section 7.03(c)"), "pursuant to Section 7.03(c)")
    assert not value_in(normalise("Section 7.05(c)"), "pursuant to Section 7.03(c)")


def test_routing_gate():
    f = fld("$1,500,000,000", "The Aggregate Commitments are $1,500,000,000")
    assert route(f, ground_field(f, text_witness())).review_status == "auto_accepted"
    low = fld("$1,500,000,000", "The Aggregate Commitments are $1,500,000,000", conf=0.6)
    assert route(low, ground_field(low, text_witness())).review_status == "needs_review"
    bad = fld("$1,800,000,000", "The Aggregate Commitments are $1,500,000,000", conf=0.99)
    routed = route(bad, ground_field(bad, text_witness()))
    assert routed.review_status == "needs_review" and routed.routing_score == 0 and routed.hallucination_flag
