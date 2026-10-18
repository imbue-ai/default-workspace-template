"""The request-side checks the routes share (docs/system/specs/screen/spec.md sections 6.3 and 10.1): who the
request is (the identity header, a header-less request being the owner), which session it speaks for, and whether
the lease lets that session act."""

from typing import Final

from flask import request

from screen.identity import IDENTITY_HEADER
from screen.identity import parse_identity_header
from screen.primitives import NekoSessionId
from screen.service import ScreenService

SESSION_ID_FIELD: Final[str] = "session_id"
VISITOR_REFUSAL: Final[str] = "this route is the workspace owner's"
CONTROLLER_REFUSAL: Final[str] = "only the session in control of the Screen may do this"
# Bodies the form parser reads (streaming a large file to disk); a JSON attempt on one would hold it whole in memory.
_FORM_MIMETYPES: Final[frozenset[str]] = frozenset({"multipart/form-data", "application/x-www-form-urlencoded"})


def is_owner_request() -> bool:
    return parse_identity_header(request.headers.get(IDENTITY_HEADER)).owner


def session_id_from_request() -> NekoSessionId | None:
    """The session a request speaks for: the ``session_id`` field of its form (a multipart or urlencoded body), else
    of its JSON body (whatever the content type says, as the human verbs read it), else of its query string."""
    if request.mimetype in _FORM_MIMETYPES:
        raw = request.form.get(SESSION_ID_FIELD)
    else:
        body = request.get_json(force=True, silent=True)
        from_body = body.get(SESSION_ID_FIELD) if isinstance(body, dict) else None
        raw = from_body if isinstance(from_body, str) else request.args.get(SESSION_ID_FIELD)
    return NekoSessionId(raw) if isinstance(raw, str) and raw else None


def describe_visitor_refusal(service: ScreenService, session_id: NekoSessionId) -> str | None:
    """A visitor acts only as a visitor's session: naming the owner's, whose id the state document carries while
    it controls, is refused. None when the request may act as the session."""
    if is_owner_request():
        return None
    session = service.known_session(session_id)
    if session is not None and session.is_owner:
        return VISITOR_REFUSAL
    return None


def describe_controller_refusal(service: ScreenService, session_id: NekoSessionId) -> str | None:
    """Why the session may not act as the controller: a visitor naming the owner's session, or any session that does
    not hold control. None when it may."""
    visitor_refusal = describe_visitor_refusal(service, session_id)
    if visitor_refusal is not None:
        return visitor_refusal
    if not service.is_controller_session(session_id):
        return CONTROLLER_REFUSAL
    return None
