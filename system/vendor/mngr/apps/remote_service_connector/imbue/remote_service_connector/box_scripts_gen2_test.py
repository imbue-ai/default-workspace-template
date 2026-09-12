from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_INSTANCES_DIR
from imbue.mngr_imbue_cloud.slices.gen2_scripts.testing import assert_valid_bash
from imbue.mngr_imbue_cloud.slices.gen2_scripts.transfer import DATADISK_OBJECT
from imbue.mngr_imbue_cloud.slices.gen2_scripts.transfer import DISK_OBJECT
from imbue.mngr_imbue_cloud.slices.gen2_scripts.transfer import TRANSFER_DIR_ROOT
from imbue.remote_service_connector import box_scripts_gen2

_INSTANCE = "mngr-slice-test-" + "a" * 32


def test_gen2_upload_script_streams_both_qcow2_disks_and_the_meta_tar() -> None:
    script = box_scripts_gen2.render_gen2_upload_script(_INSTANCE)
    assert_valid_bash(script)
    assert f'"$SLICE_DIR/disk.qcow2" "{DISK_OBJECT}" DISK' in script
    assert f'"$SLICE_DIR/datadisk.qcow2" "{DATADISK_OBJECT}" DATADISK' in script
    assert GEN2_INSTANCES_DIR in script
    # The meta tar bundles the cidata material + env, never the disks.
    for name in box_scripts_gen2.GEN2_META_TAR_FILES:
        assert f"$WS_INSTANCE/{name}" in script
    assert "STAGE uploaded" in script
    assert "set -Eeuo pipefail" in script


def test_gen2_upload_script_records_the_disks_virtual_sizes() -> None:
    script = box_scripts_gen2.render_gen2_upload_script(_INSTANCE)
    assert "VIRTUAL_BYTES_DISK" in script
    assert "VIRTUAL_BYTES_DATADISK" in script
    assert 'qemu-img info --output=json "$SLICE_DIR/datadisk.qcow2"' in script


def test_gen2_stop_commands_halt_and_disable_via_the_recorded_ordinal() -> None:
    commands = box_scripts_gen2.build_gen2_stop_vm_commands(_INSTANCE)
    joined = " && ".join(commands)
    assert_valid_bash(joined)
    assert "MNGR_SLICE_ORDINAL=" in joined
    # Separate exact-argument stop + disable (the scoped sudoers cannot match
    # flags like --now), and disable is the gen-2 boot-autostart stop marker.
    assert 'sudo /usr/bin/systemctl stop "mngr-slice@$ordinal"' in commands
    assert 'sudo /usr/bin/systemctl disable "mngr-slice@$ordinal"' in commands


def test_gen2_cancel_and_restart_reenables_boots_and_waits_for_both_banners() -> None:
    commands = box_scripts_gen2.build_gen2_cancel_and_restart_commands(_INSTANCE, 23000, 23001)
    joined = " && ".join(commands)
    assert_valid_bash(joined)
    assert 'sudo /usr/bin/systemctl enable "mngr-slice@$ordinal"' in commands
    assert 'sudo /usr/bin/systemctl start "mngr-slice@$ordinal"' in commands
    assert "/dev/tcp/127.0.0.1/23000" in joined
    assert "/dev/tcp/127.0.0.1/23001" in joined
    # The stale transfer (pid + dir) is cleared before the boot.
    assert joined.index("rm -rf") < joined.index("systemctl start")


def test_gen2_instance_exists_command_probes_the_instance_dir() -> None:
    command = box_scripts_gen2.build_gen2_instance_exists_command(_INSTANCE)
    assert_valid_bash(command)
    assert command == f"[ -d {GEN2_INSTANCES_DIR}/{_INSTANCE} ]"


def test_gen2_finalize_and_cleanup_commands_are_the_idempotent_destroy() -> None:
    finalize_commands = box_scripts_gen2.build_gen2_finalize_stop_commands(_INSTANCE)
    # The supervisor runs the tuple as one ``&&``-joined line; the join itself
    # must parse (the multi-line destroy script rides in a subshell).
    assert_valid_bash(" && ".join(finalize_commands))
    joined = "\n".join(finalize_commands)
    assert 'sudo /usr/bin/systemctl stop "mngr-slice@$ordinal"' in joined
    assert 'sudo /usr/bin/systemctl disable "mngr-slice@$ordinal"' in joined
    assert 'rm -rf "$slice_dir"' in joined
    assert TRANSFER_DIR_ROOT in joined
    assert box_scripts_gen2.build_gen2_cleanup_reserved_restore_commands(_INSTANCE) == finalize_commands
