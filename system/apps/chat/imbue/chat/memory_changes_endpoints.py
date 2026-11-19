"""HTTP endpoints for the agent's memory changes: the cards the chat shows, and their Undo.

Claude's change-log hook (``system/scripts/agent_memory_change_log.py``) records, per turn,
what the agent changed in each note (with that note's index line) as one change through the
``agent_memory.changes`` library, naming the tool calls that made it; the index's other lines
are a change of their own. The chat page lists a chat's changes, draws a card at the tool call
that made each one (the changed lines and an Undo button), and undoes one through the POST.

The changes live in the main checkout's data whatever tree the app runs from, so a preview
chat shows and undoes the real ones: an Undo there is the user's real Undo.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Final

from agent_memory.changes import ChangeLog
from agent_memory.changes import ChangeNotFoundError
from agent_memory.changes import ChangedFile
from agent_memory.changes import ChangedSinceError
from agent_memory.changes import INDEX_FILE_NAME
from agent_memory.changes import MemoryChange
from agent_memory.changes import NOTE_SUFFIX
from agent_memory.changes import changed_since_detail
from agent_memory.changes import default_log
from agent_memory.changes import note_title
from agent_memory.notes import index_titles
from agent_memory.notes import readable_text
from agent_memory.primitives import MemoryChangeId
from app_manifest.primitives import AppName
from app_manifest.registry import read_origin_label
from app_manifest.registry_location import registry_path
from app_schema.api_schema import RouteModels
from app_schema.api_schema import RouteTable
from app_schema.routes import error_response
from app_schema.routes import model_response
from app_schema.routes import parse_request_body
from chat_background_tasks.markers import main_checkout
from chat_background_tasks.markers import own_repo_root
from flask import Flask
from flask import Response
from loguru import logger as _loguru_logger
from pydantic import AnyUrl
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel

logger = _loguru_logger

# The app a workspace registers to view and edit the notes; the card links each note to it.
_MEMORY_APP_NAME: Final = AppName("memory")
_INDEX_LABEL: Final = "Your index (the list of notes I read at the start of every chat)"
_NOT_FOUND_DETAIL: Final = "That memory change is not in the record."


class ChatMemoryChangedFile(FrozenModel):
    """One file a memory change touched, as saved and as a person reads it."""

    name: str = Field(description="The file's name in the memory folder")
    before: str | None = Field(description="Its text before the change, or null if the change created it")
    after: str | None = Field(description="Its text after the change, or null if the change deleted it")
    label: str = Field(description='What the file is, in words: "New note: Likes tea", "Your index"')
    before_text: str | None = Field(
        description="The text before as a person reads it: a note's summary and text, no bookkeeping"
    )
    after_text: str | None = Field(description="The text after, read the same way")
    note_id: str = Field(description="The note's id in the Memory app (its file's stem), or empty for the index")
    details_url: AnyUrl | None = Field(
        description="Where the Memory app shows this note in full, or null (the index, a deleted note)"
    )


class ChatMemoryChange(FrozenModel):
    """What one agent turn changed in one note (with its index line), or in the index's other lines, undone as one."""

    id: MemoryChangeId = Field(description="The change's id")
    time: datetime = Field(description="When the turn first changed memory (ISO 8601, UTC)")
    summary: str = Field(description="What changed, in one line, e.g. 'added the note \"Tea\" and its index line'")
    chat_id: str = Field(description="The chat whose agent made it")
    tool_use_ids: tuple[str, ...] = Field(
        description="The tool calls that made it, in order; the card sits at the last"
    )
    files: tuple[ChatMemoryChangedFile, ...] = Field(
        description="Every file it changed; for the index, only this change's own lines (the whole index in a record from before changes were split per note)"
    )
    undone_at: datetime | None = Field(description="When it was undone, or null")
    details_url: AnyUrl | None = Field(
        description="Where the Memory app shows the change's (first) note in full, or the app itself"
    )


class ChatMemoryChangesListing(FrozenModel):
    """A chat's memory changes, oldest first."""

    changes: tuple[ChatMemoryChange, ...] = Field(description="The changes")


class UndoMemoryChangeBody(FrozenModel):
    """How to undo a memory change."""

    is_forced: bool = Field(default=False, description="Undo even though a file was changed again since")


def _error_response(detail: str, status_code: int) -> Response:
    logger.warning("Returning memory-change error response ({}): {}", status_code, detail)
    return error_response(detail, status_code)


def _log() -> ChangeLog:
    return default_log(os.environ)


def _memory_app_url() -> str | None:
    """The Memory app's address as the workspace routes it to the app's window (its ``<label>.localhost`` host)."""
    path = registry_path()
    if not path.is_absolute() and not path.exists():
        # A preview runs from an editing worktree, which has no registry of its own.
        path = main_checkout(own_repo_root()) / path
    label = read_origin_label(path, _MEMORY_APP_NAME)
    return f"http://{label}.localhost/" if label else None


def _note_id(file_name: str) -> str:
    return file_name.removesuffix(NOTE_SUFFIX)


def _file_label(item: ChangedFile, title: str) -> str:
    if item.name == INDEX_FILE_NAME:
        return _INDEX_LABEL
    if item.before is None:
        return f"New note: {title}"
    if item.after is None:
        return f"Deleted note: {title}"
    return f"Changed note: {title}"


def _file_model(item: ChangedFile, title_by_file: Mapping[str, str], app_url: str | None) -> ChatMemoryChangedFile:
    is_index = item.name == INDEX_FILE_NAME
    note_id = "" if is_index else _note_id(item.name)
    is_linked = app_url is not None and bool(note_id) and item.after is not None
    return ChatMemoryChangedFile(
        name=item.name,
        before=item.before,
        after=item.after,
        label=_file_label(item, title_by_file.get(item.name) or note_title(item.name)),
        before_text=readable_text(item.name, item.before),
        after_text=readable_text(item.name, item.after),
        note_id=note_id,
        details_url=AnyUrl(f"{app_url}?note={note_id}") if is_linked else None,
    )


def _index_titles_of(files: Sequence[ChangedFile]) -> dict[str, str]:
    """The title the change's index part gives each note, before or after."""
    index = next((item for item in files if item.name == INDEX_FILE_NAME), None)
    if index is None:
        return {}
    return {**index_titles(index.before), **index_titles(index.after)}


def _model(change: MemoryChange, memory_dir: Path, app_url: str | None) -> ChatMemoryChange:
    title_by_file = _index_titles_of(change.files)
    file_models = tuple(_file_model(item, title_by_file, app_url) for item in change.files)
    # A record of one note's index line alone still opens that note, when it is still there.
    is_note_linked = app_url is not None and change.note is not None and (memory_dir / change.note).is_file()
    note_url = f"{app_url}?note={_note_id(change.note)}" if is_note_linked and change.note is not None else None
    fallback_url = note_url or app_url
    return ChatMemoryChange(
        id=MemoryChangeId(change.id),
        time=change.time,
        summary=change.summary,
        chat_id=change.chat_id,
        tool_use_ids=change.tool_use_ids,
        files=file_models,
        undone_at=change.undone_at,
        details_url=next(
            (item.details_url for item in file_models if item.details_url),
            AnyUrl(fallback_url) if fallback_url else None,
        ),
    )


def list_chat_memory_changes(chat_id: str) -> Response:
    """The memory changes this chat's agent made, oldest first, each with the text before and after."""
    log = _log()
    app_url = _memory_app_url()
    changes = tuple(
        _model(change, log.memory_dir, app_url) for change in log.list_changes() if change.chat_id == chat_id
    )
    return model_response(ChatMemoryChangesListing(changes=changes))


def get_chat_memory_change(change_id: str) -> Response:
    """One memory change, with the text before and after: what its card in the chat shows."""
    log = _log()
    try:
        change = log.get(change_id)
    except ChangeNotFoundError:
        return _error_response(_NOT_FOUND_DETAIL, 404)
    return model_response(_model(change, log.memory_dir, _memory_app_url()))


def undo_chat_memory_change(change_id: str) -> Response:
    """Undo a memory change: put every file it touched back as it was before.

    Refused with 409 when one of those files was changed again since, unless ``is_forced``.
    """
    body = parse_request_body(UndoMemoryChangeBody)
    log = _log()
    try:
        change = log.undo(change_id, is_forced=body.is_forced)
    except ChangeNotFoundError:
        return _error_response(_NOT_FOUND_DETAIL, 404)
    except ChangedSinceError as e:
        return _error_response(changed_since_detail(e), 409)
    return model_response(_model(change, log.memory_dir, _memory_app_url()))


ROUTE_TABLE: Final[RouteTable] = {
    ("GET", "/api/chats/{chat_id}/memory-changes"): RouteModels(response_model=ChatMemoryChangesListing),
    ("GET", "/api/memory-changes/{change_id}"): RouteModels(response_model=ChatMemoryChange),
    ("POST", "/api/memory-changes/{change_id}/undo"): RouteModels(
        request_model=UndoMemoryChangeBody, response_model=ChatMemoryChange
    ),
}


def register_routes(application: Flask) -> None:
    """Wire the memory-change routes onto the Flask application."""
    application.add_url_rule(
        "/api/chats/<chat_id>/memory-changes", view_func=list_chat_memory_changes, methods=["GET"]
    )
    application.add_url_rule("/api/memory-changes/<change_id>", view_func=get_chat_memory_change, methods=["GET"])
    application.add_url_rule(
        "/api/memory-changes/<change_id>/undo", view_func=undo_chat_memory_change, methods=["POST"]
    )
