"""The `minds-evals` command line: generate a dataset, check a finished run, clean up after one.

Each command is a thin front end over the module that owns the work (`generate`, `check_run`,
`cleanup_environments`), so the exit codes and the option shapes a scheduled run depends on live in
one place while the logic stays importable without click.
"""

import asyncio
import json
import os
from datetime import datetime
from datetime import timezone
from pathlib import Path

import click
from loguru import logger
from pydantic import SecretStr

from imbue.imbue_common.logging import setup_logging
from imbue.minds_evals import check_run
from imbue.minds_evals import ci_matrix
from imbue.minds_evals import ci_report
from imbue.minds_evals import cleanup_environments
from imbue.minds_evals import flow_browser
from imbue.minds_evals import flow_lab
from imbue.minds_evals import ui_flows
from imbue.minds_evals.data_types import CheckStatus
from imbue.minds_evals.data_types import CiReportContext
from imbue.minds_evals.data_types import PairDecision
from imbue.minds_evals.decider import DEFAULT_DECIDER_MODEL
from imbue.minds_evals.errors import CiMatrixError
from imbue.minds_evals.errors import CleanupScopeError
from imbue.minds_evals.generate import MNGR_REPO
from imbue.minds_evals.generate import generate_dataset
from imbue.minds_evals.minds_bridge import ANTHROPIC_API_KEY_ENV_VAR
from imbue.mngr.cli.output_helpers import write_human_line


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


@main.command("ci-matrix")
@click.option(
    "--pairs",
    "pairs_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="JSON lines, one frozen (mngr, dwt) pair per line: {pair, mngr_ref, mngr_sha, dwt_ref, dwt_sha}",
)
@click.option(
    "--harness-configs",
    "harness_configs_path",
    default=ci_matrix.CHECKED_IN_HARNESS_CONFIGS_PATH,
    show_default=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="The named harness configs file",
)
@click.option(
    "--select",
    "selection",
    default="",
    help="Comma-separated harness config names to run; empty runs every config marked nightly",
)
@click.option(
    "--config",
    "config_path",
    required=True,
    help="The repo-relative eval config every cell generates its dataset from; part of each green marker key",
)
@click.option(
    "--green-markers",
    "green_markers_path",
    default=None,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="`gh cache list --json key,ref` output listing the green markers; absent means none is green",
)
@click.option(
    "--restorable-ref",
    "restorable_refs",
    multiple=True,
    help="A git ref whose cache entries this run may restore (its own ref and the default branch); repeatable",
)
@click.option(
    "--force/--no-force",
    "is_forced",
    default=False,
    help="Run every cell, green marker or not",
)
@click.option(
    "--output",
    "output_path",
    required=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Where to write the decided matrix as JSON (pairs, cells, and the two job matrices)",
)
@click.option(
    "--summary-md",
    "summary_md_path",
    default=None,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Write a GitHub step-summary markdown table of the decision here",
)
@click.option(
    "--repository",
    default="",
    help="The GitHub repository (owner/name), for the marker-listing hint in the summary",
)
def ci_matrix_command(
    pairs_path: Path,
    harness_configs_path: Path,
    selection: str,
    config_path: str,
    green_markers_path: Path | None,
    restorable_refs: tuple[str, ...],
    is_forced: bool,
    output_path: Path,
    summary_md_path: Path | None,
    repository: str,
) -> None:
    """Decide which arms a scheduled run evaluates: every frozen pair times every selected harness
    config, minus the cells whose green marker says that exact arm was already verified.

    The harness configs file is validated whole, selected or not, through the driver's own kwarg
    parsing, so a config the driver would refuse at construction is refused here on the free job.
    """
    try:
        entries = ci_matrix.load_harness_configs(harness_configs_path)
        selected = ci_matrix.select_harness_configs(entries, selection)
        pairs = ci_matrix.read_frozen_pairs(pairs_path)
        green_keys = (
            frozenset()
            if green_markers_path is None
            else ci_matrix.read_green_marker_keys(green_markers_path, restorable_refs)
        )
    except CiMatrixError as exc:
        raise click.UsageError(str(exc)) from exc
    matrix = ci_matrix.decide_matrix(
        pairs=pairs, entries=selected, config_path=config_path, green_keys=green_keys, is_forced=is_forced
    )
    ci_matrix.write_matrix_reports(matrix, output_path, summary_md_path, repository)
    for cell in matrix.cells:
        logger.info("{} x {}: {}", cell.pair, cell.harness_config, cell.decision.value)
    for pair in matrix.pairs:
        if pair.decision is not PairDecision.RUN:
            logger.info("pair {}: {}", pair.pair, pair.decision.value)


@main.command("ci-report")
@click.option(
    "--matrix",
    "matrix_path",
    default=None,
    type=click.Path(dir_okay=False, path_type=Path),
    help="The ci-matrix output; absent or unreadable means the run broke before deciding what to evaluate",
)
@click.option(
    "--summaries-dir",
    "summaries_dir",
    required=True,
    type=click.Path(file_okay=False, path_type=Path),
    help="Where the per-pair and per-cell summary artifacts were downloaded to; may not exist",
)
@click.option("--run-url", required=True, help="The workflow run's URL")
@click.option("--trigger", required=True, help="The event that started the run (schedule, workflow_dispatch, push)")
@click.option(
    "--duration-seconds",
    "duration_seconds",
    default=None,
    type=int,
    help="How long the run has been going; omitted when it could not be looked up",
)
@click.option(
    "--live-pass-skipped/--no-live-pass-skipped",
    "is_live_pass_skipped",
    default=False,
    help="Whether the run stopped after the oracle passes",
)
@click.option("--resolve-result", default="", help="The resolve job's result, as GitHub reports it")
@click.option("--oracle-result", default="", help="The oracle job's result, as GitHub reports it")
@click.option("--evaluate-result", default="", help="The evaluate job's result, as GitHub reports it")
@click.option(
    "--output",
    "output_path",
    required=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Where to write the Slack webhook payloads, as a JSON array of one payload per pair",
)
def ci_report_command(
    matrix_path: Path | None,
    summaries_dir: Path,
    run_url: str,
    trigger: str,
    duration_seconds: int | None,
    is_live_pass_skipped: bool,
    resolve_result: str,
    oracle_result: str,
    evaluate_result: str,
    output_path: Path,
) -> None:
    """Write the Slack report of a scheduled run: one webhook payload per pair, each a grid of the
    pair's cases by harness config.

    This command is the whole notification of a run, so it never fails: a matrix that cannot be read
    or a summary that is missing is reported as such, and the exit code is zero either way.
    """
    context = CiReportContext(
        run_url=run_url,
        trigger=trigger,
        duration_seconds=duration_seconds,
        is_live_pass_skipped=is_live_pass_skipped,
        resolve_result=resolve_result,
        oracle_result=oracle_result,
        evaluate_result=evaluate_result,
    )
    messages = ci_report.render_slack_report(matrix_path, summaries_dir, context)
    payloads = [ci_report.as_slack_payload(message) for message in messages]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payloads, indent=2))
    logger.info("Wrote {} run report message(s) to {}", len(payloads), output_path)


@main.command("flow-lab")
@click.option(
    "--app",
    "app_dir",
    required=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Directory served as the app's origin; a fixture under flow_lab_apps/, or an app taken out of a trial",
)
@click.option(
    "--page",
    default="",
    help="Appended to the served origin: empty for its index, or a query such as '?latency=300'",
)
@click.option("--actions", required=True, help="What the flow does in the UI, as a case would state it")
@click.option("--expect", required=True, help="The flow's end condition, recorded in the log for the judge")
@click.option(
    "--output",
    "output_dir",
    required=True,
    type=click.Path(file_okay=False, path_type=Path),
    help="Where log.jsonl and the step frames are written, shaped as a trial's flow directory",
)
@click.option(
    "--model",
    default=DEFAULT_DECIDER_MODEL,
    show_default=True,
    help="The model the verification agent reasons with",
)
def flow_lab_command(app_dir: Path, page: str, actions: str, expect: str, output_dir: Path, model: str) -> None:
    """Drive one UI flow against a local app with the real verification agent, and no box at all.

    Needs ANTHROPIC_API_KEY for the agent's calls. Exits non-zero when the flow did not complete
    its declared actions; whether the `expect` holds is not decided here, exactly as at trial time.
    """
    api_key = os.environ.get(ANTHROPIC_API_KEY_ENV_VAR, "")
    if not api_key:
        raise click.UsageError("{} is not set; the verification agent cannot be run".format(ANTHROPIC_API_KEY_ENV_VAR))
    agent = ui_flows.AnthropicVerificationAgent(
        model=model, api_key=SecretStr(api_key), timeout_seconds=ui_flows.DEFAULT_CALL_TIMEOUT_SECONDS
    )
    # Resolved before the loop starts: playwright's sync API refuses to run inside one.
    chromium_path = flow_browser.resolve_chromium_path()
    if not chromium_path.exists():
        raise click.UsageError(flow_browser.missing_chromium_message(chromium_path))
    run = asyncio.run(
        flow_lab.run_lab_flow(
            app_dir=app_dir,
            page=page,
            check=flow_lab.lab_flow_check("lab", actions, expect),
            agent=agent,
            output_dir=output_dir,
            chromium_path=chromium_path,
        )
    )
    for record in run.records:
        write_human_line(flow_lab.describe_record(record))
    usage = ui_flows.summarize_verifier_usage(tuple(agent.calls), model)
    logger.info(
        "Flow {}{}; {} agent call(s), {} input / {} output tokens; evidence in {}",
        run.status.value,
        " ({})".format(run.reason) if run.reason else "",
        usage.call_count,
        usage.input_token_count,
        usage.output_token_count,
        output_dir,
    )
    if run.status is not CheckStatus.PASSED:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
