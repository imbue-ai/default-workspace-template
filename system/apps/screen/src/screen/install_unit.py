"""Whether the env.d unit that installs the Screen failed in this boot, read from env-converge's own record of it.

The stack, its browser and neko are installed after boot by ``system/scripts/env.d/2000-screen.sh``, which the
``env-converge`` one-shot runs once per boot and records as a ``unit_run`` or ``unit_failed`` event
(system/services/env_converge/src/env_converge/events.py). Until everything is there, the Screen is either still
being set up or will not be in this boot; only that unit's result in this boot tells the two apart, and its exit code
and error output are what an agent asked to fix it starts from.
"""

import json
import os
from collections.abc import Iterable
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure

SCREEN_INSTALL_UNIT: Final[str] = "2000-screen.sh"
_SCREEN_INSTALL_UNIT_PATH: Final[str] = f"system/scripts/env.d/{SCREEN_INSTALL_UNIT}"
_UNIT_RUN_EVENT: Final[str] = "unit_run"
_UNIT_FAILED_EVENT: Final[str] = "unit_failed"
# env-converge's events, under the host directory (env_converge.events.default_events_path).
_EVENTS_RELATIVE_PATH: Final[Path] = Path("plugin") / "env-converge" / "events" / "env_converge" / "events.jsonl"
# supervisord writes its pid file when it starts, so its age is this boot's: a unit result older than it is a
# previous boot's, which this boot's env-converge pass may yet undo.
_SUPERVISORD_PID_FILE: Final[Path] = Path("/var/run/supervisord.pid")


class UnitFailure(FrozenModel):
    """env-converge's record of a unit that failed."""

    exit_code: int | None = Field(description="The unit's exit status (-1 if it could not run); None if unrecorded")
    stderr_tail: str = Field(description="The end of what the unit wrote to stderr; '' if unrecorded")


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


@pure
def describe_unit_failure(failure: UnitFailure) -> str:
    """What went wrong with the install, for "See details" and the agent asked to fix it."""
    exit_status = "" if failure.exit_code is None else f" (exit code {failure.exit_code})"
    description = (
        f"env-converge ran {_SCREEN_INSTALL_UNIT_PATH} in this boot and it failed{exit_status}; "
        "env-converge runs it again on the next boot."
    )
    if failure.stderr_tail.strip() == "":
        return description
    return f"{description}\nIts error output ended with:\n{failure.stderr_tail.rstrip()}"


def _boot_started_at() -> datetime | None:
    try:
        return datetime.fromtimestamp(_SUPERVISORD_PID_FILE.stat().st_mtime, timezone.utc)
    except OSError as e:
        logger.debug("Cannot tell when this boot started from {}: {}", _SUPERVISORD_PID_FILE, e)
        return None


def describe_install_failure_this_boot() -> str | None:
    """How the Screen's install unit failed, if env-converge ran it in this boot and it failed; None when it did not
    fail, has not run yet, or its record cannot be read."""
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
    failure = unit_failure_since(lines, SCREEN_INSTALL_UNIT, started_at)
    return None if failure is None else describe_unit_failure(failure)
