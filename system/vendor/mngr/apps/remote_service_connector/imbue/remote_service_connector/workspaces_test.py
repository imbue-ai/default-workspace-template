from typing import Any
from uuid import UUID

import pytest

from imbue.remote_service_connector.testing import _ADMIN_KEY_TEST_VALUE
from imbue.remote_service_connector.testing import _USER_STUB_USER_ID_PREFIX
from imbue.remote_service_connector.testing import _admin_key_headers
from imbue.remote_service_connector.testing import _make_pool_quota_test_client
from imbue.remote_service_connector.testing import _make_pool_test_client
from imbue.remote_service_connector.testing import _seed_entitlements_row
from imbue.remote_service_connector.testing import _user_headers
from imbue.remote_service_connector.testing import make_storage_config

_WS_ID = UUID("00000000-0000-0000-0000-00000000aa01")
_WS_ID_2 = UUID("00000000-0000-0000-0000-00000000aa02")


def _row_status(backend: Any, host_id: UUID) -> str:
    row = backend.find_pool_row(host_id)
    assert row is not None
    return row.status


def _seed_leased_workspace(
    backend: Any, host_id: UUID = _WS_ID, status: str = "leased", leased_to_user: str | None = None
) -> Any:
    row = backend.add_available_host(host_id=host_id, version="v1", vps_address="10.0.0.5")
    row.status = status
    row.leased_to_user = leased_to_user or _USER_STUB_USER_ID_PREFIX
    row.leased_at = "2026-01-01T00:00:00+00:00"
    row.lima_instance_name = f"mngr-slice-test-{host_id.hex}"
    row.lima_disk_name = f"mngr-slice-test-{host_id.hex}-data"
    return row


def test_list_workspaces_maps_leased_to_running_and_includes_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend = _make_pool_test_client(monkeypatch)
    _seed_leased_workspace(backend, _WS_ID, status="leased")
    stopped = _seed_leased_workspace(backend, _WS_ID_2, status="stopped")
    stopped.vps_address = None
    stopped.ssh_port = None
    stopped.container_ssh_port = None

    resp = client.get("/workspaces", headers=_user_headers())

    assert resp.status_code == 200
    body = resp.json()
    status_by_id = {entry["host_db_id"]: entry["status"] for entry in body}
    assert status_by_id[str(_WS_ID)] == "running"
    assert status_by_id[str(_WS_ID_2)] == "stopped"
    stopped_entry = next(entry for entry in body if entry["host_db_id"] == str(_WS_ID_2))
    assert stopped_entry["vps_address"] is None
    assert stopped_entry["ssh_port"] is None


def test_get_workspace_refuses_other_users_row(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend = _make_pool_test_client(monkeypatch)
    _seed_leased_workspace(backend, _WS_ID, leased_to_user="deadbeefdeadbeef")

    resp = client.get(f"/workspaces/{_WS_ID}", headers=_user_headers())

    assert resp.status_code == 403


def test_stop_workspace_requires_storage_config(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend = _make_pool_test_client(monkeypatch)
    _seed_leased_workspace(backend, _WS_ID)
    backend.storage_config = None

    resp = client.post(f"/workspaces/{_WS_ID}/stop", headers=_user_headers())

    assert resp.status_code == 503
    assert _row_status(backend, _WS_ID) == "leased"


def test_stop_workspace_flips_row_and_spawns_supervisor(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend = _make_pool_test_client(monkeypatch)
    _seed_leased_workspace(backend, _WS_ID)
    backend.storage_config = make_storage_config()

    resp = client.post(f"/workspaces/{_WS_ID}/stop", headers=_user_headers())

    assert resp.status_code == 202
    assert resp.json()["status"] == "stopping"
    row = backend.find_pool_row(_WS_ID)
    assert row is not None
    assert row.status == "stopping"
    assert row.stop_requested_at is not None
    assert backend.spawned_supervisors == [str(_WS_ID)]
    # The endpoint minted the fencing token and handed it to the supervisor.
    assert row.transition_id is not None
    assert backend.spawned_supervisor_tokens == [(str(_WS_ID), row.transition_id)]


def test_stop_workspace_is_idempotent_while_stopping(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend = _make_pool_test_client(monkeypatch)
    _seed_leased_workspace(backend, _WS_ID, status="stopping")
    backend.storage_config = make_storage_config()

    resp = client.post(f"/workspaces/{_WS_ID}/stop", headers=_user_headers())

    assert resp.status_code == 202
    assert resp.json()["status"] == "stopping"
    # No new supervisor is spawned for a request that changed nothing.
    assert backend.spawned_supervisors == []


def test_stop_workspace_conflicts_while_starting(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend = _make_pool_test_client(monkeypatch)
    _seed_leased_workspace(backend, _WS_ID, status="starting")
    backend.storage_config = make_storage_config()

    resp = client.post(f"/workspaces/{_WS_ID}/stop", headers=_user_headers())

    assert resp.status_code == 409
    assert _row_status(backend, _WS_ID) == "starting"


def test_start_workspace_from_stopped_checks_running_quota(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend, entitlements_store, _litellm = _make_pool_quota_test_client(monkeypatch)
    _seed_entitlements_row(entitlements_store, plan_name="explorer", max_remote_workspaces=1)
    backend.storage_config = make_storage_config()
    # One running workspace consumes the whole cap; the stopped one cannot start.
    _seed_leased_workspace(backend, _WS_ID, status="leased")
    stopped = _seed_leased_workspace(backend, _WS_ID_2, status="stopped")
    stopped.stopped_at = stopped.leased_at

    resp = client.post(f"/workspaces/{_WS_ID_2}/start", headers=_user_headers())

    assert resp.status_code == 403
    assert resp.json()["detail"]["entitlement"] == "max_remote_workspaces"
    assert _row_status(backend, _WS_ID_2) == "stopped"


def test_start_workspace_from_stopped_flips_row_and_spawns(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend, entitlements_store, _litellm = _make_pool_quota_test_client(monkeypatch)
    _seed_entitlements_row(entitlements_store, plan_name="ally")
    backend.storage_config = make_storage_config()
    stopped = _seed_leased_workspace(backend, _WS_ID, status="stopped")
    stopped.stopped_at = stopped.leased_at

    resp = client.post(f"/workspaces/{_WS_ID}/start", headers=_user_headers())

    assert resp.status_code == 202
    assert resp.json()["status"] == "starting"
    assert _row_status(backend, _WS_ID) == "starting"
    assert backend.spawned_supervisors == [str(_WS_ID)]
    # The endpoint minted the fencing token and handed it to the supervisor.
    assert stopped.transition_id is not None
    assert backend.spawned_supervisor_tokens == [(str(_WS_ID), stopped.transition_id)]


def test_start_workspace_while_stopping_is_refused_with_the_current_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transitions only begin from stable states: a still-stopping row refuses
    the start (409, naming the current status) so its stop supervisor is never
    raced by a start supervisor -- the caller waits for stopped and retries."""
    client, backend, entitlements_store, _litellm = _make_pool_quota_test_client(monkeypatch)
    _seed_entitlements_row(entitlements_store, plan_name="ally")
    backend.storage_config = make_storage_config()
    _seed_leased_workspace(backend, _WS_ID, status="stopping")

    resp = client.post(f"/workspaces/{_WS_ID}/start", headers=_user_headers())

    assert resp.status_code == 409
    assert "stopping" in resp.json()["detail"]
    assert _row_status(backend, _WS_ID) == "stopping"
    assert backend.spawned_supervisors == []


def test_start_workspace_on_running_row_reports_running(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend, entitlements_store, _litellm = _make_pool_quota_test_client(monkeypatch)
    _seed_entitlements_row(entitlements_store, plan_name="ally")
    backend.storage_config = make_storage_config()
    _seed_leased_workspace(backend, _WS_ID, status="leased")

    resp = client.post(f"/workspaces/{_WS_ID}/start", headers=_user_headers())

    assert resp.status_code == 202
    assert resp.json()["status"] == "running"
    assert backend.spawned_supervisors == []


def test_abandon_workspace_requires_admin_key(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend = _make_pool_test_client(monkeypatch)
    monkeypatch.setenv("MINDS_ADMIN_KEY", _ADMIN_KEY_TEST_VALUE)
    _seed_leased_workspace(backend, _WS_ID, status="stopping")

    unauthorized = client.post(
        f"/admin/workspaces/{_WS_ID}/abandon", json={"reason": "box died"}, headers=_user_headers()
    )
    assert unauthorized.status_code in (401, 403)
    assert _row_status(backend, _WS_ID) == "stopping"

    authorized = client.post(
        f"/admin/workspaces/{_WS_ID}/abandon", json={"reason": "box died"}, headers=_admin_key_headers()
    )
    assert authorized.status_code == 200
    row = backend.find_pool_row(_WS_ID)
    assert row is not None
    assert row.status == "crashed"
    assert row.transition_error == "box died"


def test_admin_stop_workspace_flips_row_and_spawns_supervisor(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend = _make_pool_test_client(monkeypatch)
    monkeypatch.setenv("MINDS_ADMIN_KEY", _ADMIN_KEY_TEST_VALUE)
    # Another user's row: the operator route has no ownership check.
    _seed_leased_workspace(backend, _WS_ID, leased_to_user="deadbeefdeadbeef")
    backend.storage_config = make_storage_config()

    resp = client.post(f"/admin/workspaces/{_WS_ID}/stop", headers=_admin_key_headers())

    assert resp.status_code == 202
    assert resp.json()["status"] == "stopping"
    row = backend.find_pool_row(_WS_ID)
    assert row is not None
    assert row.status == "stopping"
    # The spawned supervisor owns the transition_id the stop CAS minted.
    assert row.transition_id is not None
    assert backend.spawned_supervisor_tokens == [(str(_WS_ID), row.transition_id)]


def test_admin_stop_workspace_requires_admin_key(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend = _make_pool_test_client(monkeypatch)
    monkeypatch.setenv("MINDS_ADMIN_KEY", _ADMIN_KEY_TEST_VALUE)
    _seed_leased_workspace(backend, _WS_ID)
    backend.storage_config = make_storage_config()

    resp = client.post(f"/admin/workspaces/{_WS_ID}/stop", headers=_user_headers())

    assert resp.status_code == 401
    assert _row_status(backend, _WS_ID) == "leased"


def test_admin_stop_workspace_is_idempotent_and_404s_on_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend = _make_pool_test_client(monkeypatch)
    monkeypatch.setenv("MINDS_ADMIN_KEY", _ADMIN_KEY_TEST_VALUE)
    _seed_leased_workspace(backend, _WS_ID, status="stopped")
    backend.storage_config = make_storage_config()

    already = client.post(f"/admin/workspaces/{_WS_ID}/stop", headers=_admin_key_headers())
    assert already.status_code == 202
    assert already.json()["status"] == "stopped"
    assert backend.spawned_supervisors == []

    missing = client.post(f"/admin/workspaces/{_WS_ID_2}/stop", headers=_admin_key_headers())
    assert missing.status_code == 404
