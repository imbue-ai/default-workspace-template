import json
import sys
from pathlib import Path

import pytest

from app_manifest.selection import SuiteCommand
from app_manifest.selection import SuiteKind
from app_manifest.selection import SuiteSelection
from app_manifest.suite_run import CommandOutcome
from app_manifest.suite_run import SUMMARY_FILE_NAME
from app_manifest.suite_run import SuiteRunReport
from app_manifest.suite_run import render_report
from app_manifest.suite_run import run_selection
from app_manifest.testing import suite_command

# One test of each kind: always passing, always failing, and failing only on its first run
# (each of the last two leaves a marker behind, so the rerun of the failed tests passes).
_TEST_CASES = """
from pathlib import Path


def test_passes_every_time_48213() -> None:
    assert True


def test_fails_every_time_48213() -> None:
    assert False, "fails on purpose"


def test_fails_only_the_first_time_48213() -> None:
    marker = Path(__file__).parent / "ran-once-48213"
    if marker.exists():
        return
    marker.write_text("")
    assert False, "fails the first time on purpose"


def test_fails_only_the_first_time_too_48213() -> None:
    marker = Path(__file__).parent / "ran-once-too-48213"
    if marker.exists():
        return
    marker.write_text("")
    assert False, "fails the first time on purpose"
"""

# Appends a shed record for the pid it is given to the ledger, a moment after that process died,
# as the memory guard's after-kill hook does.
_RECORD_SHED_LATER = """
import json, os, sys, threading
from datetime import datetime, timezone
threading.Event().wait(0.5)
with open(sys.argv[1], "a") as ledger:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    ledger.write(json.dumps({"timestamp": stamp, "type": "process_shed", "pid": int(sys.argv[2]), "comm": "pytest"}) + "\\n")
"""

# Dies by SIGKILL, leaving behind, when given a ledger, a process that records the kill afterwards.
_DIE_THEN_SHED = f"""
import os, signal, subprocess, sys
if sys.argv[1]:
    subprocess.Popen([sys.executable, "-c", {_RECORD_SHED_LATER!r}, sys.argv[1], str(os.getpid())], start_new_session=True)
os.kill(os.getpid(), signal.SIGKILL)
"""

# A test that fails on its first run and, on the rerun of its failed tests, is shed: it kills the
# pytest process running it, leaving behind a process that records the kill in the ledger named
# beside it.
_SHED_ON_THE_RERUN = f"""
import os, signal, subprocess, sys
from pathlib import Path


def test_fails_then_is_shed_on_the_rerun_48213() -> None:
    marker = Path(__file__).parent / "failed-once-48213"
    if not marker.exists():
        marker.write_text("")
        assert False, "fails the first time on purpose"
    ledger = (Path(__file__).parent / "ledger-path-48213").read_text()
    subprocess.Popen([sys.executable, "-c", {_RECORD_SHED_LATER!r}, ledger, str(os.getpid())], start_new_session=True)
    os.kill(os.getpid(), signal.SIGKILL)
"""


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    (project / "suite").mkdir(parents=True)
    (project / "suite" / "test_cases.py").write_text(_TEST_CASES)
    # An empty ini keeps the workspace's own pytest config out of the inner runs.
    (project / "pytest.ini").write_text("[pytest]\n")
    return project


def _pytest_command(*arguments: str) -> SuiteCommand:
    return suite_command(SuiteKind.PYTEST, ".", (sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *arguments))


def _python_command(kind: SuiteKind, *arguments: str) -> SuiteCommand:
    return suite_command(kind, ".", (sys.executable, *arguments))


def _run(project: Path, ledger: Path | None, *commands: SuiteCommand) -> SuiteRunReport:
    selection = SuiteSelection(commands=commands, paths=(), is_full_root=False, notes=(), coverage_measured_roots=())
    return run_selection(selection, project, project / "run-output", ledger, lambda index, command: None)


# Five pytest processes start here, each loading every workspace plugin.
@pytest.mark.timeout(30)
def test_each_command_ends_as_passed_flaky_or_failed_by_what_its_tests_did_on_the_rerun(tmp_path: Path) -> None:
    project = _project(tmp_path)

    report = _run(
        project,
        tmp_path / "shed.jsonl",
        _pytest_command("suite/test_cases.py::test_passes_every_time_48213"),
        _pytest_command(
            "suite/test_cases.py::test_passes_every_time_48213",
            "suite/test_cases.py::test_fails_only_the_first_time_48213",
        ),
        _pytest_command("suite/test_cases.py"),
    )

    passed, flaky, failed = report.runs
    assert (passed.outcome, passed.failed_test_ids, passed.rerun_log_path) == (CommandOutcome.PASSED, (), None)
    assert (flaky.outcome, flaky.failed_test_ids, flaky.rerun) == (CommandOutcome.FLAKY, (), None)
    assert flaky.flaky_test_ids == ("suite/test_cases.py::test_fails_only_the_first_time_48213",)
    # The first fails-once test passes by the third run; the other fails once more, and passes on the rerun.
    assert failed.outcome == CommandOutcome.FAILED
    assert failed.failed_test_ids == ("suite/test_cases.py::test_fails_every_time_48213",)
    assert failed.flaky_test_ids == ("suite/test_cases.py::test_fails_only_the_first_time_too_48213",)
    assert failed.rerun_log_path is not None
    assert "1 failed, 1 passed" in failed.rerun_log_path.read_text()
    assert failed.rerun is not None
    assert failed.rerun.argv == (sys.executable, "-m", "pytest", "-m", "", *failed.failed_test_ids)
    assert not report.is_passing


# The command killed with no shed recorded waits out the grace for a record that never comes.
@pytest.mark.timeout(30)
def test_a_command_killed_while_the_guard_recorded_a_shed_is_a_shed_and_otherwise_a_failure(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    ledger = tmp_path / "shed.jsonl"

    report = _run(
        project,
        ledger,
        _python_command(SuiteKind.PYTEST, "-c", _DIE_THEN_SHED, str(ledger)),
        _python_command(SuiteKind.PYTEST, "-c", _DIE_THEN_SHED, ""),
    )

    shed, killed = report.runs
    assert shed.outcome == CommandOutcome.SHED
    assert [record.comm for record in shed.shed_records] == ["pytest"]
    # The second command died the same way, but the guard recorded nothing while it ran.
    assert killed.outcome == CommandOutcome.FAILED
    assert killed.shed_records == ()


# Two pytest processes start here, and the shed is recorded a moment after the rerun dies.
@pytest.mark.timeout(30)
def test_a_run_whose_rerun_is_shed_is_a_shed_that_keeps_its_failed_tests(tmp_path: Path) -> None:
    project = _project(tmp_path)
    ledger = tmp_path / "shed.jsonl"
    (project / "suite" / "test_shed_on_the_rerun.py").write_text(_SHED_ON_THE_RERUN)
    (project / "suite" / "ledger-path-48213").write_text(str(ledger))

    report = _run(project, ledger, _pytest_command("suite/test_shed_on_the_rerun.py"))

    (run,) = report.runs
    assert run.outcome == CommandOutcome.SHED
    assert run.failed_test_ids == ("suite/test_shed_on_the_rerun.py::test_fails_then_is_shed_on_the_rerun_48213",)
    assert run.rerun is not None
    assert [record.comm for record in run.shed_records] == ["pytest"]
    assert not report.is_passing


# Two pytest processes start here, the first under coverage.
@pytest.mark.timeout(30)
def test_a_run_that_missed_its_coverage_floor_fails_even_when_its_failed_tests_pass_on_the_rerun(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)

    # Only one test of the module runs, so the module's coverage is under the floor.
    report = _run(
        project,
        tmp_path / "shed.jsonl",
        _pytest_command(
            "--cov=suite", "--cov-fail-under=100", "suite/test_cases.py::test_fails_only_the_first_time_48213"
        ),
    )

    (run,) = report.runs
    assert (run.outcome, run.is_coverage_floor_missed, run.failed_test_ids) == (CommandOutcome.FAILED, True, ())
    assert run.flaky_test_ids == ("suite/test_cases.py::test_fails_only_the_first_time_48213",)
    assert run.rerun_log_path is not None
    assert "1 passed" in run.rerun_log_path.read_text()
    assert "    coverage floor not reached" in render_report(report, project / SUMMARY_FILE_NAME).splitlines()
    assert not report.is_passing


def test_a_failing_command_that_is_not_pytest_fails_without_a_rerun(tmp_path: Path) -> None:
    project = _project(tmp_path)

    report = _run(project, None, _python_command(SuiteKind.FRONTEND_CHECK, "-c", "raise SystemExit(3)"))

    (run,) = report.runs
    assert (run.outcome, run.exit_code, run.rerun_log_path, run.rerun) == (CommandOutcome.FAILED, 3, None, None)
    # No memory guard keeps a ledger here, so the report says a shed could not have been told from a failure.
    assert any("no memory guard" in note for note in report.notes)


def test_the_summary_on_disk_matches_the_report_and_every_command_keeps_its_output(tmp_path: Path) -> None:
    project = _project(tmp_path)

    report = _run(
        project,
        tmp_path / "shed.jsonl",
        _pytest_command("suite/test_cases.py::test_passes_every_time_48213"),
        _python_command(SuiteKind.FRONTEND_CHECK, "-c", "print('frontend check output 48213')"),
    )

    summary = json.loads((project / "run-output" / SUMMARY_FILE_NAME).read_text())
    assert summary == json.loads(report.model_dump_json())
    assert "1 passed" in report.runs[0].log_path.read_text()
    assert "frontend check output 48213" in report.runs[1].log_path.read_text()
