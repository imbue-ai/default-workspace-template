import subprocess

from inline_snapshot import snapshot

from imbue.minds_admin.slices.storage_encryption import JOURNAL_FLUSH_DROP_IN_PATH
from imbue.minds_admin.slices.storage_encryption import STORAGE_BIND_MOUNTS
from imbue.minds_admin.slices.storage_encryption import STORAGE_BIND_MOUNT_UNIT_PATHS
from imbue.minds_admin.slices.storage_encryption import STORAGE_HEADER_BACKUP_STAGING_PATH
from imbue.minds_admin.slices.storage_encryption import STORAGE_PASSPHRASE_STAGING_PATH
from imbue.minds_admin.slices.storage_encryption import STORAGE_STAGING_DIR
from imbue.minds_admin.slices.storage_encryption import StorageBindMount
from imbue.minds_admin.slices.storage_encryption import mount_unit_name
from imbue.minds_admin.slices.storage_encryption import parse_storage_encryption_from_prep_output
from imbue.minds_admin.slices.storage_encryption import render_gen2_storage_encryption_section
from imbue.minds_admin.slices.storage_encryption import render_gen2_storage_relocation_section
from imbue.minds_admin.slices.storage_encryption import render_journal_flush_drop_in
from imbue.minds_admin.slices.storage_encryption import render_storage_bind_mount_unit
from imbue.minds_admin.slices.storage_encryption import render_storage_header_backup_fetch_script
from imbue.minds_admin.slices.storage_encryption import render_storage_passphrase_cleanup_script
from imbue.minds_admin.slices.storage_encryption import render_storage_passphrase_staging_script
from imbue.minds_admin.slices.storage_encryption import render_storage_unlock_script
from imbue.minds_admin.slices.storage_encryption import storage_header_backup_object_key


def _assert_bash_syntax_ok(script: str) -> None:
    result = subprocess.run(["bash", "-n"], input=script, capture_output=True, text=True)
    assert result.returncode == 0, f"bash -n failed: {result.stderr}"


def test_every_rendered_script_passes_bash_syntax_check() -> None:
    for script in (
        render_gen2_storage_encryption_section(),
        render_gen2_storage_relocation_section(),
        render_storage_unlock_script(),
        render_storage_passphrase_staging_script(),
        render_storage_passphrase_cleanup_script(),
        render_storage_header_backup_fetch_script(),
    ):
        _assert_bash_syntax_ok(script)


def test_encryption_section_refuses_a_plain_partition_that_holds_slices() -> None:
    section = render_gen2_storage_encryption_section()
    refusal_start = section.index('if [ -n "$(ls -A "$STORAGE_ROOT/instances" 2>/dev/null)" ]; then')
    refusal = section[refusal_start : section.index("fi\n", refusal_start)]
    assert "holds slices" in refusal
    assert "Drain the box" in refusal
    assert "exit 1" in refusal
    # There is no in-place conversion: the format happens only after the refusal
    # and only wipes an empty partition.
    assert refusal_start < section.index("wipefs -a")
    assert section.index("wipefs -a") < section.index("cryptsetup luksFormat")


def test_encryption_section_formats_with_the_pinned_luks_parameters_and_persistent_flags() -> None:
    section = render_gen2_storage_encryption_section()
    assert (
        "cryptsetup luksFormat --batch-mode --type luks2 --cipher aes-xts-plain64 --key-size 512 \\\n"
        "        --sector-size 4096 --pbkdf argon2id --label mngr-storage \\\n"
        '        --key-file "$STORAGE_PASSPHRASE_FILE" "$storage_device"'
    ) in section
    # The discard and no-workqueue flags are stored in the header at the first
    # open, so the boot-time TPM open and the manual unlock inherit them.
    assert (
        'cryptsetup open --key-file "$STORAGE_PASSPHRASE_FILE" --allow-discards --perf-no_read_workqueue '
        '--perf-no_write_workqueue --persistent "$storage_device" "$STORAGE_MAPPER"'
    ) in section
    assert 'mkfs.xfs -f -L mngr-storage "$STORAGE_MAPPER_PATH"' in section


def test_encryption_section_verifies_the_staged_passphrase_then_reseals_the_tpm_keyslot() -> None:
    section = render_gen2_storage_encryption_section()
    # A plain partition cannot be formatted without a passphrase to key it.
    assert 'if [ ! -s "$STORAGE_PASSPHRASE_FILE" ]; then\n        echo "ERROR: $STORAGE_ROOT is unencrypted' in section
    # Every other state needs it too: the passphrase is verified against a
    # keyslot before the header is touched, then the TPM keyslot is re-sealed.
    assert 'if [ ! -s "$STORAGE_PASSPHRASE_FILE" ]; then\n    echo "ERROR: no recovery passphrase is staged' in section
    verify_idx = section.index(
        'cryptsetup open --test-passphrase --key-file "$STORAGE_PASSPHRASE_FILE" "$storage_device"'
    )
    # The re-seal is unconditional and replaces the stale tpm2 slots: a token
    # in the header proves nothing after a TPM clear or replacement, so its
    # presence must never gate the enrollment.
    enroll_idx = section.index(
        'systemd-cryptenroll --unlock-key-file="$STORAGE_PASSPHRASE_FILE" --tpm2-device=auto --tpm2-pcrs= '
        '--wipe-slot=tpm2 "$storage_device"'
    )
    assert "luksDump" not in section
    assert section.count('rm -f "$STORAGE_PASSPHRASE_FILE"') == 1
    assert verify_idx < enroll_idx < section.index('rm -f "$STORAGE_PASSPHRASE_FILE"')


def test_encryption_section_converges_crypttab_and_fstab_for_an_unattended_tpm_unlock() -> None:
    section = render_gen2_storage_encryption_section()
    assert (
        'storage_crypttab_line="$STORAGE_MAPPER UUID=$storage_luks_uuid none '
        'luks,discard,tpm2-device=auto,headless=true,nofail"'
    ) in section
    assert 'if ! grep -qxF "$storage_crypttab_line" /etc/crypttab; then' in section
    # The reinstall's plain-partition fstab line gives way to the mapper, nofail
    # so a locked volume never holds the boot.
    assert "awk -v root=\"$STORAGE_ROOT\" '$2 != root' /etc/fstab" in section
    assert (
        'echo "$STORAGE_MAPPER_PATH $STORAGE_ROOT xfs defaults,nofail,x-systemd.device-timeout=90s 0 0" >> /etc/fstab'
    ) in section


def test_encryption_section_opens_a_locked_volume_from_the_staged_passphrase() -> None:
    section = render_gen2_storage_encryption_section()
    locked_branch_start = section.index(
        'elif [ -s "$STORAGE_PASSPHRASE_FILE" ] && grep -q "^$STORAGE_MAPPER " /etc/crypttab'
    )
    locked_branch = section[locked_branch_start : section.index("else\n", locked_branch_start)]
    assert 'locked_device=$(blkid -U "${crypttab_uuid#UUID=}")' in locked_branch
    assert 'cryptsetup open --key-file "$STORAGE_PASSPHRASE_FILE" "$locked_device" "$STORAGE_MAPPER"' in locked_branch


def test_encryption_section_stages_a_header_backup_and_echoes_the_marker_last() -> None:
    section = render_gen2_storage_encryption_section()
    assert (
        'cryptsetup luksHeaderBackup "$storage_device" --header-backup-file "$STORAGE_HEADER_BACKUP_FILE"' in section
    )
    assert f"STORAGE_HEADER_BACKUP_FILE={STORAGE_HEADER_BACKUP_STAGING_PATH}" in section
    assert section.rstrip().endswith('echo "MNGR_STORAGE_ENCRYPTION $storage_luks_uuid $storage_device"')
    assert parse_storage_encryption_from_prep_output(
        "noise\nMNGR_STORAGE_ENCRYPTION 0b3c1d2e-aaaa-bbbb-cccc-000000000000 /dev/md4\nMNGR_BOX_PREP_DONE\n"
    ) == ("0b3c1d2e-aaaa-bbbb-cccc-000000000000", "/dev/md4")
    assert parse_storage_encryption_from_prep_output("MNGR_STORAGE_ENCRYPTION only-one-field\n") is None
    assert parse_storage_encryption_from_prep_output("") is None


def test_relocation_moves_the_journal_home_and_temp_dirs_behind_bind_mount_units() -> None:
    section = render_gen2_storage_relocation_section()
    assert "install -d -m 2755 -o root -g systemd-journal /srv/mngr-slices/system/journal" in section
    assert "install -d -m 700 -o slicehost -g slicehost /srv/mngr-slices/system/home/slicehost" in section
    assert "install -d -m 1777 /srv/mngr-slices/system/tmp /srv/mngr-slices/system/var-tmp" in section
    # The journal is relinquished to RAM around the swap and flushed onto the volume after.
    assert section.index("journalctl --relinquish-var") < section.index("systemctl start")
    assert section.index("systemctl start") < section.index("journalctl --flush")
    # The service user's root-side home becomes a root-owned stub.
    assert "chown root:root /home/slicehost" in section
    assert "chmod 755 /home/slicehost" in section
    # The copy carries the root-side directory's attributes onto the volume-side
    # home (a re-run copies from the root-owned stub), so the service user's
    # ownership is re-asserted there before the root side is turned into the stub.
    copy_idx = section.index("cp -a /home/slicehost/. /srv/mngr-slices/system/home/slicehost/")
    reown_idx = section.index("chown slicehost:slicehost /srv/mngr-slices/system/home/slicehost")
    assert copy_idx < reown_idx < section.index("chmod 700 /srv/mngr-slices/system/home/slicehost")
    assert reown_idx < section.index("find /home/slicehost -mindepth 1 -delete")
    for unit_path in STORAGE_BIND_MOUNT_UNIT_PATHS:
        assert unit_path in section
    assert JOURNAL_FLUSH_DROP_IN_PATH in section
    assert "systemctl enable var-log-journal.mount home-slicehost.mount tmp.mount var-tmp.mount" in section
    # Debian's tmpfs tmp.mount is already active, so the override unit must be
    # restarted (a plain start is a no-op) to put /tmp onto the volume.
    restart_idx = section.index(
        "if mountpoint -q /tmp && [ \"$(findmnt -no SOURCE /tmp | cut -d'[' -f1)\" != /dev/mapper/mngr-storage ]; then\n"
        "    systemctl restart tmp.mount\n"
    )
    assert section.index("systemctl enable") < restart_idx < section.index("systemctl start")


def test_bind_mount_units_are_gated_on_the_volume_and_skip_cleanly_when_locked() -> None:
    journal_bind = StorageBindMount(
        source_dir="/srv/mngr-slices/system/journal", mount_point="/var/log/journal", mount_options="bind"
    )
    assert render_storage_bind_mount_unit(journal_bind) == snapshot("""\
[Unit]
Description=mngr: /var/log/journal on the encrypted storage volume
RequiresMountsFor=/srv/mngr-slices
ConditionPathIsDirectory=/srv/mngr-slices/system/journal

[Mount]
What=/srv/mngr-slices/system/journal
Where=/var/log/journal
Type=none
Options=bind

[Install]
WantedBy=local-fs.target
""")
    assert render_journal_flush_drop_in() == snapshot("""\
[Unit]
ConditionPathIsMountPoint=/var/log/journal
""")


def test_temp_dir_binds_keep_the_distro_tmpfs_nosuid_nodev_while_journal_and_home_do_not() -> None:
    options_by_mount_point = {bind_mount.mount_point: bind_mount.mount_options for bind_mount in STORAGE_BIND_MOUNTS}
    assert options_by_mount_point == {
        "/var/log/journal": "bind",
        "/home/slicehost": "bind",
        "/tmp": "bind,nosuid,nodev",
        "/var/tmp": "bind,nosuid,nodev",
    }
    tmp_bind = next(bind_mount for bind_mount in STORAGE_BIND_MOUNTS if bind_mount.mount_point == "/tmp")
    assert "Options=bind,nosuid,nodev\n" in render_storage_bind_mount_unit(tmp_bind)


def test_mount_unit_names_follow_systemd_path_escaping() -> None:
    assert [mount_unit_name(bind_mount.mount_point) for bind_mount in STORAGE_BIND_MOUNTS] == [
        "var-log-journal.mount",
        "home-slicehost.mount",
        "tmp.mount",
        "var-tmp.mount",
    ]
    assert mount_unit_name("/srv/mngr-slices") == "srv-mngr\\x2dslices.mount"


def test_unlock_script_reads_the_passphrase_from_stdin_and_brings_the_box_back() -> None:
    script = render_storage_unlock_script()
    assert "IFS= read -r storage_passphrase" in script
    # The passphrase reaches cryptsetup through a pipe, never argv.
    assert (
        'printf \'%s\' "$storage_passphrase" | cryptsetup open --key-file - "$locked_device" "$STORAGE_MAPPER"'
    ) in script
    assert "systemctl start var-log-journal.mount home-slicehost.mount tmp.mount var-tmp.mount" in script
    assert "swapon -a" in script
    # A box with no slice units yet must still unlock: list-unit-files exits 1
    # on no match, which pipefail would otherwise turn into a failed unlock.
    assert "systemctl list-unit-files 'mngr-slice@*.service' --state=enabled --no-legend || true" in script
    assert script.rstrip().endswith("echo MNGR_STORAGE_UNLOCKED")


def test_staging_and_fetch_scripts_use_a_root_only_tmpfs_directory() -> None:
    # The staging directory is created 0700 under /run (tmpfs that only root can
    # create entries in), never in the world-writable /dev/shm where a local
    # user could pre-create the fixed paths.
    assert STORAGE_STAGING_DIR == "/run/mngr-storage"
    assert render_storage_passphrase_staging_script() == snapshot("""\
umask 077
install -d -m 700 /run/mngr-storage
cat > /run/mngr-storage/passphrase
""")
    assert render_storage_passphrase_cleanup_script() == snapshot("rm -rf /run/mngr-storage\n")
    encryption_section = render_gen2_storage_encryption_section()
    assert encryption_section.index("install -d -m 700 /run/mngr-storage") < encryption_section.index(
        "cryptsetup luksHeaderBackup"
    )
    fetch = render_storage_header_backup_fetch_script()
    # A missing backup must fail the fetch, not exit 0 on the trailing rm.
    assert fetch.startswith("set -euo pipefail\n")
    assert fetch.index(f"base64 -w0 {STORAGE_HEADER_BACKUP_STAGING_PATH}") < fetch.index(
        f"rm -f {STORAGE_HEADER_BACKUP_STAGING_PATH}"
    )
    assert STORAGE_PASSPHRASE_STAGING_PATH.startswith(f"{STORAGE_STAGING_DIR}/")
    assert STORAGE_HEADER_BACKUP_STAGING_PATH.startswith(f"{STORAGE_STAGING_DIR}/")


def test_header_backup_object_key_is_under_the_env_prefix_by_box_and_volume() -> None:
    assert storage_header_backup_object_key("dev-josh/", "ns1006991.ip-135-148-34.us", "0b3c") == (
        "dev-josh/boxes/ns1006991.ip-135-148-34.us/luks-header-0b3c.img"
    )
    assert storage_header_backup_object_key("", "ns1", "uuid") == "boxes/ns1/luks-header-uuid.img"
