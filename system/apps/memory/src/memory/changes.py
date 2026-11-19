"""The record of every change an agent made to the memory folder, and undoing one.

The records are written by ``system/scripts/agent_memory_change_log.py``, a hook that runs
after each of the agent's tool calls: one JSON file per change under ``<DATA_DIR>/changes/``
holding each changed file's text before and after (``None`` when the file did not exist).
This module only reads them, and on Undo puts the "before" texts back.
"""

from __future__ import annotations

import json
from datetime import datetime
from datetime import timezone
from pathlib import Path

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel


class ChangedFile(FrozenModel):
    """One file a change touched."""

    name: str = Field(description="The file's name in the memory folder")
    before: str | None = Field(description="Its text before the change, or null if the change created it")
    after: str | None = Field(description="Its text after the change, or null if the change deleted it")


class MemoryChange(FrozenModel):
    """One change an agent made to the memory folder in a single action."""

    id: str = Field(description="The change's id")
    time: str = Field(description="When it happened (ISO 8601, UTC)")
    summary: str = Field(description="What changed, in one line")
    chat_id: str = Field(description="The chat that made it")
    agent_name: str = Field(description="The name of the agent that made it")
    tool: str = Field(description="The kind of action that made it")
    files: tuple[ChangedFile, ...] = Field(description="Every file it changed")
    undone_at: str | None = Field(description="When it was undone, or null")


class ChangeNotFoundError(Exception):
    pass


class ChangedSinceError(Exception):
    """A file the change touched has been changed again since; undoing would lose that."""

    def __init__(self, names: list[str]) -> None:
        super().__init__(", ".join(names))
        self.names = names


class ChangeLog:
    def __init__(self, changes_dir: Path, notes_dir: Path) -> None:
        self.changes_dir = changes_dir
        self.notes_dir = notes_dir

    def _path(self, change_id: str) -> Path:
        if not change_id or not all(c.isalnum() or c in "-_" for c in change_id):
            raise ChangeNotFoundError(change_id)
        return self.changes_dir / f"{change_id}.json"

    def _read(self, path: Path) -> MemoryChange:
        raw = json.loads(path.read_text())
        return MemoryChange(
            id=raw["id"],
            time=raw.get("time", ""),
            summary=raw.get("summary", ""),
            chat_id=raw.get("chat_id", ""),
            agent_name=raw.get("agent_name", ""),
            tool=raw.get("tool", ""),
            files=tuple(ChangedFile(**f) for f in raw.get("files", [])),
            undone_at=raw.get("undone_at"),
        )

    def list_changes(self) -> list[MemoryChange]:
        """Every change, newest first."""
        if not self.changes_dir.is_dir():
            return []
        changes = []
        for path in self.changes_dir.glob("*.json"):
            try:
                changes.append(self._read(path))
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return sorted(changes, key=lambda c: c.id, reverse=True)

    def get(self, change_id: str) -> MemoryChange:
        path = self._path(change_id)
        if not path.is_file():
            raise ChangeNotFoundError(change_id)
        return self._read(path)

    def _current(self, name: str) -> str | None:
        path = self.notes_dir / name
        return path.read_text(errors="replace") if path.is_file() else None

    def undo(self, change_id: str, force: bool) -> MemoryChange:
        """Put back every file the change touched as it was before it.

        Refused with ChangedSinceError when a file no longer holds what the change left
        there (it was changed again later), unless ``force``.
        """
        change = self.get(change_id)
        if change.undone_at is not None:
            return change
        moved_on = [f.name for f in change.files if self._current(f.name) != f.after]
        if moved_on and not force:
            raise ChangedSinceError(moved_on)
        for item in change.files:
            target = self.notes_dir / Path(item.name).name
            if item.before is None:
                target.unlink(missing_ok=True)
            else:
                self.notes_dir.mkdir(parents=True, exist_ok=True)
                tmp = target.with_name(f".{target.name}.undo")
                tmp.write_text(item.before)
                tmp.replace(target)
        path = self._path(change_id)
        raw = json.loads(path.read_text())
        raw["undone_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(raw, indent=2))
        tmp.replace(path)
        return self._read(path)
