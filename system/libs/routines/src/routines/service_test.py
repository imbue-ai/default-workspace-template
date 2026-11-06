import subprocess

import pytest

from chat_background_tasks.markers import process_start_time
from imbue.imbue_common.model_update import to_update
from routines.clock import SystemClock
from routines.data_types import CurrentRun
from routines.data_types import RoutineDefinition
from routines.errors import DefinitionCommitError
from routines.errors import RunInFlightError
from routines.locks import held_lock
from routines.mock_chat_app_test import ScriptedChatApp
from routines.mock_clock_test import FakeClock
from routines.mock_notifier_test import RecordingNotifier
from routines.mock_repository_test import RecordingRepository
from routines.mock_state_store_test import StateStoreWithARunArriving
from routines.paths import RoutinesPaths
from routines.primitives import RoutineId
from routines.primitives import RunId
from routines.primitives import RunTrigger
from routines.state import FileStateStore
from routines.testing import TEST_NOW
from routines.testing import TEST_ZONE
from routines.testing import build_test_service
from routines.testing import wait_for_last_run

SYNC = RoutineId("nightly-sync")


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
    assert not paths.definition_path(SYNC).exists()


def test_a_run_record_a_dead_runner_left_is_not_a_run_in_flight(
    paths: RoutinesPaths,
    repository: RecordingRepository,
    clock: FakeClock,
    chat_app: ScriptedChatApp,
    notifier: RecordingNotifier,
) -> None:
    """Until the tick reaps it, the state still names the run; neither Run now nor a removal waits on it."""
    service = build_test_service(paths, repository, FileStateStore(paths=paths), clock, chat_app, notifier)
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

    assert not paths.definition_path(SYNC).exists()


def test_a_routine_whose_run_lock_is_held_cannot_be_removed(
    paths: RoutinesPaths,
    repository: RecordingRepository,
    clock: FakeClock,
    chat_app: ScriptedChatApp,
    notifier: RecordingNotifier,
) -> None:
    service = build_test_service(paths, repository, FileStateStore(paths=paths), clock, chat_app, notifier)
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
    assert not paths.definition_path(SYNC).exists()
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
