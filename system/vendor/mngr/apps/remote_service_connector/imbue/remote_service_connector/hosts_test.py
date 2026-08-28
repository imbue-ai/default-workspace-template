import stat
import subprocess
from pathlib import Path
from uuid import UUID

import pytest

import imbue.remote_service_connector.hosts as hosts_mod
from imbue.remote_service_connector.testing import _ADMIN_KEY_TEST_VALUE
from imbue.remote_service_connector.testing import _USER_STUB_EMAIL
from imbue.remote_service_connector.testing import _USER_STUB_USER_ID
from imbue.remote_service_connector.testing import _USER_STUB_USER_ID_PREFIX
from imbue.remote_service_connector.testing import _admin_key_headers
from imbue.remote_service_connector.testing import _make_pool_quota_test_client
from imbue.remote_service_connector.testing import _make_pool_test_client
from imbue.remote_service_connector.testing import _seed_entitlements_row
from imbue.remote_service_connector.testing import _user_headers
from imbue.remote_service_connector.testing import make_storage_config


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
    assert "backfill-host-keys" in resp.json()["detail"]
    # The row must NOT have been leased, and no SSH key injection was attempted.
    assert backend.pool_rows[0].status == "available"
    assert backend.append_key_calls == []


def test_lease_host_quarantines_dead_host_and_leases_the_next_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """A row whose SSH key injection fails is quarantined and the next match is leased.

    This is the 2026-08 production outage shape: the oldest available row sat on a
    dead box, and every lease retried it forever. The fix flips the dead row to
    'unreachable' (so it leaves rotation) and serves the caller from the next row
    in the same request.
    """
    client, backend = _make_pool_test_client(monkeypatch)
    backend.append_key_failure_addresses = {"10.0.0.1"}
    backend.add_available_host(
        host_id=UUID("00000000-0000-0000-0000-000000000001"),
        version="v0.1.0",
        vps_address="10.0.0.1",
    )
    backend.add_available_host(
        host_id=UUID("00000000-0000-0000-0000-000000000002"),
        version="v0.1.0",
        vps_address="10.0.0.2",
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
    assert resp.json()["host_db_id"] == "00000000-0000-0000-0000-000000000002"
    # The dead row is quarantined, the healthy one is leased.
    assert backend.pool_rows[0].status == "unreachable"
    assert backend.pool_rows[1].status == "leased"
    assert backend.pool_rows[1].leased_to_user == _USER_STUB_USER_ID_PREFIX


def test_lease_host_returns_503_when_the_only_candidate_was_quarantined(monkeypatch: pytest.MonkeyPatch) -> None:
    """When quarantining drains the pool, the caller gets the standard no-capacity 503.

    Retrying cannot help (there are no rows left to try), so this must read as
    no-capacity -- the client maps 503 to its lease-unavailable error -- while the
    dead row still leaves rotation.
    """
    client, backend = _make_pool_test_client(monkeypatch)
    backend.append_key_failure_addresses = {"10.0.0.1"}
    backend.add_available_host(
        host_id=UUID("00000000-0000-0000-0000-000000000001"),
        version="v0.1.0",
        vps_address="10.0.0.1",
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
    assert "No pre-created agents" in resp.json()["detail"]
    assert backend.pool_rows[0].status == "unreachable"


def test_lease_host_keeps_quarantine_when_a_keyless_row_stops_the_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """A keyless row after a quarantined one still 503s, and the quarantine sticks.

    The keyless-row 503 is raised after the lease transaction commits: raising it
    inside would roll back the quarantine of the dead row tried first, and every
    retry would re-wedge on that same dead row (the outage shape all over again).
    """
    client, backend = _make_pool_test_client(monkeypatch)
    backend.append_key_failure_addresses = {"10.0.0.1"}
    backend.add_available_host(
        host_id=UUID("00000000-0000-0000-0000-000000000001"),
        version="v0.1.0",
        vps_address="10.0.0.1",
    )
    backend.add_available_host(
        host_id=UUID("00000000-0000-0000-0000-000000000002"),
        version="v0.1.0",
        vps_address="10.0.0.2",
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
    assert "backfill-host-keys" in resp.json()["detail"]
    # The dead row's quarantine survives the keyless-row 503.
    assert backend.pool_rows[0].status == "unreachable"
    assert backend.pool_rows[1].status == "available"


def test_lease_host_bounds_quarantine_attempts_and_returns_502(monkeypatch: pytest.MonkeyPatch) -> None:
    """A request quarantines at most three rows, then returns a retryable 502.

    The cap bounds the request's worst-case latency; the fourth dead row stays
    available for the caller's retry (which quarantines onward from there).
    """
    client, backend = _make_pool_test_client(monkeypatch)
    backend.append_key_failure_addresses = {"10.0.0.1", "10.0.0.2", "10.0.0.3", "10.0.0.4"}
    for idx in (1, 2, 3, 4):
        backend.add_available_host(
            host_id=UUID(f"00000000-0000-0000-0000-00000000000{idx}"),
            version="v0.1.0",
            vps_address=f"10.0.0.{idx}",
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
    assert resp.status_code == 502
    assert "quarantined" in resp.json()["detail"]
    assert [row.status for row in backend.pool_rows] == ["unreachable", "unreachable", "unreachable", "available"]


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


def test_release_host_of_crashed_row_survives_a_dead_box(monkeypatch: pytest.MonkeyPatch) -> None:
    """Releasing a 'crashed' row whose box is unreachable still deletes the row.

    'crashed' is the operator's abandon-time assertion that the box is
    permanently dead, so the teardown attempt is best-effort: its failure is
    logged and the release proceeds, instead of wedging the row in 'removing'
    forever against a box that will never answer.
    """
    client, backend = _make_pool_test_client(monkeypatch)
    backend.slice_teardown_should_fail = True
    row = backend.add_leased_host(
        host_id=UUID("00000000-0000-0000-0000-0000000000cd"),
        version="v0.1.0",
        leased_to_user=_USER_STUB_USER_ID_PREFIX,
    )
    row.status = "crashed"
    row.transition_error = "abandoned: box permanently dead"
    row.bare_metal_server_id = UUID("00000000-0000-0000-0000-0000000000b1")
    resp = client.post("/hosts/00000000-0000-0000-0000-0000000000cd/release", headers=_user_headers())
    assert resp.status_code == 200
    assert resp.json()["status"] == "released"
    assert backend.pool_rows == []


def test_release_host_of_crashed_row_still_attempts_teardown(monkeypatch: pytest.MonkeyPatch) -> None:
    """Releasing a 'crashed' row whose box turns out reachable tears the VM down normally."""
    client, backend = _make_pool_test_client(monkeypatch)
    row = backend.add_leased_host(
        host_id=UUID("00000000-0000-0000-0000-0000000000ce"),
        version="v0.1.0",
        leased_to_user=_USER_STUB_USER_ID_PREFIX,
    )
    row.status = "crashed"
    row.bare_metal_server_id = UUID("00000000-0000-0000-0000-0000000000b1")
    resp = client.post("/hosts/00000000-0000-0000-0000-0000000000ce/release", headers=_user_headers())
    assert resp.status_code == 200
    assert resp.json()["status"] == "released"
    assert backend.pool_rows == []
    assert len(backend.slice_teardowns) == 1


def test_release_host_of_crashed_row_stays_best_effort_across_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """A crashed release interrupted after its status flip still completes on retry.

    The flip to 'removing' clears the box link, so a retry -- which reads
    'removing', not 'crashed' -- does not turn into a must-succeed teardown
    against the permanently dead box. Here the first attempt fails at the
    artifact deletion (a transient storage outage); the retry, with storage
    healed but the box still dead, must release cleanly.
    """
    client, backend = _make_pool_test_client(monkeypatch)
    backend.storage_config = make_storage_config()
    backend.delete_prefix_should_fail = True
    backend.slice_teardown_should_fail = True
    row = backend.add_leased_host(
        host_id=UUID("00000000-0000-0000-0000-0000000000cf"),
        version="v0.1.0",
        leased_to_user=_USER_STUB_USER_ID_PREFIX,
    )
    row.status = "crashed"
    row.bare_metal_server_id = UUID("00000000-0000-0000-0000-0000000000b1")

    resp = client.post("/hosts/00000000-0000-0000-0000-0000000000cf/release", headers=_user_headers())
    assert resp.status_code == 500
    assert len(backend.pool_rows) == 1
    assert backend.pool_rows[0].status == "removing"

    backend.delete_prefix_should_fail = False
    resp = client.post("/hosts/00000000-0000-0000-0000-0000000000cf/release", headers=_user_headers())
    assert resp.status_code == 200
    assert resp.json()["status"] == "released"
    assert backend.pool_rows == []


def test_release_host_of_mid_restore_starting_row_skips_teardown_and_deletes_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Releasing a 'starting' row whose restore has not yet placed its VM must succeed.

    Between a stop finalize and the restore's final CAS the row has no box link
    (bare_metal_server_id is NULL): there is no recorded VM anywhere, so a
    release must not attempt a slice teardown (which can only fail, wedging the
    row in 'removing' forever) -- it deletes the row directly.
    """
    client, backend = _make_pool_test_client(monkeypatch)
    row = backend.add_leased_host(
        host_id=UUID("00000000-0000-0000-0000-0000000000ab"),
        version="v0.1.0",
        leased_to_user=_USER_STUB_USER_ID_PREFIX,
    )
    row.status = "starting"
    row.vps_address = None
    row.ssh_port = None
    row.container_ssh_port = None
    row.lima_instance_name = "mngr-slice-test-" + "ab" * 16
    row.lima_disk_name = "mngr-slice-test-" + "ab" * 16 + "-data"
    assert row.bare_metal_server_id is None
    resp = client.post("/hosts/00000000-0000-0000-0000-0000000000ab/release", headers=_user_headers())
    assert resp.status_code == 200
    assert resp.json()["status"] == "released"
    assert backend.pool_rows == []
    assert backend.slice_teardowns == []


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


def test_route_lease_host_succeeds_for_unpaid_free_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unpaid account backfills to the free plan and can still lease (quota permitting)."""
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
    # The lazily-created row is on free (unpaid email, no explicit choice).
    row = entitlements_store.get_entitlements(_USER_STUB_USER_ID)
    assert row is not None
    assert row["plan_name"] == "free"


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


def test_slice_name_env_owner_parses_truncated_host_hex_names() -> None:
    # Slices baked since the host-id truncation carry a 16-char host hex
    # (mirroring mngr_imbue_cloud's SLICE_HOST_ID_HEX_LENGTH).
    host_hex = "0123456789abcdef"
    assert hosts_mod.slice_name_env_owner(f"mngr-slice-dev-josh-foo-{host_hex}") == "dev-josh-foo"
    assert hosts_mod.slice_name_env_owner(f"mngr-slice-dev-josh-foo-{host_hex}-data") == "dev-josh-foo"


def test_slice_name_env_owner_returns_none_for_legacy_and_non_slice_names() -> None:
    # Legacy un-stamped slice names -- full-hex or truncated -- have no env
    # owner (must be left untouched).
    for host_hex in ("0123456789abcdef0123456789abcdef", "0123456789abcdef"):
        assert hosts_mod.slice_name_env_owner(f"mngr-slice-{host_hex}") is None
        assert hosts_mod.slice_name_env_owner(f"mngr-slice-{host_hex}-data") is None
    # Non-slice lima names are never attributed to an env.
    assert hosts_mod.slice_name_env_owner("default") is None
    assert hosts_mod.slice_name_env_owner("some-other-vm") is None


# Verbatim from the 2026-08-07 production incident box (51.81.185.229): nvme0
# dropped off the bus, both RAID1 arrays run degraded on nvme1, and the dead
# disk's raw swap partition lingers as a "(deleted)" entry.
_DEGRADED_MDSTAT = """\
Personalities : [raid1] [linear] [multipath] [raid0] [raid6] [raid5] [raid4] [raid10]
md2 : active raid1 nvme1n1p2[1]
      1046528 blocks super 1.2 [2/1] [_U]
      bitmap: 1/1 pages [4KB], 65536KB chunk

md3 : active raid1 nvme1n1p3[1]
      936244224 blocks super 1.2 [2/1] [_U]
      bitmap: 7/7 pages [28KB], 65536KB chunk

unused devices: <none>
"""

_HEALTHY_MDSTAT = """\
Personalities : [raid1]
md3 : active raid1 nvme0n1p3[0] nvme1n1p3[1]
      935460864 blocks super 1.2 [2/2] [UU]
md2 : active raid1 nvme0n1p2[0] nvme1n1p2[1]
      1046528 blocks super 1.2 [2/2] [UU]
unused devices: <none>
"""

_INCIDENT_PROC_SWAPS = """\
Filename\t\t\t\tType\t\tSize\t\tUsed\t\tPriority
/dev/nvme1n1p4                          partition\t524284\t\t220932\t\t-2
/dev/nvme0n1p4\\040(deleted)             partition\t524284\t\t346108\t\t-3
/swapfile                               file\t\t33554428\t1426020\t\t-4
"""


def test_parse_degraded_md_arrays_reports_arrays_missing_a_member() -> None:
    assert hosts_mod._parse_degraded_md_arrays(_DEGRADED_MDSTAT) == ["md2", "md3"]


def test_parse_degraded_md_arrays_reports_nothing_for_healthy_arrays() -> None:
    assert hosts_mod._parse_degraded_md_arrays(_HEALTHY_MDSTAT) == []
    assert hosts_mod._parse_degraded_md_arrays("") == []


def test_parse_raw_swap_devices_flags_partitions_but_not_the_swapfile() -> None:
    # Both raw partitions are flagged -- including the dead disk's lingering
    # "(deleted)" entry -- while the mirrored swapfile is not.
    assert hosts_mod._parse_raw_swap_devices(_INCIDENT_PROC_SWAPS) == [
        "/dev/nvme1n1p4",
        "/dev/nvme0n1p4\\040(deleted)",
    ]


def test_parse_raw_swap_devices_ignores_md_backed_swap_and_empty_input() -> None:
    md_swap = "Filename\tType\tSize\tUsed\tPriority\n/dev/md1 partition 524284 0 -2\n"
    assert hosts_mod._parse_raw_swap_devices(md_swap) == []
    assert hosts_mod._parse_raw_swap_devices("") == []


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


def _run_container_file_write_command(target: Path, content: str, is_seed_only: bool) -> None:
    """Execute the built write command locally through ``sh``, failing loudly on a non-zero exit."""
    command = hosts_mod.build_container_file_write_command(str(target), content, is_seed_only=is_seed_only)
    result = subprocess.run(["sh", "-c", command], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_build_container_file_write_command_writes_shell_hostile_content_with_0600_mode(tmp_path: Path) -> None:
    """Verify the built command creates the file byte-for-byte -- including content full of
    shell metacharacters (quotes, dollars, backticks, newlines) -- with 0600 permissions,
    creating parent directories as needed, and that a second non-seed write replaces the
    content (share.env must be rewritten on every enable to rotate the relay token). The
    test would fail if quoting or the base64 transport mangled the bytes, or if a plain
    write skipped an existing file."""
    target = tmp_path / "sub dir" / "share.env"
    content = "export SHARE_RELAY_TOKEN='a b'\n$HOME `whoami` \\ end\n"
    _run_container_file_write_command(target, content, is_seed_only=False)
    assert target.read_text() == content
    assert stat.S_IMODE(target.stat().st_mode) == 0o600

    _run_container_file_write_command(target, "rotated\n", is_seed_only=False)
    assert target.read_text() == "rotated\n"


def test_build_container_file_write_command_seed_only_creates_when_absent_and_skips_when_present(
    tmp_path: Path,
) -> None:
    """Verify seed-if-absent semantics: the first seed write creates the file, but once the
    file exists a later seed write leaves it byte-for-byte untouched (and still exits 0).
    This is what keeps re-enabling sharing from clobbering a grants document the user has
    edited since the first seed. The test would fail if the existence guard were dropped,
    inverted, or mis-quoted so the write ran anyway."""
    target = tmp_path / "share_grants.toml"
    seed = "[workspace]\nemails = []\nemail_domains = []\n"
    _run_container_file_write_command(target, seed, is_seed_only=True)
    assert target.read_text() == seed

    edited = '[workspace]\nemails = ["friend@example.com"]\nemail_domains = []\n'
    target.write_text(edited)
    _run_container_file_write_command(target, seed, is_seed_only=True)
    assert target.read_text() == edited


def test_split_box_health_output_reports_missing_transfer_binaries() -> None:
    output = (
        "md0 : active raid1 sda1[0] sdb1[1]\nMNGR_BOX_HEALTH_SPLIT\n"
        "Filename Type Size Used Priority\nMNGR_BOX_HEALTH_SPLIT\n"
        "s5cmd\nage\n"
    )
    mdstat_text, proc_swaps_text, missing_binaries = hosts_mod._split_box_health_output(output)
    assert "md0" in mdstat_text
    assert "Filename" in proc_swaps_text
    assert missing_binaries == ["s5cmd", "age"]


def test_split_box_health_output_reports_no_missing_binaries_on_a_healthy_box() -> None:
    output = "md0 : active raid1 sda1[0] sdb1[1]\nMNGR_BOX_HEALTH_SPLIT\nswaps\nMNGR_BOX_HEALTH_SPLIT\n"
    _mdstat, _swaps, missing_binaries = hosts_mod._split_box_health_output(output)
    assert missing_binaries == []


def _lease_body(host_name: str = "my-workspace") -> dict[str, object]:
    return {
        "ssh_public_key": "ssh-ed25519 AAAA testkey",
        "host_name": host_name,
        "attributes": {"version": "v0.1.0"},
    }


def test_lease_host_writes_a_metadata_only_record_stub_for_the_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lease grant inserts the workspace's ACTIVE record (no secrets) so a lease never lacks a record."""
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_available_host(
        host_id=UUID("00000000-0000-0000-0000-0000000000a1"),
        version="v0.1.0",
        agent_id="agent-stub-a1",
        host_id_str="host-stub-a1",
    )

    resp = client.post("/hosts/lease", json=_lease_body("stubbed-workspace"), headers=_user_headers())

    assert resp.status_code == 200
    assert len(backend.sync_record_rows) == 1
    stub = backend.sync_record_rows[0]
    assert stub["user_id"] == _USER_STUB_USER_ID
    assert stub["agent_id"] == "agent-stub-a1"
    assert stub["host_id"] == "host-stub-a1"
    assert stub["display_name"] == "stubbed-workspace"
    assert stub["provider_kind"] == "imbue_cloud_testuser-example-com"
    assert stub["state"] == "active"
    assert stub["revision"] == 1
    assert stub["encrypted_secrets"] is None
    assert stub["hosting_device_id"] is None


def test_lease_host_leaves_an_existing_record_for_the_workspace_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """A workspace that already has a record keeps it: the stub insert is ON CONFLICT DO NOTHING."""
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_available_host(
        host_id=UUID("00000000-0000-0000-0000-0000000000a2"),
        version="v0.1.0",
        agent_id="agent-stub-a2",
        host_id_str="host-stub-a2",
    )
    backend.add_workspace_record(
        user_id=_USER_STUB_USER_ID,
        host_id="host-stub-a2",
        agent_id="agent-stub-a2",
        display_name="pre-existing",
        provider_kind="docker",
    )

    resp = client.post("/hosts/lease", json=_lease_body("new-name"), headers=_user_headers())

    assert resp.status_code == 200
    assert [row["display_name"] for row in backend.sync_record_rows] == ["pre-existing"]


def test_lease_host_stub_is_not_written_when_the_lease_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """The stub rides the lease transaction: a lease that grants nothing writes no record."""
    client, backend = _make_pool_test_client(monkeypatch)

    resp = client.post("/hosts/lease", json=_lease_body(), headers=_user_headers())

    assert resp.status_code == 503
    assert backend.sync_record_rows == []


def test_release_host_tombstones_the_workspaces_active_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """Releasing a lease tombstones its client-written record in the same step as the removing flip."""
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_leased_workspace(
        suffix="a3",
        leased_to_user=_USER_STUB_USER_ID_PREFIX,
        record_user_id=_USER_STUB_USER_ID_PREFIX,
        record_revision=2,
    )

    resp = client.post("/hosts/00000000-0000-0000-0000-0000000000a3/release", headers=_user_headers())

    assert resp.status_code == 200
    assert backend.pool_rows == []
    record = backend.sync_record_rows[0]
    assert record["state"] == "destroyed"
    assert record["destroyed_at"] is not None
    assert record["revision"] == 3


def test_release_host_deletes_a_never_written_record_stub_instead_of_tombstoning_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A record still at its lease-time stub (revision 1, no secrets, no backup bucket) has nothing to keep: no ghost tombstone."""
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_leased_workspace(
        suffix="a2", leased_to_user=_USER_STUB_USER_ID_PREFIX, record_user_id=_USER_STUB_USER_ID_PREFIX
    )

    resp = client.post("/hosts/00000000-0000-0000-0000-0000000000a2/release", headers=_user_headers())

    assert resp.status_code == 200
    assert backend.pool_rows == []
    assert backend.sync_record_rows == []


def test_release_host_tombstones_a_revision_one_record_that_names_a_backup_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A client-created record can sit at revision 1 with no secrets (metadata-only tier); its backup keeps it."""
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_leased_workspace(
        suffix="b6", leased_to_user=_USER_STUB_USER_ID_PREFIX, record_user_id=_USER_STUB_USER_ID_PREFIX
    )
    backend.sync_record_rows[0]["backup_bucket"] = f"{_USER_STUB_USER_ID_PREFIX}--agent-b6"

    resp = client.post("/hosts/00000000-0000-0000-0000-0000000000b6/release", headers=_user_headers())

    assert resp.status_code == 200
    assert backend.pool_rows == []
    record = backend.sync_record_rows[0]
    assert record["state"] == "destroyed"
    assert record["revision"] == 2
    assert record["backup_bucket"] == f"{_USER_STUB_USER_ID_PREFIX}--agent-b6"


def test_release_host_tombstone_survives_a_teardown_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """The destroy intent (removing + tombstone) is durable even when the VM teardown fails."""
    client, backend = _make_pool_test_client(monkeypatch)
    backend.slice_teardown_should_fail = True
    backend.add_leased_workspace(
        suffix="a4",
        leased_to_user=_USER_STUB_USER_ID_PREFIX,
        record_user_id=_USER_STUB_USER_ID_PREFIX,
        record_revision=2,
    )

    resp = client.post("/hosts/00000000-0000-0000-0000-0000000000a4/release", headers=_user_headers())

    assert resp.status_code == 500
    assert backend.pool_rows[0].status == "removing"
    assert backend.sync_record_rows[0]["state"] == "destroyed"


def test_release_host_retry_of_a_removing_row_lands_a_missed_tombstone(monkeypatch: pytest.MonkeyPatch) -> None:
    """A row left 'removing' by a release that failed before retiring its record still gets a tombstone on retry."""
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_removing_host(
        host_id=UUID("00000000-0000-0000-0000-0000000000a5"),
        version="v0.1.0",
        leased_to_user=_USER_STUB_USER_ID_PREFIX,
        agent_id="agent-rel-a5",
        host_id_str="host-rel-a5",
    )
    backend.add_workspace_record(
        user_id=_USER_STUB_USER_ID_PREFIX, host_id="host-rel-a5", agent_id="agent-rel-a5", revision=2
    )

    resp = client.post("/hosts/00000000-0000-0000-0000-0000000000a5/release", headers=_user_headers())

    assert resp.status_code == 200
    assert backend.pool_rows == []
    assert backend.sync_record_rows[0]["state"] == "destroyed"


def test_release_host_leaves_other_users_records_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """The tombstone is scoped to the leasing user's record for the workspace."""
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_leased_workspace(suffix="a6", leased_to_user=_USER_STUB_USER_ID_PREFIX, record_user_id="someone-else")

    resp = client.post("/hosts/00000000-0000-0000-0000-0000000000a6/release", headers=_user_headers())

    assert resp.status_code == 200
    assert backend.sync_record_rows[0]["state"] == "active"


@pytest.mark.parametrize(
    ("stderr_text", "is_absent"),
    [
        ('FATA[0000] instance "mngr-slice-x" not found', True),
        ("Error: disk mngr-slice-x-data does not exist", True),
        ('level=fatal msg="Instance Not Found"', True),
        ("bash: limactl: command not found", False),
        ("permission denied", False),
        ("", False),
    ],
)
def test_is_lima_target_absent_error_recognizes_limactl_missing_target_messages(
    stderr_text: str, is_absent: bool
) -> None:
    assert hosts_mod._is_lima_target_absent_error(stderr_text) is is_absent


def test_admin_release_workspace_requires_the_admin_key(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend = _make_pool_test_client(monkeypatch)
    monkeypatch.setenv("MINDS_ADMIN_KEY", _ADMIN_KEY_TEST_VALUE)
    backend.add_leased_host(
        host_id=UUID("00000000-0000-0000-0000-0000000000a7"),
        version="v0.1.0",
        leased_to_user="another-user",
    )

    resp = client.post("/admin/workspaces/00000000-0000-0000-0000-0000000000a7/release", headers=_user_headers())

    assert resp.status_code in (401, 403)
    assert backend.pool_rows[0].status == "leased"


def test_admin_release_workspace_releases_any_users_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    """The operator release runs the owner's exact chain, without the ownership check."""
    client, backend = _make_pool_test_client(monkeypatch)
    monkeypatch.setenv("MINDS_ADMIN_KEY", _ADMIN_KEY_TEST_VALUE)
    row = backend.add_leased_workspace(
        suffix="a8", leased_to_user="anotheruser", record_user_id="anotheruser", record_revision=2
    )
    # A stopped row is exactly the case the pool-destroy tooling cannot claim.
    row.status = "stopped"
    row.bare_metal_server_id = UUID("00000000-0000-0000-0000-0000000000b1")

    resp = client.post("/admin/workspaces/00000000-0000-0000-0000-0000000000a8/release", headers=_admin_key_headers())

    assert resp.status_code == 200
    assert resp.json()["status"] == "released"
    assert backend.pool_rows == []
    assert len(backend.slice_teardowns) == 1
    assert backend.sync_record_rows[0]["state"] == "destroyed"


def test_admin_release_workspace_of_a_missing_row_is_already_released(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_pool_test_client(monkeypatch)
    monkeypatch.setenv("MINDS_ADMIN_KEY", _ADMIN_KEY_TEST_VALUE)

    resp = client.post("/admin/workspaces/00000000-0000-0000-0000-0000000000a9/release", headers=_admin_key_headers())

    assert resp.status_code == 200
    assert resp.json()["status"] == "already_released"
