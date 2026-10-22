"""The ``Content-Security-Policy`` an app's HTML pages are served under.

Every page an app serves is reachable from the public internet once the
workspace is shared, so each carries a policy that keeps a script which
reaches the page after the server rendered it (DOM injection, a browser
extension, a tampering intermediary) from running or fetching: scripts come
only from the page's own origin (plus the page's own inline ``<script>``
blocks, each admitted by the hash of its body, computed from the response as
it goes out, so an inline script the server itself rendered is admitted and
escaping what a template interpolates stays the template's job), fetches and
sockets reach only the page's origin (``'self'`` covers a same-host ``ws(s):``
socket in every browser since Safari 16; WebKit matched the spec in April
2022), and plugins and ``<base>`` retargeting are off.
Styles may be inline, since the workspace's frontends set style attributes
from script; images come from the page's own origin, or are ``data:`` and
``blob:`` URLs the page itself holds; and frames may hold any http(s) page,
since the shell and the chat frame sibling apps whose origins only the
browser knows. So the defaults admit no cross-origin fetch at all, and an
app that renders content from elsewhere opens exactly the directives that
content needs, with the reason beside the override: the chat its markdown's
images and media from anywhere, Getting Started its catalog's thumbnails
from the catalog's origin (``url_origin`` gives the source for one). The
``frame-ancestors`` directive is deliberately absent: the proxy in front of
the workspace (the share gateway, or ``mngr forward`` locally) owns who may
embed a page, and appends its own policy, which composes with this one.

``attach_document_policy`` stamps the policy on every HTML response a Flask
app or blueprint sends that does not already carry one. The body of a
``send_file`` response is not read (it streams), so such a document gets the
policy without hashes and must carry no inline script, which the frontend
builds the apps serve that way never do.
"""

import base64
import hashlib
import re
from collections.abc import Mapping
from typing import Final
from urllib.parse import urlsplit

from flask import Blueprint, Flask, Response

from app_manifest.errors import PolicySourceError

CONTENT_SECURITY_POLICY_HEADER: Final[str] = "Content-Security-Policy"

DOCUMENT_POLICY_DIRECTIVES: Final[Mapping[str, str]] = {
    "default-src": "'self'",
    "script-src": "'self'",
    "style-src": "'self' 'unsafe-inline'",
    "img-src": "'self' data: blob:",
    "frame-src": "http: https:",
    "object-src": "'none'",
    "base-uri": "'self'",
    "form-action": "'self'",
}

# An inline script is a ``<script>`` element with a body and no ``src``. Every such body is
# hashed, data blocks (``type="application/json"``) included: a hash of a block the browser
# never runs admits nothing, and telling the two apart is not worth a parser.
_SCRIPT_ELEMENT: Final[re.Pattern[str]] = re.compile(
    r"<script\b([^>]*)>(.*?)</script>", re.IGNORECASE | re.DOTALL
)
# Not preceded by a word character or a hyphen, so ``data-src=`` is not taken for ``src=``.
_SRC_ATTRIBUTE: Final[re.Pattern[str]] = re.compile(r"(?<![\w-])src\s*=", re.IGNORECASE)


def url_origin(url: str) -> str:
    """The policy source admitting one origin: the scheme and host (and explicit port) of ``url``, nothing of its path."""
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        raise PolicySourceError(f"not an absolute http(s) URL: {url!r}")
    return f"{parts.scheme}://{parts.netloc}"


def inline_source(text: str) -> str:
    """The ``'sha256-...'`` source admitting one inline script or stylesheet whose text is exactly ``text``."""
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return f"'sha256-{base64.b64encode(digest).decode('ascii')}'"


def inline_script_hashes(html: str) -> tuple[str, ...]:
    """The ``'sha256-...'`` source admitting each inline script of ``html``, in document order, each once."""
    sources: dict[str, None] = {}
    for attributes, body in _SCRIPT_ELEMENT.findall(html):
        if not body or _SRC_ATTRIBUTE.search(attributes):
            continue
        sources[inline_source(body)] = None
    return tuple(sources)


def build_document_policy(html: str, overrides: Mapping[str, str] | None = None) -> str:
    """The policy for one HTML document: the defaults, ``overrides`` by directive, plus the document's inline scripts."""
    directives = {**DOCUMENT_POLICY_DIRECTIVES, **(overrides or {})}
    hashes = inline_script_hashes(html)
    if hashes:
        directives["script-src"] = " ".join((directives["script-src"], *hashes))
    return "; ".join(f"{name} {value}" for name, value in directives.items())


def attach_document_policy(
    target: Flask | Blueprint, overrides: Mapping[str, str] | None = None
) -> None:
    """Stamp the document policy on every HTML response of ``target`` that does not set one itself.

    ``overrides`` replace whole directives (``{"script-src": "'self' blob:"}``) for an app whose pages
    need more than the defaults; the inline-script hashes are still appended to ``script-src``.
    """

    def _stamp(response: Response) -> Response:
        if (
            response.mimetype != "text/html"
            or CONTENT_SECURITY_POLICY_HEADER in response.headers
        ):
            return response
        # A file or generator body is not read here: it is left to stream, and gets the hash-free policy.
        html = (
            ""
            if response.direct_passthrough or response.is_streamed
            else response.get_data(as_text=True)
        )
        response.headers[CONTENT_SECURITY_POLICY_HEADER] = build_document_policy(
            html, overrides
        )
        return response

    target.after_request(_stamp)
