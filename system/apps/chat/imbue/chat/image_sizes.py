"""Read an image's displayed width and height from the start of its file.

An uploaded image's size rides its stored path (see ``attachments.store_uploaded_file``), so a
chat bubble can hold exactly the space the image will take before it loads. Only the header is
read, for PNG, GIF, BMP, WebP, JPEG and SVG; the other formats a bubble draws inline (AVIF, ICO)
keep an unsized path, and so the fixed-shape box. A JPEG whose orientation tag turns it on its
side reports its size turned, since that is how a browser draws it.
An SVG reports the size its root element declares (an absolute length such as ``pt`` or ``mm`` in
CSS pixels), or its viewBox's when it declares none: an SVG with only a viewBox has no size of its
own, and draws at nothing in a bubble that shrinks to fit. A size in a font-relative unit is not
read, since it depends on a font the file does not fix.
"""

import math
import re
import struct
from pathlib import Path

from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure

# The extensions a bubble draws inline (``IMAGE_EXTENSION_RE`` in frontend/src/models/attachments.ts --
# keep the two in step). Any other file is shown as a link, so its size is never read, even when its
# bytes happen to start like an image's.
_INLINE_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".bmp", ".svg", ".ico"})

# Enough of the file for every header below but a JPEG's.
_HEAD_BYTES = 64
# A JPEG's size sits after its metadata segments, which (each capped at 64 KiB) can hold an Exif
# block and an embedded preview; a frame header past this is not looked for.
_JPEG_SCAN_BYTES = 1024 * 1024

# An OS/2 bitmap's core header stores 16-bit sides; the info header and its successors 32-bit ones.
_BMP_CORE_HEADER_SIZE = 12
_BMP_INFO_HEADER_SIZE = 40
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_GIF_SIGNATURES = (b"GIF87a", b"GIF89a")

# JPEG start-of-frame markers, which carry the size: every 0xC0-0xCF except DHT (C4), JPG (C8) and DAC (CC).
_JPEG_FRAME_MARKERS = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}
# Markers that stand alone, with no length after them.
_JPEG_STANDALONE_MARKERS = frozenset(range(0xD0, 0xD8)) | {0x01}
_JPEG_APP1 = 0xE1
_EXIF_HEADER = b"Exif\x00\x00"
_EXIF_ORIENTATION_TAG = 0x0112
# Orientations 5-8 turn the picture a quarter turn, so it is drawn with its width and height swapped.
_TURNED_ORIENTATIONS = frozenset({5, 6, 7, 8})

# An SVG's root element sits after at most a prolog, a doctype and comments.
_SVG_SCAN_BYTES = 64 * 1024
_SVG_ROOT_RE = re.compile(r"<svg\b[^>]*>", re.IGNORECASE)
# A length and its unit. The lookbehind keeps ``stroke-width`` from reading as ``width``.
_SVG_LENGTH_RE_TEMPLATE = r"(?<![\w-]){name}\s*=\s*[\"']\s*([0-9]*\.?[0-9]+)\s*([a-zA-Z]*|%)\s*[\"']"
_SVG_WIDTH_RE = re.compile(_SVG_LENGTH_RE_TEMPLATE.format(name="width"))
_SVG_HEIGHT_RE = re.compile(_SVG_LENGTH_RE_TEMPLATE.format(name="height"))
# CSS pixels per absolute unit (a unitless length is in user units, drawn as pixels). A percentage
# is relative to where the image is placed, so it declares no size, as if the side were absent.
_SVG_PIXELS_PER_UNIT = {
    "": 1.0,
    "px": 1.0,
    "pt": 96 / 72,
    "pc": 16.0,
    "in": 96.0,
    "cm": 96 / 2.54,
    "mm": 96 / 25.4,
    "q": 96 / 101.6,
}
_SVG_PERCENT_UNIT = "%"
_SVG_VIEWBOX_RE = re.compile(
    r"(?<![\w-])viewBox\s*=\s*[\"']\s*-?[0-9.]+[\s,]+-?[0-9.]+[\s,]+([0-9]*\.?[0-9]+)[\s,]+([0-9]*\.?[0-9]+)\s*[\"']"
)


class ImageSize(FrozenModel):
    """The width and height an image is drawn at, in CSS pixels."""

    width: int = Field(description="The drawn width, in CSS pixels")
    height: int = Field(description="The drawn height, in CSS pixels")


def read_image_size(path: Path) -> ImageSize | None:
    """The width and height ``path`` is drawn at, or None when it is not an image read here."""
    if path.suffix.lower() not in _INLINE_IMAGE_SUFFIXES:
        return None
    try:
        with path.open("rb") as image_file:
            if path.suffix.lower() == ".svg":
                return _svg_size(image_file.read(_SVG_SCAN_BYTES).decode("utf-8", errors="replace"))
            head = image_file.read(_HEAD_BYTES)
            if head.startswith(b"\xff\xd8"):
                image_file.seek(2)
                return _jpeg_size(image_file.read(_JPEG_SCAN_BYTES))
    except OSError as e:
        logger.debug("Could not read the size of {}: {}", path, e)
        return None
    return _header_size(head)


@pure
def _header_size(head: bytes) -> ImageSize | None:
    if head.startswith(_PNG_SIGNATURE) and len(head) >= 24:
        width, height = struct.unpack(">II", head[16:24])
        return _positive(width, height)
    if head[:6] in _GIF_SIGNATURES and len(head) >= 10:
        width, height = struct.unpack("<HH", head[6:10])
        return _positive(width, height)
    if head.startswith(b"BM") and len(head) >= 26:
        return _bmp_size(head)
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return _webp_size(head)
    return None


@pure
def _bmp_size(head: bytes) -> ImageSize | None:
    """The size from a bitmap's DIB header, whose own length at offset 14 says how its sides are stored."""
    (dib_header_size,) = struct.unpack("<I", head[14:18])
    if dib_header_size == _BMP_CORE_HEADER_SIZE:
        width, height = struct.unpack("<HH", head[18:22])
        return _positive(width, height)
    if dib_header_size < _BMP_INFO_HEADER_SIZE:
        return None
    width, height = struct.unpack("<ii", head[18:26])
    # A negative height marks a top-down bitmap, the same size.
    return _positive(width, abs(height))


@pure
def _webp_size(head: bytes) -> ImageSize | None:
    chunk = head[12:16]
    if chunk == b"VP8 " and len(head) >= 30:
        width, height = struct.unpack("<HH", head[26:30])
        return _positive(width & 0x3FFF, height & 0x3FFF)
    if chunk == b"VP8L" and len(head) >= 25:
        bits = int.from_bytes(head[21:25], "little")
        return _positive((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
    if chunk == b"VP8X" and len(head) >= 30:
        return _positive(int.from_bytes(head[24:27], "little") + 1, int.from_bytes(head[27:30], "little") + 1)
    return None


@pure
def _jpeg_size(body: bytes) -> ImageSize | None:
    """The size from a JPEG's markers (``body`` starts after its start-of-image), turned by its orientation."""
    orientation = 1
    offset = 0
    while offset + 4 <= len(body):
        if body[offset] != 0xFF:
            return None
        marker = body[offset + 1]
        if marker == 0xFF:
            # Fill bytes may pad between markers.
            offset += 1
            continue
        if marker in _JPEG_STANDALONE_MARKERS:
            offset += 2
            continue
        (length,) = struct.unpack(">H", body[offset + 2 : offset + 4])
        segment = body[offset + 4 : offset + 2 + length]
        if marker == _JPEG_APP1 and segment.startswith(_EXIF_HEADER):
            orientation = _exif_orientation(segment[len(_EXIF_HEADER) :]) or orientation
        if marker in _JPEG_FRAME_MARKERS:
            if len(segment) < 5:
                return None
            height, width = struct.unpack(">HH", segment[1:5])
            if orientation in _TURNED_ORIENTATIONS:
                width, height = height, width
            return _positive(width, height)
        offset += 2 + length
    return None


@pure
def _exif_orientation(tiff: bytes) -> int | None:
    """The orientation tag of an Exif block's first image directory, when it has one."""
    if len(tiff) < 8:
        return None
    byte_order = {b"II": "<", b"MM": ">"}.get(tiff[:2])
    if byte_order is None:
        return None
    (directory_offset,) = struct.unpack(f"{byte_order}I", tiff[4:8])
    if directory_offset + 2 > len(tiff):
        return None
    (entry_count,) = struct.unpack(f"{byte_order}H", tiff[directory_offset : directory_offset + 2])
    for index in range(entry_count):
        entry_start = directory_offset + 2 + index * 12
        entry = tiff[entry_start : entry_start + 12]
        if len(entry) < 12:
            return None
        tag, value_type = struct.unpack(f"{byte_order}HH", entry[:4])
        if tag == _EXIF_ORIENTATION_TAG and value_type == 3:
            (orientation,) = struct.unpack(f"{byte_order}H", entry[8:10])
            return orientation
    return None


@pure
def _svg_size(text: str) -> ImageSize | None:
    """The size an SVG's root element declares; a missing side follows its viewBox's shape."""
    root = _SVG_ROOT_RE.search(text)
    if root is None:
        return None
    tag = root.group(0)
    width_match = _SVG_WIDTH_RE.search(tag)
    height_match = _SVG_HEIGHT_RE.search(tag)
    viewbox_match = _SVG_VIEWBOX_RE.search(tag)
    if _is_relative_length(width_match) or _is_relative_length(height_match):
        return None
    width = _length_in_pixels(width_match)
    height = _length_in_pixels(height_match)
    if width is not None and height is not None:
        return _rounded_size(width, height)
    if viewbox_match is None:
        return None
    viewbox_width, viewbox_height = float(viewbox_match.group(1)), float(viewbox_match.group(2))
    if viewbox_width <= 0 or viewbox_height <= 0:
        return None
    if width is not None:
        return _rounded_size(width, width * viewbox_height / viewbox_width)
    if height is not None:
        return _rounded_size(height * viewbox_width / viewbox_height, height)
    return _rounded_size(viewbox_width, viewbox_height)


@pure
def _is_relative_length(length_match: re.Match[str] | None) -> bool:
    """Whether a declared SVG length is in a unit (``em``, ``ex``, ...) whose size depends on a font."""
    if length_match is None:
        return False
    unit = length_match.group(2).lower()
    return unit != _SVG_PERCENT_UNIT and unit not in _SVG_PIXELS_PER_UNIT


@pure
def _length_in_pixels(length_match: re.Match[str] | None) -> float | None:
    """A declared SVG length in CSS pixels, or None when the side is absent or a percentage."""
    if length_match is None or length_match.group(2) == _SVG_PERCENT_UNIT:
        return None
    return float(length_match.group(1)) * _SVG_PIXELS_PER_UNIT[length_match.group(2).lower()]


@pure
def _rounded_size(width: float, height: float) -> ImageSize | None:
    # A length too long to hold in a float reads as infinite, and says nothing about the drawn size.
    if not (math.isfinite(width) and math.isfinite(height)):
        return None
    return _positive(round(width), round(height))


@pure
def _positive(width: int, height: int) -> ImageSize | None:
    return ImageSize(width=width, height=height) if width > 0 and height > 0 else None
