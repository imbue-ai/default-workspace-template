import json
import tomllib
from datetime import datetime
from datetime import timezone
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from imbue.minds_evals.cleanup_environments import CI_NAME_MARKER
from imbue.minds_evals.cleanup_environments import format_ci_user_id_prefix
from imbue.minds_evals.cleanup_environments import parse_ci_timestamp
from imbue.minds_evals.cli import main
from imbue.minds_evals.cli import run_cleanup_environments
from imbue.minds_evals.mock_modal_admin_test import MockModalEnvironmentAdmin
from imbue.minds_evals.testing import CI_SWEEP_PREFIX
from imbue.minds_evals.testing import DEVELOPER_ENVIRONMENT_NAME
from imbue.minds_evals.testing import create_branch
from imbue.minds_evals.testing import expected_modal_environment_name
from imbue.minds_evals.testing import make_local_git_repo
from imbue.minds_evals.testing import tag_commit
from imbue.minds_evals.testing import write_trial_dir


def test_generate_passes_its_ref_overrides_through(tmp_path: Path) -> None:
    """The scheduled run pins a known-good pair through these two options rather than by editing the
    checked-in config, so what the command forwards is what the dataset ends up pinned to."""
    repo = make_local_git_repo(tmp_path, "fake-mngr", commit_count=2)
    dwt_repo = make_local_git_repo(tmp_path, "fake-dwt", commit_count=2)
    tag_commit(repo.repo_dir, "minds-v9.9.9", repo.commit_shas[0])
    create_branch(dwt_repo.repo_dir, "staging", dwt_repo.commit_shas[0])
    config_path = tmp_path / "eval-config.json"
    config_path.write_text(
        json.dumps(
            {
                "mngr_branch": "main",
                "dwt_repo": str(dwt_repo.repo_dir),
                "personas": [{"id": "todo-app", "prompts": ["Build me a to-do app"]}],
            }
        )
    )
    output_dir = tmp_path / "dataset"

    result = CliRunner().invoke(
        main,
        [
            "generate",
            "--config",
            str(config_path),
            "--output",
            str(output_dir),
            "--mngr-repo",
            str(repo.repo_dir),
            "--mngr-ref",
            "minds-v9.9.9",
            "--dwt-ref",
            "staging",
        ],
    )

    assert result.exit_code == 0, result.output
    task_config = tomllib.loads((output_dir / "todo-app" / "task.toml").read_text())
    assert task_config["metadata"]["mngr_sha"] == repo.commit_shas[0]
    assert task_config["metadata"]["dwt_sha"] == dwt_repo.commit_shas[0]


def test_check_run_exits_zero_and_writes_both_summaries_for_a_passing_run(tmp_path: Path) -> None:
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa")
    summary_md = tmp_path / "summaries" / "live-summary.md"
    summary_json = tmp_path / "summaries" / "live-summary.json"

    result = CliRunner().invoke(
        main,
        ["check-run", str(job_dir), "--summary-md", str(summary_md), "--summary-json", str(summary_json)],
    )

    assert result.exit_code == 0, result.output
    assert summary_md.read_text().startswith("## minds-evals: nightly-run -- pass")
    assert json.loads(summary_json.read_text())["is_passed"] is True


def test_check_run_exits_nonzero_but_still_reports_a_failing_run(tmp_path: Path) -> None:
    """The scheduled job gates on this exit code, and publishes the summary either way -- so a
    failing run must still write the rows that say which trial went wrong."""
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa")
    write_trial_dir(job_dir, "greeting__bbbbbbb", case_id="greeting", failed_gate_names=("not_timed_out",))
    summary_json = tmp_path / "summaries" / "live-summary.json"

    result = CliRunner().invoke(main, ["check-run", str(job_dir), "--summary-json", str(summary_json)])

    assert result.exit_code == 1
    report = json.loads(summary_json.read_text())
    assert report["is_passed"] is False
    assert [trial["trial_name"] for trial in report["trials"] if not trial["is_passed"]] == ["greeting__bbbbbbb"]


def test_cleanup_environments_refuses_a_request_with_no_scope() -> None:
    result = CliRunner().invoke(main, ["cleanup-environments"])

    assert result.exit_code != 0
    assert "exactly one of --job-dir and --sweep-prefix" in result.output


def test_cleanup_environments_refuses_both_scopes_at_once(tmp_path: Path) -> None:
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa")

    result = CliRunner().invoke(
        main,
        ["cleanup-environments", "--job-dir", str(job_dir), "--sweep-prefix", CI_SWEEP_PREFIX],
    )

    assert result.exit_code != 0
    assert "exactly one of --job-dir and --sweep-prefix" in result.output


def test_cleanup_environments_refuses_a_sweep_with_no_age() -> None:
    """An age this command chose for the caller would silently decide how much of a still-running
    scheduled batch the sweep takes out, so it is demanded rather than defaulted."""
    result = CliRunner().invoke(main, ["cleanup-environments", "--sweep-prefix", CI_SWEEP_PREFIX])

    assert result.exit_code != 0
    assert "--older-than-hours" in result.output


@pytest.mark.parametrize("raw_age", ["0", "-1"])
def test_cleanup_environments_refuses_an_age_that_would_take_a_running_batch(raw_age: str) -> None:
    """The cutoff is `now - age`, so an age of zero or less selects every stamped name under the
    prefix -- including the environments a batch running right now just created."""
    result = CliRunner().invoke(
        main, ["cleanup-environments", "--sweep-prefix", CI_SWEEP_PREFIX, "--older-than-hours", raw_age]
    )

    assert result.exit_code != 0
    assert "must be greater than 0" in result.output


def test_cleanup_environments_refuses_a_prefix_too_broad_as_a_usage_error() -> None:
    """The refusal that stops an operator aiming a destructive sweep at a broad prefix has to read
    like the other four scope refusals rather than as a traceback: the person who reaches it
    mistyped a prefix, and the message is what tells them what a sweep prefix has to carry. The
    module raises its own CleanupScopeError; the command is what restates it."""
    admin = MockModalEnvironmentAdmin(environment_names=[DEVELOPER_ENVIRONMENT_NAME])

    with pytest.raises(click.UsageError, match="ci-"):
        run_cleanup_environments(
            admin, job_dir=None, sweep_prefix="minds-staging-", older_than_hours=8.0, is_dry_run=False
        )

    # Refused before the workspace was reached at all, so nothing was listed and nothing deleted.
    assert admin.listing_count == 0
    assert admin.deletion_attempts == []


def test_cleanup_environments_refuses_an_age_it_would_have_to_ignore(tmp_path: Path) -> None:
    """A job-scoped deletion takes exactly what the job recorded, so an age cannot apply to it."""
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa")

    result = CliRunner().invoke(main, ["cleanup-environments", "--job-dir", str(job_dir), "--older-than-hours", "8"])

    assert result.exit_code != 0
    assert "--older-than-hours applies to --sweep-prefix" in result.output


def test_cleanup_environments_does_nothing_for_a_job_that_recorded_no_environments(tmp_path: Path) -> None:
    """The scheduled job runs this over its oracle job directory on every run. An oracle trial does
    write a state.json -- solve.sh cats one in -- but it boots no Minds and creates no Modal
    environment, so the pass finds nothing to delete and says so rather than failing."""
    job_dir = tmp_path / "oracle-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa", is_environment_recorded=False)

    result = CliRunner().invoke(main, ["cleanup-environments", "--job-dir", str(job_dir)])

    assert result.exit_code == 0, result.output
    assert "Nothing to delete" in result.output


def test_cleanup_environments_dry_run_names_the_environments_without_deleting_them(tmp_path: Path) -> None:
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa")
    write_trial_dir(job_dir, "greeting__bbbbbbb", case_id="greeting")

    result = CliRunner().invoke(main, ["cleanup-environments", "--job-dir", str(job_dir), "--dry-run"])

    assert result.exit_code == 0, result.output
    assert expected_modal_environment_name("todo-app__aaaaaaa") in result.output
    assert expected_modal_environment_name("greeting__bbbbbbb") in result.output


def test_cleanup_environments_deletes_exactly_what_the_job_recorded(tmp_path: Path) -> None:
    """Deleting is the one thing this CLI does that cannot be undone, so what it hands the Modal
    workspace is pinned against a stand-in for that workspace rather than against a log line."""
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa")
    write_trial_dir(job_dir, "greeting__bbbbbbb", case_id="greeting")
    admin = MockModalEnvironmentAdmin(
        environment_names=[
            expected_modal_environment_name("todo-app__aaaaaaa"),
            expected_modal_environment_name("greeting__bbbbbbb"),
            expected_modal_environment_name("somebody-elses-run"),
        ]
    )

    run_cleanup_environments(admin, job_dir=job_dir, sweep_prefix="", older_than_hours=None, is_dry_run=False)

    assert admin.environment_names == [expected_modal_environment_name("somebody-elses-run")]


def test_cleanup_environments_sweeps_only_the_stamped_names_old_enough_to_be_finished() -> None:
    """The backstop scope as the CLI wires it: the prefix, the age, and the moment it is measured
    from. The run happening right now is what the age has to spare, which is why one of these
    environments carries a stamp minted as this test runs."""
    running_now = "minds-staging-evals-{}todo-app-a1b2c3d4".format(
        format_ci_user_id_prefix(datetime.now(timezone.utc))
    )
    admin = MockModalEnvironmentAdmin(
        environment_names=[
            DEVELOPER_ENVIRONMENT_NAME,
            running_now,
            "minds-staging-evals-ci-20200101t110000z-todo-app-e5f6a7b8",
        ]
    )

    run_cleanup_environments(admin, job_dir=None, sweep_prefix=CI_SWEEP_PREFIX, older_than_hours=8.0, is_dry_run=False)

    assert admin.environment_names == [DEVELOPER_ENVIRONMENT_NAME, running_now]


def test_cleanup_environments_exits_nonzero_when_modal_refused_a_deletion(tmp_path: Path) -> None:
    """The scheduled job's cleanup step gates on this exit code: a token without manage access, or an
    environment Modal would not remove, has to reach it rather than a log line nobody reads."""
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa")
    admin = MockModalEnvironmentAdmin(
        environment_names=[expected_modal_environment_name("todo-app__aaaaaaa")],
        undeletable_names=frozenset({expected_modal_environment_name("todo-app__aaaaaaa")}),
    )

    with pytest.raises(SystemExit) as exit_info:
        run_cleanup_environments(admin, job_dir=job_dir, sweep_prefix="", older_than_hours=None, is_dry_run=False)

    assert exit_info.value.code == 1
    # A refusal is reported, never raised out of the pass, so the environment is still there.
    assert admin.environment_names == [expected_modal_environment_name("todo-app__aaaaaaa")]


def test_ci_user_id_prefix_mints_a_prefix_the_sweep_can_scope_and_age(tmp_path: Path) -> None:
    """The scheduled workflow reads this file and hands the value to the driver, and the sweep has
    to be able to read its own marker and timestamp back out of the environment names it ends up
    in -- so the minting and the parsing are pinned to each other here."""
    output_path = tmp_path / "scope" / "user-id-prefix.txt"

    result = CliRunner().invoke(main, ["ci-user-id-prefix", "--output", str(output_path)])

    assert result.exit_code == 0, result.output
    prefix = output_path.read_text().strip()
    assert prefix.startswith(CI_NAME_MARKER)
    assert parse_ci_timestamp("minds-staging-evals-{}todo-app-cafe1234".format(prefix)) is not None
