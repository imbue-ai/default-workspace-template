from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from uuid import UUID

import pytest

import imbue.remote_service_connector.storage as connector_storage_module
from imbue.remote_service_connector import box_scripts
from imbue.remote_service_connector.stop_start import run_transition_supervisor
from imbue.remote_service_connector.stop_start import run_transition_watchdog
from imbue.remote_service_connector.testing import _make_pool_test_client
from imbue.remote_service_connector.testing import make_storage_config

_WS_ID = UUID("00000000-0000-0000-0000-00000000bb01")
_BOX_ID = UUID("00000000-0000-0000-0000-00000000cc01")

_TEST_IDENTITY = "AGE-SECRET-KEY-1TESTIDENTITY"

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
    return row


def _completed_manifest(row: Any) -> dict[str, Any]:
    return {
        "generation": 1,
        "key_prefix": f"{row.host_id_str}/gen-1",
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

    outcome = run_transition_supervisor(str(_WS_ID))

    assert outcome == "stopped"
    assert row.status == "stopped"
    assert row.vps_address is None
    assert row.ssh_port is None
    assert row.container_ssh_port is None
    assert row.bare_metal_server_id is None
    assert row.artifact_generation == 1
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

    outcome = run_transition_supervisor(str(_WS_ID))

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

    outcome = run_transition_supervisor(str(_WS_ID))

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

    outcome = run_transition_supervisor(str(_WS_ID))

    assert outcome == "stop-failed"
    assert row.status == "stopping"
    assert "died" in (row.transition_error or "")


def test_stop_supervisor_superseded_by_restart_during_retention(monkeypatch: pytest.MonkeyPatch) -> None:
    _client, backend = _make_pool_test_client(monkeypatch)
    backend.storage_config = make_storage_config(retention_seconds=3600)
    row = _seed_stopping_workspace(backend)
    row.stop_requested_at = datetime.now(timezone.utc)
    backend.transfer_status_sequence = ["", _FINAL_UPLOAD_STATUS]

    def flip_to_starting(_seconds: float) -> None:
        row.status = "starting"

    backend.sleep_callback = flip_to_starting

    outcome = run_transition_supervisor(str(_WS_ID))

    assert outcome == "superseded"
    # The supervisor must not have finalized anything: VM intact, row untouched.
    assert row.status == "starting"
    assert row.vps_address == "10.9.9.9"
    assert "limactl delete --force" not in "\n".join(backend.box_command_log)


def test_start_supervisor_restarts_in_place_when_vm_still_local(monkeypatch: pytest.MonkeyPatch) -> None:
    _client, backend = _make_pool_test_client(monkeypatch)
    backend.storage_config = make_storage_config()
    row = _seed_stopping_workspace(backend)
    row.status = "starting"
    # A stale stopped_at from an earlier completed cycle must be cleared on
    # the way back to leased: _fail_start_back_to_stopped uses it to tell a
    # failed start-from-stopped from a failed in-window restart.
    row.stopped_at = datetime.now(timezone.utc) - timedelta(days=1)
    backend.vm_exists_on_origin = True

    outcome = run_transition_supervisor(str(_WS_ID))

    assert outcome == "restarted-in-place"
    assert row.status == "leased"
    assert row.stop_requested_at is None
    assert row.stopped_at is None
    assert row.vps_address == "10.9.9.9"
    # The pending (canceled) generation's partial objects are dropped.
    assert backend.deleted_prefixes == [f"{row.host_id_str}/gen-1/"]
    assert "limactl --log-level=warn start" in "\n".join(backend.box_command_log)


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
    return row


def test_start_supervisor_restores_onto_new_box(monkeypatch: pytest.MonkeyPatch) -> None:
    _client, backend = _make_pool_test_client(monkeypatch)
    backend.storage_config = make_storage_config()
    row = _seed_stopped_workspace(backend)
    backend.vm_exists_on_origin = False
    backend.reserve_stdout = "MNGR_RESTORE_RESERVED 23000 23001\n"
    backend.transfer_status_sequence = [_FINAL_DOWNLOAD_STATUS]

    outcome = run_transition_supervisor(str(_WS_ID))

    assert outcome == "restored"
    assert row.status == "leased"
    # Cleared so a later failed in-window restart is not mistaken for a
    # failed start-from-stopped (see _fail_start_back_to_stopped).
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

    outcome = run_transition_supervisor(str(_WS_ID))

    assert outcome == "start-failed"
    assert row.status == "stopped"
    assert "reserve exploded" in (row.transition_error or "")
    assert row.vps_address is None
    # The hard reserve failure may have half-claimed the slot: the box is
    # rolled back (instance dir, disk dir, and the staged creds/env dir).
    joined_commands = "\n".join(backend.box_command_log)
    assert f"rm -rf $HOME/.lima/{row.lima_instance_name}" in joined_commands
    assert f"rm -rf $HOME/.lima/_disks/{row.lima_disk_name}" in joined_commands
    assert f'rm -rf "$HOME/{box_scripts.TRANSFER_DIR_ROOT}/{row.lima_instance_name}"' in joined_commands


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

    outcome = run_transition_supervisor(str(_WS_ID))

    assert outcome == "superseded"
    assert row.status == "removing"
    joined_commands = "\n".join(backend.box_command_log)
    assert f"rm -rf $HOME/.lima/{row.lima_instance_name}" in joined_commands
    assert f"rm -rf $HOME/.lima/_disks/{row.lima_disk_name}" in joined_commands
    assert f'rm -rf "$HOME/{box_scripts.TRANSFER_DIR_ROOT}/{row.lima_instance_name}"' in joined_commands


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
        # The finished-status read happens after the poll iteration's DB
        # status check and before the final CAS -- exactly the window a
        # concurrent release can hit.
        if command.startswith("cat") and '/status"' in command:
            row.status = "removing"

    backend.box_command_callback = flip_when_download_finishes

    outcome = run_transition_supervisor(str(_WS_ID))

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

    outcome = run_transition_supervisor(str(_WS_ID))

    assert outcome == "start-failed"
    assert row.status == "stopped"
    assert "no capacity" in (row.transition_error or "")
    # The staged env (S3 creds + age identity) is removed from the skipped box.
    assert f'rm -rf "$HOME/{box_scripts.TRANSFER_DIR_ROOT}/{row.lima_instance_name}"' in backend.box_command_log


def test_watchdog_redrives_stale_transitions(monkeypatch: pytest.MonkeyPatch) -> None:
    _client, backend = _make_pool_test_client(monkeypatch)
    backend.storage_config = make_storage_config()
    row = _seed_stopping_workspace(backend)
    row.transition_heartbeat_at = None

    redriven = run_transition_watchdog()

    assert redriven == 1
    assert backend.spawned_supervisors == [str(row.host_id)]


def test_watchdog_skips_unconfigured_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    _client, backend = _make_pool_test_client(monkeypatch)
    backend.storage_config = None
    row = _seed_stopping_workspace(backend)
    row.transition_heartbeat_at = None

    redriven = run_transition_watchdog()

    assert redriven == 0
    assert backend.spawned_supervisors == []
