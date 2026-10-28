"""Running a test selection: each command in order, its output kept, and how it ended.

A command passed, or failed; a pytest run that failed reruns only its failed tests once, and is
flaky when they pass that time, unless it also missed its coverage floor. A command killed by a signal while the memory guard recorded a
shed is a shed, not a failure: rerunning it is a judgment about memory the caller makes.
"""

import re
import shlex
import signal
import subprocess
import threading
from collections.abc import Callable
from collections.abc import Sequence
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from enum import auto
from functools import cached_property
from pathlib import Path
from typing import Final
from typing import assert_never

from loguru import logger
from oom_priority.ledger import RECORD_TYPE_PROCESS_SHED
from oom_priority.ledger import read_records
from pydantic import Field
from pydantic import ValidationError
from pydantic import computed_field

from app_manifest.selection import SuiteCommand
from app_manifest.selection import SuiteSelection
from app_manifest.selection import render_command_line
from app_manifest.selection import rerun_command
from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure

# Exit statuses of a command killed by SIGKILL or SIGTERM: negative when it was the child itself,
# 128 plus the signal when a wrapper (uv run) reported its child's death.
_SIGNAL_EXIT_CODES: Final[frozenset[int]] = frozenset(
    {
        -signal.SIGKILL,
        -signal.SIGTERM,
        128 + signal.SIGKILL,
        128 + signal.SIGTERM,
    }
)
_PYTEST_TESTS_FAILED_EXIT_CODE: Final[int] = 1
_SHORT_SUMMARY_HEADER: Final[str] = "short test summary info"
_FAILURE_LINE_PREFIXES: Final[tuple[str, ...]] = ("FAILED ", "ERROR ")
_FAILURE_MESSAGE_SEPARATOR: Final[str] = " - "
# What pytest-cov prints when the total is under fail_under, which also makes pytest exit 1.
_COVERAGE_FLOOR_MISSED_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^FAIL Required test coverage of .* not reached\.", re.MULTILINE
)
# A whole suite under gVisor runs for tens of minutes; this only bounds a hung one.
_COMMAND_TIMEOUT_SECONDS: Final[float] = 3 * 60 * 60
_TERMINATE_GRACE_SECONDS: Final[float] = 30
# earlyoom records a kill from a hook it runs once the victim is gone, so the record of a command
# it killed lands just after the command exits.
_SHED_RECORD_GRACE: Final[timedelta] = timedelta(seconds=5)
_SHED_RECORD_POLL_SECONDS: Final[float] = 0.1
_MAX_TEST_IDS_SHOWN: Final[int] = 10
SUMMARY_FILE_NAME: Final[str] = "summary.json"


class CommandOutcome(LowerCaseStrEnum):
    """How a command of the run ended."""

    PASSED = auto()
    FLAKY = auto()
    FAILED = auto()
    SHED = auto()


class ShedRecord(FrozenModel):
    """A process the memory guard killed while a command ran."""

    timestamp: datetime = Field(description="When the guard killed it")
    pid: int = Field(description="The killed process")
    comm: str | None = Field(description="Its command name, when the guard recorded one")


class CommandRun(FrozenModel):
    """One command of the run and how it ended."""

    command: SuiteCommand = Field(description="The command the selection called for")
    outcome: CommandOutcome = Field(description="How it ended")
    exit_code: int | None = Field(description="Its exit status; None when it timed out")
    started_at: datetime = Field(description="When it started")
    finished_at: datetime = Field(description="When it, and the rerun of its failed tests, finished")
    log_path: Path = Field(description="Its output, stdout and stderr together")
    failed_test_ids: tuple[str, ...] = Field(
        description="The tests that failed: after the rerun for a failed run, the ones that passed on it for a flaky one"
    )
    rerun_log_path: Path | None = Field(description="The rerun's output, when its failed tests were rerun")
    rerun: SuiteCommand | None = Field(
        description="The command that reruns the tests in failed_test_ids on their own, for a pytest run that had any"
    )
    shed_records: tuple[ShedRecord, ...] = Field(
        description="What the memory guard killed while it ran, which may surface as test errors"
    )
    is_coverage_floor_missed: bool = Field(
        description="Whether its run reported its suite's coverage floor not reached, a failure whatever the rerun did"
    )


class SuiteRunReport(FrozenModel):
    """Every command of a run, in the order they ran."""

    runs: tuple[CommandRun, ...] = Field(description="Each command and how it ended")
    notes: tuple[str, ...] = Field(description="The selection's notes")

    @computed_field
    @cached_property
    def is_passing(self) -> bool:
        return all(run.outcome in (CommandOutcome.PASSED, CommandOutcome.FLAKY) for run in self.runs)


class _ProcessResult(FrozenModel):
    """How one process ran."""

    exit_code: int | None = Field(description="Its exit status; None when it timed out")
    started_at: datetime = Field(description="When it started")
    finished_at: datetime = Field(description="When it exited")


@pure
def _summary_line_test_id(entry: str) -> str:
    """The node id at the start of a short-summary entry, ``<node id> - <message>`` or the id
    alone; a parameter in brackets may itself hold the separator."""
    separator_index = entry.find(_FAILURE_MESSAGE_SEPARATOR)
    bracket_index = entry.find("[")
    if bracket_index != -1 and (separator_index == -1 or bracket_index < separator_index):
        parameter_end = entry.find("]" + _FAILURE_MESSAGE_SEPARATOR, bracket_index)
        if parameter_end != -1:
            return entry[: parameter_end + 1].strip()
        if entry.rstrip().endswith("]"):
            return entry.strip()
    return entry.split(_FAILURE_MESSAGE_SEPARATOR, 1)[0].strip()


@pure
def parse_failed_test_ids(pytest_output: str) -> tuple[str, ...]:
    """The node ids pytest's short test summary lists as failed or errored, in order."""
    lines = pytest_output.splitlines()
    header_indexes = [index for index, line in enumerate(lines) if _SHORT_SUMMARY_HEADER in line]
    if not header_indexes:
        return ()
    test_ids: list[str] = []
    for line in lines[header_indexes[-1] + 1 :]:
        prefix = next((prefix for prefix in _FAILURE_LINE_PREFIXES if line.startswith(prefix)), None)
        if prefix is None:
            continue
        test_id = _summary_line_test_id(line[len(prefix) :])
        if test_id and test_id not in test_ids:
            test_ids.append(test_id)
    return tuple(test_ids)


def _run_to_log(argv: Sequence[str], working_directory: Path, log_path: Path) -> _ProcessResult:
    started_at = datetime.now(timezone.utc)
    with log_path.open("w") as log_file:
        process = subprocess.Popen(argv, cwd=working_directory, stdout=log_file, stderr=subprocess.STDOUT)
        try:
            exit_code: int | None = process.wait(timeout=_COMMAND_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            logger.warning("Stopping {} after {}s", shlex.join(argv), _COMMAND_TIMEOUT_SECONDS)
            exit_code = None
            _stop(process)
    return _ProcessResult(exit_code=exit_code, started_at=started_at, finished_at=datetime.now(timezone.utc))


def _stop(process: subprocess.Popen[bytes]) -> None:
    # uv run passes SIGTERM on to the suite it runs, which then gets to clean up.
    process.terminate()
    try:
        process.wait(timeout=_TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def read_shed_records(shed_ledger_path: Path, started_at: datetime, finished_at: datetime) -> tuple[ShedRecord, ...]:
    """The memory guard's kills between the two moments."""
    records: list[ShedRecord] = []
    for record in read_records(shed_ledger_path):
        if record.get("type") != RECORD_TYPE_PROCESS_SHED:
            continue
        try:
            shed = ShedRecord.model_validate(
                {"timestamp": record.get("timestamp"), "pid": record.get("pid"), "comm": record.get("comm")}
            )
        except ValidationError as e:
            logger.warning("Skipping an unreadable shed record {}: {}", record, e)
            continue
        if started_at <= shed.timestamp <= finished_at:
            records.append(shed)
    return tuple(records)


def _await_shed_records(
    shed_ledger_path: Path, started_at: datetime, finished_at: datetime, is_killed_by_signal: bool
) -> tuple[ShedRecord, ...]:
    """The memory guard's kills while a command ran, waiting a little for the record of a kill
    that ended it."""
    if not is_killed_by_signal:
        return read_shed_records(shed_ledger_path, started_at, finished_at)
    deadline = finished_at + _SHED_RECORD_GRACE
    records = read_shed_records(shed_ledger_path, started_at, deadline)
    while not records and datetime.now(timezone.utc) < deadline:
        threading.Event().wait(_SHED_RECORD_POLL_SECONDS)
        records = read_shed_records(shed_ledger_path, started_at, deadline)
    return records


def _run_command(
    command: SuiteCommand,
    repo_root: Path,
    log_path: Path,
    rerun_log_path: Path,
    shed_ledger_path: Path,
    is_coverage_measured: bool,
) -> CommandRun:
    working_directory = repo_root / command.working_directory
    result = _run_to_log(command.argv, working_directory, log_path)
    output = log_path.read_text(errors="replace") if result.exit_code == _PYTEST_TESTS_FAILED_EXIT_CODE else ""
    failed_test_ids = parse_failed_test_ids(output)
    is_coverage_floor_missed = _COVERAGE_FLOOR_MISSED_PATTERN.search(output) is not None
    rerun = rerun_command(command, failed_test_ids, is_coverage_measured) if failed_test_ids else None
    rerun_result = _run_to_log(rerun.argv, working_directory, rerun_log_path) if rerun is not None else None
    last_result = rerun_result if rerun_result is not None else result
    finished_at = last_result.finished_at
    shed_records = _await_shed_records(
        shed_ledger_path, result.started_at, finished_at, last_result.exit_code in _SIGNAL_EXIT_CODES
    )
    outcome, final_failed_test_ids = _classify(
        result.exit_code, failed_test_ids, rerun_result, rerun_log_path, shed_records, is_coverage_floor_missed
    )
    return CommandRun(
        command=command,
        outcome=outcome,
        exit_code=result.exit_code,
        started_at=result.started_at,
        finished_at=finished_at,
        log_path=log_path,
        failed_test_ids=final_failed_test_ids,
        rerun_log_path=rerun_log_path if rerun_result is not None else None,
        rerun=rerun_command(command, final_failed_test_ids, is_coverage_measured) if final_failed_test_ids else None,
        shed_records=shed_records,
        is_coverage_floor_missed=is_coverage_floor_missed,
    )


def _classify(
    exit_code: int | None,
    failed_test_ids: tuple[str, ...],
    rerun_result: _ProcessResult | None,
    rerun_log_path: Path,
    shed_records: tuple[ShedRecord, ...],
    is_coverage_floor_missed: bool,
) -> tuple[CommandOutcome, tuple[str, ...]]:
    if exit_code == 0:
        return CommandOutcome.PASSED, ()
    elif exit_code in _SIGNAL_EXIT_CODES and shed_records:
        return CommandOutcome.SHED, ()
    elif rerun_result is None:
        return CommandOutcome.FAILED, failed_test_ids
    elif rerun_result.exit_code == 0:
        # Its failed tests pass now, so what still fails it is the coverage floor alone.
        return (CommandOutcome.FAILED, ()) if is_coverage_floor_missed else (CommandOutcome.FLAKY, failed_test_ids)
    elif rerun_result.exit_code in _SIGNAL_EXIT_CODES and shed_records:
        return CommandOutcome.SHED, failed_test_ids
    else:
        still_failing = parse_failed_test_ids(rerun_log_path.read_text(errors="replace"))
        return CommandOutcome.FAILED, still_failing or failed_test_ids


def write_report(report: SuiteRunReport, output_directory: Path) -> Path:
    summary_path = output_directory / SUMMARY_FILE_NAME
    summary_path.write_text(report.model_dump_json(indent=2) + "\n")
    return summary_path


def run_selection(
    selection: SuiteSelection,
    repo_root: Path,
    output_directory: Path,
    shed_ledger_path: Path,
    on_command_started: Callable[[int, SuiteCommand], None],
) -> SuiteRunReport:
    """Run every command of ``selection`` in order from ``repo_root``, keeping each one's output
    under ``output_directory`` and writing the summary there before the first and after each."""
    output_directory.mkdir(parents=True, exist_ok=True)
    notes = (
        selection.notes
        if shed_ledger_path.is_file()
        else (
            *selection.notes,
            f"no shed ledger at {shed_ledger_path}, so a command the memory guard killed reads as failed",
        )
    )
    report = SuiteRunReport(runs=(), notes=notes)
    write_report(report, output_directory)
    for index, command in enumerate(selection.commands, start=1):
        on_command_started(index, command)
        stem = f"{index:02d}-{command.kind}"
        run = _run_command(
            command,
            repo_root,
            output_directory / f"{stem}.log",
            output_directory / f"{stem}-rerun.log",
            shed_ledger_path,
            command.working_directory in selection.coverage_measured_roots,
        )
        report = SuiteRunReport(runs=(*report.runs, run), notes=notes)
        write_report(report, output_directory)
    return report


@pure
def _render_test_ids(label: str, test_ids: Sequence[str]) -> list[str]:
    shown = [f"    {label}: {test_id}" for test_id in test_ids[:_MAX_TEST_IDS_SHOWN]]
    remaining = len(test_ids) - len(shown)
    return shown + ([f"    and {remaining} more"] if remaining > 0 else [])


@pure
def render_report(report: SuiteRunReport, summary_path: Path) -> str:
    """Each command's outcome and output file, the tests behind a failure or a flake, and the
    totals."""
    lines: list[str] = []
    for run in report.runs:
        lines.append(f"{run.outcome}: {render_command_line(run.command)}")
        lines.append(f"    output: {run.log_path}")
        if run.rerun_log_path is not None:
            lines.append(f"    rerun output: {run.rerun_log_path}")
        if run.exit_code is None:
            lines.append(f"    timed out after {_COMMAND_TIMEOUT_SECONDS / 3600:g} hours")
        if run.is_coverage_floor_missed:
            lines.append("    coverage floor not reached")
        match run.outcome:
            case CommandOutcome.FAILED:
                lines.extend(_render_test_ids("failed", run.failed_test_ids))
            case CommandOutcome.FLAKY:
                lines.extend(_render_test_ids("passed on rerun", run.failed_test_ids))
            case CommandOutcome.PASSED | CommandOutcome.SHED:
                pass
            case _ as unreachable:
                assert_never(unreachable)
        if run.rerun is not None:
            lines.append(f"    rerun these: {render_command_line(run.rerun)}")
        lines.extend(
            f"    memory guard killed {record.comm or 'a process'} (pid {record.pid}) at {record.timestamp.isoformat()}"
            for record in run.shed_records
        )
    lines.extend(f"note: {note}" for note in report.notes)
    counts = {outcome: sum(1 for run in report.runs if run.outcome == outcome) for outcome in CommandOutcome}
    totals = ", ".join(f"{count} {outcome}" for outcome, count in counts.items())
    lines.append(f"{len(report.runs)} commands: {totals}")
    lines.append(f"summary: {summary_path}")
    return "\n".join(lines) + "\n"
