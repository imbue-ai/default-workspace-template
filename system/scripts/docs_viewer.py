"""Tiny read-only Markdown viewer for the system-interface docs.

Serves the .md files under ``system/apps/system_interface/docs`` rendered to HTML,
with a sidebar for navigation. Rendering is on the fly, so edits to a spec show up
on refresh. Run it with the ``markdown`` package available, e.g.::

    uv run --with markdown python system/scripts/docs_viewer.py --port 8791

Then register + surface it as a workspace tab::

    python3 system/scripts/forward_port.py --name queuing-specs --url http://localhost:8791 --no-icon
    python3 system/scripts/layout.py open queuing-specs
"""

import argparse
import html
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs
from urllib.parse import urlparse

import markdown

DOCS_DIR = Path(__file__).resolve().parents[1] / "apps" / "system_interface" / "docs"

# Specs most relevant to the queuing work float to the top of the sidebar, in
# reading order (the point + Claude model, then the Claude build, then codex).
_PRIORITY = [
    "codex_queuing_signals_spec.md",
    "claude_queued_messages_impl.md",
    "shoulder_tap_spec.md",
]

_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>{title}</title>
<style>
  :root {{ color-scheme: light dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; font: 15px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
         color: #1f2328; background: #fff; }}
  @media (prefers-color-scheme: dark) {{ body {{ color: #e6edf3; background: #0d1117; }} }}
  .layout {{ display: flex; min-height: 100vh; }}
  nav {{ width: 300px; flex: 0 0 300px; border-right: 1px solid #d0d7de; padding: 18px 14px; overflow-y: auto;
         position: sticky; top: 0; height: 100vh; }}
  @media (prefers-color-scheme: dark) {{ nav {{ border-color: #30363d; }} }}
  nav h2 {{ font-size: 12px; text-transform: uppercase; letter-spacing: .06em; color: #656d76; margin: 0 0 10px; }}
  nav a {{ display: block; padding: 6px 10px; border-radius: 6px; color: inherit; text-decoration: none;
           font-size: 13px; margin-bottom: 2px; }}
  nav a:hover {{ background: rgba(127,127,127,.12); }}
  nav a.active {{ background: rgba(127,127,127,.18); font-weight: 600; }}
  main {{ flex: 1; padding: 32px 40px; max-width: 900px; overflow-x: auto; }}
  main h1, main h2 {{ border-bottom: 1px solid #d0d7de; padding-bottom: .3em; }}
  @media (prefers-color-scheme: dark) {{ main h1, main h2 {{ border-color: #30363d; }} }}
  code {{ font: 13px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace;
          background: rgba(127,127,127,.15); padding: .15em .35em; border-radius: 5px; }}
  pre {{ background: rgba(127,127,127,.12); padding: 14px; border-radius: 8px; overflow-x: auto; }}
  pre code {{ background: none; padding: 0; }}
  table {{ border-collapse: collapse; }}
  th, td {{ border: 1px solid #d0d7de; padding: 6px 12px; }}
  @media (prefers-color-scheme: dark) {{ th, td {{ border-color: #30363d; }} }}
  blockquote {{ margin: 0; padding: 0 1em; color: #656d76; border-left: 3px solid #d0d7de; }}
</style></head>
<body><div class="layout"><nav><h2>Specs</h2>{nav}</nav><main>{body}</main></div></body></html>"""


def _doc_files() -> list[Path]:
    found = sorted(p for p in DOCS_DIR.glob("*.md"))
    ordered = [DOCS_DIR / n for n in _PRIORITY if (DOCS_DIR / n).exists()]
    ordered += [p for p in found if p.name not in _PRIORITY]
    return ordered


def _render(active: str) -> str:
    files = _doc_files()
    if not any(p.name == active for p in files):
        active = files[0].name if files else ""
    nav = "".join(
        f'<a class="{"active" if p.name == active else ""}" href="/?doc={p.name}">{html.escape(p.name)}</a>'
        for p in files
    )
    target = DOCS_DIR / active
    if target.exists():
        body = markdown.markdown(
            target.read_text(encoding="utf-8"),
            extensions=["fenced_code", "tables", "toc", "sane_lists"],
        )
        title = active
    else:
        body = "<p>No docs found.</p>"
        title = "Docs"
    return _PAGE.format(title=html.escape(title), nav=nav, body=body)


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path not in ("/", "/index.html"):
            self.send_response(404)
            self.end_headers()
            return
        doc = parse_qs(parsed.query).get("doc", [""])[0]
        # Guard against path traversal: only a bare filename is honored.
        doc = Path(doc).name
        page = _render(doc).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)

    def log_message(self, *_args: object) -> None:  # keep stdout quiet
        pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8791)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), _Handler)
    print(f"docs-viewer serving {DOCS_DIR} on http://{args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
