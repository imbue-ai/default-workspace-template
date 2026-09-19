"""Placements: one client's layout of one desktop, in ``placements/<desktop_id>/<client_id>.json``
(desktop-interface contracts.md section 4.2).

The layout file is the truth of the arrangement. A browser writes it through ``save_browser_layout`` for
the user's own gestures, with the stamp it was based on so a save over a newer file is refused; the shell
writes it through ``edit_layout`` for an open, an agent op, or a close.
"""

from collections.abc import Callable
from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import ValidationError

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.system_interface.shell.data_types import DesktopLayout
from imbue.system_interface.shell.data_types import Placement
from imbue.system_interface.shell.data_types import PlacementsEditOutcome
from imbue.system_interface.shell.desktop_document import PLACEMENTS_FILE_VERSION
from imbue.system_interface.shell.desktop_document import drop_stale_placements
from imbue.system_interface.shell.desktop_document import is_same_layout
from imbue.system_interface.shell.desktop_document import without_placement
from imbue.system_interface.shell.errors import StalePlacementsSaveError
from imbue.system_interface.shell.primitives import ClientId
from imbue.system_interface.shell.primitives import DesktopId
from imbue.system_interface.shell.primitives import WindowId
from imbue.system_interface.shell.state_files import STATE_FILES_LOCK
from imbue.system_interface.shell.state_files import read_json_object
from imbue.system_interface.shell.state_files import write_json_atomic

PLACEMENTS_DIRNAME: Final[str] = "placements"
LAYOUT_FILE_SUFFIX: Final[str] = ".json"


class StoredDesktopLayout(FrozenModel):
    """One layout file, with where it lives."""

    desktop_id: DesktopId = Field(description="The desktop the layout arranges")
    client_id: ClientId = Field(description="The client that owns it")
    layout: DesktopLayout = Field(description="The placements")


@pure
def _empty_desktop_layout() -> DesktopLayout:
    return DesktopLayout(version=PLACEMENTS_FILE_VERSION, updated_at=None, placements=())


@pure
def is_stale_placements_save(stored: DesktopLayout | None, base_updated_at: datetime | None) -> bool:
    """Whether a save based on ``base_updated_at`` would clobber a newer stored layout."""
    if stored is None or stored.updated_at is None:
        return False
    if base_updated_at is None:
        return True
    return stored.updated_at > base_updated_at.astimezone(timezone.utc)


@pure
def _stamped(layout: DesktopLayout, now: datetime) -> DesktopLayout:
    return layout.model_copy_update(to_update(layout.field_ref().updated_at, now.astimezone(timezone.utc)))


class PlacementStore(MutableModel):
    """Reads and writes ``placements/<desktop_id>/<client_id>.json`` under the shell's state lock."""

    state_directory: Path = Field(frozen=True, description="The shell's state directory")

    def _placements_dir(self) -> Path:
        return self.state_directory / PLACEMENTS_DIRNAME

    def _path(self, desktop_id: str, client_id: str) -> Path:
        return self._placements_dir() / desktop_id / f"{client_id}{LAYOUT_FILE_SUFFIX}"

    def _read_file(self, path: Path) -> DesktopLayout | None:
        raw = read_json_object(path)
        if raw is None:
            return None
        try:
            return DesktopLayout.model_validate(raw)
        except ValidationError as e:
            logger.warning("Ignored an unreadable placements file at {}: {}", path, e.errors()[0]["msg"])
            return None

    def read_layout(self, desktop_id: str, client_id: str, live_window_ids: AbstractSet[WindowId]) -> DesktopLayout:
        """The client's own layout of the desktop with stale placements dropped, else the empty layout."""
        with STATE_FILES_LOCK:
            stored = self._read_file(self._path(desktop_id, client_id))
        if stored is None:
            return _empty_desktop_layout()
        return drop_stale_placements(stored, live_window_ids)

    def _write_unlocked(self, desktop_id: str, client_id: str, layout: DesktopLayout, now: datetime) -> DesktopLayout:
        stamped = _stamped(layout, now)
        write_json_atomic(self._path(desktop_id, client_id), stamped.model_dump(mode="json"))
        return stamped

    def edit_layout(
        self,
        desktop_id: str,
        client_id: str,
        live_window_ids: AbstractSet[WindowId],
        transform: Callable[[DesktopLayout], DesktopLayout],
        now: datetime,
    ) -> PlacementsEditOutcome:
        """Apply ``transform`` to the client's layout (stale placements dropped first) and write the result when it
        changed, all under the state lock so a browser's save cannot land in between and be overwritten."""
        with STATE_FILES_LOCK:
            stored = self._read_file(self._path(desktop_id, client_id))
            current = drop_stale_placements(stored, live_window_ids) if stored is not None else _empty_desktop_layout()
            edited = transform(current)
            if stored is not None and is_same_layout(stored, edited):
                return PlacementsEditOutcome(layout=stored, is_written=False)
            written = self._write_unlocked(desktop_id, client_id, edited, now)
        return PlacementsEditOutcome(layout=written, is_written=True)

    def save_browser_layout(
        self,
        desktop_id: str,
        client_id: str,
        placements: Sequence[Placement],
        base_updated_at: datetime | None,
        live_window_ids: AbstractSet[WindowId],
        now: datetime,
    ) -> DesktopLayout | None:
        """A browser's save: refused when the stored layout is newer than the one it was based on, skipped (None)
        when it changes nothing, and otherwise written with the placements of windows the desktop no longer holds
        dropped."""
        layout = DesktopLayout(version=PLACEMENTS_FILE_VERSION, updated_at=None, placements=tuple(placements))
        accepted = drop_stale_placements(layout, live_window_ids)
        with STATE_FILES_LOCK:
            stored = self._read_file(self._path(desktop_id, client_id))
            if is_stale_placements_save(stored, base_updated_at):
                raise StalePlacementsSaveError(
                    f"the stored layout of desktop {desktop_id!r} for client {client_id!r} is newer than the one this "
                    "save was based on; fetch it again before saving"
                )
            if stored is not None and is_same_layout(stored, accepted):
                return None
            return self._write_unlocked(desktop_id, client_id, accepted, now)

    def layouts_of_desktop(self, desktop_id: str) -> list[StoredDesktopLayout]:
        """Every client's layout of one desktop."""
        stored_layouts: list[StoredDesktopLayout] = []
        with STATE_FILES_LOCK:
            desktop_dir = self._placements_dir() / desktop_id
            if not desktop_dir.is_dir():
                return []
            for path in sorted(desktop_dir.iterdir()):
                if not path.name.endswith(LAYOUT_FILE_SUFFIX):
                    continue
                layout = self._read_file(path)
                if layout is None:
                    continue
                try:
                    stored_layouts.append(
                        StoredDesktopLayout(
                            desktop_id=DesktopId(desktop_id),
                            client_id=ClientId(path.name[: -len(LAYOUT_FILE_SUFFIX)]),
                            layout=layout,
                        )
                    )
                except ValueError as e:
                    logger.warning("Skipped a placements file with an unusable name at {}: {}", path, e)
        return stored_layouts

    def drop_window_everywhere(self, desktop_id: str, window_id: WindowId, now: datetime) -> list[StoredDesktopLayout]:
        """Take a closed window's placement out of every client's layout of the desktop; returns the layouts rewritten."""
        rewritten: list[StoredDesktopLayout] = []
        with STATE_FILES_LOCK:
            for stored in self.layouts_of_desktop(desktop_id):
                edited = without_placement(stored.layout, window_id)
                if edited is stored.layout:
                    continue
                written = self._write_unlocked(desktop_id, stored.client_id, edited, now)
                rewritten.append(stored.model_copy_update(to_update(stored.field_ref().layout, written)))
        return rewritten

    def delete_desktop_layouts(self, desktop_id: str) -> None:
        """Remove every client's layout of a deleted desktop."""
        with STATE_FILES_LOCK:
            desktop_dir = self._placements_dir() / desktop_id
            if not desktop_dir.is_dir():
                return
            for path in desktop_dir.iterdir():
                if path.is_file():
                    path.unlink()
            desktop_dir.rmdir()

    def delete_client_layouts(self, client_id: ClientId) -> int:
        """Remove every layout a client owns; returns how many went."""
        removed = 0
        with STATE_FILES_LOCK:
            placements_dir = self._placements_dir()
            if not placements_dir.is_dir():
                return 0
            for desktop_dir in placements_dir.iterdir():
                path = desktop_dir / f"{client_id}{LAYOUT_FILE_SUFFIX}"
                if path.is_file():
                    path.unlink()
                    removed += 1
        return removed
