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
from imbue.remote_service_connector.auth import get_backfill_email
from imbue.remote_service_connector.auth import is_email_paid
from imbue.remote_service_connector.auth import is_email_paid_in_db
from imbue.remote_service_connector.auth import require_ally_eligible
from imbue.remote_service_connector.auth import require_verified_email
from imbue.remote_service_connector.auth import resolve_account_email
from imbue.remote_service_connector.errors import EmailNotVerifiedError
from imbue.remote_service_connector.testing import _ADMIN_KEY_TEST_VALUE
from imbue.remote_service_connector.testing import _FakeLoginMethod
from imbue.remote_service_connector.testing import _admin_key_headers
from imbue.remote_service_connector.testing import _make_paid_crud_test_client
from imbue.remote_service_connector.testing import _make_pool_test_client
from imbue.remote_service_connector.testing import _make_test_client
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
        email_resolver=lambda _user_id: ("alice@example.com", True),
    )
    assert isinstance(result, UserAuth)
    assert result.user_id_prefix == "a1b2c3d4e5f67890"
    assert result.email == "alice@example.com"
    assert result.is_email_verified is True
    assert result.verified_email == "alice@example.com"


def test_authenticate_supertokens_accepts_unverified_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unverified email authenticates -- verification is non-blocking.

    The account carries ``is_email_verified=False`` so the endpoints that DO
    authorize by email ownership can refuse it via ``require_verified_email``.
    """
    user_id = "a1b2c3d4-e5f6-7890-abcd-1234567890ab"
    monkeypatch.setenv("SUPERTOKENS_CONNECTION_URI", "https://st.example.com")
    result = _authenticate_supertokens(
        "valid-token",
        session_getter=lambda **kwargs: _FakeSession(user_id, email_verified=False),
        email_resolver=lambda _user_id: ("alice@example.com", False),
    )
    assert isinstance(result, UserAuth)
    assert result.email == "alice@example.com"
    assert result.is_email_verified is False
    assert result.verified_email is None


def test_authenticate_supertokens_raises_401_when_account_has_no_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the live lookup finds no email at all, auth is rejected with 401.

    Every enabled login method carries an email, so a missing email means a
    broken account record (or a deleted user), not a legitimate caller.
    """
    user_id = "a1b2c3d4-e5f6-7890-abcd-1234567890ab"
    monkeypatch.setenv("SUPERTOKENS_CONNECTION_URI", "https://st.example.com")
    with pytest.raises(HTTPException) as exc_info:
        _authenticate_supertokens(
            "valid-token",
            session_getter=lambda **kwargs: _FakeSession(user_id, email_verified=True),
            email_resolver=lambda _user_id: (None, False),
        )
    assert exc_info.value.status_code == 401
    assert "no email" in exc_info.value.detail


def test_authenticate_supertokens_ignores_stale_unverified_token_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A token minted while unverified reports verified once the core says so.

    The access token carries a cached ``email_verified=False`` claim, but the
    live ``email_resolver`` lookup reports the email verified (the user clicked
    the link after signing in). The guard must trust the live result, not the
    stale token claim.
    """
    user_id = "a1b2c3d4-e5f6-7890-abcd-1234567890ab"
    monkeypatch.setenv("SUPERTOKENS_CONNECTION_URI", "https://st.example.com")
    result = _authenticate_supertokens(
        "stale-token",
        session_getter=lambda **kwargs: _FakeSession(user_id, email_verified=False),
        email_resolver=lambda _user_id: ("alice@example.com", True),
    )
    assert isinstance(result, UserAuth)
    assert result.email == "alice@example.com"
    assert result.is_email_verified is True


def test_require_verified_email_passes_for_verified_account() -> None:
    require_verified_email(UserAuth(user_id_prefix="a" * 16, email="alice@example.com", is_email_verified=True))


def test_require_verified_email_raises_structured_error_for_unverified_account() -> None:
    user = UserAuth(user_id_prefix="a" * 16, email="alice@example.com", is_email_verified=False)
    with pytest.raises(EmailNotVerifiedError) as exc_info:
        require_verified_email(user)
    assert exc_info.value.email == "alice@example.com"


def test_authenticate_supertokens_raises_401_when_connection_uri_not_set() -> None:
    """When SUPERTOKENS_CONNECTION_URI is absent, raises 401."""
    with pytest.raises(HTTPException) as exc_info:
        _authenticate_supertokens(
            "any-token",
            session_getter=lambda **kwargs: _FakeSession("ignored"),
            email_resolver=lambda _user_id: (None, False),
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


def test_resolve_account_email_prefers_verified_over_earlier_unverified() -> None:
    """A verified login method wins even when an unverified email comes first."""
    user = _FakeStUser(
        [
            _FakeLoginMethod("unverified@gmail.com", verified=False),
            _FakeLoginMethod("alice@imbue.com", verified=True),
        ]
    )
    assert resolve_account_email("user-123", user_getter=lambda _user_id: user) == ("alice@imbue.com", True)


def test_resolve_account_email_falls_back_to_unverified_email() -> None:
    """With no verified login method, the first email is returned with is_verified=False."""
    user = _FakeStUser(
        [
            _FakeLoginMethod(None),
            _FakeLoginMethod("alice@gmail.com", verified=False),
        ]
    )
    assert resolve_account_email("user-123", user_getter=lambda _user_id: user) == ("alice@gmail.com", False)


def test_resolve_account_email_returns_none_for_missing_user() -> None:
    assert resolve_account_email("user-123", user_getter=lambda _user_id: None) == (None, False)


def test_get_backfill_email_returns_verified_email() -> None:
    user = _FakeStUser([_FakeLoginMethod("alice@imbue.com", verified=True)])
    assert get_backfill_email("user-123", user_getter=lambda _user_id: user) == "alice@imbue.com"


def test_get_backfill_email_returns_empty_string_for_unverified_user() -> None:
    """An existing-but-unverified user maps to "" so a plain explorer row is created (no paid check)."""
    user = _FakeStUser([_FakeLoginMethod("alice@imbue.com", verified=False)])
    assert get_backfill_email("user-123", user_getter=lambda _user_id: user) == ""


def test_get_backfill_email_returns_none_for_missing_user() -> None:
    """A user that cannot be resolved maps to None so no row is ever created for them."""
    assert get_backfill_email("user-123", user_getter=lambda _user_id: None) is None


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


def test_route_no_auth_returns_401(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    resp = client.get("/hosts")
    assert resp.status_code == 401


def test_route_rejects_basic_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Basic Auth is not a supported scheme; only Bearer credentials are accepted."""
    client = _make_test_client(monkeypatch)
    resp = client.get("/hosts", headers={"Authorization": "Basic dGVzdDp0ZXN0"})
    assert resp.status_code == 401


def test_route_invalid_bearer_token(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    resp = client.get("/hosts", headers={"Authorization": "Bearer not-a-valid-jwt!!!"})
    assert resp.status_code == 401


def test_authenticate_supertokens_threads_check_database_to_the_session_getter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The D3 knob: state-modifying routes verify sessions against the core, reads stay stateless."""
    user_id = "a1b2c3d4-e5f6-7890-abcd-1234567890ab"
    monkeypatch.setenv("SUPERTOKENS_CONNECTION_URI", "https://st.example.com")
    seen_kwargs: dict[str, object] = {}

    def _capturing_getter(**kwargs: object) -> _FakeSession:
        seen_kwargs.update(kwargs)
        return _FakeSession(user_id)

    _authenticate_supertokens(
        "valid-token",
        session_getter=_capturing_getter,
        email_resolver=lambda _user_id: ("alice@example.com", True),
        check_database=True,
    )
    assert seen_kwargs["check_database"] is True

    _authenticate_supertokens(
        "valid-token",
        session_getter=_capturing_getter,
        email_resolver=lambda _user_id: ("alice@example.com", True),
    )
    assert seen_kwargs["check_database"] is False
