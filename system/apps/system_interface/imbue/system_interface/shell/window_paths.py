"""Window paths: one client's paths and titles for the independent windows it has driven, in
``window_paths/<client_id>.json`` (pinned-taskbar-entries plan section 5.1).

An independent window's shared record keeps its home path forever; what each client's page reported is stored
here, beside the client's layouts rather than in them, so a browser's layout save is never made stale by a
location report. Entries naming a window no desktop holds any more are dropped on read, and the file goes with
the client when the client is pruned.
"""

from collections.abc import Set as AbstractSet
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import ValidationError

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.system_interface.shell.data_types import StoredWindowPath
from imbue.system_interface.shell.primitives import ClientId
from imbue.system_interface.shell.primitives import WindowId
from imbue.system_interface.shell.state_files import STATE_FILES_LOCK
from imbue.system_interface.shell.state_files import read_json_object
from imbue.system_interface.shell.state_files import write_json_atomic

WINDOW_PATHS_DIRNAME: Final[str] = "window_paths"
WINDOW_PATHS_FILE_VERSION: Final[int] = 1
_FILE_SUFFIX: Final[str] = ".json"


class WindowPathsDocument(FrozenModel):
    """The whole of one client's ``window_paths/<client_id>.json``."""

    version: int = Field(description="The file format version")
    windows: dict[str, StoredWindowPath] = Field(description="The client's path and title by window id")


class WindowPathStore(MutableModel):
    """Reads and writes ``window_paths/<client_id>.json`` under the shell's state lock."""

    state_directory: Path = Field(frozen=True, description="The shell's state directory")

    def _path(self, client_id: str) -> Path:
        return self.state_directory / WINDOW_PATHS_DIRNAME / f"{client_id}{_FILE_SUFFIX}"

    def _read_unlocked(self, client_id: str) -> WindowPathsDocument:
        raw = read_json_object(self._path(client_id))
        if raw is None:
            return WindowPathsDocument(version=WINDOW_PATHS_FILE_VERSION, windows={})
        try:
            document = WindowPathsDocument.model_validate(raw)
        except ValidationError as e:
            logger.warning(
                "Ignored an unreadable window paths file at {}: {}", self._path(client_id), e.errors()[0]["msg"]
            )
            return WindowPathsDocument(version=WINDOW_PATHS_FILE_VERSION, windows={})
        if document.version != WINDOW_PATHS_FILE_VERSION:
            logger.warning(
                "Ignored a window paths file of version {!r} at {}", document.version, self._path(client_id)
            )
            return WindowPathsDocument(version=WINDOW_PATHS_FILE_VERSION, windows={})
        return document

    def read_paths(self, client_id: str, live_window_ids: AbstractSet[WindowId]) -> dict[WindowId, StoredWindowPath]:
        """The client's stored paths for the windows that still exist, by window id."""
        with STATE_FILES_LOCK:
            document = self._read_unlocked(client_id)
        paths: dict[WindowId, StoredWindowPath] = {}
        for raw_window_id, stored in document.windows.items():
            try:
                window_id = WindowId(raw_window_id)
            except ValueError as e:
                logger.warning("Skipped an unusable window id {!r} in a window paths file: {}", raw_window_id, e)
                continue
            if window_id in live_window_ids:
                paths[window_id] = stored
        return paths

    def set_path(
        self,
        client_id: ClientId,
        window_id: WindowId,
        stored: StoredWindowPath,
        live_window_ids: AbstractSet[WindowId],
    ) -> bool:
        """Store the client's path and title for a window, dropping entries of windows since gone; answers whether
        anything was written (a report that changes nothing writes nothing)."""
        with STATE_FILES_LOCK:
            document = self._read_unlocked(client_id)
            kept = {
                raw_window_id: entry
                for raw_window_id, entry in document.windows.items()
                if raw_window_id in live_window_ids
            }
            if kept.get(str(window_id)) == stored and kept == document.windows:
                return False
            kept[str(window_id)] = stored
            write_json_atomic(
                self._path(client_id),
                WindowPathsDocument(version=WINDOW_PATHS_FILE_VERSION, windows=kept).model_dump(mode="json"),
            )
        return True

    def delete_client_paths(self, client_id: ClientId) -> bool:
        """Remove the client's file; answers whether there was one."""
        with STATE_FILES_LOCK:
            path = self._path(client_id)
            if not path.is_file():
                return False
            path.unlink()
        return True
