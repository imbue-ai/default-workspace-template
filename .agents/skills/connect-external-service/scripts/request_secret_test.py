"""Unit tests for the secret request script: what it refuses before it reaches the chat
app, which chat it files against, and how long it waits on a chat app that is not ready.

The filing itself, and the two failures the agent actually sees at the command line
(no chat app, a chat app that refuses), are covered end to end against a real chat app
in system/apps/chat/imbue/chat/test_secret_requests.py.
"""

import importlib.util
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(__file__).resolve().parent / "request_secret.py"


def _load_script() -> Any:
    spec = importlib.util.spec_from_file_location("request_secret_under_test", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


request_secret = _load_script()


class _Answer:
    """What ``message_chat.post_json`` hands back: a status and a parsed body."""

    def __init__(self, status: int, body: object) -> None:
        self.status = status
        self.body = body


class _RecordingChatApp:
    """A stand-in for the message_chat module, answering a scripted sequence of statuses."""

    def __init__(self, statuses: list[int]) -> None:
        self.statuses = statuses
        self.bodies: list[Mapping[str, object]] = []

    def post_json(
        self, base_url: str, path: str, body: Mapping[str, object]
    ) -> _Answer:
        del base_url, path
        self.bodies.append(body)
        status = self.statuses[min(len(self.bodies) - 1, len(self.statuses) - 1)]
        return _Answer(status, {"status": status})


class _FakeClock:
    """A clock the caller advances, so the retry window is exercised without waiting."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def read(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.mark.parametrize(
    ("file", "variables", "rationale"),
    [
        ("Svc", ["A"], "why"),
        ("-svc", ["A"], "why"),
        ("svc/../etc", ["A"], "why"),
        ("svc", [], "why"),
        ("svc", ["1A"], "why"),
        ("svc", ["A-B"], "why"),
        ("svc", ["A", "A"], "why"),
        ("svc", ["A"], "   "),
    ],
)
def test_arguments_that_cannot_become_a_card_are_refused(
    file: str, variables: list[str], rationale: str
) -> None:
    assert request_secret.validate_arguments(file, variables, rationale) is not None


def test_a_well_formed_request_passes_validation() -> None:
    assert (
        request_secret.validate_arguments("svc", ["SVC_TOKEN", "SVC_URL"], "why")
        is None
    )


def test_the_chat_the_request_belongs_to_prefers_the_chat_app_s_own_variable() -> None:
    # The chat app sets MINDS_CHAT_ID on every agent it creates; an agent created any
    # other way is its own chat, and MNGR_AGENT_ID is what names it.
    assert (
        request_secret.chat_id_from_environment(
            {"MINDS_CHAT_ID": "c", "MNGR_AGENT_ID": "a"}
        )
        == "c"
    )
    assert request_secret.chat_id_from_environment({"MNGR_AGENT_ID": "a"}) == "a"
    assert request_secret.chat_id_from_environment({"MINDS_CHAT_ID": ""}) is None
    assert request_secret.chat_id_from_environment({}) is None


def test_a_chat_app_that_is_not_ready_yet_is_retried_within_its_window() -> None:
    chat_app = _RecordingChatApp([503, 503, 201])
    clock = _FakeClock()

    status, _ = request_secret.file_request(
        chat_app, "http://x", {"file": "svc"}, clock.read, clock.sleep
    )

    assert status == 201
    assert len(chat_app.bodies) == 3
    assert clock.slept == [request_secret.NOT_READY_RETRY_INTERVAL_SECONDS] * 2


def test_a_chat_app_stuck_not_ready_gives_up_once_the_window_has_passed() -> None:
    chat_app = _RecordingChatApp([503])
    clock = _FakeClock()

    status, _ = request_secret.file_request(
        chat_app, "http://x", {"file": "svc"}, clock.read, clock.sleep
    )

    assert status == 503
    # It waited roughly the window and no longer, rather than forever or not at all.
    assert clock.now >= request_secret.NOT_READY_RETRY_WINDOW_SECONDS
    assert (
        clock.now
        < request_secret.NOT_READY_RETRY_WINDOW_SECONDS
        + request_secret.NOT_READY_RETRY_INTERVAL_SECONDS
    )


def test_an_answer_that_is_not_503_is_taken_as_it_stands() -> None:
    for status_code in (201, 404, 400):
        chat_app = _RecordingChatApp([status_code])
        clock = _FakeClock()

        status, _ = request_secret.file_request(
            chat_app, "http://x", {"file": "svc"}, clock.read, clock.sleep
        )

        assert status == status_code
        assert clock.slept == []


def test_a_run_outside_an_agent_shell_says_so_and_never_reaches_the_chat_app(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = request_secret.main(
        ["--file", "svc", "--var", "SVC_TOKEN", "--rationale", "why"], environ={}
    )

    assert exit_code == request_secret.EXIT_FAILED
    assert "MINDS_CHAT_ID" in capsys.readouterr().err
