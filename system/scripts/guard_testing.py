"""Test helper for the PreToolUse guard scripts beside this file."""

import json
import subprocess
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent


def run_guard(guard: str, payload: dict) -> int:
    """The exit code of guard script ``guard`` given the hook ``payload`` on stdin."""
    return subprocess.run(
        ["bash", str(_SCRIPTS / guard)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
    ).returncode
