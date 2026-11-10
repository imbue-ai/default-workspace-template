"""Take a screenshot of one page with the workspace's browser: ``python -I screenshot_page.py <url> <png>``.

The page is shot inside a frame on a page of another site, the way the workspace shows every app. That page is served
here, on 127.0.0.1 at a port of its own -- another site to the browser, on an ordinary http address. (A blank page will
not do: a browser treats it as no site at all and refuses even an app that allows any site to frame it. Nor will a
made-up public address: a browser refuses to let a public page frame a local one.) When the browser refuses to show
the app in the frame, the script writes why to ``<png>.blocked`` beside the picture, so the store treats the app's
window as blank.

Run by the store with the workspace's own Python environment (where Playwright is installed), not the store's, so the
store needs no browser of its own. Exits non-zero, with the reason on stderr, when the page cannot be shot.
"""

import html
import sys
import threading
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import ViewportSize
from playwright.sync_api import sync_playwright

BROWSER_PATH = "/opt/fortress/tilion-fortress/tilion"
VIEWPORT: ViewportSize = {"width": 1100, "height": 720}
# Long enough for a page that draws itself with JavaScript after it loads.
SETTLE_MILLISECONDS = 2500
BLOCKED_SUFFIX = ".blocked"
ERROR_FRAME_PREFIX = "chrome-error://"


def framing_server(document: str) -> ThreadingHTTPServer:
    """A server on a free local port answering every request with ``document``, running in a thread."""
    body = document.encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main(url: str, target: str) -> None:
    console: list[str] = []
    server = framing_server(
        f'<body style="margin:0"><iframe src="{html.escape(url)}" '
        'style="border:0;width:100vw;height:100vh;display:block"></iframe></body>'
    )
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=BROWSER_PATH, args=["--no-sandbox"])
            page = browser.new_page(viewport=VIEWPORT)
            page.on("console", lambda message: console.append(message.text))
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/", wait_until="load", timeout=30000)
            page.wait_for_timeout(SETTLE_MILLISECONDS)
            page.screenshot(path=target)
            is_blocked = any(frame.url.startswith(ERROR_FRAME_PREFIX) for frame in page.frames)
            browser.close()
    except PlaywrightError as e:
        # SystemExit with a message prints it to stderr and exits 1.
        raise SystemExit(f"could not take the screenshot: {e}") from e
    finally:
        server.shutdown()
        server.server_close()
    if is_blocked:
        reason = "\n".join(console) or "the browser would not show the page in a frame"
        Path(target + BLOCKED_SUFFIX).write_text(reason, encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
