"""Tests for rename_chat.py: the rename by title through the chat app the ``fake_chat_app`` fixture serves."""

from __future__ import annotations

import io
from typing import Any

from workspace_bare_scripts import rename_chat

_DOCS_CHAT_ID = "agent-0123456789abcdef0123456789abcdef"


def _run(*args: str) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    rc = rename_chat.main([*args], stdout=stdout, stderr=stderr)
    return rc, stdout.getvalue(), stderr.getvalue()


def _listing(*chats: tuple[str, str]) -> dict[str, Any]:
    return {
        "chats": [
            {"chat_id": chat_id, "title": title, "kind": "agent"}
            for chat_id, title in chats
        ],
        "main_chat_id": None,
    }


def test_the_chat_under_the_old_title_is_renamed_through_the_rename_route(
    fake_chat_app: Any,
) -> None:
    fake_chat_app.get_answers["/api/chats"] = (
        200,
        _listing(("agent-1", "Main chat"), (_DOCS_CHAT_ID, "Docs Chat")),
    )
    fake_chat_app.answers = [(200, {"chat_id": _DOCS_CHAT_ID, "title": "Notes Chat"})]

    rc, out, err = _run("--title", "Docs Chat", "--new-title", "Notes Chat")

    assert (rc, err) == (0, "")
    assert "Notes Chat" in out
    assert fake_chat_app.posted == [
        (f"/api/chats/{_DOCS_CHAT_ID}/rename", {"title": "Notes Chat"})
    ]


def test_no_chat_under_the_old_title_is_nothing_to_do(fake_chat_app: Any) -> None:
    fake_chat_app.get_answers["/api/chats"] = (200, _listing(("agent-1", "Main chat")))

    rc, out, _err = _run("--title", "Docs Chat", "--new-title", "Notes Chat")

    assert rc == 0
    assert "nothing to rename" in out
    assert fake_chat_app.posted == []


def test_a_refused_rename_and_an_unanswered_listing_exit_non_zero(
    fake_chat_app: Any,
) -> None:
    fake_chat_app.get_answers["/api/chats"] = (
        200,
        _listing((_DOCS_CHAT_ID, "Docs Chat")),
    )
    fake_chat_app.answers = [
        (409, {"detail": "A chat named 'Notes Chat' already exists"})
    ]
    rc, _out, err = _run("--title", "Docs Chat", "--new-title", "Notes Chat")
    assert rc == 1
    assert "already exists" in err

    fake_chat_app.get_answers["/api/chats"] = (200, {"chats": "nope"})
    rc, _out, err = _run("--title", "Docs Chat", "--new-title", "Notes Chat")
    assert rc == 2
    assert "no listing" in err

    del fake_chat_app.get_answers["/api/chats"]
    rc, _out, err = _run("--title", "Docs Chat", "--new-title", "Notes Chat")
    assert rc == 2
    assert "HTTP 404" in err


def test_the_chat_id_is_found_by_the_exact_title_alone() -> None:
    chats = rename_chat.chats_listed(
        _listing(("agent-1", "Docs Chat"), ("agent-2", "docs chat"))
    )
    assert chats is not None
    assert rename_chat.chat_id_titled(chats, "Docs Chat") == "agent-1"
    assert rename_chat.chat_id_titled(chats, "Docs") is None
    assert rename_chat.chats_listed({"chats": "nope"}) is None
    assert rename_chat.chats_listed(None) is None
