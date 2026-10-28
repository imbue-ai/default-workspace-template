from datetime import datetime
from typing import Any
from typing import Final
from typing import Self

from pydantic import ConfigDict
from pydantic import Field
from pydantic import field_validator
from pydantic import model_validator

from imbue.imbue_common.event_envelope import EventEnvelope
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.imbue_common.primitives import NonNegativeInt
from routines.errors import DefinitionValidationError
from routines.primitives import Duration
from routines.primitives import DurationUnit
from routines.primitives import RoutineHealth
from routines.primitives import RoutineId
from routines.primitives import RunId
from routines.primitives import RunKind
from routines.primitives import RunOutcome
from routines.primitives import RunTrigger
from routines.primitives import RunWarning
from routines.primitives import SECONDS_PER_DAY
from routines.primitives import TimeOfDay
from routines.primitives import Weekday

DEFAULT_TIMEOUT: Final[Duration] = Duration("1h")
DEFAULT_RETRY_AFTER: Final[Duration] = Duration("2m")
DEFAULT_NOTIFY_AFTER_FAILURES: Final[int] = 3

RUN_STARTED_EVENT: Final[str] = "run_started"
RUN_BLOCKED_EVENT: Final[str] = "run_blocked"
RUN_FINISHED_EVENT: Final[str] = "run_finished"


def _weekdays_in_any_case(value: Any) -> Any:
    """A ``days`` list as a file or a request spells it, ``mon`` or ``MON``, read as the enum's upper case."""
    if isinstance(value, list | tuple):
        return tuple(item.upper() if isinstance(item, str) else item for item in value)
    return value


def _once_as_a_local_moment(value: Any) -> Any:
    """A ``once`` is a local wall-clock moment, written to the file without a zone; one that carries a zone is
    refused rather than silently re-read as local time shifted by its offset."""
    if isinstance(value, datetime) and value.tzinfo is not None:
        raise DefinitionValidationError("'once' is a local moment written without a zone, as 2026-11-03T15:00")
    return value


class RoutineDefinition(FrozenModel):
    """What a routine is and when it runs: the contents of ``system/routines/<id>.toml``."""

    title: NonEmptyStr = Field(description="What the user sees it called")
    what: NonEmptyStr = Field(description="One sentence saying what it does for the user")
    every: Duration | None = Field(default=None, description="The cadence; None for a one-off")
    at: TimeOfDay | None = Field(default=None, description="The local time of day a run is due")
    days: tuple[Weekday, ...] = Field(default=(), description="The weekdays a run may start; empty for any")
    day_of_month: int | None = Field(default=None, ge=1, le=31, description="The day a monthly run is due")
    once: datetime | None = Field(default=None, description="A single local moment; done after one success")
    command: tuple[str, ...] | None = Field(default=None, description="The argv to run from the repo root")
    skill: NonEmptyStr | None = Field(default=None, description="The skill to run in the routine's chat")
    enabled: bool = Field(default=True, description="The default switch")
    timeout: Duration = Field(default=DEFAULT_TIMEOUT, description="How long a run may take")
    retry_after: Duration = Field(
        default=DEFAULT_RETRY_AFTER,
        description="The gap before a failed run is retried",
    )
    notify_after_failures: NonNegativeInt = Field(
        default=NonNegativeInt(DEFAULT_NOTIFY_AFTER_FAILURES),
        description="Consecutive failures before the feed is told; 0 never",
    )
    app: NonEmptyStr | None = Field(default=None, description="The app this routine belongs to")

    _read_weekdays_in_any_case = field_validator("days", mode="before")(_weekdays_in_any_case)
    _refuse_a_zoned_once = field_validator("once", mode="after")(_once_as_a_local_moment)

    @model_validator(mode="after")
    def _check_one_schedule_and_one_action(self) -> Self:
        if (self.every is None) == (self.once is None):
            raise DefinitionValidationError("a routine needs exactly one of 'every' and 'once'")
        if (self.command is None) == (self.skill is None):
            raise DefinitionValidationError("a routine needs exactly one of 'command' and 'skill'")
        if self.command is not None and len(self.command) == 0:
            raise DefinitionValidationError("'command' needs at least one word")
        if self.once is not None and (self.at is not None or self.days or self.day_of_month is not None):
            raise DefinitionValidationError("'at', 'days' and 'day_of_month' go with 'every', not 'once'")
        if self.every is not None and self.every.unit is DurationUnit.SECONDS:
            raise DefinitionValidationError(
                "'every' is a number of minutes, hours, days, weeks or months (15m, 3h, 1d, 2w, 1mo): "
                "the schedule is checked once a minute"
            )
        if self.at is not None and self.every is not None and self.every.approximate_seconds < SECONDS_PER_DAY:
            raise DefinitionValidationError("'at' only goes with a cadence of a day or longer")
        if self.day_of_month is not None and (self.every is None or self.every.unit is not DurationUnit.MONTHS):
            raise DefinitionValidationError("'day_of_month' only goes with a cadence in months")
        if self.timeout.is_calendar_months or self.retry_after.is_calendar_months:
            raise DefinitionValidationError("'timeout' and 'retry_after' are fixed lengths, not months")
        return self

    @property
    def kind(self) -> RunKind:
        return RunKind.SKILL if self.skill is not None else RunKind.COMMAND


class ScheduleChange(FrozenModel):
    """The schedule fields of a definition, as the app's editor and ``routines edit`` set them together."""

    every: Duration | None = Field(default=None, description="The cadence; None for a one-off")
    at: TimeOfDay | None = Field(default=None, description="The local time of day a run is due")
    days: tuple[Weekday, ...] = Field(default=(), description="The weekdays a run may start")
    day_of_month: int | None = Field(default=None, ge=1, le=31, description="The day a monthly run is due")
    once: datetime | None = Field(default=None, description="A single local moment")

    _read_weekdays_in_any_case = field_validator("days", mode="before")(_weekdays_in_any_case)
    _refuse_a_zoned_once = field_validator("once", mode="after")(_once_as_a_local_moment)


class LoadedRoutine(FrozenModel):
    """One definition file as read: the definition, or why it could not be read."""

    routine_id: RoutineId = Field(description="The file's stem")
    definition: RoutineDefinition | None = Field(description="The definition; None when the file is broken")
    error: str | None = Field(description="Why the file does not validate; None when it does")


class CurrentRun(FrozenModel):
    """The run in flight, as the state records it."""

    model_config = ConfigDict(extra="ignore")

    run_id: RunId = Field(description="The run")
    pid: int = Field(description="The runner process")
    pid_start: str = Field(
        default="",
        description="When that process started, as /proc reports it, so a pid reused after a reboot is not taken "
        "for the runner; empty where /proc has no answer",
    )
    trigger: RunTrigger = Field(description="What started it")
    started_at: datetime = Field(description="When it started")
    is_blocked: bool = Field(default=False, description="Whether a skill run's agent is waiting on the user")
    command_pgid: int = Field(
        default=0,
        description="The process group of a command run's process, so the reaper can stop what a dead runner "
        "left running; 0 before the command starts and for a skill run",
    )


class LastRun(FrozenModel):
    """The most recent finished run, as the state records it."""

    model_config = ConfigDict(extra="ignore")

    run_id: RunId = Field(description="The run")
    trigger: RunTrigger = Field(description="What started it")
    kind: RunKind = Field(description="A command run or a skill run")
    outcome: RunOutcome = Field(description="How it ended")
    summary: str = Field(description="One line about it, possibly empty")
    started_at: datetime = Field(description="When it started")
    finished_at: datetime = Field(description="When it ended")
    warnings: tuple[RunWarning, ...] = Field(default=(), description="What was worth saying beside the outcome")


class RoutineState(FrozenModel):
    """What the runner knows about a routine on this machine: ``data/.state/routines/<id>/state.json``."""

    model_config = ConfigDict(extra="ignore")

    is_enabled: bool | None = Field(
        default=None,
        description="The user's switch; None means the definition's default",
    )
    chat_id: str | None = Field(default=None, description="The chat a skill routine runs in")
    last_started_at: datetime | None = Field(default=None, description="When a run last started")
    last_succeeded_at: datetime | None = Field(default=None, description="When a run last succeeded")
    last_success_started_at: datetime | None = Field(
        default=None,
        description="When the run that last succeeded started: the day an `at` window is counted from, so a run "
        "that crosses midnight claims no second day; None in a state written before it was kept",
    )
    consecutive_failures: NonNegativeInt = Field(default=NonNegativeInt(0), description="Failed runs in a row")
    is_failure_notified: bool = Field(default=False, description="Whether the feed was told of the current streak")
    current_run: CurrentRun | None = Field(default=None, description="The run in flight")
    last_run: LastRun | None = Field(default=None, description="The most recent finished run")


class ResultLink(FrozenModel):
    """A link a run left for the run page."""

    label: NonEmptyStr = Field(description="What the link is called")
    url: NonEmptyStr = Field(description="Where it goes")


class RunResult(FrozenModel):
    """What a run may leave in its directory as ``result.json``."""

    model_config = ConfigDict(extra="ignore")

    summary: str | None = Field(default=None, description="One line about the run")
    links: tuple[ResultLink, ...] = Field(default=(), description="Links for the run page")
    chat_id: str | None = Field(default=None, description="The chat a wake inside the run used")
    outcome: RunOutcome | None = Field(
        default=None,
        description="How the work ended when the command knows better than its exit code (a wake's TIMED_OUT); "
        "read only for a non-zero exit, and never as a success",
    )
    warnings: tuple[RunWarning, ...] = Field(default=(), description="What a wake inside the run had to say")


class RunEvent(EventEnvelope):
    """What every run event carries beside the envelope."""

    routine_id: RoutineId = Field(description="The routine")
    run_id: RunId = Field(description="The run")


class RunStartedEvent(RunEvent):
    """A run began."""

    trigger: RunTrigger = Field(description="What started it")
    kind: RunKind = Field(description="A command run or a skill run")
    command: str = Field(description="The command line, or the skill's slash command")


class RunBlockedEvent(RunEvent):
    """A skill run's agent is waiting on the user."""


class RunFinishedEvent(RunEvent):
    """A run ended."""

    outcome: RunOutcome = Field(description="How it ended")
    exit_code: int | None = Field(description="The process's exit code; None for a skill run or an interrupted one")
    duration_seconds: float | None = Field(description="How long it took; None when nothing saw it end")
    summary: str = Field(description="One line about it, possibly empty")
    chat_id: str | None = Field(description="The chat a skill run used")
    warnings: tuple[RunWarning, ...] = Field(description="What was worth saying beside the outcome")
    output_tail: str = Field(description="The log's last bytes")


class RunRecord(FrozenModel):
    """One run as the app and the command show it: the started and finished events folded together."""

    run_id: RunId = Field(description="The run")
    routine_id: RoutineId = Field(description="The routine")
    trigger: RunTrigger = Field(description="What started it")
    kind: RunKind = Field(description="A command run or a skill run")
    outcome: RunOutcome | None = Field(description="How it ended; None while it is going")
    started_at: datetime = Field(description="When it started")
    finished_at: datetime | None = Field(description="When it ended; None while it is going")
    duration_seconds: float | None = Field(description="How long it took")
    summary: str = Field(description="One line about it, possibly empty")
    chat_id: str | None = Field(description="The chat a skill run used")
    warnings: tuple[RunWarning, ...] = Field(description="What was worth saying beside the outcome")
    is_blocked: bool = Field(description="Whether the run is waiting on the user")
    exit_code: int | None = Field(description="The process's exit code")
    when_text: str = Field(description="When it started, relative to now")
    duration_text: str = Field(description="How long it took, in words")
    outcome_text: str = Field(description="How it ended, in words")


class RoutineRow(FrozenModel):
    """One routine as the list shows it."""

    routine_id: RoutineId = Field(description="The routine")
    title: str = Field(description="What the user sees it called")
    what: str = Field(description="One sentence saying what it does")
    health: RoutineHealth = Field(description="The one word about it")
    health_text: str = Field(description="That word for the user")
    health_detail: str = Field(description="A line under the word, possibly empty")
    is_running: bool = Field(description="Whether a run is in flight")
    is_enabled: bool = Field(description="Whether the switch is on")
    kind: RunKind | None = Field(description="A command routine or a skill routine; None when broken")
    app: str | None = Field(description="The app it belongs to")
    schedule_text: str = Field(description="The schedule in words")
    next_due_at: datetime | None = Field(description="When the next scheduled run is due")
    next_run_text: str = Field(description="The next run in words")
    last_run: RunRecord | None = Field(description="The most recent run")
    last_run_text: str = Field(description="The most recent run in words")
    error: str | None = Field(description="Why the definition is broken")
    skill: str | None = Field(description="The skill a skill routine runs")
    command: tuple[str, ...] = Field(description="The argv a command routine runs; empty for a skill routine")
    chat_id: str | None = Field(description="The chat a skill routine runs in")


class ListingSummary(FrozenModel):
    """The strip above the list."""

    total: int = Field(description="How many routines there are")
    needs_attention: int = Field(description="How many are failing, overdue, broken or waiting on the user")
    running: int = Field(description="How many are running right now")
    next_run_text: str = Field(description="The soonest next run in words")


class RoutineListing(FrozenModel):
    """Every routine, with the summary strip."""

    routines: tuple[RoutineRow, ...] = Field(description="The rows, problems first")
    summary: ListingSummary = Field(description="The strip")
    app_names: dict[str, str] = Field(
        description="The display name of each registered app, by name, for the headings the rows group under"
    )
    generated_at: datetime = Field(description="When the listing was built")


class RoutineDetail(FrozenModel):
    """One routine with its definition and recent runs."""

    row: RoutineRow = Field(description="The routine as the list shows it")
    definition: RoutineDefinition | None = Field(description="The definition; None when broken")
    definition_path: str = Field(description="Where the definition lives")
    runs: tuple[RunRecord, ...] = Field(description="Recent runs, newest first")


class RunDetail(FrozenModel):
    """One run with its log."""

    run: RunRecord = Field(description="The run")
    log: str = Field(description="What the run printed, or its tail")
    is_log_truncated: bool = Field(description="Whether only the tail of the log is here")
    links: tuple[ResultLink, ...] = Field(description="Links the run left")


class WakeOutcome(FrozenModel):
    """How a skill run ended."""

    outcome: RunOutcome = Field(description="How it ended")
    summary: str = Field(description="One line about it")
    chat_id: str | None = Field(description="The chat it ran in; None when none could be made")
    warnings: tuple[RunWarning, ...] = Field(description="What was worth saying beside the outcome")


class TickReport(FrozenModel):
    """What one tick did."""

    started: tuple[RoutineId, ...] = Field(description="The routines whose runs it spawned")
    reaped: tuple[RoutineId, ...] = Field(description="The routines whose dead runs it closed off")
    is_skipped: bool = Field(description="Whether another tick held the lock")


class RunStartReceipt(FrozenModel):
    """What ``routines run`` prints for a detached run."""

    routine_id: RoutineId = Field(description="The routine")
    run_id: RunId = Field(description="The run started")
