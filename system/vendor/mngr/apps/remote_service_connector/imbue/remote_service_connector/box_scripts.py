"""Box-side scripts for workspace stop/start, rendered connector-side.

The transition supervisor SSHes a bare-metal box (as its lima service user)
and runs these. They are deliberately dumb: idempotent transfer pipelines
that stream the slice's qcow2 disks between the box and the tier's S3
bucket (``zstd | age | s5cmd``), reporting progress through a flat
KEY=VALUE status file the supervisor polls. All state-machine decisions
stay in the connector.

Duplicated constants (the lima naming scheme, the port range) mirror
``imbue.mngr_imbue_cloud.slices`` -- the shipped connector package must not
import the monorepo. Keep them in sync.
"""

import shlex
from typing import Final

from pydantic import BaseModel
from pydantic import Field

# Default box host-port range slices' forwards are chosen from. Mirrors
# ``mngr_imbue_cloud.slices.bare_metal.DEFAULT_SLICE_PORT_RANGE_*``.
SLICE_PORT_RANGE_START: Final[int] = 22000
SLICE_PORT_RANGE_END: Final[int] = 32000

# Marker file (inside the instance dir) that tells the box's
# ``mngr-slices-autostart.service`` NOT to boot this stopped VM at box boot:
# it is mid-upload (or mid-restore) and must only be started by a supervisor.
STOP_MARKER_FILENAME: Final[str] = "mngr-stop-requested"

# Everything transfer-related for one instance lives under this directory in
# the lima user's home: the detached script, its env (creds + age material),
# the status file, the log, and the pid file.
TRANSFER_DIR_ROOT: Final[str] = ".mngr-transfers"

# Box-wide lock serializing artifact downloads (one at a time per box, so a
# restore never competes with another restore for disk/network).
DOWNLOAD_LOCK_RELPATH: Final[str] = ".mngr-download.lock"
# How long a restore waits for the download lock before failing. Downloads
# run at ~1 GB/s against ~30 GB disks, so a legitimate queue clears in well
# under a minute; anything longer means the lock is stuck, and failing fast
# with a real error beats parking the workspace behind the transfer timeout.
DOWNLOAD_LOCK_WAIT_SECONDS: Final[int] = 300

# Object names within a generation prefix.
DISK_OBJECT: Final[str] = "disk.zst.age"
DATADISK_OBJECT: Final[str] = "datadisk.zst.age"
META_OBJECT: Final[str] = "meta.tar.zst.age"

# Marker printed by the restore-reserve script on success, followed by the two
# chosen host ports. Failure markers mirror the bake reserve script's.
RESTORE_RESERVED_MARKER: Final[str] = "MNGR_RESTORE_RESERVED"
RESTORE_BOX_FULL_MARKER: Final[str] = "MNGR_RESTORE_BOX_FULL"
RESTORE_NO_PORTS_MARKER: Final[str] = "MNGR_RESTORE_NO_PORTS"
# Printed (to stderr) by the restore rollback when the instance survived
# ``limactl delete``: its dirs were kept rather than pulled out from under a
# possibly-running VM.
CLEANUP_DELETE_FAILED_MARKER: Final[str] = "MNGR_CLEANUP_DELETE_FAILED"

# Slice lima resources are named mngr-slice-<env>-<host-hex>; the data disk
# adds this suffix. Mirrors ``mngr_imbue_cloud.slices.bare_metal``.
SLICE_DISK_SUFFIX: Final[str] = "-data"


class TransferEnv(BaseModel):
    """The env-file contents a transfer script sources (creds + object coordinates)."""

    s3_endpoint: str = Field(description="S3 endpoint URL")
    s3_region: str = Field(description="S3 region name")
    access_key_id: str = Field(description="S3 access key id")
    secret_access_key: str = Field(description="S3 secret access key")
    bucket: str = Field(description="Artifact bucket")
    key_prefix: str = Field(description="Object key prefix for this generation (e.g. <hex>/gen-2)")
    instance_name: str = Field(description="Lima instance name")
    age_recipient: str = Field(default="", description="age recipient for upload (empty on download)")
    age_identity: str = Field(default="", description="age identity for download (empty on upload)")


def transfer_dir(instance_name: str) -> str:
    return f"$HOME/{TRANSFER_DIR_ROOT}/{instance_name}"


def render_transfer_env(env: TransferEnv) -> str:
    """The env file a transfer script sources. Written 0600, deleted when the transfer ends."""
    lines = [
        f"export AWS_ACCESS_KEY_ID={shlex.quote(env.access_key_id)}",
        f"export AWS_SECRET_ACCESS_KEY={shlex.quote(env.secret_access_key)}",
        f"export AWS_REGION={shlex.quote(env.s3_region)}",
        f"export WS_S3_ENDPOINT={shlex.quote(env.s3_endpoint)}",
        f"export WS_BUCKET={shlex.quote(env.bucket)}",
        f"export WS_KEY_PREFIX={shlex.quote(env.key_prefix)}",
        f"export WS_INSTANCE={shlex.quote(env.instance_name)}",
        f"export WS_AGE_RECIPIENT={shlex.quote(env.age_recipient)}",
    ]
    if env.age_identity:
        lines.append(f"export WS_AGE_IDENTITY={shlex.quote(env.age_identity)}")
    return "\n".join(lines) + "\n"


# Shared bash prelude: PATH (limactl/s5cmd live in /usr/local/bin, age too),
# the transfer dir, and the atomic KEY=VALUE status writer. ``status_kv``
# appends a key to the pending status; ``status_flush`` publishes atomically.
_SCRIPT_PRELUDE: Final[str] = """\
set -Eeuo pipefail
export PATH=/usr/local/bin:$HOME/.local/bin:$PATH
TD="$HOME/{transfer_dir_root}/{instance}"
mkdir -p "$TD"
. "$TD/env"
STATUS="$TD/status"
declare -A STATUS_KV
status_kv() {{ STATUS_KV["$1"]="$2"; }}
status_flush() {{
    : > "$STATUS.tmp"
    for key in "${{!STATUS_KV[@]}}"; do printf '%s=%s\\n' "$key" "${{STATUS_KV[$key]}}" >> "$STATUS.tmp"; done
    mv "$STATUS.tmp" "$STATUS"
}}
fail() {{
    status_kv STAGE failed
    status_kv FINISHED 1
    status_kv ERROR "$1"
    status_flush
    exit 1
}}
trap 'fail "command failed: $BASH_COMMAND"' ERR
"""


def _prelude(instance_name: str) -> str:
    return _SCRIPT_PRELUDE.format(transfer_dir_root=TRANSFER_DIR_ROOT, instance=instance_name)


def render_upload_script(instance_name: str, disk_name: str) -> str:
    """The detached upload script: stream both disks + the metadata tar to S3.

    Each object is compressed, encrypted to the stop's age recipient, and
    piped straight into a multipart upload; the ciphertext sha256 + byte
    count are captured from the stream via ``tee`` so the connector can
    record them in the artifact manifest without a second pass.
    """
    return (
        _prelude(instance_name)
        + f"""\
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
status_flush

upload_one "$HOME/.lima/$WS_INSTANCE/disk" "{DISK_OBJECT}" DISK
upload_one "$HOME/.lima/_disks/{disk_name}/datadisk" "{DATADISK_OBJECT}" DATADISK

# Bundle the small instance files (config, cloud-init material) into one tar.
META_FILES=""
for f in lima.yaml cidata.iso cloud-config.yaml lima-version; do
    [ -e "$HOME/.lima/$WS_INSTANCE/$f" ] && META_FILES="$META_FILES $WS_INSTANCE/$f"
done
tar -C "$HOME/.lima" -cf "$TD/meta.tar" $META_FILES
upload_one "$TD/meta.tar" "{META_OBJECT}" META
rm -f "$TD/meta.tar"

status_kv STAGE uploaded
status_kv FINISHED 1
status_flush
"""
    )


def render_download_script(
    instance_name: str,
    disk_name: str,
    expected_sha_by_name: dict[str, str],
    vm_ssh_port: int,
    container_ssh_port: int,
) -> str:
    """The detached download script: restore both disks, boot the VM, wait for sshd.

    The metadata tar was already restored (and its ports rewritten) by the
    reserve step, so this only moves the two big objects, verifies their
    ciphertext sha256s against the manifest, boots the instance, and waits
    for both forwarded sshd ports to answer with an SSH banner.
    """
    expected_disk = shlex.quote(expected_sha_by_name["DISK"])
    expected_datadisk = shlex.quote(expected_sha_by_name["DATADISK"])
    return (
        _prelude(instance_name)
        + f"""\
# One artifact download at a time per box. The lock covers only the
# downloads: it is closed before the boot below because limactl's hostagent
# and qemu inherit every open descriptor and would otherwise keep the lock
# for the life of the VM.
status_kv STAGE waiting-for-lock
status_kv FINISHED 0
status_flush
exec 8> "$HOME/{DOWNLOAD_LOCK_RELPATH}"
flock -w {DOWNLOAD_LOCK_WAIT_SECONDS} 8 || fail "box download lock unavailable after {DOWNLOAD_LOCK_WAIT_SECONDS}s"

IDF="$TD/identity"
umask 077
printf '%s\\n' "$WS_AGE_IDENTITY" > "$IDF"
trap 'rm -f "$IDF"' EXIT

download_one() {{
    local object="$1" target="$2" name="$3" expected="$4"
    s5cmd --endpoint-url "$WS_S3_ENDPOINT" cat "s3://$WS_BUCKET/$WS_KEY_PREFIX/$object" \\
        | tee >(sha256sum | awk '{{print $1}}' > "$TD/$name.sha") \\
        | age -d -i "$IDF" \\
        | zstd -q -d -f -o "$target"
    for _ in $(seq 1 50); do
        [ -s "$TD/$name.sha" ] && break
        sleep 0.1
    done
    actual="$(cat "$TD/$name.sha")"
    if [ "$actual" != "$expected" ]; then
        fail "sha256 mismatch for $object: got $actual, expected $expected"
    fi
}}

wait_ssh() {{
    local port="$1"
    for _ in $(seq 1 90); do
        if timeout 3 bash -c "exec 3<>/dev/tcp/127.0.0.1/$port && head -c 4 <&3" 2>/dev/null | grep -q SSH; then
            return 0
        fi
        sleep 2
    done
    return 1
}}

status_kv STAGE downloading
status_flush

download_one "{DISK_OBJECT}" "$HOME/.lima/$WS_INSTANCE/disk" DISK {expected_disk}
download_one "{DATADISK_OBJECT}" "$HOME/.lima/_disks/{disk_name}/datadisk" DATADISK {expected_datadisk}
exec 8>&-

status_kv STAGE booting
status_flush
limactl --log-level=warn start "$WS_INSTANCE"

wait_ssh {vm_ssh_port} || fail "VM sshd did not come up on port {vm_ssh_port}"
wait_ssh {container_ssh_port} || fail "container sshd did not come up on port {container_ssh_port}"

rm -f "$HOME/.lima/$WS_INSTANCE/{STOP_MARKER_FILENAME}"
status_kv STAGE started
status_kv FINISHED 1
status_flush
"""
    )


def render_restore_reserve_script(
    instance_name: str,
    disk_name: str,
    slot_count: int,
    old_vm_ssh_port: int,
    old_container_ssh_port: int,
    expected_meta_sha: str,
) -> str:
    """The synchronous restore-reserve script: claim a slot + ports under the box lock.

    Mirrors the bake reserve script's guarantees: under the box-wide
    allocation flock it enforces capacity against the box's real disk
    count, picks two free host ports against bound ports plus every
    existing instance's recorded forwards, then durably claims the slot by
    materializing the instance dir (metadata tar, ports rewritten) and the
    data-disk dir (placeholder datadisk, overwritten by the download).
    Prints ``MNGR_RESTORE_RESERVED <vm_port> <container_port>``.
    """
    return (
        _prelude(instance_name)
        + f"""\
exec 9> "$HOME/.mngr-slice-alloc.lock"
flock 9

disk_count=$(limactl disk list --json 2>/dev/null \\
    | grep -oE '"name":[[:space:]]*"mngr-slice-[^"]*"' | wc -l | tr -d ' ' || true)
if [ "$disk_count" -ge {slot_count} ]; then
    echo "{RESTORE_BOX_FULL_MARKER} $disk_count/{slot_count}" >&2
    exit 4
fi

used_ports_file=$(mktemp)
trap 'rm -f "$used_ports_file"' EXIT
ss -Htln 2>/dev/null | awk '{{print $4}}' | sed 's/.*://' | grep -E '^[0-9]+$' >> "$used_ports_file" || true
for inst_yaml in "$HOME"/.lima/*/lima.yaml; do
    [ -f "$inst_yaml" ] || continue
    grep -oE 'hostPort:[[:space:]]*[0-9]+' "$inst_yaml" | grep -oE '[0-9]+' >> "$used_ports_file" || true
done

pick_port() {{
    local p
    for ((p={SLICE_PORT_RANGE_START}; p<{SLICE_PORT_RANGE_END}; p++)); do
        if ! grep -qx "$p" "$used_ports_file"; then
            echo "$p"
            return 0
        fi
    done
    return 1
}}
vm_port=$(pick_port) || {{ echo "{RESTORE_NO_PORTS_MARKER}" >&2; exit 3; }}
echo "$vm_port" >> "$used_ports_file"
container_port=$(pick_port) || {{ echo "{RESTORE_NO_PORTS_MARKER}" >&2; exit 3; }}

# Materialize the instance dir from the metadata tar (verified against the
# manifest) and rewrite the recorded host-port forwards to the chosen pair.
umask 077
IDF="$TD/identity"
printf '%s\\n' "$WS_AGE_IDENTITY" > "$IDF"
s5cmd --endpoint-url "$WS_S3_ENDPOINT" cat "s3://$WS_BUCKET/$WS_KEY_PREFIX/{META_OBJECT}" > "$TD/meta.enc"
actual=$(sha256sum "$TD/meta.enc" | awk '{{print $1}}')
if [ "$actual" != {shlex.quote(expected_meta_sha)} ]; then
    rm -f "$IDF" "$TD/meta.enc"
    fail "sha256 mismatch for {META_OBJECT}: got $actual"
fi
age -d -i "$IDF" < "$TD/meta.enc" | zstd -q -d | tar -x -C "$HOME/.lima"
rm -f "$IDF" "$TD/meta.enc"
instance_config="$HOME/.lima/$WS_INSTANCE/lima.yaml"
[ -f "$instance_config" ] || fail "metadata tar did not contain the lima instance config"

# Two-pass rewrite through unique placeholders: a chosen port may equal the
# OTHER forward's old port (the old ports came from a different box), and a
# single sequential sed pass would then re-match the just-rewritten line.
sed -i "s/hostPort: {old_vm_ssh_port}$/hostPort: MNGR_NEW_VM_PORT/; s/hostPort: {old_container_ssh_port}$/hostPort: MNGR_NEW_CONTAINER_PORT/" \\
    "$instance_config"
sed -i "s/hostPort: MNGR_NEW_VM_PORT$/hostPort: $vm_port/; s/hostPort: MNGR_NEW_CONTAINER_PORT$/hostPort: $container_port/" \\
    "$instance_config"

# Keep the box's autostart unit away from the half-restored VM; the download
# script removes the marker after a successful boot.
touch "$HOME/.lima/$WS_INSTANCE/{STOP_MARKER_FILENAME}"

# Claim the data-disk slot (the placeholder is overwritten by the download).
mkdir -p "$HOME/.lima/_disks/{disk_name}"
touch "$HOME/.lima/_disks/{disk_name}/datadisk"

echo "{RESTORE_RESERVED_MARKER} $vm_port $container_port"
"""
    )


def build_stop_vm_commands(instance_name: str) -> tuple[str, ...]:
    """Commands that halt the VM and mark it stop-requested (idempotent)."""
    quoted = shlex.quote(instance_name)
    return (
        f"limactl stop {quoted} 2>&1 || true",
        f"touch $HOME/.lima/{quoted}/{STOP_MARKER_FILENAME}",
    )


def build_launch_detached_command(instance_name: str, script_filename: str) -> str:
    """Launch a transfer script detached from the SSH session, recording its pid.

    Any prior status file is removed first so pollers only ever observe the
    launched transfer's own status (a stale file from an earlier failed or
    unrelated transfer must not masquerade as this one's result).
    """
    td = transfer_dir(instance_name)
    # The brace group backgrounds only the script itself: the stale-status
    # removal happens synchronously, before the launch command returns, so a
    # poller can never race it.
    return (
        f'cd "{td}" && rm -f status && '
        f'{{ setsid nohup bash {shlex.quote(script_filename)} >> run.log 2>&1 & echo $! > "{td}/pid"; }}'
    )


def build_is_transfer_alive_command(instance_name: str) -> str:
    """Exit 0 when the recorded transfer pid is still running."""
    td = transfer_dir(instance_name)
    return f'[ -f "{td}/pid" ] && kill -0 "$(cat "{td}/pid")" 2>/dev/null'


def build_read_status_command(instance_name: str) -> str:
    """Print the transfer status file (empty output when it does not exist yet)."""
    td = transfer_dir(instance_name)
    return f'cat "{td}/status" 2>/dev/null || true'


def build_cancel_and_restart_commands(instance_name: str) -> tuple[str, ...]:
    """Fast-path restart on the origin box: kill any upload, clear state, boot the VM."""
    td = transfer_dir(instance_name)
    quoted = shlex.quote(instance_name)
    return (
        f'if [ -f "{td}/pid" ]; then kill "$(cat "{td}/pid")" 2>/dev/null || true; fi',
        f'rm -rf "{td}"',
        f"rm -f $HOME/.lima/{quoted}/{STOP_MARKER_FILENAME}",
        f"PATH=/usr/local/bin:$HOME/.local/bin:$PATH limactl --log-level=warn start {quoted}",
    )


def build_finalize_stop_commands(instance_name: str, disk_name: str) -> tuple[str, ...]:
    """Delete the local VM + data disk once the retention window closes (frees the slot)."""
    td = transfer_dir(instance_name)
    quoted = shlex.quote(instance_name)
    quoted_disk = shlex.quote(disk_name)
    return (
        f"PATH=/usr/local/bin:$HOME/.local/bin:$PATH limactl delete --force {quoted}",
        f"PATH=/usr/local/bin:$HOME/.local/bin:$PATH limactl disk delete --force {quoted_disk}",
        f'rm -rf "{td}"',
    )


def build_cleanup_reserved_restore_commands(instance_name: str, disk_name: str) -> tuple[str, ...]:
    """Roll back a failed restore: stop the transfer, delete the VM, drop the claimed dirs.

    The detached transfer is signalled as a process group (it was launched
    under ``setsid``) so its pipeline children go with it, but only after the
    recorded pid's cwd proves it is still this instance's transfer -- the pid
    file may outlive the process on a shared box. ``limactl delete`` then
    retires the VM. Its exit status is not the survivor test (it sweeps
    every instance on the box afterwards and exits non-zero over unrelated
    wreckage); the instance config is. While that exists the VM may still be
    running off the instance dir, so the instance and disk dirs are kept
    (reapable, rather than a ghost running off unlinked inodes) and
    ``CLEANUP_DELETE_FAILED_MARKER`` is printed. The transfer dir goes
    either way: it holds the S3 credentials and the age identity.
    """
    td = transfer_dir(instance_name)
    quoted = shlex.quote(instance_name)
    quoted_disk = shlex.quote(disk_name)
    instance_config = f"$HOME/.lima/{quoted}/lima.yaml"
    kill_transfer = (
        f'if [ -f "{td}/pid" ] && pid=$(cat "{td}/pid") && [ "/proc/$pid/cwd" -ef "{td}" ]; '
        f'then pgid=$(ps -o pgid= -p "$pid" | tr -d " "); kill -TERM -- "-$pgid" 2>/dev/null || true; '
        f'for _ in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done; '
        f'kill -KILL -- "-$pgid" 2>/dev/null || true; fi'
    )
    remove_dirs_unless_vm_survived = (
        f'if [ -e "{instance_config}" ]; then echo "{CLEANUP_DELETE_FAILED_MARKER}" >&2; '
        f"else rm -rf $HOME/.lima/{quoted}; rm -rf $HOME/.lima/_disks/{quoted_disk}; fi"
    )
    return (
        kill_transfer,
        f"PATH=/usr/local/bin:$HOME/.local/bin:$PATH limactl delete --force {quoted} || true",
        remove_dirs_unless_vm_survived,
        f'rm -rf "{td}"',
    )


def parse_status_text(text: str) -> dict[str, str]:
    """Parse the flat KEY=VALUE status file a transfer script writes."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key.strip()] = value.strip()
    return values


def parse_reserved_ports_line(stdout: str) -> tuple[int, int] | None:
    """Parse ``MNGR_RESTORE_RESERVED <vm> <container>`` from the reserve run's stdout."""
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith(RESTORE_RESERVED_MARKER):
            parts = stripped.split()
            if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
                return int(parts[1]), int(parts[2])
    return None
