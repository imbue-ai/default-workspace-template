"""Tests for the app lifecycle manager: parking, waking, the wake budget, and the stop verb."""

import socket
import time
from collections.abc import Callable
from collections.abc import Iterator
from pathlib import Path

import pytest

from imbue.mngr.utils.polling import wait_for
from imbue.system_interface.shell.app_lifecycle import AppLifecycleManager
from imbue.system_interface.shell.app_lifecycle import IDLE_SWEEP_INTERVAL_SECONDS
from imbue.system_interface.shell.app_lifecycle import NO_WINDOWS_GRACE_SECONDS
from imbue.system_interface.shell.app_lifecycle import ProgramAction
from imbue.system_interface.shell.app_lifecycle import ProgramStatesReader
from imbue.system_interface.shell.app_lifecycle import TRANSITION_SWEEP_INTERVAL_SECONDS
from imbue.system_interface.shell.app_lifecycle import WAKE_BUDGET_COUNT
from imbue.system_interface.shell.app_lifecycle import WAKE_BUDGET_WINDOW_SECONDS
from imbue.system_interface.shell.app_lifecycle import WindowCountsReader
from imbue.system_interface.shell.app_lifecycle import recent_wake_times
from imbue.system_interface.shell.errors import AppLifecycleRefusedError
from imbue.system_interface.shell.errors import SupervisorProgramActionError
from imbue.system_interface.shell.inventory import AppInventory
from imbue.system_interface.shell.port_parking import ParkedPageKind
from imbue.system_interface.shell.share_grants import GrantedAppsReader
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
    """How many windows each app has, as the manager's rule reads it, counting the reads."""

    def __init__(self) -> None:
        self.count_by_app: dict[str, int] = {}
        self.read_count = 0

    def get_counts(self) -> dict[str, int]:
        self.read_count += 1
        return dict(self.count_by_app)


@pytest.fixture
def windows() -> FakeWindows:
    return FakeWindows()


class FakeShareGrants:
    """Which apps carry a per-app share grant, as the manager's rule reads it, counting the reads."""

    def __init__(self) -> None:
        self.granted: set[str] = set()
        self.read_count = 0

    def get_granted(self) -> set[str]:
        self.read_count += 1
        return set(self.granted)


@pytest.fixture
def share_grants() -> FakeShareGrants:
    return FakeShareGrants()


def _manager_over(
    inventory: AppInventory,
    supervisor: FakeSupervisor,
    count_windows_by_app: WindowCountsReader | None = None,
    granted_app_names: GrantedAppsReader | None = None,
    program_states: ProgramStatesReader | None = None,
    start_program: ProgramAction | None = None,
    clock: Callable[[], float] | None = None,
    is_enabled: bool = False,
) -> AppLifecycleManager:
    """A manager over the fake supervisor's verbs, no windows, no share grants, and a fresh fake clock, any of
    which a test replaces with its own; not enabled unless a test says so, so each test sweeps it by hand."""
    return AppLifecycleManager(
        inventory=inventory,
        is_enabled=is_enabled,
        count_windows_by_app=count_windows_by_app if count_windows_by_app is not None else lambda: {},
        granted_app_names=granted_app_names if granted_app_names is not None else lambda: set(),
        program_states=program_states if program_states is not None else supervisor.states,
        start_program=start_program if start_program is not None else supervisor.start,
        stop_program=supervisor.stop,
        clock=clock if clock is not None else FakeClock(),
    )


@pytest.fixture
def manager(
    tmp_path: Path,
    broadcaster: WebSocketBroadcaster,
    closed_port: int,
    supervisor: FakeSupervisor,
    windows: FakeWindows,
    share_grants: FakeShareGrants,
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
    built = _manager_over(
        build_inventory(registry_path, broadcaster, prober=prober),
        supervisor,
        count_windows_by_app=windows.get_counts,
        granted_app_names=share_grants.get_granted,
    )
    try:
        yield built
    finally:
        built.stop()


def test_a_pass_parks_a_stopped_stoppable_app_and_nothing_else(
    manager: AppLifecycleManager, supervisor: FakeSupervisor
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
    assert can_bind_loopback_port(closed_port)
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
    assert can_bind_loopback_port(closed_port) and not can_bind_loopback_port(moved_port)

    write_registry(registry_path, registry_row_toml("plain", "http://127.0.0.1:1"))
    manager.inventory.reload_registry()
    manager.sweep_once()

    assert manager.parked_app_names() == []
    assert can_bind_loopback_port(moved_port)
    assert supervisor.started == []


def test_a_pass_forgets_the_bookkeeping_of_an_app_it_no_longer_reaches(
    manager: AppLifecycleManager, closed_port: int, supervisor: FakeSupervisor
) -> None:
    """An app's wake in flight, failed mark, and wake times go with its parker when its row leaves the registry:
    kept, the wake awaiting its outcome would hold the sweep at its transition pace for the rest of the shell's
    life, and the failed mark would answer the app's first request as a failed start if it registered again."""
    registry_path = manager.inventory.registry_path
    docs_row = registry_row_toml("docs", f"http://127.0.0.1:{closed_port}", program="docs", stop_when_no_windows=True)

    def register(*rows: str) -> None:
        write_registry(registry_path, *rows)
        manager.inventory.reload_registry()

    # A wake awaiting its outcome when the row leaves: the sweep idles once the pass no longer reaches the app.
    assert manager.wake("docs") is ParkedPageKind.STARTING
    assert manager.sweep_interval_seconds() == TRANSITION_SWEEP_INTERVAL_SECONDS
    register(registry_row_toml("plain", "http://127.0.0.1:1"))
    manager.sweep_once()
    assert manager.parked_app_names() == []
    assert manager.sweep_interval_seconds() == IDLE_SWEEP_INTERVAL_SECONDS

    # A wake that ended in FATAL when the row leaves: the app registered again is parked as any stopped app is,
    # and its first request is answered as starting.
    register(docs_row)
    assert manager.wake("docs") is ParkedPageKind.STARTING
    supervisor.statename_by_program["docs"] = "FATAL"
    manager.sweep_once()
    assert manager.is_app_parked("docs")
    register(registry_row_toml("plain", "http://127.0.0.1:1"))
    manager.sweep_once()
    assert manager.parked_app_names() == []
    register(docs_row)
    manager.sweep_once()
    assert manager.is_app_parked("docs")
    assert b"Starting Docs" in send_raw_get_over_socket(closed_port)
    assert supervisor.started == ["docs", "docs", "docs"]


def test_a_pass_does_not_park_an_app_woken_after_its_state_was_read(
    manager: AppLifecycleManager, closed_port: int, supervisor: FakeSupervisor
) -> None:
    """A wake that lands between a pass's state read and its park (the parker's first connection, the start
    route, a POST launch) has started the program; parking on the stale STOPPED reading would take the port the
    app is about to bind. The pass leaves it, and the next one reads the outcome."""
    clock = _clock_of(manager)
    read_count = [0]

    def read_states_then_wake() -> dict[str, str] | None:
        states = supervisor.states()
        read_count[0] += 1
        if read_count[0] == 1:
            clock.now += 1
            racing.wake("docs")
        return states

    racing = _manager_over(manager.inventory, supervisor, program_states=read_states_then_wake, clock=clock)
    try:
        racing.sweep_once()
        assert supervisor.started == ["docs"]
        assert racing.parked_app_names() == [] and can_bind_loopback_port(closed_port)

        # The next pass reads the wake's outcome: STARTING parks nothing, and a STOPPED someone else caused does.
        racing.sweep_once()
        assert racing.parked_app_names() == []
        supervisor.statename_by_program["docs"] = "STOPPED"
        racing.sweep_once()
        assert racing.parked_app_names() == ["docs"]
    finally:
        racing.stop()


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
    assert can_bind_loopback_port(closed_port)
    with pytest.raises(AppLifecycleRefusedError):
        manager.wake("shell")
    with pytest.raises(AppLifecycleRefusedError):
        manager.wake("plain")


def test_recent_wake_times_keeps_only_the_window() -> None:
    now = 1000.0
    inside = now - WAKE_BUDGET_WINDOW_SECONDS + 1
    on_the_edge = now - WAKE_BUDGET_WINDOW_SECONDS
    past = now - WAKE_BUDGET_WINDOW_SECONDS - 1
    assert recent_wake_times([], now) == []
    assert recent_wake_times([past, on_the_edge, inside, now], now) == [inside, now]


def test_the_wake_budget_refuses_a_fourth_wake_in_the_window(
    manager: AppLifecycleManager, supervisor: FakeSupervisor
) -> None:
    clock = _clock_of(manager)
    for _ in range(WAKE_BUDGET_COUNT):
        assert manager.wake("docs") is ParkedPageKind.STARTING
        supervisor.statename_by_program["docs"] = "STOPPED"

    assert manager.wake("docs") is ParkedPageKind.FAILED
    assert len(supervisor.started) == WAKE_BUDGET_COUNT

    clock.now += WAKE_BUDGET_WINDOW_SECONDS + 1
    assert manager.wake("docs") is ParkedPageKind.STARTING


def test_a_wake_after_which_the_app_runs_spends_none_of_the_budget(
    manager: AppLifecycleManager, supervisor: FakeSupervisor
) -> None:
    """An app opened, closed, and stopped by the no-window rule a few times in five minutes is woken every time: the
    budget is for an app that cannot come up, and a pass that sees the app RUNNING forgets the wakes before it."""
    clock = _clock_of(manager)
    for _ in range(WAKE_BUDGET_COUNT + 2):
        assert manager.wake("docs") is ParkedPageKind.STARTING
        supervisor.statename_by_program["docs"] = "RUNNING"
        clock.now += 1
        manager.sweep_once()
        supervisor.statename_by_program["docs"] = "STOPPED"
        clock.now += 1
    assert len(supervisor.started) == WAKE_BUDGET_COUNT + 2

    # Wakes the app never came up from still count.
    for _ in range(WAKE_BUDGET_COUNT):
        assert manager.wake("docs") is ParkedPageKind.STARTING
        supervisor.statename_by_program["docs"] = "STOPPED"
        manager.sweep_once()
    assert manager.wake("docs") is ParkedPageKind.FAILED


def test_a_wake_supervisord_refuses_is_answered_as_failed_and_awaits_no_outcome(
    manager: AppLifecycleManager, closed_port: int, supervisor: FakeSupervisor
) -> None:
    """A start supervisord refuses (an unknown program, a spawn error) leaves no wake in flight: the sweep keeps
    its idle pace rather than waiting on an outcome that never comes, and the next pass parks the port with the
    failure page."""

    def refuse(program: str) -> None:
        raise SupervisorProgramActionError(f"supervisord refused to start {program!r}")

    refusing = _manager_over(manager.inventory, supervisor, start_program=refuse)
    try:
        assert refusing.wake("docs") is ParkedPageKind.FAILED
        assert supervisor.started == []
        assert refusing.sweep_interval_seconds() == IDLE_SWEEP_INTERVAL_SECONDS

        refusing.sweep_once()

        assert refusing.is_app_parked("docs")
        assert b"Docs could not start" in send_raw_get_over_socket(closed_port)
    finally:
        refusing.stop()


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

    waiting = _manager_over(manager.inventory, supervisor, start_program=start, clock=time.monotonic)
    try:
        assert waiting.wake_and_wait(entry, timeout_seconds=5.0) is True
    finally:
        listener.close()
    # An app that never binds is answered False once the wait is up, and a running one at once.
    is_bound[0] = True
    assert waiting.wake_and_wait(entry, timeout_seconds=0.3) is False
    running = manager.inventory.entry("plain")
    assert running is not None and waiting.wake_and_wait(running, timeout_seconds=0.1) is True


def test_wake_and_wait_gives_up_once_the_port_is_parked_again(
    manager: AppLifecycleManager, closed_port: int, supervisor: FakeSupervisor
) -> None:
    """A wake that ends in FATAL has the sweep park the port again with the failure page; the wait must give up
    then rather than poll the port, since a connect on a parked port is itself a wake (of a program that just
    failed) and would read the parker as the app."""
    entry = manager.inventory.entry("docs")
    assert entry is not None and entry.is_running is False
    clock = _clock_of(manager)

    def start_then_fail_and_sweep(program: str) -> None:
        supervisor.start(program)
        supervisor.statename_by_program[program] = "FATAL"
        clock.now += 1
        failing.sweep_once()

    failing = _manager_over(manager.inventory, supervisor, start_program=start_then_fail_and_sweep, clock=clock)
    try:
        assert failing.wake_and_wait(entry, timeout_seconds=5.0) is False
        assert failing.is_app_parked("docs")
    finally:
        failing.stop()
    assert supervisor.started == ["docs"]


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


def _assert_docs_stops_only_once_the_grace_elapses(manager: AppLifecycleManager, supervisor: FakeSupervisor) -> None:
    """From an idle mark just set: a pass a second short of the grace period stops nothing, the pass after it stops
    ``docs``."""
    clock = _clock_of(manager)
    clock.now += NO_WINDOWS_GRACE_SECONDS - 1
    manager.sweep_once()
    assert supervisor.stopped == []
    clock.now += 2
    manager.sweep_once()
    assert supervisor.stopped == ["docs"]


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
    _assert_docs_stops_only_once_the_grace_elapses(manager, supervisor)
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
    _assert_docs_stops_only_once_the_grace_elapses(manager, supervisor)


def test_an_app_with_a_per_app_share_grant_is_kept_running_until_the_grant_goes(
    manager: AppLifecycleManager, supervisor: FakeSupervisor, share_grants: FakeShareGrants
) -> None:
    """A visitor admitted to one app's origin alone never loads the shell, so no window of theirs exists to count:
    the grant stands in for one, and the clock starts only once the grant is gone."""
    supervisor.statename_by_program["docs"] = "RUNNING"
    clock = _clock_of(manager)
    manager.mark_visited()
    share_grants.granted = {"docs"}
    manager.sweep_once()
    clock.now += NO_WINDOWS_GRACE_SECONDS * 2
    manager.sweep_once()
    assert supervisor.stopped == []

    share_grants.granted = set()
    manager.sweep_once()
    _assert_docs_stops_only_once_the_grace_elapses(manager, supervisor)


def test_a_pass_reads_the_share_grants_at_most_once_and_only_for_an_app_with_no_window(
    tmp_path: Path,
    broadcaster: WebSocketBroadcaster,
    closed_port: int,
    supervisor: FakeSupervisor,
    windows: FakeWindows,
    share_grants: FakeShareGrants,
) -> None:
    notes_port = find_free_port()
    registry_path = write_registry(
        tmp_path / "apps.toml",
        registry_row_toml("docs", f"http://127.0.0.1:{closed_port}", program="docs", stop_when_no_windows=True),
        registry_row_toml("notes", f"http://127.0.0.1:{notes_port}", program="notes", stop_when_no_windows=True),
    )
    supervisor.statename_by_program.update({"docs": "RUNNING", "notes": "RUNNING"})
    windows.count_by_app.update({"docs": 1, "notes": 1})
    reading = _manager_over(
        build_inventory(registry_path, broadcaster),
        supervisor,
        count_windows_by_app=windows.get_counts,
        granted_app_names=share_grants.get_granted,
    )
    try:
        reading.mark_visited()
        # Every app has a window: the grants are never consulted.
        reading.sweep_once()
        assert share_grants.read_count == 0

        # Two windowless apps in one pass share one read of the grants.
        windows.count_by_app.update({"docs": 0, "notes": 0})
        reading.sweep_once()
        assert share_grants.read_count == 1
    finally:
        reading.stop()


def test_an_app_that_does_not_declare_the_field_is_never_stopped(
    tmp_path: Path, broadcaster: WebSocketBroadcaster, supervisor: FakeSupervisor
) -> None:
    registry_path = write_registry(
        tmp_path / "apps.toml", registry_row_toml("docs", "http://127.0.0.1:1", program="docs")
    )
    supervisor.statename_by_program["docs"] = "RUNNING"
    clock = FakeClock()
    keeping = _manager_over(build_inventory(registry_path, broadcaster), supervisor, clock=clock)
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
    assert can_bind_loopback_port(closed_port)


def test_a_started_manager_parks_a_stopped_app_at_once(
    manager: AppLifecycleManager, supervisor: FakeSupervisor, closed_port: int
) -> None:
    """The sweep's first pass does not wait out the idle interval (longer than this wait): a stopped app's port is
    held the moment the shell is up, since supervisord and the stopped app outlive a shell restart."""
    started = _manager_over(manager.inventory, supervisor, clock=time.monotonic, is_enabled=True)
    try:
        started.start()
        wait_for(lambda: started.is_app_parked("docs"), timeout=5.0, poll_interval=0.02)
        assert not can_bind_loopback_port(closed_port)
    finally:
        started.stop()


def test_the_sweep_runs_at_its_transition_pace_while_a_program_is_between_states(
    manager: AppLifecycleManager, supervisor: FakeSupervisor
) -> None:
    """A stopped program's port is parked on the pass after it exits, so a sweep at its idle pace would leave the
    port refused for up to that interval after a Quit; a program starting, retrying, stopping, or just told to
    stop keeps the sweep quick until it settles."""
    supervisor.statename_by_program["docs"] = "RUNNING"
    manager.sweep_once()
    assert manager.sweep_interval_seconds() == IDLE_SWEEP_INTERVAL_SECONDS

    for statename in ("STARTING", "BACKOFF", "STOPPING"):
        supervisor.statename_by_program["docs"] = statename
        manager.sweep_once()
        assert manager.sweep_interval_seconds() == TRANSITION_SWEEP_INTERVAL_SECONDS, statename

    # Parked, the app is in transition until a request wakes it; running again, the sweep idles.
    supervisor.statename_by_program["docs"] = "STOPPED"
    manager.sweep_once()
    assert manager.is_app_parked("docs")
    assert manager.sweep_interval_seconds() == TRANSITION_SWEEP_INTERVAL_SECONDS
    supervisor.statename_by_program["docs"] = "RUNNING"
    manager.sweep_once()
    assert manager.sweep_interval_seconds() == IDLE_SWEEP_INTERVAL_SECONDS

    # The pass that stops an app for having no window reports it between states at once.
    clock = _clock_of(manager)
    manager.mark_visited()
    manager.sweep_once()
    clock.now += NO_WINDOWS_GRACE_SECONDS + 1
    manager.sweep_once()
    assert supervisor.stopped == ["docs"]
    assert manager.sweep_interval_seconds() == TRANSITION_SWEEP_INTERVAL_SECONDS


def test_a_pass_reads_the_window_counts_at_most_once_and_only_when_an_app_needs_them(
    tmp_path: Path,
    broadcaster: WebSocketBroadcaster,
    closed_port: int,
    supervisor: FakeSupervisor,
    windows: FakeWindows,
) -> None:
    """The counts come from one read of every desktop (a file read under the shell's state lock): a pass over
    several running apps that declare the field reads them once, and a pass with no such app reads nothing."""
    notes_port = find_free_port()
    registry_path = write_registry(
        tmp_path / "apps.toml",
        registry_row_toml("docs", f"http://127.0.0.1:{closed_port}", program="docs", stop_when_no_windows=True),
        registry_row_toml("notes", f"http://127.0.0.1:{notes_port}", program="notes", stop_when_no_windows=True),
        registry_row_toml("keeper", "http://127.0.0.1:1", program="keeper"),
    )
    supervisor.statename_by_program.update({"docs": "RUNNING", "notes": "RUNNING", "keeper": "RUNNING"})
    windows.count_by_app["notes"] = 1
    counting = _manager_over(
        build_inventory(registry_path, broadcaster), supervisor, count_windows_by_app=windows.get_counts
    )
    try:
        counting.mark_visited()
        counting.sweep_once()
        assert windows.read_count == 1
        counting.sweep_once()
        assert windows.read_count == 2

        # With every app that declares the field down, the rule has nothing to apply to and reads nothing.
        supervisor.statename_by_program.update({"docs": "STOPPED", "notes": "STOPPED"})
        counting.sweep_once()
        assert windows.read_count == 2
        assert counting.parked_app_names() == ["docs", "notes"]
    finally:
        counting.stop()
