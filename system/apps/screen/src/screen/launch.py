"""Starting a command on the Screen (``screen run``): detached, with the display, in the most expendable band."""

import os
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from pydantic import Field

from oom_priority import bands
from screen.brokers import ENV_GATEWAY_PASSWORD
from screen.brokers import ENV_GATEWAY_PERMISSIONS_OVERRIDE
from screen.errors import ScreenError
from screen.interfaces import ProcessLauncherInterface
from screen.neko_config import PULSE_SOCKET_PATH
from screen.primitives import DISPLAY
from screen.primitives import RUNTIME_DIR

# The shell that bands itself and then becomes the command: the band survives execve and is inherited by everything
# the command spawns, so a program started on the Screen is shed before the stack (spec section 9.4). A shell does
# the tagging because the process to tag does not exist before the launch, and a ``preexec_fn`` is unsafe in a
# threaded service.
_BANDED_EXEC_SCRIPT: Final[str] = bands.oom_tag_shell_prefix(bands.AGENT_SUBPROCESS) + 'exec "$@"'
_SHELL_NAME: Final[str] = "screen-run"

# The gateway credentials mngr injects into the service's environment stay with the service: nothing started on the
# Screen needs them, and an agent that does holds them in its own shell already.
_WITHHELD_VARIABLES: Final[frozenset[str]] = frozenset({ENV_GATEWAY_PASSWORD, ENV_GATEWAY_PERMISSIONS_OVERRIDE})


def screen_process_environment() -> dict[str, str]:
    """The service's environment, less the gateway credentials, with the Screen's display and audio added: what
    every program started on the Screen (a ``screen run`` command, Chromium) runs with."""
    return {
        **{name: value for name, value in os.environ.items() if name not in _WITHHELD_VARIABLES},
        "DISPLAY": DISPLAY,
        "XDG_RUNTIME_DIR": RUNTIME_DIR,
        "PULSE_SERVER": f"unix:{PULSE_SOCKET_PATH}",
    }


class DetachedProcessLauncher(ProcessLauncherInterface):
    """Starts a command in its own session with the Screen's display and audio in its environment."""

    working_directory: Path = Field(frozen=True, description="Where the command starts")

    def launch(self, command: Sequence[str]) -> int:
        environment = screen_process_environment()
        try:
            process = subprocess.Popen(
                ["bash", "-c", _BANDED_EXEC_SCRIPT, _SHELL_NAME, *command],
                cwd=self.working_directory,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as e:
            raise ScreenError(f"could not start {command[0]!r} on the Screen: {e}") from e
        # The shell execs the command, so its pid is the command's.
        return process.pid
