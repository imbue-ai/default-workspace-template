"""Where the rendered markdown preview lives on disk.

The renderer writes these files and the server reads them, so the two must
agree. They live here rather than in either script because the dependency would
otherwise run the wrong way: the server needs only three strings, and importing
them from `render_markdown_preview` dragged in markdown-it -- a rendering
library the server never calls -- purely to learn a path.

Standard library only, so the server stays importable with nothing but Python.
"""

from pathlib import Path

# Anchored to the repo this file lives in, NOT to the caller's cwd. The renderer
# is run from wherever the work is -- the publish flow renders an assembled
# README from the worker's worktree -- while the server always reads the
# workspace's copy. A relative default silently wrote the page next to the
# caller instead, so a re-render appeared to do nothing: the tab kept serving
# whatever the workspace's state dir happened to hold.
_REPO_ROOT = Path(__file__).resolve().parents[2]

PREVIEW_STATE_DIR = _REPO_ROOT / "data" / ".state" / "markdown-preview"
RENDERED_PAGE_NAME = "index.html"
SOURCE_RECORD_NAME = "source.json"

# The supervisord program that serves the rendered page. Not autostarted: a
# registered service is a tab in the user's workspace, so it comes up only when
# there is something to show. See render_markdown_preview.py.
SERVICE_NAME = "markdown-preview"
DEFAULT_PORT = 1897
