"""Generation-2 box-side lifecycle commands for workspace stop/start, rendered connector-side.

Gen-2 slices (specs/slice-fleet-gen2) are raw qemu VMs under systemd in a
per-instance directory layout, so every lifecycle command here targets
``/srv/mngr-slices/instances/<name>/`` where the gen-1 scripts target
``~/.lima``. The layout, the destroy script, the transfer conventions, and the
restore-reserve / download / in-place resize renderers come from
``imbue.mngr_imbue_cloud.slices.gen2_scripts`` (shipped into this container);
this module keeps only the connector's own ``&&``-joined command sequences and
the upload script.
"""

import shlex
from typing import Final

from imbue.mngr_imbue_cloud.slices.gen2_scripts.box_commands import build_qemu_destroy_script
from imbue.mngr_imbue_cloud.slices.gen2_scripts.guest import GEN2_CIDATA_FILE_NAMES
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_INSTANCES_DIR
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import gen2_instance_dir
from imbue.mngr_imbue_cloud.slices.gen2_scripts.transfer import DATADISK_OBJECT
from imbue.mngr_imbue_cloud.slices.gen2_scripts.transfer import DISK_OBJECT
from imbue.mngr_imbue_cloud.slices.gen2_scripts.transfer import META_OBJECT
from imbue.mngr_imbue_cloud.slices.gen2_scripts.transfer import script_prelude
from imbue.mngr_imbue_cloud.slices.gen2_scripts.transfer import transfer_dir

# The small per-instance files a gen-2 stop bundles into the meta tar. The
# cidata ISO is deliberately absent: it is rebuilt at restore from the three
# cidata files, copied verbatim (none of them depends on the placement); only
# the env file is re-rendered for the new ordinal and ports.
GEN2_META_TAR_FILES: Final[tuple[str, ...]] = (*GEN2_CIDATA_FILE_NAMES, "env")


# Resolves the slice's ordinal from its recorded env file into ``$ordinal``.
# Prefixed to lifecycle command sequences (the sequences are &&-joined into one
# shell, so the variable carries across the tuple's entries).
def _ordinal_lookup_command(instance_name: str) -> str:
    quoted_env = shlex.quote(f"{gen2_instance_dir(instance_name)}/env")
    return f'ordinal=$(grep -s "^MNGR_SLICE_ORDINAL=" {quoted_env} | cut -d= -f2) && [ -n "$ordinal" ]'


def build_gen2_stop_vm_commands(instance_name: str) -> tuple[str, ...]:
    """Halt the VM and take it out of boot autostart (idempotent).

    ``disable`` is the gen-2 stop marker: the unit's ``WantedBy`` is the whole
    boot-autostart story, so disabling it keeps a box reboot from booting a
    VM that is mid-upload or stopped. The start paths re-enable it. Separate
    ``stop`` + ``disable`` invocations because the scoped sudoers grants are
    exact-argument (flags like ``--now`` would break the match).
    """
    return (
        _ordinal_lookup_command(instance_name),
        'sudo /usr/bin/systemctl stop "mngr-slice@$ordinal"',
        'sudo /usr/bin/systemctl disable "mngr-slice@$ordinal"',
    )


def build_gen2_wait_banner_command(port: int, round_count: int) -> str:
    """A box-local sshd-banner wait against a forwarded port (via the loopback DNAT hairpin)."""
    return (
        f"for _ in $(seq 1 {round_count}); do "
        f'if timeout 3 bash -c "exec 3<>/dev/tcp/127.0.0.1/{port} && head -c 4 <&3" 2>/dev/null | grep -q SSH; '
        f"then break; fi; sleep 2; done && "
        f'timeout 3 bash -c "exec 3<>/dev/tcp/127.0.0.1/{port} && head -c 4 <&3" 2>/dev/null | grep -q SSH'
    )


def build_gen2_cancel_and_restart_commands(
    instance_name: str, vm_ssh_port: int, container_ssh_port: int
) -> tuple[str, ...]:
    """Fast-path restart on the origin box: kill any upload, re-enable + start, wait for sshd.

    The banner waits mirror the gen-1 ``limactl start`` semantics (which block
    until the guest is reachable): ``systemctl start`` returns as soon as qemu
    is spawned, so without the waits the row would land back on ``leased``
    before the VM answers.
    """
    instance_transfer_dir = transfer_dir(instance_name)
    return (
        f'if [ -f "{instance_transfer_dir}/pid" ]; then kill "$(cat "{instance_transfer_dir}/pid")" 2>/dev/null || true; fi',
        f'rm -rf "{instance_transfer_dir}"',
        _ordinal_lookup_command(instance_name),
        'sudo /usr/bin/systemctl enable "mngr-slice@$ordinal"',
        'sudo /usr/bin/systemctl start "mngr-slice@$ordinal"',
        build_gen2_wait_banner_command(vm_ssh_port, round_count=90),
        build_gen2_wait_banner_command(container_ssh_port, round_count=90),
    )


def build_gen2_finalize_stop_commands(instance_name: str) -> tuple[str, ...]:
    """Delete the local VM once the retention window closes (frees the slot).

    The multi-line destroy script rides in a subshell: the supervisor executes
    these tuples as one ``&&``-joined line, where a bare multi-line script
    would put ``&&`` at the start of a line (a bash syntax error) -- and the
    subshell also keeps the script's ``set -u`` scoped to itself.
    """
    instance_transfer_dir = transfer_dir(instance_name)
    return (
        f"(\n{build_qemu_destroy_script(instance_name)})",
        f'rm -rf "{instance_transfer_dir}"',
    )


def build_gen2_cleanup_reserved_restore_commands(instance_name: str) -> tuple[str, ...]:
    """Roll back a failed gen-2 restore: the destroy script already tolerates every partial state."""
    return build_gen2_finalize_stop_commands(instance_name)


def build_gen2_instance_exists_command(instance_name: str) -> str:
    """Exit 0 when the gen-2 slice's instance directory exists on the box."""
    return f"[ -d {shlex.quote(gen2_instance_dir(instance_name))} ]"


def render_gen2_upload_script(instance_name: str) -> str:
    """The detached gen-2 upload script: stream both qcow2 disks + the meta tar to S3."""
    meta_file_tests = "\n".join(
        f'[ -e "$SLICE_DIR/{name}" ] && META_FILES="$META_FILES $WS_INSTANCE/{name}"' for name in GEN2_META_TAR_FILES
    )
    return (
        script_prelude(instance_name)
        + f"""\
SLICE_DIR="{GEN2_INSTANCES_DIR}/$WS_INSTANCE"

upload_one() {{
    local src="$1" object="$2" name="$3"
    zstd -q -T0 -c "$src" \\
        | age -e -r "$WS_AGE_RECIPIENT" \\
        | tee >(sha256sum | awk '{{print $1}}' > "$TD/$name.sha") >(wc -c | tr -d ' ' > "$TD/$name.bytes") \\
        | s5cmd --endpoint-url "$WS_S3_ENDPOINT" pipe "s3://$WS_BUCKET/$WS_KEY_PREFIX/$object"
    # tee's process substitutions may still be flushing when the pipeline
    # returns; wait for the files to land before reading them.
    for _ in $(seq 1 50); do
        [ -s "$TD/$name.sha" ] && [ -s "$TD/$name.bytes" ] && break
        sleep 0.1
    done
    status_kv "SHA_$name" "$(cat "$TD/$name.sha")"
    status_kv "BYTES_$name" "$(cat "$TD/$name.bytes")"
    status_flush
}}

status_kv STAGE uploading
status_kv FINISHED 0
# The disks' virtual sizes are recorded in the artifact manifest: the restore
# sizes its df guard from the MEASURED disks rather than a box-derived
# approximation (specs/slice-fleet). The JSON parse
# must read the top-level key only: qemu 10's info JSON nests child (file
# protocol) nodes that carry their own virtual-size, so a grep over the whole
# document captures several numbers.
disk_virtual_bytes=$(qemu-img info --output=json "$SLICE_DIR/disk.qcow2" | python3 -c 'import json,sys; print(json.load(sys.stdin)["virtual-size"])')
datadisk_virtual_bytes=$(qemu-img info --output=json "$SLICE_DIR/datadisk.qcow2" | python3 -c 'import json,sys; print(json.load(sys.stdin)["virtual-size"])')
status_kv VIRTUAL_BYTES_DISK "$disk_virtual_bytes"
status_kv VIRTUAL_BYTES_DATADISK "$datadisk_virtual_bytes"
status_flush

upload_one "$SLICE_DIR/disk.qcow2" "{DISK_OBJECT}" DISK
upload_one "$SLICE_DIR/datadisk.qcow2" "{DATADISK_OBJECT}" DATADISK

# Bundle the small per-instance files (cloud-init material + env) into one tar.
META_FILES=""
{meta_file_tests}
tar -C "{GEN2_INSTANCES_DIR}" -cf "$TD/meta.tar" $META_FILES
upload_one "$TD/meta.tar" "{META_OBJECT}" META
rm -f "$TD/meta.tar"

status_kv STAGE uploaded
status_kv FINISHED 1
status_flush
"""
    )
