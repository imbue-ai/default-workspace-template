"""On-disk layout of the watchdog's ledger and status file -- the single source
of truth for where those files live.

The watchdog (writer), the system interface (status reader), and the revival
SessionStart hook (ledger reader) all resolve the same paths through this
module, so the layout can't drift between producer and consumers.

This module deliberately imports nothing beyond the standard library. The
revival hook runs in a plain ``python3`` environment (not ``uv run``), where the
heavier ``memory_watchdog`` modules' third-party dependencies (loguru, pydantic,
imbue_common) are unavailable; keeping the path logic dependency-free lets that
hook import it by putting this package's ``src`` directory on ``sys.path``.

Both files live under ``runtime/`` so they ride the runtime-backup branch and
survive container loss. The base resolves relative to the agent work dir (the
repo root, where every service runs), falling back to the current directory, and
is overridable in full via ``MEMORY_WATCHDOG_RUNTIME_DIR`` -- honored uniformly
so a production override can't make readers and the writer diverge.
"""

import os
from pathlib import Path
from typing import Final

_RUNTIME_DIR_ENV_VAR: Final[str] = "MEMORY_WATCHDOG_RUNTIME_DIR"
_RUNTIME_SUBDIR: Final[Path] = Path("runtime") / "memory_watchdog"


def watchdog_runtime_dir() -> Path:
    override = os.environ.get(_RUNTIME_DIR_ENV_VAR, "")
    if override:
        return Path(override)
    work_dir = os.environ.get("MNGR_AGENT_WORK_DIR", "")
    base = Path(work_dir) if work_dir else Path.cwd()
    return base / _RUNTIME_SUBDIR


def shed_ledger_path() -> Path:
    return watchdog_runtime_dir() / "events" / "shed" / "events.jsonl"


def status_path() -> Path:
    return watchdog_runtime_dir() / "status.json"
