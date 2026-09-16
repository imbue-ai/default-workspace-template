from typing import Any
from uuid import UUID

import httpx
import pytest
from pydantic import AnyUrl
from pydantic import SecretStr

from imbue.mngr_imbue_cloud.connector.client import ImbueCloudConnectorClient
from imbue.mngr_imbue_cloud.errors import ImbueCloudWorkspaceHeldError
from imbue.mngr_imbue_cloud.errors import WORKSPACE_HELD_MESSAGE
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
    row.slice_instance_name = f"mngr-slice-test-{host_id.hex}"
    row.slice_disk_name = f"mngr-slice-test-{host_id.hex}-data"
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


def test_start_workspace_counts_the_pending_resize_target_against_the_units_quota(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, backend, entitlements_store, _litellm = _make_pool_quota_test_client(monkeypatch)
    _seed_entitlements_row(entitlements_store, plan_name="ally", max_active_machine_units=64)
    backend.storage_config = make_storage_config()
    # A running 8-unit machine plus this stopped machine's pending 64-unit
    # target overflows the 64-unit cap: the start (the units re-check for a
    # resize granted while stopped) refuses, and the machine -- targets
    # included -- stays stopped and intact.
    running = _seed_leased_workspace(backend, _WS_ID, status="leased")
    running.memory_units = 8
    stopped = _seed_leased_workspace(backend, _WS_ID_2, status="stopped")
    stopped.stopped_at = stopped.leased_at
    stopped.memory_units = 8
    stopped.target_memory_units = 64

    resp = client.post(f"/workspaces/{_WS_ID_2}/start", headers=_user_headers())

    assert resp.status_code == 403
    assert resp.json()["detail"]["entitlement"] == "max_active_machine_units"
    assert _row_status(backend, _WS_ID_2) == "stopped"
    assert stopped.target_memory_units == 64


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


def _seed_stopped_workspace(
    backend: Any,
    host_id: UUID = _WS_ID,
    *,
    status: str = "stopped",
    stop_kind: str | None = None,
    leased_to_user: str | None = None,
) -> Any:
    """A gen-1 row with a finalized stop (``stopped`` or ``stopping``): its artifact manifest saved, ``stop_kind`` stamped."""
    row = _seed_leased_workspace(backend, host_id, status=status, leased_to_user=leased_to_user)
    row.stop_kind = stop_kind
    row.artifact_manifest = {"generation": 1, "key_prefix": f"{row.host_id_str}/gen-1", "age_recipient": "age1x"}
    backend.storage_config = make_storage_config()
    return row


def _seed_parked_gen1_workspace(backend: Any, host_id: UUID = _WS_ID) -> Any:
    """A gen-1 row the cutover parked: stopped, placement and box link cleared, no artifact manifest."""
    row = _seed_leased_workspace(backend, host_id, status="stopped")
    row.box_generation = 1
    row.vps_address = None
    row.ssh_port = None
    row.container_ssh_port = None
    row.bare_metal_server_id = None
    row.artifact_manifest = None
    row.wrapped_dek = None
    return row


def test_start_workspace_refuses_a_parked_gen1_row_as_under_maintenance(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend, entitlements_store, _litellm = _make_pool_quota_test_client(monkeypatch)
    _seed_entitlements_row(entitlements_store, plan_name="explorer", max_remote_workspaces=2)
    _seed_parked_gen1_workspace(backend, _WS_ID)
    backend.storage_config = make_storage_config()

    resp = client.post(f"/workspaces/{_WS_ID}/start", headers=_user_headers())

    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "workspace_under_maintenance"
    assert _row_status(backend, _WS_ID) == "stopped"
    assert backend.spawned_supervisors == []


def test_owner_stop_stamps_the_owner_kind_and_the_start_clears_it(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend, entitlements_store, _litellm = _make_pool_quota_test_client(monkeypatch)
    _seed_entitlements_row(entitlements_store, plan_name="explorer", max_remote_workspaces=2)
    row = _seed_leased_workspace(backend, _WS_ID)
    backend.storage_config = make_storage_config()

    stopped = client.post(f"/workspaces/{_WS_ID}/stop", headers=_user_headers())
    assert stopped.status_code == 202
    assert stopped.json()["stop_kind"] == "owner"
    assert row.stop_kind == "owner"
    row.status = "stopped"
    row.artifact_manifest = {"generation": 1, "key_prefix": f"{row.host_id_str}/gen-1", "age_recipient": "age1x"}
    listed = client.get(f"/workspaces/{_WS_ID}", headers=_user_headers())
    assert listed.json()["stop_kind"] == "owner"

    started = client.post(f"/workspaces/{_WS_ID}/start", headers=_user_headers())
    assert started.status_code == 202
    assert row.status == "starting"
    assert row.stop_kind is None


def test_owner_start_refuses_a_hold_stamped_between_its_read_and_its_cas(monkeypatch: pytest.MonkeyPatch) -> None:
    """The hold is enforced by the owner's start CAS, not only by the read before it.

    The migrate re-stamps an owner-stopped row ``maintenance`` with no new
    transition, so its stamp can land after the owner route read an un-held
    row and before its CAS ran. The CAS must then match nothing: the row keeps
    its hold, no supervisor is spawned, and the route reports the row as it is.
    """
    client, backend, entitlements_store, _litellm = _make_pool_quota_test_client(monkeypatch)
    _seed_entitlements_row(entitlements_store, plan_name="ally")
    row = _seed_stopped_workspace(backend, stop_kind="owner")

    def stamp_the_hold_under_the_lock(query: str, _params: tuple[Any, ...]) -> None:
        # The per-user lock is the first statement after the route's read.
        if "pg_advisory_xact_lock" in query:
            row.stop_kind = "maintenance"

    backend.query_callback = stamp_the_hold_under_the_lock

    resp = client.post(f"/workspaces/{_WS_ID}/start", headers=_user_headers())

    assert resp.status_code == 202
    assert resp.json()["status"] == "stopped"
    assert resp.json()["stop_kind"] == "maintenance"
    assert _row_status(backend, _WS_ID) == "stopped"
    assert row.stop_kind == "maintenance"
    assert backend.spawned_supervisors == []


@pytest.mark.parametrize("held_kind", ["maintenance", "suspension"])
@pytest.mark.parametrize("db_status", ["stopping", "stopped"])
def test_owner_start_refuses_a_held_row_from_stopping_onwards(
    monkeypatch: pytest.MonkeyPatch, held_kind: str, db_status: str
) -> None:
    client, backend, entitlements_store, _litellm = _make_pool_quota_test_client(monkeypatch)
    _seed_entitlements_row(entitlements_store, plan_name="explorer", max_remote_workspaces=2)
    _seed_stopped_workspace(backend, status=db_status, stop_kind=held_kind)

    resp = client.post(f"/workspaces/{_WS_ID}/start", headers=_user_headers())

    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["code"] == "workspace_under_maintenance"
    assert detail["message"] == "This machine is undergoing maintenance and will be back shortly."
    assert _row_status(backend, _WS_ID) == db_status
    assert backend.spawned_supervisors == []


def test_owner_start_of_a_crashed_row_answers_crashed_even_when_its_kind_survived(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The abandon route never clears the kind, so a held row an operator
    # abandons keeps 'maintenance'; the hold describes a stop the row no
    # longer has, and "back shortly" would be the wrong answer for it.
    client, backend, entitlements_store, _litellm = _make_pool_quota_test_client(monkeypatch)
    _seed_entitlements_row(entitlements_store, plan_name="explorer", max_remote_workspaces=2)
    row = _seed_leased_workspace(backend, _WS_ID, status="crashed")
    row.stop_kind = "maintenance"
    backend.storage_config = make_storage_config()

    resp = client.post(f"/workspaces/{_WS_ID}/start", headers=_user_headers())

    assert resp.status_code == 409
    assert resp.json()["detail"] == "Workspace is crashed and cannot be started right now"
    assert backend.spawned_supervisors == []


def test_the_hold_refusal_is_the_detail_the_plugin_and_the_desktop_recognize(monkeypatch: pytest.MonkeyPatch) -> None:
    # The plugin maps the 409 to its typed error by the detail's code, and the
    # desktop tells a refused start from a failed one by finding the plugin's
    # copy of the sentence in mngr's stderr; both are hand copies of this
    # module's constants, so the connector's real answer is replayed through
    # the plugin client here to keep the three from drifting apart.
    client, backend, entitlements_store, _litellm = _make_pool_quota_test_client(monkeypatch)
    _seed_entitlements_row(entitlements_store, plan_name="explorer", max_remote_workspaces=2)
    _seed_stopped_workspace(backend, stop_kind="maintenance")
    refused = client.post(f"/workspaces/{_WS_ID}/start", headers=_user_headers())
    assert refused.status_code == 409

    def replay_the_connectors_answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(refused.status_code, json=refused.json())

    plugin_client = ImbueCloudConnectorClient(
        base_url=AnyUrl("https://connector.example.test"), transport=httpx.MockTransport(replay_the_connectors_answer)
    )
    with pytest.raises(ImbueCloudWorkspaceHeldError) as excinfo:
        plugin_client.start_workspace(SecretStr("tok"), str(_WS_ID))
    assert str(excinfo.value) == WORKSPACE_HELD_MESSAGE


@pytest.mark.parametrize("startable_kind", [None, "owner", "idle"])
def test_owner_start_accepts_owner_idle_and_legacy_stops(
    monkeypatch: pytest.MonkeyPatch, startable_kind: str | None
) -> None:
    client, backend, entitlements_store, _litellm = _make_pool_quota_test_client(monkeypatch)
    _seed_entitlements_row(entitlements_store, plan_name="explorer", max_remote_workspaces=2)
    row = _seed_stopped_workspace(backend, stop_kind=startable_kind)

    resp = client.post(f"/workspaces/{_WS_ID}/start", headers=_user_headers())

    assert resp.status_code == 202
    assert _row_status(backend, _WS_ID) == "starting"
    assert row.stop_kind is None


def test_admin_stop_stamps_the_requested_kind_and_defaults_to_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend = _make_pool_test_client(monkeypatch)
    monkeypatch.setenv("MINDS_ADMIN_KEY", _ADMIN_KEY_TEST_VALUE)
    held = _seed_leased_workspace(backend, _WS_ID, leased_to_user="deadbeefdeadbeef")
    drained = _seed_leased_workspace(backend, _WS_ID_2, leased_to_user="deadbeefdeadbeef")
    backend.storage_config = make_storage_config()

    with_kind = client.post(
        f"/admin/workspaces/{_WS_ID}/stop", json={"kind": "maintenance"}, headers=_admin_key_headers()
    )
    without_body = client.post(f"/admin/workspaces/{_WS_ID_2}/stop", headers=_admin_key_headers())

    assert with_kind.status_code == 202
    assert with_kind.json()["stop_kind"] == "maintenance"
    assert held.stop_kind == "maintenance"
    assert without_body.status_code == 202
    assert without_body.json()["stop_kind"] == "idle"
    assert drained.stop_kind == "idle"


def test_admin_stop_rejects_the_owner_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    # ``owner`` is a stop kind, but the owner route's alone: the operator body
    # only accepts the operator kinds.
    client, backend = _make_pool_test_client(monkeypatch)
    monkeypatch.setenv("MINDS_ADMIN_KEY", _ADMIN_KEY_TEST_VALUE)
    _seed_leased_workspace(backend, _WS_ID)
    backend.storage_config = make_storage_config()

    resp = client.post(f"/admin/workspaces/{_WS_ID}/stop", json={"kind": "owner"}, headers=_admin_key_headers())

    assert resp.status_code == 422
    assert _row_status(backend, _WS_ID) == "leased"


def test_admin_stop_restamps_the_kind_of_an_already_stopped_row(monkeypatch: pytest.MonkeyPatch) -> None:
    # An owner-stopped workspace the migrate takes must carry its hold even
    # though there is no transition left to run.
    client, backend = _make_pool_test_client(monkeypatch)
    monkeypatch.setenv("MINDS_ADMIN_KEY", _ADMIN_KEY_TEST_VALUE)
    row = _seed_stopped_workspace(backend, stop_kind="owner")

    resp = client.post(f"/admin/workspaces/{_WS_ID}/stop", json={"kind": "maintenance"}, headers=_admin_key_headers())

    assert resp.status_code == 202
    assert resp.json() == {"host_db_id": str(_WS_ID), "status": "stopped", "stop_kind": "maintenance"}
    assert row.stop_kind == "maintenance"
    assert backend.spawned_supervisors == []


def test_admin_stop_refuses_when_the_row_left_stopped_under_its_restamp(monkeypatch: pytest.MonkeyPatch) -> None:
    """An owner start that lands between the operator stop's read and its restamp CAS wins: nothing is stamped."""
    client, backend = _make_pool_test_client(monkeypatch)
    monkeypatch.setenv("MINDS_ADMIN_KEY", _ADMIN_KEY_TEST_VALUE)
    row = _seed_stopped_workspace(backend, stop_kind="owner")

    def start_the_row_under_the_restamp(query: str, _params: tuple[Any, ...]) -> None:
        if query.startswith("UPDATE pool_hosts SET stop_kind = %s WHERE id"):
            row.status = "starting"

    backend.query_callback = start_the_row_under_the_restamp

    resp = client.post(f"/admin/workspaces/{_WS_ID}/stop", json={"kind": "maintenance"}, headers=_admin_key_headers())

    assert resp.status_code == 409
    assert resp.json()["detail"] == "Workspace changed state concurrently; retry"
    assert row.stop_kind == "owner"
    assert backend.spawned_supervisors == []


def test_admin_start_ignores_the_hold_and_clears_the_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend = _make_pool_test_client(monkeypatch)
    monkeypatch.setenv("MINDS_ADMIN_KEY", _ADMIN_KEY_TEST_VALUE)
    row = _seed_stopped_workspace(backend, stop_kind="maintenance", leased_to_user="deadbeefdeadbeef")

    resp = client.post(f"/admin/workspaces/{_WS_ID}/start", headers=_admin_key_headers())

    assert resp.status_code == 202
    assert row.status == "starting"
    assert row.stop_kind is None


def test_set_stop_kind_rewrites_a_stopped_row_and_refuses_a_running_one(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend = _make_pool_test_client(monkeypatch)
    monkeypatch.setenv("MINDS_ADMIN_KEY", _ADMIN_KEY_TEST_VALUE)
    held = _seed_stopped_workspace(backend, stop_kind="maintenance")
    running = _seed_leased_workspace(backend, _WS_ID_2)

    released = client.post(
        f"/admin/workspaces/{_WS_ID}/stop-kind", json={"kind": "idle"}, headers=_admin_key_headers()
    )
    refused = client.post(
        f"/admin/workspaces/{_WS_ID_2}/stop-kind", json={"kind": "idle"}, headers=_admin_key_headers()
    )
    missing = client.post(
        f"/admin/workspaces/{UUID(int=99)}/stop-kind", json={"kind": "idle"}, headers=_admin_key_headers()
    )

    assert released.status_code == 200
    assert released.json() == {"host_db_id": str(_WS_ID), "status": "stopped", "stop_kind": "idle"}
    assert held.stop_kind == "idle"
    assert refused.status_code == 409
    assert running.stop_kind is None
    assert missing.status_code == 404


def test_set_stop_kind_names_the_status_that_refused_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """The 409 describes the row as the CAS saw it, not as the route first read it."""
    client, backend = _make_pool_test_client(monkeypatch)
    monkeypatch.setenv("MINDS_ADMIN_KEY", _ADMIN_KEY_TEST_VALUE)
    row = _seed_stopped_workspace(backend, stop_kind="maintenance")

    def start_the_row_under_the_restamp(query: str, _params: tuple[Any, ...]) -> None:
        if query.startswith("UPDATE pool_hosts SET stop_kind = %s WHERE id"):
            row.status = "starting"

    backend.query_callback = start_the_row_under_the_restamp

    resp = client.post(f"/admin/workspaces/{_WS_ID}/stop-kind", json={"kind": "idle"}, headers=_admin_key_headers())

    assert resp.status_code == 409
    assert resp.json()["detail"] == "Workspace is starting and has no stop to describe"
    assert row.stop_kind == "maintenance"


def test_start_workspace_still_starts_a_finalized_stopped_gen1_row(monkeypatch: pytest.MonkeyPatch) -> None:
    # A finalized stop (placement cleared by the retention finalize) keeps its
    # manifest, which is what tells it apart from a parked row.
    client, backend, entitlements_store, _litellm = _make_pool_quota_test_client(monkeypatch)
    _seed_entitlements_row(entitlements_store, plan_name="explorer", max_remote_workspaces=2)
    row = _seed_parked_gen1_workspace(backend, _WS_ID)
    row.artifact_manifest = {"generation": 1, "key_prefix": f"{row.host_id_str}/gen-1", "age_recipient": "age1x"}
    backend.storage_config = make_storage_config()

    resp = client.post(f"/workspaces/{_WS_ID}/start", headers=_user_headers())

    assert resp.status_code == 202
    assert _row_status(backend, _WS_ID) == "starting"


def test_start_workspace_ignores_the_migrating_guard_for_gen2_and_placed_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend, entitlements_store, _litellm = _make_pool_quota_test_client(monkeypatch)
    _seed_entitlements_row(entitlements_store, plan_name="explorer", max_remote_workspaces=5)
    gen2_row = _seed_parked_gen1_workspace(backend, _WS_ID)
    gen2_row.box_generation = 2
    placed_row = _seed_leased_workspace(backend, _WS_ID_2, status="stopped")
    placed_row.artifact_manifest = None
    backend.storage_config = make_storage_config()

    assert client.post(f"/workspaces/{_WS_ID}/start", headers=_user_headers()).status_code == 202
    assert client.post(f"/workspaces/{_WS_ID_2}/start", headers=_user_headers()).status_code == 202
    assert _row_status(backend, _WS_ID) == "starting"
    assert _row_status(backend, _WS_ID_2) == "starting"


def test_admin_start_workspace_flips_a_stopped_row_and_spawns_supervisor(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend = _make_pool_test_client(monkeypatch)
    monkeypatch.setenv("MINDS_ADMIN_KEY", _ADMIN_KEY_TEST_VALUE)
    # Another user's row, stopped with its manifest: no ownership or quota check applies.
    row = _seed_stopped_workspace(backend, leased_to_user="deadbeefdeadbeef")

    resp = client.post(f"/admin/workspaces/{_WS_ID}/start", headers=_admin_key_headers())

    assert resp.status_code == 202
    assert resp.json()["status"] == "starting"
    row = backend.find_pool_row(_WS_ID)
    assert row is not None
    assert row.status == "starting"
    assert row.transition_id is not None
    assert backend.spawned_supervisor_tokens == [(str(_WS_ID), row.transition_id)]


def test_admin_start_workspace_requires_admin_key_and_refuses_non_stopped_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, backend = _make_pool_test_client(monkeypatch)
    monkeypatch.setenv("MINDS_ADMIN_KEY", _ADMIN_KEY_TEST_VALUE)
    _seed_leased_workspace(backend, _WS_ID, status="stopped")
    _seed_leased_workspace(backend, _WS_ID_2, status="stopping")
    backend.storage_config = make_storage_config()

    assert client.post(f"/admin/workspaces/{_WS_ID}/start", headers=_user_headers()).status_code == 401
    assert _row_status(backend, _WS_ID) == "stopped"

    mid_stop = client.post(f"/admin/workspaces/{_WS_ID_2}/start", headers=_admin_key_headers())
    assert mid_stop.status_code == 409
    assert "wait for it to reach stopped" in mid_stop.json()["detail"]
    assert client.post(f"/admin/workspaces/{UUID(int=99)}/start", headers=_admin_key_headers()).status_code == 404


def test_admin_start_workspace_is_idempotent_on_running_rows_and_refuses_parked_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, backend = _make_pool_test_client(monkeypatch)
    monkeypatch.setenv("MINDS_ADMIN_KEY", _ADMIN_KEY_TEST_VALUE)
    _seed_leased_workspace(backend, _WS_ID, status="leased")
    _seed_parked_gen1_workspace(backend, _WS_ID_2)
    backend.storage_config = make_storage_config()

    running = client.post(f"/admin/workspaces/{_WS_ID}/start", headers=_admin_key_headers())
    assert running.status_code == 202
    assert running.json()["status"] == "running"
    assert backend.spawned_supervisors == []

    parked = client.post(f"/admin/workspaces/{_WS_ID_2}/start", headers=_admin_key_headers())
    assert parked.status_code == 409
    assert parked.json()["detail"]["code"] == "workspace_under_maintenance"
    assert _row_status(backend, _WS_ID_2) == "stopped"
