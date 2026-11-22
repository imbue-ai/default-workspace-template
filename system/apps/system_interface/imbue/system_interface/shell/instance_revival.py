"""Starting a stopped preview or UI sketch again, for its window's Start button.

A preview (``preview_app.py``) or a UI sketch's frame (``mock_frame.py``) is an isolated
instance its flow booted and recorded; a workspace restart stops it, and its window then
shows the stopped placeholder. Each flow's own ``revive`` boots it again as it was last
booted, run rather than reimplemented, so the shell stays one reader of the flows' records.
"""

import sys
from pathlib import Path
from typing import Final

from app_manifest.registry import IsolatedInstanceKind
from loguru import logger
from pydantic import Field

from imbue.concurrency_group.errors import ProcessError
from imbue.concurrency_group.subprocess_utils import run_local_command_modern_version
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.system_interface.shell.errors import InstanceReviveError

# own-process: other-tree -- an import would load the copies the shell started with, not the checkout's current ones
REVIVE_SCRIPT_REL_BY_KIND: Final[dict[IsolatedInstanceKind, str]] = {
    IsolatedInstanceKind.CHANGE: ".agents/skills/update-app/python/update_app_skill/preview_app.py",
    IsolatedInstanceKind.MOCK: ".agents/skills/build-app/python/build_app_skill/mock_frame.py",
}

# A revive waits up to a minute for each server it boots, a preview's siblings' included.
_REVIVE_TIMEOUT_SECONDS: Final[float] = 300.0
_REVIVE_SHUTDOWN_TIMEOUT_SECONDS: Final[float] = 5.0
# How much of the script's stderr the shell's log keeps of a failure: its last lines say why.
_STDERR_TAIL_CHARS: Final[int] = 2000


class InstanceReviver(FrozenModel):
    """Runs the ``revive`` of the flow that booted a preview or UI sketch, for the workspace at ``repo_root``."""

    repo_root: Path = Field(description="The workspace's repo root, which instances are recorded under")

    def revive(self, app: str, kind: IsolatedInstanceKind) -> None:
        """Boot the preview or UI sketch of ``app`` again; raises InstanceReviveError when it did not come back.
        The script's own words, written for agents, go to the shell's log."""
        try:
            result = run_local_command_modern_version(
                command=[
                    sys.executable,
                    str(self.repo_root / REVIVE_SCRIPT_REL_BY_KIND[kind]),
                    "revive",
                    "--app",
                    app,
                    "--repo-root",
                    str(self.repo_root),
                ],
                cwd=self.repo_root,
                is_checked=False,
                timeout=_REVIVE_TIMEOUT_SECONDS,
                shutdown_timeout_sec=_REVIVE_SHUTDOWN_TIMEOUT_SECONDS,
            )
        except (ProcessError, OSError) as e:
            logger.warning("Could not run the revive of the {} instance of {}: {}", kind, app, e)
            raise InstanceReviveError(f"The revive of the {kind} instance of {app} could not be run") from e
        if result.returncode != 0:
            logger.warning(
                "The revive of the {} instance of {} exited {}: {}",
                kind,
                app,
                result.returncode,
                result.stderr.strip()[-_STDERR_TAIL_CHARS:],
            )
            raise InstanceReviveError(f"The revive of the {kind} instance of {app} exited {result.returncode}")
