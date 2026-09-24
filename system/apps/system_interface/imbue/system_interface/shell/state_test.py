"""Tests for ``ShellState``: the stale-client prune it runs at start and on its interval, the close hints, the
arrival, and the absolute-directory invariant it is built with."""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from app_manifest.primitives import AppName
from pydantic import ValidationError

from imbue.imbue_common.model_update import to_update
from imbue.mngr.utils.polling import wait_for
from imbue.system_interface.profiles import ProfileResolver
from imbue.system_interface.shell.clients import CLIENT_RETENTION
from imbue.system_interface.shell.close_hints import WindowClosedHint
from imbue.system_interface.shell.data_types import ClientStateReport
from imbue.system_interface.shell.data_types import StoredWindowPath
from imbue.system_interface.shell.data_types import WindowOpenRequest
from imbue.system_interface.shell.identity import RequestIdentity
from imbue.system_interface.shell.primitives import ClientId
from imbue.system_interface.shell.primitives import DesktopId
from imbue.system_interface.shell.primitives import UserId
from imbue.system_interface.shell.primitives import WindowId
from imbue.system_interface.shell.primitives import WindowPath
from imbue.system_interface.shell.primitives import WindowTitle
from imbue.system_interface.shell.state import ShellState
from imbue.system_interface.shell.state import build_shell_state
from imbue.system_interface.shell.testing import TEST_NOW
from imbue.system_interface.shell.testing import TEST_TERMINAL_URL
from imbue.system_interface.shell.testing import TEST_TERMINAL_WINDOW_CLOSED_PATH
from imbue.system_interface.shell.testing import build_inventory
from imbue.system_interface.shell.testing import placement_record
from imbue.system_interface.shell.testing import write_two_app_registry
from imbue.system_interface.shell.wallpapers import DEFAULT_WALLPAPER_FILES_DIRECTORY
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster


def test_start_prunes_stale_clients_and_their_layouts_now_and_on_the_interval(
    tmp_path: Path, broadcaster: WebSocketBroadcaster
) -> None:
    registry_path = write_two_app_registry(tmp_path)
    inventory = build_inventory(registry_path, broadcaster)
    built = build_shell_state(
        tmp_path / "state", registry_path, broadcaster, inventory=inventory, repo_root=tmp_path / "repo"
    )
    shell = built.model_copy_update(to_update(built.field_ref().client_prune_interval_seconds, 0.05))
    stale_at = TEST_NOW - CLIENT_RETENTION - timedelta(days=1)
    (home,) = shell.list_desktops()
    window_id = WindowId("win-0000000000000001")
    shell.clients.record_report(
        ClientStateReport(client_id=ClientId("old"), active_desktop=DesktopId("home")), stale_at
    )
    shell.placements.save_browser_layout("home", "old", (placement_record(window_id),), None, {window_id}, stale_at)
    shell.window_paths.set_path(
        ClientId("old"),
        window_id,
        StoredWindowPath(path=WindowPath("/x"), title=WindowTitle("")),
        lambda: {window_id},
    )
    shell.start()
    try:
        # The prune at start took the stale client, its layout file, and its window paths.
        assert shell.clients.get_client("old") is None
        assert shell.placements.read_layout(home.id, "old", {window_id}).placements == ()
        assert shell.window_paths.read_paths(ClientId("old"), {window_id}) == {}
        # A client that goes stale while the shell runs is taken by the periodic prune.
        shell.clients.record_report(
            ClientStateReport(client_id=ClientId("later"), active_desktop=DesktopId("home")), stale_at
        )
        wait_for(
            lambda: shell.clients.get_client("later") is None,
            timeout=5.0,
            poll_interval=0.02,
            error_message="the periodic prune never took the stale client",
        )
    finally:
        shell.stop()


def _shell_recording_hints(
    tmp_path: Path, broadcaster: WebSocketBroadcaster, hints: list[WindowClosedHint]
) -> ShellState:
    registry_path = write_two_app_registry(tmp_path)
    built = build_shell_state(
        tmp_path / "state", registry_path, broadcaster, inventory=build_inventory(registry_path, broadcaster)
    )
    return built.model_copy_update(to_update(built.field_ref().close_hint_poster, hints.append))


def _open(shell: ShellState, desktop_id: str, app: str, path: str) -> WindowId:
    request = WindowOpenRequest(app=AppName(app), path=WindowPath(path), client_id=ClientId("laptop"))
    return shell.open_window(desktop_id, request, is_minimized=False).window.id


def test_closing_a_window_tells_its_app_when_the_row_names_a_window_closed_path(
    tmp_path: Path, broadcaster: WebSocketBroadcaster
) -> None:
    hints: list[WindowClosedHint] = []
    shell = _shell_recording_hints(tmp_path, broadcaster, hints)
    (home,) = shell.list_desktops()
    terminal_window = _open(shell, home.id, "terminal", "/?session=terminal-1")
    files_window = _open(shell, home.id, "files", "/notes/")

    assert shell.close_window(home.id, terminal_window) is True
    assert hints == [
        WindowClosedHint(
            app="terminal",
            url=f"{TEST_TERMINAL_URL}{TEST_TERMINAL_WINDOW_CLOSED_PATH}",
            body={"path": "/?session=terminal-1", "window_id": str(terminal_window), "desktop_id": "home"},
        )
    ]
    # A second close of the same window is idempotent and tells nobody; the files row names no path.
    assert shell.close_window(home.id, terminal_window) is False
    assert shell.close_window(home.id, files_window) is True
    assert len(hints) == 1


def test_deleting_a_desktop_tells_the_apps_of_every_window_it_held(
    tmp_path: Path, broadcaster: WebSocketBroadcaster
) -> None:
    hints: list[WindowClosedHint] = []
    shell = _shell_recording_hints(tmp_path, broadcaster, hints)
    shell.list_desktops()
    work = shell.desktops.create_desktop("Work", "#123456", 1, (), ())
    first = _open(shell, work.id, "terminal", "/?session=terminal-1")
    second = _open(shell, work.id, "terminal", "/?session=terminal-2")
    _open(shell, work.id, "files", "/")

    shell.delete_desktop(work.id)

    assert [(hint.body["window_id"], hint.body["desktop_id"]) for hint in hints] == [
        (str(first), "work"),
        (str(second), "work"),
    ]


def test_concurrent_first_arrivals_of_one_user_seed_a_single_desktop(
    tmp_path: Path, broadcaster: WebSocketBroadcaster
) -> None:
    registry_path = write_two_app_registry(tmp_path)
    shell = build_shell_state(
        tmp_path / "state", registry_path, broadcaster, inventory=build_inventory(registry_path, broadcaster)
    )
    alice = RequestIdentity(owner=False, user_id="user-alice", email="alice@example.com")
    arrival_count = 6
    ready = threading.Barrier(arrival_count)

    def arrive(index: int) -> tuple[str | None, bool]:
        ready.wait(timeout=5)
        outcome = shell.arrive_client(ClientId(f"tab-{index}"), alice)
        assert outcome is not None
        return outcome.desktop_id, outcome.created_desktop is not None

    with ThreadPoolExecutor(max_workers=arrival_count) as executor:
        outcomes = list(executor.map(arrive, range(arrival_count)))
    assert [landing for landing, _ in outcomes] == ["alice"] * arrival_count
    assert sum(1 for _, is_created in outcomes if is_created) == 1
    assert [desktop.id for desktop in shell.list_desktops()] == ["home", "alice"]


def test_a_visiting_users_desktop_is_named_after_the_profile_the_connector_answers(
    tmp_path: Path, broadcaster: WebSocketBroadcaster
) -> None:
    """The header names the account; what the desktop is called comes from the account's profile, fetched from the
    broker share.env names and kept on the user's record for the notice."""
    registry_path = write_two_app_registry(tmp_path)
    (tmp_path / "share.env").write_text('export SHARE_BROKER_URL="https://broker.example.test/"\n')
    requested_paths: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        return httpx.Response(
            200, json={"user_id": "user-alice", "display_name": "Alice Q", "profile_picture_url": None}
        )

    profiles = ProfileResolver(
        cache_directory=tmp_path / "profiles",
        share_env_path=tmp_path / "share.env",
        transport=httpx.MockTransport(answer),
    )
    shell = build_shell_state(
        tmp_path / "state",
        registry_path,
        broadcaster,
        inventory=build_inventory(registry_path, broadcaster),
        profiles=profiles,
    )
    alice = RequestIdentity(owner=False, user_id="user-alice", email="alice@example.com")

    outcome = shell.arrive_client(ClientId("tab-1"), alice)

    assert outcome is not None and outcome.created_desktop is not None
    assert outcome.created_desktop.name == "Alice Q" and outcome.desktop_id == "alice-q"
    assert requested_paths == ["/users/user-alice/profile"]
    stored = shell.users.get_user(UserId("user-alice"))
    assert stored is not None and stored.display_name == "Alice Q"


def test_a_visiting_user_arrives_named_by_email_when_the_connector_is_down(
    tmp_path: Path, broadcaster: WebSocketBroadcaster
) -> None:
    registry_path = write_two_app_registry(tmp_path)
    (tmp_path / "share.env").write_text("SHARE_BROKER_URL=https://broker.example.test\n")

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    profiles = ProfileResolver(
        cache_directory=tmp_path / "profiles",
        share_env_path=tmp_path / "share.env",
        transport=httpx.MockTransport(refuse),
    )
    shell = build_shell_state(
        tmp_path / "state",
        registry_path,
        broadcaster,
        inventory=build_inventory(registry_path, broadcaster),
        profiles=profiles,
    )

    outcome = shell.arrive_client(
        ClientId("tab-1"), RequestIdentity(owner=False, user_id="user-bob", email="bob.smith@example.com")
    )

    assert outcome is not None and outcome.created_desktop is not None
    assert outcome.created_desktop.name == "bob.smith"


def test_the_wallpapers_directory_is_absolute_however_the_workspace_root_is_named(
    tmp_path: Path, broadcaster: WebSocketBroadcaster, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory configured under the workspace root lands absolute even when that root is named relatively.

    The listing route walks the directory itself while the serve route hands it to a sender that resolves
    against the package, so a relative directory reaches two different places and the image 500s. ``ShellState``
    refuses one outright, which is what a second construction site beside ``build_shell_state`` would meet.
    """
    registry_path = write_two_app_registry(tmp_path)
    monkeypatch.chdir(tmp_path)

    built = build_shell_state(
        tmp_path / "state",
        registry_path,
        broadcaster,
        inventory=build_inventory(registry_path, broadcaster),
        repo_root=Path("repo"),
    )

    assert built.wallpaper_files_directory == (tmp_path / "repo" / DEFAULT_WALLPAPER_FILES_DIRECTORY).resolve()
    with pytest.raises(ValidationError) as refused:
        ShellState.model_validate({**dict(built), "wallpaper_files_directory": DEFAULT_WALLPAPER_FILES_DIRECTORY})
    assert str(DEFAULT_WALLPAPER_FILES_DIRECTORY) in str(refused.value)
