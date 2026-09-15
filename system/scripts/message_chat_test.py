"""Tests for message_chat.py: the chat-app send and create, their retries, and the `mngr` backoffs.

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
from mngr_cli_contract.contract import assert_mngr_argv_valid

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


def _run_create(
    *args: str,
    stdin: str = "",
    clock_step: float = 0.0,
) -> tuple[int, list[float]]:
    slept: list[float] = []
    rc = message_chat.main(
        ["--create", *args],
        stdin=io.StringIO(stdin),
        clock=_FakeClock(clock_step),
        sleep=slept.append,
    )
    return rc, slept


def _mngr_calls(record: Path) -> list[dict[str, Any]]:
    if not record.exists():
        return []
    return [json.loads(line) for line in record.read_text().splitlines()]


_CREATED_ANSWER = (
    201,
    {"chat_id": _CHAT_ID, "name": "assist-1a2b3c", "display_name": "assist-1a2b3c"},
)


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


def test_a_dash_initial_message_is_taken_when_bound_with_an_equals_sign(
    fake_chat_app: Any, fake_mngr: Path
) -> None:
    """``--message=-continue`` is how a caller (``create_worker.py reply``) hands over a reply
    that begins with a dash; as a separate ``-m`` value, argparse would read it as an option."""
    rc, _ = _run("--message=-continue")

    assert rc == message_chat.EXIT_DELIVERED
    [(_path, body)] = fake_chat_app.posted
    assert body["message"] == "-continue"


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
    assert call["argv"][:4] == ["message", _CHAT_ID, "--start", "--message-file"]
    assert call["text"] == "<agentic-browser-fleet>wake up</agentic-browser-fleet>"
    # The argv is hand-built, so the live CLI, not the fake, is what says it is well-formed.
    assert_mngr_argv_valid(["mngr", *call["argv"]])


def test_a_backoff_with_no_mngr_on_path_is_a_failure_not_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    registry = tmp_path / "apps.toml"
    registry.write_text('[[apps]]\nname = "chat"\nurl = "http://127.0.0.1:9"\n')
    monkeypatch.setenv(message_chat.ENV_APPS_FILE, str(registry))
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))

    rc, _ = _run("-m", "hello")

    assert rc == message_chat.EXIT_FAILED
    assert "could not run `mngr`" in capsys.readouterr().err


def test_a_connection_dropped_after_the_connect_is_a_failure_not_a_backoff(
    fake_chat_app: Any, fake_mngr: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Once the chat app has taken the connection, a drop is a failure: the request may have
    been acted on, so resending it through ``mngr message`` could deliver the text twice."""
    fake_chat_app.drop_connections = True

    rc, slept = _run("-m", "hello")

    assert rc == message_chat.EXIT_FAILED
    assert slept == []
    assert "dropped the request" in capsys.readouterr().err
    assert len(fake_chat_app.posted) == 1
    assert _mngr_calls(fake_mngr) == []


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
    # `--start` mirrors the chat app's revive-on-send, so a stopped agent is reached either way.
    assert call["argv"][:3] == ["message", _CHAT_ID, "--start"]
    assert call["text"] == "hello"
    assert_mngr_argv_valid(["mngr", *call["argv"]])


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
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
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
    # A missing or rowless registry is the expected "not registered yet"; nothing is said.
    assert capsys.readouterr().err == ""

    registry.write_text("not toml at all [[")
    assert (
        message_chat.chat_app_url({message_chat.ENV_APPS_FILE: str(registry)}, tmp_path)
        == message_chat.CHAT_APP_FALLBACK_URL
    )
    # One that exists but does not parse is a real fault, so it is named before the fallback.
    assert f"{registry} does not parse" in capsys.readouterr().err


def test_no_message_on_a_terminal_is_a_usage_error(
    fake_chat_app: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    class _Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    with pytest.raises(SystemExit) as raised:
        message_chat.main([_CHAT_ID], stdin=_Tty())

    # argparse's usage code, distinct from EXIT_FAILED ("the send failed").
    assert raised.value.code == 2
    assert "no message given" in capsys.readouterr().err
    assert fake_chat_app.posted == []


def test_a_create_asks_the_chat_app_to_wait_and_prints_the_chat_it_made(
    fake_chat_app: Any, fake_mngr: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_chat_app.answers = [_CREATED_ANSWER]

    rc, slept = _run_create(
        "--name",
        "assist-1a2b3c",
        "--label",
        "auto_open=true",
        "--label",
        "assist=true",
        "--skip-installation-check",
        "-m",
        "/assist it broke",
    )

    assert rc == message_chat.EXIT_DELIVERED
    assert slept == []
    [(path, body)] = fake_chat_app.posted
    assert path == message_chat.CREATE_CHAT_PATH
    assert body == {
        "name": "assist-1a2b3c",
        "message": "/assist it broke",
        "labels": {"auto_open": "true", "assist": "true"},
        "is_installation_check_skipped": True,
        "should_wait": True,
    }
    # The one stdout line is the chat's identity, for a caller that stamps it somewhere.
    assert json.loads(capsys.readouterr().out) == _CREATED_ANSWER[1]
    assert _mngr_calls(fake_mngr) == []


def test_a_create_with_no_message_on_a_terminal_sends_an_empty_first_message(
    fake_chat_app: Any,
) -> None:
    class _Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    fake_chat_app.answers = [_CREATED_ANSWER]

    rc = message_chat.main(
        ["--create"], stdin=_Tty(), clock=_FakeClock(0.0), sleep=lambda _: None
    )

    assert rc == message_chat.EXIT_DELIVERED
    [(_, body)] = fake_chat_app.posted
    assert body["message"] == "" and body["name"] == "" and body["labels"] == {}
    assert body["is_installation_check_skipped"] is False


@pytest.mark.parametrize(
    "status, body",
    [
        (
            500,
            {
                "detail": "mngr create exited with code 1\nError: no provider account is signed in"
            },
        ),
        (
            409,
            {
                "detail": "A chat named 'assist-1a2b3c' already exists; pick another name"
            },
        ),
        (
            400,
            {
                "detail": "The chat app sets display_name itself; a create cannot restate them"
            },
        ),
        (504, {"detail": "Chat 'assist-1a2b3c' is still being created"}),
    ],
)
def test_a_create_the_chat_app_refused_or_failed_is_final_without_the_backoff(
    fake_chat_app: Any,
    fake_mngr: Path,
    capsys: pytest.CaptureFixture[str],
    status: int,
    body: dict[str, str],
) -> None:
    fake_chat_app.answers = [(status, body)]

    rc, _ = _run_create("--name", "assist-1a2b3c", "-m", "/assist it broke")

    assert rc == message_chat.EXIT_FAILED
    captured = capsys.readouterr()
    assert body["detail"] in captured.err
    assert captured.out == ""
    assert _mngr_calls(fake_mngr) == []


def test_a_not_ready_create_is_retried_until_the_chat_app_takes_it(
    fake_chat_app: Any, fake_mngr: Path
) -> None:
    fake_chat_app.answers = [
        (503, {"detail": "Agent list not known yet"}),
        (503, {"detail": "Agent list not known yet"}),
        _CREATED_ANSWER,
    ]

    rc, slept = _run_create("-m", "/assist", clock_step=1.0)

    assert rc == message_chat.EXIT_DELIVERED
    assert slept == [message_chat.NOT_READY_RETRY_INTERVAL_SECONDS] * 2
    assert len(fake_chat_app.posted) == 3
    assert _mngr_calls(fake_mngr) == []


def test_an_unreachable_chat_app_hands_the_create_to_mngr_with_the_same_terms(
    fake_mngr: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    registry = tmp_path / "apps.toml"
    registry.write_text('[[apps]]\nname = "chat"\nurl = "http://127.0.0.1:9"\n')
    monkeypatch.setenv(message_chat.ENV_APPS_FILE, str(registry))
    monkeypatch.setenv(
        "FAKE_MNGR_STDOUT",
        '{"event": "created", "agent_id": "%s", "host_id": "h", "host_name": "ws"}\n'
        % _CHAT_ID,
    )

    rc, _ = _run_create(
        "--name",
        "update-self-1a2b3c",
        "--label",
        "auto_open=true",
        "--skip-installation-check",
        "-m",
        "/update-self",
    )

    assert rc == message_chat.EXIT_DELIVERED
    [call] = _mngr_calls(fake_mngr)
    assert call["argv"][:2] == ["create", "update-self-1a2b3c"]
    assert call["argv"][call["argv"].index("--template") + 1] == "chat"
    assert "--no-connect" in call["argv"]
    labels = [
        call["argv"][i + 1]
        for i, token in enumerate(call["argv"])
        if token == "--label"
    ]
    assert labels == ["user_created=true", "auto_open=true"]
    assert (
        call["argv"][call["argv"].index("-S") + 1]
        == message_chat.SKIP_CLAUDE_INSTALLATION_CHECK_SETTING
    )
    assert call["text"] == "/update-self"
    assert call["argv"][-2:] == ["--format", "jsonl"]
    assert_mngr_argv_valid(["mngr", *call["argv"]])
    # The backoff keeps the stdout contract: the chat's id, read from mngr's created event.
    assert json.loads(capsys.readouterr().out) == {
        "chat_id": _CHAT_ID,
        "name": "update-self-1a2b3c",
        "display_name": "update-self-1a2b3c",
    }


def test_a_chat_app_without_the_create_route_hands_the_create_to_mngr(
    fake_chat_app: Any, fake_mngr: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_chat_app.answers = [(404, {"detail": "not found"})]
    monkeypatch.setenv("FAKE_MNGR_EXIT", "3")

    rc, slept = _run_create(
        "-m", "/assist", clock_step=message_chat.UNKNOWN_RETRY_WINDOW_SECONDS / 4
    )

    # A failed backoff create passes mngr's exit status through, as the message backoff does.
    assert rc == 3
    assert slept and all(
        interval == message_chat.UNKNOWN_RETRY_INTERVAL_SECONDS for interval in slept
    )
    [call] = _mngr_calls(fake_mngr)
    # No name: mngr mints one, as the chat app would have.
    assert call["argv"][:3] == ["create", "--template", "chat"]
    assert_mngr_argv_valid(["mngr", *call["argv"]])


@pytest.mark.parametrize(
    "argv, complaint",
    [
        (["--create", _CHAT_ID, "-m", "x"], "takes no chat id"),
        (["--create", "--system", "-m", "x"], "does not apply to --create"),
        ([_CHAT_ID, "--name", "n", "-m", "x"], "apply only with --create"),
        ([_CHAT_ID, "--label", "a=b", "-m", "x"], "apply only with --create"),
        (["--create", "--label", "novalue", "-m", "x"], "takes NAME=VALUE"),
        (["-m", "x"], "a chat id is required"),
    ],
)
def test_the_two_modes_refuse_each_others_arguments(
    fake_chat_app: Any,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    complaint: str,
) -> None:
    with pytest.raises(SystemExit) as raised:
        message_chat.main(
            argv, stdin=io.StringIO(""), clock=_FakeClock(0.0), sleep=lambda _: None
        )

    assert raised.value.code == 2
    assert complaint in capsys.readouterr().err
    assert fake_chat_app.posted == []
