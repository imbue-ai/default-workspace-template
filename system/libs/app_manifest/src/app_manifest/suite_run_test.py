from datetime import datetime
from datetime import timezone
from pathlib import Path

from inline_snapshot import snapshot

from app_manifest.selection import SuiteCommand
from app_manifest.selection import SuiteKind
from app_manifest.suite_run import CommandOutcome
from app_manifest.suite_run import CommandRun
from app_manifest.suite_run import ShedRecord
from app_manifest.suite_run import SuiteRunReport
from app_manifest.suite_run import parse_failed_test_ids
from app_manifest.suite_run import render_report
from app_manifest.testing import suite_command

_MOMENT = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)

_PYTEST_OUTPUT = """\
============================= test session starts ==============================
collected 7 items / 1 error
==================== short test summary info (inner run) =======================
FAILED inner/test_ignored.py::test_from_an_earlier_section - AssertionError
=========================== short test summary info ============================
FAILED pkg/test_a.py::test_one - AssertionError: assert 1 == 2
FAILED pkg/test_a.py::test_param[x-y] - ValueError: bad
FAILED pkg/test_a.py::test_param[a - b] - ValueError: [1] - worse
ERROR pkg/test_c.py::test_setup[c - d]
ERROR pkg/test_b.py - ModuleNotFoundError: No module named 'gone'
ERROR pkg/test_c.py::test_fixture
FAILED pkg/test_a.py::test_one - AssertionError: assert 1 == 2
PASSED pkg/test_a.py::test_two
=================== 4 failed, 1 passed, 3 errors in 0.52s ======================
"""


def test_failed_test_ids_come_from_the_last_short_summary_once_each_without_their_messages() -> None:
    assert parse_failed_test_ids(_PYTEST_OUTPUT) == (
        "pkg/test_a.py::test_one",
        "pkg/test_a.py::test_param[x-y]",
        "pkg/test_a.py::test_param[a - b]",
        "pkg/test_c.py::test_setup[c - d]",
        "pkg/test_b.py",
        "pkg/test_c.py::test_fixture",
    )


def test_output_without_a_short_summary_names_no_failed_tests() -> None:
    assert parse_failed_test_ids("collected 0 items\n\nno tests ran in 0.01s\n") == ()


def _command_run(
    command: SuiteCommand,
    outcome: CommandOutcome,
    exit_code: int | None,
    failed_test_ids: tuple[str, ...] = (),
    rerun: SuiteCommand | None = None,
    shed_records: tuple[ShedRecord, ...] = (),
) -> CommandRun:
    return CommandRun(
        command=command,
        outcome=outcome,
        exit_code=exit_code,
        started_at=_MOMENT,
        finished_at=_MOMENT,
        log_path=Path("out") / f"{command.kind}.log",
        failed_test_ids=failed_test_ids,
        rerun_log_path=Path("out") / f"{command.kind}-rerun.log" if failed_test_ids else None,
        rerun=rerun,
        shed_records=shed_records,
        is_coverage_floor_missed=False,
    )


def test_the_report_names_each_commands_outcome_and_what_to_do_about_it() -> None:
    chat_run = suite_command(SuiteKind.PYTEST, "system/apps/chat", ("uv", "run", "pytest", "-m", ""))
    failed_ids = tuple(f"imbue/chat/test_page.py::test_{index}" for index in range(12))
    report = SuiteRunReport(
        runs=(
            _command_run(
                chat_run,
                CommandOutcome.FAILED,
                1,
                failed_ids,
                suite_command(
                    SuiteKind.PYTEST, "system/apps/chat", ("uv", "run", "pytest", "--no-cov", "-m", "", "rerun")
                ),
            ),
            _command_run(
                suite_command(SuiteKind.FULL_ROOT, ".", ("uv", "run", "pytest")),
                CommandOutcome.FLAKY,
                1,
                ("system/libs/x/test_x.py::test_once",),
            ),
            _command_run(
                suite_command(SuiteKind.FRONTEND_BUILD, "system", ("npm", "run", "build")), CommandOutcome.FAILED, None
            ),
            _command_run(
                suite_command(SuiteKind.PYTEST, ".", ("uv", "run", "pytest", "system/libs/y")),
                CommandOutcome.SHED,
                -9,
                shed_records=(ShedRecord(timestamp=_MOMENT, pid=4242, comm=None),),
            ),
        ),
        notes=("a note from the selection",),
    )

    assert render_report(report, Path("out") / "summary.json") == snapshot("""\
failed: (cd system/apps/chat && uv run pytest -m '')
    output: out/pytest.log
    rerun output: out/pytest-rerun.log
    failed: imbue/chat/test_page.py::test_0
    failed: imbue/chat/test_page.py::test_1
    failed: imbue/chat/test_page.py::test_2
    failed: imbue/chat/test_page.py::test_3
    failed: imbue/chat/test_page.py::test_4
    failed: imbue/chat/test_page.py::test_5
    failed: imbue/chat/test_page.py::test_6
    failed: imbue/chat/test_page.py::test_7
    failed: imbue/chat/test_page.py::test_8
    failed: imbue/chat/test_page.py::test_9
    and 2 more
    rerun these: (cd system/apps/chat && uv run pytest --no-cov -m '' rerun)
flaky: uv run pytest
    output: out/full_root.log
    rerun output: out/full_root-rerun.log
    passed on rerun: system/libs/x/test_x.py::test_once
failed: (cd system && npm run build)
    output: out/frontend_build.log
    timed out after 3 hours
shed: uv run pytest system/libs/y
    output: out/pytest.log
    memory guard killed a process (pid 4242) at 2026-10-28T12:00:00+00:00
note: a note from the selection
4 commands: 0 passed, 1 flaky, 2 failed, 1 shed
summary: out/summary.json
""")
