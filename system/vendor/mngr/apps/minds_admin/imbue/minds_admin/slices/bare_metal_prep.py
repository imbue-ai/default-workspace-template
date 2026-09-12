import hashlib
import json
import posixpath
import shlex
from collections.abc import Sequence
from typing import Final

from imbue.apt_mirror.data_types import APT_MIRROR_PUBLIC_BASE_URL
from imbue.apt_mirror.data_types import DOCKER_ARCHIVE
from imbue.apt_mirror.parsing import snapshot_archive_url
from imbue.imbue_common.pure import pure
from imbue.minds.config.data_types import ManagementOverlayAllocation
from imbue.minds.config.data_types import WireguardOperatorConfig
from imbue.minds_admin.slices.box_telemetry import render_box_telemetry_prep_section
from imbue.minds_admin.slices.docker_apt_signing_key import DOCKER_APT_SIGNING_KEY
from imbue.minds_admin.slices.management_plane import render_management_lockdown_prep_section
from imbue.minds_admin.slices.management_plane import render_nftables_persistence_prep_section
from imbue.minds_admin.slices.management_plane import render_wireguard_prep_section
from imbue.minds_admin.slices.mirror_artifacts import AGE_TARBALL
from imbue.minds_admin.slices.mirror_artifacts import AGE_VERSION
from imbue.minds_admin.slices.mirror_artifacts import GEN2_SLICE_GUEST_IMAGE
from imbue.minds_admin.slices.mirror_artifacts import GVISOR_MIRROR_RELEASE_URL
from imbue.minds_admin.slices.mirror_artifacts import S5CMD_TARBALL
from imbue.minds_admin.slices.mirror_artifacts import S5CMD_VERSION
from imbue.minds_admin.slices.mirror_artifacts import UV_TARBALL
from imbue.minds_admin.slices.mirror_artifacts import UV_VERSION
from imbue.minds_admin.slices.storage_encryption import render_gen2_storage_encryption_section
from imbue.minds_admin.slices.storage_encryption import render_gen2_storage_relocation_section
from imbue.mngr_imbue_cloud.slices.bare_metal import GEN1_SLICE_SERVICE_USER
from imbue.mngr_imbue_cloud.slices.bare_metal import SLICE_INSTANCE_PREFIX
from imbue.mngr_imbue_cloud.slices.bare_metal import box_default_workspace_template_cache_dir
from imbue.mngr_imbue_cloud.slices.bare_metal import box_image_cache_dir_for_generation
from imbue.mngr_imbue_cloud.slices.bare_metal import slice_base_image_path
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import FIRST_QEMU_BOX_GENERATION
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_BASE_IMAGE_PATH
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_BY_ORDINAL_DIR
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_DHCP_CONFIG_PATH
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_DHCP_LEASE_DIR
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_DHCP_NFT_POLICY_PATH
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_DHCP_UNIT_NAME
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_DHCP_UNIT_PATH
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_DHCP_USER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_GUEST_CONTAINERD_ROOT
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_GUEST_DATA_MOUNT
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_GUEST_DOCKER_DATA_ROOT
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_HELPER_PATH
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_INSTANCES_DIR
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_MAX_SLICE_COUNT
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_OVMF_CODE_PATH
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_SLICE_SERVICE_USER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_STORAGE_PARTITION_MARKER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_STORAGE_ROOT
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_SUDOERS_PATH
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_SWAPFILE_PATH
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_UNIT_PATH
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import GEN2_SWAPFILE_GIB
from imbue.mngr_imbue_cloud.slices.gen2_scripts.ssh_ca import SSH_CA_BOX_BOOTSTRAP_USER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.ssh_ca import SSH_CA_PRINCIPAL_OPERATOR
from imbue.mngr_imbue_cloud.slices.gen2_scripts.ssh_ca import SSH_CA_PRINCIPAL_SERVICE
from imbue.mngr_imbue_cloud.slices.gen2_scripts.ssh_ca import render_ssh_ca_trust_shell_section
from imbue.mngr_imbue_cloud.slices.qemu_slice import render_slice_dhcp_config
from imbue.mngr_imbue_cloud.slices.qemu_slice import render_slice_dhcp_nftables_policy
from imbue.mngr_imbue_cloud.slices.qemu_slice import render_slice_dhcp_unit
from imbue.mngr_imbue_cloud.slices.qemu_slice import render_slice_helper_script
from imbue.mngr_imbue_cloud.slices.qemu_slice import render_slice_sudoers
from imbue.mngr_imbue_cloud.slices.qemu_slice import render_slice_unit_file
from imbue.mngr_vps.host_setup import GVISOR_RUNSC_BINARY_PATH
from imbue.mngr_vps.host_setup import GVISOR_RUNSC_RUNTIME_ARGS
from imbue.mngr_vps.host_setup import PINNED_CONTAINERD_APT_VERSION
from imbue.mngr_vps.host_setup import PINNED_CONTAINERD_APT_VERSION_CORE
from imbue.mngr_vps.host_setup import PINNED_DOCKER_APT_VERSION
from imbue.mngr_vps.host_setup import PINNED_DOCKER_APT_VERSION_CORE
from imbue.mngr_vps.host_setup import render_gvisor_binary_install_script

# Lima release to install on the box. Must stay >= 2.2.0: earlier guestagents leak
# one goroutine + one socket FD per forwarded connection (portfwdserver's blocking
# closeCh, fixed upstream in the 2.2.0 release), which slowly wedged production
# slices. Independent of the desktop app's own lima pin (apps/minds/scripts/
# build.js), which is held back by a macOS-only usernet regression the box's
# qemu path does not use.
DEFAULT_LIMA_VERSION: Final[str] = "2.2.0"

# The gen-2 slice guest base image: the SAME Debian 13 "trixie" cloud image
# release DWT pins for desktop Lima VMs (default-workspace-template
# .mngr/settings.toml, [providers.lima] default_image_url_*) so cloud slices
# and desktop workspaces run the identical guest OS snapshot, served from
# imbue's artifact mirror (slices/mirror_artifacts.py names the upstream
# release). Bump the manifest and the dwt pins together -- the minds release
# checklist (apps/minds/docs/deploy/ops/app-release.md) carries the reminder.
DEFAULT_GEN2_SLICE_GUEST_IMAGE_URL: Final[str] = GEN2_SLICE_GUEST_IMAGE.mirror_url
DEFAULT_GEN2_SLICE_GUEST_IMAGE_SHA512: Final[str] = GEN2_SLICE_GUEST_IMAGE.digest

# Swapfile size (GiB) to provision on the box. Slice hosts run RAM near capacity, so a
# real swapfile is cheap OOM insurance against transient spikes (idle baked agents
# don't thrash steady-state). Replaces the OS-install default (two tiny ~0.5GiB swap
# partitions), which is too small to matter. Gen-1 boxes keep it on the root
# filesystem; gen-2 boxes put it on the storage partition (the gen-2 root is
# sized for the OS alone), where the sizing reserve budgets it.
_SWAPFILE_SIZE_GIB: Final[int] = GEN2_SWAPFILE_GIB
_GEN1_SWAPFILE_PATH: Final[str] = "/swapfile"

# How many slice VMs the gen-1 boot autostart brings up concurrently. A full box
# cold-booting 14 QEMU VMs at once is a boot storm (each start waits on guest
# boot + lima requirement checks); fully serial keeps users down for ~10 minutes.
_SLICE_AUTOSTART_PARALLELISM: Final[int] = 4

# Packages a gen-1 box needs to run lima/QEMU VMs and the slice bake (Docker lives
# inside each VM, not on the box). ``libguestfs-tools`` provides ``virt-customize``,
# used to pre-install Docker + inotify-tools into the golden slice image so per-VM
# first-boot provisioning skips those downloads. ``zstd`` compresses the workspace
# stop/start transfer streams (the box-health sweep probes for it alongside the
# pinned age + s5cmd installs below).
_BOX_APT_PACKAGES: Final[tuple[str, ...]] = (
    "qemu-system-x86",
    "qemu-utils",
    "btrfs-progs",
    "rsync",
    "git",
    "curl",
    "ca-certificates",
    "iproute2",
    "libguestfs-tools",
    "zstd",
)

# Packages a gen-2 box needs (specs/slice-fleet-gen2): raw qemu + OVMF firmware
# for the slice VMs, nftables for the per-VM rules and the management lockdown,
# genisoimage for the cidata ISOs, dnsmasq-base for the slice DHCP server (the
# bare binary, not the ``dnsmasq`` package, whose distro service would start
# serving DNS on every interface with the package defaults; the prep runs it
# under mngr's own unit on the rendered config), wireguard-tools for the
# operator overlay, xfsprogs for the storage partition, cryptsetup +
# systemd-cryptsetup (the LUKS storage volume and its TPM enrollment; the
# latter is trixie's split package carrying systemd-cryptenroll and the TPM2
# unlock path) + tpm2-tools (operator diagnostics), ethtool for the telemetry
# collector's link-speed audit, plus the same bake/transfer tooling as gen-1.
# No lima; gen-2 data disks are formatted inside the guest, and
# btrfs-progs is here only for the cutover's box-side transplant of gen-1 home
# subvolumes into fresh gen-2 data disks (mounted through qemu-nbd).
# CLEANUP: drop btrfs-progs and the nbd module-load below once the gen-1 ->
# gen-2 cutover has run on every tier (phase 6 of blueprint/slice-fleet-cutover).
_GEN2_BOX_APT_PACKAGES: Final[tuple[str, ...]] = (
    "qemu-system-x86",
    "qemu-utils",
    "btrfs-progs",
    "ovmf",
    "nftables",
    "genisoimage",
    "dnsmasq-base",
    "wireguard-tools",
    "xfsprogs",
    "cryptsetup",
    "systemd-cryptsetup",
    "tpm2-tools",
    "ethtool",
    "rsync",
    "git",
    "curl",
    "ca-certificates",
    "iproute2",
    "libguestfs-tools",
    "zstd",
)

# Marker directory for the pinned-binary installs. Gen-1 boxes historically
# keep theirs under the lima share dir; gen-2 boxes have no lima, so they use
# a neutral mngr share dir.
_GEN1_INSTALL_MARKER_DIR: Final[str] = "/usr/local/share/lima"
_GEN2_INSTALL_MARKER_DIR: Final[str] = "/usr/local/share/mngr"

# The Docker daemon config baked into the gen-2 guest image: runsc registered
# with the same flags the live-host path (``runsc install``) registers it with.
# Written directly rather than via ``runsc install`` because the image is
# customized offline (no dockerd to consult) and the file has no other content.
_GEN2_GUEST_DOCKER_DAEMON_JSON: Final[str] = json.dumps(
    {
        "runtimes": {"runsc": {"path": GVISOR_RUNSC_BINARY_PATH, "runtimeArgs": list(GVISOR_RUNSC_RUNTIME_ARGS)}},
        # Docker's whole data-root lives on the data disk (the workspace image,
        # the container layers, docker's metadata), the one disk a resize grows;
        # the boot disk keeps only the OS. Container logs rotate so they cannot
        # fill the workspace quota.
        "data-root": GEN2_GUEST_DOCKER_DATA_ROOT,
        "log-driver": "json-file",
        "log-opts": {"max-size": "50m", "max-file": "3"},
        # The build cache lives outside the host quota (in docker's data-root),
        # so it is bounded to fit the data disk's system reserve.
        "builder": {"gc": {"enabled": True, "defaultKeepStorage": "1GB"}},
    }
)

# Neither engine may start before the data disk (both roots) is mounted, and
# the small gen-2 boot disk caps the journal. containerd's root moves with a
# top-level ``root`` key; the rest of the package's default config stays.
_GEN2_GUEST_ENGINE_DROP_IN: Final[str] = f"[Unit]\nRequiresMountsFor={GEN2_GUEST_DATA_MOUNT}\n"
_GEN2_GUEST_CONTAINERD_CONFIG: Final[str] = f'root = "{GEN2_GUEST_CONTAINERD_ROOT}"\ndisabled_plugins = ["cri"]\n'
_GEN2_GUEST_JOURNALD_CONF: Final[str] = "[Journal]\nSystemMaxUse=512M\n"

# Nested virtualization is switched off at the kvm module level on gen-2 boxes
# (belt and braces with the unit's ``-cpu host,vmx=off,svm=off``): a workspace
# has no business running its own hypervisor. Module options apply at the next
# box reboot; the module is never unloaded live (it has VMs).
_GEN2_KVM_MODPROBE_CONF_PATH: Final[str] = "/etc/modprobe.d/mngr-kvm.conf"


@pure
def _render_transfer_tools_section(marker_dir: str) -> str:
    """The pinned age + s5cmd install section shared by both generations' preps."""
    return f"""\
# Transfer tooling for workspace stop/start: age (encryption) and s5cmd
# (parallel S3 client), both pinned static binaries from the artifact mirror,
# verified against their recorded digests and tracked via a marker file.
transfer_tools_marker={marker_dir}/.mngr-installed-transfer-tools
if [ "$(cat "$transfer_tools_marker" 2>/dev/null)" != "{AGE_VERSION}-{S5CMD_VERSION}" ]; then
    curl -fsSL -o /tmp/age.tar.gz {AGE_TARBALL.mirror_url}
    echo "{AGE_TARBALL.digest}  /tmp/age.tar.gz" | sha256sum -c -
    tar -C /tmp -xzf /tmp/age.tar.gz
    install -m 755 /tmp/age/age /usr/local/bin/age
    install -m 755 /tmp/age/age-keygen /usr/local/bin/age-keygen
    rm -rf /tmp/age /tmp/age.tar.gz
    curl -fsSL -o /tmp/s5cmd.tar.gz {S5CMD_TARBALL.mirror_url}
    echo "{S5CMD_TARBALL.digest}  /tmp/s5cmd.tar.gz" | sha256sum -c -
    tar -C /tmp -xzf /tmp/s5cmd.tar.gz s5cmd
    install -m 755 /tmp/s5cmd /usr/local/bin/s5cmd
    rm -f /tmp/s5cmd /tmp/s5cmd.tar.gz
    mkdir -p {marker_dir}
    printf '%s\\n' "{AGE_VERSION}-{S5CMD_VERSION}" > "$transfer_tools_marker"
fi
"""


@pure
def _render_swapfile_section(swapfile_path: str) -> str:
    """The swapfile-provisioning section shared by both generations' preps."""
    return f"""\
# Provision a real swapfile (idempotent). Slice hosts run RAM near capacity; the
# OS-install default swap (two ~0.5GiB partitions) is too small to cushion spikes.
if ! swapon --show=NAME --noheadings 2>/dev/null | grep -qx {swapfile_path}; then
    if [ ! -f {swapfile_path} ]; then
        fallocate -l {_SWAPFILE_SIZE_GIB}G {swapfile_path} || dd if=/dev/zero of={swapfile_path} bs=1M count=$(({_SWAPFILE_SIZE_GIB} * 1024))
        chmod 600 {swapfile_path}
        mkswap {swapfile_path}
    fi
    swapon {swapfile_path}
fi
grep -q "^{swapfile_path} " /etc/fstab || echo "{swapfile_path} none swap sw 0 0" >> /etc/fstab
"""


@pure
def _render_swap_partition_retirement_section(swapfile_path: str) -> str:
    """The raw-swap-partition retirement section shared by both generations' preps."""
    return f"""\
# Retire the OS-install per-disk swap partitions (idempotent). They sit on raw
# partitions OUTSIDE the md RAID mirrors, so when a disk dies its swapped-out
# pages are gone and every process touching one gets SIGBUS -- which killed 13
# of 14 slice VMs over several days in the 2026-08-07 production nvme failure.
# Worse, the kernel activates them at boot before the swapfile, so their
# default priorities make them the PREFERRED swap. All swap belongs on the
# mirrored swapfile: turn the partitions off (swapoff migrates their few pages;
# a failure here must fail prep loudly, not leave unmirrored swap in use),
# drop them from fstab, and wipe their signatures so nothing re-activates them.
for swap_partition in $(swapon --show=NAME --noheadings 2>/dev/null | grep '^/dev/' || true); do
    swapoff "$swap_partition"
done
awk '!($3 == "swap" && $1 != "{swapfile_path}")' /etc/fstab > /etc/fstab.mngr-tmp && mv /etc/fstab.mngr-tmp /etc/fstab
for swap_partition in $(blkid -t TYPE=swap -o device 2>/dev/null || true); do
    wipefs -a "$swap_partition"
done
"""


@pure
def _render_no_auto_reboot_section() -> str:
    """The unattended-upgrades reboot pin shared by both generations' preps."""
    return """\
# Pin unattended-upgrades to never reboot the box on its own. The Debian
# default is already "false", but an explicit pin survives config drift (an
# image or package update flipping it). Slice boxes host user workspaces:
# kernels stage and activate at the next operator-scheduled reboot instead.
cat > /etc/apt/apt.conf.d/99mngr-no-auto-reboot <<'MNGR_NO_AUTO_REBOOT'
// Managed by mngr (bare_metal_prep): never let unattended-upgrades reboot a
// slice box on its own. Kernels stage and activate at the next operator-
// scheduled reboot; slices are user workspaces and must not restart unannounced.
Unattended-Upgrade::Automatic-Reboot "false";
MNGR_NO_AUTO_REBOOT
"""


@pure
def _render_service_user_section(service_user: str) -> str:
    """The slice service user (kvm group) + uv install shared by both preps; key material is per generation."""
    return f"""\
# Dedicated non-root service user that owns the slice VMs (kvm group for /dev/kvm).
if ! id {service_user} >/dev/null 2>&1; then
    useradd -m -s /bin/bash {service_user}
fi
usermod -aG kvm {service_user}
install -d -m 700 -o {service_user} -g {service_user} /home/{service_user}/.ssh

# Install the pinned uv release for the service user (used to run the vendored
# mngr that drives the bake) from the artifact mirror, converged on version:
# a box carrying any other uv (or none) gets exactly this one.
uv_home=/home/{service_user}/.local
if [ "$("$uv_home/bin/uv" --version 2>/dev/null | awk '{{print $2}}')" != "{UV_VERSION}" ]; then
    curl -fsSL -o /tmp/uv.tar.gz {UV_TARBALL.mirror_url}
    echo "{UV_TARBALL.digest}  /tmp/uv.tar.gz" | sha256sum -c -
    rm -rf /tmp/uv-extract
    mkdir -p /tmp/uv-extract
    tar -C /tmp/uv-extract --strip-components=1 -xzf /tmp/uv.tar.gz
    install -d -m 755 -o {service_user} -g {service_user} "$uv_home" "$uv_home/bin"
    install -m 755 -o {service_user} -g {service_user} /tmp/uv-extract/uv /tmp/uv-extract/uvx "$uv_home/bin/"
    rm -rf /tmp/uv-extract /tmp/uv.tar.gz
fi
"""


@pure
def _render_gen1_pool_key_section(service_user: str, pool_public_key: str) -> str:
    """Authorize the gen-1 pool management key for the service user (a single-key overwrite).

    CLEANUP: drop with the gen-1 prep once the gen-1 -> gen-2 cutover has run on
    every tier (phase 6 of blueprint/slice-fleet-cutover).
    """
    return f"""\
# Authorize the pool management key so the admin CLI + connector can SSH in as
# this user (to bake slices and to tear them down on release).
cat > /home/{service_user}/.ssh/authorized_keys <<'MNGR_POOL_KEY'
{pool_public_key.strip()}
MNGR_POOL_KEY
chown {service_user}:{service_user} /home/{service_user}/.ssh/authorized_keys
chmod 600 /home/{service_user}/.ssh/authorized_keys
"""


@pure
def render_gen2_ssh_ca_trust_section(ssh_ca_public_key: str, service_user: str) -> str:
    """The gen-2 management-SSH trust: the tier CA, per-user principals, and NO static key anywhere.

    The bootstrap user accepts the operator principal (full sudo, as the OS image
    ships it) and the service user the service principal (its scoped sudoers). Any
    ``authorized_keys`` either account carries -- the reinstall-time throwaway key,
    or a pool key from a pre-certificate prep -- is removed, so a certificate from
    the tier CA is the only way in. The static keys go only after sshd has
    validated and reloaded the trust (the script runs under ``set -e``), so a
    rejected config never strands the box with neither access path. The running
    prep session is unaffected by the reload; a later login with a static key is
    refused.
    """
    trust_section = render_ssh_ca_trust_shell_section(
        ssh_ca_public_key,
        {SSH_CA_BOX_BOOTSTRAP_USER: SSH_CA_PRINCIPAL_OPERATOR, service_user: SSH_CA_PRINCIPAL_SERVICE},
    )
    return f"""\
# Management SSH by certificate only: trust the tier's SSH CA (imbue-ai/mngr-internal#850)
# and map its principals to the two management accounts; only once sshd has
# validated and picked up that trust are the accounts' static authorized keys dropped.
{trust_section}sshd -t
systemctl reload ssh 2>/dev/null || systemctl reload sshd
rm -f /home/{SSH_CA_BOX_BOOTSTRAP_USER}/.ssh/authorized_keys /home/{service_user}/.ssh/authorized_keys
"""


@pure
def build_box_prep_script(
    *,
    pool_public_key: str,
    slice_service_user: str,
    lima_version: str,
    slice_base_image_url: str,
) -> str:
    """Render the idempotent root bash script that prepares a fresh Debian box to host gen-1 slices.

    Installs QEMU + lima + tooling, creates the non-root ``slice_service_user`` (in
    the ``kvm`` group, with the pool management key authorized so the admin CLI and
    the connector can reach it), installs ``uv`` for that user, and stages the slice
    guest OS image (``slice_base_image_url``) once so VM boots never depend on the
    Debian mirror. The staged image is additionally customized (via ``virt-customize``)
    to pre-install the pinned Docker Engine + inotify-tools, so each slice VM's
    first-boot provisioning finds them present and skips the per-VM download/install.
    Also hardens the box against reboots: pins unattended-upgrades to never auto-reboot,
    and installs (enable-only) the ``mngr-slices-autostart.service`` boot unit that
    starts every stopped ``mngr-slice-*`` VM as the lima user after a box reboot.
    limactl is never invoked as root (lima refuses to run as root): prep itself only
    installs it, and the autostart script's limactl calls run later as the lima user
    via the unit's ``User=`` directive. Intended to be piped to ``sudo bash`` on the box.
    """
    apt_packages = " ".join(_BOX_APT_PACKAGES)
    lima_tarball = f"lima-{lima_version}-Linux-x86_64.tar.gz"
    lima_url = f"https://github.com/lima-vm/lima/releases/download/v{lima_version}/{lima_tarball}"
    base_image_path = slice_base_image_path(slice_service_user)
    default_workspace_template_cache_dir = box_default_workspace_template_cache_dir(slice_service_user)
    slice_autostart_parallelism = _SLICE_AUTOSTART_PARALLELISM
    return f"""\
#!/bin/bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

# 1. System packages for QEMU/lima + the bake tooling.
apt-get update -qq
apt-get install -y -qq {apt_packages}

# 2. Install limactl (extract as root; never run limactl as root -- lima refuses,
#    so the installed release is tracked via a marker file instead of `limactl
#    --version`). Version-aware: re-running prep on a box whose installed lima
#    does not match the pinned release (including boxes prepped before the marker
#    existed) re-extracts the tarball, so a version bump reaches existing boxes.
lima_version_marker={_GEN1_INSTALL_MARKER_DIR}/.mngr-installed-lima-version
if [ "$(cat "$lima_version_marker" 2>/dev/null)" != "{lima_version}" ]; then
    curl -fsSL -o /tmp/{lima_tarball} {lima_url}
    tar -C /usr/local -xzf /tmp/{lima_tarball}
    rm -f /tmp/{lima_tarball}
    mkdir -p {_GEN1_INSTALL_MARKER_DIR}
    printf '%s\\n' "{lima_version}" > "$lima_version_marker"
fi

{_render_transfer_tools_section(_GEN1_INSTALL_MARKER_DIR)}
{_render_service_user_section(slice_service_user)}
{_render_gen1_pool_key_section(slice_service_user, pool_public_key)}
# 6. Stage + customize the golden slice guest image once (idempotent). Download the
#    base Debian qcow2, then pre-install the pinned Docker Engine + inotify-tools INTO
#    the image with virt-customize, so each slice VM's first-boot provisioning finds
#    them already present and skips the per-VM download/install (those guards are
#    presence-only). Customize the temp copy and only atomically move it into place on
#    success, so a partial/failed download or customize never becomes the base. Runs
#    as root (virt-customize needs /dev/kvm); the finished image is chowned to the lima
#    user that limactl reads it as. Referenced via file:// so VM boots never hit the
#    Debian mirror. To re-stage with a new customization, delete the image and re-run
#    (gen-1 is retired by the cutover, so only the gen-2 prep keys the staged image on
#    a content hash of its customization).
img={base_image_path}
# Create the image dir AND its parent (the user's ~/.cache) owned by the lima user.
# This script runs as root, so a freshly-created ~/.cache would be root-owned --
# which blocks `limactl` (run as the lima user) from creating ~/.cache/lima and fails
# every VM start. `install -d` only sets ownership on the leaf it's given, so create
# the whole chain and chown it (chown also repairs a ~/.cache left root-owned by an
# earlier prep run, since mkdir -p won't change an existing dir's ownership).
image_dir="$(dirname "$img")"
cache_dir="$(dirname "$image_dir")"
mkdir -p "$image_dir"
chown {slice_service_user}:{slice_service_user} "$cache_dir" "$image_dir"
chmod 755 "$cache_dir" "$image_dir"
if [ ! -f "$img" ]; then
    curl -fsSL --retry 8 --retry-delay 15 --retry-all-errors --retry-connrefused -o "$img.tmp" {slice_base_image_url}
    qemu-img info "$img.tmp" >/dev/null
    # In-guest customization run offline by virt-customize (so cloud-init still runs
    # fresh per VM). Installs the SAME pinned Docker (apt repo + exact =version) the
    # OVH path pins, plus inotify-tools, then trims apt caches to keep the image lean.
    # No systemctl here (no init in the appliance); the per-VM boot script enables +
    # starts docker. `set -eu` only (no pipefail): the appliance shell may be dash.
    cat > /tmp/mngr-slice-image-customize.sh <<'MNGR_SLICE_CUSTOMIZE'
set -eux
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl gnupg
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable" > /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y --allow-downgrades docker-ce="{PINNED_DOCKER_APT_VERSION}" docker-ce-cli="{PINNED_DOCKER_APT_VERSION}" containerd.io="{PINNED_CONTAINERD_APT_VERSION}" docker-buildx-plugin docker-compose-plugin inotify-tools
apt-get clean
rm -rf /var/lib/apt/lists/*
MNGR_SLICE_CUSTOMIZE
    virt-customize -a "$img.tmp" --network --run /tmp/mngr-slice-image-customize.sh
    rm -f /tmp/mngr-slice-image-customize.sh
    chown {slice_service_user}:{slice_service_user} "$img.tmp"
    mv "$img.tmp" "$img"
fi

# 6b. Create the per-box DEFAULT_WORKSPACE_TEMPLATE image cache dir (owned by the lima user) where the box
#     keeps the single ``docker save`` tar slices ``docker load`` instead of rebuilding.
install -d -m 755 -o {slice_service_user} -g {slice_service_user} {default_workspace_template_cache_dir}

{_render_swapfile_section(_GEN1_SWAPFILE_PATH)}
{_render_swap_partition_retirement_section(_GEN1_SWAPFILE_PATH)}
{_render_no_auto_reboot_section()}
# 9. Boot autostart for the slice VMs. After a box reboot nothing else restarts
#    the lima VMs ({slice_service_user} has no linger session and lima has no boot
#    integration), so every workspace on the box stays down until an operator
#    intervenes. This unit starts all stopped slice VMs at boot as the lima user
#    (lima refuses root), with bounded parallelism and one retry per instance;
#    a VM that still fails start fails the unit so the breakage is visible in
#    systemd. Enable only (no start): with no reboot pending it must not run
#    now, and starting stopped-on-purpose VMs is the boot path's job alone.
#    In-VM recovery (workspace container + services agent) is handled inside
#    each VM by its own minds-autostart units.
cat > /usr/local/sbin/mngr-slices-autostart.sh <<'MNGR_SLICES_AUTOSTART'
#!/bin/bash
# Start every stopped mngr slice VM on this box (bounded parallelism, one retry
# each). Runs as the lima service user via mngr-slices-autostart.service.
set -euo pipefail
export PATH=/usr/local/bin:$PATH

start_one_slice() {{
    if limactl start "$1"; then
        return 0
    fi
    echo "first start of slice $1 failed; retrying" >&2
    limactl start "$1"
}}
export -f start_one_slice

# No stderr suppression and pipefail above: a failing listing must fail the
# unit (visible in systemd), not read as "no VMs to start".
# Skip VMs carrying the stop-requested marker: they were deliberately halted
# by a workspace stop (mid-upload) or are mid-restore, and must only ever be
# started by the connector's transition supervisor.
stopped_instances=$(limactl list --format '{{{{.Name}}}} {{{{.Status}}}}' \\
    | awk -v prefix="{SLICE_INSTANCE_PREFIX}" 'index($1, prefix) == 1 && $2 == "Stopped" {{print $1}}' \\
    | while read -r name; do
        [ -e "$HOME/.lima/$name/mngr-stop-requested" ] || echo "$name"
    done)
if [ -z "$stopped_instances" ]; then
    echo "no stopped slice VMs to start"
    exit 0
fi
printf '%s\\n' "$stopped_instances" | xargs -n1 -P {slice_autostart_parallelism} bash -c 'start_one_slice "$1"' _
MNGR_SLICES_AUTOSTART
chmod +x /usr/local/sbin/mngr-slices-autostart.sh
cat > /etc/systemd/system/mngr-slices-autostart.service <<'MNGR_SLICES_UNIT'
[Unit]
Description=Start all mngr slice VMs on box boot
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
User={slice_service_user}
WorkingDirectory=/home/{slice_service_user}
ExecStart=/usr/local/sbin/mngr-slices-autostart.sh
TimeoutStartSec=45min

[Install]
WantedBy=multi-user.target
MNGR_SLICES_UNIT
systemctl daemon-reload
systemctl enable mngr-slices-autostart.service

echo MNGR_BOX_PREP_DONE
"""


@pure
def _render_retired_gen2_service_user_section(service_user: str) -> str:
    """Converge a gen-2 box prepped before the service-user rename: re-own the slice tree, remove the old user.

    Gen-2 boxes prepped before imbue-ai/mngr-internal#848 ran their slices as
    the gen-1 user name. Everything that user owns under the storage root
    (instance dirs, env files, the setgid ``run/`` dirs' group, the staged base
    image, the image tar cache) is handed to the new service user, then the old
    user is removed outright: it must never exist on a gen-2 box. A freshly
    installed box has no such user and skips the whole block.
    """
    # CLEANUP: drop once every gen-2 box prepped before the rename has been
    # re-prepped or repaved (the dev canaries; staging and production boxes are
    # repaved fresh, so they never carry the old user).
    retired_user = GEN1_SLICE_SERVICE_USER
    return f"""\
# A gen-2 box prepped before the service-user rename still has the old user:
# hand its files under the storage root to the new service user (owner, then
# group -- the per-VM disk media stay owned by their slice user) and remove it.
if id {retired_user} >/dev/null 2>&1; then
    chown -R --from={retired_user} {service_user} {GEN2_STORAGE_ROOT}
    chown -R --from=:{retired_user} :{service_user} {GEN2_STORAGE_ROOT}
    pkill -u {retired_user} || true
    for _ in $(seq 1 10); do
        pgrep -u {retired_user} >/dev/null || break
        sleep 1
    done
    userdel -r {retired_user}
fi
"""


@pure
def _render_gen2_prep_artifacts_section() -> str:
    """Install the root-owned gen-2 slice unit, helper and sudoers, converging on content changes.

    Together with the slice DHCP server's config, unit and udp/67 policy (see
    :func:`_render_gen2_dhcp_server_section`) these are the ONLY box-installed
    gen-2 pieces (everything day-to-day is caller-rendered); each is compared
    byte-for-byte against the currently installed copy so a re-prep with
    unchanged renderers touches nothing, while a bumped renderer reaches
    existing boxes on the next prep. The sudoers file is validated with
    ``visudo -c`` before install -- a syntactically broken sudoers would lock
    every ``sudo`` on the box.
    """
    unit_text = render_slice_unit_file()
    helper_text = render_slice_helper_script()
    sudoers_text = render_slice_sudoers()
    return f"""\
# Prep artifacts: the mngr-slice@ template unit, the root helper, and the
# exact-argument sudoers grants, rendered by the mngr_imbue_cloud plugin and
# converged on content.
cat > /tmp/mngr-slice-unit.mngr-tmp <<'MNGR_GEN2_UNIT'
{unit_text}\
MNGR_GEN2_UNIT
if ! cmp -s /tmp/mngr-slice-unit.mngr-tmp {GEN2_UNIT_PATH}; then
    install -m 644 /tmp/mngr-slice-unit.mngr-tmp {GEN2_UNIT_PATH}
    systemctl daemon-reload
fi
rm -f /tmp/mngr-slice-unit.mngr-tmp

cat > /tmp/mngr-slice-helper.mngr-tmp <<'MNGR_GEN2_HELPER'
{helper_text}\
MNGR_GEN2_HELPER
if ! cmp -s /tmp/mngr-slice-helper.mngr-tmp {GEN2_HELPER_PATH}; then
    install -m 755 /tmp/mngr-slice-helper.mngr-tmp {GEN2_HELPER_PATH}
fi
rm -f /tmp/mngr-slice-helper.mngr-tmp

cat > /tmp/mngr-slice-sudoers.mngr-tmp <<'MNGR_GEN2_SUDOERS'
{sudoers_text}\
MNGR_GEN2_SUDOERS
visudo -cf /tmp/mngr-slice-sudoers.mngr-tmp
if ! cmp -s /tmp/mngr-slice-sudoers.mngr-tmp {GEN2_SUDOERS_PATH}; then
    install -m 440 /tmp/mngr-slice-sudoers.mngr-tmp {GEN2_SUDOERS_PATH}
fi
rm -f /tmp/mngr-slice-sudoers.mngr-tmp
"""


@pure
def _render_gen2_dhcp_server_section() -> str:
    """Install the slice DHCP server's user, udp/67 policy, config and unit (content-converged) and make sure it runs.

    The server's dedicated system user exists before anything else (the unit
    runs as it and owns the lease directory). The udp/67 policy is installed
    like the management lockdown's (its own file under ``/etc/nftables.d``,
    boot-persistent, loaded now) and lands BEFORE the server is (re)started,
    so no public-interface DHCP packet ever reaches a socket. The config and
    unit converge like the other prep artifacts, and the rendered config is
    syntax-checked before it can replace the live one. The server is restarted
    only when its config or unit changed (otherwise merely started), so a
    re-prep never interrupts the leases of running slices for nothing.
    """
    dhcp_config_text = render_slice_dhcp_config()
    dhcp_unit_text = render_slice_dhcp_unit()
    dhcp_nft_policy_text = render_slice_dhcp_nftables_policy()
    dhcp_config_dir = posixpath.dirname(GEN2_DHCP_CONFIG_PATH)
    return f"""\
# The slice DHCP server: dnsmasq (DHCP only, bound to the slice taps) hands
# every guest the /30 address its ordinal's rules expect, so the guests'
# cidata carries no placement and cloud-init never reruns on a restore. It
# runs as its own unprivileged user under the unit's sandbox, and the box
# answers udp/67 on the taps alone.
if ! id {GEN2_DHCP_USER} >/dev/null 2>&1; then
    useradd -r -M -s /usr/sbin/nologin {GEN2_DHCP_USER}
fi
install -d -m 755 {dhcp_config_dir}
install -d -m 755 -o {GEN2_DHCP_USER} -g {GEN2_DHCP_USER} {GEN2_DHCP_LEASE_DIR}
{render_nftables_persistence_prep_section()}\
cat > {GEN2_DHCP_NFT_POLICY_PATH}.mngr-tmp <<'MNGR_GEN2_DHCP_NFT'
{dhcp_nft_policy_text}\
MNGR_GEN2_DHCP_NFT
if ! cmp -s {GEN2_DHCP_NFT_POLICY_PATH}.mngr-tmp {GEN2_DHCP_NFT_POLICY_PATH}; then
    install -m 644 {GEN2_DHCP_NFT_POLICY_PATH}.mngr-tmp {GEN2_DHCP_NFT_POLICY_PATH}
fi
rm -f {GEN2_DHCP_NFT_POLICY_PATH}.mngr-tmp
nft -f {GEN2_DHCP_NFT_POLICY_PATH}
cat > /tmp/mngr-slice-dhcp.conf.mngr-tmp <<'MNGR_GEN2_DHCP_CONF'
{dhcp_config_text}\
MNGR_GEN2_DHCP_CONF
cat > /tmp/mngr-slice-dhcp.service.mngr-tmp <<'MNGR_GEN2_DHCP_UNIT'
{dhcp_unit_text}\
MNGR_GEN2_DHCP_UNIT
dnsmasq --test --conf-file=/tmp/mngr-slice-dhcp.conf.mngr-tmp
is_dhcp_changed=0
if ! cmp -s /tmp/mngr-slice-dhcp.conf.mngr-tmp {GEN2_DHCP_CONFIG_PATH}; then
    install -m 644 /tmp/mngr-slice-dhcp.conf.mngr-tmp {GEN2_DHCP_CONFIG_PATH}
    is_dhcp_changed=1
fi
if ! cmp -s /tmp/mngr-slice-dhcp.service.mngr-tmp {GEN2_DHCP_UNIT_PATH}; then
    install -m 644 /tmp/mngr-slice-dhcp.service.mngr-tmp {GEN2_DHCP_UNIT_PATH}
    systemctl daemon-reload
    is_dhcp_changed=1
fi
rm -f /tmp/mngr-slice-dhcp.conf.mngr-tmp /tmp/mngr-slice-dhcp.service.mngr-tmp
systemctl enable {GEN2_DHCP_UNIT_NAME}
if [ "$is_dhcp_changed" = 1 ]; then
    systemctl restart {GEN2_DHCP_UNIT_NAME}
else
    systemctl start {GEN2_DHCP_UNIT_NAME}
fi
"""


# Written next to the staged gen-2 image: the hash of the customization that
# produced it, so a re-prep re-stages exactly when the customization changed.
_GEN2_IMAGE_CUSTOMIZATION_MARKER_SUFFIX: Final[str] = ".customization-sha256"


@pure
def docker_apt_archive_url(apt_mirror_snapshot_timestamp: str) -> str:
    """The mirror's frozen copy of Docker's apt repo at a cut timestamp (apps/apt_mirror's ``docker`` archive)."""
    return snapshot_archive_url(APT_MIRROR_PUBLIC_BASE_URL, apt_mirror_snapshot_timestamp, DOCKER_ARCHIVE.name)


@pure
def render_docker_apt_source_section(apt_mirror_snapshot_timestamp: str) -> str:
    """The shell that points apt at the mirror's frozen docker archive, signed by the committed copy of Docker's key.

    The suite comes from the guest's own os-release (the archive freezes the
    trixie suite the guest image runs), and the indexes and signatures are
    served verbatim, so apt verifies them exactly as it would upstream's.
    Leaves ``os-release`` sourced for the caller's own pins.
    """
    archive_url = docker_apt_archive_url(apt_mirror_snapshot_timestamp)
    return f"""\
install -m 0755 -d /etc/apt/keyrings
cat > /etc/apt/keyrings/docker.asc <<'MNGR_DOCKER_APT_KEY'
{DOCKER_APT_SIGNING_KEY}
MNGR_DOCKER_APT_KEY
chmod a+r /etc/apt/keyrings/docker.asc
. /etc/os-release
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] {archive_url} ${{VERSION_CODENAME}} {DOCKER_ARCHIVE.required_component}" > /etc/apt/sources.list.d/docker.list
"""


@pure
def _render_gen2_guest_image_customize_script(apt_mirror_snapshot_timestamp: str) -> str:
    """The in-guest script ``virt-customize`` runs offline against the gen-2 base image.

    Docker comes from the mirror's frozen ``docker`` archive at the committed
    cut timestamp; gVisor comes from the mirror's copy of the pinned release.
    Nothing here reaches an upstream host.
    """
    return f"""\
set -eux
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl gnupg
{render_docker_apt_source_section(apt_mirror_snapshot_timestamp)}\
apt-get update
DOCKER_APT_VERSION="{PINNED_DOCKER_APT_VERSION_CORE}~${{ID}}.${{VERSION_ID}}~${{VERSION_CODENAME}}"
CONTAINERD_APT_VERSION="{PINNED_CONTAINERD_APT_VERSION_CORE}~${{ID}}.${{VERSION_ID}}~${{VERSION_CODENAME}}"
apt-get install -y --allow-downgrades docker-ce="${{DOCKER_APT_VERSION}}" docker-ce-cli="${{DOCKER_APT_VERSION}}" containerd.io="${{CONTAINERD_APT_VERSION}}" docker-buildx-plugin docker-compose-plugin inotify-tools
apt-get clean
rm -rf /var/lib/apt/lists/*
# The pinned gVisor runtime (the same install + sha512 verification the live
# VPS host setup runs, fetched from the mirror's copy of the release),
# registered with the Docker daemon by writing its config directly: the image
# is customized offline, so there is no dockerd for `runsc install` to consult.
# Every gen-2 workspace container is created under it from the bake
# (blueprint/slice-fleet-cutover/).
{render_gvisor_binary_install_script(GVISOR_MIRROR_RELEASE_URL)}
mkdir -p /etc/docker
cat > /etc/docker/daemon.json <<'MNGR_DOCKER_DAEMON_JSON'
{_GEN2_GUEST_DOCKER_DAEMON_JSON}
MNGR_DOCKER_DAEMON_JSON
# The packages enable docker and containerd at boot; the gen-2 guest must not
# start them before its first boot has mounted the data disk holding both
# roots, so the boot enablement is dropped here (no systemctl in the appliance)
# and the first-boot script enables + starts them once the disk is up. Later
# boots order them after the mount via the drop-ins below.
rm -f /etc/systemd/system/multi-user.target.wants/docker.service /etc/systemd/system/sockets.target.wants/docker.socket \\
    /etc/systemd/system/multi-user.target.wants/containerd.service
mkdir -p /etc/systemd/system/docker.service.d /etc/systemd/system/containerd.service.d /etc/systemd/journald.conf.d /etc/containerd
printf '%s' {shlex.quote(_GEN2_GUEST_ENGINE_DROP_IN)} > /etc/systemd/system/docker.service.d/mngr-data-root.conf
printf '%s' {shlex.quote(_GEN2_GUEST_ENGINE_DROP_IN)} > /etc/systemd/system/containerd.service.d/mngr-data-root.conf
printf '%s' {shlex.quote(_GEN2_GUEST_CONTAINERD_CONFIG)} > /etc/containerd/config.toml
printf '%s' {shlex.quote(_GEN2_GUEST_JOURNALD_CONF)} > /etc/systemd/journald.conf.d/mngr.conf
"""


@pure
def gen2_guest_image_customization_hash(slice_base_image_url: str, apt_mirror_snapshot_timestamp: str) -> str:
    """The content hash the gen-2 prep keys its staged guest image on: the image URL plus the customization."""
    customize_script = _render_gen2_guest_image_customize_script(apt_mirror_snapshot_timestamp)
    return hashlib.sha256(f"{slice_base_image_url}\n{customize_script}".encode()).hexdigest()


@pure
def build_gen2_box_prep_script(
    *,
    # The tier's SSH CA public key every management account on the box trusts
    # (no static key is authorized on a gen-2 box).
    ssh_ca_public_key: str,
    slice_base_image_url: str,
    # The sha512 the downloaded image must match before it is customized.
    slice_base_image_sha512: str,
    # The apt mirror cut (apps/apt_mirror/current-timestamp) whose frozen
    # docker archive the guest image installs the engine from.
    apt_mirror_snapshot_timestamp: str,
    wireguard_address: str,
    wireguard_listen_port: int,
    wireguard_operators: Sequence[WireguardOperatorConfig],
    # The tier's overlay allocation: the box's wg0 interface prefix and the
    # telemetry collector's management-source allowlist both derive from it.
    overlay: ManagementOverlayAllocation,
    # The tier's Modal Proxy static IPs for the :22 lockdown; empty converges
    # the box to open (no lockdown -- the tier has no proxy configured yet).
    management_proxy_static_ips: Sequence[str],
    # The box's declared uplink rate; sizes the telemetry collector's egress
    # signal and its link-speed audit.
    declared_uplink_mbps: int,
) -> str:
    """Render the idempotent root bash script that prepares a Debian 13 box to host gen-2 slices.

    The gen-2 sibling of :func:`build_box_prep_script` (specs/slice-fleet-gen2
    phase 3): raw qemu + OVMF + nftables + genisoimage + dnsmasq + WireGuard
    instead of lima, the ``GEN2_SLICE_SERVICE_USER`` service user
    (certificate-only management SSH: the tier CA is trusted and no static key
    is authorized) plus the pre-created per-slice unix users and the
    ``GEN2_DHCP_USER`` DHCP service user, the plugin-rendered prep artifacts
    (template unit / root helper / sudoers / the slice DHCP server's config,
    unit and udp/67 policy, content-converged), the LUKS storage volume (the
    storage partition formatted on first prep, TPM-enrolled, its passphrase
    verified and its header backup staged; the journal, the service user's
    home and both temp directories bind-mounted onto it), the staged trixie
    guest image on that volume (pinned docker + the pinned gVisor runtime
    baked in), the kvm nested-virtualization module pin, the shared swapfile /
    no-auto-reboot / transfer-tooling hardening, the management WireGuard
    bring-up (echoing the box's public key for the caller to stamp on the row),
    the ``:22`` lockdown when the tier's Modal Proxy IPs are configured, and
    the box telemetry collector + timer (phase 4) with its prep-artifact hash
    manifest.

    No boot-autostart unit: gen-2 boot autostart is exactly systemd's
    ``WantedBy=multi-user.target`` on each enabled ``mngr-slice@`` instance.
    Intended to be piped to ``sudo bash`` on the box.
    """
    apt_packages = " ".join(_GEN2_BOX_APT_PACKAGES)
    service_user = GEN2_SLICE_SERVICE_USER
    gen2_base_image_dir = posixpath.dirname(GEN2_BASE_IMAGE_PATH)
    image_cache_dir = box_image_cache_dir_for_generation(FIRST_QEMU_BOX_GENERATION, service_user)
    wireguard_section = render_wireguard_prep_section(
        wireguard_address=wireguard_address,
        listen_port=wireguard_listen_port,
        operators=wireguard_operators,
        overlay_prefix_length=overlay.overlay.prefixlen,
    )
    lockdown_section = render_management_lockdown_prep_section(management_proxy_static_ips)
    customize_script = _render_gen2_guest_image_customize_script(apt_mirror_snapshot_timestamp)
    customization_hash = gen2_guest_image_customization_hash(slice_base_image_url, apt_mirror_snapshot_timestamp)
    # The telemetry section must come after the prep artifacts and the WireGuard
    # config are installed: it records their installed hashes as the artifact
    # integrity check's expectation.
    telemetry_section = render_box_telemetry_prep_section(
        overlay_cidr=str(overlay.overlay),
        declared_uplink_mbps=declared_uplink_mbps,
        management_proxy_static_ips=management_proxy_static_ips,
    )
    ssh_ca_trust_section = render_gen2_ssh_ca_trust_section(ssh_ca_public_key, service_user)
    storage_encryption_section = render_gen2_storage_encryption_section()
    storage_relocation_section = render_gen2_storage_relocation_section()
    return f"""\
#!/bin/bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

# System packages for raw qemu slices, the management plane, and the bake tooling.
apt-get update -qq
apt-get install -y -qq {apt_packages}

# The template unit pins this OVMF pflash path (trixie ships only the _4M
# builds); fail loudly here if a future ovmf packaging change moves it (the
# constant in mngr_imbue_cloud's slices/gen2_scripts/layout.py then needs updating).
if [ ! -f {GEN2_OVMF_CODE_PATH} ]; then
    echo "ERROR: {GEN2_OVMF_CODE_PATH} not found after installing ovmf; the gen-2 unit's pflash path needs updating" >&2
    exit 1
fi

# Nested virtualization off at the module level (applies at the next box
# reboot; the module is never unloaded live -- it has VMs).
cat > {_GEN2_KVM_MODPROBE_CONF_PATH} <<'MNGR_KVM_CONF'
options kvm_intel nested=0
options kvm_amd nested=0
MNGR_KVM_CONF

{_render_service_user_section(service_user)}
{ssh_ca_trust_section}
# Per-slice unix users, pre-created so the template unit's User=mngr-slice-%i
# always resolves (the reserve script never allocates an ordinal at or above
# the bound). No home and no shell: each exists only to own one VM process.
for ordinal in $(seq 0 {GEN2_MAX_SLICE_COUNT - 1}); do
    slice_user="mngr-slice-$ordinal"
    if ! id "$slice_user" >/dev/null 2>&1; then
        useradd -M -s /usr/sbin/nologin "$slice_user"
    fi
    usermod -aG kvm "$slice_user"
done

{storage_encryption_section}
# The storage volume (the mounted LUKS mapper) must be XFS: everything
# gen-2 lives under it (base image, per-instance dirs, ordinal links) and the
# carves depend on reflink copies.
storage_fstype=$(findmnt -no FSTYPE --target {GEN2_STORAGE_ROOT})
if [ "$storage_fstype" != "xfs" ]; then
    echo "ERROR: {GEN2_STORAGE_ROOT} is $storage_fstype, expected xfs (reflink-instant slice carves)" >&2
    exit 1
fi
{storage_relocation_section}
chown {service_user}:{service_user} {GEN2_STORAGE_ROOT}
chmod 751 {GEN2_STORAGE_ROOT}
install -d -o {service_user} -g {service_user} -m 751 {GEN2_INSTANCES_DIR} {GEN2_BY_ORDINAL_DIR} {gen2_base_image_dir}

{_render_retired_gen2_service_user_section(service_user)}
{_render_transfer_tools_section(_GEN2_INSTALL_MARKER_DIR)}
{_render_gen2_prep_artifacts_section()}
{_render_gen2_dhcp_server_section()}
# Stage + customize the golden trixie guest image (idempotent; same
# atomic-publish flow as gen-1). The image comes from the artifact mirror and
# must match its recorded sha512 before anything runs against it. The docker
# pin's distro suffix is derived from the image's own os-release, so the
# identical pinned engine lands on trixie guests. The staged image is keyed on
# a content hash of its URL plus the customization script: a changed pin,
# mirror cut, or daemon config re-stages on the next prep, and an unchanged
# one touches nothing. Replacing the base is safe under live slices -- every
# carve reflink-copies it, so no VM reads it.
img={GEN2_BASE_IMAGE_PATH}
img_marker="$img{_GEN2_IMAGE_CUSTOMIZATION_MARKER_SUFFIX}"
if [ ! -f "$img" ] || [ "$(cat "$img_marker" 2>/dev/null)" != "{customization_hash}" ]; then
    curl -fsSL --retry 8 --retry-delay 15 --retry-all-errors --retry-connrefused -o "$img.tmp" {slice_base_image_url}
    echo "{slice_base_image_sha512}  $img.tmp" | sha512sum -c -
    qemu-img info "$img.tmp" >/dev/null
    cat > /tmp/mngr-slice-image-customize.sh <<'MNGR_SLICE_CUSTOMIZE'
{customize_script}\
MNGR_SLICE_CUSTOMIZE
    virt-customize -a "$img.tmp" --network --run /tmp/mngr-slice-image-customize.sh
    rm -f /tmp/mngr-slice-image-customize.sh
    chown {service_user}:{service_user} "$img.tmp"
    chmod 640 "$img.tmp"
    mv "$img.tmp" "$img"
    printf '%s\\n' "{customization_hash}" > "$img_marker"
fi

# The per-box DEFAULT_WORKSPACE_TEMPLATE image cache dir (owned by the service
# user) on the storage partition, where the box keeps the ``docker save`` tar(s)
# slices ``docker load`` instead of rebuilding.
install -d -m 755 -o {service_user} -g {service_user} {image_cache_dir}

# The nbd module for the cutover's box-side gen-1 data-disk transplant
# (qemu-nbd attaches the qcow2 images); max_part so the partitioned gen-1
# disk exposes its partition node.
# CLEANUP: drop with btrfs-progs once the gen-1 -> gen-2 cutover has run on
# every tier (phase 6 of blueprint/slice-fleet-cutover).
echo nbd > /etc/modules-load.d/mngr-nbd.conf
echo "options nbd max_part=16" > /etc/modprobe.d/mngr-nbd.conf
modprobe nbd max_part=16

{_render_swapfile_section(GEN2_SWAPFILE_PATH)}
{_render_swap_partition_retirement_section(GEN2_SWAPFILE_PATH)}
{_render_no_auto_reboot_section()}
{wireguard_section}
{lockdown_section}
{telemetry_section}
# The storage partition's whole size, for the caller to record as the box's
# disk_gb (the gen-2 disk budget is computed from the measured partition).
echo "{GEN2_STORAGE_PARTITION_MARKER} $(( $({build_storage_partition_size_bytes_command()}) / 1073741824 ))"
echo MNGR_BOX_PREP_DONE
"""


@pure
def build_storage_partition_size_bytes_command() -> str:
    """Print the mounted gen-2 storage partition's whole size in bytes (the figure ``disk_gb`` is recorded from)."""
    return f"df --output=size -B1 {GEN2_STORAGE_ROOT} | tail -1 | tr -d ' '"


@pure
def parse_storage_partition_gib_from_prep_output(stdout: str) -> int | None:
    """Extract the storage partition's whole GiB from a gen-2 prep run's marker line."""
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith(GEN2_STORAGE_PARTITION_MARKER):
            parts = stripped.split()
            if len(parts) == 2 and parts[1].isdigit():
                return int(parts[1])
    return None
