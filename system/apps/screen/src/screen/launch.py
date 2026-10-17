"""Starting a command on the Screen (``screen run``): detached, with the display, in the most expendable band."""

import os
import subprocess
from collections.abc import Sequence
from pathlib import Path

from pydantic import Field

from oom_priority import bands
from screen.errors import ScreenError
from screen.interfaces import ProcessLauncherInterface
from screen.neko_config import PULSE_SOCKET_PATH
from screen.neko_config import RUNTIME_DIR
from screen.primitives import DISPLAY


def _band_self_before_exec() -> None:
    # Runs in the child between fork and exec: the band survives execve and is inherited by everything the command
    # spawns, so a program started on the Screen is shed before the stack (spec section 9.4).
    bands.set_oom_score_adj(os.getpid(), bands.AGENT_SUBPROCESS)


class DetachedProcessLauncher(ProcessLauncherInterface):
    """Starts a command in its own session with the Screen's display and audio in its environment."""

    working_directory: Path = Field(frozen=True, description="Where the command starts")

    def launch(self, command: Sequence[str]) -> int:
        environment = {
            **os.environ,
            "DISPLAY": DISPLAY,
            "XDG_RUNTIME_DIR": RUNTIME_DIR,
            "PULSE_SERVER": f"unix:{PULSE_SOCKET_PATH}",
        }
        try:
            process = subprocess.Popen(
                list(command),
                cwd=self.working_directory,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                preexec_fn=_band_self_before_exec,
            )
        except OSError as e:
            raise ScreenError(f"could not start {command[0]!r} on the Screen: {e}") from e
        return process.pid
