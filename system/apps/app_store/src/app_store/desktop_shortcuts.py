"""An installed app's desktop shortcuts: taken off every desktop when the app is uninstalled, put back on a reinstall.

The shell offers an app's default shortcut once, the first time the app registers, and never takes it away: a
shortcut whose app is gone stays on the desktop as a plain window icon with the app's internal name. So the store
takes an app's shortcuts off when it removes the app, and keeps what it took (``desktop-shortcuts.json`` in its data
folder) to put them back where they were when the app is installed again -- the shell will not offer it a second time.

The desktops are read and edited through the shell's own desktop routes (desktop contracts.md section 5), which edit
a desktop without switching anyone's screen to it. The shell being unreachable never fails an install or uninstall:
the shortcuts are then left as they are.
"""

import threading
from abc import ABC
from abc import abstractmethod
from pathlib import Path
from typing import Any
from typing import Final

import httpx
from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import ValidationError
from workspace_layout.shell_url import DESKTOPS_ROUTE
from workspace_layout.shell_url import shell_base_url

from app_store.errors import StoreStateError
from getting_started.errors import StateFileError
from getting_started.state_files import read_json_object
from getting_started.state_files import write_json_atomic
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel

KEPT_SHORTCUTS_FORMAT: Final[int] = 1
# The columns the shell places an offered shortcut over (its SEED_GRID_COLUMNS), so a shortcut put back where its
# cell is taken lands where the shell would have put it.
SHORTCUT_GRID_COLUMNS: Final[int] = 8
SHELL_TIMEOUT_SECONDS: Final[float] = 5.0


class GridCell(FrozenModel):
    column: int = Field(ge=0, description="The column, from the left")
    row: int = Field(ge=0, description="The row, from the top")


class DesktopShortcut(FrozenModel):
    """One shortcut on one desktop, as the shell's desktop routes carry it."""

    desktop_id: str = Field(description="The desktop it is on")
    app: str = Field(description="The app it opens")
    launch: str = Field(description="The app's launch path it runs")
    mode: str = Field(description="focus or new")
    cell: GridCell = Field(description="Where it sits")


class DesktopShortcutsInterface(MutableModel, ABC):
    """Reads and edits the shortcuts of every desktop."""

    @abstractmethod
    def read(self) -> dict[str, tuple[DesktopShortcut, ...]]:
        """Every desktop's id and its shortcuts; raises StoreStateError when the shell can't be read."""

    @abstractmethod
    def remove(self, shortcut: DesktopShortcut) -> None:
        """Take ``shortcut`` off its desktop; raises StoreStateError when the shell refuses."""

    @abstractmethod
    def add(self, shortcut: DesktopShortcut) -> None:
        """Put ``shortcut`` on its desktop; raises StoreStateError when the shell refuses."""


class ShellDesktopShortcuts(DesktopShortcutsInterface):
    """The production shortcuts: the shell's desktop routes, over loopback."""

    def read(self) -> dict[str, tuple[DesktopShortcut, ...]]:
        document = self._request("GET", DESKTOPS_ROUTE, None)
        desktops: dict[str, tuple[DesktopShortcut, ...]] = {}
        try:
            for desktop in document["desktops"]:
                desktops[str(desktop["id"])] = tuple(
                    DesktopShortcut(
                        desktop_id=str(desktop["id"]),
                        app=str(shortcut["target"]["app"]),
                        launch=str(shortcut["target"]["launch"]),
                        mode=str(shortcut["mode"]),
                        cell=GridCell.model_validate(shortcut["cell"]),
                    )
                    for shortcut in desktop["shortcuts"]
                    if shortcut["target"].get("kind") == "launch"
                )
        except (KeyError, TypeError, ValidationError) as e:
            raise StoreStateError(f"The desktops the shell answered are not in the shape expected: {e}") from e
        return desktops

    def remove(self, shortcut: DesktopShortcut) -> None:
        self._request(
            "POST",
            f"{DESKTOPS_ROUTE}/{shortcut.desktop_id}/shortcuts/remove",
            {"app": shortcut.app, "launch": shortcut.launch},
        )

    def add(self, shortcut: DesktopShortcut) -> None:
        self._request(
            "POST",
            f"{DESKTOPS_ROUTE}/{shortcut.desktop_id}/shortcuts",
            {
                "target": {"kind": "launch", "app": shortcut.app, "launch": shortcut.launch},
                "mode": shortcut.mode,
                "cell": shortcut.cell.model_dump(),
            },
        )

    def _request(self, method: str, route: str, body: dict[str, Any] | None) -> dict[str, Any]:
        try:
            response = httpx.request(method, f"{shell_base_url()}{route}", json=body, timeout=SHELL_TIMEOUT_SECONDS)
        except httpx.HTTPError as e:
            raise StoreStateError(f"The desktop could not be reached: {e}") from e
        if response.status_code >= 400:
            raise StoreStateError(f"The desktop refused {method} {route}: {response.status_code} {response.text}")
        try:
            answer = response.json()
        except ValueError as e:
            raise StoreStateError(f"The desktop answered {method} {route} with no JSON") from e
        return answer if isinstance(answer, dict) else {}


class KeptShortcuts(FrozenModel):
    """``desktop-shortcuts.json``: the shortcuts taken off with each uninstalled app."""

    format: int = Field(default=KEPT_SHORTCUTS_FORMAT, description="The file's format version")
    apps: dict[str, tuple[DesktopShortcut, ...]] = Field(default_factory=dict, description="Each app's shortcuts")


class AppShortcuts(MutableModel):
    """Takes an app's shortcuts off the desktops and puts them back."""

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    desktops: DesktopShortcutsInterface = Field(frozen=True, description="The desktops' shortcuts")
    path: Path = Field(frozen=True, description="desktop-shortcuts.json")

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def take_off(self, app: str) -> None:
        """Take every shortcut of ``app`` off every desktop, and keep them for a reinstall."""
        try:
            shortcuts = [
                shortcut for held in self.desktops.read().values() for shortcut in held if shortcut.app == app
            ]
        except StoreStateError as e:
            logger.warning("Left {}'s desktop shortcuts as they are: {}", app, e)
            return
        taken: list[DesktopShortcut] = []
        for shortcut in shortcuts:
            try:
                self.desktops.remove(shortcut)
            except StoreStateError as e:
                logger.warning("Could not take {}'s shortcut off desktop {}: {}", app, shortcut.desktop_id, e)
                continue
            taken.append(shortcut)
        if taken:
            with self._lock:
                kept = self._read()
                self._write(kept.apps | {app: tuple(taken)})
            logger.info("Took {} shortcut(s) of {} off the desktops", len(taken), app)

    def put_back(self, app: str) -> None:
        """Put the shortcuts taken off with ``app`` back on the desktops that still exist and lack one: in their old
        cell when it is free, else in the first free cell in reading order. While the shell can't be read they stay
        kept, for the next install."""
        with self._lock:
            shortcuts = self._read().apps.get(app, ())
        if not shortcuts:
            return
        try:
            desktops = self.desktops.read()
        except StoreStateError as e:
            logger.warning("Could not put {}'s desktop shortcuts back: {}", app, e)
            return
        self.forget(app)
        for shortcut in shortcuts:
            held = desktops.get(shortcut.desktop_id)
            if held is None or any(other.app == app and other.launch == shortcut.launch for other in held):
                continue
            placed = shortcut.model_copy(update={"cell": cell_for(shortcut.cell, {other.cell for other in held})})
            try:
                self.desktops.add(placed)
            except StoreStateError as e:
                logger.warning("Could not put {}'s shortcut back on desktop {}: {}", app, shortcut.desktop_id, e)
                continue
            desktops[shortcut.desktop_id] = (*held, placed)

    def forget(self, app: str) -> None:
        """Drop what was kept for ``app`` (it left the store)."""
        with self._lock:
            kept = self._read()
            if app in kept.apps:
                self._write({name: held for name, held in kept.apps.items() if name != app})

    def _read(self) -> KeptShortcuts:
        document = read_json_object(self.path)
        if document is None:
            return KeptShortcuts()
        try:
            return KeptShortcuts.model_validate(document)
        except ValidationError as e:
            logger.warning("Ignored the unreadable {}: {}", self.path, e)
            return KeptShortcuts()

    def _write(self, apps: dict[str, tuple[DesktopShortcut, ...]]) -> None:
        try:
            write_json_atomic(self.path, KeptShortcuts(apps=apps).model_dump(mode="json"))
        except StateFileError as e:
            logger.warning("Could not write {}: {}", self.path, e)


def cell_for(wanted: GridCell, taken: set[GridCell]) -> GridCell:
    """``wanted`` when it is free, else the first free cell in reading order over the shell's seeding grid."""
    if wanted not in taken:
        return wanted
    # One more cell than are taken: at least one of them is free.
    reading_order = (
        GridCell(column=index % SHORTCUT_GRID_COLUMNS, row=index // SHORTCUT_GRID_COLUMNS)
        for index in range(len(taken) + 1)
    )
    return next(cell for cell in reading_order if cell not in taken)
