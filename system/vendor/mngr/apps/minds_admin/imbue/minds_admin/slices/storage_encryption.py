"""Gen-2 box storage encryption renderers: the LUKS storage volume and what moves onto it.

A gen-2 box keeps every slice's disks, the staged base image, the image tar
cache and the box swapfile on one md-mirrored partition at the storage root.
The gen-2 prep formats that partition as a LUKS2 volume and mounts the opened
mapper device there, so all of it is ciphertext at rest: a pulled disk, a
replaced NVMe, OVH's rescue mode without our key, and hardware decommissioning
all see nothing. The box unlocks itself at boot through its TPM 2.0 (sealed
with no PCR policy: any OS on this exact TPM can open it, which keeps kernel
updates from locking the box, at the price of not defending against someone
with rescue-mode access -- inside the operator trust boundary anyway), and a
per-box recovery passphrase in the tier's Vault opens it when the TPM cannot.
This is encryption at rest against the supplier and physical access, not
privacy from the operator: we hold the recovery key, and box root reads the
mapper.

The root partition stays plain. Everything user-adjacent it used to hold moves
onto the encrypted volume through prep-installed bind-mount units: the box
journal (which carries the guest consoles), the slice service user's home
(where transfers stage credentials and decrypted cidata), and the two temp
directories. The journal and the home are carried over and their root-side
directories emptied -- the home's re-owned by root -- so a box whose volume
failed to unlock cannot stage plaintext in the service user's home on the root
partition either; the temp directories are simply covered by their binds.

Everything here is a pure renderer consumed by the gen-2 box prep
(``minds-admin server prep`` / ``setup``) and ``minds-admin server unlock``.
The recovery passphrase never rides inside a rendered script: the CLI stages
it in a root-only directory on the box's ``/run`` tmpfs over a separate SSH
round trip and the prep consumes and deletes it.
"""

from typing import Final

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_SLICE_SERVICE_USER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_STORAGE_LUKS_MAPPER_NAME
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_STORAGE_LUKS_MAPPER_PATH
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_STORAGE_ROOT
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_STORAGE_SYSTEM_DIR
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_SWAPFILE_PATH

# Where the CLI stages the recovery passphrase for the prep to consume, and
# where the prep leaves the LUKS header backup for the CLI to fetch. Both live
# on tmpfs (RAM, never the disk) in a root-only directory under /run rather
# than in the world-writable /dev/shm, where any local user (a slice's qemu
# process included) could pre-create the fixed paths and have root write the
# passphrase into a file they can read; both are deleted by whoever consumes
# them.
STORAGE_STAGING_DIR: Final[str] = "/run/mngr-storage"
STORAGE_PASSPHRASE_STAGING_PATH: Final[str] = f"{STORAGE_STAGING_DIR}/passphrase"
STORAGE_HEADER_BACKUP_STAGING_PATH: Final[str] = f"{STORAGE_STAGING_DIR}/luks-header.img"

# The prep echoes the volume's LUKS UUID and backing device on this marker
# line so the CLI can name the header backup object and report the box.
STORAGE_ENCRYPTION_MARKER: Final[str] = "MNGR_STORAGE_ENCRYPTION"
# The unlock script's success marker.
STORAGE_UNLOCKED_MARKER: Final[str] = "MNGR_STORAGE_UNLOCKED"

# LUKS2 format parameters. AES-XTS with a 512-bit key (two 256-bit halves) is
# the dm-crypt default the AES-NI path is fastest on; 4096-byte sectors halve
# the crypto operations per byte under the XFS 4 KiB blocks on top (the NVMe
# reports 512-byte sectors, which LUKS2 tolerates for a fresh format); argon2id
# is the memory-hard KDF for the passphrase keyslot. The label doubles as the
# XFS label of the filesystem on the mapper.
STORAGE_LUKS_CIPHER: Final[str] = "aes-xts-plain64"
STORAGE_LUKS_KEY_SIZE_BITS: Final[int] = 512
STORAGE_LUKS_SECTOR_SIZE_BYTES: Final[int] = 4096
STORAGE_LUKS_PBKDF: Final[str] = "argon2id"
STORAGE_VOLUME_LABEL: Final[str] = "mngr-storage"

# Persistent dm-crypt flags stored in the LUKS2 header at first open, so every
# later open (the TPM unlock at boot, the manual unlock) gets them without
# repeating the flags: discards pass through to the NVMe (the weekly fstrim
# and qcow2 hole-punching would otherwise stop reclaiming space, at the cost
# of exposing which blocks are in use), and the read/write workqueues are
# bypassed, which is cryptsetup's own recommendation for NVMe latency.
STORAGE_LUKS_OPEN_FLAGS: Final[tuple[str, ...]] = (
    "--allow-discards",
    "--perf-no_read_workqueue",
    "--perf-no_write_workqueue",
)

# The crypttab options: the TPM opens the volume at boot with no prompt ever
# (``headless``), a failed unlock does not hold the boot (``nofail``; the
# telemetry collector then raises STORAGE_VOLUME_LOCKED and ``server unlock``
# opens it by passphrase), and discards are allowed at this layer too.
STORAGE_CRYPTTAB_PATH: Final[str] = "/etc/crypttab"
STORAGE_CRYPTTAB_OPTIONS: Final[str] = "luks,discard,tpm2-device=auto,headless=true,nofail"
STORAGE_FSTAB_PATH: Final[str] = "/etc/fstab"
# The storage mount's fstab options: ``nofail`` so a locked volume never holds
# the boot, with a bounded wait for the mapper device to appear.
STORAGE_FSTAB_OPTIONS: Final[str] = "defaults,nofail,x-systemd.device-timeout=90s"

# The root-partition paths that move onto the encrypted volume, each backed
# by a directory under the volume's system tree and bind-mounted over its
# usual path by a prep-installed mount unit. The units are started together in
# one systemd transaction (no ordering among them), and the journal is flushed
# only after every mount is up.
STORAGE_JOURNAL_DIR: Final[str] = f"{GEN2_STORAGE_SYSTEM_DIR}/journal"
STORAGE_SERVICE_USER_HOME_DIR: Final[str] = f"{GEN2_STORAGE_SYSTEM_DIR}/home/{GEN2_SLICE_SERVICE_USER}"
STORAGE_TMP_DIR: Final[str] = f"{GEN2_STORAGE_SYSTEM_DIR}/tmp"
STORAGE_VAR_TMP_DIR: Final[str] = f"{GEN2_STORAGE_SYSTEM_DIR}/var-tmp"
JOURNAL_MOUNT_POINT: Final[str] = "/var/log/journal"
SERVICE_USER_HOME_MOUNT_POINT: Final[str] = f"/home/{GEN2_SLICE_SERVICE_USER}"
TMP_MOUNT_POINT: Final[str] = "/tmp"
VAR_TMP_MOUNT_POINT: Final[str] = "/var/tmp"


class StorageBindMount(FrozenModel):
    """One root-partition path relocated onto the encrypted volume by a prep-installed bind-mount unit."""

    source_dir: str = Field(description="The volume-side directory the bind is mounted from")
    mount_point: str = Field(description="The root-partition path the bind covers")
    mount_options: str = Field(description="The mount(8) options of the bind (the unit's Options= line)")


# The temp directories carry the ``nosuid,nodev`` Debian's stock tmpfs
# ``tmp.mount`` gives /tmp, so a setuid file or device node planted there by a
# root process is inert.
STORAGE_BIND_MOUNTS: Final[tuple[StorageBindMount, ...]] = (
    StorageBindMount(source_dir=STORAGE_JOURNAL_DIR, mount_point=JOURNAL_MOUNT_POINT, mount_options="bind"),
    StorageBindMount(
        source_dir=STORAGE_SERVICE_USER_HOME_DIR, mount_point=SERVICE_USER_HOME_MOUNT_POINT, mount_options="bind"
    ),
    StorageBindMount(source_dir=STORAGE_TMP_DIR, mount_point=TMP_MOUNT_POINT, mount_options="bind,nosuid,nodev"),
    StorageBindMount(
        source_dir=STORAGE_VAR_TMP_DIR, mount_point=VAR_TMP_MOUNT_POINT, mount_options="bind,nosuid,nodev"
    ),
)

# journald flushes its boot-time buffer into /var/log/journal once the flush
# service runs; this drop-in makes that conditional on the journal actually
# being the bind-mounted volume directory, so a box whose volume stayed locked
# keeps its journal in RAM (/run) instead of writing the guest consoles onto
# the plain root partition.
JOURNAL_FLUSH_DROP_IN_PATH: Final[str] = "/etc/systemd/system/systemd-journal-flush.service.d/mngr-storage.conf"


@pure
def mount_unit_name(mount_point: str) -> str:
    """The systemd mount unit name for an absolute path (``/var/log/journal`` -> ``var-log-journal.mount``)."""
    escaped = mount_point.strip("/").replace("-", "\\x2d").replace("/", "-")
    return f"{escaped}.mount"


@pure
def mount_unit_path(mount_point: str) -> str:
    return f"/etc/systemd/system/{mount_unit_name(mount_point)}"


STORAGE_BIND_MOUNT_UNIT_PATHS: Final[tuple[str, ...]] = tuple(
    mount_unit_path(bind_mount.mount_point) for bind_mount in STORAGE_BIND_MOUNTS
)


@pure
def render_storage_bind_mount_unit(bind_mount: StorageBindMount) -> str:
    """A bind-mount unit for one relocated path, gated on the encrypted volume being mounted.

    ``RequiresMountsFor`` orders it after the storage mount; the directory
    condition makes a locked box skip it cleanly (the unit reports a failed
    condition, not a failure) rather than bind-mounting the bare mountpoint.
    Installed under ``/etc/systemd/system`` so ``tmp.mount`` overrides the
    distro's static tmpfs unit of the same name: the bind, not a RAM-backed
    tmpfs, is what the box gets.
    """
    return f"""\
[Unit]
Description=mngr: {bind_mount.mount_point} on the encrypted storage volume
RequiresMountsFor={GEN2_STORAGE_ROOT}
ConditionPathIsDirectory={bind_mount.source_dir}

[Mount]
What={bind_mount.source_dir}
Where={bind_mount.mount_point}
Type=none
Options={bind_mount.mount_options}

[Install]
WantedBy=local-fs.target
"""


@pure
def render_journal_flush_drop_in() -> str:
    return f"""\
[Unit]
ConditionPathIsMountPoint={JOURNAL_MOUNT_POINT}
"""


@pure
def _render_converged_root_file(content: str, path: str, mode: str, heredoc_tag: str) -> str:
    """Bash that installs ``content`` at ``path`` only when it differs, reporting a change via ``is_units_changed``."""
    return f"""\
cat > {path}.mngr-tmp <<'{heredoc_tag}'
{content}\
{heredoc_tag}
if ! cmp -s {path}.mngr-tmp {path}; then
    install -m {mode} {path}.mngr-tmp {path}
    is_units_changed=1
fi
rm -f {path}.mngr-tmp
"""


@pure
def render_gen2_storage_encryption_section() -> str:
    """The idempotent root bash section that makes the storage root a mounted LUKS2 volume.

    Three starting states converge to the same end: the storage root mounted
    from the opened mapper, a crypttab entry for it, the recovery passphrase
    verified against a keyslot, a TPM keyslot freshly sealed to the box's
    current TPM (the stale one is never trusted: a cleared or replaced TPM
    leaves its token in the header), and a fresh header backup staged for the
    CLI. The staged passphrase is required in every state: it keys the format,
    is verified, and is what the TPM keyslot is enrolled from.

    - **Already encrypted** (the mapper is mounted): converge the crypttab
      entry and re-seal to the TPM; nothing is reformatted.
    - **A plain partition is mounted** (a fresh or repaved box, where OVH's
      reinstall left an empty XFS): refused outright when the partition holds
      any slice -- there is no in-place conversion; drain and repave -- and
      otherwise wiped and formatted. The recovery passphrase must have been
      staged by the CLI, which stores it in the tier's Vault before the format
      so a crash mid-format never leaves an unrecoverable box.
    - **Nothing is mounted**: an opened-but-unmounted mapper is mounted; a
      locked volume (a crypttab entry but no mapper) is opened with the staged
      passphrase, and the re-seal that follows is how a box whose TPM was
      replaced gets its boot unlock back; a box with neither has no storage
      partition at all and is refused.

    The passphrase file is consumed and deleted here; the header backup is
    regenerated on every run (the re-seal alters it) for the CLI to fetch from
    tmpfs and upload.
    """
    open_flags = " ".join(STORAGE_LUKS_OPEN_FLAGS)
    return f"""\
# Storage encryption: the storage partition is a LUKS2 volume unlocked by the
# box's TPM at boot (recovery passphrase in the tier's Vault), mounted at the
# storage root from its mapper device. See slices/storage_encryption.py.
STORAGE_ROOT={GEN2_STORAGE_ROOT}
STORAGE_MAPPER={GEN2_STORAGE_LUKS_MAPPER_NAME}
STORAGE_MAPPER_PATH={GEN2_STORAGE_LUKS_MAPPER_PATH}
STORAGE_PASSPHRASE_FILE={STORAGE_PASSPHRASE_STAGING_PATH}
STORAGE_HEADER_BACKUP_FILE={STORAGE_HEADER_BACKUP_STAGING_PATH}
storage_backing_device() {{
    cryptsetup status "$STORAGE_MAPPER" | awk '/^ *device:/ {{print $2}}'
}}
storage_source=""
if mountpoint -q "$STORAGE_ROOT"; then
    storage_source=$(findmnt -no SOURCE "$STORAGE_ROOT")
fi
if [ "$storage_source" = "$STORAGE_MAPPER_PATH" ]; then
    storage_device=$(storage_backing_device)
elif [ -n "$storage_source" ]; then
    # A plain partition is mounted at the storage root. Only an empty one is
    # encrypted (by wiping it): a partition already holding slices is refused,
    # since there is no in-place conversion -- drain and repave the box.
    if [ -n "$(ls -A "$STORAGE_ROOT/instances" 2>/dev/null)" ]; then
        echo "ERROR: $STORAGE_ROOT is mounted from the unencrypted $storage_source and holds slices; every gen-2 storage partition must be a LUKS volume. Drain the box (minds-admin server drain) and repave it (minds-admin cutover repave, or server setup) to encrypt it." >&2
        exit 1
    fi
    if [ ! -s "$STORAGE_PASSPHRASE_FILE" ]; then
        echo "ERROR: $STORAGE_ROOT is unencrypted and no recovery passphrase is staged at $STORAGE_PASSPHRASE_FILE; run the prep through minds-admin server prep / setup, which stages it" >&2
        exit 1
    fi
    storage_device="$storage_source"
    swapoff {GEN2_SWAPFILE_PATH} 2>/dev/null || true
    # The telemetry collector's timer may be mid-read under the root for a
    # moment; a busy unmount is retried briefly rather than failing the prep.
    for _ in 1 2 3 4 5; do
        if umount "$STORAGE_ROOT"; then
            break
        fi
        sleep 2
    done
    mountpoint -q "$STORAGE_ROOT" && {{ echo "ERROR: could not unmount $STORAGE_ROOT to encrypt it" >&2; exit 1; }}
    wipefs -a "$storage_device"
    cryptsetup luksFormat --batch-mode --type luks2 --cipher {STORAGE_LUKS_CIPHER} --key-size {STORAGE_LUKS_KEY_SIZE_BITS} \\
        --sector-size {STORAGE_LUKS_SECTOR_SIZE_BYTES} --pbkdf {STORAGE_LUKS_PBKDF} --label {STORAGE_VOLUME_LABEL} \\
        --key-file "$STORAGE_PASSPHRASE_FILE" "$storage_device"
    cryptsetup open --key-file "$STORAGE_PASSPHRASE_FILE" {open_flags} --persistent "$storage_device" "$STORAGE_MAPPER"
    mkfs.xfs -f -L {STORAGE_VOLUME_LABEL} "$STORAGE_MAPPER_PATH"
    # The reinstall's plain-partition fstab line gives way to the mapper.
    awk -v root="$STORAGE_ROOT" '$2 != root' {STORAGE_FSTAB_PATH} > {STORAGE_FSTAB_PATH}.mngr-tmp \\
        && mv {STORAGE_FSTAB_PATH}.mngr-tmp {STORAGE_FSTAB_PATH}
    echo "$STORAGE_MAPPER_PATH $STORAGE_ROOT xfs {STORAGE_FSTAB_OPTIONS} 0 0" >> {STORAGE_FSTAB_PATH}
    systemctl daemon-reload
    mount "$STORAGE_ROOT"
else
    if [ -e "$STORAGE_MAPPER_PATH" ]; then
        mount "$STORAGE_ROOT"
    elif [ -s "$STORAGE_PASSPHRASE_FILE" ] && grep -q "^$STORAGE_MAPPER " {STORAGE_CRYPTTAB_PATH} 2>/dev/null; then
        # A locked volume (the TPM unlock failed at boot): open it with the
        # staged passphrase; the enrollment below re-seals it to the TPM.
        crypttab_uuid=$(awk -v n="$STORAGE_MAPPER" '$1 == n {{print $2}}' {STORAGE_CRYPTTAB_PATH})
        locked_device=$(blkid -U "${{crypttab_uuid#UUID=}}")
        cryptsetup open --key-file "$STORAGE_PASSPHRASE_FILE" "$locked_device" "$STORAGE_MAPPER"
        mount "$STORAGE_ROOT"
    else
        echo "ERROR: $STORAGE_ROOT is not a mounted filesystem; provision the XFS storage partition first (the gen-2 reinstall's custom layout does), or for a locked volume run minds-admin server unlock" >&2
        exit 1
    fi
    storage_device=$(storage_backing_device)
fi
storage_luks_uuid=$(cryptsetup luksUUID "$storage_device")
storage_crypttab_line="$STORAGE_MAPPER UUID=$storage_luks_uuid none {STORAGE_CRYPTTAB_OPTIONS}"
touch {STORAGE_CRYPTTAB_PATH}
if ! grep -qxF "$storage_crypttab_line" {STORAGE_CRYPTTAB_PATH}; then
    awk -v n="$STORAGE_MAPPER" '$1 != n' {STORAGE_CRYPTTAB_PATH} > {STORAGE_CRYPTTAB_PATH}.mngr-tmp \\
        && mv {STORAGE_CRYPTTAB_PATH}.mngr-tmp {STORAGE_CRYPTTAB_PATH}
    echo "$storage_crypttab_line" >> {STORAGE_CRYPTTAB_PATH}
    systemctl daemon-reload
fi
if [ ! -s "$STORAGE_PASSPHRASE_FILE" ]; then
    echo "ERROR: no recovery passphrase is staged at $STORAGE_PASSPHRASE_FILE to verify and re-seal the storage volume with; run the prep through minds-admin server prep / setup, which stages it" >&2
    exit 1
fi
# The staged passphrase must open a keyslot: what Vault holds is what unlocks
# this box. Verified before the header is touched below.
cryptsetup open --test-passphrase --key-file "$STORAGE_PASSPHRASE_FILE" "$storage_device"
# Re-seal the volume to the box's TPM on every run (no PCR policy, so kernel
# and firmware updates never lock the box). A token in the header proves
# nothing: a cleared or replaced TPM leaves its stale token on disk, so the
# stale tpm2 keyslots are wiped and a fresh one enrolled (the new slot is
# enrolled first and survives the wipe), which also proves the boot-time
# unlock path works before the prep succeeds.
systemd-cryptenroll --unlock-key-file="$STORAGE_PASSPHRASE_FILE" --tpm2-device=auto --tpm2-pcrs= --wipe-slot=tpm2 "$storage_device"
rm -f "$STORAGE_PASSPHRASE_FILE"
# A fresh header backup (keyslot changes alter it) for the CLI to fetch from
# tmpfs and upload to the tier bucket; a corrupt header loses every slice on
# the box at once, and the RAID mirror does not protect against a bad write.
previous_umask=$(umask)
umask 077
install -d -m 700 {STORAGE_STAGING_DIR}
rm -f "$STORAGE_HEADER_BACKUP_FILE"
cryptsetup luksHeaderBackup "$storage_device" --header-backup-file "$STORAGE_HEADER_BACKUP_FILE"
umask "$previous_umask"
echo "{STORAGE_ENCRYPTION_MARKER} $storage_luks_uuid $storage_device"
"""


@pure
def render_gen2_storage_relocation_section() -> str:
    """The idempotent root bash section moving the root partition's user-adjacent state onto the volume.

    Runs right after the storage volume is mounted. The volume-side
    directories are created with their usual ownership and modes; the first
    run carries the journal so far and the service user's home (its uv
    install) over and leaves those two root-side directories as empty stubs --
    the home re-owned by root, so a locked box has no writable home to stage
    plaintext in -- while the temp directories are only covered by their
    binds, never copied or emptied. The bind mounts are systemd mount units
    (content-converged like every other prep artifact), enabled for boot and
    started now -- restarted where something else already occupies the mount
    point, since Debian ships an active tmpfs ``tmp.mount`` that the unit of
    the same name in /etc merely overrides on disk until it is restarted; the
    journal is relinquished to RAM around the swap so journald never writes
    to the bare mountpoint, then flushed onto the volume.
    """
    unit_installs = "".join(
        _render_converged_root_file(
            render_storage_bind_mount_unit(bind_mount),
            mount_unit_path(bind_mount.mount_point),
            "644",
            "MNGR_STORAGE_MOUNT_UNIT",
        )
        for bind_mount in STORAGE_BIND_MOUNTS
    )
    unit_names = " ".join(mount_unit_name(bind_mount.mount_point) for bind_mount in STORAGE_BIND_MOUNTS)
    foreign_mount_restarts = "".join(
        f"""\
if mountpoint -q {bind_mount.mount_point} && [ "$(findmnt -no SOURCE {bind_mount.mount_point} | cut -d'[' -f1)" != {GEN2_STORAGE_LUKS_MAPPER_PATH} ]; then
    systemctl restart {mount_unit_name(bind_mount.mount_point)}
fi
"""
        for bind_mount in STORAGE_BIND_MOUNTS
    )
    flush_drop_in_dir = JOURNAL_FLUSH_DROP_IN_PATH.rsplit("/", 1)[0]
    return f"""\
# Relocate the root partition's user-adjacent state onto the encrypted volume:
# the box journal (guest consoles), the slice service user's home (transfer
# staging), and both temp directories, each bind-mounted over its usual path
# by a mount unit. See slices/storage_encryption.py.
install -d -m 755 {GEN2_STORAGE_SYSTEM_DIR} {GEN2_STORAGE_SYSTEM_DIR}/home
install -d -m 2755 -o root -g systemd-journal {STORAGE_JOURNAL_DIR}
install -d -m 700 -o {GEN2_SLICE_SERVICE_USER} -g {GEN2_SLICE_SERVICE_USER} {STORAGE_SERVICE_USER_HOME_DIR}
install -d -m 1777 {STORAGE_TMP_DIR} {STORAGE_VAR_TMP_DIR}
if ! mountpoint -q {JOURNAL_MOUNT_POINT}; then
    journalctl --relinquish-var
    if [ -d {JOURNAL_MOUNT_POINT} ]; then
        cp -a {JOURNAL_MOUNT_POINT}/. {STORAGE_JOURNAL_DIR}/
        find {JOURNAL_MOUNT_POINT} -mindepth 1 -delete
    fi
    install -d -m 2755 -o root -g systemd-journal {JOURNAL_MOUNT_POINT}
fi
if ! mountpoint -q {SERVICE_USER_HOME_MOUNT_POINT}; then
    cp -a {SERVICE_USER_HOME_MOUNT_POINT}/. {STORAGE_SERVICE_USER_HOME_DIR}/
    # cp -a of the directory's own "." entry carries the root-side ownership
    # and mode over too; on a re-run with the bind down (a box prepped while
    # its volume was locked) that is the root-owned stub made below.
    chown {GEN2_SLICE_SERVICE_USER}:{GEN2_SLICE_SERVICE_USER} {STORAGE_SERVICE_USER_HOME_DIR}
    chmod 700 {STORAGE_SERVICE_USER_HOME_DIR}
    find {SERVICE_USER_HOME_MOUNT_POINT} -mindepth 1 -delete
    chown root:root {SERVICE_USER_HOME_MOUNT_POINT}
    chmod 755 {SERVICE_USER_HOME_MOUNT_POINT}
fi
is_units_changed=0
{unit_installs}\
install -d -m 755 {flush_drop_in_dir}
{_render_converged_root_file(render_journal_flush_drop_in(), JOURNAL_FLUSH_DROP_IN_PATH, "644", "MNGR_STORAGE_FLUSH_DROP_IN")}\
if [ "$is_units_changed" = 1 ]; then
    systemctl daemon-reload
fi
systemctl enable {unit_names}
# A mount point already held by something other than the volume (the distro's
# tmpfs on /tmp) leaves a plain `start` a no-op; only a restart swaps it out.
{foreign_mount_restarts}\
systemctl start {unit_names}
journalctl --flush
"""


@pure
def render_storage_passphrase_staging_script() -> str:
    """The root bash that writes the recovery passphrase read from stdin to the tmpfs staging path (0600)."""
    return f"""\
umask 077
install -d -m 700 {STORAGE_STAGING_DIR}
cat > {STORAGE_PASSPHRASE_STAGING_PATH}
"""


@pure
def render_storage_passphrase_cleanup_script() -> str:
    """The root bash that removes the staging directory with any passphrase and header backup in it (the CLI's best-effort cleanup)."""
    return f"rm -rf {STORAGE_STAGING_DIR}\n"


@pure
def render_storage_header_backup_fetch_script() -> str:
    """The root bash that prints the staged LUKS header backup as base64 and deletes it.

    Strict mode so a missing backup fails the fetch with the box's stderr
    rather than exiting 0 on the trailing ``rm`` with nothing printed.
    """
    return f"""\
set -euo pipefail
base64 -w0 {STORAGE_HEADER_BACKUP_STAGING_PATH}
rm -f {STORAGE_HEADER_BACKUP_STAGING_PATH}
"""


@pure
def render_storage_unlock_script() -> str:
    """The root bash behind ``minds-admin server unlock``: open the locked volume by passphrase and bring the box back.

    Reads the recovery passphrase from stdin (never argv), opens the crypttab
    volume with it, mounts the storage root and the four bind mounts, turns
    the swapfile back on, flushes the journal onto the volume, and starts
    every slice unit that is enabled for boot (systemd left them down: their
    ``RequiresMountsFor`` on the storage root failed at boot). Idempotent: an
    already-open volume skips the open, an already-mounted root the mount.
    """
    unit_names = " ".join(mount_unit_name(bind_mount.mount_point) for bind_mount in STORAGE_BIND_MOUNTS)
    return f"""\
set -euo pipefail
STORAGE_ROOT={GEN2_STORAGE_ROOT}
STORAGE_MAPPER={GEN2_STORAGE_LUKS_MAPPER_NAME}
STORAGE_MAPPER_PATH={GEN2_STORAGE_LUKS_MAPPER_PATH}
IFS= read -r storage_passphrase
if [ ! -e "$STORAGE_MAPPER_PATH" ]; then
    crypttab_uuid=$(awk -v n="$STORAGE_MAPPER" '$1 == n {{print $2}}' {STORAGE_CRYPTTAB_PATH})
    if [ -z "$crypttab_uuid" ]; then
        echo "ERROR: {STORAGE_CRYPTTAB_PATH} has no $STORAGE_MAPPER entry; this box's storage is not a LUKS volume" >&2
        exit 1
    fi
    locked_device=$(blkid -U "${{crypttab_uuid#UUID=}}")
    printf '%s' "$storage_passphrase" | cryptsetup open --key-file - "$locked_device" "$STORAGE_MAPPER"
fi
if ! mountpoint -q "$STORAGE_ROOT"; then
    mount "$STORAGE_ROOT"
fi
systemctl start {unit_names}
swapon -a
journalctl --flush
# list-unit-files exits 1 when nothing matches (a box with no slices yet).
enabled_slice_units=$(systemctl list-unit-files 'mngr-slice@*.service' --state=enabled --no-legend || true)
enabled_slice_units=$(printf '%s\\n' "$enabled_slice_units" | awk '{{print $1}}')
if [ -n "$enabled_slice_units" ]; then
    systemctl start $enabled_slice_units
fi
echo {STORAGE_UNLOCKED_MARKER}
"""


@pure
def parse_storage_encryption_from_prep_output(stdout: str) -> tuple[str, str] | None:
    """Extract ``(luks_uuid, backing_device)`` from a gen-2 prep run's ``MNGR_STORAGE_ENCRYPTION`` marker line."""
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith(STORAGE_ENCRYPTION_MARKER):
            parts = stripped.split()
            if len(parts) == 3:
                return parts[1], parts[2]
    return None


@pure
def storage_header_backup_object_key(key_prefix: str, ovh_service_name: str, luks_uuid: str) -> str:
    """The tier-bucket key the box's LUKS header backup lands at (under the env's key prefix)."""
    return f"{key_prefix}boxes/{ovh_service_name}/luks-header-{luks_uuid}.img"
