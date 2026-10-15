"""Test helpers for the Datalib app: the launcher as an importable module, and a fake ``datalib-http``.

The launcher is a flat script, not a package, so it is loaded from its file under a name of its
own (the same way ``system/scripts/script_modules_testing.py`` loads the scripts beside it).
"""

import importlib.util
from pathlib import Path
from typing import Any, Final

LAUNCHER_PATH: Final[Path] = Path(__file__).parent / "launch_datalib_http.py"

# Where the fake datalib-http records the argv and environment it was started with.
ENV_FAKE_DATALIB_HTTP_DIR: Final[str] = "FAKE_DATALIB_HTTP_DIR"

_EXECUTABLE_MODE: Final[int] = 0o755

FAKE_DATALIB_HTTP_EXIT_STATUS: Final[int] = 7

# The fake records its argv and the two variables the real server reads, then exits with a status
# of its own, which a launcher that became this process reports as its own.
_FAKE_DATALIB_HTTP_SCRIPT: Final[str] = f"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$@" > "${ENV_FAKE_DATALIB_HTTP_DIR}/argv"
printf 'DATALIB_BIND=%s\\nDATALIB_TOKEN=%s\\n' "${{DATALIB_BIND:-}}" "${{DATALIB_TOKEN:-}}" > "${ENV_FAKE_DATALIB_HTTP_DIR}/environment"
if [ -r /proc/self/oom_score_adj ]; then cat /proc/self/oom_score_adj > "${ENV_FAKE_DATALIB_HTTP_DIR}/oom_score_adj"; fi
exit {FAKE_DATALIB_HTTP_EXIT_STATUS}
"""


def _load_launcher() -> Any:
    spec = importlib.util.spec_from_file_location(
        "launch_datalib_http_for_tests", LAUNCHER_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


launcher = _load_launcher()


def install_fake_datalib_http(directory: Path) -> tuple[Path, Path]:
    """Write the fake ``datalib-http`` into ``directory/bin``, returning its path and the directory it records into."""
    bin_dir = directory / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    executable = bin_dir / "datalib-http"
    executable.write_text(_FAKE_DATALIB_HTTP_SCRIPT)
    executable.chmod(_EXECUTABLE_MODE)
    record_dir = directory / "datalib-http-state"
    record_dir.mkdir(parents=True, exist_ok=True)
    return executable, record_dir


def read_fake_datalib_http_argv(record_dir: Path) -> list[str] | None:
    """The argv the fake datalib-http was started with, or None when it has not started."""
    argv_path = record_dir / "argv"
    if not argv_path.exists():
        return None
    return argv_path.read_text().splitlines()


def read_fake_datalib_http_oom_score_adj(record_dir: Path) -> int | None:
    """The ``oom_score_adj`` the fake datalib-http ran with, or None when it has not started or the host has no ``/proc``."""
    path = record_dir / "oom_score_adj"
    if not path.exists():
        return None
    return int(path.read_text())


def read_fake_datalib_http_environment(record_dir: Path) -> dict[str, str] | None:
    """The bind and token variables the fake datalib-http saw, or None when it has not started."""
    environment_path = record_dir / "environment"
    if not environment_path.exists():
        return None
    return dict(
        line.split("=", 1) for line in environment_path.read_text().splitlines()
    )
