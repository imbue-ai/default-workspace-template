"""Unit tests for quick look-and-feel change routing (``chat_quick_change.py``).

Jev is never reached: the router is given a fake ``call_jev``, and ``ask_jev`` is driven through an
``httpx.MockTransport`` with a dummy key.
"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from imbue.chat.chat_quick_change import JEV_MODEL
from imbue.chat.chat_quick_change import JEV_ROUTE_PATH
from imbue.chat.chat_quick_change import JevCredentials
from imbue.chat.chat_quick_change import JevOutcome
from imbue.chat.chat_quick_change import JevOutcomeStatus
from imbue.chat.chat_quick_change import OPENROUTER_JEV_URL
from imbue.chat.chat_quick_change import OPENROUTER_KEY_FILENAME
from imbue.chat.chat_quick_change import PENDING_WORK_QUESTION
from imbue.chat.chat_quick_change import QUICK_CHANGES_LOG_FILENAME
from imbue.chat.chat_quick_change import QUICK_CHANGE_AGENT_FILENAME
from imbue.chat.chat_quick_change import QUICK_CHANGE_LABEL
from imbue.chat.chat_quick_change import QUICK_CHANGE_QUESTION
from imbue.chat.chat_quick_change import QUICK_CHANGE_SKILL
from imbue.chat.chat_quick_change import QuickChangeDeliveryError
from imbue.chat.chat_quick_change import QuickChangeRouter
from imbue.chat.chat_quick_change import ROUTING_LOG_FILENAME
from imbue.chat.chat_quick_change import RoutingDecision
from imbue.chat.chat_quick_change import SIDE_AGENT_MODEL_BY_TIER
from imbue.chat.chat_quick_change import SideAgentTier
from imbue.chat.chat_quick_change import TIER_QUESTION
from imbue.chat.chat_quick_change import ask_jev
from imbue.chat.chat_quick_change import build_recent_conversation
from imbue.chat.chat_quick_change import decide_routing
from imbue.chat.chat_quick_change import decide_tier
from imbue.chat.chat_quick_change import find_editing_lease_app
from imbue.chat.chat_quick_change import is_routable_message
from imbue.chat.chat_quick_change import parse_editing_lease
from imbue.chat.chat_quick_change import quick_change_chat_name
from imbue.chat.chat_quick_change import read_chat_jev_credentials
from imbue.chat.chat_quick_change import read_openrouter_jev_credentials
from imbue.chat.chat_quick_change import read_probability
from imbue.chat.chat_quick_change import resolve_jev_credentials
from imbue.chat.harnesses.claude.model import CLAUDE_CATALOG
from imbue.chat.harnesses.model import validate_model_pick
from imbue.chat.primitives import ChatId

_CHAT_ID = ChatId("agent-00000000000000000000000000000001")
_QUICK_CHAT_ID = "agent-00000000000000000000000000000002"
_CAREFUL_CHAT_ID = "agent-00000000000000000000000000000003"
_CHAT_ID_BY_TIER = {SideAgentTier.FAST: _QUICK_CHAT_ID, SideAgentTier.CAREFUL: _CAREFUL_CHAT_ID}
_DUMMY_KEY = "sk-or-dummy-test-key"
_CREDENTIALS = JevCredentials(endpoint_url=OPENROUTER_JEV_URL, api_key=_DUMMY_KEY, is_openrouter_key=True)
_STYLE_REQUEST = "Make the header of my todo app dark blue"


def _answer(probability: float, pending_work: float = 0.1, careful: float | None = 0.1) -> JevOutcome:
    """Jev's answer to the three questions; ``careful=None`` leaves question 3 out."""
    answers = {
        QUICK_CHANGE_QUESTION: {"type": "noul", "noul": probability},
        PENDING_WORK_QUESTION: {"type": "noul", "noul": pending_work},
    }
    if careful is not None:
        answers[TIER_QUESTION] = {"type": "noul", "noul": careful}
    return JevOutcome(
        status=JevOutcomeStatus.ANSWERED,
        response={"model": "typesafe/jev-1.13-20260917", "answers": answers},
    )


class _Fakes:
    """What a router built over fakes did: Jev requests, chats created, messages delivered."""

    def __init__(self) -> None:
        self.jev_requests: list[dict[str, Any]] = []
        self.created: list[tuple[str, str, dict[str, str], SideAgentTier]] = []
        self.delivered: list[tuple[str, str]] = []


def _router(
    tmp_path: Path,
    *,
    outcome: JevOutcome | list[JevOutcome] | None = None,
    credentials: JevCredentials | None = _CREDENTIALS,
    conversation: list[dict[str, Any]] | None = None,
    deliver: Callable[[ChatId, str], bool] | None = None,
    lease_app: str | None = None,
    on_feed_event: Callable[[ChatId, dict[str, Any]], None] | None = None,
) -> tuple[QuickChangeRouter, _Fakes]:
    fakes = _Fakes()

    def call_jev(_credentials: JevCredentials, jev_request: dict[str, Any], _timeout: float) -> JevOutcome:
        fakes.jev_requests.append(jev_request)
        assert outcome is not None, "Jev was not expected to be asked"
        # A list is answered one item per call, in order.
        return outcome.pop(0) if isinstance(outcome, list) else outcome

    def create_agent_chat(name: str, message: str, labels: dict[str, str], tier: SideAgentTier) -> str:
        fakes.created.append((name, message, labels, tier))
        return _CHAT_ID_BY_TIER[tier]

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
        find_lease_app=lambda _main_chat_id: lease_app,
        on_feed_event=on_feed_event if on_feed_event is not None else (lambda _chat_id, _event: None),
        run_in_background=lambda fn: fn(),
        wait=lambda _seconds: None,
    )
    return router, fakes


def _quick_change_records(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / QUICK_CHANGES_LOG_FILENAME
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _log_records(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / ROUTING_LOG_FILENAME
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# --- Jev's answer -----------------------------------------------------------------------------


def test_probability_is_read_from_the_quick_change_answer() -> None:
    assert read_probability(_answer(0.83).response or {}, QUICK_CHANGE_QUESTION) == 0.83


def test_probability_is_read_from_the_unfinished_work_answer() -> None:
    assert read_probability(_answer(0.83, pending_work=0.7).response or {}, PENDING_WORK_QUESTION) == 0.7


def test_an_answer_without_the_question_has_no_probability() -> None:
    assert read_probability({"answers": {"other": {"noul": 0.9}}}, QUICK_CHANGE_QUESTION) is None


def test_an_answer_with_a_non_numeric_probability_has_none() -> None:
    assert read_probability({"answers": {QUICK_CHANGE_QUESTION: {"noul": True}}}, QUICK_CHANGE_QUESTION) is None


def test_an_answer_with_no_answers_object_has_no_probability() -> None:
    assert read_probability({"error": "bad request"}, QUICK_CHANGE_QUESTION) is None


def test_a_quick_change_to_finished_work_routes() -> None:
    assert decide_routing(_answer(0.9), 0.9, 0.1) is RoutingDecision.ROUTED


def test_a_quick_change_to_unfinished_work_does_not_route() -> None:
    assert decide_routing(_answer(0.9, 0.8), 0.9, 0.8) is RoutingDecision.PENDING_WORK


def test_an_unsure_quick_change_answer_does_not_route() -> None:
    assert decide_routing(_answer(0.55), 0.55, 0.1) is RoutingDecision.NOT_ROUTED


def test_an_unsure_unfinished_work_answer_does_not_route() -> None:
    assert decide_routing(_answer(0.9, 0.45), 0.9, 0.45) is RoutingDecision.PENDING_WORK


def test_a_likely_no_does_not_route() -> None:
    assert decide_routing(_answer(0.2), 0.2, 0.1) is RoutingDecision.NOT_ROUTED


def test_a_quick_change_without_an_unfinished_work_answer_does_not_route() -> None:
    assert decide_routing(_answer(0.9), 0.9, None) is RoutingDecision.UNUSABLE_ANSWER


def test_an_unreachable_jev_does_not_route() -> None:
    outcome = JevOutcome(status=JevOutcomeStatus.FAILED, detail="request failed: ReadTimeout")
    assert decide_routing(outcome, None, None) is RoutingDecision.JEV_FAILED


@pytest.mark.parametrize(
    ("careful_probability", "tier"),
    [
        (0.9, SideAgentTier.CAREFUL),
        (0.45, SideAgentTier.CAREFUL),
        (0.4, SideAgentTier.FAST),
        (0.1, SideAgentTier.FAST),
        (None, SideAgentTier.CAREFUL),
    ],
)
def test_the_careful_answer_picks_the_tier(careful_probability: float | None, tier: SideAgentTier) -> None:
    assert decide_tier(careful_probability) is tier


# --- the editing lease --------------------------------------------------------------------------


def _ticket(
    title: str,
    status: str = "in_progress",
    agent: str = "",
    created: str = "2026-11-05T10:00:00.000000Z",
    step: bool = False,
) -> str:
    lines = [
        "---",
        "id: w-ab12",
        f"status: {status}",
        "deps: []",
        f"created: {created}",
        "type: chore",
    ]
    if agent:
        lines.append(f"agent: {agent}")
    if step:
        lines.append("step: true")
    return "\n".join([*lines, "---", f"# {title}", "", "Held while editing.", ""])


def test_an_in_progress_editing_lease_names_its_app() -> None:
    lease = parse_editing_lease(_ticket("editing service todo"))
    assert lease is not None and lease.app_name == "todo"


def test_a_closed_editing_lease_is_not_a_lease() -> None:
    assert parse_editing_lease(_ticket("editing service todo", status="closed")) is None


def test_another_ticket_is_not_a_lease() -> None:
    assert parse_editing_lease(_ticket("update todo and notes")) is None


def test_a_held_lease_gives_its_app(tmp_path: Path) -> None:
    (tmp_path / "w-ab12.md").write_text(_ticket("editing service todo"), encoding="utf-8")
    assert find_editing_lease_app(tmp_path, str(_CHAT_ID)) == "todo"


def test_no_lease_gives_no_app(tmp_path: Path) -> None:
    (tmp_path / "w-ab12.md").write_text(_ticket("editing service todo", status="closed"), encoding="utf-8")
    assert find_editing_lease_app(tmp_path, str(_CHAT_ID)) is None


def test_the_main_chats_own_lease_wins_over_a_newer_one(tmp_path: Path) -> None:
    (tmp_path / "w-1.md").write_text(_ticket("editing service todo", agent=str(_CHAT_ID)), encoding="utf-8")
    newer = _ticket(
        "editing service notes",
        agent="other-chat",
        created="2026-11-05T11:00:00.000000Z",
    )
    (tmp_path / "w-2.md").write_text(newer, encoding="utf-8")
    assert find_editing_lease_app(tmp_path, str(_CHAT_ID)) == "todo"


def test_the_lease_app_is_passed_to_the_quick_change_agent(tmp_path: Path) -> None:
    router, fakes = _router(tmp_path, outcome=_answer(0.9), lease_app="todo")
    router.forward(_CHAT_ID, router.route(_CHAT_ID, _STYLE_REQUEST))
    [(_, first_message, _, _)] = fakes.created
    assert "Editing lease app: todo\n" in first_message


def test_no_lease_is_passed_as_none(tmp_path: Path) -> None:
    router, fakes = _router(tmp_path, outcome=_answer(0.9), lease_app=None)
    router.forward(_CHAT_ID, router.route(_CHAT_ID, _STYLE_REQUEST))
    [(_, first_message, _, _)] = fakes.created
    assert "Editing lease app: (none)\n" in first_message


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
    outcome = ask_jev(
        _CREDENTIALS,
        {},
        _client(lambda _request: httpx.Response(200, text="not json")),
        5.0,
    )
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
        {
            "ANTHROPIC_API_KEY": "sk-proxy-dummy",
            "ANTHROPIC_BASE_URL": "https://proxy.invalid/",
        }
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


def test_a_yes_routes_the_message(tmp_path: Path) -> None:
    router, _ = _router(tmp_path, outcome=_answer(0.9))
    result = router.route(_CHAT_ID, _STYLE_REQUEST)
    assert (result.is_routed, result.user_message) == (True, _STYLE_REQUEST)


def test_a_routed_message_is_logged_for_the_main_agent_with_app_and_answers(tmp_path: Path) -> None:
    router, _ = _router(tmp_path, outcome=_answer(0.9), lease_app="todo")
    router.forward(_CHAT_ID, router.route(_CHAT_ID, _STYLE_REQUEST))

    request, forward = _quick_change_records(tmp_path)
    assert (request["type"], request["message"], request["app"]) == ("request", _STYLE_REQUEST, "todo")
    assert request["jev"] == {QUICK_CHANGE_QUESTION: 0.9, PENDING_WORK_QUESTION: 0.1, TIER_QUESTION: 0.1}
    assert (forward["type"], forward["status"]) == ("forward", "forwarded")


@pytest.mark.parametrize(
    ("careful", "tier"),
    [(0.9, SideAgentTier.CAREFUL), (0.1, SideAgentTier.FAST), (None, SideAgentTier.CAREFUL)],
)
def test_a_routed_message_carries_the_tier_its_careful_answer_picks(
    tmp_path: Path, careful: float | None, tier: SideAgentTier
) -> None:
    router, _ = _router(tmp_path, outcome=_answer(0.9, careful=careful))
    result = router.route(_CHAT_ID, _STYLE_REQUEST)
    assert (result.is_routed, result.tier, result.careful_probability) == (True, tier, careful)


def test_a_routing_decision_is_logged_with_its_tier_and_careful_answer(tmp_path: Path) -> None:
    router, _ = _router(tmp_path, outcome=_answer(0.9, careful=0.8))
    router.route(_CHAT_ID, _STYLE_REQUEST)

    [record] = _log_records(tmp_path)
    assert (record["decision"], record["tier"], record["careful_probability"]) == ("routed", "careful", 0.8)


def test_an_unrouted_decision_is_logged_without_a_tier(tmp_path: Path) -> None:
    router, _ = _router(tmp_path, outcome=_answer(0.1, careful=0.8))
    router.route(_CHAT_ID, _STYLE_REQUEST)

    [record] = _log_records(tmp_path)
    assert (record["decision"], record["tier"]) == ("not_routed", None)


def test_a_routed_request_is_logged_for_the_main_agent_with_its_tier(tmp_path: Path) -> None:
    router, _ = _router(tmp_path, outcome=_answer(0.9, careful=0.8))
    router.forward(_CHAT_ID, router.route(_CHAT_ID, _STYLE_REQUEST))

    request = _quick_change_records(tmp_path)[0]
    assert (request["type"], request["tier"]) == ("request", "careful")


def test_a_routed_message_is_shown_in_its_chat_at_once(tmp_path: Path) -> None:
    shown: list[tuple[str, dict[str, Any]]] = []
    router, _ = _router(tmp_path, outcome=_answer(0.9), on_feed_event=lambda c, e: shown.append((str(c), e)))
    router.forward(_CHAT_ID, router.route(_CHAT_ID, _STYLE_REQUEST))

    [(chat_id, event)] = shown
    assert (chat_id, event["type"], event["content"]) == (str(_CHAT_ID), "user_message", _STYLE_REQUEST)


def test_a_report_is_logged_and_shown_as_the_reply(tmp_path: Path) -> None:
    shown: list[dict[str, Any]] = []
    router, _ = _router(tmp_path, outcome=_answer(0.9), on_feed_event=lambda _c, e: shown.append(e))
    result = router.route(_CHAT_ID, _STYLE_REQUEST)
    router.forward(_CHAT_ID, result)

    assert router.record_report(result.request_id, "done", "Todo: header is dark blue; window refreshed.")
    assert (shown[-1]["type"], shown[-1]["text"]) == (
        "assistant_message",
        "Todo: header is dark blue; window refreshed.",
    )
    assert _quick_change_records(tmp_path)[-1]["type"] == "report"


def test_a_report_for_an_unknown_request_is_refused(tmp_path: Path) -> None:
    router, _ = _router(tmp_path)
    assert not router.record_report("no-such-request", "done", "Done.")


def test_a_failed_handover_is_shown_as_a_failure_reply(tmp_path: Path) -> None:
    def refuse(_chat_id: ChatId, _text: str) -> bool:
        raise QuickChangeDeliveryError("busy")

    (tmp_path / QUICK_CHANGE_AGENT_FILENAME).write_text(json.dumps({"chat_id": _QUICK_CHAT_ID}), encoding="utf-8")
    shown: list[dict[str, Any]] = []
    router, _ = _router(tmp_path, outcome=_answer(0.9), deliver=refuse, on_feed_event=lambda _c, e: shown.append(e))
    router.forward(_CHAT_ID, router.route(_CHAT_ID, _STYLE_REQUEST))

    assert [event["type"] for event in shown] == ["user_message", "assistant_message"]


def test_a_yes_creates_the_quick_change_agent_with_the_request_word_for_word(
    tmp_path: Path,
) -> None:
    router, fakes = _router(tmp_path, outcome=_answer(0.9))
    router.forward(_CHAT_ID, router.route(_CHAT_ID, _STYLE_REQUEST))

    [(name, first_message, labels, tier)] = fakes.created
    assert (name, labels, tier) == (
        quick_change_chat_name(SideAgentTier.FAST),
        {QUICK_CHANGE_LABEL: "true"},
        SideAgentTier.FAST,
    )
    assert first_message.startswith(f"/{QUICK_CHANGE_SKILL}\n")
    assert f"Main chat: {_CHAT_ID}" in first_message
    assert first_message.endswith(f"The user's request, word for word:\n{_STYLE_REQUEST}")


def test_the_quick_change_agent_is_recorded_once_created(tmp_path: Path) -> None:
    router, _ = _router(tmp_path, outcome=_answer(0.9))
    router.forward(_CHAT_ID, router.route(_CHAT_ID, _STYLE_REQUEST))
    assert router.quick_change_chat_ids() == {SideAgentTier.FAST: _QUICK_CHAT_ID}


def test_a_second_yes_reuses_the_quick_change_agent(tmp_path: Path) -> None:
    router, fakes = _router(tmp_path, outcome=_answer(0.9))
    router.forward(_CHAT_ID, router.route(_CHAT_ID, _STYLE_REQUEST))
    router.forward(_CHAT_ID, router.route(_CHAT_ID, "Now make the font a bit bigger"))

    assert len(fakes.created) == 1
    assert [chat_id for chat_id, _ in fakes.delivered] == [_QUICK_CHAT_ID]


def test_the_first_fast_and_first_careful_change_each_create_their_tiers_chat(tmp_path: Path) -> None:
    router, fakes = _router(tmp_path, outcome=[_answer(0.9, careful=0.1), _answer(0.9, careful=0.9)])
    router.forward(_CHAT_ID, router.route(_CHAT_ID, _STYLE_REQUEST))
    router.forward(_CHAT_ID, router.route(_CHAT_ID, "Restyle the whole app in a calmer palette"))

    assert [tier for _, _, _, tier in fakes.created] == [SideAgentTier.FAST, SideAgentTier.CAREFUL]
    assert router.quick_change_chat_ids() == _CHAT_ID_BY_TIER


def test_a_careful_change_creates_the_careful_chat_on_the_careful_tier(tmp_path: Path) -> None:
    router, fakes = _router(tmp_path, outcome=_answer(0.9, careful=0.9))
    router.forward(_CHAT_ID, router.route(_CHAT_ID, _STYLE_REQUEST))

    [(name, _, _, tier)] = fakes.created
    assert (name, tier) == (quick_change_chat_name(SideAgentTier.CAREFUL), SideAgentTier.CAREFUL)


def test_a_second_careful_change_reuses_the_careful_chat(tmp_path: Path) -> None:
    router, fakes = _router(tmp_path, outcome=_answer(0.9, careful=0.9))
    router.forward(_CHAT_ID, router.route(_CHAT_ID, _STYLE_REQUEST))
    router.forward(_CHAT_ID, router.route(_CHAT_ID, "Now make the whole palette warmer"))

    assert len(fakes.created) == 1
    assert [chat_id for chat_id, _ in fakes.delivered] == [_CAREFUL_CHAT_ID]


def test_the_tier_names_tell_the_chats_apart() -> None:
    assert quick_change_chat_name(SideAgentTier.FAST) != quick_change_chat_name(SideAgentTier.CAREFUL)


def test_a_single_chat_record_is_read_as_the_fast_tiers_chat(tmp_path: Path) -> None:
    (tmp_path / QUICK_CHANGE_AGENT_FILENAME).write_text(json.dumps({"chat_id": "agent-old"}), encoding="utf-8")
    router, fakes = _router(tmp_path, outcome=_answer(0.9, careful=0.1))
    router.forward(_CHAT_ID, router.route(_CHAT_ID, _STYLE_REQUEST))

    assert fakes.created == []
    assert [chat_id for chat_id, _ in fakes.delivered] == ["agent-old"]


def test_a_single_chat_record_keeps_its_chat_as_fast_when_the_careful_chat_is_created(tmp_path: Path) -> None:
    (tmp_path / QUICK_CHANGE_AGENT_FILENAME).write_text(json.dumps({"chat_id": "agent-old"}), encoding="utf-8")
    router, _ = _router(tmp_path, outcome=_answer(0.9, careful=0.9))
    router.forward(_CHAT_ID, router.route(_CHAT_ID, _STYLE_REQUEST))

    assert router.quick_change_chat_ids() == {SideAgentTier.FAST: "agent-old", SideAgentTier.CAREFUL: _CAREFUL_CHAT_ID}


def test_a_routed_message_is_forwarded_with_its_tier(tmp_path: Path) -> None:
    router, _ = _router(tmp_path, outcome=_answer(0.9, careful=0.9))
    router.forward(_CHAT_ID, router.route(_CHAT_ID, _STYLE_REQUEST))

    forward = _quick_change_records(tmp_path)[-1]
    assert (forward["type"], forward["quick_change_chat_id"], forward["tier"]) == (
        "forward",
        _CAREFUL_CHAT_ID,
        "careful",
    )


def test_a_gone_quick_change_agent_is_replaced(tmp_path: Path) -> None:
    (tmp_path / QUICK_CHANGE_AGENT_FILENAME).write_text(json.dumps({"chat_id": "agent-gone"}), encoding="utf-8")
    router, fakes = _router(tmp_path, outcome=_answer(0.9), deliver=lambda _chat_id, _text: False)
    router.forward(_CHAT_ID, router.route(_CHAT_ID, _STYLE_REQUEST))

    assert len(fakes.created) == 1
    assert router.quick_change_chat_ids() == {SideAgentTier.FAST: _QUICK_CHAT_ID}


def test_a_quick_change_agent_that_keeps_refusing_is_logged_as_a_failed_forward(
    tmp_path: Path,
) -> None:
    (tmp_path / QUICK_CHANGE_AGENT_FILENAME).write_text(json.dumps({"chat_id": _QUICK_CHAT_ID}), encoding="utf-8")

    def refuse(_chat_id: ChatId, _text: str) -> bool:
        raise QuickChangeDeliveryError("busy")

    router, _ = _router(tmp_path, outcome=_answer(0.9), deliver=refuse)
    router.forward(_CHAT_ID, router.route(_CHAT_ID, _STYLE_REQUEST))

    assert [record["event"] for record in _log_records(tmp_path)] == [
        "jev_decision",
        "forward_failed",
    ]


def test_a_no_leaves_the_message_untouched(tmp_path: Path) -> None:
    router, fakes = _router(tmp_path, outcome=_answer(0.1))
    result = router.route(_CHAT_ID, "Add a login page with Google sign-in")
    router.forward(_CHAT_ID, result)

    assert result.is_routed is False
    assert fakes.created == [] and fakes.delivered == []


def test_a_quick_change_to_unfinished_work_leaves_the_message_untouched(
    tmp_path: Path,
) -> None:
    router, fakes = _router(tmp_path, outcome=_answer(0.9, pending_work=0.85))
    result = router.route(_CHAT_ID, "Make dark mode a bit warmer")
    router.forward(_CHAT_ID, result)

    assert result.is_routed is False
    assert fakes.created == [] and fakes.delivered == []


def test_a_quick_change_to_unfinished_work_logs_both_answers(tmp_path: Path) -> None:
    router, _ = _router(tmp_path, outcome=_answer(0.9, pending_work=0.85))
    router.route(_CHAT_ID, "Make dark mode a bit warmer")

    [record] = _log_records(tmp_path)
    assert (
        record["decision"],
        record["quick_change_probability"],
        record["pending_work_probability"],
    ) == (
        "pending_work",
        0.9,
        0.85,
    )


def test_an_unreachable_jev_leaves_the_message_untouched(tmp_path: Path) -> None:
    router, fakes = _router(
        tmp_path,
        outcome=JevOutcome(status=JevOutcomeStatus.FAILED, detail="ReadTimeout"),
    )
    result = router.route(_CHAT_ID, _STYLE_REQUEST)
    router.forward(_CHAT_ID, result)

    assert result.is_routed is False
    assert fakes.created == []


def test_no_credentials_leaves_the_message_untouched_without_asking_jev(
    tmp_path: Path,
) -> None:
    router, fakes = _router(tmp_path, credentials=None)
    assert router.route(_CHAT_ID, _STYLE_REQUEST).is_routed is False
    assert fakes.jev_requests == []


def test_messages_to_the_quick_change_chat_itself_are_not_asked_about(
    tmp_path: Path,
) -> None:
    (tmp_path / QUICK_CHANGE_AGENT_FILENAME).write_text(json.dumps({"chat_id": _QUICK_CHAT_ID}), encoding="utf-8")
    router, fakes = _router(tmp_path)
    router.route(ChatId(_QUICK_CHAT_ID), _STYLE_REQUEST)
    assert fakes.jev_requests == []


def test_messages_to_the_careful_quick_change_chat_are_not_asked_about(tmp_path: Path) -> None:
    record = {"fast": {"chat_id": _QUICK_CHAT_ID}, "careful": {"chat_id": _CAREFUL_CHAT_ID}}
    (tmp_path / QUICK_CHANGE_AGENT_FILENAME).write_text(json.dumps(record), encoding="utf-8")
    router, fakes = _router(tmp_path)
    router.route(ChatId(_CAREFUL_CHAT_ID), _STYLE_REQUEST)
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
    assert list(request["questions"]) == [QUICK_CHANGE_QUESTION, PENDING_WORK_QUESTION, TIER_QUESTION]


def test_every_decision_is_logged_with_request_answer_and_timestamps(
    tmp_path: Path,
) -> None:
    router, _ = _router(tmp_path, outcome=_answer(0.9))
    router.forward(_CHAT_ID, router.route(_CHAT_ID, _STYLE_REQUEST))

    decision, forwarded = _log_records(tmp_path)
    assert decision["decision"] == "routed"
    assert decision["jev_request"]["state"]["new_message"] == _STYLE_REQUEST
    assert decision["jev_outcome"]["status"] == "answered"
    assert {
        "started_at",
        "jev_sent_at",
        "jev_answered_at",
        "finished_at",
        "elapsed_ms",
    } <= decision.keys()
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
        create_agent_chat=lambda _name, message, _labels, _tier: created.append(message) or _QUICK_CHAT_ID,
        deliver=lambda _chat_id, _text: True,
        find_lease_app=lambda _main_chat_id: None,
        run_in_background=lambda fn: fn(),
    )

    result = router.route(_CHAT_ID, _STYLE_REQUEST)
    router.forward(_CHAT_ID, result)

    assert result.is_routed and len(created) == 1


@pytest.mark.parametrize("tier", list(SideAgentTier))
def test_every_side_agent_tier_names_a_model_and_effort_the_claude_catalog_accepts(tier: SideAgentTier) -> None:
    """A tier the catalog refused would launch the quick-change agent on a model the model bar cannot show."""
    model_id, effort = SIDE_AGENT_MODEL_BY_TIER[tier]
    assert validate_model_pick(CLAUDE_CATALOG.options, model_id, effort, fast=False).id == model_id
