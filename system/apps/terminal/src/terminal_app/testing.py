"""Test doubles for the terminal app: a fake ``tmux`` and a fake ``ttyd`` installed as executables on PATH."""

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from app_instances.primitives import InstanceTitle
from imbue.imbue_common.mutable_model import MutableModel
from pydantic import Field

from terminal_app.data_types import TerminalSessionRecord, TmuxClient, TmuxSession
from terminal_app.primitives import TmuxSessionName, Workdir

# Where the fake tmux keeps its canned answers and its call log.
ENV_FAKE_TMUX_DIR: Final[str] = "FAKE_TMUX_DIR"
# Where the fake ttyd records the argv it was started with.
ENV_FAKE_TTYD_DIR: Final[str] = "FAKE_TTYD_DIR"

# Where a test source starts a terminal created without a workdir.
DEFAULT_TEST_WORKDIR: Final[Workdir] = Workdir("/home/user/workspace")

_EXECUTABLE_MODE: Final[int] = 0o755

# The fake tmux answers list-sessions and list-clients from two tab-separated files (in the
# exact format the real app asks for, so the real parsers run), mutates the sessions file on
# kill-session and rename-session, appends every argv to a call log, and refuses kills while a
# marker file exists so the "session survived the kill" path can be exercised.
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
clients_path = state / "clients.tsv"
answer = ""
error = ""


def target_name() -> str:
    return sys.argv[sys.argv.index("-t") + 1].removeprefix("=")


def session_lines() -> list[str]:
    return sessions_path.read_text().splitlines() if sessions_path.exists() else []


if command == "list-sessions":
    if sessions_path.exists():
        answer = sessions_path.read_text()
    else:
        error = "no server running on /tmp/tmux-1000/default"
elif command == "list-clients":
    if clients_path.exists():
        answer = clients_path.read_text()
    else:
        error = "no server running on /tmp/tmux-1000/default"
elif command == "kill-session":
    name = target_name()
    lines = session_lines()
    remaining = [line for line in lines if line.split("\\t")[0] != name]
    if (state / "refuse-kill").exists() or len(remaining) == len(lines):
        error = f"can't find session: {{name}}"
    else:
        sessions_path.write_text("".join(line + "\\n" for line in remaining))
elif command == "rename-session":
    name = target_name()
    new_name = sys.argv[-1]
    lines = session_lines()
    renamed = [
        "\\t".join([new_name, *line.split("\\t")[1:]]) if line.split("\\t")[0] == name else line
        for line in lines
    ]
    if any(line.split("\\t")[0] == new_name for line in lines):
        error = f"duplicate session: {{new_name}}"
    elif renamed == lines:
        error = f"can't find session: {{name}}"
    else:
        sessions_path.write_text("".join(line + "\\n" for line in renamed))
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
                f"{session.name}\t{session.session_id}\t{_activity_field(session)}\n"
                for session in sessions
            )
        )

    def set_clients(self, clients: Sequence[TmuxClient]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / "clients.tsv").write_text(
            "".join(
                f"{client.client_tty}\t{client.session_name}\t{client.session_id}\n"
                for client in clients
            )
        )

    def refuse_kills(self) -> None:
        """Make every kill-session fail while leaving the session in place."""
        (self.state_dir / "refuse-kill").touch()

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


def _activity_field(session: TmuxSession) -> str:
    if session.last_activity is None:
        return ""
    return str(int(session.last_activity.timestamp()))


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
    name: str, title: str | None, workdir: str | None
) -> TerminalSessionRecord:
    """A store record from plain strings; None for a title or workdir the record has none of."""
    return TerminalSessionRecord(
        name=TmuxSessionName(name),
        title=InstanceTitle(title) if title is not None else None,
        workdir=Workdir(workdir) if workdir is not None else None,
    )
