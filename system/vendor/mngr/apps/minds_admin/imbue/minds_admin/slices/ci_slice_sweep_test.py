from uuid import uuid4

from imbue.minds_admin.slices.ci_slice_sweep import compute_stale_ci_slice_names
from imbue.minds_admin.slices.ci_slice_sweep import parse_slice_resource_ages


def _ci_slice_name(env: str) -> str:
    return f"mngr-slice-{env}-{uuid4().hex}"


def test_compute_stale_ci_slice_names_splits_by_owner_tier_and_age() -> None:
    stale_name = _ci_slice_name("ci-20260820t000000z-dead")
    young_name = _ci_slice_name("ci-20260820t120000z-live")
    warm_name = _ci_slice_name("ci-warm")
    dev_name = _ci_slice_name("dev-josh")
    legacy_name = f"mngr-slice-{uuid4().hex}"
    ages = {
        stale_name: 5 * 3600.0,
        young_name: 60.0,
        warm_name: 5 * 3600.0,
        dev_name: 10 * 3600.0,
        legacy_name: 10 * 3600.0,
        "some-unrelated-vm": 10 * 3600.0,
    }

    stale, young, foreign = compute_stale_ci_slice_names(ages, max_age_seconds=4 * 3600.0)

    assert stale == {stale_name, warm_name}
    assert young == {young_name}
    assert foreign == {dev_name}


def test_compute_stale_ci_slice_names_attributes_disk_names_via_their_suffix() -> None:
    disk_name = _ci_slice_name("ci-20260820t000000z-x") + "-data"

    stale, _young, _foreign = compute_stale_ci_slice_names({disk_name: 9 * 3600.0}, max_age_seconds=4 * 3600.0)

    assert stale == {disk_name}


def test_parse_slice_resource_ages_computes_ages_against_the_box_clock() -> None:
    instance = _ci_slice_name("ci-20260820t000000z-a")
    disk = instance + "-data"
    output = (
        "MNGR_SWEEP_NOW 1000000\n"
        f"999400 /home/limahost/.lima/{instance}/lima.yaml\n"
        f"999900 /home/limahost/.lima/_disks/{disk}\n"
    )

    instance_ages, disk_ages = parse_slice_resource_ages(output)

    assert instance_ages == {instance: 600.0}
    assert disk_ages == {disk: 100.0}


def test_parse_slice_resource_ages_tolerates_noise_and_missing_resources() -> None:
    output = "MNGR_SWEEP_NOW 1000000\nnot-a-number /home/limahost/.lima/x/lima.yaml\n\nwat\n"

    instance_ages, disk_ages = parse_slice_resource_ages(output)

    assert instance_ages == {}
    assert disk_ages == {}


def test_parse_slice_resource_ages_ignores_entries_before_the_now_marker() -> None:
    output = "999400 /home/limahost/.lima/some-vm/lima.yaml\nMNGR_SWEEP_NOW 1000000\n"

    instance_ages, _disk_ages = parse_slice_resource_ages(output)

    assert instance_ages == {}
