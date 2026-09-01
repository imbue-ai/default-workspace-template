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
