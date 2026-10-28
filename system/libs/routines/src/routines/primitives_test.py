from datetime import timedelta

import pytest

from routines.errors import InvalidDurationError
from routines.errors import InvalidRoutineIdError
from routines.errors import InvalidTimeOfDayError
from routines.primitives import Duration
from routines.primitives import DurationUnit
from routines.primitives import LEDGER_DIRNAME
from routines.primitives import RoutineId
from routines.primitives import SECONDS_PER_DAY
from routines.primitives import TimeOfDay


def test_the_ledgers_directory_name_is_not_a_routine_id() -> None:
    """A routine of that name would have the run ledger's directory as its state, and remove it with itself."""
    with pytest.raises(InvalidRoutineIdError, match="run ledger"):
        RoutineId(LEDGER_DIRNAME)
    assert RoutineId("events-digest") == "events-digest"


@pytest.mark.parametrize("value", ["Inbox", "-digest", "inbox_digest", "a" * 64, ""])
def test_a_string_that_is_not_lowercase_letters_digits_and_hyphens_is_not_a_routine_id(value: str) -> None:
    with pytest.raises(InvalidRoutineIdError):
        RoutineId(value)


def test_a_duration_knows_its_count_unit_and_length() -> None:
    assert Duration(" 3h ").count == 3
    assert Duration("3h").unit is DurationUnit.HOURS
    assert Duration("3h").timedelta == timedelta(hours=3)
    assert Duration("2w").approximate_seconds == 14 * SECONDS_PER_DAY
    assert Duration("1mo").is_calendar_months
    assert Duration("1mo").approximate_seconds == 30 * SECONDS_PER_DAY


@pytest.mark.parametrize("value", ["0m", "abc", "15", "m", "1.5h", "1 h", "1y", ""])
def test_a_string_that_is_not_a_whole_number_and_a_unit_is_not_a_duration(value: str) -> None:
    with pytest.raises(InvalidDurationError):
        Duration(value)


def test_a_cadence_in_months_has_no_fixed_length() -> None:
    with pytest.raises(InvalidDurationError, match="months"):
        _ = Duration("1mo").timedelta


def test_a_time_of_day_knows_its_hour_and_minute() -> None:
    assert (TimeOfDay("07:30").hour, TimeOfDay("07:30").minute) == (7, 30)
    assert TimeOfDay(" 23:59 ") == "23:59"


@pytest.mark.parametrize("value", ["7:30", "24:00", "07:60", "0730", "07:30:00", ""])
def test_a_string_that_is_not_hh_mm_on_a_24_hour_clock_is_not_a_time_of_day(value: str) -> None:
    with pytest.raises(InvalidTimeOfDayError):
        TimeOfDay(value)
