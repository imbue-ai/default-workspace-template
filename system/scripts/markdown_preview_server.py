#!/usr/bin/env python3
"""Serve the rendered markdown preview as the `markdown-preview` service.

Pairs with render_markdown_preview.py: that writes the page, this puts it in a
tab. Two routes and nothing else --

- ``/`` serves the rendered page from ``data/.state/markdown-preview/``;
- anything else is served from the SOURCE markdown's own directory, which is
  what makes a relative ``inspiration.svg`` resolve. Serving the state dir
  alone would show every local image broken, which is precisely the failure a
  preview exists to surface *before* the push -- so a preview that could not
  reproduce it would be worse than none.

Registers its port through forward_port.py like every other service, so it is
reachable at the workspace's per-service origin and can be opened with
``layout.py open service:markdown-preview``.

Standard library only (plus the renderer's markdown-it), so it starts instantly
and cannot break boot on a dependency.
"""

import argparse
import json
import subprocess
import sys
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from render_markdown_preview import (
    PREVIEW_STATE_DIR,
    RENDERED_PAGE_NAME,
    SOURCE_RECORD_NAME,
)

DEFAULT_PORT = 1897
SERVICE_NAME = "markdown-preview"

_EMPTY_PAGE = b"""<!doctype html>
<meta charset="utf-8">
<title>markdown preview</title>
<body style="font-family: system-ui, sans-serif; padding: 48px; line-height: 1.5">
<h1>Nothing rendered yet</h1>
<p>Render a markdown file into this tab with:</p>
<pre>uv run python system/scripts/render_markdown_preview.py &lt;path-to-markdown&gt;</pre>
</body>
"""


def read_asset_dir(state_dir: Path) -> Path | None:
    """The directory the previewed markdown lives in, if anything is rendered.

    Returns None rather than raising when nothing has been rendered yet or the
    record is unreadable: an empty preview is a normal state (the service
    starts at boot, long before anyone renders anything), and a serving loop
    must not die on it.
    """
    record_path = state_dir / SOURCE_RECORD_NAME
    if not record_path.is_file():
        return None
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    asset_dir = record.get("asset_dir")
    if not isinstance(asset_dir, str):
        return None
    candidate = Path(asset_dir)
    return candidate if candidate.is_dir() else None


class MarkdownPreviewHandler(SimpleHTTPRequestHandler):
    """Serves the rendered page at / and its sibling assets from the source dir."""

    state_dir: Path = PREVIEW_STATE_DIR

    def do_GET(self) -> None:
        request_path = unquote(urlparse(self.path).path)
        if request_path in ("/", f"/{RENDERED_PAGE_NAME}"):
            self._serve_rendered_page()
            return
        self._serve_asset(request_path)

    def _serve_rendered_page(self) -> None:
        page_path = self.state_dir / RENDERED_PAGE_NAME
        if not page_path.is_file():
            self._send_bytes(_EMPTY_PAGE, "text/html; charset=utf-8")
            return
        self._send_bytes(page_path.read_bytes(), "text/html; charset=utf-8")

    def _serve_asset(self, request_path: str) -> None:
        asset_dir = read_asset_dir(self.state_dir)
        if asset_dir is None:
            self.send_error(HTTPStatus.NOT_FOUND, "nothing rendered yet")
            return
        # Resolve and confine: the served directory is whatever markdown was
        # last previewed, so a traversal must not be able to walk out of it.
        target = (asset_dir / request_path.lstrip("/")).resolve()
        resolved_root = asset_dir.resolve()
        if resolved_root != target and resolved_root not in target.parents:
            self.send_error(HTTPStatus.FORBIDDEN, "outside the previewed directory")
            return
        if not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "no such asset")
            return
        guessed_type = self.guess_type(str(target))
        self._send_bytes(target.read_bytes(), guessed_type)

    def _send_bytes(self, payload: bytes, content_type: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        # A preview is re-rendered in place and refreshed; a cached copy would
        # show the user the version they already fixed.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        # supervisord captures stderr; per-request noise is not worth a log file.
        pass


def register_service(port: int) -> None:
    """Declare the port in apps.toml so the service gets its own origin."""
    subprocess.run(
        [
            sys.executable,
            "system/scripts/forward_port.py",
            "--name",
            SERVICE_NAME,
            "--url",
            f"http://localhost:{port}",
        ],
        check=False,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--state-dir", default=str(PREVIEW_STATE_DIR))
    parser.add_argument(
        "--skip-registration",
        action="store_true",
        help="Do not touch apps.toml (used by the tests)",
    )
    args = parser.parse_args(argv)

    MarkdownPreviewHandler.state_dir = Path(args.state_dir)
    if not args.skip_registration:
        register_service(args.port)

    server = ThreadingHTTPServer(("127.0.0.1", args.port), MarkdownPreviewHandler)
    print(f"markdown_preview_server: serving on http://localhost:{args.port}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
