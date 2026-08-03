from uuid import UUID

import pytest

import imbue.remote_service_connector.hosts as hosts_mod
from imbue.remote_service_connector.testing import _USER_STUB_EMAIL
from imbue.remote_service_connector.testing import _USER_STUB_USER_ID
from imbue.remote_service_connector.testing import _USER_STUB_USER_ID_PREFIX
from imbue.remote_service_connector.testing import _make_pool_quota_test_client
from imbue.remote_service_connector.testing import _make_pool_test_client
from imbue.remote_service_connector.testing import _seed_entitlements_row
from imbue.remote_service_connector.testing import _user_headers


def test_lease_host_returns_available_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /hosts/lease returns a host when one is available with matching version."""
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_available_host(
        host_id=UUID("00000000-0000-0000-0000-000000000001"),
        version="v0.1.0",
        vps_address="10.0.0.1",
        agent_id="agent-111",
    )
    resp = client.post(
        "/hosts/lease",
        json={
            "ssh_public_key": "ssh-ed25519 AAAA testkey",
            "host_name": "my-workspace",
            "attributes": {"version": "v0.1.0"},
        },
        headers=_user_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["host_db_id"] == "00000000-0000-0000-0000-000000000001"
    assert body["vps_address"] == "10.0.0.1"
    assert body["agent_id"] == "agent-111"
    assert body["host_name"] == "my-workspace"
    assert body["attributes"] == {"version": "v0.1.0"}
    # The lease returns both pinned sshd host keys so the client can verify the
    # host strictly instead of trust-on-first-use.
    assert body["outer_host_public_key"]
    assert body["container_host_public_key"]
    # Verify SSH key was injected on both VPS and container, each pinning the
    # corresponding recorded host key (the 6th element of the recorded call).
    assert len(backend.append_key_calls) == 2
    injected_ports = {call[1]: call[5] for call in backend.append_key_calls}
    assert injected_ports[22] == body["outer_host_public_key"]
    assert injected_ports[2222] == body["container_host_public_key"]
    # Verify host was marked as leased and the user-supplied host_name was
    # written to the row.
    assert backend.pool_rows[0].status == "leased"
    assert backend.pool_rows[0].leased_to_user == _USER_STUB_USER_ID_PREFIX
    assert backend.pool_rows[0].host_name == "my-workspace"


def test_lease_host_fails_closed_when_host_keys_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pool row with no pinned host keys is not leasable (no trust-on-first-use)."""
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_available_host(
        host_id=UUID("00000000-0000-0000-0000-000000000001"),
        version="v0.1.0",
        outer_host_public_key=None,
        container_host_public_key=None,
    )
    resp = client.post(
        "/hosts/lease",
        json={
            "ssh_public_key": "ssh-ed25519 AAAA testkey",
            "host_name": "my-workspace",
            "attributes": {"version": "v0.1.0"},
        },
        headers=_user_headers(),
    )
    assert resp.status_code == 503
    assert "host-key backfill" in resp.json()["detail"]
    # The row must NOT have been leased, and no SSH key injection was attempted.
    assert backend.pool_rows[0].status == "available"
    assert backend.append_key_calls == []


def test_lease_host_returns_503_when_pool_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /hosts/lease returns 503 when no hosts are available."""
    client, _backend = _make_pool_test_client(monkeypatch)
    resp = client.post(
        "/hosts/lease",
        json={
            "ssh_public_key": "ssh-ed25519 AAAA testkey",
            "host_name": "my-workspace",
            "attributes": {"version": "v0.1.0"},
        },
        headers=_user_headers(),
    )
    assert resp.status_code == 503
    assert "No pre-created agents" in resp.json()["detail"]


def test_lease_host_returns_503_when_version_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /hosts/lease returns 503 when available hosts have a different version."""
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_available_host(host_id=UUID("00000000-0000-0000-0000-000000000001"), version="v0.2.0")
    resp = client.post(
        "/hosts/lease",
        json={
            "ssh_public_key": "ssh-ed25519 AAAA testkey",
            "host_name": "my-workspace",
            "attributes": {"version": "v0.1.0"},
        },
        headers=_user_headers(),
    )
    assert resp.status_code == 503
    assert "No pre-created agents" in resp.json()["detail"]
    # Verify the host was not leased
    assert backend.pool_rows[0].status == "available"


def test_lease_host_hard_region_filters_out_other_regions(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hard ``region`` only leases a host in that datacenter; otherwise 503."""
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_available_host(
        host_id=UUID("00000000-0000-0000-0000-000000000001"),
        version="v0.1.0",
        region="US-WEST-OR",
    )
    resp = client.post(
        "/hosts/lease",
        json={
            "ssh_public_key": "ssh-ed25519 AAAA testkey",
            "host_name": "my-workspace",
            "attributes": {"version": "v0.1.0"},
            "region": "US-EAST-VA",
        },
        headers=_user_headers(),
    )
    assert resp.status_code == 503
    assert backend.pool_rows[0].status == "available"


def test_lease_host_hard_region_leases_matching_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hard ``region`` leases a host whose region matches."""
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_available_host(
        host_id=UUID("00000000-0000-0000-0000-000000000001"),
        version="v0.1.0",
        region="US-EAST-VA",
    )
    resp = client.post(
        "/hosts/lease",
        json={
            "ssh_public_key": "ssh-ed25519 AAAA testkey",
            "host_name": "my-workspace",
            "attributes": {"version": "v0.1.0"},
            "region": "US-EAST-VA",
        },
        headers=_user_headers(),
    )
    assert resp.status_code == 200
    assert backend.pool_rows[0].status == "leased"


def test_lease_host_rejects_invalid_host_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /hosts/lease rejects host_name values that fail the SafeName regex."""
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_available_host(host_id=UUID("00000000-0000-0000-0000-000000000001"), version="v0.1.0")
    resp = client.post(
        "/hosts/lease",
        json={
            "ssh_public_key": "ssh-ed25519 AAAA testkey",
            "host_name": "bad.name",
            "attributes": {"version": "v0.1.0"},
        },
        headers=_user_headers(),
    )
    assert resp.status_code == 422
    # The available row stays available since validation rejected the request
    # before the SELECT/UPDATE.
    assert backend.pool_rows[0].status == "available"


def test_rename_host_succeeds_for_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /hosts/{id}/rename updates the mutable host_name for the owning user."""
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_leased_host(
        host_id=UUID("00000000-0000-0000-0000-000000000051"),
        version="v0.1.0",
        leased_to_user=_USER_STUB_USER_ID_PREFIX,
    )
    resp = client.post(
        "/hosts/00000000-0000-0000-0000-000000000051/rename",
        json={"host_name": "renamed-host"},
        headers=_user_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["host_name"] == "renamed-host"
    assert backend.pool_rows[0].host_name == "renamed-host"


def test_rename_host_rejects_invalid_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /hosts/{id}/rename rejects a host_name that fails the SafeName regex (422)."""
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_leased_host(
        host_id=UUID("00000000-0000-0000-0000-000000000052"),
        version="v0.1.0",
        leased_to_user=_USER_STUB_USER_ID_PREFIX,
    )
    original_name = backend.pool_rows[0].host_name
    resp = client.post(
        "/hosts/00000000-0000-0000-0000-000000000052/rename",
        json={"host_name": "bad.name"},
        headers=_user_headers(),
    )
    assert resp.status_code == 422
    # The name is unchanged since validation rejected the request before the UPDATE.
    assert backend.pool_rows[0].host_name == original_name


def test_rename_host_404_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /hosts/{id}/rename returns 404 when no such host row exists."""
    client, _backend = _make_pool_test_client(monkeypatch)
    resp = client.post(
        "/hosts/00000000-0000-0000-0000-0000000000ff/rename",
        json={"host_name": "whatever"},
        headers=_user_headers(),
    )
    assert resp.status_code == 404


def test_rename_host_403_for_non_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /hosts/{id}/rename returns 403 when the host is leased by another user."""
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_leased_host(
        host_id=UUID("00000000-0000-0000-0000-000000000053"), version="v0.1.0", leased_to_user="someone-else"
    )
    resp = client.post(
        "/hosts/00000000-0000-0000-0000-000000000053/rename",
        json={"host_name": "renamed-host"},
        headers=_user_headers(),
    )
    assert resp.status_code == 403
    assert backend.pool_rows[0].host_name != "renamed-host"


def test_rename_host_404_when_not_leased(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /hosts/{id}/rename returns 404 when the requester owns the row but it is not leased."""
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_removing_host(
        host_id=UUID("00000000-0000-0000-0000-000000000054"),
        version="v0.1.0",
        leased_to_user=_USER_STUB_USER_ID_PREFIX,
    )
    resp = client.post(
        "/hosts/00000000-0000-0000-0000-000000000054/rename",
        json={"host_name": "renamed-host"},
        headers=_user_headers(),
    )
    assert resp.status_code == 404
    assert backend.pool_rows[0].host_name != "renamed-host"


def test_release_host_succeeds_for_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /hosts/{id}/release destroys the slice's lima VM and drops the row."""
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_leased_host(
        host_id=UUID("00000000-0000-0000-0000-000000000042"),
        version="v0.1.0",
        leased_to_user=_USER_STUB_USER_ID_PREFIX,
    )
    resp = client.post("/hosts/00000000-0000-0000-0000-000000000042/release", headers=_user_headers())
    assert resp.status_code == 200
    assert resp.json()["status"] == "released"
    # Row fully cleaned up (deleted) after the slice VM teardown ran.
    assert backend.pool_rows == []
    assert len(backend.slice_teardowns) == 1


def test_release_host_idempotent_when_already_removing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A release on a row already in 'removing' re-drives cleanup and returns 200."""
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_removing_host(
        host_id=UUID("00000000-0000-0000-0000-000000000077"),
        version="v0.1.0",
        leased_to_user=_USER_STUB_USER_ID_PREFIX,
    )
    resp = client.post("/hosts/00000000-0000-0000-0000-000000000077/release", headers=_user_headers())
    assert resp.status_code == 200
    assert resp.json()["status"] == "released"
    assert backend.pool_rows == []
    assert len(backend.slice_teardowns) == 1


def test_release_host_fails_loudly_when_slice_teardown_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed slice VM teardown makes release return an error -- never a false success.

    Synchronous release contract: a "released" 200 must mean the slice VM is actually
    destroyed. When the teardown fails the endpoint returns 5xx and keeps the row as
    'removing' so the client retries -- never a 200 that silently strands the VM.
    """
    client, backend = _make_pool_test_client(monkeypatch)
    backend.slice_teardown_should_fail = True
    backend.add_leased_host(
        host_id=UUID("00000000-0000-0000-0000-000000000099"),
        version="v0.1.0",
        leased_to_user=_USER_STUB_USER_ID_PREFIX,
    )
    resp = client.post("/hosts/00000000-0000-0000-0000-000000000099/release", headers=_user_headers())
    assert resp.status_code == 500
    # The row is NOT deleted; it stays 'removing' so the teardown is retryable.
    assert len(backend.pool_rows) == 1
    assert backend.pool_rows[0].status == "removing"


def test_release_host_returns_403_for_non_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /hosts/{id}/release returns 403 when the caller is not the lease owner."""
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_leased_host(
        host_id=UUID("00000000-0000-0000-0000-000000000042"), version="v0.1.0", leased_to_user="other-user"
    )
    resp = client.post("/hosts/00000000-0000-0000-0000-000000000042/release", headers=_user_headers())
    assert resp.status_code == 403
    assert "do not own" in resp.json()["detail"]
    # Verify the host was not released
    assert backend.pool_rows[0].status == "leased"


def test_release_host_unknown_returns_already_released(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /hosts/{id}/release on a missing row returns 200 already_released (idempotent)."""
    client, _backend = _make_pool_test_client(monkeypatch)
    resp = client.post("/hosts/00000000-0000-0000-0000-000000000999/release", headers=_user_headers())
    assert resp.status_code == 200
    assert resp.json()["status"] == "already_released"


def test_list_hosts_returns_leased_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /hosts returns only hosts leased by the authenticated user."""
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_leased_host(
        host_id=UUID("00000000-0000-0000-0000-000000000001"),
        version="v0.1.0",
        leased_to_user=_USER_STUB_USER_ID_PREFIX,
        agent_id="agent-aaa",
    )
    backend.add_leased_host(
        host_id=UUID("00000000-0000-0000-0000-000000000002"),
        version="v0.1.0",
        leased_to_user="other-user",
        agent_id="agent-bbb",
    )
    backend.add_leased_host(
        host_id=UUID("00000000-0000-0000-0000-000000000003"),
        version="v0.1.0",
        leased_to_user=_USER_STUB_USER_ID_PREFIX,
        agent_id="agent-ccc",
    )
    resp = client.get("/hosts", headers=_user_headers())
    assert resp.status_code == 200
    hosts = resp.json()
    assert len(hosts) == 2
    host_ids = {h["host_db_id"] for h in hosts}
    assert host_ids == {"00000000-0000-0000-0000-000000000001", "00000000-0000-0000-0000-000000000003"}


def test_route_lease_host_succeeds_for_unpaid_explorer_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unpaid account resolves to the explorer plan and can still lease (quota permitting)."""
    client, backend, entitlements_store, _litellm = _make_pool_quota_test_client(monkeypatch)
    backend.add_available_host(host_id=UUID("00000000-0000-0000-0000-000000000001"), version="v0.1.0")
    backend.add_paid_email(_USER_STUB_EMAIL, is_paid=False)
    resp = client.post(
        "/hosts/lease",
        json={
            "ssh_public_key": "ssh-ed25519 AAAA testkey",
            "host_name": "my-workspace",
            "attributes": {"version": "v0.1.0"},
        },
        headers=_user_headers(),
    )
    assert resp.status_code == 200
    assert backend.pool_rows[0].status == "leased"
    # The lazily-created row is on explorer (unpaid email).
    row = entitlements_store.get_entitlements(_USER_STUB_USER_ID)
    assert row is not None
    assert row["plan_name"] == "explorer"


def test_route_lease_host_returns_quota_403_at_workspace_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lease past the account's max_remote_workspaces is refused with structured detail."""
    client, backend, entitlements_store, _litellm = _make_pool_quota_test_client(monkeypatch)
    _seed_entitlements_row(entitlements_store, "explorer", max_remote_workspaces=1)
    backend.add_leased_host(
        host_id=UUID("00000000-0000-0000-0000-000000000042"),
        version="v0.1.0",
        leased_to_user=_USER_STUB_USER_ID_PREFIX,
    )
    backend.add_available_host(host_id=UUID("00000000-0000-0000-0000-000000000001"), version="v0.1.0")
    resp = client.post(
        "/hosts/lease",
        json={
            "ssh_public_key": "ssh-ed25519 AAAA testkey",
            "host_name": "my-workspace",
            "attributes": {"version": "v0.1.0"},
        },
        headers=_user_headers(),
    )
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert detail["code"] == "quota_exceeded"
    assert detail["entitlement"] == "max_remote_workspaces"
    assert detail["limit"] == 1
    assert detail["current"] == 1
    # No side effects: the available host stays available, no SSH key injection.
    available = [row for row in backend.pool_rows if row.status == "available"]
    assert len(available) == 1
    assert backend.append_key_calls == []


def test_route_release_host_works_for_unpaid_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Release only needs ownership -- an account that lost paid status can still release."""
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_leased_host(
        host_id=UUID("00000000-0000-0000-0000-000000000042"),
        version="v0.1.0",
        leased_to_user=_USER_STUB_USER_ID_PREFIX,
    )
    backend.add_paid_email(_USER_STUB_EMAIL, is_paid=False)
    resp = client.post("/hosts/00000000-0000-0000-0000-000000000042/release", headers=_user_headers())
    assert resp.status_code == 200
    assert backend.pool_rows == []


def test_route_list_hosts_works_for_unpaid_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_paid_email(_USER_STUB_EMAIL, is_paid=False)
    resp = client.get("/hosts", headers=_user_headers())
    assert resp.status_code == 200
    assert resp.json() == []


def test_slice_name_env_owner_parses_stamped_instance_and_disk_names() -> None:
    host_hex = "0123456789abcdef0123456789abcdef"
    assert hosts_mod.slice_name_env_owner(f"mngr-slice-dev-josh-foo-{host_hex}") == "dev-josh-foo"
    # The env is recoverable from the data-disk name too (the -data suffix is stripped).
    assert hosts_mod.slice_name_env_owner(f"mngr-slice-dev-josh-foo-{host_hex}-data") == "dev-josh-foo"


def test_slice_name_env_owner_returns_none_for_legacy_and_non_slice_names() -> None:
    host_hex = "0123456789abcdef0123456789abcdef"
    # Legacy un-stamped slice names have no env owner (must be left untouched).
    assert hosts_mod.slice_name_env_owner(f"mngr-slice-{host_hex}") is None
    assert hosts_mod.slice_name_env_owner(f"mngr-slice-{host_hex}-data") is None
    # Non-slice lima names are never attributed to an env.
    assert hosts_mod.slice_name_env_owner("default") is None
    assert hosts_mod.slice_name_env_owner("some-other-vm") is None


def test_build_slice_teardown_commands_includes_disk_when_present() -> None:
    """Verify that when a data disk name is supplied, teardown emits both the instance
    delete and a separate disk delete command (in that order). The test would fail if the
    disk-delete command were omitted, reordered, or built with the wrong name."""
    commands = hosts_mod.build_slice_teardown_commands("mngr-slice-abc", "mngr-slice-abc-data")
    assert commands == (
        "limactl delete --force mngr-slice-abc",
        "limactl disk delete --force mngr-slice-abc-data",
    )


def test_build_slice_teardown_commands_omits_disk_when_absent() -> None:
    """Verify that when no data disk name is supplied (None), teardown emits only the single
    instance-delete command and no disk-delete command. The test would fail if a spurious
    disk-delete were appended for the diskless case."""
    commands = hosts_mod.build_slice_teardown_commands("mngr-slice-abc", None)
    assert commands == ("limactl delete --force mngr-slice-abc",)


def test_build_slice_teardown_commands_quotes_unsafe_names() -> None:
    """Verify that instance/disk names containing shell metacharacters are shell-quoted so
    they cannot break out of the teardown command (defense-in-depth against injection). The
    test would fail if the names were interpolated raw, leaving the ``;`` separator active."""
    # Defense-in-depth: instance/disk names flow into a shell command, so they
    # must be shell-quoted.
    commands = hosts_mod.build_slice_teardown_commands("a b; rm -rf /", "d$x")
    assert ";" not in commands[0].replace("'a b; rm -rf /'", "")
    assert commands[0] == "limactl delete --force 'a b; rm -rf /'"
    assert commands[1] == "limactl disk delete --force 'd$x'"
