import json
import threading
from collections.abc import Callable
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

import httpx
import pytest
from workspace_layout.primitives import UserId

from imbue.mngr.utils.polling import wait_for
from imbue.system_interface.profiles import PROFILE_CACHE_TTL
from imbue.system_interface.profiles import ProfileResolver
from imbue.system_interface.profiles import UserProfile
from imbue.system_interface.profiles import parse_share_broker_url
from imbue.system_interface.profiles import picture_version_of
from imbue.system_interface.profiles import read_share_broker_url

_T0 = datetime(2026, 9, 19, 10, 0, 0, tzinfo=timezone.utc)
_BOB = UserId("user-bob-4471")
_BOB_WIRE = {"user_id": "user-bob-4471", "display_name": "Bob", "profile_picture_url": "https://a/bob.png"}


class _Connector:
    """A stand-in broker: answers every profile request from a table and counts what it was asked."""

    def __init__(self) -> None:
        self.response_by_user_id: dict[str, httpx.Response] = {}
        self.requests: list[httpx.Request] = []
        self.is_unreachable = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.is_unreachable:
            raise httpx.ConnectError("connection refused", request=request)
        user_id = request.url.path.split("/")[2]
        return self.response_by_user_id.get(user_id, httpx.Response(404, json={"detail": "no such user"}))


def _resolver(tmp_path: Path, handle: Callable[[httpx.Request], httpx.Response]) -> ProfileResolver:
    """A resolver whose transport answers every request (profiles, and pictures too) through ``handle``."""
    share_env_path = tmp_path / "share.env"
    share_env_path.write_text(
        'export SHARE_WORKSPACE_DOMAIN="w.example.test"\nexport SHARE_BROKER_URL="https://broker.example.test/"\n'
    )
    return ProfileResolver(
        cache_directory=tmp_path / "profiles",
        share_env_path=share_env_path,
        transport=httpx.MockTransport(handle),
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('export SHARE_BROKER_URL="https://broker.example.test/"\n', "https://broker.example.test"),
        ("SHARE_BROKER_URL=https://broker.example.test\n", "https://broker.example.test"),
        ("export SHARE_BROKER_URL=''\n", None),
        ('export SHARE_CONNECTOR_URL="https://c.example.test"\n', None),
        ("", None),
    ],
)
def test_parse_share_broker_url_reads_the_env_line_with_or_without_export_and_quotes(
    text: str, expected: str | None
) -> None:
    assert parse_share_broker_url(text) == expected


def test_read_share_broker_url_treats_an_absent_file_as_no_share(tmp_path: Path) -> None:
    assert read_share_broker_url(tmp_path / "share.env") is None


def test_resolve_fetches_the_profile_from_the_broker_and_caches_it(tmp_path: Path) -> None:
    connector = _Connector()
    connector.response_by_user_id["user-bob-4471"] = httpx.Response(200, json={**_BOB_WIRE, "a_newer_field": 1})
    resolver = _resolver(tmp_path, connector.handle)

    first = resolver.resolve(_BOB, _T0)
    second = resolver.resolve(_BOB, _T0 + PROFILE_CACHE_TTL - timedelta(seconds=1))

    assert (
        first
        == second
        == UserProfile(user_id="user-bob-4471", display_name="Bob", profile_picture_url="https://a/bob.png")
    )
    (request,) = connector.requests
    assert str(request.url) == "https://broker.example.test/users/user-bob-4471/profile"
    cached = json.loads((tmp_path / "profiles" / "user-bob-4471.json").read_text())
    assert cached["display_name"] == "Bob" and cached["is_fetch_failed"] is False


def test_resolve_reads_an_empty_picture_url_as_no_picture(tmp_path: Path) -> None:
    connector = _Connector()
    connector.response_by_user_id["user-bob-4471"] = httpx.Response(200, json={**_BOB_WIRE, "profile_picture_url": ""})
    resolver = _resolver(tmp_path, connector.handle)

    assert resolver.resolve(_BOB, _T0) == UserProfile(
        user_id="user-bob-4471", display_name="Bob", profile_picture_url=None
    )


def test_resolve_asks_again_once_the_cache_entry_is_past_its_ttl(tmp_path: Path) -> None:
    connector = _Connector()
    connector.response_by_user_id["user-bob-4471"] = httpx.Response(200, json=_BOB_WIRE)
    resolver = _resolver(tmp_path, connector.handle)
    resolver.resolve(_BOB, _T0)
    connector.response_by_user_id["user-bob-4471"] = httpx.Response(200, json={**_BOB_WIRE, "display_name": "Robert"})

    refreshed = resolver.resolve(_BOB, _T0 + PROFILE_CACHE_TTL)

    assert refreshed is not None and refreshed.display_name == "Robert"
    assert len(connector.requests) == 2


def test_a_failed_fetch_is_cached_as_a_miss_for_the_ttl(tmp_path: Path) -> None:
    connector = _Connector()
    connector.is_unreachable = True
    resolver = _resolver(tmp_path, connector.handle)

    assert resolver.resolve(_BOB, _T0) is None
    assert resolver.resolve(_BOB, _T0 + timedelta(minutes=1)) is None
    assert len(connector.requests) == 1

    connector.is_unreachable = False
    connector.response_by_user_id["user-bob-4471"] = httpx.Response(200, json=_BOB_WIRE)
    assert resolver.resolve(_BOB, _T0 + PROFILE_CACHE_TTL) is not None
    assert len(connector.requests) == 2


def test_an_error_status_or_an_unreadable_answer_is_a_miss_not_a_crash(tmp_path: Path) -> None:
    connector = _Connector()
    resolver = _resolver(tmp_path, connector.handle)

    assert resolver.resolve(UserId("user-unknown"), _T0) is None

    connector.response_by_user_id["user-garbled"] = httpx.Response(200, content=b"not json")
    assert resolver.resolve(UserId("user-garbled"), _T0) is None
    connector.response_by_user_id["user-shapeless"] = httpx.Response(200, json={"display_name": 7})
    assert resolver.resolve(UserId("user-shapeless"), _T0) is None


def test_a_nameless_profile_is_a_profile_with_no_name(tmp_path: Path) -> None:
    connector = _Connector()
    connector.response_by_user_id["user-bob-4471"] = httpx.Response(200, json={"user_id": "user-bob-4471"})
    resolver = _resolver(tmp_path, connector.handle)

    assert resolver.resolve(_BOB, _T0) == UserProfile(
        user_id="user-bob-4471", display_name=None, profile_picture_url=None
    )


def test_nothing_is_fetched_while_the_workspace_is_not_shared(tmp_path: Path) -> None:
    connector = _Connector()
    resolver = ProfileResolver(
        cache_directory=tmp_path / "profiles",
        share_env_path=tmp_path / "absent-share.env",
        transport=httpx.MockTransport(connector.handle),
    )

    assert resolver.resolve(_BOB, _T0) is None
    assert connector.requests == []
    assert not (tmp_path / "profiles").exists()


def test_a_fresh_cache_entry_answers_even_after_the_share_ended(tmp_path: Path) -> None:
    connector = _Connector()
    connector.response_by_user_id["user-bob-4471"] = httpx.Response(200, json=_BOB_WIRE)
    resolver = _resolver(tmp_path, connector.handle)
    resolver.resolve(_BOB, _T0)
    resolver.share_env_path.unlink()

    cached = resolver.resolve(_BOB, _T0 + timedelta(minutes=1))

    assert cached is not None and cached.display_name == "Bob"
    assert resolver.resolve(_BOB, _T0 + PROFILE_CACHE_TTL) is None


def _refreshed_listener(resolver: ProfileResolver) -> threading.Event:
    """An event set each time the resolver's background fetch changes a profile."""
    refreshed = threading.Event()
    resolver.add_refresh_listener(refreshed.set)
    return refreshed


def test_resolve_many_never_waits_on_the_connector_and_answers_once_the_background_fetch_lands(
    tmp_path: Path,
) -> None:
    """The roster and the presence listing are built on the request path, so a slow connector must not hold
    them: the first answer has no profile, the fetch runs behind it, and the listeners hear when it changed one."""
    connector = _Connector()
    connector.response_by_user_id["user-bob-4471"] = httpx.Response(200, json=_BOB_WIRE)
    is_released = threading.Event()

    def slow_connector(request: httpx.Request) -> httpx.Response:
        is_released.wait(timeout=10)
        return connector.handle(request)

    resolver = _resolver(tmp_path, slow_connector)
    refreshed = _refreshed_listener(resolver)

    assert resolver.resolve_many([_BOB, UserId("user-unknown")], _T0) == {}

    is_released.set()
    assert refreshed.wait(timeout=10)
    profile_by_user_id = resolver.resolve_many([_BOB, UserId("user-unknown")], _T0)
    assert set(profile_by_user_id) == {"user-bob-4471"}
    assert profile_by_user_id["user-bob-4471"].display_name == "Bob"
    assert sorted(request.url.path for request in connector.requests) == [
        "/users/user-bob-4471/profile",
        "/users/user-unknown/profile",
    ]


def test_resolve_many_answers_a_stale_profile_at_once_and_replaces_it_in_the_background(tmp_path: Path) -> None:
    connector = _Connector()
    connector.response_by_user_id["user-bob-4471"] = httpx.Response(200, json=_BOB_WIRE)
    resolver = _resolver(tmp_path, connector.handle)
    resolver.resolve(_BOB, _T0)
    connector.response_by_user_id["user-bob-4471"] = httpx.Response(200, json={**_BOB_WIRE, "display_name": "Robert"})
    refreshed = _refreshed_listener(resolver)

    stale = resolver.resolve_many([_BOB], _T0 + PROFILE_CACHE_TTL)

    assert stale["user-bob-4471"].display_name == "Bob"
    assert refreshed.wait(timeout=10)
    assert resolver.resolve_many([_BOB], _T0 + PROFILE_CACHE_TTL)["user-bob-4471"].display_name == "Robert"


class _UnexpectedConnectorError(Exception):
    """What no fetch path expects, so it ends the refresh thread that met it."""


# The raised error ends that refresh thread, which pytest reports; that is the case under test.
@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_resolve_many_fetches_again_after_a_background_fetch_raised(tmp_path: Path) -> None:
    connector = _Connector()
    connector.response_by_user_id["user-bob-4471"] = httpx.Response(200, json=_BOB_WIRE)
    raised: list[httpx.Request] = []

    def failing_once(request: httpx.Request) -> httpx.Response:
        if not raised:
            raised.append(request)
            raise _UnexpectedConnectorError("the connector answered something no one expected")
        return connector.handle(request)

    resolver = _resolver(tmp_path, failing_once)
    refreshed = _refreshed_listener(resolver)

    resolver.resolve_many([_BOB], _T0)

    wait_for(
        lambda: bool(resolver.resolve_many([_BOB], _T0)),
        timeout=10.0,
        poll_interval=0.01,
        error_message="the refresh never ran again after a fetch raised",
    )
    assert refreshed.is_set()
    assert len(raised) == 1


def test_resolve_many_does_not_serve_a_stale_profile_once_the_workspace_stops_sharing(tmp_path: Path) -> None:
    connector = _Connector()
    connector.response_by_user_id["user-bob-4471"] = httpx.Response(200, json=_BOB_WIRE)
    resolver = _resolver(tmp_path, connector.handle)
    resolver.resolve(_BOB, _T0)

    assert resolver.resolve_many([_BOB], _T0 + PROFILE_CACHE_TTL)["user-bob-4471"].display_name == "Bob"
    resolver.share_env_path.unlink()

    assert resolver.resolve_many([_BOB], _T0 + PROFILE_CACHE_TTL) == {}
    assert resolver.resolve_many([_BOB], _T0)["user-bob-4471"].display_name == "Bob"


def test_a_malformed_broker_url_is_a_failed_fetch(tmp_path: Path) -> None:
    connector = _Connector()
    resolver = _resolver(tmp_path, connector.handle)
    # httpx refuses a non-numeric port before any request is made.
    resolver.share_env_path.write_text('export SHARE_BROKER_URL="https://broker.example.test:notaport"\n')

    assert resolver.resolve(_BOB, _T0) is None
    assert connector.requests == []
    cached = json.loads((tmp_path / "profiles" / "user-bob-4471.json").read_text())
    assert cached["is_fetch_failed"] is True


def test_resolve_picture_fetches_the_bytes_once_and_again_only_for_a_new_url(tmp_path: Path) -> None:
    """The picture is fetched from the profile's URL and kept beside the profile; the cached bytes answer while the
    profile names the same URL, and a profile naming another is fetched afresh (the multiplayer spec, 4.2)."""
    connector = _Connector()
    connector.response_by_user_id["user-bob-4471"] = httpx.Response(200, json=_BOB_WIRE)
    picture_requests: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/users/"):
            return connector.handle(request)
        picture_requests.append(str(request.url))
        return httpx.Response(200, content=b"PNG:" + request.url.path.encode(), headers={"Content-Type": "image/png"})

    resolver = _resolver(tmp_path, handle)
    first = resolver.resolve_picture(UserId("user-bob-4471"), _T0)
    again = resolver.resolve_picture(UserId("user-bob-4471"), _T0 + timedelta(seconds=1))

    assert first is not None and (first.media_type, first.content, first.version) == (
        "image/png",
        b"PNG:/bob.png",
        picture_version_of("https://a/bob.png"),
    )
    assert again == first
    assert picture_requests == [_BOB_WIRE["profile_picture_url"]]
    assert (tmp_path / "profiles" / "user-bob-4471.picture.json").exists()

    # A newer picture is a new URL on the profile: fetched afresh once the profile is read again.
    connector.response_by_user_id["user-bob-4471"] = httpx.Response(
        200, json={**_BOB_WIRE, "profile_picture_url": "https://pictures.example/bob/c"}
    )
    newer = resolver.resolve_picture(UserId("user-bob-4471"), _T0 + PROFILE_CACHE_TTL + timedelta(seconds=1))
    assert newer is not None and (newer.content, newer.version) == (
        b"PNG:/bob/c",
        picture_version_of("https://pictures.example/bob/c"),
    )
    assert picture_requests == [_BOB_WIRE["profile_picture_url"], "https://pictures.example/bob/c"]


def test_resolve_picture_answers_none_for_no_picture_a_failed_fetch_or_a_non_image(tmp_path: Path) -> None:
    connector = _Connector()
    connector.response_by_user_id["user-bob-4471"] = httpx.Response(
        200, json={**_BOB_WIRE, "profile_picture_url": None}
    )
    connector.response_by_user_id["user-amy-0001"] = httpx.Response(
        200,
        json={
            "user_id": "user-amy-0001",
            "display_name": "Amy",
            "profile_picture_url": "https://pictures.example/amy",
        },
    )
    connector.response_by_user_id["user-cal-0002"] = httpx.Response(
        200,
        json={
            "user_id": "user-cal-0002",
            "display_name": "Cal",
            "profile_picture_url": "https://pictures.example/cal",
        },
    )

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/users/"):
            return connector.handle(request)
        if request.url.path.endswith("/amy"):
            return httpx.Response(404, text="gone")
        return httpx.Response(200, content=b"<html>", headers={"Content-Type": "text/html"})

    resolver = _resolver(tmp_path, handle)
    assert resolver.resolve_picture(UserId("user-bob-4471"), _T0) is None
    assert resolver.resolve_picture(UserId("user-amy-0001"), _T0) is None
    assert resolver.resolve_picture(UserId("user-cal-0002"), _T0) is None
    assert not (tmp_path / "profiles" / "user-bob-4471.picture.json").exists()
    assert json.loads((tmp_path / "profiles" / "user-amy-0001.picture.json").read_text())["is_fetch_failed"] is True


def test_a_failed_picture_fetch_is_remembered_for_the_ttl_and_a_new_url_asked_at_once(tmp_path: Path) -> None:
    """A dead picture host costs one fetch per person per TTL, as a dead connector does for the profile; a profile
    that comes to name another URL is asked for it without waiting out the failure."""
    connector = _Connector()
    connector.response_by_user_id["user-bob-4471"] = httpx.Response(200, json=_BOB_WIRE)
    picture_requests: list[str] = []
    picture_status = 503

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/users/"):
            return connector.handle(request)
        picture_requests.append(str(request.url))
        return httpx.Response(picture_status, content=b"PNG", headers={"Content-Type": "image/png"})

    resolver = _resolver(tmp_path, handle)
    assert resolver.resolve_picture(UserId("user-bob-4471"), _T0) is None
    assert resolver.resolve_picture(UserId("user-bob-4471"), _T0 + PROFILE_CACHE_TTL - timedelta(seconds=1)) is None
    assert picture_requests == ["https://a/bob.png"]

    # Past the TTL the URL is asked again, and a picture that is there now is kept.
    picture_status = 200
    recovered = resolver.resolve_picture(UserId("user-bob-4471"), _T0 + PROFILE_CACHE_TTL)
    assert recovered is not None and recovered.content == b"PNG"
    assert picture_requests == ["https://a/bob.png", "https://a/bob.png"]

    # A failure under one URL says nothing about another: a profile read before the picture failed comes to name a
    # new URL, which is asked at once although the failure is fresh.
    later = _T0 + 2 * PROFILE_CACHE_TTL
    connector.response_by_user_id["user-bob-4471"] = httpx.Response(
        200, json={**_BOB_WIRE, "profile_picture_url": "https://a/bob-2.png"}
    )
    assert resolver.resolve(UserId("user-bob-4471"), later) is not None
    picture_status = 503
    assert resolver.resolve_picture(UserId("user-bob-4471"), later + PROFILE_CACHE_TTL - timedelta(minutes=1)) is None
    connector.response_by_user_id["user-bob-4471"] = httpx.Response(
        200, json={**_BOB_WIRE, "profile_picture_url": "https://a/bob-3.png"}
    )
    picture_status = 200
    assert resolver.resolve_picture(UserId("user-bob-4471"), later + PROFILE_CACHE_TTL) is not None
    assert picture_requests[2:] == ["https://a/bob-2.png", "https://a/bob-3.png"]


def test_picture_version_of_follows_the_url() -> None:
    assert picture_version_of("https://a/b") == picture_version_of("https://a/b")
    assert picture_version_of("https://a/b") != picture_version_of("https://a/c")
    assert len(picture_version_of("https://a/b")) == 16
