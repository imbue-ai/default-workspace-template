"""Tests for the notify-user skill's script: what it posts, who it says is watching, and that every failure is
reported."""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from notify_user_skill import notify_user

_GATEWAY_ENV = {
    "LATCHKEY_GATEWAY": "http://gateway.invalid:1234/",
    "LATCHKEY_GATEWAY_PASSWORD": "pw",
    "MNGR_AGENT_ID": "agent-self",
    "MINDS_CHAT_ID": "agent-chat",
}

_CHAT_APP_URL = "http://127.0.0.1:18731"

_WATCH_WARNING = "notify-user: could not check who is watching this chat ("

# What the shell answers a push when the owner has turned notifications on nowhere.
_NO_DEVICES = (
    200,
    json.dumps({"send_id": "send-1", "status": "no_devices", "detail": "No device."}),
)
# What it answers a push it queued for the devices.
_QUEUED = (
    202,
    json.dumps({"send_id": "send-1", "status": "queued", "detail": "Queued."}),
)
_TOKEN = "the-send-token"


def _is_push(url: str) -> bool:
    return url.endswith(notify_user.WEB_PUSH_SEND_PATH)


_DESKTOP_WARNING = (
    "notify-user: could not show the notification on the workspace's desktop ("
)


def _is_to_the_app(url: str) -> bool:
    """Whether a POST goes to the Imbue Studio app through the gateway, rather than to the chat app."""
    return "/minds-api-proxy/" in url


@pytest.fixture
def environ(tmp_path: Path) -> dict[str, str]:
    """The gateway env plus an app registry naming the chat app's address."""
    return _env_with_chat_app_at(
        _GATEWAY_ENV, tmp_path / "apps.toml", f"{_CHAT_APP_URL}/"
    )


def _env_with_chat_app_at(
    base_env: dict[str, str], registry: Path, url: str
) -> dict[str, str]:
    """``base_env`` pointed at an app registry, written to ``registry``, whose chat app is at ``url``, and at a send
    token file beside it."""
    registry.write_text(f'[[apps]]\nname = "chat"\nurl = "{url}"\n')
    token_file = registry.with_name("send_token")
    token_file.write_text(f"{_TOKEN}\n")
    return {
        **base_env,
        "MINDS_APPS_FILE": str(registry),
        "MINDS_WEB_PUSH_SEND_TOKEN_FILE": str(token_file),
    }


def _watchers_answer(
    *instance_ids: str,
    owner_watched_by: tuple[str, ...] | None = None,
    is_owners_chat: bool = True,
    chat_title: str | None = "Research",
) -> tuple[int | None, str]:
    return 200, json.dumps(
        {
            "watched_by": list(instance_ids),
            "owner_watched_by": list(
                instance_ids if owner_watched_by is None else owner_watched_by
            ),
            "is_owners_chat": is_owners_chat,
            "chat_title": chat_title,
        }
    )


class _RecordingHttp(notify_user.HttpClient):
    """Records the POST to the app, the push to the shell, the chat app's desktop POST, and the watcher question, and
    answers each with a caller-supplied result."""

    def __init__(
        self,
        status: int | None,
        text: str = "",
        watchers: tuple[int | None, str] = _watchers_answer(),
        push: tuple[int | None, str] = _NO_DEVICES,
        desktop: tuple[int | None, str] = (200, '{"status": "ok"}'),
    ) -> None:
        self._status = status
        self._text = text
        self._watchers = watchers
        self._push = push
        self._desktop = desktop
        self.posts: list[tuple[str, dict, dict]] = []
        self.pushes: list[tuple[str, dict, dict, float]] = []
        self.desktop_posts: list[tuple[str, dict, float]] = []
        self.gets: list[tuple[str, float]] = []

    def post_json(
        self, url: str, payload: dict, headers: dict, timeout: float
    ) -> tuple[int | None, str]:
        if _is_push(url):
            self.pushes.append((url, payload, headers, timeout))
            return self._push
        if not _is_to_the_app(url):
            self.desktop_posts.append((url, payload, timeout))
            return self._desktop
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
        (
            (200, '{"watched_by": []}'),
            "the chat app's answer does not say whose chat this is",
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
    """The real chat app calls, and a recorded POST that the app accepts (a push finds no device)."""

    def __init__(self) -> None:
        self.posts: list[dict] = []

    def post_json(
        self, url: str, payload: dict, headers: dict, timeout: float
    ) -> tuple[int | None, str]:
        if _is_push(url):
            return _NO_DEVICES
        if not _is_to_the_app(url):
            return super().post_json(url, payload, headers, timeout)
        self.posts.append(payload)
        return 200, '{"ok": true}'


def test_a_chat_app_that_never_answers_delays_the_notification_by_the_chat_app_timeouts_only(
    silent_chat_app: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    http = _RecordingPostHttp()
    started = time.monotonic()

    is_accepted = notify_user.notify("Done.", None, http=http, environ=silent_chat_app)

    elapsed = time.monotonic() - started
    assert is_accepted is True
    assert http.posts == [{"message": "Done."}]
    assert 3.5 < elapsed < 8.0
    stderr = capsys.readouterr().err
    assert _WATCH_WARNING in stderr
    assert _DESKTOP_WARNING in stderr


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
    stderr = capsys.readouterr().err
    assert f"{_DESKTOP_WARNING}the chat app did not answer: " in stderr
    assert f"{_WATCH_WARNING}the chat app did not answer: " in stderr


def test_shows_the_notification_on_the_workspaces_desktop_through_the_chat_app(
    environ: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    http = _RecordingHttp(200)

    assert (
        notify_user.notify(
            "  All tests passed. ", " Test run ", http=http, environ=environ
        )
        is True
    )

    assert http.desktop_posts == [
        (
            f"{_CHAT_APP_URL}/api/chats/agent-chat/notification",
            {"message": "All tests passed.", "title": "Test run"},
            2.0,
        )
    ]
    assert _DESKTOP_WARNING not in capsys.readouterr().err


@pytest.mark.parametrize(
    ("desktop", "expected_reason"),
    [
        (
            (None, "connection refused"),
            "the chat app did not answer: connection refused",
        ),
        ((404, '{"detail": "Nothing is served"}'), "the chat app answered 404"),
    ],
)
def test_a_desktop_the_chat_app_cannot_show_it_on_is_a_warning_and_the_notification_still_goes_out(
    desktop: tuple[int | None, str],
    expected_reason: str,
    environ: dict[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    http = _RecordingHttp(200, desktop=desktop)

    is_accepted = notify_user.notify("Done.", None, http=http, environ=environ)

    assert is_accepted is True
    assert len(http.posts) == 1
    assert f"{_DESKTOP_WARNING}{expected_reason})" in capsys.readouterr().err


def test_shows_the_notification_on_the_desktop_even_when_the_app_cannot_be_reached(
    environ: dict[str, str],
) -> None:
    http = _RecordingHttp(200)
    del environ["LATCHKEY_GATEWAY"]

    is_accepted = notify_user.notify("Done.", None, http=http, environ=environ)

    assert is_accepted is False
    assert http.posts == []
    assert [url for url, _payload, _timeout in http.desktop_posts] == [
        f"{_CHAT_APP_URL}/api/chats/agent-chat/notification"
    ]


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
    assert http.posts == [] and http.desktop_posts == []
    stderr = capsys.readouterr().err
    assert "did not go out" in stderr
    assert f"{_DESKTOP_WARNING}MINDS_CHAT_ID is not set)" in stderr


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
    # Nothing listens on either address.
    monkeypatch.setenv("LATCHKEY_GATEWAY", "http://127.0.0.1:1")
    monkeypatch.setenv("MINDS_WORKSPACE_SERVER_URL", "http://127.0.0.1:1")

    assert notify_user.main(["Done."]) == 1


def test_asks_the_shell_to_push_naming_the_chat_with_the_send_token(
    environ: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    http = _RecordingHttp(200, push=_QUEUED)

    assert (
        notify_user.notify("The build is green.", "CI", http=http, environ=environ)
        is True
    )

    ((url, payload, headers, timeout),) = http.pushes
    assert url == "http://127.0.0.1:8000/api/web-push/send"
    assert payload == {
        "chat_id": "agent-chat",
        "chat_title": "Research",
        "title": "CI",
        "message": "The build is green.",
    }
    assert headers["X-Web-Push-Token"] == _TOKEN
    assert timeout == 5.0
    assert "also queued for your devices" in capsys.readouterr().err


def test_an_untitled_chat_and_overlong_words_are_cut_to_what_the_shell_takes() -> None:
    watch = notify_user.ChatWatch(
        watched_by=[], owner_watched_by=[], is_owners_chat=True, chat_title=None
    )

    request = notify_user.push_request("x" * 5000, "t" * 300, watch, "agent-chat")

    assert request["chat_title"] == "A chat"
    assert len(request["message"]) == 4000 and request["message"].endswith("...")
    assert len(request["title"]) == 200


def test_a_message_whose_bytes_are_not_utf8_still_makes_a_push_request() -> None:
    # What argv holds for a message whose bytes are not UTF-8: lone surrogates.
    message = b"caf\xe9 is open".decode("utf-8", "surrogateescape")
    watch = notify_user.ChatWatch(
        watched_by=[], owner_watched_by=[], is_owners_chat=True, chat_title="Research"
    )

    request = notify_user.push_request(message, None, watch, "agent-chat")

    assert request["message"] == "caf? is open"
    assert "title" not in request
    json.dumps(request).encode("utf-8")


def test_does_not_push_while_one_of_the_owners_pages_is_watching_the_chat(
    environ: dict[str, str],
) -> None:
    http = _RecordingHttp(200, watchers=_watchers_answer("page-a"), push=_QUEUED)

    notify_user.notify("Done.", None, http=http, environ=environ)

    assert http.pushes == []


def test_a_visitor_watching_the_chat_does_not_hold_back_the_owners_push(
    environ: dict[str, str],
) -> None:
    http = _RecordingHttp(
        200,
        watchers=_watchers_answer("visitor-page", owner_watched_by=()),
        push=_QUEUED,
    )

    notify_user.notify("Done.", None, http=http, environ=environ)

    assert len(http.pushes) == 1
    ((_url, payload, _headers),) = http.posts
    assert payload["watched_by"] == ["visitor-page"]


def test_a_chat_someone_else_wrote_in_pushes_nothing(
    environ: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    http = _RecordingHttp(
        200, watchers=_watchers_answer(is_owners_chat=False), push=_QUEUED
    )

    assert notify_user.notify("Done.", None, http=http, environ=environ) is True

    assert http.pushes == []
    assert "someone other than the owner wrote in this chat" in capsys.readouterr().err


def test_a_chat_whose_owner_cannot_be_told_pushes_nothing(
    environ: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    http = _RecordingHttp(200, watchers=(None, "timed out"), push=_QUEUED)

    assert notify_user.notify("Done.", None, http=http, environ=environ) is True

    assert http.pushes == []
    assert "whose chat this is could not be told" in capsys.readouterr().err


def test_an_unreadable_send_token_pushes_nothing(
    environ: dict[str, str], capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    http = _RecordingHttp(200, push=_QUEUED)
    environ = {**environ, "MINDS_WEB_PUSH_SEND_TOKEN_FILE": str(tmp_path / "absent")}

    notify_user.notify("Done.", None, http=http, environ=environ)

    assert http.pushes == []
    assert "send token is unreadable" in capsys.readouterr().err


def test_a_device_push_the_shell_queued_counts_when_the_app_did_not(
    environ: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    http = _RecordingHttp(None, "connection refused", push=_QUEUED)

    assert notify_user.notify("Done.", None, http=http, environ=environ) is True

    stderr = capsys.readouterr().err
    assert "the Imbue Studio app was unreachable" in stderr
    assert "queued for your devices, though not taken by the Imbue Studio app" in stderr
    assert "did not go out" not in stderr


@pytest.mark.parametrize(
    ("push", "expected_fragment"),
    [
        (
            (None, "connection refused"),
            "could not push the notification to your devices (the workspace was unreachable: connection",
        ),
        (
            (403, '{"detail": "no valid send token"}'),
            "could not push the notification to your devices (the workspace answered 403: no valid send token",
        ),
        (
            (
                429,
                json.dumps(
                    {
                        "send_id": "s",
                        "status": "limited",
                        "detail": "Too many; not sent.",
                    }
                ),
            ),
            "not pushed to your devices: Too many; not sent.",
        ),
    ],
)
def test_a_push_the_shell_did_not_queue_is_reported_and_leaves_the_outcome_to_the_app(
    push: tuple[int | None, str],
    expected_fragment: str,
    environ: dict[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    http = _RecordingHttp(200, push=push)

    assert notify_user.notify("Done.", None, http=http, environ=environ) is True

    assert expected_fragment in capsys.readouterr().err


def test_no_registered_device_says_nothing(
    environ: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    http = _RecordingHttp(200)

    notify_user.notify("Done.", None, http=http, environ=environ)

    assert len(http.pushes) == 1
    assert "device" not in capsys.readouterr().err


class _RealPushHttp(_RecordingHttp):
    """The app accepts and no page watches the chat, while the push to the shell goes out for real."""

    def __init__(self) -> None:
        super().__init__(200)

    def post_json(
        self, url: str, payload: dict, headers: dict, timeout: float
    ) -> tuple[int | None, str]:
        if _is_push(url):
            return notify_user.HttpClient.post_json(
                self, url, payload, headers, timeout
            )
        return super().post_json(url, payload, headers, timeout)


def test_a_malformed_shell_address_is_an_unreachable_push_not_a_crash(
    environ: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    # A typo in the port, which urllib reports as a ValueError rather than a URLError.
    environ = {**environ, "MINDS_WORKSPACE_SERVER_URL": "http://127.0.0.1:80o0"}

    assert (
        notify_user.notify("Done.", None, http=_RealPushHttp(), environ=environ) is True
    )

    assert (
        "could not push the notification to your devices (the workspace was unreachable: nonnumeric port"
        in capsys.readouterr().err
    )
