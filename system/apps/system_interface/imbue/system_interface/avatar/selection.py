"""The workspace's selected avatar design (pinned-taskbar-entries plan section 3.5): one shared value in
``avatar_selection.json`` under the shell's state directory; absent or unusable means the default design."""

from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import ValidationError

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.system_interface.avatar.designs import DEFAULT_DESIGN_ID
from imbue.system_interface.avatar.primitives import DesignId
from imbue.system_interface.shell.state_files import STATE_FILES_LOCK
from imbue.system_interface.shell.state_files import read_json_object
from imbue.system_interface.shell.state_files import write_json_atomic

SELECTION_FILENAME: Final[str] = "avatar_selection.json"
SELECTION_FILE_VERSION: Final[int] = 1


class AvatarSelection(FrozenModel):
    """The body of ``POST /api/avatar-selection`` and the whole of the selection file (less its version)."""

    design: DesignId = Field(description="The design every client draws")


class _SelectionDocument(FrozenModel):
    """The whole of ``avatar_selection.json``."""

    version: int = Field(description="The file format version")
    design: DesignId = Field(description="The selected design")


class AvatarSelectionStore(MutableModel):
    """Reads and writes ``avatar_selection.json`` under the shell's state lock."""

    state_directory: Path = Field(frozen=True, description="The shell's state directory")

    def _path(self) -> Path:
        return self.state_directory / SELECTION_FILENAME

    def read(self) -> DesignId:
        """The selected design's id; the default when nothing usable is stored (logged)."""
        with STATE_FILES_LOCK:
            raw = read_json_object(self._path())
        if raw is None:
            return DEFAULT_DESIGN_ID
        try:
            document = _SelectionDocument.model_validate(raw)
        except ValidationError as e:
            logger.warning("Ignored an unreadable avatar selection at {}: {}", self._path(), e.errors()[0]["msg"])
            return DEFAULT_DESIGN_ID
        if document.version != SELECTION_FILE_VERSION:
            logger.warning("Ignored an avatar selection of version {!r} at {}", document.version, self._path())
            return DEFAULT_DESIGN_ID
        return document.design

    def write(self, design: DesignId) -> None:
        with STATE_FILES_LOCK:
            write_json_atomic(
                self._path(), _SelectionDocument(version=SELECTION_FILE_VERSION, design=design).model_dump(mode="json")
            )
