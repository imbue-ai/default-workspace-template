import subprocess
import threading
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

import click
import psycopg2
import pytest
from click.testing import CliRunner
from inline_snapshot import snapshot

from imbue.concurrency_group.concurrency_group import ConcurrencyExceptionGroup
from imbue.imbue_common.model_update import to_update
from imbue.minds_admin.cli.server import BakeRowLedger
from imbue.minds_admin.cli.server import SliceManagementTrust
from imbue.minds_admin.cli.server import _COLLECTOR_VERIFICATION_SCRIPT
from imbue.minds_admin.cli.server import _bake_one_slice_with_retry
from imbue.minds_admin.cli.server import _box_ssh_host_key_options
from imbue.minds_admin.cli.server import _build_slice_create_args
from imbue.minds_admin.cli.server import _destroy_one_pool_host
from imbue.minds_admin.cli.server import _format_capacity_table
from imbue.minds_admin.cli.server import _import_ready_servers
from imbue.minds_admin.cli.server import _is_seed_phase_needed
from imbue.minds_admin.cli.server import _kill_bake_worker_processes
from imbue.minds_admin.cli.server import _resolve_gen2_guest_image
from imbue.minds_admin.cli.server import _resolve_service_user_for_generation
from imbue.minds_admin.cli.server import _resolve_vendored_mngr_source
from imbue.minds_admin.cli.server import _run_bake_attempts
from imbue.minds_admin.cli.server import assert_bake_box_storage_is_encrypted
from imbue.minds_admin.cli.server import assert_box_is_exclusive_to_tier
from imbue.minds_admin.cli.server import assert_gen2_box_storage_is_encrypted
from imbue.minds_admin.cli.server import box_script_provisioning_error_or_none
from imbue.minds_admin.cli.server import build_box_ssh_argv
from imbue.minds_admin.cli.server import build_box_tier_audit_report
from imbue.minds_admin.cli.server import build_pool_host_destroy_report
from imbue.minds_admin.cli.server import build_registered_server
from imbue.minds_admin.cli.server import choose_storage_passphrase
from imbue.minds_admin.cli.server import compose_box_prep_script
from imbue.minds_admin.cli.server import compute_server_slice_sizing
from imbue.minds_admin.cli.server import destroy_pool_hosts_in_parallel
from imbue.minds_admin.cli.server import gen2_register_disk_shortfall_or_none
from imbue.minds_admin.cli.server import plaintext_gen2_box_ids
from imbue.minds_admin.cli.server import reap_orphan_slices
from imbue.minds_admin.cli.server import resolve_bake_management_trust_and_key
from imbue.minds_admin.cli.server import resolve_slice_container_runtime
from imbue.minds_admin.cli.server import run_outcome_workers_in_bounded_threads
from imbue.minds_admin.cli.server import server
from imbue.minds_admin.cli.server import slice_advertised_attributes
from imbue.minds_admin.cli.server import sweep_ci_slices_across_boxes
from imbue.minds_admin.slices.bare_metal_prep import DEFAULT_GEN2_SLICE_GUEST_IMAGE_SHA512
from imbue.minds_admin.slices.bare_metal_prep import DEFAULT_GEN2_SLICE_GUEST_IMAGE_URL
from imbue.minds_admin.slices.operator_identity import POOL_KEY_FILENAME
from imbue.minds_admin.slices.testing import RecordingConnection
from imbue.minds_admin.slices.testing import make_test_management_identities
from imbue.mngr.primitives import HostId
from imbue.mngr.providers.ssh_utils import generate_ed25519_host_keypair
from imbue.mngr_imbue_cloud.data_types import BareMetalServer
from imbue.mngr_imbue_cloud.data_types import BareMetalServerCapacity
from imbue.mngr_imbue_cloud.data_types import BoxManagementTrust
from imbue.mngr_imbue_cloud.data_types import BoxTierAudit
from imbue.mngr_imbue_cloud.data_types import PoolHostDestroyOutcome
from imbue.mngr_imbue_cloud.data_types import SliceBakeOutcome
from imbue.mngr_imbue_cloud.data_types import StorageVolumeState
from imbue.mngr_imbue_cloud.data_types import UnauditedBox
from imbue.mngr_imbue_cloud.errors import BareMetalProvisioningError
from imbue.mngr_imbue_cloud.primitives import BareMetalServerDbId
from imbue.mngr_imbue_cloud.primitives import BareMetalServerStatus
from imbue.mngr_imbue_cloud.primitives import PoolHostDestroyOutcomeStatus
from imbue.mngr_imbue_cloud.primitives import SERVER_STATUS_READY
from imbue.mngr_imbue_cloud.primitives import SliceBakeOutcomeStatus
from imbue.mngr_imbue_cloud.primitives import SliceContainerRuntime
from imbue.mngr_imbue_cloud.slices.bare_metal import GEN1_SLICE_SERVICE_USER
from imbue.mngr_imbue_cloud.slices.bare_metal import compute_capacity
from imbue.mngr_imbue_cloud.slices.bare_metal import slice_disk_name
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_SLICE_SERVICE_USER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import GEN2_BOOT_DISK_GIB
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import SLICE_BOOT_DISK_GIB
from imbue.mngr_imbue_cloud.slices.mock_box_image_cache_test import MockBoxImageCache
from imbue.mngr_imbue_cloud.slices.mock_slice_vm_client_test import MockSliceVmClient


def _server(
    slot_count: int,
    cpu_threads: int,
    *,
    memory_per_slice_gb: int = 8,
    cpu_overcommit_ratio: float = 1.5,
    disk_gb: int = 477,
) -> BareMetalServer:
    now = datetime(2026, 6, 13, tzinfo=timezone.utc)
    return BareMetalServer(
        id=BareMetalServerDbId("11111111-1111-1111-1111-111111111111"),
        plan_code="24rise02-v1-us",
        region="vin",
        public_address="15.204.140.221",
        cpu_threads=cpu_threads,
        ram_gb=slot_count * memory_per_slice_gb,
        disk_gb=disk_gb,
        memory_per_slice_gb=memory_per_slice_gb,
        cpu_overcommit_ratio=cpu_overcommit_ratio,
        slot_count=slot_count,
        status=BareMetalServerStatus(SERVER_STATUS_READY),
        created_at=now,
        updated_at=now,
        uplink_mbps=1000,
    )


def test_import_ready_servers_skips_a_box_the_target_already_registers() -> None:
    # A source box the target DB already holds under another row id trips the
    # ovh_service_name unique index; the import must roll that statement back,
    # report the box as skipped, and still import the rest.
    source = _server(slot_count=15, cpu_threads=32)
    fresh = source.model_copy_update(to_update(source.field_ref().ovh_service_name, "ns-fresh"))
    duplicate = fresh.model_copy_update(
        to_update(fresh.field_ref().id, BareMetalServerDbId("22222222-2222-2222-2222-222222222222")),
        to_update(fresh.field_ref().ovh_service_name, "ns-duplicate"),
    )
    conn = RecordingConnection(
        [],
        rowcount=1,
        error_by_param={
            "ns-duplicate": psycopg2.errors.UniqueViolation(
                'duplicate key value violates unique constraint "bare_metal_servers_service_name_idx"'
            )
        },
    )
    imported, skipped = _import_ready_servers(conn, [duplicate, fresh])
    assert [server.id for server in imported] == [fresh.id]
    assert [(server.id, "bare_metal_servers_service_name_idx" in reason) for server, reason in skipped] == [
        (duplicate.id, True)
    ]
    assert conn.rollback_count == 1
    assert [params[0] for _sql, params in conn.recording_cursor.executed] == [str(fresh.id)]


def test_build_registered_server_derives_slot_count_from_memory_per_slice() -> None:
    built = build_registered_server(
        ovh_service_name="ns1.ovh.us",
        plan_code="24rise02-v1-us",
        region="vin",
        public_address="1.2.3.4",
        ram_gb=64,
        cpu_cores=8,
        cpu_threads=16,
        disk_gb=477,
        memory_per_slice_gb=8,
        cpu_overcommit_ratio=1.5,
        raid_level="RAID1",
        slice_service_user="slicehost",
        ovh_order_id="8144904",
        status=SERVER_STATUS_READY,
        box_generation=1,
        uplink_mbps=1000,
    )
    # 64GB box, 8GB slices: (64-8)*1024 // (8*1024 + 512) = 6 slots after host reserve.
    assert built.slot_count == 6
    assert built.disk_gb == 477
    assert built.ovh_service_name == "ns1.ovh.us"
    assert str(built.status) == "ready"


def test_compute_server_slice_sizing_uses_server_inputs_and_specs() -> None:
    sizing = compute_server_slice_sizing(_server(slot_count=8, cpu_threads=16), None)
    # 16 threads * 1.5 / 8 slots = 3 vCPU per slice.
    assert sizing["vcpus"] == 3
    assert sizing["advertised_memory_gb"] == 8
    # Guest gets the full advertised RAM (per-VM overhead is accounted in slot_count).
    assert sizing["memory_mib"] == 8 * 1024
    # Per-slice disk budget = (477 - max(20, ceil(477*0.10))=48 reserve) // 8, minus boot.
    assert sizing["disk_gib"] == (477 - 48) // 8 - SLICE_BOOT_DISK_GIB
    assert slice_advertised_attributes(sizing) == {"memory_gb": 8, "cpus": 3}
    # The row's sizing columns: the advertised units, and the disk the machine
    # has once the cutover moves it to gen-2 (its data disk plus the gen-2 base).
    assert sizing["row_memory_units"] == 8
    assert sizing["row_disk_gb"] == sizing["disk_gib"] + 16


def test_format_capacity_table_shows_per_server_and_fleet_totals() -> None:
    gen2_server = _server(slot_count=16, cpu_threads=32)
    gen2_server = gen2_server.model_copy_update(
        to_update(gen2_server.field_ref().box_generation, 2),
        to_update(gen2_server.field_ref().ram_gb, 128),
        to_update(gen2_server.field_ref().disk_gb, 1000),
    )
    capacities = [
        compute_capacity(_server(slot_count=8, cpu_threads=16), used_slots=3),
        compute_capacity(gen2_server, used_slots=1),
    ]
    # Three default machines' usage: 24 units, 3 x (boot disk + 44GiB data) disk.
    used_disk_gib = 3 * (GEN2_BOOT_DISK_GIB + 44)
    table = _format_capacity_table(capacities, {str(gen2_server.id): (24, used_disk_gib)})
    # Gen-1 keeps the slot display; gen-2 shows used/total units and disk
    # against the box budgets (128GB RAM -> 120 units; a 1000 GiB storage
    # partition -> 936 GiB after the 64 GiB reserve).
    assert "3/8 slots" in table
    assert f"24/120u {used_disk_gib}/936G" in table
    # Fleet line: 24 total slots, 4 used, 20 free.
    assert "4/24 slots used, 20 free" in table
    assert "UPLINK" in table.splitlines()[0]
    assert "   1000M" in table


def test_box_ssh_host_key_options_pins_recorded_key() -> None:
    """With a recorded box host key, box SSH strictly pins it (no trust-on-first-use)."""
    with _box_ssh_host_key_options("203.0.113.7", 22, "ssh-ed25519 AAAAtestboxkey") as opts:
        assert "StrictHostKeyChecking=yes" in opts
        assert any(o.startswith("UserKnownHostsFile=") for o in opts)
    # The accept-new TOFU fallback is gone entirely.
    assert "accept-new" not in " ".join(opts)


def test_box_ssh_host_key_options_fails_closed_without_a_key() -> None:
    """No recorded box host key -> refuse to SSH rather than trust-on-first-use."""
    with pytest.raises(BareMetalProvisioningError, match="strict host-key"):
        with _box_ssh_host_key_options("203.0.113.7", 22, "") as _opts:
            pass


def test_compose_prep_script_with_nothing_configured_is_the_base_script() -> None:
    composed = compose_box_prep_script(
        base_script="echo base\n", collector_install_script=None, extra_prep_script_text=None
    )
    assert composed == "echo base\n"


def test_compose_prep_script_runs_the_extra_script_after_the_standard_prep_steps() -> None:
    # The extra script must run in the same sudo bash session, strictly after the
    # base prep, on its own line (so its first command is never glued onto the
    # base script's last line). Without the collector there is no unit to verify,
    # so no verification step may be appended.
    composed = compose_box_prep_script(
        base_script="echo base", collector_install_script=None, extra_prep_script_text="echo extra"
    )
    assert composed == "echo base\necho extra"


def test_compose_prep_script_orders_base_then_collector_then_extra_then_verification() -> None:
    composed = compose_box_prep_script(
        base_script="echo base",
        collector_install_script="echo collector",
        extra_prep_script_text="echo extra",
    )
    assert composed == "echo base\necho collector\necho extra\n" + _COLLECTOR_VERIFICATION_SCRIPT


def test_compose_prep_script_verifies_the_collector_only_when_it_is_installed() -> None:
    with_collector = compose_box_prep_script(
        base_script="echo base", collector_install_script="echo collector", extra_prep_script_text=None
    )
    assert with_collector == "echo base\necho collector\n" + _COLLECTOR_VERIFICATION_SCRIPT
    without_collector = compose_box_prep_script(
        base_script="echo base", collector_install_script=None, extra_prep_script_text=None
    )
    assert "otelcol-contrib" not in without_collector


def test_collector_verification_checks_the_unit_and_fails_loudly() -> None:
    # The verification is the fail-closed half of the collector contract: it must
    # probe the actual systemd unit and exit non-zero (failing the whole prep,
    # and thus `setup`'s flip to 'ready') when the unit is not active.
    assert "systemctl is-active otelcol-contrib" in _COLLECTOR_VERIFICATION_SCRIPT
    assert "exit 1" in _COLLECTOR_VERIFICATION_SCRIPT


def test_setup_exposes_the_extra_prep_script_escape_hatch() -> None:
    # `setup` runs the same composed prep as `prep`, including the ad-hoc
    # --extra-prep-script escape hatch.
    result = CliRunner().invoke(server, ["setup", "--help"])
    assert result.exit_code == 0
    assert "--extra-prep-script" in result.output


def test_server_group_help_lists_commands() -> None:
    result = CliRunner().invoke(server, ["--help"])
    assert result.exit_code == 0
    # The server group holds only the fleet-lifecycle verbs; slice baking moved to
    # ``minds-admin pool create``.
    for command in ("prep", "list", "register", "set-status", "drain", "undrain"):
        assert command in result.output
    assert "allocate-slice" not in result.output


def test_drain_command_exposes_the_turnover_surface() -> None:
    result = CliRunner().invoke(server, ["drain", "--help"])
    assert result.exit_code == 0
    for option in ("--server-id", "--database-url", "--connector-url", "--api-key", "--max-concurrency"):
        assert option in result.output
    # The core contract: draining a box force-stops its leased workspaces.
    assert "force-stop" in result.output


def test_set_status_refuses_draining_and_points_at_drain() -> None:
    result = CliRunner().invoke(
        server, ["set-status", "--server-id", "box-1", "--status", "draining", "--database-url", "postgres://x"]
    )
    assert result.exit_code == 2
    assert "server drain" in result.output


def test_undrain_is_on_the_cli_surface_and_documents_its_scope() -> None:
    result = CliRunner().invoke(server, ["undrain", "--help"])
    assert result.exit_code == 0
    assert "draining" in result.output
    assert "--server-id" in result.output


def test_order_command_exposes_the_uplink_override() -> None:
    result = CliRunner().invoke(server, ["order", "--help"])
    assert result.exit_code == 0
    assert "--uplink-mbps" in result.output


def _register_args(*varied_args: str) -> list[str]:
    """A `register` invocation for a plausible box, minus the options a test varies."""
    return [
        "register",
        "--ovh-service-name",
        "ns1.ovh.us",
        "--plan-code",
        "24rise02-v1-us",
        "--public-address",
        "1.2.3.4",
        "--ram-gb",
        "64",
        "--cpu-cores",
        "8",
        "--cpu-threads",
        "16",
        "--disk-gb",
        "477",
        "--memory-per-slice-gb",
        "8",
        "--database-url",
        "postgres://example",
        *varied_args,
    ]


def test_register_warns_about_a_short_gen2_disk_instead_of_refusing() -> None:
    # Ordering refuses a config whose disk cannot hold the RAM's full complement
    # of default machines; a delivered box is registered with a warning that
    # says how many fit, since its prep re-measures disk_gb anyway.
    shortfall = gen2_register_disk_shortfall_or_none(ram_gb=128, disk_gb=792)
    assert shortfall is not None
    assert "holds 13 of them" in shortfall
    assert "re-measures disk_gb" in shortfall
    assert gen2_register_disk_shortfall_or_none(ram_gb=128, disk_gb=872) is None


def test_register_requires_the_uplink_and_a_known_datacenter() -> None:
    # No uplink: refused before any DB connection.
    missing_uplink = CliRunner().invoke(server, _register_args("--region", "vin"))
    assert missing_uplink.exit_code == 2
    assert "--uplink-mbps" in missing_uplink.output
    # A zero rate would make the egress threshold zero and the shaper a 1 mbit class.
    zero_uplink = CliRunner().invoke(server, _register_args("--region", "vin", "--uplink-mbps", "0"))
    assert zero_uplink.exit_code == 2
    assert "--uplink-mbps" in zero_uplink.output
    unknown_datacenter = CliRunner().invoke(server, _register_args("--region", "gra", "--uplink-mbps", "1000"))
    assert unknown_datacenter.exit_code == 2
    assert "--region" in unknown_datacenter.output


def test_resolve_service_user_defaults_per_generation_and_pins_gen2() -> None:
    assert _resolve_service_user_for_generation(1, None, None) == GEN1_SLICE_SERVICE_USER
    assert _resolve_service_user_for_generation(2, None, None) == GEN2_SLICE_SERVICE_USER
    # A gen-1 box may run any user (the gen-1 prep templates it in): the row's
    # recorded user is the default, and an explicit override wins over it.
    assert _resolve_service_user_for_generation(1, None, "recorded-user") == "recorded-user"
    assert _resolve_service_user_for_generation(1, "custom-user", "recorded-user") == "custom-user"
    # A gen-2 box is pinned to the user the prep artifacts and sudoers grants
    # name, even when its row still records the retired pre-rename user.
    assert _resolve_service_user_for_generation(2, GEN2_SLICE_SERVICE_USER, None) == GEN2_SLICE_SERVICE_USER
    assert _resolve_service_user_for_generation(2, None, GEN1_SLICE_SERVICE_USER) == GEN2_SLICE_SERVICE_USER
    with pytest.raises(click.UsageError, match="--slice-service-user"):
        _resolve_service_user_for_generation(2, "custom-user", None)


def test_register_refuses_a_gen2_service_user_override_before_any_db_connection() -> None:
    result = CliRunner().invoke(
        server,
        _register_args(
            "--region", "vin", "--uplink-mbps", "1000", "--box-generation", "2", "--slice-service-user", "limauser"
        ),
    )
    assert result.exit_code == 2
    assert "--slice-service-user" in result.output
    assert GEN2_SLICE_SERVICE_USER in result.output


def test_order_command_exposes_dry_run_flag() -> None:
    # `order --dry-run` is the no-charge price/spec preview the deployment playbook
    # relies on; guard that the flag stays on the CLI surface with its no-charge contract.
    result = CliRunner().invoke(server, ["order", "--help"])
    assert result.exit_code == 0
    assert "--dry-run" in result.output
    assert "No charge" in result.output or "no charge" in result.output


def test_bake_row_ledger_drains_only_the_rows_workers_never_cleaned_up() -> None:
    # A worker records its baking row after the insert and forgets it on its own
    # cleanup; the bake's final sweep only ever sees the rows of killed workers.
    ledger = BakeRowLedger()
    ledger.record("row-a", "slice-a")
    ledger.record("row-b", "slice-b")
    ledger.forget("row-a")
    ledger.forget("row-never-recorded")
    assert ledger.drain() == [("row-b", "slice-b")]
    assert ledger.drain() == []


def test_kill_bake_worker_processes_terminates_a_child() -> None:
    # On a top-level kill the bake's in-flight `mngr create` workers must be reaped
    # so they don't keep carving VMs; this is the helper that does it. Spawn a child
    # and confirm it is killed (the helper kills all children of this process).
    child = subprocess.Popen(["sleep", "39517"])
    try:
        assert child.poll() is None
        _kill_bake_worker_processes(grace_seconds=5.0)
        assert child.wait(timeout=5) is not None
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def test_from_tag_bake_keeps_the_tags_vendored_mngr() -> None:
    """A --from-tag bake (no explicit --mngr-source) must NOT vendor the local checkout.

    Regression test: --from-tag means byte-for-byte tag content, including the mngr
    vendored at the tag. Returning the local repo_root here would silently bake the
    operator's working-tree mngr over the tag's, producing a same-version content
    skew (the bug that broke chat-agent creation on a minds-vX slice).
    """
    resolved = _resolve_vendored_mngr_source(mngr_source=None, repo_root=Path("/monorepo"), is_from_tag=True)
    assert resolved is None


def test_workspace_dir_bake_vendors_the_local_checkout() -> None:
    """A --workspace-dir (dev) bake with no explicit --mngr-source vendors repo_root."""
    resolved = _resolve_vendored_mngr_source(mngr_source=None, repo_root=Path("/monorepo"), is_from_tag=False)
    assert resolved == Path("/monorepo")


def test_explicit_mngr_source_always_wins() -> None:
    """An explicit --mngr-source overrides the vendored mngr for either bake source."""
    for is_from_tag in (True, False):
        resolved = _resolve_vendored_mngr_source(
            mngr_source="/some/other/mngr", repo_root=Path("/monorepo"), is_from_tag=is_from_tag
        )
        assert resolved == Path("/some/other/mngr")


def test_destroy_report_counts_already_gone_as_destroyed() -> None:
    """Re-running the same id list after a partial failure must converge to success.

    Ids whose rows are already gone report 'already_gone' and count as destroyed;
    only genuine teardown failures make the report (and thus the command) fail.
    """
    report = build_pool_host_destroy_report(
        [
            PoolHostDestroyOutcome(pool_host_id="a", status=PoolHostDestroyOutcomeStatus.DESTROYED),
            PoolHostDestroyOutcome(pool_host_id="b", status=PoolHostDestroyOutcomeStatus.ALREADY_GONE),
            PoolHostDestroyOutcome(pool_host_id="c", status=PoolHostDestroyOutcomeStatus.SKIPPED_LEASED),
            PoolHostDestroyOutcome(
                pool_host_id="d", status=PoolHostDestroyOutcomeStatus.FAILED, detail="box unreachable"
            ),
        ]
    )
    assert report.requested == 4
    assert report.destroyed == 2
    assert report.skipped == 1
    assert report.failed == 1
    assert [host.pool_host_id for host in report.hosts] == ["a", "b", "c", "d"]
    # The wire form the CLI emits keeps the documented lowercase statuses and omits
    # absent details.
    dumped = report.model_dump(mode="json", exclude_none=True)
    assert dumped["hosts"][0] == {"pool_host_id": "a", "status": "destroyed"}
    assert dumped["hosts"][3]["status"] == "failed"


def test_destroy_report_for_no_hosts_is_all_zero() -> None:
    report = build_pool_host_destroy_report([])
    assert report.model_dump(mode="json", exclude_none=True) == {
        "requested": 0,
        "destroyed": 0,
        "skipped": 0,
        "failed": 0,
        "hosts": [],
    }


def test_reap_orphan_slices_requires_an_activated_env() -> None:
    # The reap is scoped to one env's slice names; without an env it would have
    # nothing safe to match, so it refuses before touching the DB or the box.
    with pytest.raises(click.UsageError):
        reap_orphan_slices(
            server_id="11111111-1111-1111-1111-111111111111",
            database_url="postgres://example",
            identities=make_test_management_identities("dev", "unused"),
            env_name=None,
            is_dry_run=True,
        )


def test_destroy_pool_hosts_in_parallel_rejects_nonpositive_concurrency() -> None:
    with pytest.raises(click.UsageError):
        destroy_pool_hosts_in_parallel(
            pool_host_ids=["row-1"],
            database_url="postgres://example",
            identities=None,
            eligible_statuses=("available",),
            is_row_drop_only=False,
            max_concurrency=0,
        )


def test_destroy_worker_turns_db_errors_into_failed_outcomes() -> None:
    """A DB failure in one worker must become a per-host 'failed' outcome, not a raise.

    ObservableThread.join() re-raises worker exceptions, so an uncaught psycopg2 error
    in one thread would abort the whole batch mid-join, skip the outcome report, and
    tear down the shared temp key dir under the sibling threads.
    """
    outcome = _destroy_one_pool_host(
        pool_host_id="11111111-1111-1111-1111-111111111111",
        # Port 1 on localhost refuses connections immediately -- a fast, deterministic
        # psycopg2.OperationalError without any real DB.
        database_url="postgresql://user@127.0.0.1:1/db",
        identities=None,
        eligible_statuses=("available",),
        is_row_drop_only=True,
    )
    assert outcome.status == PoolHostDestroyOutcomeStatus.FAILED
    assert outcome.pool_host_id == "11111111-1111-1111-1111-111111111111"


def test_bounded_fan_out_caps_concurrency_and_collects_all_outcomes() -> None:
    """The shared fan-out runs at most max_concurrency workers at once and returns every outcome.

    The Barrier(2) forces each admitted pair to overlap (so the cap is actually
    exercised, not just scheduled around), and the counter asserts the semaphore
    never admits more than the cap.
    """
    barrier = threading.Barrier(2)
    concurrency_lock = threading.Lock()
    concurrency_state = {"current": 0, "max": 0}

    def worker(item: int) -> dict[str, Any]:
        with concurrency_lock:
            concurrency_state["current"] += 1
            concurrency_state["max"] = max(concurrency_state["max"], concurrency_state["current"])
        barrier.wait(timeout=30)
        with concurrency_lock:
            concurrency_state["current"] -= 1
        return {"item": item, "status": "done"}

    outcomes = run_outcome_workers_in_bounded_threads(
        worker=worker,
        worker_kwargs_list=[dict(item=idx) for idx in range(4)],
        max_concurrency=2,
        thread_name_prefix="fanout-test",
        progress_noun="Fan-out test",
        describe_outcome=lambda outcome: str(outcome["item"]),
        interruption_exception_types=(),
        on_join_interrupted=None,
    )
    assert sorted(outcome["item"] for outcome in outcomes) == [0, 1, 2, 3]
    assert concurrency_state["max"] == 2


def _bake_outcome(status: SliceBakeOutcomeStatus, host_name: str) -> SliceBakeOutcome:
    # error is documented "failed only" on SliceBakeOutcome, so stamp it only there.
    error = "boom" if status == SliceBakeOutcomeStatus.FAILED else None
    return SliceBakeOutcome(host_name=host_name, server_id="server-1", status=status, error=error)


def test_run_bake_attempts_returns_the_first_success_without_retrying() -> None:
    call_count = {"count": 0}

    def bake_once() -> SliceBakeOutcome:
        call_count["count"] += 1
        return _bake_outcome(SliceBakeOutcomeStatus.SUCCEEDED, f"slice-{call_count['count']}")

    outcome = _run_bake_attempts(bake_once, attempt_count=3, termination_event=threading.Event())
    assert outcome.status == SliceBakeOutcomeStatus.SUCCEEDED
    assert call_count["count"] == 1


def test_run_bake_attempts_retries_a_transient_failure_with_a_fresh_slice() -> None:
    # A failed bake destroys its VM and writes no row, so the retry is a clean fresh
    # slice: two transient failures followed by a success must yield the success.
    call_count = {"count": 0}

    def bake_once() -> SliceBakeOutcome:
        call_count["count"] += 1
        status = SliceBakeOutcomeStatus.SUCCEEDED if call_count["count"] == 3 else SliceBakeOutcomeStatus.FAILED
        return _bake_outcome(status, f"slice-{call_count['count']}")

    outcome = _run_bake_attempts(bake_once, attempt_count=3, termination_event=threading.Event())
    assert outcome.status == SliceBakeOutcomeStatus.SUCCEEDED
    assert outcome.host_name == "slice-3"
    assert call_count["count"] == 3


def test_run_bake_attempts_returns_the_last_failure_after_exhausting_attempts() -> None:
    call_count = {"count": 0}

    def bake_once() -> SliceBakeOutcome:
        call_count["count"] += 1
        return _bake_outcome(SliceBakeOutcomeStatus.FAILED, f"slice-{call_count['count']}")

    outcome = _run_bake_attempts(bake_once, attempt_count=2, termination_event=threading.Event())
    assert outcome.status == SliceBakeOutcomeStatus.FAILED
    assert outcome.host_name == "slice-2"
    assert call_count["count"] == 2


def test_run_bake_attempts_does_not_retry_after_the_bake_is_terminated() -> None:
    # A termination signal's kill sweep makes every in-flight attempt fail; retrying
    # those would spawn replacement bakes (new VMs) after the operator killed the
    # bake, so a set termination event must return the failure without retrying.
    termination_event = threading.Event()
    termination_event.set()
    call_count = {"count": 0}

    def bake_once() -> SliceBakeOutcome:
        call_count["count"] += 1
        return _bake_outcome(SliceBakeOutcomeStatus.FAILED, f"slice-{call_count['count']}")

    outcome = _run_bake_attempts(bake_once, attempt_count=3, termination_event=termination_event)
    assert outcome.status == SliceBakeOutcomeStatus.FAILED
    assert call_count["count"] == 1


def test_bake_worker_does_not_start_a_first_attempt_after_termination() -> None:
    # A worker still queued on the concurrency semaphore when the bake is terminated
    # must not start its first `mngr create` (a brand-new VM carve after the kill
    # sweep); it reports the slice as failed instead. The worker kwargs deliberately
    # omit everything _bake_one_slice requires, so any accidental bake attempt
    # fails loudly.
    termination_event = threading.Event()
    termination_event.set()
    outcome = _bake_one_slice_with_retry(termination_event=termination_event, server=_server(4, 16))
    assert outcome.status == SliceBakeOutcomeStatus.FAILED
    assert outcome.host_name == "slice-never-started"
    assert outcome.error is not None and "terminated before" in outcome.error


_TIER_CA = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFAKECAFAKECAFAKECAFAKECAFAKECAFAKECAFAKE minds-staging-ssh-ca"


def _gen1_trust(authorized_key_count: int) -> BoxManagementTrust:
    return BoxManagementTrust(authorized_key_count=authorized_key_count, trusted_ca_public_key=None)


def _gen2_trust(authorized_key_count: int = 0, trusted_ca_public_key: str | None = _TIER_CA) -> BoxManagementTrust:
    return BoxManagementTrust(authorized_key_count=authorized_key_count, trusted_ca_public_key=trusted_ca_public_key)


def test_choose_storage_passphrase_mints_fresh_only_for_a_plain_mounted_partition() -> None:
    encrypted = StorageVolumeState(mounted_source="/dev/mapper/mngr-storage", is_encrypted=True)
    # The mapper is mounted but the probe's block-device type read did not
    # confirm it as a crypt device.
    unconfirmed = StorageVolumeState(mounted_source="/dev/mapper/mngr-storage", is_encrypted=False)
    plain = StorageVolumeState(mounted_source="/dev/md4", is_encrypted=False)
    unmounted = StorageVolumeState(mounted_source=None, is_encrypted=False)

    def choose(storage_state: StorageVolumeState, vault_passphrase: str | None) -> str:
        return choose_storage_passphrase(
            storage_state=storage_state,
            vault_passphrase=vault_passphrase,
            fresh_passphrase="new",
            box_service_name="ns1",
        )

    # An encrypted volume and an unmounted (locked or empty) root keep Vault's
    # passphrase: it is the only thing that opens an existing header.
    assert choose(encrypted, "vault") == "vault"
    assert choose(unmounted, "vault") == "vault"
    # The prep formats only a non-mapper device, so a mounted mapper keeps
    # Vault's passphrase even when it was not confirmed as a crypt device: a
    # fresh one recorded there would replace the passphrase that really opens
    # the volume with one that opens nothing.
    assert choose(unconfirmed, "vault") == "vault"
    # A plain partition is about to be formatted, so a stale entry is replaced,
    # and a missing one is minted.
    assert choose(plain, "vault") == "new"
    assert choose(plain, None) == "new"
    # Without a Vault entry, a fresh passphrase could never open an existing
    # volume; refusing keeps Vault from recording one that opens nothing.
    with pytest.raises(BareMetalProvisioningError, match="ns1 already has an encrypted storage volume"):
        choose(encrypted, None)
    with pytest.raises(BareMetalProvisioningError, match="ns1 already has an encrypted storage volume"):
        choose(unconfirmed, None)
    with pytest.raises(BareMetalProvisioningError, match="ns1 has nothing mounted at its storage root"):
        choose(unmounted, None)


def test_assert_gen2_box_storage_is_encrypted_refuses_plain_and_locked_storage_on_gen2_only() -> None:
    base = _server(slot_count=6, cpu_threads=16)
    gen2 = base.model_copy_update(to_update(base.field_ref().box_generation, 2))
    assert_gen2_box_storage_is_encrypted(
        gen2, StorageVolumeState(mounted_source="/dev/mapper/mngr-storage", is_encrypted=True)
    )
    with pytest.raises(click.UsageError, match="unencrypted /dev/md4"):
        assert_gen2_box_storage_is_encrypted(gen2, StorageVolumeState(mounted_source="/dev/md4", is_encrypted=False))
    # The mapper is mounted but the probe's type read did not say crypt: still
    # refused, but the volume exists, so the operator is not told to repave.
    with pytest.raises(click.UsageError, match="could not confirm that mapper as a crypt device") as unconfirmed:
        assert_gen2_box_storage_is_encrypted(
            gen2, StorageVolumeState(mounted_source="/dev/mapper/mngr-storage", is_encrypted=False)
        )
    assert "Drain" not in str(unconfirmed.value)
    locked = StorageVolumeState(mounted_source=None, is_encrypted=False)
    with pytest.raises(click.UsageError, match="server unlock"):
        assert_gen2_box_storage_is_encrypted(gen2, locked)
    with pytest.raises(click.UsageError, match="server unlock"):
        assert_bake_box_storage_is_encrypted(
            gen2,
            MockSliceVmClient(box_address="15.204.140.221", box_ssh_user="slicehost", storage_volume_state=locked),
        )
    # A gen-1 box has no storage volume, so it is never probed (the mock's read
    # raises when it is called).
    assert_bake_box_storage_is_encrypted(base, MockSliceVmClient(box_address="15.204.140.221", box_ssh_user="lima"))


def test_unlock_is_on_the_cli_surface_and_documents_its_scope() -> None:
    result = CliRunner().invoke(server, ["unlock", "--help"])
    assert result.exit_code == 0, result.output
    assert "--server-id" in result.output
    assert "passphrase" in result.output
    assert "STORAGE_VOLUME_LOCKED" in result.output


def test_assert_box_is_exclusive_to_tier_accepts_a_single_key_and_same_tier_slices() -> None:
    mine = slice_disk_name(HostId.generate(), "staging")
    assert_box_is_exclusive_to_tier(
        server=_server(slot_count=6, cpu_threads=16),
        env_name="staging",
        box_disk_names={mine},
        trust=_gen1_trust(1),
        expected_ca_public_key=None,
    )


def test_assert_box_is_exclusive_to_tier_rejects_an_extra_authorized_key() -> None:
    # prep writes authorized_keys with a single-key overwrite, so a second key can
    # only have been added out of band -- it hands another tier SSH into this box.
    with pytest.raises(click.UsageError) as exc_info:
        assert_box_is_exclusive_to_tier(
            server=_server(slot_count=6, cpu_threads=16),
            env_name="staging",
            box_disk_names=set(),
            trust=_gen1_trust(2),
            expected_ca_public_key=None,
        )
    assert "authorizes 2 static SSH key(s)" in str(exc_info.value)
    assert "added out of band" in str(exc_info.value)
    # Re-prepping rewrites authorized_keys, so it must not be advised before the
    # operator has checked whether the other key's owner has slices running here.
    assert "Do NOT re-prep before checking" in str(exc_info.value)


def test_assert_box_is_exclusive_to_tier_rejects_an_empty_authorized_keys_as_never_prepped() -> None:
    # An empty authorized_keys is the opposite failure from an extra key: nothing was
    # added out of band, prep simply never ran, and re-prepping is the safe remedy.
    with pytest.raises(click.UsageError) as exc_info:
        assert_box_is_exclusive_to_tier(
            server=_server(slot_count=6, cpu_threads=16),
            env_name="staging",
            box_disk_names=set(),
            trust=_gen1_trust(0),
            expected_ca_public_key=None,
        )
    assert "authorizes 0 static SSH key(s)" in str(exc_info.value)
    assert "never prepped" in str(exc_info.value)
    assert "added out of band" not in str(exc_info.value)


def test_assert_box_is_exclusive_to_tier_on_gen2_wants_no_static_key_and_exactly_the_tier_ca() -> None:
    gen2 = _gen2_server_with_host_key()
    assert_box_is_exclusive_to_tier(
        server=gen2, env_name="staging", box_disk_names=set(), trust=_gen2_trust(), expected_ca_public_key=_TIER_CA
    )
    # A CA line differing only in its comment is the same CA.
    assert_box_is_exclusive_to_tier(
        server=gen2,
        env_name="staging",
        box_disk_names=set(),
        trust=_gen2_trust(trusted_ca_public_key=_TIER_CA.rsplit(" ", 1)[0] + " other-comment"),
        expected_ca_public_key=_TIER_CA,
    )
    # Any static key on a gen-2 box is out of band: management SSH is by certificate.
    with pytest.raises(click.UsageError) as exc_info:
        assert_box_is_exclusive_to_tier(
            server=gen2,
            env_name="staging",
            box_disk_names=set(),
            trust=_gen2_trust(authorized_key_count=1),
            expected_ca_public_key=_TIER_CA,
        )
    assert "expected exactly 0" in str(exc_info.value)
    assert "by certificate" in str(exc_info.value)
    # No CA trust at all: never prepped for certificates.
    with pytest.raises(click.UsageError) as exc_info:
        assert_box_is_exclusive_to_tier(
            server=gen2,
            env_name="staging",
            box_disk_names=set(),
            trust=_gen2_trust(trusted_ca_public_key=None),
            expected_ca_public_key=_TIER_CA,
        )
    assert "trusts no SSH CA" in str(exc_info.value)
    # A foreign CA: another party can mint certificates the box accepts.
    with pytest.raises(click.UsageError) as exc_info:
        assert_box_is_exclusive_to_tier(
            server=gen2,
            env_name="staging",
            box_disk_names=set(),
            trust=_gen2_trust(trusted_ca_public_key="ssh-ed25519 AAAAOTHER other-tier-ca"),
            expected_ca_public_key=_TIER_CA,
        )
    assert "not this tier's" in str(exc_info.value)
    # A tier with no committed CA cannot verify any gen-2 box.
    with pytest.raises(click.UsageError) as exc_info:
        assert_box_is_exclusive_to_tier(
            server=gen2, env_name="staging", box_disk_names=set(), trust=_gen2_trust(), expected_ca_public_key=None
        )
    assert "no SSH CA public key committed" in str(exc_info.value)


def test_assert_box_is_exclusive_to_tier_rejects_a_foreign_tier_slice() -> None:
    theirs = slice_disk_name(HostId.generate(), "dev-xiaq")
    with pytest.raises(click.UsageError) as exc_info:
        assert_box_is_exclusive_to_tier(
            server=_server(slot_count=6, cpu_threads=16),
            env_name="staging",
            box_disk_names={theirs},
            trust=_gen1_trust(1),
            expected_ca_public_key=None,
        )
    assert theirs in str(exc_info.value)
    assert "another tier" in str(exc_info.value)


def test_assert_box_is_exclusive_to_tier_allows_sibling_dev_envs_on_one_box() -> None:
    assert_box_is_exclusive_to_tier(
        server=_server(slot_count=6, cpu_threads=16),
        env_name="dev-josh",
        box_disk_names={
            slice_disk_name(HostId.generate(), "dev-josh"),
            slice_disk_name(HostId.generate(), "dev-alice"),
        },
        trust=_gen1_trust(1),
        expected_ca_public_key=None,
    )


def test_assert_box_is_exclusive_to_tier_still_checks_keys_for_an_unstamped_bake() -> None:
    # env_name None means a legacy un-stamped bake: the slice tier is unknowable so
    # that half is skipped, but the key check does not depend on our tier.
    assert_box_is_exclusive_to_tier(
        server=_server(slot_count=6, cpu_threads=16),
        env_name=None,
        box_disk_names={slice_disk_name(HostId.generate(), "dev-xiaq")},
        trust=_gen1_trust(1),
        expected_ca_public_key=None,
    )
    with pytest.raises(click.UsageError):
        assert_box_is_exclusive_to_tier(
            server=_server(slot_count=6, cpu_threads=16),
            env_name=None,
            box_disk_names=set(),
            trust=_gen1_trust(3),
            expected_ca_public_key=None,
        )


def _audit(
    server_id: str,
    *,
    public_address: str = "203.0.113.1",
    box_used_slots: int = 0,
    authorized_key_count: int = 0,
    expected_authorized_key_count: int = 0,
    is_storage_encrypted: bool = True,
) -> BoxTierAudit:
    return BoxTierAudit(
        server_id=server_id,
        public_address=public_address,
        slot_count=6,
        box_used_slots=box_used_slots,
        authorized_key_count=authorized_key_count,
        expected_authorized_key_count=expected_authorized_key_count,
        trusted_ca_public_key=None,
        is_trusted_ca_correct=True,
        foreign_tier_slices=(),
        degraded_md_arrays=(),
        raw_swap_devices=(),
        is_storage_encrypted=is_storage_encrypted,
    )


def test_build_box_tier_audit_report_counts_each_verdict_separately() -> None:
    # An unaudited box is NOT a clean one: it must never be folded into `exclusive`.
    clean = _audit("a", box_used_slots=1, authorized_key_count=1, expected_authorized_key_count=1)
    contaminated = _audit(
        "b", public_address="203.0.113.2", box_used_slots=2, authorized_key_count=2, expected_authorized_key_count=1
    )
    report = build_box_tier_audit_report(
        env_name="staging",
        audits=[clean, contaminated],
        unaudited=[UnauditedBox(server_id="c", public_address=None, reason="the row has no public_address")],
    )
    assert (report.exclusive, report.contaminated, report.unaudited) == (1, 1, 1)
    assert report.env_name == "staging"
    assert report.is_foreign_tier_checked


def _capacity(server_id: str, box_generation: int) -> BareMetalServerCapacity:
    base = _server(slot_count=6, cpu_threads=16)
    box = base.model_copy_update(
        to_update(base.field_ref().id, BareMetalServerDbId(server_id)),
        to_update(base.field_ref().box_generation, box_generation),
    )
    return BareMetalServerCapacity(server=box, used_slots=0, free_slots=6)


def test_plaintext_gen2_box_ids_lists_only_audited_gen2_boxes_reading_unencrypted() -> None:
    gen1_plain = "11111111-1111-1111-1111-111111111111"
    gen2_encrypted = "22222222-2222-2222-2222-222222222222"
    gen2_plain = "33333333-3333-3333-3333-333333333333"
    gen2_unaudited = "44444444-4444-4444-4444-444444444444"
    report = build_box_tier_audit_report(
        env_name="staging",
        # A gen-1 audit always reads unencrypted (no storage volume) and must not be listed.
        audits=[
            _audit(gen1_plain, is_storage_encrypted=False),
            _audit(gen2_encrypted),
            _audit(gen2_plain, is_storage_encrypted=False),
        ],
        unaudited=[UnauditedBox(server_id=gen2_unaudited, public_address=None, reason="unreachable")],
    )
    capacities = [
        _capacity(gen1_plain, 1),
        _capacity(gen2_encrypted, 2),
        _capacity(gen2_plain, 2),
        _capacity(gen2_unaudited, 2),
    ]
    assert plaintext_gen2_box_ids(report, capacities) == [gen2_plain]


def test_build_box_tier_audit_report_marks_the_foreign_tier_half_as_unchecked_without_an_env() -> None:
    # Without an env there is no tier to compare against, so an empty foreign-slice
    # list must not be readable as a clean bill of health.
    report = build_box_tier_audit_report(env_name=None, audits=[], unaudited=[])
    assert not report.is_foreign_tier_checked


def test_seed_phase_is_skipped_without_a_cache_tag() -> None:
    # A plain dev bake (no cache tag) never seeds: every slice builds for itself.
    assert _is_seed_phase_needed(MockBoxImageCache(), None) is False


def test_seed_phase_is_skipped_when_the_box_already_holds_the_tar() -> None:
    cache = MockBoxImageCache(tars_present={"default-workspace-template:content-abc"})
    assert _is_seed_phase_needed(cache, "default-workspace-template:content-abc") is False


def test_seed_phase_is_skipped_when_another_seeder_holds_the_build_lock() -> None:
    # E.g. the CI cache pre-warm job is mid-build: the fan-out slices wait on its
    # tar (or take over via the dead-seeder handoff), so no local seed phase.
    cache = MockBoxImageCache(locks_held={"default-workspace-template:content-abc"})
    assert _is_seed_phase_needed(cache, "default-workspace-template:content-abc") is False


def test_seed_phase_is_needed_when_the_tag_has_neither_tar_nor_lock() -> None:
    assert _is_seed_phase_needed(MockBoxImageCache(), "default-workspace-template:content-abc") is True


def test_compute_server_slice_sizing_gen2_sizes_from_units() -> None:
    gen2_server = _server(slot_count=14, cpu_threads=16, cpu_overcommit_ratio=2.0, disk_gb=1000)
    gen2_server = gen2_server.model_copy_update(
        to_update(gen2_server.field_ref().box_generation, 2),
        to_update(gen2_server.field_ref().ram_gb, 128),
    )
    default_sizing = compute_server_slice_sizing(gen2_server, None)
    # The default machine: 8 units = 8GiB RAM, a proportional vCPU share of
    # the 128GB box's 120-unit budget, and the 16 + 3.5GiB/unit data disk.
    assert default_sizing["units"] == 8
    assert default_sizing["total_units"] == 120
    # The guest boots with its units minus the 512 MiB holdback (the container cap follows).
    assert default_sizing["memory_mib"] == 8192 - 512
    assert default_sizing["disk_gib"] == 44
    assert default_sizing["vcpus"] == 2
    assert default_sizing["advertised_memory_gb"] == 8
    # The row's disk_gb is the measured storage partition; the budget is it minus the named reserve.
    assert default_sizing["disk_budget_gib"] == 1000 - 64
    assert default_sizing["row_memory_units"] == 8
    assert default_sizing["row_disk_gb"] == 44
    big_sizing = compute_server_slice_sizing(gen2_server, 64)
    assert big_sizing["units"] == 64
    assert big_sizing["memory_mib"] == 64 * 1024 - 512
    assert big_sizing["disk_gib"] == 16 + 224
    assert big_sizing["vcpus"] == 16


def test_compute_server_slice_sizing_rejects_disallowed_units_and_gen1_overrides() -> None:
    gen2_server = _server(slot_count=14, cpu_threads=16, disk_gb=1000)
    gen2_server = gen2_server.model_copy_update(
        to_update(gen2_server.field_ref().box_generation, 2),
        to_update(gen2_server.field_ref().ram_gb, 128),
    )
    with pytest.raises(BareMetalProvisioningError, match="multiple of 8"):
        compute_server_slice_sizing(gen2_server, 12)
    gen1_server = _server(slot_count=8, cpu_threads=16)
    with pytest.raises(BareMetalProvisioningError, match="generation 1"):
        compute_server_slice_sizing(gen1_server, 16)


def _gen2_server_with_host_key() -> BareMetalServer:
    server = _server(slot_count=14, cpu_threads=16, cpu_overcommit_ratio=2.0, disk_gb=1000)
    return server.model_copy_update(
        to_update(server.field_ref().box_generation, 2),
        to_update(server.field_ref().ram_gb, 128),
        to_update(server.field_ref().box_host_public_key, "ssh-ed25519 AAAAbox"),
    )


def _slice_create_overrides(server: BareMetalServer, runtime: SliceContainerRuntime | None) -> dict[str, str]:
    args = _build_slice_create_args(
        server=server,
        sizing=compute_server_slice_sizing(server, None),
        region="US-WEST-OR",
        env_name="dev-x",
        management_trust=SliceManagementTrust(
            trusted_user_ca_public_key="ssh-ed25519 AAAAtierca minds-dev-ssh-ca", pool_public_key=None
        ),
        private_key_path=Path("/tmp/pool-key"),
        ssh_user="slicehost",
        port_range_start=22000,
        port_range_end=32000,
        default_workspace_template_cache_tag=None,
        container_runtime=runtime,
        slice_host_id=HostId("host-0123456789abcdef0123456789abcdef"),
    )
    overrides = {}
    for flag, setting in zip(args[::2], args[1::2], strict=True):
        assert flag == "-S"
        key, value = setting.split("=", 1)
        overrides[key.removeprefix("providers.imbue_cloud_slice.")] = value
    return overrides


def test_resolve_slice_container_runtime_defaults_gen2_to_runsc_and_honors_the_runc_override() -> None:
    gen2_server = _gen2_server_with_host_key()
    assert resolve_slice_container_runtime(gen2_server, None) == SliceContainerRuntime.RUNSC
    assert resolve_slice_container_runtime(gen2_server, SliceContainerRuntime.RUNC) == SliceContainerRuntime.RUNC
    # A gen-1 (lima) guest has no runsc: no runtime knobs, and no override.
    gen1_server = _server(slot_count=8, cpu_threads=16)
    assert resolve_slice_container_runtime(gen1_server, None) is None
    with pytest.raises(BareMetalProvisioningError, match="generation 1"):
        resolve_slice_container_runtime(gen1_server, SliceContainerRuntime.RUNC)


def test_slice_create_args_carry_the_runtime_and_tmpfs_for_gen2_bakes_only() -> None:
    gen2_server = _gen2_server_with_host_key()
    runsc_overrides = _slice_create_overrides(gen2_server, SliceContainerRuntime.RUNSC)
    assert runsc_overrides["docker_runtime"] == "runsc"
    # The list rides as JSON: `-S` parses values as JSON first, so it lands on
    # the provider config as the start-args tuple.
    assert runsc_overrides["default_start_args"] == '["--tmpfs", "/run", "--tmpfs", "/tmp"]'
    assert runsc_overrides["box_generation"] == "2"
    # The runc comparison bake keeps the tmpfs mounts so the runtime is the only
    # variable between the two slices.
    runc_overrides = _slice_create_overrides(gen2_server, SliceContainerRuntime.RUNC)
    assert runc_overrides["docker_runtime"] == "runc"
    assert runc_overrides["default_start_args"] == runsc_overrides["default_start_args"]
    # A gen-1 bake gets neither knob (its lima bake would fail "unknown runtime").
    gen1_server = _server(slot_count=8, cpu_threads=16).model_copy_update(
        to_update(_server(slot_count=8, cpu_threads=16).field_ref().box_host_public_key, "ssh-ed25519 AAAAbox"),
    )
    gen1_overrides = _slice_create_overrides(gen1_server, None)
    assert "docker_runtime" not in gen1_overrides
    assert "default_start_args" not in gen1_overrides


def test_build_box_ssh_argv_pins_the_dial_and_forwards_the_remote_command() -> None:
    argv = build_box_ssh_argv(
        dial_host="127.0.0.1",
        dial_port=45123,
        ssh_user="debian",
        private_key_path=Path("/tmp/pool.pem"),
        host_key_options=("-o", "StrictHostKeyChecking=yes", "-o", "UserKnownHostsFile=/tmp/kh"),
        remote_command=("uptime",),
    )

    assert argv == snapshot(
        [
            "ssh",
            "-p",
            "45123",
            "-i",
            "/tmp/pool.pem",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "UserKnownHostsFile=/tmp/kh",
            "-o",
            "ConnectTimeout=30",
            "debian@127.0.0.1",
            "uptime",
        ]
    )


def test_build_box_ssh_argv_omits_a_remote_command_for_an_interactive_session() -> None:
    argv = build_box_ssh_argv(
        dial_host="51.81.208.81",
        dial_port=22,
        ssh_user="debian",
        private_key_path=Path("/tmp/pool.pem"),
        host_key_options=(),
        remote_command=(),
    )

    assert argv[-1] == "debian@51.81.208.81"


def test_box_ssh_host_key_options_quote_the_known_hosts_path_for_ssh() -> None:
    """ssh reads UserKnownHostsFile as a whitespace-separated list and splits the value itself.

    The path comes from ``tempfile.mkstemp``, which honours ``TMPDIR``, so it is not
    guaranteed space-free; the quotes are what keep it one file either way.
    """
    public_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI" + "A" * 20
    with _box_ssh_host_key_options("198.51.100.7", 22, public_key) as options:
        option = next(arg for arg in options if arg.startswith("UserKnownHostsFile="))

    value = option.removeprefix("UserKnownHostsFile=")
    assert value.startswith('"') and value.endswith('"'), option
    assert value.strip('"'), option


def test_slice_create_args_carry_the_tier_ca_for_gen2_and_the_pool_key_for_gen1() -> None:
    gen2_overrides = _slice_create_overrides(_gen2_server_with_host_key(), None)
    assert gen2_overrides["trusted_user_ca_public_key"] == "ssh-ed25519 AAAAtierca minds-dev-ssh-ca"
    assert "pool_authorized_public_key" not in gen2_overrides
    gen1 = SliceManagementTrust(trusted_user_ca_public_key=None, pool_public_key="ssh-ed25519 AAAApool pool")
    assert gen1.slice_create_overrides() == {"pool_authorized_public_key": "ssh-ed25519 AAAApool pool"}


def test_resolve_bake_management_trust_and_key_checks_the_committed_ca_before_the_identity() -> None:
    # A tier with no committed CA refuses a gen-2 bake with the bring-up pointer
    # BEFORE the operator identity (a Vault certificate sign) is resolved: the
    # resolver below has no tier, so resolving it would raise a different error.
    gen2_identities = make_test_management_identities(None, "unused")
    with pytest.raises(BareMetalProvisioningError, match=r"\[ssh_ca\] block"):
        resolve_bake_management_trust_and_key(_gen2_server_with_host_key(), gen2_identities)
    assert gen2_identities.operator_key_path is None

    # Gen-1 needs no CA: the trust is the pool public key derived from the key the bake dials with.
    private_key_pem, public_key = generate_ed25519_host_keypair()
    gen1_identities = make_test_management_identities(None, private_key_pem)
    try:
        trust, key_path = resolve_bake_management_trust_and_key(_server(slot_count=6, cpu_threads=16), gen1_identities)
        assert trust.trusted_user_ca_public_key is None
        assert trust.pool_public_key is not None
        assert trust.pool_public_key.split()[:2] == public_key.split()[:2]
        assert gen1_identities.gen1_key_dir is not None
        assert key_path == gen1_identities.gen1_key_dir / POOL_KEY_FILENAME
    finally:
        gen1_identities.close()


def test_ci_slice_sweep_lists_a_box_whose_management_key_cannot_be_resolved_as_unreachable_and_keeps_going() -> None:
    # The CI workflow runs the sweep non-activated (no tier), where a gen-2 box's
    # operator certificate cannot be resolved. That box must land in the report's
    # unreachable list like one whose sshd does not answer -- and the loop must
    # move on to the next box rather than raise out of the fleet sweep.
    first = _gen2_server_with_host_key()
    second = first.model_copy_update(
        to_update(first.field_ref().id, BareMetalServerDbId("22222222-2222-2222-2222-222222222222"))
    )
    report = sweep_ci_slices_across_boxes(
        [first, second], identities=make_test_management_identities(None, "unused"), max_age_hours=4.0
    )
    assert report.boxes == ()
    assert report.unreachable_boxes == (str(first.id), str(second.id))


def test_resolve_gen2_guest_image_defaults_to_the_pinned_mirror_artifact_and_requires_a_paired_override() -> None:
    assert _resolve_gen2_guest_image(None, None) == (
        DEFAULT_GEN2_SLICE_GUEST_IMAGE_URL,
        DEFAULT_GEN2_SLICE_GUEST_IMAGE_SHA512,
    )
    assert _resolve_gen2_guest_image("https://example.test/img.qcow2", "a" * 128) == (
        "https://example.test/img.qcow2",
        "a" * 128,
    )
    # An override URL without its digest would stage an unverified image.
    with pytest.raises(click.UsageError, match="must be given together"):
        _resolve_gen2_guest_image("https://example.test/img.qcow2", None)
    with pytest.raises(click.UsageError, match="must be given together"):
        _resolve_gen2_guest_image(None, "a" * 128)


def test_box_script_provisioning_error_is_unwrapped_from_its_concurrency_group() -> None:
    """A refused round trip surfaces as a group around the provisioning error; the best-effort
    steps after a gen-2 prep catch the plain error, so the wrapper must give it back."""
    refused = BareMetalProvisioningError("copying the box script to 203.0.113.9 failed (exit 255)")
    with_main = ConcurrencyExceptionGroup("box script", [refused], main_exception=refused)
    assert box_script_provisioning_error_or_none(with_main) is refused
    only_child = ConcurrencyExceptionGroup("box script", [refused])
    assert box_script_provisioning_error_or_none(only_child) is refused
    unrelated = ConcurrencyExceptionGroup("box script", [RuntimeError("worker died")])
    assert box_script_provisioning_error_or_none(unrelated) is None
    mixed = ConcurrencyExceptionGroup("box script", [refused, RuntimeError("worker died")])
    assert box_script_provisioning_error_or_none(mixed) is None
