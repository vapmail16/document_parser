from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont


@pytest.fixture
def scanned_pdf(tmp_path) -> Path:
    """A 3-page image-only PDF (no text layer), like a production scan."""
    font = ImageFont.load_default(size=14)
    pages = []
    for n in range(1, 4):
        img = Image.new("L", (306, 396), 255)  # half-letter at 72 DPI
        draw = ImageDraw.Draw(img)
        draw.text((20, 20), f"SECTION {n}.01 Applicable Margin", fill=0, font=font)
        draw.text((20, 50), "The Applicable Margin shall be 2.25% per annum.", fill=0, font=font)
        pages.append(img)
    path = tmp_path / "scan.pdf"
    pages[0].save(path, format="PDF", save_all=True, append_images=pages[1:], resolution=72)
    return path


@pytest.fixture
def text_pdf(tmp_path) -> Path:
    """A 1-page PDF with a real text layer (hand-built, Helvetica)."""
    content = b"BT /F1 12 Tf 72 720 Td (Total Commitments: $50,000,000) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    path = tmp_path / "text.pdf"
    path.write_bytes(bytes(out))
    return path
