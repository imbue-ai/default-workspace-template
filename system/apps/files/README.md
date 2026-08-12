# files

A **read-only** macOS-style file browser served as its own app tab. A
single-page frontend talks to a small Flask backend over a JSON API; every
file access funnels through one security check before it touches disk.

## What it does

Browse, preview, and download workspace files without a terminal.

- **Two browse roots**
  - **My Files** — the workspace `data/` directory (personal files).
  - **Code** *(advanced)* — the workspace git repo, with syntax highlighting
    and git diffs. Never exposes the personal `data/` tree.
- **Two views** (toggle in the menubar)
  - **Finder** — a macOS-style desktop and window with three layouts:
    **Icons**, **List**, and **Columns** (Miller columns for drilling through
    nested folders). Double-click a file to open a preview modal.
  - **Viewer** — a VS Code-style layout: a file tree, tabs for open files, and
    a content pane. Read-only (the name reflects that it cannot edit).
- **Previews** — images, PDF, audio, and video render inline; text, markdown
  (sanitized), and code (syntax-highlighted) render as text. Files that are too
  large or binary fall back to a download button.
- **Downloads** — any file, or a whole folder zipped on the fly.
- **Git** *(Code root)* — a Changes list of modified files and a per-file diff,
  with a File/Diff toggle in previews and the Viewer.
- **Search** — a name search across the current root with a type filter
  (folders, or a file kind: images, PDFs, code, markdown, …). Names only, not
  file contents.
- **Dark mode** — a theme toggle, remembered in the browser, with a matching
  code-highlight palette.

## Architecture

Data flows top → bottom on request, bottom → top on response:

```
Browser (single-page app, assets/index.html)
   Finder view · Viewer view · root toggle · search · dark mode
        |  fetch() -- JSON for data, bytes for media
        v
Flask backend (runner.py, threaded)
   listing · preview · download · git · search
        |  every path is validated here
        v
resolve_within_root(root, path)   <- the single security chokepoint
        |
        v
Sources on disk
   My Files -> data/     Code -> the repo     git -> status & diffs
```

## HTTP API

All routes are read-only. Every route that names a path validates it through
`resolve_within_root` first.

| Route | Returns |
|---|---|
| `GET /` | The single-page app (HTML). |
| `GET /logo.png` | The imbue logomark shown in the menubar. |
| `GET /api/roots` | The browse roots (`files`, `code`). |
| `GET /api/list?root&path` | A folder's entries (name, type, kind, size, mtime, git status) plus its breadcrumb. |
| `GET /api/search?root&q&type` | Name matches under a root, filtered by type; bounded by result/scan caps. |
| `GET /api/content?root&path` | Rendered text for preview: markdown (sanitized) or syntax-highlighted code. |
| `GET /api/raw?root&path` | Raw bytes for image/PDF/audio/video -- streamed, Range-capable, script-blocked. |
| `GET /api/download?root&path` | One file, as a download. |
| `GET /api/download-folder?root&path` | A whole folder, zipped. |
| `GET /api/changes` | Git-changed files (Code root). |
| `GET /api/diff?path` | The git diff for one file. |
| `GET /api/pygments.css` | Highlight colors (light + dark). |
| `GET /health` | `{"status": "ok"}`. |

## Security model

- **One gate.** Every filesystem route goes through `resolve_within_root`; no
  route touches disk without it.
- **Sandboxed.** My Files sees only `data/`; Code sees only the repo and never
  the personal `data/` tree (excluded at the root level, including in zips).
- **Hidden/secret files** (dot-folders such as `.secrets`/`.git`, `.env`
  files, and build noise like `node_modules`/`__pycache__`) are never listed,
  served, searched, or zipped.
- **No escapes.** `..` traversal and symlinks pointing outside a root are
  rejected; folder-zips skip symlinks so a link can't leak an out-of-root file.
- **No script injection.** Filenames are HTML-escaped in the UI, previewed
  markdown is sanitized with `nh3`, and raw files are served under a
  script-blocking `Content-Security-Policy`.
- **Read-only.** There is no route that writes, renames, moves, or deletes.

## Dependencies

- **Flask** (threaded) -- serves the page and the JSON API.
- **Pygments** -- syntax-highlighted HTML for code (light + dark themes).
- **Markdown + nh3** -- renders markdown, then strips anything executable.
- **git** (subprocess) -- changed files and per-file diffs; status is cached a
  few seconds so bursts of listings share one scan.
- **zipfile** (stdlib) -- builds folder downloads in memory.

## Running

Registered as a supervisord program (`files`) that binds `127.0.0.1:PORT` and
is exposed at its own origin via `forward_port.py`. Configuration is via
environment variables:

- `FILES_PORT` -- listen port (defaults to the app's assigned port).
- `FILES_REPO_ROOT` -- the repo root the two browse roots resolve from
  (defaults to the workspace root; overridable for tests).

## Testing

`cd system/apps/files && uv run pytest` -- the suite is the standard ratchet
checks (`test_files_ratchets.py`).
