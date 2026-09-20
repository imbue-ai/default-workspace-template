"""The VM-side and box-side commands of the workspace archive (``minds-admin archives create``), rendered operator-side.

The VM root snapshots the workspace's btrfs subvolume and runs the zip
streamer (``archive_stream_zip.py``, uploaded there for the run); the box's
slice service user pipes that stream over its loopback SSH into the tier
bucket with ``s5cmd``, measuring the zip's sha256 and size on the way. All
pure renderers; the drivers run them.
"""

import posixpath
import shlex
from collections.abc import Mapping
from collections.abc import Sequence
from importlib import resources as importlib_resources
from typing import Final

from imbue.imbue_common.pure import pure
from imbue.minds_admin.slices.archive_types import ARCHIVE_BYTES_MARKER
from imbue.minds_admin.slices.archive_types import ARCHIVE_SHA256_MARKER
from imbue.mngr_imbue_cloud.slices.ssh_box_image_cache import SLICE_LOOPBACK_SSH_OPTS

# Where the run's files live on the VM (root-owned, removed afterwards).
VM_ARCHIVE_DIR: Final[str] = "/root/.mngr-archive"
VM_ARCHIVE_SCRIPT_PATH: Final[str] = f"{VM_ARCHIVE_DIR}/archive_stream_zip.py"
VM_ARCHIVE_README_PATH: Final[str] = f"{VM_ARCHIVE_DIR}/README.txt"
# The read-only snapshots the archive takes live beside the host subvolume,
# outside the ``snapshots/`` directory host_backup sweeps.
ARCHIVE_SNAPSHOT_DIRNAME: Final[str] = "archive"
# The box-side script and its measurement files, in the instance's transfer dir.
BOX_ARCHIVE_SCRIPT_FILENAME: Final[str] = "archive.sh"
_SHA_FILENAME: Final[str] = "zip.sha"
_BYTES_FILENAME: Final[str] = "zip.bytes"


def load_archive_stream_script() -> str:
    """The zip streamer's source, shipped in this package and uploaded to the VM for each run."""
    return importlib_resources.files("imbue.minds_admin.slices").joinpath("archive_stream_zip.py").read_text()


@pure
def build_container_layer_path_command(container_id: str) -> str:
    """Print the container's overlay upper directory (its writable layer) on the VM.

    Read off the container's root mount in its own mount table rather than
    ``docker inspect``'s GraphDriver section, which a dockerd on the containerd
    image store (the VM images since Docker 29) leaves empty.
    """
    pid_command = f"docker inspect --format {shlex.quote('{{.State.Pid}}')} {shlex.quote(container_id)}"
    upperdir_awk = (
        '$5 == "/" { n = split($NF, options, ","); for (i = 1; i <= n; i++) '
        "if (options[i] ~ /^upperdir=/) { print substr(options[i], 10); exit } }"
    )
    return f"pid=$({pid_command}) && awk {shlex.quote(upperdir_awk)} /proc/$pid/mountinfo"


@pure
def build_volume_device_path_command(volume_name: str) -> str:
    """Print the VM path the named docker volume bind-mounts (the workspace's btrfs subvolume)."""
    device_format = '{{index .Options "device"}}'
    return f"docker volume inspect --format {shlex.quote(device_format)} {shlex.quote(volume_name)}"


@pure
def archive_snapshot_path(subvolume_path: str, stamp: str) -> str:
    """Where the read-only snapshot of the subvolume is taken: ``<mount>/archive/<name>-<stamp>``."""
    parent, name = posixpath.split(subvolume_path.rstrip("/"))
    return posixpath.join(parent, ARCHIVE_SNAPSHOT_DIRNAME, f"{name}-{stamp}")


@pure
def build_snapshot_create_command(subvolume_path: str, snapshot_path: str) -> str:
    return (
        f"mkdir -p {shlex.quote(posixpath.dirname(snapshot_path))} && "
        f"btrfs subvolume snapshot -r {shlex.quote(subvolume_path)} {shlex.quote(snapshot_path)}"
    )


@pure
def build_snapshot_delete_command(snapshot_path: str) -> str:
    """Delete the snapshot when it exists (a re-run after a crash finds none), and its parent dir once empty.

    A failed delete is the command's exit status (the driver logs it); the
    rmdir is best-effort, the parent may hold another run's snapshot.
    """
    quoted = shlex.quote(snapshot_path)
    parent = shlex.quote(posixpath.dirname(snapshot_path))
    return f"if [ -d {quoted} ]; then btrfs subvolume delete {quoted}; fi && {{ rmdir {parent} 2>/dev/null || true; }}"


@pure
def build_vm_archive_cleanup_command() -> str:
    return f"rm -rf {shlex.quote(VM_ARCHIVE_DIR)}"


@pure
def build_vm_stream_command(
    root_path_by_prefix: Mapping[str, str], excludes: Sequence[str], *, is_readme_included: bool
) -> str:
    """The zip streamer invocation the box runs on the VM root over its loopback SSH."""
    parts = ["python3", VM_ARCHIVE_SCRIPT_PATH]
    for prefix, path in root_path_by_prefix.items():
        parts.extend(["--root", f"{prefix}={path}"])
    for pattern in excludes:
        parts.extend(["--exclude", pattern])
    if is_readme_included:
        parts.extend(["--readme", VM_ARCHIVE_README_PATH])
    return " ".join(shlex.quote(part) for part in parts)


@pure
def render_box_archive_upload_script(
    *, transfer_dir_path: str, transfer_key_path: str, vm_ssh_port: int, vm_command: str, zip_object_key: str
) -> str:
    """The box-side script: VM stream -> sha256 / byte count taps -> ``s5cmd pipe`` into the tier bucket.

    ``pipefail`` makes a failed stream (the VM script dying, the SSH dropping)
    fail the whole pipeline instead of landing a truncated zip as a success;
    the markers are printed only once the upload returned and both taps
    flushed.
    """
    sha_path = shlex.quote(f"{transfer_dir_path}/{_SHA_FILENAME}")
    bytes_path = shlex.quote(f"{transfer_dir_path}/{_BYTES_FILENAME}")
    return f"""\
#!/bin/bash
set -Eeuo pipefail
export PATH=/usr/local/bin:$HOME/.local/bin:$PATH
TD={shlex.quote(transfer_dir_path)}
. "$TD/env"
rm -f {sha_path} {bytes_path}
ssh -i {shlex.quote(transfer_key_path)} {SLICE_LOOPBACK_SSH_OPTS} -p {int(vm_ssh_port)} root@127.0.0.1 {shlex.quote(vm_command)} \\
    | tee >(sha256sum | awk '{{print $1}}' > {sha_path}) >(wc -c | tr -d ' ' > {bytes_path}) \\
    | s5cmd --endpoint-url "$WS_S3_ENDPOINT" pipe "s3://$WS_BUCKET/{zip_object_key}"
# tee's process substitutions may still be flushing when the pipeline
# returns; wait for the files to land before reading them.
for _ in $(seq 1 100); do
    [ -s {sha_path} ] && [ -s {bytes_path} ] && break
    sleep 0.1
done
echo "{ARCHIVE_SHA256_MARKER} $(cat {sha_path})"
echo "{ARCHIVE_BYTES_MARKER} $(cat {bytes_path})"
"""


@pure
def render_archive_readme(
    *,
    host_name: str,
    host_id: str,
    owner_email: str | None,
    created_at_text: str,
    home_layout: str,
    version_probe: str,
    excludes: Sequence[str],
) -> str:
    """The README.txt at the zip root: what the two folders hold and what was left out."""
    version_line = version_probe or "unknown (the workspace named no release version)"
    excludes_text = "\n".join(f"  {pattern}" for pattern in excludes)
    return f"""\
Imbue Cloud workspace archive
=============================

Workspace:  {host_name} ({host_id})
Owner:      {owner_email or "unknown"}
Taken:      {created_at_text}
Version:    {version_line}
Layout:     {home_layout}

This archive holds everything the workspace kept, in two folders:

  volume/           The workspace's persistent volume. On the current layout
                    this is home/ (the whole /home/user tree, including the
                    workspace checkout under home/workspace and the agent
                    state under home/.mngr). On the legacy layout it is
                    host_dir/ (the agent state: chats, logs, transcripts) and
                    agents/.
  container-layer/  Every file the workspace container wrote outside its
                    volume: home dotfiles on the legacy layout (including
                    Claude's config and transcripts under home/user/.claude),
                    /var/log/mngr and the supervisor logs.

Regenerable caches were left out (each pattern below, anywhere in the tree):
{excludes_text}

The archive contains credentials the workspace held (API keys, tokens,
SSH keys). Treat it as sensitive.
"""
