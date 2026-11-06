from datetime import datetime
from datetime import timedelta
from datetime import timezone

from routines.data_types import LoadedRoutine
from routines.data_types import RoutineDefinition
from routines.data_types import RoutineDetail
from routines.data_types import RoutineState
from routines.data_types import RunRecord
from routines.fix_brief import build_fix_brief
from routines.fix_brief import fix_chat_name
from routines.presentation import build_row
from routines.primitives import RoutineId
from routines.primitives import RunId
from routines.primitives import RunKind
from routines.primitives import RunOutcome
from routines.primitives import RunTrigger
from routines.testing import TEST_NOW
from routines.testing import TEST_ZONE

SYNC = RoutineId("news-sync")


def _run(started_at: datetime) -> RunRecord:
    return RunRecord(
        run_id=RunId.generate(),
        routine_id=SYNC,
        trigger=RunTrigger.SCHEDULED,
        kind=RunKind.COMMAND,
        outcome=RunOutcome.FAILED,
        started_at=started_at,
        finished_at=started_at + timedelta(seconds=3),
        duration_seconds=3.0,
        summary="the feed answered 500",
        chat_id=None,
        warnings=(),
        is_blocked=False,
        exit_code=1,
        when_text="2 hours ago",
        duration_text="3s",
        outcome_text="Failed",
    )


def test_the_brief_names_the_routine_its_failure_and_each_run_with_an_unambiguous_moment() -> None:
    definition = RoutineDefinition.model_validate(
        {
            "title": "News sync",
            "what": "Pulls news.",
            "every": "6h",
            "command": ["python3", "sync.py", "--source", "top stories"],
            "app": "news",
        }
    )
    loaded = LoadedRoutine(routine_id=SYNC, definition=definition, error=None)
    state = RoutineState(consecutive_failures=2, last_started_at=TEST_NOW - timedelta(hours=2))
    row = build_row(loaded, state, TEST_NOW, TEST_ZONE)
    detail = RoutineDetail(
        row=row,
        definition=definition,
        definition_path="system/routines/news-sync.toml",
        runs=(_run(TEST_NOW - timedelta(hours=2)),),
    )

    brief = build_fix_brief(detail, "/news-sync", "pulling...\nTraceback: the feed answered 500\n")

    assert brief.startswith('/assist The routine "News sync" is not working')
    assert "## Last run's output\n\n```\npulling...\nTraceback: the feed answered 500\n```" in brief
    # Quoted as a shell would need it, so the fixing agent can run the line as it is.
    assert "- Command it runs: `python3 sync.py --source 'top stories'`" in brief
    assert "- Belongs to the app: news" in brief
    assert "- Current state: Failing" in brief
    # The moment carries its zone, so the fixing agent cannot read it as local time.
    assert "- 2 hours ago (2026-10-20 13:00 UTC) · scheduled · Failed" in brief
    assert "  the feed answered 500" in brief
    assert fix_chat_name("News sync") == "Fix News sync"
    assert len(fix_chat_name("A" * 60)) == 40


def test_a_run_started_in_another_zone_is_still_printed_in_utc() -> None:
    local = _run(datetime(2026, 10, 20, 5, 0, tzinfo=timezone(timedelta(hours=-8))))
    definition = RoutineDefinition.model_validate({"title": "T", "what": "W", "every": "1d", "skill": "digest"})
    row = build_row(
        LoadedRoutine(routine_id=SYNC, definition=definition, error=None), RoutineState(), TEST_NOW, TEST_ZONE
    )
    detail = RoutineDetail(row=row, definition=definition, definition_path="x", runs=(local,))

    assert "(2026-10-20 13:00 UTC)" in build_fix_brief(detail, "/news-sync", "")


def test_a_long_log_is_quoted_by_its_final_lines_and_an_empty_one_not_at_all() -> None:
    definition = RoutineDefinition.model_validate({"title": "T", "what": "W", "every": "1d", "skill": "digest"})
    row = build_row(
        LoadedRoutine(routine_id=SYNC, definition=definition, error=None), RoutineState(), TEST_NOW, TEST_ZONE
    )
    detail = RoutineDetail(row=row, definition=definition, definition_path="x", runs=())

    long_log = "\n".join(f"line {number}" for number in range(100)) + "\n"
    quoted = build_fix_brief(detail, "/news-sync", long_log)
    assert "## Last run's output (its final lines)" in quoted
    assert "line 60\n" in quoted and "line 59\n" not in quoted

    assert "Last run's output" not in build_fix_brief(detail, "/news-sync", "")
