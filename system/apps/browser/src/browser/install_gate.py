"""Whether the browser's deferred install failed in this boot, read from env-converge's own record of its units.

Fortress (and, for headful runs, Xvfb) are installed after boot by the env-converge one-shot's unit
``system/scripts/env.d/1000-playwright-fortress.sh``, which env-converge runs once per boot and reports as a
``unit_run`` or ``unit_failed`` event (system/services/env_converge/src/env_converge/events.py). Until the engine is
there, a browser that cannot launch is either still being set up or will not be in this boot; only that unit's result in
this boot tells the two apart, and its exit code and output are what an agent asked to fix it starts from.
"""

import json
import os
from collections.abc import Iterable
from datetime import datetime, timezone
from enum import auto
from pathlib import Path
from typing import Final

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from loguru import logger
from pydantic import Field

FORTRESS_UNIT: Final[str] = "1000-playwright-fortress.sh"
_FORTRESS_UNIT_PATH: Final[str] = f"system/scripts/env.d/{FORTRESS_UNIT}"
_UNIT_RUN_EVENT: Final[str] = "unit_run"
_UNIT_FAILED_EVENT: Final[str] = "unit_failed"
# env-converge's events, under the host directory (env_converge.events.default_events_path).
_EVENTS_RELATIVE_PATH: Final[Path] = Path("plugin") / "env-converge" / "events" / "env_converge" / "events.jsonl"
# supervisord writes its pid file when it starts, so its age is this boot's: a unit result older than it is a
# previous boot's, whose failure this boot's env-converge pass may yet undo.
_SUPERVISORD_PID_FILE: Final[Path] = Path("/var/run/supervisord.pid")

INSTALLING_MESSAGE: Final[str] = "The browser is still being set up in this workspace. Try again in a minute."
INSTALL_FAILED_MESSAGE: Final[str] = "The browser couldn't be set up in this workspace. Ask an agent to fix it."
_INSTALL_FAILED_STEM: Final[str] = "The browser couldn't be set up in this workspace"


class InstallState(UpperCaseStrEnum):
    READY = auto()
    INSTALLING = auto()
    FAILED = auto()


class UnitFailure(FrozenModel):
    """env-converge's record of a unit that failed."""

    exit_code: int | None = Field(description="The unit's exit status (-1 if it could not run); None if unrecorded")
    stderr_tail: str = Field(description="The end of what the unit wrote to stderr; '' if unrecorded")


class AgentFix(FrozenModel):
    """What "Ask an agent to fix this" hands an agent: the app contract's ``AgentFixRequest``."""

    what_failed: str = Field(description="A sentence stem saying what failed")
    details: str = Field(description="What went wrong underneath it, verbatim")


class InstallGate(FrozenModel):
    """Whether a browser can launch, and if not, what the caller and the person are told."""

    state: InstallState = Field(description="Ready, still installing, or failed in this boot")
    reason: str = Field(description="What is missing, for agents and logs; 'ready' when nothing is")
    user_facing_message: str = Field(description="What a person is told; '' when ready")
    agent_fix: AgentFix | None = Field(default=None, description="What an agent is asked to fix; None if not for one")

    @property
    def is_ready(self) -> bool:
        return self.state == InstallState.READY


@pure
def unit_failure_since(event_lines: Iterable[str], unit: str, since: datetime) -> UnitFailure | None:
    """``unit``'s failure, if its last result in env-converge's event lines is a failure recorded at or after
    ``since``. A line that is not an event of that shape is skipped."""
    last_failure: UnitFailure | None = None
    last_failed_at: datetime | None = None
    for line in event_lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") not in (_UNIT_RUN_EVENT, _UNIT_FAILED_EVENT):
            continue
        detail = event.get("detail")
        if not isinstance(detail, dict) or detail.get("unit") != unit:
            continue
        if event["type"] == _UNIT_RUN_EVENT:
            last_failure, last_failed_at = None, None
            continue
        try:
            failed_at = datetime.fromisoformat(str(event.get("timestamp")))
        except ValueError:
            continue
        last_failed_at = failed_at if failed_at.tzinfo is not None else failed_at.replace(tzinfo=timezone.utc)
        exit_code = detail.get("exit_code")
        stderr_tail = detail.get("stderr_tail")
        last_failure = UnitFailure(
            exit_code=exit_code if isinstance(exit_code, int) else None,
            stderr_tail=stderr_tail if isinstance(stderr_tail, str) else "",
        )
    return last_failure if last_failed_at is not None and last_failed_at >= since else None


def _boot_started_at() -> datetime | None:
    try:
        return datetime.fromtimestamp(_SUPERVISORD_PID_FILE.stat().st_mtime, timezone.utc)
    except OSError as e:
        logger.debug("Cannot tell when this boot started from {}: {}", _SUPERVISORD_PID_FILE, e)
        return None


def fortress_unit_failure_this_boot() -> UnitFailure | None:
    """How the Fortress unit failed, if env-converge ran it in this boot and it failed; None when that cannot be
    read."""
    host_dir = os.environ.get("MNGR_HOST_DIR", "")
    started_at = _boot_started_at()
    if not host_dir or started_at is None:
        return None
    events_path = Path(host_dir) / _EVENTS_RELATIVE_PATH
    try:
        lines = events_path.read_text().splitlines()
    except OSError as e:
        logger.debug("Cannot read env-converge's events at {}: {}", events_path, e)
        return None
    return unit_failure_since(lines, FORTRESS_UNIT, started_at)


@pure
def missing_install_gate(missing: str, failure: UnitFailure | None) -> InstallGate:
    """The gate for a browser whose ``missing`` part is not installed yet, or will not be in this boot because the
    install unit failed as ``failure`` says."""
    if failure is not None:
        exit_status = "" if failure.exit_code is None else f" (exit code {failure.exit_code})"
        reason = (
            f"{missing} could not be installed in this workspace: env-converge ran {_FORTRESS_UNIT_PATH} in this "
            f"boot and it failed{exit_status}; env-converge runs it again on the next boot."
        )
        if failure.stderr_tail.strip() != "":
            reason = f"{reason}\nIts stderr ended with:\n{failure.stderr_tail.rstrip()}"
        return InstallGate(
            state=InstallState.FAILED,
            reason=reason,
            user_facing_message=INSTALL_FAILED_MESSAGE,
            agent_fix=AgentFix(what_failed=_INSTALL_FAILED_STEM, details=reason),
        )
    return InstallGate(
        state=InstallState.INSTALLING,
        reason=f"{missing} is still installing in this workspace; try again in a minute.",
        user_facing_message=INSTALLING_MESSAGE,
    )


READY_GATE: Final[InstallGate] = InstallGate(state=InstallState.READY, reason="ready", user_facing_message="")
