from typing import Any
from uuid import UUID

import pytest

from imbue.remote_service_connector.testing import _ADMIN_KEY_TEST_VALUE
from imbue.remote_service_connector.testing import _USER_STUB_USER_ID_PREFIX
from imbue.remote_service_connector.testing import _admin_key_headers
from imbue.remote_service_connector.testing import _make_pool_test_client
from imbue.remote_service_connector.testing import _user_headers

_MACHINE_ID = UUID("00000000-0000-0000-0000-00000000dd01")
_MACHINE_ID_2 = UUID("00000000-0000-0000-0000-00000000dd02")


def _seed_machine(
    backend: Any,
    host_id: UUID = _MACHINE_ID,
    status: str = "leased",
    box_generation: int = 2,
    memory_units: int | None = 8,
    disk_gb: int | None = 28,
    leased_to_user: str | None = None,
) -> Any:
    row = backend.add_available_host(host_id=host_id, version="v1", vps_address="10.0.0.5")
    row.status = status
    row.leased_to_user = leased_to_user or _USER_STUB_USER_ID_PREFIX
    row.leased_at = "2026-01-01T00:00:00+00:00"
    row.box_generation = box_generation
    row.memory_units = memory_units
    row.disk_gb = disk_gb
    return row


def test_resize_stamps_the_unit_target_and_reports_it(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend = _make_pool_test_client(monkeypatch)
    row = _seed_machine(backend)

    resp = client.post(f"/machines/{_MACHINE_ID}/resize", json={"target_memory_units": 16}, headers=_user_headers())

    assert resp.status_code == 200
    body = resp.json()
    assert body["memory_units"] == 8
    assert body["target_memory_units"] == 16
    assert row.target_memory_units == 16
    # Nothing else changes until the next start.
    assert row.status == "leased"
    assert row.memory_units == 8


def test_resize_accepts_a_downsize_within_the_allowed_set(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend = _make_pool_test_client(monkeypatch)
    row = _seed_machine(backend, memory_units=16, disk_gb=56)

    resp = client.post(f"/machines/{_MACHINE_ID}/resize", json={"target_memory_units": 8}, headers=_user_headers())

    assert resp.status_code == 200
    assert row.target_memory_units == 8
    # A unit downsize leaves the disk untouched.
    assert row.disk_gb == 56
    assert row.target_disk_gb is None


def test_resize_rejects_sizes_outside_the_allowed_set(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend = _make_pool_test_client(monkeypatch)
    _seed_machine(backend)
    for bad_units in (0, 4, 12, 136):
        resp = client.post(
            f"/machines/{_MACHINE_ID}/resize", json={"target_memory_units": bad_units}, headers=_user_headers()
        )
        assert resp.status_code == 400, bad_units


def test_resize_refuses_a_disk_shrink_with_a_structured_400(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend = _make_pool_test_client(monkeypatch)
    row = _seed_machine(backend, disk_gb=56)

    resp = client.post(f"/machines/{_MACHINE_ID}/resize", json={"target_disk_gb": 28}, headers=_user_headers())

    assert resp.status_code == 400
    assert "never shrinks" in resp.json()["detail"]
    assert row.target_disk_gb is None


def test_resize_state_matrix_only_starting_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend = _make_pool_test_client(monkeypatch)
    # Every state except `starting` accepts (crashed included: it is just
    # database fields); `starting` 409s with the current status.
    for idx, (status, expected_code) in enumerate(
        [("leased", 200), ("stopping", 200), ("stopped", 200), ("crashed", 200), ("starting", 409)]
    ):
        host_id = UUID(f"00000000-0000-0000-0000-00000000ee{idx:02x}")
        _seed_machine(backend, host_id=host_id, status=status)
        resp = client.post(f"/machines/{host_id}/resize", json={"target_memory_units": 16}, headers=_user_headers())
        assert resp.status_code == expected_code, (status, resp.json())
    starting_resp = client.post(
        "/machines/00000000-0000-0000-0000-00000000ee04/resize",
        json={"target_memory_units": 16},
        headers=_user_headers(),
    )
    assert "starting" in starting_resp.json()["detail"]


def test_resize_refuses_a_gen1_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend = _make_pool_test_client(monkeypatch)
    _seed_machine(backend, box_generation=1)

    resp = client.post(f"/machines/{_MACHINE_ID}/resize", json={"target_memory_units": 16}, headers=_user_headers())

    assert resp.status_code == 409
    assert "not resizable" in resp.json()["detail"]


def test_resize_target_equal_to_current_clears_the_pending_target(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend = _make_pool_test_client(monkeypatch)
    row = _seed_machine(backend)
    row.target_memory_units = 16
    row.target_disk_gb = 56

    resp = client.post(
        f"/machines/{_MACHINE_ID}/resize",
        json={"target_memory_units": 8, "target_disk_gb": 28},
        headers=_user_headers(),
    )

    assert resp.status_code == 200
    assert row.target_memory_units is None
    assert row.target_disk_gb is None


def test_resize_enforces_ownership_and_existence(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend = _make_pool_test_client(monkeypatch)
    _seed_machine(backend, leased_to_user="deadbeefdeadbeef")

    other_users_resp = client.post(
        f"/machines/{_MACHINE_ID}/resize", json={"target_memory_units": 16}, headers=_user_headers()
    )
    assert other_users_resp.status_code == 403
    missing_resp = client.post(
        f"/machines/{_MACHINE_ID_2}/resize", json={"target_memory_units": 16}, headers=_user_headers()
    )
    assert missing_resp.status_code == 404


def test_resize_units_quota_counts_the_machine_at_its_target(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend = _make_pool_test_client(monkeypatch)
    # The stub account resolves to the ally plan (max_active_machine_units=80).
    # Two running 8-unit machines: growing one to 64 fits (8 + 64 = 72), but
    # growing it to 128 would not (8 + 128 > 80).
    _seed_machine(backend, host_id=_MACHINE_ID)
    _seed_machine(backend, host_id=_MACHINE_ID_2)

    over_resp = client.post(
        f"/machines/{_MACHINE_ID}/resize", json={"target_memory_units": 128}, headers=_user_headers()
    )
    assert over_resp.status_code == 403
    assert over_resp.json()["detail"]["code"] == "quota_exceeded"
    assert over_resp.json()["detail"]["entitlement"] == "max_active_machine_units"

    fits_resp = client.post(
        f"/machines/{_MACHINE_ID}/resize", json={"target_memory_units": 64}, headers=_user_headers()
    )
    assert fits_resp.status_code == 200


def test_resize_units_quota_ignores_stopped_machines(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend = _make_pool_test_client(monkeypatch)
    # A stopped machine's units are not in the active sum; its resize is
    # granted now and re-checked when it starts.
    _seed_machine(backend, status="stopped")
    running_units_taken = UUID("00000000-0000-0000-0000-00000000ee10")
    running = _seed_machine(backend, host_id=running_units_taken)
    running.memory_units = 64

    resp = client.post(f"/machines/{_MACHINE_ID}/resize", json={"target_memory_units": 64}, headers=_user_headers())

    assert resp.status_code == 200


def test_resize_disk_quota_counts_running_and_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend = _make_pool_test_client(monkeypatch)
    # ally's max_total_machine_disk_gb is 1400; a stopped machine's disk
    # still counts (disk never changes at stop).
    stopped = _seed_machine(backend, host_id=_MACHINE_ID_2, status="stopped")
    stopped.disk_gb = 1300
    _seed_machine(backend, host_id=_MACHINE_ID, disk_gb=28)

    over_resp = client.post(f"/machines/{_MACHINE_ID}/resize", json={"target_disk_gb": 200}, headers=_user_headers())
    assert over_resp.status_code == 403
    assert over_resp.json()["detail"]["entitlement"] == "max_total_machine_disk_gb"

    fits_resp = client.post(f"/machines/{_MACHINE_ID}/resize", json={"target_disk_gb": 90}, headers=_user_headers())
    assert fits_resp.status_code == 200


def test_resize_disk_quota_counts_a_crashed_machine_conservatively(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend = _make_pool_test_client(monkeypatch)
    # A crashed machine is outside the running+stopped sum, so its grow is
    # checked as if the machine re-entered the counted set at its new size --
    # it must not "free" its own footprint from the sum it was never in.
    stopped = _seed_machine(backend, host_id=_MACHINE_ID_2, status="stopped")
    stopped.disk_gb = 1100
    crashed = _seed_machine(backend, host_id=_MACHINE_ID, status="crashed")
    crashed.disk_gb = 200

    # 1100 (stopped) + 350 (crashed at its target) > ally's 1400 cap; a
    # subtract-first check would have wrongly seen 1100 - 200 + 350 = 1250.
    over_resp = client.post(f"/machines/{_MACHINE_ID}/resize", json={"target_disk_gb": 350}, headers=_user_headers())
    assert over_resp.status_code == 403
    assert over_resp.json()["detail"]["entitlement"] == "max_total_machine_disk_gb"
    assert crashed.target_disk_gb is None

    # A crashed-machine grow that genuinely fits is still granted.
    fits_resp = client.post(f"/machines/{_MACHINE_ID}/resize", json={"target_disk_gb": 250}, headers=_user_headers())
    assert fits_resp.status_code == 200
    assert crashed.target_disk_gb == 250


def test_admin_resize_skips_quota_but_keeps_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend = _make_pool_test_client(monkeypatch)
    monkeypatch.setenv("MINDS_ADMIN_KEY", _ADMIN_KEY_TEST_VALUE)
    row = _seed_machine(backend, leased_to_user="deadbeefdeadbeef")

    resp = client.post(
        f"/admin/machines/{_MACHINE_ID}/resize", json={"target_memory_units": 128}, headers=_admin_key_headers()
    )
    assert resp.status_code == 200
    assert row.target_memory_units == 128

    shrink_resp = client.post(
        f"/admin/machines/{_MACHINE_ID}/resize", json={"target_disk_gb": 1}, headers=_admin_key_headers()
    )
    assert shrink_resp.status_code == 400


def test_resize_with_no_targets_is_a_400(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend = _make_pool_test_client(monkeypatch)
    _seed_machine(backend)

    resp = client.post(f"/machines/{_MACHINE_ID}/resize", json={}, headers=_user_headers())

    assert resp.status_code == 400
