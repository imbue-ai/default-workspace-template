import pytest

from activity.chats import ChatAction
from activity.chats import DEFAULT_CHAT_APP_URL
from activity.chats import chat_action_route
from activity.chats import chat_app_url
from activity.chats import fetch_chats
from activity.chats import parse_chat_list
from activity.chats import refusal_reason
from activity.chats import request_chat_action
from activity.errors import ChatAppUnavailableError
from activity.testing import FakeChatApp
from activity.testing import chat_snapshot
from app_manifest.registry import RegistryRow

_BASE = "http://chat.test"


def test_the_chat_app_is_found_in_the_registry_else_at_its_default_port() -> None:
    rows = [RegistryRow.model_validate({"name": "chat", "url": "http://localhost:8011/"})]
    assert chat_app_url(rows) == "http://localhost:8011"
    assert chat_app_url([]) == DEFAULT_CHAT_APP_URL


def test_a_chat_keeps_every_agent_it_has_run_on_and_its_active_harness() -> None:
    snapshot = chat_snapshot("c1", "Plan the launch", "idle", "agent-2", "pi-coding", earlier_agent_ids=["agent-1"])
    snapshot["unknown_newer_field"] = True
    [chat] = parse_chat_list({"chats": [snapshot]})
    assert (chat.title, chat.harness, chat.agent_id, chat.agent_ids) == (
        "Plan the launch",
        "pi-coding",
        "agent-2",
        ("agent-1", "agent-2"),
    )


def test_a_chat_list_of_another_shape_is_refused_not_read_as_empty() -> None:
    with pytest.raises(ChatAppUnavailableError, match="not in the expected shape"):
        parse_chat_list({"detail": "starting"})
    with pytest.raises(ChatAppUnavailableError, match="not in the expected shape"):
        parse_chat_list({"chats": [{"chat_id": "c1"}]})
    with pytest.raises(ChatAppUnavailableError, match="not in the expected shape"):
        bad_time = chat_snapshot("c1", "A", "idle", "agent-1", "claude")
        bad_time["last_messaged_at"] = "yesterday"
        parse_chat_list({"chats": [bad_time]})


def test_fetching_chats_turns_an_error_status_into_an_unavailable_chat_app() -> None:
    fake = FakeChatApp([chat_snapshot("c1", "A", "idle", "agent-1", "claude")])
    assert [chat.chat_id for chat in fetch_chats(fake.client(), _BASE)] == ["c1"]
    with pytest.raises(ChatAppUnavailableError, match="could not list chats"):
        fetch_chats(FakeChatApp([], list_status=503).client(), _BASE)


def test_a_chat_id_is_path_quoted_so_it_can_never_name_another_route() -> None:
    assert chat_action_route("c1", ChatAction.STOP) == "/api/chats/c1/stop"
    assert chat_action_route("../create", ChatAction.START) == "/api/chats/..%2Fcreate/start"
    assert chat_action_route("create#", ChatAction.STOP) == "/api/chats/create%23/stop"


def test_a_chat_action_posts_to_the_chat_apps_own_route_and_passes_its_reason_on() -> None:
    fake = FakeChatApp([chat_snapshot("c1", "A", "idle", "agent-1", "claude")])
    request_chat_action(fake.client(), _BASE, "c1", ChatAction.STOP)
    assert fake.actions == [("c1", "stop")]
    with pytest.raises(ChatAppUnavailableError, match="refused to start the chat: Chat 'missing' not found$"):
        request_chat_action(fake.client(), _BASE, "missing", ChatAction.START)


def test_a_refusal_shows_the_chat_apps_detail_else_its_body() -> None:
    assert refusal_reason('{"detail": "converging"}') == "converging"
    assert refusal_reason("plain text") == "plain text"
    assert refusal_reason('["a"]') == '["a"]'
