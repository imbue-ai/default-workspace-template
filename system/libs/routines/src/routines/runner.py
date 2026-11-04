"""The runner: one run of a routine, the once-a-minute tick that starts the due ones, and the housekeeping."""

import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
from collections.abc import Callable
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field

from chat_background_tasks.markers import is_process_live
from chat_background_tasks.markers import process_start_time
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import format_nanosecond_iso_timestamp
from imbue.imbue_common.logging import generate_log_event_id
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.primitives import NonNegativeInt
from routines.data_types import CurrentRun
from routines.data_types import LastRun
from routines.data_types import LoadedRoutine
from routines.data_types import RUN_BLOCKED_EVENT
from routines.data_types import RUN_FINISHED_EVENT
from routines.data_types import RUN_STARTED_EVENT
from routines.data_types import RoutineDefinition
from routines.data_types import RoutineState
from routines.data_types import RunBlockedEvent
from routines.data_types import RunFinishedEvent
from routines.data_types import RunResult
from routines.data_types import RunStartedEvent
from routines.data_types import TickReport
from routines.data_types import WakeOutcome
from routines.errors import DefinitionValidationError
from routines.interfaces import ChatAppInterface
from routines.interfaces import ClockInterface
from routines.interfaces import DefinitionStoreInterface
from routines.interfaces import LedgerInterface
from routines.interfaces import NotifierInterface
from routines.interfaces import StateStoreInterface
from routines.locks import held_lock
from routines.locks import tried_lock
from routines.paths import RUNS_DIRNAME
from routines.paths import RoutinesPaths
from routines.primitives import LEDGER_DIRNAME
from routines.primitives import RUN_EVENTS_SOURCE
from routines.primitives import RoutineId
from routines.primitives import RunId
from routines.primitives import RunKind
from routines.primitives import RunOutcome
from routines.primitives import RunTrigger
from routines.primitives import RunWarning
from routines.schedule import is_switched_on
from routines.schedule import next_due
from routines.tails import read_tail
from routines.wake import slash_command_of
from routines.wake import wake_skill

OUTPUT_TAIL_BYTES: Final[int] = 4096
# A run finding its lock held tries again this often before it counts as refused: a removal holds the lock for
# an instant, and two spawns of one routine must end with one run and one refusal, not two refusals.
RUN_LOCK_RETRY_PAUSES: Final[tuple[float, ...]] = (0.1,) * 5
# After the timeout the process group is asked to stop, then killed if it has not.
TERMINATE_GRACE_SECONDS: Final[float] = 5.0
_GROUP_DRAIN_POLL_SECONDS: Final[float] = 0.05
RUN_DIR_RETENTION: Final[timedelta] = timedelta(days=90)
INTERRUPTED_SUMMARY: Final[str] = "The run was cut off before it finished, most likely by a restart."
TIMED_OUT_SUMMARY: Final[str] = "The run was stopped because it took longer than its time limit."
RUNNER_FAILED_SUMMARY: Final[str] = "The runner itself failed before the run could finish; its log has the error."
RUN_DIR_ENV: Final[str] = "ROUTINES_RUN_DIR"
RUN_ID_ENV: Final[str] = "ROUTINES_RUN_ID"
ROUTINE_ID_ENV: Final[str] = "ROUTINES_ROUTINE_ID"
_TIMED_OUT_EXIT_CODE: Final[int] = 124
# What a shell answers for a command it could not find, for a command whose executable is missing.
_MISSING_EXECUTABLE_EXIT_CODE: Final[int] = 127


class _Execution(FrozenModel):
    """What running the routine's work produced, before the books are closed."""

    outcome: RunOutcome = Field(description="How the work ended")
    exit_code: int | None = Field(description="The process's exit code; None for a skill run")
    summary: str = Field(description="One line about it, possibly empty")
    chat_id: str | None = Field(description="The chat a skill run, or a wake inside the run, used")
    warnings: tuple[RunWarning, ...] = Field(description="What was worth saying beside the outcome")


def _envelope(event_type: str, now: datetime) -> dict[str, str]:
    return {
        "timestamp": format_nanosecond_iso_timestamp(now),
        "type": event_type,
        "event_id": generate_log_event_id(),
        "source": RUN_EVENTS_SOURCE,
    }


def _read_output_tail(log_path: Path) -> str:
    """The log's last part for the finish event; empty for a run that wrote no log, or one whose log cannot be read."""
    try:
        text, _ = read_tail(log_path, OUTPUT_TAIL_BYTES)
    except FileNotFoundError:
        return ""
    except OSError as e:
        logger.warning("Recording the run without its output: the log {} could not be read ({})", log_path, e)
        return ""
    return text


def is_run_process_alive(current: CurrentRun) -> bool:
    """Whether the run's process still runs: the process the run recorded, not a later one given its pid."""
    return is_process_live(current.pid, current.pid_start)


def spawn_run_process(paths: RoutinesPaths, routine_id: RoutineId, trigger: RunTrigger, run_id: RunId) -> None:
    """A detached runner process for one run, its own output going to the run's log: a caller never waits on a run."""
    log_path = paths.run_log_path(routine_id, run_id)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    argv = [
        sys.executable,
        "-m",
        "routines.cli",
        "run",
        str(routine_id),
        "--trigger",
        trigger.lower(),
        "--run-id",
        str(run_id),
    ]
    with log_path.open("ab") as log_handle:
        process = subprocess.Popen(
            argv,
            cwd=paths.repo_root,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    reap_in_background(process)


def reap_in_background(process: subprocess.Popen[bytes]) -> None:
    """Collect the process's exit from a thread of its own, so a spawner that lives on (the Routines app) leaves
    no zombie behind, whose pid the tick would keep reading as a run still going."""
    threading.Thread(target=process.wait, name=f"reap-{process.pid}", daemon=True).start()


class Runner(MutableModel):
    """Runs routines and ticks the schedule, over the stores and services it is given."""

    paths: RoutinesPaths = Field(frozen=True, description="Where everything lives")
    definitions: DefinitionStoreInterface = Field(frozen=True, description="The definition files")
    states: StateStoreInterface = Field(frozen=True, description="The state files")
    ledger: LedgerInterface = Field(frozen=True, description="The run events")
    chat_app: ChatAppInterface = Field(frozen=True, description="The chat app, for skill runs")
    notifier: NotifierInterface = Field(frozen=True, description="The feed")
    clock: ClockInterface = Field(frozen=True, description="The time")

    def run(self, routine_id: RoutineId, trigger: RunTrigger, run_id: RunId | None) -> RunFinishedEvent:
        """One run, start to finish; raises RunInFlightError when one is already going."""
        loaded = self.definitions.load(routine_id)
        if loaded.definition is None:
            raise DefinitionValidationError(f"{routine_id} cannot run: {loaded.error}")
        definition = loaded.definition
        with held_lock(
            self.paths.run_lock_path(routine_id), f"A run of {routine_id}", RUN_LOCK_RETRY_PAUSES, self.clock.pause
        ):
            effective_run_id = run_id if run_id is not None else RunId.generate()
            run_dir = self.paths.run_dir(routine_id, effective_run_id)
            run_dir.mkdir(parents=True, exist_ok=True)
            started_at = self.clock.now()
            self._record_start(routine_id, effective_run_id, definition, trigger, started_at)
            execution: _Execution | None = None
            try:
                execution = self._execute(routine_id, effective_run_id, definition, run_dir)
            finally:
                # An error in the runner itself goes on to the caller (and the run's log, which is this process's
                # stderr); the books are closed first so the run is not later taken for one a restart cut off.
                if execution is None:
                    self._record_finish(
                        routine_id,
                        effective_run_id,
                        definition,
                        trigger,
                        started_at,
                        self.clock.now(),
                        _Execution(
                            outcome=RunOutcome.FAILED,
                            exit_code=None,
                            summary=RUNNER_FAILED_SUMMARY,
                            chat_id=None,
                            warnings=(),
                        ),
                    )
            finished_at = self.clock.now()
            return self._record_finish(
                routine_id,
                effective_run_id,
                definition,
                trigger,
                started_at,
                finished_at,
                execution,
            )

    def _record_start(
        self,
        routine_id: RoutineId,
        run_id: RunId,
        definition: RoutineDefinition,
        trigger: RunTrigger,
        now: datetime,
    ) -> None:
        command = shlex.join(definition.command) if definition.command else slash_command_of(str(definition.skill))
        current = CurrentRun(
            run_id=run_id,
            pid=os.getpid(),
            pid_start=process_start_time(os.getpid()),
            trigger=trigger,
            started_at=now,
        )
        # The state first: it is what the reaper reads, so a runner that dies between the two writes leaves a
        # run the tick can close off rather than a ledger start nothing ever finishes.
        self.states.update(
            routine_id,
            lambda state: state.model_copy_update(
                to_update(state.field_ref().current_run, current),
                to_update(state.field_ref().last_started_at, now),
            ),
        )
        self.ledger.append(
            RunStartedEvent(
                **_envelope(RUN_STARTED_EVENT, now),
                routine_id=routine_id,
                run_id=run_id,
                trigger=trigger,
                kind=definition.kind,
                command=command,
            )
        )

    def _execute(
        self,
        routine_id: RoutineId,
        run_id: RunId,
        definition: RoutineDefinition,
        run_dir: Path,
    ) -> _Execution:
        if definition.skill is not None:
            return self._execute_skill(routine_id, run_id, definition)
        assert definition.command is not None
        return self._execute_command(
            routine_id,
            run_id,
            definition.command,
            definition.timeout.timedelta,
            run_dir,
        )

    def _execute_command(
        self,
        routine_id: RoutineId,
        run_id: RunId,
        command: tuple[str, ...],
        timeout: timedelta,
        run_dir: Path,
    ) -> _Execution:
        log_path = self.paths.run_log_path(routine_id, run_id)
        environment = {
            **os.environ,
            RUN_DIR_ENV: str(run_dir),
            RUN_ID_ENV: str(run_id),
            ROUTINE_ID_ENV: str(routine_id),
        }
        with log_path.open("ab") as log_handle:
            try:
                process = subprocess.Popen(
                    list(command),
                    cwd=self.paths.repo_root,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                    env=environment,
                )
            except OSError as e:
                log_handle.write(f"could not start the command: {e}\n".encode())
                return _Execution(
                    outcome=RunOutcome.FAILED,
                    exit_code=_MISSING_EXECUTABLE_EXIT_CODE,
                    summary=str(e),
                    chat_id=None,
                    warnings=(),
                )
            self.states.update(routine_id, _recording_command_group(run_id, process.pid))
            try:
                exit_code = process.wait(timeout=timeout.total_seconds())
            except subprocess.TimeoutExpired:
                _stop_process_group(process)
                log_handle.write(f"\n{TIMED_OUT_SUMMARY}\n".encode())
                return _Execution(
                    outcome=RunOutcome.TIMED_OUT,
                    exit_code=_TIMED_OUT_EXIT_CODE,
                    summary=TIMED_OUT_SUMMARY,
                    chat_id=None,
                    warnings=(),
                )
        result = read_result(self.paths.result_path(routine_id, run_id))
        return _Execution(
            outcome=_command_outcome(exit_code, result),
            exit_code=exit_code,
            summary=result.summary or "",
            chat_id=result.chat_id,
            warnings=result.warnings,
        )

    def _execute_skill(self, routine_id: RoutineId, run_id: RunId, definition: RoutineDefinition) -> _Execution:
        assert definition.skill is not None
        wake = self.wake(
            routine_id, run_id, routine_id, str(definition.title), str(definition.skill), definition.timeout.timedelta
        )
        self._append_to_log(routine_id, run_id, f"{wake.outcome.lower()}: {wake.summary}\n")
        return _Execution(
            outcome=wake.outcome,
            exit_code=None,
            summary=wake.summary,
            chat_id=wake.chat_id,
            warnings=wake.warnings,
        )

    def wake(
        self,
        routine_id: RoutineId,
        run_id: RunId | None,
        run_routine_id: RoutineId | None,
        title: str,
        skill: str,
        timeout: timedelta,
    ) -> WakeOutcome:
        """Wake the routine's chat with the skill and wait for the turn, keeping the chat on the routine's state.

        Given the run this happens inside (``run_id`` of ``run_routine_id``: a skill run's own, or the run of a
        command that calls ``routines wake``, possibly for another routine's chat), an agent that stops to wait
        on the user is recorded on that run and announced to the feed.
        """

        def on_blocked() -> None:
            if run_id is None or run_routine_id is None:
                return
            self.ledger.append(
                RunBlockedEvent(
                    **_envelope(RUN_BLOCKED_EVENT, self.clock.now()),
                    routine_id=run_routine_id,
                    run_id=run_id,
                )
            )
            self.states.update(run_routine_id, _mark_current_run_blocked)
            if not self.notifier.notify(run_routine_id, title, f"{title} needs you."):
                self._append_to_log(
                    run_routine_id, run_id, "The notification that the agent is waiting did not go out.\n"
                )

        def on_chat_known(chat_id: str) -> None:
            self.states.update(
                routine_id,
                lambda current: current.model_copy_update(to_update(current.field_ref().chat_id, chat_id)),
            )

        return wake_skill(
            routine_id=routine_id,
            title=title,
            skill=skill,
            known_chat_id=self.states.read(routine_id).chat_id,
            timeout=timeout,
            chat_app=self.chat_app,
            clock=self.clock,
            on_blocked=on_blocked,
            on_chat_known=on_chat_known,
        )

    def _append_to_log(self, routine_id: RoutineId, run_id: RunId, text: str) -> None:
        log_path = self.paths.run_log_path(routine_id, run_id)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log_handle:
            log_handle.write(text)

    def _record_finish(
        self,
        routine_id: RoutineId,
        run_id: RunId,
        definition: RoutineDefinition,
        trigger: RunTrigger,
        started_at: datetime,
        finished_at: datetime,
        execution: _Execution,
    ) -> RunFinishedEvent:
        duration = (finished_at - started_at).total_seconds()
        event = RunFinishedEvent(
            **_envelope(RUN_FINISHED_EVENT, finished_at),
            routine_id=routine_id,
            run_id=run_id,
            outcome=execution.outcome,
            exit_code=execution.exit_code,
            duration_seconds=duration,
            summary=execution.summary,
            chat_id=execution.chat_id,
            warnings=execution.warnings,
            output_tail=_read_output_tail(self.paths.run_log_path(routine_id, run_id)),
        )
        self.ledger.append(event)
        last_run = LastRun(
            run_id=run_id,
            trigger=trigger,
            kind=definition.kind,
            outcome=execution.outcome,
            summary=execution.summary,
            started_at=started_at,
            finished_at=finished_at,
            warnings=execution.warnings,
        )
        is_success = execution.outcome is RunOutcome.SUCCEEDED
        state = self.states.update(
            routine_id,
            lambda current: _close_books(current, last_run, is_success, execution.chat_id, finished_at),
        )
        self._notify_after_run(routine_id, run_id, definition, state)
        return event

    def _notify_after_run(
        self, routine_id: RoutineId, run_id: RunId, definition: RoutineDefinition, state: RoutineState
    ) -> None:
        failures = int(state.consecutive_failures)
        threshold = int(definition.notify_after_failures)
        if failures == 0:
            if state.is_failure_notified:
                self._announce(routine_id, run_id, definition, f"{definition.title} is working again.", False)
            return
        if threshold > 0 and failures >= threshold and not state.is_failure_notified:
            times = "once" if failures == 1 else f"{failures} times in a row"
            self._announce(routine_id, run_id, definition, f"{definition.title} has failed {times}.", True)

    def _announce(
        self, routine_id: RoutineId, run_id: RunId, definition: RoutineDefinition, message: str, is_notified: bool
    ) -> None:
        """Tell the feed, and only then record that the streak's state has been told; a post that did not go out
        is noted in the run's log and tried again after the next run."""
        if not self.notifier.notify(routine_id, str(definition.title), message):
            self._append_to_log(routine_id, run_id, f"The notification {message!r} did not go out.\n")
            return
        self.states.update(
            routine_id,
            lambda current: current.model_copy_update(to_update(current.field_ref().is_failure_notified, is_notified)),
        )

    def tick(self) -> TickReport:
        """Section 6.1 of the spec: reap, start what is due, keep house once a day."""
        with tried_lock(self.paths.tick_lock_path) as is_held:
            if not is_held:
                return TickReport(started=(), reaped=(), is_skipped=True)
            routines = self.definitions.list_routines()
            reaped = self._reap_dead_runs(routines)
            started = self._start_due_runs(routines)
            self._housekeep()
            return TickReport(started=tuple(started), reaped=tuple(reaped), is_skipped=False)

    def _state_or_none(self, routine_id: RoutineId) -> RoutineState | None:
        """The routine's state, or None (logged) for a state file that cannot be opened: the tick serves every
        routine, so one broken file keeps the rest from running only if it is let to."""
        try:
            return self.states.read(routine_id)
        except OSError as e:
            logger.warning("Skipping {} this tick: its state cannot be read ({})", routine_id, e)
            return None

    def _reap_dead_runs(self, routines: list[LoadedRoutine]) -> list[RoutineId]:
        reaped: list[RoutineId] = []
        for loaded in routines:
            routine_id = loaded.routine_id
            state = self._state_or_none(routine_id)
            if state is None:
                continue
            current = state.current_run
            if current is None or is_run_process_alive(current):
                continue
            if current.command_pgid:
                # The runner is gone but its command may not be: a run is closed only once nothing of it still
                # runs, or the next run of the routine would go beside it.
                _kill_process_group(current.command_pgid)
            now = self.clock.now()
            kind = loaded.definition.kind if loaded.definition is not None else RunKind.COMMAND
            last_run = LastRun(
                run_id=current.run_id,
                trigger=current.trigger,
                kind=kind,
                outcome=RunOutcome.INTERRUPTED,
                summary=INTERRUPTED_SUMMARY,
                started_at=current.started_at,
                finished_at=now,
            )
            # The state is closed under its lock and only while it still names the run: a run that finished
            # between the read above and here closed its own books, and gets no second finish.
            closed = self.states.update(routine_id, _closing_off(current.run_id, last_run))
            if closed.last_run != last_run:
                continue
            logger.warning(
                "Closing off the run {} of {}: its process is gone",
                current.run_id,
                routine_id,
            )
            self.ledger.append(
                RunFinishedEvent(
                    **_envelope(RUN_FINISHED_EVENT, now),
                    routine_id=routine_id,
                    run_id=current.run_id,
                    outcome=RunOutcome.INTERRUPTED,
                    exit_code=None,
                    duration_seconds=None,
                    summary=INTERRUPTED_SUMMARY,
                    chat_id=state.chat_id,
                    warnings=(),
                    output_tail=_read_output_tail(self.paths.run_log_path(routine_id, current.run_id)),
                )
            )
            # An interruption is a failed attempt: a runner that keeps dying is as broken as a command that
            # keeps failing, and the user is told the same way.
            if loaded.definition is not None:
                self._notify_after_run(routine_id, current.run_id, loaded.definition, closed)
            reaped.append(routine_id)
        return reaped

    def _start_due_runs(self, routines: list[LoadedRoutine]) -> list[RoutineId]:
        # The state says whether a run is in flight; the lock is never probed here, since a probe takes it for
        # an instant and would refuse a run starting at that very moment. A spawn that finds the lock held
        # (a run started between the read and the spawn) exits silently, as a scheduled run does.
        now = self.clock.now()
        zone = self.clock.local_zone()
        started: list[RoutineId] = []
        for loaded in routines:
            if loaded.definition is None:
                continue
            state = self._state_or_none(loaded.routine_id)
            if state is None or not is_switched_on(loaded, state) or state.current_run is not None:
                continue
            due = next_due(loaded.definition, state, now, zone)
            if due is None or due > now:
                continue
            self._spawn_run(loaded.routine_id)
            started.append(loaded.routine_id)
        return started

    def _spawn_run(self, routine_id: RoutineId) -> None:
        logger.info("Starting a scheduled run of {}", routine_id)
        spawn_run_process(self.paths, routine_id, RunTrigger.SCHEDULED, RunId.generate())

    def _housekeep(self) -> None:
        now = self.clock.now()
        today = now.strftime("%Y-%m-%d")
        stamp_path = self.paths.housekeeping_path
        if _read_housekeeping_date(stamp_path) == today:
            return
        self.ledger.rotate_if_large(now)
        self._prune_old_run_dirs(now)
        stamp_path.parent.mkdir(parents=True, exist_ok=True)
        stamp_path.write_text(json.dumps({"date": today}) + "\n")

    def _prune_old_run_dirs(self, now: datetime) -> None:
        # Every routine directory under the state, not only the defined routines': the runs of a routine whose
        # definition is gone are the ones nothing else cleans up.
        cutoff = now.timestamp() - RUN_DIR_RETENTION.total_seconds()
        if not self.paths.state_dir.is_dir():
            return
        for routine_dir in self.paths.state_dir.iterdir():
            if routine_dir.name == LEDGER_DIRNAME:
                continue
            runs_dir = routine_dir / RUNS_DIRNAME
            if not runs_dir.is_dir():
                continue
            for run_dir in runs_dir.iterdir():
                try:
                    if run_dir.is_dir() and run_dir.stat().st_mtime < cutoff:
                        shutil.rmtree(run_dir)
                except OSError as e:
                    logger.warning("Could not prune the run directory {}: {}", run_dir, e)


def _read_housekeeping_date(stamp_path: Path) -> str | None:
    """The day the house was last kept, from the stamp; None, with a warning, for a stamp that cannot be read."""
    if not stamp_path.is_file():
        return None
    try:
        stamp = json.loads(stamp_path.read_text())
    except (OSError, ValueError) as e:
        logger.warning("Keeping house again: the stamp {} could not be read ({})", stamp_path, e)
        return None
    date = stamp.get("date") if isinstance(stamp, dict) else None
    if not isinstance(date, str):
        logger.warning("Keeping house again: the stamp {} names no date", stamp_path)
        return None
    return date


def _recording_command_group(run_id: RunId, pgid: int) -> Callable[[RoutineState], RoutineState]:
    """Note the command's process group on the run in flight, so a reaper can stop it if this runner dies."""

    def record(state: RoutineState) -> RoutineState:
        if state.current_run is None or state.current_run.run_id != run_id:
            return state
        current = state.current_run.model_copy_update(to_update(state.current_run.field_ref().command_pgid, pgid))
        return state.model_copy_update(to_update(state.field_ref().current_run, current))

    return record


def _closing_off(run_id: RunId, last_run: LastRun) -> Callable[[RoutineState], RoutineState]:
    """Close the books on the run as a failed attempt, unless the state no longer names it as the one in flight."""

    def close(state: RoutineState) -> RoutineState:
        if state.current_run is None or state.current_run.run_id != run_id:
            return state
        return _close_books(state, last_run, is_success=False, chat_id=None, finished_at=last_run.finished_at)

    return close


def _close_books(
    state: RoutineState, last_run: LastRun, is_success: bool, chat_id: str | None, finished_at: datetime
) -> RoutineState:
    """The state after a run: no run in flight, the last run recorded, the streak counted, the chat kept."""
    failures = NonNegativeInt(0) if is_success else NonNegativeInt(int(state.consecutive_failures) + 1)
    updates = [
        to_update(state.field_ref().current_run, None),
        to_update(state.field_ref().last_run, last_run),
        to_update(state.field_ref().consecutive_failures, failures),
    ]
    if is_success:
        updates.append(to_update(state.field_ref().last_succeeded_at, finished_at))
        updates.append(to_update(state.field_ref().last_success_started_at, last_run.started_at))
    elif int(state.consecutive_failures) == 0:
        # A new streak: a recovery the feed never took is moot now, and this streak is told at its threshold.
        updates.append(to_update(state.field_ref().is_failure_notified, False))
    else:
        # A streak already going keeps whether it has been told.
        pass
    if chat_id is not None:
        updates.append(to_update(state.field_ref().chat_id, chat_id))
    return state.model_copy_update(*updates)


def _mark_current_run_blocked(state: RoutineState) -> RoutineState:
    if state.current_run is None:
        return state
    blocked = state.current_run.model_copy_update(to_update(state.current_run.field_ref().is_blocked, True))
    return state.model_copy_update(to_update(state.field_ref().current_run, blocked))


def _command_outcome(exit_code: int, result: RunResult) -> RunOutcome:
    """Exit 0 is a success; otherwise the failure kind the result names (a wake's TIMED_OUT), else FAILED."""
    if exit_code == 0:
        return RunOutcome.SUCCEEDED
    if result.outcome is None or result.outcome is RunOutcome.SUCCEEDED:
        return RunOutcome.FAILED
    return result.outcome


def read_result(path: Path) -> RunResult:
    """What the run left in its ``result.json``; an empty result when there is none or it does not parse."""
    if not path.is_file():
        return RunResult()
    try:
        return RunResult.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning("Ignoring the run's result file: {}", e)
        return RunResult()


def _stop_process_group(process: subprocess.Popen[bytes]) -> None:
    """Ask the process group to stop, then kill what is left, each drained for a bounded time; a member that
    outlives the kill is logged, not waited for.

    The direct child is reaped here; its descendants are reaped by whoever inherits them, so the group is
    polled for every member to be gone (a run is closed only once nothing of it still runs).
    """
    if _signal_group_and_drain(process.pid, signal.SIGTERM, process):
        return
    if not _signal_group_and_drain(process.pid, signal.SIGKILL, process):
        logger.warning("The process group {} of the timed-out run is still alive after SIGKILL", process.pid)


def _kill_process_group(pgid: int) -> None:
    """Stop a command's process group whose runner is gone, as the timeout does, with nobody to reap its leader."""
    if not _is_process_group_alive(pgid):
        return
    logger.warning("Stopping the process group {} a dead runner left behind", pgid)
    if _signal_group_and_drain(pgid, signal.SIGTERM, None):
        return
    if not _signal_group_and_drain(pgid, signal.SIGKILL, None):
        logger.warning("The process group {} a dead runner left behind is still alive after SIGKILL", pgid)


def _signal_group_and_drain(pgid: int, signal_number: signal.Signals, process: subprocess.Popen[bytes] | None) -> bool:
    """Send the signal to the group and wait up to the grace for it to empty; whether it did. The leader is
    reaped through its handle when this process is its parent."""
    try:
        os.killpg(pgid, signal_number)
    except ProcessLookupError:
        return True
    except OSError as e:
        logger.warning("Could not send {} to the process group {}: {}", signal_number.name, pgid, e)
        return False
    if process is not None:
        try:
            process.wait(timeout=TERMINATE_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            return False
    sleeper = threading.Event()
    for _ in range(int(TERMINATE_GRACE_SECONDS / _GROUP_DRAIN_POLL_SECONDS)):
        if not _is_process_group_alive(pgid):
            return True
        sleeper.wait(_GROUP_DRAIN_POLL_SECONDS)
    return not _is_process_group_alive(pgid)


def _is_process_group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
