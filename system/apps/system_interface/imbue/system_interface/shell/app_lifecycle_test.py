"""Tests for the app lifecycle manager: parking, waking, the wake budget, and the stop verb."""

import socket
from collections.abc import Iterator
from pathlib import Path

import pytest

from imbue.system_interface.shell.app_lifecycle import AppLifecycleManager
from imbue.system_interface.shell.app_lifecycle import NO_WINDOWS_GRACE_SECONDS
from imbue.system_interface.shell.app_lifecycle import WAKE_BUDGET_COUNT
from imbue.system_interface.shell.app_lifecycle import WAKE_BUDGET_WINDOW_SECONDS
from imbue.system_interface.shell.errors import AppLifecycleRefusedError
from imbue.system_interface.shell.liveness import probe_tcp_url
from imbue.system_interface.shell.port_parking import ParkedPageKind
from imbue.system_interface.shell.testing import FakeLivenessProber
from imbue.system_interface.shell.testing import build_inventory
from imbue.system_interface.shell.testing import registry_row_toml
from imbue.system_interface.shell.testing import write_registry
from imbue.system_interface.testing import can_bind_loopback_port
from imbue.system_interface.testing import find_free_port
from imbue.system_interface.testing import send_raw_get_over_socket
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster


class FakeSupervisor:
    """A statename table with the two verbs, recording what was asked."""

    def __init__(self, statename_by_program: dict[str, str]) -> None:
        self.statename_by_program = statename_by_program
        self.started: list[str] = []
        self.stopped: list[str] = []
        self.is_reachable = True

    def states(self) -> dict[str, str] | None:
        return dict(self.statename_by_program) if self.is_reachable else None

    def start(self, program: str) -> None:
        self.started.append(program)
        self.statename_by_program[program] = "STARTING"

    def stop(self, program: str) -> None:
        self.stopped.append(program)
        self.statename_by_program[program] = "STOPPED"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def supervisor() -> FakeSupervisor:
    return FakeSupervisor({"docs": "STOPPED", "shell": "RUNNING", "plain": "RUNNING"})


class FakeWindows:
    """How many windows each app has, as the manager's rule reads it."""

    def __init__(self) -> None:
        self.count_by_app: dict[str, int] = {}

    def get_count(self, app: str) -> int:
        return self.count_by_app.get(app, 0)


@pytest.fixture
def windows() -> FakeWindows:
    return FakeWindows()


@pytest.fixture
def manager(
    tmp_path: Path,
    broadcaster: WebSocketBroadcaster,
    closed_port: int,
    supervisor: FakeSupervisor,
    windows: FakeWindows,
) -> Iterator[AppLifecycleManager]:
    """A manager over a stoppable ``docs`` app on the closed loopback port that stops when no window shows it, a
    critical ``shell``, and an unsupervised ``plain`` row, against a fake supervisord that starts with ``docs``
    stopped."""
    prober = FakeLivenessProber()
    prober.is_running_by_name["docs"] = False
    registry_path = write_registry(
        tmp_path / "apps.toml",
        registry_row_toml("docs", f"http://127.0.0.1:{closed_port}", program="docs", stop_when_no_windows=True),
        registry_row_toml("shell", "http://127.0.0.1:1", program="shell", is_critical=True),
        registry_row_toml("plain", "http://127.0.0.1:1"),
    )
    inventory = build_inventory(registry_path, broadcaster, prober=prober)
    clock = FakeClock()
    built = AppLifecycleManager(
        inventory=inventory,
        is_enabled=False,
        count_windows_of_app=windows.get_count,
        program_states=supervisor.states,
        start_program=supervisor.start,
        stop_program=supervisor.stop,
        clock=clock,
    )
    try:
        yield built
    finally:
        built.stop()


def _is_refused(port: int) -> bool:
    return not probe_tcp_url(f"http://127.0.0.1:{port}")


def test_a_pass_parks_a_stopped_stoppable_app_and_nothing_else(
    manager: AppLifecycleManager, closed_port: int, supervisor: FakeSupervisor
) -> None:
    manager.sweep_once()

    assert manager.parked_app_names() == ["docs"]
    # A critical app and an unsupervised row are never parked, and a running app is left alone.
    supervisor.statename_by_program["shell"] = "STOPPED"
    manager.sweep_once()
    assert manager.parked_app_names() == ["docs"]


def test_a_pass_releases_a_parked_app_that_came_up_on_its_own(
    manager: AppLifecycleManager, closed_port: int, supervisor: FakeSupervisor
) -> None:
    manager.sweep_once()
    assert manager.is_app_parked("docs")

    supervisor.statename_by_program["docs"] = "STARTING"
    manager.sweep_once()

    assert not manager.is_app_parked("docs")
    assert _is_refused(closed_port)
    # A program supervisord is retrying (BACKOFF) needs the port too.
    supervisor.statename_by_program["docs"] = "STOPPED"
    manager.sweep_once()
    assert manager.is_app_parked("docs")
    supervisor.statename_by_program["docs"] = "BACKOFF"
    manager.sweep_once()
    assert not manager.is_app_parked("docs")


def test_a_connection_to_a_parked_port_wakes_the_app(
    manager: AppLifecycleManager, closed_port: int, supervisor: FakeSupervisor
) -> None:
    manager.sweep_once()

    answer = send_raw_get_over_socket(closed_port)

    assert answer.startswith(b"HTTP/1.1 503") and b"Starting Docs" in answer
    assert supervisor.started == ["docs"]
    assert not manager.is_app_parked("docs")
    # The port is free for the app; a pass while supervisord says STARTING parks nothing.
    manager.sweep_once()
    assert not manager.is_app_parked("docs")


def test_a_parked_port_follows_its_row_and_is_released_once_the_row_is_gone(
    manager: AppLifecycleManager, closed_port: int, supervisor: FakeSupervisor, tmp_path: Path
) -> None:
    """A parker outlives neither the port its row names nor the row itself: an app re-registered on another port
    moves the shell's hold there, and one that left the registry gets its port back at once, so whatever binds it
    next is not stranded behind a loading page the shell can never resolve."""
    manager.sweep_once()
    assert manager.is_app_parked("docs")
    registry_path = manager.inventory.registry_path
    moved_port = find_free_port()

    write_registry(
        registry_path,
        registry_row_toml("docs", f"http://127.0.0.1:{moved_port}", program="docs", stop_when_no_windows=True),
    )
    manager.inventory.reload_registry()
    manager.sweep_once()

    assert manager.parked_app_names() == ["docs"]
    assert _is_refused(closed_port) and not can_bind_loopback_port(moved_port)

    write_registry(registry_path, registry_row_toml("plain", "http://127.0.0.1:1"))
    manager.inventory.reload_registry()
    manager.sweep_once()

    assert manager.parked_app_names() == []
    assert can_bind_loopback_port(moved_port)
    assert supervisor.started == []


def test_a_pass_does_nothing_when_supervisord_does_not_answer(
    manager: AppLifecycleManager, supervisor: FakeSupervisor
) -> None:
    supervisor.is_reachable = False
    manager.sweep_once()
    assert manager.parked_app_names() == []


def test_a_wake_releases_the_parker_before_starting(
    manager: AppLifecycleManager, closed_port: int, supervisor: FakeSupervisor
) -> None:
    manager.sweep_once()

    assert manager.wake("docs") is ParkedPageKind.STARTING
    assert supervisor.started == ["docs"]
    assert _is_refused(closed_port)
    with pytest.raises(AppLifecycleRefusedError):
        manager.wake("shell")
    with pytest.raises(AppLifecycleRefusedError):
        manager.wake("plain")


def test_the_wake_budget_refuses_a_fourth_wake_in_the_window(
    manager: AppLifecycleManager, supervisor: FakeSupervisor
) -> None:
    clock = manager.clock
    assert isinstance(clock, FakeClock)
    for _ in range(WAKE_BUDGET_COUNT):
        assert manager.wake("docs") is ParkedPageKind.STARTING
        supervisor.statename_by_program["docs"] = "STOPPED"

    assert manager.wake("docs") is ParkedPageKind.FAILED
    assert len(supervisor.started) == WAKE_BUDGET_COUNT

    clock.now += WAKE_BUDGET_WINDOW_SECONDS + 1
    assert manager.wake("docs") is ParkedPageKind.STARTING


def test_a_wake_that_ends_in_fatal_re_parks_with_the_failure_page(
    manager: AppLifecycleManager, closed_port: int, supervisor: FakeSupervisor
) -> None:
    manager.sweep_once()
    send_raw_get_over_socket(closed_port)
    supervisor.statename_by_program["docs"] = "FATAL"

    manager.sweep_once()

    assert manager.is_app_parked("docs")
    # The next request wakes it again (within the budget) and is told where to look while it waits.
    answer = send_raw_get_over_socket(closed_port)
    assert b"Docs could not start" in answer and b"supervisorctl tail docs stderr" in answer
    assert supervisor.started == ["docs", "docs"]
    # Once the app has run again, a later stop is an ordinary one and its request is answered as starting.
    supervisor.statename_by_program["docs"] = "RUNNING"
    manager.sweep_once()
    supervisor.statename_by_program["docs"] = "STOPPED"
    manager.sweep_once()
    assert b"Starting Docs" in send_raw_get_over_socket(closed_port)


def test_stop_app_stops_the_program_and_refuses_what_it_cannot_stop(
    manager: AppLifecycleManager, supervisor: FakeSupervisor
) -> None:
    supervisor.statename_by_program["docs"] = "RUNNING"
    manager.stop_app("docs")
    assert supervisor.stopped == ["docs"]
    with pytest.raises(AppLifecycleRefusedError):
        manager.stop_app("shell")


def test_wake_and_wait_answers_once_the_app_accepts(
    manager: AppLifecycleManager, closed_port: int, supervisor: FakeSupervisor
) -> None:
    entry = manager.inventory.entry("docs")
    assert entry is not None and entry.is_running is False
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    is_bound = [False]

    def start(program: str) -> None:
        supervisor.start(program)
        if not is_bound[0]:
            listener.bind(("127.0.0.1", closed_port))
            listener.listen(1)
            is_bound[0] = True

    waiting = AppLifecycleManager(
        inventory=manager.inventory,
        is_enabled=False,
        count_windows_of_app=lambda app: 0,
        program_states=supervisor.states,
        start_program=start,
        stop_program=supervisor.stop,
    )
    try:
        assert waiting.wake_and_wait(entry, timeout_seconds=5.0) is True
    finally:
        listener.close()
    # An app that never binds is answered False once the wait is up, and a running one at once.
    is_bound[0] = True
    assert waiting.wake_and_wait(entry, timeout_seconds=0.3) is False
    running = manager.inventory.entry("plain")
    assert running is not None and waiting.wake_and_wait(running, timeout_seconds=0.1) is True


def test_wake_and_wait_gives_up_once_the_manager_is_stopped(
    manager: AppLifecycleManager, supervisor: FakeSupervisor
) -> None:
    """A launch in flight while the shell stops is answered False at once rather than polled to its deadline
    (the fake clock never reaches one, so a poll that ignored the stop would never return)."""
    entry = manager.inventory.entry("docs")
    assert entry is not None and entry.is_running is False
    manager.stop()

    assert manager.wake_and_wait(entry, timeout_seconds=5.0) is False
    assert supervisor.started == ["docs"]


def _clock_of(manager: AppLifecycleManager) -> FakeClock:
    clock = manager.clock
    assert isinstance(clock, FakeClock)
    return clock


def test_a_running_app_with_no_window_is_stopped_after_the_grace_period_once_visited(
    manager: AppLifecycleManager, supervisor: FakeSupervisor
) -> None:
    supervisor.statename_by_program["docs"] = "RUNNING"
    clock = _clock_of(manager)

    # Before anyone has arrived, no window says nothing about use.
    manager.sweep_once()
    clock.now += NO_WINDOWS_GRACE_SECONDS + 1
    manager.sweep_once()
    assert supervisor.stopped == []

    manager.mark_visited()
    manager.sweep_once()
    assert supervisor.stopped == []
    clock.now += NO_WINDOWS_GRACE_SECONDS - 1
    manager.sweep_once()
    assert supervisor.stopped == []
    clock.now += 2
    manager.sweep_once()
    assert supervisor.stopped == ["docs"]
    # The stopped program is parked on the next pass.
    manager.sweep_once()
    assert manager.is_app_parked("docs")


def test_a_window_opening_within_the_grace_period_keeps_the_app(
    manager: AppLifecycleManager, supervisor: FakeSupervisor, windows: FakeWindows
) -> None:
    supervisor.statename_by_program["docs"] = "RUNNING"
    clock = _clock_of(manager)
    manager.mark_visited()
    manager.sweep_once()
    clock.now += NO_WINDOWS_GRACE_SECONDS / 2

    windows.count_by_app["docs"] = 1
    manager.sweep_once()
    clock.now += NO_WINDOWS_GRACE_SECONDS
    manager.sweep_once()
    assert supervisor.stopped == []

    # The clock starts over once the last window closes.
    windows.count_by_app["docs"] = 0
    manager.sweep_once()
    clock.now += NO_WINDOWS_GRACE_SECONDS - 1
    manager.sweep_once()
    assert supervisor.stopped == []
    clock.now += 2
    manager.sweep_once()
    assert supervisor.stopped == ["docs"]


def test_an_app_that_does_not_declare_the_field_is_never_stopped(
    tmp_path: Path, broadcaster: WebSocketBroadcaster, supervisor: FakeSupervisor
) -> None:
    registry_path = write_registry(
        tmp_path / "apps.toml", registry_row_toml("docs", "http://127.0.0.1:1", program="docs")
    )
    supervisor.statename_by_program["docs"] = "RUNNING"
    clock = FakeClock()
    keeping = AppLifecycleManager(
        inventory=build_inventory(registry_path, broadcaster),
        is_enabled=False,
        count_windows_of_app=lambda app: 0,
        program_states=supervisor.states,
        start_program=supervisor.start,
        stop_program=supervisor.stop,
        clock=clock,
    )
    keeping.mark_visited()
    keeping.sweep_once()
    clock.now += NO_WINDOWS_GRACE_SECONDS * 2
    keeping.sweep_once()

    assert supervisor.stopped == []


def test_stop_releases_every_parked_port(manager: AppLifecycleManager, closed_port: int) -> None:
    manager.sweep_once()
    assert manager.is_app_parked("docs")

    manager.stop()

    assert manager.parked_app_names() == []
    assert _is_refused(closed_port)
