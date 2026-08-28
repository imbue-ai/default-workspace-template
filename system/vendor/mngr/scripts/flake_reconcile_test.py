from collections.abc import Sequence
from textwrap import dedent

from scripts.flake_reconcile import CheckRunRecord
from scripts.flake_reconcile import ClusterStatus
from scripts.flake_reconcile import RunOutcome
from scripts.flake_reconcile import aggregate_flaky_tests
from scripts.flake_reconcile import parse_check_run_summary
from scripts.flake_reconcile import preferred_status_for_branches


def test_preferred_status_ready_when_a_flake_hit_main() -> None:
    # Any instance on main => it is a live problem on main => ready to work on.
    assert preferred_status_for_branches({"main"}) is ClusterStatus.READY
    assert preferred_status_for_branches({"main", "feature-a", "feature-b"}) is ClusterStatus.READY


def test_preferred_status_ready_when_multiple_feature_branches_but_no_main() -> None:
    # Never on main, but >1 distinct feature branch => branch-independent / systemic => ready.
    assert preferred_status_for_branches({"feature-a", "feature-b"}) is ClusterStatus.READY


def test_preferred_status_backlog_when_single_unmerged_feature_branch() -> None:
    # Only ever on one unmerged feature branch => likely that branch's own bug, not a main
    # problem => backlog (a fixer could not reproduce it on main).
    assert preferred_status_for_branches({"feature-a"}) is ClusterStatus.BACKLOG


def test_preferred_status_unknown_when_no_branches() -> None:
    assert preferred_status_for_branches(set()) is ClusterStatus.UNKNOWN


def _sample_acceptance_summary() -> str:
    return dedent(
        """\
        ## Acceptance Tests: retry + flaky summary

        - Unique tests: **3**
        - Total runs (attempts across retries): **6**
        - Tests that ran more than once: **3**
        - Tests marked `@pytest.mark.flaky`: **2**
        - Flaky-recovered (failed then passed): **1**
        - Failing (final): **1**

        ## Failures

        <details><summary><code>pkg/test_x.py::test_flaker</code> (attempt 1/2) &mdash; failure: AssertionError: Creating host abc-def-ghi in modal</summary>

        ```
        traceback here
        ```

        </details>

        <details><summary><code>pkg/test_x.py::test_broken</code> (attempt 1/2) &mdash; failure: AssertionError: assert &#x27;/&#x27; == &#x27;/login&#x27;</summary>

        ```
        tb
        ```

        </details>

        | Test | Runs | Final | `@flaky` |
        | --- | ---: | --- | :---: |
        | `pkg/test_x.py::test_broken` | 2 | failed | no |
        | `pkg/test_x.py::test_flaker` | 2 | flaked 1, passed 1 | yes |
        | `pkg/test_x.py::test_solid` | 3 | passed | yes |
        """
    )


def test_parse_check_run_summary_classifies_table_rows() -> None:
    parsed = parse_check_run_summary(_sample_acceptance_summary())
    row_by_test = {row.test: row for row in parsed.rows}
    assert row_by_test["pkg/test_x.py::test_flaker"].status is RunOutcome.FLAKY_RECOVERED
    assert row_by_test["pkg/test_x.py::test_flaker"].flaked_count == 1
    assert row_by_test["pkg/test_x.py::test_flaker"].passed_count == 1
    assert row_by_test["pkg/test_x.py::test_flaker"].is_marked_flaky is True
    assert row_by_test["pkg/test_x.py::test_broken"].status is RunOutcome.HARD_FAILURE
    assert row_by_test["pkg/test_x.py::test_broken"].is_marked_flaky is False
    assert row_by_test["pkg/test_x.py::test_solid"].status is RunOutcome.PASSED


def test_parse_check_run_summary_extracts_and_unescapes_failure_lines() -> None:
    parsed = parse_check_run_summary(_sample_acceptance_summary())
    line_by_test = {line.test: line for line in parsed.failure_lines}
    flaker_line = line_by_test["pkg/test_x.py::test_flaker"]
    assert flaker_line.kind == "failure"
    assert flaker_line.attempt == 1
    assert flaker_line.total_attempts == 2
    assert "Creating host abc-def-ghi in modal" in flaker_line.first_line
    # HTML entities in the message are unescaped back to their literal characters.
    assert line_by_test["pkg/test_x.py::test_broken"].first_line == "AssertionError: assert '/' == '/login'"


def test_parse_check_run_summary_reads_header_counts() -> None:
    parsed = parse_check_run_summary(_sample_acceptance_summary())
    assert parsed.flaky_recovered_count == 1
    assert parsed.failing_final_count == 1


def _summary_from_rows(rows: Sequence[tuple[str, str, str, str]]) -> str:
    # Each row is (test id, final-cell text, "yes"/"no" flaky mark, failure first line).
    failure_blocks = "\n\n".join(
        f"<details><summary><code>{test}</code> (attempt 1/2) &mdash; failure: {first_line}</summary>\n\n```\ntb\n```\n\n</details>"
        for test, _final, _flaky, first_line in rows
        if first_line
    )
    table_rows = "\n".join(f"| `{test}` | 2 | {final} | {flaky} |" for test, final, flaky, _first_line in rows)
    return (
        "## Acceptance Tests\n\n"
        "- Flaky-recovered (failed then passed): **1**\n"
        "- Failing (final): **1**\n\n"
        "## Failures\n\n" + failure_blocks + "\n\n"
        "| Test | Runs | Final | `@flaky` |\n"
        "| --- | ---: | --- | :---: |\n" + table_rows + "\n"
    )


def _acceptance_record(commit: str, occurred_at: str, summary: str) -> CheckRunRecord:
    return CheckRunRecord(
        suite="Acceptance Tests",
        conclusion="neutral",
        commit=commit,
        branch="feature-branch",
        occurred_at=occurred_at,
        url=f"https://github.example/runs/{commit}",
        parsed=parse_check_run_summary(summary),
    )


_MODAL_TEST: str = "libs/mngr_modal/test_create.py::test_create_on_modal"
_RUFF_TEST: str = "scripts/test_ratchets.py::test_no_ruff_errors"


def test_aggregate_flaky_tests_keeps_flaky_tests_and_drops_pure_hard_failures() -> None:
    records = [
        _acceptance_record(
            "commit-1",
            "2026-08-10T01:00:00Z",
            _summary_from_rows(
                [
                    (_MODAL_TEST, "flaked 1, passed 1", "yes", "AssertionError: Creating host aaa-bbb in modal"),
                    (_RUFF_TEST, "failed", "no", "AssertionError: ruff would reformat foo.py"),
                ]
            ),
        ),
        _acceptance_record(
            "commit-2",
            "2026-08-11T01:00:00Z",
            _summary_from_rows(
                [(_MODAL_TEST, "flaked 1, passed 1", "yes", "AssertionError: Creating host ccc-ddd in modal")]
            ),
        ),
        _acceptance_record(
            "commit-3",
            "2026-08-12T01:00:00Z",
            _summary_from_rows([(_MODAL_TEST, "failed", "yes", "AssertionError: Sandbox failed to come online")]),
        ),
    ]

    flaky_tests = aggregate_flaky_tests(records)

    flaky_by_test = {flaky_test.test: flaky_test for flaky_test in flaky_tests}
    # The ruff gate hard-failed but never flaky-recovered, so it is not a flake.
    assert set(flaky_by_test) == {_MODAL_TEST}
    modal_flake = flaky_by_test[_MODAL_TEST]
    assert modal_flake.flake_commit_count == 2
    assert modal_flake.hard_fail_commit_count == 1
    assert modal_flake.is_marked_flaky is True
    assert modal_flake.suites == ("Acceptance Tests",)
    assert modal_flake.first_seen == "2026-08-10T01:00:00Z"
    assert modal_flake.last_seen == "2026-08-12T01:00:00Z"
    # Raw failure lines are handed to the agent to cluster -- no root-causing here.
    assert "AssertionError: Creating host aaa-bbb in modal" in modal_flake.sample_failure_lines
    assert "AssertionError: Creating host ccc-ddd in modal" in modal_flake.sample_failure_lines
