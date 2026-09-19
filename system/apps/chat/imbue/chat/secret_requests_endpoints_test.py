"""Tests for the `/api/secret-requests` routes: filing, the card's submit and decline, hydration,
and the notice each verdict puts into the chat -- with a recording delivery in place of the router's."""

from pathlib import Path

from flask import Flask
from flask.testing import FlaskClient

from imbue.chat import secret_requests_endpoints
from imbue.chat.secret_requests import SecretRequestStore
from imbue.chat.secret_requests_endpoints import ChatLookup
from imbue.chat.secret_requests_endpoints import NoticeDeliveryError
from imbue.chat.state import attach_state
from imbue.chat.testing import build_test_state

_CHAT = "agent-00000000000000000000000000000001"
_OTHER_CHAT = "agent-00000000000000000000000000000002"


class _Router:
    """The two things the routes borrow from the router, recorded."""

    def __init__(self, known: frozenset[str], is_ready: bool = True, is_delivery_failing: bool = False) -> None:
        self.known = known
        self.is_ready = is_ready
        self.is_delivery_failing = is_delivery_failing
        self.delivered: list[tuple[str, str]] = []

    def lookup(self, chat_id: str) -> ChatLookup:
        if not self.is_ready:
            return ChatLookup.NOT_READY
        return ChatLookup.KNOWN if chat_id in self.known else ChatLookup.UNKNOWN

    def deliver(self, chat_id: str, text: str) -> None:
        if self.is_delivery_failing:
            raise NoticeDeliveryError("the agent is away")
        self.delivered.append((chat_id, text))


def _client(tmp_path: Path, router: _Router) -> tuple[FlaskClient, SecretRequestStore]:
    store = SecretRequestStore(
        requests_directory=tmp_path / "requests", secrets_directory=tmp_path / "data" / ".secrets"
    )
    application = Flask(__name__)
    attach_state(application, build_test_state(secret_requests=store))
    secret_requests_endpoints.register_routes(application, router.lookup, router.deliver)
    return application.test_client(), store


def _file(client: FlaskClient, chat_id: str = _CHAT, file: str = "svc", variables: list[str] | None = None) -> dict:
    response = client.post(
        "/api/secret-requests",
        json={"chat_id": chat_id, "file": file, "variables": variables or ["SVC_TOKEN"], "rationale": "to call it"},
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()


def test_filing_returns_what_the_card_and_the_agent_need(tmp_path: Path) -> None:
    client, _ = _client(tmp_path, _Router(frozenset({_CHAT})))
    filed = _file(client, variables=["SVC_TOKEN", "SVC_URL"])
    assert filed["request_id"].startswith("secret-")
    assert filed["file"] == "svc"
    assert filed["variables"] == ["SVC_TOKEN", "SVC_URL"]
    assert filed["env_path"] == "data/.secrets/svc.env"
    assert filed["status"] == "pending"
    assert filed["existing_variables"] == []
    assert filed["overwrites"] == []


def test_a_submit_writes_the_file_and_tells_the_chat_the_names_not_the_values(tmp_path: Path) -> None:
    router = _Router(frozenset({_CHAT}))
    client, _ = _client(tmp_path, router)
    filed = _file(client, variables=["SVC_TOKEN", "SVC_URL"])
    value = "sk-" + "q" * 30

    response = client.post(
        f"/api/secret-requests/{filed['request_id']}/submit", json={"values": {"SVC_TOKEN": value, "SVC_URL": "u"}}
    )

    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    assert body["status"] == "stored"
    assert body["is_notice_delivered"] is True
    assert value not in response.get_data(as_text=True)
    assert (tmp_path / "data" / ".secrets" / "svc.env").read_text() == f"SVC_TOKEN='{value}'\nSVC_URL='u'\n"
    [(chat_id, notice)] = router.delivered
    assert chat_id == _CHAT
    assert (
        notice
        == f"Secret stored: data/.secrets/svc.env (SVC_TOKEN, SVC_URL) (secret: stored, request_id: {filed['request_id']})"
    )
    assert client.get(f"/api/secret-requests/{filed['request_id']}").get_json()["status"] == "stored"


def test_a_decline_carries_the_note_into_the_chat(tmp_path: Path) -> None:
    router = _Router(frozenset({_CHAT}))
    client, _ = _client(tmp_path, router)
    filed = _file(client)

    response = client.post(
        f"/api/secret-requests/{filed['request_id']}/decline", json={"note": "use the other account"}
    )

    assert response.status_code == 200
    assert response.get_json()["status"] == "declined"
    [(_, notice)] = router.delivered
    assert notice == (
        f"Secret declined: data/.secrets/svc.env (SVC_TOKEN) (secret: declined, request_id: {filed['request_id']}) "
        "use the other account"
    )
    assert not (tmp_path / "data" / ".secrets" / "svc.env").exists()


def test_the_file_is_kept_when_the_chat_cannot_take_the_notice(tmp_path: Path) -> None:
    client, _ = _client(tmp_path, _Router(frozenset({_CHAT}), is_delivery_failing=True))
    filed = _file(client)
    response = client.post(f"/api/secret-requests/{filed['request_id']}/submit", json={"values": {"SVC_TOKEN": "v"}})
    assert response.status_code == 200
    assert response.get_json()["is_notice_delivered"] is False
    assert (tmp_path / "data" / ".secrets" / "svc.env").read_text() == "SVC_TOKEN='v'\n"


def test_a_newer_request_supersedes_the_pending_one_and_a_submit_on_it_is_refused(tmp_path: Path) -> None:
    router = _Router(frozenset({_CHAT, _OTHER_CHAT}))
    client, _ = _client(tmp_path, router)
    first = _file(client)
    second = _file(client, chat_id=_OTHER_CHAT)

    assert client.get(f"/api/secret-requests/{first['request_id']}").get_json()["status"] == "superseded"
    refused = client.post(f"/api/secret-requests/{first['request_id']}/submit", json={"values": {"SVC_TOKEN": "v"}})
    assert refused.status_code == 409
    # The superseded request lived in another chat, which learns of it only through a notice.
    assert router.delivered == [
        (
            _CHAT,
            f"Secret request superseded by a newer request for data/.secrets/svc.env (secret: superseded, request_id: {first['request_id']})",
        )
    ]
    assert client.get(f"/api/secret-requests/{second['request_id']}").get_json()["status"] == "pending"


def test_a_supersession_within_one_chat_sends_no_notice(tmp_path: Path) -> None:
    router = _Router(frozenset({_CHAT}))
    client, _ = _client(tmp_path, router)
    _file(client)
    _file(client)
    assert router.delivered == []


def test_filing_answers_like_the_message_route_for_an_unknown_or_not_yet_known_chat(tmp_path: Path) -> None:
    client, _ = _client(tmp_path, _Router(frozenset({_CHAT})))
    body = {"chat_id": _OTHER_CHAT, "file": "svc", "variables": ["A"], "rationale": "why"}
    assert client.post("/api/secret-requests", json=body).status_code == 404
    not_ready_client, _ = _client(tmp_path / "b", _Router(frozenset(), is_ready=False))
    assert not_ready_client.post("/api/secret-requests", json=body).status_code == 503


def test_malformed_bodies_are_400s_and_unknown_ids_are_404s(tmp_path: Path) -> None:
    client, _ = _client(tmp_path, _Router(frozenset({_CHAT})))
    assert client.post("/api/secret-requests", data="nope", content_type="application/json").status_code == 400
    assert (
        client.post(
            "/api/secret-requests", json={"chat_id": _CHAT, "file": "Bad", "variables": ["A"], "rationale": "r"}
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/secret-requests", json={"chat_id": _CHAT, "file": "svc", "variables": "A", "rationale": "r"}
        ).status_code
        == 400
    )
    filed = _file(client)
    assert (
        client.post(f"/api/secret-requests/{filed['request_id']}/submit", json={"values": {"OTHER": "v"}}).status_code
        == 400
    )
    assert client.post(f"/api/secret-requests/{filed['request_id']}/submit", json={"values": "v"}).status_code == 400
    assert client.post(f"/api/secret-requests/{filed['request_id']}/decline", json={"note": 3}).status_code == 400
    assert client.get("/api/secret-requests/secret-00000000000000000000000000000000").status_code == 404
    assert client.post("/api/secret-requests/nope/submit", json={"values": {}}).status_code == 404
