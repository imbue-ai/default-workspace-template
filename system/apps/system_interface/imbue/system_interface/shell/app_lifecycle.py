"""The app lifecycle manager: the one owner of every stop, start, park, and wake of a stoppable app (the
stop-when-no-windows spec, sections 5 and 6).

Its sweep reads every supervised program's state in one supervisord RPC, parks the port of each stoppable app
that is down (``shell/port_parking.py``), releases a parker whose app is up or about to bind again, re-parks
an app that failed to start with the failure page, and stops a running app that declares
``stop_when_no_windows`` once no window on any desktop has shown it for the grace period, provided a client has
arrived at the shell since it started (spec section 6: before anyone has looked at the workspace, "no windows"
says nothing about use, and the apps that deliver something on the first visit need to be running for it). A
wake releases the parker before asking supervisord to start the program, so the app's first bind never collides
with the shell's listener, and is budgeted so a broken app cannot be restarted by every reload (a wake that brings
the app up spends nothing, so an app opened and closed a few times in five minutes is not refused). Supervisord
access is injectable, and the sweep thread runs (and a POST launch wakes a stopped app) only when the manager is
enabled: never in a preview shell, whose registry is a copy of the live one, and in tests only when a test says
so.
"""

import functools
import socket
import threading
import time
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.system_interface.shell.data_types import AppInventoryEntry
from imbue.system_interface.shell.data_types import stoppable_program_of
from imbue.system_interface.shell.errors import AppLifecycleRefusedError
from imbue.system_interface.shell.errors import PortInUseError
from imbue.system_interface.shell.errors import PortParkingError
from imbue.system_interface.shell.errors import SupervisorProgramActionError
from imbue.system_interface.shell.inventory import AppInventory
from imbue.system_interface.shell.liveness import SUPERVISOR_BACKOFF_STATENAME
from imbue.system_interface.shell.liveness import SUPERVISOR_DOWN_STATENAMES
from imbue.system_interface.shell.liveness import SUPERVISOR_FATAL_STATENAME
from imbue.system_interface.shell.liveness import SUPERVISOR_RUNNING_STATENAME
from imbue.system_interface.shell.liveness import SUPERVISOR_STARTING_STATENAME
from imbue.system_interface.shell.liveness import fetch_supervisor_program_statenames
from imbue.system_interface.shell.liveness import start_supervisor_program
from imbue.system_interface.shell.liveness import stop_supervisor_program
from imbue.system_interface.shell.liveness import supervisor_socket_path
from imbue.system_interface.shell.port_parking import ParkedPageKind
from imbue.system_interface.shell.port_parking import ParkedPort
from imbue.system_interface.shell.port_parking import ParkingTarget
from imbue.system_interface.shell.port_parking import parked_page_html
from imbue.system_interface.shell.port_parking import parking_target_of
from imbue.system_interface.shell.share_grants import GrantedAppsReader

TRANSITION_SWEEP_INTERVAL_SECONDS: Final[float] = 2.0
IDLE_SWEEP_INTERVAL_SECONDS: Final[float] = 10.0
# How many wakes that do not bring an app up it gets in a window before its page stops asking for more (spec
# section 5.5); a wake after which the app is seen RUNNING spends nothing.
WAKE_BUDGET_COUNT: Final[int] = 3
WAKE_BUDGET_WINDOW_SECONDS: Final[float] = 300.0
# How long an app must have shown no window before it is stopped (spec section 6.1).
NO_WINDOWS_GRACE_SECONDS: Final[float] = 60.0
# How long a POST launch waits for a woken app to accept a connection (spec section 5.6).
WAKE_WAIT_SECONDS: Final[float] = 20.0
_WAKE_WAIT_POLL_SECONDS: Final[float] = 0.2
_WAKE_WAIT_CONNECT_TIMEOUT_SECONDS: Final[float] = 0.5

ProgramStatesReader = Callable[[], dict[str, str] | None]
ProgramAction = Callable[[str], None]
# How many windows across every desktop show each app, by app name; an app with none may be absent.
WindowCountsReader = Callable[[], Mapping[str, int]]


def read_supervisor_program_statenames() -> dict[str, str] | None:
    return fetch_supervisor_program_statenames(supervisor_socket_path())


def start_supervisor_program_by_name(program: str) -> None:
    start_supervisor_program(program, supervisor_socket_path())


def stop_supervisor_program_by_name(program: str) -> None:
    stop_supervisor_program(program, supervisor_socket_path())


@pure
def recent_wake_times(wake_times: Sequence[float], now: float) -> list[float]:
    """The wake times still inside the budget window at ``now``, in their order."""
    return [wake_time for wake_time in wake_times if now - wake_time < WAKE_BUDGET_WINDOW_SECONDS]


@pure
def is_wake_budget_spent(wake_times: Sequence[float], now: float) -> bool:
    return len(recent_wake_times(wake_times, now)) >= WAKE_BUDGET_COUNT


class AppLifecycleManager(MutableModel):
    """Parks, wakes, starts, and stops stoppable apps; see the module docstring."""

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    inventory: AppInventory = Field(frozen=True, description="The registry rows and each app's liveness")
    is_enabled: bool = Field(
        frozen=True,
        description="Whether the manager owns the live workspace's app lifecycles: its sweep runs, and a POST "
        "launch wakes a stopped app. A preview shell and most tests leave it off; the verbs a caller asks for "
        "(wake, stop) act regardless",
    )
    count_windows_by_app: WindowCountsReader = Field(
        frozen=True,
        description="Every app's window count across every desktop in one read (a file read under the shell's "
        "state lock), taken at most once per sweep pass and only when a pass has a running app that declares "
        "stop_when_no_windows",
    )
    granted_app_names: GrantedAppsReader = Field(
        frozen=True,
        description="Every app with a per-app share grant in one read (the gateway's grants document), taken at most "
        "once per sweep pass and only when a pass finds a running app that declares stop_when_no_windows and has no "
        "window: a visitor admitted to one app's origin alone never loads the shell, so no window of theirs exists",
    )
    no_windows_grace_seconds: float = Field(
        default=NO_WINDOWS_GRACE_SECONDS,
        frozen=True,
        description="How long an app goes without a window before a stop",
    )
    idle_sweep_interval_seconds: float = Field(
        default=IDLE_SWEEP_INTERVAL_SECONDS,
        frozen=True,
        description="How often the sweep runs while nothing is in transition",
    )
    program_states: ProgramStatesReader = Field(
        default=read_supervisor_program_statenames,
        frozen=True,
        description="Every supervised program's statename, or None when supervisord cannot be reached",
    )
    start_program: ProgramAction = Field(default=start_supervisor_program_by_name, frozen=True)
    stop_program: ProgramAction = Field(default=stop_supervisor_program_by_name, frozen=True)
    clock: Callable[[], float] = Field(default=time.monotonic, frozen=True, description="A monotonic clock")

    _lock: threading.RLock = PrivateAttr(default_factory=threading.RLock)
    _parked_by_app: dict[str, ParkedPort] = PrivateAttr(default_factory=dict)
    _wake_times_by_app: dict[str, list[float]] = PrivateAttr(default_factory=dict)
    _apps_awaiting_wake_outcome: set[str] = PrivateAttr(default_factory=set)
    _failed_apps: set[str] = PrivateAttr(default_factory=set)
    _idle_since_by_app: dict[str, float] = PrivateAttr(default_factory=dict)
    _is_any_app_between_states: bool = PrivateAttr(default=False)
    _is_visited: bool = PrivateAttr(default=False)
    _sweep_stop: threading.Event = PrivateAttr(default_factory=threading.Event)
    _sweep_wake: threading.Event = PrivateAttr(default_factory=threading.Event)
    _sweep_thread: threading.Thread | None = PrivateAttr(default=None)

    # Lifecycle

    def start(self) -> None:
        if not self.is_enabled:
            return
        # The first pass runs at once: supervisord and its programs outlive the shell, so after a restart a stopped
        # app's port must be held before the idle interval elapses.
        self._sweep_wake.set()
        thread = threading.Thread(target=self._run_sweep, daemon=True, name="app-lifecycle-sweep")
        self._sweep_thread = thread
        thread.start()

    def stop(self) -> None:
        self._sweep_stop.set()
        self._sweep_wake.set()
        if self._sweep_thread is not None:
            self._sweep_thread.join(timeout=5)
            self._sweep_thread = None
        with self._lock:
            parked = list(self._parked_by_app.values())
            self._parked_by_app.clear()
        for port in parked:
            port.release()

    def wake_soon(self) -> None:
        """Run a sweep pass as soon as the thread is free (a window closed or opened, a stop or start happened)."""
        self._sweep_wake.set()

    def mark_visited(self) -> None:
        """A client has arrived at the shell: the no-window rule applies from here on."""
        with self._lock:
            if not self._is_visited:
                self._is_visited = True
                logger.info(
                    "Marked the workspace visited: apps without windows now stop after {}s",
                    self.no_windows_grace_seconds,
                )
        self.wake_soon()

    # Reads

    @property
    def is_visited(self) -> bool:
        with self._lock:
            return self._is_visited

    def parked_app_names(self) -> list[str]:
        with self._lock:
            return sorted(self._parked_by_app)

    def is_app_parked(self, app: str) -> bool:
        with self._lock:
            return app in self._parked_by_app

    # The verbs

    def wake(self, app: str) -> ParkedPageKind:
        """Release the app's parked port (if any) and ask supervisord to start its program, within the wake budget.
        Answers STARTING when the program was asked to start, FAILED when the budget refused it or supervisord could
        not start it. Raises AppLifecycleRefusedError for an app the workspace cannot start."""
        program = self._stoppable_program_or_refuse(app, "started")
        now = self.clock()
        with self._lock:
            parked = self._parked_by_app.pop(app, None)
            # Only the window's worth of wake times is kept, so an app woken on every reload does not grow the list.
            recent = recent_wake_times(self._wake_times_by_app.get(app, ()), now)
            if is_wake_budget_spent(recent, now):
                logger.warning(
                    "Refused to wake {} again: {} wakes within {}s", app, WAKE_BUDGET_COUNT, WAKE_BUDGET_WINDOW_SECONDS
                )
                self._wake_times_by_app[app] = recent
                self._failed_apps.add(app)
                kind = ParkedPageKind.FAILED
            else:
                self._wake_times_by_app[app] = recent + [now]
                self._apps_awaiting_wake_outcome.add(app)
                self._failed_apps.discard(app)
                kind = ParkedPageKind.STARTING
        if parked is not None:
            parked.release()
        if kind is ParkedPageKind.STARTING:
            try:
                self.start_program(program)
                logger.info("Woke app {} (program {})", app, program)
            except SupervisorProgramActionError as e:
                logger.warning("Could not start {} for a wake: {}", app, e)
                # Nothing is in flight for the sweep to read the outcome of; the wake still counts against the budget.
                with self._lock:
                    self._failed_apps.add(app)
                    self._apps_awaiting_wake_outcome.discard(app)
                kind = ParkedPageKind.FAILED
        self.inventory.refresh_liveness()
        self.wake_soon()
        return kind

    def _wake_from_parked_port(self, app: str) -> ParkedPageKind:
        """The parker's first connection: wake the app, and answer the failure page when its last wake ended in
        FATAL or was refused (spec section 5.5), so the page names the program's log and reloads slowly; the wake
        itself still happens within the budget, and that reload is the next attempt."""
        with self._lock:
            was_failed = app in self._failed_apps
        kind = self.wake(app)
        return ParkedPageKind.FAILED if was_failed else kind

    def stop_app(self, app: str) -> None:
        """Ask supervisord to stop the app's program; the next sweep parks its port. Raises
        AppLifecycleRefusedError for an app the workspace cannot stop, and SupervisorProgramActionError as the stop
        route does."""
        program = self._stoppable_program_or_refuse(app, "stopped")
        self.stop_program(program)
        logger.info("Stopped app {} (program {})", app, program)
        self.inventory.refresh_liveness()
        self.wake_soon()

    def _stoppable_program_or_refuse(self, app: str, verb: str) -> str:
        """The supervised program the workspace may act on for the app; raises AppLifecycleRefusedError, naming
        the verb, for an unknown app and for one with no stoppable program."""
        entry = self.inventory.entry(app)
        program = stoppable_program_of(entry, self.inventory.entries()) if entry is not None else None
        if entry is None or program is None:
            raise AppLifecycleRefusedError(f"App {app!r} cannot be {verb} through the workspace")
        return program

    def wake_and_wait(self, entry: AppInventoryEntry, timeout_seconds: float = WAKE_WAIT_SECONDS) -> bool:
        """Wake a stoppable app that is not running and wait until its port accepts a connection; True when it does
        within the timeout, False when it does not, when the sweep parks the port again meanwhile (the wake ended in
        FATAL), or when the manager is stopped. An app that is running, or that cannot be parked, is answered True
        at once."""
        target = parking_target_of(str(entry.row.url))
        if entry.is_running or target is None:
            return True
        app = str(entry.row.name)
        kind = self.wake(app)
        if kind is not ParkedPageKind.STARTING:
            return False
        deadline = self.clock() + timeout_seconds
        while self.clock() < deadline:
            # A connect on a parked port is itself a wake, so a port parked again is checked for before every poll.
            if self.is_app_parked(app):
                return False
            if _is_accepting(target):
                return True
            if self._sweep_stop.wait(_WAKE_WAIT_POLL_SECONDS):
                return False
        return False

    # The sweep

    def _run_sweep(self) -> None:
        while not self._sweep_stop.is_set():
            self._sweep_wake.wait(timeout=self.sweep_interval_seconds())
            self._sweep_wake.clear()
            if self._sweep_stop.is_set():
                return
            try:
                self.sweep_once()
            except (OSError, ValueError) as e:
                logger.opt(exception=e).error("The app lifecycle sweep failed; the next pass will retry")

    def sweep_interval_seconds(self) -> float:
        """How long the sweep waits before its next pass: the transition pace while any port is parked, a wake is
        awaiting its outcome, or the last pass found a program between states; the idle pace otherwise."""
        with self._lock:
            is_transitioning = (
                bool(self._parked_by_app) or bool(self._apps_awaiting_wake_outcome) or self._is_any_app_between_states
            )
        return (
            min(TRANSITION_SWEEP_INTERVAL_SECONDS, self.idle_sweep_interval_seconds)
            if is_transitioning
            else self.idle_sweep_interval_seconds
        )

    def sweep_once(self) -> None:
        """One pass (spec sections 5.4 and 6.1): park every stoppable app that is down, release every parker whose
        app is up or retrying (or that the pass no longer reaches: the row left the registry, or its program is
        unknown to supervisord), note an app whose wake ended in FATAL, and stop a running app that has had no
        window for the grace period."""
        # A wake that lands after this moment makes the reading stale for its app (see ``_is_woken_since``).
        states_read_at = self.clock()
        statename_by_program = self.program_states()
        if statename_by_program is None:
            logger.debug("Skipped an app lifecycle pass: supervisord did not answer")
            return
        entries = self.inventory.entries()
        # One read of the desktops, and one of the share grants, serves every app the pass applies the no-window
        # rule to, and none is made when no app needs it.
        window_counts = functools.cache(self.count_windows_by_app)
        granted_apps = functools.cache(self.granted_app_names)
        reconciled_apps: set[str] = set()
        is_any_between_states = False
        for entry in entries:
            program = stoppable_program_of(entry, entries)
            if program is None:
                continue
            statename = statename_by_program.get(program)
            if statename is None:
                continue
            reconciled_apps.add(str(entry.row.name))
            is_settled = self._reconcile_app(entry, program, statename, states_read_at, window_counts, granted_apps)
            is_any_between_states = is_any_between_states or not is_settled
        with self._lock:
            self._is_any_app_between_states = is_any_between_states
        self._release_parked_except(reconciled_apps)

    def _release_parked_except(self, apps: AbstractSet[str]) -> None:
        """Let go of every parked port whose app the pass did not reconcile: the shell can no longer start the app,
        so holding its port would only strand whatever binds it next."""
        with self._lock:
            unreachable = [(app, parked) for app, parked in self._parked_by_app.items() if app not in apps]
            for app, _parked in unreachable:
                del self._parked_by_app[app]
        for app, parked in unreachable:
            parked.release()
            logger.info("Released the parked port of {}: the app is no longer one the shell can start", app)

    def _reconcile_app(
        self,
        entry: AppInventoryEntry,
        program: str,
        statename: str,
        states_read_at: float,
        window_counts: WindowCountsReader,
        granted_apps: GrantedAppsReader,
    ) -> bool:
        """Bring one app's parker in line with its program's state, and answer whether the app is settled: running
        and left running, or down with its port parked (or unparkable). A program starting, retrying, or stopping,
        one the no-window rule just stopped, and one woken after the pass read its state are between states, and
        the sweep keeps its transition pace until they settle, so the pass that parks a stopped port comes soon
        after the program exits. ``window_counts`` and ``granted_apps`` are the pass's one read of every app's
        windows and of the per-app share grants."""
        app = str(entry.row.name)
        if statename in (SUPERVISOR_RUNNING_STATENAME, SUPERVISOR_STARTING_STATENAME, SUPERVISOR_BACKOFF_STATENAME):
            # Up, or about to bind again on its own: the port must be free.
            with self._lock:
                parked = self._parked_by_app.pop(app, None)
                if statename == SUPERVISOR_RUNNING_STATENAME:
                    self._apps_awaiting_wake_outcome.discard(app)
                    self._failed_apps.discard(app)
                    self._forget_wakes_before(app, states_read_at)
            if parked is not None:
                parked.release()
            if statename == SUPERVISOR_BACKOFF_STATENAME:
                return False
            is_stopped_now = self._apply_no_window_rule(entry, program, window_counts, granted_apps)
            return statename == SUPERVISOR_RUNNING_STATENAME and not is_stopped_now
        with self._lock:
            self._idle_since_by_app.pop(app, None)
        if statename not in SUPERVISOR_DOWN_STATENAMES:
            # STOPPING: the port is still the app's until it exits.
            return False
        if self._is_woken_since(app, states_read_at):
            logger.debug("Left {} unparked this pass: it was woken after its state was read", app)
            return False
        target = parking_target_of(str(entry.row.url))
        with self._lock:
            if statename == SUPERVISOR_FATAL_STATENAME and app in self._apps_awaiting_wake_outcome:
                self._failed_apps.add(app)
                self._apps_awaiting_wake_outcome.discard(app)
            parked = self._parked_by_app.get(app)
            if parked is not None and parked.target == target:
                return True
            # A parker on a port the row no longer names (the app re-registered elsewhere) is let go first.
            stale = self._parked_by_app.pop(app, None)
        if stale is not None:
            stale.release()
            logger.info(
                "Released the parked port {}:{} of {}: its row moved", stale.target.host, stale.target.port, app
            )
        if target is not None:
            self._park(entry, program, target)
        return True

    def _forget_wakes_before(self, app: str, moment: float) -> None:
        """The app was running at ``moment``: every wake before it brought the app up and spends none of the budget,
        which is for an app that cannot come up, not one opened and closed a few times in five minutes. A wake after
        the reading is kept, both for the budget and for ``_is_woken_since``. Runs under the lock."""
        wake_times = self._wake_times_by_app.get(app)
        if wake_times:
            self._wake_times_by_app[app] = [wake_time for wake_time in wake_times if wake_time > moment]

    def _is_woken_since(self, app: str, moment: float) -> bool:
        """Whether a wake started the app's program after ``moment``: a state read before it is stale for the app,
        and a park on that reading would take the port the app is about to bind. The next pass reads the outcome."""
        with self._lock:
            wake_times = self._wake_times_by_app.get(app)
            return bool(wake_times) and wake_times[-1] > moment

    def _apply_no_window_rule(
        self,
        entry: AppInventoryEntry,
        program: str,
        window_counts: WindowCountsReader,
        granted_apps: GrantedAppsReader,
    ) -> bool:
        """Stop a running app that declares ``stop_when_no_windows`` once no window has shown it for the grace
        period, and only once the workspace has been visited (spec section 6.1); True when the program was told to
        stop. An app with a per-app share grant counts as one with windows: its visitors reach it without the
        shell, so their use leaves no window to count."""
        app = str(entry.row.name)
        if not entry.row.stop_when_no_windows or window_counts().get(app, 0) > 0 or app in granted_apps():
            with self._lock:
                self._idle_since_by_app.pop(app, None)
            return False
        now = self.clock()
        with self._lock:
            if not self._is_visited:
                return False
            idle_since = self._idle_since_by_app.setdefault(app, now)
            if now - idle_since < self.no_windows_grace_seconds:
                return False
            self._idle_since_by_app.pop(app, None)
        try:
            self.stop_program(program)
        except SupervisorProgramActionError as e:
            logger.warning("Could not stop {} after {}s without a window: {}", app, self.no_windows_grace_seconds, e)
            return False
        logger.info(
            "Stopped app {} (program {}): no window showed it for {}s", app, program, self.no_windows_grace_seconds
        )
        self.inventory.refresh_liveness()
        return True

    def _park(self, entry: AppInventoryEntry, program: str, target: ParkingTarget) -> None:
        app = str(entry.row.name)
        display_name = str(entry.row.display_name) if entry.row.display_name is not None else app
        parked = ParkedPort(
            app=app,
            target=target,
            on_first_connection=lambda: self._wake_from_parked_port(app),
            page_for=lambda kind: parked_page_html(kind, display_name, program),
        )
        # The parker is recorded under the lock its first connection's wake pops it with, so a connection that
        # arrives the moment the port binds cannot find no parker and leave a woken one on the books.
        with self._lock:
            try:
                parked.start()
            except PortInUseError:
                logger.debug("Left {} unparked: something listens on {}:{}", app, target.host, target.port)
                return
            except PortParkingError as e:
                logger.warning("Could not park {}: {}", app, e)
                return
            self._parked_by_app[app] = parked
        logger.info("Parked {} on {}:{} (program {} is down)", app, target.host, target.port, program)


def _is_accepting(target: ParkingTarget) -> bool:
    try:
        with socket.create_connection((target.host, target.port), timeout=_WAKE_WAIT_CONNECT_TIMEOUT_SECONDS):
            return True
    except OSError:
        return False


def build_app_lifecycle_manager(
    inventory: AppInventory,
    is_enabled: bool,
    count_windows_by_app: WindowCountsReader,
    granted_app_names: GrantedAppsReader,
    no_windows_grace_seconds: float = NO_WINDOWS_GRACE_SECONDS,
    idle_sweep_interval_seconds: float = IDLE_SWEEP_INTERVAL_SECONDS,
) -> AppLifecycleManager:
    return AppLifecycleManager(
        inventory=inventory,
        is_enabled=is_enabled,
        count_windows_by_app=count_windows_by_app,
        granted_app_names=granted_app_names,
        no_windows_grace_seconds=no_windows_grace_seconds,
        idle_sweep_interval_seconds=idle_sweep_interval_seconds,
    )
