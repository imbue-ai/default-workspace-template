"""Running a local command under a timeout; injectable so tests answer for the command."""

import subprocess
from collections.abc import Callable
from collections.abc import Sequence

RunCommand = Callable[[Sequence[str], float], "subprocess.CompletedProcess[str]"]


def run_subprocess(argv: Sequence[str], timeout_seconds: float) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout_seconds, check=False)
