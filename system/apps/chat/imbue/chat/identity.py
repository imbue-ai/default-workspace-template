"""Who a request comes from, as the proxy that admitted it vouches in the `X-Imbue-Identity` header.

The same reading the shell makes (``imbue.app_sdk.identity``): a missing header means the request came
through no current proxy -- an older forward, or a caller inside the container -- and is the owner's; a present
header that does not parse is nobody's proxy's, and the request is refused with a 400 (the multiplayer spec, D10)
rather than read as the owner's. Only a header that says ``owner: false`` is a visitor, and a visitor is a person
(``user_id``) or a visiting agent (``agent_id``). Every requester has a participant key (the multiplayer spec,
section 4.1): the owner of a workspace that has not shared is the synthetic ``owner``.
"""

from typing import Final

from flask import Response
from flask import request
from loguru import logger
from workspace_layout.errors import InvalidIdentityHeaderError
from workspace_layout.primitives import ParticipantKey

from imbue.app_sdk.identity import IDENTITY_HEADER
from imbue.app_sdk.identity import RequestIdentity
from imbue.app_sdk.identity import is_owner_participant
from imbue.app_sdk.identity import parse_identity_header
from imbue.app_sdk.identity import participant_key_of
from imbue.app_sdk.routes import error_response
from imbue.chat.errors import ChatAppError

OWNER_ONLY_DETAIL: Final = "Only the owner of this workspace can connect an AI account."


class UnreadableIdentityHeaderError(ChatAppError):
    """A request whose identity header does not parse: refused, as every chat app error is, with a 400."""


def _parse_request_header(header_value: str | None) -> RequestIdentity:
    """The identity a header vouches for; a present header that does not parse refuses the request."""
    try:
        return parse_identity_header(header_value)
    except InvalidIdentityHeaderError as e:
        raise UnreadableIdentityHeaderError(str(e)) from e


def request_identity() -> RequestIdentity:
    """The requester the current request's header vouches for; the anonymous owner without one."""
    return _parse_request_header(request.headers.get(IDENTITY_HEADER))


def request_participant() -> ParticipantKey:
    """The participant key of the current request's sender."""
    return participant_key_of(request_identity())


def is_owner_identity(header_value: str | None) -> bool:
    """Whether a header value vouches for the workspace's owner (under either of the owner's keys)."""
    return is_owner_participant(_parse_request_header(header_value))


def forbid_unless_owner() -> Response | None:
    """A 403 for a request a visitor made, or None for the owner's."""
    if is_owner_identity(request.headers.get(IDENTITY_HEADER)):
        return None
    logger.warning("Refused a visitor's request to {}", request.path)
    return error_response(OWNER_ONLY_DETAIL, 403)
