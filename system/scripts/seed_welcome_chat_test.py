"""Tests for seed_welcome_chat.py: the seed POST, its retries while the chat app is not ready, and its output.

Driven through ``main`` with an injected clock and sleeper so the retry window elapses without
waiting; the chat app is the ``fake_chat_app`` fixture over loopback.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest

from conftest import message_chat, seed_welcome_chat

_BODY = {
    "title": "Getting started",
    "turns": [
        {"role": "user", "text": "Wait.. what is honest software?"},
        {
            "role": "assistant",
            "text": "Software that works for you.\nWith quotes: \"yes\" and 'yes'.",
        },
    ],
}


class _FakeClock:
    def __init__(self, step: float) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def _run(*args: str, clock_step: float = 0.0) -> tuple[int, list[float]]:
    slept: list[float] = []
    rc = seed_welcome_chat.main(
        list(args), clock=_FakeClock(clock_step), sleep=slept.append
    )
    return rc, slept


def _encoded(body: object) -> str:
    return base64.b64encode(json.dumps(body).encode("utf-8")).decode("ascii")


def test_a_seeded_chat_is_posted_whole_and_its_id_printed(
    fake_chat_app: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_chat_app.answers = [
        (
            201,
            {
                "chat_id": "agent-1",
                "name": "Getting-started",
                "display_name": "Getting started",
            },
        )
    ]

    rc, slept = _run("--transcript-base64", _encoded(_BODY))

    assert rc == seed_welcome_chat.EXIT_SEEDED
    assert slept == []
    assert fake_chat_app.posted == [(seed_welcome_chat.SEED_PATH, _BODY)]
    assert json.loads(capsys.readouterr().out) == {"chat_id": "agent-1"}


def test_the_transcript_may_come_from_a_file(
    fake_chat_app: Any, tmp_path: Path
) -> None:
    fake_chat_app.answers = [(201, {"chat_id": "agent-1"})]
    transcript = tmp_path / "body.json"
    transcript.write_text(json.dumps(_BODY))

    rc, _ = _run("--transcript-file", str(transcript))

    assert rc == seed_welcome_chat.EXIT_SEEDED
    assert [body for _path, body in fake_chat_app.posted] == [_BODY]


def test_a_chat_app_that_is_not_ready_yet_is_retried_until_it_is(
    fake_chat_app: Any,
) -> None:
    fake_chat_app.answers = [
        (503, {"detail": "The agent list has not been read yet"}),
        (503, {"detail": "The agent list has not been read yet"}),
        (201, {"chat_id": "agent-1"}),
    ]

    rc, slept = _run("--transcript-base64", _encoded(_BODY))

    assert rc == seed_welcome_chat.EXIT_SEEDED
    assert slept == [seed_welcome_chat.RETRY_INTERVAL_SECONDS] * 2
    assert len(fake_chat_app.posted) == 3


def test_a_chat_app_that_never_becomes_ready_fails_after_the_window(
    fake_chat_app: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_chat_app.answers = [(503, {"detail": "The agent list has not been read yet"})]

    rc, slept = _run(
        "--transcript-base64",
        _encoded(_BODY),
        clock_step=seed_welcome_chat.RETRY_WINDOW_SECONDS / 4,
    )

    assert rc == seed_welcome_chat.EXIT_FAILED
    assert len(slept) >= 1
    assert "The agent list has not been read yet" in capsys.readouterr().err


def test_a_refused_seed_fails_at_once(
    fake_chat_app: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_chat_app.answers = [(400, {"detail": "turns must have at least 1 item"})]

    rc, slept = _run("--transcript-base64", _encoded(_BODY))

    assert rc == seed_welcome_chat.EXIT_FAILED
    assert slept == []
    assert "turns must have at least 1 item" in capsys.readouterr().err


def test_an_unreachable_chat_app_is_retried_for_the_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    registry = tmp_path / "apps.toml"
    registry.write_text('[[apps]]\nname = "chat"\nurl = "http://127.0.0.1:1"\n')
    monkeypatch.setenv(message_chat.ENV_APPS_FILE, str(registry))

    rc, slept = _run(
        "--transcript-base64",
        _encoded(_BODY),
        clock_step=seed_welcome_chat.RETRY_WINDOW_SECONDS / 2,
    )

    assert rc == seed_welcome_chat.EXIT_FAILED
    assert slept == [seed_welcome_chat.RETRY_INTERVAL_SECONDS]
    assert "could not connect" in capsys.readouterr().err


def test_a_transcript_that_is_not_a_json_object_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as excinfo:
        _run("--transcript-base64", _encoded([1, 2]))
    assert excinfo.value.code == 2
    with pytest.raises(SystemExit):
        _run("--transcript-base64", base64.b64encode(b"{not json").decode("ascii"))
