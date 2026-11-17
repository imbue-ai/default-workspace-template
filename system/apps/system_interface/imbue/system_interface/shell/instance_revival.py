"""Starting a stopped preview or UI sketch again, for its window's Start button.

A preview or a mock's frame is an isolated instance ``serve_isolated_instance.py`` booted
and recorded; a workspace restart stops it, and its window then shows the stopped
placeholder. The script's own ``revive`` subcommand boots it again on its recorded ports,
run rather than reimplemented, so the shell stays one reader of the instance's record.
"""

import sys
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.concurrency_group.errors import ProcessError
from imbue.concurrency_group.subprocess_utils import run_local_command_modern_version
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.system_interface.shell.errors import InstanceReviveError

# own-process: other-tree -- an import would load the copy the shell started with, not the checkout's current one
REVIVE_SCRIPT_REL: Final[str] = ".agents/shared/scripts/agents_shared/serve_isolated_instance.py"

# A revive waits up to a minute for each server it boots: a preview's own two, and two more
# for each sibling it frames.
_REVIVE_TIMEOUT_SECONDS: Final[float] = 300.0
_REVIVE_SHUTDOWN_TIMEOUT_SECONDS: Final[float] = 5.0
# How much of the script's stderr the shell's log keeps of a failure: its last lines say why.
_STDERR_TAIL_CHARS: Final[int] = 2000
# What the script exits with when the instance cannot come back as it was
# (``serve_isolated_instance.REVIVE_IMPOSSIBLE_EXIT_CODE``).
_REVIVE_IMPOSSIBLE_EXIT_CODE: Final[int] = 3


class InstanceReviver(FrozenModel):
    """Runs ``serve_isolated_instance.py revive`` for the workspace at ``repo_root``."""

    repo_root: Path = Field(description="The workspace's repo root, which instances are recorded under")

    def revive(self, instance_name: str) -> None:
        """Boot again whatever part of the instance has stopped; raises InstanceReviveError, final when only a fresh
        instance can come back. The script's own words, written for agents, go to the shell's log."""
        try:
            result = run_local_command_modern_version(
                command=[
                    sys.executable,
                    str(self.repo_root / REVIVE_SCRIPT_REL),
                    "revive",
                    "--name",
                    instance_name,
                    "--repo-root",
                    str(self.repo_root),
                ],
                cwd=self.repo_root,
                is_checked=False,
                timeout=_REVIVE_TIMEOUT_SECONDS,
                shutdown_timeout_sec=_REVIVE_SHUTDOWN_TIMEOUT_SECONDS,
            )
        except (ProcessError, OSError) as e:
            logger.warning("Could not run the revive of {}: {}", instance_name, e)
            raise InstanceReviveError(f"The revive of {instance_name} could not be run", is_final=False) from e
        if result.returncode != 0:
            logger.warning(
                "The revive of {} exited {}: {}",
                instance_name,
                result.returncode,
                result.stderr.strip()[-_STDERR_TAIL_CHARS:],
            )
            raise InstanceReviveError(
                f"The revive of {instance_name} exited {result.returncode}",
                is_final=result.returncode == _REVIVE_IMPOSSIBLE_EXIT_CODE,
            )
