"""Tests for chat_room.py: reading a room and posting into one through the chat app over loopback."""

from __future__ import annotations

import io
from typing import Any

from script_modules_testing import chat_room

_LOUNGE_ID = "room-0123456789abcdef"
_LOUNGE = {
    "id": _LOUNGE_ID,
    "kind": "room",
    "title": "Lounge",
    "last_messaged_at": None,
    "awaiting_chat_ids": [],
}


def _message(event_id: str, name: str, text: str) -> dict[str, Any]:
    return {
        "timestamp": "2026-10-22T10:00:00Z",
        "type": "user_message",
        "event_id": event_id,
        "content": text,
        "author": "user:x",
        "author_name": name,
    }


def test_read_prints_the_rooms_messages_resolved_by_title(fake_chat_app: Any) -> None:
    fake_chat_app.get_answers = {
        "/api/rooms": (200, {"rooms": [_LOUNGE]}),
        f"/api/rooms/{_LOUNGE_ID}/messages": (
            200,
            {
                "events": [
                    _message("e-1", "Josh", "hello @agent"),
                    _message("e-2", "Bob", "two\nlines"),
                ]
            },
        ),
    }
    out = io.StringIO()

    code = chat_room.main(["read", "lounge"], out=out)

    assert code == 0
    assert out.getvalue().splitlines() == [
        f"# Lounge ({_LOUNGE_ID}): 2 message(s)",
        "[2026-10-22T10:00:00Z] Josh: hello @agent  (e-1)",
        "[2026-10-22T10:00:00Z] Bob: two",
        "    lines  (e-2)",
    ]


def test_read_passes_since_through_and_keeps_the_last_n(fake_chat_app: Any) -> None:
    fake_chat_app.get_answers = {
        "/api/rooms": (200, {"rooms": [_LOUNGE]}),
        f"/api/rooms/{_LOUNGE_ID}/messages?after=e-1": (
            200,
            {
                "events": [
                    _message("e-2", "Bob", "two"),
                    _message("e-3", "Bob", "three"),
                ]
            },
        ),
    }
    out = io.StringIO()

    code = chat_room.main(
        ["read", _LOUNGE_ID, "--since", "e-1", "--limit", "1"], out=out
    )

    assert code == 0
    assert out.getvalue().splitlines()[1:] == [
        "[2026-10-22T10:00:00Z] Bob: three  (e-3)"
    ]


def test_post_says_it_as_the_workspace_agent_and_makes_the_lounge_on_first_use(
    fake_chat_app: Any,
) -> None:
    fake_chat_app.get_answers = {"/api/rooms": (200, {"rooms": []})}
    fake_chat_app.answers = [(200, _LOUNGE), (200, {"status": "ok"})]

    code = chat_room.main(["post", "Lounge", "-m", "Sunny today."])

    assert code == 0
    assert fake_chat_app.posted == [
        ("/api/rooms/lounge", {}),
        (
            f"/api/rooms/{_LOUNGE_ID}/post",
            {"message": "Sunny today.", "author": "workspace-agent"},
        ),
    ]


def test_post_reads_stdin_and_fails_on_an_unknown_room_or_a_refusal(
    fake_chat_app: Any,
) -> None:
    fake_chat_app.get_answers = {"/api/rooms": (200, {"rooms": [_LOUNGE]})}
    fake_chat_app.answers = [(404, {"detail": "Room 'x' not found"})]

    assert chat_room.main(["post", "Nowhere", "-m", "hi"]) == 1
    assert chat_room.main(["post", "Lounge"], stdin=io.StringIO("from stdin\n")) == 1
    assert fake_chat_app.posted == [
        (
            f"/api/rooms/{_LOUNGE_ID}/post",
            {"message": "from stdin", "author": "workspace-agent"},
        )
    ]
