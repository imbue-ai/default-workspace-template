import subprocess
from datetime import datetime
from datetime import timedelta

import pytest

from chat_background_tasks.markers import process_start_time
from imbue.imbue_common.model_update import to_update
from routines.clock import SystemClock
from routines.data_types import CurrentRun
from routines.data_types import RoutineDefinition
from routines.data_types import ScheduleChange
from routines.data_types import ScheduleOverride
from routines.errors import AppOwnedRoutineError
from routines.errors import BrokenDefinitionError
from routines.errors import DefinitionCommitError
from routines.errors import DuplicateRoutineError
from routines.errors import RunInFlightError
from routines.locks import held_lock
from routines.mock_chat_app_test import ScriptedChatApp
from routines.mock_clock_test import FakeClock
from routines.mock_notifier_test import RecordingNotifier
from routines.mock_repository_test import RecordingRepository
from routines.mock_state_store_test import StateStoreWithARunArriving
from routines.paths import RoutinesPaths
from routines.primitives import Duration
from routines.primitives import RoutineId
from routines.primitives import RunId
from routines.primitives import RunTrigger
from routines.primitives import TimeOfDay
from routines.primitives import Weekday
from routines.schedule import apply_schedule
from routines.service import RoutinesService
from routines.state import FileStateStore
from routines.testing import TEST_NOW
from routines.testing import TEST_ZONE
from routines.testing import build_test_service
from routines.testing import wait_for_last_run
from routines.testing import write_app_definition

SYNC = RoutineId("nightly-sync")
_DAILY_TOML = (
    '# Shipped by the app.\ntitle = "Nightly sync"\nwhat = "Syncs."\nevery = "1d"\nat = "07:30"\ncommand = ["true"]\n'
)
_EVERY_TWO_HOURS = ScheduleChange(every=Duration("2h"))


def test_a_removal_whose_commit_is_refused_still_stops_the_chat_and_drops_the_state(
    paths: RoutinesPaths,
    clock: FakeClock,
    chat_app: ScriptedChatApp,
    notifier: RecordingNotifier,
) -> None:
    repository = RecordingRepository(repo_root=paths.repo_root)
    service = build_test_service(paths, repository, FileStateStore(paths=paths), clock, chat_app, notifier)
    service.add(
        SYNC, RoutineDefinition.model_validate({"title": "Digest", "what": "Digests.", "every": "1d", "skill": "x"})
    )
    service.states.update(SYNC, lambda state: state.model_copy_update(to_update(state.field_ref().chat_id, "agent-1")))
    repository.is_refusing = True

    with pytest.raises(DefinitionCommitError):
        service.remove(SYNC)

    assert chat_app.verbs == [("agent-1", "stop")]
    assert not paths.routine_state_dir(SYNC).exists()
    assert not paths.workspace_definition_path(SYNC).exists()


def test_a_run_record_a_dead_runner_left_is_not_a_run_in_flight(
    paths: RoutinesPaths,
    clock: FakeClock,
    service: RoutinesService,
) -> None:
    """Until the tick reaps it, the state still names the run; neither Run now nor a removal waits on it."""
    service.add(
        SYNC,
        RoutineDefinition.model_validate(
            {"title": "Nightly sync", "what": "Syncs.", "every": "1d", "command": ["true"]}
        ),
    )
    gone = subprocess.Popen(["sleep", "30"])
    stale = CurrentRun(
        run_id=RunId.generate(),
        pid=gone.pid,
        pid_start=process_start_time(gone.pid),
        trigger=RunTrigger.SCHEDULED,
        started_at=clock.now(),
    )
    gone.kill()
    gone.wait()
    service.states.update(SYNC, lambda state: state.model_copy_update(to_update(state.field_ref().current_run, stale)))

    assert not service.is_running(SYNC)
    service.remove(SYNC)

    assert not paths.workspace_definition_path(SYNC).exists()


def test_a_routine_whose_run_lock_is_held_cannot_be_removed(
    paths: RoutinesPaths,
    service: RoutinesService,
) -> None:
    service.add(
        SYNC,
        RoutineDefinition.model_validate(
            {"title": "Nightly sync", "what": "Syncs.", "every": "1d", "command": ["true"]}
        ),
    )

    with held_lock(paths.run_lock_path(SYNC), "A run"), pytest.raises(RunInFlightError):
        service.remove(SYNC)

    assert service.definitions.load(SYNC).definition is not None
    service.remove(SYNC)
    assert not paths.workspace_definition_path(SYNC).exists()
    assert not paths.routine_state_dir(SYNC).exists()


def test_a_manual_start_answers_once_the_run_has_taken_hold(
    paths: RoutinesPaths,
    repository: RecordingRepository,
    chat_app: ScriptedChatApp,
    notifier: RecordingNotifier,
) -> None:
    """The receipt means a run is going: a page that reads the routine right after it sees the run, not a gap
    before the spawned interpreter has taken the lock."""
    service = build_test_service(paths, repository, FileStateStore(paths=paths), SystemClock(), chat_app, notifier)
    service.add(
        SYNC,
        RoutineDefinition.model_validate(
            {"title": "Nightly sync", "what": "Syncs.", "every": "1d", "command": ["sleep", "2"]}
        ),
    )

    receipt = service.start_run_detached(SYNC)

    assert service.is_running(SYNC)
    with pytest.raises(RunInFlightError):
        service.start_run_detached(SYNC)
    last_run = wait_for_last_run(service.states, SYNC, 15)
    assert last_run.run_id == receipt.run_id
    assert last_run.trigger is RunTrigger.MANUAL


def test_a_manual_start_that_loses_the_routine_to_another_run_is_refused(
    paths: RoutinesPaths,
    repository: RecordingRepository,
    chat_app: ScriptedChatApp,
    notifier: RecordingNotifier,
) -> None:
    """Between the check and the spawn a scheduled run can take the lock; the spawned run then stops at it, and the
    caller is told rather than handed a receipt for a run that never happens."""
    states = StateStoreWithARunArriving(paths=paths)
    service = build_test_service(
        paths, repository, states, FakeClock(current=TEST_NOW, zone=TEST_ZONE), chat_app, notifier
    )
    service.add(
        SYNC,
        RoutineDefinition.model_validate(
            {"title": "Nightly sync", "what": "Syncs.", "every": "1d", "command": ["true"]}
        ),
    )

    with pytest.raises(RunInFlightError):
        service.start_run_detached(SYNC)


def test_rescheduling_a_routine_an_app_owns_keeps_the_schedule_in_its_state_and_leaves_the_file_alone(
    paths: RoutinesPaths,
    repository: RecordingRepository,
    clock: FakeClock,
    chat_app: ScriptedChatApp,
    notifier: RecordingNotifier,
) -> None:
    path = write_app_definition(paths, "news_sync", SYNC, _DAILY_TOML)
    states = FileStateStore(paths=paths)
    service = build_test_service(paths, repository, states, clock, chat_app, notifier)
    states.update(
        SYNC,
        lambda state: state.model_copy_update(
            to_update(state.field_ref().last_succeeded_at, clock.now() - timedelta(minutes=30))
        ),
    )
    before = service.row(SYNC)

    rescheduled = service.change_schedule(SYNC, _EVERY_TWO_HOURS)

    assert path.read_bytes() == _DAILY_TOML.encode()
    assert repository.commits == []
    assert (rescheduled.every, rescheduled.at) == (Duration("2h"), None)
    assert states.read(SYNC).schedule_override == ScheduleOverride(every=Duration("2h"))
    after = service.row(SYNC)
    assert (before.schedule_text, before.next_due_at) == (
        "Every day at 7:30 am",
        datetime(2026, 10, 21, 7, 30, tzinfo=TEST_ZONE),
    )
    assert (after.schedule_text, after.next_due_at) == ("Every 2 hours", clock.now() + timedelta(minutes=90))
    # What the app's page and `routines show` read: the routine as it runs, and where its file is.
    detail = service.detail(SYNC)
    assert detail.definition == rescheduled
    assert detail.definition_path == str(path)

    # A service built afresh over the same files, as the next process is, runs on the kept schedule.
    reloaded = build_test_service(paths, repository, FileStateStore(paths=paths), clock, chat_app, notifier)
    assert reloaded.row(SYNC).next_due_at == after.next_due_at
    assert reloaded.effective(SYNC).definition == rescheduled
    assert reloaded.definitions.load(SYNC).definition == RoutineDefinition.model_validate(
        {"title": "Nightly sync", "what": "Syncs.", "every": "1d", "at": "07:30", "command": ["true"]}
    )


def test_rescheduling_a_routine_an_app_owns_back_to_its_files_schedule_drops_the_users(
    paths: RoutinesPaths,
    repository: RecordingRepository,
    service: RoutinesService,
) -> None:
    path = write_app_definition(paths, "news_sync", SYNC, _DAILY_TOML)
    service.change_schedule(SYNC, _EVERY_TWO_HOURS)

    service.change_schedule(SYNC, ScheduleChange(every=Duration("1d"), at=TimeOfDay("07:30")))

    assert service.states.read(SYNC).schedule_override is None
    assert service.row(SYNC).schedule_text == "Every day at 7:30 am"
    assert path.read_bytes() == _DAILY_TOML.encode()
    assert repository.commits == []


def test_a_schedule_put_back_with_its_weekdays_in_another_order_is_the_files_own_again(
    paths: RoutinesPaths,
    service: RoutinesService,
) -> None:
    """The app's editor appends a day that is ticked again, and `routines edit --days` takes them as typed."""
    write_app_definition(paths, "news_sync", SYNC, _DAILY_TOML + 'days = ["mon", "wed", "fri"]\n')
    file_schedule = service.effective(SYNC).definition
    assert file_schedule is not None
    reordered = ScheduleChange(every=Duration("1d"), at=TimeOfDay("07:30"), days=(Weekday.MON, Weekday.FRI, Weekday.WED))

    service.change_schedule(SYNC, ScheduleChange(every=Duration("1d"), at=TimeOfDay("07:30"), days=(Weekday.MON, Weekday.FRI)))
    service.change_schedule(SYNC, reordered)
    after_the_editor = service.states.read(SYNC).schedule_override
    service.change_schedule(SYNC, _EVERY_TWO_HOURS)
    service.replace(SYNC, apply_schedule(file_schedule, reordered), "edit")

    assert after_the_editor is None
    assert service.states.read(SYNC).schedule_override is None
    assert service.row(SYNC).schedule_text == "Every Monday, Wednesday and Friday at 7:30 am"


def test_rescheduling_a_routine_the_workspace_owns_rewrites_its_file_and_commits_it(
    repository: RecordingRepository,
    service: RoutinesService,
) -> None:
    path = service.add(
        SYNC,
        RoutineDefinition.model_validate(
            {"title": "Nightly sync", "what": "Syncs.", "every": "1d", "at": "07:30", "command": ["true"]}
        ),
    )

    service.change_schedule(SYNC, _EVERY_TWO_HOURS)

    assert 'every = "2h"' in path.read_text()
    assert "07:30" not in path.read_text()
    assert repository.commits[-1] == (path, "routines: change the schedule of nightly-sync")
    assert service.states.read(SYNC).schedule_override is None
    assert service.row(SYNC).schedule_text == "Every 2 hours"
    assert service.detail(SYNC).definition_path == str(path)


def test_a_definition_of_an_apps_routine_is_taken_for_its_schedule_alone(
    paths: RoutinesPaths,
    repository: RecordingRepository,
    service: RoutinesService,
) -> None:
    """What `routines edit` hands over: the routine as it runs with the named fields changed."""
    path = write_app_definition(paths, "news_sync", SYNC, _DAILY_TOML)
    as_it_runs = service.effective(SYNC).definition
    assert as_it_runs is not None
    retitled = as_it_runs.model_copy_update(to_update(as_it_runs.field_ref().title, "Morning sync"))
    moved = as_it_runs.model_copy_update(to_update(as_it_runs.field_ref().at, TimeOfDay("09:00")))

    with pytest.raises(AppOwnedRoutineError, match="belongs to the app in system/apps/news_sync/"):
        service.replace(SYNC, retitled, "edit")
    assert service.states.read(SYNC).schedule_override is None

    written_to = service.replace(SYNC, moved, "edit")

    assert written_to == paths.state_path(SYNC)
    assert service.row(SYNC).schedule_text == "Every day at 9:00 am"
    assert path.read_bytes() == _DAILY_TOML.encode()
    assert repository.commits == []


def test_a_change_to_an_apps_routine_whose_file_does_not_load_says_the_file_is_broken(
    paths: RoutinesPaths,
    service: RoutinesService,
) -> None:
    write_app_definition(paths, "news_sync", SYNC, "title = [not toml")

    with pytest.raises(BrokenDefinitionError, match=r"nightly-sync is broken \(not valid TOML.*fix its file first"):
        service.change_schedule(SYNC, _EVERY_TWO_HOURS)
    with pytest.raises(BrokenDefinitionError, match=r"nightly-sync is broken \(not valid TOML.*fix its file first"):
        service.replace(
            SYNC,
            RoutineDefinition.model_validate({"title": "T", "what": "W", "every": "2h", "command": ["true"]}),
            "edit",
        )

    assert service.states.read(SYNC).schedule_override is None


def test_a_routine_an_app_owns_cannot_be_removed_and_keeps_its_chat_and_its_state(
    paths: RoutinesPaths,
    repository: RecordingRepository,
    chat_app: ScriptedChatApp,
    service: RoutinesService,
) -> None:
    path = write_app_definition(paths, "news_sync", SYNC, _DAILY_TOML)
    service.states.update(SYNC, lambda state: state.model_copy_update(to_update(state.field_ref().chat_id, "agent-1")))

    with pytest.raises(AppOwnedRoutineError, match="removing it included"):
        service.remove(SYNC)

    assert path.read_bytes() == _DAILY_TOML.encode()
    assert service.states.read(SYNC).chat_id == "agent-1"
    assert chat_app.verbs == []
    assert repository.commits == []


def test_a_routine_two_files_define_is_broken_and_nothing_of_it_is_changed_or_removed(
    paths: RoutinesPaths,
    repository: RecordingRepository,
    service: RoutinesService,
) -> None:
    own = paths.workspace_definition_path(SYNC)
    own.write_text(_DAILY_TOML)
    in_app = write_app_definition(paths, "news_sync", SYNC, _DAILY_TOML)
    service.states.update(SYNC, lambda state: state.model_copy_update(to_update(state.field_ref().chat_id, "agent-1")))

    row = service.row(SYNC)
    assert row.health == "BROKEN"
    assert row.error is not None and "more than one file" in row.error
    assert service.runner.tick().started == ()
    with pytest.raises(DuplicateRoutineError, match="more than one file"):
        service.remove(SYNC)
    with pytest.raises(DuplicateRoutineError, match="more than one file"):
        service.replace(
            SYNC,
            RoutineDefinition.model_validate({"title": "T", "what": "W", "every": "1d", "command": ["true"]}),
            "edit",
        )

    assert service.states.read(SYNC).chat_id == "agent-1"
    assert (own.read_text(), in_app.read_text()) == (_DAILY_TOML, _DAILY_TOML)
    assert repository.commits == []
