"""REQ-OPS-04: upload validation by magic bytes, never client-declared type.

Fixtures live in shared/fixtures/images. Activates when P1's claims upload
validation lands; skips cleanly before that.
"""

from pathlib import Path

import pytest

up = pytest.importorskip("app.services.uploads", reason="P1's upload validation not landed yet")

FIXTURES = Path(__file__).resolve().parents[3] / "shared" / "fixtures" / "images"


def _bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def test_real_png_passes():
    assert up.sniff_magic(_bytes("valid.png")) == "png"


def test_real_jpeg_passes():
    assert up.sniff_magic(_bytes("valid.jpg")) == "jpeg"


def test_webp_passes():
    assert up.sniff_magic(_bytes("valid.webp")) == "webp"


def test_text_spoofing_a_jpg_is_rejected():
    # magic bytes, not the client-declared type, decide validity
    assert up.sniff_magic(_bytes("spoofed_jpeg.txt")) != "jpeg"


def test_png_fixture_starts_with_png_signature():
    data = _bytes("valid.png")
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
