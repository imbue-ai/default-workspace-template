#!/usr/bin/env python3
"""Tiny static server that wraps a registered service in a labeled "preview" frame.

A pre-merge preview is just another registered service served raw at its own
browser origin, so by default it renders as a bare tab -- visually
indistinguishable from the live interface. This server wraps that inner
service in a chrome page that frames it on all four sides: an accent border +
a header label marking the tab as a *preview of a proposed change*, with the
inner service held in a bordered "stage" iframe inside that frame.

It is deliberately service-agnostic. Given any inner service name already
registered via ``forward_port.py``, plus a human-readable title, it serves the
wrapper page -- so the same mechanism can wrap a preview of any service, not
just the system interface itself.

The wrapper itself is registered as its own service, so it is served at its
own origin too. Every service origin prefixes the service name as one hostname
label on the workspace host (``<name>.<workspace-host>``), and the workspace
host cannot be known server-side -- so the inner iframe's ``src`` is derived
in the page's JavaScript from ``location.host`` by swapping the wrapper's own
leading label for the inner service's name.

Run via bare ``python3`` (standard library only, no venv needed):

    python3 preview_wrapper_server.py --port 8200 --inner-service si-preview-app \\
        --title "my-change"
"""

import argparse
import html
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def build_wrapper_html(inner_service: str, title: str) -> str:
    """Build the wrapper chrome page embedding ``inner_service`` in an iframe.

    ``title`` is shown in the banner (HTML-escaped). The inner iframe ``src`` is
    assigned from JavaScript because the inner service's origin is a sibling of
    the wrapper's own and can only be derived from ``location.host`` at render
    time (see the module docstring).
    """
    safe_title = html.escape(title)
    # json.dumps yields a safe, quoted JS string literal for the service name.
    service_literal = json.dumps(inner_service)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Preview: {safe_title}</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; height: 100%; }}
  /* The amber accent surrounds the previewed app on all four sides (a frame),
     with the label as a header inside it -- so the whole tab reads as a
     contained preview, not just a page with a header strip. */
  body {{
    display: flex; flex-direction: column; gap: 8px;
    height: 100%; padding: 10px;
    background: #2c2200;
    border: 2px solid #f5b301;
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  }}
  .preview-banner {{
    flex: 0 0 auto;
    display: flex; align-items: center; gap: 12px;
    padding: 2px 4px;
    color: #ffd76a;
    font-size: 13px; line-height: 1.3;
  }}
  .preview-banner .tag {{
    font-weight: 700; text-transform: uppercase; letter-spacing: .07em;
    font-size: 11px;
    background: #f5b301; color: #1a1500;
    padding: 2px 8px; border-radius: 3px;
    white-space: nowrap;
  }}
  .preview-banner .title {{ font-weight: 600; color: #ffffff; }}
  .preview-banner .desc {{ color: #d8c489; }}
  .preview-banner .hint {{
    margin-left: auto; color: #cdbb86;
    font-size: 12px; white-space: nowrap;
  }}
  /* The previewed app sits inside a bordered, rounded stage so it reads as an
     object held within the frame rather than the live UI itself. */
  .preview-stage {{
    flex: 1 1 auto; min-height: 0;
    border: 1px solid #f5b301; border-radius: 6px; overflow: hidden;
    background: #0f1115;
  }}
  .preview-frame {{
    display: block; width: 100%; height: 100%; border: none; background: #0f1115;
  }}
</style>
</head>
<body>
  <div class="preview-banner">
    <span class="tag">Preview</span>
    <span class="title">{safe_title}</span>
    <span class="desc">proposed change &mdash; not yet live</span>
    <span class="hint">Approve or request changes in chat</span>
  </div>
  <div class="preview-stage">
    <iframe class="preview-frame" id="preview-frame" title="Preview of {safe_title}"></iframe>
  </div>
  <script>
    // Derive the inner service's sibling origin from this page's own host.
    // Every service origin is <name>.<workspace-host> (the same prefix rule
    // locally and on shares), so the wrapper at <wrapper>.<workspace-host>
    // reaches the inner service by swapping its own leading label.
    // The origin scheme is owned by deriveServiceOrigin in system_interface's
    // frontend/src/origin.ts; if it changes, this sibling-swap must change too.
    var previewService = {service_literal};
    var host = location.host;
    var innerHost = previewService + host.slice(host.indexOf("."));
    var previewTarget = location.protocol + "//" + innerHost + "/";
    document.getElementById("preview-frame").setAttribute("src", previewTarget);
  </script>
</body>
</html>"""


def _make_handler(page_html: str) -> type[BaseHTTPRequestHandler]:
    encoded = page_html.encode("utf-8")

    class _WrapperHandler(BaseHTTPRequestHandler):
        # ``do_GET`` is the method name ``http.server`` dispatches to; only the
        # wrapper root is ever fetched here, since the inner iframe is loaded by
        # the browser directly from the inner service's own origin and never
        # reaches this server.
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            # Silence the default stderr access log (signature matches the base
            # method); the detached server's stderr is captured to a log file and
            # per-request noise is not useful.
            pass

    return _WrapperHandler


def serve(port: int, inner_service: str, title: str) -> None:
    page_html = build_wrapper_html(inner_service=inner_service, title=title)
    server = ThreadingHTTPServer(("127.0.0.1", port), _make_handler(page_html))
    server.serve_forever()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--port", type=int, required=True, help="Port to listen on (127.0.0.1)."
    )
    parser.add_argument(
        "--inner-service",
        required=True,
        help="Name of the already-registered service to embed (reached at its "
        "own sibling origin, derived in the page from location.host).",
    )
    parser.add_argument(
        "--title",
        required=True,
        help="Human-readable label shown in the preview banner.",
    )
    args = parser.parse_args(argv)
    serve(port=args.port, inner_service=args.inner_service, title=args.title)
    return 0


if __name__ == "__main__":
    sys.exit(main())
