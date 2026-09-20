"""Test helpers for the shell subpackage: registry rows and files, an inventory over them, and desktop records."""

import json
import queue
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Final

from app_manifest.primitives import AppName
from flask import Flask

from imbue.system_interface.server import create_application
from imbue.system_interface.shell.data_types import Desktop
from imbue.system_interface.shell.data_types import Window
from imbue.system_interface.shell.data_types import WindowPlacement
from imbue.system_interface.shell.desktop_document import cascade_frame
from imbue.system_interface.shell.inventory import AppInventory
from imbue.system_interface.shell.primitives import DesktopId
from imbue.system_interface.shell.primitives import SharingMode
from imbue.system_interface.shell.primitives import WindowId
from imbue.system_interface.shell.primitives import WindowPath
from imbue.system_interface.shell.primitives import WindowState
from imbue.system_interface.shell.primitives import WindowTitle
from imbue.system_interface.testing import build_test_state
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster

# The one clock the shell tests stamp records with.
TEST_NOW: Final[datetime] = datetime(2026, 9, 4, tzinfo=timezone.utc)
# The URL of the supervised ``terminal`` row of ``write_two_app_registry``, which declares a launch path.
TEST_TERMINAL_URL: Final[str] = "http://localhost:7681"
# The URL of the ``files`` row of ``write_two_app_registry``, which declares none.
TEST_FILES_URL: Final[str] = "http://localhost:7000"


def registry_row_toml(
    name: str,
    url: str,
    program: str | None = None,
    is_critical: bool = False,
    is_internal: bool = False,
    default_shortcut: tuple[str, str] | None = None,
    display_name: str | None = None,
    label: str = "",
    launcher_rank: int | None = None,
    # Each launch path as ``(id, label, path)``; ``launch_params`` names each one's param names by id.
    launch_paths: Sequence[tuple[str, str, str]] = (),
    launch_params: Mapping[str, Sequence[str]] | None = None,
) -> str:
    """One ``[[apps]]`` row as ``forward_port.py`` writes it, with the manifest-derived keys the shell reads.
    ``default_shortcut`` is ``(launch, mode)``."""
    lines = [
        "[[apps]]",
        f'name = "{name}"',
        f'url = "{url}"',
        f'label = "{label}"',
        f'display_name = "{display_name if display_name is not None else name.capitalize()}"',
        f"critical = {'true' if is_critical else 'false'}",
        f"internal = {'true' if is_internal else 'false'}",
    ]
    if program is not None:
        lines.append(f'program = "{program}"')
    if launcher_rank is not None:
        lines.append(f"launcher_rank = {launcher_rank}")
    if default_shortcut is not None:
        lines.append(f'default_shortcut = {{ launch = "{default_shortcut[0]}", mode = "{default_shortcut[1]}" }}')
    for launch_id, launch_label, launch_path in launch_paths:
        lines.append("[[apps.launch_paths]]")
        lines.append(f'id = "{launch_id}"')
        lines.append(f'label = "{launch_label}"')
        lines.append(f'path = "{launch_path}"')
        params = (launch_params or {}).get(launch_id, ())
        if params:
            lines.append("params = [" + ", ".join(f'"{param}"' for param in params) + "]")
    return "\n".join(lines) + "\n"


def write_registry(path: Path, *rows: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(rows))
    return path


def write_two_app_registry(tmp_path: Path, *extra_rows: str) -> Path:
    """``apps.toml`` under ``tmp_path`` with a supervised ``terminal`` row declaring a ``new`` launch path, a
    ``files`` row declaring none, and ``extra_rows``."""
    return write_registry(
        tmp_path / "apps.toml",
        registry_row_toml(
            "terminal",
            TEST_TERMINAL_URL,
            program="terminal",
            default_shortcut=("new", "new"),
            launch_paths=[("new", "New terminal", "/new")],
        ),
        registry_row_toml("files", TEST_FILES_URL, program="files", default_shortcut=("open", "focus")),
        *extra_rows,
    )


def shell_application(tmp_path: Path, inventory: AppInventory, broadcaster: WebSocketBroadcaster) -> Flask:
    """The shell app over ``inventory``, its state under ``tmp_path``, sharing the inventory's broadcaster as in production.

    Its bundle directory is ``tmp_path / "static"``, empty until a test fills it, so no route answer depends
    on whether the frontend has been built in the checkout.
    """
    state = build_test_state(
        broadcaster=broadcaster,
        shell_state_directory=tmp_path / "state",
        inventory=inventory,
        static_directory=tmp_path / "static",
    )
    return create_application(state)


class FakeLivenessProber:
    """Answers the liveness sweep from a table; every app not in it counts as running."""

    def __init__(self) -> None:
        self.is_running_by_name: dict[str, bool] = {}
        self.call_count = 0

    def __call__(self, rows: Sequence[tuple[str, str, str]]) -> dict[str, bool]:
        self.call_count += 1
        return {name: self.is_running_by_name.get(name, True) for name, _program, _url in rows}


def build_inventory(
    registry_path: Path,
    broadcaster: WebSocketBroadcaster,
    prober: Callable[[Sequence[tuple[str, str, str]]], dict[str, bool]] | None = None,
) -> AppInventory:
    """An inventory that has read the registry and probed liveness once, with no watcher or sweep running."""
    inventory = AppInventory(
        registry_path=registry_path,
        broadcaster=broadcaster,
        liveness_prober=prober if prober is not None else FakeLivenessProber(),
    )
    inventory.reload_registry()
    inventory.refresh_liveness()
    return inventory


def drain_messages(client_queue: "queue.Queue[str | None]") -> list[dict[str, Any]]:
    """Every message a registered fake client has been sent so far, parsed."""
    messages: list[dict[str, Any]] = []
    while not client_queue.empty():
        raw = client_queue.get_nowait()
        if raw is not None:
            messages.append(json.loads(raw))
    return messages


def window_record(window_id: WindowId, app: str, path: str, is_settling: bool = False) -> Window:
    """A window record with a fixed opening time and an empty title."""
    return Window(
        id=window_id,
        app=AppName(app),
        path=WindowPath(path),
        title=WindowTitle(""),
        opened_at=TEST_NOW,
        is_settling=is_settling,
    )


def placement_record(
    window_id: WindowId, is_minimized: bool = False, state: WindowState = WindowState.NORMAL
) -> WindowPlacement:
    """A placement at the first cascade frame; shown and normal unless told otherwise."""
    return WindowPlacement(window_id=window_id, frame=cascade_frame(0), state=state, is_minimized=is_minimized)


def desktop_with_windows(*windows: Window) -> Desktop:
    """The ``home`` desktop holding ``windows`` and no shortcuts."""
    return Desktop(
        id=DesktopId("home"),
        name="Home",
        color="#2f6b4f",
        glyph=0,
        sharing=SharingMode.SHARED,
        wallpaper=None,
        shortcuts=(),
        windows=windows,
    )
