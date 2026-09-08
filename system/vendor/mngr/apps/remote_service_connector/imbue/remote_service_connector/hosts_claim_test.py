"""Tests for the web-create claim endpoint (lease + adopt + share bring-up)."""

import hashlib
import re
from uuid import UUID

import pytest

from imbue.remote_service_connector.hosts import _replace_env_file_line
from imbue.remote_service_connector.testing import _CONTENT_DOMAIN
from imbue.remote_service_connector.testing import _USER_STUB_USER_ID
from imbue.remote_service_connector.testing import _USER_STUB_USER_ID_PREFIX
from imbue.remote_service_connector.testing import _make_pool_quota_test_client
from imbue.remote_service_connector.testing import _seed_entitlements_row
from imbue.remote_service_connector.testing import _user_headers

_HOST_DB_ID = UUID("00000000-0000-0000-0000-0000000000cc")
_HOST_ID_STR = "host-" + "c" * 32
_AGENT_ID = "agent-claimtest"
_TEMPLATE_REPO = "github.com/imbue-ai/default-workspace-template"
_TEMPLATE_REF = "mngr/test-pin"
_OWNER_LABEL = _USER_STUB_USER_ID.replace("-", "")


def _install_claim_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHARE_CONTENT_DOMAIN", _CONTENT_DOMAIN)
    monkeypatch.setenv("MINDS_WEB_TEMPLATE_REPO", _TEMPLATE_REPO)
    monkeypatch.setenv("MINDS_WEB_TEMPLATE_REF", _TEMPLATE_REF)


def _pinned_attributes() -> dict[str, object]:
    return {"repo_url": _TEMPLATE_REPO, "repo_branch_or_tag": _TEMPLATE_REF, "cpus": 2}


def _claim_body(display_name: str | None = "My Workspace") -> dict[str, object]:
    body: dict[str, object] = {
        "ssh_public_key": "ssh-ed25519 AAAA webkey",
        "host_name": "my-web-workspace",
    }
    if display_name is not None:
        body["display_name"] = display_name
    return body


def test_replace_env_file_line_appends_and_replaces_without_touching_other_lines() -> None:
    original = 'MNGR_HOST_DIR=/home/user/.mngr\nMNGR_PREFIX="mngr-"\n'
    appended = _replace_env_file_line(original, "REMOTE_SERVICE_CONNECTOR_URL", "https://c.example")
    assert appended == (
        'MNGR_HOST_DIR=/home/user/.mngr\nMNGR_PREFIX="mngr-"\nREMOTE_SERVICE_CONNECTOR_URL=https://c.example\n'
    )
    replaced = _replace_env_file_line(appended, "REMOTE_SERVICE_CONNECTOR_URL", "https://other.example")
    assert replaced.count("REMOTE_SERVICE_CONNECTOR_URL=") == 1
    assert "https://other.example" in replaced
    assert 'MNGR_PREFIX="mngr-"' in replaced


def test_claim_leases_adopts_and_enables_sharing(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_claim_env(monkeypatch)
    client, backend, _entitlements, _litellm = _make_pool_quota_test_client(monkeypatch)
    backend.add_available_host(
        host_id=_HOST_DB_ID,
        version="v0.1.0",
        agent_id=_AGENT_ID,
        host_id_str=_HOST_ID_STR,
        attributes=_pinned_attributes(),
    )

    resp = client.post("/hosts/claim", json=_claim_body(), headers=_user_headers())

    assert resp.status_code == 200
    body = resp.json()
    assert body["host_db_id"] == str(_HOST_DB_ID)
    assert body["agent_id"] == _AGENT_ID
    assert body["host_id"] == _HOST_ID_STR
    assert body["host_name"] == "my-web-workspace"
    assert body["display_name"] == "My Workspace"
    # The test host has no datacenter record, so the region is the
    # deterministic hash-of-host-id spread: host-ccc... lands on us2.
    domain_labels = str(body["workspace_domain"]).split(".")
    assert re.fullmatch(r"[a-f0-9]{32}", domain_labels[0])
    assert domain_labels[1] == hashlib.sha256(_USER_STUB_USER_ID.encode()).hexdigest()[:32]
    assert body["workspace_domain"].endswith(f".us2.{_CONTENT_DOMAIN}")
    expected_domain = str(body["workspace_domain"])
    assert body["region"] == "us2"
    # The chrome's routable entry origin is recorded later, by the frps
    # NewProxy callback once the workspace's tunnel claims its service labels
    # -- a fresh claim has no label yet (the connector never reads anything
    # from inside the workspace).
    assert body["entry_label"] is None
    share_row = backend.find_share(_HOST_ID_STR, _OWNER_LABEL)
    assert share_row is not None
    assert share_row["entry_label"] is None

    # The row is leased to the caller and the caller's key was injected on
    # both sshd endpoints.
    assert backend.pool_rows[0].status == "leased"
    assert backend.pool_rows[0].leased_to_user == _USER_STUB_USER_ID_PREFIX
    assert len(backend.append_key_calls) == 2

    # The adopt ran against the container port with the user's names and this
    # connector's public base URL.
    assert backend.adopted_containers == [
        ("203.0.113.10", 2222, _AGENT_ID, "my-web-workspace", "My Workspace", "http://testserver")
    ]

    # The adopted agent was started (the bake leaves slices stopped).
    assert backend.started_agent_containers == [("203.0.113.10", 2222)]

    # Sharing came up: share materials were written into the container.
    assert len(backend.written_container_files) == 1
    _host, _port, files = backend.written_container_files[0]
    assert set(files) == {
        "/home/user/workspace/data/.secrets/share.env",
        "/home/user/workspace/data/.secrets/share_grants.toml",
    }
    assert f"SHARE_WORKSPACE_DOMAIN={expected_domain}" in files["/home/user/workspace/data/.secrets/share.env"]


def test_claim_defaults_display_name_to_host_name(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_claim_env(monkeypatch)
    client, backend, _entitlements, _litellm = _make_pool_quota_test_client(monkeypatch)
    backend.add_available_host(
        host_id=_HOST_DB_ID,
        version="v0.1.0",
        agent_id=_AGENT_ID,
        host_id_str=_HOST_ID_STR,
        attributes=_pinned_attributes(),
    )

    resp = client.post("/hosts/claim", json=_claim_body(display_name=None), headers=_user_headers())

    assert resp.status_code == 200
    assert resp.json()["display_name"] == "my-web-workspace"
    assert backend.adopted_containers[0][4] == "my-web-workspace"


def test_claim_refused_when_tier_has_no_pinned_template(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHARE_CONTENT_DOMAIN", _CONTENT_DOMAIN)
    monkeypatch.delenv("MINDS_WEB_TEMPLATE_REPO", raising=False)
    monkeypatch.delenv("MINDS_WEB_TEMPLATE_REF", raising=False)
    client, backend, _entitlements, _litellm = _make_pool_quota_test_client(monkeypatch)
    backend.add_available_host(
        host_id=_HOST_DB_ID,
        version="v0.1.0",
        attributes=_pinned_attributes(),
    )

    resp = client.post("/hosts/claim", json=_claim_body(), headers=_user_headers())

    assert resp.status_code == 503
    assert "not configured" in resp.json()["detail"]
    assert backend.pool_rows[0].status == "available"


def test_claim_finds_no_host_when_the_pin_does_not_match(monkeypatch: pytest.MonkeyPatch) -> None:
    # A pool baked from a different template ref must never satisfy a web
    # claim: the pinned filter is the whole point (no silent adoption of a
    # stale bake).
    _install_claim_env(monkeypatch)
    client, backend, _entitlements, _litellm = _make_pool_quota_test_client(monkeypatch)
    backend.add_available_host(
        host_id=_HOST_DB_ID,
        version="v0.1.0",
        attributes={"repo_url": _TEMPLATE_REPO, "repo_branch_or_tag": "some-older-ref"},
    )

    resp = client.post("/hosts/claim", json=_claim_body(), headers=_user_headers())

    assert resp.status_code == 503
    assert backend.pool_rows[0].status == "available"
    assert backend.adopted_containers == []


def test_claim_shape_pin_constrains_the_match(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_claim_env(monkeypatch)
    monkeypatch.setenv("MINDS_WEB_SHAPE_CPUS", "4")
    client, backend, _entitlements, _litellm = _make_pool_quota_test_client(monkeypatch)
    # The seeded row has cpus == 2 while the pin wants 4, so it must not match.
    backend.add_available_host(
        host_id=_HOST_DB_ID,
        version="v0.1.0",
        attributes=_pinned_attributes(),
    )

    resp = client.post("/hosts/claim", json=_claim_body(), headers=_user_headers())

    assert resp.status_code == 503
    assert backend.pool_rows[0].status == "available"


def test_claim_enforces_the_workspace_quota(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_claim_env(monkeypatch)
    client, backend, entitlements_store, _litellm = _make_pool_quota_test_client(monkeypatch)
    _seed_entitlements_row(entitlements_store, "explorer", max_remote_workspaces=1)
    backend.add_leased_host(
        host_id=UUID("00000000-0000-0000-0000-0000000000dd"),
        version="v0.1.0",
        leased_to_user=_USER_STUB_USER_ID_PREFIX,
    )
    backend.add_available_host(
        host_id=_HOST_DB_ID,
        version="v0.1.0",
        attributes=_pinned_attributes(),
    )

    resp = client.post("/hosts/claim", json=_claim_body(), headers=_user_headers())

    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert detail["code"] == "quota_exceeded"
    assert detail["entitlement"] == "max_remote_workspaces"
    assert backend.pool_rows[1].status == "available"


def test_claim_releases_the_lease_when_the_agent_start_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_claim_env(monkeypatch)
    client, backend, _entitlements, _litellm = _make_pool_quota_test_client(monkeypatch)
    backend.add_available_host(
        host_id=_HOST_DB_ID,
        version="v0.1.0",
        agent_id=_AGENT_ID,
        host_id_str=_HOST_ID_STR,
        attributes=_pinned_attributes(),
    )
    backend.agent_start_should_fail = True

    resp = client.post("/hosts/claim", json=_claim_body(), headers=_user_headers())

    assert resp.status_code == 502
    assert "start" in resp.json()["detail"].lower()
    # The adopt ran, but the failed start rolled the lease back: teardown
    # recorded and the row deleted, so a retry starts from a clean pool.
    assert len(backend.adopted_containers) == 1
    assert len(backend.slice_teardowns) == 1
    assert backend.pool_rows == []
    # The lease-time record stub went with it: the user never received this
    # workspace, so no tombstone shows up in their "recently destroyed" list.
    assert backend.sync_record_rows == []
    # Sharing was never attempted, so no share row is left dangling for a
    # released host.
    assert backend.written_container_files == []
    assert backend.share_rows == []


def test_claim_releases_the_lease_when_the_adopt_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_claim_env(monkeypatch)
    client, backend, _entitlements, _litellm = _make_pool_quota_test_client(monkeypatch)
    backend.add_available_host(
        host_id=_HOST_DB_ID,
        version="v0.1.0",
        agent_id=_AGENT_ID,
        host_id_str=_HOST_ID_STR,
        attributes=_pinned_attributes(),
    )
    backend.adopt_should_fail = True

    resp = client.post("/hosts/claim", json=_claim_body(), headers=_user_headers())

    assert resp.status_code == 502
    assert "adopt" in resp.json()["detail"].lower()
    # The lease was rolled back: teardown recorded and the row deleted, so a
    # retry starts from a clean pool.
    assert len(backend.slice_teardowns) == 1
    assert backend.pool_rows == []
    # The lease-time record stub went with it: the user never received this
    # workspace, so no tombstone shows up in their "recently destroyed" list.
    assert backend.sync_record_rows == []
    # Neither the agent start nor sharing was attempted.
    assert backend.started_agent_containers == []
    assert backend.written_container_files == []


def test_claim_retry_after_a_failed_adopt_succeeds_on_a_fresh_slice(monkeypatch: pytest.MonkeyPatch) -> None:
    # The browser's create flow retries a failed claim; the failed attempt
    # released its lease, so the retry must converge on a working workspace
    # with the user holding exactly one lease.
    _install_claim_env(monkeypatch)
    client, backend, _entitlements, _litellm = _make_pool_quota_test_client(monkeypatch)
    backend.add_available_host(
        host_id=_HOST_DB_ID,
        version="v0.1.0",
        agent_id=_AGENT_ID,
        host_id_str=_HOST_ID_STR,
        attributes=_pinned_attributes(),
    )
    second_host_db_id = UUID("00000000-0000-0000-0000-0000000000ee")
    second_host_id_str = "host-" + "e" * 32
    backend.add_available_host(
        host_id=second_host_db_id,
        version="v0.1.0",
        agent_id="agent-claimretry",
        host_id_str=second_host_id_str,
        attributes=_pinned_attributes(),
    )

    backend.adopt_should_fail = True
    first = client.post("/hosts/claim", json=_claim_body(), headers=_user_headers())
    backend.adopt_should_fail = False
    retry = client.post("/hosts/claim", json=_claim_body(), headers=_user_headers())

    assert first.status_code == 502
    assert retry.status_code == 200
    # The retry adopted, started, and shared a workspace end to end.
    assert len(backend.started_agent_containers) == 1
    assert len(backend.written_container_files) == 1
    # Exactly one lease is held; the failed attempt's row is gone.
    leased_rows = [row for row in backend.pool_rows if row.status == "leased"]
    assert len(leased_rows) == 1
    assert leased_rows[0].leased_to_user == _USER_STUB_USER_ID_PREFIX
    assert retry.json()["host_db_id"] == str(leased_rows[0].host_id)


def test_claim_retry_succeeds_while_the_failed_attempts_teardown_is_stuck(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A failed claim's lease release is best-effort: when the slice teardown
    # itself fails, the row is left in 'removing' for the sweep. That stuck
    # row must neither be re-leased nor count against the user's workspace
    # quota, so an immediate retry still succeeds on another available slice.
    _install_claim_env(monkeypatch)
    client, backend, entitlements_store, _litellm = _make_pool_quota_test_client(monkeypatch)
    _seed_entitlements_row(entitlements_store, "explorer", max_remote_workspaces=1)
    backend.add_available_host(
        host_id=_HOST_DB_ID,
        version="v0.1.0",
        agent_id=_AGENT_ID,
        host_id_str=_HOST_ID_STR,
        attributes=_pinned_attributes(),
    )
    second_host_db_id = UUID("00000000-0000-0000-0000-0000000000ee")
    second_host_id_str = "host-" + "e" * 32
    backend.add_available_host(
        host_id=second_host_db_id,
        version="v0.1.0",
        agent_id="agent-claimretry",
        host_id_str=second_host_id_str,
        attributes=_pinned_attributes(),
    )

    backend.adopt_should_fail = True
    backend.slice_teardown_should_fail = True
    first = client.post("/hosts/claim", json=_claim_body(), headers=_user_headers())
    backend.adopt_should_fail = False
    backend.slice_teardown_should_fail = False
    retry = client.post("/hosts/claim", json=_claim_body(), headers=_user_headers())

    assert first.status_code == 502
    assert retry.status_code == 200
    # The failed attempt's row is stuck in 'removing' (the sweep's problem),
    # and the retry's lease is the user's only one.
    statuses = sorted(str(row.status) for row in backend.pool_rows)
    assert statuses == ["leased", "removing"]
    leased_row = next(row for row in backend.pool_rows if row.status == "leased")
    assert str(retry.json()["host_db_id"]) == str(leased_row.host_id)


def test_claim_writes_a_record_stub_carrying_the_display_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """The web claim's lease stub starts with the user's display name, not the slug."""
    _install_claim_env(monkeypatch)
    client, backend, _entitlements, _litellm = _make_pool_quota_test_client(monkeypatch)
    backend.add_available_host(
        host_id=_HOST_DB_ID,
        version="v0.1.0",
        agent_id=_AGENT_ID,
        host_id_str=_HOST_ID_STR,
        attributes=_pinned_attributes(),
    )

    resp = client.post("/hosts/claim", json=_claim_body(), headers=_user_headers())

    assert resp.status_code == 200
    assert len(backend.sync_record_rows) == 1
    stub = backend.sync_record_rows[0]
    assert stub["user_id"] == _USER_STUB_USER_ID
    assert stub["agent_id"] == _AGENT_ID
    assert stub["display_name"] == "My Workspace"
    assert stub["state"] == "active"
