"""Claude's session files, and moving a chat's between accounts for a rebind (spec 6).

Claude keeps a session under the config dir it runs with, at
``<config dir>/projects/<encoded work dir>/<session id>.jsonl`` with the session's subagents
beside it under ``<session id>/``. A chat's config dir is its account folder, so an agent
that changes account changes where claude looks for its sessions: ``claude --resume`` (the
launch chain mngr builds) finds nothing under the new dir and starts afresh, and the chat
app's watcher, which searches the same tree, loses the transcript. Moving the files with the
agent is what keeps a rebind a continuation rather than a new conversation.
"""

import os
import shutil
from pathlib import Path
from typing import Final

from loguru import logger as _loguru_logger

from imbue.imbue_common.ids import InvalidRandomIdError
from imbue.mngr.primitives import AgentId
from imbue.mngr_claude.claude_config import encode_claude_project_dir_name

logger = _loguru_logger

# Where mngr's SessionStart hook lists every session the agent has run, first mention first.
SESSION_ID_HISTORY_FILENAME: Final = "claude_session_id_history"
PROJECTS_DIRNAME: Final = "projects"


def claude_session_ids(agent_state_dir: Path, agent_id: str) -> tuple[str, ...]:
    """Every session id a claude agent has run under, in history order, the agent's own uuid included.

    The uuid is the agent's first session (mngr launches it with ``--session-id <uuid>``), so it
    is listed even for an agent whose hook has not written the history yet.
    """
    ordered: dict[str, None] = {}
    try:
        ordered[str(AgentId(agent_id).get_uuid())] = None
    except InvalidRandomIdError:
        logger.debug("Agent id {} is not an mngr id; no uuid session is assumed for it", agent_id)
    history_path = agent_state_dir / SESSION_ID_HISTORY_FILENAME
    if history_path.exists():
        for line in history_path.read_text().splitlines():
            parts = line.strip().split()
            if parts:
                ordered.setdefault(parts[0], None)
    return tuple(ordered)


def move_claude_sessions(session_ids: tuple[str, ...], source_config_dir: Path, target_config_dir: Path) -> list[Path]:
    """Move the named sessions' files and subagent trees from one config dir's projects tree to the other's.

    Each file keeps its place relative to ``projects/`` (claude files a session by its encoded
    work dir, and the rebound agent's work dir does not change). Idempotent: a session already
    at the destination, or nowhere, is skipped, so a resume after a partial move finishes it.
    Returns the paths moved into place.
    """
    if source_config_dir == target_config_dir:
        return []
    source_projects = source_config_dir / PROJECTS_DIRNAME
    if not source_projects.is_dir():
        return []
    moved: list[Path] = []
    for session_id in session_ids:
        session_file = find_session_file(source_projects, session_id)
        if session_file is None:
            continue
        relative_dir = session_file.parent.relative_to(source_projects)
        target_dir = target_config_dir / PROJECTS_DIRNAME / relative_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        for source_path in (session_file, session_file.parent / session_id):
            if not source_path.exists():
                continue
            target_path = target_dir / source_path.name
            if target_path.exists():
                logger.warning("Session file {} already exists at {}; leaving both in place", source_path, target_path)
                continue
            shutil.move(str(source_path), str(target_path))
            moved.append(target_path)
    return moved


def expected_session_file(projects_dir: Path, session_id: str, work_dir: str) -> Path | None:
    """The session's main file where claude files it for a session run from ``work_dir``, or None.

    Both spellings of the work dir are tried: claude names the directory for the cwd it
    saw, which is the resolved path when the work dir is reached through a symlink.
    """
    target_name = f"{session_id}.jsonl"
    for spelling in dict.fromkeys((work_dir, os.path.realpath(work_dir))):
        candidate = projects_dir / encode_claude_project_dir_name(Path(spelling)) / target_name
        if candidate.is_file():
            return candidate
    return None


def find_session_file(projects_dir: Path, session_id: str) -> Path | None:
    """The session's main file under a config dir's ``projects/`` tree, wherever claude filed it, or None.

    Only the project dirs directly under ``projects/`` are looked in: a main session file
    always sits there, and the trees below them hold subagent files, which are named
    differently.
    """
    target_name = f"{session_id}.jsonl"
    try:
        entries = os.scandir(projects_dir)
    except FileNotFoundError:
        return None
    with entries:
        for entry in entries:
            if entry.is_dir():
                candidate = Path(entry.path) / target_name
                if candidate.is_file():
                    return candidate
    return None


class MissingSessionScanSchedule:
    """When a session whose file has not been found may be scanned for again.

    A scan is due at once the first time an id is looked for; each miss then puts the
    next one off by a delay that doubles from ``first_delay_seconds`` up to
    ``max_delay_seconds``. Callers pass the current monotonic time in.
    """

    _first_delay_seconds: float
    _max_delay_seconds: float
    _next_scan_at_by_session: dict[str, float]
    _last_delay_by_session: dict[str, float]

    @classmethod
    def build(cls, first_delay_seconds: float, max_delay_seconds: float) -> "MissingSessionScanSchedule":
        schedule = cls.__new__(cls)
        schedule._first_delay_seconds = first_delay_seconds
        schedule._max_delay_seconds = max_delay_seconds
        schedule._next_scan_at_by_session = {}
        schedule._last_delay_by_session = {}
        return schedule

    def is_scan_due(self, session_id: str, now: float) -> bool:
        return now >= self._next_scan_at_by_session.get(session_id, now)

    def record_miss(self, session_id: str, now: float) -> float:
        """Put the next scan for ``session_id`` off, and return by how long."""
        previous_delay = self._last_delay_by_session.get(session_id)
        delay = (
            self._first_delay_seconds
            if previous_delay is None
            else min(previous_delay * 2, self._max_delay_seconds)
        )
        self._last_delay_by_session[session_id] = delay
        self._next_scan_at_by_session[session_id] = now + delay
        return delay

    def forget(self, session_id: str) -> None:
        self._next_scan_at_by_session.pop(session_id, None)
        self._last_delay_by_session.pop(session_id, None)
