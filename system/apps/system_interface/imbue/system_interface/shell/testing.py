"""Test helpers for the shell subpackage: registry rows and files, an inventory over them, and desktop records."""

import json
import queue
import shutil
import subprocess
from collections.abc import Callable
from collections.abc import Iterator
from collections.abc import Mapping
from collections.abc import Sequence
from contextlib import contextmanager
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Final

from app_manifest.manifest import LocationScope
from app_manifest.manifest import load_manifest
from app_manifest.primitives import AppName
from app_manifest.registry import RegistryRow
from app_manifest.registry import read_registry
from flask import Flask
from flask import request
from loguru import logger
from workspace_layout.primitives import DesktopId
from workspace_layout.primitives import WindowId

from imbue.mngr.utils.polling import wait_for
from imbue.system_interface.server import create_application
from imbue.system_interface.shell.data_types import Desktop
from imbue.system_interface.shell.data_types import DesktopsDocument
from imbue.system_interface.shell.data_types import Window
from imbue.system_interface.shell.data_types import WindowPlacement
from imbue.system_interface.shell.desktop_document import DESKTOPS_FILE_VERSION
from imbue.system_interface.shell.desktop_document import cascade_frame
from imbue.system_interface.shell.desktops import DEFAULT_SHORTCUTS_OFFERED_FILENAME
from imbue.system_interface.shell.desktops import DESKTOPS_FILENAME
from imbue.system_interface.shell.identity import IDENTITY_HEADER
from imbue.system_interface.shell.identity import RequestIdentity
from imbue.system_interface.shell.inventory import AppInventory
from imbue.system_interface.shell.launches import LaunchPoster
from imbue.system_interface.shell.primitives import WindowPath
from imbue.system_interface.shell.primitives import WindowState
from imbue.system_interface.shell.primitives import WindowTitle
from imbue.system_interface.shell.state_files import write_json_atomic
from imbue.system_interface.shell.update_notice import LAST_GOOD_RECORD_REL
from imbue.system_interface.shell.update_notice import UPDATE_SELF_SCRIPT_REL
from imbue.system_interface.testing import build_test_state
from imbue.system_interface.testing import find_free_port
from imbue.system_interface.testing import is_server_answering
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster

# The one clock the shell tests stamp records with.
TEST_NOW: Final[datetime] = datetime(2026, 9, 4, tzinfo=timezone.utc)
# The URL of the supervised ``terminal`` row of ``write_two_app_registry``, which declares a launch path.
TEST_TERMINAL_URL: Final[str] = "http://localhost:7681"
# The URL of the ``files`` row of ``write_two_app_registry``, which declares none.
TEST_FILES_URL: Final[str] = "http://localhost:7000"
# Where the ``terminal`` row of ``write_two_app_registry`` asks to be told a window of its closed.
TEST_TERMINAL_WINDOW_CLOSED_PATH: Final[str] = "/api/window-closed"


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
    # Each launch path as ``(id, label, path)``; ``launch_params`` names each one's param names by id,
    # ``launch_text_params`` the param of each that takes typed text, ``launch_draft_params`` the param of each
    # that takes drafted text, ``launch_methods`` the method of each that is not a GET, and ``launch_presets``
    # the presets of each that declares any.
    launch_paths: Sequence[tuple[str, str, str]] = (),
    launch_params: Mapping[str, Sequence[str]] | None = None,
    launch_text_params: Mapping[str, str] | None = None,
    launch_draft_params: Mapping[str, str] | None = None,
    launch_methods: Mapping[str, str] | None = None,
    launch_presets: Mapping[str, Mapping[str, str]] | None = None,
    # The ``[pin]`` table as ``(path, style, scope, default_mode)``.
    pin: tuple[str, str, str, str] | None = None,
    window_closed_path: str | None = None,
    stop_when_no_windows: bool = False,
    # Each message handler as ``(type, path)``.
    message_handlers: Sequence[tuple[str, str]] = (),
    # Each ``show`` message handler as ``(type, show, showing)``.
    shown_message_handlers: Sequence[tuple[str, str, Sequence[str]]] = (),
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
        f"stop_when_no_windows = {'true' if stop_when_no_windows else 'false'}",
    ]
    if program is not None:
        lines.append(f'program = "{program}"')
    if launcher_rank is not None:
        lines.append(f"launcher_rank = {launcher_rank}")
    if default_shortcut is not None:
        lines.append(f'default_shortcut = {{ launch = "{default_shortcut[0]}", mode = "{default_shortcut[1]}" }}')
    if pin is not None:
        path, style, scope, default_mode = pin
        lines.append(
            f'pin = {{ path = "{path}", style = "{style}", scope = "{scope}", default_mode = "{default_mode}" }}'
        )
    if window_closed_path is not None:
        lines.append(f'window_closed_path = "{window_closed_path}"')
    if message_handlers or shown_message_handlers:
        handlers = [f'{{ type = "{kind}", path = "{path}" }}' for kind, path in message_handlers]
        for kind, show, showing in shown_message_handlers:
            pages = ", ".join(f'"{page}"' for page in showing)
            handlers.append(f'{{ type = "{kind}", show = "{show}", showing = [{pages}] }}')
        lines.append(f"message_handlers = [{', '.join(handlers)}]")
    for launch_id, launch_label, launch_path in launch_paths:
        lines.append("[[apps.launch_paths]]")
        lines.append(f'id = "{launch_id}"')
        lines.append(f'label = "{launch_label}"')
        lines.append(f'path = "{launch_path}"')
        params = (launch_params or {}).get(launch_id, ())
        if params:
            lines.append("params = [" + ", ".join(f'"{param}"' for param in params) + "]")
        text_param = (launch_text_params or {}).get(launch_id)
        if text_param is not None:
            lines.append(f'text_param = "{text_param}"')
        draft_param = (launch_draft_params or {}).get(launch_id)
        if draft_param is not None:
            lines.append(f'draft_param = "{draft_param}"')
        method = (launch_methods or {}).get(launch_id)
        if method is not None:
            lines.append(f'method = "{method}"')
        presets = (launch_presets or {}).get(launch_id)
        if presets:
            lines.append("presets = {" + ", ".join(f'{name} = "{value}"' for name, value in presets.items()) + "}")
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
            launch_params={"new": ["workdir"]},
            window_closed_path=TEST_TERMINAL_WINDOW_CLOSED_PATH,
        ),
        registry_row_toml("files", TEST_FILES_URL, program="files", default_shortcut=("open", "focus")),
        *extra_rows,
    )


# The apps the built-in rows seed a desktop with, in launcher order: without the chat, and with it.
BUILTIN_SHORTCUT_APPS_BEFORE_CHAT: Final[tuple[str, ...]] = ("getting-started", "files", "browser", "terminal")
BUILTIN_SHORTCUT_APPS_WITH_CHAT: Final[tuple[str, ...]] = ("chat", *BUILTIN_SHORTCUT_APPS_BEFORE_CHAT)


def builtin_rows_toml_before_chat() -> tuple[str, ...]:
    """The rows of the built-in apps that register before the chat, shaped as their manifests make them: the shell
    (internal), Getting Started, the file viewer, the browser, and the terminal."""
    return (
        registry_row_toml("system_interface", "http://localhost:8000", is_internal=True, is_critical=True),
        registry_row_toml(
            "getting-started",
            "http://localhost:7400",
            display_name="Getting Started",
            launcher_rank=15,
            default_shortcut=("open", "focus"),
        ),
        registry_row_toml(
            "files",
            TEST_FILES_URL,
            display_name="File Viewer",
            launcher_rank=20,
            default_shortcut=("new", "new"),
            launch_paths=[("new", "File Viewer", "/home/user/workspace/")],
        ),
        registry_row_toml(
            "browser",
            "http://localhost:7500",
            launcher_rank=30,
            default_shortcut=("new", "focus"),
            launch_paths=[("new", "Browser", "/new")],
            launch_methods={"new": "POST"},
        ),
        registry_row_toml(
            "terminal",
            TEST_TERMINAL_URL,
            is_critical=True,
            launcher_rank=40,
            default_shortcut=("new", "new"),
            launch_paths=[("new", "Terminal", "/new")],
            launch_methods={"new": "POST"},
        ),
    )


def builtin_chat_row_toml() -> str:
    """The chat's row, shaped as its manifest makes it: ranked first, its default shortcut the chat list, pinned."""
    return registry_row_toml(
        "chat",
        "http://localhost:7800",
        is_critical=True,
        launcher_rank=10,
        default_shortcut=("root", "new"),
        launch_paths=[("root", "Chat", "/"), ("new", "New Chat", "/api/chats/intake")],
        launch_methods={"new": "POST"},
        pin=("/", "avatar", "independent", "floating"),
    )


def builtin_registry_rows(directory: Path) -> tuple[list[RegistryRow], list[RegistryRow]]:
    """The built-in apps' rows, read from registries written under ``directory``: without the chat, and with it
    registered last."""
    before_chat = read_registry(write_registry(directory / "before_chat.toml", *builtin_rows_toml_before_chat()))
    with_chat = read_registry(
        write_registry(directory / "with_chat.toml", *builtin_rows_toml_before_chat(), builtin_chat_row_toml())
    )
    return before_chat, with_chat


def shortcut_apps_on(desktop: Desktop) -> tuple[str, ...]:
    return tuple(str(shortcut.target.app) for shortcut in desktop.shortcuts)


def write_desktops_file(state_directory: Path, *desktops: Desktop) -> None:
    write_json_atomic(
        state_directory / DESKTOPS_FILENAME,
        DesktopsDocument(version=DESKTOPS_FILE_VERSION, desktops=desktops).model_dump(mode="json"),
    )


def read_default_shortcuts_offered(state_directory: Path) -> dict[str, Any]:
    return json.loads((state_directory / DEFAULT_SHORTCUTS_OFFERED_FILENAME).read_text())


# The ``desktops.json`` a released shell reads: its version, and its keys at the top and per desktop.
_RELEASED_DESKTOPS_FILE_VERSION: Final[int] = 1
_RELEASED_DESKTOPS_FILE_KEYS: Final[frozenset[str]] = frozenset({"version", "desktops"})
_RELEASED_DESKTOP_KEYS: Final[frozenset[str]] = frozenset(
    {"id", "name", "color", "glyph", "wallpaper", "shortcuts", "windows"}
)


def read_desktops_file_in_its_released_shape(state_directory: Path) -> tuple[Desktop, ...]:
    """The desktops ``desktops.json`` holds, after checking it is the version and has exactly the keys a released
    shell reads."""
    raw = json.loads((state_directory / DESKTOPS_FILENAME).read_text())
    assert set(raw) == _RELEASED_DESKTOPS_FILE_KEYS and raw["version"] == _RELEASED_DESKTOPS_FILE_VERSION
    for desktop in raw["desktops"]:
        assert set(desktop) == _RELEASED_DESKTOP_KEYS
    return DesktopsDocument.model_validate(raw).desktops


def shell_application(
    tmp_path: Path,
    inventory: AppInventory,
    broadcaster: WebSocketBroadcaster,
    is_preview: bool = False,
    wallpaper_files_directory: Path | None = None,
    launch_poster: LaunchPoster | None = None,
) -> Flask:
    """The shell app over ``inventory``, its state under ``tmp_path/state`` and the update notice's workspace at
    ``tmp_path/repo``, sharing the inventory's broadcaster as in production.

    Its bundle directory is ``tmp_path / "static"``, empty until a test fills it, so no route answer depends
    on whether the frontend has been built in the checkout. ``wallpaper_files_directory`` is where the
    workspace's own wallpapers are read from, absolute by default; a relative one names a place under
    ``tmp_path/repo``, as the shipped default names one under the workspace root.
    """
    state = build_test_state(
        broadcaster=broadcaster,
        shell_state_directory=tmp_path / "state",
        inventory=inventory,
        is_preview=is_preview,
        repo_root=tmp_path / "repo",
        static_directory=tmp_path / "static",
        wallpaper_files_directory=wallpaper_files_directory,
        launch_poster=launch_poster,
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


def recording_app(received: list[dict[str, Any]]) -> Flask:
    """An app that appends every JSON body posted to ``/api/window-closed`` to ``received`` and answers 204."""
    app = Flask("recording")

    def take() -> tuple[str, int]:
        received.append(request.get_json(force=True))
        return "", 204

    app.add_url_rule(TEST_TERMINAL_WINDOW_CLOSED_PATH, view_func=take, methods=["POST"], endpoint="take")
    return app


def message_handling_app(received: list[dict[str, Any]], path: str, status: int) -> Flask:
    """An app that appends every JSON body posted to ``path`` to ``received`` and answers ``status``."""
    app = Flask("message-handling")

    def take() -> tuple[str, int]:
        received.append(request.get_json(force=True))
        return "{}", status

    app.add_url_rule(path, view_func=take, methods=["POST"], endpoint="take")
    return app


def identity_headers(identity: RequestIdentity) -> dict[str, str]:
    """The ``X-Imbue-Identity`` header a share gateway stamps on a request, as a test client sends it."""
    return {IDENTITY_HEADER: identity.model_dump_json()}


def drain_messages(client_queue: "queue.Queue[str | None]") -> list[dict[str, Any]]:
    """Every message a registered fake client has been sent so far, parsed."""
    messages: list[dict[str, Any]] = []
    while not client_queue.empty():
        raw = client_queue.get_nowait()
        if raw is not None:
            messages.append(json.loads(raw))
    return messages


def window_record(
    window_id: WindowId,
    app: str,
    path: str,
    is_pinned: bool = False,
    scope: LocationScope = LocationScope.LINKED,
    title: str = "",
) -> Window:
    """A window record with a fixed opening time and an empty title."""
    return Window(
        id=window_id,
        app=AppName(app),
        path=WindowPath(path),
        title=WindowTitle(title),
        opened_at=TEST_NOW,
        is_pinned=is_pinned,
        scope=scope,
    )


def placement_record(
    window_id: WindowId,
    is_minimized: bool = False,
    state: WindowState = WindowState.NORMAL,
    is_detached: bool = False,
) -> WindowPlacement:
    """A placement at the first cascade frame; shown, normal, and on the desktop unless told otherwise."""
    return WindowPlacement(
        window_id=window_id, frame=cascade_frame(0), state=state, is_minimized=is_minimized, is_detached=is_detached
    )


def desktop_with_windows(*windows: Window) -> Desktop:
    """The ``home`` desktop holding ``windows`` and no shortcuts."""
    return Desktop(
        id=DesktopId("home"),
        name="Home",
        color="#2f6b4f",
        glyph=0,
        wallpaper=None,
        shortcuts=(),
        windows=windows,
    )


# The update notice

# Where the stub update-self script records each call it took.
STUB_UPDATE_SELF_CALLS_REL: Final[str] = "data/.state/update-apply/stub-calls.jsonl"

# A stand-in for ``update_self.py`` that records its argv, working directory, and session id, closes
# the record on ``confirm-last`` and writes the first progress on ``rollback-last`` the way the real
# script does (then holds for a moment, as a real rollback would), and exits as told. Written under
# the test's workspace root so the shell finds it where it finds the real one.
_STUB_UPDATE_SELF_SCRIPT = """\
import json
import os
import sys
import threading
from pathlib import Path

root = Path(__file__).resolve().parents[4]
record = root / "data/.state/update-apply/last-good.json"
with (root / "{calls_rel}").open("a") as calls:
    calls.write(json.dumps({{"argv": sys.argv[1:], "cwd": os.getcwd(), "sid": os.getsid(0)}}) + "\\n")
if {exit_code} != 0:
    sys.exit("the stub was told to fail")
if sys.argv[1:] == ["confirm-last"]:
    record.unlink(missing_ok=True)
if sys.argv[1:] == ["rollback-last"]:
    current = json.loads(record.read_text())
    current["progress"] = "Reverting the update"
    scratch = record.with_name(record.name + ".tmp")
    scratch.write_text(json.dumps(current))
    os.replace(scratch, record)
    threading.Event().wait({rollback_hold_seconds})
sys.exit(0)
"""


def write_rollback_point(
    repo_root: Path,
    *,
    apps: Sequence[str] = ("terminal",),
    programs: Sequence[str] | None = None,
    needs_system_services_restart: bool = False,
    progress: str | None = None,
    outcome: str | None = None,
) -> Path:
    """The record an apply run with ``--keep-rollback-point`` leaves, in the apply's own shape (its extra
    fields included), under ``repo_root``."""
    path = repo_root / LAST_GOOD_RECORD_REL
    write_json_atomic(
        path,
        {
            "merge_sha": "abc1234abc1234abc1234abc1234abc1234abc12",
            "rollback_to": "def5678def5678def5678def5678def5678def56",
            "applied_at": 1_780_000_000.0,
            "driven_by": "mngr/update-widgets",
            "snapshots": [
                {"name": "bundle", "source": "system/x", "copy": "data/.state/update-apply/snapshots/bundle"}
            ],
            "programs": list(programs) if programs is not None else list(apps),
            "apps": list(apps),
            "needs_system_services_restart": needs_system_services_restart,
            "progress": progress,
            "outcome": outcome,
        },
    )
    return path


def write_stub_update_self_script(repo_root: Path, exit_code: int = 0, rollback_hold_seconds: float = 0.0) -> Path:
    path = repo_root / UPDATE_SELF_SCRIPT_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _STUB_UPDATE_SELF_SCRIPT.format(
            calls_rel=STUB_UPDATE_SELF_CALLS_REL, exit_code=exit_code, rollback_hold_seconds=rollback_hold_seconds
        )
    )
    return path


def read_stub_update_self_calls(repo_root: Path) -> list[dict[str, Any]]:
    path = repo_root / STUB_UPDATE_SELF_CALLS_REL
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


# The File Viewer (``system/apps/files``): dufs, a binary the workspace image installs, serving the workspace's
# vendored and patched frontend. Tests that run the real viewer skip where dufs is not installed.
FILES_APP_DIRECTORY: Final[Path] = Path(__file__).resolve().parents[4] / "files"
DUFS_BINARY: Final[str | None] = shutil.which("dufs")


@contextmanager
def running_file_viewer(root: Path) -> Iterator[str]:
    """Run dufs over ``root`` as the File Viewer's program line runs it over ``/``; yields its loopback URL."""
    assert DUFS_BINARY is not None, "dufs is not installed"
    port = find_free_port()
    url = f"http://127.0.0.1:{port}"
    command = [DUFS_BINARY, "--allow-all", "--bind", "127.0.0.1", "--port", str(port)]
    command += ["--assets", str(FILES_APP_DIRECTORY / "assets"), str(root)]
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_for(
            lambda: is_server_answering(url),
            timeout=10.0,
            poll_interval=0.1,
            error_message=f"dufs did not come up at {url}",
        )
        yield url
    finally:
        process.terminate()
        try:
            process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            logger.warning("dufs at {} did not stop on SIGTERM; killing it", url)
            process.kill()
            process.wait(timeout=5.0)


def file_viewer_registry_row(url: str) -> str:
    """The File Viewer's registry row at ``url``, with the message handlers its own manifest declares."""
    manifest = load_manifest(FILES_APP_DIRECTORY / "app.toml")
    return registry_row_toml(
        str(manifest.name),
        url,
        display_name=str(manifest.display_name),
        launch_paths=(("new", "File Viewer", "/"),),
        message_handlers=[
            (str(handler.type), str(handler.path)) for handler in manifest.message_handlers if handler.path is not None
        ],
        shown_message_handlers=[
            (str(handler.type), str(handler.show), [str(page) for page in handler.showing])
            for handler in manifest.message_handlers
            if handler.show is not None
        ],
    )
