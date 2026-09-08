"""The per-request assembly of the shell document: the vite build's HTML with meta tags injected per request."""

import html
from typing import Final

from flask import Response

# Stamped on every document response so a caller can tell the real app from
# the "not built" placeholder, which is otherwise an identical HTTP 200 HTML
# response. The reveal flow's frontend health check reads it.
FRONTEND_BUILT_HEADER: Final[str] = "X-Frontend-Built"

BASE_PATH_META_NAME: Final[str] = "system-interface-base-path"


def html_response(html_content: str, status_code: int = 200) -> Response:
    """Build an uncacheable HTML response for a document.

    A document is assembled per request (the base path and the staleness tag are
    injected into it), so it is never a cacheable artifact to begin with. It is
    also the *only* thing standing between a reload and a stale UI: the built
    assets it links are content-hashed, so a freshly-fetched document always
    names the current bundle, and a cached one always names the old one.

    That matters because a page cannot drop its own HTTP cache -- the
    ``location.reload(true)`` form is a Firefox-only extension -- so
    ``reloadInterface`` (see the frontend's ``reload.ts``) can only reload and
    trust the response to be fresh. ``no-store`` is what makes that trust
    well-founded, including for viewers reaching the workspace through a
    shared tunnel, where an intermediary is free to cache anything we do not
    mark otherwise.
    """
    response = Response(html_content, status=status_code, mimetype="text/html")
    response.headers["Cache-Control"] = "no-store"
    return response


def document_response(html_content: str, *, is_frontend_built: bool) -> Response:
    """Return a document response, stamped with whether it is the real app.

    Both the app and the not-built placeholder are HTTP 200 HTML, so nothing
    downstream can tell them apart from the status line alone. The header says
    which one this is, so a health check does not have to pattern-match markup
    that is free to change.
    """
    response = html_response(html_content)
    response.headers[FRONTEND_BUILT_HEADER] = "true" if is_frontend_built else "false"
    return response


def inject_meta_tag(html_content: str, name: str, content: str) -> str:
    meta_tag = f'<meta name="{name}" content="{html.escape(content, quote=True)}">'
    return html_content.replace("</head>", f"{meta_tag}\n</head>")


def inject_base_path_meta_tag(html_content: str, root_path: str) -> str:
    return inject_meta_tag(html_content, BASE_PATH_META_NAME, root_path)
