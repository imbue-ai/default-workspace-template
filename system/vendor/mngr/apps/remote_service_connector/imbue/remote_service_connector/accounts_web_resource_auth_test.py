"""Tests for browser-session (cookie) authentication on the resource endpoints.

The hosted web chrome is served same-origin with the connector, so its API
calls carry the SuperTokens browser-session cookie instead of a Bearer
header. These tests establish a fake cookie session and exercise the
resource endpoints the chrome uses.
"""

from pathlib import Path
from uuid import UUID

import pytest
from starlette.testclient import TestClient
from supertokens_python.recipe.emailpassword.interfaces import SignUpOkResult as EPSignUpOkResult

from imbue.remote_service_connector.testing import FakeSuperTokensBackend
from imbue.remote_service_connector.testing import _USER_STUB_USER_ID_PREFIX
from imbue.remote_service_connector.testing import _make_pool_quota_web_test_client
from imbue.remote_service_connector.testing import _user_headers

_BROWSER_EMAIL = "webuser@example.com"


def _sign_in_browser(client: TestClient, st_backend: FakeSuperTokensBackend) -> str:
    """Sign up + plant a cookie-based browser session on the client; returns the user id."""
    signup = st_backend.sign_up(tenant_id="public", email=_BROWSER_EMAIL, password="pw-123456")
    assert isinstance(signup, EPSignUpOkResult)
    session = st_backend.sdk_create_browser_session(None, signup.user.id)
    client.cookies.set(FakeSuperTokensBackend.BROWSER_SESSION_COOKIE, session.access_token)
    return signup.user.id


def test_hosts_list_accepts_the_browser_session_cookie(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend, _entitlements, _litellm, st_backend = _make_pool_quota_web_test_client(monkeypatch)
    _sign_in_browser(client, st_backend)

    resp = client.get("/hosts")

    assert resp.status_code == 200
    assert resp.json() == []


def test_hosts_list_still_rejects_anonymous_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend, _entitlements, _litellm, _st_backend = _make_pool_quota_web_test_client(monkeypatch)

    resp = client.get("/hosts")

    assert resp.status_code == 401


def test_bearer_header_still_wins_over_the_cookie(monkeypatch: pytest.MonkeyPatch) -> None:
    # A desktop/CLI caller with a Bearer token must resolve to the token's
    # user even if a stale browser cookie rides along. The lease below belongs
    # to the bearer stub user, so it only appears if the bearer identity won
    # (the cookie user would see an empty list).
    client, backend, _entitlements, _litellm, st_backend = _make_pool_quota_web_test_client(monkeypatch)
    _sign_in_browser(client, st_backend)
    lease_db_id = UUID("00000000-0000-0000-0000-0000000000ee")
    backend.add_leased_host(host_id=lease_db_id, version="v0.1.0", leased_to_user=_USER_STUB_USER_ID_PREFIX)

    resp = client.get("/hosts", headers=_user_headers())

    assert resp.status_code == 200
    assert [entry["host_db_id"] for entry in resp.json()] == [str(lease_db_id)]


def test_cookie_authenticated_state_change_rejects_a_cross_site_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend, _entitlements, _litellm, st_backend = _make_pool_quota_web_test_client(monkeypatch)
    _sign_in_browser(client, st_backend)

    resp = client.post(
        "/sync/scrub-secrets",
        headers={"Origin": "https://evil.example.com"},
    )

    assert resp.status_code == 403
    assert "Cross-site" in resp.json()["detail"]


def test_cookie_authenticated_state_change_accepts_the_same_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend, _entitlements, _litellm, st_backend = _make_pool_quota_web_test_client(monkeypatch)
    _sign_in_browser(client, st_backend)

    resp = client.post(
        "/sync/scrub-secrets",
        headers={"Origin": "http://testserver"},
    )

    assert resp.status_code == 200


def test_claim_accepts_the_browser_session_cookie(monkeypatch: pytest.MonkeyPatch) -> None:
    # The quota check must not re-derive the caller from an Authorization
    # header: the chrome's create flow is cookie-authenticated, and claim
    # (like lease and key minting) resolves entitlements. This is exactly the
    # 401 the first live browser create hit.
    monkeypatch.setenv("SHARE_CONTENT_DOMAIN", "shares.example")
    monkeypatch.setenv("SHARE_DEFAULT_REGION", "us1")
    monkeypatch.setenv("SHARE_RELAY_ENDPOINTS", "us1=relay-us1.shares.example:7000")
    monkeypatch.setenv("MINDS_WEB_TEMPLATE_REPO", "github.com/imbue-ai/default-workspace-template")
    monkeypatch.setenv("MINDS_WEB_TEMPLATE_REF", "mngr/test-pin")
    client, backend, _entitlements, _litellm, st_backend = _make_pool_quota_web_test_client(monkeypatch)
    _sign_in_browser(client, st_backend)
    backend.add_available_host(
        host_id=UUID("00000000-0000-0000-0000-0000000000cf"),
        version="v0.1.0",
        host_id_str="host-" + "f" * 32,
        attributes={
            "repo_url": "github.com/imbue-ai/default-workspace-template",
            "repo_branch_or_tag": "mngr/test-pin",
        },
    )

    resp = client.post(
        "/hosts/claim",
        json={"ssh_public_key": "ssh-ed25519 AAAA webkey", "host_name": "cookie-claimed"},
        headers={"Origin": "http://testserver"},
    )

    assert resp.status_code == 200
    assert resp.json()["host_name"] == "cookie-claimed"
    assert backend.pool_rows[0].status == "leased"


def test_sync_records_round_trip_via_the_browser_session(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend, _entitlements, _litellm, st_backend = _make_pool_quota_web_test_client(monkeypatch)
    _sign_in_browser(client, st_backend)
    host_id = "host-" + "b" * 32

    put_resp = client.put(
        f"/sync/records/{host_id}",
        json={
            "host_id": host_id,
            "agent_id": "agent-web-1",
            "display_name": "web workspace",
            "color": None,
            "provider_kind": "imbue_cloud",
            "hosting_device_id": "web",
            "device_label": "web",
            "state": "active",
            "restored_from_host_id": None,
            "encrypted_secrets": None,
            "revision": 1,
        },
    )
    assert put_resp.status_code == 200

    list_resp = client.get("/sync/records")
    assert list_resp.status_code == 200
    records = list_resp.json()["records"]
    assert [record["host_id"] for record in records] == [host_id]


def test_web_chrome_pages_serve_the_placeholder_without_a_built_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend, _entitlements, _litellm, _st = _make_pool_quota_web_test_client(monkeypatch)
    monkeypatch.setenv("WEB_CHROME_FRONTEND_DIST", "/nonexistent-web-chrome-dist")

    resp = client.get("/web")

    assert resp.status_code == 503
    assert "not built" in resp.text


def test_web_chrome_pages_serve_the_built_bundle_for_every_spa_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client, _backend, _entitlements, _litellm, _st = _make_pool_quota_web_test_client(monkeypatch)
    (tmp_path / "index.html").write_text("<!doctype html><title>minds web</title>")
    monkeypatch.setenv("WEB_CHROME_FRONTEND_DIST", str(tmp_path))

    for page in ("/web", "/web/", "/web/workspaces/host-abc", "/web/settings"):
        resp = client.get(page)
        assert resp.status_code == 200
        assert "minds web" in resp.text


def test_web_chrome_assets_route_serves_files_and_blocks_traversal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client, _backend, _entitlements, _litellm, _st = _make_pool_quota_web_test_client(monkeypatch)
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "chrome.js").write_text("console.log('chrome')")
    (tmp_path / "secret.txt").write_text("outside assets")
    monkeypatch.setenv("WEB_CHROME_FRONTEND_DIST", str(tmp_path))

    ok = client.get("/web/assets/chrome.js")
    assert ok.status_code == 200
    assert "console.log" in ok.text

    missing = client.get("/web/assets/nope.js")
    assert missing.status_code == 404

    traversal = client.get("/web/assets/%2e%2e/secret.txt")
    assert traversal.status_code == 404
