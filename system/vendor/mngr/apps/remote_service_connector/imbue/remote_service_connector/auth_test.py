from collections.abc import Callable
from typing import Any

import psycopg2
import pytest
from fastapi import HTTPException
from supertokens_python.exceptions import GeneralError as SuperTokensGeneralError
from supertokens_python.recipe.session.exceptions import SuperTokensSessionError

from imbue.remote_service_connector.auth import UserAuth
from imbue.remote_service_connector.auth import _authenticate_supertokens
from imbue.remote_service_connector.auth import clear_paid_status_cache
from imbue.remote_service_connector.auth import default_email_getter
from imbue.remote_service_connector.auth import is_email_paid
from imbue.remote_service_connector.auth import is_email_paid_in_db
from imbue.remote_service_connector.auth import require_ally_eligible
from imbue.remote_service_connector.testing import _ADMIN_KEY_TEST_VALUE
from imbue.remote_service_connector.testing import _FakeLoginMethod
from imbue.remote_service_connector.testing import _admin_key_headers
from imbue.remote_service_connector.testing import _make_paid_crud_test_client
from imbue.remote_service_connector.testing import _make_pool_test_client
from imbue.remote_service_connector.testing import make_fake_pool_backend


class _FakeSession:
    """Minimal mock for supertokens SessionContainer."""

    def __init__(self, user_id: str, email_verified: bool = True) -> None:
        self._user_id = user_id
        self._email_verified = email_verified

    def get_user_id(self) -> str:
        return self._user_id

    def get_access_token_payload(self) -> dict[str, object]:
        return {"st-ev": {"v": self._email_verified, "t": 0}}


def test_authenticate_supertokens_returns_user_auth_with_user_id_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid token returns UserAuth whose user_id_prefix is the first 16 hex chars of the user ID."""
    user_id = "a1b2c3d4-e5f6-7890-abcd-1234567890ab"
    monkeypatch.setenv("SUPERTOKENS_CONNECTION_URI", "https://st.example.com")
    result = _authenticate_supertokens(
        "valid-token",
        session_getter=lambda **kwargs: _FakeSession(user_id, email_verified=True),
        email_getter=lambda _user_id: "alice@example.com",
    )
    assert isinstance(result, UserAuth)
    assert result.user_id_prefix == "a1b2c3d4e5f67890"
    assert result.email == "alice@example.com"


def test_authenticate_supertokens_raises_401_when_no_verified_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the live lookup finds no verified email, auth is rejected with 401.

    ``email_getter`` (``default_email_getter`` in production) returns None both
    when the user has no email and when their only emails are unverified; either
    way the caller has proven no verified identity, so the guard denies access.
    """
    user_id = "a1b2c3d4-e5f6-7890-abcd-1234567890ab"
    monkeypatch.setenv("SUPERTOKENS_CONNECTION_URI", "https://st.example.com")
    with pytest.raises(HTTPException) as exc_info:
        _authenticate_supertokens(
            "valid-token",
            session_getter=lambda **kwargs: _FakeSession(user_id, email_verified=True),
            email_getter=lambda _user_id: None,
        )
    assert exc_info.value.status_code == 401
    assert "verified" in exc_info.value.detail


def test_authenticate_supertokens_ignores_stale_unverified_token_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A token minted while unverified still authenticates once the core reports the email verified.

    The access token carries a cached ``email_verified=False`` claim, but the
    live ``email_getter`` lookup returns a verified email (e.g. the user was
    just added to the paid list and auto-verified). The guard must trust the
    live result, not the stale token claim, so the request succeeds without the
    user having to refresh their token first.
    """
    user_id = "a1b2c3d4-e5f6-7890-abcd-1234567890ab"
    monkeypatch.setenv("SUPERTOKENS_CONNECTION_URI", "https://st.example.com")
    result = _authenticate_supertokens(
        "stale-token",
        session_getter=lambda **kwargs: _FakeSession(user_id, email_verified=False),
        email_getter=lambda _user_id: "alice@example.com",
    )
    assert isinstance(result, UserAuth)
    assert result.email == "alice@example.com"


def test_authenticate_supertokens_raises_401_when_connection_uri_not_set() -> None:
    """When SUPERTOKENS_CONNECTION_URI is absent, raises 401."""
    with pytest.raises(HTTPException) as exc_info:
        _authenticate_supertokens(
            "any-token",
            session_getter=lambda **kwargs: _FakeSession("ignored"),
            email_getter=lambda _user_id: None,
        )
    assert exc_info.value.status_code == 401
    assert "not configured" in exc_info.value.detail


def test_authenticate_supertokens_raises_401_when_session_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the session getter returns None, raises 401."""
    monkeypatch.setenv("SUPERTOKENS_CONNECTION_URI", "https://st.example.com")
    with pytest.raises(HTTPException) as exc_info:
        _authenticate_supertokens(
            "expired-token",
            session_getter=lambda **kwargs: None,
        )
    assert exc_info.value.status_code == 401


def test_authenticate_supertokens_raises_401_on_session_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the session getter raises SuperTokensSessionError, raises 401."""
    monkeypatch.setenv("SUPERTOKENS_CONNECTION_URI", "https://st.example.com")

    def _raise(**kwargs: object) -> None:
        raise SuperTokensSessionError("bad session")

    with pytest.raises(HTTPException) as exc_info:
        _authenticate_supertokens("bad-token", session_getter=_raise)
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Invalid token"


def test_authenticate_supertokens_raises_401_on_general_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the SDK is not initialized (GeneralError), raises 401 instead of 500."""
    monkeypatch.setenv("SUPERTOKENS_CONNECTION_URI", "https://st.example.com")

    def _raise(**kwargs: object) -> None:
        raise SuperTokensGeneralError("Initialisation not done")

    with pytest.raises(HTTPException) as exc_info:
        _authenticate_supertokens("bad-token", session_getter=_raise)
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Invalid token"


class _FakeStUser:
    """Stand-in for a SuperTokens User -- only the ``login_methods`` attribute is used."""

    def __init__(self, login_methods: list[_FakeLoginMethod]) -> None:
        self.login_methods = login_methods


def test_default_email_getter_returns_first_verified_non_empty_email() -> None:
    """The first login method with both a non-empty email and ``verified=True`` is returned."""
    user = _FakeStUser([_FakeLoginMethod(None), _FakeLoginMethod(""), _FakeLoginMethod("alice@example.com")])
    assert default_email_getter("user-123", user_getter=lambda _user_id: user) == "alice@example.com"


def test_default_email_getter_skips_unverified_emails() -> None:
    """Unverified login methods are skipped; the first *verified* email is returned.

    A user with both an unverified third-party login (``evil@gmail.com``) and a verified
    emailpassword login (``alice@imbue.com``) must surface ``alice@imbue.com``, since the
    paid-feature gate authorizes by domain ownership and only verified emails prove that.
    """
    user = _FakeStUser(
        [
            _FakeLoginMethod("evil@gmail.com", verified=False),
            _FakeLoginMethod("alice@imbue.com", verified=True),
        ]
    )
    assert default_email_getter("user-123", user_getter=lambda _user_id: user) == "alice@imbue.com"


def test_default_email_getter_returns_none_when_only_unverified_emails() -> None:
    """When every login method is unverified, returns None even if emails are present."""
    user = _FakeStUser(
        [
            _FakeLoginMethod("evil@gmail.com", verified=False),
            _FakeLoginMethod("other@gmail.com", verified=False),
        ]
    )
    assert default_email_getter("user-123", user_getter=lambda _user_id: user) is None


def test_default_email_getter_returns_none_when_no_login_method_has_email() -> None:
    """When no login method has a non-empty email, returns None."""
    user = _FakeStUser([_FakeLoginMethod(None), _FakeLoginMethod("")])
    assert default_email_getter("user-123", user_getter=lambda _user_id: user) is None


def test_default_email_getter_returns_none_when_user_is_none() -> None:
    """When the SDK reports no user for the id, returns None."""
    assert default_email_getter("user-123", user_getter=lambda _user_id: None) is None


def test_default_email_getter_returns_none_on_general_error() -> None:
    """When the SDK raises a GeneralError (e.g. transient core problem), it is swallowed and None is returned."""

    def _raise(_user_id: str) -> None:
        raise SuperTokensGeneralError("transient core problem")

    assert default_email_getter("user-123", user_getter=_raise) is None


def test_default_email_getter_returns_none_on_session_error() -> None:
    """When the SDK raises a SessionError, it is swallowed and None is returned."""

    def _raise(_user_id: str) -> None:
        raise SuperTokensSessionError("bad session")

    assert default_email_getter("user-123", user_getter=_raise) is None


def _paid_lookup_backend(
    *,
    paid_domains: tuple[str, ...] = (),
    paid_emails: tuple[str, ...] = (),
) -> Callable[[], Any]:
    """Build a connection factory over a fake backend seeded with the given lists."""
    backend = make_fake_pool_backend()
    for domain in paid_domains:
        backend.add_paid_domain(domain)
    for email in paid_emails:
        backend.add_paid_email(email)
    return backend.get_connection


@pytest.mark.parametrize(
    ("email", "paid_domains", "paid_emails", "expected"),
    [
        # Exact domain match (case-insensitive on both sides).
        ("alice@imbue.com", ("imbue.com",), (), True),
        ("ALICE@IMBUE.COM", ("imbue.com",), (), True),
        ("alice@imbue.com", ("IMBUE.COM",), (), True),
        # Subdomains do NOT match a bare-domain entry (exact match only).
        ("alice@eng.imbue.com", ("imbue.com",), (), False),
        ("alice@eng.imbue.com", ("eng.imbue.com",), (), True),
        # Full-email match.
        ("bob@gmail.com", (), ("bob@gmail.com",), True),
        ("eve@gmail.com", (), ("bob@gmail.com",), False),
        # Either list grants access.
        ("carol@imbue.com", ("imbue.com",), ("dave@elsewhere.com",), True),
        # Empty lists deny everyone.
        ("alice@imbue.com", (), (), False),
    ],
)
def test_is_email_paid_in_db_matching(
    email: str,
    paid_domains: tuple[str, ...],
    paid_emails: tuple[str, ...],
    expected: bool,
) -> None:
    factory = _paid_lookup_backend(paid_domains=paid_domains, paid_emails=paid_emails)
    assert is_email_paid_in_db(email, connection_factory=factory) is expected


def test_is_email_paid_in_db_ignores_soft_deleted_rows() -> None:
    backend = make_fake_pool_backend()
    backend.add_paid_email("bob@gmail.com", is_paid=False)
    backend.add_paid_domain("imbue.com", is_paid=False)
    assert is_email_paid_in_db("bob@gmail.com", connection_factory=backend.get_connection) is False
    assert is_email_paid_in_db("alice@imbue.com", connection_factory=backend.get_connection) is False


def test_is_email_paid_caches_within_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """With a positive TTL, a second lookup is served from cache (db_lookup not re-invoked)."""
    monkeypatch.setenv("MINDS_PAID_LIST_CACHE_TTL_SECONDS", "60")
    clear_paid_status_cache()
    call_count = 0

    def _counting_lookup(email: str) -> bool:
        nonlocal call_count
        call_count += 1
        return True

    fake_clock = [1000.0]
    assert is_email_paid("x@imbue.com", db_lookup=_counting_lookup, monotonic=lambda: fake_clock[0]) is True
    # 30s later: still within the 60s window, so the cached value is reused.
    fake_clock[0] = 1030.0
    assert is_email_paid("x@imbue.com", db_lookup=_counting_lookup, monotonic=lambda: fake_clock[0]) is True
    assert call_count == 1
    clear_paid_status_cache()


def test_is_email_paid_refreshes_after_ttl_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINDS_PAID_LIST_CACHE_TTL_SECONDS", "60")
    clear_paid_status_cache()
    call_count = 0

    def _counting_lookup(email: str) -> bool:
        nonlocal call_count
        call_count += 1
        return True

    fake_clock = [1000.0]
    is_email_paid("x@imbue.com", db_lookup=_counting_lookup, monotonic=lambda: fake_clock[0])
    # 61s later: past the window, so the lookup runs again.
    fake_clock[0] = 1061.0
    is_email_paid("x@imbue.com", db_lookup=_counting_lookup, monotonic=lambda: fake_clock[0])
    assert call_count == 2
    clear_paid_status_cache()


def test_is_email_paid_bypasses_cache_when_ttl_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINDS_PAID_LIST_CACHE_TTL_SECONDS", "0")
    clear_paid_status_cache()
    call_count = 0

    def _counting_lookup(email: str) -> bool:
        nonlocal call_count
        call_count += 1
        return True

    is_email_paid("x@imbue.com", db_lookup=_counting_lookup)
    is_email_paid("x@imbue.com", db_lookup=_counting_lookup)
    assert call_count == 2


def test_require_ally_eligible_allows_when_email_is_listed() -> None:
    require_ally_eligible("alice@imbue.com", paid_checker=lambda email: True)


def test_require_ally_eligible_raises_403_when_email_not_listed() -> None:
    with pytest.raises(HTTPException) as exc_info:
        require_ally_eligible("alice@elsewhere.com", paid_checker=lambda email: False)
    assert exc_info.value.status_code == 403
    assert "partner access" in exc_info.value.detail


def test_require_ally_eligible_raises_403_when_email_is_none() -> None:
    with pytest.raises(HTTPException) as exc_info:
        require_ally_eligible(None, paid_checker=lambda email: True)
    assert exc_info.value.status_code == 403
    assert "email unavailable" in exc_info.value.detail


def test_require_ally_eligible_fails_closed_on_db_error() -> None:
    """A database error during the lookup denies eligibility (403), never allows it."""

    def _raise_db_error(email: str) -> bool:
        raise psycopg2.OperationalError("connection refused")

    with pytest.raises(HTTPException) as exc_info:
        require_ally_eligible("alice@imbue.com", paid_checker=_raise_db_error)
    assert exc_info.value.status_code == 403
    assert "database error" in exc_info.value.detail


def test_admin_key_accepted_under_legacy_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """The deprecated MINDS_PAID_ADMIN_KEY spelling still authenticates during migration."""
    client, _backend = _make_pool_test_client(monkeypatch)
    monkeypatch.delenv("MINDS_ADMIN_KEY", raising=False)
    monkeypatch.setenv("MINDS_PAID_ADMIN_KEY", _ADMIN_KEY_TEST_VALUE)
    resp = client.get("/paid/domains", headers=_admin_key_headers())
    assert resp.status_code == 200


def test_admin_key_prefers_new_env_var_over_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    """When both env vars are set, only MINDS_ADMIN_KEY authenticates."""
    client, _backend = _make_pool_test_client(monkeypatch)
    monkeypatch.setenv("MINDS_ADMIN_KEY", _ADMIN_KEY_TEST_VALUE)
    monkeypatch.setenv("MINDS_PAID_ADMIN_KEY", "legacy-value-not-accepted-4c1d")
    assert client.get("/paid/domains", headers=_admin_key_headers()).status_code == 200
    legacy_headers = {"Authorization": "Bearer legacy-value-not-accepted-4c1d"}
    assert client.get("/paid/domains", headers=legacy_headers).status_code == 401


def test_admin_key_is_rejected_on_user_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The admin key must not authenticate user-facing routes (e.g. /hosts)."""
    client, _backend = _make_paid_crud_test_client(monkeypatch)
    resp = client.get("/hosts", headers=_admin_key_headers())
    assert resp.status_code == 401
