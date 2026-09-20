import os
from pathlib import Path

import pytest
from flask.testing import FlaskClient

from terminal_app.data_types import TerminalPaths
from terminal_app.sessions import TmuxSessionSource
from terminal_app.store import JsonTerminalSessionStore
from terminal_app.testing import (
    DEFAULT_TEST_WORKDIR,
    ENV_FAKE_TMUX_DIR,
    TEST_PTY_LABEL,
    TEST_SESSION_COMMAND,
    TEST_SHELL_LABEL,
    FakeTmux,
    build_pages_test_client,
    install_fake_tmux,
    write_registry_labels,
)
from terminal_app.tmux import SubprocessTmux


@pytest.fixture
def fake_tmux(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeTmux:
    """A fake ``tmux`` on PATH, reporting a running server with no sessions."""
    fake = install_fake_tmux(tmp_path / "fake-tmux")
    monkeypatch.setenv("PATH", f"{fake.bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv(ENV_FAKE_TMUX_DIR, str(fake.state_dir))
    fake.set_sessions([])
    return fake


@pytest.fixture
def terminal_paths(tmp_path: Path) -> TerminalPaths:
    return TerminalPaths(state_dir=tmp_path / "state")


@pytest.fixture
def session_store(tmp_path: Path) -> JsonTerminalSessionStore:
    return JsonTerminalSessionStore(store_path=tmp_path / "apps" / "instances.json")


@pytest.fixture
def session_source(
    fake_tmux: FakeTmux,
    session_store: JsonTerminalSessionStore,
    terminal_paths: TerminalPaths,
) -> TmuxSessionSource:
    return TmuxSessionSource(
        tmux=SubprocessTmux(),
        store=session_store,
        agent_session_prefix="mngr-",
        default_workdir=DEFAULT_TEST_WORKDIR,
        sessions_dir=terminal_paths.sessions_dir,
        session_command=TEST_SESSION_COMMAND,
    )


@pytest.fixture
def pages_client(session_source: TmuxSessionSource, tmp_path: Path) -> FlaskClient:
    """A test client over the wrapper pages, with both the shell and the pty registered."""
    registry_path = write_registry_labels(
        tmp_path / "apps.toml", {"system_interface": TEST_SHELL_LABEL, "terminal-pty": TEST_PTY_LABEL}
    )
    return build_pages_test_client(session_source, registry_path)
