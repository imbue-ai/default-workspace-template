"""Golden wire-compat test: live responses must parse for every shipped client snapshot.

The connector deploys continuously while shipped desktop clients update on
their own cadence, so an already-released client routinely parses responses
from a newer server. Each module under ``compat/`` freezes one shipped
release's strict response models; this test exercises the real endpoints
in-process (fake stores, no network) and validates every response body
against every in-window snapshot -- so a response-shape change that would
break the shipped fleet fails CI here, before it can deploy.

Three enforcement layers:

1. Response validation: real endpoint responses parse under each snapshot.
2. Route completeness: every APIRoute on the app is classified -- either
   strictly parsed by shipped clients (and exercised here) or explicitly
   exempt with a reason. A new endpoint cannot silently escape coverage.
3. Snapshot aging: a snapshot past its ``SUPPORT_ENDS`` date fails with a
   prune-or-extend message, so un-freezing response shapes is always a
   deliberate decision.
"""

import ast
import base64
import secrets
from datetime import date
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs
from urllib.parse import urlencode
from urllib.parse import urlsplit
from uuid import UUID

import pytest
from fastapi.routing import APIRoute
from pydantic import BaseModel
from starlette.testclient import TestClient
from supertokens_python.recipe.emailpassword.interfaces import SignUpOkResult as EPSignUpOkResult

import imbue.remote_service_connector.auth_proxy as auth_proxy_mod
from imbue.remote_service_connector.accounts_web import compute_pkce_challenge
from imbue.remote_service_connector.compat import wire_models_minds_0_3_16
from imbue.remote_service_connector.compat import wire_models_minds_0_4_0
from imbue.remote_service_connector.testing import FakeSuperTokensBackend
from imbue.remote_service_connector.testing import _USER_STUB_USER_ID
from imbue.remote_service_connector.testing import _make_accounts_web_test_client
from imbue.remote_service_connector.testing import _make_bucket_test_client
from imbue.remote_service_connector.testing import _make_pool_quota_test_client
from imbue.remote_service_connector.testing import _make_quota_test_client
from imbue.remote_service_connector.testing import _make_sync_test_client
from imbue.remote_service_connector.testing import _user_headers
from imbue.remote_service_connector.testing import make_fake_supertokens_backend
from imbue.remote_service_connector.testing import make_storage_config
from imbue.remote_service_connector.web import web_app

# Every snapshot currently enforced. Append the new release's module at each
# minds release; delete a module (and its entry here) when it leaves the
# support window (see test_compat_snapshots_are_within_their_support_window).
_SNAPSHOTS = (wire_models_minds_0_3_16, wire_models_minds_0_4_0)


def _validate_for_snapshots(endpoint_key: str, body: object) -> None:
    """Validate one response body against every snapshot that strictly parses this endpoint."""
    for snapshot in _SNAPSHOTS:
        model = snapshot.MODEL_BY_ENDPOINT.get(endpoint_key)
        if model is None:
            continue
        try:
            model.model_validate(body)
        except ValueError as exc:
            pytest.fail(
                f"Response of {endpoint_key} no longer parses for {snapshot.SNAPSHOT_NAME}: {exc}\n"
                "This change would break every shipped client of that release. Either make the change "
                "additively compatible, or (if that release has left the support window) prune its "
                "snapshot from compat/."
            )


def _validate_entries_for_snapshots(endpoint_key: str, entries: object) -> None:
    assert isinstance(entries, list) and entries, f"{endpoint_key}: fixture produced no entries to validate"
    for entry in entries:
        _validate_for_snapshots(endpoint_key, entry)


# ---------------------------------------------------------------------------
# Snapshot aging
# ---------------------------------------------------------------------------


def test_compat_snapshots_are_within_their_support_window() -> None:
    """A snapshot past its SUPPORT_ENDS date must be pruned (or deliberately extended)."""
    for snapshot in _SNAPSHOTS:
        assert date.today() <= snapshot.SUPPORT_ENDS, (
            f"The compat snapshot '{snapshot.SNAPSHOT_NAME}' passed its support end "
            f"({snapshot.SUPPORT_ENDS}). Decide deliberately: if the fleet (per the access log's "
            "imbue_client field) no longer shows in-window clients of that release, DELETE the "
            "snapshot module to un-freeze the response shapes it pins; otherwise extend its "
            "SUPPORT_ENDS with a dated justification."
        )


# ---------------------------------------------------------------------------
# Response validation, per endpoint group
# ---------------------------------------------------------------------------


def test_auth_responses_parse_for_all_snapshots(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPERTOKENS_CONNECTION_URI", "https://st.example.com")
    st_backend = make_fake_supertokens_backend()
    st_backend.install_on_app_module(auth_proxy_mod, monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)

    signup = client.post("/auth/signup", json={"email": "compat@example.com", "password": "password123"})
    assert signup.status_code == 200
    _validate_for_snapshots("POST /auth/signup", signup.json())

    signin = client.post("/auth/signin", json={"email": "compat@example.com", "password": "password123"})
    assert signin.status_code == 200
    _validate_for_snapshots("POST /auth/signin", signin.json())


def test_device_token_exchange_parses_for_all_snapshots(monkeypatch: pytest.MonkeyPatch) -> None:
    client, st_backend, _codes = _make_accounts_web_test_client(monkeypatch)
    # Browser sign-in, then the confirmed authorize -> code -> exchange handoff.
    signup = st_backend.sign_up(tenant_id="public", email="device@example.com", password="pw-123456")
    assert isinstance(signup, EPSignUpOkResult)
    session = st_backend.sdk_create_browser_session(None, signup.user.id)
    client.cookies.set(FakeSuperTokensBackend.BROWSER_SESSION_COOKIE, session.access_token)
    verifier = secrets.token_urlsafe(32)
    query = {
        "redirect_uri": "http://127.0.0.1:8123/callback",
        "state": "state-1",
        "code_challenge": compute_pkce_challenge(verifier),
        "confirmed": "1",
    }
    authorize = client.get(f"/accounts/authorize?{urlencode(query)}", follow_redirects=False)
    assert authorize.status_code == 302, authorize.text
    callback_query = parse_qs(urlsplit(authorize.headers["location"]).query)
    code = callback_query["code"][0]

    exchange = client.post(
        "/auth/device/token",
        json={"code": code, "code_verifier": verifier, "redirect_uri": "http://127.0.0.1:8123/callback"},
    )

    assert exchange.status_code == 200, exchange.text
    _validate_for_snapshots("POST /auth/device/token", exchange.json())


def test_host_and_workspace_responses_parse_for_all_snapshots(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend, _entitlements_store, _litellm = _make_pool_quota_test_client(monkeypatch)
    backend.storage_config = make_storage_config()
    backend.add_available_host(
        host_id=UUID("00000000-0000-0000-0000-00000000c0a1"), version="v0.1.0", agent_id="agent-compat"
    )

    lease = client.post(
        "/hosts/lease",
        json={"ssh_public_key": "ssh-ed25519 AAAA compat", "host_name": "compat-ws", "attributes": {}},
        headers=_user_headers(),
    )
    assert lease.status_code == 200, lease.text
    _validate_for_snapshots("POST /hosts/lease", lease.json())

    hosts = client.get("/hosts", headers=_user_headers())
    assert hosts.status_code == 200
    _validate_entries_for_snapshots("GET /hosts [entry]", hosts.json())

    workspaces = client.get("/workspaces", headers=_user_headers())
    assert workspaces.status_code == 200
    _validate_entries_for_snapshots("GET /workspaces [entry]", workspaces.json())

    host_db_id = lease.json()["host_db_id"]
    one = client.get(f"/workspaces/{host_db_id}", headers=_user_headers())
    assert one.status_code == 200
    _validate_for_snapshots("GET /workspaces/{host_db_id}", one.json())

    stop = client.post(f"/workspaces/{host_db_id}/stop", headers=_user_headers())
    assert stop.status_code == 202, stop.text
    _validate_for_snapshots("POST /workspaces/{host_db_id}/stop", stop.json())

    # Drive the row to stopped so the start transition is legal.
    row = next(r for r in backend.pool_rows if str(r.host_id) == host_db_id)
    row.status = "stopped"
    row.stopped_at = datetime.now(timezone.utc)
    start = client.post(f"/workspaces/{host_db_id}/start", headers=_user_headers())
    assert start.status_code == 202, start.text
    _validate_for_snapshots("POST /workspaces/{host_db_id}/start", start.json())


def test_llm_key_responses_parse_for_all_snapshots(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend, _entitlements_store, _litellm = _make_pool_quota_test_client(monkeypatch)

    created = client.post("/keys/create", json={"key_alias": "compat-alias"}, headers=_user_headers())
    assert created.status_code == 200, created.text
    _validate_for_snapshots("POST /keys/create", created.json())

    listed = client.get("/keys", headers=_user_headers())
    assert listed.status_code == 200
    _validate_entries_for_snapshots("GET /keys [entry]", listed.json())

    key_token = listed.json()[0]["token"]
    one = client.get(f"/keys/{key_token}", headers=_user_headers())
    assert one.status_code == 200, one.text
    _validate_for_snapshots("GET /keys/{key_id}", one.json())


def test_bucket_responses_parse_for_all_snapshots(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _fake, _store = _make_bucket_test_client(monkeypatch)

    created = client.post("/buckets", json={"name": "compat-data"}, headers=_user_headers())
    assert created.status_code == 200, created.text
    _validate_for_snapshots("POST /buckets", created.json())

    listed = client.get("/buckets", headers=_user_headers())
    assert listed.status_code == 200
    _validate_entries_for_snapshots("GET /buckets [entry]", listed.json())

    info = client.get("/buckets/compat-data", headers=_user_headers())
    assert info.status_code == 200, info.text
    _validate_for_snapshots("GET /buckets/{name}", info.json())

    rolled = client.post("/buckets/compat-data/roll-key", headers=_user_headers())
    assert rolled.status_code == 200, rolled.text
    _validate_for_snapshots("POST /buckets/{name}/roll-key", rolled.json())

    bucket_keys = client.get("/buckets/compat-data/keys", headers=_user_headers())
    assert bucket_keys.status_code == 200
    _validate_entries_for_snapshots("GET /buckets/{name}/keys [entry]", bucket_keys.json())

    all_keys = client.get("/bucket-keys", headers=_user_headers())
    assert all_keys.status_code == 200
    _validate_entries_for_snapshots("GET /bucket-keys [entry]", all_keys.json())


def test_account_responses_parse_for_all_snapshots(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _entitlements_store, litellm = _make_quota_test_client(monkeypatch)
    litellm.users_by_id[_USER_STUB_USER_ID] = {
        "user_id": _USER_STUB_USER_ID,
        "spend": 1.5,
        "max_budget": 100.0,
        "budget_reset_at": "2026-09-01T00:00:00Z",
    }

    account = client.get("/account", headers=_user_headers())
    assert account.status_code == 200, account.text
    _validate_for_snapshots("GET /account", account.json())

    grant = client.post("/account/storage-cleanup-grant", headers=_user_headers())
    assert grant.status_code == 200, grant.text
    _validate_for_snapshots("POST /account/storage-cleanup-grant", grant.json())

    recheck = client.post("/account/storage-recheck", headers=_user_headers())
    assert recheck.status_code == 200, recheck.text
    _validate_for_snapshots("POST /account/storage-recheck", recheck.json())


def test_sync_responses_parse_for_all_snapshots(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _store, _caller = _make_sync_test_client(monkeypatch)

    record_body = {
        "host_id": "host-compat01",
        "agent_id": "agent-compat01",
        "display_name": "compat workspace",
        "color": "#aabbcc",
        "provider_kind": "lima",
        "hosting_device_id": "device-compat",
        "device_label": "compat-laptop",
        "state": "active",
        "restored_from_host_id": None,
        "encrypted_secrets": base64.b64encode(b"opaque").decode("ascii"),
        "revision": 1,
    }
    put = client.put("/sync/records/host-compat01", json=record_body, headers=_user_headers())
    assert put.status_code == 200, put.text
    _validate_for_snapshots("PUT /sync/records/{host_id}", put.json())

    # The workspace-keyed route serves the same record shape (including one
    # carrying the new backup_bucket column, which must stay off the wire
    # while any strict snapshot is in-window).
    workspace_put_body = dict(record_body, revision=2, backup_bucket="testuser--agent-compat01")
    workspace_put = client.put(
        "/sync/records/by-workspace/agent-compat01", json=workspace_put_body, headers=_user_headers()
    )
    assert workspace_put.status_code == 200, workspace_put.text
    _validate_for_snapshots("PUT /sync/records/{host_id}", workspace_put.json())

    listed = client.get("/sync/records", headers=_user_headers())
    assert listed.status_code == 200
    _validate_entries_for_snapshots("GET /sync/records [entry]", listed.json()["records"])

    bundle_body = {
        "kdf_salt": base64.b64encode(b"salt").decode("ascii"),
        "kdf_time_cost": 2,
        "kdf_memory_kib": 8,
        "kdf_parallelism": 1,
        "wrapped_dek": base64.b64encode(b"wrapped").decode("ascii"),
        "key_epoch": 1,
    }
    assert client.put("/sync/bundle", json=bundle_body, headers=_user_headers()).status_code == 200
    bundle = client.get("/sync/bundle", headers=_user_headers())
    assert bundle.status_code == 200, bundle.text
    _validate_for_snapshots("GET /sync/bundle", bundle.json())


# ---------------------------------------------------------------------------
# Route completeness
# ---------------------------------------------------------------------------

# Routes whose responses are strictly parsed by shipped clients, mapped to the
# snapshot endpoint key(s) whose fixtures above exercise them. Each key must
# have at least one _validate_* call in this file.
_STRICTLY_PARSED_ROUTES: dict[tuple[str, str], str] = {
    ("POST", "/auth/signin"): "POST /auth/signin",
    ("POST", "/auth/signup"): "POST /auth/signup",
    ("POST", "/auth/device/token"): "POST /auth/device/token",
    ("POST", "/hosts/lease"): "POST /hosts/lease",
    ("GET", "/hosts"): "GET /hosts [entry]",
    ("GET", "/workspaces"): "GET /workspaces [entry]",
    ("GET", "/workspaces/{host_db_id}"): "GET /workspaces/{host_db_id}",
    ("POST", "/workspaces/{host_db_id}/stop"): "POST /workspaces/{host_db_id}/stop",
    ("POST", "/workspaces/{host_db_id}/start"): "POST /workspaces/{host_db_id}/start",
    ("POST", "/keys/create"): "POST /keys/create",
    ("GET", "/keys"): "GET /keys [entry]",
    ("GET", "/keys/{key_id}"): "GET /keys/{key_id}",
    ("POST", "/buckets"): "POST /buckets",
    ("GET", "/buckets"): "GET /buckets [entry]",
    ("GET", "/buckets/{name}"): "GET /buckets/{name}",
    ("POST", "/buckets/{name}/roll-key"): "POST /buckets/{name}/roll-key",
    ("GET", "/buckets/{name}/keys"): "GET /buckets/{name}/keys [entry]",
    ("GET", "/bucket-keys"): "GET /bucket-keys [entry]",
    ("GET", "/account"): "GET /account",
    ("POST", "/account/storage-cleanup-grant"): "POST /account/storage-cleanup-grant",
    ("POST", "/account/storage-recheck"): "POST /account/storage-recheck",
    ("GET", "/sync/records"): "GET /sync/records [entry]",
    ("PUT", "/sync/records/{host_id}"): "PUT /sync/records/{host_id}",
    # The workspace-keyed PUT serves the identical record shape as the
    # host-keyed one, so it shares that endpoint key: any snapshot that parses
    # the record shape covers both routes.
    ("PUT", "/sync/records/by-workspace/{workspace_id}"): "PUT /sync/records/{host_id}",
    ("GET", "/sync/bundle"): "GET /sync/bundle",
}

# Reasons a route needs no snapshot coverage. Additions here need review: the
# reason must explain why NO shipped client parses the response strictly.
_TOLERANT_CLIENT = "shipped clients hand-parse this response tolerantly (.get readers); additions cannot break them"
_STATUS_ONLY = "shipped clients read at most a status/count field tolerantly (or nothing) from this response"
_WEB_BUNDLE = "consumed only by the connector's own path-served web bundles, which deploy with the server"
_OPERATOR = "operator/admin surface: its CLI ships from this repo, not with the desktop fleet"
_WORKSPACE_SIDE = "consumed by workspace-side services (share gateway / frps / relay), which parse tolerantly"
_BROWSER_ONLY = "a browser navigation target (redirect or HTML page); no client parses its body"

_EXEMPT_ROUTES: dict[tuple[str, str], str] = {
    # System probes (tolerantly parsed by minds env tooling and deploy checks).
    ("GET", "/generation"): _TOLERANT_CLIENT,
    ("GET", "/version"): _TOLERANT_CLIENT,
    ("GET", "/health/liveness"): _STATUS_ONLY,
    # Dev/ci-only reporting probe; consumed by the deployment-test suite,
    # which ships from this repo, not with the desktop fleet.
    ("GET", "/health/reporting-probe"): _OPERATOR,
    ("GET", "/policies/destroyed-workspace-backups"): _TOLERANT_CLIENT,
    # Auth surface beyond the three strictly-parsed responses.
    ("POST", "/auth/session/refresh"): _TOLERANT_CLIENT,
    ("POST", "/auth/session/revoke"): _STATUS_ONLY,
    ("POST", "/auth/session/revoke-current"): _STATUS_ONLY,
    ("POST", "/auth/email/send-verification"): _TOLERANT_CLIENT,
    ("POST", "/auth/email/is-verified"): _TOLERANT_CLIENT,
    ("POST", "/auth/password/forgot"): _STATUS_ONLY,
    ("POST", "/auth/password/reset"): _STATUS_ONLY,
    ("GET", "/auth/users/{user_id}"): _TOLERANT_CLIENT,
    ("GET", "/auth/reset-password"): _BROWSER_ONLY,
    ("GET", "/auth/verify-email"): _BROWSER_ONLY,
    # Hosts/keys/buckets operations whose responses carry no strictly-parsed body.
    ("POST", "/hosts/claim"): _WEB_BUNDLE,
    ("POST", "/hosts/{host_db_id}/release"): _STATUS_ONLY,
    ("POST", "/hosts/{host_db_id}/rename"): _STATUS_ONLY,
    ("POST", "/hosts/{host_db_id}/enable-sharing"): _TOLERANT_CLIENT,
    ("POST", "/keys/workspace-mint"): _WEB_BUNDLE,
    ("DELETE", "/keys/{key_id}"): _STATUS_ONLY,
    ("PUT", "/keys/{key_id}/budget"): _STATUS_ONLY,
    ("DELETE", "/buckets/{name}"): _STATUS_ONLY,
    ("DELETE", "/bucket-keys/{access_key_id}"): _STATUS_ONLY,
    # Account plan switch: the plugin returns the raw dict.
    ("POST", "/account/plan"): _TOLERANT_CLIENT,
    # Sync operations without strictly-parsed bodies.
    ("DELETE", "/sync/records/{host_id}"): _STATUS_ONLY,
    ("DELETE", "/sync/records/by-workspace/{workspace_id}"): _STATUS_ONLY,
    ("POST", "/sync/scrub-secrets"): _TOLERANT_CLIENT,
    ("PUT", "/sync/bundle"): _STATUS_ONLY,
    ("DELETE", "/sync/bundle"): _STATUS_ONLY,
    # Shares: every shipped parser is a tolerant .get reader (_parse_share_info).
    ("POST", "/shares"): _TOLERANT_CLIENT,
    ("GET", "/shares"): _TOLERANT_CLIENT,
    ("GET", "/shares/relays"): _TOLERANT_CLIENT,
    ("GET", "/shares/{host_id}/status"): _TOLERANT_CLIENT,
    ("DELETE", "/shares/{host_id}"): _STATUS_ONLY,
    ("GET", "/shares/assignment"): _WORKSPACE_SIDE,
    ("POST", "/shares/cert"): _WORKSPACE_SIDE,
    ("POST", "/frps/auth/{plugin_secret}/{relay_id}"): _WORKSPACE_SIDE,
    # The hosted accounts pages + web chrome (path-served bundles).
    ("GET", "/accounts/api/config"): _WEB_BUNDLE,
    ("GET", "/accounts/api/me"): _WEB_BUNDLE,
    ("POST", "/accounts/api/change-password"): _WEB_BUNDLE,
    ("POST", "/accounts/api/send-verification"): _WEB_BUNDLE,
    ("POST", "/accounts/api/signin"): _WEB_BUNDLE,
    ("POST", "/accounts/api/signout"): _WEB_BUNDLE,
    ("POST", "/accounts/api/signout-all"): _WEB_BUNDLE,
    ("POST", "/accounts/api/signup"): _WEB_BUNDLE,
    ("POST", "/accounts/api/verify-email"): _WEB_BUNDLE,
    ("GET", "/accounts/assets/{asset_path:path}"): _WEB_BUNDLE,
    ("GET", "/accounts/authorize"): _BROWSER_ONLY,
    ("GET", "/accounts/oauth/google/start"): _BROWSER_ONLY,
    ("GET", "/share/oauth/google/callback"): _BROWSER_ONLY,
    ("GET", "/share/authorize"): _BROWSER_ONLY,
    ("GET", "/share/jwks.json"): _WORKSPACE_SIDE,
    ("GET", "/share/login"): _BROWSER_ONLY,
    ("GET", "/login"): _BROWSER_ONLY,
    ("GET", "/signup"): _BROWSER_ONLY,
    ("GET", "/manage"): _BROWSER_ONLY,
    ("GET", "/check-inbox"): _BROWSER_ONLY,
    ("GET", "/terms-of-service"): _BROWSER_ONLY,
    ("GET", "/code-of-conduct"): _BROWSER_ONLY,
    ("GET", "/privacy-policy"): _BROWSER_ONLY,
    ("GET", "/web"): _WEB_BUNDLE,
    ("GET", "/web/assets/{asset_path:path}"): _WEB_BUNDLE,
    ("GET", "/web/{page_path:path}"): _WEB_BUNDLE,
    ("GET", "/download"): _BROWSER_ONLY,
    # Operator/admin surface.
    ("GET", "/paid/domains"): _OPERATOR,
    ("GET", "/paid/emails"): _OPERATOR,
    ("POST", "/paid/domains/add"): _OPERATOR,
    ("POST", "/paid/domains/remove"): _OPERATOR,
    ("POST", "/paid/emails/add"): _OPERATOR,
    ("POST", "/paid/emails/remove"): _OPERATOR,
    ("GET", "/admin/accounts/{email}"): _OPERATOR,
    ("POST", "/admin/accounts/{email}/plan"): _OPERATOR,
    ("POST", "/admin/accounts/{email}/quota"): _OPERATOR,
    ("POST", "/admin/accounts/{email}/revoke-sessions"): _OPERATOR,
    ("POST", "/admin/accounts/{email}/suspend"): _OPERATOR,
    ("POST", "/admin/accounts/{email}/unsuspend"): _OPERATOR,
    ("GET", "/admin/relays"): _OPERATOR,
    ("POST", "/admin/relays"): _OPERATOR,
    ("DELETE", "/admin/relays/{relay_id}"): _OPERATOR,
    ("POST", "/admin/sweep/backup-retention"): _OPERATOR,
    ("POST", "/admin/sweep/r2"): _OPERATOR,
    ("POST", "/admin/test-signup"): _OPERATOR,
    ("POST", "/admin/workspaces/{host_db_id}/abandon"): _OPERATOR,
    ("POST", "/admin/workspaces/{host_db_id}/stop"): _OPERATOR,
}


def _iter_app_api_routes(routes: list[Any]) -> list[APIRoute]:
    """Every APIRoute on the app, descending through fastapi's lazy included-router wrappers."""
    collected: list[APIRoute] = []
    for route in routes:
        if isinstance(route, APIRoute):
            collected.append(route)
        elif type(route).__name__ == "_IncludedRouter":
            collected.extend(_iter_app_api_routes(route.original_router.routes))
        else:
            # Docs/static/mount routes carry no JSON API contract.
            pass
    return collected


def test_every_route_is_classified_for_wire_compat() -> None:
    """A new client-facing endpoint must either get compat coverage or an explicit exemption."""
    app_routes = {
        (method, route.path)
        for route in _iter_app_api_routes(web_app.routes)
        for method in route.methods or ()
        if method != "HEAD"
    }
    classified = set(_STRICTLY_PARSED_ROUTES) | set(_EXEMPT_ROUTES)
    unclassified = app_routes - classified
    assert not unclassified, (
        f"Unclassified route(s) for wire compat: {sorted(unclassified)}. Either add the route to "
        "_STRICTLY_PARSED_ROUTES (with a fixture exercising it against the compat snapshots) or to "
        "_EXEMPT_ROUTES with a reason no shipped client parses its response strictly."
    )
    stale = classified - app_routes
    assert not stale, f"Route classification entries no longer on the app: {sorted(stale)}"
    doubly = set(_STRICTLY_PARSED_ROUTES) & set(_EXEMPT_ROUTES)
    assert not doubly, f"Routes classified both ways: {sorted(doubly)}"


def test_every_strictly_parsed_route_key_is_exercised_by_a_fixture() -> None:
    """Every _STRICTLY_PARSED_ROUTES endpoint key must be validated by a fixture in this module.

    The route-completeness test only proves classification; this proves the
    classification is not hollow -- a route mapped to an endpoint key with no
    _validate_* call would otherwise get zero snapshot coverage silently.
    """
    tree = ast.parse(Path(__file__).read_text())
    validated_keys: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else None
        if name not in ("_validate_for_snapshots", "_validate_entries_for_snapshots"):
            continue
        first_arg = node.args[0] if node.args else None
        if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
            validated_keys.add(first_arg.value)
    unexercised = set(_STRICTLY_PARSED_ROUTES.values()) - validated_keys
    assert not unexercised, (
        f"Endpoint key(s) in _STRICTLY_PARSED_ROUTES with no _validate_* fixture call: "
        f"{sorted(unexercised)}. Add a fixture that exercises the endpoint and validates its "
        "response with the literal endpoint key."
    )


def test_every_snapshot_endpoint_key_maps_to_a_classified_route() -> None:
    """Snapshot endpoint keys and the route classification must agree (no orphaned keys)."""
    covered_keys = set(_STRICTLY_PARSED_ROUTES.values())
    for snapshot in _SNAPSHOTS:
        for endpoint_key, model in snapshot.MODEL_BY_ENDPOINT.items():
            assert issubclass(model, BaseModel)
            assert endpoint_key in covered_keys, (
                f"{snapshot.SNAPSHOT_NAME} pins '{endpoint_key}' but no route is classified to "
                "exercise it -- add the mapping in _STRICTLY_PARSED_ROUTES."
            )
