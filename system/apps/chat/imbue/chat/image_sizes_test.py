"""Unit tests for reading an image's size from its header."""

import struct
from pathlib import Path

import pytest

from imbue.chat.image_sizes import ImageSize
from imbue.chat.image_sizes import read_image_size


def _png(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x06\x00\x00\x00"
    )


def _gif(width: int, height: int) -> bytes:
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\x00" * 8


def _bmp(width: int, height: int) -> bytes:
    return b"BM" + b"\x00" * 12 + struct.pack("<Iii", 40, width, height) + b"\x00" * 28


def _os2_bmp(width: int, height: int) -> bytes:
    return b"BM" + b"\x00" * 12 + struct.pack("<IHH", 12, width, height) + b"\x00" * 32


def _webp(chunk: bytes, payload: bytes) -> bytes:
    body = b"WEBP" + chunk + struct.pack("<I", len(payload)) + payload
    return b"RIFF" + struct.pack("<I", len(body)) + body


def _exif(orientation: int) -> bytes:
    tiff = (
        b"MM\x00\x2a"
        + struct.pack(">I", 8)
        + struct.pack(">H", 1)
        + struct.pack(">HHIH", 0x0112, 3, 1, orientation)
        + b"\x00\x00"
        + struct.pack(">I", 0)
    )
    segment = b"Exif\x00\x00" + tiff
    return b"\xff\xe1" + struct.pack(">H", len(segment) + 2) + segment


def _jpeg(width: int, height: int, *, orientation: int | None = None) -> bytes:
    frame = struct.pack(">BHHB", 8, height, width, 3) + b"\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    exif = _exif(orientation) if orientation is not None else b""
    # A JFIF segment and fill bytes ahead of the frame, as real files carry.
    jfif = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    return (
        b"\xff\xd8" + jfif + exif + b"\xff\xff" + b"\xff\xc0" + struct.pack(">H", len(frame) + 2) + frame + b"\xff\xd9"
    )


def _write(tmp_path: Path, name: str, content: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(content)
    return path


@pytest.mark.parametrize(
    ("name", "content", "expected"),
    [
        ("a.png", _png(1600, 900), (1600, 900)),
        ("a.gif", _gif(320, 240), (320, 240)),
        ("a.bmp", _bmp(64, 48), (64, 48)),
        ("top-down.bmp", _bmp(64, -48), (64, 48)),
        ("os2.bmp", _os2_bmp(64, 48), (64, 48)),
        ("lossy.webp", _webp(b"VP8 ", b"\x00\x00\x00\x9d\x01\x2a" + struct.pack("<HH", 800, 600)), (800, 600)),
        (
            "lossless.webp",
            _webp(b"VP8L", b"\x2f" + ((800 - 1) | ((600 - 1) << 14)).to_bytes(4, "little")),
            (800, 600),
        ),
        (
            "extended.webp",
            _webp(b"VP8X", b"\x00" * 4 + (1999).to_bytes(3, "little") + (999).to_bytes(3, "little")),
            (2000, 1000),
        ),
        ("a.jpg", _jpeg(4032, 3024), (4032, 3024)),
        ("upright.jpg", _jpeg(4032, 3024, orientation=1), (4032, 3024)),
    ],
)
def test_reads_the_size_from_each_inline_format(
    tmp_path: Path, name: str, content: bytes, expected: tuple[int, int]
) -> None:
    assert read_image_size(_write(tmp_path, name, content)) == ImageSize(width=expected[0], height=expected[1])


@pytest.mark.parametrize(
    ("svg", "expected"),
    [
        ('<svg xmlns="http://www.w3.org/2000/svg" width="640" height="480"><rect/></svg>', (640, 480)),
        ('<svg width="64px" height="32px" viewBox="0 0 10 10"></svg>', (64, 32)),
        ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 200" role="img"></svg>', (300, 200)),
        ("<svg viewBox='0,0,120.5,80.4'></svg>", (120, 80)),
        ('<svg width="600" viewBox="0 0 300 200"></svg>', (600, 400)),
        ('<svg height="100" viewBox="0 0 300 200"></svg>', (150, 100)),
        ('<svg width="100%" height="100%" viewBox="0 0 40 30"></svg>', (40, 30)),
        ('<?xml version="1.0"?>\n<!-- drawn by hand -->\n<svg viewBox="0 0 50 50"></svg>', (50, 50)),
        # A matplotlib figure and an Inkscape page draw at their absolute size, not their viewBox's.
        ('<svg width="460.8pt" height="345.6pt" viewBox="0 0 460.8 345.6"></svg>', (614, 461)),
        ('<svg width="210mm" height="297mm" viewBox="0 0 210 297"></svg>', (794, 1123)),
        ('<svg width="2IN" viewBox="0 0 10 5"></svg>', (192, 96)),
    ],
)
def test_reads_the_size_an_svg_declares_or_its_viewbox_shape(
    tmp_path: Path, svg: str, expected: tuple[int, int]
) -> None:
    assert read_image_size(_write(tmp_path, "drawing.svg", svg.encode())) == ImageSize(
        width=expected[0], height=expected[1]
    )


def test_a_stroke_width_is_not_read_as_an_svg_width(tmp_path: Path) -> None:
    svg = b'<svg stroke-width="2" viewBox="0 0 300 200"></svg>'

    assert read_image_size(_write(tmp_path, "drawing.svg", svg)) == ImageSize(width=300, height=200)


@pytest.mark.parametrize(
    "svg",
    [
        '<svg width="100%" height="100%"></svg>',
        '<svg width="10em" height="5em"></svg>',
        '<svg width="10em" height="5em" viewBox="0 0 40 20"></svg>',
        "<html>not an svg</html>",
        f'<svg width="{"9" * 400}" height="10"></svg>',
        f'<svg width="1{"0" * 300}" viewBox="0 0 1 1{"0" * 300}"></svg>',
    ],
)
def test_an_svg_that_declares_no_drawn_size_has_none(tmp_path: Path, svg: str) -> None:
    assert read_image_size(_write(tmp_path, "drawing.svg", svg.encode())) is None


def test_a_phone_photo_turned_on_its_side_reports_the_size_it_is_drawn_at(tmp_path: Path) -> None:
    """A portrait phone photo is stored landscape with an orientation tag (6: turn clockwise), and
    a browser draws it portrait, so its box must be portrait too."""
    assert read_image_size(_write(tmp_path, "portrait.jpg", _jpeg(4032, 3024, orientation=6))) == ImageSize(
        width=3024, height=4032
    )


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("notes.txt", b"just some text, nothing to measure"),
        ("bitmap-bytes.txt", _bmp(64, 48)),
        ("empty.png", b""),
        ("truncated.png", b"\x89PNG\r\n\x1a\n\x00\x00"),
        ("zero.png", _png(0, 0)),
        ("no-frame.jpg", b"\xff\xd8\xff\xd9"),
    ],
)
def test_a_file_that_is_not_a_readable_image_has_no_size(tmp_path: Path, name: str, content: bytes) -> None:
    assert read_image_size(_write(tmp_path, name, content)) is None


def test_a_missing_file_has_no_size(tmp_path: Path) -> None:
    assert read_image_size(tmp_path / "gone.png") is None
