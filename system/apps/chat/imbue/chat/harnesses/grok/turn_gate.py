"""Whether mngr reports a grok turn in flight: what holds the messages the chat keeps for grok."""

from pathlib import Path

from imbue.chat.activity_state import ACTIVE_MARKER_FILENAME
from imbue.chat.activity_state import resolve_is_turn_in_flight


def is_active_marker_present(agent_state_dir: Path) -> bool:
    """Whether mngr's ``active`` marker is on disk: a top-level turn, a foreground subagent or an expected wake turn."""
    return (agent_state_dir / ACTIVE_MARKER_FILENAME).exists()


def is_turn_in_flight(lifecycle_state: str, agent_state_dir: Path) -> bool:
    """Whether mngr reports a turn in flight, by the agent's lifecycle state and its ``active`` marker."""
    return resolve_is_turn_in_flight(lifecycle_state, is_active_marker_present(agent_state_dir))
