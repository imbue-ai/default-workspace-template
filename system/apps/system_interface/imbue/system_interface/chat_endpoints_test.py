"""The /api/chats twins must be observably identical to their /api/agents siblings.

The blueprint dispatches to the very same view functions, so the only thing
that can diverge is the chat->agent resolution in front of them; these tests
pin that seam: identity fallback for unrecorded chats, registry resolution for
recorded ones, and byte-identical responses either way.
"""

from collections.abc import Iterator

import pytest
from flask import Flask
from flask.testing import FlaskClient

from imbue.system_interface.agent_manager import AgentManager
from imbue.system_interface.harnesses.harness_type import HarnessType
from imbue.system_interface.models import AgentStateItem
from imbue.system_interface.models import ChatId
from imbue.system_interface.server import create_application
from imbue.system_interface.testing import build_test_state
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster

_AGENT_ID = "agent-" + "a" * 32

# Every aliased rule, as (suffix, method). Kept in sync with the registration
# table in ``create_application`` by ``test_every_agent_route_has_a_chat_twin``.
_TWINNED_RULES: tuple[tuple[str, str], ...] = (
    ("/events", "GET"),
    ("/events/some-event/detail", "GET"),
    ("/stream", "GET"),
    ("/message", "POST"),
    ("/model", "POST"),
    ("/model-options", "GET"),
    ("/powered-by", "GET"),
    ("/fast-mode-answered", "POST"),
    ("/interrupt", "POST"),
    ("/flush-queue", "POST"),
    ("/shoulder-tap-atomic", "POST"),
    ("/drain-to-composer", "POST"),
    ("/screen", "GET"),
    ("/destroy", "POST"),
    ("/start", "POST"),
    ("/stop", "POST"),
    ("/subagents/some-session/events", "GET"),
    ("/subagents/some-session/stream", "GET"),
)


@pytest.fixture()
def app() -> Iterator[Flask]:
    state = build_test_state(agent_manager=AgentManager.build(WebSocketBroadcaster()))
    application = create_application(state)
    application.config["TESTING"] = True
    yield application
    state.shutdown()


@pytest.fixture()
def client(app: Flask) -> FlaskClient:
    return app.test_client()


def _seed_agent(app: Flask, agent_id: str) -> None:
    from imbue.system_interface.app_context import state_of

    manager = state_of(app).agent_manager
    with manager._lock:
        manager._agents[agent_id] = AgentStateItem(
            id=agent_id,
            name="chat-1",
            state="RUNNING",
            labels={"user_created": "true"},
            work_dir=None,
            harness=HarnessType.CODEX,
        )


@pytest.mark.parametrize(("suffix", "method"), _TWINNED_RULES)
def test_unknown_id_answers_identically_on_both_families(client: FlaskClient, suffix: str, method: str) -> None:
    unknown = "agent-" + "f" * 32
    agent_response = client.open(f"/api/agents/{unknown}{suffix}", method=method, json={})
    chat_response = client.open(f"/api/chats/{unknown}{suffix}", method=method, json={})

    assert chat_response.status_code == agent_response.status_code
    assert chat_response.get_data() == agent_response.get_data()
    # The twins must never silently 404 at the ROUTING layer (a missing rule
    # would also produce a 404); both answer the handler's own JSON error.
    assert agent_response.status_code == 404
    assert b"not found" in agent_response.get_data()


def test_a_seeded_agent_answers_identically_on_both_families(app: Flask, client: FlaskClient) -> None:
    _seed_agent(app, _AGENT_ID)

    agent_response = client.get(f"/api/agents/{_AGENT_ID}/powered-by")
    chat_response = client.get(f"/api/chats/{_AGENT_ID}/powered-by")

    assert agent_response.status_code == 200
    assert chat_response.status_code == agent_response.status_code
    assert chat_response.get_data() == agent_response.get_data()


def test_resolution_follows_the_registry_record(app: Flask, client: FlaskClient) -> None:
    from imbue.system_interface.app_context import state_of

    _seed_agent(app, _AGENT_ID)
    # Record the chat explicitly (what the bootstrap does); resolution must go
    # through the record rather than the identity fallback.
    state_of(app).chat_registry.ensure_chat(
        ChatId(_AGENT_ID), agent_id=_AGENT_ID, harness=HarnessType.CODEX, account_id=None
    )

    chat_response = client.get(f"/api/chats/{_AGENT_ID}/powered-by")

    assert chat_response.status_code == 200


def test_every_agent_route_has_a_chat_twin(app: Flask) -> None:
    """Every /api/agents/<agent_id>/... rule is mirrored under /api/chats/<chat_id>/...

    Guards the registration table in ``create_application``: a new agent route
    added without its chat twin fails here, so the chat family cannot silently
    fall behind the physical one.
    """
    agent_rules: set[tuple[str, frozenset[str]]] = set()
    chat_rules: set[tuple[str, frozenset[str]]] = set()
    for rule in app.url_map.iter_rules():
        methods = frozenset((rule.methods or set()) - {"HEAD", "OPTIONS"})
        text = str(rule)
        if text.startswith("/api/agents/<agent_id>"):
            agent_rules.add((text.removeprefix("/api/agents/<agent_id>"), methods))
        if text.startswith("/api/chats/<chat_id>"):
            chat_rules.add((text.removeprefix("/api/chats/<chat_id>"), methods))
    assert agent_rules, "expected /api/agents/<agent_id> rules to exist"
    assert agent_rules == chat_rules
