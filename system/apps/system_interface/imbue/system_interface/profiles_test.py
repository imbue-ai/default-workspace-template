import json
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

import httpx
import pytest

from imbue.system_interface.profiles import PROFILE_CACHE_TTL
from imbue.system_interface.profiles import ProfileResolver
from imbue.system_interface.profiles import UserProfile
from imbue.system_interface.profiles import parse_share_broker_url
from imbue.system_interface.profiles import read_share_broker_url
from imbue.system_interface.shell.primitives import UserId

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


def _resolver(tmp_path: Path, connector: _Connector) -> ProfileResolver:
    share_env_path = tmp_path / "share.env"
    share_env_path.write_text(
        'export SHARE_WORKSPACE_DOMAIN="w.example.test"\nexport SHARE_BROKER_URL="https://broker.example.test/"\n'
    )
    return ProfileResolver(
        cache_directory=tmp_path / "profiles",
        share_env_path=share_env_path,
        transport=httpx.MockTransport(connector.handle),
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
    resolver = _resolver(tmp_path, connector)

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


def test_resolve_asks_again_once_the_cache_entry_is_past_its_ttl(tmp_path: Path) -> None:
    connector = _Connector()
    connector.response_by_user_id["user-bob-4471"] = httpx.Response(200, json=_BOB_WIRE)
    resolver = _resolver(tmp_path, connector)
    resolver.resolve(_BOB, _T0)
    connector.response_by_user_id["user-bob-4471"] = httpx.Response(200, json={**_BOB_WIRE, "display_name": "Robert"})

    refreshed = resolver.resolve(_BOB, _T0 + PROFILE_CACHE_TTL)

    assert refreshed is not None and refreshed.display_name == "Robert"
    assert len(connector.requests) == 2


def test_a_failed_fetch_is_cached_as_a_miss_for_the_ttl(tmp_path: Path) -> None:
    connector = _Connector()
    connector.is_unreachable = True
    resolver = _resolver(tmp_path, connector)

    assert resolver.resolve(_BOB, _T0) is None
    assert resolver.resolve(_BOB, _T0 + timedelta(minutes=1)) is None
    assert len(connector.requests) == 1

    connector.is_unreachable = False
    connector.response_by_user_id["user-bob-4471"] = httpx.Response(200, json=_BOB_WIRE)
    assert resolver.resolve(_BOB, _T0 + PROFILE_CACHE_TTL) is not None
    assert len(connector.requests) == 2


def test_an_error_status_or_an_unreadable_answer_is_a_miss_not_a_crash(tmp_path: Path) -> None:
    connector = _Connector()
    resolver = _resolver(tmp_path, connector)

    assert resolver.resolve(UserId("user-unknown"), _T0) is None

    connector.response_by_user_id["user-garbled"] = httpx.Response(200, content=b"not json")
    assert resolver.resolve(UserId("user-garbled"), _T0) is None
    connector.response_by_user_id["user-shapeless"] = httpx.Response(200, json={"display_name": 7})
    assert resolver.resolve(UserId("user-shapeless"), _T0) is None


def test_a_nameless_profile_is_a_profile_with_no_name(tmp_path: Path) -> None:
    connector = _Connector()
    connector.response_by_user_id["user-bob-4471"] = httpx.Response(200, json={"user_id": "user-bob-4471"})
    resolver = _resolver(tmp_path, connector)

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
    resolver = _resolver(tmp_path, connector)
    resolver.resolve(_BOB, _T0)
    resolver.share_env_path.unlink()

    cached = resolver.resolve(_BOB, _T0 + timedelta(minutes=1))

    assert cached is not None and cached.display_name == "Bob"
    assert resolver.resolve(_BOB, _T0 + PROFILE_CACHE_TTL) is None


def test_resolve_many_answers_only_the_users_with_a_profile(tmp_path: Path) -> None:
    connector = _Connector()
    connector.response_by_user_id["user-bob-4471"] = httpx.Response(200, json=_BOB_WIRE)
    resolver = _resolver(tmp_path, connector)

    profile_by_user_id = resolver.resolve_many([_BOB, UserId("user-unknown")], _T0)

    assert set(profile_by_user_id) == {"user-bob-4471"}
    assert profile_by_user_id["user-bob-4471"].display_name == "Bob"
