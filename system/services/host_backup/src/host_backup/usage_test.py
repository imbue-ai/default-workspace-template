"""Unit tests for the backup store's usage probe (no network; the opener is stubbed)."""

import email.message
import io
import json
import urllib.error
import urllib.request

from host_backup.usage import (
    USAGE_PROBE_TIMEOUT_SECONDS,
    StoreUsage,
    read_store_usage,
    usage_probe_url,
)

_STORE_ENV = {
    "RESTIC_REPOSITORY": "rest:https://backups.example.com/user1/agent-abc/",
    "RESTIC_REST_USERNAME": "agent-abc",
    "RESTIC_REST_PASSWORD": "tok",
    "RESTIC_PASSWORD": "pw",
}


def _usage_body(**overrides: int | str) -> bytes:
    body: dict[str, int | str] = {
        "used_bytes": 10,
        "account_used_bytes": 30,
        "quota_bytes": 100,
        "hard_limit_bytes": 110,
        "mode": "active",
    }
    body.update(overrides)
    return json.dumps(body).encode()


class _RecordingOpener:
    """An `open_url` seam answering a scripted body (or raising) and recording the request."""

    def __init__(
        self, body: bytes | None = None, error: Exception | None = None
    ) -> None:
        self._body = body
        self._error = error
        self.requests: list[urllib.request.Request] = []
        self.timeouts: list[float] = []

    def __call__(
        self, request: urllib.request.Request, timeout_seconds: float
    ) -> bytes:
        self.requests.append(request)
        self.timeouts.append(timeout_seconds)
        if self._error is not None:
            raise self._error
        assert self._body is not None
        return self._body


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://x/_usage", code, "refused", email.message.Message(), io.BytesIO(b"")
    )


def test_usage_probe_url_is_under_the_rest_repository_path() -> None:
    assert (
        usage_probe_url("rest:https://backups.example.com/user1/agent-abc/")
        == "https://backups.example.com/user1/agent-abc/_usage"
    )
    assert (
        usage_probe_url("rest:https://backups.example.com/user1/agent-abc")
        == "https://backups.example.com/user1/agent-abc/_usage"
    )


def test_only_a_rest_repository_has_a_probe() -> None:
    assert usage_probe_url("s3:https://acct.r2.cloudflarestorage.com/bucket") is None
    assert usage_probe_url("local:/var/tmp/repo") is None
    assert usage_probe_url("rest:sftp://nope") is None
    assert usage_probe_url("") is None


def test_the_probe_sends_the_rest_credentials_as_basic_auth() -> None:
    opener = _RecordingOpener(_usage_body())

    usage = read_store_usage(_STORE_ENV, open_url=opener)

    assert usage == StoreUsage(
        used_bytes=10,
        account_used_bytes=30,
        quota_bytes=100,
        hard_limit_bytes=110,
        mode="active",
    )
    (request,) = opener.requests
    assert request.full_url == "https://backups.example.com/user1/agent-abc/_usage"
    # base64("agent-abc:tok")
    assert request.get_header("Authorization") == "Basic YWdlbnQtYWJjOnRvaw=="
    assert opener.timeouts == [USAGE_PROBE_TIMEOUT_SECONDS]


def test_credentials_in_the_repository_url_are_moved_into_the_header() -> None:
    opener = _RecordingOpener(_usage_body())
    env = {
        "RESTIC_REPOSITORY": "rest:https://agent-abc:tok@backups.example.com/user1/agent-abc/"
    }

    assert read_store_usage(env, open_url=opener) is not None
    (request,) = opener.requests
    assert request.full_url == "https://backups.example.com/user1/agent-abc/_usage"
    assert request.get_header("Authorization") == "Basic YWdlbnQtYWJjOnRvaw=="


def test_credentials_in_the_repository_url_leave_an_ipv6_host_intact() -> None:
    opener = _RecordingOpener(_usage_body())
    env = {"RESTIC_REPOSITORY": "rest:http://agent-abc:tok@[::1]:8787/user1/agent-abc/"}

    assert read_store_usage(env, open_url=opener) is not None
    (request,) = opener.requests
    assert request.full_url == "http://[::1]:8787/user1/agent-abc/_usage"
    assert request.get_header("Authorization") == "Basic YWdlbnQtYWJjOnRvaw=="


def test_a_non_rest_repository_is_never_asked() -> None:
    opener = _RecordingOpener(_usage_body())

    assert (
        read_store_usage({"RESTIC_REPOSITORY": "s3:https://x/b"}, open_url=opener)
        is None
    )
    assert opener.requests == []


def test_a_server_without_the_probe_means_no_quota_information() -> None:
    assert (
        read_store_usage(_STORE_ENV, open_url=_RecordingOpener(error=_http_error(404)))
        is None
    )


def test_a_refusal_or_an_unreachable_store_means_no_quota_information() -> None:
    assert (
        read_store_usage(_STORE_ENV, open_url=_RecordingOpener(error=_http_error(401)))
        is None
    )
    assert (
        read_store_usage(
            _STORE_ENV,
            open_url=_RecordingOpener(
                error=urllib.error.URLError("connection refused")
            ),
        )
        is None
    )
    assert (
        read_store_usage(_STORE_ENV, open_url=_RecordingOpener(error=TimeoutError()))
        is None
    )


def test_a_malformed_repository_address_means_no_quota_information() -> None:
    # urlsplit rejects the unclosed IPv6 bracket; the probe reports no quota
    # information rather than ending the tick, and restic reports the address.
    opener = _RecordingOpener(_usage_body())
    env = {"RESTIC_REPOSITORY": "rest:https://[::1/user1/agent-abc/"}

    assert read_store_usage(env, open_url=opener) is None
    assert opener.requests == []


def test_an_unreadable_answer_means_no_quota_information() -> None:
    assert read_store_usage(_STORE_ENV, open_url=_RecordingOpener(b"not json")) is None
    assert (
        read_store_usage(_STORE_ENV, open_url=_RecordingOpener(b'{"used_bytes": 1}'))
        is None
    )


def test_over_the_soft_limit_and_the_headroom_are_read_from_the_answer() -> None:
    under = read_store_usage(
        _STORE_ENV, open_url=_RecordingOpener(_usage_body(account_used_bytes=100))
    )
    over = read_store_usage(
        _STORE_ENV, open_url=_RecordingOpener(_usage_body(account_used_bytes=101))
    )

    assert under is not None and not under.is_over_soft_limit
    assert over is not None and over.is_over_soft_limit
    assert over.headroom_bytes == 10
