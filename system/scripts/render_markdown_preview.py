#!/usr/bin/env python3
"""Render a markdown file to HTML for the `markdown-preview` service.

Handing a user raw markdown in chat and asking them to picture the rendered
page is a bad trade when the page is the deliverable -- an inspiration's README
is the thing that decides whether anyone boots it. This renders the file the
way GitHub will, into a tab the user can actually look at.

What it deliberately gets right, because these are what a README preview is
FOR and a plain text dump loses all of them:

- **raw HTML passes through** (`html=True`), so the centered `<p align="center">`
  hero and the "Open in Minds" badge render as the block they are;
- **tables render** (the `js-default` preset), which CommonMark alone does not;
- **local images resolve**, because the server serves the markdown file's own
  directory alongside the page -- a relative `inspiration.svg` is exactly what
  a README references, and a broken one is exactly what a preview should catch.

Remote images (a shields.io badge) load in the browser as normal.

Writes into ``data/.state/markdown-preview/``: ``index.html`` plus a
``source.json`` naming the markdown file, which the server reads to know which
directory to serve assets from.

**Rendering is what brings the tab into existence.** The preview service is not
autostarted -- a registered service is a panel in the user's workspace, and a
previewer that is idle most of the time has no business sitting in front of
them empty. This script starts it once there is something to show, and
``--close`` stops it again (the server withdraws its port on the way out, which
is what removes the tab).

Usage:
    uv run python system/scripts/render_markdown_preview.py <path-to-markdown>
    uv run python system/scripts/render_markdown_preview.py --close
"""

import argparse
import html
import json
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from markdown_it import MarkdownIt

# The state directory the sibling markdown_preview_server.py serves from.
PREVIEW_STATE_DIR = Path("data/.state/markdown-preview")
RENDERED_PAGE_NAME = "index.html"
SOURCE_RECORD_NAME = "source.json"

# The supervisord program that serves the rendered page. Started on demand from
# here rather than at boot; see the module docstring.
SERVICE_NAME = "markdown-preview"
_SUPERVISORCTL_TIMEOUT_SECONDS = 30.0

# `js-default` rather than `commonmark`: it enables tables (GitHub renders them,
# CommonMark does not) without pulling in mdit_py_plugins or linkify-it-py,
# neither of which is in the workspace environment. `gfm-like` requires linkify
# and raises at construction time without it.
_MARKDOWN_PRESET = "js-default"

_PAGE_STYLE = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans",
      Helvetica, Arial, sans-serif;
  font-size: 16px;
  line-height: 1.5;
  color: #1f2328;
  background: #ffffff;
}
.pathbar {
  position: sticky; top: 0; z-index: 2;
  display: flex; align-items: center; gap: 12px;
  padding: 10px 16px;
  background: #f6f8fa; border-bottom: 1px solid #d1d9e0;
  font-size: 12px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
.pathbar code { flex: 1; overflow-wrap: anywhere; }
.pathbar button {
  font: inherit; padding: 4px 10px; cursor: pointer;
  border: 1px solid #d1d9e0; border-radius: 6px; background: #ffffff;
}
.pathbar button:hover { background: #eef1f4; }
.readme { max-width: 1012px; margin: 0 auto; padding: 32px 32px 96px; }
.readme img { max-width: 100%; }
.readme h1, .readme h2 {
  padding-bottom: .3em; border-bottom: 1px solid #d1d9e0; margin-top: 24px;
}
.readme h1 { font-size: 2em; } .readme h2 { font-size: 1.5em; }
.readme code {
  background: rgba(129,139,152,.12); padding: .2em .4em;
  border-radius: 6px; font-size: 85%;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
.readme pre { background: #f6f8fa; padding: 16px; overflow: auto; border-radius: 6px; }
.readme pre code { background: none; padding: 0; }
.readme table { border-collapse: collapse; display: block; overflow: auto; }
.readme th, .readme td { border: 1px solid #d1d9e0; padding: 6px 13px; }
.readme tr:nth-child(2n) { background: #f6f8fa; }
.readme blockquote {
  margin: 0; padding: 0 1em; color: #59636e; border-left: .25em solid #d1d9e0;
}
.readme a { color: #0969da; }
@media (prefers-color-scheme: dark) {
  body { color: #f0f6fc; background: #0d1117; }
  .pathbar { background: #151b23; border-bottom-color: #3d444d; }
  .pathbar button { background: #212830; border-color: #3d444d; color: inherit; }
  .pathbar button:hover { background: #2a313c; }
  .readme h1, .readme h2, .readme th, .readme td { border-color: #3d444d; }
  .readme pre, .readme tr:nth-child(2n) { background: #151b23; }
  .readme blockquote { color: #9198a1; border-left-color: #3d444d; }
  .readme a { color: #4493f8; }
}
"""

# The path bar is the one piece of chrome: it names the file on disk and makes
# it one click to copy, so the user can open it in their own editor.
_COPY_SCRIPT = """
document.querySelector('#copy-path').addEventListener('click', async (event) => {
  const button = event.currentTarget;
  await navigator.clipboard.writeText(button.dataset.path);
  const original = button.textContent;
  button.textContent = 'Copied';
  setTimeout(() => { button.textContent = original; }, 1200);
});
"""


def render_markdown(markdown_text: str) -> str:
    """The markdown rendered to an HTML fragment, raw HTML preserved."""
    parser = MarkdownIt(_MARKDOWN_PRESET, {"html": True})
    return parser.render(markdown_text)


def build_page(markdown_text: str, source_path: Path) -> str:
    """The full standalone preview page for one markdown file."""
    absolute_source = str(source_path)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(source_path.name)} -- preview</title>
<style>{_PAGE_STYLE}</style>
</head>
<body>
<div class="pathbar">
  <code>{html.escape(absolute_source)}</code>
  <button id="copy-path" data-path="{html.escape(absolute_source, quote=True)}">Copy path</button>
</div>
<article class="readme">
{render_markdown(markdown_text)}
</article>
<script>{_COPY_SCRIPT}</script>
</body>
</html>
"""


def write_preview(source_path: Path, state_dir: Path) -> Path:
    """Render `source_path` into `state_dir`; returns the rendered page path.

    Also records the source, which is what lets the server resolve the relative
    image paths a README actually uses.
    """
    markdown_text = source_path.read_text(encoding="utf-8", errors="replace")
    state_dir.mkdir(parents=True, exist_ok=True)
    page_path = state_dir / RENDERED_PAGE_NAME
    page_path.write_text(build_page(markdown_text, source_path), encoding="utf-8")
    (state_dir / SOURCE_RECORD_NAME).write_text(
        json.dumps(
            {"source_path": str(source_path), "asset_dir": str(source_path.parent)},
            indent=2,
        ),
        encoding="utf-8",
    )
    return page_path


def _run_supervisorctl(action: str) -> tuple[bool, str]:
    """Run one supervisorctl action against the preview service.

    Returns (ok, message). Never raises: outside a workspace container there is
    no supervisord, and the render itself is still useful there -- the caller
    just gets told the tab could not be opened.
    """
    if shutil.which("supervisorctl") is None:
        return False, "supervisorctl not found (not running inside a workspace)"
    completed = subprocess.run(
        ["supervisorctl", action, SERVICE_NAME],
        capture_output=True,
        text=True,
        check=False,
        timeout=_SUPERVISORCTL_TIMEOUT_SECONDS,
    )
    output = (completed.stdout + completed.stderr).strip()
    # `start` on an already-running program and `stop` on an already-stopped one
    # both exit non-zero, and both mean the requested state already holds.
    if "already started" in output or "not running" in output:
        return True, output
    return completed.returncode == 0, output


def main(
    argv: list[str] | None = None,
    run_supervisorctl: Callable[[str], tuple[bool, str]] = _run_supervisorctl,
) -> int:
    """Render a markdown file (and raise its tab), or close the preview.

    `run_supervisorctl` is injected so the tests can drive the lifecycle
    without a supervisord.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "markdown_path",
        nargs="?",
        help="The markdown file to render",
    )
    parser.add_argument(
        "--close",
        action="store_true",
        help="Stop the preview service and remove its tab; renders nothing",
    )
    parser.add_argument(
        "--state-dir",
        default=str(PREVIEW_STATE_DIR),
        help=f"Where to write the rendered page (default: {PREVIEW_STATE_DIR})",
    )
    args = parser.parse_args(argv)

    if args.close:
        ok, message = run_supervisorctl("stop")
        print(
            f"render_markdown_preview: preview closed{f' ({message})' if message else ''}"
            if ok
            else f"render_markdown_preview: could not close the preview: {message}"
        )
        return 0 if ok else 1

    if args.markdown_path is None:
        parser.error("a markdown file is required (or pass --close)")

    source_path = Path(args.markdown_path).resolve()
    if not source_path.is_file():
        print(
            f"render_markdown_preview: no such file: {source_path}",
            file=sys.stderr,
        )
        return 1

    page_path = write_preview(source_path, Path(args.state_dir))
    print(f"render_markdown_preview: rendered {source_path} -> {page_path}")

    started, message = run_supervisorctl("start")
    if started:
        print(
            "render_markdown_preview: preview service is up. Open it with:\n"
            "  python3 system/scripts/layout.py open service:markdown-preview --layout <layout>\n"
            "Close it when you are done (this removes the tab):\n"
            "  uv run python system/scripts/render_markdown_preview.py --close"
        )
    else:
        print(
            f"render_markdown_preview: rendered, but could not start the preview service: {message}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
