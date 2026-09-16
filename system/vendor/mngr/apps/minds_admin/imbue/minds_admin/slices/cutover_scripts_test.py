import base64
import io
import json
import os
import platform
import subprocess
import tarfile
import tomllib
from collections.abc import Sequence
from pathlib import Path

import pytest
from inline_snapshot import snapshot

from imbue.imbue_common.model_update import to_update
from imbue.minds_admin.slices.cutover_scripts import HARVEST_FILE_MARKER
from imbue.minds_admin.slices.cutover_scripts import LATCHKEY_DIR_PRESENT_MARKER
from imbue.minds_admin.slices.cutover_scripts import LATCHKEY_DISK_REPLAY_TAR_PATH
from imbue.minds_admin.slices.cutover_scripts import LATCHKEY_HARVEST_FILE_MARKER
from imbue.minds_admin.slices.cutover_scripts import LATCHKEY_TMPFS_REPLAY_TAR_PATH
from imbue.minds_admin.slices.cutover_scripts import TRANSPLANT_DONE_MARKER
from imbue.minds_admin.slices.cutover_scripts import authorized_keys_without
from imbue.minds_admin.slices.cutover_scripts import build_banner_wait_command
from imbue.minds_admin.slices.cutover_scripts import build_container_id_command
from imbue.minds_admin.slices.cutover_scripts import build_container_key_harvest_command
from imbue.minds_admin.slices.cutover_scripts import build_disk_materialize_command
from imbue.minds_admin.slices.cutover_scripts import build_docker_create_args
from imbue.minds_admin.slices.cutover_scripts import build_gen1_datadisk_info_command
from imbue.minds_admin.slices.cutover_scripts import build_git_describe_command
from imbue.minds_admin.slices.cutover_scripts import build_home_layout_probe_command
from imbue.minds_admin.slices.cutover_scripts import build_image_load_command
from imbue.minds_admin.slices.cutover_scripts import build_image_publish_command
from imbue.minds_admin.slices.cutover_scripts import build_latchkey_replay_tar
from imbue.minds_admin.slices.cutover_scripts import build_latchkey_tar_extract_command
from imbue.minds_admin.slices.cutover_scripts import build_replayed_container_files
from imbue.minds_admin.slices.cutover_scripts import build_stage_replayed_container_files_command
from imbue.minds_admin.slices.cutover_scripts import build_transplant_clear_command
from imbue.minds_admin.slices.cutover_scripts import build_transplant_rescue_command
from imbue.minds_admin.slices.cutover_scripts import build_unit_enable_command
from imbue.minds_admin.slices.cutover_scripts import build_vm_gateway_port_probe_command
from imbue.minds_admin.slices.cutover_scripts import build_vm_key_harvest_command
from imbue.minds_admin.slices.cutover_scripts import build_vm_latchkey_harvest_command
from imbue.minds_admin.slices.cutover_scripts import build_vm_latchkey_supervisor_status_command
from imbue.minds_admin.slices.cutover_scripts import build_workspace_version_probe_script
from imbue.minds_admin.slices.cutover_scripts import container_name_from_inspect
from imbue.minds_admin.slices.cutover_scripts import container_ssh_host_port_from_inspect
from imbue.minds_admin.slices.cutover_scripts import cutover_image_object_key
from imbue.minds_admin.slices.cutover_scripts import cutover_transplant_dir
from imbue.minds_admin.slices.cutover_scripts import extract_autostart_installer_commands
from imbue.minds_admin.slices.cutover_scripts import extract_slice_volume_home_path
from imbue.minds_admin.slices.cutover_scripts import extract_template_replay_inputs
from imbue.minds_admin.slices.cutover_scripts import home_layout_error_or_none
from imbue.minds_admin.slices.cutover_scripts import latchkey_gateway_files_error_or_none
from imbue.minds_admin.slices.cutover_scripts import latchkey_replay_detail
from imbue.minds_admin.slices.cutover_scripts import latchkey_tunnel_port_error_or_none
from imbue.minds_admin.slices.cutover_scripts import migration_rollback_key_prefix
from imbue.minds_admin.slices.cutover_scripts import parse_docker_inspect
from imbue.minds_admin.slices.cutover_scripts import parse_latchkey_harvest_output
from imbue.minds_admin.slices.cutover_scripts import parse_marked_files
from imbue.minds_admin.slices.cutover_scripts import parse_qemu_img_info
from imbue.minds_admin.slices.cutover_scripts import parse_supervisorctl_not_running
from imbue.minds_admin.slices.cutover_scripts import parse_supervisorctl_unhealthy
from imbue.minds_admin.slices.cutover_scripts import render_gen2_disk_transplant_script
from imbue.minds_admin.slices.cutover_scripts import replayed_container_dirs
from imbue.minds_admin.slices.cutover_scripts import staged_container_dir_path
from imbue.minds_admin.slices.cutover_scripts import staged_container_file_path
from imbue.minds_admin.slices.cutover_scripts import tunnel_conf_container_ssh_port
from imbue.minds_admin.slices.cutover_types import CutoverError
from imbue.minds_admin.slices.cutover_types import HarvestedFile
from imbue.minds_admin.slices.cutover_types import HarvestedLatchkeyState
from imbue.minds_admin.slices.cutover_types import LatchkeyReplayPlan
from imbue.minds_admin.slices.cutover_types import VM_LATCHKEY_DIR
from imbue.minds_admin.slices.cutover_types import VM_LATCHKEY_SUPERVISOR_CONF_DIR
from imbue.minds_admin.slices.cutover_types import VM_LATCHKEY_TMPFS_DIR
from imbue.minds_admin.slices.testing import HARVESTED_TUNNEL_CONTAINER_PORT
from imbue.minds_admin.slices.testing import make_harvested_file
from imbue.minds_admin.slices.testing import make_harvested_keys
from imbue.minds_admin.slices.testing import make_harvested_latchkey_state
from imbue.mngr.providers.ssh_host_setup import SSHD_PROVISIONED_MARKER_PATH
from imbue.mngr_imbue_cloud.slices.gen2_scripts.testing import assert_valid_bash
from imbue.mngr_latchkey.remote.provisioning import MACHINE_LATCHKEY_DISK_FILENAMES
from imbue.mngr_latchkey.remote.provisioning import MACHINE_LATCHKEY_SUPERVISOR_CONF_FILENAMES
from imbue.mngr_latchkey.remote.provisioning import MACHINE_LATCHKEY_TMPFS_FILENAMES
from imbue.mngr_vps.container_setup import CONTAINER_ENTRYPOINT_CMD

_INSTANCE = "mngr-slice-dev-josh-" + "b" * 16
_HOST_HEX = "3f2a" * 8
_TRANSFER_DIR = f"/home/slicehost/.mngr-transfers/{_INSTANCE}"
_TRANSPLANT_DIR = f"/srv/mngr-slices/cutover/{_INSTANCE}"
# The slice clients send every box command as ``PATH=<dirs> <command>``; a
# command that starts with a reserved word is a syntax error under that prefix.
_BOX_COMMAND_PATH_PREFIX = "PATH=/usr/local/bin:$HOME/.local/bin:$PATH "


def _assert_valid_box_command(command: str) -> None:
    assert_valid_bash(command)
    assert_valid_bash(_BOX_COMMAND_PATH_PREFIX + command)


def _inspect_entry() -> dict:
    return {
        "Name": f"/mngr-bake-slice-{_HOST_HEX}",
        "Config": {
            "Labels": {
                "com.imbue.mngr.host-id": f"host-{_HOST_HEX}",
                "com.imbue.mngr.host-name": f"slice-{_HOST_HEX}",
                "com.imbue.mngr.provider": "imbue_cloud_slice",
                "com.imbue.mngr.tags": "{}",
            },
            "Env": ["PATH=/root/.local/bin:/usr/bin", "CLAUDE_CODE_VERSION=2.1.227"],
            "Image": "sha256:deadbeef",
        },
        "HostConfig": {
            "PortBindings": {"22/tcp": [{"HostIp": "0.0.0.0", "HostPort": "2222"}]},
            "RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
            "Runtime": "runc",
        },
        "Mounts": [
            {"Type": "volume", "Name": f"mngr-host-vol-{_HOST_HEX}", "Destination": "/mngr-vol", "RW": True},
            {
                "Type": "volume",
                "Name": f"mngr-snapshot-trigger-{_HOST_HEX}",
                "Destination": "/mngr-snapshot",
                "RW": True,
            },
            {"Type": "bind", "Source": "/mngr-btrfs/snapshots", "Destination": "/mngr-snapshots", "RW": False},
        ],
    }


def test_rollback_and_image_keys_live_under_the_cutover_prefix() -> None:
    assert migration_rollback_key_prefix("dev-josh/", "host-abc") == "dev-josh/cutover/host-abc/rollback"
    assert migration_rollback_key_prefix("", "host-abc") == "cutover/host-abc/rollback"
    assert cutover_image_object_key("dev-josh/", "minds-v0.4.2") == "dev-josh/cutover/images/minds-v0.4.2.tar.zst"


def test_transplant_script_builds_a_gen2_layout_and_receives_the_home_subvolume() -> None:
    assert cutover_transplant_dir(_INSTANCE) == _TRANSPLANT_DIR
    script = render_gen2_disk_transplant_script(
        transfer_dir_path=_TRANSFER_DIR,
        transplant_dir_path=_TRANSPLANT_DIR,
        host_hex=_HOST_HEX,
        migrated_data_disk_gib=44,
        expected_datadisk_sha256="aa11",
    )
    assert_valid_bash(script)
    # The env file is the only thing read from the (root-partition) transfer
    # dir; the images are downloaded and built on the storage partition.
    assert f"TD={_TRANSFER_DIR}\n" in script
    assert '. "$TD/env"' in script
    assert f"WORK={_TRANSPLANT_DIR}\n" in script
    assert "install -d -m 751 -o slicehost -g slicehost /srv/mngr-slices/cutover" in script
    # The work dir is service-user-owned from creation (not only on success),
    # so the service-user clear command can always remove it.
    assert 'install -d -m 700 -o slicehost -g slicehost "$WORK"' in script
    assert "$HOME" not in script
    assert 'cat "s3://$WS_BUCKET/$WS_KEY_PREFIX/datadisk.zst.age"' in script
    assert "!= 'aa11' ]" in script or "!= aa11 ]" in script
    assert 'qemu-img create -q -f qcow2 "$NEW_IMG_PARTIAL" 44G' in script
    # A failed attach names the image and carries qemu-nbd's own error, not just "no free device".
    assert (
        'echo "qemu-nbd could not attach $image on any free nbd device: ${last_error:-no free device}" >&2' in script
    )
    assert 'OLD_DEV=$(attach_nbd "$OLD_IMG") || exit 1' in script
    assert 'mkfs.btrfs -q -L mngr-data "$NEW_DEV"' in script
    assert 'btrfs quota enable --simple "$NEW_MNT"' in script
    assert 'btrfs qgroup create 1/0 "$NEW_MNT"' in script
    assert script.index("btrfs quota enable") < script.index("btrfs send")
    # The partitioned gen-1 disk is read from its first partition, whose node
    # is waited for (the box has no parted, so the table is re-read with blockdev).
    assert 'if [ -b "${OLD_DEV}p1" ]; then' in script
    assert 'blockdev --rereadpt "$OLD_DEV"' in script
    assert "partprobe" not in script
    assert script.index("udevadm settle") < script.index('blockdev --rereadpt "$OLD_DEV"') < script.index("mkfs.btrfs")
    assert f'btrfs subvolume snapshot -r "$OLD_MNT/{_HOST_HEX}" "$OLD_MNT/{_HOST_HEX}-cutover-ro"' in script
    assert f'btrfs send "$OLD_MNT/{_HOST_HEX}-cutover-ro" | btrfs receive "$NEW_MNT/"' in script
    # The received subvolume keeps received_uuid; only a forced flip makes it writable.
    assert f'btrfs property set -f -ts "$NEW_MNT/{_HOST_HEX}" ro false' in script
    # A freshly attached nbd device reports size 0 for a moment; the format must not race it.
    assert "never reported a size after attaching" in script
    assert 'btrfs qgroup assign "0/$subvolume_id" 1/0 "$NEW_MNT"' in script
    assert 'mkdir -p "$NEW_MNT/snapshots"' in script
    # The prepared disk is handed to the slice service user and only then takes
    # its final name (a crashed attempt leaves a .partial, never a datadisk.qcow2);
    # the gen-1 copy goes.
    assert 'rmdir "$OLD_MNT" "$NEW_MNT"' in script
    assert 'chown slicehost:slicehost "$NEW_IMG_PARTIAL"' in script
    assert script.index('chmod 660 "$NEW_IMG_PARTIAL"') < script.index('mv "$NEW_IMG_PARTIAL" "$NEW_IMG"')
    assert 'rm -f "$IDF" "$OLD_IMG" "$NEW_IMG_PARTIAL"' in script
    assert f'echo "{TRANSPLANT_DONE_MARKER} $(stat -c %s "$NEW_IMG")"' in script


def test_disk_materialize_and_unit_commands_target_the_slice_dir() -> None:
    command = build_disk_materialize_command(_INSTANCE, _TRANSPLANT_DIR)
    _assert_valid_box_command(command)
    assert (
        f"cp --reflink=auto /srv/mngr-slices/base/debian-13-base.qcow2 /srv/mngr-slices/instances/{_INSTANCE}/disk.qcow2"
        in command
    )
    assert f"qemu-img resize -q /srv/mngr-slices/instances/{_INSTANCE}/disk.qcow2 10G" in command
    # Same filesystem as the slice dir: the move is a rename, not a copy.
    assert f"mv {_TRANSPLANT_DIR}/datadisk.qcow2 /srv/mngr-slices/instances/{_INSTANCE}/datadisk.qcow2" in command
    # Exact-argument sudoers: enable is its own invocation.
    assert build_unit_enable_command(3) == "sudo /usr/bin/systemctl enable mngr-slice@3"
    wait = build_banner_wait_command(22010, 600)
    _assert_valid_box_command(wait)
    # The while loop rides inside ``bash -c``: bare, it would be a syntax error under the PATH prefix.
    assert wait.startswith("bash -c ")
    assert "/dev/tcp/127.0.0.1/22010" in wait
    assert "exit 7" in wait


def test_image_publish_and_load_commands_source_the_transfer_env() -> None:
    publish = build_image_publish_command(
        transfer_dir_path="/home/slicehost/.mngr-transfers/images",
        tar_path="/srv/mngr-slices/image-cache/default-workspace-template-minds-v0.4.2.tar",
        image_object_key="dev-josh/cutover/images/minds-v0.4.2.tar.zst",
    )
    _assert_valid_box_command(publish)
    assert ". /home/slicehost/.mngr-transfers/images/env" in publish
    assert 'pipe "s3://$WS_BUCKET/dev-josh/cutover/images/minds-v0.4.2.tar.zst"' in publish
    load = build_image_load_command(
        transfer_dir_path=f"/home/slicehost/.mngr-transfers/{_INSTANCE}",
        image_object_key="dev-josh/cutover/images/minds-v0.4.2.tar.zst",
        transfer_key_path="/srv/mngr-slices/image-cache/.transfer-abc",
        vm_ssh_port=22010,
    )
    _assert_valid_box_command(load)
    assert "| zstd -q -d | ssh -i /srv/mngr-slices/image-cache/.transfer-abc" in load
    assert "-p 22010 root@127.0.0.1 'docker load'" in load


def test_replayed_container_files_carry_the_harvested_trust_material_and_the_provisioned_marker() -> None:
    keys = make_harvested_keys()
    files_by_path = {
        replayed.container_path: replayed
        for replayed in build_replayed_container_files(
            keys, ssh_ca_public_key="ssh-ed25519 AAAAca tier-ca", pool_public_key="ssh-ed25519 AAAAPOOL pool"
        )
    }
    assert files_by_path["/etc/ssh/ssh_host_ed25519_key"].content == keys.container_host_private_key.get_secret_value()
    assert files_by_path["/etc/ssh/ssh_host_ed25519_key"].mode == "0600"
    assert files_by_path["/etc/ssh/ssh_host_ed25519_key.pub"].content == keys.container_host_public_key
    assert files_by_path["/etc/ssh/ssh_host_ed25519_key.pub"].mode == "0644"
    assert files_by_path["/root/.ssh/authorized_keys"].content == keys.container_authorized_keys
    assert files_by_path["/root/.ssh/authorized_keys"].mode == "0600"
    # The gen-2 target authorizes no static management key: the container trusts
    # the tier CA (container principal) the way the bake would have installed it.
    assert files_by_path["/etc/ssh/mngr_user_ca.pub"].content == "ssh-ed25519 AAAAca tier-ca\n"
    assert (
        "TrustedUserCAKeys /etc/ssh/mngr_user_ca.pub"
        in files_by_path["/etc/ssh/sshd_config.d/61-mngr-user-ca.conf"].content
    )
    assert files_by_path["/etc/ssh/principals/root"].content == "mngr-container\n"
    # The self-healing entrypoint only restarts sshd behind this marker; without it
    # the container would come back from a VM reboot or a connector start unreachable.
    assert files_by_path[SSHD_PROVISIONED_MARKER_PATH].content == ""
    assert len(files_by_path) == 7
    # The files are staged per container directory (the replay copies each
    # directory's contents, since the image has no /root/.ssh for a lone-file
    # docker cp to land in); one VM round trip writes them all with their modes.
    replayed_files = tuple(files_by_path.values())
    assert replayed_container_dirs(replayed_files) == (
        "/etc/ssh",
        "/root/.ssh",
        "/etc/ssh/sshd_config.d",
        "/etc/ssh/principals",
    )
    assert staged_container_dir_path("/tmp/x", "/root/.ssh") == "/tmp/x/root_.ssh"
    assert staged_container_file_path("/tmp/x", files_by_path["/root/.ssh/authorized_keys"]) == (
        "/tmp/x/root_.ssh/authorized_keys"
    )
    stage = build_stage_replayed_container_files_command("/tmp/mngr-cutover-keys-abc", replayed_files)
    assert_valid_bash(stage)
    assert stage.startswith(
        "umask 077 && mkdir -p /tmp/mngr-cutover-keys-abc/etc_ssh /tmp/mngr-cutover-keys-abc/root_.ssh "
        "/tmp/mngr-cutover-keys-abc/etc_ssh_sshd_config.d /tmp/mngr-cutover-keys-abc/etc_ssh_principals && "
    )
    assert "chmod 0600 /tmp/mngr-cutover-keys-abc/etc_ssh/ssh_host_ed25519_key &&" in stage
    assert "chmod 0644 /tmp/mngr-cutover-keys-abc/etc_ssh/ssh_host_ed25519_key.pub" in stage
    assert "chmod 0600 /tmp/mngr-cutover-keys-abc/root_.ssh/authorized_keys" in stage
    assert stage.endswith("chmod 0644 /tmp/mngr-cutover-keys-abc/etc_ssh/mngr_host_provisioned")
    assert base64.b64encode(keys.container_authorized_keys.encode()).decode() in stage


def test_harvest_commands_mark_each_file_and_parse_back() -> None:
    vm_command = build_vm_key_harvest_command()
    assert_valid_bash(vm_command)
    assert vm_command.count(HARVEST_FILE_MARKER) == 3
    container_command = build_container_key_harvest_command("abc123")
    assert_valid_bash(container_command)
    assert container_command.count("docker exec --workdir / abc123") == 3
    # Each file is followed by one echoed newline, and a missing file still fails the chain.
    assert container_command.count("cat /root/.ssh/authorized_keys && echo") == 1
    # The echo after each file shows up as a blank line when the file ended in a
    # newline (the key files) and supplies the missing one otherwise (an
    # authorized_keys without a trailing newline): both come back newline-terminated.
    output = (
        "noise\n"
        f"{HARVEST_FILE_MARKER} /etc/ssh/ssh_host_ed25519_key\n-----BEGIN-----\nkey\n-----END-----\n\n"
        f"{HARVEST_FILE_MARKER} /etc/ssh/ssh_host_ed25519_key.pub\nssh-ed25519 AAAA host\n\n"
        f"{HARVEST_FILE_MARKER} /root/.ssh/authorized_keys\nssh-ed25519 BBBB one\nssh-ed25519 CCCC two\n"
    )
    assert parse_marked_files(output) == snapshot(
        {
            "/etc/ssh/ssh_host_ed25519_key": "-----BEGIN-----\nkey\n-----END-----\n",
            "/etc/ssh/ssh_host_ed25519_key.pub": "ssh-ed25519 AAAA host\n",
            "/root/.ssh/authorized_keys": "ssh-ed25519 BBBB one\nssh-ed25519 CCCC two\n",
        }
    )
    assert parse_marked_files("") == {}


def test_probe_commands_address_the_container_by_label_and_workspace_checkout() -> None:
    assert build_container_id_command(f"host-{_HOST_HEX}") == (
        f"docker ps -aq --filter label=com.imbue.mngr.host-id=host-{_HOST_HEX}"
    )
    describe = build_git_describe_command("abc123")
    assert_valid_bash(describe)
    assert "safe.directory=/home/user/workspace" in describe
    assert "describe --tags --match" in describe and "minds-v*" in describe
    assert "/home/user/workspace/system/vendor/mngr/apps/minds/imbue/minds/build_info.py" in describe
    info = build_gen1_datadisk_info_command(f"{_INSTANCE}-data")
    assert_valid_bash(info)
    assert info == f'qemu-img info -U --output=json "$HOME"/.lima/_disks/{_INSTANCE}-data/datadisk'


def test_parse_qemu_img_info_reads_the_top_level_keys_only() -> None:
    qemu_10_style = json.dumps(
        {
            "children": [{"name": "file", "info": {"virtual-size": 197120, "format": "file"}}],
            "virtual-size": 30064771072,
            "format": "qcow2",
        }
    )
    assert parse_qemu_img_info(qemu_10_style) == ("qcow2", 30064771072)
    with pytest.raises(CutoverError):
        parse_qemu_img_info("not json")
    with pytest.raises(CutoverError):
        parse_qemu_img_info(json.dumps({"format": "raw"}))


def test_parse_supervisorctl_unhealthy_accepts_exited_one_shots_and_lists_the_rest() -> None:
    output = (
        "app_watcher                      RUNNING   pid 41, uptime 0:10:01\n"
        "eval-worker                      EXITED    Aug 28 10:00 AM\n"
        "system_interface                 STARTING\n"
        "browser                          BACKOFF   Exited too quickly (process log may have details)\n"
        "terminal                         FATAL     Exited too quickly\n"
        "files                            RUNNING   pid 44, uptime 0:10:00\n"
    )
    assert parse_supervisorctl_unhealthy(output) == [
        "system_interface STARTING",
        "browser BACKOFF",
        "terminal FATAL",
    ]
    assert parse_supervisorctl_unhealthy("web RUNNING pid 1, uptime 0:00:01\n\n") == []


def test_parse_supervisorctl_unhealthy_reports_an_unreachable_supervisord_verbatim() -> None:
    # supervisorctl prints this (to stdout) when supervisord is not running at all.
    output = "unix:///var/run/supervisor.sock no such file\n"
    assert parse_supervisorctl_unhealthy(output) == ["unix:///var/run/supervisor.sock no such file"]


def test_docker_create_args_keep_identity_and_mounts_and_apply_the_gen2_overrides() -> None:
    entry = parse_docker_inspect(json.dumps([_inspect_entry()]))
    assert container_name_from_inspect(entry) == f"mngr-bake-slice-{_HOST_HEX}"
    args = build_docker_create_args(entry, image_tag="default-workspace-template:minds-v0.4.2", guest_memory_mib=7680)
    assert args[:2] == ["--name", f"mngr-bake-slice-{_HOST_HEX}"]
    assert ("--label", f"com.imbue.mngr.host-id=host-{_HOST_HEX}") in zip(args, args[1:], strict=False)
    assert ("--label", "com.imbue.mngr.tags={}") in zip(args, args[1:], strict=False)
    assert ("-e", "CLAUDE_CODE_VERSION=2.1.227") in zip(args, args[1:], strict=False)
    assert ("-p", "0.0.0.0:2222:22/tcp") in zip(args, args[1:], strict=False)
    assert ("-v", f"mngr-host-vol-{_HOST_HEX}:/mngr-vol:rw") in zip(args, args[1:], strict=False)
    assert ("-v", f"mngr-snapshot-trigger-{_HOST_HEX}:/mngr-snapshot:rw") in zip(args, args[1:], strict=False)
    assert ("-v", "/mngr-btrfs/snapshots:/mngr-snapshots:ro") in zip(args, args[1:], strict=False)
    assert "--restart=unless-stopped" in args
    # The fixed overrides: runsc + tmpfs, workdir, no-new-privileges, the
    # memory cap from the guest RAM (7680 - 1024), the current entrypoint and
    # the tag image (not the recorded sha).
    assert ("--runtime", "runsc") in zip(args, args[1:], strict=False)
    assert ("--tmpfs", "/run") in zip(args, args[1:], strict=False)
    assert ("--tmpfs", "/tmp") in zip(args, args[1:], strict=False)
    assert "--workdir=/" in args
    assert "--security-opt=no-new-privileges" in args
    assert "--memory=6656m" in args
    assert "--memory-swap=6656m" in args
    assert args[-5:] == [
        "--entrypoint",
        "sh",
        "default-workspace-template:minds-v0.4.2",
        "-c",
        CONTAINER_ENTRYPOINT_CMD,
    ]
    assert "sha256:deadbeef" not in args


def test_parse_docker_inspect_refuses_anything_but_one_full_entry() -> None:
    with pytest.raises(CutoverError):
        parse_docker_inspect("[]")
    with pytest.raises(CutoverError):
        parse_docker_inspect(json.dumps([{"Name": "/x"}]))
    with pytest.raises(CutoverError):
        parse_docker_inspect("nope")


def test_extract_autostart_installer_commands_reads_the_pool_host_template() -> None:
    settings = """
[create_templates.pool_host]
target_path = "/home/user/workspace/"
post_host_create_outer_command__extend = [
    '''
set -eu
echo installer
''',
]

[create_templates.vultr]
post_host_create_outer_command__extend = ["echo other"]
"""
    commands = extract_autostart_installer_commands(tomllib.loads(settings))
    # TOML drops the newline right after the opening quotes of a multi-line literal.
    assert commands == ("set -eu\necho installer\n",)
    with pytest.raises(CutoverError, match="autostart installer"):
        extract_autostart_installer_commands(tomllib.loads("[create_templates.pool_host]\ntarget_path = 'x'\n"))


def test_extract_slice_volume_home_path_reads_the_slice_provider_section() -> None:
    settings = """
[providers.lima]
volume_home_path = "/home/lima-user"

[providers.imbue_cloud_slice]
host_dir = "/home/user/.mngr"
volume_home_path = "/home/user"
"""
    assert extract_slice_volume_home_path(tomllib.loads(settings)) == "/home/user"
    with pytest.raises(CutoverError, match="volume_home_path"):
        extract_slice_volume_home_path(tomllib.loads('[providers.imbue_cloud_slice]\nhost_dir = "/home/user/.mngr"\n'))
    with pytest.raises(CutoverError, match="absolute path"):
        extract_slice_volume_home_path(
            tomllib.loads('[providers.imbue_cloud_slice]\nvolume_home_path = "home/user"\n')
        )


def test_extract_template_replay_inputs_combines_the_installer_and_the_home_path() -> None:
    settings = """
[create_templates.pool_host]
post_host_create_outer_command__extend = ["echo installer"]

[providers.imbue_cloud_slice]
volume_home_path = "/home/user"
"""
    inputs = extract_template_replay_inputs(settings)
    assert inputs.installer_commands == ("echo installer",)
    assert inputs.container_home_path == "/home/user"
    with pytest.raises(CutoverError, match="does not parse"):
        extract_template_replay_inputs("= broken")


def test_transplant_rescue_moves_a_crashed_attempts_disk_back_only_when_the_transplant_dir_lacks_it() -> None:
    command = build_transplant_rescue_command(_INSTANCE, _TRANSPLANT_DIR)
    _assert_valid_box_command(command)
    # The if/then/fi rides inside ``bash -c``: bare, it would be a syntax error under the PATH prefix.
    assert command.startswith("bash -c ")
    assert (
        f"if [ -f /srv/mngr-slices/instances/{_INSTANCE}/datadisk.qcow2 ] && [ ! -f {_TRANSPLANT_DIR}/datadisk.qcow2 ]"
        in command
    )
    assert "systemctl stop" in command
    assert f"mv /srv/mngr-slices/instances/{_INSTANCE}/datadisk.qcow2 " in command


def test_transplant_clear_removes_the_whole_transplant_dir() -> None:
    # A fresh migration must never resume from a prepared disk a rolled-back
    # earlier attempt left behind (the transplant skips itself on presence).
    command = build_transplant_clear_command(_TRANSPLANT_DIR)
    _assert_valid_box_command(command)
    assert f"rm -rf {_TRANSPLANT_DIR}" in command


def test_authorized_keys_without_drops_only_the_named_key_and_keeps_comments() -> None:
    text = "# the pool key\nssh-ed25519 AAAAPOOL pool\n\nssh-ed25519 AAAAOWNER owner\n"
    assert authorized_keys_without(text, "ssh-ed25519 AAAAPOOL other-comment") == (
        "# the pool key\n\nssh-ed25519 AAAAOWNER owner\n"
    )
    keys = make_harvested_keys()
    container_files = build_replayed_container_files(
        keys, ssh_ca_public_key="ssh-ed25519 AAAAca tier-ca", pool_public_key="ssh-ed25519 AAAAOWNER owner"
    )
    stripped = next(f for f in container_files if f.container_path == "/root/.ssh/authorized_keys")
    assert stripped.content == "\n"


def _latchkey_harvest_output(files: Sequence[HarvestedFile], *, is_dir_present: bool) -> str:
    """What the harvest command prints for these files (the dir marker first, then one marker + base64 line per file)."""
    lines = [LATCHKEY_DIR_PRESENT_MARKER] if is_dir_present else ["some shell noise"]
    for harvested in files:
        # stat -c %a prints three digits for an ordinary mode.
        lines.append(f"{LATCHKEY_HARVEST_FILE_MARKER} {harvested.path} {harvested.mode.lstrip('0')}")
        lines.append(harvested.content_base64.get_secret_value())
    return "\n".join(lines) + "\n"


def test_latchkey_harvest_command_guards_every_path_and_lists_the_extensions_dir() -> None:
    command = build_vm_latchkey_harvest_command()
    assert_valid_bash(command)
    for filename in MACHINE_LATCHKEY_DISK_FILENAMES:
        assert f"if [ -f /root/.latchkey/{filename} ]" in command
    for filename in MACHINE_LATCHKEY_SUPERVISOR_CONF_FILENAMES:
        assert f"if [ -f /etc/supervisor/conf.d/{filename} ]" in command
    for filename in MACHINE_LATCHKEY_TMPFS_FILENAMES:
        assert f"if [ -f /run/mngr-latchkey/{filename} ]" in command
    assert "for f in /root/.latchkey/extensions/*" in command
    # The logs stay behind, and no file is read with cat: an absent file emits
    # nothing, and the content is base64 so it round-trips byte for byte.
    assert "gateway.log" not in command and "tunnel.log" not in command
    assert "cat " not in command
    assert "base64 -w0" in command and "stat -c %a" in command
    # The reads are not chained with &&, which would hide a failed read from set -e.
    assert "&&" not in command
    # The harvested paths are replayed verbatim, so the origin's root home must be /root.
    assert '[ "$HOME" = /root ]' in command
    assert f"echo {LATCHKEY_DIR_PRESENT_MARKER}" in command


def _write_latchkey_state_under(tmp_path: Path, state: HarvestedLatchkeyState) -> None:
    for harvested in state.all_files:
        local = tmp_path / harvested.path.lstrip("/")
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(harvested.content)
        local.chmod(int(harvested.mode, 8))


def _run_latchkey_harvest_under(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """Run the harvest as the VM does, with its three locations rewritten onto ``tmp_path`` and $HOME at its /root."""
    command = build_vm_latchkey_harvest_command()
    for vm_dir in (VM_LATCHKEY_DIR, VM_LATCHKEY_SUPERVISOR_CONF_DIR, VM_LATCHKEY_TMPFS_DIR):
        command = command.replace(vm_dir, str(tmp_path / vm_dir.lstrip("/")))
    command = command.replace('[ "$HOME" = /root ]', f'[ "$HOME" = {tmp_path / "root"} ]')
    return subprocess.run(
        ["bash", "-c", command],
        capture_output=True,
        text=True,
        env={"HOME": str(tmp_path / "root"), "PATH": os.environ["PATH"]},
    )


@pytest.mark.skipif(platform.system() != "Linux", reason="the harvest runs GNU stat/base64 on a Linux VM")
def test_latchkey_harvest_round_trips_through_a_real_shell(tmp_path: Path) -> None:
    # Real stat and base64 produce the output the parser reads.
    expected = make_harvested_latchkey_state(LatchkeyReplayPlan.FULL)
    _write_latchkey_state_under(tmp_path, expected)
    (tmp_path / "root/.latchkey/gateway.log").write_text("not harvested\n")
    result = _run_latchkey_harvest_under(tmp_path)
    assert result.returncode == 0, result.stderr
    parsed = parse_latchkey_harvest_output(result.stdout.replace(str(tmp_path), ""))
    assert parsed == expected
    assert parsed.replay_plan == LatchkeyReplayPlan.FULL


@pytest.mark.skipif(
    platform.system() != "Linux" or os.geteuid() == 0, reason="needs GNU tools and a user the mode can lock out"
)
def test_latchkey_harvest_fails_loudly_on_a_file_it_cannot_read(tmp_path: Path) -> None:
    # A file the harvest cannot read must end the harvest with the cause on
    # stderr, not print its marker and move on (which would surface later as a
    # parser complaint about the output's shape).
    _write_latchkey_state_under(tmp_path, make_harvested_latchkey_state(LatchkeyReplayPlan.FULL))
    unreadable = tmp_path / "root/.latchkey/credentials.json.enc"
    unreadable.chmod(0)
    result = _run_latchkey_harvest_under(tmp_path)
    assert result.returncode != 0
    assert "credentials.json.enc" in result.stderr


def test_parse_latchkey_harvest_output_classifies_the_three_shapes() -> None:
    full = make_harvested_latchkey_state(LatchkeyReplayPlan.FULL)
    parsed_full = parse_latchkey_harvest_output(_latchkey_harvest_output(full.all_files, is_dir_present=True))
    assert parsed_full == full
    assert parsed_full.replay_plan == LatchkeyReplayPlan.FULL
    # Modes come back zero-padded so the replay tar's ``int(mode, 8)`` and the
    # manifest read uniformly, and a file with no trailing newline round-trips
    # without gaining one.
    assert {harvested.mode for harvested in parsed_full.all_files} == {"0600", "0644", "0700"}
    store = next(h for h in parsed_full.disk_files if h.path.endswith("credentials.json.enc"))
    assert store.content == b'{"enc":"c2VjcmV0"}'
    assert "c2VjcmV0" not in repr(store) and "c2VjcmV0" not in str(store)

    disk_only = make_harvested_latchkey_state(LatchkeyReplayPlan.DISK_ONLY)
    parsed_disk_only = parse_latchkey_harvest_output(
        _latchkey_harvest_output(disk_only.all_files, is_dir_present=True)
    )
    assert parsed_disk_only == disk_only
    assert parsed_disk_only.replay_plan == LatchkeyReplayPlan.DISK_ONLY

    parsed_absent = parse_latchkey_harvest_output(_latchkey_harvest_output((), is_dir_present=False))
    assert parsed_absent == make_harvested_latchkey_state(LatchkeyReplayPlan.ABSENT)
    assert parsed_absent.replay_plan == LatchkeyReplayPlan.ABSENT


def test_parse_latchkey_harvest_output_zero_pads_the_short_modes_stat_prints() -> None:
    # GNU ``stat -c %a`` drops leading zeros (mode 0o070 prints ``70``, mode 0
    # prints ``0``); the parser must accept them and pad to the four digits
    # the replay tar's modes are parsed from.
    marker = LATCHKEY_HARVEST_FILE_MARKER
    parsed = parse_latchkey_harvest_output(
        f"{LATCHKEY_DIR_PRESENT_MARKER}\n"
        f"{marker} /root/.latchkey/config.json 70\ne30=\n"
        f"{marker} /root/.latchkey/permissions.json 0\ne30=\n"
    )
    assert {h.path.rsplit("/", 1)[1]: h.mode for h in parsed.disk_files} == {
        "config.json": "0070",
        "permissions.json": "0000",
    }


def test_parse_latchkey_harvest_output_refuses_malformed_output() -> None:
    marker = LATCHKEY_HARVEST_FILE_MARKER
    with pytest.raises(CutoverError, match="malformed mode"):
        parse_latchkey_harvest_output(
            f"{LATCHKEY_DIR_PRESENT_MARKER}\n{marker} /root/.latchkey/config.json rw\ne30=\n"
        )
    with pytest.raises(CutoverError, match="non-base64"):
        parse_latchkey_harvest_output(
            f"{LATCHKEY_DIR_PRESENT_MARKER}\n{marker} /root/.latchkey/config.json 600\n{{}}\n"
        )
    with pytest.raises(CutoverError, match="ends after the marker"):
        parse_latchkey_harvest_output(f"{LATCHKEY_DIR_PRESENT_MARKER}\n{marker} /root/.latchkey/config.json 600\n")
    with pytest.raises(CutoverError, match="outside every known location"):
        parse_latchkey_harvest_output(f"{LATCHKEY_DIR_PRESENT_MARKER}\n{marker} /etc/passwd 644\ne30=\n")
    with pytest.raises(CutoverError, match="no latchkey directory yet printed files"):
        parse_latchkey_harvest_output(f"{marker} /run/mngr-latchkey/gateway_listen_password 600\ne30=\n")


def test_latchkey_replay_tar_carries_every_file_with_its_mode_rooted_at_slash() -> None:
    full = make_harvested_latchkey_state(LatchkeyReplayPlan.FULL)
    disk_group = full.disk_replay_files
    disk_tar = build_latchkey_replay_tar(disk_group, is_including_latchkey_dirs=True)
    with tarfile.open(fileobj=io.BytesIO(disk_tar)) as archive:
        members = {member.name: member for member in archive.getmembers()}
        # The latchkey dirs ride along 0700 so a fresh VM gets them; the
        # supervisord drop-in dir (whose mode must stay the package's) does not.
        assert members["root/.latchkey"].isdir() and members["root/.latchkey"].mode == 0o700
        assert members["root/.latchkey/extensions"].isdir() and members["root/.latchkey/extensions"].mode == 0o700
        assert "etc/supervisor/conf.d" not in members
        for harvested in disk_group:
            member = members[harvested.path.lstrip("/")]
            assert member.isreg()
            assert member.mode == int(harvested.mode, 8)
            assert (member.uid, member.gid, member.uname) == (0, 0, "root")
            extracted = archive.extractfile(member)
            assert extracted is not None and extracted.read() == harvested.content
        assert len(members) == len(disk_group) + 2
    tmpfs_tar = build_latchkey_replay_tar(full.tmpfs_files, is_including_latchkey_dirs=False)
    with tarfile.open(fileobj=io.BytesIO(tmpfs_tar)) as archive:
        assert sorted(member.name for member in archive.getmembers()) == sorted(
            harvested.path.lstrip("/") for harvested in full.tmpfs_files
        )
    # Deterministic: the same files give the same bytes.
    assert build_latchkey_replay_tar(full.tmpfs_files, is_including_latchkey_dirs=False) == tmpfs_tar


def test_latchkey_tar_extract_command_extracts_over_root_and_drops_the_tar() -> None:
    command = build_latchkey_tar_extract_command(LATCHKEY_DISK_REPLAY_TAR_PATH)
    assert_valid_bash(command)
    assert command == (
        "tar -xpf /root/.mngr-cutover-latchkey.tar -C /; _status=$?; "
        "rm -f /root/.mngr-cutover-latchkey.tar; exit $_status"
    )
    assert LATCHKEY_TMPFS_REPLAY_TAR_PATH.startswith("/run/mngr-latchkey/")


@pytest.mark.skipif(platform.system() != "Linux", reason="the replay runs GNU tar on a Linux VM")
def test_latchkey_tar_extract_command_drops_the_tar_and_fails_when_the_extract_fails(tmp_path: Path) -> None:
    # The tar carries secrets: a failed extract must still remove it, and the
    # command must still fail so the driver raises.
    tar_path = tmp_path / "corrupt.tar"
    tar_path.write_bytes(b"not a tar archive")
    command = build_latchkey_tar_extract_command(str(tar_path)).replace(" -C /;", f" -C {tmp_path};")
    result = subprocess.run(["bash", "-c", command], capture_output=True, text=True)
    assert result.returncode != 0
    assert not tar_path.exists()


@pytest.mark.skipif(platform.system() != "Linux", reason="the replay runs GNU tar on a Linux VM")
def test_latchkey_replay_tar_round_trips_through_a_real_tar_extract(tmp_path: Path) -> None:
    full = make_harvested_latchkey_state(LatchkeyReplayPlan.FULL)
    disk_group = full.disk_replay_files
    tar_path = tmp_path / "replay.tar"
    tar_path.write_bytes(build_latchkey_replay_tar(disk_group, is_including_latchkey_dirs=True))
    # ``-C <tmp>`` stands in for ``-C /`` (the extract command is pinned above).
    result = subprocess.run(["tar", "-xpf", str(tar_path), "-C", str(tmp_path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "root/.latchkey").stat().st_mode & 0o777 == 0o700
    assert (tmp_path / "root/.latchkey/extensions").stat().st_mode & 0o777 == 0o700
    for harvested in disk_group:
        extracted = tmp_path / harvested.path.lstrip("/")
        assert extracted.read_bytes() == harvested.content
        assert extracted.stat().st_mode & 0o777 == int(harvested.mode, 8), harvested.path


def test_tunnel_port_check_compares_the_drop_in_with_the_containers_published_sshd_port() -> None:
    full = make_harvested_latchkey_state(LatchkeyReplayPlan.FULL)
    tunnel_conf = next(h for h in full.supervisor_confs if h.path.endswith("latchkey-tunnel.conf"))
    assert tunnel_conf_container_ssh_port(tunnel_conf.content.decode()) == HARVESTED_TUNNEL_CONTAINER_PORT
    assert tunnel_conf_container_ssh_port("[program:x]\nautostart=true\n") is None
    assert container_ssh_host_port_from_inspect(_inspect_entry()) == 2222
    assert container_ssh_host_port_from_inspect({"HostConfig": {}}) is None
    assert latchkey_tunnel_port_error_or_none(full, _inspect_entry()) is None
    mismatched = dict(_inspect_entry())
    mismatched["HostConfig"] = {"PortBindings": {"22/tcp": [{"HostIp": "0.0.0.0", "HostPort": "2223"}]}}
    error = latchkey_tunnel_port_error_or_none(full, mismatched)
    assert error is not None and "2222" in error and "2223" in error
    # A drop-in with no port, or a container with no published sshd, is refused
    # rather than passed: the replayed tunnel could never connect.
    portless_conf = make_harvested_file(
        tunnel_conf.path, b"[program:latchkey-tunnel]\ncommand=/usr/bin/ssh -N\n", "0600"
    )
    portless = full.model_copy_update(to_update(full.field_ref().supervisor_confs, (portless_conf,)))
    portless_error = latchkey_tunnel_port_error_or_none(portless, _inspect_entry())
    assert portless_error is not None and "-p <port>" in portless_error
    unpublished_error = latchkey_tunnel_port_error_or_none(full, {"HostConfig": {}})
    assert unpublished_error is not None and "22/tcp" in unpublished_error and "2222" in unpublished_error
    # No tunnel drop-in harvested: nothing to compare.
    assert (
        latchkey_tunnel_port_error_or_none(make_harvested_latchkey_state(LatchkeyReplayPlan.ABSENT), mismatched)
        is None
    )


def test_gateway_files_check_requires_the_wrapper_and_both_drop_ins_for_a_full_replay() -> None:
    full = make_harvested_latchkey_state(LatchkeyReplayPlan.FULL)
    assert latchkey_gateway_files_error_or_none(full) is None
    # Without the machine's tmpfs pair nothing is started, so nothing is required.
    assert latchkey_gateway_files_error_or_none(make_harvested_latchkey_state(LatchkeyReplayPlan.DISK_ONLY)) is None
    assert latchkey_gateway_files_error_or_none(make_harvested_latchkey_state(LatchkeyReplayPlan.ABSENT)) is None

    without_wrapper = full.model_copy_update(
        to_update(
            full.field_ref().disk_files,
            tuple(h for h in full.disk_files if not h.path.endswith("/gateway_run.sh")),
        )
    )
    wrapper_error = latchkey_gateway_files_error_or_none(without_wrapper)
    assert wrapper_error is not None and "gateway_run.sh" in wrapper_error
    without_confs = full.model_copy_update(to_update(full.field_ref().supervisor_confs, ()))
    confs_error = latchkey_gateway_files_error_or_none(without_confs)
    assert confs_error is not None
    assert "latchkey-gateway.conf" in confs_error and "latchkey-tunnel.conf" in confs_error
    assert "gateway_run.sh" not in confs_error


def test_vm_latchkey_probe_commands_name_both_programs_and_the_gateway_port() -> None:
    status = build_vm_latchkey_supervisor_status_command()
    probe = build_vm_gateway_port_probe_command()
    assert_valid_bash(status)
    assert_valid_bash(probe)
    assert status == "supervisorctl status latchkey-gateway latchkey-tunnel"
    assert "/dev/tcp/127.0.0.1/1989" in probe
    assert "curl" not in probe


def test_parse_supervisorctl_not_running_treats_exited_as_a_failure() -> None:
    output = "latchkey-gateway RUNNING pid 12, uptime 0:01:00\nlatchkey-tunnel EXITED Sep 13 08:00 PM\n"
    assert parse_supervisorctl_not_running(output) == ["latchkey-tunnel EXITED"]
    assert parse_supervisorctl_not_running("latchkey-gateway RUNNING pid 1\nlatchkey-tunnel RUNNING pid 2\n") == []
    assert parse_supervisorctl_not_running("unix:///var/run/supervisor.sock no such file\n") == [
        "unix:///var/run/supervisor.sock no such file"
    ]


def test_latchkey_replay_detail_names_each_plan() -> None:
    assert latchkey_replay_detail(None) == snapshot("latchkey state not harvested (record predates the latchkey leg)")
    assert latchkey_replay_detail(LatchkeyReplayPlan.ABSENT) == snapshot("no latchkey state on the origin")
    assert latchkey_replay_detail(LatchkeyReplayPlan.DISK_ONLY) == snapshot(
        "latchkey files replayed; the gateway starts at the desktop's next provisioning pass (no tmpfs pair)"
    )
    assert latchkey_replay_detail(LatchkeyReplayPlan.FULL) == snapshot(
        "latchkey state replayed and the gateway restarted"
    )


def _init_workspace_checkout(checkout: Path, vendored_pin: str | None) -> None:
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "initial",
        ],
        check=True,
    )
    if vendored_pin is not None:
        build_info = checkout / "system/vendor/mngr/apps/minds/imbue/minds/build_info.py"
        build_info.parent.mkdir(parents=True)
        build_info.write_text(f'from typing import Final\n\nFALLBACK_BRANCH: Final[str] = "{vendored_pin}"\n')


def _run_version_probe(checkout: Path) -> str:
    return subprocess.run(
        ["sh", "-c", build_workspace_version_probe_script(str(checkout))], capture_output=True, text=True, check=True
    ).stdout.strip()


def test_version_probe_prefers_the_nearest_release_tag(tmp_path: Path) -> None:
    checkout = tmp_path / "workspace"
    _init_workspace_checkout(checkout, vendored_pin="minds-v0.5.0")
    subprocess.run(["git", "-C", str(checkout), "tag", "minds-v0.5.2"], check=True)
    subprocess.run(["git", "-C", str(checkout), "tag", "v9.9.9"], check=True)

    assert _run_version_probe(checkout) == "minds-v0.5.2"


def test_version_probe_falls_back_to_the_vendored_pin_when_the_checkout_has_no_release_tag(tmp_path: Path) -> None:
    checkout = tmp_path / "workspace"
    _init_workspace_checkout(checkout, vendored_pin="minds-v0.5.0")

    assert _run_version_probe(checkout) == "minds-v0.5.0"


def test_version_probe_prints_nothing_when_neither_a_tag_nor_a_vendored_pin_exists(tmp_path: Path) -> None:
    checkout = tmp_path / "workspace"
    _init_workspace_checkout(checkout, vendored_pin=None)

    assert _run_version_probe(checkout) == ""


def test_home_layout_probe_command_classifies_the_container_home() -> None:
    command = build_home_layout_probe_command("abc123")
    assert_valid_bash(command)
    assert command.startswith("docker exec --workdir / abc123 sh -c ")
    assert "-L /home/user ]" in command and "-L /home/user/.mngr ]" in command


def test_home_layout_error_accepts_only_the_home_layout() -> None:
    assert home_layout_error_or_none("home\n", "host-abc") is None
    legacy = home_layout_error_or_none("legacy\n", "host-abc")
    assert legacy is not None and "writable layer" in legacy
    assert "repair-home-layout --host-id host-abc --migrate" in legacy
    unknown = home_layout_error_or_none("", "host-abc")
    assert unknown is not None and "unrecognized home layout" in unknown
