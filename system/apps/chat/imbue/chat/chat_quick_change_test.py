"""Unit tests for quick look-and-feel change routing (``chat_quick_change.py``).

Jev is never reached: the router is given a fake ``call_jev``, and ``ask_jev`` is driven through an
``httpx.MockTransport`` with a dummy key.
"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from imbue.chat.chat_quick_change import JEV_MODEL
from imbue.chat.chat_quick_change import JEV_ROUTE_PATH
from imbue.chat.chat_quick_change import JevCredentials
from imbue.chat.chat_quick_change import JevOutcome
from imbue.chat.chat_quick_change import JevOutcomeStatus
from imbue.chat.chat_quick_change import OPENROUTER_JEV_URL
from imbue.chat.chat_quick_change import OPENROUTER_KEY_FILENAME
from imbue.chat.chat_quick_change import QUICK_CHANGE_AGENT_FILENAME
from imbue.chat.chat_quick_change import QUICK_CHANGE_CHAT_NAME
from imbue.chat.chat_quick_change import QUICK_CHANGE_LABEL
from imbue.chat.chat_quick_change import QUICK_CHANGE_QUESTION
from imbue.chat.chat_quick_change import QUICK_CHANGE_SKILL
from imbue.chat.chat_quick_change import QuickChangeDeliveryError
from imbue.chat.chat_quick_change import QuickChangeRouter
from imbue.chat.chat_quick_change import ROUTING_LOG_FILENAME
from imbue.chat.chat_quick_change import ROUTING_NOTE
from imbue.chat.chat_quick_change import RoutingDecision
from imbue.chat.chat_quick_change import ask_jev
from imbue.chat.chat_quick_change import build_recent_conversation
from imbue.chat.chat_quick_change import decide_routing
from imbue.chat.chat_quick_change import is_routable_message
from imbue.chat.chat_quick_change import mark_routed_message
from imbue.chat.chat_quick_change import read_chat_jev_credentials
from imbue.chat.chat_quick_change import read_openrouter_jev_credentials
from imbue.chat.chat_quick_change import read_quick_change_probability
from imbue.chat.chat_quick_change import resolve_jev_credentials
from imbue.chat.harnesses.events import DisplayKind
from imbue.chat.harnesses.message_display import classify_user_message
from imbue.chat.primitives import ChatId

_CHAT_ID = ChatId("agent-00000000000000000000000000000001")
_QUICK_CHAT_ID = "agent-00000000000000000000000000000002"
_DUMMY_KEY = "sk-or-dummy-test-key"
_CREDENTIALS = JevCredentials(endpoint_url=OPENROUTER_JEV_URL, api_key=_DUMMY_KEY, is_openrouter_key=True)
_STYLE_REQUEST = "Make the header of my todo app dark blue"


def _answer(probability: float) -> JevOutcome:
    return JevOutcome(
        status=JevOutcomeStatus.ANSWERED,
        response={
            "model": "typesafe/jev-1.13-20260917",
            "answers": {QUICK_CHANGE_QUESTION: {"type": "noul", "noul": probability}},
        },
    )


class _Fakes:
    """What a router built over fakes did: Jev requests, chats created, messages delivered."""

    def __init__(self) -> None:
        self.jev_requests: list[dict[str, Any]] = []
        self.created: list[tuple[str, str, dict[str, str]]] = []
        self.delivered: list[tuple[str, str]] = []


def _router(
    tmp_path: Path,
    *,
    outcome: JevOutcome | None = None,
    credentials: JevCredentials | None = _CREDENTIALS,
    conversation: list[dict[str, Any]] | None = None,
    deliver: Callable[[ChatId, str], bool] | None = None,
) -> tuple[QuickChangeRouter, _Fakes]:
    fakes = _Fakes()

    def call_jev(_credentials: JevCredentials, jev_request: dict[str, Any], _timeout: float) -> JevOutcome:
        fakes.jev_requests.append(jev_request)
        assert outcome is not None, "Jev was not expected to be asked"
        return outcome

    def create_agent_chat(name: str, message: str, labels: dict[str, str]) -> str:
        fakes.created.append((name, message, labels))
        return _QUICK_CHAT_ID

    def default_deliver(chat_id: ChatId, text: str) -> bool:
        fakes.delivered.append((str(chat_id), text))
        return True

    router = QuickChangeRouter(
        data_dir=tmp_path,
        resolve_credentials=lambda _chat_id: credentials,
        get_conversation_tail=lambda _chat_id, _limit: list(conversation or []),
        call_jev=call_jev,
        create_agent_chat=create_agent_chat,
        deliver=deliver if deliver is not None else default_deliver,
        run_in_background=lambda fn: fn(),
        wait=lambda _seconds: None,
    )
    return router, fakes


def _log_records(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / ROUTING_LOG_FILENAME
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# --- Jev's answer -----------------------------------------------------------------------------


def test_probability_is_read_from_the_quick_change_answer() -> None:
    assert read_quick_change_probability(_answer(0.83).response or {}) == 0.83


def test_an_answer_without_the_question_has_no_probability() -> None:
    assert read_quick_change_probability({"answers": {"other": {"noul": 0.9}}}) is None


def test_an_answer_with_a_non_numeric_probability_has_none() -> None:
    assert read_quick_change_probability({"answers": {QUICK_CHANGE_QUESTION: {"noul": True}}}) is None


def test_an_answer_with_no_answers_object_has_no_probability() -> None:
    assert read_quick_change_probability({"error": "bad request"}) is None


def test_a_likely_yes_routes() -> None:
    assert decide_routing(_answer(0.9), 0.9) is RoutingDecision.ROUTED


def test_a_likely_no_does_not_route() -> None:
    assert decide_routing(_answer(0.2), 0.2) is RoutingDecision.NOT_ROUTED


def test_an_unreachable_jev_does_not_route() -> None:
    outcome = JevOutcome(status=JevOutcomeStatus.FAILED, detail="request failed: ReadTimeout")
    assert decide_routing(outcome, None) is RoutingDecision.JEV_FAILED


# --- ask_jev over a stubbed endpoint ------------------------------------------------------------


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_ask_jev_posts_the_request_with_the_key_and_returns_the_answer() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_answer(0.7).response)

    outcome = ask_jev(_CREDENTIALS, {"model": JEV_MODEL}, _client(handler), 5.0)

    assert outcome.status is JevOutcomeStatus.ANSWERED
    assert str(seen[0].url) == OPENROUTER_JEV_URL
    assert seen[0].headers["Authorization"] == f"Bearer {_DUMMY_KEY}"


def test_ask_jev_reports_a_refused_key_as_unavailable() -> None:
    outcome = ask_jev(_CREDENTIALS, {}, _client(lambda _request: httpx.Response(401)), 5.0)
    assert outcome.status is JevOutcomeStatus.UNAVAILABLE


def test_ask_jev_reports_a_timeout_as_failed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    assert ask_jev(_CREDENTIALS, {}, _client(handler), 5.0).status is JevOutcomeStatus.FAILED


def test_ask_jev_reports_an_unreadable_answer_as_failed() -> None:
    outcome = ask_jev(_CREDENTIALS, {}, _client(lambda _request: httpx.Response(200, text="not json")), 5.0)
    assert outcome.status is JevOutcomeStatus.FAILED


# --- credentials --------------------------------------------------------------------------------


def test_the_openrouter_key_file_gives_openrouter_credentials(tmp_path: Path) -> None:
    key_path = tmp_path / OPENROUTER_KEY_FILENAME
    key_path.write_text(f"{_DUMMY_KEY}\n", encoding="utf-8")
    assert read_openrouter_jev_credentials(key_path) == _CREDENTIALS


def test_an_empty_openrouter_key_file_gives_no_credentials(tmp_path: Path) -> None:
    key_path = tmp_path / OPENROUTER_KEY_FILENAME
    key_path.write_text("  \n", encoding="utf-8")
    assert read_openrouter_jev_credentials(key_path) is None


def test_the_openrouter_key_wins_over_a_proxy_sign_in(tmp_path: Path) -> None:
    key_path = tmp_path / OPENROUTER_KEY_FILENAME
    key_path.write_text(_DUMMY_KEY, encoding="utf-8")
    assert read_chat_jev_credentials(None, key_path) == _CREDENTIALS


def test_a_proxy_sign_in_points_at_the_proxy_jev_route() -> None:
    credentials = resolve_jev_credentials(
        {"ANTHROPIC_API_KEY": "sk-proxy-dummy", "ANTHROPIC_BASE_URL": "https://proxy.invalid/"}
    )
    assert credentials is not None
    assert credentials.endpoint_url == f"https://proxy.invalid{JEV_ROUTE_PATH}"


# --- which messages are asked about, and how a routed one looks ----------------------------------


def test_the_users_own_words_are_routable() -> None:
    assert is_routable_message(_STYLE_REQUEST)


def test_a_background_task_report_is_not_routable() -> None:
    report = "<background-task-report><summary>Done.</summary>\ndetails\n</background-task-report>"
    assert not is_routable_message(report)


def test_a_slash_command_is_not_routable() -> None:
    assert not is_routable_message("/compact")


def test_a_routed_message_shows_only_the_users_words() -> None:
    decision = classify_user_message(mark_routed_message(_STYLE_REQUEST))
    assert decision is not None
    assert (decision.display, decision.display_body) == (DisplayKind.PROMPT_WITH_CONTEXT, _STYLE_REQUEST)


def test_recent_conversation_keeps_the_last_messages_and_skips_the_new_one() -> None:
    events = [
        {"type": "user_message", "role": "user", "content": "first"},
        {"type": "assistant_message", "text": "reply one"},
        {"type": "tool_call", "name": "Bash"},
        {"type": "user_message", "role": "user", "content": "second"},
        {"type": "assistant_message", "text": "reply two"},
        {"type": "user_message", "role": "user", "content": _STYLE_REQUEST},
    ]
    assert (
        build_recent_conversation(events, _STYLE_REQUEST, 3) == "Agent: reply one\n\nUser: second\n\nAgent: reply two"
    )


# --- the router ---------------------------------------------------------------------------------


def test_a_yes_marks_the_message_for_the_main_agent(tmp_path: Path) -> None:
    router, _ = _router(tmp_path, outcome=_answer(0.9))
    result = router.route(_CHAT_ID, _STYLE_REQUEST)
    assert result.text == mark_routed_message(_STYLE_REQUEST)
    assert ROUTING_NOTE in result.text


def test_a_yes_creates_the_quick_change_agent_with_the_request_word_for_word(tmp_path: Path) -> None:
    router, fakes = _router(tmp_path, outcome=_answer(0.9))
    router.forward(_CHAT_ID, router.route(_CHAT_ID, _STYLE_REQUEST))

    [(name, first_message, labels)] = fakes.created
    assert (name, labels) == (QUICK_CHANGE_CHAT_NAME, {QUICK_CHANGE_LABEL: "true"})
    assert first_message.startswith(f"/{QUICK_CHANGE_SKILL}\n")
    assert f"Main chat: {_CHAT_ID}" in first_message
    assert first_message.endswith(f"The user's request, word for word:\n{_STYLE_REQUEST}")


def test_the_quick_change_agent_is_recorded_once_created(tmp_path: Path) -> None:
    router, _ = _router(tmp_path, outcome=_answer(0.9))
    router.forward(_CHAT_ID, router.route(_CHAT_ID, _STYLE_REQUEST))
    assert router.quick_change_chat_id() == _QUICK_CHAT_ID


def test_a_second_yes_reuses_the_quick_change_agent(tmp_path: Path) -> None:
    router, fakes = _router(tmp_path, outcome=_answer(0.9))
    router.forward(_CHAT_ID, router.route(_CHAT_ID, _STYLE_REQUEST))
    router.forward(_CHAT_ID, router.route(_CHAT_ID, "Now make the font a bit bigger"))

    assert len(fakes.created) == 1
    assert [chat_id for chat_id, _ in fakes.delivered] == [_QUICK_CHAT_ID]


def test_a_gone_quick_change_agent_is_replaced(tmp_path: Path) -> None:
    (tmp_path / QUICK_CHANGE_AGENT_FILENAME).write_text(json.dumps({"chat_id": "agent-gone"}), encoding="utf-8")
    router, fakes = _router(tmp_path, outcome=_answer(0.9), deliver=lambda _chat_id, _text: False)
    router.forward(_CHAT_ID, router.route(_CHAT_ID, _STYLE_REQUEST))

    assert len(fakes.created) == 1
    assert router.quick_change_chat_id() == _QUICK_CHAT_ID


def test_a_quick_change_agent_that_keeps_refusing_is_logged_as_a_failed_forward(tmp_path: Path) -> None:
    (tmp_path / QUICK_CHANGE_AGENT_FILENAME).write_text(json.dumps({"chat_id": _QUICK_CHAT_ID}), encoding="utf-8")

    def refuse(_chat_id: ChatId, _text: str) -> bool:
        raise QuickChangeDeliveryError("busy")

    router, _ = _router(tmp_path, outcome=_answer(0.9), deliver=refuse)
    router.forward(_CHAT_ID, router.route(_CHAT_ID, _STYLE_REQUEST))

    assert [record["event"] for record in _log_records(tmp_path)] == ["jev_decision", "forward_failed"]


def test_a_no_leaves_the_message_untouched(tmp_path: Path) -> None:
    router, fakes = _router(tmp_path, outcome=_answer(0.1))
    result = router.route(_CHAT_ID, "Add a login page with Google sign-in")
    router.forward(_CHAT_ID, result)

    assert (result.text, result.is_routed) == ("Add a login page with Google sign-in", False)
    assert fakes.created == [] and fakes.delivered == []


def test_an_unreachable_jev_leaves_the_message_untouched(tmp_path: Path) -> None:
    router, fakes = _router(tmp_path, outcome=JevOutcome(status=JevOutcomeStatus.FAILED, detail="ReadTimeout"))
    result = router.route(_CHAT_ID, _STYLE_REQUEST)
    router.forward(_CHAT_ID, result)

    assert result.text == _STYLE_REQUEST
    assert fakes.created == []


def test_no_credentials_leaves_the_message_untouched_without_asking_jev(tmp_path: Path) -> None:
    router, fakes = _router(tmp_path, credentials=None)
    assert router.route(_CHAT_ID, _STYLE_REQUEST).text == _STYLE_REQUEST
    assert fakes.jev_requests == []


def test_messages_to_the_quick_change_chat_itself_are_not_asked_about(tmp_path: Path) -> None:
    (tmp_path / QUICK_CHANGE_AGENT_FILENAME).write_text(json.dumps({"chat_id": _QUICK_CHAT_ID}), encoding="utf-8")
    router, fakes = _router(tmp_path)
    router.route(ChatId(_QUICK_CHAT_ID), _STYLE_REQUEST)
    assert fakes.jev_requests == []


def test_jev_reads_the_message_and_recent_conversation(tmp_path: Path) -> None:
    conversation = [{"type": "assistant_message", "text": "Your todo app is ready."}]
    router, fakes = _router(tmp_path, outcome=_answer(0.9), conversation=conversation)
    router.route(_CHAT_ID, _STYLE_REQUEST)

    [request] = fakes.jev_requests
    assert request["state"] == {
        "new_message": _STYLE_REQUEST,
        "recent_conversation": "Agent: Your todo app is ready.",
    }
    assert list(request["questions"]) == [QUICK_CHANGE_QUESTION]


def test_every_decision_is_logged_with_request_answer_and_timestamps(tmp_path: Path) -> None:
    router, _ = _router(tmp_path, outcome=_answer(0.9))
    router.forward(_CHAT_ID, router.route(_CHAT_ID, _STYLE_REQUEST))

    decision, forwarded = _log_records(tmp_path)
    assert decision["decision"] == "routed"
    assert decision["jev_request"]["state"]["new_message"] == _STYLE_REQUEST
    assert decision["jev_outcome"]["status"] == "answered"
    assert {"started_at", "jev_sent_at", "jev_answered_at", "finished_at", "elapsed_ms"} <= decision.keys()
    assert (forwarded["event"], forwarded["is_agent_created"]) == ("forwarded", True)


def test_the_key_is_never_logged(tmp_path: Path) -> None:
    router, _ = _router(tmp_path, outcome=_answer(0.9))
    router.route(_CHAT_ID, _STYLE_REQUEST)
    assert _DUMMY_KEY not in (tmp_path / ROUTING_LOG_FILENAME).read_text(encoding="utf-8")


def test_routing_end_to_end_over_a_stubbed_openrouter_endpoint(tmp_path: Path) -> None:
    """The production credential path (key file) and HTTP call, against a stubbed endpoint."""
    key_path = tmp_path / OPENROUTER_KEY_FILENAME
    key_path.write_text(_DUMMY_KEY, encoding="utf-8")
    http_client = _client(lambda _request: httpx.Response(200, json=_answer(0.95).response))
    created: list[str] = []
    router = QuickChangeRouter(
        data_dir=tmp_path,
        resolve_credentials=lambda _chat_id: read_chat_jev_credentials(None, key_path),
        get_conversation_tail=lambda _chat_id, _limit: [],
        call_jev=lambda credentials, request, timeout: ask_jev(credentials, request, http_client, timeout),
        create_agent_chat=lambda _name, message, _labels: created.append(message) or _QUICK_CHAT_ID,
        deliver=lambda _chat_id, _text: True,
        run_in_background=lambda fn: fn(),
    )

    result = router.route(_CHAT_ID, _STYLE_REQUEST)
    router.forward(_CHAT_ID, result)

    assert result.is_routed and len(created) == 1
