"""Bring the stopped local agents' mngr-generated files to the workspace venv's mngr.

The boot runs it before anything starts an agent, and update-self runs it (``upgrade-stopped-agents``) once an
update that moved the venv's mngr has landed or been rolled back. A running agent is never named: ``mngr upgrade``
restarts an idle one, and restarts are the chat app's. Both run from the repo root, which every path here is
relative to.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from importlib.metadata import Distribution, EntryPoint, distributions
from pathlib import Path

from loguru import logger

WORKSPACE_VENV = Path(".venv")
# The mngr that last left every agent current, such as ``{"mngr": {"imbue-mngr": "<url>@<commit>", ...}}``.
COVERAGE_RECORD = Path("data/.state/agent_upgrade/coverage.json")

# Without it mngr refuses a settings key it does not know.
_MNGR_ENV_OVERRIDES = {"MNGR_ALLOW_UNKNOWN_CONFIG": "1"}
# The agent running this, which can never read stopped.
_CALLER_ENV_VAR = "MNGR_AGENT_ID"
_MNGR_LIST_FIELD_SEPARATOR = "|"
# The local agents as `id|state` rows; inside a workspace a template answers where `--format json` can fail.
MNGR_LIST_AGENT_STATES_ARGV = (
    "list",
    "--provider",
    "local",
    "--on-error",
    "continue",
    "--format",
    f"{{id}}{_MNGR_LIST_FIELD_SEPARATOR}{{state}}",
)
_STOPPED_STATE = "STOPPED"
_MNGR_LIST_TIMEOUT_SECONDS = 60.0
# The most a `mngr upgrade` that hangs (say, on another mngr's host lock) holds up its caller.
_MNGR_UPGRADE_TIMEOUT_SECONDS = 60.0
# `mngr upgrade` skipped an agent and none failed.
_MNGR_UPGRADE_EXIT_SKIPPED = 9
# How `mngr upgrade` starts the line for an agent it failed to upgrade, such as `failed a: prepare (...); untouched`.
_FAILED_OUTCOME_PREFIX = "failed "
# What `mngr upgrade` appends to an agent it cannot upgrade, such as ``; re-create it with `mngr create a --reuse --update` ``.
_RECREATE_HINT_PATTERN = re.compile(r"; re-create it with `[^`]*`")
# The entry point group mngr loads its plugins from.
_MNGR_PLUGIN_GROUP = "mngr"
_MNGR_COMMAND = "mngr"


def _is_part_of_mngr(entry_point: EntryPoint) -> bool:
    return entry_point.group == _MNGR_PLUGIN_GROUP or (
        entry_point.group == "console_scripts" and entry_point.name == _MNGR_COMMAND
    )


def _vcs_source(distribution: Distribution) -> str | None:
    """The repository and commit ``distribution`` was installed from, or None when it came from anywhere else."""
    try:
        direct_url = json.loads(distribution.read_text("direct_url.json") or "null")
    except ValueError:
        return None
    if not isinstance(direct_url, dict):
        return None
    vcs_info = direct_url.get("vcs_info")
    commit = vcs_info.get("commit_id") if isinstance(vcs_info, dict) else None
    if not isinstance(commit, str) or not commit:
        return None
    return f"{direct_url.get('url')}@{commit}"


def installed_mngr(venv: Path) -> dict[str, str] | None:
    """The commit each of ``venv``'s mngr packages (mngr and its plugins) was installed from, by package; None
    when one was not installed from a commit, so its files could change under the same metadata."""
    site_packages = [str(path) for path in venv.glob("lib/python*/site-packages")]
    sources: dict[str, str] = {}
    for distribution in distributions(path=site_packages):
        if not any(
            _is_part_of_mngr(entry_point) for entry_point in distribution.entry_points
        ):
            continue
        source = _vcs_source(distribution)
        if source is None:
            return None
        sources[distribution.name] = source
    return sources or None


def _recorded_mngr(record: Path) -> object:
    try:
        recorded = json.loads(record.read_text())
    except (OSError, ValueError):
        return None
    return recorded.get("mngr") if isinstance(recorded, dict) else None


def _remove_record(record: Path) -> None:
    try:
        record.unlink(missing_ok=True)
    except OSError as e:
        logger.warning("Could not remove {} ({})", record, e)


def _write_record(record: Path, mngr: Mapping[str, str]) -> None:
    staged = record.with_name(f"{record.name}.tmp")
    try:
        record.parent.mkdir(parents=True, exist_ok=True)
        staged.write_text(json.dumps({"mngr": mngr}, indent=2, sort_keys=True) + "\n")
        os.replace(staged, record)
    except OSError as e:
        logger.warning("Could not record that every agent is current ({})", e)
        return
    logger.info("Every agent is current with this mngr; recorded in {}", record)


def _run_mngr(
    mngr_binary: Path, args: Sequence[str], timeout: float
) -> subprocess.CompletedProcess[str] | None:
    """Run ``mngr_binary`` with ``args``; None, with a warning, when it could not run or timed out."""
    try:
        return subprocess.run(
            [str(mngr_binary), *args],
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
            stdin=subprocess.DEVNULL,
            env={**os.environ, **_MNGR_ENV_OVERRIDES},
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        _log_mngr_output(args[0], _decoded_output(e.stdout))
        logger.warning("mngr {} could not run ({})", args[0], e)
        return None
    except OSError as e:
        logger.warning("mngr {} could not run ({})", args[0], e)
        return None


def _decoded_output(output: bytes | str | None) -> str:
    """Captured output as text; a timed-out run leaves it as undecoded bytes."""
    if isinstance(output, bytes):
        return output.decode(errors="replace")
    return output or ""


def _log_mngr_output(subcommand: str, stdout: str) -> None:
    """Log each line ``mngr <subcommand>`` printed, without the re-create command it suggests; a failed agent's line is a warning."""
    for line in stdout.splitlines():
        if line.strip():
            log = (
                logger.warning
                if line.startswith(_FAILED_OUTCOME_PREFIX)
                else logger.info
            )
            log("mngr {}: {}", subcommand, _RECREATE_HINT_PATTERN.sub("", line))


def _list_agent_states(mngr_binary: Path) -> tuple[dict[str, str], bool]:
    """Each local agent's state by id, from whatever ``mngr list`` listed, and whether it listed every agent."""
    result = _run_mngr(
        mngr_binary, MNGR_LIST_AGENT_STATES_ARGV, _MNGR_LIST_TIMEOUT_SECONDS
    )
    if result is None:
        return {}, False
    if result.returncode != 0:
        logger.warning(
            "mngr list exited {}: {}", result.returncode, result.stderr.strip()[-500:]
        )
    rows = (
        [column.strip() for column in line.split(_MNGR_LIST_FIELD_SEPARATOR)]
        for line in result.stdout.splitlines()
    )
    return {
        row[0]: row[1] for row in rows if len(row) == 2 and row[0]
    }, result.returncode == 0


def _upgrade(mngr_binary: Path, agent_ids: Sequence[str]) -> bool:
    """Run ``mngr upgrade`` on ``agent_ids``; whether it finished with none failed."""
    result = _run_mngr(
        mngr_binary,
        ["upgrade", *agent_ids, "--on-error", "continue"],
        _MNGR_UPGRADE_TIMEOUT_SECONDS,
    )
    if result is None:
        return False
    _log_mngr_output("upgrade", result.stdout)
    if result.returncode in (0, _MNGR_UPGRADE_EXIT_SKIPPED):
        return True
    stderr_tail = _RECREATE_HINT_PATTERN.sub("", result.stderr.strip()[-500:])
    logger.warning(
        "mngr upgrade exited {}{}",
        result.returncode,
        f": {stderr_tail}" if stderr_tail else "",
    )
    return False


def upgrade_stopped_agents(
    venv: Path = WORKSPACE_VENV, record: Path = COVERAGE_RECORD
) -> None:
    """Rewrite each stopped local agent's mngr-generated files with ``venv``'s mngr, leaving it stopped, unless
    ``record`` names that mngr. Best-effort.

    ``record`` names it only after a pass that left every agent current: the listing succeeded, every agent but
    the caller read stopped, and ``mngr upgrade`` neither failed nor timed out. Any other pass removes it.
    """
    mngr = installed_mngr(venv)
    if mngr is None:
        logger.info(
            "The venv's mngr was not installed from a commit, so no record says its agents are current"
        )
    elif _recorded_mngr(record) == mngr:
        logger.info("Every agent is already current with this mngr")
        return
    _remove_record(record)
    mngr_binary = venv / "bin" / _MNGR_COMMAND
    states, is_listing_complete = _list_agent_states(mngr_binary)
    stopped_ids = [
        agent_id for agent_id, state in states.items() if state == _STOPPED_STATE
    ]
    if stopped_ids:
        is_upgrade_complete = _upgrade(mngr_binary, stopped_ids)
    else:
        logger.info("No stopped agents to upgrade")
        is_upgrade_complete = True
    caller = os.environ.get(_CALLER_ENV_VAR, "")
    is_every_other_agent_stopped = all(
        state == _STOPPED_STATE or agent_id == caller
        for agent_id, state in states.items()
    )
    if (
        mngr is not None
        and is_listing_complete
        and is_every_other_agent_stopped
        and is_upgrade_complete
    ):
        _write_record(record, mngr)


def main() -> None:
    """``upgrade-stopped-agents``: the pass, run from the repo root, with each line it logs on stderr."""
    argparse.ArgumentParser(
        prog="upgrade-stopped-agents",
        description="Rewrite every stopped local agent's mngr-generated files with the workspace venv's mngr, "
        "unless every agent is already current with it. Run from the workspace root; takes no arguments.",
    ).parse_args()
    logger.remove()
    logger.add(sys.stderr, format="{message}", level="INFO")
    upgrade_stopped_agents()
