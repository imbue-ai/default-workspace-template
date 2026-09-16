import urllib.error
from email.message import Message
from io import BytesIO

import pytest

from imbue.remote_service_connector.errors import WebChannelManifestError
from imbue.remote_service_connector.web_template_channel import UPDATE_FEED_BASE_URL_ENV_VAR
from imbue.remote_service_connector.web_template_channel import channel_web_template_ref
from imbue.remote_service_connector.web_template_channel import normalize_web_channel
from imbue.remote_service_connector.web_template_channel import parse_web_channel_manifest
from imbue.remote_service_connector.web_template_channel import read_web_template_ref

_FEED = "https://updates.example.com"


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(f"{_FEED}/stable-web.json", code, "error", Message(), BytesIO(b""))


@pytest.mark.parametrize(
    ("requested", "expected"),
    [(None, "stable"), ("", "stable"), ("alpha", "alpha"), (" Beta ", "beta"), ("nightly", "stable")],
)
def test_normalize_web_channel_defaults_unknown_and_empty_values_to_stable(
    requested: str | None, expected: str
) -> None:
    assert normalize_web_channel(requested) == expected


def test_parse_web_channel_manifest_reads_the_template_tag() -> None:
    assert parse_web_channel_manifest('{"version": "0.6.1", "templateRef": "minds-v0.6.1"}') == "minds-v0.6.1"


@pytest.mark.parametrize(
    "manifest_text",
    [
        '{"version": "0.6.1"}',
        '{"templateRef": "main"}',
        '{"templateRef": ["a", "list"]}',
        '["not", "an", "object"]',
        '{"templateRef": "minds-v0.6.1"',
    ],
)
def test_parse_web_channel_manifest_refuses_anything_but_a_release_tag(manifest_text: str) -> None:
    with pytest.raises(WebChannelManifestError):
        parse_web_channel_manifest(manifest_text)


def test_read_web_template_ref_fetches_the_channel_file_by_name() -> None:
    fetched: list[tuple[str, str]] = []

    def fetch(feed_base_url: str, channel_file: str) -> str:
        fetched.append((feed_base_url, channel_file))
        return '{"templateRef": "minds-v0.6.1"}'

    assert read_web_template_ref("alpha", _FEED, fetch) == "minds-v0.6.1"
    assert fetched == [(_FEED, "alpha-web.json")]


@pytest.mark.parametrize(
    "failure",
    [_http_error(404), _http_error(500), OSError("connection reset"), UnicodeDecodeError("utf-8", b"", 0, 1, "bad")],
)
def test_read_web_template_ref_is_none_when_the_feed_cannot_be_read(failure: Exception) -> None:
    def fetch(feed_base_url: str, channel_file: str) -> str:
        raise failure

    assert read_web_template_ref("stable", _FEED, fetch) is None


def test_read_web_template_ref_is_none_for_a_malformed_manifest() -> None:
    assert (
        read_web_template_ref("stable", _FEED, lambda feed_base_url, channel_file: '{"templateRef": "main"}') is None
    )


def test_channel_web_template_ref_is_none_without_a_configured_feed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(UPDATE_FEED_BASE_URL_ENV_VAR, raising=False)
    assert channel_web_template_ref("stable") is None
    monkeypatch.setenv(UPDATE_FEED_BASE_URL_ENV_VAR, "   ")
    assert channel_web_template_ref("stable") is None
