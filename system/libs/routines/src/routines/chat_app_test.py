import os
from pathlib import Path

import pytest

from routines.chat_app import HttpChatApp
from routines.errors import ChatAppError
from routines.errors import ChatCreateError
from routines.mock_clock_test import FakeClock
from routines.mock_http_server_test import FakeHttpServer
from routines.primitives import ChatStatusWord
from routines.primitives import RoutineId
from routines.primitives import SendVerdict


def test_the_status_is_read_from_the_chat_list_and_unknown_words_read_as_unknown(
    http_chat_app: HttpChatApp, http_server: FakeHttpServer
) -> None:
    http_server.answer(
        "GET",
        "/api/chats",
        (200, {"chats": [{"chat_id": "a", "status": "working"}, {"chat_id": "b", "status": "dreaming"}]}),
    )
    assert http_chat_app.chat_status("a") is ChatStatusWord.WORKING
    assert http_chat_app.chat_status("b") is ChatStatusWord.UNKNOWN
    assert http_chat_app.chat_status("c") is None


def test_a_status_read_waits_out_a_not_ready_answer_too(
    http_chat_app: HttpChatApp, http_server: FakeHttpServer
) -> None:
    """A chat app that has restarted answers 503 until its agent list is known; a poll is not a refusal then."""
    http_server.answer(
        "GET",
        "/api/chats",
        (503, {"detail": "agent list not known yet"}),
        (200, {"chats": [{"chat_id": "a", "status": "idle"}]}),
    )
    assert http_chat_app.chat_status("a") is ChatStatusWord.IDLE
    assert [request.path for request in http_server.requests] == ["/api/chats", "/api/chats"]


def test_a_send_waits_out_a_not_ready_answer_and_a_refusal_is_an_error_in_the_apps_words(
    http_chat_app: HttpChatApp, http_server: FakeHttpServer
) -> None:
    http_server.answer("POST", "/api/chats/a/message", (503, {"detail": "starting"}), (200, {}))
    http_server.answer("POST", "/api/chats/b/message", (409, {"detail": "the chat moved on"}))

    http_chat_app.send_message("a", "/digest")
    assert [request.path for request in http_server.requests] == ["/api/chats/a/message", "/api/chats/a/message"]
    body = http_server.requests[0].body
    assert body is not None and body["message"] == "/digest"
    with pytest.raises(ChatAppError, match="the chat moved on"):
        http_chat_app.send_message("b", "/digest")


def test_the_chat_verbs_post_to_the_chat_waiting_out_a_not_ready_answer_and_a_refusal_is_an_error(
    http_chat_app: HttpChatApp, http_server: FakeHttpServer
) -> None:
    http_server.answer("POST", "/api/chats/a/stop", (200, {}))
    http_server.answer("POST", "/api/chats/a/start", (503, {"detail": "starting"}), (200, {}))
    http_server.answer("POST", "/api/chats/a/interrupt", (409, {"detail": "nothing is going"}))

    http_chat_app.stop_chat("a")
    http_chat_app.start_chat("a")
    with pytest.raises(ChatAppError, match="refused to interrupt the chat: nothing is going"):
        http_chat_app.interrupt_chat("a")

    assert [(request.method, request.path) for request in http_server.requests] == [
        ("POST", "/api/chats/a/stop"),
        ("POST", "/api/chats/a/start"),
        ("POST", "/api/chats/a/start"),
        ("POST", "/api/chats/a/interrupt"),
    ]


def test_a_refusals_reason_is_its_error_field_or_its_body_when_it_is_not_an_object(
    http_chat_app: HttpChatApp, http_server: FakeHttpServer
) -> None:
    http_server.answer("POST", "/api/chats/a/stop", (500, {"error": "the harness is gone"}))
    http_server.answer("POST", "/api/chats/b/stop", (400, ["not", "an", "object"]))

    with pytest.raises(ChatAppError, match="the harness is gone"):
        http_chat_app.stop_chat("a")
    with pytest.raises(ChatAppError, match=r'\["not", "an", "object"\]'):
        http_chat_app.stop_chat("b")


def test_the_agents_newest_message_after_the_cursor_is_the_answer(
    http_chat_app: HttpChatApp, http_server: FakeHttpServer
) -> None:
    http_server.answer("GET", "/api/chats/a/events?limit=1", (200, {"events": [{"event_id": "e9"}]}))
    http_server.answer(
        "GET",
        "/api/chats/a/events?after=e9&limit=200",
        (
            200,
            {
                "events": [
                    {"type": "assistant_message", "text": "first"},
                    {"type": "tool_use", "text": "x"},
                    {"type": "assistant_message", "text": "Done."},
                ]
            },
        ),
    )
    assert http_chat_app.latest_event_id("a") == "e9"
    assert http_chat_app.assistant_text_after("a", "e9") == "Done."


def test_a_turn_longer_than_a_page_is_read_to_its_end_for_the_newest_message(
    http_chat_app: HttpChatApp, http_server: FakeHttpServer
) -> None:
    """A page after the cursor holds the events that follow it, so the answer may sit past the first page."""
    first_page = [{"event_id": f"e{index}", "type": "tool_use", "text": "x"} for index in range(10, 210)]
    first_page[0] = {"event_id": "e10", "type": "assistant_message", "text": "Starting."}
    http_server.answer("GET", "/api/chats/a/events?after=e9&limit=200", (200, {"events": first_page}))
    http_server.answer(
        "GET",
        "/api/chats/a/events?after=e209&limit=200",
        (200, {"events": [{"event_id": "e210", "type": "assistant_message", "text": "Done."}]}),
    )
    assert http_chat_app.assistant_text_after("a", "e9") == "Done."
    assert [request.path for request in http_server.requests] == [
        "/api/chats/a/events?after=e9&limit=200",
        "/api/chats/a/events?after=e209&limit=200",
    ]


def test_an_unreachable_chat_app_is_an_error_not_a_crash(tmp_path: Path, clock: FakeClock) -> None:
    registry = tmp_path / "apps.toml"
    registry.write_text('[[apps]]\nname = "chat"\nurl = "http://127.0.0.1:9"\n')
    chat_app = HttpChatApp(repo_root=tmp_path, registry_path=registry, environ={}, clock=clock)
    with pytest.raises(ChatAppError, match="could not be reached"):
        chat_app.chat_status("a")


def _chat_app_with_fake_uv(tmp_path: Path, clock: FakeClock, script: str) -> tuple[HttpChatApp, Path]:
    """A chat app whose `uv run mngr create` is the given shell script, which can record its argv in ARGV_FILE."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "uv").write_text("#!/bin/sh\n" + script)
    (fake_bin / "uv").chmod(0o755)
    argv_file = tmp_path / "argv"
    environ = {
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        "ARGV_FILE": str(argv_file),
        "MNGR_HOST_DIR": str(tmp_path / "no-such-host"),
    }
    registry = tmp_path / "apps.toml"
    registry.write_text('[[apps]]\nname = "chat"\nurl = "http://127.0.0.1:9"\n')
    return HttpChatApp(repo_root=tmp_path, registry_path=registry, environ=environ, clock=clock), argv_file


def test_the_chat_is_created_through_mngr_and_named_by_its_created_event(tmp_path: Path, clock: FakeClock) -> None:
    chat_app, argv_file = _chat_app_with_fake_uv(
        tmp_path,
        clock,
        'printf "%s\\n" "$@" > "$ARGV_FILE"\n'
        'echo \'{"event": "progress", "step": "creating"}\'\n'
        'echo \'{"event": "created", "agent_id": "agent-new"}\'\n',
    )

    chat_id = chat_app.create_chat("Morning digest", RoutineId("digest"), "/digest")

    assert chat_id == "agent-new"
    argv = argv_file.read_text().splitlines()
    assert argv[:4] == ["run", "--no-sync", "mngr", "create"]
    assert "--template" in argv and argv[argv.index("--template") + 1] == "automation"
    assert "--label" in argv and "routine=digest" in argv and "display_name=Morning digest" in argv
    assert argv[-2:] == ["--message", "/digest"]


def test_an_agent_of_the_routine_is_found_through_mngr_list_by_either_label(tmp_path: Path, clock: FakeClock) -> None:
    chat_app, argv_file = _chat_app_with_fake_uv(
        tmp_path,
        clock,
        'printf "%s\\n" "$@" > "$ARGV_FILE"\necho "agent-old"\necho "agent-older"\n',
    )

    assert chat_app.find_chat(RoutineId("digest")) == "agent-old"
    argv = argv_file.read_text().splitlines()
    assert argv[:5] == ["run", "--no-sync", "mngr", "list", "--include"]
    assert argv[5] == 'labels.routine == "digest" || labels.automation == "digest"'
    assert argv[6:] == ["--ids", "--on-error", "continue"]


def test_no_agent_of_the_routine_is_none_as_is_a_listing_that_fails(tmp_path: Path, clock: FakeClock) -> None:
    chat_app, _ = _chat_app_with_fake_uv(tmp_path, clock, "")
    assert chat_app.find_chat(RoutineId("digest")) is None
    (tmp_path / "failing").mkdir()
    failing, _ = _chat_app_with_fake_uv(tmp_path / "failing", clock, "exit 2\n")
    assert failing.find_chat(RoutineId("digest")) is None


def test_a_create_that_fails_is_refused_with_its_last_line(tmp_path: Path, clock: FakeClock) -> None:
    chat_app, _ = _chat_app_with_fake_uv(
        tmp_path, clock, 'echo "Signing in first." >&2\necho "No provider account is signed in." >&2\nexit 3\n'
    )
    with pytest.raises(ChatCreateError, match="No provider account is signed in"):
        chat_app.create_chat("Morning digest", RoutineId("digest"), "/digest")


def test_a_create_that_names_no_agent_is_refused(tmp_path: Path, clock: FakeClock) -> None:
    chat_app, _ = _chat_app_with_fake_uv(tmp_path, clock, 'echo \'{"event": "progress"}\'\n')
    with pytest.raises(ChatCreateError, match="named no agent"):
        chat_app.create_chat("Morning digest", RoutineId("digest"), "/digest")


def test_a_send_the_chat_app_holds_for_a_handoff_is_told_apart_from_one_it_took(
    http_chat_app: HttpChatApp, http_server: FakeHttpServer
) -> None:
    http_server.answer("POST", "/api/chats/a/message", (202, {"status": "held", "phase": "starting"}), (200, {}))
    assert http_chat_app.send_message("a", "/clear") is SendVerdict.HELD
    assert http_chat_app.send_message("a", "/digest") is SendVerdict.ACCEPTED
