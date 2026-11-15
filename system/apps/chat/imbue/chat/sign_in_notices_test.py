"""Which batches of a chat's live events start a sign-in failure the owner should hear of."""

from typing import Any

from imbue.chat.primitives import ChatId
from imbue.chat.sign_in_notices import SignInFailureNotices


def _reply(is_auth_error: bool) -> dict[str, Any]:
    return {"type": "assistant_message", "content": "...", "is_auth_error": is_auth_error}


def _user_turn() -> dict[str, Any]:
    return {"type": "user_message", "content": "hello"}


def test_the_first_refused_reply_starts_an_episode() -> None:
    notices = SignInFailureNotices()

    assert notices.record_live_events(ChatId("agent-a1"), [_user_turn(), _reply(is_auth_error=True)]) is True


def test_further_refused_replies_in_the_same_episode_are_not_news() -> None:
    notices = SignInFailureNotices()
    chat_id = ChatId("agent-a1")
    notices.record_live_events(chat_id, [_reply(is_auth_error=True)])

    assert notices.record_live_events(chat_id, [_user_turn(), _reply(is_auth_error=True)]) is False


def test_a_reply_that_was_not_refused_ends_the_episode() -> None:
    notices = SignInFailureNotices()
    chat_id = ChatId("agent-a1")
    notices.record_live_events(chat_id, [_reply(is_auth_error=True)])
    notices.record_live_events(chat_id, [_reply(is_auth_error=False)])

    assert notices.record_live_events(chat_id, [_reply(is_auth_error=True)]) is True


def test_a_batch_whose_failure_was_already_answered_is_not_news() -> None:
    notices = SignInFailureNotices()

    assert (
        notices.record_live_events(ChatId("agent-a1"), [_reply(is_auth_error=True), _reply(is_auth_error=False)])
        is False
    )


def test_batches_without_replies_change_nothing() -> None:
    notices = SignInFailureNotices()
    chat_id = ChatId("agent-a1")
    notices.record_live_events(chat_id, [_reply(is_auth_error=True)])

    assert notices.record_live_events(chat_id, [_user_turn()]) is False
    assert notices.record_live_events(chat_id, [_reply(is_auth_error=True)]) is False


def test_chats_have_separate_episodes() -> None:
    notices = SignInFailureNotices()
    notices.record_live_events(ChatId("agent-a1"), [_reply(is_auth_error=True)])

    assert notices.record_live_events(ChatId("agent-b2"), [_reply(is_auth_error=True)]) is True
