"""Who a request comes from, as the proxy that admitted it vouches in the `X-Imbue-Identity` header.

The parsing itself is the ``workspace_profile`` library's -- one definition for the shell, this
app, and the profile resolution alike: a missing header means the request came through no current
proxy (an older forward, or a caller inside the container) and is the owner's; an unreadable one
is logged and read the same way. Only a header that says ``owner: false`` is a visitor.
"""

from __future__ import annotations

from typing import Final

from app_schema.routes import error_response
from flask import Response
from flask import request
from loguru import logger
from workspace_profile.identity import IDENTITY_HEADER as IDENTITY_HEADER
from workspace_profile.identity import parse_identity_header

OWNER_ONLY_DETAIL: Final = "Only the owner of this workspace can connect an AI account."


def is_owner_identity(header_value: str | None) -> bool:
    """Whether a header value vouches for the workspace's owner."""
    return parse_identity_header(header_value).owner


def forbid_unless_owner() -> Response | None:
    """A 403 for a request a visitor made, or None for the owner's."""
    if is_owner_identity(request.headers.get(IDENTITY_HEADER)):
        return None
    logger.warning("Refused a visitor's request to {}", request.path)
    return error_response(OWNER_ONLY_DETAIL, 403)
