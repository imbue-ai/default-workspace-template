"""Converting the cron entries a workspace wrote before routines existed into definitions, once.

CLEANUP: remove this module, its ``migrate-cron`` command and the bootstrap's call to it once every workspace
has booted a release with routines (the migration leaves nothing behind for a second pass to find).
"""

import re
from collections.abc import Mapping
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import ValidationError

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.primitives import NonNegativeInt
from imbue.imbue_common.pure import pure
from routines.data_types import RoutineDefinition
from routines.data_types import RoutineState
from routines.definitions import describe_validation_error
from routines.errors import DefinitionCommitError
from routines.errors import InvalidRoutineIdError
from routines.errors import RoutineNotFoundError
from routines.interfaces import DefinitionStoreInterface
from routines.interfaces import StateStoreInterface
from routines.primitives import Duration
from routines.primitives import RoutineId
from routines.primitives import SECONDS_PER_DAY
from routines.primitives import TimeOfDay
from routines.primitives import WEEKDAYS_IN_ORDER
from routines.primitives import Weekday

LEGACY_CRON_DIR: Final[Path] = Path("data/.state/cron.d")
LEGACY_JOB_STATE_DIR: Final[Path] = Path("data/.state/jobs")
LIVE_CRON_DIR: Final[Path] = Path("/etc/cron.d")
# Where a test points the live cron directory; unset in a workspace.
LIVE_CRON_DIR_ENV: Final[str] = "ROUTINES_LIVE_CRON_DIR"
CARETAKER_ROUTINE_ID: Final[RoutineId] = RoutineId("caretaker")
_RUN_JOB_SUFFIX: Final[str] = "run_job.sh"
_RUN_AUTOMATION_SUFFIX: Final[str] = "run_automation.sh"
_ENV_WRAPPER_SUFFIX: Final[str] = "with_agent_env.sh"
_CARETAKER_CHECK_SUFFIX: Final[str] = "caretaker_check.sh"
_CRON_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]+$")
# Where every pre-routines cron line found the env wrapper, and where it lives now: an entry left in place
# keeps running only if it names the wrapper where it is.
LEGACY_ENV_WRAPPER_PATH: Final[str] = "/home/user/workspace/system/libs/automations/with_agent_env.sh"
ENV_WRAPPER_PATH: Final[str] = "/home/user/workspace/system/libs/routines/with_agent_env.sh"


class ConvertedEntry(FrozenModel):
    """One cron entry as a definition."""

    routine_id: RoutineId = Field(description="The routine it became")
    definition: RoutineDefinition = Field(description="What it says")
    is_caretaker_switch: bool = Field(
        description="Whether it was the Caretaker's entry, which only turns the shipped one on"
    )
    legacy_job_name: str = Field(
        description="The name the retired runner kept the job's state under (the routine id for a plain cron line)"
    )


class SkippedEntry(FrozenModel):
    """One cron entry left where it was."""

    file_name: str = Field(description="The entry file")
    reason: str = Field(description="Why it could not be converted")


class MigrationReport(FrozenModel):
    """What one migration pass did."""

    converted: tuple[RoutineId, ...] = Field(description="The routines written")
    enabled_caretaker: bool = Field(description="Whether the Caretaker's entry turned the shipped routine on")
    skipped: tuple[SkippedEntry, ...] = Field(description="The entries left in place")


@pure
def _strip_redirect(tokens: list[str]) -> list[str]:
    cleaned: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in (">>", ">") and index + 1 < len(tokens):
            index += 2
            continue
        if token.startswith(">") or token.startswith("2>"):
            index += 1
            continue
        cleaned.append(token)
        index += 1
    return cleaned


def _convert_run_job(tokens: list[str], file_name: str) -> ConvertedEntry | SkippedEntry:
    """``<id> --every N [--at H] [--retry-after N] <command...>`` after the runner's name."""
    if not tokens:
        return SkippedEntry(file_name=file_name, reason="the runner line names no job")
    try:
        routine_id = _routine_id_from_name(tokens[0])
    except InvalidRoutineIdError as e:
        return SkippedEntry(file_name=file_name, reason=str(e))
    rest = tokens[1:]
    every: Duration | None = None
    at: TimeOfDay | None = None
    retry_after: Duration | None = None
    index = 0
    try:
        while index + 1 < len(rest) and rest[index].startswith("--"):
            flag, value = rest[index], rest[index + 1]
            if flag == "--every":
                every = Duration(value)
            elif flag == "--at":
                at = TimeOfDay(f"{int(value):02d}:00")
            elif flag == "--retry-after":
                retry_after = Duration(value)
            else:
                return SkippedEntry(file_name=file_name, reason=f"the runner flag {flag} is unknown")
            index += 2
    except ValueError as e:
        return SkippedEntry(file_name=file_name, reason=str(e))
    command = rest[index:]
    if every is None or not command:
        return SkippedEntry(file_name=file_name, reason="the runner line has no cadence or no command")
    if at is not None and every.approximate_seconds < SECONDS_PER_DAY:
        logger.warning("Dropping the due hour of {}: its cadence is shorter than a day", routine_id)
        at = None
    is_caretaker = routine_id == CARETAKER_ROUTINE_ID and any(
        token.endswith(_CARETAKER_CHECK_SUFFIX) for token in command
    )
    skill = _skill_of(command)
    fields: dict[str, object] = {
        "title": routine_id.as_title,
        "what": " ".join(command),
        "every": every,
        "at": at,
    }
    if skill is not None:
        fields["skill"] = skill
    else:
        fields["command"] = tuple(command)
    if retry_after is not None:
        fields["retry_after"] = retry_after
    try:
        definition = RoutineDefinition.model_validate(fields)
    except ValidationError as e:
        return SkippedEntry(file_name=file_name, reason=describe_validation_error(e))
    return ConvertedEntry(
        routine_id=routine_id,
        definition=definition,
        is_caretaker_switch=is_caretaker,
        legacy_job_name=tokens[0],
    )


@pure
def _routine_id_from_name(name: str) -> RoutineId:
    """A job id or an entry file name as a routine id: the retired runner took capitals and underscores."""
    return RoutineId(name.lower().replace("_", "-"))


@pure
def _skill_of(command: list[str]) -> str | None:
    """``bash .../run_automation.sh [--option value ...] <skill>`` names a skill; anything else is a command."""
    for index, token in enumerate(command):
        if not token.endswith(_RUN_AUTOMATION_SUFFIX):
            continue
        rest = command[index + 1 :]
        # The retired script took --template, --type and --agent-name before the skill.
        while len(rest) >= 2 and rest[0].startswith("--"):
            rest = rest[2:]
        return rest[0] if rest and not rest[0].startswith("--") else None
    return None


@pure
def _convert_plain_line(fields: list[str], command: list[str], file_name: str) -> ConvertedEntry | SkippedEntry:
    """The cron shapes a definition can say: every N minutes or hours, daily at a time, weekly, monthly."""
    minute, hour, day_of_month, month, day_of_week = fields
    try:
        routine_id = _routine_id_from_name(file_name)
    except InvalidRoutineIdError as e:
        return SkippedEntry(file_name=file_name, reason=str(e))
    values: dict[str, object] = {
        "title": routine_id.as_title,
        "what": " ".join(command),
        "command": tuple(command),
    }
    if month != "*":
        return SkippedEntry(file_name=file_name, reason="a schedule naming a month has no routine form")
    try:
        schedule = _schedule_of_cron_fields(minute, hour, day_of_month, day_of_week)
    except ValueError as e:
        return SkippedEntry(file_name=file_name, reason=str(e))
    if schedule is None:
        return SkippedEntry(file_name=file_name, reason="the schedule has no routine form")
    values.update(schedule)
    try:
        definition = RoutineDefinition.model_validate(values)
    except ValidationError as e:
        return SkippedEntry(file_name=file_name, reason=describe_validation_error(e))
    return ConvertedEntry(
        routine_id=routine_id, definition=definition, is_caretaker_switch=False, legacy_job_name=str(routine_id)
    )


@pure
def _schedule_of_cron_fields(minute: str, hour: str, day_of_month: str, day_of_week: str) -> dict[str, object] | None:
    """The schedule fields a cron shape says, None for a shape a definition cannot say; a field a duration or a
    time of day refuses (``*/0``, hour 24) raises its ValueError."""
    if minute.startswith("*/") and minute[2:].isdigit() and hour == "*" and day_of_month == "*" and day_of_week == "*":
        return {"every": Duration(f"{int(minute[2:])}m")}
    if (
        hour.startswith("*/")
        and hour[2:].isdigit()
        and minute.isdigit()
        and day_of_month == "*"
        and day_of_week == "*"
    ):
        return {"every": Duration(f"{int(hour[2:])}h")}
    if minute == "*" and hour == "*" and day_of_month == "*" and day_of_week == "*":
        return {"every": Duration("1m")}
    if minute.isdigit() and hour == "*" and day_of_month == "*" and day_of_week == "*":
        return {"every": Duration("1h")}
    if not (minute.isdigit() and hour.isdigit()):
        return None
    at = TimeOfDay(f"{int(hour):02d}:{int(minute):02d}")
    if day_of_month == "*" and day_of_week == "*":
        return {"every": Duration("1d"), "at": at}
    if day_of_month == "*" and (weekdays := _weekdays_of(day_of_week)) is not None:
        return {"every": Duration("1d"), "at": at, "days": weekdays}
    if day_of_month.isdigit() and day_of_week == "*":
        return {"every": Duration("1mo"), "at": at, "day_of_month": int(day_of_month)}
    return None


@pure
def rewrite_env_wrapper(text: str) -> str:
    """The entry's text with the env wrapper named where it lives now, for an entry that stays with cron."""
    return text.replace(LEGACY_ENV_WRAPPER_PATH, ENV_WRAPPER_PATH)


@pure
def _weekdays_of(field: str) -> tuple[Weekday, ...] | None:
    """A cron day-of-week field of digits, lists and ranges (0 and 7 both Sunday) as weekdays; None otherwise."""
    weekdays: list[Weekday] = []
    for part in field.split(","):
        first, dash, last = part.partition("-")
        if not first.isdigit() or (dash and not last.isdigit()):
            return None
        if int(last or first) < int(first):
            return None
        for number in range(int(first), int(last or first) + 1):
            if number > 7:
                return None
            weekday = WEEKDAYS_IN_ORDER[(number + 6) % 7]
            if weekday not in weekdays:
                weekdays.append(weekday)
    return tuple(weekdays) if weekdays else None


@pure
def convert_cron_entry(text: str, file_name: str) -> list[ConvertedEntry | SkippedEntry]:
    """Every job line of one entry file as a definition or a reason it stays."""
    results: list[ConvertedEntry | SkippedEntry] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" in stripped.split()[0]:
            continue
        tokens = stripped.split()
        if len(tokens) < 7:
            results.append(SkippedEntry(file_name=file_name, reason="the line is not a cron job"))
            continue
        fields, command = tokens[:5], _strip_redirect(tokens[6:])
        if command and command[0].endswith(_ENV_WRAPPER_SUFFIX):
            command = command[1:]
        runner_index = next(
            (index for index, token in enumerate(command) if token.endswith(_RUN_JOB_SUFFIX)),
            None,
        )
        if runner_index is not None:
            results.append(_convert_run_job(command[runner_index + 1 :], file_name))
        else:
            results.append(_convert_plain_line(fields, command, file_name))
    return results


def _read_epoch(path: Path) -> datetime | None:
    """The moment a legacy state file holds as epoch seconds; None for no file, or one that cannot be read."""
    if not path.is_file():
        return None
    try:
        return datetime.fromtimestamp(int(path.read_text().strip()), tz=timezone.utc)
    except (OSError, ValueError) as e:
        logger.warning("Ignoring the legacy job state file {}: {}", path, e)
        return None


def _read_count(path: Path) -> int:
    """The count a legacy state file holds; 0 for no file, or one that cannot be read."""
    if not path.is_file():
        return 0
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError) as e:
        logger.warning("Ignoring the legacy job state file {}: {}", path, e)
        return 0


def live_cron_dir_from_environment(environ: Mapping[str, str]) -> Path:
    """The live cron directory: /etc/cron.d, or the one the environment names."""
    named = environ.get(LIVE_CRON_DIR_ENV)
    return Path(named) if named else LIVE_CRON_DIR


def migrate_cron_entries(
    repo_root: Path,
    definitions: DefinitionStoreInterface,
    states: StateStoreInterface,
    live_cron_dir: Path = LIVE_CRON_DIR,
) -> MigrationReport:
    """Convert every entry under data/.state/cron.d, carry the runner's state over, and remove both copies."""
    durable_dir = repo_root / LEGACY_CRON_DIR
    if not durable_dir.is_dir():
        return MigrationReport(converted=(), enabled_caretaker=False, skipped=())
    converted: list[RoutineId] = []
    skipped: list[SkippedEntry] = []
    is_caretaker_enabled = False
    for entry_path in sorted(durable_dir.iterdir()):
        if not entry_path.is_file() or not _CRON_NAME_PATTERN.fullmatch(entry_path.name):
            continue
        try:
            text = entry_path.read_text(encoding="utf-8")
        except OSError as e:
            skipped.append(SkippedEntry(file_name=entry_path.name, reason=str(e)))
            continue
        results = convert_cron_entry(text, entry_path.name)
        failures = [result for result in results if isinstance(result, SkippedEntry)]
        if failures or not results:
            skipped.extend(failures or [SkippedEntry(file_name=entry_path.name, reason="the entry has no job line")])
            for failure in failures:
                logger.warning(
                    "Leaving the cron entry {} in place: {}",
                    entry_path.name,
                    failure.reason,
                )
            _leave_in_place(entry_path, text, live_cron_dir / entry_path.name)
            continue
        entries = [result for result in results if isinstance(result, ConvertedEntry)]
        assert len(entries) == len(results)
        # A file converts whole or not at all: a line whose routine exists (or that two lines of the file would
        # both be) is found before any line is written, so no definition is left beside a cron line that stays.
        collisions = _collisions_of(entries, definitions, entry_path.name)
        if collisions:
            for collision in collisions:
                logger.warning("Leaving the cron entry {} in place: {}", entry_path.name, collision.reason)
            skipped.extend(collisions)
            _leave_in_place(entry_path, text, live_cron_dir / entry_path.name)
            continue
        for result in entries:
            if result.is_caretaker_switch:
                states.update(
                    CARETAKER_ROUTINE_ID,
                    lambda state: state.model_copy_update(to_update(state.field_ref().is_enabled, True)),
                )
                _carry_over_job_state(repo_root, result.legacy_job_name, CARETAKER_ROUTINE_ID, states)
                is_caretaker_enabled = True
                continue
            try:
                definitions.add(result.routine_id, result.definition)
            except DefinitionCommitError as e:
                # The file is written and the routine runs from it; the next write of the file commits both.
                logger.warning("Converted the cron entry {} but could not commit it: {}", entry_path.name, e)
            _carry_over_job_state(repo_root, result.legacy_job_name, result.routine_id, states)
            converted.append(result.routine_id)
        entry_path.unlink()
        live_copy = live_cron_dir / entry_path.name
        try:
            live_copy.unlink(missing_ok=True)
        except OSError as e:
            logger.warning("Could not remove the live cron entry {}: {}", live_copy, e)
    return MigrationReport(
        converted=tuple(converted),
        enabled_caretaker=is_caretaker_enabled,
        skipped=tuple(skipped),
    )


def _collisions_of(
    entries: list[ConvertedEntry], definitions: DefinitionStoreInterface, file_name: str
) -> list[SkippedEntry]:
    """Why the file's lines cannot all become routines: one that exists already, or two lines with one id."""
    collisions: list[SkippedEntry] = []
    seen: set[RoutineId] = set()
    for entry in entries:
        if entry.is_caretaker_switch:
            continue
        if entry.routine_id in seen:
            collisions.append(
                SkippedEntry(
                    file_name=file_name, reason=f"two of its lines would both be the routine {entry.routine_id}"
                )
            )
            continue
        seen.add(entry.routine_id)
        try:
            definitions.load(entry.routine_id)
        except RoutineNotFoundError:
            continue
        collisions.append(SkippedEntry(file_name=file_name, reason=f"the routine {entry.routine_id} exists"))
    return collisions


def _leave_in_place(entry_path: Path, text: str, live_copy: Path) -> None:
    """Keep the entry with cron, naming the env wrapper where it lives now in both copies."""
    rewritten = rewrite_env_wrapper(text)
    if rewritten == text:
        return
    copies = [entry_path, live_copy] if live_copy.is_file() else [entry_path]
    for copy in copies:
        try:
            copy.write_text(rewritten, encoding="utf-8")
        except OSError as e:
            logger.warning("Could not point the cron entry {} at the moved env wrapper: {}", copy, e)
            continue
        logger.info("Pointed the cron entry {} at the moved env wrapper", copy)


def _carry_over_job_state(
    repo_root: Path, legacy_job_name: str, routine_id: RoutineId, states: StateStoreInterface
) -> None:
    job_dir = repo_root / LEGACY_JOB_STATE_DIR / legacy_job_name
    if not job_dir.is_dir():
        return
    last_success = _read_epoch(job_dir / "last_success")
    failures = NonNegativeInt(_read_count(job_dir / "failures"))

    def carry(state: RoutineState) -> RoutineState:
        return state.model_copy_update(
            to_update(state.field_ref().last_succeeded_at, last_success),
            to_update(state.field_ref().consecutive_failures, failures),
        )

    states.update(routine_id, carry)
