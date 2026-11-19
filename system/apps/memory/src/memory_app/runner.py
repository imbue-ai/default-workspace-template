"""The Memory app: the notes the agent remembers about the user across chats, to view and edit.

Services run from /home/user/workspace (the repo root). The app stores nothing of its own: the notes are the
agent's memory folder (``data/memories/``, Claude's ``autoMemoryDirectory``), read and written through
``agent_memory.notes``. The changes an agent made to them are the records Claude's change-log hook writes for the
chat (``data/.apps/chat/memory_changes/``); the chat's "Memory changed" cards show and undo them. The app's own
page does not list them, but the ``/api/changes`` routes read and undo the same records through
``agent_memory.changes``, so an agent or a script reaching the app has them, and a save in this app moves every
chat's baseline past it rather than being charged to the agent.

- Listen address: bind ``HOST`` and ``PORT``, which default to the loopback address and this app's assigned
  port but honor ``MEMORY_APP_HOST`` and ``MEMORY_APP_PORT``, so a preview can boot on a spare port beside the
  live one.
- The folders: ``main`` builds them with ``agent_memory.changes.default_log``, which resolves both against the
  main checkout and honors ``AGENT_MEMORY_DIR`` and ``AGENT_MEMORY_CHANGES_DIR``, which a preview points at copies
  (``[preview]`` in ``app.toml``), never at the user's real notes, and hands them to ``create_app``.
- The page's files ship alongside this file: ``Path(__file__).parent / "static"``.

This is a synchronous Flask app served by the threaded Werkzeug server. The app owns its own browser origin
(the forwarder routes ``http://memory.<workspace-host>/`` straight to this port), so it serves at ``/`` and
root-absolute URLs work unmodified.
"""

import os
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

from flask import Flask
from flask import Response
from flask import abort
from flask import send_file
from loguru import logger
from werkzeug.serving import run_simple

from agent_memory.changes import ChangeLog
from agent_memory.changes import ChangeNotFoundError
from agent_memory.changes import ChangedFile
from agent_memory.changes import ChangedSinceError
from agent_memory.changes import MemoryChange
from agent_memory.changes import changed_since_detail
from agent_memory.changes import default_log
from agent_memory.notes import INDEX_LINE_LIMIT
from agent_memory.notes import InvalidNoteError
from agent_memory.notes import Note
from agent_memory.notes import NoteChangedError
from agent_memory.notes import NoteDraft
from agent_memory.notes import NoteNotFoundError
from agent_memory.notes import NoteStore
from app_manifest.content_security_policy import attach_document_policy
from app_manifest.registry import SHELL_MODULE_PATHS
from app_schema.api_schema import DEFAULT_API_SCHEMA_PATH
from app_schema.api_schema import RouteModels
from app_schema.api_schema import RouteTable
from app_schema.api_schema import SchemalessRoute
from app_schema.api_schema import register_api_schema_route
from app_schema.request_guard import install_request_guard
from app_schema.routes import error_response
from app_schema.routes import model_response
from app_schema.routes import parse_request_body
from imbue.imbue_common.logging import setup_logging
from memory_app.data_types import DeletedNote
from memory_app.data_types import HealthResponse
from memory_app.data_types import MemoryAppChange
from memory_app.data_types import MemoryAppChangedFile
from memory_app.data_types import MemoryAppChangesListing
from memory_app.data_types import NoteChange
from memory_app.data_types import NoteDeletion
from memory_app.data_types import NotesListing
from memory_app.data_types import UndoRequest
from workspace_themes.flask_routes import THEME_STATIC_ROUTE_TABLE
from workspace_themes.flask_routes import register_workspace_theme_route

HOST: Final[str] = os.environ.get("MEMORY_APP_HOST", "127.0.0.1")
PORT: Final[int] = int(os.environ.get("MEMORY_APP_PORT", "8070"))

# What the manifest says the app is, repeated here for the API document (the test checks they agree).
APP_DISPLAY_NAME: Final[str] = "Memory"
APP_DESCRIPTION: Final[str] = (
    "View and edit the notes the agent remembers about you across chats: preferences, feedback, projects and "
    "references."
)

STATIC_DIR: Final[Path] = Path(__file__).parent / "static"
# The page's own script and stylesheet, by file name under /assets/.
_PAGE_FILE_TYPES: Final[dict[str, str]] = {"app.js": "text/javascript", "app.css": "text/css"}

# The browser-side files the workspace shell builds and every app serves from its own origin, by file name under
# /_static/: the app contract and the element context menu every page imports (``SHELL_MODULE_PATHS``), and the page
# kit, whose stylesheet gives the page the workspace's look and whose script follows the theme the shell names
# (docs/system/themeable-apps.md). A module import is a fetch without cookies, which the forwarder refuses across
# origins, so they are served here rather than from the shell. Relative to the repo root the service runs from.
_SHELL_STATIC_DIR: Final[Path] = Path("system/apps/system_interface/imbue/system_interface/static/_static")
_SHELL_FILES: Final[dict[str, tuple[Path, str]]] = {
    **{path.name: (path, "text/javascript") for path in SHELL_MODULE_PATHS},
    "workspace_theme.js": (_SHELL_STATIC_DIR / "workspace_theme.js", "text/javascript"),
    "workspace_theme.css": (_SHELL_STATIC_DIR / "workspace_theme.css", "text/css"),
}

_CHANGED_ELSEWHERE: Final[str] = "This note changed since you opened it. Reload to see the latest version."
_NOTE_NOT_FOUND: Final[str] = "This note no longer exists. Reload to see the current notes."
_CHANGE_NOT_FOUND: Final[str] = "That change is not in the record."

# What every route exchanges, which the app's API document publishes and every parse and answer
# is checked against (build-app skill, "The app describes its own API"): a route that reads or
# answers JSON names its pydantic models here and uses ``parse_request_body`` and
# ``model_response``; a page, a file or a stream is declared schemaless with the reason.
_ROUTE_TABLE: Final[RouteTable] = {
    ("GET", "/"): SchemalessRoute(reason="the app's page"),
    ("GET", "/assets/{basename}"): SchemalessRoute(reason="the page's own script or stylesheet"),
    ("GET", "/_static/{basename}"): SchemalessRoute(reason="a browser-side file the shell builds"),
    ("GET", "/health"): RouteModels(response_model=HealthResponse),
    ("GET", "/api/notes"): RouteModels(response_model=NotesListing),
    ("POST", "/api/notes"): RouteModels(request_model=NoteDraft, response_model=Note, success_status=201),
    ("PUT", "/api/notes/{note_id}"): RouteModels(request_model=NoteChange, response_model=Note),
    ("POST", "/api/notes/{note_id}/delete"): RouteModels(request_model=NoteDeletion, response_model=DeletedNote),
    ("GET", "/api/changes"): RouteModels(response_model=MemoryAppChangesListing),
    ("GET", "/api/changes/{change_id}"): RouteModels(response_model=MemoryAppChange),
    ("POST", "/api/changes/{change_id}/undo"): RouteModels(request_model=UndoRequest, response_model=MemoryAppChange),
    **THEME_STATIC_ROUTE_TABLE,
}


@contextmanager
def _uncharged_user_edit(changes: ChangeLog) -> Generator[None]:
    """Run a save or delete made in this app so that no chat's agent is charged with it."""
    before = changes.snapshot()
    try:
        yield
    finally:
        changes.absorb_user_edit(before)


def _error_response(detail: str, status: int) -> Response:
    """Answer a refused request with ``detail``, logging it first: every refusal here is one the page expects."""
    logger.debug("Refused a Memory app request with {}: {}", status, detail)
    return error_response(detail, status)


def _file_model(item: ChangedFile) -> MemoryAppChangedFile:
    return MemoryAppChangedFile(
        name=item.name, before=item.before, after=item.after, is_index_lines=item.is_index_lines
    )


def _change_model(change: MemoryChange) -> MemoryAppChange:
    return MemoryAppChange(
        id=change.id,
        time=change.time,
        summary=change.summary,
        chat_id=change.chat_id,
        agent_name=change.agent_name,
        note=change.note,
        files=tuple(_file_model(item) for item in change.files),
        undone_at=change.undone_at,
    )


def create_app(changes: ChangeLog, store: NoteStore) -> Flask:
    """The app over ``changes`` (the change log Claude's hook writes) and ``store`` (the notes in its memory folder)."""
    app = Flask("memory_app", static_folder=None)
    # Every built-in installs the provenance check ahead of its routes (system/apps/README.md).
    install_request_guard(app, exempt_rules=frozenset())
    # The app describes its own API: an OpenAPI document of every route below, at the manifest's api_schema_path.
    register_api_schema_route(
        app, DEFAULT_API_SCHEMA_PATH, title=APP_DISPLAY_NAME, description=APP_DESCRIPTION, route_table=_ROUTE_TABLE
    )
    # The page is reachable from the internet once the workspace is shared: it is served under the workspace's
    # document policy, scripts only from this origin.
    attach_document_policy(app, None)
    # The workspace's themes, from this app's own origin, so the page can wear the one the shell names.
    register_workspace_theme_route(app)

    @app.get("/")
    def index() -> Response:
        """The app's page: the list of remembered notes and the editor."""
        return Response((STATIC_DIR / "index.html").read_text(encoding="utf-8"), mimetype="text/html")

    @app.get("/assets/<basename>")
    def page_file(basename: str) -> Response:
        """The page's own script or stylesheet."""
        mimetype = _PAGE_FILE_TYPES.get(basename)
        if mimetype is None:
            abort(404)
        return send_file((STATIC_DIR / basename).absolute(), mimetype=mimetype)

    @app.get("/_static/<basename>")
    def shell_file(basename: str) -> Response:
        """One of the browser-side files the workspace shell builds and every app serves from its own origin."""
        # The shell-built files named above and nothing else: any other name is a
        # 404, so this route can never read outside that directory.
        entry = _SHELL_FILES.get(basename)
        if entry is None or not entry[0].is_file():
            abort(404)
        path, mimetype = entry
        # Flask resolves a relative path against the app's own directory, not the cwd.
        return send_file(path.absolute(), mimetype=mimetype)

    @app.get("/api/notes")
    def list_memory_notes() -> Response:
        """List every remembered note, in the order the index lists them, with the index itself line by line."""
        notes = store.list_notes()
        return model_response(
            NotesListing(
                notes=tuple(notes), folder=store.folder, index=store.outline(notes), index_line_limit=INDEX_LINE_LIMIT
            )
        )

    @app.post("/api/notes")
    def create_memory_note() -> Response:
        """Add a new note, and list it in the index."""
        draft = parse_request_body(NoteDraft)
        try:
            with _uncharged_user_edit(changes):
                note = store.create(draft)
        except InvalidNoteError as e:
            return _error_response(str(e), 400)
        return model_response(note, 201)

    @app.put("/api/notes/<note_id>")
    def update_memory_note(note_id: str) -> Response:
        """Save new content for a note, and update its line in the index.

        A note written straight into the index is turned into a note of its own,
        with a pointer line where it was. A note changed elsewhere since the editor
        loaded it is refused with 409.
        """
        change = parse_request_body(NoteChange)
        try:
            with _uncharged_user_edit(changes):
                note = store.update(note_id, change.version, change.note)
        except InvalidNoteError as e:
            return _error_response(str(e), 400)
        except NoteNotFoundError:
            return _error_response(_NOTE_NOT_FOUND, 404)
        except NoteChangedError:
            return _error_response(_CHANGED_ELSEWHERE, 409)
        return model_response(note)

    @app.post("/api/notes/<note_id>/delete")
    def delete_memory_note(note_id: str) -> Response:
        """Delete a note and its line in the index."""
        deletion = parse_request_body(NoteDeletion)
        try:
            with _uncharged_user_edit(changes):
                store.delete(note_id, deletion.version)
        except NoteNotFoundError:
            return _error_response(_NOTE_NOT_FOUND, 404)
        except NoteChangedError:
            return _error_response(_CHANGED_ELSEWHERE, 409)
        return model_response(DeletedNote(id=note_id))

    @app.get("/api/changes")
    def list_memory_app_changes() -> Response:
        """List every change an agent made to the notes, newest first, with the text before and after."""
        listed = reversed(changes.list_changes())
        return model_response(MemoryAppChangesListing(changes=tuple(_change_model(change) for change in listed)))

    @app.get("/api/changes/<change_id>")
    def get_memory_app_change(change_id: str) -> Response:
        """One change an agent made to the notes."""
        try:
            change = changes.get(change_id)
        except ChangeNotFoundError:
            return _error_response(_CHANGE_NOT_FOUND, 404)
        return model_response(_change_model(change))

    @app.post("/api/changes/<change_id>/undo")
    def undo_memory_app_change(change_id: str) -> Response:
        """Undo a change: put back the note and the index lines it touched as they were before it.

        Refused with 409 when one of them was changed again since, unless ``is_forced``.
        """
        undo = parse_request_body(UndoRequest)
        try:
            change = changes.undo(change_id, is_forced=undo.is_forced)
        except ChangeNotFoundError:
            return _error_response(_CHANGE_NOT_FOUND, 404)
        except ChangedSinceError as e:
            return _error_response(changed_since_detail(e), 409)
        return model_response(_change_model(change))

    @app.get("/health")
    def health() -> Response:
        """Whether the app is up."""
        return model_response(HealthResponse(status="ok"))

    return app


def main() -> None:
    setup_logging()
    # The change log Claude's hook writes and the memory folder it watches; the notes are that same folder.
    changes = default_log(os.environ)
    store = NoteStore(changes.memory_dir)
    app = create_app(changes, store)
    logger.info("Serving the Memory app at http://{}:{}/ over {}", HOST, PORT, store.folder)
    run_simple(HOST, PORT, app, threaded=True, use_reloader=False, use_debugger=False)


if __name__ == "__main__":
    main()
