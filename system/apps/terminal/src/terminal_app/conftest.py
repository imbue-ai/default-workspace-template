import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from app_instances.testing import (
    RecordedShellRequests,
    RecordingNudger,
    serve_recording_shell,
)
from app_manifest.primitives import AppName
from flask import Flask
from flask.testing import FlaskClient

from terminal_app.data_types import TerminalPaths
from terminal_app.hooks import HttpShellPoster, build_tmux_hook_blueprint
from terminal_app.sessions import TmuxSessionSource
from terminal_app.store import JsonTerminalSessionStore
from terminal_app.testing import ENV_FAKE_TMUX_DIR, FakeTmux, install_fake_tmux
from terminal_app.tmux import SubprocessTmux


@pytest.fixture
def fake_tmux(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeTmux:
    """A fake ``tmux`` on PATH, reporting a running server with no sessions and no clients."""
    fake = install_fake_tmux(tmp_path / "fake-tmux")
    monkeypatch.setenv("PATH", f"{fake.bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv(ENV_FAKE_TMUX_DIR, str(fake.state_dir))
    fake.set_sessions([])
    fake.set_clients([])
    return fake


@pytest.fixture
def terminal_paths(tmp_path: Path) -> TerminalPaths:
    return TerminalPaths(state_dir=tmp_path / "state")


@pytest.fixture
def session_store(terminal_paths: TerminalPaths) -> JsonTerminalSessionStore:
    return JsonTerminalSessionStore(store_path=terminal_paths.store_path)


@pytest.fixture
def session_source(
    fake_tmux: FakeTmux, session_store: JsonTerminalSessionStore
) -> TmuxSessionSource:
    return TmuxSessionSource(
        tmux=SubprocessTmux(), store=session_store, agent_session_prefix="mngr-"
    )


@pytest.fixture
def recording_shell() -> Iterator[RecordedShellRequests]:
    with serve_recording_shell() as recorded:
        yield recorded


@pytest.fixture
def recording_nudger() -> RecordingNudger:
    return RecordingNudger()


@pytest.fixture
def hook_client(
    fake_tmux: FakeTmux,
    terminal_paths: TerminalPaths,
    recording_shell: RecordedShellRequests,
    recording_nudger: RecordingNudger,
) -> FlaskClient:
    """A test client over the hook route alone, posting to the recording shell."""
    app = Flask(__name__, static_folder=None)
    app.register_blueprint(
        build_tmux_hook_blueprint(
            tmux=SubprocessTmux(),
            paths=terminal_paths,
            shell=HttpShellPoster(shell_url=recording_shell.base_url),
            nudger=recording_nudger,
            app_name=AppName("terminal"),
        )
    )
    return app.test_client()
