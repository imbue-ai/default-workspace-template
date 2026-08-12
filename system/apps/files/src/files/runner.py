"""Visual macOS-style file browser for viewing and downloading workspace files.

Read-only. Two browse roots:

- ``files``  -- the user's workspace ``data/`` directory (personal files).
- ``code``   -- the workspace git repo (advanced; code viewing + git diffs).

Security is centralized in :func:`resolve_within_root` -- every route that
touches the filesystem goes through it, so a single chokepoint enforces the
sandbox (no ``..`` escape, no symlink escape, no hidden/secret segments).

Conventions (this service runs from the repo root, ``/home/user/workspace``):

- Static assets shipped alongside this file are read via
  ``Path(__file__).parent / "assets/..."``.
- The listen port binds ``PORT`` (overridable via ``FILES_PORT``) and the
  browse root resolves from ``REPO_ROOT`` (overridable via
  ``FILES_REPO_ROOT``), so an editing agent can boot a throwaway instance.

This is a synchronous Flask app served by the threaded Werkzeug server.
"""

import io
import json
import mimetypes
import os
import subprocess
import time
import zipfile
from pathlib import Path
from typing import Any, NamedTuple

import markdown as markdown_lib
import nh3
from flask import Flask, Response, request, send_file
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import TextLexer, get_lexer_for_filename, guess_lexer
from pygments.util import ClassNotFound
from werkzeug.serving import run_simple

PORT = int(os.environ.get("FILES_PORT", "8080"))

# The repo root the service runs from. ``code`` browses this; ``files``
# browses its ``data/`` subdirectory. Overridable for tests.
REPO_ROOT = Path(os.environ.get("FILES_REPO_ROOT", ".")).resolve()
DATA_ROOT = (REPO_ROOT / "data").resolve()

# Preview caps: content above these sizes is download-only, so the browser
# never chokes on a huge file or diff.
MAX_TEXT_PREVIEW_BYTES = 2 * 1024 * 1024
MAX_DIFF_BYTES = 4 * 1024 * 1024

# Search bounds: cap results and total files scanned so a name search over a
# large tree (e.g. the vendored code root) can't hang the request.
MAX_SEARCH_RESULTS = 500
MAX_SEARCH_SCAN = 60000

# Served raw files (any type, including user-authored HTML/SVG) get a
# script-free CSP so a file opened directly in the browser can never execute
# JavaScript in this app's origin. Images/PDF/audio/video still render.
_RAW_CSP = "default-src 'none'; img-src 'self' data:; media-src 'self'; style-src 'unsafe-inline'; frame-ancestors 'self'"


class BrowseRoot(NamedTuple):
    key: str
    label: str
    path: Path
    git: bool
    advanced: bool


ROOTS: dict[str, BrowseRoot] = {
    "files": BrowseRoot("files", "My Files", DATA_ROOT, git=False, advanced=False),
    "code": BrowseRoot("code", "Workspace Code", REPO_ROOT, git=True, advanced=True),
}

# Non-dot machinery and build noise that is never listed or served, at any
# depth, under any root. Dot-prefixed entries (``.git``, ``.secrets``,
# ``.venv``, ...) are already hidden by the dot rule in ``_segment_is_hidden``.
_ALWAYS_DENIED_SEGMENTS = frozenset({"node_modules", "__pycache__"})

# Top-level names hidden per root (beyond the dot rule). ``data`` is hidden
# in ``code`` because the personal files belong to the ``files`` root.
_ROOT_TOP_LEVEL_DENIED: dict[str, frozenset[str]] = {
    "files": frozenset(),
    "code": frozenset({"data"}),
}


class PathRejected(Exception):
    """Raised when a requested path fails the sandbox check."""


def _segment_is_hidden(name: str) -> bool:
    """True if a single path segment must never be listed or served."""
    if name.startswith("."):  # dot-dirs/files: .git, .secrets, .env, ...
        return True
    if name in _ALWAYS_DENIED_SEGMENTS:
        return True
    lowered = name.lower()
    return lowered.endswith(".env") or lowered.endswith(".env.local")


def resolve_within_root(root: BrowseRoot, rel_path: str) -> Path:
    """Resolve ``rel_path`` against ``root`` and confirm it is safe.

    Rejects (via :class:`PathRejected`): traversal escaping the root,
    symlinks pointing outside it, and any hidden/denied path segment
    (secrets, dot-dirs, build noise). This is the one place every
    filesystem route validates input.
    """
    rel = (rel_path or "").strip().lstrip("/")
    candidate = (root.path / rel).resolve()

    # Must stay inside the root after resolving symlinks/``..``.
    if candidate != root.path and root.path not in candidate.parents:
        raise PathRejected("Path escapes the browse root")

    relative = candidate.relative_to(root.path) if candidate != root.path else Path()
    for i, seg in enumerate(relative.parts):
        if _segment_is_hidden(seg):
            raise PathRejected(f"Hidden or denied path segment: {seg}")
        if i == 0 and seg in _ROOT_TOP_LEVEL_DENIED.get(root.key, frozenset()):
            raise PathRejected(f"Denied top-level entry: {seg}")
    return candidate


def _get_root(key: str) -> BrowseRoot:
    root = ROOTS.get(key)
    if root is None:
        raise PathRejected(f"Unknown root: {key}")
    return root


def _resolve(root_key: str, rel: str) -> tuple[BrowseRoot, Path]:
    """Look up a root and resolve a request-relative path within it."""
    root = _get_root(root_key)
    return root, resolve_within_root(root, rel)


# ---- File classification -------------------------------------------------


def _build_kind_map() -> dict[str, str]:
    """Map file extensions to a coarse "kind" used for icons and preview."""
    groups: list[tuple[tuple[str, ...], str]] = [
        ((".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".bmp", ".ico", ".svg"), "img"),
        ((".pdf",), "pdf"),
        ((".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac"), "audio"),
        ((".mp4", ".webm", ".mov", ".m4v", ".ogv"), "video"),
        ((".md", ".markdown"), "md"),
        ((".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar"), "zip"),
        ((".csv", ".tsv"), "csv"),
        ((".txt", ".log", ".rst", ".ini", ".cfg", ".conf"), "txt"),
        (
            (
                ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".toml", ".yaml", ".yml",
                ".html", ".css", ".scss", ".sh", ".bash", ".c", ".cpp", ".h", ".hpp",
                ".go", ".rs", ".java", ".rb", ".php", ".sql", ".xml", ".lua", ".swift",
                ".kt", ".dart", ".vue", ".svelte", ".r", ".jl", ".pl",
            ),
            "code",
        ),
    ]
    return {ext: kind for exts, kind in groups for ext in exts}


_KIND_BY_EXT = _build_kind_map()


def _kind_for(path: Path) -> str:
    if path.is_dir():
        return "folder"
    return _KIND_BY_EXT.get(path.suffix.lower(), "file")


def _is_text_kind(kind: str) -> bool:
    return kind in ("md", "txt", "code", "csv")


# ---- Git helpers (code root) --------------------------------------------


def _git(args: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=15
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return proc.returncode, proc.stdout if proc.returncode == 0 else proc.stderr


# Short-lived cache for the status map. `git status` scans the whole repo
# (which vendors a large tree), so recomputing it on every directory listing
# makes code browsing sluggish; a few seconds of staleness is fine.
_STATUS_TTL_SECONDS = 5.0
_status_cache_at: float = 0.0
_status_cache_map: dict[str, str] = {}


def _git_status_map() -> dict[str, str]:
    """Return ``{repo-relative-path: XY status code}`` for changed files.

    Cached for ``_STATUS_TTL_SECONDS`` so a burst of listing requests shares
    one ``git status`` scan.
    """
    global _status_cache_at, _status_cache_map
    now = time.monotonic()
    if now - _status_cache_at < _STATUS_TTL_SECONDS:
        return _status_cache_map

    code, out = _git(["status", "--porcelain", "-z"])
    if code != 0:
        return {}
    result: dict[str, str] = {}
    tokens = out.split("\0")
    i = 0
    while i < len(tokens):
        entry = tokens[i]
        if not entry:
            i += 1
            continue
        status, name = entry[:2], entry[3:]
        # Renames and copies encode "orig -> new" across two NUL tokens; the
        # first token is the new path, the second the original -- skip it.
        if status[0] in ("R", "C") and i + 1 < len(tokens):
            i += 1
        result[name] = status
        i += 1
    _status_cache_at, _status_cache_map = now, result
    return result


# ---- Flask app -----------------------------------------------------------

app = Flask("files", static_folder=None)

_INDEX_HTML = (Path(__file__).parent / "assets" / "index.html").read_text(encoding="utf-8")
_LOGO_PNG = (Path(__file__).parent / "assets" / "imbue-logo.png").read_bytes()
# Light theme scoped to ``.highlight``; dark theme scoped to ``body.dark
# .highlight`` so one stylesheet covers both and the dark rules win only when
# the ``dark`` class is on ``<body>``.
_PYGMENTS_CSS = (
    HtmlFormatter(style="default").get_style_defs(".highlight")
    + "\n"
    + HtmlFormatter(style="one-dark").get_style_defs("body.dark .highlight")
)


def _json(payload: Any, status: int = 200) -> Response:
    return Response(json.dumps(payload), status=status, mimetype="application/json")


@app.errorhandler(PathRejected)
def _on_path_rejected(exc: PathRejected) -> Response:
    return _json({"error": str(exc)}, 403)


@app.route("/")
def index() -> Response:
    return Response(_INDEX_HTML, mimetype="text/html")


@app.route("/logo.png")
def logo() -> Response:
    return Response(_LOGO_PNG, mimetype="image/png")


@app.route("/health")
def health() -> Response:
    return _json({"status": "ok"})


@app.route("/favicon.ico")
def favicon() -> Response:
    # The app has no icon asset; answer the browser's automatic request
    # with No Content so it doesn't log a 404.
    return Response(status=204)


@app.route("/api/roots")
def api_roots() -> Response:
    return _json(
        [{"key": r.key, "label": r.label, "git": r.git, "advanced": r.advanced} for r in ROOTS.values()]
    )


@app.route("/api/list")
def api_list() -> Response:
    rel = request.args.get("path", "")
    root, target = _resolve(request.args.get("root", "files"), rel)
    if not target.is_dir():
        return _json({"error": "Not a folder"}, 404)

    status_map = _git_status_map() if root.git else {}
    denied_top = _ROOT_TOP_LEVEL_DENIED.get(root.key, frozenset())

    entries = []
    for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        if _segment_is_hidden(child.name):
            continue
        rel_from_root = child.relative_to(root.path)
        if len(rel_from_root.parts) == 1 and child.name in denied_top:
            continue
        try:
            stat = child.stat()
        except OSError:
            continue
        entry = {
            "name": child.name,
            "path": str(rel_from_root),
            "type": "folder" if child.is_dir() else "file",
            "kind": _kind_for(child),
            "size": stat.st_size if child.is_file() else None,
            "mtime": int(stat.st_mtime),
        }
        if root.git:
            entry["git"] = status_map.get(str(rel_from_root))
        entries.append(entry)

    crumbs = [{"name": root.label, "path": ""}]
    acc = Path()
    for part in Path(rel).parts if rel else []:
        acc = acc / part
        crumbs.append({"name": part, "path": str(acc)})

    return _json({"root": root.key, "path": rel, "crumbs": crumbs, "entries": entries})


@app.route("/api/search")
def api_search() -> Response:
    """Name search under a root, optionally filtered by type.

    ``type`` is ``all``, ``folder``, or a file kind (``img``, ``pdf``,
    ``code``, ``md``, ``txt``, ``csv``, ``audio``, ``video``, ``zip``,
    ``file``). Matching is a case-insensitive substring on the entry name.
    Applies the same exclusions as listing (hidden/denied/symlink) and is
    bounded by result and scan caps so a huge tree can't hang the request.
    """
    root = _get_root(request.args.get("root", "files"))
    query = request.args.get("q", "").strip().lower()
    type_filter = request.args.get("type", "all")
    if not query:
        return _json({"results": [], "truncated": False})

    denied_top = _ROOT_TOP_LEVEL_DENIED.get(root.key, frozenset())
    results: list[dict[str, object]] = []
    scanned = 0
    truncated = False
    for dirpath, dirnames, filenames in os.walk(root.path):
        here = Path(dirpath)
        at_root = here == root.path
        dirnames[:] = [
            d
            for d in dirnames
            if not _segment_is_hidden(d)
            and not (here / d).is_symlink()
            and not (at_root and d in denied_top)
        ]
        if type_filter in ("all", "folder"):
            for d in dirnames:
                if query in d.lower():
                    results.append(
                        {"name": d, "path": str((here / d).relative_to(root.path)), "type": "folder", "kind": "folder"}
                    )
        for f in filenames:
            scanned += 1
            if _segment_is_hidden(f) or (at_root and f in denied_top):
                continue
            if query not in f.lower():
                continue
            abs_path = here / f
            if abs_path.is_symlink():
                continue
            kind = _kind_for(abs_path)
            if type_filter == "folder":
                continue
            if type_filter != "all" and type_filter != kind:
                continue
            try:
                size = abs_path.stat().st_size
            except OSError:
                size = None
            results.append(
                {"name": f, "path": str(abs_path.relative_to(root.path)), "type": "file", "kind": kind, "size": size}
            )
        if len(results) >= MAX_SEARCH_RESULTS or scanned >= MAX_SEARCH_SCAN:
            truncated = True
            break

    results.sort(key=lambda r: (r["type"] != "folder", str(r["name"]).lower()))
    return _json({"results": results[:MAX_SEARCH_RESULTS], "truncated": truncated})


@app.route("/api/content")
def api_content() -> Response:
    """Return rendered text/code/markdown for the preview pane."""
    _, target = _resolve(request.args.get("root", "files"), request.args.get("path", ""))
    if not target.is_file():
        return _json({"error": "Not a file"}, 404)

    kind = _kind_for(target)
    size = target.stat().st_size
    if not _is_text_kind(kind):
        return _json({"kind": kind, "renderable": False})
    if size > MAX_TEXT_PREVIEW_BYTES:
        return _json({"kind": kind, "renderable": False, "reason": "too_large", "size": size})
    try:
        text = target.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return _json({"kind": kind, "renderable": False, "reason": "binary"})

    if kind == "md":
        # Sanitize: python-markdown passes raw HTML through, so a .md file
        # could carry <script>; nh3 strips anything that could execute.
        html = nh3.clean(markdown_lib.markdown(text, extensions=["fenced_code", "tables"]))
        return _json({"kind": "md", "renderable": True, "html": html})

    # code / txt / csv -> syntax-highlighted HTML via pygments.
    formatter = HtmlFormatter(linenos="table", cssclass="highlight", wrapcode=True)
    try:
        lexer = get_lexer_for_filename(target.name, stripall=False)
    except ClassNotFound:
        try:
            lexer = guess_lexer(text)
        except ClassNotFound:
            lexer = TextLexer()
    return _json({"kind": kind, "renderable": True, "html": highlight(text, lexer, formatter)})


@app.route("/api/pygments.css")
def api_pygments_css() -> Response:
    return Response(_PYGMENTS_CSS, mimetype="text/css")


def _serve_file(root_key: str, rel: str, as_attachment: bool) -> Response:
    _, target = _resolve(root_key, rel)
    if not target.is_file():
        return _json({"error": "Not a file"}, 404)
    mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    # send_file streams from disk and honors Range requests (so audio/video
    # can seek and large files don't load fully into memory), and encodes the
    # download filename safely (RFC 5987) rather than raw-quoting it.
    resp = send_file(
        target,
        mimetype=mime,
        as_attachment=as_attachment,
        download_name=target.name,
        conditional=True,
    )
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Content-Security-Policy"] = _RAW_CSP
    return resp


@app.route("/api/raw")
def api_raw() -> Response:
    """Inline bytes for image/pdf/audio/video preview."""
    return _serve_file(request.args.get("root", "files"), request.args.get("path", ""), as_attachment=False)


@app.route("/api/download")
def api_download() -> Response:
    return _serve_file(request.args.get("root", "files"), request.args.get("path", ""), as_attachment=True)


@app.route("/api/download-folder")
def api_download_folder() -> Response:
    root, target = _resolve(request.args.get("root", "files"), request.args.get("path", ""))
    if not target.is_dir():
        return _json({"error": "Not a folder"}, 404)

    denied_top = _ROOT_TOP_LEVEL_DENIED.get(root.key, frozenset())
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # followlinks stays False so symlinked directories aren't descended;
        # symlinked *files* are skipped explicitly below. Together this keeps
        # a symlink pointing outside the sandbox from leaking its target's
        # bytes into the archive.
        for dirpath, dirnames, filenames in os.walk(target):
            here = Path(dirpath)
            # Apply the same exclusions as api_list: hidden/denied segments at
            # any depth, symlinked dirs, and the root's top-level denied names
            # (e.g. ``data`` under the code root) so the zip can't reach past
            # the boundary the listing enforces.
            at_root = here == root.path
            dirnames[:] = [
                d
                for d in dirnames
                if not _segment_is_hidden(d)
                and not (here / d).is_symlink()
                and not (at_root and d in denied_top)
            ]
            for fname in filenames:
                if _segment_is_hidden(fname) or (at_root and fname in denied_top):
                    continue
                abs_path = here / fname
                if abs_path.is_symlink():
                    continue
                zf.write(abs_path, abs_path.relative_to(target.parent))
    buf.seek(0)
    # send_file encodes the download filename safely (RFC 5987) rather than
    # raw-quoting a folder name that could contain a double quote.
    return send_file(
        buf,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"{target.name or 'files'}.zip",
    )


@app.route("/api/changes")
def api_changes() -> Response:
    """List git-changed files (code root only)."""
    items = [{"path": p, "status": s} for p, s in sorted(_git_status_map().items())]
    return _json({"changes": items})


@app.route("/api/diff")
def api_diff() -> Response:
    """Return the git diff for one file (code root only)."""
    rel = request.args.get("path", "")
    _resolve("code", rel)  # validate against the sandbox; raises on rejection

    status = _git_status_map().get(rel)
    if status and status.strip() == "??":  # untracked: no diff to show
        return _json({"path": rel, "status": status, "untracked": True, "diff": ""})

    code, out = _git(["diff", "HEAD", "--", rel])
    if code != 0:
        # File may be staged-only or newly added; fall back to a plain diff.
        _, out = _git(["diff", "--", rel])
    if len(out.encode("utf-8")) > MAX_DIFF_BYTES:
        out = out[:MAX_DIFF_BYTES] + "\n... (diff truncated)"
    return _json({"path": rel, "status": status, "diff": out})


def main() -> None:
    run_simple("127.0.0.1", PORT, app, threaded=True, use_reloader=False, use_debugger=False)


if __name__ == "__main__":
    main()
