"""Test doubles for the terminal app: a fake ``tmux`` and a fake ``ttyd`` installed as executables on PATH, and the
environment a process under test registers in."""

import json
import socket
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Final

import pytest
from app_manifest.registry import ENV_APPS_FILE
from flask import Flask
from flask.testing import FlaskClient
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from pydantic import Field

from terminal_app.data_types import TerminalSessionRecord, TmuxSession
from terminal_app.pages import build_pages_blueprint
from terminal_app.primitives import (
    TerminalTitle,
    TmuxSessionId,
    TmuxSessionName,
    Workdir,
)
from terminal_app.sessions import TmuxSessionSource
from terminal_app.tmux import parse_tmux_sessions

LOOPBACK_HOST: Final[str] = "127.0.0.1"

# Where the fake tmux keeps its canned answers and its call log.
ENV_FAKE_TMUX_DIR: Final[str] = "FAKE_TMUX_DIR"
# Where the fake ttyd records the argv it was started with.
ENV_FAKE_TTYD_DIR: Final[str] = "FAKE_TTYD_DIR"
# The fake tmux stamps a created session with this plus its id number as its creation time.
FAKE_CREATED_EPOCH_BASE: Final[int] = 1_700_000_000

# Where a test source starts a terminal created without a workdir.
DEFAULT_TEST_WORKDIR: Final[Workdir] = Workdir("/home/user/workspace")
# The command a test source gives a new session (the fake tmux records it, never runs it).
TEST_SESSION_COMMAND: Final[tuple[str, ...]] = ("python3", "/opt/oom_tag_service.py", "terminal-session", "bash", "-l")
# The origin label a test registry gives the pty, the row the wrapper page reads.
TEST_PTY_LABEL: Final[str] = "terminal-pty-c3d4"
# What a test's stand-in for the shell's built contract module says.
TEST_APP_CONTRACT_SOURCE: Final[str] = "export function connectToShell() {}\n"

_EXECUTABLE_MODE: Final[int] = 0o755

# The fake tmux answers list-sessions from a tab-separated file (in the exact format the real
# app asks for, so the real parser runs), mutates the sessions file on kill-session and
# new-session, appends every argv to a call log, and refuses kills while a marker file exists
# so the "session survived the kill" path can be exercised.
_FAKE_TMUX_SCRIPT: Final[str] = f'''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

state = Path(os.environ["{ENV_FAKE_TMUX_DIR}"])
with (state / "calls.log").open("a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
command = sys.argv[1]
sessions_path = state / "sessions.tsv"
answer = ""
error = ""


def target() -> str:
    return sys.argv[sys.argv.index("-t") + 1]


def matches_target(line: str, wanted: str) -> bool:
    name, session_id = line.split("\\t")[:2]
    if wanted.startswith("$"):
        return session_id == wanted
    return name == wanted.removeprefix("=")


def session_lines() -> list[str]:
    return sessions_path.read_text().splitlines() if sessions_path.exists() else []


def next_session_id(lines: list[str]) -> str:
    numbers = [int(line.split("\\t")[1].removeprefix("$")) for line in lines if "\\t$" in line]
    return f"${{max(numbers, default=0) + 1}}"


if command == "list-sessions":
    if sessions_path.exists():
        answer = sessions_path.read_text()
    else:
        error = "no server running on /tmp/tmux-1000/default"
elif command == "kill-session":
    wanted = target()
    lines = session_lines()
    remaining = [line for line in lines if not matches_target(line, wanted)]
    if (state / "refuse-kill").exists() or len(remaining) == len(lines):
        error = f"can't find session: {{wanted}}"
    else:
        sessions_path.write_text("".join(line + "\\n" for line in remaining))
elif command == "new-session":
    name = sys.argv[sys.argv.index("-s") + 1]
    lines = session_lines()
    if (state / "refuse-create").exists():
        error = "fake tmux: refusing to create sessions"
    elif any(line.split("\\t")[0] == name for line in lines):
        error = f"duplicate session: {{name}}"
    else:
        session_id = next_session_id(lines)
        # Creation times count up from a fixed epoch, so a test can tell sessions apart by them.
        created = str({FAKE_CREATED_EPOCH_BASE} + int(session_id.removeprefix("$")))
        state.mkdir(parents=True, exist_ok=True)
        with sessions_path.open("a") as sessions_file:
            sessions_file.write(f"{{name}}\\t{{session_id}}\\t\\t{{created}}\\n")
        if "-P" in sys.argv:
            answer = session_id + "\\t" + created + "\\n"
else:
    error = f"fake tmux: unknown command {{command}}"

# The fake's stdout and stderr are tmux's answers.
if error:
    sys.stderr.write(error + "\\n")
    sys.exit(1)
sys.stdout.write(answer)
'''

# The fake ttyd records its argv and then waits to be signalled, as the real one would.
_FAKE_TTYD_SCRIPT: Final[str] = f"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$@" > "${ENV_FAKE_TTYD_DIR}/argv"
exec sleep 100000
"""


class FakeTmux(MutableModel):
    """Drives the fake ``tmux`` executable: what it answers, and what it was asked."""

    state_dir: Path = Field(frozen=True, description="The fake's state directory")
    bin_dir: Path = Field(
        frozen=True, description="The directory holding the fake executable"
    )

    def set_sessions(self, sessions: Sequence[TmuxSession]) -> None:
        """Make the server report exactly these sessions (an empty sequence is a running, empty server)."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / "sessions.tsv").write_text(
            "".join(
                f"{session.name}\t{session.session_id}\t{_activity_field(session)}\t{_created_field(session)}\n"
                for session in sessions
            )
        )

    def refuse_kills(self) -> None:
        """Make every kill-session fail while leaving the session in place."""
        (self.state_dir / "refuse-kill").touch()

    def refuse_creates(self) -> None:
        """Make every new-session fail, as a tmux server that cannot fork a shell would."""
        (self.state_dir / "refuse-create").touch()

    def sessions(self) -> list[TmuxSession]:
        """The sessions the fake currently reports, as the real parser reads them."""
        sessions_path = self.state_dir / "sessions.tsv"
        if not sessions_path.exists():
            return []
        return parse_tmux_sessions(sessions_path.read_text())

    def session_names(self) -> list[str]:
        sessions_path = self.state_dir / "sessions.tsv"
        if not sessions_path.exists():
            return []
        return [line.split("\t")[0] for line in sessions_path.read_text().splitlines()]

    def calls(self) -> list[list[str]]:
        """Every tmux invocation so far, as argument lists."""
        log_path = self.state_dir / "calls.log"
        if not log_path.exists():
            return []
        return [json.loads(line) for line in log_path.read_text().splitlines()]

    def creates(self) -> list[list[str]]:
        """The ``new-session`` invocations so far, as argument lists."""
        return [call for call in self.calls() if call[0] == "new-session"]


def _activity_field(session: TmuxSession) -> str:
    if session.last_activity is None:
        return ""
    return str(int(session.last_activity.timestamp()))


def _created_field(session: TmuxSession) -> str:
    return "" if session.created_epoch is None else str(session.created_epoch)


def fake_created_epoch(session_id: str) -> int:
    """The creation time the fake tmux stamps on the session it minted under ``session_id``."""
    return FAKE_CREATED_EPOCH_BASE + int(session_id.removeprefix("$"))


def make_tmux_session(
    name: str, session_id: str, last_activity: datetime | None = None
) -> TmuxSession:
    """A live session as the fake tmux would have minted it: created at the time its id implies."""
    return TmuxSession(
        name=name,
        session_id=session_id,
        created_epoch=fake_created_epoch(session_id),
        last_activity=last_activity,
    )


def expected_session_id_file(session_id: str) -> str:
    """The id file the app writes for a session the fake tmux minted (or one built by ``make_tmux_session``)."""
    return f"{session_id}\n{fake_created_epoch(session_id)}\n"


def install_fake_tmux(directory: Path) -> FakeTmux:
    """Write the fake ``tmux`` into ``directory/bin`` (prepend it to PATH and set FAKE_TMUX_DIR to use it)."""
    bin_dir = directory / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    executable = bin_dir / "tmux"
    executable.write_text(_FAKE_TMUX_SCRIPT)
    executable.chmod(_EXECUTABLE_MODE)
    return FakeTmux(state_dir=directory / "tmux-state", bin_dir=bin_dir)


def install_fake_ttyd(directory: Path) -> Path:
    """Write the fake ``ttyd`` into ``directory/bin`` and return the directory it records its argv in."""
    bin_dir = directory / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    executable = bin_dir / "ttyd"
    executable.write_text(_FAKE_TTYD_SCRIPT)
    executable.chmod(_EXECUTABLE_MODE)
    record_dir = directory / "ttyd-state"
    record_dir.mkdir(parents=True, exist_ok=True)
    return record_dir


def read_fake_ttyd_argv(record_dir: Path) -> list[str] | None:
    """The argv the fake ttyd was started with, or None when it has not started."""
    argv_path = record_dir / "argv"
    if not argv_path.exists():
        return None
    return argv_path.read_text().splitlines()


def make_terminal_record(
    name: str,
    title: str | None,
    workdir: str | None,
    session_id: str | None = None,
    is_stopped: bool = False,
    session_created: int | None = None,
    is_session_created_known: bool = True,
) -> TerminalSessionRecord:
    """A store record from plain strings; None for a title, workdir, or session id the record has none of.

    A record with a session id remembers the creation time the fake tmux stamps on that id unless
    ``session_created`` names another (a stale record from an earlier server) or
    ``is_session_created_known`` is false (a record that knows none).
    """
    if session_created is None and is_session_created_known and session_id is not None:
        session_created = fake_created_epoch(session_id)
    return TerminalSessionRecord(
        name=TmuxSessionName(name),
        title=TerminalTitle(title) if title is not None else None,
        workdir=Workdir(workdir) if workdir is not None else None,
        session_id=TmuxSessionId(session_id) if session_id is not None else None,
        session_created=session_created,
        is_stopped=is_stopped,
    )


def expected_new_session_call(name: str, workdir: str) -> list[str]:
    """The argv a test source's create hands the fake tmux for a session of this name in this directory."""
    return [
        "new-session",
        "-d",
        "-s",
        name,
        "-c",
        workdir,
        "-P",
        "-F",
        "#{session_id}\t#{session_created}",
        *TEST_SESSION_COMMAND,
    ]


def write_session_id_file(
    sessions_dir: Path, name: str, session_id: str, session_created: int | None = None
) -> None:
    """Record a session id (and creation time) under ``sessions_dir`` the way the app does, for a terminal it remembers."""
    sessions_dir.mkdir(parents=True, exist_ok=True)
    created = "" if session_created is None else str(session_created)
    (sessions_dir / name).write_text(f"{session_id}\n{created}\n")


def read_session_id_file(sessions_dir: Path, name: str) -> str | None:
    """The session id file's text for this terminal, or None when the app wrote none."""
    path = sessions_dir / name
    return path.read_text() if path.exists() else None


def write_registry_labels(path: Path, label_by_app_name: Mapping[str, str]) -> Path:
    """Write a registry of one row per app, carrying only what the wrapper page reads: the name and the origin label."""
    path.write_text(
        "".join(
            f'[[apps]]\nname = "{name}"\nurl = "http://localhost:1"\nlabel = "{label}"\n\n'
            for name, label in label_by_app_name.items()
        )
    )
    return path


def build_pages_test_client(source: TmuxSessionSource, registry_path: Path, contract_path: Path) -> FlaskClient:
    """A test client over the wrapper pages alone, reading origin labels from ``registry_path`` and serving the
    contract module at ``contract_path``."""
    app = Flask(__name__, static_folder=None)
    app.register_blueprint(build_pages_blueprint(source=source, registry_path=registry_path, contract_path=contract_path))
    return app.test_client()


class TerminalEnvironment(FrozenModel):
    """Where a terminal process under test keeps its files, and the registry it registers in."""

    scratch_dir: Path = Field(description="The test's own directory for manifests, stores, and logs")
    registry_path: Path = Field(description="The apps.toml registrations land in")


def prepare_terminal_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repo_root: Path
) -> TerminalEnvironment:
    """cwd at ``repo_root`` (the registration script is cwd-relative) and a scratch registry."""
    monkeypatch.chdir(repo_root)
    registry_path = tmp_path / "apps.toml"
    monkeypatch.setenv(ENV_APPS_FILE, str(registry_path))
    return TerminalEnvironment(scratch_dir=tmp_path, registry_path=registry_path)


def free_port() -> int:
    """A loopback port nothing is listening on right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((LOOPBACK_HOST, 0))
        return probe.getsockname()[1]


def is_port_accepting(port: int) -> bool:
    try:
        with socket.create_connection((LOOPBACK_HOST, port), timeout=0.2):
            return True
    except OSError:
        return False


def write_terminal_manifest(directory: Path, app_name: str) -> Path:
    """Write a terminal-shaped app.toml (the ``new`` launch path and the icon it names) into ``directory``."""
    (directory / "icon.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="M3 3h18v18H3z"/></svg>'
    )
    manifest_path = directory / "app.toml"
    manifest_path.write_text(
        f'name = "{app_name}"\n'
        f'display_name = "Terminal {app_name}"\n'
        'icon = "icon.svg"\n'
        "critical = true\n"
        'priority = "terminal"\n'
        "\n"
        "[[launch_paths]]\n"
        'id = "new"\n'
        'label = "New Terminal"\n'
        'path = "/new"\n'
    )
    return manifest_path
