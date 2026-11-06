"""Where the gateway's event streams live: ``$MNGR_AGENT_STATE_DIR/events/<source>/events.jsonl``.

The state dir is the services agent's, from the environment supervisord gives the agent's programs, so the
streams sit beside the workspace's other event logs, where ``mngr event``, the backups and the analytics
collection read them.
"""

from collections.abc import Mapping
from pathlib import Path
from typing import Final

EVENTS_FILENAME: Final[str] = "events.jsonl"


def events_dir_from_environ(environ: Mapping[str, str], event_source: str) -> Path | None:
    """The source's events directory under the services agent's state dir, or None when the state dir is unset."""
    state_dir = environ.get("MNGR_AGENT_STATE_DIR", "")
    if not state_dir:
        return None
    return Path(state_dir) / "events" / event_source
