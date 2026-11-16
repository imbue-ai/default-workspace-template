"""Serve recorded Claude session transcripts in the real chat app, one chat per session, to read them as a user saw them.

Run from the repo root, after building the chat frontend (``npm run build`` in ``system/apps/chat/frontend``):

    uv run --no-sync python system/apps/chat/scripts/replay_transcripts.py <session.jsonl or dir> [...]

Each argument is a Claude session file (``<session id>.jsonl``, or ``session-<session id>.jsonl`` as an eval
trial exports it) or a directory holding such files, which then name their chats. The chat app and the shell
run over the fakes the chat's own e2e suite uses, so nothing is sent anywhere; the transcripts are read
through the app's own parser and drawn by its own page. Ctrl-C stops the servers.
"""

import argparse
import json
import logging
import re
import sys
import tempfile
import threading
import webbrowser
from pathlib import Path
from typing import Any
from typing import Final
from uuid import UUID

from loguru import logger

from imbue.chat.testing import is_chat_frontend_built
from imbue.chat.testing import make_agent_fixture
from imbue.chat.testing import running_workspace
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.primitives import AgentId
from imbue.system_interface.testing import find_free_port

_SESSION_FILE_PATTERN: Final = re.compile(
    r"^(?:session-)?([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$"
)


class ReplayInputError(Exception):
    """An argument names no session file this script can serve."""


class RecordedSession(FrozenModel):
    """One session file to show as a chat."""

    session_id: str
    chat_name: str
    events: tuple[dict[str, Any], ...]

    @property
    def agent_id(self) -> str:
        # mngr launches claude with the agent's uuid as its first session id, so the chat's agent wears it back.
        return str(AgentId(f"agent-{UUID(self.session_id).hex}"))


def _read_session_events(path: Path) -> tuple[dict[str, Any], ...]:
    # A session file can carry lines that are not JSON objects; the parser only reads object records.
    records = (json.loads(line) for line in path.read_text().splitlines() if line.startswith("{"))
    return tuple(record for record in records if isinstance(record, dict))


def _session_files_in(directory: Path) -> list[Path]:
    return sorted(path for path in directory.iterdir() if _SESSION_FILE_PATTERN.match(path.name))


def load_sessions(paths: list[Path]) -> list[RecordedSession]:
    """The sessions the arguments name, in argument order; a directory's sessions are named after it."""
    sessions: list[RecordedSession] = []
    for path in paths:
        if path.is_dir():
            files = _session_files_in(path)
            if not files:
                raise ReplayInputError(f"{path} holds no Claude session file (<session id>.jsonl)")
            names = (
                [path.name] if len(files) == 1 else [f"{path.name} ({index})" for index in range(1, len(files) + 1)]
            )
        else:
            if not _SESSION_FILE_PATTERN.match(path.name):
                raise ReplayInputError(f"{path} is not named like a Claude session file (<session id>.jsonl)")
            files = [path]
            names = [path.stem]
        for file, name in zip(files, names, strict=True):
            match = _SESSION_FILE_PATTERN.match(file.name)
            assert match is not None
            sessions.append(
                RecordedSession(session_id=match.group(1), chat_name=name, events=_read_session_events(file))
            )
    session_ids = [session.session_id for session in sessions]
    if len(set(session_ids)) != len(session_ids):
        raise ReplayInputError("The same session was named more than once")
    return sessions


def serve(sessions: list[RecordedSession], chat_port: int, is_opening_browser: bool) -> None:
    """Serve ``sessions`` as chats until interrupted."""
    first, *rest = sessions
    with tempfile.TemporaryDirectory(prefix="replay-transcripts-") as tmp:
        tmp_path = Path(tmp)
        seeded_agents = [
            make_agent_fixture(
                tmp_path,
                agent_id=session.agent_id,
                agent_name=session.chat_name,
                session_events=session.events,
                session_id=session.session_id,
            )[0]
            for session in rest
        ]
        with running_workspace(
            tmp_path,
            find_free_port(),
            chat_port,
            session_events=first.events,
            agent_id=first.agent_id,
            agent_name=first.chat_name,
            session_id=first.session_id,
            seeded_agents=seeded_agents,
        ) as workspace:
            for session in sessions:
                logger.info("{}: {}/?chat={}", session.chat_name, workspace.chat_url, session.agent_id)
            logger.info("Serving {} chat(s) at {}/ (Ctrl-C to stop)", len(sessions), workspace.chat_url)
            if is_opening_browser:
                webbrowser.open(f"{workspace.chat_url}/?chat={first.agent_id}")
            try:
                threading.Event().wait()
            except KeyboardInterrupt:
                logger.info("Stopping")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="+", type=Path, help="Claude session files, or directories holding them")
    parser.add_argument("--port", type=int, default=0, help="The chat app's port (default: a free one)")
    parser.add_argument("--open", action="store_true", help="Open the chat list in the default browser")
    args = parser.parse_args()

    logger.remove()
    logger.add(sys.stderr, level="INFO")
    # The servers' per-request access lines would bury the chat links.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    if not is_chat_frontend_built():
        parser.error("The chat frontend is not built; run `npm run build` in system/apps/chat/frontend first")
    try:
        sessions = load_sessions(args.paths)
    except ReplayInputError as e:
        parser.error(str(e))
    serve(sessions, args.port or find_free_port(), args.open)


if __name__ == "__main__":
    main()
