"""Image budget & tiling (spec §8).

A full 300-DPI page usually exceeds a VLM's image budget, and the provider silently
downscales it until clause text is illegible. Instead the page is split into overlapping,
full-width horizontal bands, cut where OCR shows a gap between lines so no line of text
(or table row) is sliced. Bands are never taller than the budget allows, so each is only
downscaled by the unavoidable width factor; `effective_dpi` makes that visible.
"""

import math

from pydantic import BaseModel

from .config import ModelSpec
from .ocr import OcrPage


class Band(BaseModel):
    index: int
    y0: int  # pixel rows [y0, y1) of the full page image
    y1: int
    scale: float  # downscale the model will apply to this band (1.0 == none)
    effective_dpi: float


def _line_extents(ocr: OcrPage, height: int) -> list[tuple[int, int]]:
    lines: dict[int, list[float]] = {}
    for t in ocr.tokens:
        y = lines.setdefault(t.line, [1.0, 0.0])
        y[0], y[1] = min(y[0], t.bbox[1]), max(y[1], t.bbox[3])
    return [(int(a * height), math.ceil(b * height)) for a, b in lines.values()]


def _snap(target: int, lines: list[tuple[int, int]], lo: int, hi: int) -> int:
    """The y in [lo, hi] nearest `target` that is not inside any OCR line; `target` if none."""
    for delta in range(max(target - lo, hi - target) + 1):
        for y in (target - delta, target + delta):
            if lo <= y <= hi and all(not (a < y < b) for a, b in lines):
                return y
    return target


def plan_bands(width: int, height: int, spec: ModelSpec, dpi: int, ocr: OcrPage | None = None) -> list[Band]:
    budget = spec.image.max_long_edge_px
    scale = min(1.0, budget / width)
    max_h = int(budget / scale)  # band's long edge after downscale == budget
    if spec.image.max_pixels:
        max_h = min(max_h, int(spec.image.max_pixels / (width * scale * scale)))
    overlap = int(max_h * spec.tiling.overlap_frac)

    needed = 1 if height <= max_h else math.ceil((height - overlap) / (max_h - overlap))
    if needed > spec.tiling.max_tiles_per_page:
        # Cost cap wins: fewer, taller bands, which the model downscales further. Recorded in scale.
        needed = spec.tiling.max_tiles_per_page
        max_h = math.ceil((height + (needed - 1) * overlap) / needed)

    # Exactly `needed` bands. Evenly sized targets leave slack below max_h, so snapping a cut to an
    # OCR line gap never pushes the remainder into an extra sliver band.
    target_h = math.ceil((height + (needed - 1) * overlap) / needed)
    lines = _line_extents(ocr, height) if ocr else []
    bands, y0 = [], 0
    while True:
        if len(bands) == needed - 1:
            y1 = height  # last band takes the remainder; its downscale (if any) is recorded below
        else:
            cut = y0 + target_h
            y1 = _snap(cut, lines, lo=cut - overlap // 2, hi=min(cut + overlap // 2, y0 + max_h))
        band_h = y1 - y0
        s = min(1.0, budget / max(width, band_h))
        if spec.image.max_pixels:
            s = min(s, math.sqrt(spec.image.max_pixels / (width * band_h)))
        bands.append(Band(index=len(bands), y0=y0, y1=y1, scale=round(s, 4), effective_dpi=round(dpi * s, 1)))
        if y1 >= height:
            return bands
        y0 = _snap(y1 - overlap, lines, lo=max(y0 + 1, y1 - 2 * overlap), hi=y1 - 1)
