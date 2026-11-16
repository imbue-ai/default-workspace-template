"""Serve recorded Claude session transcripts in the real chat app, one chat per session, to read them as a user saw them.

Run from the repo root, after building the chat frontend (``npm run build`` in ``system/apps/chat/frontend``):

    .venv/bin/python system/apps/chat/scripts/replay_transcripts.py <session.jsonl or dir> [...]

Each argument is a Claude session file (``<session id>.jsonl``, or ``session-<session id>.jsonl`` as an eval
trial exports it) or a directory holding such files, which then name their chats. The chat app and the shell
run over the fakes the chat's own e2e suite uses, so nothing is sent anywhere; the transcripts are read
through the app's own parser and drawn by its own page. Ctrl-C stops the servers.
"""

import argparse
import json
import logging
import re
import signal
import sys
import tempfile
import threading
import webbrowser
from pathlib import Path
from typing import Any
from typing import Final
from uuid import UUID

from loguru import logger

from imbue.chat.auto_open import chat_root_path
from imbue.chat.errors import ChatAppError
from imbue.chat.primitives import ChatId
from imbue.chat.testing import SeededSession
from imbue.chat.testing import is_chat_frontend_built
from imbue.chat.testing import running_workspace
from imbue.mngr.primitives import AgentId
from imbue.system_interface.testing import find_free_port

_SESSION_FILE_PATTERN: Final = re.compile(
    r"^(?:session-)?([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$"
)


class ReplayInputError(ChatAppError):
    """An argument names no session file this script can serve."""


def _agent_id_for_session(session_id: str) -> str:
    # mngr launches claude with the agent's uuid as its first session id, so the chat's agent wears it back.
    return str(AgentId(f"agent-{UUID(session_id).hex}"))


def _read_session_events(path: Path) -> tuple[dict[str, Any], ...]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text().splitlines(), start=1):
        # A session file can carry lines that are not JSON objects; the parser only reads object records.
        if not line.startswith("{"):
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as e:
            raise ReplayInputError(f"{path}:{line_number} is not valid JSON ({e})") from e
        records.append(record)
    return tuple(records)


def _session_id_of(session_file: Path) -> str:
    match = _SESSION_FILE_PATTERN.match(session_file.name)
    assert match is not None
    return match.group(1)


def _session_files_in(directory: Path) -> list[Path]:
    return sorted(path for path in directory.iterdir() if _SESSION_FILE_PATTERN.match(path.name))


def load_sessions(paths: list[Path]) -> list[SeededSession]:
    """The sessions the arguments name, in argument order; a directory's sessions are named after it."""
    sessions: list[SeededSession] = []
    for path in paths:
        if not path.exists():
            raise ReplayInputError(f"{path} does not exist")
        if path.is_dir():
            files = _session_files_in(path)
            if not files:
                raise ReplayInputError(f"{path} holds no Claude session file (<session id>.jsonl)")
            directory_name = path.resolve().name
            names = (
                [directory_name]
                if len(files) == 1
                else [f"{directory_name} ({_session_id_of(file)[:8]})" for file in files]
            )
        else:
            if not _SESSION_FILE_PATTERN.match(path.name):
                raise ReplayInputError(f"{path} is not named like a Claude session file (<session id>.jsonl)")
            files = [path]
            names = [path.stem]
        for file, name in zip(files, names, strict=True):
            session_id = _session_id_of(file)
            sessions.append(
                SeededSession(
                    agent_id=_agent_id_for_session(session_id),
                    agent_name=name,
                    session_id=session_id,
                    events=_read_session_events(file),
                )
            )
    session_ids = [session.session_id for session in sessions]
    if len(set(session_ids)) != len(session_ids):
        raise ReplayInputError("The same session was named more than once")
    return sessions


def serve(sessions: list[SeededSession], chat_port: int, is_opening_browser: bool) -> None:
    """Serve ``sessions`` as chats until interrupted."""
    # A kill or a closed terminal stops like Ctrl-C, so the temp dir of transcript copies is deleted too.
    for stop_signal in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(stop_signal, signal.default_int_handler)
    first, *rest = sessions
    with tempfile.TemporaryDirectory(prefix="replay-transcripts-") as tmp:
        with running_workspace(
            Path(tmp),
            find_free_port(),
            chat_port,
            session_events=first.events,
            agent_id=first.agent_id,
            agent_name=first.agent_name,
            session_id=first.session_id,
            seeded_sessions=rest,
        ) as workspace:
            for session in sessions:
                logger.info(
                    "{}: {}{}", session.agent_name, workspace.chat_url, chat_root_path(ChatId(session.agent_id))
                )
            logger.info("Serving {} chat(s) at {}/ (Ctrl-C to stop)", len(sessions), workspace.chat_url)
            if is_opening_browser:
                webbrowser.open(f"{workspace.chat_url}{chat_root_path(ChatId(first.agent_id))}")
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
