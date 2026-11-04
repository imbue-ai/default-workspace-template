"""Whether a grok agent has a top-level turn in flight, and which of its turns Ctrl+C can cancel."""

from pathlib import Path

from imbue.mngr_grok.api import GrokRootTurn
from imbue.mngr_grok.api import get_root_turns
from imbue.mngr_grok.api import is_root_turn_in_flight


def is_hold_required(agent_state_dir: Path) -> bool:
    """Whether a held message must wait: a top-level turn, or a pending-wake placeholder, is in flight."""
    return is_root_turn_in_flight(agent_state_dir, is_pending_wake_counted=True)


def get_cancellable_turns(agent_state_dir: Path) -> frozenset[GrokRootTurn]:
    """The top-level turns in flight that Ctrl+C can cancel, each by its session and prompt id; a pending-wake
    placeholder is not one."""
    return frozenset(root_turn for root_turn in get_root_turns(agent_state_dir) if not root_turn.is_pending_wake)
