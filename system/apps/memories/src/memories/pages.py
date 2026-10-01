"""The app's routes on its own origin: the page, the health probe, the notes, correcting and deleting a note, and
the contract module. Everything is read when asked; the app keeps nothing between requests."""

from collections.abc import Mapping
from pathlib import Path
from typing import Any
from typing import Final

import httpx
from flask import Blueprint
from flask import Response
from flask import jsonify
from flask import request
from flask import send_file
from flask import send_from_directory
from flask.typing import ResponseReturnValue
from werkzeug.exceptions import NotFound

from app_manifest.errors import RegistryReadError
from app_manifest.registry import APP_CONTRACT_ROUTE
from app_manifest.registry import read_registry
from memories.attribution import DEFAULT_CHAT_APP_URL
from memories.attribution import TranscriptSources
from memories.attribution import read_attributions
from memories.backups import read_backup_retention
from memories.errors import MemoriesError
from memories.errors import NoteChangedError
from memories.errors import NoteNameError
from memories.errors import NoteNotFoundError
from memories.notes import INDEX_FILENAME
from memories.notes import delete_note
from memories.notes import list_notes
from memories.notes import update_note
from memories.request_guard import is_write_allowed

BLUEPRINT_NAME: Final[str] = "memories_pages"
HEALTH_PATH: Final[str] = "/api/health"
NOTES_PATH: Final[str] = "/api/notes"
PAGE_DOCUMENT_FILENAME: Final[str] = "index.html"
CHAT_APP_NAME: Final[str] = "chat"

HTTP_BAD_REQUEST: Final[int] = 400
HTTP_FORBIDDEN: Final[int] = 403
HTTP_NOT_FOUND: Final[int] = 404
HTTP_CONFLICT: Final[int] = 409
HTTP_SERVER_ERROR: Final[int] = 500

_NOT_BUILT_PAGE: Final[str] = (
    '<!doctype html><html><head><meta charset="utf-8"><title>What agents know</title></head>'
    "<body><p>This page has not been built yet (run <code>npm run build</code> in <code>system/</code>).</p>"
    "</body></html>"
)


def _chat_app_url(registry_path: Path) -> str:
    try:
        rows = read_registry(registry_path)
    except RegistryReadError:
        return DEFAULT_CHAT_APP_URL
    return next((str(row.url).rstrip("/") for row in rows if row.name == CHAT_APP_NAME), DEFAULT_CHAT_APP_URL)


def _error_response(error: MemoriesError) -> ResponseReturnValue:
    match error:
        case NoteNameError():
            status = HTTP_BAD_REQUEST
        case NoteNotFoundError():
            status = HTTP_NOT_FOUND
        case NoteChangedError():
            status = HTTP_CONFLICT
        case _:
            status = HTTP_SERVER_ERROR
    return jsonify({"detail": str(error)}), status


def _json_body() -> Mapping[str, Any]:
    body = request.get_json(silent=True)
    return body if isinstance(body, Mapping) else {}


def build_pages_blueprint(
    static_directory: Path,
    contract_path: Path,
    notes_dir: Path,
    backup_config_path: Path,
    restic_env_path: Path,
    transcript_sources: TranscriptSources,
    registry_path: Path,
    client: httpx.Client,
) -> Blueprint:
    blueprint = Blueprint(BLUEPRINT_NAME, __name__)

    @blueprint.before_request
    def refuse_foreign_writes() -> ResponseReturnValue | None:
        if is_write_allowed(request.method, request.headers.get("Origin"), request.host, request.content_type):
            return None
        return jsonify({"detail": "writes must be JSON and come from this app's own page"}), HTTP_FORBIDDEN

    @blueprint.get("/")
    def page() -> ResponseReturnValue:
        document = static_directory / PAGE_DOCUMENT_FILENAME
        if not document.is_file():
            return Response(_NOT_BUILT_PAGE, mimetype="text/html", headers={"Cache-Control": "no-store"})
        response = send_file(document.absolute(), mimetype="text/html")
        response.headers["Cache-Control"] = "no-store"
        return response

    @blueprint.get("/assets/<path:filename>")
    def asset(filename: str) -> ResponseReturnValue:
        try:
            return send_from_directory((static_directory / "assets").absolute(), filename)
        except NotFound:
            return Response(status=HTTP_NOT_FOUND)

    @blueprint.get(HEALTH_PATH)
    def health() -> ResponseReturnValue:
        return jsonify({"status": "ok", "is_frontend_built": (static_directory / PAGE_DOCUMENT_FILENAME).is_file()})

    @blueprint.get(NOTES_PATH)
    def notes() -> ResponseReturnValue:
        attributions, messages = read_attributions(transcript_sources, client, _chat_app_url(registry_path))
        payload = {
            "notes_dir": str(notes_dir),
            "index_path": str(notes_dir / INDEX_FILENAME),
            "backups": read_backup_retention(backup_config_path, restic_env_path).model_dump(mode="json"),
            "notes": [
                {
                    **note.model_dump(mode="json"),
                    "attribution": attributions[note.file_name].model_dump(mode="json")
                    if note.file_name in attributions
                    else None,
                }
                for note in list_notes(notes_dir)
            ],
            "messages": list(messages),
        }
        response = jsonify(payload)
        response.headers["Cache-Control"] = "no-store"
        return response

    @blueprint.put(f"{NOTES_PATH}/<file_name>")
    def correct(file_name: str) -> ResponseReturnValue:
        body = _json_body()
        description = str(body.get("description", "")).strip()
        if not description:
            return jsonify({"detail": "a note needs a summary"}), HTTP_BAD_REQUEST
        try:
            note = update_note(
                notes_dir, file_name, description, str(body.get("body", "")), str(body.get("version", ""))
            )
        except MemoriesError as e:
            return _error_response(e)
        return jsonify(note.model_dump(mode="json"))

    @blueprint.delete(f"{NOTES_PATH}/<file_name>")
    def delete(file_name: str) -> ResponseReturnValue:
        try:
            delete_note(notes_dir, file_name, str(_json_body().get("version", "")))
        except MemoriesError as e:
            return _error_response(e)
        return jsonify({"file_name": file_name})

    @blueprint.get(APP_CONTRACT_ROUTE)
    def app_contract() -> ResponseReturnValue:
        if not contract_path.is_file():
            return (
                jsonify({"detail": f"the workspace shell's frontend is not built: {contract_path} is missing"}),
                HTTP_NOT_FOUND,
            )
        return send_file(contract_path.absolute(), mimetype="text/javascript")

    return blueprint
