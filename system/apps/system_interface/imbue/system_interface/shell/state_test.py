"""Tests for ``ShellState``: the stale-client prune it runs at start and on its interval, the close hints, the
arrival, and the absolute-directory invariant it is built with."""

import itertools
import queue
import socket
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from app_manifest.primitives import AppName
from app_manifest.primitives import LaunchPathId
from pydantic import ValidationError

from imbue.imbue_common.model_update import to_update
from imbue.mngr.utils.polling import wait_for
from imbue.system_interface.profiles import ProfileResolver
from imbue.system_interface.shell.app_lifecycle import AppLifecycleManager
from imbue.system_interface.shell.app_lifecycle import WAKE_WAIT_SECONDS
from imbue.system_interface.shell.clients import CLIENT_RETENTION
from imbue.system_interface.shell.close_hints import WindowClosedHint
from imbue.system_interface.shell.data_types import ClientStateReport
from imbue.system_interface.shell.data_types import Desktop
from imbue.system_interface.shell.data_types import StoredWindowPath
from imbue.system_interface.shell.data_types import WindowOpenRequest
from imbue.system_interface.shell.desktop_document import seed_desktop_shortcuts
from imbue.system_interface.shell.desktops import DEFAULT_SHORTCUTS_OFFERED_FILENAME
from imbue.system_interface.shell.desktops import default_desktop
from imbue.system_interface.shell.errors import LaunchUnavailableError
from imbue.system_interface.shell.identity import RequestIdentity
from imbue.system_interface.shell.inventory import AppInventory
from imbue.system_interface.shell.launches import LaunchPost
from imbue.system_interface.shell.launches import LaunchPostOutcome
from imbue.system_interface.shell.primitives import ClientId
from imbue.system_interface.shell.primitives import DesktopId
from imbue.system_interface.shell.primitives import UserId
from imbue.system_interface.shell.primitives import WindowId
from imbue.system_interface.shell.primitives import WindowPath
from imbue.system_interface.shell.primitives import WindowTitle
from imbue.system_interface.shell.state import ShellState
from imbue.system_interface.shell.state import build_shell_state
from imbue.system_interface.shell.testing import BUILTIN_SHORTCUT_APPS_BEFORE_CHAT
from imbue.system_interface.shell.testing import BUILTIN_SHORTCUT_APPS_WITH_CHAT
from imbue.system_interface.shell.testing import FakeLivenessProber
from imbue.system_interface.shell.testing import TEST_NOW
from imbue.system_interface.shell.testing import TEST_TERMINAL_URL
from imbue.system_interface.shell.testing import TEST_TERMINAL_WINDOW_CLOSED_PATH
from imbue.system_interface.shell.testing import build_inventory
from imbue.system_interface.shell.testing import builtin_chat_row_toml
from imbue.system_interface.shell.testing import builtin_registry_rows
from imbue.system_interface.shell.testing import builtin_rows_toml_before_chat
from imbue.system_interface.shell.testing import drain_messages
from imbue.system_interface.shell.testing import placement_record
from imbue.system_interface.shell.testing import read_default_shortcuts_offered
from imbue.system_interface.shell.testing import read_desktops_file_in_its_released_shape
from imbue.system_interface.shell.testing import registry_row_toml
from imbue.system_interface.shell.testing import shortcut_apps_on
from imbue.system_interface.shell.testing import write_desktops_file
from imbue.system_interface.shell.testing import write_registry
from imbue.system_interface.shell.testing import write_two_app_registry
from imbue.system_interface.shell.wallpapers import DEFAULT_WALLPAPER_FILES_DIRECTORY
from imbue.system_interface.testing import find_free_port
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


def test_a_stopped_app_is_not_told_of_its_closed_window(tmp_path: Path, broadcaster: WebSocketBroadcaster) -> None:
    """The post would reach the shell's own parker and wake the app to tell it a window closed."""
    hints: list[WindowClosedHint] = []
    prober = FakeLivenessProber()
    prober.is_running_by_name["terminal"] = False
    registry_path = write_two_app_registry(tmp_path)
    built = build_shell_state(
        tmp_path / "state",
        registry_path,
        broadcaster,
        inventory=build_inventory(registry_path, broadcaster, prober=prober),
    )
    shell = built.model_copy_update(to_update(built.field_ref().close_hint_poster, hints.append))
    (home,) = shell.list_desktops()
    terminal_window = _open(shell, home.id, "terminal", "/?session=terminal-1")

    assert shell.close_window(home.id, terminal_window) is True

    assert hints == []


def test_a_post_launch_to_a_stopped_stoppable_app_wakes_it_and_waits_for_it_to_answer(
    tmp_path: Path, broadcaster: WebSocketBroadcaster
) -> None:
    """The stop-when-no-windows spec, section 5.6: a POST launch cannot go through the parker (which answers
    503), so the shell wakes the app and posts once its port accepts; an app that does not come up in time fails
    the launch without a post."""
    docs_port = find_free_port()
    registry_path = write_registry(
        tmp_path / "apps.toml",
        registry_row_toml(
            "docs",
            f"http://127.0.0.1:{docs_port}",
            program="docs",
            launch_paths=(("new", "New doc", "/api/intake"),),
            launch_methods={"new": "POST"},
        ),
    )
    prober = FakeLivenessProber()
    prober.is_running_by_name["docs"] = False
    posts: list[LaunchPost] = []

    def poster(post: LaunchPost) -> LaunchPostOutcome:
        posts.append(post)
        return LaunchPostOutcome(status_code=200, body={"path": "/?doc=1"})

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    started: list[str] = []

    def start_and_bind(program: str) -> None:
        started.append(program)
        listener.bind(("127.0.0.1", docs_port))
        listener.listen(1)

    built = build_shell_state(
        tmp_path / "state",
        registry_path,
        broadcaster,
        inventory=build_inventory(registry_path, broadcaster, prober=prober),
        launch_poster=poster,
    )
    shell = built.model_copy_update(
        to_update(built.field_ref().lifecycle, _launch_waking_manager(built, start_and_bind, time.monotonic))
    )
    entry = shell.inventory.entry("docs")
    assert entry is not None and entry.is_running is False
    (launch_path,) = entry.row.launch_paths

    try:
        path = shell.launch_destination(entry, launch_path, {}, ClientId("c1"), DesktopId("home"), None)
    finally:
        listener.close()

    assert path == "/?doc=1" and started == ["docs"]
    (post,) = posts
    assert post.url == f"http://127.0.0.1:{docs_port}/api/intake"

    # An app that never binds: the wait (over a clock that steps past it on each read) ends in a 502, unposted.
    ticks = itertools.count(start=0.0, step=WAKE_WAIT_SECONDS)
    never_up = built.model_copy_update(
        to_update(built.field_ref().lifecycle, _launch_waking_manager(built, started.append, lambda: next(ticks)))
    )
    with pytest.raises(LaunchUnavailableError, match="could not be brought up"):
        never_up.launch_destination(entry, launch_path, {}, ClientId("c1"), DesktopId("home"), None)
    assert started == ["docs", "docs"] and len(posts) == 1

    # A manager that does not own the live apps (a preview's) wakes nothing: the post goes out as it is and fails
    # as one to any unreachable app does.
    preview = built.model_copy_update(
        to_update(
            built.field_ref().lifecycle,
            _launch_waking_manager(built, started.append, time.monotonic, is_enabled=False),
        )
    ).model_copy_update(to_update(built.field_ref().launch_poster, _refusing_launch_poster))
    with pytest.raises(LaunchUnavailableError, match="refused"):
        preview.launch_destination(entry, launch_path, {}, ClientId("c1"), DesktopId("home"), None)
    assert started == ["docs", "docs"]


def _refusing_launch_poster(post: LaunchPost) -> LaunchPostOutcome:
    raise LaunchUnavailableError(f"{post.app} could not be reached for the launch: refused")


def _launch_waking_manager(
    shell: ShellState, start_program: Callable[[str], None], clock: Callable[[], float], is_enabled: bool = True
) -> AppLifecycleManager:
    """A lifecycle manager over the shell's inventory whose supervisord is a stopped ``docs`` and whose start is
    ``start_program``; enabled, so a launch wakes the app, but never started, so nothing is swept."""
    return AppLifecycleManager(
        inventory=shell.inventory,
        is_enabled=is_enabled,
        count_windows_by_app=lambda: {},
        granted_app_names=lambda: set(),
        program_states=lambda: {"docs": "STOPPED"},
        start_program=start_program,
        stop_program=lambda program: None,
        clock=clock,
    )


def test_the_lifecycle_manager_reads_the_per_app_share_grants_under_the_workspace_root(
    tmp_path: Path, broadcaster: WebSocketBroadcaster
) -> None:
    registry_path = write_two_app_registry(tmp_path)
    secrets_directory = tmp_path / "repo" / "data" / ".secrets"
    secrets_directory.mkdir(parents=True)
    (secrets_directory / "share_grants.toml").write_text('[services.docs]\nemails = ["reviewer@example.com"]\n')
    shell = build_shell_state(
        tmp_path / "state",
        registry_path,
        broadcaster,
        inventory=build_inventory(registry_path, broadcaster),
        repo_root=tmp_path / "repo",
    )

    assert shell.lifecycle.granted_app_names() == set()
    (secrets_directory / "share.env").write_text("export SHARE_BROKER_URL=https://broker.example\n")
    assert shell.lifecycle.granted_app_names() == {"docs"}


def test_an_arrival_marks_the_workspace_visited(tmp_path: Path, broadcaster: WebSocketBroadcaster) -> None:
    registry_path = write_two_app_registry(tmp_path)
    shell = build_shell_state(
        tmp_path / "state", registry_path, broadcaster, inventory=build_inventory(registry_path, broadcaster)
    )
    assert shell.lifecycle.is_visited is False
    shell.list_desktops()

    shell.arrive_client(ClientId("laptop"), RequestIdentity(owner=True))

    assert shell.lifecycle.is_visited is True


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


# Default shortcuts of apps that register after the default desktop was seeded (desktop plan section 3.2)


def _pinned_apps_on(desktop: Desktop) -> list[str]:
    return [str(window.app) for window in desktop.windows if window.is_pinned]


def _register_chat(registry_path: Path) -> None:
    write_registry(registry_path, *builtin_rows_toml_before_chat(), builtin_chat_row_toml())


def _deregister_chat(registry_path: Path) -> None:
    write_registry(registry_path, *builtin_rows_toml_before_chat())


def _shell_over(state_directory: Path, registry_path: Path, broadcaster: WebSocketBroadcaster) -> ShellState:
    """A shell over ``state_directory`` whose inventory has read ``registry_path`` once."""
    return build_shell_state(
        state_directory, registry_path, broadcaster, inventory=build_inventory(registry_path, broadcaster)
    )


def _shell_before_the_chat(tmp_path: Path, broadcaster: WebSocketBroadcaster) -> ShellState:
    """A shell over ``tmp_path / "state"`` whose inventory has read a registry of the built-in apps' rows without the
    chat's."""
    registry_path = write_registry(tmp_path / "apps.toml", *builtin_rows_toml_before_chat())
    return _shell_over(tmp_path / "state", registry_path, broadcaster)


def _shell_restarted_after_the_chat_registered(tmp_path: Path, broadcaster: WebSocketBroadcaster) -> ShellState:
    """A shell over the state of one that seeded Home before the chat registered, its inventory having read the
    chat's row before the shell listened to it (a restart)."""
    seeding = _shell_before_the_chat(tmp_path, broadcaster)
    (home,) = seeding.list_desktops()
    assert shortcut_apps_on(home) == BUILTIN_SHORTCUT_APPS_BEFORE_CHAT
    registry_path = seeding.inventory.registry_path
    _register_chat(registry_path)
    return _shell_over(tmp_path / "state", registry_path, broadcaster)


def _desktops_updates(client_queue: "queue.Queue[str | None]") -> list[tuple[str, ...]]:
    """The shortcut apps of the first desktop in each ``desktops_updated`` the client was sent."""
    return [
        tuple(shortcut["target"]["app"] for shortcut in message["desktops"][0]["shortcuts"])
        for message in drain_messages(client_queue)
        if message["type"] == "desktops_updated"
    ]


def test_an_app_registering_after_home_was_seeded_reaches_it_through_the_registry_change_alone(
    tmp_path: Path, broadcaster: WebSocketBroadcaster
) -> None:
    shell = _shell_before_the_chat(tmp_path, broadcaster)
    (home,) = shell.list_desktops()
    assert shortcut_apps_on(home) == BUILTIN_SHORTCUT_APPS_BEFORE_CHAT and _pinned_apps_on(home) == []
    client_queue = broadcaster.register()

    _register_chat(shell.inventory.registry_path)
    shell.inventory.reload_registry()

    # Read straight from the store, so nothing but the registry change can have added the chat.
    (stored,) = shell.desktops.list_desktops()
    assert shortcut_apps_on(stored) == BUILTIN_SHORTCUT_APPS_WITH_CHAT
    assert stored.shortcuts == seed_desktop_shortcuts([entry.row for entry in shell.inventory.entries()])
    assert _pinned_apps_on(stored) == ["chat"]
    assert _desktops_updates(client_queue) == [BUILTIN_SHORTCUT_APPS_WITH_CHAT]


def test_an_app_registering_after_home_was_seeded_reaches_it_through_a_later_read(
    tmp_path: Path, broadcaster: WebSocketBroadcaster
) -> None:
    """A shell whose inventory read the chat's row before the shell listened to it (a restart) adds it on the read."""
    reading = _shell_restarted_after_the_chat_registered(tmp_path, broadcaster)
    assert shortcut_apps_on(reading.desktops.list_desktops()[0]) == BUILTIN_SHORTCUT_APPS_BEFORE_CHAT
    client_queue = broadcaster.register()

    (read,) = reading.list_desktops()

    assert shortcut_apps_on(read) == BUILTIN_SHORTCUT_APPS_WITH_CHAT and _pinned_apps_on(read) == ["chat"]
    assert _desktops_updates(client_queue) == [BUILTIN_SHORTCUT_APPS_WITH_CHAT]
    # The next read finds nothing to add and announces nothing.
    assert reading.list_desktops() == [read]
    assert _desktops_updates(client_queue) == []


def test_a_desktop_created_before_a_late_app_was_reconciled_leaves_it_offered_on_every_desktop(
    tmp_path: Path, broadcaster: WebSocketBroadcaster
) -> None:
    """A shell whose inventory read the chat's row before the shell listened to it, and before any read of the
    desktops: creating a desktop, which records the chat as offered, first adds it to the desktops already there."""
    reading = _shell_restarted_after_the_chat_registered(tmp_path, broadcaster)

    work = reading.create_desktop("Work", "#123456", 1)

    stored_home, stored_work = reading.desktops.list_desktops()
    assert shortcut_apps_on(stored_home) == BUILTIN_SHORTCUT_APPS_WITH_CHAT
    assert _pinned_apps_on(stored_home) == ["chat"]
    assert stored_work == work and shortcut_apps_on(work) == BUILTIN_SHORTCUT_APPS_WITH_CHAT
    assert "chat" in read_default_shortcuts_offered(tmp_path / "state")["apps"]


def test_a_visiting_users_desktop_made_before_a_late_app_registered_gets_its_shortcut_too(
    tmp_path: Path, broadcaster: WebSocketBroadcaster
) -> None:
    """A visitor's desktop, a copy of Home made before the chat registered, records no app Home was not seeded with,
    and is laid out again in launcher order with Home when the chat registers."""
    shell = _shell_before_the_chat(tmp_path, broadcaster)
    shell.list_desktops()
    arrival = shell.arrive_client(
        ClientId("visitor-tab"), RequestIdentity(owner=False, user_id="user-alice", email="alice@example.com")
    )
    assert arrival is not None and arrival.created_desktop is not None
    assert read_default_shortcuts_offered(tmp_path / "state")["apps"] == sorted(BUILTIN_SHORTCUT_APPS_BEFORE_CHAT)

    _register_chat(shell.inventory.registry_path)
    shell.inventory.reload_registry()

    seeded_with_chat = seed_desktop_shortcuts([entry.row for entry in shell.inventory.entries()])
    home, visitors = shell.desktops.list_desktops()
    assert visitors.id == arrival.created_desktop.id
    assert home.shortcuts == seeded_with_chat and visitors.shortcuts == seeded_with_chat
    assert read_default_shortcuts_offered(tmp_path / "state")["apps"] == sorted(BUILTIN_SHORTCUT_APPS_WITH_CHAT)


def test_a_removed_default_shortcut_stays_removed_across_registrations_and_a_new_shell(
    tmp_path: Path, broadcaster: WebSocketBroadcaster
) -> None:
    shell = _shell_before_the_chat(tmp_path, broadcaster)
    registry_path = shell.inventory.registry_path
    shell.list_desktops()
    _register_chat(registry_path)
    shell.inventory.reload_registry()
    assert shortcut_apps_on(shell.desktops.list_desktops()[0]) == BUILTIN_SHORTCUT_APPS_WITH_CHAT
    shell.desktops.remove_shortcut("home", AppName("chat"), LaunchPathId("root"))

    # The chat deregisters and registers again; each change reached the desktops (its pinned window was released and
    # taken back), and neither brought the shortcut back.
    _deregister_chat(registry_path)
    shell.inventory.reload_registry()
    assert _pinned_apps_on(shell.desktops.list_desktops()[0]) == []
    _register_chat(registry_path)
    shell.inventory.reload_registry()
    assert _pinned_apps_on(shell.desktops.list_desktops()[0]) == ["chat"]
    assert shortcut_apps_on(shell.list_desktops()[0]) == BUILTIN_SHORTCUT_APPS_BEFORE_CHAT

    restarted = _shell_over(tmp_path / "state", registry_path, broadcaster)
    assert shortcut_apps_on(restarted.list_desktops()[0]) == BUILTIN_SHORTCUT_APPS_BEFORE_CHAT


def test_a_state_directory_from_before_the_offered_record_gets_a_late_apps_shortcut_once(
    tmp_path: Path, broadcaster: WebSocketBroadcaster
) -> None:
    """A workspace seeded by a shell that kept no record of what it offered: its first read adds the chat, creates the
    record, and writes a desktops.json every release reads."""
    before_chat, _ = builtin_registry_rows(tmp_path / "registry")
    state_directory = tmp_path / "state"
    write_desktops_file(state_directory, default_desktop(seed_desktop_shortcuts(before_chat)))
    registry_path = write_registry(tmp_path / "apps.toml", *builtin_rows_toml_before_chat(), builtin_chat_row_toml())
    shell = _shell_over(state_directory, registry_path, broadcaster)

    (home,) = shell.list_desktops()

    assert shortcut_apps_on(home) == BUILTIN_SHORTCUT_APPS_WITH_CHAT
    assert read_default_shortcuts_offered(state_directory) == {
        "version": 1,
        "apps": sorted(BUILTIN_SHORTCUT_APPS_WITH_CHAT),
    }
    assert read_desktops_file_in_its_released_shape(state_directory) == (home,)
    # Removed once added, it stays removed: the record now names it.
    shell.desktops.remove_shortcut("home", AppName("chat"), LaunchPathId("root"))
    assert shortcut_apps_on(shell.list_desktops()[0]) == BUILTIN_SHORTCUT_APPS_BEFORE_CHAT


def test_a_registry_change_whose_reconcile_cannot_write_is_logged_and_the_next_read_completes_it(
    tmp_path: Path, broadcaster: WebSocketBroadcaster, loguru_records: list[str]
) -> None:
    """The reconcile a registry change runs must not raise into the registry read (the watch thread's); once the
    state can be written again, the next read of the desktops records the late app as offered."""
    shell = _shell_before_the_chat(tmp_path, broadcaster)
    shell.list_desktops()
    # A directory where the offered record goes makes every write of it fail.
    offered_path = tmp_path / "state" / DEFAULT_SHORTCUTS_OFFERED_FILENAME
    offered_path.unlink()
    offered_path.mkdir()

    _register_chat(shell.inventory.registry_path)
    shell.inventory.reload_registry()

    assert any(
        record.startswith("ERROR Failed to reconcile the desktops with the changed app registry")
        for record in loguru_records
    )
    offered_path.rmdir()
    (home,) = shell.list_desktops()
    assert shortcut_apps_on(home) == BUILTIN_SHORTCUT_APPS_WITH_CHAT
    assert read_default_shortcuts_offered(tmp_path / "state")["apps"] == sorted(BUILTIN_SHORTCUT_APPS_WITH_CHAT)


def test_a_late_registration_reaches_the_desktops_through_the_registry_watch(
    tmp_path: Path, broadcaster: WebSocketBroadcaster
) -> None:
    registry_path = write_registry(tmp_path / "apps.toml", *builtin_rows_toml_before_chat())
    inventory = AppInventory(
        registry_path=registry_path,
        broadcaster=broadcaster,
        liveness_prober=FakeLivenessProber(),
        sweep_interval_seconds=60.0,
    )
    shell = build_shell_state(tmp_path / "state", registry_path, broadcaster, inventory=inventory)
    inventory.start()
    try:
        (home,) = shell.list_desktops()
        assert shortcut_apps_on(home) == BUILTIN_SHORTCUT_APPS_BEFORE_CHAT

        _register_chat(registry_path)

        wait_for(
            lambda: shortcut_apps_on(shell.desktops.list_desktops()[0]) == BUILTIN_SHORTCUT_APPS_WITH_CHAT,
            timeout=5.0,
            poll_interval=0.02,
            error_message="the registry watch never brought the chat's shortcut to the default desktop",
        )
    finally:
        inventory.stop()
