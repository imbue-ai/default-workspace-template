"""Pure renderers for the gen-1 -> gen-2 migration's box-side and VM-side commands.

The commands, the parsers of what they print, and the files they ship: the
gen-2 disk transplant, the reserve follow-ups, the container ``docker create``
line replayed from a harvested ``docker inspect``, the machine-owned latchkey
state (its harvest command and parser, the replay tars extracted over ``/``
on the target, the tunnel-port check), and the small probe commands. The
drivers in ``cli/cutover_drivers.py`` ship these over SSH; the transfer
conventions (env file, status file) come from ``gen2_scripts.transfer``.

One-time tooling: deleted with the rest of ``minds-admin cutover`` in phase 6
of blueprint/slice-fleet-cutover.
"""

import base64
import binascii
import io
import json
import posixpath
import re
import shlex
import tarfile
import tomllib
from collections.abc import Mapping
from collections.abc import Sequence
from typing import Any
from typing import Final
from typing import assert_never

from pydantic import SecretStr

from imbue.imbue_common.pure import pure
from imbue.minds_admin.primitives import SLICE_PROVIDER_INSTANCE_NAME
from imbue.minds_admin.slices.cutover_types import CutoverError
from imbue.minds_admin.slices.cutover_types import HarvestedFile
from imbue.minds_admin.slices.cutover_types import HarvestedKeys
from imbue.minds_admin.slices.cutover_types import HarvestedLatchkeyState
from imbue.minds_admin.slices.cutover_types import LatchkeyReplayPlan
from imbue.minds_admin.slices.cutover_types import ReplayedContainerFile
from imbue.minds_admin.slices.cutover_types import TemplateReplayInputs
from imbue.minds_admin.slices.cutover_types import VM_LATCHKEY_DIR
from imbue.minds_admin.slices.cutover_types import VM_LATCHKEY_SUPERVISOR_CONF_DIR
from imbue.minds_admin.slices.cutover_types import VM_LATCHKEY_TMPFS_DIR
from imbue.minds_admin.slices.cutover_types import VM_ROOT_HOME
from imbue.minds_admin.slices.cutover_types import classify_harvested_latchkey_files
from imbue.mngr.providers.ssh_host_setup import SSHD_PROVISIONED_MARKER_PATH
from imbue.mngr_imbue_cloud.slices.bare_metal import GEN2_CONTAINER_RUNTIME
from imbue.mngr_imbue_cloud.slices.bare_metal import GEN2_CONTAINER_TMPFS_START_ARGS
from imbue.mngr_imbue_cloud.slices.bare_metal import build_slice_container_memory_start_args
from imbue.mngr_imbue_cloud.slices.bare_metal import docker_runtime_name
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_BASE_IMAGE_PATH
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_GUEST_DATA_FS_LABEL
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_GUEST_HOST_QUOTA_QGROUP
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_HOST_ID_CONTAINER_LABEL
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_SLICE_SERVICE_USER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_STORAGE_ROOT
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import gen2_instance_dir
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import slice_unit_name
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import GEN2_BOOT_DISK_GIB
from imbue.mngr_imbue_cloud.slices.gen2_scripts.ssh_ca import SSH_CA_PRINCIPAL_CONTAINER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.ssh_ca import SSH_CA_ROOT_USER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.ssh_ca import is_same_ssh_public_key
from imbue.mngr_imbue_cloud.slices.gen2_scripts.ssh_ca import ssh_ca_trust_files
from imbue.mngr_imbue_cloud.slices.gen2_scripts.transfer import DATADISK_OBJECT
from imbue.mngr_imbue_cloud.slices.ssh_box_image_cache import SLICE_LOOPBACK_SSH_OPTS
from imbue.mngr_latchkey.remote.provisioning import GATEWAY_PROGRAM_NAME
from imbue.mngr_latchkey.remote.provisioning import GATEWAY_RUN_SCRIPT_FILENAME
from imbue.mngr_latchkey.remote.provisioning import MACHINE_LATCHKEY_DISK_FILENAMES
from imbue.mngr_latchkey.remote.provisioning import MACHINE_LATCHKEY_SUPERVISOR_CONF_FILENAMES
from imbue.mngr_latchkey.remote.provisioning import MACHINE_LATCHKEY_TMPFS_FILENAMES
from imbue.mngr_latchkey.remote.provisioning import OUTER_PORT
from imbue.mngr_latchkey.remote.provisioning import REMOTE_EXTENSIONS_DIR_NAME
from imbue.mngr_latchkey.remote.provisioning import TUNNEL_CONF_FILENAME
from imbue.mngr_latchkey.remote.provisioning import TUNNEL_PROGRAM_NAME
from imbue.mngr_vps.container_setup import CONTAINER_ENTRYPOINT_CMD

# Object layout under the tier bucket's key prefix: one rollback-copy dir per
# migrated workspace (the product stop artifact's three objects, copied so the
# workspace's later gen-2 stops cannot delete them) and one tar per version.
CUTOVER_KEY_SEGMENT: Final[str] = "cutover"
ROLLBACK_KEY_SEGMENT: Final[str] = "rollback"
IMAGES_KEY_SEGMENT: Final[str] = "images"

# The marker the disk transplant prints on success (followed by the new
# disk's byte size), and the marker the key harvest prints before each file.
TRANSPLANT_DONE_MARKER: Final[str] = "MNGR_CUTOVER_TRANSPLANT_DONE"
HARVEST_FILE_MARKER: Final[str] = "MNGR_CUTOVER_FILE"
# The latchkey harvest's markers: one per file (path and octal mode, the base64
# content on the next line) and one line saying the VM has a latchkey directory
# at all (so an absent directory is told apart from an empty harvest).
LATCHKEY_HARVEST_FILE_MARKER: Final[str] = "MNGR_CUTOVER_LATCHKEY_FILE"
LATCHKEY_DIR_PRESENT_MARKER: Final[str] = "MNGR_CUTOVER_LATCHKEY_DIR_PRESENT"

# The octal mode ``stat -c %a`` reports, printed without leading zeros (one to
# three digits, four with a special bit); the parser zero-pads it to four.
_OCTAL_MODE_RE: Final[re.Pattern[str]] = re.compile(r"^[0-7]{1,4}$")
# Where the replay lands each group's tar on the target VM before extracting it
# over ``/``: the disk files' beside their destination on the boot disk, the
# tmpfs secrets' inside the RAM-backed directory they are extracted into (so the
# machine's key never touches the disk, not even in transit).
LATCHKEY_DISK_REPLAY_TAR_PATH: Final[str] = f"{VM_ROOT_HOME}/.mngr-cutover-latchkey.tar"
LATCHKEY_TMPFS_REPLAY_TAR_PATH: Final[str] = f"{VM_LATCHKEY_TMPFS_DIR}/.mngr-cutover-latchkey.tar"
# The directories the disk tar carries as members, so a fresh VM gets them 0700
# (the supervisord drop-in dir already exists once supervisor is installed and
# must keep its own mode, so it is deliberately not a member).
_LATCHKEY_TAR_DIRS: Final[tuple[str, ...]] = (VM_LATCHKEY_DIR, f"{VM_LATCHKEY_DIR}/{REMOTE_EXTENSIONS_DIR_NAME}")
_LATCHKEY_TAR_DIR_MODE: Final[int] = 0o700
# The ``-p <port>`` of the tunnel program's ssh command line.
_TUNNEL_CONF_PORT_RE: Final[re.Pattern[str]] = re.compile(r" -p (\d+) ")

# Where the disk transplant works on the box: on the storage partition (the
# gen-2 root partition is 20 GiB and holds the OS alone), one dir per instance.
# The transfer dir under the service user's home keeps only the sourced env file.
CUTOVER_TRANSPLANT_ROOT: Final[str] = f"{GEN2_STORAGE_ROOT}/cutover"

# The sshd trust material the harvest reads (off the VM and off the container
# alike) and the restore replays.
SSHD_HOST_KEY_PATH: Final[str] = "/etc/ssh/ssh_host_ed25519_key"
ROOT_AUTHORIZED_KEYS_PATH: Final[str] = "/root/.ssh/authorized_keys"
# The workspace checkout inside the container, where the version tag is read.
WORKSPACE_HOME_PATH: Final[str] = "/home/user"
WORKSPACE_CHECKOUT_PATH: Final[str] = "/home/user/workspace"
# The system_interface the health probe curls inside the container.
SYSTEM_INTERFACE_URL: Final[str] = "http://127.0.0.1:8000/"

# The top-level ``docker inspect`` keys the replay reads under; an entry
# lacking any of them is refused at parse time rather than at ``docker create``.
_INSPECT_ROOT_KEYS: Final[tuple[str, ...]] = ("Name", "Config", "HostConfig", "Mounts")


@pure
def migration_rollback_key_prefix(storage_key_prefix: str, host_id: str) -> str:
    """The object key prefix holding one migrated workspace's rollback copy of its product stop artifact."""
    return f"{storage_key_prefix}{CUTOVER_KEY_SEGMENT}/{host_id}/{ROLLBACK_KEY_SEGMENT}"


@pure
def cutover_image_object_key(storage_key_prefix: str, version_tag: str) -> str:
    """The object key of the published ``default-workspace-template:<tag>`` image tar."""
    return f"{storage_key_prefix}{CUTOVER_KEY_SEGMENT}/{IMAGES_KEY_SEGMENT}/{version_tag}.tar.zst"


@pure
def cutover_transplant_dir(instance_name: str) -> str:
    """The box dir (on the storage partition) where one instance's data disk is downloaded and rebuilt."""
    return f"{CUTOVER_TRANSPLANT_ROOT}/{instance_name}"


@pure
def render_gen2_disk_transplant_script(
    *,
    # The instance's transfer dir on the box (an absolute path: this runs as
    # root over the management dial, so ``$HOME`` is not the service user's);
    # only its env file is read.
    transfer_dir_path: str,
    # Where the images are downloaded and built: ``cutover_transplant_dir``, on
    # the storage partition.
    transplant_dir_path: str,
    host_hex: str,
    migrated_data_disk_gib: int,
    expected_datadisk_sha256: str,
) -> str:
    """The root-run transplant: a fresh gen-2 data disk with the gen-1 home subvolume received into it.

    Downloads and verifies the drained gen-1 data disk, creates the gen-2
    qcow2 at the migrated size, attaches both through ``qemu-nbd``, formats the
    new one exactly like a gen-2 carve (whole-disk btrfs, the ``mngr-data``
    label, simple quotas, the ``1/0`` host qgroup), ``btrfs send | receive``s a
    read-only snapshot of the ``<host_hex>`` home subvolume, renames it back,
    makes it writable, and assigns it to the host qgroup so the transplanted
    data is fully accounted. Gen-1 backup snapshots are not carried. The disk
    is built under a ``.partial`` name and renamed to ``<transplant_dir>/datadisk.qcow2``
    only once complete (so a re-run never mistakes a crashed attempt's file for
    a finished transplant), with the transplant dir owned by the slice service
    user; the downloaded gen-1 image is deleted.
    """
    quoted_td = shlex.quote(transfer_dir_path)
    quoted_work = shlex.quote(transplant_dir_path)
    service_user = GEN2_SLICE_SERVICE_USER
    return f"""\
#!/bin/bash
set -Eeuo pipefail
export PATH=/usr/local/bin:$PATH
TD={quoted_td}
. "$TD/env"
install -d -m 751 -o {service_user} -g {service_user} {shlex.quote(CUTOVER_TRANSPLANT_ROOT)}
WORK={quoted_work}
# Service-user-owned from creation (install -d re-owns a crashed attempt's
# root-owned dir too): the migrate/rollback clear runs as the service user.
install -d -m 700 -o {service_user} -g {service_user} "$WORK"
OLD_IMG="$WORK/gen1-datadisk.qcow2"
NEW_IMG="$WORK/datadisk.qcow2"
NEW_IMG_PARTIAL="$NEW_IMG.partial"
OLD_MNT="$WORK/old"
NEW_MNT="$WORK/new"
OLD_DEV=""
NEW_DEV=""
IDF="$TD/identity"
cleanup() {{
    set +e
    mountpoint -q "$OLD_MNT" && umount "$OLD_MNT"
    mountpoint -q "$NEW_MNT" && umount "$NEW_MNT"
    [ -n "$OLD_DEV" ] && qemu-nbd -d "$OLD_DEV" >/dev/null
    [ -n "$NEW_DEV" ] && qemu-nbd -d "$NEW_DEV" >/dev/null
    rm -f "$IDF" "$OLD_IMG" "$NEW_IMG_PARTIAL"
}}
trap cleanup EXIT
umask 077
rm -f "$NEW_IMG" "$NEW_IMG_PARTIAL" "$WORK/DATADISK.sha"
printf '%s\\n' "$WS_AGE_IDENTITY" > "$IDF"

# Download + verify the drained gen-1 data disk (ciphertext sha, like a restore).
s5cmd --endpoint-url "$WS_S3_ENDPOINT" cat "s3://$WS_BUCKET/$WS_KEY_PREFIX/{DATADISK_OBJECT}" \\
    | tee >(sha256sum | awk '{{print $1}}' > "$WORK/DATADISK.sha") \\
    | age -d -i "$IDF" \\
    | zstd -q -d -f -o "$OLD_IMG"
for _ in $(seq 1 50); do
    [ -s "$WORK/DATADISK.sha" ] && break
    sleep 0.1
done
actual=$(cat "$WORK/DATADISK.sha")
if [ "$actual" != {shlex.quote(expected_datadisk_sha256)} ]; then
    echo "sha256 mismatch for {DATADISK_OBJECT}: got $actual" >&2
    exit 1
fi
rm -f "$IDF"

# A fresh gen-2 data disk at the migrated size, and both images on nbd devices
# (a free device has size 0; the gen-1 disk is partitioned, so its filesystem
# is on the first partition node, which the kernel creates asynchronously after
# the attach -- re-read the table and wait for it rather than trust one probe).
qemu-img create -q -f qcow2 "$NEW_IMG_PARTIAL" {migrated_data_disk_gib}G
attach_nbd() {{
    local image="$1" i last_error=""
    for i in $(seq 0 15); do
        [ "$(cat "/sys/class/block/nbd$i/size" 2>/dev/null || echo 1)" = "0" ] || continue
        if last_error=$(qemu-nbd -c "/dev/nbd$i" "$image" 2>&1); then
            # qemu-nbd returns before the kernel publishes the device size; a
            # format or mount that races it sees a 0-byte device.
            for _ in $(seq 1 100); do
                [ "$(cat "/sys/class/block/nbd$i/size" 2>/dev/null || echo 0)" != "0" ] && break
                sleep 0.1
            done
            if [ "$(cat "/sys/class/block/nbd$i/size" 2>/dev/null || echo 0)" = "0" ]; then
                qemu-nbd -d "/dev/nbd$i" >/dev/null 2>&1 || true
                echo "/dev/nbd$i never reported a size after attaching $image" >&2
                return 1
            fi
            echo "/dev/nbd$i"
            return 0
        fi
    done
    echo "qemu-nbd could not attach $image on any free nbd device: ${{last_error:-no free device}}" >&2
    return 1
}}
OLD_DEV=$(attach_nbd "$OLD_IMG") || exit 1
NEW_DEV=$(attach_nbd "$NEW_IMG_PARTIAL") || exit 1
udevadm settle 2>/dev/null || true
OLD_SRC="$OLD_DEV"
for _ in $(seq 1 20); do
    if [ -b "${{OLD_DEV}}p1" ]; then
        OLD_SRC="${{OLD_DEV}}p1"
        break
    fi
    blockdev --rereadpt "$OLD_DEV" 2>/dev/null || true
    sleep 0.5
done

# The gen-2 carve layout: whole-disk btrfs, the data label, simple quotas
# and the host qgroup created before any data lands.
mkfs.btrfs -q -L {GEN2_GUEST_DATA_FS_LABEL} "$NEW_DEV"
mkdir -p "$OLD_MNT" "$NEW_MNT"
mount "$OLD_SRC" "$OLD_MNT"
mount "$NEW_DEV" "$NEW_MNT"
btrfs quota enable --simple "$NEW_MNT"
btrfs qgroup create {GEN2_GUEST_HOST_QUOTA_QGROUP} "$NEW_MNT"
[ -d "$OLD_MNT/{host_hex}" ] || {{ echo "gen-1 data disk has no {host_hex} home subvolume" >&2; exit 1; }}

# Transplant the home subvolume: send needs a read-only snapshot; the
# received copy is renamed back, made writable, and joins the host qgroup
# (received extents belong to the receiving subvolume, so simple quotas
# account them fully).
btrfs subvolume snapshot -r "$OLD_MNT/{host_hex}" "$OLD_MNT/{host_hex}-cutover-ro"
btrfs send "$OLD_MNT/{host_hex}-cutover-ro" | btrfs receive "$NEW_MNT/"
mv "$NEW_MNT/{host_hex}-cutover-ro" "$NEW_MNT/{host_hex}"
# -f: a received subvolume keeps received_uuid, which current btrfs-progs
# refuse to flip rw without it. A rw snapshot instead would leave the extents
# accounted to the deleted received subvolume under simple quotas.
btrfs property set -f -ts "$NEW_MNT/{host_hex}" ro false
subvolume_id=$(btrfs subvolume show "$NEW_MNT/{host_hex}" | awk '/Subvolume ID:/ {{print $3}}')
btrfs qgroup assign "0/$subvolume_id" {GEN2_GUEST_HOST_QUOTA_QGROUP} "$NEW_MNT"
mkdir -p "$NEW_MNT/snapshots"
sync
umount "$NEW_MNT"
umount "$OLD_MNT"
qemu-nbd -d "$NEW_DEV" >/dev/null
qemu-nbd -d "$OLD_DEV" >/dev/null
NEW_DEV=""
OLD_DEV=""
rmdir "$OLD_MNT" "$NEW_MNT"
chown {service_user}:{service_user} "$NEW_IMG_PARTIAL"
chmod 660 "$NEW_IMG_PARTIAL"
mv "$NEW_IMG_PARTIAL" "$NEW_IMG"
echo "{TRANSPLANT_DONE_MARKER} $(stat -c %s "$NEW_IMG")"
"""


@pure
def build_disk_materialize_command(instance_name: str, transplant_dir_path: str) -> str:
    """After the fixed-ports reserve: the reflink boot disk (the carve's step 5) and the transplanted data disk.

    The connector's restore-reserve never creates disks (its download lands
    both), so the cutover materializes them itself: a reflink copy of the
    staged base image resized to the gen-2 boot size, and the transplant's
    prepared ``datadisk.qcow2`` moved into the slice dir (a rename: both sit
    on the storage partition).
    """
    slice_dir = shlex.quote(gen2_instance_dir(instance_name))
    transplant_dir = shlex.quote(transplant_dir_path)
    return (
        f"export PATH=/usr/local/bin:$PATH && "
        f"cp --reflink=auto {shlex.quote(GEN2_BASE_IMAGE_PATH)} {slice_dir}/disk.qcow2 && "
        f"qemu-img resize -q {slice_dir}/disk.qcow2 {GEN2_BOOT_DISK_GIB}G && "
        f"mv {transplant_dir}/datadisk.qcow2 {slice_dir}/datadisk.qcow2"
    )


@pure
def _as_simple_box_command(compound_command: str) -> str:
    """Wrap a compound command so it survives the slice clients' ``PATH=... <command>`` prefix.

    ``run_on_box`` sends every command as ``PATH=<dirs> <command>``; an
    assignment prefix followed by a reserved word (``if``, ``while``, ``for``)
    is a bash syntax error, so compound commands ride inside ``bash -c``.
    """
    return f"bash -c {shlex.quote(compound_command)}"


@pure
def build_transplant_rescue_command(instance_name: str, transplant_dir_path: str) -> str:
    """Before a re-run's reserve: stop a crashed attempt's unit and move its data disk back to the transplant dir.

    The reserve reclaims a leftover slice dir with ``rm -rf``; without this the
    transplanted disk a crashed attempt already materialized would go with it
    and the re-run would have to download and transplant again.
    """
    slice_dir = shlex.quote(gen2_instance_dir(instance_name))
    transplant_dir = shlex.quote(transplant_dir_path)
    body = (
        f"if [ -f {slice_dir}/datadisk.qcow2 ] && [ ! -f {transplant_dir}/datadisk.qcow2 ]; then "
        f"stale_ordinal=$(grep -s '^MNGR_SLICE_ORDINAL=' {slice_dir}/env | cut -d= -f2 || true); "
        f'if [ -n "$stale_ordinal" ]; then sudo /usr/bin/systemctl stop "mngr-slice@$stale_ordinal" || true; fi; '
        f"mkdir -p {transplant_dir} && mv {slice_dir}/datadisk.qcow2 {transplant_dir}/datadisk.qcow2; fi"
    )
    return _as_simple_box_command(body)


@pure
def build_transplant_clear_command(transplant_dir_path: str) -> str:
    """Before a fresh migration's first step: drop any prepared disk a previous attempt left.

    The transplant skips itself whenever ``datadisk.qcow2`` is already present,
    so a disk left behind by a rolled-back earlier migration of the same
    workspace would otherwise be reused with its stale pre-rollback data.
    """
    return _as_simple_box_command(f"rm -rf {shlex.quote(transplant_dir_path)}")


@pure
def build_unit_enable_command(ordinal: int) -> str:
    return f"sudo /usr/bin/systemctl enable {shlex.quote(slice_unit_name(ordinal))}"


@pure
def build_banner_wait_command(port: int, timeout_seconds: int) -> str:
    """A box-local sshd-banner wait against a forwarded port; exits 7 on timeout."""
    body = (
        f'while [ "$SECONDS" -lt {timeout_seconds} ]; do '
        f'if timeout 3 bash -c "exec 3<>/dev/tcp/127.0.0.1/{port} && head -c 4 <&3" 2>/dev/null | grep -q SSH; '
        f"then exit 0; fi; sleep 2; done; "
        f'echo "no SSH banner on port {port} after {timeout_seconds}s" >&2; exit 7'
    )
    return _as_simple_box_command(body)


@pure
def build_image_publish_command(*, transfer_dir_path: str, tar_path: str, image_object_key: str) -> str:
    """Stream a box-cached image tar to the tier bucket (plain zstd, no age: public template content)."""
    return (
        f"export PATH=/usr/local/bin:$PATH && . {shlex.quote(transfer_dir_path)}/env && "
        f"zstd -q -T0 -c {shlex.quote(tar_path)} | "
        f's5cmd --endpoint-url "$WS_S3_ENDPOINT" pipe "s3://$WS_BUCKET/{image_object_key}"'
    )


@pure
def build_image_load_command(
    *, transfer_dir_path: str, image_object_key: str, transfer_key_path: str, vm_ssh_port: int
) -> str:
    """Stream a published image tar from the bucket into the new VM's dockerd over the box loopback."""
    return (
        f"export PATH=/usr/local/bin:$PATH && . {shlex.quote(transfer_dir_path)}/env && "
        f's5cmd --endpoint-url "$WS_S3_ENDPOINT" cat "s3://$WS_BUCKET/{image_object_key}" | zstd -q -d | '
        f"ssh -i {shlex.quote(transfer_key_path)} {SLICE_LOOPBACK_SSH_OPTS} "
        f"-p {int(vm_ssh_port)} root@127.0.0.1 'docker load'"
    )


@pure
def build_container_id_command(host_id: str) -> str:
    """Print the workspace container's id (by the mngr host-id label), nothing when absent."""
    return f"docker ps -aq --filter {shlex.quote(f'label={GEN2_HOST_ID_CONTAINER_LABEL}={host_id}')}"


@pure
def build_workspace_version_probe_script(checkout_path: str) -> str:
    """A shell script printing the workspace checkout's release version: its nearest ``minds-v*`` tag, else the vendored pin.

    A workspace created from a branch rather than a release tag holds no tags at
    all (the desktop's single-branch clone fetches heads only), so ``git describe``
    finds nothing. Its vendored mngr still names the release it was cut from in
    ``FALLBACK_BRANCH``, which is the release image whose system layers match it,
    so that pin is the fallback. Prints nothing (and exits 0) when neither source
    names a version, so the caller reports the version, not a shell error.
    """
    describe = (
        f"git -c safe.directory={checkout_path} -C {checkout_path} describe --tags --match 'minds-v*' --abbrev=0"
    )
    vendored_build_info = f"{checkout_path}/system/vendor/mngr/apps/minds/imbue/minds/build_info.py"
    read_pin = (
        f"sed -n 's/^FALLBACK_BRANCH: Final\\[str\\] = \"\\(minds-v[0-9][0-9.]*\\)\"$/\\1/p' {vendored_build_info}"
    )
    return f"{describe} 2>/dev/null || {read_pin} 2>/dev/null || true"


@pure
def build_git_describe_command(container_id: str) -> str:
    """The version probe (``build_workspace_version_probe_script``), run inside the workspace container."""
    inner = build_workspace_version_probe_script(WORKSPACE_CHECKOUT_PATH)
    return f"docker exec --workdir / {shlex.quote(container_id)} sh -c {shlex.quote(inner)}"


# The home-layout verdicts the probe prints; the same classification as the
# ``repair-home-layout`` operator command's in-VM script.
HOME_LAYOUT_HOME: Final[str] = "home"
HOME_LAYOUT_LEGACY: Final[str] = "legacy"
HOME_LAYOUT_UNKNOWN: Final[str] = "unknown"


@pure
def build_home_layout_probe_command(container_id: str) -> str:
    """Print ``home`` / ``legacy`` / ``unknown`` for the workspace container's home layout.

    ``home``: ``/home/user`` is a symlink onto the volume's ``home/`` (the bake
    layout, where the data disk carries the whole home tree). ``legacy``: a real
    ``/home/user`` in the container's writable layer with only ``.mngr`` on the
    volume -- the slow-path rebuild layout ``repair-home-layout`` exists for.
    """
    inner = (
        f"if [ -L {WORKSPACE_HOME_PATH} ]; then echo {HOME_LAYOUT_HOME}; "
        f"elif [ -d {WORKSPACE_HOME_PATH} ] && [ -L {WORKSPACE_HOME_PATH}/.mngr ]; then echo {HOME_LAYOUT_LEGACY}; "
        f"else echo {HOME_LAYOUT_UNKNOWN}; fi"
    )
    return f"docker exec --workdir / {shlex.quote(container_id)} sh -c {shlex.quote(inner)}"


@pure
def home_layout_error_or_none(probe_output: str, host_id: str) -> str | None:
    """Why the container's home layout blocks a migration, or None when the data disk carries the home tree.

    The migrate transplants the data disk only, so a workspace whose home lives
    in the container's writable layer would come back empty.
    """
    layout = probe_output.strip()
    if layout == HOME_LAYOUT_HOME:
        return None
    remedy = f"run `minds-admin repair-home-layout --host-id {host_id} --migrate` on its gen-1 box first"
    if layout == HOME_LAYOUT_LEGACY:
        return (
            f"legacy home layout: /home/user lives in the container's writable layer, not on the data disk; {remedy}"
        )
    return f"unrecognized home layout {layout!r}: /home/user is neither a symlink onto the volume nor a legacy tree; {remedy}"


@pure
def build_vm_key_harvest_command() -> str:
    """Print the VM's sshd host key pair and root authorized_keys, each behind a file marker."""
    return _build_marked_cat_command(
        (f"{SSHD_HOST_KEY_PATH}", f"{SSHD_HOST_KEY_PATH}.pub", ROOT_AUTHORIZED_KEYS_PATH), ""
    )


@pure
def build_container_key_harvest_command(container_id: str) -> str:
    """Print the container's sshd host key pair and root authorized_keys, each behind a file marker."""
    return _build_marked_cat_command(
        (f"{SSHD_HOST_KEY_PATH}", f"{SSHD_HOST_KEY_PATH}.pub", ROOT_AUTHORIZED_KEYS_PATH),
        f"docker exec --workdir / {shlex.quote(container_id)} ",
    )


@pure
def _build_marked_cat_command(paths: Sequence[str], exec_prefix: str) -> str:
    """One ``sh -c`` per file: the marker line, the file, then a newline of its own.

    The trailing ``echo`` keeps the next marker on a line of its own even when
    the file does not end in a newline; ``&&`` keeps a missing file failing the
    chain (``cat``'s exit status) instead of harvesting an empty one.
    """
    parts = [
        f"{exec_prefix}sh -c {shlex.quote(f'echo {HARVEST_FILE_MARKER} {path}; cat {path} && echo')}" for path in paths
    ]
    return " && ".join(parts)


@pure
def _marked_file_content(lines: Sequence[str]) -> str:
    """A harvested file's text from its lines, always newline-terminated.

    The harvest command echoes one newline after each file, which shows up as
    a trailing empty line when the file already ended in a newline; dropping
    that one line returns such a file byte-for-byte.
    """
    kept = lines[:-1] if lines and lines[-1] == "" else lines
    return "\n".join(kept) + "\n"


@pure
def parse_marked_files(output: str) -> dict[str, str]:
    """Split ``MNGR_CUTOVER_FILE <path>``-delimited output into ``{path: content}``."""
    content_by_path: dict[str, str] = {}
    current_path: str | None = None
    current_lines: list[str] = []
    for line in output.splitlines():
        if line.startswith(f"{HARVEST_FILE_MARKER} "):
            if current_path is not None:
                content_by_path[current_path] = _marked_file_content(current_lines)
            current_path = line[len(HARVEST_FILE_MARKER) + 1 :].strip()
            current_lines = []
        elif current_path is not None:
            current_lines.append(line)
        else:
            # Output before the first marker is shell noise; nothing to keep.
            pass
    if current_path is not None:
        content_by_path[current_path] = _marked_file_content(current_lines)
    return content_by_path


@pure
def _guarded_base64_file_block(path_word: str) -> str:
    """One ``if [ -f ... ]`` block printing the marker line (path, mode) and the file's base64 on one line.

    ``path_word`` is a shell word: a quoted literal path, or the loop variable
    of the extensions listing. An absent file emits nothing at all (unlike the
    key harvest, where a missing file must fail the chain), and the content is
    base64 so it round-trips byte for byte (a bare ``cat`` cannot tell a file
    ending in a newline from one that does not). The reads are separate
    commands rather than an ``&&`` chain so the script's ``set -e`` ends the
    harvest on a file it cannot read, with the error on stderr.
    """
    return (
        f"if [ -f {path_word} ]; then "
        f"_mode=$(stat -c %a {path_word}); "
        f"printf '%s %s %s\\n' {LATCHKEY_HARVEST_FILE_MARKER} {path_word} \"$_mode\"; "
        f"base64 -w0 {path_word}; echo; fi"
    )


@pure
def build_vm_latchkey_harvest_command() -> str:
    """Print the VM's machine-owned latchkey state: every disk file, supervisord drop-in and tmpfs secret that exists.

    Refuses when the VM root's ``$HOME`` is not ``/root`` (the replay writes the
    harvested paths verbatim on the target, whose root home is ``/root``).
    """
    disk_paths = [f"{VM_LATCHKEY_DIR}/{filename}" for filename in MACHINE_LATCHKEY_DISK_FILENAMES]
    conf_paths = [
        f"{VM_LATCHKEY_SUPERVISOR_CONF_DIR}/{filename}" for filename in MACHINE_LATCHKEY_SUPERVISOR_CONF_FILENAMES
    ]
    tmpfs_paths = [f"{VM_LATCHKEY_TMPFS_DIR}/{filename}" for filename in MACHINE_LATCHKEY_TMPFS_FILENAMES]
    extensions_glob = f"{VM_LATCHKEY_DIR}/{REMOTE_EXTENSIONS_DIR_NAME}/*"
    extension_block = _guarded_base64_file_block('"$f"')
    lines = [
        "set -e",
        f'[ "$HOME" = {shlex.quote(VM_ROOT_HOME)} ] || {{ echo "VM root home is $HOME, not {VM_ROOT_HOME}" >&2; exit 1; }}',
        f"if [ -d {shlex.quote(VM_LATCHKEY_DIR)} ]; then echo {LATCHKEY_DIR_PRESENT_MARKER}; fi",
        *(_guarded_base64_file_block(shlex.quote(path)) for path in disk_paths),
        f"for f in {extensions_glob}; do {extension_block}; done",
        *(_guarded_base64_file_block(shlex.quote(path)) for path in [*conf_paths, *tmpfs_paths]),
    ]
    return "\n".join(lines)


@pure
def _normalized_octal_mode(mode: str, path: str) -> str:
    if not _OCTAL_MODE_RE.match(mode):
        raise CutoverError(f"latchkey harvest printed a malformed mode {mode!r} for {path}")
    return mode.zfill(4)


@pure
def parse_latchkey_harvest_output(output: str) -> HarvestedLatchkeyState:
    """Read the latchkey harvest's markers back into the harvested state (validating each file's base64)."""
    is_present = False
    files: list[HarvestedFile] = []
    lines = output.splitlines()
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        if line.strip() == LATCHKEY_DIR_PRESENT_MARKER:
            is_present = True
            idx += 1
        elif line.startswith(f"{LATCHKEY_HARVEST_FILE_MARKER} "):
            parts = line.split(" ")
            if len(parts) != 3:
                raise CutoverError(f"latchkey harvest printed a malformed marker line: {line!r}")
            path, mode = parts[1], parts[2]
            if idx + 1 >= len(lines):
                raise CutoverError(f"latchkey harvest output ends after the marker for {path}")
            encoded = lines[idx + 1].strip()
            try:
                base64.b64decode(encoded, validate=True)
            except binascii.Error as exc:
                raise CutoverError(f"latchkey harvest printed non-base64 content for {path}") from exc
            files.append(
                HarvestedFile(path=path, mode=_normalized_octal_mode(mode, path), content_base64=SecretStr(encoded))
            )
            idx += 2
        else:
            # Anything else is shell noise; nothing to keep.
            idx += 1
    return classify_harvested_latchkey_files(is_present, files)


@pure
def _tar_member(name: str, *, mode: int, size: int, type_flag: bytes) -> tarfile.TarInfo:
    member = tarfile.TarInfo(name=name)
    member.mode = mode
    member.size = size
    member.type = type_flag
    member.uid = 0
    member.gid = 0
    member.uname = "root"
    member.gname = "root"
    member.mtime = 0
    return member


@pure
def build_latchkey_replay_tar(files: Sequence[HarvestedFile], *, is_including_latchkey_dirs: bool) -> bytes:
    """One tar, rooted at ``/``, carrying the harvested files with their modes (and, for the disk group, the 0700 dirs).

    Extracted on the target with ``tar -xp -C /`` as root, which recreates
    each file byte for byte at its VM path with the origin's mode -- one
    upload and one command per group instead of a round trip per file, and
    the content never rides a command line.
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        if is_including_latchkey_dirs:
            for directory in _LATCHKEY_TAR_DIRS:
                archive.addfile(
                    _tar_member(directory.lstrip("/"), mode=_LATCHKEY_TAR_DIR_MODE, size=0, type_flag=tarfile.DIRTYPE)
                )
        for harvested in files:
            content = harvested.content
            archive.addfile(
                _tar_member(
                    harvested.path.lstrip("/"),
                    mode=int(harvested.mode, 8),
                    size=len(content),
                    type_flag=tarfile.REGTYPE,
                ),
                io.BytesIO(content),
            )
    return buffer.getvalue()


@pure
def build_latchkey_tar_extract_command(tar_path: str) -> str:
    """Extract a replay tar over ``/`` keeping its modes and owners, then drop the tar.

    The tar carries secrets, so it is removed whether or not the extract
    succeeded; the extract's own exit status is what the command reports.
    """
    quoted = shlex.quote(tar_path)
    return f"tar -xpf {quoted} -C /; _status=$?; rm -f {quoted}; exit $_status"


@pure
def tunnel_conf_container_ssh_port(conf_text: str) -> int | None:
    """The container sshd port the harvested tunnel drop-in dials on the VM loopback; None when it has none."""
    for line in conf_text.splitlines():
        if line.startswith("command="):
            match = _TUNNEL_CONF_PORT_RE.search(line)
            return int(match.group(1)) if match is not None else None
    return None


@pure
def container_ssh_host_port_from_inspect(inspect_entry: Mapping[str, Any]) -> int | None:
    """The VM-side port the container's sshd (``22/tcp``) is published on, per the harvested inspect."""
    bindings = ((inspect_entry.get("HostConfig") or {}).get("PortBindings") or {}).get("22/tcp") or []
    for binding in bindings:
        host_port = binding.get("HostPort")
        if host_port:
            return int(host_port)
    return None


@pure
def latchkey_tunnel_port_error_or_none(
    latchkey_state: HarvestedLatchkeyState, inspect_entry: Mapping[str, Any]
) -> str | None:
    """Why the harvested tunnel drop-in would dial the wrong port on the target, or None when it matches the container.

    The drop-in embeds the container's published sshd port; the container is
    recreated from the same inspect, so the two agree unless the origin's
    tunnel was configured against a port the container no longer publishes. A
    drop-in with no port to read, or an inspect with no published sshd, is
    refused too: the replayed tunnel could never connect, and the harvest is
    where that shows while the origin still runs.
    """
    tunnel_conf = next(
        (
            harvested
            for harvested in latchkey_state.supervisor_confs
            if posixpath.basename(harvested.path) == TUNNEL_CONF_FILENAME
        ),
        None,
    )
    if tunnel_conf is None:
        return None
    conf_port = tunnel_conf_container_ssh_port(tunnel_conf.content.decode("utf-8", "replace"))
    if conf_port is None:
        return (
            f"the latchkey tunnel drop-in {tunnel_conf.path} names no container sshd port (no '-p <port>' on its "
            "command line); the replayed tunnel would have nothing to dial"
        )
    inspect_port = container_ssh_host_port_from_inspect(inspect_entry)
    if inspect_port is None:
        return (
            "the container's inspect publishes no 22/tcp port on the VM, so the latchkey tunnel drop-in "
            f"(dialing port {conf_port}) would never connect"
        )
    if conf_port == inspect_port:
        return None
    return (
        f"the latchkey tunnel drop-in dials the container sshd on port {conf_port} but the container "
        f"publishes it on {inspect_port}; the replayed tunnel would never connect"
    )


@pure
def latchkey_gateway_files_error_or_none(latchkey_state: HarvestedLatchkeyState) -> str | None:
    """Why a FULL replay could not start the gateway and tunnel on the target, or None when every file it needs was harvested.

    A FULL replay restarts both supervisord programs, which needs the two
    drop-ins and the gateway's wrapper; the machine's tmpfs pair alone (what
    decides FULL) does not guarantee them, since provisioning writes the
    secrets before the wrapper and the drop-in. Checked at the harvest so an
    origin caught mid-provisioning is refused while it still runs, rather than
    failing at the restart with the row parked.
    """
    if latchkey_state.replay_plan is not LatchkeyReplayPlan.FULL:
        return None
    disk_names = {posixpath.basename(harvested.path) for harvested in latchkey_state.disk_files}
    conf_names = {posixpath.basename(harvested.path) for harvested in latchkey_state.supervisor_confs}
    missing = [
        *(name for name in (GATEWAY_RUN_SCRIPT_FILENAME,) if name not in disk_names),
        *(name for name in MACHINE_LATCHKEY_SUPERVISOR_CONF_FILENAMES if name not in conf_names),
    ]
    if not missing:
        return None
    return (
        "the origin holds the gateway's tmpfs pair but not "
        + ", ".join(missing)
        + "; the replayed gateway and tunnel could not be started"
    )


@pure
def build_vm_latchkey_supervisor_status_command() -> str:
    return f"supervisorctl status {GATEWAY_PROGRAM_NAME} {TUNNEL_PROGRAM_NAME}"


@pure
def build_vm_gateway_port_probe_command() -> str:
    """A TCP connect to the gateway's loopback port on the VM (its HTTP routes all need the listen password)."""
    return f"timeout 3 bash -c {shlex.quote(f'exec 3<>/dev/tcp/127.0.0.1/{OUTER_PORT}')}"


@pure
def latchkey_replay_detail(plan: LatchkeyReplayPlan | None) -> str:
    """The outcome note saying how much latchkey state the migration carried."""
    match plan:
        case None:
            return "latchkey state not harvested (record predates the latchkey leg)"
        case LatchkeyReplayPlan.ABSENT:
            return "no latchkey state on the origin"
        case LatchkeyReplayPlan.DISK_ONLY:
            return (
                "latchkey files replayed; the gateway starts at the desktop's next provisioning pass (no tmpfs pair)"
            )
        case LatchkeyReplayPlan.FULL:
            return "latchkey state replayed and the gateway restarted"
        case _ as unreachable:
            assert_never(unreachable)


@pure
def build_docker_inspect_command(container_id: str) -> str:
    return f"docker inspect {shlex.quote(container_id)}"


@pure
def parse_docker_inspect(output: str) -> dict[str, Any]:
    """The single container entry of a ``docker inspect <id>`` output."""
    try:
        entries = json.loads(output)
    except json.JSONDecodeError as exc:
        raise CutoverError(f"docker inspect output is not JSON: {exc}") from exc
    if not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], dict):
        raise CutoverError(f"docker inspect did not return exactly one container entry: {output[:200]!r}")
    entry = entries[0]
    for key in _INSPECT_ROOT_KEYS:
        if key not in entry:
            raise CutoverError(f"docker inspect entry lacks {key!r}")
    return entry


@pure
def build_gen1_datadisk_info_command(disk_name: str) -> str:
    """``qemu-img info`` of the lima data disk (``-U`` tolerates the running VM's lock)."""
    return f'qemu-img info -U --output=json "$HOME"/.lima/_disks/{shlex.quote(disk_name)}/datadisk'


@pure
def parse_qemu_img_info(output: str) -> tuple[str, int]:
    """(format, virtual-size bytes) from ``qemu-img info --output=json``; the top-level keys only."""
    try:
        info = json.loads(output)
    except json.JSONDecodeError as exc:
        raise CutoverError(f"qemu-img info output is not JSON: {exc}") from exc
    image_format = info.get("format")
    virtual_size = info.get("virtual-size")
    if not isinstance(image_format, str) or not isinstance(virtual_size, int):
        raise CutoverError(f"qemu-img info lacks format / virtual-size: {output[:200]!r}")
    return image_format, virtual_size


@pure
def build_supervisorctl_status_command(container_name: str) -> str:
    return f"docker exec --workdir / {shlex.quote(container_name)} supervisorctl status"


# supervisord's process states; a status line whose second token is none of
# these is not a program line but supervisorctl itself complaining (typically
# that the socket is missing because supervisord is not running).
_SUPERVISOR_STATES: Final[frozenset[str]] = frozenset(
    ("STOPPED", "STARTING", "RUNNING", "BACKOFF", "STOPPING", "EXITED", "FATAL", "UNKNOWN")
)
# EXITED is healthy: the template's one-shot programs run once and exit by design.
_SUPERVISOR_HEALTHY_STATES: Final[frozenset[str]] = frozenset(("RUNNING", "EXITED"))


@pure
def _parse_supervisorctl_outside_states(output: str, healthy_states: frozenset[str]) -> list[str]:
    unhealthy: list[str] = []
    for line in output.splitlines():
        parts = line.split()
        if not parts:
            continue
        if len(parts) >= 2 and parts[1] in _SUPERVISOR_STATES:
            if parts[1] not in healthy_states:
                unhealthy.append(f"{parts[0]} {parts[1]}")
        else:
            unhealthy.append(line.strip())
    return unhealthy


@pure
def parse_supervisorctl_unhealthy(output: str) -> list[str]:
    """The supervisord programs whose state is neither RUNNING nor EXITED, plus any non-program line (supervisord unreachable)."""
    return _parse_supervisorctl_outside_states(output, _SUPERVISOR_HEALTHY_STATES)


@pure
def parse_supervisorctl_not_running(output: str) -> list[str]:
    """The programs not RUNNING (the gateway and tunnel are long-running: EXITED is a failure for them), plus any non-program line."""
    return _parse_supervisorctl_outside_states(output, frozenset(("RUNNING",)))


@pure
def build_system_interface_probe_command(container_name: str) -> str:
    return f"docker exec --workdir / {shlex.quote(container_name)} curl -fsS -o /dev/null {SYSTEM_INTERFACE_URL}"


@pure
def build_container_running_command(container_name: str) -> str:
    return f"docker inspect -f '{{{{.State.Running}}}}' {shlex.quote(container_name)}"


@pure
def container_name_from_inspect(inspect_entry: Mapping[str, Any]) -> str:
    """The container's name (``<bake MNGR_PREFIX><host_name>``, which host_state.json addresses it by)."""
    name = str(inspect_entry["Name"]).lstrip("/")
    if not name:
        raise CutoverError("docker inspect entry has an empty Name")
    return name


@pure
def build_docker_create_args(
    inspect_entry: Mapping[str, Any],
    *,
    image_tag: str,
    guest_memory_mib: int,
) -> list[str]:
    """The ``docker create`` argument list replaying a harvested container with the gen-2 overrides.

    Kept from the inspect entry: the name, every ``com.imbue.mngr.*`` label,
    the env, the published ports, the mounts (the host volume, the snapshot
    trigger volume, the read-only snapshots bind) and the restart policy.
    Overridden: the image (the workspace's version tag), gVisor with the tmpfs
    mounts, ``--workdir=/``, ``no-new-privileges``, the memory cap derived
    from the machine's guest RAM, and the current entrypoint.
    """
    config = inspect_entry["Config"]
    host_config = inspect_entry["HostConfig"]
    args: list[str] = ["--name", container_name_from_inspect(inspect_entry)]
    labels = config.get("Labels") or {}
    for key in sorted(labels):
        args.extend(["--label", f"{key}={labels[key]}"])
    for env_entry in config.get("Env") or []:
        args.extend(["-e", str(env_entry)])
    port_bindings = host_config.get("PortBindings") or {}
    for container_port in sorted(port_bindings):
        for binding in port_bindings[container_port] or []:
            host_ip = binding.get("HostIp") or "0.0.0.0"
            args.extend(["-p", f"{host_ip}:{binding['HostPort']}:{container_port}"])
    for mount in inspect_entry.get("Mounts") or []:
        access = "rw" if mount.get("RW", True) else "ro"
        source = mount["Name"] if mount.get("Type") == "volume" else mount["Source"]
        args.extend(["-v", f"{source}:{mount['Destination']}:{access}"])
    restart_policy = (host_config.get("RestartPolicy") or {}).get("Name") or ""
    if restart_policy and restart_policy != "no":
        args.append(f"--restart={restart_policy}")
    args.extend(["--runtime", docker_runtime_name(GEN2_CONTAINER_RUNTIME)])
    args.extend(GEN2_CONTAINER_TMPFS_START_ARGS)
    args.extend(["--workdir=/", "--security-opt=no-new-privileges"])
    args.extend(build_slice_container_memory_start_args(guest_memory_mib))
    args.extend(["--entrypoint", "sh", image_tag, "-c", CONTAINER_ENTRYPOINT_CMD])
    return args


@pure
def authorized_keys_without(authorized_keys_text: str, excluded_public_key: str) -> str:
    """The ``authorized_keys`` text with every line carrying ``excluded_public_key`` dropped (comments kept)."""
    kept = [
        line
        for line in authorized_keys_text.splitlines()
        if line.strip().startswith("#") or not line.strip() or not is_same_ssh_public_key(line, excluded_public_key)
    ]
    return "\n".join(kept) + ("\n" if authorized_keys_text.endswith("\n") else "")


@pure
def build_replayed_container_files(
    keys: HarvestedKeys,
    *,
    # The tier's SSH CA the migrated container trusts for management SSH on its
    # gen-2 box (the gen-1 source trusted a static pool key instead).
    ssh_ca_public_key: str,
    # The gen-1 pool public key to strip from the harvested authorized_keys:
    # gen-2 authorizes no static management key.
    pool_public_key: str,
) -> tuple[ReplayedContainerFile, ...]:
    """The files copied into the recreated container before it first starts.

    The harvested sshd host key pair and root ``authorized_keys`` (minus the
    pool key), the tier CA trust, plus the provisioned marker mngr's own
    container setup writes next to the host key: the image's self-healing
    entrypoint starts sshd on a container (re)start only behind that marker,
    which is what brings the container back after a VM reboot or a connector
    stop/start (neither exec's sshd itself).
    """
    ca_trust_files = tuple(
        ReplayedContainerFile(container_path=trust_file.path, content=trust_file.content, mode=trust_file.mode)
        for trust_file in ssh_ca_trust_files(ssh_ca_public_key, {SSH_CA_ROOT_USER: SSH_CA_PRINCIPAL_CONTAINER})
    )
    return (
        ReplayedContainerFile(
            container_path=SSHD_HOST_KEY_PATH, content=keys.container_host_private_key.get_secret_value(), mode="0600"
        ),
        ReplayedContainerFile(
            container_path=f"{SSHD_HOST_KEY_PATH}.pub", content=keys.container_host_public_key, mode="0644"
        ),
        ReplayedContainerFile(
            container_path=ROOT_AUTHORIZED_KEYS_PATH,
            content=authorized_keys_without(keys.container_authorized_keys, pool_public_key),
            mode="0600",
        ),
        *ca_trust_files,
        ReplayedContainerFile(container_path=SSHD_PROVISIONED_MARKER_PATH, content="", mode="0644"),
    )


@pure
def replayed_container_dirs(replayed_files: Sequence[ReplayedContainerFile]) -> tuple[str, ...]:
    """The distinct container directories the replayed files land in, in first-seen order."""
    return tuple(dict.fromkeys(posixpath.dirname(replayed_file.container_path) for replayed_file in replayed_files))


@pure
def staged_container_dir_path(staging_dir_path: str, container_dir: str) -> str:
    """Where one container directory's replayed files are staged on the VM (``/root/.ssh`` -> ``<staging>/root_.ssh``).

    The replay copies each staged directory's *contents* into the container
    (``docker cp <staged>/. <container>:<dir>``): a lone-file ``docker cp``
    needs the destination's parent to already exist, and the image ships
    without ``/root/.ssh`` (mngr's container setup creates it at runtime),
    while a directory copy creates a missing destination and merges into an
    existing one.
    """
    return f"{staging_dir_path}/{container_dir.strip('/').replace('/', '_')}"


@pure
def staged_container_file_path(staging_dir_path: str, replayed_file: ReplayedContainerFile) -> str:
    """Where one replayed file is staged on the VM (its basename under its container directory's staged dir)."""
    staged_dir = staged_container_dir_path(staging_dir_path, posixpath.dirname(replayed_file.container_path))
    return f"{staged_dir}/{posixpath.basename(replayed_file.container_path)}"


@pure
def build_stage_replayed_container_files_command(
    staging_dir_path: str, replayed_files: Sequence[ReplayedContainerFile]
) -> str:
    """One VM command writing every replayed file into its staged dir with its mode (``docker cp`` keeps the modes).

    The staged dirs are created under ``umask 077``, so a container directory
    ``docker cp`` has to create from one (``/root/.ssh``) comes out 0700.
    """
    staged_dirs = " ".join(
        shlex.quote(staged_container_dir_path(staging_dir_path, container_dir))
        for container_dir in replayed_container_dirs(replayed_files)
    )
    parts = [f"umask 077 && mkdir -p {staged_dirs}"]
    for replayed_file in replayed_files:
        staged_path = shlex.quote(staged_container_file_path(staging_dir_path, replayed_file))
        encoded = base64.b64encode(replayed_file.content.encode()).decode()
        parts.append(
            f"printf '%s' {shlex.quote(encoded)} | base64 -d > {staged_path} && chmod {replayed_file.mode} {staged_path}"
        )
    return " && ".join(parts)


@pure
def _parse_template_settings(settings_toml_text: str) -> Mapping[str, Any]:
    """A default-workspace-template ``.mngr/settings.toml`` as a mapping."""
    try:
        return tomllib.loads(settings_toml_text)
    except tomllib.TOMLDecodeError as exc:
        raise CutoverError(f"default-workspace-template settings.toml does not parse: {exc}") from exc


@pure
def extract_autostart_installer_commands(settings: Mapping[str, Any]) -> tuple[str, ...]:
    """The pool_host template's outer autostart installer block(s) from a parsed default-workspace-template ``.mngr/settings.toml``."""
    try:
        commands = settings["create_templates"]["pool_host"]["post_host_create_outer_command__extend"]
    except KeyError as exc:
        raise CutoverError(
            "default-workspace-template settings.toml has no "
            "create_templates.pool_host.post_host_create_outer_command__extend (the autostart installer)"
        ) from exc
    if not isinstance(commands, list) or not commands or not all(isinstance(command, str) for command in commands):
        raise CutoverError("the pool_host autostart installer block is not a non-empty list of strings")
    return tuple(commands)


@pure
def extract_slice_volume_home_path(settings: Mapping[str, Any]) -> str:
    """The container home path (``/home/user``) the slice provider symlinks onto the host volume, from the parsed settings.

    mngr's container setup creates that symlink at bake time, so the image alone
    never carries it; the replayed container needs it recreated the same way.
    """
    try:
        volume_home_path = settings["providers"][SLICE_PROVIDER_INSTANCE_NAME]["volume_home_path"]
    except KeyError as exc:
        raise CutoverError(
            f"default-workspace-template settings.toml has no providers.{SLICE_PROVIDER_INSTANCE_NAME}.volume_home_path"
        ) from exc
    if not isinstance(volume_home_path, str) or not volume_home_path.startswith("/"):
        raise CutoverError(f"providers.{SLICE_PROVIDER_INSTANCE_NAME}.volume_home_path is not an absolute path")
    return volume_home_path


@pure
def extract_template_replay_inputs(settings_toml_text: str) -> TemplateReplayInputs:
    """Everything the restore replays from one version's ``.mngr/settings.toml``."""
    settings = _parse_template_settings(settings_toml_text)
    return TemplateReplayInputs(
        installer_commands=extract_autostart_installer_commands(settings),
        container_home_path=extract_slice_volume_home_path(settings),
    )
