"""The app's routes on its own origin: the page, the health probe, the notes, correcting and deleting a note, and
the contract module. Everything is read when asked; the app keeps nothing between requests but the record of the
user's changes (``changes``), which chats read."""

import threading
from collections.abc import Callable
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Final

import httpx
from flask import Blueprint
from flask import Response
from flask import jsonify
from flask import request
from flask import send_file
from flask import send_from_directory
from flask.typing import ResponseReturnValue
from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import ValidationError
from werkzeug.exceptions import NotFound

from app_manifest.errors import RegistryReadError
from app_manifest.manifest import describe_validation_error
from app_manifest.registry import APP_CONTRACT_ROUTE
from app_manifest.registry import read_registry
from imbue.imbue_common.frozen_model import FrozenModel
from memories.attribution import DEFAULT_CHAT_APP_URL
from memories.attribution import TranscriptSources
from memories.attribution import read_attributions
from memories.backups import read_backup_retention
from memories.changes import NoteChangeKind
from memories.changes import record_note_change
from memories.errors import MemoriesError
from memories.errors import NoteChangedError
from memories.errors import NoteNameError
from memories.errors import NoteNotFoundError
from memories.errors import NoteWriteError
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


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class NoteCorrection(FrozenModel):
    """A PUT's body: the corrected summary and text, and the version of the note they correct."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    description: str = Field(description="The new one-line summary")
    body: str = Field(description="The new text after the frontmatter")
    version: str = Field(description="The note's version the correction was made to")


class NoteDeletion(FrozenModel):
    """A DELETE's body: the version of the note the user chose to delete."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(description="The note's version the user saw")


def _chat_app_url(registry_path: Path) -> str:
    try:
        rows = read_registry(registry_path)
    except RegistryReadError as e:
        logger.debug("Could not read the app registry for the chat app's URL; using the default: {}", e)
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


def _request_model[RequestModelT: FrozenModel](model: type[RequestModelT]) -> RequestModelT | ResponseReturnValue:
    """The request's JSON body as ``model``, or a 400 naming what is wrong with it."""
    try:
        return model.model_validate(request.get_json(silent=True))
    except ValidationError as e:
        fields = ", ".join(model.model_fields)
        return jsonify(
            {"detail": f"expected a JSON object with {fields}: {describe_validation_error(e)}"}
        ), HTTP_BAD_REQUEST


def build_pages_blueprint(
    static_directory: Path,
    contract_path: Path,
    notes_dir: Path,
    backup_config_path: Path,
    restic_env_path: Path,
    transcript_sources: TranscriptSources,
    registry_path: Path,
    client: httpx.Client,
    changes_path: Path,
    now: Callable[[], datetime],
) -> Blueprint:
    blueprint = Blueprint(BLUEPRINT_NAME, __name__)
    # Serializes the note writes and the change record's read-append-write: the server answers on several threads.
    changes_lock = threading.Lock()

    def record_or_explain(file_name: str, change: NoteChangeKind) -> ResponseReturnValue | None:
        """Record a change that already happened; when that fails, the answer says so instead of claiming success."""
        try:
            record_note_change(changes_path, file_name, change, now())
        except NoteWriteError as e:
            logger.opt(exception=e).error("Could not record that {} was {}", file_name, change.lower())
            return jsonify(
                {
                    "detail": f"{file_name} was {change.lower()}, but open chats could not be told, so they may undo it: {e}"
                }
            ), HTTP_SERVER_ERROR
        return None

    @blueprint.before_request
    def refuse_foreign_writes() -> ResponseReturnValue | None:
        if is_write_allowed(request.method, request.headers.get("Sec-Fetch-Site"), request.content_type):
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
        listing = list_notes(notes_dir)
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
                for note in listing.notes
            ],
            "messages": [
                *messages,
                *(f"{file_name} could not be read, so it isn't shown." for file_name in listing.unreadable_file_names),
            ],
        }
        response = jsonify(payload)
        response.headers["Cache-Control"] = "no-store"
        return response

    @blueprint.put(f"{NOTES_PATH}/<file_name>")
    def correct(file_name: str) -> ResponseReturnValue:
        correction = _request_model(NoteCorrection)
        if not isinstance(correction, NoteCorrection):
            return correction
        description = correction.description.strip()
        if not description:
            return jsonify({"detail": "a note needs a summary"}), HTTP_BAD_REQUEST
        with changes_lock:
            try:
                note = update_note(notes_dir, file_name, description, correction.body, correction.version)
            except MemoriesError as e:
                return _error_response(e)
            unrecorded = record_or_explain(file_name, NoteChangeKind.EDITED)
        if unrecorded is not None:
            return unrecorded
        return jsonify(note.model_dump(mode="json"))

    @blueprint.delete(f"{NOTES_PATH}/<file_name>")
    def delete(file_name: str) -> ResponseReturnValue:
        deletion = _request_model(NoteDeletion)
        if not isinstance(deletion, NoteDeletion):
            return deletion
        with changes_lock:
            try:
                delete_note(notes_dir, file_name, deletion.version)
            except MemoriesError as e:
                return _error_response(e)
            unrecorded = record_or_explain(file_name, NoteChangeKind.DELETED)
        if unrecorded is not None:
            return unrecorded
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
