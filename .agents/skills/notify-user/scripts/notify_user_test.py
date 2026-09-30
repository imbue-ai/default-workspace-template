"""Tests for the notify-user skill's script: what it posts, who it says is watching, and that every failure is
reported."""

from __future__ import annotations

import importlib.util
import json
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parent / "notify_user.py"
_spec = importlib.util.spec_from_file_location("notify_user", _SCRIPT)
assert _spec is not None and _spec.loader is not None
notify_user = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(notify_user)

_GATEWAY_ENV = {
    "LATCHKEY_GATEWAY": "http://gateway.invalid:1234/",
    "LATCHKEY_GATEWAY_PASSWORD": "pw",
    "MNGR_AGENT_ID": "agent-self",
    "MINDS_CHAT_ID": "agent-chat",
}

_CHAT_APP_URL = "http://127.0.0.1:18731"

_WATCH_WARNING = "notify-user: could not check who is watching this chat ("


@pytest.fixture
def environ(tmp_path: Path) -> dict[str, str]:
    """The gateway env plus an app registry naming the chat app's address."""
    return _env_with_chat_app_at(
        _GATEWAY_ENV, tmp_path / "apps.toml", f"{_CHAT_APP_URL}/"
    )


def _env_with_chat_app_at(
    base_env: dict[str, str], registry: Path, url: str
) -> dict[str, str]:
    """``base_env`` pointed at an app registry, written to ``registry``, whose chat app is at ``url``."""
    registry.write_text(f'[[apps]]\nname = "chat"\nurl = "{url}"\n')
    return {**base_env, "MINDS_APPS_FILE": str(registry)}


def _watchers_answer(*instance_ids: str) -> tuple[int | None, str]:
    return 200, json.dumps({"watched_by": list(instance_ids)})


class _RecordingHttp(notify_user.HttpClient):
    """Records the POST and the watcher question, and answers each with a caller-supplied result."""

    def __init__(
        self,
        status: int | None,
        text: str = "",
        watchers: tuple[int | None, str] = (200, '{"watched_by": []}'),
    ) -> None:
        self._status = status
        self._text = text
        self._watchers = watchers
        self.posts: list[tuple[str, dict, dict]] = []
        self.gets: list[tuple[str, float]] = []

    def post_json(
        self, url: str, payload: dict, headers: dict, timeout: float
    ) -> tuple[int | None, str]:
        self.posts.append((url, payload, headers))
        return self._status, self._text

    def get(self, url: str, timeout: float) -> tuple[int | None, str]:
        self.gets.append((url, timeout))
        return self._watchers


def test_posts_to_the_runtime_agents_route(environ: dict[str, str]) -> None:
    http = _RecordingHttp(200, '{"ok": true}')

    is_accepted = notify_user.notify(
        "The build is green.", None, http=http, environ=environ
    )

    assert is_accepted is True
    ((url, payload, headers),) = http.posts
    assert (
        url
        == "http://gateway.invalid:1234/minds-api-proxy/api/v1/agents/agent-self/notifications"
    )
    assert payload["message"] == "The build is green."
    assert headers["X-Latchkey-Gateway-Password"] == "pw"
    assert "X-Latchkey-Gateway-Permissions-Override" not in headers


def test_says_which_pages_are_watching_the_chat(
    environ: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    http = _RecordingHttp(200, watchers=_watchers_answer("page-a", "page-b"))

    assert notify_user.notify("Done.", None, http=http, environ=environ) is True

    assert http.gets == [(f"{_CHAT_APP_URL}/api/chats/agent-chat/watchers", 2.0)]
    ((_url, payload, _headers),) = http.posts
    assert payload == {"message": "Done.", "watched_by": ["page-a", "page-b"]}
    assert _WATCH_WARNING not in capsys.readouterr().err


def test_says_nobody_is_watching_when_nobody_is(environ: dict[str, str]) -> None:
    http = _RecordingHttp(200, watchers=_watchers_answer())

    notify_user.notify("Done.", None, http=http, environ=environ)

    ((_url, payload, _headers),) = http.posts
    assert payload == {"message": "Done.", "watched_by": []}


def test_asks_about_the_agents_own_chat_when_it_has_no_chat_id(
    environ: dict[str, str],
) -> None:
    http = _RecordingHttp(200)
    del environ["MINDS_CHAT_ID"]

    notify_user.notify("Done.", None, http=http, environ=environ)

    assert http.gets == [(f"{_CHAT_APP_URL}/api/chats/agent-self/watchers", 2.0)]


@pytest.mark.parametrize(
    ("watchers", "expected_reason"),
    [
        ((None, "timed out"), "the chat app did not answer: timed out"),
        ((404, '{"detail": "Nothing is served"}'), "the chat app answered 404"),
        ((200, "<html></html>"), "the chat app's answer is not JSON"),
        (
            (200, '{"watched_by": "page-a"}'),
            "the chat app's answer carries no list of watchers",
        ),
    ],
)
def test_sends_anyway_with_a_warning_when_the_watchers_are_unknown(
    watchers: tuple[int | None, str],
    expected_reason: str,
    environ: dict[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    http = _RecordingHttp(200, watchers=watchers)

    is_accepted = notify_user.notify("Done.", None, http=http, environ=environ)

    assert is_accepted is True
    ((_url, payload, _headers),) = http.posts
    assert payload == {"message": "Done."}
    assert (
        f"{_WATCH_WARNING}{expected_reason}); sending anyway" in capsys.readouterr().err
    )


@pytest.fixture
def silent_chat_app(
    environ: dict[str, str], tmp_path: Path
) -> Iterator[dict[str, str]]:
    """A chat app address whose listener takes the connection and never answers."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    chat_app_env = _env_with_chat_app_at(
        environ,
        tmp_path / "silent_apps.toml",
        f"http://127.0.0.1:{listener.getsockname()[1]}",
    )
    try:
        yield chat_app_env
    finally:
        listener.close()


class _RecordingPostHttp(notify_user.HttpClient):
    """The real watcher question, and a recorded POST that the app accepts."""

    def __init__(self) -> None:
        self.posts: list[dict] = []

    def post_json(
        self, url: str, payload: dict, headers: dict, timeout: float
    ) -> tuple[int | None, str]:
        self.posts.append(payload)
        return 200, '{"ok": true}'


def test_a_chat_app_that_never_answers_delays_the_notification_by_the_watcher_timeout_only(
    silent_chat_app: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    http = _RecordingPostHttp()
    started = time.monotonic()

    is_accepted = notify_user.notify("Done.", None, http=http, environ=silent_chat_app)

    elapsed = time.monotonic() - started
    assert is_accepted is True
    assert http.posts == [{"message": "Done."}]
    assert 1.5 < elapsed < 6.0
    assert _WATCH_WARNING in capsys.readouterr().err


@pytest.fixture
def garbled_chat_app(
    environ: dict[str, str], tmp_path: Path
) -> Iterator[dict[str, str]]:
    """A chat app address that answers the watcher question with something that is not HTTP."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def answer_once() -> None:
        connection, _address = listener.accept()
        with connection:
            connection.recv(65536)
            connection.sendall(b"NOT-HTTP\r\n\r\n")

    answering = threading.Thread(target=answer_once, daemon=True)
    answering.start()
    chat_app_env = _env_with_chat_app_at(
        environ,
        tmp_path / "garbled_apps.toml",
        f"http://127.0.0.1:{listener.getsockname()[1]}",
    )
    try:
        yield chat_app_env
    finally:
        listener.close()
        answering.join(timeout=5.0)


def test_a_garbled_watcher_answer_still_sends_the_notification(
    garbled_chat_app: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    http = _RecordingPostHttp()

    is_accepted = notify_user.notify("Done.", None, http=http, environ=garbled_chat_app)

    assert is_accepted is True
    assert http.posts == [{"message": "Done."}]
    assert f"{_WATCH_WARNING}the chat app did not answer: " in capsys.readouterr().err


def test_carries_the_optional_title_and_the_permissions_override_when_present(
    environ: dict[str, str],
) -> None:
    http = _RecordingHttp(200)
    environ = {**environ, "LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE": "jwt"}

    notify_user.notify("All tests passed.", "Test run", http=http, environ=environ)

    ((_url, payload, headers),) = http.posts
    assert payload == {
        "message": "All tests passed.",
        "title": "Test run",
        "watched_by": [],
    }
    assert headers["X-Latchkey-Gateway-Permissions-Override"] == "jwt"


@pytest.mark.parametrize(
    ("status", "text", "expected_fragment"),
    [
        (None, "connection refused", "unreachable"),
        (403, '{"error": "not permitted"}', "answered 403"),
        (501, '{"error": "Notification feed not configured"}', "answered 501"),
    ],
)
def test_reports_an_unreachable_or_refusing_app_and_fails(
    status: int | None,
    text: str,
    expected_fragment: str,
    environ: dict[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    http = _RecordingHttp(status, text)

    is_accepted = notify_user.notify("Done.", None, http=http, environ=environ)

    assert is_accepted is False
    stderr = capsys.readouterr().err
    assert expected_fragment in stderr
    assert "did not go out" in stderr


def test_reports_a_missing_gateway_env_without_posting(
    capsys: pytest.CaptureFixture[str],
) -> None:
    http = _RecordingHttp(200)

    is_accepted = notify_user.notify("Done.", None, http=http, environ={})

    assert is_accepted is False
    assert http.posts == []
    assert "did not go out" in capsys.readouterr().err


def test_requires_a_runtime_agent_id(capsys: pytest.CaptureFixture[str]) -> None:
    http = _RecordingHttp(200)
    environ = {
        "LATCHKEY_GATEWAY": "http://gateway.invalid",
        "LATCHKEY_GATEWAY_PASSWORD": "pw",
    }

    is_accepted = notify_user.notify("Done.", None, http=http, environ=environ)

    assert is_accepted is False
    assert http.posts == []
    stderr = capsys.readouterr().err
    assert "MNGR_AGENT_ID is not set" in stderr
    assert "did not go out" in stderr


def test_main_exit_code_follows_acceptance(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in _GATEWAY_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("LATCHKEY_GATEWAY", "http://127.0.0.1:1")  # nothing listens here

    assert notify_user.main(["Done."]) == 1
