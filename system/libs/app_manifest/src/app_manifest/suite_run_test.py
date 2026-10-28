import json
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from app_manifest.cli import app_manifest_cli
from app_manifest.primitives import RepoRelativePath
from app_manifest.selection import ChangedPathClass
from app_manifest.selection import SelectionReason
from app_manifest.selection import SuiteCommand
from app_manifest.selection import SuiteKind
from app_manifest.selection import SuiteSelection
from app_manifest.suite_run import CommandOutcome
from app_manifest.suite_run import SUMMARY_FILE_NAME
from app_manifest.suite_run import SuiteRunReport
from app_manifest.suite_run import run_selection
from app_manifest.testing import build_selection_workspace
from imbue.imbue_common.primitives import NonEmptyStr

_REASON = SelectionReason(
    path=RepoRelativePath("suite/test_cases.py"),
    path_class=ChangedPathClass.TEST_FILE,
    detail=NonEmptyStr("own tests"),
)

# One test of each kind: always passing, always failing, and failing only on its first run
# (it leaves a marker behind, so the rerun of the failed tests passes).
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
"""

# Appends a shed record for itself to the ledger, as the memory guard does, then dies by SIGKILL.
_SHED_THEN_DIE = """
import json, os, signal, sys
from datetime import datetime, timezone
if sys.argv[1]:
    with open(sys.argv[1], "a") as ledger:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        ledger.write(json.dumps({"timestamp": stamp, "type": "process_shed", "pid": os.getpid(), "comm": "pytest"}) + "\\n")
os.kill(os.getpid(), signal.SIGKILL)
"""


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    (project / "suite").mkdir(parents=True)
    (project / "suite" / "test_cases.py").write_text(_TEST_CASES)
    # An empty ini keeps the workspace's own pytest config out of the inner runs.
    (project / "pytest.ini").write_text("[pytest]\n")
    return project


def _pytest_command(*node_ids: str) -> SuiteCommand:
    return SuiteCommand(
        kind=SuiteKind.PYTEST,
        working_directory=NonEmptyStr("."),
        argv=(sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *node_ids),
        reasons=(_REASON,),
    )


def _python_command(kind: SuiteKind, *arguments: str) -> SuiteCommand:
    return SuiteCommand(
        kind=kind, working_directory=NonEmptyStr("."), argv=(sys.executable, *arguments), reasons=(_REASON,)
    )


def _run(project: Path, ledger: Path, *commands: SuiteCommand) -> SuiteRunReport:
    selection = SuiteSelection(
        commands=commands, paths=(), is_full_root=False, notes=(), coverage_measured_roots=frozenset()
    )
    return run_selection(selection, project, project / "run-output", ledger, lambda index, command: None)


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
    assert flaky.outcome == CommandOutcome.FLAKY
    assert flaky.failed_test_ids == ("suite/test_cases.py::test_fails_only_the_first_time_48213",)
    # The marker is already there by the third run, so only the always-failing test is left.
    assert failed.outcome == CommandOutcome.FAILED
    assert failed.failed_test_ids == ("suite/test_cases.py::test_fails_every_time_48213",)
    assert failed.rerun_log_path is not None
    assert "1 failed" in failed.rerun_log_path.read_text()
    assert not report.is_passing


def test_a_command_killed_while_the_guard_recorded_a_shed_is_a_shed_and_otherwise_a_failure(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    ledger = tmp_path / "shed.jsonl"

    report = _run(
        project,
        ledger,
        _python_command(SuiteKind.PYTEST, "-c", _SHED_THEN_DIE, str(ledger)),
        _python_command(SuiteKind.PYTEST, "-c", _SHED_THEN_DIE, ""),
    )

    shed, killed = report.runs
    assert shed.outcome == CommandOutcome.SHED
    assert [record.comm for record in shed.shed_records] == ["pytest"]
    # The second command died the same way, but the guard recorded nothing while it ran.
    assert killed.outcome == CommandOutcome.FAILED
    assert killed.shed_records == ()


def test_a_failing_command_that_is_not_pytest_fails_without_a_rerun(tmp_path: Path) -> None:
    project = _project(tmp_path)

    report = _run(
        project, tmp_path / "shed.jsonl", _python_command(SuiteKind.FRONTEND_CHECK, "-c", "raise SystemExit(3)")
    )

    (run,) = report.runs
    assert (run.outcome, run.exit_code, run.rerun_log_path) == (CommandOutcome.FAILED, 3, None)


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


def test_select_tests_all_runs_every_suite_with_all_its_markers(tmp_path: Path) -> None:
    build_selection_workspace(tmp_path)

    result = CliRunner().invoke(app_manifest_cli, ["select-tests", "--repo-root", str(tmp_path), "--all"])

    assert result.exit_code == 0, result.output
    lines = [line for line in result.output.splitlines() if not line.startswith("#")]
    assert "uv run pytest" in lines
    assert any(line.startswith("(cd system/apps/chat && uv run pytest -m '' ") for line in lines)
    assert "(cd system && npm run build)" in lines


@pytest.mark.parametrize(
    "conflicting",
    [["--diff-base", "main"], ["--path", "README.md"], ["--scope-file", "scope.json"]],
)
def test_all_refuses_to_be_combined_with_another_way_of_naming_the_change(
    tmp_path: Path, conflicting: list[str]
) -> None:
    result = CliRunner().invoke(app_manifest_cli, ["run-tests", "--repo-root", str(tmp_path), "--all", *conflicting])

    assert result.exit_code == 2
    assert "--all selects every suite on its own" in result.output


def test_run_tests_with_nothing_selected_passes_and_still_writes_the_summary(tmp_path: Path) -> None:
    build_selection_workspace(tmp_path)
    output_directory = tmp_path / "run-output"

    # A change to documentation only selects nothing, so the run passes without running anything.
    nothing = CliRunner().invoke(
        app_manifest_cli,
        [
            "run-tests",
            "--repo-root",
            str(tmp_path),
            "--path",
            "system/libs/corelib/README.md",
            "--output-dir",
            str(output_directory),
        ],
    )

    assert nothing.exit_code == 0, nothing.output
    assert "0 commands" in nothing.output
    summary = json.loads((output_directory / SUMMARY_FILE_NAME).read_text())
    assert summary["runs"] == [] and summary["is_passing"] is True
