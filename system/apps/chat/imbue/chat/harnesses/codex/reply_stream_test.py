from typing import Any

from imbue.chat.harnesses.codex.reply_stream import CodexReplyStream
from imbue.chat.harnesses.session_watcher import StreamedReply


def _delta(item_id: str, delta: str) -> tuple[str, dict[str, Any]]:
    return "item/agentMessage/delta", {"threadId": "thread-1", "itemId": item_id, "delta": delta}


def _agent_message(method: str, item_id: str, text: str = "") -> tuple[str, dict[str, Any]]:
    return method, {"threadId": "thread-1", "item": {"type": "agentMessage", "id": item_id, "text": text}}


def _fed(*notifications: tuple[str, dict[str, Any]]) -> CodexReplyStream:
    stream = CodexReplyStream()
    for method, params in notifications:
        stream.handle_notification(method, params)
    return stream


def test_nothing_is_streamed_before_an_agent_message_starts() -> None:
    assert _fed(("turn/started", {"turn": {"id": "turn-1"}})).read() is None


def test_deltas_add_up_to_the_message_written_so_far() -> None:
    stream = _fed(_agent_message("item/started", "msg-1"), _delta("msg-1", "Tides "), _delta("msg-1", "rise"))

    assert stream.read() == StreamedReply(message_id="msg-1", text="Tides rise")


def test_the_next_message_replaces_the_previous_one() -> None:
    stream = _fed(
        _delta("msg-1", "Looking at the file."),
        _agent_message("item/completed", "msg-1", "Looking at the file."),
        _agent_message("item/started", "msg-2"),
        _delta("msg-2", "It says"),
    )

    assert stream.read() == StreamedReply(message_id="msg-2", text="It says")


def test_a_finished_message_reads_as_its_whole_text_even_with_a_delta_missed() -> None:
    stream = _fed(_delta("msg-1", "Tides "), _agent_message("item/completed", "msg-1", "Tides rise twice a day."))

    assert stream.read() == StreamedReply(message_id="msg-1", text="Tides rise twice a day.")


def test_a_finished_message_outlives_its_turn_until_the_next_starts() -> None:
    stream = _fed(
        _delta("msg-1", "Done."),
        _agent_message("item/completed", "msg-1", "Done."),
        ("turn/completed", {"turn": {"id": "turn-1", "status": "completed"}}),
    )

    assert stream.read() == StreamedReply(message_id="msg-1", text="Done.")


def test_a_message_its_turn_ended_before_finishing_is_dropped() -> None:
    stream = _fed(_delta("msg-1", "Half a"), ("turn/completed", {"turn": {"id": "turn-1", "status": "interrupted"}}))

    assert stream.read() is None


def test_items_other_than_agent_messages_change_nothing() -> None:
    stream = _fed(
        _delta("msg-1", "Running it."),
        ("item/started", {"threadId": "thread-1", "item": {"type": "commandExecution", "id": "cmd-1"}}),
        ("item/completed", {"threadId": "thread-1", "item": {"type": "commandExecution", "id": "cmd-1"}}),
    )

    assert stream.read() == StreamedReply(message_id="msg-1", text="Running it.")
