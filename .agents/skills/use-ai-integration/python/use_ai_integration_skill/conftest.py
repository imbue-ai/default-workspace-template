import os
from pathlib import Path

import pytest


@pytest.fixture
def anthropic_snapshot_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """A key snapshot path under ``tmp_path``, with every process-env Anthropic credential cleared."""
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    return str(tmp_path / "data" / ".secrets" / "anthropic.env")


@pytest.fixture
def stub_bin_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A directory put first on ``PATH``, for stub ``claude`` and ``codex`` executables."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return bin_dir
