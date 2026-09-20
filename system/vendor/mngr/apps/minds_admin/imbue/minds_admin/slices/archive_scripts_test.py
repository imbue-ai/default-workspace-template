import os
import shlex
import subprocess
from pathlib import Path

from inline_snapshot import snapshot

from imbue.minds_admin.slices.archive_scripts import VM_ARCHIVE_README_PATH
from imbue.minds_admin.slices.archive_scripts import VM_ARCHIVE_SCRIPT_PATH
from imbue.minds_admin.slices.archive_scripts import archive_snapshot_path
from imbue.minds_admin.slices.archive_scripts import build_container_layer_path_command
from imbue.minds_admin.slices.archive_scripts import build_snapshot_create_command
from imbue.minds_admin.slices.archive_scripts import build_snapshot_delete_command
from imbue.minds_admin.slices.archive_scripts import build_vm_stream_command
from imbue.minds_admin.slices.archive_scripts import build_volume_device_path_command
from imbue.minds_admin.slices.archive_scripts import load_archive_stream_script
from imbue.minds_admin.slices.archive_scripts import render_archive_readme
from imbue.minds_admin.slices.archive_scripts import render_box_archive_upload_script
from imbue.minds_admin.slices.archive_types import ARCHIVE_BYTES_MARKER
from imbue.minds_admin.slices.archive_types import ARCHIVE_SHA256_MARKER
from imbue.mngr.utils.testing import write_executable_script
from imbue.mngr_imbue_cloud.slices.gen2_scripts.testing import assert_valid_bash


def test_snapshot_lives_beside_the_subvolume_outside_the_host_backup_snapshots_dir() -> None:
    path = archive_snapshot_path("/mngr-btrfs/0123abcd", "20260918T120000Z")
    assert path == snapshot("/mngr-btrfs/archive/0123abcd-20260918T120000Z")
    create = build_snapshot_create_command("/mngr-btrfs/0123abcd", path)
    assert create == snapshot(
        "mkdir -p /mngr-btrfs/archive && btrfs subvolume snapshot -r /mngr-btrfs/0123abcd /mngr-btrfs/archive/0123abcd-20260918T120000Z"
    )
    assert_valid_bash(create)
    delete = build_snapshot_delete_command(path)
    assert "btrfs subvolume delete" in delete and "if [ -d" in delete
    assert "rmdir /mngr-btrfs/archive" in delete
    assert_valid_bash(delete)


def _run_with_stub(tmp_path: Path, command: str, stub_name: str, stub_body: str) -> subprocess.CompletedProcess[str]:
    """Run a rendered VM command through bash with a stub binary ahead of the real one on PATH."""
    stub_bin = tmp_path / "stubbin"
    stub_bin.mkdir(exist_ok=True)
    write_executable_script(stub_bin / stub_name, f"#!/bin/bash\n{stub_body}\n")
    env = {"PATH": f"{stub_bin}{os.pathsep}{os.environ['PATH']}"}
    return subprocess.run(["bash", "-c", command], capture_output=True, text=True, env=env)


def test_snapshot_delete_reports_a_failed_delete_and_only_removes_an_emptied_parent(tmp_path: Path) -> None:
    archive_dir = tmp_path / "mngr-btrfs" / "archive"
    snapshot_dir = archive_dir / "0123abcd-20260918T120000Z"
    snapshot_dir.mkdir(parents=True)
    delete = build_snapshot_delete_command(str(snapshot_dir))
    # The driver logs a snapshot it could not remove, so the delete's failure must be the command's exit status.
    failed = _run_with_stub(tmp_path, delete, "btrfs", "exit 1")
    assert failed.returncode != 0
    assert archive_dir.is_dir()
    succeeded = _run_with_stub(tmp_path, delete, "btrfs", 'rmdir "${!#}"')
    assert succeeded.returncode == 0, succeeded.stderr
    assert not archive_dir.exists()
    # A re-run after a crash finds no snapshot; a parent holding another run's snapshot is kept.
    (archive_dir / "other-run").mkdir(parents=True)
    rerun = _run_with_stub(tmp_path, delete, "btrfs", "exit 1")
    assert rerun.returncode == 0, rerun.stderr
    assert (archive_dir / "other-run").is_dir()


_CONTAINERD_SNAPSHOTS = "/var/lib/containerd/io.containerd.snapshotter.v1.overlayfs/snapshots"
_OVERLAY_ROOT_MOUNTINFO = (
    "1290 1234 0:60 / / rw,relatime shared:5 master:2 - overlay overlay "
    f"rw,lowerdir={_CONTAINERD_SNAPSHOTS}/12/fs:{_CONTAINERD_SNAPSHOTS}/11/fs,"
    f"upperdir={_CONTAINERD_SNAPSHOTS}/13/fs,workdir={_CONTAINERD_SNAPSHOTS}/13/work\n"
    "1291 1290 0:61 / /proc rw,nosuid,nodev,noexec,relatime - proc proc rw\n"
    "1300 1290 0:40 /volumes/x /home/user rw,relatime - btrfs /dev/vdb rw,ssd,subvolid=300,subvol=/x\n"
)
_PLAIN_ROOT_MOUNTINFO = (
    "1290 1234 8:1 / / rw,relatime - ext4 /dev/sda1 rw\n"
    "1291 1290 0:61 / /proc rw,nosuid,nodev,noexec,relatime - proc proc rw\n"
)


def test_container_layer_command_reads_the_root_mounts_upperdir_from_the_pids_mountinfo(tmp_path: Path) -> None:
    mountinfo = tmp_path / "proc" / "4242" / "mountinfo"
    mountinfo.parent.mkdir(parents=True)
    command = build_container_layer_path_command("c1").replace(" /proc/", f" {tmp_path}/proc/")
    assert command != build_container_layer_path_command("c1")
    mountinfo.write_text(_OVERLAY_ROOT_MOUNTINFO)
    result = _run_with_stub(tmp_path, command, "docker", "echo 4242")
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{_CONTAINERD_SNAPSHOTS}/13/fs\n"
    # A root that is not an overlay has no upper dir: the command prints nothing, and the driver reports it.
    mountinfo.write_text(_PLAIN_ROOT_MOUNTINFO)
    result = _run_with_stub(tmp_path, command, "docker", "echo 4242")
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_docker_lookups_are_quoted_for_the_vm_shell() -> None:
    assert build_container_layer_path_command("abc 123") == snapshot(
        "pid=$(docker inspect --format '{{.State.Pid}}' 'abc 123') && awk '$5 == \"/\" { n = split($NF, options, \",\"); for (i = 1; i <= n; i++) if (options[i] ~ /^upperdir=/) { print substr(options[i], 10); exit } }' /proc/$pid/mountinfo"
    )
    assert build_volume_device_path_command("mngr-host-vol-ff") == snapshot(
        "docker volume inspect --format '{{index .Options \"device\"}}' mngr-host-vol-ff"
    )


def test_vm_stream_command_names_every_root_and_exclude() -> None:
    command = build_vm_stream_command(
        {"volume": "/mngr-btrfs/archive/snap", "container-layer": "/var/lib/docker/overlay2/x/diff"},
        ("**/.venv", "**/node_modules"),
        is_readme_included=True,
    )
    words = shlex.split(command)
    assert words[:2] == ["python3", VM_ARCHIVE_SCRIPT_PATH]
    assert words[2:6] == [
        "--root",
        "volume=/mngr-btrfs/archive/snap",
        "--root",
        "container-layer=/var/lib/docker/overlay2/x/diff",
    ]
    assert words[6:10] == ["--exclude", "**/.venv", "--exclude", "**/node_modules"]
    assert words[10:] == ["--readme", VM_ARCHIVE_README_PATH]
    assert "--readme" not in build_vm_stream_command({"volume": "/s"}, (), is_readme_included=False)


def test_box_upload_script_pipes_the_vm_stream_through_the_taps_into_s5cmd() -> None:
    script = render_box_archive_upload_script(
        transfer_dir_path="/home/limaops/.mngr-transfers/archive-host-abc",
        transfer_key_path="/home/limaops/.image-cache/.transfer-key",
        vm_ssh_port=22010,
        vm_command="python3 /root/.mngr-archive/archive_stream_zip.py --root volume=/s",
        zip_object_key="dev-x/archives/host-abc/20260918T120000Z/workspace.zip",
    )
    assert_valid_bash(script)
    assert "set -Eeuo pipefail" in script
    assert "-p 22010 root@127.0.0.1 'python3 /root/.mngr-archive/archive_stream_zip.py --root volume=/s'" in script
    assert (
        's5cmd --endpoint-url "$WS_S3_ENDPOINT" pipe "s3://$WS_BUCKET/dev-x/archives/host-abc/20260918T120000Z/workspace.zip"'
        in script
    )
    assert f'echo "{ARCHIVE_SHA256_MARKER} $(cat' in script
    assert f'echo "{ARCHIVE_BYTES_MARKER} $(cat' in script


def test_readme_names_the_owner_the_layout_and_the_excludes() -> None:
    readme = render_archive_readme(
        host_name="my-mind",
        host_id="host-abc",
        owner_email="alice@example.com",
        created_at_text="2026-09-18T12:00:00+00:00",
        home_layout="legacy",
        version_probe="",
        excludes=("**/.venv",),
    )
    assert "my-mind (host-abc)" in readme
    assert "alice@example.com" in readme
    assert "Layout:     legacy" in readme
    assert "unknown (the workspace named no release version)" in readme
    assert "  **/.venv" in readme


def test_the_shipped_streamer_is_the_module_beside_this_test() -> None:
    # The VM runs the package's own copy; a resource that drifted from the
    # module (or was left out of the wheel) would break every archive.
    assert "MNGR_ARCHIVE_SUMMARY" in load_archive_stream_script()
    assert "def main()" in load_archive_stream_script()
