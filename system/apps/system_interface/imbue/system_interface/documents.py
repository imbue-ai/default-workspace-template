"""The per-request assembly of the shell document: the vite build's HTML with meta tags injected per request."""

import html
import re
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


def _meta_tag(name: str, content: str) -> str:
    return f'<meta name="{name}" content="{html.escape(content, quote=True)}">'


def inject_meta_tag(html_content: str, name: str, content: str) -> str:
    return html_content.replace("</head>", f"{_meta_tag(name, content)}\n</head>")


def inject_base_path_meta_tag(html_content: str, root_path: str) -> str:
    return inject_meta_tag(html_content, BASE_PATH_META_NAME, root_path)


# The shell's page background (``--c-bg`` in ``system/libs/workspace_ui/src/base.css``).
SHELL_BACKGROUND_COLOR: Final[str] = "#fafaf8"

VIEWPORT_META_NAME: Final[str] = "viewport"
VIEWPORT_FIT_COVER: Final[str] = "viewport-fit=cover"
_DEFAULT_VIEWPORT: Final[str] = "width=device-width, initial-scale=1.0"

_TITLE_PATTERN: Final[re.Pattern[str]] = re.compile(r"<title>.*?</title>", re.IGNORECASE | re.DOTALL)
_CONTENT_ATTRIBUTE_PATTERN: Final[re.Pattern[str]] = re.compile(r'\scontent="([^"]*)"', re.IGNORECASE)


def _tag_pattern(tag: str, attribute: str, value: str) -> re.Pattern[str]:
    """A ``<tag ...>`` whose ``attribute`` is exactly ``value``, wherever the attribute sits in the tag."""
    return re.compile(rf'<{tag}\s(?:[^>]*\s)?{attribute}="{re.escape(value)}"[^>]*>', re.IGNORECASE)


def _replaced_or_added(html_content: str, pattern: re.Pattern[str], tag: str) -> str:
    """The document with the first match of ``pattern`` replaced by ``tag``, or ``tag`` added to the head."""
    if pattern.search(html_content) is not None:
        return pattern.sub(lambda _match: tag, html_content, count=1)
    return html_content.replace("</head>", f"{tag}\n</head>", 1)


def set_document_title(html_content: str, title: str) -> str:
    """The document titled ``title`` (escaped): its ``<title>`` replaced, or one added to the head."""
    return _replaced_or_added(html_content, _TITLE_PATTERN, f"<title>{html.escape(title, quote=False)}</title>")


def set_meta_tag(html_content: str, name: str, content: str) -> str:
    """The document with the ``name`` meta tag carrying ``content`` (escaped): an existing tag of that name is
    replaced, so the build may already carry it."""
    return _replaced_or_added(html_content, _tag_pattern("meta", "name", name), _meta_tag(name, content))


def set_link_tag(html_content: str, rel: str, href: str, *, is_credentialed: bool) -> str:
    """The document with one ``<link rel=...>`` pointing at ``href`` (escaped), replacing one the build carries.
    A credentialed link is fetched with the page's cookies (``crossorigin="use-credentials"``)."""
    credentials = ' crossorigin="use-credentials"' if is_credentialed else ""
    tag = f'<link rel="{rel}" href="{html.escape(href, quote=True)}"{credentials}>'
    return _replaced_or_added(html_content, _tag_pattern("link", "rel", rel), tag)


def with_viewport_fit_cover(html_content: str) -> str:
    """The document whose viewport lets the page draw under a phone's notch and home indicator: the build's
    viewport with ``viewport-fit=cover`` added when it lacks it, or a viewport of its own when it has none."""
    match = _tag_pattern("meta", "name", VIEWPORT_META_NAME).search(html_content)
    content_match = _CONTENT_ATTRIBUTE_PATTERN.search(match.group(0)) if match is not None else None
    if content_match is None:
        return set_meta_tag(html_content, VIEWPORT_META_NAME, f"{_DEFAULT_VIEWPORT}, {VIEWPORT_FIT_COVER}")
    content = html.unescape(content_match.group(1))
    if VIEWPORT_FIT_COVER in content.replace(" ", ""):
        return html_content
    return set_meta_tag(html_content, VIEWPORT_META_NAME, f"{content}, {VIEWPORT_FIT_COVER}")


APPLE_WEB_APP_TITLE_META_NAME: Final[str] = "apple-mobile-web-app-title"
# Lets the home-screen app draw under the status bar (the phone home grid's wallpaper reaches the top edge).
APPLE_WEB_APP_STATUS_BAR_STYLE_META_NAME: Final[str] = "apple-mobile-web-app-status-bar-style"
APPLE_WEB_APP_STATUS_BAR_STYLE: Final[str] = "black-translucent"
THEME_COLOR_META_NAME: Final[str] = "theme-color"
TOUCH_ICON_PATH: Final[str] = "/apple-touch-icon.png"
MANIFEST_PATH: Final[str] = "/manifest.webmanifest"


def inject_install_tags(html_content: str, workspace_name: str, root_path: str) -> str:
    """What a phone saving the page to its home screen reads, which only the server knows: the workspace's name as
    the title and the tile's label, the page colour, the touch icon and the manifest under the shell's root path, and
    a viewport that reaches under the notch. Each tag the build already carries is replaced rather than repeated."""
    html_content = set_document_title(html_content, workspace_name)
    html_content = set_meta_tag(html_content, APPLE_WEB_APP_TITLE_META_NAME, workspace_name)
    html_content = set_meta_tag(html_content, APPLE_WEB_APP_STATUS_BAR_STYLE_META_NAME, APPLE_WEB_APP_STATUS_BAR_STYLE)
    html_content = set_meta_tag(html_content, THEME_COLOR_META_NAME, SHELL_BACKGROUND_COLOR)
    html_content = with_viewport_fit_cover(html_content)
    html_content = set_link_tag(
        html_content, "apple-touch-icon", f"{root_path}{TOUCH_ICON_PATH}", is_credentialed=False
    )
    # A browser fetches a manifest without cookies, even from the page's own origin, unless the link asks for them;
    # the workspace's forwarder refuses a request that carries no session.
    return set_link_tag(html_content, "manifest", f"{root_path}{MANIFEST_PATH}", is_credentialed=True)
