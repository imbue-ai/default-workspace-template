"""Recording the model calls of harnesses that report no usage of their own.

Antigravity exposes no per-call tokens or cost, so nothing inside the agent
can write its usage. The chat app sees every turn start and end, and records
each finished turn as one ``model_call`` with no tokens and no cost: the
workspace's usage shows that the chat used the model, and that what it spent
is unknown, rather than nothing. The call is the whole turn, so it carries
that turn's id as its ``turn_id``.
"""

import os
import threading
from collections.abc import Callable
from collections.abc import Mapping
from datetime import datetime
from datetime import timezone
from typing import Final

from workspace_usage.context import ORIGIN_ID
from workspace_usage.context import ORIGIN_KIND
from workspace_usage.context import ORIGIN_KIND_CHAT
from workspace_usage.context import read_workspace_id
from workspace_usage.emit import record_model_call
from workspace_usage.events import build_model_call

from imbue.chat.harnesses.harness_type import HarnessType

# The harnesses whose turns the chat app records itself.
TURN_RECORDED_HARNESSES: Final[frozenset[HarnessType]] = frozenset({HarnessType.ANTIGRAVITY})
_PROVIDER_BY_HARNESS: Final[dict[HarnessType, str]] = {HarnessType.ANTIGRAVITY: "google"}


class UnreportedTurnRecorder:
    """Turns of harnesses in ``TURN_RECORDED_HARNESSES``: noted when they start, recorded when they end."""

    def __init__(
        self,
        emit: Callable[[Mapping[str, object]], object] = record_model_call,
        workspace_id: str | None = None,
    ) -> None:
        self._emit = emit
        self._workspace_id = workspace_id if workspace_id is not None else read_workspace_id(os.environ)
        self._lock = threading.Lock()
        self._started_at_by_agent: dict[str, datetime] = {}

    def turn_started(self, agent_id: str, harness: HarnessType, at: datetime) -> None:
        if harness not in TURN_RECORDED_HARNESSES:
            return
        with self._lock:
            self._started_at_by_agent[agent_id] = at

    def turn_ended(
        self, agent_id: str, harness: HarnessType, chat_id: str | None, model_id: str | None, at: datetime
    ) -> None:
        """Record the turn ``agent_id`` just finished; a turn whose start was not seen starts when it ends."""
        if harness not in TURN_RECORDED_HARNESSES:
            return
        with self._lock:
            started_at = self._started_at_by_agent.pop(agent_id, at)
        context = {ORIGIN_KIND: ORIGIN_KIND_CHAT, ORIGIN_ID: chat_id} if chat_id else {}
        turn_id = f"{harness.value}-{agent_id}-{int(started_at.timestamp() * 1000)}"
        event = build_model_call(
            event_id=turn_id,
            started_at=started_at,
            ended_at=max(at, started_at),
            provider=_PROVIDER_BY_HARNESS[harness],
            model=model_id,
            context=context,
            workspace_id=self._workspace_id,
            harness=harness.value,
            purpose="TURN",
            turn_id=turn_id,
        )
        attribution = event["attribution"]
        if isinstance(attribution, dict):
            attribution["agent_id"] = agent_id
            attribution["call_source"] = "MAIN"
        self._emit(event)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
