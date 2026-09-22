from pathlib import Path
from typing import Final

import pytest
from terminal_app.testing import TerminalEnvironment, prepare_terminal_environment

# system/apps/terminal/conftest.py -> the repository root, the cwd the app resolves
# system/scripts/forward_port.py against.
REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]


@pytest.fixture
def terminal_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TerminalEnvironment:
    """The cwd and registry the terminal app under test runs against."""
    return prepare_terminal_environment(tmp_path, monkeypatch, REPO_ROOT)
