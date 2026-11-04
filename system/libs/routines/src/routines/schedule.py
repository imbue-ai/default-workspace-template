"""When a routine is due, and how its schedule reads: pure functions of the definition, the state and now.

Every datetime that crosses these functions is timezone-aware. A definition's ``at``, ``days``, ``day_of_month``
and ``once`` are in the workspace's local zone, which the caller passes in, so the arithmetic that touches a
calendar day happens in that zone and the answer comes back aware.
"""

import calendar
from datetime import datetime
from datetime import timedelta
from datetime import tzinfo
from typing import Final
from typing import assert_never

from imbue.imbue_common.pure import pure
from routines.data_types import LoadedRoutine
from routines.data_types import RoutineDefinition
from routines.data_types import RoutineState
from routines.primitives import Duration
from routines.primitives import DurationUnit
from routines.primitives import SECONDS_PER_DAY
from routines.primitives import SECONDS_PER_HOUR
from routines.primitives import SECONDS_PER_MINUTE
from routines.primitives import TimeOfDay
from routines.primitives import WEEKDAYS_IN_ORDER
from routines.primitives import Weekday

# How far past its due moment a routine may be before it is called overdue (section 5.2 of the spec).
OVERDUE_MARGIN_FLOOR_SECONDS: Final[int] = 5 * SECONDS_PER_MINUTE
OVERDUE_MARGIN_CEILING_SECONDS: Final[int] = SECONDS_PER_DAY

_WEEKDAY_NAMES: Final[dict[Weekday, str]] = {
    Weekday.MON: "Monday",
    Weekday.TUE: "Tuesday",
    Weekday.WED: "Wednesday",
    Weekday.THU: "Thursday",
    Weekday.FRI: "Friday",
    Weekday.SAT: "Saturday",
    Weekday.SUN: "Sunday",
}
_WORKING_WEEK: Final[frozenset[Weekday]] = frozenset((Weekday.MON, Weekday.TUE, Weekday.WED, Weekday.THU, Weekday.FRI))


@pure
def is_switched_on(loaded: LoadedRoutine, state: RoutineState) -> bool:
    """Whether the routine runs: the switch in its state, else its definition's default, else on."""
    if state.is_enabled is not None:
        return state.is_enabled
    return loaded.definition.enabled if loaded.definition is not None else True


@pure
def localize(moment: datetime, zone: tzinfo) -> datetime:
    """An aware moment in the zone; a naive one is read as already being in it."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=zone)
    return moment.astimezone(zone)


@pure
def add_cadence(moment: datetime, cadence: Duration, day_of_month: int | None, zone: tzinfo) -> datetime:
    """The moment one cadence later; days and weeks step the local calendar (the wall clock survives a daylight
    saving change), months step it and clamp the day, pinned by day_of_month."""
    if cadence.unit in (DurationUnit.DAYS, DurationUnit.WEEKS):
        return moment.astimezone(zone) + cadence.timedelta
    if not cadence.is_calendar_months:
        return moment + cadence.timedelta
    local = moment.astimezone(zone)
    month_index = local.month - 1 + cadence.count
    year = local.year + month_index // 12
    month = month_index % 12 + 1
    wanted_day = day_of_month if day_of_month is not None else local.day
    day = min(wanted_day, calendar.monthrange(year, month)[1])
    return local.replace(year=year, month=month, day=day)


@pure
def _at_time_on_date(date_moment: datetime, at: TimeOfDay) -> datetime:
    return date_moment.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)


@pure
def _advance_to_allowed_weekday(moment: datetime, days: tuple[Weekday, ...], zone: tzinfo) -> datetime:
    local = moment.astimezone(zone)
    allowed = frozenset(days)
    for offset in range(7):
        candidate = local + timedelta(days=offset)
        if WEEKDAYS_IN_ORDER[candidate.weekday()] in allowed:
            return candidate
    return local


@pure
def _cadence_anchor(definition: RoutineDefinition, state: RoutineState) -> datetime:
    """The moment the cadence counts from: the last success, which for an ``at`` window is the day its run
    started (a run that crosses midnight claims no second day) when the state kept that moment."""
    assert state.last_succeeded_at is not None
    if definition.at is not None and state.last_success_started_at is not None:
        return state.last_success_started_at
    return state.last_succeeded_at


@pure
def next_due(definition: RoutineDefinition, state: RoutineState, now: datetime, zone: tzinfo) -> datetime | None:
    """The earliest moment a scheduled run may start, or None for a routine that is done."""
    # Rules 1 and 2 give the due moment outright and 3 to 5 compose it; rule 6 floors whichever answered.
    if definition.once is not None:
        if state.last_succeeded_at is not None:
            return None
        due = localize(definition.once, zone)
    elif state.last_succeeded_at is None:
        due = now
    else:
        assert definition.every is not None
        base = add_cadence(_cadence_anchor(definition, state), definition.every, definition.day_of_month, zone)
        due = _at_time_on_date(base.astimezone(zone), definition.at) if definition.at is not None else base
        if definition.days:
            due = _advance_to_allowed_weekday(due, definition.days, zone)
    is_last_attempt_unfinished = state.last_started_at is not None and (
        state.last_succeeded_at is None or state.last_started_at > state.last_succeeded_at
    )
    if is_last_attempt_unfinished:
        assert state.last_started_at is not None
        retry_floor = state.last_started_at + definition.retry_after.timedelta
        due = max(due, retry_floor)
    return due


@pure
def overdue_margin_seconds(definition: RoutineDefinition) -> int:
    """How far past its due moment a routine may be before it is overdue."""
    if definition.every is None:
        return OVERDUE_MARGIN_FLOOR_SECONDS
    half_cadence = definition.every.approximate_seconds // 2
    return max(OVERDUE_MARGIN_FLOOR_SECONDS, min(half_cadence, OVERDUE_MARGIN_CEILING_SECONDS))


@pure
def is_overdue(definition: RoutineDefinition, due: datetime | None, now: datetime) -> bool:
    if due is None:
        return False
    return (now - due).total_seconds() > overdue_margin_seconds(definition)


@pure
def _plural(count: int, singular: str, plural: str) -> str:
    return singular if count == 1 else f"{count} {plural}"


@pure
def describe_cadence(cadence: Duration) -> str:
    match cadence.unit:
        case DurationUnit.SECONDS:
            return f"Every {cadence.count} seconds"
        case DurationUnit.MINUTES:
            return "Every minute" if cadence.count == 1 else f"Every {cadence.count} minutes"
        case DurationUnit.HOURS:
            return "Every hour" if cadence.count == 1 else f"Every {cadence.count} hours"
        case DurationUnit.DAYS:
            return "Every day" if cadence.count == 1 else f"Every {cadence.count} days"
        case DurationUnit.WEEKS:
            return "Every week" if cadence.count == 1 else f"Every {cadence.count} weeks"
        case DurationUnit.MONTHS:
            return "Every month" if cadence.count == 1 else f"Every {cadence.count} months"
        case _ as unreachable:
            assert_never(unreachable)


@pure
def describe_time_of_day(at: TimeOfDay) -> str:
    if at.hour == 0 and at.minute == 0:
        return "midnight"
    if at.hour == 12 and at.minute == 0:
        return "noon"
    display_hour = at.hour % 12 or 12
    suffix = "am" if at.hour < 12 else "pm"
    return f"{display_hour}:{at.minute:02d} {suffix}"


@pure
def _describe_days(days: tuple[Weekday, ...]) -> str:
    if frozenset(days) == _WORKING_WEEK:
        return "weekdays"
    names = [_WEEKDAY_NAMES[day] for day in WEEKDAYS_IN_ORDER if day in days]
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


@pure
def _ordinal(day: int) -> str:
    if 10 <= day % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix}"


@pure
def format_local_moment(moment: datetime, zone: tzinfo) -> str:
    """``Nov 3 at 3:00 pm`` in the zone."""
    local = moment.astimezone(zone)
    return f"{local.strftime('%b')} {local.day} at {describe_time_of_day(TimeOfDay(local.strftime('%H:%M')))}"


@pure
def describe_schedule(definition: RoutineDefinition, zone: tzinfo) -> str:
    """The schedule in the words the rows show."""
    if definition.once is not None:
        return f"Once, {format_local_moment(localize(definition.once, zone), zone)}"
    assert definition.every is not None
    if definition.every.is_calendar_months and definition.day_of_month is not None:
        head = (
            f"Monthly on the {_ordinal(definition.day_of_month)}"
            if definition.every.count == 1
            else f"{describe_cadence(definition.every)} on the {_ordinal(definition.day_of_month)}"
        )
    elif definition.days and definition.every.unit is DurationUnit.DAYS and definition.every.count == 1:
        head = (
            "Every weekday"
            if frozenset(definition.days) == _WORKING_WEEK
            else f"Every {_describe_days(definition.days)}"
        )
    elif definition.days:
        head = f"{describe_cadence(definition.every)} on {_describe_days(definition.days)}"
    else:
        head = describe_cadence(definition.every)
    if definition.at is not None:
        return f"{head} at {describe_time_of_day(definition.at)}"
    return head


@pure
def describe_moment(moment: datetime, now: datetime, zone: tzinfo) -> str:
    """A future moment relative to today: ``today 7:30 am``, ``tomorrow 7:30 am``, ``Nov 3 7:30 am``."""
    local = moment.astimezone(zone)
    today = now.astimezone(zone).date()
    time_text = describe_time_of_day(TimeOfDay(local.strftime("%H:%M")))
    days_ahead = (local.date() - today).days
    if days_ahead == 0:
        return f"today {time_text}"
    if days_ahead == 1:
        return f"tomorrow {time_text}"
    return f"{local.strftime('%b')} {local.day} {time_text}"


@pure
def describe_age(moment: datetime, now: datetime) -> str:
    """``just now``, ``3 minutes ago``, ``2 hours ago``, ``5 days ago``."""
    seconds = (now - moment).total_seconds()
    if seconds < SECONDS_PER_MINUTE:
        return "just now"
    if seconds < SECONDS_PER_HOUR:
        return f"{_plural(int(seconds // SECONDS_PER_MINUTE), '1 minute', 'minutes')} ago"
    if seconds < SECONDS_PER_DAY:
        return f"{_plural(int(seconds // SECONDS_PER_HOUR), '1 hour', 'hours')} ago"
    return f"{_plural(int(seconds // SECONDS_PER_DAY), '1 day', 'days')} ago"


@pure
def describe_duration(seconds: float) -> str:
    if seconds < 1:
        return "under a second"
    if seconds < SECONDS_PER_MINUTE:
        return f"{int(seconds)}s"
    if seconds < SECONDS_PER_HOUR:
        return f"{int(seconds // SECONDS_PER_MINUTE)}m {int(seconds % SECONDS_PER_MINUTE):02d}s"
    return f"{int(seconds // SECONDS_PER_HOUR)}h {int(seconds % SECONDS_PER_HOUR // SECONDS_PER_MINUTE):02d}m"
