"""Test doubles for the files app: a fake ``dufs`` installed as an executable."""

from pathlib import Path
from typing import Final

# Where the fake dufs records the argv it was started with.
ENV_FAKE_DUFS_DIR: Final[str] = "FAKE_DUFS_DIR"

_EXECUTABLE_MODE: Final[int] = 0o755

# The fake dufs records its argv and then waits to be signalled, as the real one would.
_FAKE_DUFS_SCRIPT: Final[str] = f"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$@" > "${ENV_FAKE_DUFS_DIR}/argv"
exec sleep 100000
"""


def install_fake_dufs(directory: Path) -> Path:
    """Write the fake ``dufs`` into ``directory/bin`` and return the directory it records its argv in."""
    bin_dir = directory / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    executable = bin_dir / "dufs"
    executable.write_text(_FAKE_DUFS_SCRIPT)
    executable.chmod(_EXECUTABLE_MODE)
    record_dir = directory / "dufs-state"
    record_dir.mkdir(parents=True, exist_ok=True)
    return record_dir


def read_fake_dufs_argv(record_dir: Path) -> list[str] | None:
    """The argv the fake dufs was started with, or None when it has not started."""
    argv_path = record_dir / "argv"
    if not argv_path.exists():
        return None
    return argv_path.read_text().splitlines()
