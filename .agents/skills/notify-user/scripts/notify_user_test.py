"""Tests for the notify-user skill's script: what it posts, and that every failure is reported."""

from __future__ import annotations

import importlib.util
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
    "MINDS_CHAT_ID": "agent-chat",
    "MNGR_AGENT_ID": "agent-self",
}


class _RecordingHttp(notify_user.HttpClient):
    """Records the one POST and answers with a caller-supplied result."""

    def __init__(self, status: int | None, text: str = "") -> None:
        self._status = status
        self._text = text
        self.posts: list[tuple[str, dict, dict]] = []

    def post_json(self, url: str, payload: dict, headers: dict, timeout: float) -> tuple[int | None, str]:
        self.posts.append((url, payload, headers))
        return self._status, self._text


def test_posts_to_the_runtime_agents_route_when_the_chat_id_differs() -> None:
    http = _RecordingHttp(200, '{"ok": true}')

    is_accepted = notify_user.notify("The build is green.", None, http=http, environ=dict(_GATEWAY_ENV))

    assert is_accepted is True
    ((url, payload, headers),) = http.posts
    assert url == "http://gateway.invalid:1234/minds-api-proxy/api/v1/agents/agent-self/notifications"
    assert payload == {"message": "The build is green."}
    assert headers["X-Latchkey-Gateway-Password"] == "pw"
    assert "X-Latchkey-Gateway-Permissions-Override" not in headers


def test_carries_the_optional_title_and_the_permissions_override_when_present() -> None:
    http = _RecordingHttp(200)
    environ = {**_GATEWAY_ENV, "LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE": "jwt"}

    notify_user.notify("All tests passed.", "Test run", http=http, environ=environ)

    ((_url, payload, headers),) = http.posts
    assert payload == {"message": "All tests passed.", "title": "Test run"}
    assert headers["X-Latchkey-Gateway-Permissions-Override"] == "jwt"


def test_posts_to_the_runtime_agents_route_without_a_separate_chat_id() -> None:
    http = _RecordingHttp(200)
    environ = {key: value for key, value in _GATEWAY_ENV.items() if key != "MINDS_CHAT_ID"}

    notify_user.notify("Done.", None, http=http, environ=environ)

    ((url, _payload, _headers),) = http.posts
    assert url.endswith("/agents/agent-self/notifications")


@pytest.mark.parametrize(
    ("status", "text", "expected_fragment"),
    [
        (None, "connection refused", "unreachable"),
        (403, '{"error": "not permitted"}', "answered 403"),
        (501, '{"error": "Notification feed not configured"}', "answered 501"),
    ],
)
def test_reports_an_unreachable_or_refusing_app_and_fails(
    status: int | None, text: str, expected_fragment: str, capsys: pytest.CaptureFixture[str]
) -> None:
    http = _RecordingHttp(status, text)

    is_accepted = notify_user.notify("Done.", None, http=http, environ=dict(_GATEWAY_ENV))

    assert is_accepted is False
    stderr = capsys.readouterr().err
    assert expected_fragment in stderr
    assert "did not go out" in stderr


def test_reports_a_missing_gateway_env_without_posting(capsys: pytest.CaptureFixture[str]) -> None:
    http = _RecordingHttp(200)

    is_accepted = notify_user.notify("Done.", None, http=http, environ={"MINDS_CHAT_ID": "agent-chat"})

    assert is_accepted is False
    assert http.posts == []
    assert "did not go out" in capsys.readouterr().err


@pytest.mark.parametrize("chat_id", [None, "agent-chat"])
def test_requires_a_runtime_agent_id_even_when_a_chat_id_is_set(
    chat_id: str | None, capsys: pytest.CaptureFixture[str]
) -> None:
    http = _RecordingHttp(200)
    environ = {"LATCHKEY_GATEWAY": "http://gateway.invalid", "LATCHKEY_GATEWAY_PASSWORD": "pw"}
    if chat_id is not None:
        environ["MINDS_CHAT_ID"] = chat_id

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
