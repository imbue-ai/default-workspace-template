"""Root conftest for the chat app's own pytest run.

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

    This suite plants executable stubs in `tmp_path` and hands them to code that runs
    them (`testing.py`'s `write_recording_mngr_binary` and the fake-claude/fake-mngr
    pair, plus several tests that build their own). A workspace container mounts `/tmp`
    `noexec`, so there every one of those stubs is skipped by `PATH` resolution and the
    real binary runs instead. See `system/scripts/exec_temp_root.py`.
    """
    if config.getoption("basetemp") is not None:
        return
    exec_temp_root = _load_exec_temp_root()
    try:
        exec_temp_root.relocate_temp_root_if_needed()
    except exec_temp_root.NoExecutableTempRootError as e:
        raise pytest.UsageError(str(e)) from e
