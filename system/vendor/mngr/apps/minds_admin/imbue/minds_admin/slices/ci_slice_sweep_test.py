from collections.abc import Sequence
from uuid import uuid4

from imbue.minds_admin.slices.ci_slice_sweep import CiSliceSweepBoxReport
from imbue.minds_admin.slices.ci_slice_sweep import DEFAULT_CI_SLICE_MAX_AGE_HOURS
from imbue.minds_admin.slices.ci_slice_sweep import sweep_ci_slices_on_box
from imbue.mngr_imbue_cloud.slices.bare_metal import CI_SLICE_MAX_AGE_SECONDS
from imbue.mngr_imbue_cloud.slices.gen2_scripts.box_commands import SliceInstanceObservation
from imbue.mngr_imbue_cloud.slices.mock_slice_vm_client_test import MockSliceVmClient

_SERVER_ID = "0d0b5f0c-1c1e-4bd6-b4f5-3a1e4e9b6c7d"
_BOX = "203.0.113.42"
_MAX_AGE = CI_SLICE_MAX_AGE_SECONDS


def _ci_slice_name(env: str) -> str:
    return f"mngr-slice-{env}-{uuid4().hex}"


def _observation(name: str, *, age_seconds: float, is_active: bool) -> SliceInstanceObservation:
    return SliceInstanceObservation(instance_name=name, is_active=is_active, age_seconds=age_seconds)


def _box(
    observations: Sequence[SliceInstanceObservation],
    disk_names: set[str],
    failing_resource_names: set[str] | None = None,
) -> MockSliceVmClient:
    return MockSliceVmClient(
        box_address=_BOX,
        box_ssh_user="slicehost",
        observations=list(observations),
        disk_names=disk_names,
        failing_resource_names=failing_resource_names or set(),
    )


def _sweep(client: MockSliceVmClient) -> CiSliceSweepBoxReport:
    return sweep_ci_slices_on_box(client, server_id=_SERVER_ID, public_address=_BOX, max_age_seconds=_MAX_AGE)


def test_default_ci_sweep_age_is_the_shared_threshold_in_hours() -> None:
    assert DEFAULT_CI_SLICE_MAX_AGE_HOURS == CI_SLICE_MAX_AGE_SECONDS / 3600.0 == 4.0


def test_sweep_destroys_stale_ci_slices_even_while_running_and_keeps_young_and_foreign_ones() -> None:
    stale_running = _ci_slice_name("ci-20260820t000000z-dead")
    stale_warm = _ci_slice_name("ci-warm")
    young = _ci_slice_name("ci-20260820t120000z-live")
    dev_owned = _ci_slice_name("dev-josh")
    legacy = f"mngr-slice-{uuid4().hex}"
    client = _box(
        observations=[
            _observation(stale_running, age_seconds=5 * 3600.0, is_active=True),
            _observation(stale_warm, age_seconds=5 * 3600.0, is_active=False),
            _observation(young, age_seconds=60.0, is_active=True),
            _observation(dev_owned, age_seconds=10 * 3600.0, is_active=False),
            _observation(legacy, age_seconds=10 * 3600.0, is_active=False),
        ],
        disk_names={f"{name}-data" for name in (stale_running, stale_warm, young, dev_owned, legacy)},
    )

    report = _sweep(client)

    # A crashed run leaves its VMs running, so a stale CI VM is destroyed regardless
    # of its unit state; the VM destroy takes its own disk with it.
    assert report.swept_instances == tuple(sorted((stale_running, stale_warm)))
    assert client.destroyed_instance_names == sorted((stale_running, stale_warm))
    assert report.swept_disks == ()
    assert report.kept_ci_slices == (young,)
    assert report.foreign_slices == tuple(sorted((dev_owned, f"{dev_owned}-data")))
    assert report.failed == ()
    assert client.list_instance_names() == {young, dev_owned, legacy}
    assert client.disk_names == {f"{young}-data", f"{dev_owned}-data", f"{legacy}-data"}


def test_sweep_reclaims_a_ci_disk_whose_vm_is_already_gone_but_holds_a_young_vms_disk() -> None:
    young = _ci_slice_name("ci-20260820t120000z-live")
    leaked_disk = _ci_slice_name("ci-20260819t000000z-gone") + "-data"
    client = _box(
        observations=[_observation(young, age_seconds=60.0, is_active=True)],
        disk_names={f"{young}-data", leaked_disk},
    )

    report = _sweep(client)

    assert report.swept_disks == (leaked_disk,)
    assert client.destroyed_disk_names == [leaked_disk]
    assert client.disk_names == {f"{young}-data"}
    assert report.swept_instances == ()


def test_sweep_collects_a_failed_destroy_and_keeps_that_vms_disk() -> None:
    wedged = _ci_slice_name("ci-20260820t000000z-wedged")
    stale = _ci_slice_name("ci-20260820t000000z-dead")
    client = _box(
        observations=[
            _observation(wedged, age_seconds=5 * 3600.0, is_active=True),
            _observation(stale, age_seconds=5 * 3600.0, is_active=False),
        ],
        disk_names={f"{wedged}-data", f"{stale}-data"},
        failing_resource_names={wedged},
    )

    report = _sweep(client)

    # The wedged VM is reported and retried next sweep; its disk stays with it (a
    # VM that is still on the box holds its disk), while the sibling is swept.
    assert report.failed == (wedged,)
    assert report.swept_instances == (stale,)
    assert report.swept_disks == ()
    assert client.disk_names == {f"{wedged}-data"}


def test_sweep_leaves_a_box_with_nothing_stale_untouched() -> None:
    young = _ci_slice_name("ci-20260820t120000z-live")
    client = _box(
        observations=[_observation(young, age_seconds=_MAX_AGE, is_active=True)],
        disk_names={f"{young}-data"},
    )

    report = _sweep(client)

    # Exactly the threshold is not yet stale (the comparison is strict).
    assert report.swept_instances == report.swept_disks == report.failed == ()
    assert report.kept_ci_slices == (young,)
    assert client.destroyed_instance_names == client.destroyed_disk_names == []
