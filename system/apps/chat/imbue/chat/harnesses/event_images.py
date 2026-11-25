"""Images a harness's tool output carries, served one at a time by the per-event images route.

A tool result's images stay on disk in the harness's own transcript, like the rest of its
output (the payload-free wire contract in :mod:`harnesses.events`): the event carries only
``image_count``, and ``GET /api/chats/<chat>/events/<event>/images/<n>`` re-reads the nth
image from the event's source line.
"""

import base64
import binascii
import re
from typing import Final

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure

# The image types the chat renders. An SVG is left out: it is a document that can carry
# script, and nothing a harness generates or views needs it.
SERVED_IMAGE_MEDIA_TYPES: Final[frozenset[str]] = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})

_BASE64_DATA_URL_RE: Final[re.Pattern[str]] = re.compile(r"data:([\w.+-]+/[\w.+-]+);base64,(.*)", re.DOTALL)


class EventImage(FrozenModel):
    """One image from a tool result, decoded."""

    media_type: str = Field(description="The image's MIME type, one of SERVED_IMAGE_MEDIA_TYPES")
    data: bytes = Field(description="The decoded image bytes")


@pure
def served_media_type(image_url: str) -> str | None:
    """The media type of a base64 ``data:`` URL the chat can serve, or None for any other URL."""
    match = _BASE64_DATA_URL_RE.match(image_url)
    if match is None or match.group(1) not in SERVED_IMAGE_MEDIA_TYPES:
        return None
    return match.group(1)


@pure
def decode_image_data_url(image_url: str) -> EventImage | None:
    """The image a base64 ``data:`` URL holds, or None when it is not a servable image."""
    media_type = served_media_type(image_url)
    if media_type is None:
        return None
    _, _, encoded = image_url.partition(",")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return None
    return EventImage(media_type=media_type, data=data)
