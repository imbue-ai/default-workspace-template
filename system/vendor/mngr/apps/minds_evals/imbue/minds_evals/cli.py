"""The `minds-evals` command line: generate a dataset, check a finished run, clean up after one.

Each command is a thin front end over the module that owns the work (`generate`, `check_run`,
`cleanup_environments`), so the exit codes and the option shapes a scheduled run depends on live in
one place while the logic stays importable without click.
"""

from datetime import datetime
from datetime import timezone
from pathlib import Path

import click
from loguru import logger

from imbue.imbue_common.logging import setup_logging
from imbue.minds_evals import check_run
from imbue.minds_evals import cleanup_environments
from imbue.minds_evals.errors import CleanupScopeError
from imbue.minds_evals.generate import MNGR_REPO
from imbue.minds_evals.generate import generate_dataset


@click.group()
def main() -> None:
    """Run and inspect the Minds persona evals."""
    setup_logging(level="INFO")


@main.command()
@click.option(
    "--config",
    "config_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help=(
        "Eval config json: {mngr_branch, dwt_repo?, dwt_branch?, timeout_seconds?, "
        "verification_timeout_seconds?, personas:[...]}"
    ),
)
@click.option(
    "--output",
    "output_dir",
    required=True,
    type=click.Path(file_okay=False, path_type=Path),
    help="Dataset directory to create (one harbor task subdirectory per persona case)",
)
@click.option(
    "--mngr-repo",
    default=MNGR_REPO,
    show_default=True,
    help="The mngr remote the box source is fetched from",
)
@click.option(
    "--mngr-ref",
    "mngr_ref",
    default=None,
    help="Override the config's mngr_branch: a branch, tag, or full SHA to build the box from",
)
@click.option(
    "--dwt-ref",
    "dwt_ref",
    default=None,
    help="Override the config's dwt_branch: a branch, tag, or full SHA for the workspace template",
)
def generate(config_path: Path, output_dir: Path, mngr_repo: str, mngr_ref: str | None, dwt_ref: str | None) -> None:
    """Generate one harbor task per persona case from an eval config."""
    task_dirs = generate_dataset(
        config_path=config_path,
        output_dir=output_dir,
        mngr_repo=mngr_repo,
        mngr_ref=mngr_ref,
        dwt_ref=dwt_ref,
    )
    logger.info("Generated {} task(s) in {}", len(task_dirs), output_dir)
    logger.info(
        "Run them from the monorepo root with: uv run --project apps/minds_evals harbor run "
        "-p {} -a imbue.minds_evals.driver:MindsPersonaDriver -e modal -y",
        output_dir,
    )


@main.command("check-run")
@click.argument("job_dir", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option(
    "--summary-md",
    "summary_md_path",
    default=None,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Write a GitHub step-summary markdown table of the run here",
)
@click.option(
    "--summary-json",
    "summary_json_path",
    default=None,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Write the machine-readable run check (per-trial rows, verdict, Modal environments) here",
)
def check_run_command(job_dir: Path, summary_md_path: Path | None, summary_json_path: Path | None) -> None:
    """Decide whether a finished harbor job passed, and exit non-zero when it did not.

    A trial passes when it ran to the end, its structural gates held, nothing in its evidence bundle
    went unmeasured, and it is not recorded as having answered on a model other than the one its
    harness config asked for. Judge scores are reported and never gated.
    """
    result = check_run.check_job_directory(job_dir)
    check_run.write_run_check_reports(result, summary_md_path, summary_json_path)
    for trial in result.trials:
        if not trial.is_passed:
            logger.error(
                "Trial {} failed: completed={} ({}) gates={} errored evidence={} arm={}",
                trial.trial_name,
                trial.is_completed,
                trial.incompletion_reason or "ran to the end",
                trial.is_gates_passed,
                ", ".join(trial.error_entry_ids) or "none",
                trial.wrong_model_reason or "no wrong model recorded",
            )
    logger.info(
        "{}: {} of {} trial(s) passed",
        result.job_name,
        sum(1 for trial in result.trials if trial.is_passed),
        len(result.trials),
    )
    if not result.is_passed:
        raise SystemExit(1)


@main.command("cleanup-environments")
@click.option(
    "--job-dir",
    "job_dir",
    default=None,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Delete exactly the Modal environments this job's own trials recorded, and nothing else",
)
@click.option(
    "--sweep-prefix",
    default="",
    help=(
        "Instead of a job directory, delete every environment whose name starts with this prefix and "
        "whose embedded ci- timestamp is older than --older-than-hours. The prefix must contain "
        "'ci-', so the sweep cannot reach an environment a developer run created"
    ),
)
@click.option(
    "--older-than-hours",
    "older_than_hours",
    default=None,
    type=float,
    help="Required with --sweep-prefix: how old an environment must be before the sweep removes it",
)
@click.option(
    "--dry-run/--no-dry-run",
    default=False,
    help="List what would be deleted without deleting anything",
)
def cleanup_environments_command(
    job_dir: Path | None,
    sweep_prefix: str,
    older_than_hours: float | None,
    dry_run: bool,
) -> None:
    """Delete the Modal environments an eval run left behind.

    A trial never destroys its own environment: it is where a person debugging that trial recovers
    the workspace's state from. Pass --job-dir to remove the ones your own run created.
    """
    run_cleanup_environments(
        cleanup_environments.ModalSdkEnvironmentAdmin(),
        job_dir=job_dir,
        sweep_prefix=sweep_prefix,
        older_than_hours=older_than_hours,
        is_dry_run=dry_run,
    )


def run_cleanup_environments(
    admin: cleanup_environments.ModalEnvironmentAdminInterface,
    *,
    job_dir: Path | None,
    sweep_prefix: str,
    older_than_hours: float | None,
    is_dry_run: bool,
) -> None:
    """Everything `cleanup-environments` decides, against a given Modal workspace.

    Split from the command so the two irreversible decisions -- which environments the sweep selects,
    and whether a deletion Modal refused fails the pass -- can be driven against a stand-in admin.
    Deleting is what this CLI does that cannot be undone, and the scheduled job's cleanup step reads
    the exit code, so both raise from here rather than being reported to the command.

    Raises click.UsageError for a request whose scope does not make sense and SystemExit(1) when any
    deletion was refused.
    """
    is_job_scoped = job_dir is not None
    is_sweep_scoped = bool(sweep_prefix)
    if is_job_scoped == is_sweep_scoped:
        raise click.UsageError("pass exactly one of --job-dir and --sweep-prefix")
    if is_job_scoped and older_than_hours is not None:
        # A job-scoped deletion takes exactly what the job recorded, so an age here would be
        # discarded. Refused rather than ignored: silently dropping it reads as "only the old ones
        # went".
        raise click.UsageError("--older-than-hours applies to --sweep-prefix, not --job-dir")
    if job_dir is not None:
        environment_names = cleanup_environments.read_job_environment_names(job_dir)
    else:
        # Demanded rather than defaulted: an age this command chose for the caller would silently
        # decide how much of a still-running scheduled batch the sweep takes out.
        if older_than_hours is None:
            raise click.UsageError("--sweep-prefix needs --older-than-hours")
        try:
            environment_names = cleanup_environments.select_swept_environment_names(
                admin, sweep_prefix, older_than_hours, datetime.now(timezone.utc)
            )
        except CleanupScopeError as exc:
            # Restated as usage errors at the boundary, so the refusals that stop a destructive
            # sweep -- an over-broad prefix, an age that would reach a running batch -- read like
            # the three above them rather than as a traceback. The module keeps raising its own
            # error for importers, which is where those two guards belong: they are decided from
            # the request alone, and an importer gets them without going through click.
            raise click.UsageError(str(exc)) from exc
    report = cleanup_environments.run_cleanup(admin, environment_names, is_dry_run)
    if report is not None and report.failed_names:
        raise SystemExit(1)


@main.command("ci-user-id-prefix")
@click.option(
    "--output",
    "output_path",
    required=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help="File to write the prefix into, for the caller to read back",
)
def ci_user_id_prefix_command(output_path: Path) -> None:
    """Mint the `--ak user_id_prefix=` value a scheduled run should pass.

    The prefix is minted here rather than by whatever schedules the run because the same module
    parses it back: the `ci-` marker is what keeps the backstop sweep off a developer's
    environments, and the embedded timestamp is the age the sweep judges by. A second speller of
    that format would break the sweep silently.
    """
    prefix = cleanup_environments.format_ci_user_id_prefix(datetime.now(timezone.utc))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(prefix + "\n")
    logger.info("Wrote the CI user id prefix {} to {}", prefix, output_path)


if __name__ == "__main__":
    main()
