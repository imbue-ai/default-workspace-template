"""Which themeable apps the user turned theming off for (docs/system/themeable-apps.md), kept in ``app_theming.json``
under the shell's state directory."""

from pathlib import Path
from typing import Final

from app_manifest.manifest import ThemingMode
from app_manifest.primitives import AppName
from app_manifest.registry import RegistryRow
from pydantic import Field
from workspace_layout.primitives import AppTheming

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.system_interface.shell.state_files import STATE_FILES_LOCK
from imbue.system_interface.shell.state_files import parse_versioned_document
from imbue.system_interface.shell.state_files import read_json_object
from imbue.system_interface.shell.state_files import write_json_atomic

APP_THEMING_FILENAME: Final[str] = "app_theming.json"
APP_THEMING_FILE_VERSION: Final[int] = 1


class AppThemingDocument(FrozenModel):
    """The whole of ``app_theming.json``: the themeable apps whose theming the user turned off."""

    version: int = Field(description="The file format version")
    off: tuple[AppName, ...] = Field(description="The apps whose theming is off, by app name")


@pure
def is_themeable(row: RegistryRow) -> bool:
    """Whether the app is written to wear the workspace's theme over a look of its own, so the user can turn its
    theming on and off."""
    return row.theming_mode == ThemingMode.OUTSIDE


@pure
def theming_of(row: RegistryRow, off_apps: frozenset[AppName]) -> AppTheming | None:
    """The app's Theming submenu state: on or off for a themeable app, unthemed for an app that declares no
    ``[theming]`` (so it can be made themeable), and None for an app that declares another mode and has no submenu."""
    if row.theming_mode is None:
        return AppTheming.UNTHEMED
    if not is_themeable(row):
        return None
    return AppTheming.OFF if row.name in off_apps else AppTheming.ON


class AppThemingStore(MutableModel):
    """Reads and writes ``app_theming.json`` under the shell's state lock."""

    state_directory: Path = Field(frozen=True, description="The shell's state directory")

    def _path(self) -> Path:
        return self.state_directory / APP_THEMING_FILENAME

    def _read_unlocked(self) -> frozenset[AppName]:
        document = parse_versioned_document(
            read_json_object(self._path()), AppThemingDocument, APP_THEMING_FILE_VERSION, self._path()
        )
        return frozenset() if document is None else frozenset(document.off)

    def read(self) -> frozenset[AppName]:
        """The apps whose theming is off; every other themeable app has it on."""
        with STATE_FILES_LOCK:
            return self._read_unlocked()

    def set(self, app: AppName, is_on: bool) -> None:
        """Turn an app's theming on or off."""
        with STATE_FILES_LOCK:
            off = self._read_unlocked() - {app} if is_on else self._read_unlocked() | {app}
            document = AppThemingDocument(version=APP_THEMING_FILE_VERSION, off=tuple(sorted(off)))
            write_json_atomic(self._path(), document.model_dump(mode="json"))
