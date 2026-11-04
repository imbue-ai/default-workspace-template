"""Read an image's displayed width and height from the start of its file.

An uploaded image's size rides its stored path (see ``attachments.store_uploaded_file``), so a
chat bubble can hold exactly the space the image will take before it loads. Only the header is
read, for the formats a chat bubble draws inline: PNG, GIF, BMP, WebP and JPEG. A JPEG whose
orientation tag turns it on its side reports its size turned, since that is how a browser draws it.
"""

import struct
from pathlib import Path

from imbue.imbue_common.pure import pure

# Enough of the file for every header below but a JPEG's.
_HEAD_BYTES = 64
# A JPEG's size sits after its metadata segments, which (each capped at 64 KiB) can hold an Exif
# block and an embedded preview; a frame header past this is not looked for.
_JPEG_SCAN_BYTES = 1024 * 1024

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


def read_image_size(path: Path) -> tuple[int, int] | None:
    """The width and height ``path`` is drawn at, or None when it is not an image read here."""
    try:
        with path.open("rb") as image_file:
            head = image_file.read(_HEAD_BYTES)
            if head.startswith(b"\xff\xd8"):
                image_file.seek(2)
                return _jpeg_size(image_file.read(_JPEG_SCAN_BYTES))
    except OSError:
        return None
    return _header_size(head)


@pure
def _header_size(head: bytes) -> tuple[int, int] | None:
    if head.startswith(_PNG_SIGNATURE) and len(head) >= 24:
        width, height = struct.unpack(">II", head[16:24])
        return _positive(width, height)
    if head[:6] in _GIF_SIGNATURES and len(head) >= 10:
        width, height = struct.unpack("<HH", head[6:10])
        return _positive(width, height)
    if head.startswith(b"BM") and len(head) >= 26:
        width, height = struct.unpack("<ii", head[18:26])
        # A negative height marks a top-down bitmap, the same size.
        return _positive(width, abs(height))
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return _webp_size(head)
    return None


@pure
def _webp_size(head: bytes) -> tuple[int, int] | None:
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
def _jpeg_size(body: bytes) -> tuple[int, int] | None:
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
def _positive(width: int, height: int) -> tuple[int, int] | None:
    return (width, height) if width > 0 and height > 0 else None
