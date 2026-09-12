import re
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

from imbue.minds_evals.cleanup_environments import delete_environments
from imbue.minds_evals.cleanup_environments import format_ci_user_id_prefix
from imbue.minds_evals.cleanup_environments import parse_ci_timestamp
from imbue.minds_evals.cleanup_environments import read_job_environment_names
from imbue.minds_evals.cleanup_environments import resolve_sweep_cutoff
from imbue.minds_evals.cleanup_environments import run_cleanup
from imbue.minds_evals.cleanup_environments import select_sweepable_environment_names
from imbue.minds_evals.cleanup_environments import select_swept_environment_names
from imbue.minds_evals.driver import derive_user_id
from imbue.minds_evals.errors import CleanupScopeError
from imbue.minds_evals.errors import JobReadError
from imbue.minds_evals.minds_bridge import derive_modal_environment_name
from imbue.minds_evals.mock_modal_admin_test import MockModalEnvironmentAdmin
from imbue.minds_evals.testing import CI_SWEEP_PREFIX
from imbue.minds_evals.testing import DEVELOPER_ENVIRONMENT_NAME
from imbue.minds_evals.testing import SCHEDULED_WORKFLOW_PATH
from imbue.minds_evals.testing import STAGING_MNGR_PREFIX
from imbue.minds_evals.testing import expected_modal_environment_name
from imbue.minds_evals.testing import read_scheduled_workflow_text
from imbue.minds_evals.testing import write_trial_dir

# The two tests below are what hold testing.CI_SWEEP_PREFIX to the workflow's CI_ENVIRONMENT_PREFIX:
# one reads the workflow and compares, the other builds an environment name the way a CI trial does
# and asserts the prefix selects it.
_CI_ENVIRONMENT_PREFIX_PATTERN = re.compile(r"^\s*CI_ENVIRONMENT_PREFIX:\s*(\S+)\s*$", re.MULTILINE)
_NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)

# A scheduled run's environment from an hour ago, and one from a week ago. That one and
# DEVELOPER_ENVIRONMENT_NAME must survive every sweep in this file.
_RECENT_CI_ENVIRONMENT = "minds-staging-evals-ci-20260901t110000z-todo-app-a1b2c3d4"
_OLD_CI_ENVIRONMENT = "minds-staging-evals-ci-20260825t110000z-todo-app-e5f6a7b8"
_UNRELATED_ENVIRONMENT = "minds-staging-c6445288c83f448d82e9837ed1410796"


def test_format_ci_user_id_prefix_round_trips_through_the_name_it_ends_up_in() -> None:
    prefix = format_ci_user_id_prefix(_NOW)

    assert prefix == "ci-20260901t120000z-"
    assert parse_ci_timestamp("minds-staging-evals-{}todo-app-cafe1234".format(prefix)) == _NOW


def test_the_sweep_prefix_this_file_pins_is_the_one_the_scheduled_workflow_actually_sweeps_with() -> None:
    """The workflow hard-codes CI_ENVIRONMENT_PREFIX because a GitHub Actions workflow cannot import
    Python, so the two ends can only be held together from here -- and holding them against a second
    copy of the literal would hold nothing. A mismatch leaves the run's live Modal environments
    behind in silence, because a sweep that selects nothing exits 0."""
    match = _CI_ENVIRONMENT_PREFIX_PATTERN.search(read_scheduled_workflow_text())

    assert match is not None, "no CI_ENVIRONMENT_PREFIX in {}; the sweep's scope is now unpinned".format(
        SCHEDULED_WORKFLOW_PATH
    )
    assert match.group(1) == CI_SWEEP_PREFIX


def test_the_sweep_prefix_the_scheduled_workflow_uses_matches_the_name_a_ci_trial_actually_creates() -> None:
    """The workflow hard-codes CI_ENVIRONMENT_PREFIX and its own comment says a mismatch leaves live
    environments behind. It would do so in silence: a sweep that selects nothing exits 0. So the name
    is built here the way a CI trial builds it -- minted prefix, driver user id, staging MNGR_PREFIX
    -- rather than typed out, and the sweep is asked to select it."""
    minted_at = datetime(2026, 8, 25, 11, 0, 0, tzinfo=timezone.utc)
    user_id = derive_user_id("todo-app__aaaaaaa", "cafe1234", format_ci_user_id_prefix(minted_at))
    environment_name = derive_modal_environment_name({"MNGR_PREFIX": STAGING_MNGR_PREFIX}, user_id)

    assert environment_name.startswith(CI_SWEEP_PREFIX)
    assert select_sweepable_environment_names(
        [environment_name, DEVELOPER_ENVIRONMENT_NAME], CI_SWEEP_PREFIX, resolve_sweep_cutoff(24.0, _NOW)
    ) == (environment_name,)


def test_select_sweepable_environment_names_takes_only_the_scheduled_runs_that_are_old_enough() -> None:
    environment_names = [
        DEVELOPER_ENVIRONMENT_NAME,
        _RECENT_CI_ENVIRONMENT,
        _OLD_CI_ENVIRONMENT,
        _UNRELATED_ENVIRONMENT,
    ]

    selected = select_sweepable_environment_names(environment_names, CI_SWEEP_PREFIX, resolve_sweep_cutoff(24.0, _NOW))

    assert selected == (_OLD_CI_ENVIRONMENT,)


def test_select_sweepable_environment_names_refuses_a_prefix_too_broad_to_be_a_scheduled_runs() -> None:
    """The `ci-` requirement bounds what an operator can point the sweep at. It is not what keeps a
    developer's environment out -- see the test below for that."""
    with pytest.raises(CleanupScopeError, match="ci-"):
        select_sweepable_environment_names(
            [DEVELOPER_ENVIRONMENT_NAME], "minds-staging-evals-", resolve_sweep_cutoff(24.0, _NOW)
        )


def test_select_swept_environment_names_refuses_a_broad_prefix_before_it_asks_modal_anything() -> None:
    """An over-broad prefix is inadmissible whatever the workspace holds, so the refusal must not
    cost a listing -- and must not need Modal credentials to be reached at all."""
    admin = MockModalEnvironmentAdmin(environment_names=[DEVELOPER_ENVIRONMENT_NAME, _OLD_CI_ENVIRONMENT])

    with pytest.raises(CleanupScopeError, match="ci-"):
        select_swept_environment_names(admin, "minds-staging-evals-", 24.0, _NOW)

    assert admin.listing_count == 0


@pytest.mark.parametrize("older_than_hours", [0.0, -1.0])
def test_select_swept_environment_names_refuses_an_age_that_would_take_a_running_batch(
    older_than_hours: float,
) -> None:
    """The cutoff is `now - age`, so an age of zero or less selects every stamped name under the
    prefix -- the environments a batch running right now just created among them. Refused from the
    request alone, beside the prefix guard and before the workspace is listed, for the reason that
    one is: a guard reachable only past a Modal round trip needs Modal credentials to be reached."""
    admin = MockModalEnvironmentAdmin(environment_names=[_RECENT_CI_ENVIRONMENT, _OLD_CI_ENVIRONMENT])

    with pytest.raises(CleanupScopeError, match="greater than 0"):
        select_swept_environment_names(admin, CI_SWEEP_PREFIX, older_than_hours, _NOW)

    assert admin.listing_count == 0


def test_select_sweepable_environment_names_spares_a_developer_run_of_a_ci_prefixed_case() -> None:
    """A prefix match does not prove a scheduled run made the environment: the trial name follows the
    eval namespace directly, so a developer's run of a case whose id starts with `ci-` lands under
    the CI sweep prefix with no user_id_prefix at all. The embedded timestamp is what excludes it,
    which is why that check is the fence and not a convenience."""
    developer_environment = STAGING_MNGR_PREFIX + derive_user_id("ci-cd-dashboard__abc1234", "cafe1234", "")

    assert developer_environment.startswith(CI_SWEEP_PREFIX)
    assert select_sweepable_environment_names(
        [developer_environment, _OLD_CI_ENVIRONMENT], CI_SWEEP_PREFIX, resolve_sweep_cutoff(8.0, _NOW)
    ) == (_OLD_CI_ENVIRONMENT,)


@pytest.mark.parametrize(
    "unageable",
    [
        # The pattern accepts any eight digits, a `t`, and six more, so a name can carry something
        # that looks like a stamp and is not a date.
        pytest.param("minds-staging-evals-ci-99999999t999999z-todo-app-cafe1234", id="not-a-real-moment"),
        pytest.param("minds-staging-evals-ci-not-a-timestamp-cafe1234", id="no-stamp-at-all"),
    ],
)
def test_select_sweepable_environment_names_skips_a_name_it_cannot_age(
    unageable: str, captured_log_messages: list[str]
) -> None:
    """A name under the ci- prefix with no age to judge by has to be skipped, not raised over from
    the middle of a destructive pass and not deleted on the prefix alone -- which would take out a
    run that is still going. Skipped out loud, though: an environment nobody is told about is one
    that stays up and keeps costing, and this pass is the only thing that would ever have said so."""
    assert parse_ci_timestamp(unageable) is None

    selected = select_sweepable_environment_names(
        [unageable, _OLD_CI_ENVIRONMENT], CI_SWEEP_PREFIX, resolve_sweep_cutoff(24.0, _NOW)
    )

    assert selected == (_OLD_CI_ENVIRONMENT,)
    assert any(unageable in message for message in captured_log_messages)


def test_select_sweepable_environment_names_keeps_everything_when_nothing_is_old_enough() -> None:
    selected = select_sweepable_environment_names(
        [_RECENT_CI_ENVIRONMENT, _OLD_CI_ENVIRONMENT], CI_SWEEP_PREFIX, resolve_sweep_cutoff(24 * 365.0, _NOW)
    )

    assert selected == ()


def test_read_job_environment_names_takes_exactly_what_the_jobs_own_trials_recorded(tmp_path: Path) -> None:
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa")
    write_trial_dir(job_dir, "greeting__bbbbbbb", case_id="greeting")
    # A trial that never wrote its state contributes no name rather than an empty one.
    write_trial_dir(job_dir, "roadmap__ccccccc", case_id="roadmap", is_state_written=False)

    assert read_job_environment_names(job_dir) == (
        expected_modal_environment_name("greeting__bbbbbbb"),
        expected_modal_environment_name("todo-app__aaaaaaa"),
    )


def test_read_job_environment_names_yields_nothing_for_a_job_that_never_got_a_trial(tmp_path: Path) -> None:
    """harbor makes the job directory before it makes any trial, so a run that died at task load
    leaves an empty one. The run gate refuses that directory; cleanup has nothing to do with it."""
    empty_job_dir = tmp_path / "died-at-load"
    empty_job_dir.mkdir()

    assert read_job_environment_names(empty_job_dir) == ()


def test_read_job_environment_names_still_refuses_a_job_directory_that_is_not_there(tmp_path: Path) -> None:
    with pytest.raises(JobReadError, match="not a job directory"):
        read_job_environment_names(tmp_path / "never-created")


def test_read_job_environment_names_keeps_the_readable_trials_when_one_is_corrupt(tmp_path: Path) -> None:
    """The run gate refuses a job it cannot parse, because an unjudged run must never read as a pass.
    Cleanup cannot inherit that: a trial whose state was truncated by the very crash being cleaned up
    after would otherwise take every other trial's environment down with it, and no sweep reaches
    those -- the backstop's cutoff is hours older than the run that just created them."""
    job_dir = tmp_path / "died-hard"
    write_trial_dir(job_dir, "todo-app__aaaaaaa")
    write_trial_dir(job_dir, "greeting__bbbbbbb", case_id="greeting")
    (job_dir / "greeting__bbbbbbb" / "agent" / "state.json").write_text('{"case_name": "greet')

    assert read_job_environment_names(job_dir) == (expected_modal_environment_name("todo-app__aaaaaaa"),)


def test_select_swept_environment_names_narrows_the_whole_workspace_to_the_finished_ci_runs() -> None:
    """The backstop sweep as the CLI wires it: everything the workspace holds, narrowed by prefix,
    stamp and age in one call."""
    admin = MockModalEnvironmentAdmin(
        environment_names=[
            DEVELOPER_ENVIRONMENT_NAME,
            _RECENT_CI_ENVIRONMENT,
            _OLD_CI_ENVIRONMENT,
            _UNRELATED_ENVIRONMENT,
        ]
    )

    assert select_swept_environment_names(admin, CI_SWEEP_PREFIX, 24.0, _NOW) == (_OLD_CI_ENVIRONMENT,)


def test_run_cleanup_deletes_the_selection_and_reports_it() -> None:
    admin = MockModalEnvironmentAdmin(environment_names=[_OLD_CI_ENVIRONMENT, DEVELOPER_ENVIRONMENT_NAME])

    report = run_cleanup(admin, [_OLD_CI_ENVIRONMENT], is_dry_run=False)

    assert report is not None
    assert report.deleted_names == (_OLD_CI_ENVIRONMENT,)
    assert admin.environment_names == [DEVELOPER_ENVIRONMENT_NAME]


def test_run_cleanup_touches_nothing_on_a_dry_run() -> None:
    """The recipe a developer is told to run first. It has to be provably incapable of deleting."""
    admin = MockModalEnvironmentAdmin(environment_names=[_OLD_CI_ENVIRONMENT])

    assert run_cleanup(admin, [_OLD_CI_ENVIRONMENT], is_dry_run=True) is None
    assert admin.deletion_attempts == []
    assert admin.environment_names == [_OLD_CI_ENVIRONMENT]


def test_run_cleanup_reaches_the_workspace_for_nothing_when_there_is_nothing_to_delete() -> None:
    admin = MockModalEnvironmentAdmin(environment_names=[_OLD_CI_ENVIRONMENT])

    assert run_cleanup(admin, [], is_dry_run=False) is None
    assert admin.deletion_attempts == []


def test_run_cleanup_reports_a_refusal_so_the_caller_can_exit_nonzero() -> None:
    """The scheduled job's cleanup step gates on this: an environment Modal would not remove is real
    money still running, and has to reach the exit code rather than a log line."""
    admin = MockModalEnvironmentAdmin(
        environment_names=[_OLD_CI_ENVIRONMENT], undeletable_names=frozenset({_OLD_CI_ENVIRONMENT})
    )

    report = run_cleanup(admin, [_OLD_CI_ENVIRONMENT], is_dry_run=False)

    assert report is not None
    assert report.failed_names == (_OLD_CI_ENVIRONMENT,)


def test_delete_environments_removes_each_name_and_reports_what_it_did() -> None:
    admin = MockModalEnvironmentAdmin(environment_names=[_OLD_CI_ENVIRONMENT, DEVELOPER_ENVIRONMENT_NAME])

    report = delete_environments(admin, [_OLD_CI_ENVIRONMENT])

    assert report.deleted_names == (_OLD_CI_ENVIRONMENT,)
    assert report.already_gone_names == ()
    assert report.failed_names == ()
    assert admin.environment_names == [DEVELOPER_ENVIRONMENT_NAME]


def test_delete_environments_tolerates_an_environment_that_is_already_gone() -> None:
    admin = MockModalEnvironmentAdmin(environment_names=[])

    report = delete_environments(admin, [_OLD_CI_ENVIRONMENT, _RECENT_CI_ENVIRONMENT])

    assert report.already_gone_names == (_OLD_CI_ENVIRONMENT, _RECENT_CI_ENVIRONMENT)
    assert report.failed_names == ()


def test_delete_environments_reports_a_refusal_rather_than_swallowing_it() -> None:
    admin = MockModalEnvironmentAdmin(
        environment_names=[_OLD_CI_ENVIRONMENT, _RECENT_CI_ENVIRONMENT],
        undeletable_names=frozenset({_OLD_CI_ENVIRONMENT}),
    )

    report = delete_environments(admin, [_OLD_CI_ENVIRONMENT, _RECENT_CI_ENVIRONMENT])

    assert report.failed_names == (_OLD_CI_ENVIRONMENT,)
    assert report.deleted_names == (_RECENT_CI_ENVIRONMENT,)
    # One refusal must not end the pass: the environments after it still get their attempt.
    assert admin.deletion_attempts == [_OLD_CI_ENVIRONMENT, _RECENT_CI_ENVIRONMENT]
