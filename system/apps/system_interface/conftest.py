"""Root conftest for the system interface's own pytest run.

This app carries its own pytest config and is `--ignore`d by the repo root's, so it
becomes its own rootdir and never loads the repo-root `conftest.py`. Anything that has
to hold for every suite in the repo has to be repeated here.
"""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

_EXEC_TEMP_ROOT_PATH = Path(__file__).resolve().parents[2] / "scripts/exec_temp_root.py"


def _load_exec_temp_root() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_exec_temp_root", _EXEC_TEMP_ROOT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def pytest_configure(config: pytest.Config) -> None:
    """Move pytest's temp tree off a filesystem that cannot execute what tests write there.

    Carried here for the same reason as the repo root's copy: a workspace container
    mounts `/tmp` `noexec`, so a stub binary written into `tmp_path` cannot run, and
    `PATH` resolution skips it silently and reaches the real one. This suite has no
    such stub today, but it roots separately from the repo, so it would not inherit
    the fix the day it grows one. See `system/scripts/exec_temp_root.py`.
    """
    if config.getoption("basetemp") is not None:
        return
    exec_temp_root = _load_exec_temp_root()
    try:
        exec_temp_root.relocate_temp_root_if_needed()
    except exec_temp_root.NoExecutableTempRootError as e:
        raise pytest.UsageError(str(e)) from e
