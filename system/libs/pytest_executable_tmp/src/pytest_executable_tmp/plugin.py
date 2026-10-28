import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Final

import pytest
from imbue.imbue_common.pure import pure

_PROBE_SCRIPT: Final[str] = "#!/bin/sh\nexit 0\n"
_PROBE_TIMEOUT_SECONDS: Final[float] = 10.0


def can_run_files_in(directory: Path) -> bool:
    """Whether a script written under ``directory`` runs: false on a noexec mount, or where nothing can be written."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe_dir = Path(tempfile.mkdtemp(prefix="executable-probe-", dir=directory))
    except OSError:
        return False
    probe = probe_dir / "probe"
    try:
        probe.write_text(_PROBE_SCRIPT)
        probe.chmod(0o700)
        completed = subprocess.run(
            [str(probe)], check=False, timeout=_PROBE_TIMEOUT_SECONDS
        )
    except OSError:
        return False
    finally:
        shutil.rmtree(probe_dir)
    return completed.returncode == 0


def _temp_root(config: pytest.Config) -> Path:
    # pytest creates (and empties) --basetemp itself, so what must allow running
    # files is the directory it is created in.
    if config.option.basetemp is not None:
        return Path(config.option.basetemp).resolve().parent
    from_env = os.environ.get("PYTEST_DEBUG_TEMPROOT")
    return Path(from_env) if from_env else Path(tempfile.gettempdir())


@pure
def _unusable_root_message(root: Path) -> str:
    return (
        f"pytest-executable-tmp: a file written under {root} cannot be run (a noexec mount?). A test's stub "
        "executable on PATH would be skipped there, and the real program it stands in for would run instead. "
        "Point TMPDIR, --basetemp or PYTEST_DEBUG_TEMPROOT at a directory that allows running files."
    )


def pytest_configure(config: pytest.Config) -> None:
    root = _temp_root(config)
    if not can_run_files_in(root):
        pytest.exit(
            _unusable_root_message(root), returncode=pytest.ExitCode.USAGE_ERROR
        )
