"""Degraded-scan test variants (spec §19). Test tooling only — never used on production input.

A pristine HTML->PDF render is not a scan. This turns any PDF into an image-only PDF with
scan artefacts, deterministically from a seed, so accuracy is measured on realistic input.
"""

import io
from pathlib import Path

import numpy as np
import pypdfium2 as pdfium
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont
from pydantic import BaseModel, ConfigDict, Field


class DegradeProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skew_deg_max: float = Field(default=1.2, ge=0)
    blur_radius_max: float = Field(default=0.9, ge=0)
    gaussian_noise_sigma: float = Field(default=10.0, ge=0)
    salt_pepper_frac: float = Field(default=0.001, ge=0, le=0.05)
    contrast: float = Field(default=0.8, gt=0)  # <1 washes the page out
    downsample_dpi: int | None = Field(default=200, gt=0)  # simulate a lower-resolution scanner
    jpeg_quality: int = Field(default=45, ge=5, le=95)
    stamp_prob: float = Field(default=0.25, ge=0, le=1)


def _stamp(img: Image.Image, rng: np.random.Generator) -> Image.Image:
    w, h = img.size
    font = ImageFont.load_default(size=max(24, w // 30))
    label = str(rng.choice(["RECEIVED", "COPY", "FILED", "SCANNED"]))
    layer = Image.new("L", (w // 3, w // 10), 0)
    draw = ImageDraw.Draw(layer)
    draw.rectangle([4, 4, layer.width - 5, layer.height - 5], outline=255, width=max(3, w // 400))
    draw.text((layer.width // 2, layer.height // 2), label, fill=255, font=font, anchor="mm")
    layer = layer.rotate(float(rng.uniform(-25, 25)), expand=True, fillcolor=0)
    x = int(rng.uniform(0, max(1, w - layer.width)))
    y = int(rng.uniform(0, max(1, h - layer.height)))
    ink = Image.new("L", layer.size, 90)
    out = img.copy()
    out.paste(ink, (x, y), layer.point(lambda v: int(v * 0.55)))  # semi-transparent ink
    return out


def degrade_page(img: Image.Image, rng: np.random.Generator, profile: DegradeProfile, dpi: int) -> Image.Image:
    """Apply scan artefacts to one grayscale page image. Output size equals input size."""
    img = img.convert("L")
    size = img.size

    if profile.downsample_dpi and profile.downsample_dpi < dpi:
        f = profile.downsample_dpi / dpi
        img = img.resize((max(1, int(size[0] * f)), max(1, int(size[1] * f))), Image.Resampling.BILINEAR)
        img = img.resize(size, Image.Resampling.BILINEAR)

    img = ImageEnhance.Contrast(img).enhance(profile.contrast)
    if profile.skew_deg_max:
        img = img.rotate(float(rng.uniform(-profile.skew_deg_max, profile.skew_deg_max)),
                         resample=Image.Resampling.BILINEAR, fillcolor=255)
    if profile.blur_radius_max:
        img = img.filter(ImageFilter.GaussianBlur(float(rng.uniform(0, profile.blur_radius_max))))
    if rng.random() < profile.stamp_prob:
        img = _stamp(img, rng)

    arr = np.asarray(img, dtype=np.float32)
    if profile.gaussian_noise_sigma:
        arr = arr + rng.normal(0, profile.gaussian_noise_sigma, arr.shape)
    if profile.salt_pepper_frac:
        mask = rng.random(arr.shape)
        arr[mask < profile.salt_pepper_frac / 2] = 0
        arr[mask > 1 - profile.salt_pepper_frac / 2] = 255
    img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), mode="L")

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=profile.jpeg_quality)
    return Image.open(io.BytesIO(buf.getvalue())).convert("L")


def degrade_pdf(src: Path, dest: Path, *, seed: int, dpi: int = 300,
                profile: DegradeProfile | None = None) -> int:
    """Write an image-only, degraded copy of `src` to `dest`. Returns the page count.

    Pages are processed one at a time (a 300-page document must not be held in memory).
    Each page gets its own RNG stream derived from (seed, page) so output is reproducible.
    """
    profile = profile or DegradeProfile()
    source = pdfium.PdfDocument(src)
    out = pdfium.PdfDocument.new()
    try:
        for i in range(len(source)):
            page = source[i]
            img = page.render(scale=dpi / 72, grayscale=True).to_pil()
            page.close()
            degraded = degrade_page(img, np.random.default_rng([seed, i]), profile, dpi)
            buf = io.BytesIO()
            degraded.save(buf, format="PDF", resolution=dpi)
            single = pdfium.PdfDocument(buf.getvalue())
            out.import_pages(single)
            single.close()
        dest.parent.mkdir(parents=True, exist_ok=True)
        out.save(dest)
        return len(source)
    finally:
        out.close()
        source.close()
