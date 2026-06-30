"""Serve agent-authored images referenced by their absolute on-disk path.

An agent (Claude Code) running in this container can write image files and read
them back, but the browser rendering the chat cannot reach the container's
filesystem. Markdown like ``![chart](/mngr/code/runtime/chat-images/chart.png)``
makes the browser issue an HTTP GET for that path; the system interface runs in
the same container as the agent, so it answers the GET by streaming the file's
bytes. The absolute on-disk path therefore doubles as the URL -- no rewriting,
no dedicated directory, no separate server.

This hangs off the single-page-app catch-all (see ``server._index_catch_all``):
a request whose path carries an image extension is unambiguously an image
request, never a client-side route, so it is served (or 404s) here; anything
else falls through to the app shell untouched.
"""

from pathlib import Path

from flask import Response
from flask import send_file

# Long-lived caching: agents are instructed (see the root CLAUDE.md) to give
# each image a unique filename, so a served URL never changes content. A
# one-year max-age plus ``immutable`` lets the browser skip revalidation
# entirely while a conversation is re-rendered.
_IMAGE_CACHE_MAX_AGE_SECONDS = 31_536_000

# Image extensions served inline, each mapped to an explicit Content-Type so the
# wire result does not depend on the host's mimetypes registry (macOS and Linux
# disagree on, e.g., webp). Suffixes are matched case-insensitively.
_IMAGE_EXTENSION_TO_MIME_TYPE = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
}

_SVG_EXTENSION = ".svg"

# An SVG loaded via a chat ``<img>`` never executes its scripts, but a user who
# opens the image URL directly in a tab would render it as a document. Lock that
# path down: no scripts, objects, or external loads; inline styles only (common
# in legitimate SVGs). Paired with nosniff so the declared type is honored.
_SVG_CONTENT_SECURITY_POLICY = "default-src 'none'; style-src 'unsafe-inline'"


def image_mime_type_for_path(url_path: str) -> str | None:
    """Return the image Content-Type for ``url_path``, or None if it is not an image path."""
    suffix = Path(url_path).suffix.lower()
    return _IMAGE_EXTENSION_TO_MIME_TYPE.get(suffix)


def try_serve_image(url_path: str) -> Response | None:
    """Serve the on-disk image addressed by a chat markdown image URL.

    ``url_path`` is the catch-all's path component (the request path with its
    leading slash stripped and percent-escapes already decoded). The leading
    slash is restored to recover the absolute on-disk path the agent emitted.

    A path carrying an image extension is an image request, never a client-side
    route: an existing file is streamed back, and a missing one yields a 404 so
    a typo'd path renders a broken image rather than the app shell. A path with
    no image extension yields ``None`` instead, so the caller falls through to
    the single-page-app catch-all and client-side routing is unaffected.
    """
    mime_type = image_mime_type_for_path(url_path)
    if mime_type is None:
        return None

    file_path = Path("/" + url_path)
    if not file_path.is_file():
        return Response(status=404)

    response = send_file(file_path, mimetype=mime_type)
    # send_file's default cache policy is conservative; override it so the
    # browser caches aggressively (filenames are unique per image by
    # convention). ``immutable`` suppresses revalidation entirely.
    response.headers["Cache-Control"] = f"public, max-age={_IMAGE_CACHE_MAX_AGE_SECONDS}, immutable"
    if file_path.suffix.lower() == _SVG_EXTENSION:
        response.headers["Content-Security-Policy"] = _SVG_CONTENT_SECURITY_POLICY
        response.headers["X-Content-Type-Options"] = "nosniff"
    return response
