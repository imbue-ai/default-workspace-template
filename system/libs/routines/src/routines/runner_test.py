import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import datetime
from datetime import timedelta
from pathlib import Path

import pytest

from chat_background_tasks.markers import is_pid_alive
from chat_background_tasks.markers import process_start_time
from imbue.imbue_common.model_update import to_update
from routines.data_types import CurrentRun
from routines.data_types import LastRun
from routines.data_types import RUN_STARTED_EVENT
from routines.data_types import RoutineDefinition
from routines.data_types import RoutineState
from routines.data_types import RunFinishedEvent
from routines.data_types import RunResult
from routines.data_types import RunStartedEvent
from routines.definitions import FileDefinitionStore
from routines.errors import DefinitionValidationError
from routines.errors import RunInFlightError
from routines.ledger import FileLedger
from routines.locks import held_lock
from routines.mock_chat_app_test import BrokenChatApp
from routines.mock_chat_app_test import ScriptedChatApp
from routines.mock_clock_test import FakeClock
from routines.mock_ledger_test import StateWatchingLedger
from routines.mock_notifier_test import RecordingNotifier
from routines.mock_state_store_test import OneRefusingStateStore
from routines.mock_state_store_test import StaleReadStateStore
from routines.paths import RoutinesPaths
from routines.primitives import ChatStatusWord
from routines.primitives import RUN_EVENTS_SOURCE
from routines.primitives import RoutineId
from routines.primitives import RunId
from routines.primitives import RunKind
from routines.primitives import RunOutcome
from routines.primitives import RunTrigger
from routines.primitives import RunWarning
from routines.runner import INTERRUPTED_SUMMARY
from routines.runner import RUNNER_FAILED_SUMMARY
from routines.runner import Runner
from routines.runner import read_result
from routines.runner import reap_in_background
from routines.state import FileStateStore

SYNC = RoutineId("nightly-sync")


def _command_routine(definitions: FileDefinitionStore, *command: str, **fields: object) -> None:
    values: dict[str, object] = {"title": "Nightly sync", "what": "Syncs.", "every": "1d", "command": list(command)}
    values.update(fields)
    definitions.add(SYNC, RoutineDefinition.model_validate(values))


def _skill_routine(definitions: FileDefinitionStore, **fields: object) -> None:
    values: dict[str, object] = {"title": "Morning digest", "what": "Digests.", "every": "1d", "skill": "digest"}
    values.update(fields)
    definitions.add(SYNC, RoutineDefinition.model_validate(values))


def _dead_pid() -> int:
    """The pid of a process that has already exited, for a run whose runner is gone."""
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    return dead.pid


def _with_command_group(run_id: RunId, pgid: int) -> Callable[[RoutineState], RoutineState]:
    """Note a command's process group on the run in flight, as the runner does once the command starts."""

    def record(state: RoutineState) -> RoutineState:
        assert state.current_run is not None and state.current_run.run_id == run_id
        current = state.current_run.model_copy_update(to_update(state.current_run.field_ref().command_pgid, pgid))
        return state.model_copy_update(to_update(state.field_ref().current_run, current))

    return record


def _record_current_run(
    states: FileStateStore,
    run_id: RunId,
    pid: int,
    started_at: datetime,
    trigger: RunTrigger = RunTrigger.SCHEDULED,
    pid_start: str = "",
) -> None:
    """Write a run in flight into the routine's state, as the runner does when it starts one."""
    current = CurrentRun(run_id=run_id, pid=pid, pid_start=pid_start, trigger=trigger, started_at=started_at)
    states.update(SYNC, lambda state: state.model_copy_update(to_update(state.field_ref().current_run, current)))


def test_a_command_run_streams_its_output_records_both_events_and_updates_the_state(
    runner: Runner, definitions: FileDefinitionStore, states: FileStateStore, ledger: FileLedger
) -> None:
    _command_routine(definitions, "sh", "-c", 'echo "hello from the run"; echo "stderr too" >&2')

    event = runner.run(SYNC, RunTrigger.MANUAL, None)

    assert event.outcome is RunOutcome.SUCCEEDED
    assert event.exit_code == 0
    assert "hello from the run" in event.output_tail
    assert "stderr too" in event.output_tail
    log = runner.paths.run_log_path(SYNC, event.run_id).read_text()
    assert "hello from the run" in log
    state = states.read(SYNC)
    assert state.current_run is None
    assert state.last_succeeded_at == runner.clock.now()
    assert state.last_success_started_at == state.last_started_at
    assert state.consecutive_failures == 0
    assert state.last_run is not None
    assert state.last_run.run_id == event.run_id
    assert state.last_run.trigger is RunTrigger.MANUAL
    events = ledger.events_of_run(SYNC, event.run_id)
    assert isinstance(events[0], RunStartedEvent)
    assert isinstance(events[1], RunFinishedEvent)
    # Quoted as a shell would need it, so the ledger's line is the command that ran.
    assert events[0].command == 'sh -c \'echo "hello from the run"; echo "stderr too" >&2\''


def test_a_command_sees_its_run_directory_and_its_result_file_becomes_the_summary(
    runner: Runner, definitions: FileDefinitionStore
) -> None:
    script = 'printf \'{"summary": "3 things synced", "links": [{"label": "Note", "url": "/x"}]}\' > "$ROUTINES_RUN_DIR/result.json"'
    _command_routine(definitions, "sh", "-c", script)

    event = runner.run(SYNC, RunTrigger.SCHEDULED, None)

    assert event.summary == "3 things synced"
    assert runner.paths.result_path(SYNC, event.run_id).is_file()


def test_a_result_file_can_name_how_a_failed_run_ended_and_what_it_had_to_say(
    runner: Runner, definitions: FileDefinitionStore
) -> None:
    """What a wake inside the run leaves: a timed-out wake is a timed-out run, and its warning is the run's."""
    result = '{"summary": "Still going", "outcome": "TIMED_OUT", "warnings": ["RESTARTED_PREVIOUS_RUN"]}'
    _command_routine(definitions, "sh", "-c", f"printf '{result}' > \"$ROUTINES_RUN_DIR/result.json\"; exit 1")

    event = runner.run(SYNC, RunTrigger.SCHEDULED, None)

    assert event.outcome is RunOutcome.TIMED_OUT
    assert event.warnings == (RunWarning.RESTARTED_PREVIOUS_RUN,)
    assert event.summary == "Still going"


def test_a_result_file_cannot_turn_a_failed_exit_into_a_success(
    runner: Runner, definitions: FileDefinitionStore
) -> None:
    _command_routine(
        definitions, "sh", "-c", 'printf \'{"outcome": "SUCCEEDED"}\' > "$ROUTINES_RUN_DIR/result.json"; exit 1'
    )

    event = runner.run(SYNC, RunTrigger.SCHEDULED, None)

    assert event.outcome is RunOutcome.FAILED


def test_a_failing_command_counts_toward_the_streak_and_tells_the_feed_at_the_threshold(
    runner: Runner, definitions: FileDefinitionStore, states: FileStateStore, notifier: RecordingNotifier
) -> None:
    _command_routine(definitions, "sh", "-c", "echo boom; exit 3", notify_after_failures=2)

    first = runner.run(SYNC, RunTrigger.SCHEDULED, None)
    assert first.outcome is RunOutcome.FAILED
    assert first.exit_code == 3
    assert notifier.sent == []

    second = runner.run(SYNC, RunTrigger.SCHEDULED, None)
    assert second.outcome is RunOutcome.FAILED
    assert states.read(SYNC).consecutive_failures == 2
    assert notifier.sent == [(SYNC, "Nightly sync", "Nightly sync has failed 2 times in a row.")]

    runner.run(SYNC, RunTrigger.SCHEDULED, None)
    assert len(notifier.sent) == 1, "a streak notifies once"


def test_a_success_after_a_notified_streak_says_it_is_working_again(
    runner: Runner, definitions: FileDefinitionStore, notifier: RecordingNotifier, states: FileStateStore
) -> None:
    marker = runner.paths.repo_root / "fail-once"
    marker.write_text("")
    _command_routine(
        definitions, "sh", "-c", f'if [ -e "{marker}" ]; then rm "{marker}"; exit 1; fi', notify_after_failures=1
    )

    runner.run(SYNC, RunTrigger.SCHEDULED, None)
    runner.run(SYNC, RunTrigger.SCHEDULED, None)

    assert notifier.sent == [
        (SYNC, "Nightly sync", "Nightly sync has failed once."),
        (SYNC, "Nightly sync", "Nightly sync is working again."),
    ]
    assert states.read(SYNC).is_failure_notified is False


def test_a_command_whose_executable_is_missing_is_a_failed_run_with_the_reason(
    runner: Runner, definitions: FileDefinitionStore, states: FileStateStore
) -> None:
    _command_routine(definitions, "/nowhere/no-such-program", "--flag")

    event = runner.run(SYNC, RunTrigger.MANUAL, None)

    assert event.outcome is RunOutcome.FAILED
    assert event.exit_code == 127
    assert "No such file or directory" in event.summary
    assert "could not start the command" in runner.paths.run_log_path(SYNC, event.run_id).read_text()
    assert states.read(SYNC).consecutive_failures == 1


def test_a_command_past_its_timeout_is_killed_with_its_whole_process_group(
    runner: Runner, definitions: FileDefinitionStore, states: FileStateStore
) -> None:
    pid_file = runner.paths.repo_root / "child.pid"
    _command_routine(definitions, "sh", "-c", f'sleep 73841 & echo $! > "{pid_file}"; wait', timeout="1s")

    event = runner.run(SYNC, RunTrigger.MANUAL, None)

    assert event.outcome is RunOutcome.TIMED_OUT
    child_pid = int(pid_file.read_text())
    assert not is_pid_alive(child_pid), "the sleep the command left in its process group outlived the run"
    assert states.read(SYNC).consecutive_failures == 1


def test_a_spawned_process_is_collected_once_it_exits_without_the_spawner_waiting() -> None:
    process = subprocess.Popen(["true"], stdin=subprocess.DEVNULL, start_new_session=True)
    reap_in_background(process)
    deadline = time.monotonic() + 10
    while process.returncode is None and time.monotonic() < deadline:
        threading.Event().wait(0.05)
    assert process.returncode == 0


def test_an_error_in_the_runner_closes_the_books_as_failed_and_still_surfaces(
    runner: Runner, definitions: FileDefinitionStore, states: FileStateStore, ledger: FileLedger
) -> None:
    _skill_routine(definitions)
    runner = runner.model_copy_update(to_update(runner.field_ref().chat_app, BrokenChatApp()))

    with pytest.raises(OSError, match="vanished"):
        runner.run(SYNC, RunTrigger.MANUAL, None)

    state = states.read(SYNC)
    assert state.current_run is None
    assert state.last_run is not None
    assert state.last_run.outcome is RunOutcome.FAILED
    assert state.last_run.summary == RUNNER_FAILED_SUMMARY
    assert state.consecutive_failures == 1
    finished = ledger.recent_events(SYNC, run_limit=1)[0]
    assert isinstance(finished, RunFinishedEvent)
    assert finished.outcome is RunOutcome.FAILED


def test_a_second_run_is_refused_while_one_holds_the_lock(runner: Runner, definitions: FileDefinitionStore) -> None:
    _command_routine(definitions, "true")
    with held_lock(runner.paths.run_lock_path(SYNC), "A run"):
        with pytest.raises(RunInFlightError):
            runner.run(SYNC, RunTrigger.MANUAL, None)


def test_a_broken_definition_cannot_run(runner: Runner, definitions: FileDefinitionStore) -> None:
    (definitions.paths.definitions_dir / f"{SYNC}.toml").write_text('title = "x"\n')
    with pytest.raises(DefinitionValidationError):
        runner.run(SYNC, RunTrigger.MANUAL, None)


def test_a_skill_run_wakes_the_chat_and_records_it(
    runner: Runner, definitions: FileDefinitionStore, states: FileStateStore, chat_app: ScriptedChatApp
) -> None:
    _skill_routine(definitions)
    chat_app.script(
        "agent-created", (ChatStatusWord.WORKING, ChatStatusWord.IDLE), "Done: 3 messages summarized.\nMore."
    )

    event = runner.run(SYNC, RunTrigger.SCHEDULED, None)

    assert event.outcome is RunOutcome.SUCCEEDED
    assert event.summary == "Done: 3 messages summarized."
    assert event.chat_id == "agent-created"
    assert chat_app.created == [("Morning digest", SYNC, "/digest")]
    assert states.read(SYNC).chat_id == "agent-created"
    assert states.read(SYNC).last_run is not None


def test_a_skill_run_whose_agent_waits_on_the_user_is_recorded_blocked_and_notified(
    runner: Runner,
    definitions: FileDefinitionStore,
    chat_app: ScriptedChatApp,
    notifier: RecordingNotifier,
    ledger: FileLedger,
) -> None:
    _skill_routine(definitions)
    chat_app.script(
        "agent-created", (ChatStatusWord.WORKING, ChatStatusWord.ATTENTION, ChatStatusWord.IDLE), "Answered."
    )

    event = runner.run(SYNC, RunTrigger.SCHEDULED, None)

    assert event.outcome is RunOutcome.SUCCEEDED
    assert notifier.sent == [(SYNC, "Morning digest", "Morning digest needs you.")]
    assert [type(e).__name__ for e in ledger.events_of_run(SYNC, event.run_id)] == [
        "RunStartedEvent",
        "RunBlockedEvent",
        "RunFinishedEvent",
    ]


def test_a_busy_chat_is_restarted_and_the_run_carries_the_warning(
    runner: Runner, definitions: FileDefinitionStore, states: FileStateStore, chat_app: ScriptedChatApp
) -> None:
    _skill_routine(definitions)
    states.update(SYNC, lambda state: state.model_copy_update(to_update(state.field_ref().chat_id, "agent-old")))
    chat_app.script(
        "agent-old", (ChatStatusWord.WORKING, ChatStatusWord.WORKING, ChatStatusWord.IDLE, ChatStatusWord.IDLE), "ok"
    )

    event = runner.run(SYNC, RunTrigger.SCHEDULED, None)

    assert event.outcome is RunOutcome.SUCCEEDED
    assert event.warnings == (RunWarning.RESTARTED_PREVIOUS_RUN,)
    last_run = states.read(SYNC).last_run
    assert last_run is not None and last_run.warnings == (RunWarning.RESTARTED_PREVIOUS_RUN,)
    assert chat_app.verbs == [("agent-old", "stop"), ("agent-old", "start")]
    assert chat_app.sent == [("agent-old", "/clear"), ("agent-old", "/digest")]


def test_the_tick_closes_off_a_run_whose_process_is_gone(
    runner: Runner, definitions: FileDefinitionStore, states: FileStateStore, ledger: FileLedger, clock: FakeClock
) -> None:
    _command_routine(definitions, "true", enabled=False)
    run_id = RunId.generate()
    _record_current_run(states, run_id, _dead_pid(), clock.now())

    report = runner.tick()

    assert report.reaped == (SYNC,)
    assert report.started == ()
    state = states.read(SYNC)
    assert state.current_run is None
    assert state.last_run is not None
    assert state.last_run.outcome is RunOutcome.INTERRUPTED
    assert state.last_run.summary == INTERRUPTED_SUMMARY
    finished = ledger.events_of_run(SYNC, run_id)[-1]
    assert isinstance(finished, RunFinishedEvent)
    assert finished.outcome is RunOutcome.INTERRUPTED


def test_the_tick_goes_on_past_a_routine_whose_state_cannot_be_read(
    runner: Runner, paths: RoutinesPaths, definitions: FileDefinitionStore, clock: FakeClock
) -> None:
    """One broken state file must not keep every other routine from being reaped or started, tick after tick."""
    states = OneRefusingStateStore(paths=paths, refused_id=RoutineId("broken"))
    definitions.add(
        RoutineId("broken"),
        RoutineDefinition.model_validate({"title": "B", "what": "W", "every": "1h", "command": ["true"]}),
    )
    _command_routine(definitions, "true", enabled=False)
    _record_current_run(states, RunId.generate(), _dead_pid(), clock.now())
    runner = runner.model_copy_update(to_update(runner.field_ref().states, states))

    report = runner.tick()

    assert report.reaped == (SYNC,)
    assert report.started == ()


def test_the_tick_skips_a_routine_that_is_off_or_not_due(
    runner: Runner, definitions: FileDefinitionStore, states: FileStateStore, clock: FakeClock
) -> None:
    _command_routine(definitions, "true", enabled=False)
    assert runner.tick().started == ()
    states.update(SYNC, lambda state: state.model_copy_update(to_update(state.field_ref().is_enabled, True)))
    states.update(
        SYNC, lambda state: state.model_copy_update(to_update(state.field_ref().last_succeeded_at, clock.now()))
    )
    assert runner.tick().started == ()


def test_the_tick_keeps_house_once_a_day(runner: Runner, definitions: FileDefinitionStore, clock: FakeClock) -> None:
    _command_routine(definitions, "true", enabled=False)
    runner.tick()
    stamp = json.loads(runner.paths.housekeeping_path.read_text())
    assert stamp == {"date": "2026-10-20"}
    old_run = runner.paths.run_dir(SYNC, RunId.generate())
    # A routine whose definition is gone keeps its state directory; its old runs go too.
    old_run_of_removed = runner.paths.run_dir(RoutineId("retired"), RunId.generate())
    # The ledger's directory sits beside the routines' own and is no routine's: nothing under it is pruned.
    old_ledger_dir = runner.paths.events_path.parent / "archive"
    ninety_one_days_ago = (clock.now() - timedelta(days=91)).timestamp()
    for run_dir in (old_run, old_run_of_removed, old_ledger_dir):
        run_dir.mkdir(parents=True)
        os.utime(run_dir, (ninety_one_days_ago, ninety_one_days_ago))

    runner.tick()
    assert old_run.exists(), "housekeeping ran today already"

    clock.advance(timedelta(days=1))
    runner.tick()
    assert not old_run.exists()
    assert not old_run_of_removed.exists()
    assert old_ledger_dir.exists()


@pytest.mark.parametrize("stamp", ["not json", "[1, 2]", '{"day": "2026-10-20"}'])
def test_a_housekeeping_stamp_that_cannot_be_read_means_the_house_is_kept_again(
    runner: Runner, definitions: FileDefinitionStore, stamp: str
) -> None:
    _command_routine(definitions, "true", enabled=False)
    runner.paths.housekeeping_path.parent.mkdir(parents=True, exist_ok=True)
    runner.paths.housekeeping_path.write_text(stamp)

    runner.tick()

    assert json.loads(runner.paths.housekeeping_path.read_text()) == {"date": "2026-10-20"}


def test_a_result_file_that_does_not_validate_or_parse_reads_as_an_empty_result(tmp_path: Path) -> None:
    """A run's result.json is the command's to write; a bad one costs the summary, not the run's books."""
    path = tmp_path / "result.json"
    path.write_text('{"summary": 5}')
    assert read_result(path) == RunResult()
    path.write_text("not json")
    assert read_result(path) == RunResult()
    assert read_result(tmp_path / "missing.json") == RunResult()


def test_a_wake_inside_a_command_run_records_the_waiting_agent_on_that_run(
    runner: Runner,
    definitions: FileDefinitionStore,
    chat_app: ScriptedChatApp,
    notifier: RecordingNotifier,
    ledger: FileLedger,
    states: FileStateStore,
) -> None:
    """What ``routines wake`` does from inside a command routine's run: the same books as a skill run's."""
    _command_routine(definitions, "true")
    chat_app.script("agent-created", (ChatStatusWord.ATTENTION, ChatStatusWord.IDLE), "Answered.")
    run_id = RunId.generate()
    ledger.append(
        RunStartedEvent(
            timestamp="2026-10-20T15:00:00.000000000+00:00",
            type=RUN_STARTED_EVENT,
            event_id="e1",
            source=RUN_EVENTS_SOURCE,
            routine_id=SYNC,
            run_id=run_id,
            trigger=RunTrigger.MANUAL,
            kind=RunKind.COMMAND,
            command="true",
        )
    )
    _record_current_run(states, run_id, os.getpid(), runner.clock.now(), trigger=RunTrigger.MANUAL)

    outcome = runner.wake(SYNC, run_id, SYNC, "Weekly check-up", "caretaker", timedelta(hours=1))

    assert outcome.outcome is RunOutcome.SUCCEEDED
    assert notifier.sent == [(SYNC, "Weekly check-up", "Weekly check-up needs you.")]
    assert [type(e).__name__ for e in ledger.events_of_run(SYNC, run_id)] == ["RunStartedEvent", "RunBlockedEvent"]
    state = states.read(SYNC)
    assert state.chat_id == "agent-created"
    assert state.current_run is not None and state.current_run.is_blocked


def test_a_wake_of_another_routines_chat_inside_a_run_records_the_block_on_the_run(
    runner: Runner,
    definitions: FileDefinitionStore,
    chat_app: ScriptedChatApp,
    notifier: RecordingNotifier,
    ledger: FileLedger,
    states: FileStateStore,
) -> None:
    """``routines wake caretaker --routine weekly-check`` from a command run of nightly-sync: the block is the
    run's, the chat is weekly-check's."""
    _command_routine(definitions, "true")
    weekly_check = RoutineId("weekly-check")
    chat_app.script("agent-created", (ChatStatusWord.ATTENTION, ChatStatusWord.IDLE), "Answered.")
    run_id = RunId.generate()
    _record_current_run(states, run_id, os.getpid(), runner.clock.now(), trigger=RunTrigger.MANUAL)

    outcome = runner.wake(weekly_check, run_id, SYNC, "Weekly check", "caretaker", timedelta(hours=1))

    assert outcome.outcome is RunOutcome.SUCCEEDED
    assert notifier.sent == [(SYNC, "Weekly check", "Weekly check needs you.")]
    assert [type(e).__name__ for e in ledger.events_of_run(SYNC, run_id)] == ["RunBlockedEvent"]
    assert ledger.recent_events(weekly_check, 5) == []
    state = states.read(SYNC)
    assert state.current_run is not None and state.current_run.is_blocked
    assert state.chat_id is None
    assert states.read(weekly_check).chat_id == "agent-created"


def test_a_wake_outside_any_run_only_keeps_the_chat(
    runner: Runner, chat_app: ScriptedChatApp, notifier: RecordingNotifier, ledger: FileLedger, states: FileStateStore
) -> None:
    chat_app.script("agent-created", (ChatStatusWord.ATTENTION, ChatStatusWord.IDLE), "Answered.")

    outcome = runner.wake(SYNC, None, None, "Weekly check-up", "caretaker", timedelta(hours=1))

    assert outcome.outcome is RunOutcome.SUCCEEDED
    assert notifier.sent == []
    assert ledger.recent_events(SYNC, 5) == []
    assert states.read(SYNC).chat_id == "agent-created"


def test_a_notification_the_feed_did_not_take_is_tried_again_after_the_next_run(
    runner: Runner, definitions: FileDefinitionStore, notifier: RecordingNotifier, states: FileStateStore
) -> None:
    _command_routine(definitions, "false", notify_after_failures=1)
    notifier.is_accepting = False

    first = runner.run(SYNC, RunTrigger.SCHEDULED, None)
    assert states.read(SYNC).is_failure_notified is False
    assert "did not go out" in runner.paths.run_log_path(SYNC, first.run_id).read_text()

    notifier.is_accepting = True
    runner.run(SYNC, RunTrigger.SCHEDULED, None)
    assert states.read(SYNC).is_failure_notified is True
    assert [message for _, _, message in notifier.sent] == [
        "Nightly sync has failed once.",
        "Nightly sync has failed 2 times in a row.",
    ]


def test_a_new_streak_after_an_unsent_recovery_post_is_still_announced(
    runner: Runner, definitions: FileDefinitionStore, notifier: RecordingNotifier, states: FileStateStore
) -> None:
    marker = runner.paths.repo_root / "succeed-once"
    _command_routine(
        definitions,
        "sh",
        "-c",
        f'if [ -e "{marker}" ]; then rm "{marker}"; exit 0; fi; exit 1',
        notify_after_failures=1,
    )
    runner.run(SYNC, RunTrigger.SCHEDULED, None)
    assert states.read(SYNC).is_failure_notified is True

    # The recovery post does not go out, so the flag stays up for a retry...
    notifier.is_accepting = False
    marker.write_text("")
    runner.run(SYNC, RunTrigger.SCHEDULED, None)
    assert states.read(SYNC).is_failure_notified is True

    # ...but a new streak starts over: the feed hears about it at the threshold.
    notifier.is_accepting = True
    runner.run(SYNC, RunTrigger.SCHEDULED, None)
    assert [message for _, _, message in notifier.sent] == [
        "Nightly sync has failed once.",
        "Nightly sync is working again.",
        "Nightly sync has failed once.",
    ]
    assert states.read(SYNC).is_failure_notified is True


def test_the_tick_leaves_a_run_that_finished_while_it_was_looking_alone(
    runner: Runner, paths: RoutinesPaths, definitions: FileDefinitionStore, ledger: FileLedger, clock: FakeClock
) -> None:
    _command_routine(definitions, "true", enabled=False)
    run_id = RunId.generate()
    finished = LastRun(
        run_id=run_id,
        trigger=RunTrigger.SCHEDULED,
        kind=RunKind.COMMAND,
        outcome=RunOutcome.SUCCEEDED,
        summary="all done",
        started_at=clock.now(),
        finished_at=clock.now(),
    )
    states = StaleReadStateStore(paths=paths)
    states.update(SYNC, lambda state: state.model_copy_update(to_update(state.field_ref().last_run, finished)))
    states.stale = RoutineState(
        current_run=CurrentRun(run_id=run_id, pid=_dead_pid(), trigger=RunTrigger.SCHEDULED, started_at=clock.now())
    )
    runner = runner.model_copy_update(to_update(runner.field_ref().states, states))

    report = runner.tick()

    assert report.reaped == ()
    assert states.read(SYNC).last_run == finished
    assert ledger.events_of_run(SYNC, run_id) == []


def test_the_tick_closes_off_a_run_whose_pid_now_belongs_to_another_process(
    runner: Runner, definitions: FileDefinitionStore, states: FileStateStore, clock: FakeClock
) -> None:
    """This process is alive, but the run recorded a start time that is not its own: a recycled pid."""
    _command_routine(definitions, "true", enabled=False)
    run_id = RunId.generate()
    _record_current_run(states, run_id, os.getpid(), clock.now(), pid_start="1")

    assert runner.tick().reaped == (SYNC,)
    assert states.read(SYNC).current_run is None


def test_a_run_in_flight_in_this_very_process_is_left_alone(
    runner: Runner, definitions: FileDefinitionStore, states: FileStateStore, clock: FakeClock
) -> None:
    _command_routine(definitions, "true", enabled=False)
    _record_current_run(states, RunId.generate(), os.getpid(), clock.now(), pid_start=process_start_time(os.getpid()))

    assert runner.tick().reaped == ()
    assert states.read(SYNC).current_run is not None


def test_the_state_names_a_run_before_the_ledger_does(
    paths: RoutinesPaths,
    definitions: FileDefinitionStore,
    states: FileStateStore,
    chat_app: ScriptedChatApp,
    notifier: RecordingNotifier,
    clock: FakeClock,
) -> None:
    """A runner that dies between the two writes then leaves a run the tick can close off, not a start nothing finishes."""
    _command_routine(definitions, "true")
    ledger = StateWatchingLedger(paths=paths, states=states)
    runner = Runner(
        paths=paths,
        definitions=definitions,
        states=states,
        ledger=ledger,
        chat_app=chat_app,
        notifier=notifier,
        clock=clock,
    )

    event = runner.run(SYNC, RunTrigger.MANUAL, None)

    assert ledger.run_in_flight_at_start == [event.run_id]


def test_a_reaped_run_counts_toward_the_streak_and_tells_the_feed(
    runner: Runner,
    definitions: FileDefinitionStore,
    states: FileStateStore,
    notifier: RecordingNotifier,
    clock: FakeClock,
) -> None:
    """A runner that keeps dying is as broken as a command that keeps failing."""
    _command_routine(definitions, "true", enabled=False, notify_after_failures=2)
    _record_current_run(states, RunId.generate(), _dead_pid(), clock.now())
    runner.tick()
    assert states.read(SYNC).consecutive_failures == 1
    assert notifier.sent == []

    _record_current_run(states, RunId.generate(), _dead_pid(), clock.now())
    runner.tick()

    state = states.read(SYNC)
    assert state.consecutive_failures == 2
    assert state.is_failure_notified
    assert notifier.sent == [(SYNC, "Nightly sync", "Nightly sync has failed 2 times in a row.")]


def test_the_reaper_stops_the_command_a_dead_runner_left_running(
    runner: Runner, definitions: FileDefinitionStore, states: FileStateStore, clock: FakeClock
) -> None:
    _command_routine(definitions, "true", enabled=False)
    orphan = subprocess.Popen(["sleep", "73842"], start_new_session=True)
    # In the workspace the orphan's parent is the dead runner, so init reaps it; here this process is its parent.
    reap_in_background(orphan)
    try:
        run_id = RunId.generate()
        _record_current_run(states, run_id, _dead_pid(), clock.now())
        states.update(SYNC, _with_command_group(run_id, orphan.pid))

        report = runner.tick()

        assert report.reaped == (SYNC,)
        assert orphan.wait(timeout=10) != 0, "the orphaned command was not stopped"
    finally:
        if orphan.poll() is None:
            orphan.kill()
            orphan.wait()


def test_a_command_run_records_its_process_group_while_it_runs(
    runner: Runner, definitions: FileDefinitionStore, states: FileStateStore
) -> None:
    seen = runner.paths.repo_root / "seen.json"
    state_path = runner.paths.state_path(SYNC)
    # The runner records the group after the command has started, so the command waits (bounded) for it.
    _command_routine(
        definitions,
        "sh",
        "-c",
        f'for i in $(seq 1 250); do grep -q \'"command_pgid": *[1-9]\' "{state_path}" && break; sleep 0.02; done; '
        f'cat "{state_path}" > "{seen}"',
    )

    runner.run(SYNC, RunTrigger.MANUAL, None)

    recorded = json.loads(seen.read_text())["current_run"]
    assert recorded["command_pgid"] > 0
    assert states.read(SYNC).current_run is None
