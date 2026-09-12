import json
import os
import posixpath
import subprocess
from pathlib import Path

from imbue.minds.config.data_types import WireguardOperatorConfig
from imbue.minds.config.data_types import management_overlay_for_tier
from imbue.minds_admin.slices.bare_metal_prep import DEFAULT_GEN2_SLICE_GUEST_IMAGE_SHA512
from imbue.minds_admin.slices.bare_metal_prep import DEFAULT_GEN2_SLICE_GUEST_IMAGE_URL
from imbue.minds_admin.slices.bare_metal_prep import build_box_prep_script
from imbue.minds_admin.slices.bare_metal_prep import build_gen2_box_prep_script
from imbue.minds_admin.slices.bare_metal_prep import docker_apt_archive_url
from imbue.minds_admin.slices.bare_metal_prep import gen2_guest_image_customization_hash
from imbue.minds_admin.slices.bare_metal_prep import parse_storage_partition_gib_from_prep_output
from imbue.minds_admin.slices.bare_metal_prep import render_docker_apt_source_section
from imbue.minds_admin.slices.docker_apt_signing_key import DOCKER_APT_SIGNING_KEY
from imbue.minds_admin.slices.mirror_artifacts import AGE_TARBALL
from imbue.minds_admin.slices.mirror_artifacts import S5CMD_TARBALL
from imbue.minds_admin.slices.mirror_artifacts import UV_TARBALL
from imbue.minds_admin.slices.mirror_artifacts import UV_VERSION
from imbue.minds_admin.slices.storage_encryption import render_gen2_storage_encryption_section
from imbue.minds_admin.slices.storage_encryption import render_gen2_storage_relocation_section
from imbue.mngr_imbue_cloud.slices.bare_metal import GEN1_SLICE_SERVICE_USER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_DHCP_CONFIG_PATH
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_DHCP_LEASE_DIR
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_DHCP_NFT_POLICY_PATH
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_DHCP_UNIT_NAME
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_DHCP_UNIT_PATH
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_DHCP_USER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_HELPER_PATH
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_MAX_SLICE_COUNT
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_SUDOERS_PATH
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_UNIT_PATH
from imbue.mngr_imbue_cloud.slices.qemu_slice import render_slice_dhcp_config
from imbue.mngr_imbue_cloud.slices.qemu_slice import render_slice_dhcp_nftables_policy
from imbue.mngr_imbue_cloud.slices.qemu_slice import render_slice_dhcp_unit
from imbue.mngr_imbue_cloud.slices.qemu_slice import render_slice_helper_script
from imbue.mngr_imbue_cloud.slices.qemu_slice import render_slice_sudoers
from imbue.mngr_imbue_cloud.slices.qemu_slice import render_slice_unit_file
from imbue.mngr_vps.host_setup import PINNED_CONTAINERD_APT_VERSION_CORE
from imbue.mngr_vps.host_setup import PINNED_DOCKER_APT_VERSION
from imbue.mngr_vps.host_setup import PINNED_DOCKER_APT_VERSION_CORE
from imbue.mngr_vps.host_setup import PINNED_GVISOR_RELEASE

_POOL_PUB = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITESTpoolkey mngr-pool"
_APT_MIRROR_TIMESTAMP = "20260725T000000Z"
# Every host the prep used to download from directly. None may appear in a
# rendered gen-2 prep: each would be a single point of failure for every
# future prep and repave once upstream prunes the pinned release.
_UPSTREAM_HOSTS = ("cloud.debian.org", "download.docker.com", "storage.googleapis.com", "github.com", "astral.sh")
_GEN1_USER = GEN1_SLICE_SERVICE_USER
_IMAGE_URL = (
    "https://cloud.debian.org/images/cloud/bookworm/20260601-2496/debian-12-genericcloud-amd64-20260601-2496.qcow2"
)


def _script() -> str:
    return build_box_prep_script(
        pool_public_key=_POOL_PUB,
        slice_service_user=_GEN1_USER,
        lima_version="2.2.0",
        slice_base_image_url=_IMAGE_URL,
    )


def test_prep_script_stages_base_image_under_lima_user_via_file_path() -> None:
    script = _script()
    # The OS image is fetched once, validated as a real qcow2, and atomically moved
    # into place under the lima user's home (so the bake can boot it via file://
    # with no Debian-mirror dependency). Idempotent: skips if already present.
    assert _IMAGE_URL in script
    assert f"/home/{_GEN1_USER}/.cache/mngr-slice-base/debian-base.qcow2" in script
    assert "qemu-img info" in script
    assert 'if [ ! -f "$img" ]; then' in script


def test_prep_script_chowns_cache_dir_to_lima_user() -> None:
    script = _script()
    # The script runs as root; staging the image under ~/.cache must leave ~/.cache
    # owned by the lima user, or `limactl` (run as that user) cannot create
    # ~/.cache/lima and every VM start fails. The parent cache dir must be chowned,
    # not just the leaf image dir.
    assert 'cache_dir="$(dirname "$image_dir")"' in script
    assert f'chown {_GEN1_USER}:{_GEN1_USER} "$cache_dir" "$image_dir"' in script


def test_prep_script_installs_qemu_and_lima() -> None:
    script = _script()
    assert "qemu-system-x86" in script
    assert "lima-2.2.0-Linux-x86_64.tar.gz" in script
    assert "github.com/lima-vm/lima/releases/download/v2.2.0/" in script


def test_prep_script_never_invokes_limactl_as_root() -> None:
    # The script runs as root; limactl refuses to run as root, so it must only be
    # extracted, never executed, here. The boot-autostart heredoc is excluded:
    # its limactl calls run later as the lima user via the systemd unit's
    # User= directive, not during root prep.
    script = _script()
    assert "tar -C /usr/local" in script
    autostart_start = script.index("<<'MNGR_SLICES_AUTOSTART'")
    autostart_end = script.rindex("MNGR_SLICES_AUTOSTART")
    root_executed_script = script[:autostart_start] + script[autostart_end:]
    assert "limactl --version" not in root_executed_script
    assert "limactl start" not in root_executed_script
    assert "limactl list" not in root_executed_script


def test_prep_script_creates_service_user_with_kvm_and_pool_key() -> None:
    script = _script()
    assert f"useradd -m -s /bin/bash {_GEN1_USER}" in script
    assert f"usermod -aG kvm {_GEN1_USER}" in script
    assert _POOL_PUB in script
    assert f"/home/{_GEN1_USER}/.ssh/authorized_keys" in script


def test_prep_script_is_idempotent_guarded() -> None:
    script = _script()
    # Re-runnable: guards on the recorded lima version and the existing user.
    assert 'if [ "$(cat "$lima_version_marker" 2>/dev/null)" != "2.2.0" ]; then' in script
    assert f"id {_GEN1_USER} >/dev/null 2>&1" in script


def test_prep_script_upgrades_lima_when_installed_version_differs() -> None:
    # The lima install guard compares a marker file against the pinned release
    # (never `limactl --version` -- limactl refuses to run as root), so re-running
    # prep on a box with an older lima (or one prepped before the marker existed,
    # where the cat yields "") re-extracts the tarball and records the new version.
    script = _script()
    assert "lima_version_marker=/usr/local/share/lima/.mngr-installed-lima-version" in script
    marker_write_idx = script.index('printf \'%s\\n\' "2.2.0" > "$lima_version_marker"')
    extract_idx = script.index("tar -C /usr/local -xzf")
    assert extract_idx < marker_write_idx


def test_prep_script_installs_pinned_uv_for_service_user_from_the_mirror() -> None:
    script = _script()
    # The pinned release tarball from the artifact mirror, digest-verified,
    # converged on version (any other uv on the box is replaced), owned by the
    # service user under the ~/.local/bin the box PATH prefix already names.
    assert UV_TARBALL.mirror_url in script
    assert f'echo "{UV_TARBALL.digest}  /tmp/uv.tar.gz" | sha256sum -c -' in script
    assert f"""!= "{UV_VERSION}" ]; then""" in script
    assert f"/home/{_GEN1_USER}/.local" in script
    assert f"install -m 755 -o {_GEN1_USER} -g {_GEN1_USER} /tmp/uv-extract/uv /tmp/uv-extract/uvx" in script
    assert "astral.sh" not in script


def test_prep_script_provisions_swapfile() -> None:
    # Slice hosts run RAM near capacity, so prep adds a real 32GiB swapfile (the
    # OS-install default of two tiny partitions is useless). Idempotent + in fstab.
    script = _script()
    assert "mkswap /swapfile" in script
    assert "swapon /swapfile" in script
    assert "32G" in script
    assert "/swapfile none swap sw 0 0" in script


def test_prep_script_retires_per_disk_swap_partitions() -> None:
    # The OS-install per-disk swap partitions sit OUTSIDE the md RAID mirrors, so a
    # single disk death loses their swapped-out pages and slowly SIGBUS-kills every
    # process on the box (the 2026-08-07 production nvme incident). Prep must turn
    # them off, drop them from fstab (keeping the mirrored swapfile's own line),
    # and wipe their swap signatures so nothing re-activates them at boot.
    script = _script()
    assert 'swapoff "$swap_partition"' in script
    assert 'awk \'!($3 == "swap" && $1 != "/swapfile")\' /etc/fstab' in script
    assert 'wipefs -a "$swap_partition"' in script
    # The partition swapoff loop only ever targets block devices, never the
    # swapfile it just enabled.
    assert "grep '^/dev/'" in script


def test_prep_script_fstab_filter_drops_partition_swap_and_keeps_everything_else(tmp_path: Path) -> None:
    # Execute the script's own fstab-filtering awk (extracted verbatim, not
    # re-typed) against a realistic OVH fstab: the two UUID swap-partition lines
    # must go, while the root/boot mounts, comments, and the mirrored swapfile
    # line all survive byte-for-byte.
    script = _script()
    awk_line = next(line for line in script.splitlines() if line.startswith("awk "))
    awk_command = awk_line.split(" /etc/fstab")[0]
    fstab = tmp_path / "fstab"
    kept_lines = [
        "# /etc/fstab: static file system information.",
        "UUID=aaaa-root\t/\text4\terrors=remount-ro\t0\t1",
        "UUID=bbbb-boot\t/boot\text4\tdefaults\t0\t2",
        "/swapfile none swap sw 0 0",
    ]
    dropped_lines = [
        "UUID=cccc-swap0\tswap\tswap\tdefaults\t0\t0",
        "UUID=dddd-swap1\tswap\tswap\tdefaults\t0\t0",
    ]
    fstab.write_text("\n".join(kept_lines[:3] + dropped_lines + kept_lines[3:]) + "\n")
    result = subprocess.run(
        ["bash", "-c", f"{awk_command} {fstab}"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "\n".join(kept_lines) + "\n"


def test_prep_script_retires_swap_partitions_after_enabling_the_swapfile() -> None:
    # Order matters: the mirrored swapfile must be active before the partitions
    # are swapped off, so their in-use pages migrate somewhere durable instead of
    # competing for RAM on a box running near capacity.
    script = _script()
    assert script.index("swapon /swapfile") < script.index('swapoff "$swap_partition"')


def test_prep_script_pins_unattended_upgrades_to_never_auto_reboot() -> None:
    # Slice boxes host user workspaces; an unattended-upgrades auto-reboot would
    # take every slice on the box down unannounced (and nothing restarts them).
    # The Debian default is already "false", but prep pins it explicitly so config
    # drift cannot flip it.
    script = _script()
    assert "/etc/apt/apt.conf.d/99mngr-no-auto-reboot" in script
    assert 'Unattended-Upgrade::Automatic-Reboot "false";' in script


def test_prep_script_installs_boot_autostart_for_slice_vms() -> None:
    # After a box reboot nothing else restarts the lima slice VMs (no linger, no
    # lima boot integration), so prep installs a boot unit that starts them all.
    # It must run as the lima user (lima refuses root) and be enabled, not
    # started: prep runs on live boxes where the VMs are already up or were
    # stopped on purpose.
    script = _script()
    assert "/usr/local/sbin/mngr-slices-autostart.sh" in script
    assert "/etc/systemd/system/mngr-slices-autostart.service" in script
    assert f"User={_GEN1_USER}" in script
    assert "WantedBy=multi-user.target" in script
    assert "systemctl enable mngr-slices-autostart.service" in script
    assert "systemctl start mngr-slices-autostart" not in script


def test_prep_script_autostart_targets_only_stopped_slice_instances() -> None:
    # The boot script must start every stopped slice VM (leased or available)
    # and nothing else: not running instances (re-running the unit must be a
    # no-op) and not non-slice VMs someone parked on the box.
    script = _script()
    assert "limactl list --format '{{.Name}} {{.Status}}'" in script
    assert 'awk -v prefix="mngr-slice-" \'index($1, prefix) == 1 && $2 == "Stopped" {print $1}\'' in script


def test_prep_script_autostart_bounds_parallelism_and_retries() -> None:
    # A full box cold-booting 14 QEMU VMs at once is a boot storm, and fully
    # serial keeps users down for ~10 minutes, so starts are capped at a small
    # concurrency. Each instance gets one retry; a VM that still fails must fail
    # the unit (xargs propagates the failure) so the breakage is visible in
    # systemd rather than swallowed.
    script = _script()
    assert "xargs -n1 -P 4" in script
    assert "retrying" in script
    autostart_body = script[script.index("MNGR_SLICES_AUTOSTART") : script.rindex("MNGR_SLICES_AUTOSTART")]
    assert "|| true" not in autostart_body
    # A failing `limactl list` must fail the unit too, not read as "no VMs to
    # start": errexit+pipefail with no stderr suppression on the listing.
    assert "set -euo pipefail" in autostart_body
    assert "2>/dev/null" not in autostart_body


def test_prep_script_installs_libguestfs_for_image_customization() -> None:
    # virt-customize (from libguestfs-tools) is how we pre-install Docker + inotify
    # into the golden image; it must be among the box apt packages.
    script = _script()
    assert "libguestfs-tools" in script


def test_prep_script_preinstalls_pinned_docker_and_inotify_into_golden_image() -> None:
    script = _script()
    # The image is customized offline with virt-customize over the network, running an
    # in-guest script that installs the SAME pinned Docker the OVH path pins, plus
    # inotify-tools -- so each slice VM's first-boot guards (presence-only) skip them.
    assert "virt-customize -a" in script
    assert "--network" in script
    assert "--run /tmp/mngr-slice-image-customize.sh" in script
    assert f'docker-ce="{PINNED_DOCKER_APT_VERSION}"' in script
    assert "download.docker.com/linux/debian" in script
    assert "inotify-tools" in script


def test_prep_script_customizes_before_atomic_publish() -> None:
    # The customize must run on the temp copy and only move it into place on success,
    # so a partial/failed customize never becomes the staged base image.
    script = _script()
    customize_idx = script.index("virt-customize -a")
    publish_idx = script.index('mv "$img.tmp" "$img"')
    assert customize_idx < publish_idx
    # The finished image is chowned to the lima user that limactl reads it as.
    assert f'chown {_GEN1_USER}:{_GEN1_USER} "$img.tmp"' in script


def _extract_pin_script_body(tmp_path: Path, hosts_file: Path) -> Path:
    """Extract the S3 IPv4 pin heredoc verbatim, retargeted at a test hosts file."""
    script = _script()
    body = script.split("<<'MNGR_S3_PIN'\n")[1].split("\nMNGR_S3_PIN\n")[0]
    retargeted = body.replace("hosts_file=/etc/hosts", f"hosts_file={hosts_file}")
    pin_script = tmp_path / "pin.sh"
    pin_script.write_text(retargeted)
    return pin_script


def _write_dig_stub(tmp_path: Path, stub_body: str) -> dict[str, str]:
    """Install a fake ``dig`` on PATH and return the env to run the pin script with."""
    stub_bin = tmp_path / "stubbin"
    stub_bin.mkdir(exist_ok=True)
    dig = stub_bin / "dig"
    dig.write_text(f"#!/bin/bash\n{stub_body}\n")
    dig.chmod(0o755)
    return {"PATH": f"{stub_bin}:{os.environ['PATH']}"}


_RESOLVING_DIG_STUB = """\
case "${!#}" in
  s3.us-east-va.io.cloud.ovh.us) echo "51.81.92.24";;
  s3.us-west-or.io.cloud.ovh.us) echo "147.135.33.112";;
esac"""

_UNRELATED_HOSTS_CONTENT = "127.0.0.1 localhost\n::1 localhost\n10.0.0.5 mybox\n"


def _hosts_content_with_pin_block(east_address: str, west_address: str) -> str:
    """Hosts-file content with unrelated lines plus a managed pin block holding the given addresses."""
    return (
        _UNRELATED_HOSTS_CONTENT + "# BEGIN mngr-s3-ipv4-pin (managed block, do not edit)\n"
        f"{east_address} s3.us-east-va.io.cloud.ovh.us\n"
        f"{west_address} s3.us-west-or.io.cloud.ovh.us\n"
        "# END mngr-s3-ipv4-pin\n"
    )


def _run_pin_script(pin_script: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run the extracted pin script, assert it succeeded, and return the completed process."""
    result = subprocess.run(["bash", str(pin_script)], capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stderr
    return result


def test_prep_script_installs_s3_ipv4_pin_refresher_with_minutely_timer() -> None:
    # Box->S3 transfers must ride IPv4 (the in-DC IPv6 path blackholes flows, OVH
    # ticket #723301): prep installs a hosts-file pin refresher, runs it once
    # synchronously, and keeps it fresh with a 1-minute systemd timer so a changed
    # VIP heals within a minute. dig comes from the box apt packages because the
    # refresher must query DNS directly, not through the hosts file it manages.
    script = _script()
    assert "bind9-dnsutils" in script
    assert "/usr/local/sbin/mngr-s3-ipv4-pin.sh" in script
    assert "/etc/systemd/system/mngr-s3-ipv4-pin.service" in script
    assert "/etc/systemd/system/mngr-s3-ipv4-pin.timer" in script
    assert "OnUnitActiveSec=1min" in script
    assert "systemctl enable --now mngr-s3-ipv4-pin.timer" in script
    # The seed run happens after the units land, so the first transfer after prep
    # already rides IPv4 even before the timer's first tick.
    assert script.index("systemctl enable --now mngr-s3-ipv4-pin.timer") < script.rindex(
        "/usr/local/sbin/mngr-s3-ipv4-pin.sh"
    )


def test_s3_ipv4_pin_script_writes_managed_block_and_preserves_other_lines(tmp_path: Path) -> None:
    # The pin script (extracted verbatim from the prep heredoc) must append a
    # managed block with one IPv4 line per endpoint and leave every unrelated
    # hosts line untouched, and a re-run must not modify the file again.
    hosts_file = tmp_path / "hosts"
    hosts_file.write_text(_UNRELATED_HOSTS_CONTENT)
    pin_script = _extract_pin_script_body(tmp_path, hosts_file)
    env = _write_dig_stub(tmp_path, _RESOLVING_DIG_STUB)

    _run_pin_script(pin_script, env)

    content = hosts_file.read_text()
    assert content.startswith(_UNRELATED_HOSTS_CONTENT)
    assert "51.81.92.24 s3.us-east-va.io.cloud.ovh.us" in content
    assert "147.135.33.112 s3.us-west-or.io.cloud.ovh.us" in content
    assert "# BEGIN mngr-s3-ipv4-pin" in content
    assert content.rstrip().endswith("# END mngr-s3-ipv4-pin")

    # Second run: byte-for-byte identical (the atomic rewrite is skipped).
    before_second_run = hosts_file.read_text()
    _run_pin_script(pin_script, env)
    assert hosts_file.read_text() == before_second_run


def test_s3_ipv4_pin_script_keeps_existing_pin_when_resolution_fails(tmp_path: Path) -> None:
    # A transient DNS outage must never drop a working pin: with dig failing, the
    # script re-reads each endpoint's address from the existing managed block and
    # succeeds, leaving the file unchanged -- but warns on stderr so a persistent
    # failure is visible in the journal.
    hosts_file = tmp_path / "hosts"
    hosts_file.write_text(_hosts_content_with_pin_block("51.81.92.24", "147.135.33.112"))
    before = hosts_file.read_text()
    pin_script = _extract_pin_script_body(tmp_path, hosts_file)
    env = _write_dig_stub(tmp_path, "exit 9")

    result = _run_pin_script(pin_script, env)

    assert hosts_file.read_text() == before
    assert "failed to resolve s3.us-east-va.io.cloud.ovh.us" in result.stderr
    assert "failed to resolve s3.us-west-or.io.cloud.ovh.us" in result.stderr


def test_s3_ipv4_pin_script_updates_pin_when_vip_address_changes(tmp_path: Path) -> None:
    # A VIP address change must propagate: an existing block holding a stale
    # address is rewritten with the freshly resolved one.
    hosts_file = tmp_path / "hosts"
    hosts_file.write_text(_hosts_content_with_pin_block("192.0.2.99", "192.0.2.98"))
    pin_script = _extract_pin_script_body(tmp_path, hosts_file)
    env = _write_dig_stub(tmp_path, _RESOLVING_DIG_STUB)

    _run_pin_script(pin_script, env)

    content = hosts_file.read_text()
    assert "192.0.2.99" not in content
    assert "51.81.92.24 s3.us-east-va.io.cloud.ovh.us" in content
    assert content.count("# BEGIN mngr-s3-ipv4-pin") == 1


def test_s3_ipv4_pin_script_merges_fresh_resolution_with_fallback_for_failed_lookup(tmp_path: Path) -> None:
    # Partial DNS outage: one endpoint resolves to a new address while the other
    # lookup fails. The single rewrite must merge both mechanisms -- the fresh
    # address for the resolved endpoint, the existing pin for the failed one --
    # and warn only about the failed lookup.
    hosts_file = tmp_path / "hosts"
    hosts_file.write_text(_hosts_content_with_pin_block("192.0.2.99", "147.135.33.112"))
    pin_script = _extract_pin_script_body(tmp_path, hosts_file)
    east_only_dig_stub = """\
case "${!#}" in
  s3.us-east-va.io.cloud.ovh.us) echo "198.51.100.7";;
  *) exit 9;;
esac"""
    env = _write_dig_stub(tmp_path, east_only_dig_stub)

    result = _run_pin_script(pin_script, env)

    content = hosts_file.read_text()
    assert "198.51.100.7 s3.us-east-va.io.cloud.ovh.us" in content
    assert "192.0.2.99" not in content
    assert "147.135.33.112 s3.us-west-or.io.cloud.ovh.us" in content
    assert content.count("# BEGIN mngr-s3-ipv4-pin") == 1
    assert "failed to resolve s3.us-west-or.io.cloud.ovh.us" in result.stderr
    assert "failed to resolve s3.us-east-va.io.cloud.ovh.us" not in result.stderr


def test_box_prep_installs_transfer_tooling_and_skips_stop_marked_vms() -> None:
    script = build_box_prep_script(
        slice_service_user="slicehost",
        lima_version="2.2.0",
        pool_public_key="ssh-ed25519 AAAA poolkey",
        slice_base_image_url="https://example.com/debian.qcow2",
    )
    # age + s5cmd land in /usr/local/bin, version-marker-guarded like limactl,
    # fetched from the artifact mirror and verified against their digests.
    assert "/usr/local/bin/age" in script
    assert "/usr/local/bin/age-keygen" in script
    assert "/usr/local/bin/s5cmd" in script
    assert ".mngr-installed-transfer-tools" in script
    assert AGE_TARBALL.mirror_url in script
    assert S5CMD_TARBALL.mirror_url in script
    assert f'echo "{AGE_TARBALL.digest}  /tmp/age.tar.gz" | sha256sum -c -' in script
    assert f'echo "{S5CMD_TARBALL.digest}  /tmp/s5cmd.tar.gz" | sha256sum -c -' in script
    transfer_section = script[script.index("transfer_tools_marker=") :]
    transfer_section = transfer_section[: transfer_section.index("\nfi\n")]
    assert "github.com" not in transfer_section
    # zstd (the transfer stream compressor) comes from apt with the box packages.
    assert "zstd" in script
    # The boot autostart must never resurrect a VM a workspace stop halted.
    assert "mngr-stop-requested" in script


_SSH_CA = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFAKECAFAKECAFAKECAFAKECAFAKECAFAKECAFAKE minds-dev-ssh-ca"


def _gen2_script(proxy_ips: tuple[str, ...] = ("203.0.113.10",)) -> str:
    return build_gen2_box_prep_script(
        ssh_ca_public_key=_SSH_CA,
        slice_base_image_url=DEFAULT_GEN2_SLICE_GUEST_IMAGE_URL,
        slice_base_image_sha512=DEFAULT_GEN2_SLICE_GUEST_IMAGE_SHA512,
        apt_mirror_snapshot_timestamp=_APT_MIRROR_TIMESTAMP,
        wireguard_address="10.112.1.1",
        wireguard_listen_port=51820,
        wireguard_operators=(
            WireguardOperatorConfig.model_validate(
                {"name": "josh", "public_key": "opkeyjosh=", "address": "10.112.0.2"}
            ),
        ),
        overlay=management_overlay_for_tier("dev"),
        management_proxy_static_ips=proxy_ips,
        declared_uplink_mbps=1000,
    )


def _gen2_customize_script(script: str) -> str:
    """The in-guest customization block a rendered gen-2 prep hands to virt-customize."""
    return script.split("<<'MNGR_SLICE_CUSTOMIZE'")[1].split("MNGR_SLICE_CUSTOMIZE")[0]


def test_gen2_prep_script_passes_bash_syntax_check() -> None:
    for script in (_gen2_script(), _gen2_script(proxy_ips=())):
        result = subprocess.run(["bash", "-n"], input=script, capture_output=True, text=True)
        assert result.returncode == 0, f"bash -n failed: {result.stderr}"


def test_gen2_prep_script_installs_the_qemu_management_stack_without_lima() -> None:
    script = _gen2_script()
    for package in ("qemu-system-x86", "ovmf", "nftables", "genisoimage", "wireguard-tools", "xfsprogs"):
        assert package in script
    # No lima anywhere: gen-2 slices are raw qemu under systemd.
    assert "limactl" not in script
    assert "lima-vm/lima/releases" not in script
    # The unit's pinned OVMF pflash path (trixie ships only the _4M builds)
    # is verified post-install so a packaging move fails the prep loudly.
    assert "/usr/share/OVMF/OVMF_CODE_4M.fd" in script


def test_gen2_prep_script_creates_the_service_user_and_all_slice_users() -> None:
    script = _gen2_script()
    assert "useradd -m -s /bin/bash slicehost" in script
    assert "usermod -aG kvm slicehost" in script
    # All 64 per-slice users are pre-created (no home, no shell, kvm group) so
    # User=mngr-slice-%i always resolves.
    assert f"for ordinal in $(seq 0 {GEN2_MAX_SLICE_COUNT - 1})" in script
    assert 'useradd -M -s /usr/sbin/nologin "$slice_user"' in script
    assert 'usermod -aG kvm "$slice_user"' in script


def test_gen2_prep_script_retires_the_pre_rename_service_user_after_re_owning_the_slice_tree() -> None:
    script = _gen2_script()
    retire_block_start = script.index(f"if id {_GEN1_USER} >/dev/null 2>&1; then")
    retire_block = script[retire_block_start : script.index("fi\n", retire_block_start)]
    # Owner first, then group (the setgid run/ dirs and the per-VM disk media
    # carry the old user only as their group), then the user itself goes away.
    assert f"chown -R --from={_GEN1_USER} slicehost /srv/mngr-slices" in retire_block
    assert f"chown -R --from=:{_GEN1_USER} :slicehost /srv/mngr-slices" in retire_block
    assert f"userdel -r {_GEN1_USER}" in retire_block
    assert retire_block.index("chown -R --from=") < retire_block.index("userdel -r")
    # The block runs once the new service user and the storage tree exist, and
    # before the prep artifacts (rendered for the new user) are installed.
    assert script.index("useradd -m -s /bin/bash slicehost") < retire_block_start
    assert script.index("install -d -o slicehost -g slicehost -m 751 /srv/mngr-slices/instances") < retire_block_start
    assert retire_block_start < script.index(GEN2_SUDOERS_PATH)


def test_gen2_prep_script_encrypts_the_storage_partition_before_using_it() -> None:
    script = _gen2_script()
    assert render_gen2_storage_encryption_section() in script
    assert render_gen2_storage_relocation_section() in script
    # The volume is made before the XFS check (which checks the mapper's
    # filesystem), the storage tree, the transfer tooling (which downloads to
    # the relocated /tmp) and every other consumer of the storage root.
    encryption_idx = script.index("cryptsetup luksFormat")
    assert encryption_idx < script.index('if [ "$storage_fstype" != "xfs" ]')
    assert encryption_idx < script.index("install -d -o slicehost -g slicehost -m 751 /srv/mngr-slices/instances")
    assert encryption_idx < script.index("transfer_tools_marker=")
    assert encryption_idx < script.index("MNGR_GEN2_UNIT")
    # ... but after the service user exists (its home moves onto the volume)
    # and its certificate trust is installed.
    assert script.index("useradd -m -s /bin/bash slicehost") < encryption_idx
    assert script.index("TrustedUserCAKeys") < encryption_idx
    # The relocation follows the mount and precedes everything that writes to
    # the service user's home or /tmp.
    relocation_idx = script.index("journalctl --relinquish-var")
    assert encryption_idx < relocation_idx < script.index("transfer_tools_marker=")
    for package in ("cryptsetup", "systemd-cryptsetup", "tpm2-tools"):
        assert package in script


def test_gen2_prep_script_refuses_without_the_xfs_storage_partition() -> None:
    script = _gen2_script()
    # Nothing mounted at the storage root, no opened mapper and no locked
    # volume to open: the box has no storage partition at all.
    refusal_start = script.index('echo "ERROR: $STORAGE_ROOT is not a mounted filesystem; provision the XFS storage')
    assert script[refusal_start : script.index("fi\n", refusal_start)].rstrip().endswith("exit 1")
    assert 'if [ "$storage_fstype" != "xfs" ]' in script
    # The storage tree is owned by the service user, traversable-not-listable.
    assert (
        "install -d -o slicehost -g slicehost -m 751 /srv/mngr-slices/instances /srv/mngr-slices/by-ordinal /srv/mngr-slices/base"
        in script
    )


def test_gen2_prep_script_installs_the_rendered_prep_artifacts_with_convergence() -> None:
    script = _gen2_script()
    # Every plugin-rendered artifact lands at its pinned path, compared
    # byte-for-byte so an unchanged re-prep touches nothing.
    assert GEN2_UNIT_PATH in script
    assert GEN2_HELPER_PATH in script
    assert GEN2_SUDOERS_PATH in script
    assert GEN2_DHCP_CONFIG_PATH in script
    assert GEN2_DHCP_UNIT_PATH in script
    assert GEN2_DHCP_NFT_POLICY_PATH in script
    assert render_slice_unit_file() in script
    assert render_slice_helper_script() in script
    assert render_slice_sudoers() in script
    assert render_slice_dhcp_config() in script
    assert render_slice_dhcp_unit() in script
    assert render_slice_dhcp_nftables_policy() in script
    assert script.count("cmp -s") >= 6
    # A changed unit is followed by a daemon-reload; sudoers is validated
    # before install (a broken sudoers would lock every sudo on the box).
    assert "systemctl daemon-reload" in script
    assert "visudo -cf" in script


def test_gen2_prep_script_runs_the_slice_dhcp_server_and_restarts_it_only_on_change() -> None:
    script = _gen2_script()
    # The bare dnsmasq binary, never the distro service (which would serve DNS
    # on every interface with the package defaults).
    installed_packages = {
        package for line in script.splitlines() if line.startswith("apt-get install") for package in line.split()[2:]
    }
    assert "dnsmasq-base" in installed_packages
    assert "dnsmasq" not in installed_packages
    # The rendered config is syntax-checked before it can replace the live one,
    # the lease dir exists (owned by the service user the unit runs as) before
    # the first start, and the unit is enabled for boot (the slice units Want it).
    assert "dnsmasq --test --conf-file=/tmp/mngr-slice-dhcp.conf.mngr-tmp" in script
    assert f"install -d -m 755 {posixpath.dirname(GEN2_DHCP_CONFIG_PATH)}" in script
    assert f"install -d -m 755 -o {GEN2_DHCP_USER} -g {GEN2_DHCP_USER} {GEN2_DHCP_LEASE_DIR}" in script
    assert f"systemctl enable {GEN2_DHCP_UNIT_NAME}" in script
    # A re-prep with an unchanged config and unit only makes sure the server is
    # running; a change restarts it.
    assert f"systemctl restart {GEN2_DHCP_UNIT_NAME}" in script
    assert f"systemctl start {GEN2_DHCP_UNIT_NAME}" in script
    assert script.index("is_dhcp_changed=1") < script.index(f"systemctl restart {GEN2_DHCP_UNIT_NAME}")


def test_gen2_prep_script_runs_the_slice_dhcp_server_unprivileged_behind_a_tap_only_udp_67_policy() -> None:
    script = _gen2_script(proxy_ips=())
    # The dedicated system user (no home, no shell) exists before the unit
    # that runs as it is installed, and it owns the lease directory.
    useradd_line = f"useradd -r -M -s /usr/sbin/nologin {GEN2_DHCP_USER}"
    assert f"if ! id {GEN2_DHCP_USER} >/dev/null 2>&1; then\n    {useradd_line}\nfi" in script
    assert script.index(useradd_line) < script.index(f"-o {GEN2_DHCP_USER} -g {GEN2_DHCP_USER} {GEN2_DHCP_LEASE_DIR}")
    assert script.index(useradd_line) < script.index(f"systemctl enable {GEN2_DHCP_UNIT_NAME}")
    # The udp/67 policy is content-converged into its own file, made
    # boot-persistent the same way as the management lockdown, and loaded
    # BEFORE the server is (re)started -- even on a box with no lockdown at all.
    assert f"cmp -s {GEN2_DHCP_NFT_POLICY_PATH}.mngr-tmp {GEN2_DHCP_NFT_POLICY_PATH}" in script
    assert f"nft -f {GEN2_DHCP_NFT_POLICY_PATH}" in script
    assert script.index(f"nft -f {GEN2_DHCP_NFT_POLICY_PATH}") < script.index(
        f"systemctl restart {GEN2_DHCP_UNIT_NAME}"
    )
    assert "sed -i 's/^flush ruleset$//' /etc/nftables.conf" in script
    assert 'include "/etc/nftables.d/*.nft"' in script
    assert "systemctl enable nftables" in script
    assert script.index("systemctl enable nftables") < script.index(f"nft -f {GEN2_DHCP_NFT_POLICY_PATH}")
    # The lockdown section carries the same persistence block, so a
    # lockdown-configured prep has it exactly twice.
    assert script.count("systemctl enable nftables") == 1
    assert _gen2_script(proxy_ips=("203.0.113.10",)).count("systemctl enable nftables") == 2


def test_gen2_prep_script_stages_the_trixie_image_with_the_os_release_derived_docker_pin() -> None:
    script = _gen2_script()
    assert DEFAULT_GEN2_SLICE_GUEST_IMAGE_URL in script
    assert DEFAULT_GEN2_SLICE_GUEST_IMAGE_URL.startswith("https://apt.imbuepackages.com/artifacts/debian-cloud-image/")
    # The downloaded image is verified against its recorded digest before
    # qemu-img or virt-customize ever touch it.
    assert f'echo "{DEFAULT_GEN2_SLICE_GUEST_IMAGE_SHA512}  $img.tmp" | sha512sum -c -' in script
    assert script.index("sha512sum -c -") < script.index('qemu-img info "$img.tmp"')
    assert "/srv/mngr-slices/base/debian-13-base.qcow2" in script
    # The docker pin's distro suffix comes from the image's own os-release, so
    # the same pinned engine core lands on trixie guests (no hardcoded
    # bookworm suffix anywhere in the gen-2 flow).
    assert (
        f'DOCKER_APT_VERSION="{PINNED_DOCKER_APT_VERSION_CORE}~${{ID}}.${{VERSION_ID}}~${{VERSION_CODENAME}}"'
        in script
    )
    assert (
        f'CONTAINERD_APT_VERSION="{PINNED_CONTAINERD_APT_VERSION_CORE}~${{ID}}.${{VERSION_ID}}~${{VERSION_CODENAME}}"'
        in script
    )
    assert "bookworm" not in script
    assert "virt-customize" in script


def test_gen2_prep_script_keys_the_staged_image_on_the_customization_hash() -> None:
    script = _gen2_script()
    expected_hash = gen2_guest_image_customization_hash(DEFAULT_GEN2_SLICE_GUEST_IMAGE_URL, _APT_MIRROR_TIMESTAMP)
    # The image is (re-)staged when absent OR when the marker beside it records a
    # different customization; a successful stage writes the marker last.
    assert 'img_marker="$img.customization-sha256"' in script
    assert f'if [ ! -f "$img" ] || [ "$(cat "$img_marker" 2>/dev/null)" != "{expected_hash}" ]; then' in script
    assert f"""    mv "$img.tmp" "$img"\n    printf '%s\\n' "{expected_hash}" > "$img_marker"\n""" in script


def test_gen2_guest_image_customization_hash_changes_with_the_image_url_and_the_mirror_cut() -> None:
    default_hash = gen2_guest_image_customization_hash(DEFAULT_GEN2_SLICE_GUEST_IMAGE_URL, _APT_MIRROR_TIMESTAMP)
    assert default_hash == gen2_guest_image_customization_hash(
        DEFAULT_GEN2_SLICE_GUEST_IMAGE_URL, _APT_MIRROR_TIMESTAMP
    )
    assert default_hash != gen2_guest_image_customization_hash(
        "https://example.invalid/other-image.qcow2", _APT_MIRROR_TIMESTAMP
    )
    # A new mirror cut changes the frozen docker archive the guest installs from, so it re-stages too.
    assert default_hash != gen2_guest_image_customization_hash(DEFAULT_GEN2_SLICE_GUEST_IMAGE_URL, "20260801T000000Z")


def test_gen1_prep_script_still_stages_the_image_only_when_absent() -> None:
    # Gen-1 is retired by the cutover; its staging stays presence-only on purpose.
    script = _script()
    assert 'if [ ! -f "$img" ]; then' in script
    assert "customization-sha256" not in script


def test_gen2_prep_script_bakes_the_pinned_gvisor_runtime_into_the_guest_image() -> None:
    script = _gen2_script()
    customize = _gen2_customize_script(script)
    # The same pinned release + sha512 verification the live VPS host setup
    # runs, fetched from the mirror's copy of the release and installed into
    # the image; registration is a direct daemon.json write (the image is
    # customized offline, so `runsc install` has no dockerd to consult) with
    # the same --overlay2=none the VPS path uses.
    assert f'URL="https://apt.imbuepackages.com/artifacts/gvisor/{PINNED_GVISOR_RELEASE}/${{ARCH}}"' in customize
    assert "sha512sum -c runsc.sha512" in customize
    assert "sha512sum -c containerd-shim-runsc-v1.sha512" in customize
    assert "mv runsc containerd-shim-runsc-v1 /usr/local/bin/" in customize
    customize_commands = [line.strip() for line in customize.splitlines() if not line.strip().startswith("#")]
    assert not any(line.startswith("runsc install") for line in customize_commands)
    assert not any(line.startswith("systemctl") for line in customize_commands)
    daemon_json = customize.split("<<'MNGR_DOCKER_DAEMON_JSON'")[1].split("MNGR_DOCKER_DAEMON_JSON")[0]
    assert json.loads(daemon_json) == {
        "runtimes": {"runsc": {"path": "/usr/local/bin/runsc", "runtimeArgs": ["--overlay2=none"]}},
        "data-root": "/mnt/mngr-data/docker",
        "log-driver": "json-file",
        "log-opts": {"max-size": "50m", "max-file": "3"},
        "builder": {"gc": {"enabled": True, "defaultKeepStorage": "1GB"}},
    }
    # Both engines wait for the data disk (their roots); containerd's root moves
    # there too (its snapshotter holds the layers); the small boot disk caps the journal.
    assert "RequiresMountsFor=/mnt/mngr-data" in customize
    assert "/etc/systemd/system/docker.service.d/mngr-data-root.conf" in customize
    assert "/etc/systemd/system/containerd.service.d/mngr-data-root.conf" in customize
    assert 'root = "/mnt/mngr-data/containerd"' in customize
    assert "SystemMaxUse=512M" in customize
    # The image must not start the engines at boot before the data disk exists.
    assert "rm -f /etc/systemd/system/multi-user.target.wants/docker.service" in customize
    assert "/etc/systemd/system/multi-user.target.wants/containerd.service" in customize
    # The gen-1 image keeps plain runc: nothing gVisor in its customization.
    gen1_script = build_box_prep_script(
        pool_public_key="ssh-ed25519 AAAA pool",
        slice_service_user="slicehost",
        lima_version="2.2.0",
        slice_base_image_url="https://example.invalid/bookworm.qcow2",
    )
    assert "gvisor" not in gen1_script
    assert "runsc" not in gen1_script


def test_gen2_prep_script_disables_nested_virtualization_at_the_module_level() -> None:
    script = _gen2_script()
    assert "cat > /etc/modprobe.d/mngr-kvm.conf" in script
    assert "options kvm_intel nested=0" in script
    assert "options kvm_amd nested=0" in script
    # Applied at the next reboot only: the box has live VMs, so the module is
    # never unloaded/reloaded by prep.
    assert "rmmod" not in script
    assert "modprobe -r" not in script


def test_gen2_prep_script_keeps_the_shared_hardening_and_transfer_tooling() -> None:
    script = _gen2_script()
    assert "/usr/local/bin/age" in script
    assert "/usr/local/bin/s5cmd" in script
    # Gen-2 boxes use the neutral marker dir (no lima share dir).
    assert "/usr/local/share/mngr/.mngr-installed-transfer-tools" in script
    # The gen-2 swapfile lives on the storage partition (the root is sized for
    # the OS alone), and the swap-partition retirement keeps that line.
    assert "swapon /srv/mngr-slices/swapfile" in script
    assert "swapon /swapfile" not in script
    assert """awk '!($3 == "swap" && $1 != "/srv/mngr-slices/swapfile")'""" in script
    assert "99mngr-no-auto-reboot" in script


def _s3_ipv4_pin_block(script: str) -> str:
    """The rendered pin section: from the refresher script's heredoc through its synchronous seed run."""
    start = script.index("cat > /usr/local/sbin/mngr-s3-ipv4-pin.sh")
    enable_idx = script.index("systemctl enable --now mngr-s3-ipv4-pin.timer", start)
    seed_marker = "/usr/local/sbin/mngr-s3-ipv4-pin.sh\n"
    return script[start : script.index(seed_marker, enable_idx) + len(seed_marker)]


def test_gen2_prep_script_installs_the_same_s3_ipv4_pin_as_gen1() -> None:
    # The gen-2 boxes are the ones doing workspace stop/start uploads with s5cmd,
    # so the IPv4 pin (script, oneshot unit, minutely timer, synchronous seed run)
    # lands there too, byte-identical to the gen-1 block the pin script tests
    # above exercise, with dig among the gen-2 apt packages.
    gen2_script = _gen2_script()
    assert _s3_ipv4_pin_block(gen2_script) == _s3_ipv4_pin_block(_script())
    gen2_apt_line = next(line for line in gen2_script.splitlines() if line.startswith("apt-get install"))
    assert "bind9-dnsutils" in gen2_apt_line.split()
    # The pin follows the transfer tooling it exists for, and precedes the
    # telemetry manifest like every other prep artifact.
    assert gen2_script.index("transfer_tools_marker=") < gen2_script.index("mngr-s3-ipv4-pin.sh")
    assert gen2_script.index("mngr-s3-ipv4-pin.sh") < gen2_script.index("prep-artifacts.sha256")


def test_gen2_prep_script_keeps_the_image_tar_cache_on_the_storage_partition() -> None:
    script = _gen2_script()
    assert "install -d -m 755 -o slicehost -g slicehost /srv/mngr-slices/image-cache" in script
    assert "mngr-slice-default-workspace-template" not in script


def test_gen2_prep_script_installs_the_cutover_disk_transplant_tooling() -> None:
    # btrfs-progs and the nbd module (max_part so the partitioned gen-1 disk
    # exposes its partition node) for the cutover's box-side transplant.
    script = _gen2_script()
    assert "btrfs-progs" in script
    assert "echo nbd > /etc/modules-load.d/mngr-nbd.conf" in script
    assert 'echo "options nbd max_part=16" > /etc/modprobe.d/mngr-nbd.conf' in script
    assert "modprobe nbd max_part=16" in script


def test_gen2_prep_script_echoes_the_storage_partition_size_last() -> None:
    script = _gen2_script()
    marker_line = "echo \"MNGR_STORAGE_PARTITION_GIB $(( $(df --output=size -B1 /srv/mngr-slices | tail -1 | tr -d ' ') / 1073741824 ))\""
    assert marker_line in script
    assert script.index(marker_line) < script.index("echo MNGR_BOX_PREP_DONE")
    assert (
        parse_storage_partition_gib_from_prep_output("noise\nMNGR_STORAGE_PARTITION_GIB 879\nMNGR_BOX_PREP_DONE\n")
        == 879
    )
    assert parse_storage_partition_gib_from_prep_output("MNGR_STORAGE_PARTITION_GIB\n") is None
    assert parse_storage_partition_gib_from_prep_output("") is None


def test_gen2_prep_script_has_no_boot_autostart_unit() -> None:
    # Gen-2 boot autostart is exactly systemd WantedBy on each enabled
    # mngr-slice@ instance; the gen-1 autostart machinery must not ship.
    script = _gen2_script()
    assert "mngr-slices-autostart" not in script


def test_gen2_prep_script_brings_up_wireguard_and_echoes_the_public_key() -> None:
    script = _gen2_script()
    assert "wg genkey" in script
    assert "MNGR_WIREGUARD_PUBLIC_KEY" in script
    assert "Address = 10.112.1.1/16" in script
    assert "AllowedIPs = 10.112.0.2/32" in script


def test_gen2_prep_script_lockdown_follows_the_proxy_configuration() -> None:
    locked = _gen2_script(proxy_ips=("203.0.113.10",))
    assert "ip saddr { 203.0.113.10 } tcp dport 22 accept" in locked
    assert "tcp dport 22 counter drop" in locked

    open_script = _gen2_script(proxy_ips=())
    assert "tcp dport 22 counter drop" not in open_script
    assert "rm -f /etc/nftables.d/mngr-management.nft" in open_script


def test_gen2_prep_script_installs_the_telemetry_collector_after_the_wg_and_lockdown_sections() -> None:
    script = _gen2_script()
    assert "ethtool" in script
    assert "mngr-box-telemetry.timer" in script
    # The hash manifest must be recorded AFTER the artifacts it hashes are
    # installed (the WireGuard config is the last of them).
    manifest_idx = script.index("prep-artifacts.sha256")
    assert manifest_idx > script.index("wg genkey")
    assert manifest_idx > script.index("MNGR_GEN2_SUDOERS")
    # The rendered collector rides inside the prep as a heredoc and carries
    # the box's declared uplink for the egress signal.
    assert '"declared_uplink_mbps": 1000' in script


def test_gen2_prep_script_threads_the_proxy_ips_into_the_telemetry_allowlist() -> None:
    script = _gen2_script(proxy_ips=("203.0.113.10",))
    assert '"proxy_static_ips": ["203.0.113.10"]' in script

    open_script = _gen2_script(proxy_ips=())
    assert '"proxy_static_ips": []' in open_script


def test_gen2_prep_script_trusts_the_tier_ca_and_authorizes_no_static_key() -> None:
    script = _gen2_script()
    # The tier CA is the only management trust: it lands with the sshd drop-in and
    # one principals file per management account ...
    assert f"cat > /etc/ssh/mngr_user_ca.pub <<'MNGR_SSH_CA_FILE'\n{_SSH_CA}\n" in script
    assert "TrustedUserCAKeys /etc/ssh/mngr_user_ca.pub" in script
    assert "AuthorizedPrincipalsFile /etc/ssh/principals/%u" in script
    assert "cat > /etc/ssh/principals/debian <<'MNGR_SSH_CA_FILE'\nmngr-operator\n" in script
    assert "cat > /etc/ssh/principals/slicehost <<'MNGR_SSH_CA_FILE'\nmngr-service\n" in script
    # ... sshd validates and re-reads its config (without dropping the running
    # prep session), and only then is every static authorized key (the reinstall
    # throwaway, a pre-certificate pool key) removed, so a rejected config under
    # `set -e` never leaves the box with neither access path.
    remove_keys = "rm -f /home/debian/.ssh/authorized_keys /home/slicehost/.ssh/authorized_keys"
    assert remove_keys in script
    assert "sshd -t" in script
    assert "systemctl reload ssh" in script
    assert script.index("sshd -t") < script.index("systemctl reload ssh") < script.index(remove_keys)
    assert "MNGR_POOL_KEY" not in script
    assert _POOL_PUB not in script
    # The service user still exists with its kvm membership and uv.
    assert "useradd -m -s /bin/bash slicehost" in script
    assert "usermod -aG kvm slicehost" in script
    assert UV_TARBALL.mirror_url in script


def test_gen1_prep_script_still_authorizes_the_pool_key_only() -> None:
    script = _script()
    assert f"cat > /home/{_GEN1_USER}/.ssh/authorized_keys <<'MNGR_POOL_KEY'\n{_POOL_PUB}\n" in script
    assert "mngr_user_ca.pub" not in script


def test_gen2_prep_script_installs_docker_from_the_mirror_with_the_committed_signing_key() -> None:
    script = _gen2_script()
    customize = _gen2_customize_script(script)
    # The engine comes from the mirror's frozen docker archive at the committed
    # cut, and its signing key is written from the repo rather than fetched, so
    # the guest customization reaches no docker host at all.
    assert docker_apt_archive_url(_APT_MIRROR_TIMESTAMP) == (
        f"https://apt.imbuepackages.com/snap/{_APT_MIRROR_TIMESTAMP}/docker"
    )
    assert (
        f"signed-by=/etc/apt/keyrings/docker.asc] {docker_apt_archive_url(_APT_MIRROR_TIMESTAMP)} "
        "${VERSION_CODENAME} stable"
    ) in customize
    # The section is shared with the live release test (test_docker_mirror_release.py).
    assert render_docker_apt_source_section(_APT_MIRROR_TIMESTAMP) in customize
    key_block = customize.split("<<'MNGR_DOCKER_APT_KEY'\n")[1].split("\nMNGR_DOCKER_APT_KEY\n")[0]
    assert key_block == DOCKER_APT_SIGNING_KEY
    assert DOCKER_APT_SIGNING_KEY.startswith("-----BEGIN PGP PUBLIC KEY BLOCK-----")
    assert DOCKER_APT_SIGNING_KEY.endswith("-----END PGP PUBLIC KEY BLOCK-----")
    assert "curl -fsSL https://download.docker.com" not in customize


def test_gen2_prep_script_reaches_no_upstream_download_host() -> None:
    # The acceptance criterion of imbue-ai/mngr-internal#851: a gen-2 prep must
    # complete with every upstream host blocked. This is the CI-side guard for
    # the live sinkhole run described in apps/apt_mirror/README.md; the gen-1
    # prep (retired by the cutover) deliberately keeps its lima and bookworm
    # docker downloads.
    for script in (_gen2_script(), _gen2_script(proxy_ips=())):
        for host in _UPSTREAM_HOSTS:
            assert host not in script, f"gen-2 prep still reaches {host}"
        assert "apt.imbuepackages.com" in script
