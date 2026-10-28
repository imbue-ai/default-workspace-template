import re
from datetime import timedelta
from enum import auto
from typing import Final
from typing import Self

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.ids import RandomId
from imbue.imbue_common.primitives import NonEmptyStr
from routines.errors import InvalidDurationError
from routines.errors import InvalidRoutineIdError
from routines.errors import InvalidTimeOfDayError

ROUTINE_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
# The run ledger's directory under the state dir, beside the routines' own; no routine may take the name.
LEDGER_DIRNAME: Final[str] = "events"
# The ledger's source: the folder under the ledger's directory the run events live in, and the ``source`` every
# event's envelope names.
RUN_EVENTS_SOURCE: Final[str] = "runs"
_DURATION_PATTERN: Final[re.Pattern[str]] = re.compile(r"^(\d+)(mo|m|h|d|w|s)$")
_TIME_OF_DAY_PATTERN: Final[re.Pattern[str]] = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

SECONDS_PER_MINUTE: Final[int] = 60
SECONDS_PER_HOUR: Final[int] = 3600
SECONDS_PER_DAY: Final[int] = 86400
SECONDS_PER_WEEK: Final[int] = 7 * SECONDS_PER_DAY


class RoutineId(NonEmptyStr):
    """A routine's id: its definition file's stem, safe as a chat name, a log name, and a path segment."""

    def __new__(cls, value: str) -> Self:
        if ROUTINE_ID_PATTERN.fullmatch(value) is None:
            raise InvalidRoutineIdError(
                f"{value!r} is not a routine id: lowercase letters, digits and hyphens, starting with a letter or digit"
            )
        if value == LEDGER_DIRNAME:
            raise InvalidRoutineIdError(f"{value!r} cannot be a routine id: the run ledger lives under that name")
        return super().__new__(cls, value)

    @property
    def as_title(self) -> str:
        """The id read as a title, until someone gives the routine one: ``inbox-digest`` is ``Inbox digest``."""
        return self.replace("-", " ").capitalize()


class RunId(RandomId):
    """Unique identifier for one run of a routine."""

    PREFIX = "run"


class DurationUnit(UpperCaseStrEnum):
    """The units a duration is written in."""

    SECONDS = auto()
    MINUTES = auto()
    HOURS = auto()
    DAYS = auto()
    WEEKS = auto()
    MONTHS = auto()


_UNIT_BY_SUFFIX: Final[dict[str, DurationUnit]] = {
    "s": DurationUnit.SECONDS,
    "m": DurationUnit.MINUTES,
    "h": DurationUnit.HOURS,
    "d": DurationUnit.DAYS,
    "w": DurationUnit.WEEKS,
    "mo": DurationUnit.MONTHS,
}

_SECONDS_BY_UNIT: Final[dict[DurationUnit, int]] = {
    DurationUnit.SECONDS: 1,
    DurationUnit.MINUTES: SECONDS_PER_MINUTE,
    DurationUnit.HOURS: SECONDS_PER_HOUR,
    DurationUnit.DAYS: SECONDS_PER_DAY,
    DurationUnit.WEEKS: SECONDS_PER_WEEK,
}

# A month has no fixed length; this is what a month-long cadence counts as wherever seconds are needed.
APPROXIMATE_SECONDS_PER_MONTH: Final[int] = 30 * SECONDS_PER_DAY


class Duration(NonEmptyStr):
    """A duration as a definition spells it: a whole number and a unit, ``15m``, ``3h``, ``1d``, ``2w`` or ``1mo``."""

    def __new__(cls, value: str) -> Self:
        stripped = value.strip()
        match = _DURATION_PATTERN.fullmatch(stripped)
        if match is None or int(match.group(1)) < 1:
            raise InvalidDurationError(
                f"{value!r} is not a duration: a whole number and a unit, such as 30s, 15m, 3h, 1d, 2w or 1mo"
            )
        return super().__new__(cls, stripped)

    @property
    def count(self) -> int:
        match = _DURATION_PATTERN.fullmatch(self)
        assert match is not None
        return int(match.group(1))

    @property
    def unit(self) -> DurationUnit:
        match = _DURATION_PATTERN.fullmatch(self)
        assert match is not None
        return _UNIT_BY_SUFFIX[match.group(2)]

    @property
    def is_calendar_months(self) -> bool:
        return self.unit is DurationUnit.MONTHS

    @property
    def approximate_seconds(self) -> int:
        """The duration in seconds, a month counted as thirty days."""
        if self.unit is DurationUnit.MONTHS:
            return self.count * APPROXIMATE_SECONDS_PER_MONTH
        return self.count * _SECONDS_BY_UNIT[self.unit]

    @property
    def timedelta(self) -> timedelta:
        """The exact duration; raises for a calendar cadence, which has no fixed length."""
        if self.unit is DurationUnit.MONTHS:
            raise InvalidDurationError("a cadence in months has no fixed length")
        return timedelta(seconds=self.count * _SECONDS_BY_UNIT[self.unit])


class TimeOfDay(NonEmptyStr):
    """A local time of day as a definition spells it, ``HH:MM``."""

    def __new__(cls, value: str) -> Self:
        stripped = value.strip()
        if _TIME_OF_DAY_PATTERN.fullmatch(stripped) is None:
            raise InvalidTimeOfDayError(f"{value!r} is not a time of day: HH:MM on a 24 hour clock")
        return super().__new__(cls, stripped)

    @property
    def hour(self) -> int:
        return int(self.split(":")[0])

    @property
    def minute(self) -> int:
        return int(self.split(":")[1])


class Weekday(UpperCaseStrEnum):
    """A day of the week, in the order ``datetime.weekday`` counts them."""

    MON = auto()
    TUE = auto()
    WED = auto()
    THU = auto()
    FRI = auto()
    SAT = auto()
    SUN = auto()


WEEKDAYS_IN_ORDER: Final[tuple[Weekday, ...]] = tuple(Weekday)


class RunTrigger(UpperCaseStrEnum):
    """What started a run."""

    SCHEDULED = auto()
    MANUAL = auto()


class RunKind(UpperCaseStrEnum):
    """What a run does: execute a process, or wake the routine's chat with a skill."""

    COMMAND = auto()
    SKILL = auto()


class RunOutcome(UpperCaseStrEnum):
    """How a run ended."""

    SUCCEEDED = auto()
    FAILED = auto()
    TIMED_OUT = auto()
    INTERRUPTED = auto()


class RunWarning(UpperCaseStrEnum):
    """Something worth saying about a run that is not its outcome."""

    RESTARTED_PREVIOUS_RUN = auto()


class RoutineHealth(UpperCaseStrEnum):
    """The one word said about a routine, in the order the first that holds wins."""

    BROKEN = auto()
    OFF = auto()
    DONE = auto()
    NEEDS_USER = auto()
    FAILING = auto()
    OVERDUE = auto()
    NOT_RUN_YET = auto()
    WORKING = auto()


class SendVerdict(UpperCaseStrEnum):
    """How the chat app took a message: handed to the agent, or held for the agent the chat is moving to."""

    ACCEPTED = "ACCEPTED"
    HELD = "HELD"


class RestartVerdict(UpperCaseStrEnum):
    """How a restarted chat came back: ready, gone for good, or not within the run's time."""

    IDLE = auto()
    GONE = auto()
    TIMED_OUT = auto()


class ChatStatusWord(UpperCaseStrEnum):
    """A chat's status as the chat app reports it, plus UNKNOWN for a word this library does not know."""

    WORKING = auto()
    BACKGROUND = auto()
    IDLE = auto()
    ATTENTION = auto()
    STOPPED = auto()
    ERROR = auto()
    UNKNOWN = auto()
