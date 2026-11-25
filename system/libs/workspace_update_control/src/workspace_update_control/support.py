import subprocess
from collections.abc import Mapping, Sequence
from typing import Final

# Relative to the workspace root, which the shell and cron's tick both run from.
# own-process: other-tree -- the shell imported its code when it started, and the busy chats are read by the CLI the tree holds now, which an update may have replaced since
BACKGROUND_TASKS_CLI_PATH: Final[str] = "system/scripts/background_tasks.py"


def run_command(
    argv: Sequence[str],
    timeout_seconds: float,
    cwd: str | None = None,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` to completion with its output captured; a tool that cannot start or overruns reads as exit 127."""
    try:
        return subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
            cwd=cwd,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(
            list(argv),
            returncode=127,
            stdout="",
            stderr="failed to run %s: %s" % (argv[0], e),
        )


def as_json_object(value: object) -> dict[str, object] | None:
    """``value`` as a JSON object, or None when it is any other JSON value."""
    if not isinstance(value, dict):
        return None
    return {str(key): item for key, item in value.items()}
