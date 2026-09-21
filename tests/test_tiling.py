from credit_extract.config import ModelSpec
from credit_extract.ocr import OcrPage, OcrToken
from credit_extract.tiling import plan_bands


def spec(long_edge=1568, max_pixels=None, max_tiles=4, overlap=0.10) -> ModelSpec:
    return ModelSpec(role="frontier", provider="anthropic", model_name="x", prod_allowed=False, supports_sampling_params=False,
                     structured_output=True, max_output_tokens=1000,
                     image={"max_long_edge_px": long_edge, "max_pixels": max_pixels},
                     tiling={"overlap_frac": overlap, "max_tiles_per_page": max_tiles})


def ocr_lines(height: int, every_px: int = 60, line_h: int = 40) -> OcrPage:
    """Text lines every `every_px` px, each `line_h` px tall."""
    tokens = [OcrToken(text="w", conf=0.9, block=1, line=i,
                       bbox=(0.1, y / height, 0.9, min(1.0, (y + line_h) / height)))
              for i, y in enumerate(range(10, height - line_h, every_px))]
    return OcrPage(page=1, image_sha256="0" * 64, engine="fake", tokens=tokens)


def covers(bands, height):
    assert bands[0].y0 == 0 and bands[-1].y1 == height
    for a, b in zip(bands, bands[1:]):
        assert b.y0 < a.y1, "consecutive bands must overlap"
        assert b.y0 > a.y0


def test_small_page_is_one_band_at_full_resolution():
    bands = plan_bands(1000, 1400, spec(), dpi=300)
    assert len(bands) == 1 and bands[0].scale == 1.0 and bands[0].effective_dpi == 300


def test_a4_at_300dpi_splits_without_extra_downscale():
    w, h = 2481, 3508
    bands = plan_bands(w, h, spec(), dpi=300)
    covers(bands, h)
    assert len(bands) == 2
    for b in bands:
        assert b.y1 - b.y0 <= w  # never taller than wide -> only the width downscale applies
        assert abs(b.effective_dpi - 300 * 1568 / w) < 0.5


def test_cuts_land_between_ocr_lines():
    w, h = 2481, 3508
    ocr = ocr_lines(h)
    lines = [(int(t.bbox[1] * h), int(t.bbox[3] * h) + 1) for t in ocr.tokens]
    bands = plan_bands(w, h, spec(), dpi=300, ocr=ocr)
    covers(bands, h)
    for b in bands:
        for y in (b.y0, b.y1):
            if y not in (0, h):
                assert all(not (a < y < c) for a, c in lines), f"cut at {y} slices a line"


def test_snapping_never_adds_a_sliver_band():
    # Regression: snapping cuts upward to OCR gaps used to leave a ~20px third band.
    w, h = 2479, 3508
    s = spec(long_edge=2576, max_pixels=3_750_000, max_tiles=2, overlap=0.08)
    for every_px in (57, 60, 63, 71):
        bands = plan_bands(w, h, s, dpi=300, ocr=ocr_lines(h, every_px=every_px))
        covers(bands, h)
        assert len(bands) == 2
        assert min(b.y1 - b.y0 for b in bands) > h / 3


def test_max_pixels_limits_band_height():
    bands = plan_bands(2481, 3508, spec(max_pixels=1_150_000), dpi=300)
    covers(bands, 3508)
    assert len(bands) >= 3


def test_tile_cap_trades_resolution_for_cost():
    bands = plan_bands(2481, 3508, spec(max_pixels=500_000, max_tiles=2), dpi=300)
    covers(bands, 3508)
    assert len(bands) == 2
    assert all(b.effective_dpi < 300 * 1568 / 2481 for b in bands)  # visible cost of the cap
