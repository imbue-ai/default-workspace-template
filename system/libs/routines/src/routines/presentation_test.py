from datetime import datetime
from datetime import timedelta

from imbue.imbue_common.logging import format_nanosecond_iso_timestamp
from imbue.imbue_common.logging import generate_log_event_id
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.primitives import NonNegativeInt
from routines.data_types import CurrentRun
from routines.data_types import LastRun
from routines.data_types import LoadedRoutine
from routines.data_types import RUN_BLOCKED_EVENT
from routines.data_types import RUN_STARTED_EVENT
from routines.data_types import RoutineDefinition
from routines.data_types import RoutineState
from routines.data_types import RunBlockedEvent
from routines.data_types import RunStartedEvent
from routines.data_types import ScheduleOverride
from routines.presentation import app_schedule_text
from routines.presentation import build_listing
from routines.presentation import build_row
from routines.presentation import fold_run_events
from routines.presentation import has_schedule_override
from routines.presentation import is_schedule_only
from routines.primitives import Duration
from routines.primitives import RUN_EVENTS_SOURCE
from routines.primitives import RoutineHealth
from routines.primitives import RunId
from routines.primitives import RunKind
from routines.primitives import RunOutcome
from routines.primitives import RunTrigger
from routines.primitives import RunWarning
from routines.primitives import TimeOfDay
from routines.testing import TEST_NOW
from routines.testing import TEST_ROUTINE_ID
from routines.testing import TEST_ZONE
from routines.testing import loaded_routine


def _loaded(**fields: object) -> LoadedRoutine:
    values: dict[str, object] = {
        "title": "Morning inbox digest",
        "what": "Reads mail.",
        "every": "1h",
        "command": ["true"],
    }
    values.update(fields)
    return loaded_routine(TEST_ROUTINE_ID, RoutineDefinition.model_validate(values))


def _last_run(outcome: RunOutcome, started_at: datetime) -> LastRun:
    return LastRun(
        run_id=RunId.generate(),
        trigger=RunTrigger.SCHEDULED,
        kind=RunKind.COMMAND,
        outcome=outcome,
        summary="3 messages summarized",
        started_at=started_at,
        finished_at=started_at + timedelta(seconds=12),
    )


def test_a_broken_definition_is_broken_whatever_its_state() -> None:
    loaded = loaded_routine(TEST_ROUTINE_ID, None, error="what: Field required")
    row = build_row(loaded, RoutineState(is_enabled=False), TEST_NOW, TEST_ZONE, {})
    assert row.health is RoutineHealth.BROKEN
    assert row.health_detail == "what: Field required"
    assert row.title == "Inbox digest"


def test_the_switch_in_the_state_wins_over_the_definition_default() -> None:
    assert build_row(_loaded(enabled=False), RoutineState(), TEST_NOW, TEST_ZONE, {}).health is RoutineHealth.OFF
    assert (
        build_row(_loaded(enabled=False), RoutineState(is_enabled=True), TEST_NOW, TEST_ZONE, {}).health
        is RoutineHealth.NOT_RUN_YET
    )
    assert build_row(_loaded(), RoutineState(is_enabled=False), TEST_NOW, TEST_ZONE, {}).health is RoutineHealth.OFF


def test_a_blocked_run_in_flight_needs_the_user() -> None:
    current = CurrentRun(
        run_id=RunId.generate(), pid=1, trigger=RunTrigger.SCHEDULED, started_at=TEST_NOW, is_blocked=True
    )
    row = build_row(
        _loaded(), RoutineState(current_run=current, consecutive_failures=NonNegativeInt(2)), TEST_NOW, TEST_ZONE, {}
    )
    assert row.health is RoutineHealth.NEEDS_USER
    assert row.is_running
    assert row.next_run_text == "running now"


def test_a_failing_routine_says_how_long_since_it_worked_and_how_many_tries() -> None:
    state = RoutineState(
        last_succeeded_at=TEST_NOW - timedelta(hours=5),
        last_started_at=TEST_NOW - timedelta(minutes=1),
        consecutive_failures=NonNegativeInt(3),
        last_run=_last_run(RunOutcome.FAILED, TEST_NOW - timedelta(minutes=1)),
    )
    row = build_row(_loaded(), state, TEST_NOW, TEST_ZONE, {})
    assert row.health is RoutineHealth.FAILING
    assert row.health_detail == "Last worked 5 hours ago; 3 failed tries in a row."
    assert row.next_run_text == "today 7:01 am"
    assert row.last_run_text == "Failed 1 minute ago, automatic"


def test_a_routine_past_its_margin_with_nothing_running_is_overdue() -> None:
    state = RoutineState(
        last_succeeded_at=TEST_NOW - timedelta(hours=2),
        last_run=_last_run(RunOutcome.SUCCEEDED, TEST_NOW - timedelta(hours=2)),
    )
    row = build_row(_loaded(), state, TEST_NOW, TEST_ZONE, {})
    assert row.health is RoutineHealth.OVERDUE
    assert row.next_run_text == "any minute"


def test_a_working_routine_reads_its_schedule_and_its_last_run() -> None:
    state = RoutineState(
        last_succeeded_at=TEST_NOW - timedelta(minutes=10),
        last_run=_last_run(RunOutcome.SUCCEEDED, TEST_NOW - timedelta(minutes=10)),
    )
    row = build_row(_loaded(), state, TEST_NOW, TEST_ZONE, {})
    assert row.health is RoutineHealth.WORKING
    assert row.schedule_text == "Every hour"
    assert row.next_run_text == "today 7:50 am"
    assert row.last_run_text == "Worked 10 minutes ago, automatic"
    assert row.last_run is not None
    assert row.last_run.summary == "3 messages summarized"


def test_the_row_carries_the_last_runs_warnings() -> None:
    worked = _last_run(RunOutcome.SUCCEEDED, TEST_NOW - timedelta(minutes=10))
    last = worked.model_copy_update(to_update(worked.field_ref().warnings, (RunWarning.RESTARTED_PREVIOUS_RUN,)))
    state = RoutineState(last_succeeded_at=TEST_NOW - timedelta(minutes=10), last_run=last)
    row = build_row(_loaded(), state, TEST_NOW, TEST_ZONE, {})
    assert row.last_run is not None
    assert row.last_run.warnings == (RunWarning.RESTARTED_PREVIOUS_RUN,)


def test_a_one_off_that_ran_is_done() -> None:
    loaded = _loaded(every=None, once=datetime(2026, 10, 20, 6, 0))
    row = build_row(loaded, RoutineState(last_succeeded_at=TEST_NOW - timedelta(hours=1)), TEST_NOW, TEST_ZONE, {})
    assert row.health is RoutineHealth.DONE
    assert row.next_run_text == ""


def test_the_listing_puts_problems_first_and_counts_them() -> None:
    failing = build_row(
        _loaded(title="B failing"),
        RoutineState(
            last_succeeded_at=TEST_NOW - timedelta(hours=1),
            last_started_at=TEST_NOW,
            consecutive_failures=NonNegativeInt(1),
        ),
        TEST_NOW,
        TEST_ZONE,
        {},
    )
    working = build_row(
        _loaded(title="A working"),
        RoutineState(last_succeeded_at=TEST_NOW - timedelta(minutes=5)),
        TEST_NOW,
        TEST_ZONE,
        {},
    )
    off = build_row(_loaded(title="C off", enabled=False), RoutineState(), TEST_NOW, TEST_ZONE, {})

    listing = build_listing([off, working, failing], {}, TEST_NOW, TEST_ZONE)

    assert [row.title for row in listing.routines] == ["B failing", "A working", "C off"]
    assert listing.summary.needs_attention == 1
    assert listing.summary.total == 3
    assert listing.summary.next_run_text == "today 7:02 am"


def test_run_events_fold_into_records_with_a_blocked_run_still_going() -> None:
    run_id = RunId.generate()
    envelope = {
        "timestamp": format_nanosecond_iso_timestamp(TEST_NOW - timedelta(minutes=2)),
        "event_id": generate_log_event_id(),
        "source": RUN_EVENTS_SOURCE,
    }
    started = RunStartedEvent(
        **envelope,
        type=RUN_STARTED_EVENT,
        routine_id=TEST_ROUTINE_ID,
        run_id=run_id,
        trigger=RunTrigger.MANUAL,
        kind=RunKind.SKILL,
        command="/inbox-digest",
    )
    blocked = RunBlockedEvent(
        **{**envelope, "event_id": generate_log_event_id()},
        type=RUN_BLOCKED_EVENT,
        routine_id=TEST_ROUTINE_ID,
        run_id=run_id,
    )

    records = fold_run_events([blocked, started], TEST_NOW)

    assert len(records) == 1
    assert records[0].outcome is None
    assert records[0].is_blocked
    assert records[0].outcome_text == "Waiting for you"
    assert records[0].when_text == "2 minutes ago"


def test_a_row_reads_the_schedule_the_user_gave_a_routine_an_app_owns() -> None:
    """The words, the next run and the overdue margin all follow the user's schedule, not the app's file."""
    definition = RoutineDefinition.model_validate(
        {"title": "Inbox status", "what": "Checks mail.", "every": "1h", "command": ["true"]}
    )
    loaded = loaded_routine(TEST_ROUTINE_ID, definition, app_package="inbox_status")
    last = _last_run(RunOutcome.SUCCEEDED, TEST_NOW - timedelta(hours=3))
    ran_three_hours_ago = RoutineState(last_succeeded_at=TEST_NOW - timedelta(hours=3), last_run=last)
    rescheduled = ran_three_hours_ago.model_copy_update(
        to_update(
            ran_three_hours_ago.field_ref().schedule_override,
            ScheduleOverride(every=Duration("1d"), at=TimeOfDay("09:00")),
        )
    )

    by_the_file = build_row(loaded, ran_three_hours_ago, TEST_NOW, TEST_ZONE, {})
    by_the_user = build_row(loaded, rescheduled, TEST_NOW, TEST_ZONE, {})

    assert (by_the_file.schedule_text, by_the_file.health) == ("Every hour", RoutineHealth.OVERDUE)
    assert (by_the_user.schedule_text, by_the_user.health) == ("Every day at 9:00 am", RoutineHealth.WORKING)
    assert by_the_user.next_due_at == datetime(2026, 10, 21, 9, 0, tzinfo=TEST_ZONE)
    assert by_the_user.next_run_text == "tomorrow 9:00 am"


def test_only_a_routine_in_an_apps_directory_is_schedule_only_whatever_its_definition_names() -> None:
    """The directory the file is in decides it: a definition's own ``app`` field is for grouping, and a routine
    under system/routines that names an app is still the workspace's to edit and remove."""
    named = _loaded(app="inbox-status")
    unnamed_in_an_app = loaded_routine(TEST_ROUTINE_ID, named.definition, app_package="inbox_status")
    broken_in_an_app = loaded_routine(TEST_ROUTINE_ID, None, error="what: Field required", app_package="inbox_status")

    assert not is_schedule_only(named)
    assert is_schedule_only(unnamed_in_an_app)
    assert is_schedule_only(broken_in_an_app)


def test_an_override_is_in_place_only_for_a_readable_routine_an_app_owns_whose_state_keeps_one() -> None:
    definition = RoutineDefinition.model_validate(
        {"title": "Inbox status", "what": "Checks mail.", "every": "1h", "command": ["true"]}
    )
    rescheduled = RoutineState(schedule_override=ScheduleOverride(every=Duration("1d"), at=TimeOfDay("09:00")))
    in_an_app = loaded_routine(TEST_ROUTINE_ID, definition, app_package="inbox_status")
    in_the_workspace = loaded_routine(TEST_ROUTINE_ID, definition)
    broken_in_an_app = loaded_routine(TEST_ROUTINE_ID, None, error="what: Field required", app_package="inbox_status")

    assert has_schedule_override(in_an_app, rescheduled)
    assert app_schedule_text(in_an_app, rescheduled, TEST_ZONE) == "Every hour"
    # The row itself reads the user's schedule, so the two together say what was changed from what.
    assert build_row(in_an_app, rescheduled, TEST_NOW, TEST_ZONE, {}).schedule_text == "Every day at 9:00 am"

    assert not has_schedule_override(in_an_app, RoutineState())
    assert app_schedule_text(in_an_app, RoutineState(), TEST_ZONE) is None
    # A routine under system/routines never reads the field, and a broken one has no schedule to go back to.
    assert not has_schedule_override(in_the_workspace, rescheduled)
    assert app_schedule_text(in_the_workspace, rescheduled, TEST_ZONE) is None
    assert not has_schedule_override(broken_in_an_app, rescheduled)
    assert app_schedule_text(broken_in_an_app, rescheduled, TEST_ZONE) is None


def test_a_row_names_the_app_that_owns_the_routine_and_what_that_leaves_to_change() -> None:
    """The four fields the page and the command's JSON read: the owner by the directory the file is in, under the
    name the workspace's record gives that directory when it has one, and the app's own schedule while the
    user's is in its place."""
    definition = RoutineDefinition.model_validate(
        {"title": "Inbox status", "what": "Checks mail.", "every": "1h", "command": ["true"]}
    )
    in_an_app = loaded_routine(TEST_ROUTINE_ID, definition, app_package="inbox_status")
    rescheduled = RoutineState(schedule_override=ScheduleOverride(every=Duration("1d"), at=TimeOfDay("09:00")))

    named = build_row(in_an_app, RoutineState(), TEST_NOW, TEST_ZONE, {"inbox_status": "inbox-status"})
    assert (named.owning_app, named.app) == ("inbox-status", None)
    assert (named.is_schedule_only, named.has_schedule_override, named.app_schedule_text) == (True, False, None)

    by_directory = build_row(in_an_app, rescheduled, TEST_NOW, TEST_ZONE, {"mail_digest": "mail-digest"})
    assert by_directory.owning_app == "inbox_status"
    assert (by_directory.has_schedule_override, by_directory.app_schedule_text) == (True, "Every hour")
    assert by_directory.schedule_text == "Every day at 9:00 am"

    workspace_row = build_row(
        _loaded(app="inbox-status"), rescheduled, TEST_NOW, TEST_ZONE, {"inbox_status": "inbox-status"}
    )
    assert (workspace_row.owning_app, workspace_row.app) == (None, "inbox-status")
    assert (workspace_row.is_schedule_only, workspace_row.has_schedule_override) == (False, False)
    assert workspace_row.app_schedule_text is None
