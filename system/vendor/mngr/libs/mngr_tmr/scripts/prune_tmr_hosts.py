"""Destroy the hosts that past TMR runs left behind.

TMR never destroys the hosts it creates: they idle-shut-down but keep their
records (and, on modal, their volumes and sandboxes) forever. The records
accumulate on the provider's shared state volume, and every subsequent host
creation reads all of them to check name uniqueness, until at a few thousand
records host creation fails outright.

Each variant's most recent run is kept, so its mappers stay available to
re-attach to for debugging. Everything older is destroyed, together with the
host records, so the state volume stays small.

Run from the repo root:

    uv run --project libs/mngr_tmr python libs/mngr_tmr/scripts/prune_tmr_hosts.py --dry-run

CI runs this daily against the shared tmr-ci namespace
(.github/workflows/tmr-cleanup.yml); run it by hand with MNGR_HOST_DIR
pointed at the host dir that setup_tmr_ci_debug.py creates.
"""

import argparse
import json
import sys
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

from loguru import logger
from pydantic import ValidationError

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.subprocess_utils import FinishedProcess
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr_mapreduce.cli import RUN_NAME_LABEL_KEY
from imbue.mngr_tmr.host_pruning import group_agents_by_run
from imbue.mngr_tmr.host_pruning import select_prunable_run_names

_DEFAULT_PROVIDER = "modal"
_DEFAULT_KEPT_RUN_COUNT_PER_VARIANT = 1
# A TMR run is capped at four hours, so a run younger than this is treated as
# possibly still going and is left alone even when a newer run exists.
_DEFAULT_MINIMUM_PRUNABLE_AGE_HOURS = 12.0
_LIST_TIMEOUT_SECONDS = 30.0 * 60.0
_CLEANUP_TIMEOUT_SECONDS = 3.0 * 60.0 * 60.0


class PruneTmrHostsError(MngrError, RuntimeError):
    """Raised when a mngr invocation this script depends on could not be completed."""

    ...


def _mngr_command() -> list[str]:
    """The mngr entry point of the environment this script runs in."""
    executable = Path(sys.executable).with_name("mngr")
    if executable.exists():
        return [str(executable)]
    return ["mngr"]


def _run_mngr(arguments: list[str], timeout_seconds: float, cg: ConcurrencyGroup) -> FinishedProcess:
    command = [*_mngr_command(), *arguments]
    logger.info("Running: {}", " ".join(command))
    result = cg.run_process_to_completion(command, timeout=timeout_seconds, is_checked_after=False)
    if result.is_timed_out:
        raise PruneTmrHostsError(f"mngr {arguments[0]} timed out after {timeout_seconds:.0f}s")
    return result


def _list_agents(provider: str, cg: ConcurrencyGroup) -> tuple[AgentDetails, ...]:
    """List every agent the provider knows about, including those on stopped hosts.

    ``mngr list`` exits non-zero when an individual provider fails but still
    prints the agents it did find, so the output is parsed regardless of the
    exit code and only an unparseable one is an error.
    """
    result = _run_mngr(["list", "--provider", provider, "--format", "json"], _LIST_TIMEOUT_SECONDS, cg)
    if not result.stdout.strip():
        raise PruneTmrHostsError(f"mngr list produced no output (exit code {result.returncode}): {result.stderr}")
    try:
        listed = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise PruneTmrHostsError(f"mngr list produced invalid JSON: {e}") from e
    try:
        return tuple(AgentDetails.model_validate(agent) for agent in listed.get("agents", []))
    except ValidationError as e:
        raise PruneTmrHostsError(f"mngr list produced JSON with an unexpected schema: {e}") from e


def _destroy_runs(provider: str, prunable_run_names: tuple[str, ...], is_dry_run: bool, cg: ConcurrencyGroup) -> None:
    """Destroy the agents of the given runs, and with them the hosts they sit on."""
    run_name_list = ", ".join(json.dumps(run_name) for run_name in prunable_run_names)
    arguments = [
        "cleanup",
        "--provider",
        provider,
        "--include",
        f"labels.{RUN_NAME_LABEL_KEY} in [{run_name_list}]",
        "--yes",
        *_record_purging_settings(provider),
    ]
    if is_dry_run:
        arguments.append("--dry-run")
    result = _run_mngr(arguments, _CLEANUP_TIMEOUT_SECONDS, cg)
    logger.info("mngr cleanup reported:\n{}", result.stdout)
    if result.returncode != 0:
        raise PruneTmrHostsError(f"mngr cleanup failed (exit code {result.returncode}): {result.stderr}")


def _collect_garbage(provider: str, is_dry_run: bool, cg: ConcurrencyGroup) -> None:
    """Delete the records and unused resources the destroyed hosts left behind."""
    arguments = ["gc", "--provider", provider, "--on-error", "continue", *_record_purging_settings(provider)]
    if is_dry_run:
        arguments.append("--dry-run")
    result = _run_mngr(arguments, _CLEANUP_TIMEOUT_SECONDS, cg)
    logger.info("mngr gc reported:\n{}", result.stdout)
    if result.returncode != 0:
        raise PruneTmrHostsError(f"mngr gc failed (exit code {result.returncode}): {result.stderr}")


def _record_purging_settings(provider: str) -> list[str]:
    """Settings that make gc delete a destroyed host's record straight away.

    The default keeps records for a week so a mistaken destroy stays visible in
    ``mngr list``. Here the destroy is the deliberate point of the run, and it is
    the sheer number of retained records that slows host creation down, so the
    records go in the same sweep.
    """
    return ["-S", f"providers.{provider}.destroyed_host_persisted_seconds=0"]


def prune_hosts(
    provider: str,
    kept_run_count_per_variant: int,
    minimum_prunable_age: timedelta,
    is_dry_run: bool,
    cg: ConcurrencyGroup,
) -> None:
    # Work out which runs may go, and report every run either way so the log
    # shows what was kept as well as what was pruned.
    now = datetime.now(tz=timezone.utc)
    runs = group_agents_by_run(_list_agents(provider, cg))
    prunable_run_names = select_prunable_run_names(
        runs,
        now=now,
        kept_run_count_per_variant=kept_run_count_per_variant,
        minimum_prunable_age=minimum_prunable_age,
    )
    logger.info("Found {} run(s) on provider {!r}", len(runs), provider)
    for run in runs:
        age_hours = (now - run.latest_agent_create_time).total_seconds() / 3600.0
        disposition = "prune" if run.run_name in prunable_run_names else "keep"
        logger.info(
            "[{}] {} {}: {} host(s), {} agent(s), {:.1f}h old",
            disposition,
            run.variant,
            run.run_name,
            len(run.host_names),
            run.agent_count,
            age_hours,
        )

    # Destroy the prunable runs, then sweep up the records and any host that
    # never received an agent.
    if prunable_run_names:
        _destroy_runs(provider, prunable_run_names, is_dry_run, cg)
    else:
        logger.info("No runs are old enough to prune")
    _collect_garbage(provider, is_dry_run, cg)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--provider",
        default=_DEFAULT_PROVIDER,
        help=f"Provider whose hosts to prune [default: {_DEFAULT_PROVIDER}]",
    )
    parser.add_argument(
        "--keep-runs",
        type=int,
        default=_DEFAULT_KEPT_RUN_COUNT_PER_VARIANT,
        help=f"How many recent runs of each variant to keep [default: {_DEFAULT_KEPT_RUN_COUNT_PER_VARIANT}]",
    )
    parser.add_argument(
        "--min-age-hours",
        type=float,
        default=_DEFAULT_MINIMUM_PRUNABLE_AGE_HOURS,
        help=f"Never prune a run younger than this [default: {_DEFAULT_MINIMUM_PRUNABLE_AGE_HOURS}]",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be destroyed without destroying anything",
    )
    args = parser.parse_args()

    with ConcurrencyGroup(name="prune-tmr-hosts") as cg:
        prune_hosts(
            provider=args.provider,
            kept_run_count_per_variant=args.keep_runs,
            minimum_prunable_age=timedelta(hours=args.min_age_hours),
            is_dry_run=args.dry_run,
            cg=cg,
        )


if __name__ == "__main__":
    main()
