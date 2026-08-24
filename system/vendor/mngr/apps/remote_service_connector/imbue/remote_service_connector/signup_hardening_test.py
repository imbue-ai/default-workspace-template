"""Tests for the IP-based signup hardening (velocity limits, reputation bands, step-up)."""

from urllib.parse import parse_qs
from urllib.parse import urlencode
from urllib.parse import urlsplit

import httpx
import psycopg2
import pytest
from starlette.requests import Request
from starlette.testclient import TestClient

from imbue.remote_service_connector.signup_hardening import CachedTorExitList
from imbue.remote_service_connector.signup_hardening import IpReputation
from imbue.remote_service_connector.signup_hardening import IpinfoReputationProvider
from imbue.remote_service_connector.signup_hardening import MAX_REPUTATION_LOOKUPS_PER_DAY
from imbue.remote_service_connector.signup_hardening import MAX_SIGNUP_ATTEMPTS_PER_IP_PER_HOUR
from imbue.remote_service_connector.signup_hardening import MAX_SIGNUP_ATTEMPTS_PER_SUBNET_PER_DAY
from imbue.remote_service_connector.signup_hardening import SignupIpVerdict
from imbue.remote_service_connector.signup_hardening import _MAX_RECORDED_EMAIL_CHARS
from imbue.remote_service_connector.signup_hardening import classify_reputation
from imbue.remote_service_connector.signup_hardening import client_ip_for_request
from imbue.remote_service_connector.signup_hardening import subnet_for_client_ip
from imbue.remote_service_connector.testing import FakeSuperTokensBackend
from imbue.remote_service_connector.testing import TEST_OAUTH_SIGNING_KEY_PEM
from imbue.remote_service_connector.testing import _make_accounts_web_test_client

_CLEAN_IP = "203.0.113.77"
_CLEAN_SUBNET = "203.0.113.0/24"


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_subnet_for_client_ip_aggregates_v4_to_a_slash_24() -> None:
    assert subnet_for_client_ip("203.0.113.77") == "203.0.113.0/24"


def test_subnet_for_client_ip_aggregates_v6_to_a_slash_48() -> None:
    assert subnet_for_client_ip("2001:db8:abcd:12::1") == "2001:db8:abcd::/48"


def test_subnet_for_client_ip_is_none_for_garbage() -> None:
    assert subnet_for_client_ip("not-an-ip") is None


@pytest.mark.parametrize(
    ("reputation", "expected_verdict"),
    [
        (IpReputation(), SignupIpVerdict.CLEAN),
        (IpReputation(tor=True), SignupIpVerdict.ABUSIVE),
        (IpReputation(hosting=True), SignupIpVerdict.ABUSIVE),
        (IpReputation(vpn=True), SignupIpVerdict.SUSPICIOUS),
        (IpReputation(proxy=True), SignupIpVerdict.SUSPICIOUS),
        (IpReputation(relay=True), SignupIpVerdict.SUSPICIOUS),
        # tor/hosting outrank the step-up flags when both are present.
        (IpReputation(vpn=True, hosting=True), SignupIpVerdict.ABUSIVE),
    ],
)
def test_classify_reputation_maps_flags_onto_bands(
    reputation: IpReputation, expected_verdict: SignupIpVerdict
) -> None:
    assert classify_reputation(reputation) is expected_verdict


def _request_with_peer(peer: tuple[str, int] | None) -> Request:
    return Request({"type": "http", "client": peer, "headers": []})


def test_client_ip_for_request_returns_the_valid_socket_peer() -> None:
    assert client_ip_for_request(_request_with_peer(("198.51.100.9", 40000))) == "198.51.100.9"


def test_client_ip_for_request_rejects_a_non_ip_peer() -> None:
    # starlette's TestClient reports the literal peer "testclient"; anything
    # that does not parse as an IP must yield None, not reach inet columns.
    assert client_ip_for_request(_request_with_peer(("testclient", 50000))) is None


def test_client_ip_for_request_is_none_without_a_peer() -> None:
    assert client_ip_for_request(_request_with_peer(None)) is None


def test_cached_tor_exit_list_serves_membership_without_fetching_when_fresh() -> None:
    tor_list = CachedTorExitList(exit_ips=frozenset({"198.51.100.1"}), next_fetch_monotonic=float("inf"))
    assert tor_list.is_tor_exit("198.51.100.1") is True
    assert tor_list.is_tor_exit("198.51.100.2") is False


def test_cached_tor_exit_list_fetches_once_and_parses_the_exit_list() -> None:
    served_requests: list[httpx.Request] = []

    def _serve_exit_list(request: httpx.Request) -> httpx.Response:
        served_requests.append(request)
        return httpx.Response(200, text="198.51.100.1\n198.51.100.2\n\n")

    tor_list = CachedTorExitList(transport=httpx.MockTransport(_serve_exit_list))

    assert tor_list.is_tor_exit("198.51.100.1") is True
    assert tor_list.is_tor_exit("203.0.113.5") is False
    # The second membership check rode the fresh copy instead of refetching.
    assert len(served_requests) == 1


def test_cached_tor_exit_list_fetch_failure_keeps_the_previous_copy_and_backs_off() -> None:
    served_requests: list[httpx.Request] = []

    def _refuse_fetch(request: httpx.Request) -> httpx.Response:
        served_requests.append(request)
        raise httpx.ConnectError("tor project down")

    tor_list = CachedTorExitList(exit_ips=frozenset({"198.51.100.1"}), transport=httpx.MockTransport(_refuse_fetch))

    # The stale-copy refresh fails, but the previous copy keeps serving.
    assert tor_list.is_tor_exit("198.51.100.1") is True
    assert tor_list.is_tor_exit("203.0.113.5") is False
    # The failure armed the retry backoff: no immediate second fetch.
    assert len(served_requests) == 1


def test_ipinfo_provider_is_disabled_without_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fail_on_any_request(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request may be sent when the provider has no token")

    monkeypatch.delenv("IPINFO_TOKEN", raising=False)
    provider = IpinfoReputationProvider(transport=httpx.MockTransport(_fail_on_any_request))

    assert provider.fetch_reputation(_CLEAN_IP) is None


def test_ipinfo_provider_parses_the_max_lookup_payload_and_keeps_the_token_out_of_the_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    served_requests: list[httpx.Request] = []

    # The shape https://api.ipinfo.io/lookup/{ip} actually serves (verified
    # against a live Max token 2026-08-21): anonymizer flags nested under
    # "anonymous", is_hosting at the top level.
    def _serve_max_lookup_payload(request: httpx.Request) -> httpx.Response:
        served_requests.append(request)
        return httpx.Response(
            200,
            json={
                "ip": _CLEAN_IP,
                "as": {"asn": "AS64496", "type": "hosting"},
                "anonymous": {
                    "name": "ExampleVPN",
                    "is_vpn": True,
                    "is_proxy": False,
                    "is_tor": False,
                    "is_relay": True,
                    "is_res_proxy": False,
                },
                "is_anonymous": True,
                "is_hosting": False,
            },
        )

    monkeypatch.setenv("IPINFO_TOKEN", "secret-token-123")
    provider = IpinfoReputationProvider(transport=httpx.MockTransport(_serve_max_lookup_payload))

    reputation = provider.fetch_reputation(_CLEAN_IP)

    assert reputation == IpReputation(vpn=True, relay=True, service="ExampleVPN")
    request = served_requests[0]
    assert str(request.url) == f"https://api.ipinfo.io/lookup/{_CLEAN_IP}"
    # Bearer auth keeps the secret out of the URL (which httpx echoes into
    # logs and error messages); it must ride the Authorization header only.
    assert "secret-token-123" not in str(request.url)
    assert request.headers["Authorization"] == "Bearer secret-token-123"


def test_ipinfo_provider_folds_residential_proxies_into_the_proxy_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _serve_res_proxy_payload(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ip": _CLEAN_IP,
                "anonymous": {"name": "ExampleResNet", "is_res_proxy": True},
                "is_anonymous": True,
                "is_hosting": False,
            },
        )

    monkeypatch.setenv("IPINFO_TOKEN", "secret-token-123")
    provider = IpinfoReputationProvider(transport=httpx.MockTransport(_serve_res_proxy_payload))

    reputation = provider.fetch_reputation(_CLEAN_IP)

    # Residential proxies land in the step-up band (proxy), never the block band.
    assert reputation is not None
    assert reputation == IpReputation(proxy=True, service="ExampleResNet")
    assert classify_reputation(reputation) is SignupIpVerdict.SUSPICIOUS


# ---------------------------------------------------------------------------
# Password signup gate (enforced tiers)
# ---------------------------------------------------------------------------


def _make_production_signup_client(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TestClient, FakeSuperTokensBackend]:
    client, st_backend, _codes = _make_accounts_web_test_client(monkeypatch)
    monkeypatch.setenv("MNGR_DEPLOY_ENV", "production")
    return client, st_backend


def _post_signup(client: TestClient, email: str = "new-user@example.com") -> dict[str, object]:
    response = client.post("/accounts/api/signup", json={"email": email, "password": "password123"})
    assert response.status_code == 200, response.text
    return response.json()


def test_clean_ip_signup_succeeds_and_records_an_allowed_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    client, st_backend = _make_production_signup_client(monkeypatch)

    body = _post_signup(client)

    assert body["status"] == "OK"
    assert "new-user@example.com" in st_backend.accounts_by_email
    attempts = st_backend.signup_attempt_store.attempts
    assert len(attempts) == 1
    assert attempts[0]["client_ip"] == _CLEAN_IP
    assert attempts[0]["subnet"] == _CLEAN_SUBNET
    assert attempts[0]["signup_method"] == "password"
    assert attempts[0]["verdict"] == "clean"
    assert attempts[0]["outcome"] == "allowed"


def test_recorded_email_is_clamped_to_a_bounded_length(monkeypatch: pytest.MonkeyPatch) -> None:
    client, st_backend = _make_production_signup_client(monkeypatch)
    # The gate records BEFORE any field validation, so the raw request-body
    # value must be clamped on its way into the table and the log.
    giant_email = "a" * (10 * _MAX_RECORDED_EMAIL_CHARS) + "@example.com"

    _post_signup(client, email=giant_email)

    attempts = st_backend.signup_attempt_store.attempts
    assert len(attempts) == 1
    assert attempts[0]["email"] == giant_email[:_MAX_RECORDED_EMAIL_CHARS]


def test_vpn_ip_signup_is_stepped_up_to_oauth_only(monkeypatch: pytest.MonkeyPatch) -> None:
    client, st_backend = _make_production_signup_client(monkeypatch)
    st_backend.ip_reputation_provider.reputation_by_ip[_CLEAN_IP] = IpReputation(vpn=True, service="ExampleVPN")

    body = _post_signup(client)

    assert body["status"] == "OAUTH_ONLY"
    assert "new-user@example.com" not in st_backend.accounts_by_email
    attempts = st_backend.signup_attempt_store.attempts
    assert len(attempts) == 1
    assert attempts[0]["verdict"] == "suspicious"
    assert attempts[0]["outcome"] == "oauth_only"
    assert attempts[0]["reputation_json"] is not None
    assert "ExampleVPN" in attempts[0]["reputation_json"]


@pytest.mark.parametrize("reputation", [IpReputation(tor=True), IpReputation(hosting=True)])
def test_tor_and_hosting_ips_are_blocked_outright(monkeypatch: pytest.MonkeyPatch, reputation: IpReputation) -> None:
    client, st_backend = _make_production_signup_client(monkeypatch)
    st_backend.ip_reputation_provider.reputation_by_ip[_CLEAN_IP] = reputation

    body = _post_signup(client)

    assert body["status"] == "SIGNUP_BLOCKED"
    assert "new-user@example.com" not in st_backend.accounts_by_email
    assert st_backend.signup_attempt_store.attempts[0]["outcome"] == "blocked"
    assert st_backend.signup_attempt_store.attempts[0]["verdict"] == "abusive"


def test_tor_exit_list_blocks_even_without_a_reputation_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    client, st_backend = _make_production_signup_client(monkeypatch)
    st_backend.ip_reputation_provider.is_configured = False
    st_backend.tor_exit_list.exit_ips.add(_CLEAN_IP)

    body = _post_signup(client)

    assert body["status"] == "SIGNUP_BLOCKED"
    assert "new-user@example.com" not in st_backend.accounts_by_email


def test_per_ip_velocity_cap_rate_limits_without_burning_reputation_lookups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, st_backend = _make_production_signup_client(monkeypatch)
    st_backend.signup_attempt_store.seed_attempts(_CLEAN_IP, _CLEAN_SUBNET, MAX_SIGNUP_ATTEMPTS_PER_IP_PER_HOUR)

    body = _post_signup(client)

    assert body["status"] == "RATE_LIMITED"
    assert "new-user@example.com" not in st_backend.accounts_by_email
    # The refused attempt is itself recorded (so a sustained flood stays visible).
    assert st_backend.signup_attempt_store.attempts[-1]["outcome"] == "rate_limited"
    # Rate-limited attempts must not spend provider lookups.
    assert st_backend.ip_reputation_provider.fetch_count == 0


def test_per_subnet_velocity_cap_rate_limits_across_rotating_ips(monkeypatch: pytest.MonkeyPatch) -> None:
    client, st_backend = _make_production_signup_client(monkeypatch)
    # Fill the /24's daily budget from OTHER addresses inside it.
    for last_octet in range(MAX_SIGNUP_ATTEMPTS_PER_SUBNET_PER_DAY):
        st_backend.signup_attempt_store.seed_attempts(f"203.0.113.{last_octet}", _CLEAN_SUBNET, 1)

    body = _post_signup(client)

    assert body["status"] == "RATE_LIMITED"
    assert "new-user@example.com" not in st_backend.accounts_by_email


def test_reputation_provider_outage_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    client, st_backend = _make_production_signup_client(monkeypatch)
    st_backend.ip_reputation_provider.error_to_raise = httpx.ConnectError("provider down")

    body = _post_signup(client)

    assert body["status"] == "OK"
    assert "new-user@example.com" in st_backend.accounts_by_email


def test_reputation_cache_outage_fails_open_and_still_uses_the_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, st_backend = _make_production_signup_client(monkeypatch)
    # Every cache call (read, budget check, write) fails, but the provider
    # is up and knows this IP is a VPN: the outage must degrade to a live
    # lookup -- not to a skipped verdict, and never to a refusal.
    st_backend.ip_reputation_cache.error_to_raise = psycopg2.OperationalError("neon down")
    st_backend.ip_reputation_provider.reputation_by_ip[_CLEAN_IP] = IpReputation(vpn=True)

    body = _post_signup(client)

    assert body["status"] == "OAUTH_ONLY"
    assert st_backend.ip_reputation_provider.fetch_count == 1


def test_velocity_store_outage_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    client, st_backend = _make_production_signup_client(monkeypatch)
    st_backend.signup_attempt_store.error_to_raise = psycopg2.OperationalError("neon down")

    body = _post_signup(client)

    assert body["status"] == "OK"
    assert "new-user@example.com" in st_backend.accounts_by_email


def test_unknown_client_ip_skips_the_gate_but_still_records(monkeypatch: pytest.MonkeyPatch) -> None:
    client, st_backend = _make_production_signup_client(monkeypatch)
    st_backend.fake_client_ip = None
    st_backend.ip_reputation_provider.reputation_by_ip[_CLEAN_IP] = IpReputation(tor=True)

    body = _post_signup(client)

    assert body["status"] == "OK"
    attempts = st_backend.signup_attempt_store.attempts
    assert len(attempts) == 1
    assert attempts[0]["client_ip"] is None
    assert attempts[0]["verdict"] == "clean"
    # No IP means nothing to look up.
    assert st_backend.ip_reputation_provider.fetch_count == 0


def test_reputation_lookups_are_cached_per_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    client, st_backend = _make_production_signup_client(monkeypatch)

    first = _post_signup(client, email="first@example.com")
    second = _post_signup(client, email="second@example.com")

    assert first["status"] == "OK"
    assert second["status"] == "OK"
    assert st_backend.ip_reputation_provider.fetch_count == 1


def test_exhausted_daily_lookup_budget_degrades_to_tor_list_only(monkeypatch: pytest.MonkeyPatch) -> None:
    client, st_backend = _make_production_signup_client(monkeypatch)
    # A vpn verdict is waiting at the provider, but the budget is spent.
    st_backend.ip_reputation_provider.reputation_by_ip[_CLEAN_IP] = IpReputation(vpn=True)
    for lookup_idx in range(MAX_REPUTATION_LOOKUPS_PER_DAY):
        st_backend.ip_reputation_cache.store(f"2001:db8::{lookup_idx:x}", IpReputation())

    body = _post_signup(client)

    assert body["status"] == "OK"
    assert st_backend.ip_reputation_provider.fetch_count == 0


def test_dev_tiers_record_the_verdict_but_never_refuse(monkeypatch: pytest.MonkeyPatch) -> None:
    # The conftest pins MNGR_DEPLOY_ENV to a dev env name, where enforcement
    # is off (the headless JSON signup is open there anyway).
    client, st_backend, _codes = _make_accounts_web_test_client(monkeypatch)
    st_backend.ip_reputation_provider.reputation_by_ip[_CLEAN_IP] = IpReputation(tor=True)

    body = _post_signup(client)

    assert body["status"] == "OK"
    assert "new-user@example.com" in st_backend.accounts_by_email
    attempts = st_backend.signup_attempt_store.attempts
    assert attempts[0]["verdict"] == "abusive"
    assert attempts[0]["outcome"] == "allowed"


# ---------------------------------------------------------------------------
# Google OAuth account-creation gate
# ---------------------------------------------------------------------------


def _make_production_oauth_client(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TestClient, FakeSuperTokensBackend]:
    client, st_backend = _make_production_signup_client(monkeypatch)
    monkeypatch.setenv("BROKER_JWT_SIGNING_KEY_PEM", TEST_OAUTH_SIGNING_KEY_PEM)
    st_backend.register_provider("google", email="visitor@example.com", is_verified=True)
    return client, st_backend


def _run_oauth_callback(client: TestClient) -> httpx.Response:
    # terms=1 models the signup tab's Google button (the terms gate is
    # exercised by accounts_web_test; here the IP gate is under test).
    start = client.get("/accounts/oauth/google/start?next=%2F&terms=1", follow_redirects=False)
    assert start.status_code == 302
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    return client.get(
        f"/share/oauth/google/callback?{urlencode({'code': 'code-1', 'state': state})}", follow_redirects=False
    )


def test_oauth_signup_from_an_abusive_ip_is_rolled_back_and_bounced(monkeypatch: pytest.MonkeyPatch) -> None:
    client, st_backend = _make_production_oauth_client(monkeypatch)
    st_backend.ip_reputation_provider.reputation_by_ip[_CLEAN_IP] = IpReputation(hosting=True)

    resp = _run_oauth_callback(client)

    assert resp.status_code == 303
    assert parse_qs(urlsplit(resp.headers["location"]).query)["error"] == ["signup_blocked"]
    # The just-created account was rolled back and no session was minted.
    assert "visitor@example.com" not in st_backend.accounts_by_email
    assert st_backend.last_browser_session is None
    attempts = st_backend.signup_attempt_store.attempts
    assert attempts[-1]["signup_method"] == "google"
    assert attempts[-1]["outcome"] == "blocked"


def test_oauth_signup_from_a_vpn_ip_proceeds_because_oauth_is_the_step_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, st_backend = _make_production_oauth_client(monkeypatch)
    st_backend.ip_reputation_provider.reputation_by_ip[_CLEAN_IP] = IpReputation(vpn=True)

    resp = _run_oauth_callback(client)

    assert resp.status_code == 303
    assert "error" not in parse_qs(urlsplit(resp.headers["location"]).query)
    assert "visitor@example.com" in st_backend.accounts_by_email
    attempts = st_backend.signup_attempt_store.attempts
    assert attempts[-1]["signup_method"] == "google"
    assert attempts[-1]["verdict"] == "suspicious"
    assert attempts[-1]["outcome"] == "allowed"


def test_oauth_rate_limited_signup_is_rolled_back(monkeypatch: pytest.MonkeyPatch) -> None:
    client, st_backend = _make_production_oauth_client(monkeypatch)
    st_backend.signup_attempt_store.seed_attempts(_CLEAN_IP, _CLEAN_SUBNET, MAX_SIGNUP_ATTEMPTS_PER_IP_PER_HOUR)

    resp = _run_oauth_callback(client)

    assert resp.status_code == 303
    assert parse_qs(urlsplit(resp.headers["location"]).query)["error"] == ["signup_blocked"]
    assert "visitor@example.com" not in st_backend.accounts_by_email
    assert st_backend.signup_attempt_store.attempts[-1]["outcome"] == "rate_limited"


def test_oauth_sign_in_to_an_existing_account_ignores_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    client, st_backend = _make_production_oauth_client(monkeypatch)
    st_backend.ip_reputation_provider.reputation_by_ip[_CLEAN_IP] = IpReputation(tor=True)
    existing_user_id = st_backend.add_third_party_account(
        provider_id="google", email="visitor@example.com", third_party_user_id="tp-user-1", is_verified=True
    )

    resp = _run_oauth_callback(client)

    # A returning sign-in is untouched: session minted, account intact, and
    # nothing recorded (only account CREATION is gated).
    assert resp.status_code == 303
    assert "error" not in parse_qs(urlsplit(resp.headers["location"]).query)
    assert st_backend.last_browser_session is not None
    assert st_backend.last_browser_session.user_id == existing_user_id
    assert st_backend.signup_attempt_store.attempts == []
