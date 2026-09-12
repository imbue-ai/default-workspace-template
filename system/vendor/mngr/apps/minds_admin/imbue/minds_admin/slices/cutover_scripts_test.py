import base64
import json
import tomllib

import pytest
from inline_snapshot import snapshot

from imbue.minds_admin.slices.cutover_scripts import HARVEST_FILE_MARKER
from imbue.minds_admin.slices.cutover_scripts import TRANSPLANT_DONE_MARKER
from imbue.minds_admin.slices.cutover_scripts import authorized_keys_without
from imbue.minds_admin.slices.cutover_scripts import build_banner_wait_command
from imbue.minds_admin.slices.cutover_scripts import build_container_id_command
from imbue.minds_admin.slices.cutover_scripts import build_container_key_harvest_command
from imbue.minds_admin.slices.cutover_scripts import build_disk_materialize_command
from imbue.minds_admin.slices.cutover_scripts import build_docker_create_args
from imbue.minds_admin.slices.cutover_scripts import build_gen1_datadisk_info_command
from imbue.minds_admin.slices.cutover_scripts import build_git_describe_command
from imbue.minds_admin.slices.cutover_scripts import build_image_load_command
from imbue.minds_admin.slices.cutover_scripts import build_image_publish_command
from imbue.minds_admin.slices.cutover_scripts import build_replayed_container_files
from imbue.minds_admin.slices.cutover_scripts import build_stage_replayed_container_files_command
from imbue.minds_admin.slices.cutover_scripts import build_transplant_clear_command
from imbue.minds_admin.slices.cutover_scripts import build_transplant_rescue_command
from imbue.minds_admin.slices.cutover_scripts import build_unit_enable_command
from imbue.minds_admin.slices.cutover_scripts import build_vm_key_harvest_command
from imbue.minds_admin.slices.cutover_scripts import container_name_from_inspect
from imbue.minds_admin.slices.cutover_scripts import cutover_image_object_key
from imbue.minds_admin.slices.cutover_scripts import cutover_transplant_dir
from imbue.minds_admin.slices.cutover_scripts import extract_autostart_installer_commands
from imbue.minds_admin.slices.cutover_scripts import extract_slice_volume_home_path
from imbue.minds_admin.slices.cutover_scripts import extract_template_replay_inputs
from imbue.minds_admin.slices.cutover_scripts import migration_rollback_key_prefix
from imbue.minds_admin.slices.cutover_scripts import parse_docker_inspect
from imbue.minds_admin.slices.cutover_scripts import parse_marked_files
from imbue.minds_admin.slices.cutover_scripts import parse_qemu_img_info
from imbue.minds_admin.slices.cutover_scripts import parse_supervisorctl_unhealthy
from imbue.minds_admin.slices.cutover_scripts import render_gen2_disk_transplant_script
from imbue.minds_admin.slices.cutover_scripts import replayed_container_dirs
from imbue.minds_admin.slices.cutover_scripts import staged_container_dir_path
from imbue.minds_admin.slices.cutover_scripts import staged_container_file_path
from imbue.minds_admin.slices.cutover_types import CutoverError
from imbue.minds_admin.slices.testing import make_harvested_keys
from imbue.mngr.providers.ssh_host_setup import SSHD_PROVISIONED_MARKER_PATH
from imbue.mngr_imbue_cloud.slices.gen2_scripts.testing import assert_valid_bash
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
