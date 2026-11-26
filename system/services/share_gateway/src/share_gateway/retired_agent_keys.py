"""Removal of the ``authorized_keys`` lines a visiting agent's grant used to carry.

A release that ran the owner-exec daemon wrote one line for each agent granted the
whole workspace, because the daemon checked an agent's signature against that
file. The gateway admits an agent by its token alone, so nothing reads those
lines, while sshd honours each one for as long as it stays in the file.

CLEANUP: remove this module and its call in ``runner`` once every workspace has
started a release without the owner-exec daemon.
"""

import fcntl
from pathlib import Path

from share_grants.grants import write_text_atomic

DEFAULT_AUTHORIZED_KEYS_PATH = Path.home() / ".ssh" / "authorized_keys"
# The comment that marked an ``authorized_keys`` line as written for a visiting agent.
AGENT_KEY_LINE_MARKER = "imbue-agent-access"


def _is_agent_key_line(line: str) -> bool:
    return f" {AGENT_KEY_LINE_MARKER} " in line


def remove_agent_key_lines(authorized_keys_path: Path) -> bool:
    """Remove every visiting agent's line from ``authorized_keys_path``; True when the file changed.

    Every other line (the owner's keys, the desktop's, the cross-workspace SSH
    grants) is kept as it is, in its order. The read-and-replace holds the
    ``authorized_keys`` lock, which the file's other writers take around their
    own read-and-write, and replaces the file atomically ``0600``, so sshd keeps
    honouring it. A file with no such line is left untouched and unlocked.
    """
    try:
        if not any(_is_agent_key_line(line) for line in authorized_keys_path.read_text().splitlines()):
            return False
    except FileNotFoundError:
        return False
    lock_path = authorized_keys_path.with_name(authorized_keys_path.name + ".lock")
    with open(lock_path, "a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            lines = authorized_keys_path.read_text().splitlines()
            kept = [line for line in lines if not _is_agent_key_line(line)]
            if len(kept) == len(lines):
                return False
            write_text_atomic(authorized_keys_path, ("\n".join(kept) + "\n") if kept else "", 0o600)
            return True
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
