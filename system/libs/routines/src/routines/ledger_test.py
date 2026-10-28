import gzip
import threading
from datetime import datetime
from datetime import timedelta

from imbue.imbue_common.logging import format_nanosecond_iso_timestamp
from imbue.imbue_common.logging import generate_log_event_id
from routines.data_types import RUN_FINISHED_EVENT
from routines.data_types import RUN_STARTED_EVENT
from routines.data_types import RunFinishedEvent
from routines.data_types import RunStartedEvent
from routines.ledger import FileLedger
from routines.ledger import LEDGER_SIZE_CAP_BYTES
from routines.locks import waited_lock
from routines.primitives import RUN_EVENTS_SOURCE
from routines.primitives import RoutineId
from routines.primitives import RunId
from routines.primitives import RunKind
from routines.primitives import RunOutcome
from routines.primitives import RunTrigger
from routines.testing import TEST_NOW
from routines.testing import TEST_ROUTINE_ID

OTHER = RoutineId("other")


def _started(routine_id: RoutineId, run_id: RunId, at: datetime) -> RunStartedEvent:
    return RunStartedEvent(
        timestamp=format_nanosecond_iso_timestamp(at),
        type=RUN_STARTED_EVENT,
        event_id=generate_log_event_id(),
        source=RUN_EVENTS_SOURCE,
        routine_id=routine_id,
        run_id=run_id,
        trigger=RunTrigger.SCHEDULED,
        kind=RunKind.COMMAND,
        command="true",
    )


def _finished(routine_id: RoutineId, run_id: RunId, at: datetime) -> RunFinishedEvent:
    return RunFinishedEvent(
        timestamp=format_nanosecond_iso_timestamp(at),
        type=RUN_FINISHED_EVENT,
        event_id=generate_log_event_id(),
        source=RUN_EVENTS_SOURCE,
        routine_id=routine_id,
        run_id=run_id,
        outcome=RunOutcome.SUCCEEDED,
        exit_code=0,
        duration_seconds=1.0,
        summary="",
        chat_id=None,
        warnings=(),
        output_tail="",
    )


def test_recent_events_cover_only_the_newest_runs_of_the_routine(ledger: FileLedger) -> None:
    run_ids = [RunId.generate() for _ in range(3)]
    for index, run_id in enumerate(run_ids):
        at = TEST_NOW + timedelta(minutes=index)
        ledger.append(_started(TEST_ROUTINE_ID, run_id, at))
        ledger.append(_finished(TEST_ROUTINE_ID, run_id, at + timedelta(seconds=1)))
    ledger.append(_started(OTHER, RunId.generate(), TEST_NOW + timedelta(minutes=5)))

    events = ledger.recent_events(TEST_ROUTINE_ID, run_limit=2)

    assert [event.run_id for event in events] == [run_ids[2], run_ids[2], run_ids[1], run_ids[1]]
    assert all(event.routine_id == TEST_ROUTINE_ID for event in events)


def test_the_events_of_one_run_come_back_oldest_first(ledger: FileLedger) -> None:
    run_id = RunId.generate()
    ledger.append(_started(TEST_ROUTINE_ID, run_id, TEST_NOW))
    ledger.append(_started(TEST_ROUTINE_ID, RunId.generate(), TEST_NOW + timedelta(minutes=1)))
    ledger.append(_finished(TEST_ROUTINE_ID, run_id, TEST_NOW + timedelta(seconds=2)))

    events = ledger.events_of_run(TEST_ROUTINE_ID, run_id)

    assert [type(event).__name__ for event in events] == ["RunStartedEvent", "RunFinishedEvent"]
    assert ledger.events_of_run(TEST_ROUTINE_ID, RunId.generate()) == []


def test_a_line_that_is_not_an_event_is_skipped(ledger: FileLedger) -> None:
    run_id = RunId.generate()
    ledger.append(_started(TEST_ROUTINE_ID, run_id, TEST_NOW))
    with ledger.paths.events_path.open("a") as handle:
        handle.write('not json\n{"type": "run_started"}\n')

    assert [event.run_id for event in ledger.recent_events(TEST_ROUTINE_ID, run_limit=5)] == [run_id]


def test_the_ledger_is_archived_once_past_the_cap(ledger: FileLedger) -> None:
    ledger.append(_started(TEST_ROUTINE_ID, RunId.generate(), TEST_NOW))
    assert ledger.rotate_if_large(TEST_NOW) is False
    with ledger.paths.events_path.open("a") as handle:
        handle.write("x" * LEDGER_SIZE_CAP_BYTES)

    assert ledger.rotate_if_large(TEST_NOW) is True

    assert not ledger.paths.events_path.exists()
    archive = ledger.paths.events_path.parent / "events.2026-10-20.jsonl.gz"
    with gzip.open(archive, "rt", encoding="utf-8") as handle:
        assert handle.read().count("run_started") == 1
    assert not (ledger.paths.events_path.parent / "events.jsonl.rotating").exists()
    # The next append starts the new file.
    run_id = RunId.generate()
    ledger.append(_started(TEST_ROUTINE_ID, run_id, TEST_NOW))
    assert [event.run_id for event in ledger.recent_events(TEST_ROUTINE_ID, run_limit=5)] == [run_id]


def test_a_second_archive_on_the_same_day_gets_its_own_name(ledger: FileLedger) -> None:
    for _ in range(2):
        ledger.append(_started(TEST_ROUTINE_ID, RunId.generate(), TEST_NOW))
        with ledger.paths.events_path.open("a") as handle:
            handle.write("x" * LEDGER_SIZE_CAP_BYTES)
        assert ledger.rotate_if_large(TEST_NOW) is True

    archives = sorted(path.name for path in ledger.paths.events_path.parent.glob("*.gz"))
    assert archives == ["events.2026-10-20.1.jsonl.gz", "events.2026-10-20.jsonl.gz"]


def test_a_file_a_dead_rotation_left_aside_is_archived_before_the_next(ledger: FileLedger) -> None:
    ledger.append(_started(TEST_ROUTINE_ID, RunId.generate(), TEST_NOW))
    left_aside = ledger.paths.events_path.parent / "events.jsonl.rotating"
    left_aside.write_text('{"type": "run_started", "left": "aside"}\n')

    assert ledger.rotate_if_large(TEST_NOW) is False

    assert not left_aside.exists()
    with gzip.open(ledger.paths.events_path.parent / "events.2026-10-20.jsonl.gz", "rt", encoding="utf-8") as handle:
        assert "aside" in handle.read()
    assert ledger.paths.events_path.is_file()


def test_an_append_waits_for_a_rotation_holding_the_ledger_lock(ledger: FileLedger) -> None:
    """A run appends from a process of its own; one that lands while the file is moved aside must not be lost."""
    ledger.append(_started(TEST_ROUTINE_ID, RunId.generate(), TEST_NOW))
    appended = threading.Event()
    run_id = RunId.generate()

    def append() -> None:
        ledger.append(_started(TEST_ROUTINE_ID, run_id, TEST_NOW))
        appended.set()

    with waited_lock(ledger.paths.ledger_lock_path):
        appender = threading.Thread(target=append)
        appender.start()
        assert not appended.wait(0.2), "the append went through under the rotation's lock"
    assert appended.wait(5)
    appender.join()
    assert [event.run_id for event in ledger.recent_events(TEST_ROUTINE_ID, run_limit=1)] == [run_id]
