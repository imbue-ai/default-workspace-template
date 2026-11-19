"""View and edit the notes the agent remembers about you across chats: preferences, feedback, projects and references.

Services run from /home/user/workspace (the repo root). Conventions:

- Persistent state (anything written and read across runs -- cursors,
  caches, snapshots, user records): read and write it under ``DATA_DIR``
  (defined below), never a hardcoded ``data/.apps/memory/`` at the
  call site. ``DATA_DIR`` defaults to ``data/.apps/memory/`` but
  honors the ``MEMORY_DATA_DIR`` env var, so an editing agent can point a
  throwaway instance at a *copy* of the data instead of the live store
  (see the update-app skill). Do NOT use ``Path(__file__)``-based
  paths for state -- the bug to avoid is one process writing to
  ``/home/user/workspace/data/.apps/...`` while another reads from
  ``/home/user/workspace/system/apps/<pkg>/data/...``.
- Static assets shipped alongside this file (templates, default
  configs, bundled JSON): ``Path(__file__).parent / "assets/..."`` is
  fine and is the right pattern.
- Listen port: bind ``PORT`` (defined below), which defaults to this
  app's assigned port but honors the ``MEMORY_PORT`` env var, so
  an editing agent can boot a throwaway instance on a *spare* port
  alongside the live one (see the update-app skill). Never hardcode
  the port at the ``run_simple`` call.

This is a synchronous Flask app served by the threaded Werkzeug server.
The app owns its own browser origin (the forwarder routes
``http://memory.<workspace-host>/`` straight to this port), so it serves
at ``/`` and root-absolute URLs, cookies, and service workers all work
unmodified -- nothing rewrites anything. Use ``flask_sock`` if you need
WebSockets.
"""

import base64
import hashlib
import os
from pathlib import Path

from app_schema.api_schema import (
    DEFAULT_API_SCHEMA_PATH,
    RouteModels,
    RouteTable,
    SchemalessRoute,
    register_api_schema_route,
)
from app_schema.routes import error_response
from app_schema.routes import model_response
from app_schema.routes import parse_request_body
from memory.changes import ChangeLog
from memory.changes import ChangeNotFoundError
from memory.changes import ChangedSinceError
from memory.changes import MemoryChange
from memory.notes import IndexOutline
from memory.notes import InvalidNoteError
from memory.notes import Note
from memory.notes import NoteChangedError
from memory.notes import NoteDraft
from memory.notes import NoteNotFoundError
from memory.notes import NoteStore
from flask import Flask, Response, abort, request, send_file
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import setup_logging
from loguru import logger
from pydantic import Field
from werkzeug.serving import run_simple
from workspace_themes.flask_routes import THEME_STATIC_ROUTE_TABLE
from workspace_themes.flask_routes import register_workspace_theme_route

# Persistent state for this app lives under DATA_DIR. It defaults to
# ``data/.apps/memory/`` but is overridable via the ``MEMORY_DATA_DIR`` env var
# so a throwaway instance can run against a *copy* of the data while editing --
# see the update-app skill. Always read/write state through DATA_DIR;
# never hardcode ``data/.apps/memory/`` at a call site, or the override is
# bypassed. A writing call site should ``DATA_DIR.mkdir(parents=True,
# exist_ok=True)`` before writing.
DATA_DIR = Path(os.environ.get("MEMORY_DATA_DIR", "data/.apps/memory"))

# Listen port. Defaults to this app's assigned port but is overridable via
# the ``MEMORY_PORT`` env var so an editing agent can boot a throwaway
# instance on a spare port next to the live one (see the update-app skill).
# Never hardcode the port at the ``run_simple`` call, or the override is bypassed.
PORT = int(os.environ.get("MEMORY_PORT", "8081"))

# The agent's memory folder, the notes this app shows and edits. It is the folder
# the agent's own memory reads (``autoMemoryDirectory`` in .claude/settings.json),
# so it is not under DATA_DIR; ``MEMORY_NOTES_DIR`` points a throwaway instance at
# a copy of it instead.
NOTES_DIR = Path(os.environ.get("MEMORY_NOTES_DIR", "data/memories"))
STORE = NoteStore(NOTES_DIR)
# Every change an agent made to the notes, recorded by the agent's change-log hook
# (system/scripts/agent_memory_change_log.py) for review and undo.
CHANGES = ChangeLog(DATA_DIR / "changes", NOTES_DIR)

# The browser-side modules the workspace shell builds and every app serves from
# its own origin: the app contract (how a page talks to the shell framing it) and
# the element context menu (the right-click menu whose last rows hand the
# clicked element to a chat). A module import is a fetch without cookies, which
# the forwarder refuses across origins, so they are served here rather than from
# the shell. Relative to the repo root the service runs from, like DATA_DIR.
SHELL_STATIC_MODULES_DIR = Path("system/apps/system_interface/imbue/system_interface/static/_static")
# The page's own static files, shipped beside this module.
ASSETS_DIR = Path(__file__).parent / "assets"
ASSET_TYPES = {"app.js": "text/javascript", "app.css": "text/css"}

# The page kit (workspace_theme.css and .js) is part of it: the stylesheet gives the
# page the workspace's standard look and the script follows the theme the shell names,
# the way the built-in apps do (docs/system/themeable-apps.md).
SHELL_STATIC_MODULE_TYPES = {
    "app_contract.js": "text/javascript",
    "context_menu.js": "text/javascript",
    "workspace_theme.js": "text/javascript",
    "workspace_theme.css": "text/css",
}

# The script every page serves (keep it on every page): it connects the page to
# the shell, reports where the page is on the handshake so the shell can reopen
# this app's window at the same place, and installs the element context menu.
# A page visited outside the shell runs it harmlessly: nothing arrives, and the
# menu's Explain and Modify rows grey out.
SHELL_PAGE_SCRIPT_BODY = """
  import { connectToShell } from "/_static/app_contract.js";
  import { installElementContextMenu } from "/_static/context_menu.js";
  let handshake = null;
  const connection = connectToShell({
    onHandshake: (received) => {
      handshake = received;
      connection.location(location.pathname + location.search, document.title);
    },
  });
  installElementContextMenu({ connection, handshake: () => handshake });
"""
SHELL_PAGE_SCRIPT = f'<script type="module">{SHELL_PAGE_SCRIPT_BODY}</script>'


def inline_script_source(body: str) -> str:
    """The Content-Security-Policy source admitting one inline <script> whose body is exactly ``body``."""
    return "'sha256-" + base64.b64encode(hashlib.sha256(body.encode()).digest()).decode() + "'"


# Every HTML page is served under this policy (the app is reachable from the
# internet once the workspace is shared): scripts only from this origin plus the
# inline blocks listed here by hash, fetches, sockets and images only from this
# origin, inline styles, frames of any http(s) page, no plugins. Anything else a
# page pulls in (a CDN script, Google Fonts, an image hosted elsewhere, a
# third-party API called from the browser) is blocked until its origin is added
# to the matching directive; a new inline <script> needs its
# ``inline_script_source`` added.
# The browser posts every block to ``/_csp-report`` below, which logs it on
# stderr (the supervisor log), so a blocked resource is never silent; the
# ``'report-sample'`` makes the browser include where a blocked inline script
# is and how it starts, since the report otherwise names it only as "inline".
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    f"script-src 'self' 'report-sample' {inline_script_source(SHELL_PAGE_SCRIPT_BODY)}; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; frame-src http: https:; "
    "object-src 'none'; base-uri 'self'; form-action 'self'; "
    "report-uri /_csp-report"
)
BROWSER_EXTENSION_SCHEMES = ("chrome-extension:", "moz-extension:", "safari-web-extension:")


class HealthResponse(FrozenModel):
    """What the health probe answers."""

    status: str = Field(description="Always 'ok'")


class NotesListing(FrozenModel):
    """Every remembered note, in the index's order."""

    notes: tuple[Note, ...] = Field(description="The notes")
    folder: str = Field(description="The memory folder the notes are read from")
    index: IndexOutline = Field(description="The index (MEMORY.md) line by line, as the navigation mirrors it")


class NoteChange(FrozenModel):
    """New content for an existing note."""

    version: str = Field(description="The note's version when the editor loaded it")
    note: NoteDraft = Field(description="The note's new content")


class NoteDeletion(FrozenModel):
    """Which version of a note to delete."""

    version: str = Field(description="The note's version when the editor loaded it")


class ChangesListing(FrozenModel):
    """Every change an agent made to the notes, newest first."""

    changes: tuple[MemoryChange, ...] = Field(description="The changes")


class UndoRequest(FrozenModel):
    """How to undo a change."""

    force: bool = Field(default=False, description="Undo even if a file was changed again since")


class DeletedNote(FrozenModel):
    """The note that was deleted."""

    id: str = Field(description="The deleted note's id")


# What every route exchanges, which the app's API document publishes and every parse and answer
# is checked against (build-app skill, "The app describes its own API"): a route that reads or
# answers JSON names its pydantic models here and uses ``parse_request_body`` and
# ``model_response``; a page, a file or a stream is declared schemaless with the reason.
ROUTE_TABLE: RouteTable = {
    ("GET", "/"): SchemalessRoute(reason="the app's page"),
    ("GET", "/assets/{basename}"): SchemalessRoute(reason="the page's own script or stylesheet"),
    ("GET", "/_static/{basename}"): SchemalessRoute(reason="a browser-side module the shell builds"),
    ("GET", "/health"): RouteModels(response_model=HealthResponse),
    ("GET", "/api/notes"): RouteModels(response_model=NotesListing),
    ("POST", "/api/notes"): RouteModels(request_model=NoteDraft, response_model=Note, success_status=201),
    ("PUT", "/api/notes/{note_id}"): RouteModels(request_model=NoteChange, response_model=Note),
    ("POST", "/api/notes/{note_id}/delete"): RouteModels(request_model=NoteDeletion, response_model=DeletedNote),
    ("GET", "/api/changes"): RouteModels(response_model=ChangesListing),
    ("GET", "/api/changes/{change_id}"): RouteModels(response_model=MemoryChange),
    ("POST", "/api/changes/{change_id}/undo"): RouteModels(request_model=UndoRequest, response_model=MemoryChange),
    ("POST", "/_csp-report"): SchemalessRoute(reason="the browser's report of what the page's policy blocked"),
    **THEME_STATIC_ROUTE_TABLE,
}

app = Flask("memory", static_folder=None)
# The app describes its own API: an OpenAPI document of every route below, served at the
# manifest's api_schema_path.
register_api_schema_route(
    app, DEFAULT_API_SCHEMA_PATH, title='Memory', description='View and edit the notes the agent remembers about you across chats: preferences, feedback, projects and references.', route_table=ROUTE_TABLE
)
# The workspace's themes, from this app's own origin, so the page can wear the one the shell names.
register_workspace_theme_route(app)


@app.after_request
def add_content_security_policy(response: Response) -> Response:
    if response.mimetype == "text/html":
        response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
    return response


@app.route("/")
def index() -> Response:
    """The app's page: the list of remembered notes and the editor."""
    page = (ASSETS_DIR / "index.html").read_text()
    return Response(page.replace("</body>", SHELL_PAGE_SCRIPT + "</body>"), mimetype="text/html")


@app.route("/assets/<basename>")
def asset(basename: str) -> Response:
    """One of the page's own scripts or stylesheets."""
    if basename not in ASSET_TYPES:
        abort(404)
    return send_file((ASSETS_DIR / basename).absolute(), mimetype=ASSET_TYPES[basename])


@app.route("/_csp-report", methods=["POST"])
def content_security_policy_report() -> Response:
    """Where the browser reports what the page's security policy blocked."""
    body = request.get_json(force=True, silent=True)
    report = body.get("csp-report") if isinstance(body, dict) else None
    if not isinstance(report, dict):
        return Response(status=204)
    blocked, page, source, directive, line, sample = (
        str(report.get(key, ""))[:500]
        for key in ("blocked-uri", "document-uri", "source-file", "effective-directive", "line-number", "script-sample")
    )
    # A block caused by a browser extension is the policy doing its job and is not logged.
    if blocked.startswith(BROWSER_EXTENSION_SCHEMES) or source.startswith(BROWSER_EXTENSION_SCHEMES):
        return Response(status=204)
    # An inline script or style is reported as "inline": say where it is and how it starts instead.
    where = f" at {source}:{line}" if source and line else ""
    starting = f" starting {sample!r}" if sample else ""
    remedy = (
        "add its inline_script_source to CONTENT_SECURITY_POLICY if the page needs it"
        if blocked == "inline"
        else "add its origin to CONTENT_SECURITY_POLICY if the page needs it"
    )
    logger.error(
        "Content-Security-Policy blocked {!r} on {!r} ({}){}{}; {}", blocked, page, directive, where, starting, remedy
    )
    return Response(status=204)


@app.route("/_static/<basename>")
def shell_module(basename: str) -> Response:
    """One of the browser-side files the workspace shell builds and every app serves from its own origin."""
    # The shell-built files named above and nothing else: any other name is a
    # 404, so this route can never read outside that directory.
    if basename not in SHELL_STATIC_MODULE_TYPES:
        abort(404)
    module_path = SHELL_STATIC_MODULES_DIR / basename
    if not module_path.is_file():
        abort(404)
    # Flask resolves a relative path against the app's own directory, not the cwd.
    return send_file(module_path.absolute(), mimetype=SHELL_STATIC_MODULE_TYPES[basename])


CHANGED_ELSEWHERE = "This note changed since you opened it. Reload to see the latest version."
NOT_FOUND = "This note no longer exists. Reload to see the current notes."


@app.route("/api/notes")
def list_notes() -> Response:
    """List every remembered note, in the order the index lists them, with the index itself line by line."""
    notes = STORE.list_notes()
    return model_response(NotesListing(notes=tuple(notes), folder=str(NOTES_DIR), index=STORE.outline(notes)))


@app.route("/api/notes", methods=["POST"])
def create_note() -> Response:
    """Add a new note, and list it in the index."""
    draft = parse_request_body(NoteDraft)
    try:
        return model_response(STORE.create(draft), 201)
    except InvalidNoteError as e:
        return error_response(str(e), 400)


@app.route("/api/notes/<note_id>", methods=["PUT"])
def update_note(note_id: str) -> Response:
    """Save new content for a note, and update its line in the index.

    A note written straight into the index is turned into a note of its own,
    with a pointer line where it was. A note changed elsewhere since the editor
    loaded it is refused with 409.
    """
    change = parse_request_body(NoteChange)
    try:
        return model_response(STORE.update(note_id, change.version, change.note))
    except InvalidNoteError as e:
        return error_response(str(e), 400)
    except NoteNotFoundError:
        return error_response(NOT_FOUND, 404)
    except NoteChangedError:
        return error_response(CHANGED_ELSEWHERE, 409)


@app.route("/api/notes/<note_id>/delete", methods=["POST"])
def delete_note(note_id: str) -> Response:
    """Delete a note and its line in the index."""
    deletion = parse_request_body(NoteDeletion)
    try:
        STORE.delete(note_id, deletion.version)
    except NoteNotFoundError:
        return error_response(NOT_FOUND, 404)
    except NoteChangedError:
        return error_response(CHANGED_ELSEWHERE, 409)
    return model_response(DeletedNote(id=note_id))


@app.route("/api/changes")
def list_changes() -> Response:
    """List every change an agent made to the notes, newest first, with the text before and after."""
    return model_response(ChangesListing(changes=tuple(CHANGES.list_changes())))


@app.route("/api/changes/<change_id>")
def get_change(change_id: str) -> Response:
    """One change an agent made to the notes."""
    try:
        return model_response(CHANGES.get(change_id))
    except ChangeNotFoundError:
        return error_response("That change is not in the record.", 404)


@app.route("/api/changes/<change_id>/undo", methods=["POST"])
def undo_change(change_id: str) -> Response:
    """Undo a change: put every file it touched back as it was before.

    Refused with 409 when one of those files was changed again since, unless ``force``.
    """
    undo = parse_request_body(UndoRequest)
    try:
        return model_response(CHANGES.undo(change_id, undo.force))
    except ChangeNotFoundError:
        return error_response("That change is not in the record.", 404)
    except ChangedSinceError as e:
        return error_response(f"Changed again since: {', '.join(e.names)}. Undoing would lose those later edits.", 409)


@app.route("/health")
def health() -> Response:
    """Whether the app is up."""
    return model_response(HealthResponse(status="ok"))


def main() -> None:
    setup_logging()
    run_simple(
        "127.0.0.1", PORT, app, threaded=True, use_reloader=False, use_debugger=False
    )


if __name__ == "__main__":
    main()
