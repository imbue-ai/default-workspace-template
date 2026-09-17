from pathlib import Path

import pytest
from pydantic import ValidationError

from terminal_app.data_types import TerminalPaths


def test_terminal_paths_refuse_a_relative_state_directory() -> None:
    with pytest.raises(ValidationError, match="must be absolute"):
        TerminalPaths(state_dir=Path("data/.state/terminal"))
