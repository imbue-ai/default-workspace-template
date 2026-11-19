"""The Memory app: the notes the agent remembers about the user across chats, and every change an agent made to them.

Services run from /home/user/workspace (the repo root). The app stores nothing of its own: the notes are the
agent's memory folder (``data/memories/``, Claude's ``autoMemoryDirectory``), read and written through
``agent_memory.notes``, and the changes are the records Claude's change-log hook writes for the chat
(``data/.apps/chat/memory_changes/``), read and undone through ``agent_memory.changes`` -- the same library and
the same records the chat's "Memory changed" cards use, so an Undo here and one in the chat are one Undo.

- Listen address: bind ``HOST`` and ``PORT``, which default to the loopback address and this app's assigned
  port but honor ``MEMORY_APP_HOST`` and ``MEMORY_APP_PORT``, so a preview can boot on a spare port beside the
  live one.
- The folders: ``agent_memory.changes.default_log`` resolves both against the main checkout and honors
  ``AGENT_MEMORY_DIR`` and ``AGENT_MEMORY_CHANGES_DIR``, which a preview points at copies (``[preview]`` in
  ``app.toml``), never at the user's real notes.
- Static assets shipped alongside this file: ``Path(__file__).parent / "assets"``.

This is a synchronous Flask app served by the threaded Werkzeug server. The app owns its own browser origin
(the forwarder routes ``http://memory.<workspace-host>/`` straight to this port), so it serves at ``/`` and
root-absolute URLs work unmodified.
"""

import base64
import hashlib
import os
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Final
from typing import TypeVar

from flask import Flask
from flask import Response
from flask import abort
from flask import request
from flask import send_file
from loguru import logger
from pydantic import Field
from werkzeug.serving import run_simple

from agent_memory.changes import ChangeNotFoundError
from agent_memory.changes import ChangedFile
from agent_memory.changes import ChangedSinceError
from agent_memory.changes import INDEX_FILE_NAME
from agent_memory.changes import MemoryChange as MemoryChangeRecord
from agent_memory.changes import default_log
from agent_memory.notes import INDEX_LINE_LIMIT
from agent_memory.notes import IndexOutline
from agent_memory.notes import InvalidNoteError
from agent_memory.notes import Note
from agent_memory.notes import NoteChangedError
from agent_memory.notes import NoteDraft
from agent_memory.notes import NoteNotFoundError
from agent_memory.notes import NoteStore
from app_schema.api_schema import DEFAULT_API_SCHEMA_PATH
from app_schema.api_schema import RouteModels
from app_schema.api_schema import RouteTable
from app_schema.api_schema import SchemalessRoute
from app_schema.api_schema import register_api_schema_route
from app_schema.request_guard import install_request_guard
from app_schema.routes import error_response
from app_schema.routes import model_response
from app_schema.routes import parse_request_body
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import setup_logging
from workspace_themes.flask_routes import THEME_STATIC_ROUTE_TABLE
from workspace_themes.flask_routes import register_workspace_theme_route

APP_DISPLAY_NAME: Final = "Memory"
APP_DESCRIPTION: Final = (
    "View and edit the notes the agent remembers about you across chats: preferences, feedback, projects and "
    "references, and undo any change an agent made to them."
)
HOST: Final = os.environ.get("MEMORY_APP_HOST", "127.0.0.1")
PORT: Final = int(os.environ.get("MEMORY_APP_PORT", "8070"))

# The change log Claude's hook writes and the memory folder it watches; the notes are that same folder.
CHANGES = default_log(os.environ)
STORE = NoteStore(CHANGES.memory_dir)

# The browser-side modules the workspace shell builds and every app serves from
# its own origin: the app contract (how a page talks to the shell framing it) and
# the element context menu (the right-click menu whose last rows hand the
# clicked element to a chat). A module import is a fetch without cookies, which
# the forwarder refuses across origins, so they are served here rather than from
# the shell. Relative to the repo root the service runs from.
SHELL_STATIC_MODULES_DIR = Path("system/apps/system_interface/imbue/system_interface/static/_static")
# The page's own static files, shipped beside this module.
ASSETS_DIR = Path(__file__).parent / "assets"
ASSET_TYPES = {"app.js": "text/javascript", "app.css": "text/css"}

# The page kit (workspace_theme.css and .js) is part of it: the stylesheet gives the
# page the workspace's standard look and the script follows the theme the shell names,
# the way the built-in apps do (docs/system/themeable-apps.md).
SHELL_STATIC_MODULE_TYPES: Final = {
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
    index_line_limit: int = Field(description="How long an index line may be before the editor warns about it")


class NoteChange(FrozenModel):
    """New content for an existing note."""

    version: str = Field(description="The note's version when the editor loaded it")
    note: NoteDraft = Field(description="The note's new content")


class NoteDeletion(FrozenModel):
    """Which version of a note to delete."""

    version: str = Field(description="The note's version when the editor loaded it")


class ChangedMemoryFile(FrozenModel):
    """One file a change touched: its text before and after."""

    name: str = Field(description="The file's name in the memory folder")
    before: str | None = Field(description="Its text before the change, or null if the change created it")
    after: str | None = Field(description="Its text after the change, or null if the change deleted it")
    is_index_lines: bool = Field(
        description="Whether this is the index holding only the change's own lines (a note's pointer line, say), "
        "rather than the whole file"
    )


class MemoryChange(FrozenModel):
    """What one agent turn changed in one note (with its index line), or in the index's other lines, undone as one."""

    id: str = Field(description="The change's id; ids sort by time")
    time: datetime = Field(description="When the turn first changed memory (ISO 8601, UTC)")
    summary: str = Field(description="What changed, in one line, e.g. 'added the note \"Tea\" and its index line'")
    chat_id: str = Field(description="The chat whose agent made it")
    agent_name: str = Field(description="The name of the agent that made it")
    note: str | None = Field(description="The note file the change belongs to; null for the index's other lines")
    files: tuple[ChangedMemoryFile, ...] = Field(description="Every file it changed")
    undone_at: datetime | None = Field(description="When it was undone, or null")


class ChangesListing(FrozenModel):
    """Every change an agent made to the notes, newest first."""

    changes: tuple[MemoryChange, ...] = Field(description="The changes")


class UndoRequest(FrozenModel):
    """How to undo a change."""

    is_forced: bool = Field(default=False, description="Undo even if a file was changed again since")


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

app = Flask("memory_app", static_folder=None)
# Every built-in installs the provenance check ahead of its routes (system/apps/README.md).
install_request_guard(app, exempt_rules=frozenset())
# The app describes its own API: an OpenAPI document of every route below, served at the
# manifest's api_schema_path.
register_api_schema_route(
    app, DEFAULT_API_SCHEMA_PATH, title=APP_DISPLAY_NAME, description=APP_DESCRIPTION, route_table=ROUTE_TABLE
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
        for key in (
            "blocked-uri",
            "document-uri",
            "source-file",
            "effective-directive",
            "line-number",
            "script-sample",
        )
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


_T = TypeVar("_T")


def _as_user_edit(write: Callable[[], _T]) -> _T:
    """Run ``write`` (a save or delete in this app) so that no chat's agent is charged with it."""
    before = CHANGES.snapshot()
    try:
        return write()
    finally:
        CHANGES.absorb_user_edit(before)


CHANGED_ELSEWHERE = "This note changed since you opened it. Reload to see the latest version."
NOT_FOUND = "This note no longer exists. Reload to see the current notes."


@app.route("/api/notes")
def list_notes() -> Response:
    """List every remembered note, in the order the index lists them, with the index itself line by line."""
    notes = STORE.list_notes()
    return model_response(
        NotesListing(
            notes=tuple(notes),
            folder=str(STORE.folder),
            index=STORE.outline(notes),
            index_line_limit=INDEX_LINE_LIMIT,
        )
    )


@app.route("/api/notes", methods=["POST"])
def create_note() -> Response:
    """Add a new note, and list it in the index."""
    draft = parse_request_body(NoteDraft)
    try:
        return model_response(_as_user_edit(lambda: STORE.create(draft)), 201)
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
        return model_response(_as_user_edit(lambda: STORE.update(note_id, change.version, change.note)))
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
        _as_user_edit(lambda: STORE.delete(note_id, deletion.version))
    except NoteNotFoundError:
        return error_response(NOT_FOUND, 404)
    except NoteChangedError:
        return error_response(CHANGED_ELSEWHERE, 409)
    return model_response(DeletedNote(id=note_id))


def _file_model(item: ChangedFile) -> ChangedMemoryFile:
    return ChangedMemoryFile(name=item.name, before=item.before, after=item.after, is_index_lines=item.is_index_lines)


def _change_model(change: MemoryChangeRecord) -> MemoryChange:
    return MemoryChange(
        id=change.id,
        time=change.time,
        summary=change.summary,
        chat_id=change.chat_id,
        agent_name=change.agent_name,
        note=change.note,
        files=tuple(_file_model(item) for item in change.files),
        undone_at=change.undone_at,
    )


CHANGE_NOT_FOUND = "That change is not in the record."


@app.route("/api/changes")
def list_changes() -> Response:
    """List every change an agent made to the notes, newest first, with the text before and after."""
    changes = reversed(CHANGES.list_changes())
    return model_response(ChangesListing(changes=tuple(_change_model(change) for change in changes)))


@app.route("/api/changes/<change_id>")
def get_change(change_id: str) -> Response:
    """One change an agent made to the notes."""
    try:
        return model_response(_change_model(CHANGES.get(change_id)))
    except ChangeNotFoundError:
        return error_response(CHANGE_NOT_FOUND, 404)


@app.route("/api/changes/<change_id>/undo", methods=["POST"])
def undo_change(change_id: str) -> Response:
    """Undo a change: put back the note and the index lines it touched as they were before it.

    Refused with 409 when one of them was changed again since, unless ``is_forced``.
    """
    undo = parse_request_body(UndoRequest)
    try:
        return model_response(_change_model(CHANGES.undo(change_id, is_forced=undo.is_forced)))
    except ChangeNotFoundError:
        return error_response(CHANGE_NOT_FOUND, 404)
    except ChangedSinceError as e:
        names = ", ".join("the index" if name == INDEX_FILE_NAME else name for name in e.names)
        return error_response(f"Changed again since: {names}. Undoing would lose those later edits.", 409)


@app.route("/health")
def health() -> Response:
    """Whether the app is up."""
    return model_response(HealthResponse(status="ok"))


def main() -> None:
    setup_logging()
    logger.info("Serving the Memory app at http://{}:{}/ over {}", HOST, PORT, STORE.folder)
    run_simple(HOST, PORT, app, threaded=True, use_reloader=False, use_debugger=False)


if __name__ == "__main__":
    main()
