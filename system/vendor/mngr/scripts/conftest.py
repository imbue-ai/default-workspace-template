from collections.abc import Generator
from pathlib import Path

import pytest

from imbue.mngr.utils.testing import isolate_git
from imbue.mngr.utils.testing import isolate_home


@pytest.fixture
def isolated_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """Run git against a temp HOME with a known .gitconfig so the developer's global config cannot leak in.

    Opt-in rather than autouse: other scripts tests run git against the real checkout and
    read real HOME state.
    """
    isolate_home(tmp_path, monkeypatch)
    with isolate_git(monkeypatch):
        yield
