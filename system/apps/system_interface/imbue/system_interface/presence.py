"""Per-agent, per-client presence: which clients have a chat's page open, and whether it is showing.

The chat page reports its own presence to the chat app (contracts.md section 10 and
``phase_06_chat_as_document.md``): ``hidden`` once the shell has handed it its handshake,
``visible`` on ``shell:shown``, ``hidden`` again on ``shell:hidden``, ``closed`` on
``pagehide``, and a heartbeat of its current state every minute. Only the chat's own page
reports: a subagent view is a second page of the same chat in the same client, and one
standing report per chat and client is kept here, so its reports would overwrite the chat
page's. The OOM prioritizer reads the aggregate: a chat is *open* while any client has an
unexpired report, and *visible* while any client's last report says so. A report expires
after ten minutes, so a page that vanished without its ``pagehide`` (a crashed tab, a lost
laptop) stops counting on its own.
"""

import threading
import time
from collections.abc import Callable
from enum import auto
from typing import Final

from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel

# A report that has not been refreshed for this long no longer counts. Ten heartbeats,
# so a page has to miss every one before its chat reads as closed.
PRESENCE_EXPIRY_SECONDS: Final[float] = 600.0

# How often the page re-reports its current state.
PRESENCE_HEARTBEAT_SECONDS: Final[float] = 60.0


class PresenceState(LowerCaseStrEnum):
    """What one client last said about one chat's page (a wire value of the presence route)."""

    VISIBLE = auto()
    HIDDEN = auto()
    CLOSED = auto()


class PresenceReport(FrozenModel):
    """The body of ``POST /api/agents/<id>/presence``."""

    client_id: str = Field(min_length=1, description="The reporting client, as the shell's handshake named it")
    state: PresenceState = Field(description="The page's state in that client")


class _ClientPresence(FrozenModel):
    """One client's standing report about one chat."""

    state: PresenceState = Field(description="visible or hidden; a closed report deletes the record instead")
    reported_at: float = Field(description="Wall-clock epoch seconds of the report, for expiry")


class PresenceTracker(MutableModel):
    """Holds every client's last presence report per chat and answers the aggregate questions.

    Thread-safe: reports arrive on request threads while the prioritizer's sweep reads.
    ``clock`` supplies wall-clock epoch seconds so tests can advance time explicitly.
    """

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    clock: Callable[[], float] = Field(default=time.time, frozen=True, description="Wall-clock epoch seconds")
    expiry_seconds: float = Field(
        default=PRESENCE_EXPIRY_SECONDS, frozen=True, description="How long an unrefreshed report counts"
    )

    _presence_by_client_by_agent: dict[str, dict[str, _ClientPresence]] = PrivateAttr(default_factory=dict)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def record(self, agent_id: str, client_id: str, state: PresenceState) -> None:
        """Replace ``client_id``'s standing report about ``agent_id``; ``CLOSED`` drops it."""
        with self._lock:
            by_client = self._presence_by_client_by_agent.setdefault(agent_id, {})
            if state is PresenceState.CLOSED:
                by_client.pop(client_id, None)
                if not by_client:
                    del self._presence_by_client_by_agent[agent_id]
                return
            by_client[client_id] = _ClientPresence(state=state, reported_at=self.clock())

    def forget_agent(self, agent_id: str) -> None:
        with self._lock:
            self._presence_by_client_by_agent.pop(agent_id, None)

    def is_open(self, agent_id: str) -> bool:
        """Whether any client holds an unexpired report about the chat, visible or hidden."""
        return len(self._live_reports(agent_id)) > 0

    def is_visible(self, agent_id: str) -> bool:
        """Whether any client's unexpired last report says the chat is showing."""
        return any(report.state is PresenceState.VISIBLE for report in self._live_reports(agent_id))

    def open_agent_ids(self) -> set[str]:
        with self._lock:
            agent_ids = list(self._presence_by_client_by_agent)
        return {agent_id for agent_id in agent_ids if self.is_open(agent_id)}

    def visible_agent_ids(self) -> set[str]:
        with self._lock:
            agent_ids = list(self._presence_by_client_by_agent)
        return {agent_id for agent_id in agent_ids if self.is_visible(agent_id)}

    def _live_reports(self, agent_id: str) -> list[_ClientPresence]:
        """The unexpired reports about ``agent_id``, dropping the expired ones as they are found."""
        now = self.clock()
        with self._lock:
            by_client = self._presence_by_client_by_agent.get(agent_id)
            if by_client is None:
                return []
            expired_client_ids = [
                client_id for client_id, report in by_client.items() if now - report.reported_at > self.expiry_seconds
            ]
            for client_id in expired_client_ids:
                del by_client[client_id]
            if not by_client:
                del self._presence_by_client_by_agent[agent_id]
            return list(by_client.values())
