"""The app lifecycle manager: the one owner of every stop, start, park, and wake of a stoppable app (the
stop-when-no-windows spec, sections 5 and 6).

Its sweep reads every supervised program's state in one supervisord RPC, parks the port of each stoppable app
that is down (``shell/port_parking.py``), releases a parker whose app is up or about to bind again, re-parks
an app that failed to start with the failure page, and stops a running app that declares
``stop_when_no_windows`` once no window on any desktop has shown it for the grace period, provided a client has
arrived at the shell since it started (spec section 6: before anyone has looked at the workspace, "no windows"
says nothing about use, and the apps that deliver something on the first visit need to be running for it). A wake releases the parker before asking supervisord to start
the program, so the app's first bind never collides with the shell's listener, and is budgeted so a broken app
cannot be restarted by every reload. Supervisord access is injectable, and the sweep thread runs only when the
manager is enabled (never in a preview shell, and in tests only when a test says so).
"""

import socket
import threading
import time
from collections.abc import Callable
from collections.abc import Sequence
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.system_interface.shell.data_types import AppInventoryEntry
from imbue.system_interface.shell.data_types import stoppable_program_of
from imbue.system_interface.shell.errors import AppWakeRefusedError
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

# The sweep runs often while any stoppable app is not running (a parked port, a stop or start in flight), and at
# the inventory's own pace otherwise.
TRANSITION_SWEEP_INTERVAL_SECONDS: Final[float] = 2.0
IDLE_SWEEP_INTERVAL_SECONDS: Final[float] = 10.0
# How many wakes an app gets in a window before its page stops asking for more (spec section 5.5).
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


def read_supervisor_program_statenames() -> dict[str, str] | None:
    return fetch_supervisor_program_statenames(supervisor_socket_path())


def start_supervisor_program_by_name(program: str) -> None:
    start_supervisor_program(program, supervisor_socket_path())


def stop_supervisor_program_by_name(program: str) -> None:
    stop_supervisor_program(program, supervisor_socket_path())


@pure
def is_wake_budget_spent(wake_times: Sequence[float], now: float) -> bool:
    recent = [wake_time for wake_time in wake_times if now - wake_time < WAKE_BUDGET_WINDOW_SECONDS]
    return len(recent) >= WAKE_BUDGET_COUNT


class AppLifecycleManager(MutableModel):
    """Parks, wakes, starts, and stops stoppable apps; see the module docstring."""

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    inventory: AppInventory = Field(frozen=True, description="The registry rows and each app's liveness")
    is_enabled: bool = Field(
        frozen=True, description="Whether the sweep thread runs; a preview shell and most tests leave it off"
    )
    count_windows_of_app: Callable[[str], int] = Field(
        frozen=True, description="How many windows across every desktop show the app, by app name"
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
    _woken_at_by_app: dict[str, float] = PrivateAttr(default_factory=dict)
    _failed_apps: set[str] = PrivateAttr(default_factory=set)
    _idle_since_by_app: dict[str, float] = PrivateAttr(default_factory=dict)
    _is_visited: bool = PrivateAttr(default=False)
    _sweep_stop: threading.Event = PrivateAttr(default_factory=threading.Event)
    _sweep_wake: threading.Event = PrivateAttr(default_factory=threading.Event)
    _sweep_thread: threading.Thread | None = PrivateAttr(default=None)

    # Lifecycle

    def start(self) -> None:
        if not self.is_enabled:
            return
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
                    "The workspace has been visited; apps without windows now stop after {}s",
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
        Answers what a request that caused the wake should be told. Raises AppWakeRefusedError for an app the
        workspace cannot start."""
        entry = self.inventory.entry(app)
        program = stoppable_program_of(entry, self.inventory.entries()) if entry is not None else None
        if entry is None or program is None:
            raise AppWakeRefusedError(f"App {app!r} cannot be started through the workspace")
        now = self.clock()
        with self._lock:
            parked = self._parked_by_app.pop(app, None)
            wake_times = self._wake_times_by_app.setdefault(app, [])
            if is_wake_budget_spent(wake_times, now):
                logger.warning(
                    "Refused to wake {} again: {} wakes within {}s", app, WAKE_BUDGET_COUNT, WAKE_BUDGET_WINDOW_SECONDS
                )
                self._failed_apps.add(app)
                kind = ParkedPageKind.FAILED
            else:
                wake_times.append(now)
                self._woken_at_by_app[app] = now
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
                with self._lock:
                    self._failed_apps.add(app)
                kind = ParkedPageKind.FAILED
        self.inventory.refresh_liveness()
        self.wake_soon()
        return kind

    def stop_app(self, app: str) -> None:
        """Ask supervisord to stop the app's program; the next sweep parks its port. Raises
        SupervisorProgramActionError as the stop route does."""
        entry = self.inventory.entry(app)
        program = stoppable_program_of(entry, self.inventory.entries()) if entry is not None else None
        if entry is None or program is None:
            raise AppWakeRefusedError(f"App {app!r} cannot be stopped through the workspace")
        self.stop_program(program)
        logger.info("Stopped app {} (program {})", app, program)
        self.inventory.refresh_liveness()
        self.wake_soon()

    def wake_and_wait(self, entry: AppInventoryEntry, timeout_seconds: float = WAKE_WAIT_SECONDS) -> bool:
        """Wake a stoppable app that is not running and wait until its port accepts a connection; True when it does
        within the timeout. An app that is running, or that cannot be parked, is answered True at once."""
        target = parking_target_of(str(entry.row.url))
        if entry.is_running or target is None:
            return True
        kind = self.wake(str(entry.row.name))
        if kind is not ParkedPageKind.STARTING:
            return False
        deadline = self.clock() + timeout_seconds
        while self.clock() < deadline:
            if _is_accepting(target):
                return True
            self._sweep_stop.wait(_WAKE_WAIT_POLL_SECONDS)
        return False

    # The sweep

    def _run_sweep(self) -> None:
        while not self._sweep_stop.is_set():
            self._sweep_wake.wait(timeout=self._sweep_interval_seconds())
            self._sweep_wake.clear()
            if self._sweep_stop.is_set():
                return
            try:
                self.sweep_once()
            except (OSError, ValueError) as e:
                logger.opt(exception=e).error("The app lifecycle sweep failed; the next pass will retry")

    def _sweep_interval_seconds(self) -> float:
        with self._lock:
            is_transitioning = bool(self._parked_by_app) or bool(self._woken_at_by_app)
        return (
            min(TRANSITION_SWEEP_INTERVAL_SECONDS, self.idle_sweep_interval_seconds)
            if is_transitioning
            else self.idle_sweep_interval_seconds
        )

    def sweep_once(self) -> None:
        """One pass (spec sections 5.4 and 6.1): park every stoppable app that is down, release every parker whose
        app is up or retrying, note an app whose wake ended in FATAL, and stop a running app that has had no window
        for the grace period."""
        statename_by_program = self.program_states()
        if statename_by_program is None:
            logger.debug("Skipped an app lifecycle pass: supervisord did not answer")
            return
        entries = self.inventory.entries()
        for entry in entries:
            program = stoppable_program_of(entry, entries)
            if program is None:
                continue
            statename = statename_by_program.get(program)
            if statename is None:
                continue
            self._reconcile_app(entry, program, statename)

    def _reconcile_app(self, entry: AppInventoryEntry, program: str, statename: str) -> None:
        app = str(entry.row.name)
        if statename in (SUPERVISOR_RUNNING_STATENAME, SUPERVISOR_STARTING_STATENAME, SUPERVISOR_BACKOFF_STATENAME):
            # Up, or about to bind again on its own: the port must be free.
            with self._lock:
                parked = self._parked_by_app.pop(app, None)
                if statename == SUPERVISOR_RUNNING_STATENAME:
                    self._woken_at_by_app.pop(app, None)
                    self._failed_apps.discard(app)
            if parked is not None:
                parked.release()
            if statename != SUPERVISOR_BACKOFF_STATENAME:
                self._apply_no_window_rule(entry, program)
            return
        with self._lock:
            self._idle_since_by_app.pop(app, None)
        if statename not in SUPERVISOR_DOWN_STATENAMES:
            # STOPPING: the port is still the app's until it exits.
            return
        with self._lock:
            if statename == SUPERVISOR_FATAL_STATENAME and app in self._woken_at_by_app:
                self._failed_apps.add(app)
                self._woken_at_by_app.pop(app, None)
            if app in self._parked_by_app:
                return
        self._park(entry, program)

    def _apply_no_window_rule(self, entry: AppInventoryEntry, program: str) -> None:
        """Stop a running app that declares ``stop_when_no_windows`` once no window has shown it for the grace
        period, and only once the workspace has been visited (spec section 6.1)."""
        app = str(entry.row.name)
        if not entry.row.stop_when_no_windows or self.count_windows_of_app(app) > 0:
            with self._lock:
                self._idle_since_by_app.pop(app, None)
            return
        now = self.clock()
        with self._lock:
            if not self._is_visited:
                return
            idle_since = self._idle_since_by_app.setdefault(app, now)
            if now - idle_since < self.no_windows_grace_seconds:
                return
            self._idle_since_by_app.pop(app, None)
        try:
            self.stop_program(program)
        except SupervisorProgramActionError as e:
            logger.warning("Could not stop {} after {}s without a window: {}", app, self.no_windows_grace_seconds, e)
            return
        logger.info(
            "Stopped app {} (program {}): no window showed it for {}s", app, program, self.no_windows_grace_seconds
        )
        self.inventory.refresh_liveness()

    def _park(self, entry: AppInventoryEntry, program: str) -> None:
        app = str(entry.row.name)
        target = parking_target_of(str(entry.row.url))
        if target is None:
            return
        display_name = str(entry.row.display_name) if entry.row.display_name is not None else app
        parked = ParkedPort(
            app=app,
            target=target,
            on_first_connection=lambda: self.wake(app),
            page_for=lambda kind: parked_page_html(kind, display_name, program),
        )
        try:
            parked.start()
        except PortInUseError:
            logger.debug("Left {} unparked: something listens on {}:{}", app, target.host, target.port)
            return
        except PortParkingError as e:
            logger.warning("Could not park {}: {}", app, e)
            return
        with self._lock:
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
    count_windows_of_app: Callable[[str], int],
    no_windows_grace_seconds: float = NO_WINDOWS_GRACE_SECONDS,
    idle_sweep_interval_seconds: float = IDLE_SWEEP_INTERVAL_SECONDS,
) -> AppLifecycleManager:
    return AppLifecycleManager(
        inventory=inventory,
        is_enabled=is_enabled,
        count_windows_of_app=count_windows_of_app,
        no_windows_grace_seconds=no_windows_grace_seconds,
        idle_sweep_interval_seconds=idle_sweep_interval_seconds,
    )
