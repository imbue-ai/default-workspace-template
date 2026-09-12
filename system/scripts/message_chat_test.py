"""Tests for message_chat.py: the chat-app send, its retries, and the `mngr message` backoff.

The script is driven through ``main`` with an injected clock and sleeper so the retry windows
elapse without waiting; the chat app is the ``fake_chat_app`` fixture over loopback and
``mngr`` is the recording fake the ``fake_mngr`` fixture puts on PATH.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from conftest import message_chat

_CHAT_ID = "agent-0123456789abcdef0123456789abcdef"


class _FakeClock:
    """A clock that advances by ``step`` on every read, so a retry window elapses in a few reads."""

    def __init__(self, step: float) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def _run(
    *args: str,
    stdin: str = "",
    clock_step: float = 0.0,
) -> tuple[int, list[float]]:
    slept: list[float] = []
    stdin_stream = io.StringIO(stdin)
    rc = message_chat.main(
        [_CHAT_ID, *args],
        stdin=stdin_stream,
        clock=_FakeClock(clock_step),
        sleep=slept.append,
    )
    return rc, slept


def _mngr_calls(record: Path) -> list[dict[str, Any]]:
    if not record.exists():
        return []
    return [json.loads(line) for line in record.read_text().splitlines()]


def test_a_delivered_send_posts_the_text_with_a_minted_id_and_no_client_fields(
    fake_chat_app: Any, fake_mngr: Path
) -> None:
    rc, slept = _run("-m", "hello there")

    assert rc == message_chat.EXIT_DELIVERED
    assert slept == []
    [(path, body)] = fake_chat_app.posted
    assert path == f"/api/agents/{_CHAT_ID}/message"
    assert body["message"] == "hello there"
    assert len(body["message_id"]) == 32
    assert set(body) == {"message", "message_id"}
    assert _mngr_calls(fake_mngr) == []


def test_the_message_comes_from_a_file_or_stdin_when_not_given_inline(
    fake_chat_app: Any, fake_mngr: Path, tmp_path: Path
) -> None:
    message_file = tmp_path / "task.md"
    message_file.write_text("from the file\n")

    assert _run("--message-file", str(message_file))[0] == message_chat.EXIT_DELIVERED
    assert _run(stdin="from stdin\n")[0] == message_chat.EXIT_DELIVERED

    assert [body["message"] for _path, body in fake_chat_app.posted] == [
        "from the file\n",
        "from stdin\n",
    ]


def test_a_system_message_is_wrapped_in_the_sentinel(
    fake_chat_app: Any, fake_mngr: Path
) -> None:
    rc, _ = _run("--system", "-m", "the browser is yours again")

    assert rc == message_chat.EXIT_DELIVERED
    [(_path, body)] = fake_chat_app.posted
    assert (
        body["message"]
        == "<agentic-browser-fleet>the browser is yours again</agentic-browser-fleet>"
    )


def test_a_blocked_send_exits_seven_and_is_never_resent(
    fake_chat_app: Any, fake_mngr: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_chat_app.answers = [
        (
            500,
            {
                "detail": "a permission dialog is holding the input",
                "kind": "input_blocked",
            },
        )
    ]

    rc, _ = _run("-m", "hello")

    assert rc == message_chat.EXIT_DELIVERED_BUT_BLOCKED
    assert "a permission dialog is holding the input" in capsys.readouterr().err
    assert len(fake_chat_app.posted) == 1
    assert _mngr_calls(fake_mngr) == []


@pytest.mark.parametrize(
    "status, body",
    [
        (500, {"detail": "the agent's pane is gone", "kind": "agent_unreachable"}),
        (409, {"detail": "the chat is converging"}),
        (400, {"detail": "bad request"}),
    ],
)
def test_a_refusal_fails_without_the_backoff(
    fake_chat_app: Any,
    fake_mngr: Path,
    capsys: pytest.CaptureFixture[str],
    status: int,
    body: dict[str, str],
) -> None:
    fake_chat_app.answers = [(status, body)]

    rc, _ = _run("-m", "hello")

    assert rc == message_chat.EXIT_FAILED
    assert body["detail"] in capsys.readouterr().err
    assert _mngr_calls(fake_mngr) == []


def test_a_not_ready_answer_is_retried_until_the_chat_app_takes_the_message(
    fake_chat_app: Any, fake_mngr: Path
) -> None:
    fake_chat_app.answers = [
        (503, {"detail": "the agent list is not known yet"}),
        (503, {"detail": "the agent list is not known yet"}),
        (200, {"status": "ok"}),
    ]

    rc, slept = _run("-m", "hello")

    assert rc == message_chat.EXIT_DELIVERED
    assert slept == [message_chat.NOT_READY_RETRY_INTERVAL_SECONDS] * 2
    assert len(fake_chat_app.posted) == 3
    # Every retry carries the same message id: it is one message, however many attempts.
    assert len({body["message_id"] for _path, body in fake_chat_app.posted}) == 1
    assert _mngr_calls(fake_mngr) == []


def test_a_not_ready_answer_that_outlasts_the_window_is_a_failure_not_a_backoff(
    fake_chat_app: Any, fake_mngr: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_chat_app.answers = [(503, {"detail": "still starting"})]

    rc, slept = _run(
        "-m", "hello", clock_step=message_chat.NOT_READY_RETRY_WINDOW_SECONDS / 4
    )

    assert rc == message_chat.EXIT_FAILED
    assert slept
    assert "still starting" in capsys.readouterr().err
    assert _mngr_calls(fake_mngr) == []


def test_an_unreachable_chat_app_hands_the_message_to_mngr_and_passes_its_exit_code_through(
    fake_mngr: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A registry row pointing at a port nothing listens on: the connection fails outright.
    registry = tmp_path / "apps.toml"
    registry.write_text('[[apps]]\nname = "chat"\nurl = "http://127.0.0.1:9"\n')
    monkeypatch.setenv(message_chat.ENV_APPS_FILE, str(registry))
    monkeypatch.setenv("FAKE_MNGR_EXIT", "7")

    rc, slept = _run("--system", "-m", "wake up")

    assert rc == 7
    assert slept == []
    [call] = _mngr_calls(fake_mngr)
    assert call["argv"][:3] == ["message", _CHAT_ID, "--message-file"]
    assert call["text"] == "<agentic-browser-fleet>wake up</agentic-browser-fleet>"


def test_a_persisting_404_hands_the_message_to_mngr(
    fake_chat_app: Any, fake_mngr: Path
) -> None:
    fake_chat_app.answers = [(404, {"detail": "Agent 'x' not found"})]

    rc, slept = _run(
        "-m", "hello", clock_step=message_chat.UNKNOWN_RETRY_WINDOW_SECONDS / 4
    )

    assert rc == message_chat.EXIT_DELIVERED
    assert slept and all(
        interval == message_chat.UNKNOWN_RETRY_INTERVAL_SECONDS for interval in slept
    )
    [call] = _mngr_calls(fake_mngr)
    assert call["argv"][:2] == ["message", _CHAT_ID]
    assert call["text"] == "hello"


def test_a_404_that_clears_within_the_window_is_delivered_by_the_chat_app(
    fake_chat_app: Any, fake_mngr: Path
) -> None:
    fake_chat_app.answers = [(404, {"detail": "not yet"}), (200, {"status": "ok"})]

    rc, slept = _run("-m", "hello")

    assert rc == message_chat.EXIT_DELIVERED
    assert slept == [message_chat.UNKNOWN_RETRY_INTERVAL_SECONDS]
    assert len(fake_chat_app.posted) == 2
    assert _mngr_calls(fake_mngr) == []


def test_the_chat_app_url_comes_from_the_registry_row_else_the_fixed_port(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "apps.toml"

    assert message_chat.chat_app_url({}, tmp_path) == message_chat.CHAT_APP_FALLBACK_URL

    registry.write_text('[[apps]]\nname = "terminal"\nurl = "http://127.0.0.1:7681"\n')
    assert (
        message_chat.chat_app_url({message_chat.ENV_APPS_FILE: str(registry)}, tmp_path)
        == message_chat.CHAT_APP_FALLBACK_URL
    )

    registry.write_text('[[apps]]\nname = "chat"\nurl = "http://127.0.0.1:8123/"\n')
    assert (
        message_chat.chat_app_url({message_chat.ENV_APPS_FILE: str(registry)}, tmp_path)
        == "http://127.0.0.1:8123"
    )

    registry.write_text("not toml at all [[")
    assert (
        message_chat.chat_app_url({message_chat.ENV_APPS_FILE: str(registry)}, tmp_path)
        == message_chat.CHAT_APP_FALLBACK_URL
    )


def test_no_message_on_a_terminal_is_a_usage_error(fake_chat_app: Any) -> None:
    class _Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    with pytest.raises(SystemExit) as raised:
        message_chat.main([_CHAT_ID], stdin=_Tty())

    assert "no message given" in str(raised.value)
    assert fake_chat_app.posted == []
