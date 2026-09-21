"""The skill's scripts import each other as siblings (the directory is ``sys.path[0]``
when ``update_self.py`` runs); put it there for the tests too."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture(autouse=True)
def _isolate_git_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's git config out of the real-git tests.

    The ledger and recovery tests drive real ``git`` (in the test and in the
    scripts under test), and a global ``commit.gpgsign`` or ``core.hooksPath``
    would reach into every one of them. Identity is set per repo by the
    helpers, so nothing here needs the global file.
    """
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


@pytest.fixture(autouse=True)
def _isolate_tool_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point the pinned tool home at a temporary directory for every test.

    The apply falls back to the home the build pins when it cannot resolve an
    installation from PATH, and its tests drive it with a fake PATH that
    resolves nothing -- so the fallback would otherwise read (and name in the
    argv it records) the real ``/root`` of whatever machine runs the suite.
    That is live workspace state, which is how a test came to delete the
    installation it was validating a release against.
    """
    monkeypatch.setenv("TOOL_ENV_HOME", str(tmp_path / "pinned-tool-home"))
