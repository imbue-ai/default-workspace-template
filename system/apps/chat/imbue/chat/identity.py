"""Who a request comes from, as the proxy that admitted it vouches in the `X-Imbue-Identity` header.

The same reading the shell makes (system_interface's `shell/identity.py`): a missing header means
the request came through no current proxy -- an older forward, or a caller inside the container --
and is the owner's; an unreadable one is logged and read the same way. Only a header that says
`owner: false` is a visitor.
"""

from __future__ import annotations

import json
from typing import Final

from flask import Response
from flask import request
from loguru import logger

from imbue.chat.models import ErrorResponse

IDENTITY_HEADER: Final = "X-Imbue-Identity"
OWNER_ONLY_DETAIL: Final = "Only the owner of this workspace can connect an AI account."


def is_owner_identity(header_value: str | None) -> bool:
    """Whether a header value vouches for the workspace's owner."""
    if header_value is None or not header_value.strip():
        return True
    try:
        identity = json.loads(header_value)
    except json.JSONDecodeError:
        logger.warning("Ignored an unreadable {} header", IDENTITY_HEADER)
        return True
    if not isinstance(identity, dict) or not isinstance(identity.get("owner"), bool):
        logger.warning("Ignored an unreadable {} header", IDENTITY_HEADER)
        return True
    return identity["owner"]


def forbid_unless_owner() -> Response | None:
    """A 403 for a request a visitor made, or None for the owner's."""
    if is_owner_identity(request.headers.get(IDENTITY_HEADER)):
        return None
    logger.warning("Refused a visitor's request to {}", request.path)
    body = json.dumps(ErrorResponse(detail=OWNER_ONLY_DETAIL).model_dump(), separators=(",", ":"))
    return Response(body, status=403, mimetype="application/json")
