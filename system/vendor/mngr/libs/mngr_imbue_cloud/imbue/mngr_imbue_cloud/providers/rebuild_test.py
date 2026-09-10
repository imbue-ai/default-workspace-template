"""Unit tests for the slow-path rebuild provider/config builders."""

from imbue.mngr_imbue_cloud.config import ImbueCloudProviderConfig
from imbue.mngr_imbue_cloud.primitives import ImbueCloudAccount
from imbue.mngr_imbue_cloud.providers.rebuild import _build_delegated_vps_config
from imbue.mngr_imbue_cloud.providers.rebuild import build_slice_rebuild_config
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import GUEST_RAM_HOLDBACK_MIB
from imbue.mngr_imbue_cloud.wire_types import LeaseResult


def test_build_delegated_vps_config_forwards_runtime_knobs() -> None:
    """The slow-path rebuild must carry runsc + hardening args onto the vps_docker config."""
    config = ImbueCloudProviderConfig(
        account=ImbueCloudAccount("a@b.com"),
        docker_runtime="runsc",
        install_gvisor_runtime=True,
        default_start_args=("--workdir=/", "--security-opt=no-new-privileges"),
    )
    vps_config = _build_delegated_vps_config(config)
    assert vps_config.backend == "vps_docker"
    assert vps_config.docker_runtime == "runsc"
    assert vps_config.install_gvisor_runtime is True
    assert vps_config.default_start_args == ("--workdir=/", "--security-opt=no-new-privileges")
    # The connection-shape fields are still forwarded from the imbue_cloud config.
    assert vps_config.host_dir == config.host_dir
    assert vps_config.container_ssh_port == config.container_ssh_port


def test_build_delegated_vps_config_defaults_to_no_runtime() -> None:
    """With an unconfigured imbue_cloud config, no runtime is forced (runc)."""
    config = ImbueCloudProviderConfig(account=ImbueCloudAccount("a@b.com"))
    vps_config = _build_delegated_vps_config(config)
    assert vps_config.docker_runtime is None
    assert vps_config.install_gvisor_runtime is False
    assert vps_config.default_start_args == ()


def _lease(box_generation: int, memory_units: int | None = 8) -> LeaseResult:
    body: dict[str, object] = {
        "host_db_id": "11111111-1111-1111-1111-111111111111",
        "vps_address": "51.81.208.81",
        "ssh_port": 22004,
        "ssh_user": "root",
        "container_ssh_port": 22005,
        "agent_id": "agent-" + "a" * 32,
        "host_id": "host-" + "b" * 32,
        "host_name": "my-workspace",
        "attributes": {"memory_gb": 8, "cpus": 2},
        "box_generation": box_generation,
    }
    if memory_units is not None:
        body["memory_units"] = memory_units
    return LeaseResult.model_validate(body)


def _runsc_account_config() -> ImbueCloudProviderConfig:
    """The per-account block minds bootstrap writes: runsc plus the hardening start args."""
    return ImbueCloudProviderConfig(
        account=ImbueCloudAccount("a@b.com"),
        docker_runtime="runsc",
        default_start_args=("--workdir=/", "--security-opt=no-new-privileges"),
    )


def test_slice_rebuild_config_runs_a_gen2_container_under_the_account_runtime_with_tmpfs() -> None:
    slice_config = build_slice_rebuild_config(_runsc_account_config(), _lease(box_generation=2))
    # The gen-2 guest image ships runsc; the rebuilt container gets the same
    # runtime + hardening args + tmpfs mounts the bake creates it with.
    assert slice_config.docker_runtime == "runsc"
    assert slice_config.default_start_args == (
        "--workdir=/",
        "--security-opt=no-new-privileges",
        "--tmpfs",
        "/run",
        "--tmpfs",
        "/tmp",
    )
    assert slice_config.box_generation == 2
    # The cap is sized from the guest's RAM exactly as the bake sizes it: a gen-2
    # guest boots with its units minus the holdback, so the rebuilt container is
    # capped like the original (and like the guest's own reconcile oneshot caps it).
    assert slice_config.slice_memory_mib == 8 * 1024 - GUEST_RAM_HOLDBACK_MIB
    assert slice_config.box_public_address == "51.81.208.81"


def test_slice_rebuild_config_keeps_a_gen1_container_on_plain_runc() -> None:
    slice_config = build_slice_rebuild_config(_runsc_account_config(), _lease(box_generation=1))
    # A lima guest has no runsc: forcing the runtime would fail every rebuild
    # with docker's "unknown runtime".
    assert slice_config.docker_runtime is None
    assert slice_config.default_start_args == ()
    assert slice_config.box_generation == 1
    # A lima guest gets its full advertised RAM, so its cap is sized from all of it.
    assert slice_config.slice_memory_mib == 8 * 1024


def test_slice_rebuild_config_without_a_machine_size_has_no_memory_cap() -> None:
    # A connector too old to serve the sizing columns omits memory_units; the
    # bake-stamped memory_gb attribute is deliberately NOT consulted, so the
    # rebuilt container gets no cap rather than a guessed one.
    slice_config = build_slice_rebuild_config(_runsc_account_config(), _lease(box_generation=2, memory_units=None))
    assert slice_config.slice_memory_mib is None
