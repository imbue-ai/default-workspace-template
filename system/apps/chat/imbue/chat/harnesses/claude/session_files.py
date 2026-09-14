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


def find_session_file(projects_dir: Path, session_id: str) -> Path | None:
    """The session's main file under a config dir's ``projects/`` tree, wherever claude filed it, or None."""
    target_name = f"{session_id}.jsonl"
    for root, _dirs, files in os.walk(str(projects_dir)):
        if target_name in files:
            return Path(root) / target_name
    return None
