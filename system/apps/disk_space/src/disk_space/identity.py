"""Who is asking: the participant a request comes from, and the refusal for the routes only the owner may use.

Once the workspace is shared, anyone holding a grant on it (or on this app) reaches every route, so the routes that
delete files, start a chat in the owner's name or move the alert line refuse a visitor on the server, where hiding a
control could not. Reading the identity is ``workspace_layout.identity``'s, the same reading the shell and the other
workspace apps make: a request with no ``X-Imbue-Identity`` header is the owner's, and a header that says
``owner: false`` is a person or a visiting agent.
"""

from typing import Final

from app_schema.routes import error_response
from flask import Response, request
from loguru import logger
from workspace_layout.identity import (
    IDENTITY_HEADER,
    RequestIdentity,
    is_owner_participant,
    parse_identity_header,
    participant_key_of,
)

OWNER_ONLY_DETAIL: Final[str] = (
    "Only the owner of this workspace can delete files, start the cleanup helper or change the alert line."
)


def request_identity() -> RequestIdentity:
    """The requester this request's header vouches for; the anonymous owner without one.

    A header that does not parse raises, which the request guard's handler answers with a 400: it is never read as
    the owner's.
    """
    return parse_identity_header(request.headers.get(IDENTITY_HEADER))


def request_participant() -> str:
    """The participant key of the current request's sender, recorded with anything it changes."""
    return str(participant_key_of(request_identity()))


def forbid_unless_owner() -> Response | None:
    """A 403 for a request a visitor made, or None for the owner's (a request with no header included)."""
    if is_owner_participant(request_identity()):
        return None
    logger.warning("Refused a visitor's {} {}", request.method, request.path)
    return error_response(OWNER_ONLY_DETAIL, 403)
