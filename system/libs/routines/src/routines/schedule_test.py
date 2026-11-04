from datetime import datetime
from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest
from inline_snapshot import snapshot

from routines.data_types import RoutineDefinition
from routines.data_types import RoutineState
from routines.schedule import describe_age
from routines.schedule import describe_duration
from routines.schedule import describe_moment
from routines.schedule import describe_schedule
from routines.schedule import is_overdue
from routines.schedule import next_due
from routines.schedule import overdue_margin_seconds
from routines.testing import TEST_NOW
from routines.testing import TEST_ZONE


def _definition(**fields: object) -> RoutineDefinition:
    values: dict[str, object] = {"title": "A routine", "what": "Does a thing.", "command": ["true"]}
    values.update(fields)
    return RoutineDefinition.model_validate(values)


def _local(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=TEST_ZONE)


def test_a_routine_that_never_ran_is_due_now() -> None:
    assert next_due(_definition(every="1d", at="07:30"), RoutineState(), TEST_NOW, TEST_ZONE) == TEST_NOW


def test_a_routine_that_never_ran_is_due_now_whatever_weekdays_it_names() -> None:
    # TEST_NOW is a Tuesday; a routine that has never succeeded is due outright, not moved to Saturday.
    definition = _definition(every="1d", at="09:00", days=["sat"])
    assert next_due(definition, RoutineState(), TEST_NOW, TEST_ZONE) == TEST_NOW


def test_the_next_due_moment_is_one_cadence_after_the_last_success() -> None:
    state = RoutineState(last_succeeded_at=TEST_NOW - timedelta(minutes=10))
    assert next_due(_definition(every="15m"), state, TEST_NOW, TEST_ZONE) == TEST_NOW + timedelta(minutes=5)


def test_a_time_of_day_anchors_the_due_moment_to_that_time_on_the_next_day() -> None:
    state = RoutineState(last_succeeded_at=_local(2026, 10, 20, 7, 4))
    assert next_due(_definition(every="1d", at="07:30"), state, TEST_NOW, TEST_ZONE) == _local(2026, 10, 21, 7, 30)


def test_a_late_catch_up_run_still_lands_on_the_time_of_day_the_next_day() -> None:
    state = RoutineState(last_succeeded_at=_local(2026, 10, 20, 10, 0))
    assert next_due(_definition(every="1d", at="07:30"), state, TEST_NOW, TEST_ZONE) == _local(2026, 10, 21, 7, 30)


def test_a_time_of_day_window_is_counted_from_the_day_the_successful_run_started() -> None:
    # A 23:50 run that finished past midnight belongs to the day it started; the next window is the day after.
    state = RoutineState(
        last_success_started_at=_local(2026, 10, 20, 23, 50), last_succeeded_at=_local(2026, 10, 21, 0, 3)
    )
    now = _local(2026, 10, 21, 0, 4)
    assert next_due(_definition(every="1d", at="23:50"), state, now, TEST_ZONE) == _local(2026, 10, 21, 23, 50)
    # Without the start (a state written before it was kept) the finish counts, as it does without ``at``.
    finish_only = RoutineState(last_succeeded_at=_local(2026, 10, 21, 0, 3))
    assert next_due(_definition(every="1d", at="23:50"), finish_only, now, TEST_ZONE) == _local(2026, 10, 22, 23, 50)
    assert next_due(_definition(every="1d"), state, now, TEST_ZONE) == _local(2026, 10, 22, 0, 3)


def test_a_time_of_day_window_steps_whole_local_days_across_a_daylight_saving_change() -> None:
    zone = ZoneInfo("America/Los_Angeles")
    # Daylight saving ends on 2026-11-01: a day later than 00:30 PDT is 00:30 PST, not 23:30 the same day.
    ended = datetime(2026, 11, 1, 0, 30, tzinfo=zone)
    state = RoutineState(last_success_started_at=ended, last_succeeded_at=ended + timedelta(minutes=5))
    due = next_due(_definition(every="1d", at="00:30"), state, ended + timedelta(minutes=6), zone)
    assert due == datetime(2026, 11, 2, 0, 30, tzinfo=zone)
    # Daylight saving starts on 2026-03-08: a day later than 23:30 PST is 23:30 PDT the next day, none skipped.
    started = datetime(2026, 3, 7, 23, 30, tzinfo=zone)
    state = RoutineState(last_success_started_at=started, last_succeeded_at=started + timedelta(minutes=5))
    due = next_due(_definition(every="1d", at="23:30"), state, started + timedelta(minutes=6), zone)
    assert due == datetime(2026, 3, 8, 23, 30, tzinfo=zone)
    # A week steps seven local days the same way.
    due = next_due(_definition(every="1w", at="23:30"), state, started + timedelta(minutes=6), zone)
    assert due == datetime(2026, 3, 14, 23, 30, tzinfo=zone)


def test_weekdays_move_a_due_moment_to_the_next_listed_day() -> None:
    # The last success was Friday; the next day is Saturday, which is not listed, so Monday it is.
    state = RoutineState(last_succeeded_at=_local(2026, 10, 23, 9, 0))
    due = next_due(
        _definition(every="1d", at="09:00", days=["mon", "tue", "wed", "thu", "fri"]), state, TEST_NOW, TEST_ZONE
    )
    assert due == _local(2026, 10, 26, 9, 0)


def test_a_monthly_cadence_steps_the_calendar_and_clamps_the_day() -> None:
    state = RoutineState(last_succeeded_at=_local(2026, 1, 31, 8, 0))
    assert next_due(_definition(every="1mo"), state, TEST_NOW, TEST_ZONE) == _local(2026, 2, 28, 8, 0)


def test_a_day_of_the_month_pins_a_monthly_cadence() -> None:
    # A catch-up on the 3rd still makes the next run due on the 1st.
    state = RoutineState(last_succeeded_at=_local(2026, 10, 3, 9, 15))
    due = next_due(_definition(every="1mo", day_of_month=1, at="00:00"), state, TEST_NOW, TEST_ZONE)
    assert due == _local(2026, 11, 1, 0, 0)


def test_a_failed_attempt_floors_the_due_moment_at_the_retry_gap() -> None:
    state = RoutineState(
        last_succeeded_at=TEST_NOW - timedelta(hours=2), last_started_at=TEST_NOW - timedelta(seconds=30)
    )
    assert next_due(_definition(every="1h"), state, TEST_NOW, TEST_ZONE) == TEST_NOW + timedelta(seconds=90)


def test_the_retry_gap_also_floors_a_routine_that_never_succeeded_or_a_one_off() -> None:
    # Without the floor a failing routine would be started again on every tick.
    failed_just_now = RoutineState(last_started_at=TEST_NOW - timedelta(seconds=30))
    assert next_due(_definition(every="1h"), failed_just_now, TEST_NOW, TEST_ZONE) == TEST_NOW + timedelta(seconds=90)
    once = _definition(once=datetime(2026, 10, 20, 6, 0))
    assert next_due(once, failed_just_now, TEST_NOW, TEST_ZONE) == TEST_NOW + timedelta(seconds=90)


def test_a_gap_of_days_leaves_the_routine_due_at_once_and_counts_from_the_catch_up() -> None:
    state = RoutineState(last_succeeded_at=TEST_NOW - timedelta(days=3))
    definition = _definition(every="1d", at="07:30")
    due = next_due(definition, state, TEST_NOW, TEST_ZONE)
    assert due is not None and due < TEST_NOW
    caught_up = RoutineState(last_succeeded_at=TEST_NOW)
    assert next_due(definition, caught_up, TEST_NOW, TEST_ZONE) == _local(2026, 10, 21, 7, 30)


def test_a_one_off_is_due_at_its_moment_and_done_after_a_success() -> None:
    definition = _definition(once=datetime(2026, 11, 3, 15, 0))
    assert next_due(definition, RoutineState(), TEST_NOW, TEST_ZONE) == _local(2026, 11, 3, 15, 0)
    assert next_due(definition, RoutineState(last_succeeded_at=TEST_NOW), TEST_NOW, TEST_ZONE) is None


@pytest.mark.parametrize(
    ("every", "expected_seconds"),
    [("1m", 300), ("15m", 450), ("1h", 1800), ("1d", 43200), ("1w", 86400), ("1mo", 86400)],
)
def test_the_overdue_margin_is_half_a_cadence_within_five_minutes_and_a_day(every: str, expected_seconds: int) -> None:
    assert overdue_margin_seconds(_definition(every=every)) == expected_seconds


def test_a_routine_past_its_margin_is_overdue_and_one_within_it_is_not() -> None:
    definition = _definition(every="1h")
    assert is_overdue(definition, TEST_NOW - timedelta(minutes=31), TEST_NOW)
    assert not is_overdue(definition, TEST_NOW - timedelta(minutes=29), TEST_NOW)
    assert not is_overdue(definition, None, TEST_NOW)


def test_schedules_read_in_plain_words() -> None:
    assert [
        describe_schedule(_definition(every="15m"), TEST_ZONE),
        describe_schedule(_definition(every="1h"), TEST_ZONE),
        describe_schedule(_definition(every="1d", at="07:30"), TEST_ZONE),
        describe_schedule(_definition(every="1d", at="09:00", days=["mon", "fri"]), TEST_ZONE),
        describe_schedule(_definition(every="1d", at="09:00", days=["mon", "tue", "wed", "thu", "fri"]), TEST_ZONE),
        describe_schedule(_definition(every="2w", days=["sat"]), TEST_ZONE),
        describe_schedule(_definition(every="1mo", day_of_month=1, at="00:00"), TEST_ZONE),
        describe_schedule(_definition(every="2mo", day_of_month=15, at="12:00"), TEST_ZONE),
        describe_schedule(_definition(once=datetime(2026, 11, 3, 15, 0)), TEST_ZONE),
    ] == snapshot(
        [
            "Every 15 minutes",
            "Every hour",
            "Every day at 7:30 am",
            "Every Monday and Friday at 9:00 am",
            "Every weekday at 9:00 am",
            "Every 2 weeks on Saturday",
            "Monthly on the 1st at midnight",
            "Every 2 months on the 15th at noon",
            "Once, Nov 3 at 3:00 pm",
        ]
    )


def test_moments_ages_and_durations_read_relative_to_now() -> None:
    assert [
        describe_moment(_local(2026, 10, 20, 9, 30), TEST_NOW, TEST_ZONE),
        describe_moment(_local(2026, 10, 21, 7, 30), TEST_NOW, TEST_ZONE),
        describe_moment(_local(2026, 11, 3, 15, 0), TEST_NOW, TEST_ZONE),
        describe_age(TEST_NOW - timedelta(seconds=5), TEST_NOW),
        describe_age(TEST_NOW - timedelta(minutes=1), TEST_NOW),
        describe_age(TEST_NOW - timedelta(minutes=3), TEST_NOW),
        describe_age(TEST_NOW - timedelta(hours=2), TEST_NOW),
        describe_age(TEST_NOW - timedelta(days=5), TEST_NOW),
        describe_duration(0.4),
        describe_duration(12),
        describe_duration(185),
        describe_duration(3720),
    ] == snapshot(
        [
            "today 9:30 am",
            "tomorrow 7:30 am",
            "Nov 3 3:00 pm",
            "just now",
            "1 minute ago",
            "3 minutes ago",
            "2 hours ago",
            "5 days ago",
            "under a second",
            "12s",
            "3m 05s",
            "1h 02m",
        ]
    )
