"""HTTP endpoints for the agent's memory changes: the cards the chat shows, and their Undo.

Claude's change-log hook (``system/scripts/agent_memory_change_log.py``) records, per turn,
what the agent changed in each note (with that note's index line) as one change through the
``agent_memory.changes`` library, naming the tool calls that made it; the index's other lines
are a change of their own. The chat page lists a
chat's changes, draws a card at the tool call that made each one (the changed lines and an
Undo button), and undoes one through the POST.

The changes live in the main checkout's data whatever tree the app runs from, so a preview
chat shows and undoes the real ones: an Undo there is the user's real Undo.
"""

from __future__ import annotations

from typing import Any
from typing import Final

from agent_memory.changes import INDEX_FILE_NAME
from agent_memory.changes import ChangeLog
from agent_memory.changes import ChangeNotFoundError
from agent_memory.changes import ChangedSinceError
from agent_memory.changes import default_log
from agent_memory.changes import describe
from agent_memory.changes import main_checkout
from agent_memory.changes import note_title
from agent_memory.changes import own_repo_root
from agent_memory.notes import index_titles
from agent_memory.notes import readable_text
from app_manifest.registry import read_registry
from app_manifest.registry_location import registry_path
from app_schema.api_schema import RouteModels
from app_schema.api_schema import RouteTable
from app_schema.routes import error_response
from app_schema.routes import model_response
from app_schema.routes import parse_request_body
from flask import Flask
from flask import Response
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel


class ChangedMemoryFile(FrozenModel):
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
    details_url: str | None = Field(
        description="Where the Memory app shows this note in full, or null (the index, a deleted note)"
    )


class MemoryChange(FrozenModel):
    """What one agent turn changed in one note (with its index line), or in the index's other lines, undone as one."""

    id: str = Field(description="The change's id")
    time: str = Field(description="When the turn first changed memory (ISO 8601, UTC)")
    summary: str = Field(description="What changed, in one line, e.g. \"added note 'tea' and its index line\"")
    chat_id: str = Field(description="The chat whose agent made it")
    tool_use_ids: tuple[str, ...] = Field(
        description="The tool calls that made it, in order; the card sits at the last"
    )
    files: tuple[ChangedMemoryFile, ...] = Field(
        description="Every file it changed; for the index, only this change's own lines (the whole index in a record from before changes were split per note)"
    )
    undone_at: str | None = Field(description="When it was undone, or null")
    details_url: str | None = Field(
        description="Where the Memory app shows the change's (first) note in full, or the app itself"
    )


class MemoryChangesListing(FrozenModel):
    """A chat's memory changes, oldest first."""

    changes: tuple[MemoryChange, ...] = Field(description="The changes")


class UndoMemoryChangeBody(FrozenModel):
    """How to undo a memory change."""

    force: bool = Field(default=False, description="Undo even though a file was changed again since")


def _log() -> ChangeLog:
    return default_log()


def memory_app_url() -> str | None:
    """The Memory app's address as the workspace routes it to the app's window (its ``<label>.localhost`` host)."""
    path = registry_path()
    if not path.is_absolute() and not path.exists():
        # A preview runs from an editing worktree, which has no registry of its own.
        path = main_checkout(own_repo_root()) / path
    try:
        rows = read_registry(path)
    except (OSError, ValueError):
        return None
    for row in rows:
        if row.name == "memory" and row.label:
            return f"http://{row.label}.localhost/"
    return None


def _file_model(item: dict[str, Any], titles: dict[str, str], app_url: str | None) -> ChangedMemoryFile:
    name = item["name"]
    before, after = item.get("before"), item.get("after")
    is_index = name == INDEX_FILE_NAME
    title = titles.get(name) or note_title(name)
    verb = "New note" if before is None else "Deleted note" if after is None else "Changed note"
    note_id = "" if is_index else name.removesuffix(".md")
    return ChangedMemoryFile(
        name=name,
        before=before,
        after=after,
        label="Your index (the list of notes I read at the start of every chat)" if is_index else f"{verb}: {title}",
        before_text=readable_text(name, before),
        after_text=readable_text(name, after),
        note_id=note_id,
        details_url=f"{app_url}?note={note_id}" if app_url and note_id and after is not None else None,
    )


def _model(change: dict[str, Any]) -> MemoryChange:
    files = change.get("files", [])
    index = next((f for f in files if f["name"] == INDEX_FILE_NAME), None)
    titles = {**index_titles(index["before"] if index else None), **index_titles(index["after"] if index else None)}
    app_url = memory_app_url()
    file_models = tuple(_file_model(f, titles, app_url) for f in files)
    # A record of one note's index line alone still opens that note, when it is still there.
    note = change.get("note")
    note_url = (
        f"{app_url}?note={note.removesuffix('.md')}"
        if app_url and note and (_log().memory_dir / note).is_file()
        else None
    )
    return MemoryChange(
        id=change["id"],
        time=change.get("time", ""),
        summary=change.get("summary") or describe(files, note),
        chat_id=change.get("chat_id", ""),
        tool_use_ids=tuple(change.get("tool_use_ids", [])),
        files=file_models,
        undone_at=change.get("undone_at"),
        details_url=next((f.details_url for f in file_models if f.details_url), note_url or app_url),
    )


def list_changes(chat_id: str) -> Response:
    """The memory changes this chat's agent made, oldest first, each with the text before and after."""
    changes = [c for c in _log().list_changes() if c.get("chat_id") == chat_id]
    return model_response(MemoryChangesListing(changes=tuple(_model(c) for c in changes)))


def get_change(change_id: str) -> Response:
    """One memory change, with the text before and after: what its card in the chat shows."""
    try:
        return model_response(_model(_log().get(change_id)))
    except ChangeNotFoundError:
        return error_response("That memory change is not in the record.", 404)


def undo_change(change_id: str) -> Response:
    """Undo a memory change: put every file it touched back as it was before.

    Refused with 409 when one of those files was changed again since, unless ``force``.
    """
    body = parse_request_body(UndoMemoryChangeBody)
    try:
        return model_response(_model(_log().undo(change_id, force=body.force)))
    except ChangeNotFoundError:
        return error_response("That memory change is not in the record.", 404)
    except ChangedSinceError as e:
        names = ", ".join("the index" if n == "MEMORY.md" else n for n in e.names)
        return error_response(f"Changed again since: {names}. Undoing now would also lose those later edits.", 409)


ROUTE_TABLE: Final[RouteTable] = {
    ("GET", "/api/chats/{chat_id}/memory-changes"): RouteModels(response_model=MemoryChangesListing),
    ("GET", "/api/memory-changes/{change_id}"): RouteModels(response_model=MemoryChange),
    ("POST", "/api/memory-changes/{change_id}/undo"): RouteModels(
        request_model=UndoMemoryChangeBody, response_model=MemoryChange
    ),
}


def register_routes(application: Flask) -> None:
    """Wire the memory-change routes onto the Flask application."""
    application.add_url_rule("/api/chats/<chat_id>/memory-changes", view_func=list_changes, methods=["GET"])
    application.add_url_rule("/api/memory-changes/<change_id>", view_func=get_change, methods=["GET"])
    application.add_url_rule("/api/memory-changes/<change_id>/undo", view_func=undo_change, methods=["POST"])
