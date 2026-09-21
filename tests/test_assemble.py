from credit_extract.assemble import (Assembly, classify, coverage, detect_furniture, is_furniture, join_overlap,
                                     merge_page)
from credit_extract.contracts import (EnvelopeBlock, EnvelopeCell, EnvelopeField, EnvelopeTable, PageEnvelope)
from credit_extract.gateway import ModelResponse
from credit_extract.ocr import OcrPage, OcrToken
from credit_extract.pass1 import BandAttempt, PagePass1
from credit_extract.tiling import Band

FOOTER = "https://www.sec.gov/Archives/edgar/data/915913/exhibit.htm {n}/211"
HEADER = "21/09/2026, 10:38 Document"


def ocr_page(page: int, body: list[str]) -> OcrPage:
    lines = [(HEADER, 0.02)] + [(t, 0.1 + 0.05 * i) for i, t in enumerate(body)] + [(FOOTER.format(n=page), 0.98)]
    tokens = [OcrToken(text=w, conf=0.9, bbox=(0.1, y, 0.2, y + 0.01), block=1, line=li)
              for li, (text, y) in enumerate(lines) for w in text.split()]
    return OcrPage(page=page, image_sha256="0" * 64, engine="fake", tokens=tokens)


def band_result(page: int, envelopes: list[PageEnvelope], height=1000) -> PagePass1:
    n = len(envelopes)
    bands = [BandAttempt(band=Band(index=i, y0=int(i * height / n * 0.9), y1=int((i + 1) * height / n) if i < n - 1 else height,
                                   scale=1, effective_dpi=300),
                         bands_on_page=n, cache_key="k", cached=True,
                         response=ModelResponse(model_key="m", envelope=e)) for i, e in enumerate(envelopes)]
    return PagePass1(page=page, model_key="m", prompt_version="v1", prompt_sha256="x", status="ok",
                     bands=bands, attempts=bands)


def env(blocks=(), fields=(), tables=()) -> PageEnvelope:
    return PageEnvelope(blocks=list(blocks), fields=list(fields), tables=list(tables), parties=[])


def test_furniture_detected_from_margins_and_masked_page_numbers():
    pages = [ocr_page(p, [f"body text of page {p}"]) for p in range(1, 6)]
    furniture = detect_furniture(pages)
    assert len(furniture) == 2
    assert is_furniture(FOOTER.format(n=187), furniture)
    assert is_furniture("21/09/2026, 11:02 Document", furniture)
    assert is_furniture("17", furniture) and is_furniture("- iv -", furniture)
    assert not is_furniture("The Applicable Rate shall be 1.375%", furniture)


def test_join_overlap_removes_repeated_words():
    assert join_overlap("the rate shall be one point", "shall be one point five percent") == \
        "the rate shall be one point five percent"
    assert join_overlap("alpha beta", "gamma delta") == "alpha beta gamma delta"


def test_band_overlap_merges_blocks_tables_and_fields():
    grid = lambda rows: EnvelopeTable(cells=[EnvelopeCell(row=r, col=c, raw_value=v)
                                            for r, row in enumerate(rows) for c, v in enumerate(row)],
                                      continues_on_next=True)
    b0 = env(blocks=[EnvelopeBlock(kind="other", text=HEADER),
                     EnvelopeBlock(kind="clause", text="“Applicable Rate” means as set forth below:",
                                   continues_on_next=True)],
             fields=[EnvelopeField(name="Level 5 rate", raw_value="1.375%", source_text="5 | BB+ | 1.375%",
                                   confidence=0.9, block_index=1)],
             tables=[grid([["Level", "Rate"], ["4", "1.200%"], ["5", "1.375%"]])])
    b1 = env(blocks=[EnvelopeBlock(kind="other", text="5 | 1.375%", continues_from_previous=True),
                     EnvelopeBlock(kind="clause", text="Initially, Pricing Level 3 applies."),
                     EnvelopeBlock(kind="other", text=FOOTER.format(n=17))],
             fields=[EnvelopeField(name="Rate (column 1), level 5", raw_value="1.375%", source_text="5 1.375%",
                                   confidence=0.9, block_index=0),
                     EnvelopeField(name="Initial Pricing Level", raw_value="Level 3",
                                   source_text="Initially, Pricing Level 3 applies.", confidence=0.9, block_index=1)],
             tables=[EnvelopeTable(cells=[EnvelopeCell(row=0, col=0, raw_value="5"),
                                          EnvelopeCell(row=0, col=1, raw_value="1.375%")],
                                   continues_from_previous=True)])
    furniture = detect_furniture([ocr_page(p, ["x"]) for p in range(1, 5)])
    asm = Assembly()
    merge_page(band_result(17, [b0, b1]), furniture, None, lambda y: y / 1000, asm)

    assert [b.text for b in asm.blocks] == ["“Applicable Rate” means as set forth below:",
                                            "Initially, Pricing Level 3 applies."]
    assert len(asm.tables) == 1 and [r[1][4] for r in asm.tables[0].rows] == ["Rate", "1.200%", "1.375%"]
    assert [f.raw.name for f in asm.fields] == ["Level 5 rate", "Initial Pricing Level"]  # dup dropped


def test_clause_stitched_across_pages_only_when_adjacent():
    p1 = env(blocks=[EnvelopeBlock(kind="clause", text="The Borrower shall repay the", continues_on_next=True)])
    p2 = env(blocks=[EnvelopeBlock(kind="clause", text="Loans on the Maturity Date.", continues_from_previous=True)])
    asm = Assembly()
    merge_page(band_result(4, [p1]), [], None, lambda y: y / 1000, asm)
    merge_page(band_result(5, [p2]), [], None, lambda y: y / 1000, asm)
    assert [(b.text, b.pages) for b in asm.blocks] == [("The Borrower shall repay the Loans on the Maturity Date.", [4, 5])]

    asm = Assembly()
    merge_page(band_result(4, [p1]), [], None, lambda y: y / 1000, asm)
    merge_page(band_result(9, [p2]), [], None, lambda y: y / 1000, asm)  # gap: never stitched
    assert len(asm.blocks) == 2


def test_coverage_counts_ocr_words_accounted_for():
    assert coverage(["the", "rate", "is", "1.375%"], "The rate is 1.375%.") == 1.0
    assert coverage(["the", "rate", "is", "1.375%"], "The rate") == 0.5


def test_classify_cover_page():
    assert classify("Execution Version AMENDED AND RESTATED CREDIT AGREEMENT") == ("executed", "restatement")
    assert classify("FIRST AMENDMENT TO FOURTH AMENDED AND RESTATED CREDIT AGREEMENT")[1] == "amendment"
    assert classify("DRAFT 3/1 CREDIT AGREEMENT") == ("draft", "original")
