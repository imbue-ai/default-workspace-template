"""How the chat reaches the shell: what ``post_to_shell`` logs for a shell that is down, refuses the body, or fails,
and what the layout client makes of the shell's client list and its answer to a ``show``, over loopback servers
standing in for the shell."""

import json
from typing import Any

import pytest
from flask import Flask
from flask import Response
from flask import jsonify

from imbue.chat.shell_client import DisconnectedShell
from imbue.chat.shell_client import ShellAnswerMalformedError
from imbue.chat.shell_client import ShellLayoutClient
from imbue.chat.shell_client import ShellRefusedOpError
from imbue.chat.shell_client import ShellUnreachableError
from imbue.chat.shell_client import ShowAnswer
from imbue.chat.shell_client import ShowRequest
from imbue.chat.shell_client import post_to_shell
from imbue.chat.testing import RecordingLayoutOpShell
from imbue.chat.testing import serve_app
from imbue.system_interface.testing import find_free_port

_SHOW = ShowRequest(path="/?chat=agent-1", showing=("/agent-1",), repoint=("/",), client_id="client-1")
_SHOWN_ANSWER = {"ok": True, "shown": "navigated", "window_id": "win-0123456789abcdef", "desktop_id": "home"}


def _shell_answering(status: int, body: str) -> Flask:
    application = Flask("answering-shell")
    application.add_url_rule(
        "/api/client-activity",
        view_func=lambda: Response(body, status=status),
        methods=["POST"],
        endpoint="client_activity",
    )
    return application


def test_an_unreachable_shell_is_a_debug_log_and_no_error(loguru_records: list[str]) -> None:
    post_to_shell(f"http://127.0.0.1:{find_free_port()}/api/client-activity", {"kind": "message"})

    assert any(record.startswith("DEBUG Skipped posting to the shell") for record in loguru_records)
    assert not any("refused the body" in record for record in loguru_records)


def test_a_shell_that_refuses_the_body_is_a_warning_quoting_the_refusal(loguru_records: list[str]) -> None:
    with serve_app(_shell_answering(400, "desktop_id is required")) as served:
        post_to_shell(f"{served.http_url}/api/client-activity", {"kind": "message"})

    assert any(
        record.startswith("WARNING") and "refused the body with 400: desktop_id is required" in record
        for record in loguru_records
    )


def test_a_failing_shell_is_a_debug_log_and_no_warning(loguru_records: list[str]) -> None:
    with serve_app(_shell_answering(500, "boom")) as served:
        post_to_shell(f"{served.http_url}/api/client-activity", {"kind": "message"})

    assert any(record.startswith("DEBUG") and "answered 500" in record for record in loguru_records)
    assert not any("refused the body" in record for record in loguru_records)


def test_a_show_posts_the_show_op_for_this_app_and_answers_the_shells_shown_and_window() -> None:
    shell = RecordingLayoutOpShell(200, json.dumps(_SHOWN_ANSWER))

    with serve_app(shell.application) as served:
        answer = ShellLayoutClient(shell_url=served.http_url).show(_SHOW)

    assert answer == ShowAnswer(shown="navigated", window_id="win-0123456789abcdef")
    assert shell.received == [
        {
            "op": "show",
            "args": {
                "app": "chat",
                "path": "/?chat=agent-1",
                "showing": ["/agent-1"],
                "repoint": ["/"],
                "client": "client-1",
            },
            "requester": {"app": "chat", "marker": ""},
        }
    ]


def test_a_shown_the_chat_app_does_not_know_is_passed_through() -> None:
    shell = RecordingLayoutOpShell(200, json.dumps({**_SHOWN_ANSWER, "shown": "tiled"}))

    with serve_app(shell.application) as served:
        answer = ShellLayoutClient(shell_url=served.http_url).show(_SHOW)

    assert answer.shown == "tiled"


def test_a_shell_that_refuses_the_show_raises_quoting_the_status_and_the_refusal() -> None:
    shell = RecordingLayoutOpShell(404, json.dumps({"detail": "No client 'client-1'"}))

    with serve_app(shell.application) as served:
        with pytest.raises(ShellRefusedOpError, match=r"\(404\).*No client 'client-1'"):
            ShellLayoutClient(shell_url=served.http_url).show(_SHOW)

    assert len(shell.received) == 1


def test_a_shell_that_cannot_be_reached_for_a_show_raises() -> None:
    with pytest.raises(ShellUnreachableError, match="Could not reach the shell"):
        ShellLayoutClient(shell_url=f"http://127.0.0.1:{find_free_port()}").show(_SHOW)


@pytest.mark.parametrize(
    "body",
    ["not json", "[]", json.dumps({"ok": True}), json.dumps({**_SHOWN_ANSWER, "window_id": None})],
    ids=["not-json", "a-list", "no-shown", "no-window"],
)
def test_a_2xx_to_a_show_that_is_not_a_show_answer_raises(body: str) -> None:
    with serve_app(RecordingLayoutOpShell(200, body).application) as served:
        with pytest.raises(ShellAnswerMalformedError):
            ShellLayoutClient(shell_url=served.http_url).show(_SHOW)


def test_the_disconnected_shell_reaches_nobody() -> None:
    shell = DisconnectedShell()

    assert shell.connected_client_ids() == []
    with pytest.raises(ShellUnreachableError):
        shell.show(_SHOW)


@pytest.mark.parametrize(
    ("body", "expected"),
    (
        ({"clients": [{"id": "c1", "is_connected": True}, {"id": "c2", "is_connected": False}]}, ["c1"]),
        ({"clients": []}, []),
        ({}, []),
        ({"clients": {"c1": True}}, []),
        ({"clients": "c1"}, []),
        ([{"id": "c1", "is_connected": True}], []),
        ({"clients": [{"is_connected": True}, "c2"]}, []),
    ),
    ids=("the-contract", "nobody", "no-key", "a-map", "a-string", "a-bare-list", "entries-without-an-id"),
)
def test_a_client_list_of_the_wrong_shape_reads_as_nobody_rather_than_killing_the_flush_thread(
    body: Any, expected: list[str]
) -> None:
    """The auto-open flush thread's own catch does not cover a KeyError or TypeError from reading this, so an
    answer the shell should never give would end the thread and silently stop surfacing every window."""
    application = Flask("stub-shell")
    application.add_url_rule("/api/clients", view_func=lambda: jsonify(body), endpoint="clients")
    with serve_app(application) as served:
        assert ShellLayoutClient(shell_url=served.http_url).connected_client_ids() == expected
