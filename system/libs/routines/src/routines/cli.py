"""``routines``: the agent-facing command, and what the tick and the app's buttons run."""

import json
import os
import sys
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import Final
from typing import NoReturn

import click
from pydantic import BaseModel
from pydantic import Field
from pydantic import ValidationError

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.ids import InvalidRandomIdError
from imbue.imbue_common.logging import setup_logging
from imbue.imbue_common.pure import pure
from routines.data_types import CurrentRun
from routines.data_types import RoutineDefinition
from routines.data_types import RoutineRow
from routines.data_types import RunRecord
from routines.data_types import RunResult
from routines.data_types import RunStartReceipt
from routines.data_types import WakeOutcome
from routines.definitions import describe_validation_error
from routines.definitions import once_moment_from_text
from routines.errors import DefinitionValidationError
from routines.errors import RoutineNotFoundError
from routines.errors import RoutinesError
from routines.errors import RunInFlightError
from routines.errors import RunNotFoundError
from routines.migrate import live_cron_dir_from_environment
from routines.migrate import migrate_cron_entries
from routines.paths import RESULT_FILENAME
from routines.paths import paths_from_environment
from routines.primitives import Duration
from routines.primitives import RoutineId
from routines.primitives import RunId
from routines.primitives import RunOutcome
from routines.primitives import RunTrigger
from routines.primitives import Weekday
from routines.runner import ROUTINE_ID_ENV
from routines.runner import RUN_DIR_ENV
from routines.runner import RUN_ID_ENV
from routines.service import DEFAULT_RUN_LIMIT
from routines.service import RoutinesService
from routines.service import build_service

JSON_FORMAT: Final[str] = "json"
HUMAN_FORMAT: Final[str] = "human"
# How long a wake outside any defined routine waits for the agent's turn.
DEFAULT_WAKE_TIMEOUT: Final[str] = "1h"
# A wake inside a run stops this much before the run's own deadline, so it can interrupt the chat and write
# its result before the runner stops the run's process group.
WAKE_MARGIN_BEFORE_RUN_DEADLINE: Final[timedelta] = timedelta(seconds=30)
LEAST_WAKE_WAIT: Final[timedelta] = timedelta(seconds=5)


class WakeDefaults(FrozenModel):
    """What a wake calls the chat and how long it waits, as the flags or the routine's definition say."""

    title: str = Field(description="The chat's name when it has to be made")
    timeout: Duration = Field(description="How long to wait for the agent's turn")


def wake_defaults(
    definition: RoutineDefinition | None, routine_id: RoutineId, title: str | None, timeout: str | None
) -> WakeDefaults:
    """A flag wins; else the routine's definition, so a command routine's wake is recorded as a skill run's is; else
    the id read as a title and the fixed timeout."""
    if title is None:
        title = str(definition.title) if definition is not None else routine_id.as_title
    if timeout is None:
        return WakeDefaults(
            title=title, timeout=definition.timeout if definition is not None else Duration(DEFAULT_WAKE_TIMEOUT)
        )
    return WakeDefaults(title=title, timeout=Duration(timeout))


def wait_within_run(
    current: CurrentRun | None, run_id: RunId, run_timeout: timedelta, wait: timedelta, now: datetime
) -> timedelta:
    """How long a wake inside the run may wait: the wanted wait, capped at what is left of the run's own time
    less the margin, so the runner's deadline never cuts the wake off first; the whole wait when the state
    does not name the run."""
    if current is None or current.run_id != run_id:
        return wait
    remaining = current.started_at + run_timeout - now - WAKE_MARGIN_BEFORE_RUN_DEADLINE
    return max(min(wait, remaining), LEAST_WAKE_WAIT)


@pure
def wake_run_result(outcome: WakeOutcome, wanted_id: RoutineId, run_routine_id: str) -> RunResult:
    """What the wake leaves for the run it happened inside: the agent's summary, how the wake ended and what it
    had to say (so a timed-out wake is a timed-out run), and the chat only when the wake was of the run's own
    routine, since the runner records the result's chat on that routine."""
    is_own_chat = str(wanted_id) == run_routine_id
    return RunResult(
        summary=outcome.summary,
        chat_id=outcome.chat_id if is_own_chat else None,
        outcome=outcome.outcome,
        warnings=outcome.warnings,
    )


@pure
def run_routine_of(run_id: RunId | None, environ: Mapping[str, str], wanted_id: RoutineId) -> RoutineId | None:
    """The routine of the run a wake happens inside: the one the run's environment names, else the wanted
    routine itself; None outside any run."""
    if run_id is None:
        return None
    named = environ.get(ROUTINE_ID_ENV)
    return RoutineId(named) if named else wanted_id


def _definition_if_any(service: RoutinesService, routine_id: RoutineId) -> RoutineDefinition | None:
    """The routine's definition when it has a valid one; None for a wake of a chat no routine defines."""
    try:
        return service.definitions.load(routine_id).definition
    except RoutineNotFoundError:
        return None


_FORMAT_OPTION = click.option(
    "--format",
    "output_format",
    type=click.Choice([HUMAN_FORMAT, JSON_FORMAT]),
    default=HUMAN_FORMAT,
    show_default=True,
    help="Lines for a reader, or JSON for a script",
)


def _service() -> RoutinesService:
    return build_service(paths_from_environment(), dict(os.environ))


def _run_id_of(text: str) -> RunId:
    """The run id a user or the environment named; a string that is not one is a RunNotFoundError, in the
    library's words, rather than the id type's own error."""
    try:
        return RunId(text)
    except InvalidRandomIdError as e:
        raise RunNotFoundError(text) from e


def _print_json(model: BaseModel | Sequence[BaseModel]) -> None:
    if isinstance(model, BaseModel):
        click.echo(model.model_dump_json(indent=2))
    else:
        click.echo(json.dumps([item.model_dump(mode="json") for item in model], indent=2))


def _row_line(row: RoutineRow) -> str:
    next_text = f"  next: {row.next_run_text}" if row.next_run_text else ""
    return f"{row.routine_id:28} {row.health_text:12} {row.schedule_text:32} last: {row.last_run_text}{next_text}"


def _run_line(run: RunRecord) -> str:
    summary = f"  {run.summary}" if run.summary else ""
    return f"{run.run_id}  {run.started_at:%Y-%m-%d %H:%M}  {run.trigger.lower():9} {run.outcome_text:14} {run.duration_text}{summary}"


def _fail(message: str) -> NoReturn:
    click.echo(f"error: {message}", err=True)
    sys.exit(1)


@click.group()
def routines() -> None:
    """Everything in this workspace that runs on a schedule."""


@routines.command("list")
@_FORMAT_OPTION
def list_command(output_format: str) -> None:
    """Every routine with its health, schedule, next run and last run."""
    listing = _service().listing()
    if output_format == JSON_FORMAT:
        _print_json(listing)
        return
    if not listing.routines:
        click.echo("No routines yet. `routines add` makes one.")
        return
    for row in listing.routines:
        click.echo(_row_line(row))
    click.echo(f"{listing.summary.total} routines, {listing.summary.needs_attention} need attention")


@routines.command("show")
@click.argument("routine_id")
@_FORMAT_OPTION
def show_command(routine_id: str, output_format: str) -> None:
    """One routine with its recent runs."""
    try:
        detail = _service().detail(RoutineId(routine_id))
    except RoutinesError as e:
        _fail(str(e))
    if output_format == JSON_FORMAT:
        _print_json(detail)
        return
    row = detail.row
    click.echo(f"{row.title} ({row.routine_id})")
    click.echo(f"  {row.what}")
    click.echo(f"  schedule: {row.schedule_text}" + (f"  next: {row.next_run_text}" if row.next_run_text else ""))
    click.echo(f"  health:   {row.health_text}" + (f" ({row.health_detail})" if row.health_detail else ""))
    click.echo(f"  last run: {row.last_run_text}")
    if row.chat_id:
        click.echo(f"  chat:     {row.chat_id}")
    click.echo(f"  file:     {detail.definition_path}")
    if detail.runs:
        click.echo("  runs:")
        for run in detail.runs:
            click.echo(f"    {_run_line(run)}")


def _definition_options(command: Callable[..., None]) -> Callable[..., None]:
    options = [
        click.option("--title", default=None, help="What the user sees it called"),
        click.option("--what", default=None, help="One sentence saying what it does"),
        click.option("--every", default=None, help="The cadence: 15m, 3h, 1d, 2w, 1mo"),
        click.option("--once", default=None, help="A single local moment, YYYY-MM-DDTHH:MM"),
        click.option("--at", "at_time", default=None, help="The local time of day, HH:MM; '' clears it on edit"),
        click.option(
            "--days",
            default=None,
            help="Weekdays a run may start, comma separated: mon,wed,fri; '' clears them on edit",
        ),
        click.option(
            "--day-of-month",
            type=int,
            default=None,
            help="The day a monthly run is due",
        ),
        click.option("--skill", default=None, help="The skill to run in the routine's chat"),
        click.option("--app", "app_name", default=None, help="The app this routine belongs to; '' clears it on edit"),
        click.option("--timeout", default=None, help="How long a run may take"),
        click.option("--retry-after", default=None, help="The gap before a failed run is retried"),
        click.option(
            "--notify-after-failures",
            type=int,
            default=None,
            help="Failures in a row before the feed is told",
        ),
        click.option("--disabled", is_flag=True, default=False, help="Start switched off"),
        click.argument("command", nargs=-1),
    ]
    for option in reversed(options):
        command = option(command)
    return command


def _parse_days(text: str | None) -> tuple[Weekday, ...] | None:
    """The ``--days`` flag as weekdays; None when the flag was not given, () to clear them."""
    if text is None:
        return None
    if not text.strip():
        return ()
    try:
        return tuple(Weekday(part.strip().upper()) for part in text.split(","))
    except ValueError as e:
        raise DefinitionValidationError(f"days: {text!r} is not a comma separated list of mon to sun") from e


class DefinitionOptions(FrozenModel):
    """The definition flags of ``routines add`` and ``routines edit``, as click hands them over."""

    title: str | None = Field(description="What the user sees it called")
    what: str | None = Field(description="One sentence saying what it does")
    every: str | None = Field(description="The cadence")
    once: str | None = Field(description="A single local moment")
    at_time: str | None = Field(description="The local time of day")
    days: str | None = Field(description="Weekdays, comma separated")
    day_of_month: int | None = Field(description="The day a monthly run is due")
    skill: str | None = Field(description="The skill to run")
    command: tuple[str, ...] = Field(description="The command to run")
    app_name: str | None = Field(description="The app it belongs to")
    timeout: str | None = Field(description="How long a run may take")
    retry_after: str | None = Field(description="The gap before a retry")
    notify_after_failures: int | None = Field(description="Failures before the feed is told")
    disabled: bool = Field(description="Whether to start switched off")


def _definition_from_options(base: RoutineDefinition | None, options: DefinitionOptions) -> RoutineDefinition:
    """A definition from the flags, over the existing one for an edit; raises ValidationError with the problem, or
    DefinitionValidationError for both members of an exclusive pair."""
    if options.every is not None and options.once is not None:
        raise DefinitionValidationError("A routine runs --every so often or --once at a moment, not both.")
    if options.skill is not None and options.command:
        raise DefinitionValidationError("A routine runs a --skill or a command after --, not both.")
    values: dict[str, object] = base.model_dump() if base is not None else {}
    if options.title is not None:
        values["title"] = options.title
    if options.what is not None:
        values["what"] = options.what
    if options.every is not None:
        values["every"] = options.every
        values["once"] = None
    if options.once is not None:
        values["once"] = once_moment_from_text(options.once)
        values["every"] = None
        values["at"] = None
        values["days"] = ()
        values["day_of_month"] = None
    if options.at_time is not None:
        values["at"] = options.at_time or None
    parsed_days = _parse_days(options.days)
    if parsed_days is not None:
        values["days"] = parsed_days
    if options.day_of_month is not None:
        values["day_of_month"] = options.day_of_month
    if options.skill is not None:
        values["skill"] = options.skill
        values["command"] = None
    if options.command:
        values["command"] = tuple(options.command)
        values["skill"] = None
    if options.app_name is not None:
        values["app"] = options.app_name or None
    if options.timeout is not None:
        values["timeout"] = options.timeout
    if options.retry_after is not None:
        values["retry_after"] = options.retry_after
    if options.notify_after_failures is not None:
        values["notify_after_failures"] = options.notify_after_failures
    if options.disabled:
        values["enabled"] = False
    return RoutineDefinition.model_validate(values)


@routines.command("add")
@click.argument("routine_id")
@_definition_options
def add_command(routine_id: str, **options: object) -> None:
    """Write a new routine's definition. The command to run goes after `--`, or pass --skill."""
    try:
        definition = _definition_from_options(None, DefinitionOptions.model_validate(options))
        path = _service().add(RoutineId(routine_id), definition)
    except ValidationError as e:
        _fail(describe_validation_error(e))
    except RoutinesError as e:
        _fail(str(e))
    click.echo(f"Added {routine_id} at {path}")


@routines.command("edit")
@click.argument("routine_id")
@_definition_options
def edit_command(routine_id: str, **options: object) -> None:
    """Rewrite the named fields of a routine, keeping the rest."""
    service = _service()
    try:
        loaded = service.definitions.load(RoutineId(routine_id))
        base = loaded.definition
        definition = _definition_from_options(base, DefinitionOptions.model_validate(options))
        path = service.replace(RoutineId(routine_id), definition, "edit")
    except ValidationError as e:
        _fail(describe_validation_error(e))
    except RoutinesError as e:
        _fail(str(e))
    click.echo(f"Updated {routine_id} at {path}")


@routines.command("enable")
@click.argument("routine_id")
def enable_command(routine_id: str) -> None:
    """Turn a routine on."""
    try:
        _service().set_enabled(RoutineId(routine_id), True)
    except RoutinesError as e:
        _fail(str(e))
    click.echo(f"Turned {routine_id} on")


@routines.command("disable")
@click.argument("routine_id")
def disable_command(routine_id: str) -> None:
    """Turn a routine off; nothing runs until it is turned on again."""
    try:
        _service().set_enabled(RoutineId(routine_id), False)
    except RoutinesError as e:
        _fail(str(e))
    click.echo(f"Turned {routine_id} off")


@routines.command("run")
@click.argument("routine_id")
@click.option(
    "--wait",
    "is_waited",
    is_flag=True,
    default=False,
    help="Run in this process and report the outcome",
)
@click.option(
    "--trigger",
    type=click.Choice([trigger.lower() for trigger in RunTrigger]),
    default=RunTrigger.MANUAL.lower(),
    hidden=True,
    help="What started the run; 'scheduled' is the tick's spelling and nobody else's",
)
@click.option(
    "--run-id",
    default=None,
    hidden=True,
    help="The run id the tick or the app minted for this run, so its receipt names what they started",
)
def run_command(routine_id: str, is_waited: bool, trigger: str, run_id: str | None) -> None:
    """Run a routine now. Without --wait the run goes on in the background and its id is printed."""
    service = _service()
    run_trigger = RunTrigger(trigger.upper())
    if not is_waited and run_trigger is RunTrigger.MANUAL and run_id is None:
        try:
            receipt = service.start_run_detached(RoutineId(routine_id))
        except RoutinesError as e:
            _fail(str(e))
        click.echo(receipt.model_dump_json())
        return
    try:
        event = service.runner.run(RoutineId(routine_id), run_trigger, _run_id_of(run_id) if run_id else None)
    except RunInFlightError as e:
        # The tick found the lock free a moment ago; a run that started since is the one that counts.
        if run_trigger is RunTrigger.SCHEDULED:
            return
        _fail(str(e))
    except RoutinesError as e:
        _fail(str(e))
    if run_id is not None:
        # A spawned run's stdout is its own log, which already has what the run wrote.
        return
    click.echo(RunStartReceipt(routine_id=RoutineId(routine_id), run_id=event.run_id).model_dump_json())
    click.echo(
        f"{event.outcome.lower()}: {event.summary}" if event.summary else event.outcome.lower(),
        err=True,
    )
    sys.exit(0 if event.outcome is RunOutcome.SUCCEEDED else 1)


@routines.command("runs")
@click.argument("routine_id")
@click.option("--limit", type=int, default=DEFAULT_RUN_LIMIT, show_default=True, help="How many recent runs")
@_FORMAT_OPTION
def runs_command(routine_id: str, limit: int, output_format: str) -> None:
    """A routine's recent runs, newest first."""
    try:
        records = _service().runs(RoutineId(routine_id), limit)
    except RoutinesError as e:
        _fail(str(e))
    if output_format == JSON_FORMAT:
        _print_json(records)
        return
    for run in records:
        click.echo(_run_line(run))


@routines.command("log")
@click.argument("routine_id")
@click.argument("run_id", required=False)
def log_command(routine_id: str, run_id: str | None) -> None:
    """A run's log, the latest run's by default."""
    service = _service()
    try:
        service.definitions.load(RoutineId(routine_id))
        if run_id:
            wanted = _run_id_of(run_id)
            if not service.run_exists(RoutineId(routine_id), wanted):
                raise RunNotFoundError(run_id)
        else:
            wanted = service.last_run_id(RoutineId(routine_id))
        if wanted is None:
            click.echo("No runs yet.")
            return
        text, is_truncated = service.log_tail(RoutineId(routine_id), wanted)
    except RoutinesError as e:
        _fail(str(e))
    if is_truncated:
        click.echo("... (earlier output left out)")
    click.echo(text, nl=False)


@routines.command("remove")
@click.argument("routine_id")
def remove_command(routine_id: str) -> None:
    """Delete a routine's definition and state; its runs stay in the ledger and its chat stays in the list."""
    try:
        _service().remove(RoutineId(routine_id))
    except RoutinesError as e:
        _fail(str(e))
    click.echo(f"Removed {routine_id}")


@routines.command("wake")
@click.argument("skill")
@click.option(
    "--routine",
    "routine_id",
    default=None,
    help="The routine whose chat to use; inside a run the routine being run, else the skill's name",
)
@click.option(
    "--title",
    default=None,
    help="What the chat is called when it has to be made; the routine's title, else the id read as one",
)
@click.option(
    "--timeout",
    default=None,
    help=f"How long to wait for the agent's turn; the routine's timeout, else {DEFAULT_WAKE_TIMEOUT}",
)
def wake_command(skill: str, routine_id: str | None, title: str | None, timeout: str | None) -> None:
    """Wake the routine's chat with /<skill> and wait for the turn; inside a run, the chat and a waiting agent are
    recorded on the run and the agent's summary becomes the run's."""
    service = _service()
    raw_run_id = os.environ.get(RUN_ID_ENV, "")
    try:
        wanted_id = RoutineId(routine_id or os.environ.get(ROUTINE_ID_ENV) or skill)
        run_id = _run_id_of(raw_run_id) if raw_run_id else None
        definition = _definition_if_any(service, wanted_id)
        defaults = wake_defaults(definition, wanted_id, title, timeout)
        wait = defaults.timeout.timedelta
        run_routine_id = run_routine_of(run_id, os.environ, wanted_id)
        if run_id is not None and run_routine_id is not None:
            # Inside a run the wait never reaches past the run's own time, whatever the flag says.
            run_definition = _definition_if_any(service, run_routine_id)
            run_timeout = run_definition.timeout.timedelta if run_definition is not None else wait
            wait = wait_within_run(
                service.states.read(run_routine_id).current_run, run_id, run_timeout, wait, service.clock.now()
            )
        outcome = service.runner.wake(wanted_id, run_id, run_routine_id, defaults.title, skill, wait)
    except RoutinesError as e:
        _fail(str(e))
    run_dir = os.environ.get(RUN_DIR_ENV, "")
    if run_dir:
        result = wake_run_result(outcome, wanted_id, os.environ.get(ROUTINE_ID_ENV, ""))
        (Path(run_dir) / RESULT_FILENAME).write_text(result.model_dump_json(), encoding="utf-8")
    click.echo(f"{outcome.outcome.lower()}: {outcome.summary}")
    sys.exit(0 if outcome.outcome is RunOutcome.SUCCEEDED else 1)


@routines.command("tick")
def tick_command() -> None:
    """Start every run that is due; what cron runs once a minute."""
    report = _service().runner.tick()
    for started in report.started:
        click.echo(f"started {started}")
    for reaped in report.reaped:
        click.echo(f"closed off the dead run of {reaped}")


@routines.command("migrate-cron")
def migrate_cron_command() -> None:
    """Convert the cron entries written before routines existed into definitions; a no-op once there are none."""
    service = _service()
    try:
        report = migrate_cron_entries(
            service.paths.repo_root,
            service.definitions,
            service.states,
            live_cron_dir=live_cron_dir_from_environment(os.environ),
        )
    except RoutinesError as e:
        _fail(str(e))
    for routine_id in report.converted:
        click.echo(f"converted {routine_id}")
    if report.enabled_caretaker:
        click.echo("turned the caretaker on")
    for skipped in report.skipped:
        click.echo(f"left {skipped.file_name} in place: {skipped.reason}")


def main() -> None:
    # Configured here, in the process entry point, and never in the group: a test driving the group through
    # click's runner would otherwise leave the log sink on the runner's since-closed stream.
    setup_logging(level=os.environ.get("ROUTINES_LOG_LEVEL", "INFO"))
    routines()


if __name__ == "__main__":
    main()
