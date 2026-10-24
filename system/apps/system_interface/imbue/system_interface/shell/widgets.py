"""Which widgets the workspace shows (app-widgets plan section 3.2): one shared switch per app declaring a widget,
stored in ``widgets.json`` under the shell's state directory, else the manifest's ``default_enabled``.

A stored value for an app that no longer declares a widget is kept, so a widget withdrawn and restored keeps the
user's choice, and is ignored meanwhile.
"""

from collections.abc import Iterable
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from app_manifest.primitives import AppName
from app_manifest.registry import RegistryRow
from pydantic import Field
from workspace_layout.answers import WidgetState

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.system_interface.shell.state_files import STATE_FILES_LOCK
from imbue.system_interface.shell.state_files import parse_versioned_document
from imbue.system_interface.shell.state_files import read_json_object
from imbue.system_interface.shell.state_files import write_json_atomic

WIDGETS_ROUTE: Final[str] = "/api/widgets"
WIDGETS_FILENAME: Final[str] = "widgets.json"
WIDGETS_FILE_VERSION: Final[int] = 1


class WidgetEnabledRequest(FrozenModel):
    """The body of ``POST /api/widgets/<app>``."""

    is_enabled: bool = Field(strict=True, description="Whether the workspace shows the app's widget")


class _StoredWidget(FrozenModel):
    """One entry of the ``apps`` map (the app name is the key)."""

    is_enabled: bool = Field(description="The workspace's choice for the app's widget")


class _WidgetsDocument(FrozenModel):
    """The whole of ``widgets.json``."""

    version: int = Field(description="The file format version")
    apps: dict[str, _StoredWidget] = Field(default_factory=dict, description="Each stored choice, by app name")


@pure
def widget_states(rows: Iterable[RegistryRow], stored: Mapping[str, bool]) -> tuple[WidgetState, ...]:
    """The effective switch of every registered, non-internal app declaring a widget, in registry order."""
    states: list[WidgetState] = []
    for row in rows:
        if row.widget is not None and not row.internal:
            states.append(WidgetState(app=row.name, is_enabled=stored.get(str(row.name), row.widget.default_enabled)))
    return tuple(states)


@pure
def enabled_widget_apps(rows: Iterable[RegistryRow], stored: Mapping[str, bool]) -> frozenset[str]:
    """The names of the apps whose widget the workspace shows."""
    return frozenset(str(state.app) for state in widget_states(rows, stored) if state.is_enabled)


@pure
def declares_widget(rows: Iterable[RegistryRow], app: AppName) -> bool:
    """Whether ``app`` is a registered, non-internal app declaring a widget."""
    return any(state.app == app for state in widget_states(rows, {}))


class WidgetsStore(MutableModel):
    """Reads and writes ``widgets.json`` under the shell's state lock."""

    state_directory: Path = Field(frozen=True, description="The shell's state directory")

    def _path(self) -> Path:
        return self.state_directory / WIDGETS_FILENAME

    def _read_unlocked(self) -> _WidgetsDocument:
        path = self._path()
        document = parse_versioned_document(read_json_object(path), _WidgetsDocument, WIDGETS_FILE_VERSION, path)
        return document if document is not None else _WidgetsDocument(version=WIDGETS_FILE_VERSION)

    def read(self) -> dict[str, bool]:
        """Every stored choice, by app name; empty when nothing usable is stored (logged)."""
        with STATE_FILES_LOCK:
            document = self._read_unlocked()
        return {app: widget.is_enabled for app, widget in document.apps.items()}

    def write(self, app: AppName, is_enabled: bool) -> None:
        """Store the workspace's choice for one app's widget, keeping every other choice."""
        with STATE_FILES_LOCK:
            document = self._read_unlocked()
            apps = {**document.apps, str(app): _StoredWidget(is_enabled=is_enabled)}
            write_json_atomic(
                self._path(),
                _WidgetsDocument(version=WIDGETS_FILE_VERSION, apps=apps).model_dump(mode="json"),
            )
