"""Per-agent record of the user-turn event ids the live codex ledger has broadcast.

The codex file watcher suppresses ``user_message`` events from its live broadcast on the
assumption that the subscribed ledger owns the live user-turn (the A3b ordered handoff:
chip out, then turn). That assumption has a hole: a connection built against a daemon
that is still starting -- the stopped-agent revive path -- can miss the commit
notification entirely, and the turn then never reaches the live stream at all. The
frontend's "Sending..." bubble clears only on that arrival, so it hangs forever while
the store (and any reload) has the turn.

So the suppression is conditional on this record: the watcher drops a file-read
user_message only when the ledger has already broadcast the same event id (the two
copies share the id -- the rollout stores the clientUserMessageId the ledger keys by).
The ledger normally hears the commit within milliseconds while the file tail follows a
filesystem wake plus an emit cycle, so in the healthy case the ledger still wins and the
file copy stays suppressed; when the ledger is deaf, the file copy flows and heals the
stream. A double delivery in the rare reversed race is harmless: the frontend dedups
transcript events and Sending-bubble arrivals by id.

Module-level like the codex queue tracker: the record must survive session and watcher
rebuilds, which happen independently around a stop/revive. Bounded per agent because
only the fresh-turn race window ever matters.
"""

import threading
from collections import deque

_MAX_REMEMBERED_TURNS = 512

_order_by_agent: dict[str, deque[str]] = {}
_members_by_agent: dict[str, set[str]] = {}
_lock = threading.Lock()


def note_live_user_turn(agent_id: str, event_id: str) -> None:
    """Record that the live ledger has broadcast ``event_id`` for ``agent_id``."""
    with _lock:
        order = _order_by_agent.setdefault(agent_id, deque())
        members = _members_by_agent.setdefault(agent_id, set())
        if event_id in members:
            return
        order.append(event_id)
        members.add(event_id)
        while len(order) > _MAX_REMEMBERED_TURNS:
            members.discard(order.popleft())


def was_live_user_turn_broadcast(agent_id: str, event_id: str) -> bool:
    """Whether the live ledger has already broadcast ``event_id`` for ``agent_id``."""
    with _lock:
        members = _members_by_agent.get(agent_id)
        return members is not None and event_id in members


def drop_live_user_turns(agent_id: str) -> None:
    """Forget ``agent_id``'s record -- called when the agent is destroyed (and by tests,
    which share one agent id across a module-level registry)."""
    with _lock:
        _order_by_agent.pop(agent_id, None)
        _members_by_agent.pop(agent_id, None)
