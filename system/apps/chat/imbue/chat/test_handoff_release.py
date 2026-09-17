"""A handoff driven through the API against real agents on two real accounts (spec section 8, Phase 4).

Needs the two credentials below in the environment, plus ``mngr``, ``claude``, and ``pi`` on the
PATH, so it skips everywhere those are absent (CI has neither key yet). It creates a claude chat
on an Anthropic account, sends it one message, moves it to an OpenRouter (pi) account through
``POST /api/chats/<id>/handoff``, and reads the chat back as one transcript with the switch chip
between the two agents; the agents it makes are destroyed at the end.
"""

import os
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from flask import Flask

from imbue.chat.agent_manager import AgentManager
from imbue.chat.chat_transcript import AGENT_SWITCH_EVENT_TYPE
from imbue.chat.harnesses.auth_flows import FlowState
from imbue.chat.harnesses.harness_type import HarnessType
from imbue.chat.harnesses.lanes import LANE_ANTHROPIC
from imbue.chat.harnesses.lanes import LANE_OPENROUTER
from imbue.chat.harnesses.lanes import Lane
from imbue.chat.harnesses.lanes import PasteMethod
from imbue.chat.models import HandoffPhase
from imbue.chat.primitives import ChatId
from imbue.chat.server import create_application
from imbue.chat.state import state_of
from imbue.chat.testing import build_test_state
from imbue.chat.ws_broadcaster import WebSocketBroadcaster
from imbue.mngr.utils.polling import wait_for

ANTHROPIC_KEY_ENV: str = "MINDS_HANDOFF_E2E_ANTHROPIC_API_KEY"
OPENROUTER_KEY_ENV: str = "MINDS_HANDOFF_E2E_OPENROUTER_API_KEY"

# A real create waits for readiness (45s in this workspace) and a real summary turn can take a
# while; the whole handoff is given the summary's own cap plus the two creates.
_HANDOFF_TIMEOUT_SECONDS: float = 480.0
_CREATE_TIMEOUT_SECONDS: float = 120.0


def _missing_requirements() -> list[str]:
    missing = [name for name in (ANTHROPIC_KEY_ENV, OPENROUTER_KEY_ENV) if not os.environ.get(name)]
    missing.extend(f"{binary} on PATH" for binary in ("mngr", "claude", "pi") if shutil.which(binary) is None)
    return missing


pytestmark = [
    pytest.mark.release,
    pytest.mark.skipif(bool(_missing_requirements()), reason=f"needs {', '.join(_missing_requirements())}"),
]


def _sign_in_with_key(app: Flask, lane: Lane, api_key: str, key_provider: str | None) -> str:
    """Sign one account in through the app's own paste flow, and return its id.

    The test state's flow service probes nothing (its probe answers UNKNOWN), so the key
    commits without a harness CLI check.
    """
    method = next(method for method in lane.methods if isinstance(method, PasteMethod))
    auth_flows = state_of(app).auth_flows
    started = auth_flows.start(lane.id, method.id)
    status = auth_flows.submit_key(started.flow_id, api_key, key_provider)
    assert status.state is FlowState.OK and status.account_id is not None, status
    return status.account_id


@pytest.fixture
def handoff_app(tmp_path: Path) -> Iterator[Flask]:
    manager = AgentManager.build(WebSocketBroadcaster(), chat_files_root=tmp_path / "chats")
    state = build_test_state(agent_manager=manager)
    app = create_application(state)
    manager.start()
    try:
        yield app
    finally:
        state.shutdown()


def _events(app: Flask, chat_id: str) -> dict[str, Any]:
    response = app.test_client().get(f"/api/chats/{chat_id}/events")
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()


def test_a_chat_moves_from_claude_to_pi_and_reads_as_one_transcript(handoff_app: Flask) -> None:
    anthropic_id = _sign_in_with_key(handoff_app, LANE_ANTHROPIC, os.environ[ANTHROPIC_KEY_ENV], None)
    openrouter_id = _sign_in_with_key(handoff_app, LANE_OPENROUTER, os.environ[OPENROUTER_KEY_ENV], "openrouter")
    client = handoff_app.test_client()
    manager: AgentManager = state_of(handoff_app).agent_manager

    created = manager.create_chat("Handoff e2e", account_id=anthropic_id)
    chat_id = created.chat_id
    try:
        wait_for(
            lambda: manager.get_provisional_chat(chat_id) is None and manager.get_chat_snapshot(chat_id) is not None,
            timeout=_CREATE_TIMEOUT_SECONDS,
        )
        sent = client.post(
            f"/api/chats/{chat_id}/message", json={"message": "Remember the word pelican.", "message_id": "m-1"}
        )
        assert sent.status_code == 200, sent.get_data(as_text=True)
        wait_for(lambda: _events(handoff_app, chat_id)["total"] >= 2, timeout=_CREATE_TIMEOUT_SECONDS)

        switched = client.post(
            f"/api/chats/{chat_id}/handoff",
            json={"account_id": openrouter_id, "message": "What word did I ask you to remember?", "message_id": "m-2"},
        )
        assert switched.status_code == 202, switched.get_data(as_text=True)
        assert switched.get_json()["phase"] in (HandoffPhase.SUMMARIZING.value, HandoffPhase.SWITCHING.value)

        def is_settled() -> bool:
            snapshot = manager.get_chat_snapshot(chat_id)
            return snapshot is not None and snapshot.handoff is None and len(snapshot.agent_ids) == 2

        wait_for(is_settled, timeout=_HANDOFF_TIMEOUT_SECONDS)
        snapshot = manager.get_chat_snapshot(chat_id)
        assert snapshot is not None
        assert snapshot.active_agent.harness is HarnessType.PI_CODING
        assert snapshot.active_agent.account_id == openrouter_id
        assert (snapshot.title, snapshot.name) == ("Handoff e2e", "Handoff-e2e")

        record = manager._chat_record_store.read(ChatId(chat_id))
        assert record is not None and record.handoff is None
        assert [entry.harness for entry in record.agents] == [HarnessType.CLAUDE, HarnessType.PI_CODING]
        assert record.agents[0].final_event_count is not None and record.agents[0].final_event_count >= 2

        whole = _events(handoff_app, chat_id)
        types = [event["type"] for event in whole["events"]]
        assert AGENT_SWITCH_EVENT_TYPE in types
        switch_index = types.index(AGENT_SWITCH_EVENT_TYPE)
        assert whole["events"][switch_index]["from_harness"] == HarnessType.CLAUDE.value
        assert whole["events"][switch_index]["to_harness"] == HarnessType.PI_CODING.value
        assert {event["agent_id"] for event in whole["events"][:switch_index]} == {record.agents[0].agent_id}
        # The successor's first turn is the handoff prompt carrying the user's message.
        successor_turns = [
            event
            for event in whole["events"][switch_index + 1 :]
            if event["type"] == "user_message" and event["agent_id"] == record.agents[1].agent_id
        ]
        assert successor_turns and "What word did I ask you to remember?" in successor_turns[0]["content"]
    finally:
        if manager.get_chat_snapshot(chat_id) is not None:
            manager.destroy_chat(chat_id)
