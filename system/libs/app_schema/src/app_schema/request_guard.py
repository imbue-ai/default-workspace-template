"""Who may call an app's routes at all: the provenance check every workspace app installs ahead of its routes
(``workspace_layout.identity``: a page of another origin is refused, and a request with no identity header is admitted
from loopback alone, under a loopback ``Host``), and the 400 for a present identity header no proxy stamped."""

from collections.abc import Set as AbstractSet
from typing import Final

from flask import Flask, Response, request
from flask.typing import ResponseReturnValue
from loguru import logger
from workspace_layout.errors import InvalidIdentityHeaderError
from workspace_layout.identity import (
    FETCH_MODE_HEADER,
    FETCH_SITE_HEADER,
    IDENTITY_HEADER,
    RequestProvenance,
    describe_provenance_refusal,
)

from app_schema.api_schema import HTTP_BAD_REQUEST
from app_schema.routes import error_response

HTTP_FORBIDDEN: Final[int] = 403


def request_provenance() -> RequestProvenance:
    """The current request's provenance, as the server saw it."""
    return RequestProvenance(
        method=request.method,
        identity_header=request.headers.get(IDENTITY_HEADER),
        fetch_site=request.headers.get(FETCH_SITE_HEADER),
        fetch_mode=request.headers.get(FETCH_MODE_HEADER),
        host=request.host,
        remote_address=request.remote_addr,
    )


def _answer_invalid_identity_header(error: InvalidIdentityHeaderError) -> Response:
    logger.warning(
        "{} {} refused its identity header: {}", request.method, request.path, error
    )
    return error_response(str(error), HTTP_BAD_REQUEST)


def install_request_guard(app: Flask, exempt_rules: AbstractSet[str]) -> None:
    """Refuse, ahead of every route of ``app``, a request whose provenance the check does not admit, and answer a
    present identity header that does not parse with a 400 on every route.

    Install it before any other request hook, so nothing runs for a request it refuses. ``exempt_rules`` are the
    Flask rules of routes meant for pages of other origins (a module served with a CORS header), which the check
    skips.
    """

    @app.before_request
    def refuse_requests_of_unknown_provenance() -> ResponseReturnValue | None:
        if request.url_rule is not None and request.url_rule.rule in exempt_rules:
            return None
        provenance = request_provenance()
        refusal = describe_provenance_refusal(provenance)
        if refusal is None:
            return None
        logger.warning(
            "Refused {} {} ({}): host {!r}, {} {!r}, from {}",
            request.method,
            request.path,
            refusal,
            provenance.host,
            FETCH_SITE_HEADER,
            provenance.fetch_site,
            provenance.remote_address,
        )
        return error_response(refusal, HTTP_FORBIDDEN)

    app.register_error_handler(
        InvalidIdentityHeaderError, _answer_invalid_identity_header
    )
