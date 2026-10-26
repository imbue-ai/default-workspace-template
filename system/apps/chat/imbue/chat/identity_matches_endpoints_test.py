"""Tests for the `/api/identity-matches` routes: filing a look-up's candidates, the popup's answers, what
each change broadcasts to the chat pages, and what the agent is told -- over a recording bridge in place of
the router's."""

import json
from pathlib import Path

from app_schema.api_schema import DEFAULT_API_SCHEMA_PATH
from app_schema.api_schema import register_api_schema_route
from flask import Flask
from flask.testing import FlaskClient

from imbue.chat import identity_matches_endpoints
from imbue.chat.identity_matches import IdentityMatchStore
from imbue.chat.mock_secret_request_bridge_test import RecordingSecretRequestBridge
from imbue.chat.state import attach_state
from imbue.chat.testing import build_test_state

_CHAT = "agent-00000000000000000000000000000001"
_CANDIDATES = [
    {
        "name": "Ada Lovelace",
        "summary": "Mathematician, London",
        "source_url": "https://example.org/ada",
        "source_title": "Ada Lovelace",
    },
    {
        "name": "Ada King",
        "summary": "Countess of Lovelace",
        "source_url": "https://example.org/king",
        "source_title": "Ada King",
    },
]


class _Harness:
    def __init__(self, tmp_path: Path, bridge: RecordingSecretRequestBridge, is_secondary: bool = False) -> None:
        self.bridge = bridge
        self.store = IdentityMatchStore(matches_directory=tmp_path / "identity-matches")
        application = Flask(__name__)
        state = build_test_state(identity_matches=self.store, is_secondary=is_secondary)
        state.secret_request_bridge = bridge
        attach_state(application, state)
        register_api_schema_route(
            application,
            DEFAULT_API_SCHEMA_PATH,
            title="Chat",
            description="The identity-match routes alone",
            route_table=identity_matches_endpoints.ROUTE_TABLE,
        )
        identity_matches_endpoints.register_routes(application)
        self.client: FlaskClient = application.test_client()
        self._broadcasts = state.agent_manager.broadcaster.register()

    def broadcasts(self) -> list[dict]:
        """Every message broadcast since the last call; the routes broadcast on the test's own thread."""
        messages = []
        while not self._broadcasts.empty():
            text = self._broadcasts.get_nowait()
            assert text is not None
            messages.append(json.loads(text))
        return messages

    def file(self, candidates: list[dict] | None = None) -> dict:
        response = self.client.post(
            "/api/identity-matches",
            json={"chat_id": _CHAT, "searched_for": "Ada Lovelace, London", "candidates": candidates or _CANDIDATES},
        )
        assert response.status_code == 201, response.get_json()
        return response.get_json()


def _known(is_delivery_failing: bool = False) -> RecordingSecretRequestBridge:
    return RecordingSecretRequestBridge(known_chat_ids=frozenset({_CHAT}), is_delivery_failing=is_delivery_failing)


def test_filing_stores_the_match_and_broadcasts_it_to_the_pages(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, _known())

    filed = harness.file()

    assert filed["match_id"].startswith("identity-")
    assert filed["status"] == "pending"
    assert [candidate["name"] for candidate in filed["candidates"]] == ["Ada Lovelace", "Ada King"]
    assert filed["is_reply_delivered"] is None
    [message] = harness.broadcasts()
    assert message["type"] == "identity_match_updated"
    assert message["match"]["match_id"] == filed["match_id"]
    assert message["match"]["status"] == "pending"
    assert harness.bridge.delivered == []


def test_a_second_filing_broadcasts_the_first_as_superseded(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, _known())
    first = harness.file()
    harness.broadcasts()

    second = harness.file()

    statuses = [(message["match"]["match_id"], message["match"]["status"]) for message in harness.broadcasts()]
    assert statuses == [(first["match_id"], "superseded"), (second["match_id"], "pending")]


def test_confirming_tells_the_agent_who_in_the_users_words_and_closes_the_popup(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, _known())
    filed = harness.file()
    harness.broadcasts()

    response = harness.client.post(f"/api/identity-matches/{filed['match_id']}/confirm", json={"candidate_index": 0})

    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    assert body["status"] == "confirmed"
    assert body["chosen_index"] == 0
    assert body["is_reply_delivered"] is True
    assert harness.bridge.delivered == [
        (_CHAT, "That's me: Ada Lovelace, Mathematician, London (https://example.org/ada)")
    ]
    [message] = harness.broadcasts()
    assert message["match"]["status"] == "confirmed"


def test_rejecting_tells_the_agent_none_of_them(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, _known())
    filed = harness.file()

    response = harness.client.post(f"/api/identity-matches/{filed['match_id']}/reject")

    assert response.status_code == 200, response.get_json()
    assert response.get_json()["status"] == "rejected"
    assert harness.bridge.delivered == [(_CHAT, "None of those are me.")]


def test_an_answer_the_chat_cannot_take_is_still_recorded(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, _known(is_delivery_failing=True))
    filed = harness.file()

    response = harness.client.post(f"/api/identity-matches/{filed['match_id']}/reject")

    assert response.status_code == 200
    assert response.get_json()["is_reply_delivered"] is False
    assert harness.store.list_pending() == ()


def test_a_second_answer_is_refused(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, _known())
    filed = harness.file()
    harness.client.post(f"/api/identity-matches/{filed['match_id']}/reject")

    response = harness.client.post(f"/api/identity-matches/{filed['match_id']}/confirm", json={"candidate_index": 0})

    assert response.status_code == 409
    assert len(harness.bridge.delivered) == 1


def test_confirming_a_candidate_that_does_not_exist_is_refused(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, _known())
    filed = harness.file()

    response = harness.client.post(f"/api/identity-matches/{filed['match_id']}/confirm", json={"candidate_index": 5})

    assert response.status_code == 400
    assert harness.bridge.delivered == []


def test_an_unknown_match_is_404(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, _known())

    assert harness.client.get("/api/identity-matches/identity-ffffffffffffffffffffffffffffffff").status_code == 404
    assert (
        harness.client.post("/api/identity-matches/identity-ffffffffffffffffffffffffffffffff/reject").status_code
        == 404
    )


def test_a_read_returns_the_match_as_it_stands(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, _known())
    filed = harness.file()

    response = harness.client.get(f"/api/identity-matches/{filed['match_id']}")

    assert response.status_code == 200
    assert response.get_json()["searched_for"] == "Ada Lovelace, London"


def test_filing_for_an_unknown_chat_or_before_the_agent_list_is_read_is_refused(tmp_path: Path) -> None:
    unknown = _Harness(tmp_path / "unknown", RecordingSecretRequestBridge())
    not_ready = _Harness(tmp_path / "not-ready", RecordingSecretRequestBridge(is_ready=False))
    body = {"chat_id": _CHAT, "searched_for": "Ada", "candidates": _CANDIDATES}

    assert unknown.client.post("/api/identity-matches", json=body).status_code == 404
    assert not_ready.client.post("/api/identity-matches", json=body).status_code == 503
    assert unknown.store.list_pending() == () and not_ready.store.list_pending() == ()


def test_filing_refuses_a_candidate_that_links_anywhere_but_a_web_page(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, _known())
    bad = [{**_CANDIDATES[0], "source_url": "javascript:alert(1)"}]

    response = harness.client.post(
        "/api/identity-matches", json={"chat_id": _CHAT, "searched_for": "Ada", "candidates": bad}
    )

    assert response.status_code == 400
    assert harness.store.list_pending() == ()


def test_filing_refuses_no_candidates(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, _known())

    response = harness.client.post(
        "/api/identity-matches", json={"chat_id": _CHAT, "searched_for": "Ada", "candidates": []}
    )

    assert response.status_code == 400


def test_a_preview_chat_answers_no_popup(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, _known(), is_secondary=True)
    filed = harness.file()

    response = harness.client.post(f"/api/identity-matches/{filed['match_id']}/reject")

    assert response.status_code == 409
    assert harness.bridge.delivered == []
