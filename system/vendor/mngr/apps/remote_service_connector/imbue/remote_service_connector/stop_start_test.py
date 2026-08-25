import logging
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from uuid import UUID

import pytest

import imbue.remote_service_connector.storage as connector_storage_module
from imbue.remote_service_connector import box_scripts
from imbue.remote_service_connector.stop_start import _WATCHDOG_BACKOFF_CAP_SECONDS
from imbue.remote_service_connector.stop_start import _redrive_delay_seconds
from imbue.remote_service_connector.stop_start import _take_over_transition
from imbue.remote_service_connector.stop_start import run_transition_supervisor
from imbue.remote_service_connector.stop_start import run_transition_watchdog
from imbue.remote_service_connector.testing import _make_pool_test_client
from imbue.remote_service_connector.testing import make_storage_config

_WS_ID = UUID("00000000-0000-0000-0000-00000000bb01")
_BOX_ID = UUID("00000000-0000-0000-0000-00000000cc01")

_TEST_IDENTITY = "AGE-SECRET-KEY-1TESTIDENTITY"
_TEST_TRANSITION_ID = "11111111-2222-3333-4444-555555555555"

_FINAL_UPLOAD_STATUS = (
    "STAGE=uploaded\nFINISHED=1\n"
    "SHA_DISK=aa11\nBYTES_DISK=100\n"
    "SHA_DATADISK=bb22\nBYTES_DATADISK=50\n"
    "SHA_META=cc33\nBYTES_META=10\n"
)
_FINAL_DOWNLOAD_STATUS = "STAGE=started\nFINISHED=1\n"


def _seed_stopping_workspace(backend: Any) -> Any:
    backend.add_box(_BOX_ID, public_address="10.9.9.9", region="vin")
    row = backend.add_available_host(
        host_id=_WS_ID,
        version="v1",
        vps_address="10.9.9.9",
        ssh_port=22000,
        container_ssh_port=22001,
        host_id_str="host-" + "f" * 32,
        region="US-EAST-VA",
    )
    row.status = "stopping"
    row.leased_to_user = "0123456789abcdef"
    row.stop_requested_at = datetime.now(timezone.utc) - timedelta(seconds=10)
    row.bare_metal_server_id = _BOX_ID
    row.lima_instance_name = "mngr-slice-test-" + "f" * 32
    row.lima_disk_name = "mngr-slice-test-" + "f" * 32 + "-data"
    row.transition_id = _TEST_TRANSITION_ID
    return row


def _completed_manifest(row: Any, generation: int = 1) -> dict[str, Any]:
    return {
        "generation": generation,
        "key_prefix": f"{row.host_id_str}/gen-{generation}",
        "age_recipient": "age1qtestrecipient",
        "source_vm_ssh_port": 22000,
        "source_container_ssh_port": 22001,
        "object_by_name": {
            "DISK": {"sha256": "aa11", "size_bytes": 100},
            "DATADISK": {"sha256": "bb22", "size_bytes": 50},
            "META": {"sha256": "cc33", "size_bytes": 10},
        },
    }


def test_stop_supervisor_uploads_finalizes_and_frees_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    _client, backend = _make_pool_test_client(monkeypatch)
    backend.storage_config = make_storage_config()
    row = _seed_stopping_workspace(backend)
    # First status read: nothing yet (so the upload gets launched); then done.
    backend.transfer_status_sequence = ["", _FINAL_UPLOAD_STATUS]

    outcome = run_transition_supervisor(str(_WS_ID), _TEST_TRANSITION_ID)

    assert outcome == "stopped"
    assert row.status == "stopped"
    assert row.stopped_at is not None
    assert row.vps_address is None
    assert row.ssh_port is None
    assert row.container_ssh_port is None
    assert row.bare_metal_server_id is None
    assert row.artifact_generation == 1
    assert row.transition_failure_count == 0
    assert row.wrapped_dek is not None
    assert row.artifact_manifest["object_by_name"]["DISK"]["sha256"] == "aa11"
    # The identity round-trips through the KEK wrap.
    unwrapped = connector_storage_module.unwrap_dek(backend.storage_config, row.wrapped_dek)
    assert unwrapped == _TEST_IDENTITY
    # The VM was halted, the upload launched, and the slot freed.
    joined_commands = "\n".join(backend.box_command_log)
    assert "limactl stop" in joined_commands
    assert "setsid nohup bash upload.sh" in joined_commands
    assert "limactl delete --force" in joined_commands
    assert f"{row.lima_instance_name}/env" in backend.box_file_writes
    assert f"{row.lima_instance_name}/upload.sh" in backend.box_file_writes


def test_second_stop_after_restore_mints_a_new_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    """A restore leaves the completed generation's manifest + wrapped dek on the
    leased row. A later stop must NOT reuse them (that would upload over the
    workspace's only artifact and then delete it as the 'previous' generation):
    it mints gen-2 material, uploads there, and deletes only gen-1."""
    _client, backend = _make_pool_test_client(monkeypatch)
    backend.storage_config = make_storage_config()
    row = _seed_stopping_workspace(backend)
    row.artifact_generation = 1
    row.artifact_manifest = _completed_manifest(row)
    old_wrapped = connector_storage_module.wrap_dek(backend.storage_config, "AGE-SECRET-KEY-1OLDIDENTITY")
    row.wrapped_dek = old_wrapped
    backend.transfer_status_sequence = ["", _FINAL_UPLOAD_STATUS]

    outcome = run_transition_supervisor(str(_WS_ID), _TEST_TRANSITION_ID)

    assert outcome == "stopped"
    assert row.status == "stopped"
    assert row.artifact_generation == 2
    assert row.artifact_manifest["generation"] == 2
    assert row.artifact_manifest["key_prefix"] == f"{row.host_id_str}/gen-2"
    # Fresh per-stop material, not the restored generation's.
    assert row.wrapped_dek != old_wrapped
    assert connector_storage_module.unwrap_dek(backend.storage_config, row.wrapped_dek) == _TEST_IDENTITY
    # The upload env targets gen-2, and only the superseded gen-1 was deleted.
    assert "gen-2" in backend.box_file_writes[f"{row.lima_instance_name}/env"]
    assert backend.deleted_prefixes == [f"{row.host_id_str}/gen-1/"]


def test_stop_supervisor_relaunches_upload_over_stale_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stale finished status (a failed earlier attempt, or a leftover download
    status from a previous restore onto this box) must not be mistaken for a
    completed upload: the re-driven supervisor relaunches and the stop lands."""
    _client, backend = _make_pool_test_client(monkeypatch)
    backend.storage_config = make_storage_config()
    row = _seed_stopping_workspace(backend)
    backend.transfer_status_sequence = [
        "STAGE=failed\nFINISHED=1\nERROR=old transient failure\n",
        _FINAL_UPLOAD_STATUS,
    ]

    outcome = run_transition_supervisor(str(_WS_ID), _TEST_TRANSITION_ID)

    assert outcome == "stopped"
    assert row.status == "stopped"
    assert "setsid nohup bash upload.sh" in "\n".join(backend.box_command_log)


def test_stop_supervisor_fails_promptly_when_transfer_dies_mid_way(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dead transfer that left only a partial status must fail the run promptly
    (with the error recorded for the client), not spin until the transfer
    deadline."""
    _client, backend = _make_pool_test_client(monkeypatch)
    backend.storage_config = make_storage_config()
    row = _seed_stopping_workspace(backend)
    backend.transfer_status_sequence = ["STAGE=uploading\nFINISHED=0\n"]
    backend.transfer_alive = False

    outcome = run_transition_supervisor(str(_WS_ID), _TEST_TRANSITION_ID)

    assert outcome == "stop-failed"
    assert row.status == "stopping"
    assert "died" in (row.transition_error or "")
    assert row.transition_failure_count == 1


def test_supervisor_with_a_stale_token_exits_without_touching_anything(monkeypatch: pytest.MonkeyPatch) -> None:
    """A supervisor whose transition was taken over (fresh transition_id on the
    row) must exit immediately: no box commands, no DB writes -- in particular
    it can never stamp its own failure over the live transition's state."""
    _client, backend = _make_pool_test_client(monkeypatch)
    backend.storage_config = make_storage_config()
    row = _seed_stopping_workspace(backend)
    row.transition_id = "99999999-8888-7777-6666-555555555555"

    outcome = run_transition_supervisor(str(_WS_ID), _TEST_TRANSITION_ID)

    assert outcome == "superseded"
    assert row.status == "stopping"
    assert row.transition_error is None
    assert backend.box_command_log == []


def test_stop_lands_on_stopped_promptly_and_a_start_during_retention_supersedes_the_finalize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The row reaches ``stopped`` the moment the upload verifies -- placement
    and the local VM kept for the retention window -- and a start within that
    window fences the finalize out, leaving the VM for the restart."""
    _client, backend = _make_pool_test_client(monkeypatch)
    backend.storage_config = make_storage_config(retention_seconds=3600)
    row = _seed_stopping_workspace(backend)
    row.stop_requested_at = datetime.now(timezone.utc)
    backend.transfer_status_sequence = ["", _FINAL_UPLOAD_STATUS]

    observed_status_at_first_wait: list[tuple[str, str | None]] = []

    def start_takes_over(_seconds: float) -> None:
        observed_status_at_first_wait.append((row.status, row.vps_address))
        # A user start: the endpoint CAS's stopped -> starting under a fresh
        # fencing token, which must stop this supervisor from finalizing.
        row.status = "starting"
        row.transition_id = "99999999-8888-7777-6666-555555555555"

    backend.sleep_callback = start_takes_over

    outcome = run_transition_supervisor(str(_WS_ID), _TEST_TRANSITION_ID)

    assert outcome == "superseded"
    # The stop itself landed durably before the takeover, with placement kept.
    assert observed_status_at_first_wait == [("stopped", "10.9.9.9")]
    # The superseded supervisor must not have finalized anything: VM intact.
    assert row.status == "starting"
    assert row.vps_address == "10.9.9.9"
    assert row.bare_metal_server_id == _BOX_ID
    assert "limactl delete --force" not in "\n".join(backend.box_command_log)


def test_supervisor_resumes_retention_finalize_for_a_stopped_row_with_a_box_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``stopped`` row still holding its box link (its previous supervisor
    died after landing the stop) gets its retention finalize resumed: local VM
    deleted, previous generation dropped, placement cleared."""
    _client, backend = _make_pool_test_client(monkeypatch)
    backend.storage_config = make_storage_config()
    row = _seed_stopping_workspace(backend)
    row.status = "stopped"
    row.stopped_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    row.artifact_generation = 2
    row.artifact_manifest = _completed_manifest(row, generation=2)

    placement_when_vm_deleted: list[str | None] = []

    def record_placement_at_delete(command: str) -> None:
        if "limactl delete --force" in command:
            placement_when_vm_deleted.append(row.vps_address)

    backend.box_command_callback = record_placement_at_delete

    outcome = run_transition_supervisor(str(_WS_ID), _TEST_TRANSITION_ID)

    assert outcome == "stopped"
    assert row.status == "stopped"
    assert row.vps_address is None
    assert row.bare_metal_server_id is None
    assert backend.deleted_prefixes == [f"{row.host_id_str}/gen-1/"]
    assert "limactl delete --force" in "\n".join(backend.box_command_log)
    # The placement was cleared (under the ownership CAS) *before* the VM
    # deletion ran: a start landing mid-deletion can only take the restore
    # path, never restart-in-place a VM that is being destroyed.
    assert placement_when_vm_deleted == [None]


def test_retention_finalize_refuses_to_delete_the_vm_without_a_complete_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``stopped`` row whose recorded manifest names no uploaded objects (a
    legacy start claimed from ``stopping`` that failed back to ``stopped``) has
    no durable artifact: the finalize must keep the local VM -- the only
    bootable copy, and the restart-in-place recovery path -- and record the
    failure instead."""
    _client, backend = _make_pool_test_client(monkeypatch)
    backend.storage_config = make_storage_config()
    row = _seed_stopping_workspace(backend)
    row.status = "stopped"
    row.stopped_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    skeleton = _completed_manifest(row, generation=1)
    del skeleton["object_by_name"]
    row.artifact_manifest = skeleton

    outcome = run_transition_supervisor(str(_WS_ID), _TEST_TRANSITION_ID)

    assert outcome == "finalize-failed"
    assert row.status == "stopped"
    assert row.vps_address == "10.9.9.9"
    assert row.bare_metal_server_id == _BOX_ID
    assert "manifest is incomplete" in (row.transition_error or "")
    assert row.transition_failure_count == 1
    assert backend.deleted_prefixes == []
    assert "limactl delete --force" not in "\n".join(backend.box_command_log)


def test_start_supervisor_restarts_in_place_when_vm_still_local(monkeypatch: pytest.MonkeyPatch) -> None:
    """A start within the retention window boots the halted local VM and drops
    the recorded stop artifact (the booted VM immediately diverges from it),
    stepping the generation counter back to the previous durable one."""
    _client, backend = _make_pool_test_client(monkeypatch)
    backend.storage_config = make_storage_config()
    row = _seed_stopping_workspace(backend)
    row.status = "starting"
    row.stopped_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    row.artifact_generation = 2
    row.artifact_manifest = _completed_manifest(row, generation=2)
    row.wrapped_dek = connector_storage_module.wrap_dek(backend.storage_config, _TEST_IDENTITY)
    backend.vm_exists_on_origin = True

    outcome = run_transition_supervisor(str(_WS_ID), _TEST_TRANSITION_ID)

    assert outcome == "restarted-in-place"
    assert row.status == "leased"
    assert row.stop_requested_at is None
    assert row.stopped_at is None
    assert row.vps_address == "10.9.9.9"
    assert row.artifact_manifest is None
    assert row.wrapped_dek is None
    # The recorded (now diverged) generation is dropped and the counter steps
    # back to the previous durable one.
    assert backend.deleted_prefixes == [f"{row.host_id_str}/gen-2/"]
    assert row.artifact_generation == 1
    assert "limactl --log-level=warn start" in "\n".join(backend.box_command_log)


def test_start_supervisor_restart_in_place_without_a_recorded_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    """A legacy in-flight row (start claimed before its stop recorded a
    manifest) still restarts in place, dropping the partial next generation."""
    _client, backend = _make_pool_test_client(monkeypatch)
    backend.storage_config = make_storage_config()
    row = _seed_stopping_workspace(backend)
    row.status = "starting"
    backend.vm_exists_on_origin = True

    outcome = run_transition_supervisor(str(_WS_ID), _TEST_TRANSITION_ID)

    assert outcome == "restarted-in-place"
    assert row.status == "leased"
    assert backend.deleted_prefixes == [f"{row.host_id_str}/gen-1/"]
    assert row.artifact_generation == 0


def _seed_stopped_workspace(backend: Any) -> Any:
    backend.add_box(_BOX_ID, public_address="10.9.9.9", region="vin")
    row = backend.add_available_host(
        host_id=_WS_ID,
        version="v1",
        host_id_str="host-" + "f" * 32,
        region="US-EAST-VA",
    )
    row.status = "starting"
    row.leased_to_user = "0123456789abcdef"
    row.stopped_at = datetime.now(timezone.utc) - timedelta(hours=1)
    row.vps_address = None
    row.ssh_port = None
    row.container_ssh_port = None
    row.bare_metal_server_id = None
    row.lima_instance_name = "mngr-slice-test-" + "f" * 32
    row.lima_disk_name = "mngr-slice-test-" + "f" * 32 + "-data"
    row.artifact_generation = 1
    row.artifact_manifest = _completed_manifest(row)
    row.wrapped_dek = connector_storage_module.wrap_dek(backend.storage_config, _TEST_IDENTITY)
    row.transition_id = _TEST_TRANSITION_ID
    return row


def test_start_supervisor_restores_onto_new_box(monkeypatch: pytest.MonkeyPatch) -> None:
    _client, backend = _make_pool_test_client(monkeypatch)
    backend.storage_config = make_storage_config()
    row = _seed_stopped_workspace(backend)
    backend.vm_exists_on_origin = False
    backend.reserve_stdout = "MNGR_RESTORE_RESERVED 23000 23001\n"
    backend.transfer_status_sequence = [_FINAL_DOWNLOAD_STATUS]

    outcome = run_transition_supervisor(str(_WS_ID), _TEST_TRANSITION_ID)

    assert outcome == "restored"
    assert row.status == "leased"
    assert row.stopped_at is None
    assert row.vps_address == "10.9.9.9"
    assert row.ssh_port == 23000
    assert row.container_ssh_port == 23001
    assert row.bare_metal_server_id == _BOX_ID
    # The download identity reached the box env, and the download launched.
    env_text = backend.box_file_writes[f"{row.lima_instance_name}/env"]
    assert _TEST_IDENTITY in env_text
    assert "setsid nohup bash download.sh" in "\n".join(backend.box_command_log)
    # The transfer dir (creds + status) is removed once the restore is done.
    assert f'rm -rf "$HOME/{box_scripts.TRANSFER_DIR_ROOT}/{row.lima_instance_name}"' in backend.box_command_log


def test_start_supervisor_failure_lands_back_on_stopped_with_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _client, backend = _make_pool_test_client(monkeypatch)
    backend.storage_config = make_storage_config()
    row = _seed_stopped_workspace(backend)
    backend.reserve_rc = 1
    backend.reserve_stderr = "reserve exploded"

    outcome = run_transition_supervisor(str(_WS_ID), _TEST_TRANSITION_ID)

    assert outcome == "start-failed"
    assert row.status == "stopped"
    assert "reserve exploded" in (row.transition_error or "")
    assert row.transition_failure_count == 1
    assert row.vps_address is None
    # The hard reserve failure may have half-claimed the slot: the box is
    # rolled back (instance dir, disk dir, and the staged creds/env dir).
    joined_commands = "\n".join(backend.box_command_log)
    assert f"rm -rf $HOME/.lima/{row.lima_instance_name}" in joined_commands
    assert f"rm -rf $HOME/.lima/_disks/{row.lima_disk_name}" in joined_commands
    assert f'rm -rf "$HOME/{box_scripts.TRANSFER_DIR_ROOT}/{row.lima_instance_name}"' in joined_commands


def test_failed_start_within_retention_keeps_the_placement_for_the_next_try(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A start within the retention window that fails lands back on ``stopped``
    with the box link and placement intact: the halted local VM is still there,
    so the next try can restart in place instead of restoring from scratch."""
    _client, backend = _make_pool_test_client(monkeypatch)
    backend.storage_config = make_storage_config()
    row = _seed_stopping_workspace(backend)
    row.status = "starting"
    row.stopped_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    row.artifact_generation = 1
    row.artifact_manifest = _completed_manifest(row)
    backend.vm_exists_on_origin = True
    backend.box_command_should_fail_matching = "limactl --log-level=warn start"

    outcome = run_transition_supervisor(str(_WS_ID), _TEST_TRANSITION_ID)

    assert outcome == "start-failed"
    assert row.status == "stopped"
    assert "limactl" in (row.transition_error or "")
    assert row.transition_failure_count == 1
    assert row.vps_address == "10.9.9.9"
    assert row.bare_metal_server_id == _BOX_ID
    assert row.artifact_manifest is not None


def test_start_supervisor_superseded_mid_download_rolls_back_the_claimed_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A release/abandon that flips the row away from 'starting' mid-download
    must roll the candidate box back (instance dir, disk dir, staged creds):
    the row's box link never pointed at that box, so nothing else can ever
    reclaim the claimed slot."""
    _client, backend = _make_pool_test_client(monkeypatch)
    backend.storage_config = make_storage_config()
    row = _seed_stopped_workspace(backend)
    backend.vm_exists_on_origin = False
    backend.reserve_stdout = "MNGR_RESTORE_RESERVED 23000 23001\n"
    backend.transfer_status_sequence = ["STAGE=downloading\nFINISHED=0\n"]
    backend.transfer_alive = True

    def flip_to_removing(_seconds: float) -> None:
        row.status = "removing"

    backend.sleep_callback = flip_to_removing

    outcome = run_transition_supervisor(str(_WS_ID), _TEST_TRANSITION_ID)

    assert outcome == "superseded"
    assert row.status == "removing"
    joined_commands = "\n".join(backend.box_command_log)
    # The detached download is stopped and the (possibly booted) VM deleted
    # before the dirs go, so the rollback never strands a waiter on the box's
    # download lock or a ghost qemu on unlinked inodes.
    assert "kill -TERM" in joined_commands
    assert f"limactl delete --force {row.lima_instance_name}" in joined_commands
    assert f"rm -rf $HOME/.lima/{row.lima_instance_name}" in joined_commands
    assert f"rm -rf $HOME/.lima/_disks/{row.lima_disk_name}" in joined_commands
    assert f'rm -rf "$HOME/{box_scripts.TRANSFER_DIR_ROOT}/{row.lima_instance_name}"' in joined_commands


def test_start_supervisor_rollback_warns_when_the_restore_vm_survives(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A rollback whose ``limactl delete`` leaves the instance behind keeps the
    dirs (the box reports the survivor) and the supervisor logs it, so the
    leftover VM is visible to ops instead of silently occupying the slot."""
    _client, backend = _make_pool_test_client(monkeypatch)
    backend.storage_config = make_storage_config()
    row = _seed_stopped_workspace(backend)
    backend.vm_exists_on_origin = False
    backend.reserve_stdout = "MNGR_RESTORE_RESERVED 23000 23001\n"
    backend.transfer_status_sequence = ["STAGE=failed\nFINISHED=1\nERROR=command failed: limactl start\n"]
    backend.cleanup_vm_survives = True

    with caplog.at_level(logging.WARNING):
        outcome = run_transition_supervisor(str(_WS_ID), _TEST_TRANSITION_ID)

    assert outcome == "start-failed"
    assert row.status == "stopped"
    assert "limactl start" in (row.transition_error or "")
    survivor_warnings = [record for record in caplog.records if "survived the rollback" in record.getMessage()]
    assert len(survivor_warnings) == 1
    assert row.lima_instance_name in survivor_warnings[0].getMessage()


def test_start_supervisor_superseded_at_final_cas_tears_down_the_booted_vm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A release/abandon landing after the download boots the VM but before the
    final CAS records its placement must delete the booted VM outright
    (limactl delete, not just a directory rollback): the row's box link never
    pointed at that box, so nothing else can ever reclaim the running VM or
    its slot."""
    _client, backend = _make_pool_test_client(monkeypatch)
    backend.storage_config = make_storage_config()
    row = _seed_stopped_workspace(backend)
    backend.vm_exists_on_origin = False
    backend.reserve_stdout = "MNGR_RESTORE_RESERVED 23000 23001\n"
    backend.transfer_status_sequence = [_FINAL_DOWNLOAD_STATUS]

    def flip_when_download_finishes(command: str) -> None:
        # The finished-status read happens after the poll iteration's guarded
        # heartbeat and before the final CAS -- exactly the window a
        # concurrent release can hit.
        if command.startswith("cat") and '/status"' in command:
            row.status = "removing"

    backend.box_command_callback = flip_when_download_finishes

    outcome = run_transition_supervisor(str(_WS_ID), _TEST_TRANSITION_ID)

    assert outcome == "superseded"
    assert row.status == "removing"
    joined_commands = "\n".join(backend.box_command_log)
    assert f"limactl delete --force {row.lima_instance_name}" in joined_commands
    assert f"limactl disk delete --force {row.lima_disk_name}" in joined_commands
    assert f'rm -rf "$HOME/{box_scripts.TRANSFER_DIR_ROOT}/{row.lima_instance_name}"' in joined_commands


def test_start_supervisor_reports_no_capacity_when_every_box_is_full(monkeypatch: pytest.MonkeyPatch) -> None:
    _client, backend = _make_pool_test_client(monkeypatch)
    backend.storage_config = make_storage_config()
    row = _seed_stopped_workspace(backend)
    backend.reserve_rc = 4
    backend.reserve_stderr = "MNGR_RESTORE_BOX_FULL 6/6"

    outcome = run_transition_supervisor(str(_WS_ID), _TEST_TRANSITION_ID)

    assert outcome == "start-failed"
    assert row.status == "stopped"
    assert "no capacity" in (row.transition_error or "")
    # The staged env (S3 creds + age identity) is removed from the skipped box.
    assert f'rm -rf "$HOME/{box_scripts.TRANSFER_DIR_ROOT}/{row.lima_instance_name}"' in backend.box_command_log


def test_watchdog_takes_over_stale_transitions_under_a_fresh_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _client, backend = _make_pool_test_client(monkeypatch)
    backend.storage_config = make_storage_config()
    row = _seed_stopping_workspace(backend)
    row.transition_heartbeat_at = None

    redriven = run_transition_watchdog()

    assert redriven == 1
    assert backend.spawned_supervisors == [str(row.host_id)]
    # The takeover minted a fresh fencing token, stamped it on the row, and
    # handed exactly that token to the spawned supervisor.
    spawned_id, spawned_token = backend.spawned_supervisor_tokens[0]
    assert spawned_id == str(row.host_id)
    assert spawned_token == row.transition_id
    assert row.transition_id != _TEST_TRANSITION_ID


def test_watchdog_leaves_rows_with_a_live_heartbeat_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    _client, backend = _make_pool_test_client(monkeypatch)
    backend.storage_config = make_storage_config()
    row = _seed_stopping_workspace(backend)
    row.transition_heartbeat_at = datetime.now(timezone.utc)

    redriven = run_transition_watchdog()

    assert redriven == 0
    assert backend.spawned_supervisors == []
    assert row.transition_id == _TEST_TRANSITION_ID


def test_watchdog_backs_off_a_persistently_failing_transition(monkeypatch: pytest.MonkeyPatch) -> None:
    """A transition with many consecutive failures is not re-driven on every
    tick: its heartbeat must be staler than the backed-off delay first."""
    _client, backend = _make_pool_test_client(monkeypatch)
    backend.storage_config = make_storage_config()
    row = _seed_stopping_workspace(backend)
    # Stale by the base threshold (120s) but within the backed-off delay.
    row.transition_heartbeat_at = datetime.now(timezone.utc) - timedelta(seconds=300)
    row.transition_failure_count = 6

    redriven = run_transition_watchdog()

    assert redriven == 0
    assert backend.spawned_supervisors == []


def test_takeover_leaves_a_settled_row_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """A transition that completed between the watchdog's candidate read and
    its claim (completion nulls the heartbeat, which would otherwise read as
    stale) must not get a fresh token stamped onto its settled row."""
    _client, backend = _make_pool_test_client(monkeypatch)
    backend.storage_config = make_storage_config()
    row = _seed_stopping_workspace(backend)
    row.status = "leased"
    row.transition_heartbeat_at = None

    assert _take_over_transition(str(_WS_ID)) is None
    assert row.transition_id == _TEST_TRANSITION_ID
    assert row.transition_heartbeat_at is None


def test_redrive_delay_saturates_at_the_cap_for_any_failure_count() -> None:
    # The delay reaches the cap once doubling passes it, and an arbitrarily
    # large count must yield the same cap (not a float-pow overflow, which
    # would crash the whole watchdog run).
    assert _redrive_delay_seconds(8) == _WATCHDOG_BACKOFF_CAP_SECONDS
    assert _redrive_delay_seconds(10_000) == _WATCHDOG_BACKOFF_CAP_SECONDS


def test_watchdog_escalates_a_transition_that_keeps_failing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _client, backend = _make_pool_test_client(monkeypatch)
    backend.storage_config = make_storage_config()
    row = _seed_stopping_workspace(backend)
    row.transition_heartbeat_at = None
    row.transition_failure_count = 8

    with caplog.at_level(logging.ERROR, logger="imbue.remote_service_connector.stop_start"):
        redriven = run_transition_watchdog()

    assert redriven == 1
    assert any("needs operator attention" in record.message for record in caplog.records)


def test_watchdog_skips_unconfigured_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    _client, backend = _make_pool_test_client(monkeypatch)
    backend.storage_config = None
    row = _seed_stopping_workspace(backend)
    row.transition_heartbeat_at = None

    redriven = run_transition_watchdog()

    assert redriven == 0
    assert backend.spawned_supervisors == []


def test_start_supervisor_tries_the_next_box_after_a_hard_reserve_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One broken/drifted candidate box (e.g. missing transfer tooling) must not
    fail the whole start: its claim is rolled back and the remaining candidates
    are tried. Observed live on dev: a box missing s5cmd hard-failed the reserve
    and the start gave up although a healthy box had 13 free slots."""
    _client, backend = _make_pool_test_client(monkeypatch)
    backend.storage_config = make_storage_config()
    row = _seed_stopped_workspace(backend)
    backend.add_box(UUID("00000000-0000-0000-0000-00000000cc02"), public_address="10.9.9.10", region="vin")
    backend.vm_exists_on_origin = False
    backend.transfer_status_sequence = [_FINAL_DOWNLOAD_STATUS]
    # First reserve attempt (whichever box the shuffle picks first) hard-fails
    # the way a box without s5cmd does; the second succeeds.
    reserve_attempts: list[str] = []

    def fail_first_reserve(command: str) -> None:
        if "reserve.sh" in command:
            reserve_attempts.append(command)
            if len(reserve_attempts) == 1:
                backend.reserve_rc = 1
                backend.reserve_stderr = "reserve.sh: line 59: s5cmd: command not found"
            else:
                backend.reserve_rc = 0
                backend.reserve_stderr = ""

    backend.box_command_callback = fail_first_reserve

    outcome = run_transition_supervisor(str(_WS_ID), _TEST_TRANSITION_ID)

    assert outcome == "restored"
    assert row.status == "leased"
    assert len(reserve_attempts) == 2
    # The failed candidate was rolled back (instance dir, disk dir, staged creds).
    joined_commands = "\n".join(backend.box_command_log)
    assert f"rm -rf $HOME/.lima/{row.lima_instance_name}" in joined_commands


def test_start_supervisor_reports_the_last_hard_failure_when_every_box_is_broken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _client, backend = _make_pool_test_client(monkeypatch)
    backend.storage_config = make_storage_config()
    row = _seed_stopped_workspace(backend)
    backend.reserve_rc = 1
    backend.reserve_stderr = "reserve.sh: line 59: s5cmd: command not found"

    outcome = run_transition_supervisor(str(_WS_ID), _TEST_TRANSITION_ID)

    assert outcome == "start-failed"
    assert row.status == "stopped"
    assert "s5cmd: command not found" in (row.transition_error or "")
    assert "1 candidate(s) tried" in (row.transition_error or "")
