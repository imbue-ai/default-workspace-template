"""Whether a grok agent has a top-level turn in flight, and the per-agent interlock on its Ctrl+C."""

import threading
import time
from pathlib import Path
from typing import Final

from imbue.mngr_grok.api import GrokRootTurn
from imbue.mngr_grok.api import get_root_turns
from imbue.mngr_grok.api import is_root_turn_in_flight

# mngr's own spacing between two Ctrl+C presses to one grok: a second press within about a second of a press that
# found no turn to cancel quits grok.
MIN_PRESS_INTERVAL_SECONDS: Final[float] = 1.5


def is_hold_required(agent_state_dir: Path) -> bool:
    """Whether a held message must wait: a top-level turn, or a pending-wake placeholder, is in flight."""
    return is_root_turn_in_flight(agent_state_dir, is_pending_wake_counted=True)


def get_cancellable_turns(agent_state_dir: Path) -> frozenset[GrokRootTurn]:
    """The top-level turns in flight that Ctrl+C can cancel, each by its session and prompt id; a pending-wake
    placeholder is not one."""
    return frozenset(root_turn for root_turn in get_root_turns(agent_state_dir) if not root_turn.is_pending_wake)


class GrokTurnState:
    """One grok agent's Ctrl+C interlock, shared by stop and the shoulder tap."""

    _lock: threading.Lock
    _last_press_at: float | None
    min_press_interval_seconds: float

    @classmethod
    def build(cls) -> "GrokTurnState":
        self = cls.__new__(cls)
        self._lock = threading.Lock()
        self._last_press_at = None
        self.min_press_interval_seconds = MIN_PRESS_INTERVAL_SECONDS
        return self

    def try_claim_press(self, *, now: float | None = None) -> float | None:
        """Reserve one Ctrl+C and return when it was reserved; None when the previous one was too recent."""
        moment = time.monotonic() if now is None else now
        with self._lock:
            if self._last_press_at is not None and moment - self._last_press_at < self.min_press_interval_seconds:
                return None
            self._last_press_at = moment
            return moment

    def release_press(self, claimed_at: float) -> None:
        """Give back a reserved Ctrl+C that mngr refused, unless a later one was reserved since."""
        with self._lock:
            if self._last_press_at == claimed_at:
                self._last_press_at = None


_STATES: Final[dict[str, GrokTurnState]] = {}
_STATES_LOCK: Final[threading.Lock] = threading.Lock()


def get_turn_state(agent_id: str) -> GrokTurnState:
    with _STATES_LOCK:
        existing = _STATES.get(agent_id)
        if existing is not None:
            return existing
        created = GrokTurnState.build()
        _STATES[agent_id] = created
        return created


def drop_turn_state(agent_id: str) -> None:
    with _STATES_LOCK:
        _STATES.pop(agent_id, None)
