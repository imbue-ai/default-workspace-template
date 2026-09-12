"""``minds-admin server ...`` -- operator-only bare-metal fleet management.

Manages the OVH bare-metal servers we rent (the lima-VM "slices" we carve on them
are baked via ``minds-admin pool create``, whose shared implementation
lives here as :func:`allocate_slices`). Writes the connector's host_pool Neon DB
directly (laptop-side), mirroring ``minds-admin pool create``; the connector only reads
these rows (plus its release-time teardown). Every step is resumable: ordering and
OS install can take a long time, and re-running advances a box one step. The
OVH-touching steps act on the real account and are validated against a delivered
box; ``list`` / ``register`` are exercised without OVH.
"""

import base64
import json
import os
import secrets
import shlex
import signal
import tempfile
import threading
from collections.abc import Callable
from collections.abc import Iterator
from collections.abc import Mapping
from collections.abc import Sequence
from contextlib import contextmanager
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import AbstractSet
from typing import Any
from typing import Final
from typing import TypeVar
from urllib.parse import urlencode
from uuid import uuid4

import click
import psutil
import psycopg2
from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr
from tabulate import tabulate

from imbue.apt_mirror.cli import CURRENT_TIMESTAMP_PATH
from imbue.apt_mirror.cli import read_current_timestamp
from imbue.concurrency_group.concurrency_group import ConcurrencyExceptionGroup
from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.concurrency_group import ObservableThread
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.minds.config.data_types import ManagementOverlayAllocation
from imbue.minds.config.data_types import ManagementWireguardConfig
from imbue.minds.config.data_types import management_overlay_for_tier
from imbue.minds.config.loader import load_deploy_config
from imbue.minds.envs.paths import active_env_name_or_none
from imbue.minds_admin.bake.content_tag import DEFAULT_WORKSPACE_TEMPLATE_IMAGE_REPOSITORY
from imbue.minds_admin.bake.content_tag import compute_content_addressed_cache_tag
from imbue.minds_admin.bake.pool_bake import BAKED_SERVICES_AGENT_NAME
from imbue.minds_admin.bake.pool_bake import BAKED_SERVICES_CHECKOUT_PATH
from imbue.minds_admin.bake.pool_bake import BakedPoolHost
from imbue.minds_admin.bake.pool_bake import PoolBakeError
from imbue.minds_admin.bake.pool_bake import bake_pool_host
from imbue.minds_admin.bake.pool_bake import ephemeral_bake_namespace
from imbue.minds_admin.bake.pool_bake import finalize_baked_pool_host
from imbue.minds_admin.bake.pool_bake import sweep_stale_bake_namespaces
from imbue.minds_admin.bake.pool_bake import sync_mngr_into_template
from imbue.minds_admin.bake.pool_bake import verify_only_primary_agents_baked
from imbue.minds_admin.bake.pool_bake import wait_for_env_converge
from imbue.minds_admin.cli._tier_secrets import DATABASE_URL_HELP
from imbue.minds_admin.cli._tier_secrets import make_admin_connector_client
from imbue.minds_admin.cli._tier_secrets import make_workspace_storage_s3_client
from imbue.minds_admin.cli._tier_secrets import read_box_storage_passphrase_or_none
from imbue.minds_admin.cli._tier_secrets import resolve_boxes_collector_install_config_or_none
from imbue.minds_admin.cli._tier_secrets import resolve_management_plane_config_or_none
from imbue.minds_admin.cli._tier_secrets import resolve_ovh_config
from imbue.minds_admin.cli._tier_secrets import resolve_pool_database_url
from imbue.minds_admin.cli._tier_secrets import resolve_pool_private_key_pem
from imbue.minds_admin.cli._tier_secrets import resolve_workspace_storage_config
from imbue.minds_admin.cli._tier_secrets import write_box_storage_passphrase
from imbue.minds_admin.cli.paid import paid_auth_options
from imbue.minds_admin.cli.paid import resolve_admin_api_key
from imbue.minds_admin.primitives import SLICE_PROVIDER_INSTANCE_NAME
from imbue.minds_admin.slices.bare_metal_db import POOL_HOST_STATUS_BAKING
from imbue.minds_admin.slices.bare_metal_db import POOL_HOST_STATUS_LEASED
from imbue.minds_admin.slices.bare_metal_db import build_baking_slice_pool_host_insert_values
from imbue.minds_admin.slices.bare_metal_db import claim_pool_host_for_removal
from imbue.minds_admin.slices.bare_metal_db import delete_baking_slice_pool_host
from imbue.minds_admin.slices.bare_metal_db import delete_pool_host_row
from imbue.minds_admin.slices.bare_metal_db import destroy_eligible_pool_host_statuses
from imbue.minds_admin.slices.bare_metal_db import fetch_machine_usage_on_server
from imbue.minds_admin.slices.bare_metal_db import fetch_pool_host_destroy_target
from imbue.minds_admin.slices.bare_metal_db import fetch_pool_host_ids_on_server_by_status
from imbue.minds_admin.slices.bare_metal_db import fetch_pool_host_status
from imbue.minds_admin.slices.bare_metal_db import fetch_server_by_id
from imbue.minds_admin.slices.bare_metal_db import fetch_server_capacities
from imbue.minds_admin.slices.bare_metal_db import fetch_servers
from imbue.minds_admin.slices.bare_metal_db import fetch_slice_disk_names_for_server
from imbue.minds_admin.slices.bare_metal_db import fetch_slice_instance_names_for_server
from imbue.minds_admin.slices.bare_metal_db import fetch_unleased_slice_teardown_row_ids
from imbue.minds_admin.slices.bare_metal_db import finish_baking_slice_pool_host
from imbue.minds_admin.slices.bare_metal_db import insert_baking_slice_pool_host
from imbue.minds_admin.slices.bare_metal_db import insert_bare_metal_server
from imbue.minds_admin.slices.bare_metal_db import update_server
from imbue.minds_admin.slices.bare_metal_db import upsert_bare_metal_server
from imbue.minds_admin.slices.bare_metal_prep import DEFAULT_GEN2_SLICE_GUEST_IMAGE_SHA512
from imbue.minds_admin.slices.bare_metal_prep import DEFAULT_GEN2_SLICE_GUEST_IMAGE_URL
from imbue.minds_admin.slices.bare_metal_prep import DEFAULT_LIMA_VERSION
from imbue.minds_admin.slices.bare_metal_prep import build_box_prep_script
from imbue.minds_admin.slices.bare_metal_prep import build_gen2_box_prep_script
from imbue.minds_admin.slices.bare_metal_prep import parse_storage_partition_gib_from_prep_output
from imbue.minds_admin.slices.box_access import BoxManagementDial
from imbue.minds_admin.slices.box_access import MANAGEMENT_SSH_PORT
from imbue.minds_admin.slices.box_access import close_box_management_tunnels
from imbue.minds_admin.slices.box_access import resolve_box_management_dial
from imbue.minds_admin.slices.box_access import resolve_server_management_dial
from imbue.minds_admin.slices.ci_slice_sweep import CiSliceSweepBoxReport
from imbue.minds_admin.slices.ci_slice_sweep import CiSliceSweepReport
from imbue.minds_admin.slices.ci_slice_sweep import DEFAULT_CI_SLICE_MAX_AGE_HOURS
from imbue.minds_admin.slices.ci_slice_sweep import sweep_ci_slices_on_box
from imbue.minds_admin.slices.cutover_types import bake_tag_generation_error_or_none
from imbue.minds_admin.slices.management_plane import parse_wireguard_public_key_from_prep_output
from imbue.minds_admin.slices.management_plane import resolve_box_overlay_address
from imbue.minds_admin.slices.operator_identity import ManagementIdentityResolver
from imbue.minds_admin.slices.operator_identity import management_identities
from imbue.minds_admin.slices.ordering import DEFAULT_REINSTALL_OS_TEMPLATE
from imbue.minds_admin.slices.ordering import GEN2_REINSTALL_OS_TEMPLATE
from imbue.minds_admin.slices.ordering import build_and_assign_eco_cart
from imbue.minds_admin.slices.ordering import build_gen2_reinstall_storage
from imbue.minds_admin.slices.ordering import checkout_eco_cart
from imbue.minds_admin.slices.ordering import delete_cart_quietly
from imbue.minds_admin.slices.ordering import derive_server_specs
from imbue.minds_admin.slices.ordering import derive_uplink_mbps_from_option_codes
from imbue.minds_admin.slices.ordering import start_os_reinstall
from imbue.minds_admin.slices.ordering import summarize_checkout_prices
from imbue.minds_admin.slices.ordering import wait_for_dedicated_server_address
from imbue.minds_admin.slices.ordering import wait_for_order_service_name
from imbue.minds_admin.slices.ordering import wait_for_os_reinstall
from imbue.minds_admin.slices.pricing import compute_slice_pricing_rows
from imbue.minds_admin.slices.storage_encryption import STORAGE_UNLOCKED_MARKER
from imbue.minds_admin.slices.storage_encryption import parse_storage_encryption_from_prep_output
from imbue.minds_admin.slices.storage_encryption import render_storage_header_backup_fetch_script
from imbue.minds_admin.slices.storage_encryption import render_storage_passphrase_cleanup_script
from imbue.minds_admin.slices.storage_encryption import render_storage_passphrase_staging_script
from imbue.minds_admin.slices.storage_encryption import render_storage_unlock_script
from imbue.minds_admin.slices.storage_encryption import storage_header_backup_object_key
from imbue.minds_admin.slices.storage_header_backup import upload_storage_header_backup
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.errors import MngrError
from imbue.mngr.primitives import HostId
from imbue.mngr.providers.ssh_utils import add_host_to_known_hosts
from imbue.mngr.providers.ssh_utils import generate_ed25519_host_keypair
from imbue.mngr.utils.interactive_subprocess import run_interactive_subprocess
from imbue.mngr.utils.polling import poll_for_value
from imbue.mngr.utils.ssh import quote_ssh_option_value
from imbue.mngr_imbue_cloud.cli._common import emit_json
from imbue.mngr_imbue_cloud.data_types import BareMetalServer
from imbue.mngr_imbue_cloud.data_types import BareMetalServerCapacity
from imbue.mngr_imbue_cloud.data_types import BoxManagementTrust
from imbue.mngr_imbue_cloud.data_types import BoxTierAudit
from imbue.mngr_imbue_cloud.data_types import BoxTierAuditReport
from imbue.mngr_imbue_cloud.data_types import OrphanReapReport
from imbue.mngr_imbue_cloud.data_types import PoolHostDestroyOutcome
from imbue.mngr_imbue_cloud.data_types import PoolHostDestroyReport
from imbue.mngr_imbue_cloud.data_types import SliceBakeOutcome
from imbue.mngr_imbue_cloud.data_types import SliceBakeReport
from imbue.mngr_imbue_cloud.data_types import SlicePricingRow
from imbue.mngr_imbue_cloud.data_types import StorageVolumeState
from imbue.mngr_imbue_cloud.data_types import UnauditedBox
from imbue.mngr_imbue_cloud.data_types import WarmCacheReport
from imbue.mngr_imbue_cloud.errors import BareMetalConfigError
from imbue.mngr_imbue_cloud.errors import BareMetalProvisioningError
from imbue.mngr_imbue_cloud.errors import ImbueCloudConnectorError
from imbue.mngr_imbue_cloud.errors import SliceBakeTerminatedError
from imbue.mngr_imbue_cloud.errors import SliceCommandError
from imbue.mngr_imbue_cloud.interfaces import SliceVmClientInterface
from imbue.mngr_imbue_cloud.primitives import BareMetalServerDbId
from imbue.mngr_imbue_cloud.primitives import BareMetalServerStatus
from imbue.mngr_imbue_cloud.primitives import DEV_TIER
from imbue.mngr_imbue_cloud.primitives import OVH_US_DATACENTER_CODES
from imbue.mngr_imbue_cloud.primitives import PoolHostDestroyOutcomeStatus
from imbue.mngr_imbue_cloud.primitives import SERVER_STATUS_DELIVERED
from imbue.mngr_imbue_cloud.primitives import SERVER_STATUS_DRAINING
from imbue.mngr_imbue_cloud.primitives import SERVER_STATUS_INSTALLING
from imbue.mngr_imbue_cloud.primitives import SERVER_STATUS_ORDERED
from imbue.mngr_imbue_cloud.primitives import SERVER_STATUS_READY
from imbue.mngr_imbue_cloud.primitives import SliceBakeOutcomeStatus
from imbue.mngr_imbue_cloud.primitives import SliceContainerRuntime
from imbue.mngr_imbue_cloud.primitives import US_REGION_BY_OVH_DATACENTER_CODE
from imbue.mngr_imbue_cloud.primitives import is_box_exclusive_to_tier
from imbue.mngr_imbue_cloud.primitives import tier_for_env_name
from imbue.mngr_imbue_cloud.slices.bare_metal import DEFAULT_MEMORY_PER_SLICE_GB
from imbue.mngr_imbue_cloud.slices.bare_metal import DEFAULT_SLICE_CPU_OVERCOMMIT_RATIO
from imbue.mngr_imbue_cloud.slices.bare_metal import GEN2_CONTAINER_RUNTIME
from imbue.mngr_imbue_cloud.slices.bare_metal import GEN2_CONTAINER_TMPFS_START_ARGS
from imbue.mngr_imbue_cloud.slices.bare_metal import ORPHAN_SLICE_MIN_AGE_SECONDS
from imbue.mngr_imbue_cloud.slices.bare_metal import assert_env_name_fits_slice_names
from imbue.mngr_imbue_cloud.slices.bare_metal import assert_gen2_box_disk_fits_default_machines
from imbue.mngr_imbue_cloud.slices.bare_metal import box_image_cache_dir_for_generation
from imbue.mngr_imbue_cloud.slices.bare_metal import box_service_user
from imbue.mngr_imbue_cloud.slices.bare_metal import build_read_storage_volume_command
from imbue.mngr_imbue_cloud.slices.bare_metal import compute_gen2_box_default_machine_fit
from imbue.mngr_imbue_cloud.slices.bare_metal import compute_orphan_slice_disk_names
from imbue.mngr_imbue_cloud.slices.bare_metal import compute_orphan_slice_instance_names
from imbue.mngr_imbue_cloud.slices.bare_metal import compute_slice_disk_gib
from imbue.mngr_imbue_cloud.slices.bare_metal import compute_slice_memory_mib
from imbue.mngr_imbue_cloud.slices.bare_metal import compute_slice_vcpus
from imbue.mngr_imbue_cloud.slices.bare_metal import compute_slot_count
from imbue.mngr_imbue_cloud.slices.bare_metal import count_slice_resource_names
from imbue.mngr_imbue_cloud.slices.bare_metal import default_slice_service_user
from imbue.mngr_imbue_cloud.slices.bare_metal import describe_gen2_box_disk_shortfall
from imbue.mngr_imbue_cloud.slices.bare_metal import docker_runtime_name
from imbue.mngr_imbue_cloud.slices.bare_metal import expected_static_authorized_key_count
from imbue.mngr_imbue_cloud.slices.bare_metal import find_server_capacity_by_id
from imbue.mngr_imbue_cloud.slices.bare_metal import foreign_tier_slice_names
from imbue.mngr_imbue_cloud.slices.bare_metal import is_slice_owned_by_env
from imbue.mngr_imbue_cloud.slices.bare_metal import is_trusted_ca_correct_for_tier
from imbue.mngr_imbue_cloud.slices.bare_metal import parse_degraded_md_arrays
from imbue.mngr_imbue_cloud.slices.bare_metal import parse_raw_swap_devices
from imbue.mngr_imbue_cloud.slices.bare_metal import parse_storage_volume_output
from imbue.mngr_imbue_cloud.slices.bare_metal import slice_disk_name
from imbue.mngr_imbue_cloud.slices.bare_metal import slice_instance_name
from imbue.mngr_imbue_cloud.slices.box_image_cache import BoxImageCacheInterface
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import DEFAULT_SLICE_PORT_RANGE_END
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import DEFAULT_SLICE_PORT_RANGE_START
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import FIRST_QEMU_BOX_GENERATION
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_SLICE_SERVICE_USER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_STORAGE_LUKS_MAPPER_NAME
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_STORAGE_LUKS_MAPPER_PATH
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_STORAGE_PARTITION_MARKER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import DEFAULT_MACHINE_UNITS
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import GEN2_BOOT_DISK_GIB
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import MACHINE_UNITS_STEP
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import MAX_MACHINE_UNITS
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import compute_box_total_units
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import compute_gen1_migrated_data_disk_gib
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import compute_gen2_disk_budget_gib
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import compute_machine_data_disk_gib
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import compute_machine_guest_memory_mib
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import compute_machine_memory_footprint_mib
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import compute_machine_vcpus
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import is_allowed_machine_units
from imbue.mngr_imbue_cloud.slices.slice_client import build_slice_vm_client
from imbue.mngr_imbue_cloud.slices.ssh_box_image_cache import SshBoxImageCache
from imbue.mngr_lima.constants import DEFAULT_IMAGE_URL_X86_64
from imbue.mngr_lima.errors import LimaCommandError
from imbue.mngr_ovh.client import build_ovh_client
from imbue.mngr_vps.primitives import VpsInstanceId
from imbue.observability.collector_install import render_collector_install_script


def _format_gen2_capacity(server: BareMetalServer, usage: tuple[int, int]) -> str:
    """A gen-2 box's capacity cell: used/total units and used/budget disk (specs/slice-fleet)."""
    used_units, used_disk_gb = usage
    total_units = compute_box_total_units(server.ram_gb) if server.ram_gb is not None else 0
    disk_budget_gib = compute_gen2_disk_budget_gib(server.disk_gb) if server.disk_gb is not None else 0
    return f"{used_units}/{total_units}u {used_disk_gb}/{disk_budget_gib}G"


def _format_capacity_table(
    capacities: list[BareMetalServerCapacity],
    # Per-box (units, boot+data disk GB) usage for the gen-2 rows (unit-based
    # capacity display); gen-1 rows keep the slot display.
    machine_usage_by_server_id: dict[str, tuple[int, int]],
) -> str:
    """Render the server capacity table (one row per box + a fleet total)."""
    header = (
        f"{'ID':<38}{'PLAN':<20}{'REGION':<8}{'STATUS':<12}{'ADDRESS':<18}{'GEN':>4}{'UPLINK':>8}"
        f"{'CAPACITY(used/total)':>24}"
    )
    lines = [header]
    total_slots = 0
    total_used = 0
    for capacity in capacities:
        server = capacity.server
        total_slots += server.slot_count
        total_used += capacity.used_slots
        usage = machine_usage_by_server_id.get(str(server.id))
        if server.box_generation >= FIRST_QEMU_BOX_GENERATION and usage is not None:
            capacity_cell = _format_gen2_capacity(server, usage)
        else:
            capacity_cell = f"{capacity.used_slots}/{server.slot_count} slots"
        lines.append(
            f"{str(server.id):<38}{server.plan_code[:19]:<20}{server.region[:7]:<8}"
            f"{str(server.status):<12}{str(server.public_address or '-')[:17]:<18}"
            f"{server.box_generation:>4}{f'{server.uplink_mbps}M':>8}{capacity_cell:>24}"
        )
    lines.append(
        f"\nFLEET: {len(capacities)} servers, {total_used}/{total_slots} slots used, {total_slots - total_used} free"
    )
    return "\n".join(lines)


@click.group(name="server")
def server() -> None:
    """Bare-metal server fleet management for the activated minds env (pricing / order / setup / prep / list / ...)."""


@contextmanager
def box_management_identities(
    # Resolves the gen-1 pool key PEM when a gen-1 box is dialed; the default is
    # the activated tier's Vault entry (or the POOL_SSH_PRIVATE_KEY override).
    resolve_gen1_pool_private_key_pem: Callable[[], str] = resolve_pool_private_key_pem,
) -> Iterator[ManagementIdentityResolver]:
    """The management keys this command dials boxes with: the operator's Vault-signed certificate for gen-2, the pool key for gen-1."""
    env_name = active_env_name_or_none()
    tier = tier_for_env_name(env_name) if env_name is not None else None
    with management_identities(
        tier=tier, resolve_gen1_pool_private_key_pem=resolve_gen1_pool_private_key_pem
    ) as identities:
        yield identities


def resolve_tier_ssh_ca_public_key_or_none() -> str | None:
    """The activated tier's committed SSH CA public key (deploy.toml ``[ssh_ca]``), or None when absent."""
    env_name = active_env_name_or_none()
    if env_name is None:
        return None
    ssh_ca = load_deploy_config(tier_for_env_name(env_name)).ssh_ca
    return str(ssh_ca.public_key) if ssh_ca is not None else None


def require_tier_ssh_ca_public_key(purpose: str) -> str:
    """The activated tier's SSH CA public key, refusing ``purpose`` with the bring-up pointer when it is not committed."""
    ca_public_key = resolve_tier_ssh_ca_public_key_or_none()
    if ca_public_key is None:
        env_name = active_env_name_or_none()
        tier_hint = f"tier '{tier_for_env_name(env_name)}'" if env_name is not None else "an activated env's tier"
        raise BareMetalProvisioningError(
            f"{purpose} needs the tier's SSH CA public key, but {tier_hint} has no [ssh_ca] block in its "
            "deploy.toml. Bring the tier's Vault SSH CA up and commit its public key first "
            "(apps/minds/docs/deploy/setup/tier-bringup.md)."
        )
    return ca_public_key


def derive_ssh_public_key(private_key_path: Path) -> str:
    """Derive the OpenSSH public key from a private key file via ssh-keygen -y."""
    cg = ConcurrencyGroup(name="ssh-keygen")
    with cg:
        result = cg.run_process_to_completion(
            command=["ssh-keygen", "-y", "-f", str(private_key_path)],
            timeout=30.0,
            is_checked_after=False,
        )
    if result.returncode != 0:
        raise BareMetalProvisioningError(f"ssh-keygen -y failed: {result.stderr.strip()}")
    return result.stdout.strip()


# Hard timeout for the box-prep SSH script. Generous because prep does heavy,
# network-bound one-time work: apt installs (incl. libguestfs-tools), the lima
# download, the multi-hundred-MB guest-image download, and the virt-customize pass
# that boots an appliance and apt-installs pinned Docker into the image.
_BOX_PREP_SSH_TIMEOUT_SECONDS: Final[float] = 1800.0
# How long the setup-to-ready path waits for the reinstalled box's sshd before prepping it.
DEFAULT_SETUP_SSH_READY_TIMEOUT_SECONDS: Final[float] = 900.0


@contextmanager
def _box_ssh_host_key_options(server_address: str, port: int, box_host_public_key: str) -> Iterator[list[str]]:
    """Yield ssh ``-o`` options that strictly pin the box's recorded host key.

    Every box SSH pins the box's sshd host key -- there is no trust-on-first-use
    fallback. The key is injected by us at OS reinstall (``server setup``) and
    recorded on the ``bare_metal_servers`` row, or captured once by the sanctioned
    ``minds-admin pool backfill-host-keys`` keyscan; callers fail closed when it is
    absent rather than reaching this helper.
    """
    if not box_host_public_key:
        raise BareMetalProvisioningError(
            f"no recorded box host key to pin for {server_address}; refusing to SSH without strict host-key "
            "checking (run `minds-admin server setup` or the one-time `minds-admin pool backfill-host-keys` first)"
        )
    known_hosts_fd, known_hosts_path = tempfile.mkstemp(prefix="mngr_box_known_hosts_")
    os.close(known_hosts_fd)
    try:
        add_host_to_known_hosts(Path(known_hosts_path), server_address, port, box_host_public_key)
        yield [
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={quote_ssh_option_value(known_hosts_path)}",
        ]
    finally:
        Path(known_hosts_path).unlink(missing_ok=True)


@pure
def box_script_provisioning_error_or_none(group: ConcurrencyExceptionGroup) -> BareMetalProvisioningError | None:
    """The provisioning error a box-script round trip raised inside its concurrency group, if that is all it holds.

    The SSH runner opens a concurrency group per round trip, so a refused
    connection at the copy step surfaces as a group wrapping the
    ``BareMetalProvisioningError``; callers catch the plain error, so the
    runner hands the wrapped one back.
    """
    if isinstance(group.main_exception, BareMetalProvisioningError):
        return group.main_exception
    if len(group.exceptions) == 1 and isinstance(group.exceptions[0], BareMetalProvisioningError):
        return group.exceptions[0]
    return None


def run_root_script_over_ssh(
    server_address: str,
    port: int,
    ssh_user: str,
    private_key_path: Path,
    script: str,
    box_host_public_key: str,
    *,
    # How long the script may run; the default is sized for the box prep.
    run_timeout_seconds: float = _BOX_PREP_SSH_TIMEOUT_SECONDS,
    # Bytes handed to the script on its stdin (a secret that must never ride in
    # argv or in the script file); None leaves stdin closed.
    stdin_bytes: bytes | None = None,
    # Whether every stdout line is echoed into the log as it arrives; off for
    # runs whose output is a payload to parse (a base64 header backup) rather
    # than progress to watch.
    is_output_logged: bool = True,
) -> str:
    """Copy a bash script to the box over scp, then run it with ``sudo bash``.

    The script travels as a file rather than on the ssh command line: the
    rendered gen-2 prep script (512 per-ordinal sudoers grants) exceeds the
    kernel's per-argument size limit (MAX_ARG_STRLEN, 128KiB), so embedding it
    in argv fails with "Argument list too long" on either endpoint.

    The script is staged on the box's tmpfs (``/dev/shm``) rather than under
    ``/tmp``: the gen-2 prep bind-mounts the encrypted volume's tmp directory
    over ``/tmp`` partway through, which would shadow a script staged there so
    the trailing removal misses it and leaves the file (it carries the
    telemetry collector's ingest credential) on the plaintext root partition.

    Returns the run's full stdout, so callers can parse marker lines (e.g. the
    gen-2 prep's ``MNGR_WIREGUARD_PUBLIC_KEY`` echo).
    """
    remote_script_path = f"/dev/shm/mngr-box-script-{uuid4().hex}.sh"
    # Removal always runs, and the exit status of the script itself is preserved.
    remote = f"sudo bash {remote_script_path}; status=$?; rm -f {remote_script_path}; exit $status"
    cg = ConcurrencyGroup(name="box-prep-ssh")
    with _box_ssh_host_key_options(server_address, port, box_host_public_key) as host_key_opts:
        with tempfile.NamedTemporaryFile("w", prefix="mngr_box_script_", suffix=".sh") as local_script:
            local_script.write(script)
            local_script.flush()
            try:
                with cg:
                    copy_result = cg.run_process_to_completion(
                        command=[
                            "scp",
                            "-P",
                            str(port),
                            "-i",
                            str(private_key_path),
                            *host_key_opts,
                            "-o",
                            "ConnectTimeout=30",
                            local_script.name,
                            f"{ssh_user}@{server_address}:{remote_script_path}",
                        ],
                        timeout=_BOX_PREP_SSH_TIMEOUT_SECONDS,
                        is_checked_after=False,
                    )
                    if copy_result.returncode != 0:
                        raise BareMetalProvisioningError(
                            f"copying the box script to {server_address} failed (exit {copy_result.returncode}): "
                            f"{copy_result.stderr.strip()}"
                        )
                    result = cg.run_process_to_completion(
                        command=[
                            "ssh",
                            "-p",
                            str(port),
                            "-i",
                            str(private_key_path),
                            *host_key_opts,
                            "-o",
                            "ConnectTimeout=30",
                            f"{ssh_user}@{server_address}",
                            remote,
                        ],
                        timeout=run_timeout_seconds,
                        is_checked_after=False,
                        on_output=(lambda line, _is_stdout: logger.info("  [box] {}", line.rstrip()))
                        if is_output_logged
                        else None,
                        stdin_bytes=stdin_bytes,
                    )
            except ConcurrencyExceptionGroup as exc:
                provisioning_error = box_script_provisioning_error_or_none(exc)
                if provisioning_error is None:
                    raise
                raise provisioning_error from exc
    if result.returncode != 0:
        raise BareMetalProvisioningError(
            f"the box script on {server_address} failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout


# Appended to the composed prep whenever the collector install is included, so
# a collector that installed but did not come up fails the prep loudly (in the
# same pinned-host-key SSH session) instead of leaving a silently dark box.
_COLLECTOR_VERIFICATION_SCRIPT: Final[str] = """\
# Verify the observability collector actually came up. A tier with a boxes
# ingest credential is fail-closed on the collector: an inactive unit fails
# the whole prep (and `server setup` then refuses to mark the box ready).
if ! systemctl is-active otelcol-contrib; then
    echo "otelcol-contrib is not active after the collector install; failing the prep" >&2
    exit 1
fi
"""


@pure
def compose_box_prep_script(
    *,
    base_script: str,
    # The rendered observability collector install, or None when the tier has
    # no boxes ingest credential (clean skip: no install, no verification).
    collector_install_script: str | None,
    # The --extra-prep-script escape hatch's content (idempotent, root,
    # rendered by its owner), or None when the flag was not passed.
    extra_prep_script_text: str | None,
) -> str:
    """Compose the full box prep: base steps, collector install, extra script, collector verification.

    Everything runs in one pinned-host-key ``sudo bash`` SSH session, in that
    order; the verification step is included only when the collector install
    is (there is no unit to verify otherwise).
    """
    script_parts = [base_script]
    if collector_install_script is not None:
        script_parts.append(collector_install_script)
    if extra_prep_script_text is not None:
        script_parts.append(extra_prep_script_text)
    if collector_install_script is not None:
        script_parts.append(_COLLECTOR_VERIFICATION_SCRIPT)
    return "\n".join(script_parts)


def _compose_prep_with_tier_collector(base_script: str, extra_prep_script: Path | None) -> str:
    """Compose a generation's base prep with the tier collector + extra script + verification.

    Resolves the activated tier's box observability collector in-process (see
    :func:`resolve_boxes_collector_install_config_or_none`): a missing/empty
    ingest credential skips the collector cleanly, a present one makes the
    composed prep fail-closed on it.
    """
    collector_config = resolve_boxes_collector_install_config_or_none()
    if collector_config is not None:
        logger.info(
            "Including the observability collector install in the box prep (tier '{}', ingest {})",
            collector_config.tier,
            collector_config.ingest_url,
        )
    collector_install_script = (
        render_collector_install_script(collector_config) if collector_config is not None else None
    )
    return compose_box_prep_script(
        base_script=base_script,
        collector_install_script=collector_install_script,
        extra_prep_script_text=extra_prep_script.read_text() if extra_prep_script is not None else None,
    )


def _build_composed_prep_script(
    *,
    pool_public_key: str,
    slice_service_user: str,
    lima_version: str,
    slice_base_image_url: str,
    extra_prep_script: Path | None,
) -> str:
    """Build the composed gen-1 prep script `prep` and `setup` share (base + collector + extra + verification)."""
    base_script = build_box_prep_script(
        pool_public_key=pool_public_key,
        slice_service_user=slice_service_user,
        lima_version=lima_version,
        slice_base_image_url=slice_base_image_url,
    )
    return _compose_prep_with_tier_collector(base_script, extra_prep_script)


def _ensure_box_wireguard_address(dsn: str, server: BareMetalServer, allocation: ManagementOverlayAllocation) -> str:
    """Return the box's management overlay address, assigning + stamping one when unset or out of plan.

    Sequential from the overlay's box block, across every recorded box (any
    status -- a draining box keeps its address until its row is deleted, so a
    repaved box may come back with a different one; nothing pins addresses to
    hardware). A stamped address outside the tier's current allocation is
    renumbered here, so a re-prep converges the fleet after an addressing-plan
    change; the box's wg0 picks the new address up in the same prep (which may
    drop the prep's own overlay-riding session mid-run -- re-run to converge).
    """
    conn = psycopg2.connect(dsn)
    try:
        assigned_addresses = {row.wireguard_address for row in fetch_servers(conn) if row.wireguard_address}
    finally:
        conn.close()
    wireguard_address = resolve_box_overlay_address(server.wireguard_address, assigned_addresses, allocation)
    if wireguard_address == server.wireguard_address:
        return wireguard_address
    # CLEANUP: drop the legacy wg_address dual write once every tier's pool DB
    # has applied migration 037 and no pre-rename checkout is in use.
    _update_server_fields(dsn, str(server.id), wireguard_address=wireguard_address, wg_address=wireguard_address)
    if server.wireguard_address:
        logger.warning(
            "Renumbered server {} from overlay address {} (outside the box address range of the tier '{}' "
            "allocation {}) to {}",
            server.id,
            server.wireguard_address,
            allocation.tier,
            allocation.overlay,
            wireguard_address,
        )
    else:
        logger.info("Assigned management overlay address {} to server {}", wireguard_address, server.id)
    return wireguard_address


def _build_composed_gen2_prep_script(
    *,
    dsn: str,
    server: BareMetalServer,
    ssh_ca_public_key: str,
    slice_base_image_url: str,
    slice_base_image_sha512: str,
    extra_prep_script: Path | None,
) -> str:
    """Build the composed gen-2 prep (base + collector + extra + verification).

    Same composition contract as :func:`_build_composed_prep_script` (shared
    via :func:`_compose_prep_with_tier_collector`); the base is the gen-2
    builder, fed by the activated tier's ``[management_plane]`` config
    (operator WireGuard peers + the Modal Proxy static IPs for the ``:22``
    lockdown), the box's overlay address (assigned + stamped here on first
    prep), and the committed apt mirror cut whose frozen docker archive the
    guest image installs the engine from.
    """
    management_plane_config = resolve_management_plane_config_or_none()
    wireguard_config = (
        management_plane_config.wireguard if management_plane_config is not None else ManagementWireguardConfig()
    )
    proxy_static_ips = (
        tuple(str(ip) for ip in management_plane_config.modal_proxy.static_ips)
        if management_plane_config is not None and management_plane_config.modal_proxy is not None
        else ()
    )
    allocation = _resolve_active_tier_overlay_allocation()
    base_script = build_gen2_box_prep_script(
        ssh_ca_public_key=ssh_ca_public_key,
        slice_base_image_url=slice_base_image_url,
        slice_base_image_sha512=slice_base_image_sha512,
        apt_mirror_snapshot_timestamp=read_current_timestamp(CURRENT_TIMESTAMP_PATH),
        wireguard_address=_ensure_box_wireguard_address(dsn, server, allocation),
        wireguard_listen_port=int(wireguard_config.listen_port),
        wireguard_operators=wireguard_config.operators,
        overlay=allocation,
        management_proxy_static_ips=proxy_static_ips,
        declared_uplink_mbps=server.uplink_mbps,
    )
    return _compose_prep_with_tier_collector(base_script, extra_prep_script)


def _resolve_active_tier_overlay_allocation() -> ManagementOverlayAllocation:
    """The activated tier's overlay allocation; a non-activated invocation falls back to dev's.

    The fallback mirrors the management-plane config resolution above: a
    non-activated prep is a dev-only escape hatch, and the dev allocation
    keeps it functional rather than refusing outright.
    """
    env_name = active_env_name_or_none()
    if env_name is None:
        logger.warning("No minds env is activated; assigning box overlay addresses from the dev tier's allocation")
        return management_overlay_for_tier(DEV_TIER)
    return management_overlay_for_tier(tier_for_env_name(env_name))


def _record_box_storage_partition_gib(dsn: str, server_id: str, prep_stdout: str) -> None:
    """Stamp the gen-2 box's measured storage partition (echoed by the prep) as its ``disk_gb``.

    The gen-2 disk budget is computed from this figure, so a box's row must
    carry the partition's real size (not the catalog's usable-disk estimate it
    was ordered or registered with) before anything is carved on it.
    """
    storage_partition_gib = parse_storage_partition_gib_from_prep_output(prep_stdout)
    if storage_partition_gib is None:
        raise BareMetalProvisioningError(
            f"gen-2 prep on server {server_id} completed but printed no {GEN2_STORAGE_PARTITION_MARKER} marker; "
            "cannot record the box's storage partition size (the gen-2 disk budget needs it)"
        )
    _update_server_fields(dsn, server_id, disk_gb=storage_partition_gib)


def _record_box_wireguard_public_key(dsn: str, server_id: str, prep_stdout: str) -> None:
    """Stamp the box's WireGuard public key (echoed by the gen-2 prep) on its row."""
    wireguard_public_key = parse_wireguard_public_key_from_prep_output(prep_stdout)
    if wireguard_public_key is None:
        raise BareMetalProvisioningError(
            f"gen-2 prep on server {server_id} completed but printed no MNGR_WIREGUARD_PUBLIC_KEY marker; "
            "cannot record the box's WireGuard public key (operator WireGuard configs need it)"
        )
    # CLEANUP: drop the legacy wg_public_key dual write once every tier's pool
    # DB has applied migration 037 and no pre-rename checkout is in use.
    _update_server_fields(
        dsn, server_id, wireguard_public_key=wireguard_public_key, wg_public_key=wireguard_public_key
    )


def _resolve_gen2_guest_image(url_override: str | None, sha512_override: str | None) -> tuple[str, str]:
    """The gen-2 guest image (URL, sha512) to stage: the pinned mirror artifact, or an override given with its digest."""
    if url_override is None and sha512_override is None:
        return DEFAULT_GEN2_SLICE_GUEST_IMAGE_URL, DEFAULT_GEN2_SLICE_GUEST_IMAGE_SHA512
    if url_override is None or sha512_override is None:
        raise click.UsageError(
            "--slice-base-image-url and --slice-base-image-sha512 must be given together for a gen-2 box: "
            "the prep verifies the downloaded image against the digest before customizing it"
        )
    return url_override, sha512_override


def _resolve_service_user_for_generation(
    box_generation: int, override: str | None, recorded_service_user: str | None
) -> str:
    """The service user a box of ``box_generation`` gets: the override, else the row's recorded user, else the default.

    Gen-2 boxes pin the user (the prep artifacts and sudoers grants name it), so
    a differing override is refused early -- and the recorded user is ignored: a
    row from before the rename records the retired gen-1 user, and re-prepping
    is how that box converges onto the pinned one.
    """
    if box_generation >= FIRST_QEMU_BOX_GENERATION:
        if override is not None and override != GEN2_SLICE_SERVICE_USER:
            raise click.UsageError(
                f"--slice-service-user {override!r} conflicts with the gen-2 layout, whose service "
                f"user is fixed to {GEN2_SLICE_SERVICE_USER!r} (the prep artifacts and sudoers grants name it)"
            )
        return GEN2_SLICE_SERVICE_USER
    return override or recorded_service_user or default_slice_service_user(box_generation)


def _stamp_server_service_user(dsn: str, server_id: str, service_user: str, **extra_fields: Any) -> None:
    """Record the service user a prep created on the box, so box commands SSH as it from now on."""
    # CLEANUP: drop the legacy lima_service_user dual write once every tier's
    # pool DB has applied migration 041 and no pre-rename checkout is in use.
    _update_server_fields(
        dsn, server_id, slice_service_user=service_user, lima_service_user=service_user, **extra_fields
    )


# Bytes of entropy in a box's LUKS recovery passphrase (a keyslot's only
# human-side secret; the TPM keyslot is the everyday unlock).
_STORAGE_PASSPHRASE_ENTROPY_BYTES: Final[int] = 32
# A single box command is a few round trips, never a prep.
_STORAGE_BOX_COMMAND_TIMEOUT_SECONDS: Final[float] = 120.0


class _BoxSshTarget(FrozenModel):
    """Where and how the prep and the storage-encryption helpers dial one box's management sshd."""

    host: str = Field(description="Address to dial (the resolved management dial, or the public address)")
    port: int = Field(description="Port to dial")
    ssh_user: str = Field(description="The box's management SSH user (the OS image's sudo user)")
    private_key_path: Path = Field(description="The operator identity the session authenticates with")
    box_host_public_key: str = Field(description="The box's recorded sshd host key, strictly pinned")


def _run_box_root_script(
    target: _BoxSshTarget,
    script: str,
    *,
    # Sized for a short box command (a few round trips); the prep passes its own.
    run_timeout_seconds: float = _STORAGE_BOX_COMMAND_TIMEOUT_SECONDS,
    stdin_bytes: bytes | None = None,
    is_output_logged: bool = True,
) -> str:
    """Run one root script on the box and return its stdout; a failed round trip raises ``BareMetalProvisioningError``."""
    return run_root_script_over_ssh(
        target.host,
        target.port,
        target.ssh_user,
        target.private_key_path,
        script,
        target.box_host_public_key,
        run_timeout_seconds=run_timeout_seconds,
        stdin_bytes=stdin_bytes,
        is_output_logged=is_output_logged,
    )


def _require_box_service_name_for_storage(server: BareMetalServer) -> str:
    """The OVH service name keying the box's recovery passphrase in Vault; a row without one cannot be encrypted."""
    if not server.ovh_service_name:
        raise BareMetalProvisioningError(
            f"server {server.id} has no ovh_service_name recorded, which keys its storage recovery passphrase in "
            "Vault; record it (`minds-admin server register --ovh-service-name ...`) before prepping the box"
        )
    return server.ovh_service_name


def _require_active_env_for_storage() -> str:
    env_name = active_env_name_or_none()
    if env_name is None:
        raise BareMetalProvisioningError(
            "no minds env is activated: `minds-admin env activate <env>` first (a gen-2 box's storage recovery "
            "passphrase lives in the activated tier's Vault)"
        )
    return env_name


@pure
def choose_storage_passphrase(
    *,
    storage_state: StorageVolumeState,
    vault_passphrase: str | None,
    fresh_passphrase: str,
    box_service_name: str,
) -> str:
    """Which recovery passphrase to stage for a prep: Vault's, or a fresh one that Vault must then record.

    A fresh passphrase is minted under exactly the rule the prep formats under:
    the storage root is mounted from a device other than the LUKS mapper (a
    stale Vault entry from the box's previous life is overwritten). Every
    other state -- the mapper mounted, or nothing mounted (an
    opened-but-unmounted mapper, a locked volume, or no storage partition at
    all) -- needs the passphrase that opens the existing header, which only
    Vault can supply: the prep verifies the staged passphrase against a
    keyslot, so a fresh one would fail there after being recorded in Vault as
    if it opened the box. That is why the mapper being mounted decides on its
    own, whatever the probe's block-device type read said: a mapper the audit
    could not confirm as a crypt device must never cost the box its recorded
    passphrase. A box in one of those states with no Vault entry is refused
    instead.
    """
    if storage_state.mounted_source is not None and storage_state.mounted_source != GEN2_STORAGE_LUKS_MAPPER_PATH:
        return fresh_passphrase
    if vault_passphrase is None:
        state = (
            "already has an encrypted storage volume"
            if storage_state.mounted_source is not None
            else "has nothing mounted at its storage root (a locked volume, an opened-but-unmounted mapper, or no "
            "storage partition)"
        )
        raise BareMetalProvisioningError(
            f"box {box_service_name} {state} but the tier's Vault holds no recovery passphrase for it; only that "
            "passphrase can open an existing volume, and a lost one cannot be recovered -- drain the box and repave it "
            "(a box with no storage partition needs the gen-2 reinstall layout first)"
        )
    return vault_passphrase


def _probe_storage_volume_state(target: _BoxSshTarget) -> StorageVolumeState:
    return parse_storage_volume_output(_run_box_root_script(target, build_read_storage_volume_command() + "\n"))


def _resolve_storage_passphrase_for_prep(server: BareMetalServer, target: _BoxSshTarget) -> str:
    """The passphrase the prep stages, recorded in Vault BEFORE the prep can format anything.

    A volume that is already encrypted must have its passphrase in Vault: the
    prep verifies the staged passphrase opens a keyslot, so a wrong or missing
    one is refused rather than papered over, and a lost passphrase means the
    box is drained and repaved.
    """
    env_name = _require_active_env_for_storage()
    service_name = _require_box_service_name_for_storage(server)
    storage_state = _probe_storage_volume_state(target)
    vault_passphrase = read_box_storage_passphrase_or_none(env_name, service_name)
    passphrase = choose_storage_passphrase(
        storage_state=storage_state,
        vault_passphrase=vault_passphrase,
        fresh_passphrase=secrets.token_urlsafe(_STORAGE_PASSPHRASE_ENTROPY_BYTES),
        box_service_name=service_name,
    )
    if passphrase != vault_passphrase:
        write_box_storage_passphrase(env_name, service_name, passphrase)
        logger.info("Recorded a fresh storage recovery passphrase for box {} in the tier's Vault", service_name)
    return passphrase


def _fetch_and_upload_storage_header_backup(server: BareMetalServer, target: _BoxSshTarget, luks_uuid: str) -> None:
    """Fetch the header backup staged on the box and upload it to the tier's storage bucket."""
    header_bytes = base64.b64decode(
        _run_box_root_script(target, render_storage_header_backup_fetch_script(), is_output_logged=False).strip()
    )
    storage = resolve_workspace_storage_config()
    object_key = storage_header_backup_object_key(
        storage.key_prefix, _require_box_service_name_for_storage(server), luks_uuid
    )
    upload_storage_header_backup(make_workspace_storage_s3_client(storage), storage.bucket, object_key, header_bytes)


def _upload_storage_header_backup_or_warn(server: BareMetalServer, target: _BoxSshTarget, prep_stdout: str) -> None:
    """Fetch the header backup the prep staged and park it in the tier bucket; any failure here only warns.

    The volume is fine without the backup (it is the insurance against a
    corrupt header), so neither a tier whose storage bucket is not configured
    nor a transient failure fetching or uploading it may fail an otherwise
    successful prep. Only a missing MNGR_STORAGE_ENCRYPTION marker -- which
    means the encryption step itself did not behave as expected -- raises.
    """
    encryption = parse_storage_encryption_from_prep_output(prep_stdout)
    if encryption is None:
        raise BareMetalProvisioningError(
            f"gen-2 prep on server {server.id} completed but printed no MNGR_STORAGE_ENCRYPTION marker; "
            "the storage volume's state is unknown"
        )
    luks_uuid, _backing_device = encryption
    try:
        _fetch_and_upload_storage_header_backup(server, target, luks_uuid)
    except (click.ClickException, BareMetalProvisioningError) as exc:
        logger.warning("Not uploading the LUKS header backup of box {}: {}", server.public_address, exc)


def _run_gen2_prep_with_storage_encryption(
    server: BareMetalServer,
    target: _BoxSshTarget,
    script: str,
    # Where the round trips after the prep script dial, given the script's stdout: the prep locks the box's public
    # ``:22`` down, so a fresh install continues over the overlay the script just brought up.
    resolve_post_script_target: Callable[[str], _BoxSshTarget],
) -> str:
    """Run a gen-2 prep with its recovery passphrase staged on the box's tmpfs, then bank the header backup.

    The passphrase never rides inside the prep script (which lands as a file
    on the box): it is written to tmpfs over its own SSH round trip, the prep
    consumes and deletes it, and a best-effort cleanup removes both staging
    files however the prep ended.
    """
    passphrase = _resolve_storage_passphrase_for_prep(server, target)
    _run_box_root_script(target, render_storage_passphrase_staging_script(), stdin_bytes=passphrase.encode())
    post_script_target = target
    try:
        prep_stdout = _run_box_root_script(target, script, run_timeout_seconds=_BOX_PREP_SSH_TIMEOUT_SECONDS)
        post_script_target = resolve_post_script_target(prep_stdout)
        _upload_storage_header_backup_or_warn(server, post_script_target, prep_stdout)
    finally:
        try:
            _run_box_root_script(post_script_target, render_storage_passphrase_cleanup_script())
        except BareMetalProvisioningError as exc:
            logger.warning("Could not clean the storage staging files off {}: {}", post_script_target.host, exc)
    return prep_stdout


def _run_box_prep_script(
    server: BareMetalServer,
    target: _BoxSshTarget,
    script: str,
    resolve_post_script_target: Callable[[str], _BoxSshTarget],
) -> str:
    """Run the composed prep on the box: with the storage-encryption round trips on gen-2, plainly on gen-1."""
    if server.box_generation >= FIRST_QEMU_BOX_GENERATION:
        return _run_gen2_prep_with_storage_encryption(server, target, script, resolve_post_script_target)
    return _run_box_root_script(target, script, run_timeout_seconds=_BOX_PREP_SSH_TIMEOUT_SECONDS)


def _management_target_after_prep(
    dsn: str, server_id: str, ssh_user: str, private_key_path: Path, box_host_public_key: str, prep_stdout: str
) -> _BoxSshTarget:
    """The dial for the round trips that follow a gen-2 prep script, wherever the prep itself dialed.

    The box's WireGuard key is recorded first (so a run that dies after this
    point resumes over the overlay instead of the now locked-down public
    ``:22``), then the management resolver picks the dial, falling back to the
    public address for a box that has no overlay yet.
    """
    _record_box_wireguard_public_key(dsn, server_id, prep_stdout)
    dial = resolve_server_management_dial(_fetch_server_or_raise(dsn, server_id))
    return _BoxSshTarget(
        host=dial.host,
        port=dial.port,
        ssh_user=ssh_user,
        private_key_path=private_key_path,
        box_host_public_key=box_host_public_key,
    )


def _post_prep_target_resolver(
    dsn: str, server_id: str, ssh_user: str, private_key_path: Path, box_host_public_key: str
) -> Callable[[str], _BoxSshTarget]:
    """The ``resolve_post_script_target`` for one prep run: ``_management_target_after_prep`` over the run's connection details."""
    return lambda prep_stdout: _management_target_after_prep(
        dsn, server_id, ssh_user, private_key_path, box_host_public_key, prep_stdout
    )


@server.command(name="prep")
@click.option(
    "--server-id", required=True, help="bare_metal_servers row id (from `register`/`order`) of the box to prep."
)
@click.option("--ssh-user", default="debian", help="Bootstrap SSH user (the OS image's default cloud user).")
@click.option(
    "--slice-service-user",
    default=None,
    help=(
        "Dedicated non-root user to create for the slice VMs. Defaults per generation (gen-2 boxes pin "
        f"{GEN2_SLICE_SERVICE_USER!r}; gen-1 boxes keep the row's recorded user)."
    ),
)
@click.option("--lima-version", default=DEFAULT_LIMA_VERSION, help="Lima release to install on the box (gen-1 only).")
@click.option(
    "--slice-base-image-url",
    default=None,
    help=(
        "Guest OS image to stage on the box once (slices boot from this via file://, never a live mirror). "
        "Defaults per generation: the bookworm lima image for gen-1 boxes, the pinned trixie image from "
        "imbue's artifact mirror for gen-2 (which then also requires --slice-base-image-sha512)."
    ),
)
@click.option(
    "--slice-base-image-sha512",
    default=None,
    help="The sha512 a gen-2 --slice-base-image-url override must match; the pinned image's digest by default.",
)
@click.option("--database-url", default=None, help=DATABASE_URL_HELP)
@click.option(
    "--extra-prep-script",
    "extra_prep_script",
    default=None,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help=(
        "Path to an additional idempotent root bash script appended to the composed box prep "
        "(e.g. a collector install rendered by `observability render-collector-install` for "
        "non-activated one-off use). Runs on the box after the standard prep steps and the "
        "collector install, under the same `sudo bash` invocation."
    ),
)
def prep_box(
    server_id: str,
    ssh_user: str,
    slice_service_user: str | None,
    lima_version: str,
    slice_base_image_url: str | None,
    slice_base_image_sha512: str | None,
    database_url: str | None,
    extra_prep_script: Path | None,
) -> None:
    """Install the slice stack on a delivered box (dispatched on its recorded generation).

    Idempotent. Gen-1: QEMU + lima + tooling, the lima service user, the staged
    bookworm guest image. Gen-2 (specs/slice-fleet-gen2): raw qemu + OVMF +
    nftables + genisoimage + dnsmasq + WireGuard, the 512 per-slice users and
    the ``mngr-dhcp`` DHCP service user, the plugin-rendered prep artifacts
    (template unit / root helper / sudoers / the slice DHCP server's config,
    unit and udp/67 policy), the LUKS storage volume (an empty storage
    partition is formatted, TPM-enrolled and keyed by a per-box recovery
    passphrase this command mints into or reads from the tier's Vault and
    stages on the box's tmpfs; a plain partition holding slices is refused;
    the header backup lands in the tier bucket), the staged trixie guest
    image on that volume, the management WireGuard bring-up (overlay address
    assigned + public key recorded on the row), and -- when the activated
    tier's ``[management_plane]`` table names a Modal Proxy -- the box ``:22``
    lockdown. Gen-1 authorizes the pool management key (POOL_SSH_PRIVATE_KEY)
    for the service user; gen-2 installs the tier's SSH CA trust instead and
    removes every static authorized key, so the admin CLI and the connector
    reach the box by certificate. Run after the OS install, before
    ``minds-admin pool create``.

    When the activated tier has a boxes observability ingest credential in
    Vault, the prep also installs the pinned OpenTelemetry Collector in the same
    session and verifies its unit is active -- fail-closed: an install or
    verification failure fails the prep. No credential = clean skip. Re-running
    prep is also how the collector rolls out to (or gets refreshed on) the
    existing fleet.

    The box SSH strictly pins the box's recorded sshd host key (no
    trust-on-first-use); the key is injected by ``server setup`` (OS reinstall) or
    captured once by ``minds-admin pool backfill-host-keys``. Fails closed if the row has
    no recorded host key.
    """
    dsn = resolve_pool_database_url(database_url)
    server = _fetch_server_or_raise(dsn, server_id)
    box_host_public_key = _require_box_reachable_with_pinned_host_key(server, action="prepping")
    dial = resolve_server_management_dial(server)
    is_gen2 = server.box_generation >= FIRST_QEMU_BOX_GENERATION
    service_user = _resolve_service_user_for_generation(
        server.box_generation, slice_service_user, server.slice_service_user
    )
    with box_management_identities() as identities:
        # The gen-2 script is composed before the operator identity is resolved (a
        # Vault certificate sign), so a tier without its committed [ssh_ca] key
        # refuses with the bring-up pointer rather than with a failed sign.
        if is_gen2:
            gen2_image_url, gen2_image_sha512 = _resolve_gen2_guest_image(
                slice_base_image_url, slice_base_image_sha512
            )
            script = _build_composed_gen2_prep_script(
                dsn=dsn,
                server=server,
                ssh_ca_public_key=require_tier_ssh_ca_public_key("prepping a gen-2 box"),
                slice_base_image_url=gen2_image_url,
                slice_base_image_sha512=gen2_image_sha512,
                extra_prep_script=extra_prep_script,
            )
            logger.info("Prepping gen-2 box {} as {} (service user {})", server.public_address, ssh_user, service_user)
        else:
            script = _build_composed_prep_script(
                pool_public_key=derive_ssh_public_key(identities.private_key_path_for(server.box_generation)),
                slice_service_user=service_user,
                lima_version=lima_version,
                slice_base_image_url=slice_base_image_url or DEFAULT_IMAGE_URL_X86_64,
                extra_prep_script=extra_prep_script,
            )
            logger.info(
                "Prepping box {} as {} (lima user {}, lima {})",
                server.public_address,
                ssh_user,
                service_user,
                lima_version,
            )
        private_key_path = identities.private_key_path_for(server.box_generation)
        prep_stdout = _run_box_prep_script(
            server,
            _BoxSshTarget(
                host=dial.host,
                port=dial.port,
                ssh_user=ssh_user,
                private_key_path=private_key_path,
                box_host_public_key=box_host_public_key,
            ),
            script,
            _post_prep_target_resolver(dsn, server_id, ssh_user, private_key_path, box_host_public_key),
        )
    # The prep created (or converged) this user on the box; the row must say so
    # before the connector's next box command, which SSHes as the recorded user.
    _stamp_server_service_user(dsn, server_id, service_user)
    if is_gen2:
        # The WireGuard key was recorded by the post-script resolver, before the storage round trips.
        _record_box_storage_partition_gib(dsn, server_id, prep_stdout)
        logger.info(
            "Gen-2 box {} prepped: qemu stack installed, {} ready, storage volume encrypted, trixie image staged, "
            "WireGuard up",
            server.public_address,
            service_user,
        )
    else:
        logger.info(
            "Box {} prepped: qemu+lima installed, {} ready, OS image staged", server.public_address, service_user
        )


@pure
def build_box_ssh_argv(
    *,
    dial_host: str,
    dial_port: int,
    ssh_user: str,
    private_key_path: Path,
    host_key_options: Sequence[str],
    remote_command: Sequence[str],
) -> list[str]:
    return [
        "ssh",
        "-p",
        str(dial_port),
        "-i",
        str(private_key_path),
        *host_key_options,
        "-o",
        "ConnectTimeout=30",
        f"{ssh_user}@{dial_host}",
        *remote_command,
    ]


@server.command(name="ssh", context_settings={"ignore_unknown_options": True})
@click.option(
    "--server-id", required=True, help="bare_metal_servers row id (from `list`) of the box to open a session on."
)
@click.option("--ssh-user", default="debian", help="Management SSH user on the box (the OS image's sudo user).")
@click.option("--database-url", default=None, help=DATABASE_URL_HELP)
@click.argument("remote_command", nargs=-1, type=click.UNPROCESSED)
def ssh_into_box(server_id: str, ssh_user: str, database_url: str | None, remote_command: tuple[str, ...]) -> None:
    """Open an interactive management SSH session on a box (or run one command: `... ssh --server-id X -- uptime`).

    Resolves the dial like every box-management command -- the userspace
    WireGuard tunnel when onetun and your operator key are in place, else a
    reachable kernel-route overlay, else the public address -- so a
    locked-down box needs no ``wg-quick`` and no root. Auth is the box
    generation's management key (your Vault-signed operator certificate on
    gen-2, the tier's pool key on gen-1) with the box's recorded host key
    strictly pinned.
    """
    dsn = resolve_pool_database_url(database_url)
    server_row = _fetch_server_or_raise(dsn, server_id)
    box_host_public_key = _require_box_reachable_with_pinned_host_key(server_row, action="opening a session")
    dial = resolve_server_management_dial(server_row)
    logger.debug("Opening management SSH to box {} via {}:{}", server_row.public_address, dial.host, dial.port)
    with box_management_identities() as identities:
        private_key_path = identities.private_key_path_for(server_row.box_generation)
        with _box_ssh_host_key_options(dial.host, dial.port, box_host_public_key) as host_key_options:
            argv = build_box_ssh_argv(
                dial_host=dial.host,
                dial_port=dial.port,
                ssh_user=ssh_user,
                private_key_path=private_key_path,
                host_key_options=host_key_options,
                remote_command=remote_command,
            )
            # The spawned tunnel (when the dial took the userspace path) stays
            # up for the whole session and is closed by the process-lifetime
            # tunnel group at exit.
            completed = run_interactive_subprocess(argv)
    raise SystemExit(completed.returncode)


@server.command(name="unlock")
@click.option("--server-id", required=True, help="bare_metal_servers row id (from `list`) of the gen-2 box to unlock.")
@click.option("--ssh-user", default="debian", help="Management SSH user on the box (the OS image's sudo user).")
@click.option("--database-url", default=None, help=DATABASE_URL_HELP)
def unlock_storage_volume(server_id: str, ssh_user: str, database_url: str | None) -> None:
    """Open a gen-2 box's locked LUKS storage volume with its Vault recovery passphrase and bring its slices back.

    The everyday unlock is the box's TPM at boot; this is the recovery path
    for a box whose TPM unlock failed (the telemetry collector's
    STORAGE_VOLUME_LOCKED signal, or every slice on the box down after a
    reboot). Reads the passphrase from the tier's Vault, hands it to the box
    on stdin (never argv), opens and mounts the volume, restores the bind
    mounts and the swapfile, flushes the journal, and starts every slice unit
    enabled for boot. Re-running `server prep` afterwards re-seals the volume
    to the box's current TPM.
    """
    dsn = resolve_pool_database_url(database_url)
    server_row = _fetch_server_or_raise(dsn, server_id)
    if server_row.box_generation < FIRST_QEMU_BOX_GENERATION:
        raise click.UsageError(f"server {server_id} is a gen-1 box, which has no encrypted storage volume to unlock")
    box_host_public_key = _require_box_reachable_with_pinned_host_key(
        server_row, action="unlocking its storage volume"
    )
    env_name = _require_active_env_for_storage()
    service_name = _require_box_service_name_for_storage(server_row)
    passphrase = read_box_storage_passphrase_or_none(env_name, service_name)
    if passphrase is None:
        raise BareMetalProvisioningError(
            f"the tier's Vault holds no storage recovery passphrase for {service_name}; the volume cannot be "
            "opened -- drain the box and repave it"
        )
    dial = resolve_server_management_dial(server_row)
    with box_management_identities() as identities:
        target = _BoxSshTarget(
            host=dial.host,
            port=dial.port,
            ssh_user=ssh_user,
            private_key_path=identities.private_key_path_for(server_row.box_generation),
            box_host_public_key=box_host_public_key,
        )
        # The trailing newline terminates the script's `read`; the passphrase
        # bytes themselves carry none (the keyslot was formatted without one).
        unlock_stdout = _run_box_root_script(
            target, render_storage_unlock_script(), stdin_bytes=f"{passphrase}\n".encode()
        )
    if STORAGE_UNLOCKED_MARKER not in unlock_stdout:
        raise BareMetalProvisioningError(
            f"the unlock script on {server_row.public_address} finished without its {STORAGE_UNLOCKED_MARKER} marker"
        )
    write_human_line(f"Server {server_id} ({server_row.public_address}): storage volume unlocked, slices started.")


def audit_box_against_tier(
    *,
    server_to_audit: BareMetalServer,
    env_name: str | None,
    identities: ManagementIdentityResolver,
    # The tier's committed SSH CA, which a gen-2 box must trust exactly; None
    # when the tier has none committed (every gen-2 box then reads as not exclusive).
    expected_ca_public_key: str | None,
) -> BoxTierAudit:
    """Report a box's REAL occupancy plus any cross-tier contamination on it.

    The DB-derived slot accounting in ``list`` counts only the querying env's own
    rows, so a slice belonging to another env -- and in particular another *tier* --
    is invisible to it. This SSHes the box and reports what is actually there, which
    is the only way to see a foreign-tier slice or a hand-added SSH key short of a
    bake refusing to run.
    """
    audit_dial = resolve_server_management_dial(server_to_audit)
    client = build_slice_vm_client(
        box_generation=server_to_audit.box_generation,
        box_address=audit_dial.host,
        box_ssh_port=audit_dial.port,
        box_ssh_user=box_service_user(server_to_audit),
        private_key_path=str(identities.private_key_path_for(server_to_audit.box_generation)),
        box_host_public_key=server_to_audit.box_host_public_key,
    )
    disk_names = client.list_disk_names()
    mdstat_text, proc_swaps_text = client.read_box_health_texts()
    trust = client.read_management_trust()
    # A gen-1 box has no storage volume, so the probe (one more round trip) is
    # skipped for it rather than made and read as "nothing mounted".
    is_storage_encrypted = (
        client.read_storage_volume_state().is_encrypted
        if server_to_audit.box_generation >= FIRST_QEMU_BOX_GENERATION
        else False
    )
    return BoxTierAudit(
        server_id=str(server_to_audit.id),
        public_address=str(server_to_audit.public_address),
        slot_count=server_to_audit.slot_count,
        box_used_slots=count_slice_resource_names(disk_names),
        authorized_key_count=trust.authorized_key_count,
        expected_authorized_key_count=expected_static_authorized_key_count(server_to_audit.box_generation),
        trusted_ca_public_key=trust.trusted_ca_public_key,
        is_trusted_ca_correct=is_trusted_ca_correct_for_tier(
            trust, server_to_audit.box_generation, expected_ca_public_key
        ),
        foreign_tier_slices=tuple(sorted(foreign_tier_slice_names(disk_names, env_name)))
        if env_name is not None
        else (),
        degraded_md_arrays=tuple(parse_degraded_md_arrays(mdstat_text)),
        raw_swap_devices=tuple(parse_raw_swap_devices(proc_swaps_text)),
        is_storage_encrypted=is_storage_encrypted,
    )


def audit_fleet_against_tier(
    *,
    capacities: Sequence[BareMetalServerCapacity],
    env_name: str | None,
    identities: ManagementIdentityResolver,
    expected_ca_public_key: str | None,
) -> BoxTierAuditReport:
    """Audit every box in the fleet, reporting -- never raising on -- the ones that cannot be read.

    A box that is down, mid-reinstall, or has no pinned host key must not cost the
    operator every other box's verdict: this command exists precisely to find boxes
    in a bad state, so an unreadable one is an entry in the report (and a logged
    warning), not an abort.
    """
    audits: list[BoxTierAudit] = []
    unaudited: list[UnauditedBox] = []
    for capacity in capacities:
        server_to_audit = capacity.server
        if not server_to_audit.public_address:
            unaudited.append(
                UnauditedBox(
                    server_id=str(server_to_audit.id),
                    public_address=None,
                    reason="the row has no public_address, so the box cannot be reached",
                )
            )
            continue
        try:
            audits.append(
                audit_box_against_tier(
                    server_to_audit=server_to_audit,
                    env_name=env_name,
                    identities=identities,
                    expected_ca_public_key=expected_ca_public_key,
                )
            )
        # ``OSError`` too: auditing a box is not purely a remote call. It writes the
        # box's pinned host key to a known_hosts file and spawns ``ssh``, so a local
        # I/O failure on one box would otherwise cost every other box its verdict --
        # which is precisely what this command promises never to do. (The same
        # reason ``LimaSliceVpsClient._best_effort_destroy`` catches it.)
        except (LimaCommandError, SliceCommandError, BareMetalProvisioningError, OSError) as exc:
            logger.warning("Could not audit box {} ({}): {}", server_to_audit.id, server_to_audit.public_address, exc)
            unaudited.append(
                UnauditedBox(
                    server_id=str(server_to_audit.id),
                    public_address=server_to_audit.public_address,
                    reason=str(exc),
                )
            )
    return build_box_tier_audit_report(env_name=env_name, audits=audits, unaudited=unaudited)


def build_box_tier_audit_report(
    *,
    env_name: str | None,
    audits: Sequence[BoxTierAudit],
    unaudited: Sequence[UnauditedBox],
) -> BoxTierAuditReport:
    """Aggregate per-box audits into the summary ``list --verify-occupancy`` emits."""
    contaminated_count = sum(1 for audit in audits if not audit.is_exclusive_to_tier)
    return BoxTierAuditReport(
        env_name=env_name,
        is_foreign_tier_checked=env_name is not None,
        exclusive=len(audits) - contaminated_count,
        contaminated=contaminated_count,
        unaudited=len(unaudited),
        boxes=tuple(audits),
        unaudited_boxes=tuple(unaudited),
    )


@server.command(name="list")
@click.option("--database-url", default=None, help=DATABASE_URL_HELP)
@click.option(
    "--verify-occupancy",
    "is_occupancy_verified",
    is_flag=True,
    default=False,
    help=(
        "SSH each box and report its REAL occupancy plus any cross-tier contamination "
        "(foreign-tier slices, extra authorized SSH keys, a gen-2 box pinning another SSH CA) and "
        "whether a gen-2 box's storage root is the mounted LUKS volume (is_storage_encrypted). "
        "The plain table counts only this env's own DB rows, so it undercounts a shared box. "
        "Each box is reached with its generation's management key: the operator certificate "
        "on gen-2, the pool key from the activated tier's Vault entry (or $POOL_SSH_PRIVATE_KEY) "
        "on gen-1."
    ),
)
def list_servers(database_url: str | None, is_occupancy_verified: bool) -> None:
    """List bare-metal servers with per-server and fleet slot accounting (from the DB).

    With ``--verify-occupancy`` the activated env name decides which slices on a
    box are foreign-tier; without an activated env there is no tier to compare
    against, so only the authorized-key half of the audit runs.
    """
    conn = psycopg2.connect(resolve_pool_database_url(database_url))
    try:
        capacities = fetch_server_capacities(conn)
        machine_usage_by_server_id = {
            str(capacity.server.id): fetch_machine_usage_on_server(conn, capacity.server.id)
            for capacity in capacities
            if capacity.server.box_generation >= FIRST_QEMU_BOX_GENERATION
        }
    finally:
        conn.close()
    logger.info("\n{}", _format_capacity_table(capacities, machine_usage_by_server_id))
    if not is_occupancy_verified:
        return
    env_name = active_env_name_or_none()
    with box_management_identities() as identities:
        report = audit_fleet_against_tier(
            capacities=capacities,
            env_name=env_name,
            identities=identities,
            expected_ca_public_key=resolve_tier_ssh_ca_public_key_or_none(),
        )
    emit_json(report.model_dump(mode="json"))
    if not report.is_foreign_tier_checked:
        logger.warning(
            "No --env-name given, so only the authorized-key half of the audit ran: an empty "
            "foreign_tier_slices above means NOT CHECKED, not clean."
        )
    if report.contaminated:
        logger.warning(
            "{} of {} audited box(es) are NOT exclusive to this tier -- a bake onto them will refuse. "
            "See the JSON above.",
            report.contaminated,
            len(report.boxes),
        )
    if report.unaudited:
        logger.warning(
            "{} box(es) could not be read, so their occupancy and tier state are UNKNOWN (not clean). "
            "See unaudited_boxes in the JSON above.",
            report.unaudited,
        )
    plaintext_gen2_boxes = plaintext_gen2_box_ids(report, capacities)
    if plaintext_gen2_boxes:
        logger.warning(
            "{} gen-2 box(es) do NOT have their storage root mounted from the LUKS volume, so their slices are in "
            "plaintext (or the box is locked): {}. A locked box needs `minds-admin server unlock`; a never-encrypted "
            "box must be drained and repaved.",
            len(plaintext_gen2_boxes),
            ", ".join(plaintext_gen2_boxes),
        )


@pure
def plaintext_gen2_box_ids(report: BoxTierAuditReport, capacities: Sequence[BareMetalServerCapacity]) -> list[str]:
    """The audited gen-2 boxes whose storage root is not the mounted LUKS volume, in report order.

    Gen-1 boxes have no storage volume (their audits always read unencrypted)
    and unaudited boxes have no verdict, so neither is listed.
    """
    box_generation_by_server_id = {str(capacity.server.id): capacity.server.box_generation for capacity in capacities}
    return [
        audit.server_id
        for audit in report.boxes
        if box_generation_by_server_id[audit.server_id] >= FIRST_QEMU_BOX_GENERATION and not audit.is_storage_encrypted
    ]


@server.command(name="register")
@click.option("--ovh-service-name", required=True, help="OVH dedicated serviceName of the delivered box.")
@click.option("--plan-code", required=True, help="Catalog planCode the box was ordered as.")
@click.option(
    "--region",
    required=True,
    type=click.Choice(sorted(OVH_US_DATACENTER_CODES)),
    help="OVH datacenter code the box lives in (vin = US-EAST-VA, hil = US-WEST-OR).",
)
@click.option("--public-address", required=True, help="SSH-reachable public address of the box.")
@click.option("--ram-gb", type=int, required=True, help="Total RAM in GB.")
@click.option("--cpu-cores", type=int, required=True, help="Physical CPU cores.")
@click.option("--cpu-threads", type=int, required=True, help="CPU threads.")
@click.option("--disk-gb", type=int, required=True, help="Usable disk in GB for slice data (split across slices).")
@click.option(
    "--memory-per-slice-gb",
    type=int,
    required=True,
    help="RAM (GB) each slice on this box advertises; sets slot count + per-slice sizing.",
)
@click.option(
    "--cpu-overcommit",
    type=float,
    default=DEFAULT_SLICE_CPU_OVERCOMMIT_RATIO,
    show_default=True,
    help="CPU overcommit factor for sizing each slice's vCPUs.",
)
@click.option("--raid-level", default=None, help="RAID level configured at install (e.g. RAID1).")
@click.option(
    "--slice-service-user",
    default=None,
    help=(
        "Non-root OS user that owns the box's slice VMs. Defaults per --box-generation (gen-2 boxes pin "
        f"{GEN2_SLICE_SERVICE_USER!r}; gen-1 boxes keep their lima user)."
    ),
)
@click.option("--ovh-order-id", default=None, help="OVH order id, if known.")
@click.option("--status", default=SERVER_STATUS_READY, help="Initial lifecycle status.")
@click.option(
    "--box-generation",
    type=int,
    default=1,
    show_default=True,
    help="Slice-fleet generation of the box (1 = bookworm+lima, 2 = trixie+qemu; specs/slice-fleet-gen2).",
)
@click.option(
    "--uplink-mbps",
    type=click.IntRange(min=1),
    required=True,
    help=(
        "Declared public uplink rate in Mbit/s (the plan's bandwidth option); sizes gen-2 fair-share "
        "bandwidth classes, the egress signal, and the link-speed audit."
    ),
)
@click.option("--database-url", default=None)
def register_server(
    ovh_service_name: str,
    plan_code: str,
    region: str,
    public_address: str,
    ram_gb: int,
    cpu_cores: int,
    cpu_threads: int,
    disk_gb: int,
    memory_per_slice_gb: int,
    cpu_overcommit: float,
    raid_level: str | None,
    slice_service_user: str | None,
    ovh_order_id: str | None,
    status: str,
    box_generation: int,
    uplink_mbps: int,
    database_url: str | None,
) -> None:
    """Record an already-provisioned bare-metal box in the pool DB."""
    resolved_service_user = _resolve_service_user_for_generation(box_generation, slice_service_user, None)
    # The gen-2 units-valid check (specs/slice-fleet) only refuses at ordering
    # time: a delivered box is what it is, its prep re-measures disk_gb from
    # the storage partition, and the two-budget accounting fits fewer machines
    # on a short disk. So registration warns and proceeds.
    if box_generation >= FIRST_QEMU_BOX_GENERATION:
        try:
            shortfall = gen2_register_disk_shortfall_or_none(ram_gb=ram_gb, disk_gb=disk_gb)
        except BareMetalConfigError as exc:
            raise click.UsageError(str(exc)) from exc
        if shortfall is not None:
            logger.warning("{}", shortfall)
    server_row = build_registered_server(
        ovh_service_name=ovh_service_name,
        plan_code=plan_code,
        region=region,
        public_address=public_address,
        ram_gb=ram_gb,
        cpu_cores=cpu_cores,
        cpu_threads=cpu_threads,
        disk_gb=disk_gb,
        memory_per_slice_gb=memory_per_slice_gb,
        cpu_overcommit_ratio=cpu_overcommit,
        raid_level=raid_level,
        slice_service_user=resolved_service_user,
        ovh_order_id=ovh_order_id,
        status=status,
        box_generation=box_generation,
        uplink_mbps=uplink_mbps,
    )
    conn = psycopg2.connect(resolve_pool_database_url(database_url))
    try:
        insert_bare_metal_server(conn, server_row)
    finally:
        conn.close()
    logger.info(
        "Registered bare-metal server {} ({}): {} slots, status {}",
        server_row.id,
        ovh_service_name,
        server_row.slot_count,
        status,
    )


@server.command(name="sweep-ci-slices")
@click.option(
    "--max-age-hours",
    type=float,
    default=DEFAULT_CI_SLICE_MAX_AGE_HOURS,
    show_default=True,
    help=(
        "Destroy CI-owned slices older than this. Old enough that no live (serialized) release run "
        "can still be using one; young CI slices and every non-ci-tier resource are kept."
    ),
)
@click.option("--database-url", default=None, help=DATABASE_URL_HELP)
def sweep_ci_slices(max_age_hours: float, database_url: str | None) -> None:
    """Destroy stale CI-tier slices left on the ready boxes by crashed release runs.

    Reads each ready box's real slice resources over SSH (with the box generation's
    management key, through its slice client) and destroys every slice stamped for a
    ``ci-*`` env that is older than the threshold --
    the crash backstop for release runs whose per-run env (and its DB) died before the
    normal teardown. See specs/remote-workspaces-in-ci.md.
    """
    if max_age_hours <= 0:
        raise click.UsageError("--max-age-hours must be positive")
    conn = psycopg2.connect(resolve_pool_database_url(database_url))
    try:
        ready_servers = [server for server in fetch_servers(conn) if str(server.status) == SERVER_STATUS_READY]
    finally:
        conn.close()
    with box_management_identities() as identities:
        report = sweep_ci_slices_across_boxes(ready_servers, identities=identities, max_age_hours=max_age_hours)
    emit_json(report.model_dump(mode="json"))
    if report.unreachable_boxes or any(box.failed for box in report.boxes):
        raise click.ClickException(
            "the CI slice sweep could not fully clean the fleet (unreachable boxes or failed destroys above); "
            "stale slices will be retried on the next sweep"
        )


def sweep_ci_slices_across_boxes(
    ready_servers: Sequence[BareMetalServer],
    *,
    identities: ManagementIdentityResolver,
    max_age_hours: float,
) -> CiSliceSweepReport:
    """Sweep every ready box, reporting -- never raising on -- the ones that cannot be reached.

    A box whose management key cannot even be resolved (a gen-2 box outside an
    activated env, or a failed operator certificate sign) counts as unreachable
    like one whose sshd does not answer, so the rest of the fleet is still swept.
    """
    box_reports: list[CiSliceSweepBoxReport] = []
    unreachable: list[str] = []
    for server_row in ready_servers:
        if not server_row.public_address:
            unreachable.append(str(server_row.id))
            continue
        try:
            sweep_dial = resolve_server_management_dial(server_row)
            client = build_slice_vm_client(
                box_generation=server_row.box_generation,
                box_address=sweep_dial.host,
                box_ssh_port=sweep_dial.port,
                box_ssh_user=box_service_user(server_row),
                private_key_path=str(identities.private_key_path_for(server_row.box_generation)),
                box_host_public_key=server_row.box_host_public_key,
            )
            box_reports.append(
                sweep_ci_slices_on_box(
                    client,
                    server_id=str(server_row.id),
                    public_address=str(server_row.public_address),
                    max_age_seconds=max_age_hours * 3600.0,
                )
            )
        except (MngrError, OSError) as exc:
            logger.warning("CI slice sweep: box {} unreachable: {}", server_row.public_address, exc)
            unreachable.append(str(server_row.id))
    return CiSliceSweepReport(
        max_age_hours=max_age_hours,
        boxes=tuple(box_reports),
        unreachable_boxes=tuple(sorted(unreachable)),
    )


@server.command(name="import-boxes")
@click.option(
    "--source-database-url",
    required=True,
    help=(
        "Pool DSN holding the canonical bare_metal_servers rows to copy from (for the CI standing "
        "boxes: the CI infra DB at secrets/minds/ci/neon/DATABASE_URL)."
    ),
)
@click.option("--database-url", default=None, help=DATABASE_URL_HELP)
def import_boxes(source_database_url: str, database_url: str | None) -> None:
    """Copy every ready bare_metal_servers row from a source pool DB into this env's pool DB.

    Id-preserving and idempotent (upsert by row id), so re-running after a box changed
    (address, host key, status) converges the target on the source. Used by the CI
    release flow to make the standing CI boxes leasable from each per-run ci env --
    see specs/remote-workspaces-in-ci.md.

    Rows are copied whole, so the source DB must already carry connector migration
    040 (``uplink_mbps`` set on every row); there is no ``--uplink-mbps`` here.
    """
    source_conn = psycopg2.connect(source_database_url)
    try:
        ready_servers = [server for server in fetch_servers(source_conn) if str(server.status) == SERVER_STATUS_READY]
    finally:
        source_conn.close()
    if not ready_servers:
        raise click.ClickException(
            f"the source pool DB has no '{SERVER_STATUS_READY}' bare_metal_servers rows to import"
        )
    # A box whose datacenter the region map does not know could never be
    # matched by any lease label; refuse the whole import before any upsert.
    unknown_datacenter_servers = [
        server_row for server_row in ready_servers if server_row.region not in OVH_US_DATACENTER_CODES
    ]
    if unknown_datacenter_servers:
        raise click.ClickException(
            "refusing to import boxes whose datacenter is not in the region map "
            f"{sorted(OVH_US_DATACENTER_CODES)}: "
            + ", ".join(f"{server_row.id} ({server_row.region!r})" for server_row in unknown_datacenter_servers)
        )
    target_conn = psycopg2.connect(resolve_pool_database_url(database_url))
    try:
        imported, skipped = _import_ready_servers(target_conn, ready_servers)
    finally:
        target_conn.close()
    emit_json(
        {
            "imported": [
                {
                    "id": str(server_row.id),
                    "region": server_row.region,
                    "public_address": server_row.public_address,
                    "slot_count": server_row.slot_count,
                }
                for server_row in imported
            ],
            "skipped": [
                {"id": str(server_row.id), "public_address": server_row.public_address, "reason": reason}
                for server_row, reason in skipped
            ],
        }
    )


def _import_ready_servers(
    target_conn: Any, ready_servers: Sequence[BareMetalServer]
) -> tuple[list[BareMetalServer], list[tuple[BareMetalServer, str]]]:
    """Upsert each box into the target; a box the target already registers under another row id is skipped, not fatal."""
    imported: list[BareMetalServer] = []
    skipped: list[tuple[BareMetalServer, str]] = []
    for server_row in ready_servers:
        try:
            upsert_bare_metal_server(target_conn, server_row)
        except psycopg2.errors.UniqueViolation as exc:
            target_conn.rollback()
            reason = (exc.diag.message_detail or str(exc)).strip()
            logger.warning(
                "Skipping box {} ({}): the target already registers it under another row ({})",
                server_row.id,
                server_row.public_address,
                reason,
            )
            skipped.append((server_row, reason))
            continue
        imported.append(server_row)
    return imported, skipped


def build_registered_server(
    *,
    ovh_service_name: str,
    plan_code: str,
    region: str,
    public_address: str,
    ram_gb: int,
    cpu_cores: int,
    cpu_threads: int,
    disk_gb: int,
    memory_per_slice_gb: int,
    cpu_overcommit_ratio: float,
    raid_level: str | None,
    slice_service_user: str,
    ovh_order_id: str | None,
    status: str,
    box_generation: int,
    uplink_mbps: int,
) -> BareMetalServer:
    """Build a BareMetalServer from register inputs (slot count = floor(ram_gb / memory_per_slice_gb))."""
    now = datetime.now(timezone.utc)
    return BareMetalServer(
        id=BareMetalServerDbId(str(uuid4())),
        ovh_order_id=ovh_order_id,
        ovh_service_name=ovh_service_name,
        plan_code=plan_code,
        region=region,
        public_address=public_address,
        cpu_cores=cpu_cores,
        cpu_threads=cpu_threads,
        ram_gb=ram_gb,
        disk_gb=disk_gb,
        memory_per_slice_gb=memory_per_slice_gb,
        cpu_overcommit_ratio=cpu_overcommit_ratio,
        slot_count=compute_slot_count(ram_gb, memory_per_slice_gb),
        raid_level=raid_level,
        slice_service_user=slice_service_user,
        status=BareMetalServerStatus(status),
        created_at=now,
        updated_at=now,
        box_generation=box_generation,
        uplink_mbps=uplink_mbps,
    )


@pure
def gen2_register_disk_shortfall_or_none(*, ram_gb: int, disk_gb: int) -> str | None:
    """The warning ``server register`` logs for a gen-2 box whose disk holds fewer than its full complement, else None."""
    fit = compute_gen2_box_default_machine_fit(ram_gb=ram_gb, disk_gb=disk_gb)
    if fit.is_sufficient:
        return None
    return (
        describe_gen2_box_disk_shortfall(fit, ram_gb=ram_gb, disk_gb=disk_gb)
        + ". Registering anyway: the two-budget accounting fits fewer machines on it, and the gen-2 prep "
        "re-measures disk_gb from the storage partition at setup."
    )


def compute_server_slice_sizing(server: BareMetalServer, machine_units: int | None) -> dict[str, int]:
    """Compute the per-slice VM sizing for ``server`` from its stored inputs + specs.

    Returns ``{vcpus, memory_mib, disk_gib, advertised_memory_gb,
    row_memory_units, row_disk_gb}`` -- the last two being the values stamped on
    the pool row's sizing columns -- plus (gen-2 only)
    ``{units, total_units, disk_budget_gib}`` -- the unit-based sizing of
    specs/slice-fleet, where ``machine_units`` (default: the fleet's uniform
    default size) drives RAM, proportional vCPUs, and the carve-time data disk.
    Gen-1 boxes keep the slot-derived sizing and refuse a units override; their
    row records the disk size the machine has after the gen-2 cutover.
    Raises ``BareMetalProvisioningError`` if the server is missing the inputs a
    pre-sizing registration would have set (re-register it first).
    """
    if (
        server.memory_per_slice_gb is None
        or server.cpu_overcommit_ratio is None
        or server.cpu_threads is None
        or server.disk_gb is None
        or server.slot_count <= 0
    ):
        raise BareMetalProvisioningError(
            f"server {server.id} is missing sizing inputs (memory_per_slice_gb / cpu_overcommit_ratio / "
            f"cpu_threads / disk_gb / slot_count); re-register it with the slice-sizing options"
        )
    if server.box_generation >= FIRST_QEMU_BOX_GENERATION:
        if server.ram_gb is None:
            raise BareMetalProvisioningError(
                f"server {server.id} has no recorded ram_gb; re-register it before baking gen-2 machines"
            )
        units = machine_units if machine_units is not None else DEFAULT_MACHINE_UNITS
        if not is_allowed_machine_units(units):
            raise BareMetalProvisioningError(
                f"--units must be a multiple of {MACHINE_UNITS_STEP} between {MACHINE_UNITS_STEP} and "
                f"{MAX_MACHINE_UNITS}, got {units}"
            )
        total_units = compute_box_total_units(server.ram_gb)
        data_disk_gib = compute_machine_data_disk_gib(units)
        return {
            "advertised_memory_gb": units,
            "vcpus": compute_machine_vcpus(server.cpu_threads, server.cpu_overcommit_ratio, units, total_units),
            # The guest's boot RAM (units minus the per-machine holdback): what qemu
            # gets and what the bake-time container cap is derived from, so the cap
            # matches what the guest's own reconcile oneshot computes at every boot.
            "memory_mib": compute_machine_guest_memory_mib(units),
            "disk_gib": data_disk_gib,
            "units": units,
            "total_units": total_units,
            "disk_budget_gib": compute_gen2_disk_budget_gib(server.disk_gb),
            "row_memory_units": units,
            "row_disk_gb": data_disk_gib,
        }
    if machine_units is not None:
        raise BareMetalProvisioningError(
            f"server {server.id} is generation 1; --units applies only to gen-2 boxes (specs/slice-fleet)"
        )
    gen1_data_disk_gib = compute_slice_disk_gib(server.disk_gb, server.slot_count)
    return {
        "advertised_memory_gb": server.memory_per_slice_gb,
        "vcpus": compute_slice_vcpus(server.cpu_threads, server.slot_count, server.cpu_overcommit_ratio),
        "memory_mib": compute_slice_memory_mib(server.memory_per_slice_gb),
        "disk_gib": gen1_data_disk_gib,
        "row_memory_units": server.memory_per_slice_gb,
        "row_disk_gb": compute_gen1_migrated_data_disk_gib(gen1_data_disk_gib),
    }


@pure
def estimate_gen2_machine_capacity(sizing: Mapping[str, int]) -> int:
    """How many machines of ``sizing``'s unit size the box could hold, against its two gen-2 budgets.

    A soft pre-check estimate (specs/slice-fleet), shared by the bake and the
    cutover migrate: existing occupancy is approximated at the same machine
    size (the real sizes live in the on-box env files, which the authoritative
    reserve reads). ``sizing`` is ``compute_server_slice_sizing``'s gen-2 dict.
    """
    machine_footprint_mib = compute_machine_memory_footprint_mib(sizing["units"])
    memory_capacity = (sizing["total_units"] * 1024) // machine_footprint_mib
    disk_capacity = sizing["disk_budget_gib"] // (GEN2_BOOT_DISK_GIB + sizing["disk_gib"])
    return min(memory_capacity, disk_capacity)


def slice_advertised_attributes(sizing: dict[str, int]) -> dict[str, Any]:
    """The lease attributes a slice advertises (so a lease matches a slice or a VPS identically)."""
    return {"memory_gb": sizing["advertised_memory_gb"], "cpus": sizing["vcpus"]}


@pure
def resolve_slice_container_runtime(
    server: BareMetalServer, runtime_override: SliceContainerRuntime | None
) -> SliceContainerRuntime | None:
    """The Docker runtime a bake on ``server`` creates the workspace container under.

    Gen-2 boxes carve runsc-native slices (the guest image ships gVisor);
    ``--docker-runtime runc`` overrides that for a side-by-side comparison bake.
    Gen-1 (lima) guests have no runsc and take no override: None leaves the
    container on the guest docker's default (runc) with no start args added.
    Raises ``BareMetalProvisioningError`` on an override for a gen-1 box.
    """
    if server.box_generation < FIRST_QEMU_BOX_GENERATION:
        if runtime_override is not None:
            raise BareMetalProvisioningError(
                f"server {server.id} is generation 1; --docker-runtime applies only to gen-2 boxes "
                "(a lima guest has no runsc)"
            )
        return None
    return runtime_override if runtime_override is not None else GEN2_CONTAINER_RUNTIME


# The reserved pseudo-env label stamped into the lima names of the cache
# pre-warm verb's throwaway seed slices (specs/remote-workspaces-in-ci.md).
# It parses as a ci-tier owner (``tier_for_env_name`` sees the ``ci-`` prefix),
# so a warm slice a killed invocation leaked is reclaimed by the age-based
# ``server sweep-ci-slices`` like any other CI slice.
CI_WARM_PSEUDO_ENV_NAME: Final[str] = "ci-warm"

# Per-slice ``mngr create`` hard timeout (carve + DEFAULT_WORKSPACE_TEMPLATE container build + agent
# bootstrap). 45 min gives headroom for the build under concurrency; the bake's
# semaphore keeps concurrency low enough that any single create stays well under
# it. Applied per create, so one slice timing out never aborts the others.
_SLICE_MNGR_CREATE_TIMEOUT_SECONDS: Final[int] = 2700

# Default cap on how many slices bake concurrently per invocation (overridable via
# --max-concurrency). Bounds box CPU/IO/network contention so each create finishes
# within its timeout; the rest queue and start as slots free.
DEFAULT_SLICE_BAKE_CONCURRENCY: Final[int] = 4

# How many times one requested slice is baked before its failure is recorded. A
# failed bake destroys its VM and writes no pool row, so each retry is a clean fresh
# slice -- transient failures (an SSH reset, a flaky image build) self-heal instead
# of permanently consuming one of the requested slices. Production seed builds have
# been observed failing 2-3 times in a row before succeeding, so allow 3 attempts.
_SLICE_BAKE_ATTEMPT_COUNT: Final[int] = 3


class SliceManagementTrust(FrozenModel):
    """What a slice bake installs for management SSH: the tier CA (gen-2) or the static pool public key (gen-1)."""

    trusted_user_ca_public_key: str | None = Field(
        description="Gen-2: the tier's SSH CA the VM root and container trust for certificate logins"
    )
    pool_public_key: str | None = Field(
        description="Gen-1: the static pool public key authorized on the VM root and container (CLEANUP with gen-1)"
    )

    def slice_create_overrides(self) -> dict[str, str]:
        overrides: dict[str, str] = {}
        if self.trusted_user_ca_public_key is not None:
            overrides["trusted_user_ca_public_key"] = self.trusted_user_ca_public_key
        if self.pool_public_key is not None:
            overrides["pool_authorized_public_key"] = self.pool_public_key
        return overrides


def slice_management_trust_for_server(
    server: BareMetalServer, identities: ManagementIdentityResolver
) -> SliceManagementTrust:
    """The management trust a bake onto ``server`` installs: the committed tier CA on gen-2, the pool key on gen-1."""
    if server.box_generation >= FIRST_QEMU_BOX_GENERATION:
        return SliceManagementTrust(
            trusted_user_ca_public_key=require_tier_ssh_ca_public_key("baking a gen-2 slice"), pool_public_key=None
        )
    return SliceManagementTrust(
        trusted_user_ca_public_key=None,
        pool_public_key=derive_ssh_public_key(identities.private_key_path_for(server.box_generation)),
    )


def resolve_bake_management_trust_and_key(
    server: BareMetalServer, identities: ManagementIdentityResolver
) -> tuple[SliceManagementTrust, Path]:
    """The trust a bake onto ``server`` installs and the key it dials the box with, in that order.

    The committed-CA check is local and comes first, so a tier whose SSH CA is
    not brought up refuses with the bring-up pointer rather than with the
    failed Vault certificate sign the identity resolution would hit.
    """
    management_trust = slice_management_trust_for_server(server, identities)
    return management_trust, identities.private_key_path_for(server.box_generation)


def _build_slice_create_args(
    *,
    server: BareMetalServer,
    sizing: dict[str, int],
    region: str,
    env_name: str | None,
    # The management trust the slice is baked with: the tier's SSH CA public
    # key on a gen-2 box (VM root and container trust certificates), the static
    # pool public key on a gen-1 box (authorized on both).
    management_trust: SliceManagementTrust,
    private_key_path: Path,
    ssh_user: str,
    port_range_start: int,
    port_range_end: int,
    default_workspace_template_cache_tag: str | None,
    # The runtime the workspace container is created under (gen-2 boxes only;
    # see ``resolve_slice_container_runtime``). None adds no runtime knobs.
    container_runtime: SliceContainerRuntime | None,
    # The host id the bake chose up front (its ``baking`` row is already inserted
    # under it), so the provider carves the slice the row names.
    slice_host_id: HostId,
) -> list[str]:
    """Render the ``-S`` provider-config overrides that point one slice bake at this box.

    The carve knobs (vcpus / memory / disk) are computed per box so the leased
    host's actual size matches its advertised attributes; the box address + service
    user + management key and trust (the tier CA on gen-2, the pool key on gen-1) +
    the owning env + the box's slot count + the full box port range are passed the
    same way. The on-box reservation lock makes concurrent
    bakes (this env's and other envs') pick distinct ports from the shared range, so
    every bake is handed the full range rather than a disjoint window.
    """
    # Fail closed: the slice carve SSHes the box with strict host-key pinning, so
    # the box's host key must be known. It is set at provision (or by the one-time
    # keyscan backfill); refuse to bake against an un-keyscanned box rather than
    # fall back to trust-on-first-use.
    if not server.box_host_public_key:
        raise BareMetalProvisioningError(
            f"bare-metal server {server.id} has no box_host_public_key; run the one-time "
            "`minds-admin pool backfill-host-keys` (or re-provision the box) before baking slices"
        )
    prefix = f"providers.{SLICE_PROVIDER_INSTANCE_NAME}"
    management_dial = resolve_server_management_dial(server)
    overrides = {
        # The forwarded slice ports stay on the public address (they are not
        # locked down); the carve's management SSH takes the resolved dial (a
        # userspace-tunnel local forward or the overlay once :22 locks down).
        "box_public_address": str(server.public_address),
        "box_management_address": management_dial.host,
        "box_management_ssh_port": str(management_dial.port),
        "box_ssh_user": ssh_user,
        "pool_private_key_path": str(private_key_path),
        **management_trust.slice_create_overrides(),
        # The box's pinned sshd host key (same -S-with-spaces pattern as the pool key).
        "box_host_public_key": server.box_host_public_key,
        # Lease-region label (the app's region code, e.g. US-EAST-VA), NOT the
        # box's raw datacenter code -- so the connector's region-filtered lease
        # matches what the minds create form requests.
        "slice_region": region,
        "slice_host_id": str(slice_host_id),
        "slice_vcpus": str(sizing["vcpus"]),
        "slice_memory_mib": str(sizing["memory_mib"]),
        "slice_disk_gib": str(sizing["disk_gib"]),
        # The box's total slot count: the on-box reservation refuses to carve once
        # the box already holds this many slices (the cross-env over-allocation guard).
        "slice_slot_count": str(server.slot_count),
        "slice_port_range_start": str(port_range_start),
        "slice_port_range_end": str(port_range_end),
        # The box's slice-fleet generation selects the provider's backend (lima
        # vs raw qemu); a box only ever runs one generation.
        "box_generation": str(server.box_generation),
    }
    # Gen-2 machine sizing (specs/slice-fleet): the machine's units and the
    # box's two budgets, which the on-box reserve's accounting enforces.
    if "units" in sizing:
        overrides["slice_units"] = str(sizing["units"])
        overrides["slice_box_total_units"] = str(sizing["total_units"])
        overrides["slice_box_disk_budget_gib"] = str(sizing["disk_budget_gib"])
    overrides["slice_uplink_mbps"] = str(server.uplink_mbps)
    # Gen-2 containers are created under the resolved runtime with /run and
    # /tmp on tmpfs (the list rides as JSON, which ``-S`` parses into the
    # provider config's tuple). A gen-1 bake gets neither: its lima guest has
    # no runsc, so `--runtime runsc` would fail the create with "unknown
    # runtime".
    if container_runtime is not None:
        overrides["docker_runtime"] = docker_runtime_name(container_runtime)
        overrides["default_start_args"] = json.dumps(list(GEN2_CONTAINER_TMPFS_START_ARGS))
    # The owning env (stamped into the slice's lima names) is omitted entirely when
    # absent, so the provider falls back to legacy un-stamped names.
    if env_name is not None:
        overrides["slice_env_name"] = env_name
    # Production (--from-tag) bakes enable the per-box DEFAULT_WORKSPACE_TEMPLATE image cache: the first
    # slice builds + seeds the box tar, the rest docker-load it. Omitted for dev bakes.
    if default_workspace_template_cache_tag is not None:
        overrides["default_workspace_template_cache_tag"] = default_workspace_template_cache_tag
    args: list[str] = []
    for key, value in overrides.items():
        args.extend(["-S", f"{prefix}.{key}={value}"])
    return args


class BakeRowLedger(MutableModel):
    """The ``baking`` rows a bake has inserted and not yet finished or deleted.

    A worker records its row right after the insert and forgets it once the row
    is finished or deleted; whatever is left when the bake ends belongs to a
    worker that never reached its own cleanup (a kill mid-create), and the
    bake's final sweep deletes it.
    """

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _host_name_by_row_id: dict[str, str] = PrivateAttr(default_factory=dict)

    def record(self, row_id: str, host_name: str) -> None:
        with self._lock:
            self._host_name_by_row_id[row_id] = host_name

    def forget(self, row_id: str) -> None:
        with self._lock:
            self._host_name_by_row_id.pop(row_id, None)

    def drain(self) -> list[tuple[str, str]]:
        """Return and clear every recorded ``(row_id, host_name)``."""
        with self._lock:
            leftovers = list(self._host_name_by_row_id.items())
            self._host_name_by_row_id.clear()
        return leftovers


def _delete_leftover_baking_rows(database_url: str, row_ledger: BakeRowLedger) -> None:
    """Drop the ``baking`` rows of workers that never reached their own cleanup."""
    for row_id, host_name in row_ledger.drain():
        logger.warning("Deleting the baking row {} of slice {} left behind by an interrupted bake", row_id, host_name)
        _delete_baking_row(database_url, row_id, host_name)


def _delete_baking_row_and_forget(database_url: str, row_ledger: BakeRowLedger, row_id: str, host_name: str) -> None:
    """Drop a failed bake's ``baking`` row and take it off the ledger: its worker reached its own cleanup."""
    _delete_baking_row(database_url, row_id, host_name)
    row_ledger.forget(row_id)


def _delete_baking_row(database_url: str, row_id: str, host_name: str) -> None:
    """Best-effort: drop a failed bake's ``baking`` row so it neither holds a slot nor looks like a live bake."""
    try:
        conn = psycopg2.connect(database_url)
        try:
            if not delete_baking_slice_pool_host(conn, row_id):
                logger.warning("Baking row {} of slice {} was already gone or claimed", row_id, host_name)
        finally:
            conn.close()
    except psycopg2.Error as exc:
        logger.warning(
            "Could not delete baking row {} of slice {} (an operator destroy will): {}", row_id, host_name, exc
        )


def _rollback_slice_vm(
    *, server: BareMetalServer, ssh_user: str, private_key_path: Path, host_id: str, env_name: str | None
) -> None:
    """Best-effort: destroy a carved slice VM whose later bake/bookkeeping failed, so it does not leak.

    Drives the box's generation-specific teardown over SSH (via the same
    SSH-backed client the carve uses) for the deterministic instance/disk names
    derived from ``host_id`` and the owning ``env_name``. Swallows + logs any
    failure -- the caller is already on a failure path -- so it never masks the
    original error.
    """
    rollback_dial = resolve_server_management_dial(server)
    client = build_slice_vm_client(
        box_generation=server.box_generation,
        box_address=rollback_dial.host,
        box_ssh_port=rollback_dial.port,
        box_ssh_user=ssh_user,
        private_key_path=str(private_key_path),
        box_host_public_key=server.box_host_public_key,
    )
    instance_id = VpsInstanceId(slice_instance_name(HostId(host_id), env_name))
    try:
        client.destroy_instance(instance_id)
    except (MngrError, OSError) as exc:
        logger.warning("Rollback of orphaned slice VM for {} on {} failed: {}", host_id, server.public_address, exc)


def _slice_run_in_container(
    baked: BakedPoolHost, label: str, command: str, timeout_seconds: float
) -> tuple[int | None, str, str]:
    """Run a shell command inside a slice's container by SSHing the create-reported port.

    The :class:`~imbue.minds_admin.bake.pool_bake.ContainerCommandRunner` for
    slices: a slice's per-host forwarded port lives only in the create process's
    memory, so a fresh ``mngr`` can't resolve it -- instead we SSH straight to the
    container's box-forwarded port (``baked.ssh_port``) with the container key the
    create recorded. Wrapped in ``bash -lc`` so ``uv``/``mngr`` are on PATH in the
    DEFAULT_WORKSPACE_TEMPLATE image. Returns ``(returncode, stdout, stderr)``.
    """
    if not baked.ssh_host or baked.ssh_port is None or not baked.ssh_key_path:
        return 1, "", f"baked slice {baked.host_name} missing container SSH connection info"
    if not baked.container_host_public_key:
        return 1, "", f"baked slice {baked.host_name} missing container host public key; cannot pin it"
    # Bake-time op to a container we just created, reached at a box-forwarded port
    # that earlier slices have reused with different host keys. Pin the container's
    # known host key in a throwaway known_hosts file (NOT the operator's shared one,
    # whose stale entry for this box:port from a prior slice would mismatch) -- so we
    # still get strict host-key checking with no trust-on-first-use.
    known_hosts_fd, known_hosts_path = tempfile.mkstemp(prefix="mngr_slice_known_hosts_")
    os.close(known_hosts_fd)
    try:
        add_host_to_known_hosts(
            Path(known_hosts_path), baked.ssh_host, baked.ssh_port, baked.container_host_public_key
        )
        ssh_command = [
            "ssh",
            "-i",
            baked.ssh_key_path,
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={quote_ssh_option_value(known_hosts_path)}",
            "-o",
            "ConnectTimeout=20",
            "-o",
            "ServerAliveInterval=30",
            "-p",
            str(baked.ssh_port),
            f"{baked.ssh_user}@{baked.ssh_host}",
            f"bash -lc {shlex.quote(command)}",
        ]
        cg = ConcurrencyGroup(name=f"slice-container-{label}")
        with cg:
            result = cg.run_process_to_completion(command=ssh_command, timeout=timeout_seconds, is_checked_after=False)
        return result.returncode, result.stdout, result.stderr
    finally:
        Path(known_hosts_path).unlink(missing_ok=True)


def _bake_one_slice(
    *,
    server: BareMetalServer,
    sizing: dict[str, int],
    lease_attributes: dict[str, Any],
    region: str,
    env_name: str | None,
    workspace_dir: Path,
    management_trust: SliceManagementTrust,
    private_key_path: Path,
    database_url: str,
    port_range_start: int,
    port_range_end: int,
    is_env_converge_wait_skipped: bool,
    default_workspace_template_cache_tag: str | None,
    container_runtime: SliceContainerRuntime | None,
    # The invocation's ephemeral bake namespace overrides (MNGR_HOST_DIR / MNGR_PREFIX),
    # so the inner ``mngr create`` never touches the operator's own mngr data root.
    extra_create_env: Mapping[str, str],
    row_ledger: BakeRowLedger,
) -> SliceBakeOutcome:
    """Bake one slice (laptop-driven ``mngr create`` against the slice provider) + insert its pool row.

    Returns an outcome (never raises). ``bake_pool_host`` carves the VM (over
    SSH on the box, inside the slice provider) and bakes the shared container; the
    shared :func:`finalize_baked_pool_host` then hardens the container sshd and
    clears the baked git identity over the slice (direct-SSH) transport. Any
    failure once the VM exists rolls the VM back so it does not leak its box
    slot/ports (a ``mngr create`` failure is already rolled back by the provider).
    """
    ssh_user = box_service_user(server)
    host_name = f"slice-{uuid4().hex}"
    # The slice advertises the operator's lease attributes (e.g. repo_branch_or_tag,
    # so the minds fast-path lease matches) with the derived per-box size stamped on
    # top (authoritative). Mirrors how OVH pool hosts carry the operator's attributes.
    attributes = {**lease_attributes, **slice_advertised_attributes(sizing)}
    attributes_json = json.dumps(attributes)
    # The slice's identity is chosen here, not by the provider, so its row can be
    # inserted (as ``baking``) BEFORE the carve: the orphan reap then sees the
    # in-flight VM as tracked instead of as a rowless orphan to destroy.
    host_id_obj = HostId.generate()
    row_id = str(uuid4())
    try:
        conn = psycopg2.connect(database_url)
        try:
            insert_baking_slice_pool_host(
                conn,
                build_baking_slice_pool_host_insert_values(
                    row_id=row_id,
                    box_public_address=str(server.public_address),
                    host_id=str(host_id_obj),
                    host_name=host_name,
                    attributes_json=attributes_json,
                    region=region,
                    bare_metal_server_id=str(server.id),
                    slice_instance_name=slice_instance_name(host_id_obj, env_name),
                    slice_disk_name=slice_disk_name(host_id_obj, env_name),
                    box_generation=server.box_generation,
                    memory_units=sizing["row_memory_units"],
                    disk_gb=sizing["row_disk_gb"],
                ),
            )
        finally:
            conn.close()
        row_ledger.record(row_id, host_name)
        try:
            baked = bake_pool_host(
                provider_instance=SLICE_PROVIDER_INSTANCE_NAME,
                host_name=host_name,
                attributes=attributes,
                workspace_dir=workspace_dir,
                extra_create_args=_build_slice_create_args(
                    server=server,
                    sizing=sizing,
                    region=region,
                    env_name=env_name,
                    management_trust=management_trust,
                    private_key_path=private_key_path,
                    ssh_user=ssh_user,
                    port_range_start=port_range_start,
                    port_range_end=port_range_end,
                    default_workspace_template_cache_tag=default_workspace_template_cache_tag,
                    container_runtime=container_runtime,
                    slice_host_id=host_id_obj,
                ),
                extra_create_env=extra_create_env,
                mngr_create_timeout_seconds=_SLICE_MNGR_CREATE_TIMEOUT_SECONDS,
            )
        except (PoolBakeError, BareMetalProvisioningError, MngrError, OSError):
            # The provider rolled its VM back; only the pre-carve row is left.
            _delete_baking_row_and_forget(database_url, row_ledger, row_id, host_name)
            raise
        if baked.host_id != str(host_id_obj):
            # The provider ignored the requested id: the row names a slice that does not exist.
            _rollback_slice_vm(
                server=server,
                ssh_user=ssh_user,
                private_key_path=private_key_path,
                host_id=baked.host_id,
                env_name=env_name,
            )
            _delete_baking_row_and_forget(database_url, row_ledger, row_id, host_name)
            raise BareMetalProvisioningError(
                f"slice {host_name} was carved as {baked.host_id}, not the requested {host_id_obj}"
            )
        # The VM now exists; any failure in the post-create steps or the row update must
        # tear it down so it does not leak its box slot + forwarded ports.
        try:
            if baked.outer_ssh_port is None or baked.ssh_port is None:
                raise BareMetalProvisioningError(
                    f"slice {host_name} create JSON missing the forwarded ports (vm={baked.outer_ssh_port}, "
                    f"container={baked.ssh_port})"
                )
            finalize_baked_pool_host(_slice_run_in_container, baked, host_name=host_name)
            # Let the DEFAULT_WORKSPACE_TEMPLATE env-converge slow phase (heavy apt + browser
            # download, record capture, rootfs stamp) finish before we stop the services agent:
            # the stop kills it mid-run, shipping an image without apt.json / the rootfs stamp,
            # and stopping mid-apt corrupts dpkg (see wait_for_env_converge). Dev bakes may skip
            # this wait to save the few minutes; the tradeoff is the baked container's converge
            # can be left incomplete/corrupt (acceptable for slow-path dev bakes, whose container
            # is rebuilt on lease anyway).
            if is_env_converge_wait_skipped:
                logger.warning(
                    "Skipping env-converge wait for slice {} (dev bake); its baked converge may be incomplete",
                    host_name,
                )
            else:
                wait_for_env_converge(_slice_run_in_container, baked, host_name=host_name)
            # Stop the services agent so it lands in the pool STOPPED.
            # The fast-path lease then *starts* the adopted agent, which re-runs the
            # DEFAULT_WORKSPACE_TEMPLATE bootstrap (it runs on every start, e.g.
            # re-supplying the neutral git identity finalize unset above). Without
            # this stop the agent stays running from bake through lease and the
            # adopting user's boot-time setup never re-runs. We stop it inside the
            # container (the operator's mngr can't resolve the slice's in-memory
            # forwarded ports, so the OVH local-stop approach can't be reused here).
            stop_rc, _stop_out, stop_err = _slice_run_in_container(
                baked,
                "stop-services",
                f"cd {BAKED_SERVICES_CHECKOUT_PATH} && uv run mngr stop {BAKED_SERVICES_AGENT_NAME}",
                120.0,
            )
            if stop_rc != 0:
                raise BareMetalProvisioningError(
                    f"stopping the services agent on slice {host_name} failed (exit {stop_rc}): {stop_err.strip()}"
                )
            # Last gate before the pool-row insert: the parked container must hold only the
            # primary services agent. Runs after the stop so nothing (the bootstrap included)
            # can create an agent once the check has passed; a failure here rolls the VM back
            # instead of shipping a host with a leaked agent (and thereby refuses old
            # default-workspace-template tags whose bootstrap creates a boot chat).
            verify_only_primary_agents_baked(_slice_run_in_container, baked, host_name=host_name)
            if not baked.outer_host_public_key or not baked.container_host_public_key:
                raise BareMetalProvisioningError(
                    f"baked slice {host_name} did not surface its sshd host public keys "
                    "(needs a slice provider that emits them in `mngr create --format json`); cannot finish its pool row"
                )
            conn = psycopg2.connect(database_url)
            try:
                is_finished = finish_baking_slice_pool_host(
                    conn,
                    row_id,
                    agent_id=baked.agent_id,
                    vm_ssh_host_port=baked.outer_ssh_port,
                    container_ssh_host_port=baked.ssh_port,
                    outer_host_public_key=baked.outer_host_public_key,
                    container_host_public_key=baked.container_host_public_key,
                )
            finally:
                conn.close()
            if not is_finished:
                # An operator destroy claimed the stale-looking row mid-bake (or dropped
                # it): the slice is unwanted, so it must not survive as a rowless VM.
                raise BareMetalProvisioningError(
                    f"slice {host_name}'s baking row {row_id} was claimed or removed during the bake; discarding the VM"
                )
        except (PoolBakeError, BareMetalProvisioningError, MngrError, psycopg2.Error, OSError):
            _rollback_slice_vm(
                server=server,
                ssh_user=ssh_user,
                private_key_path=private_key_path,
                host_id=baked.host_id,
                env_name=env_name,
            )
            _delete_baking_row_and_forget(database_url, row_ledger, row_id, host_name)
            raise
        row_ledger.forget(row_id)
        logger.info(
            "Slice {} ready on {} (host_id={}, ports vm={}/container={})",
            host_name,
            server.public_address,
            baked.host_id,
            baked.outer_ssh_port,
            baked.ssh_port,
        )
        return SliceBakeOutcome(
            host_name=host_name,
            server_id=str(server.id),
            status=SliceBakeOutcomeStatus.SUCCEEDED,
            host_id=baked.host_id,
            agent_id=baked.agent_id,
            vm_ssh_port=baked.outer_ssh_port,
            container_ssh_port=baked.ssh_port,
            attributes=attributes,
        )
    except (PoolBakeError, BareMetalProvisioningError, MngrError, psycopg2.Error, OSError) as exc:
        logger.warning("Slice bake {} failed: {}", host_name, exc)
        return SliceBakeOutcome(
            host_name=host_name, server_id=str(server.id), status=SliceBakeOutcomeStatus.FAILED, error=str(exc)
        )


def _run_bake_attempts(
    bake_once: Callable[[], SliceBakeOutcome],
    attempt_count: int,
    *,
    termination_event: threading.Event,
) -> SliceBakeOutcome:
    """Run bake_once up to attempt_count times, returning the first success (else the last failure).

    A failed bake destroys its VM and writes no pool row, so each attempt is a
    clean fresh slice: a transient failure (an SSH reset, a flaky image build)
    self-heals instead of permanently consuming one of the requested slices.

    ``termination_event`` stops the retries: a terminated bake's kill sweep makes
    every in-flight attempt fail, and retrying those would spawn replacement
    ``mngr create`` workers (new VMs) after the operator killed the bake.
    """
    last_outcome: SliceBakeOutcome | None = None
    for attempt_idx in range(attempt_count):
        outcome = bake_once()
        if outcome.status == SliceBakeOutcomeStatus.SUCCEEDED:
            return outcome
        last_outcome = outcome
        if termination_event.is_set():
            logger.info("Slice bake {} failed after the bake was terminated; not retrying", outcome.host_name)
            return outcome
        if attempt_idx < attempt_count - 1:
            logger.warning(
                "Slice bake {} failed (attempt {}/{}); retrying with a fresh slice: {}",
                outcome.host_name,
                attempt_idx + 1,
                attempt_count,
                outcome.error,
            )
    if last_outcome is None:
        raise BareMetalProvisioningError(f"attempt_count must be positive, got {attempt_count}")
    return last_outcome


def _bake_one_slice_with_retry(*, termination_event: threading.Event, **worker_kwargs: Any) -> SliceBakeOutcome:
    """The bake fan-out worker: one requested slice, baked with bounded retries."""
    if termination_event.is_set():
        # A worker still queued on the concurrency semaphore when the bake was
        # terminated: its ``mngr create`` never started, and starting it now would
        # carve a brand-new VM after the kill sweep (and stall the fan-out's
        # post-interruption re-join for the create's full timeout).
        logger.info("Slice bake terminated before this queued slice started; not baking it")
        return SliceBakeOutcome(
            host_name="slice-never-started",
            server_id=str(worker_kwargs["server"].id),
            status=SliceBakeOutcomeStatus.FAILED,
            error="the bake was terminated before this slice's first attempt started",
        )
    return _run_bake_attempts(
        lambda: _bake_one_slice(**worker_kwargs),
        _SLICE_BAKE_ATTEMPT_COUNT,
        termination_event=termination_event,
    )


# The per-item result type produced by a bounded fan-out's workers (bake and
# destroy outcomes today).
OutcomeT = TypeVar("OutcomeT")


def _run_worker_into_outcomes(
    *,
    worker: Callable[..., OutcomeT],
    worker_kwargs: Mapping[str, Any],
    semaphore: "threading.Semaphore",
    total: int,
    progress_noun: str,
    describe_outcome: Callable[[OutcomeT], str],
    outcomes: list[OutcomeT],
    outcomes_lock: "threading.Lock",
) -> None:
    """Thread target: run one outcome worker under the concurrency semaphore, recording progress.

    The semaphore caps how many workers run at once (the rest block here until a
    slot frees). Workers return their outcome instead of raising, so one item
    failing never aborts the rest.
    """
    with semaphore:
        outcome = worker(**worker_kwargs)
    with outcomes_lock:
        outcomes.append(outcome)
        done = len(outcomes)
    logger.info("{} progress: {}/{} done -- {}", progress_noun, done, total, describe_outcome(outcome))


def run_outcome_workers_in_bounded_threads(
    *,
    worker: Callable[..., OutcomeT],
    worker_kwargs_list: Sequence[Mapping[str, Any]],
    max_concurrency: int,
    thread_name_prefix: str,
    progress_noun: str,
    describe_outcome: Callable[[OutcomeT], str],
    # The exception types that count as an interruption of the start/join loop (e.g.
    # the bake's SIGTERM-raised SliceBakeTerminatedError). Empty: nothing is
    # intercepted and any exception propagates immediately.
    interruption_exception_types: tuple[type[Exception], ...],
    # Invoked once when an interruption exception arrives, before the threads are
    # re-joined and the exception re-raised -- the caller's chance to kill in-flight
    # worker subprocesses so the re-join can finish.
    on_join_interrupted: Callable[[], None] | None,
) -> list[OutcomeT]:
    """Run one worker call per kwargs mapping in parallel threads, at most ``max_concurrency`` at once.

    The shared fan-out used by both the slice bake and the pool-host destroy.
    Returns the outcomes in completion order. Workers must return their outcome
    rather than raising -- an exception escaping a worker aborts the whole batch
    at join time (``ObservableThread.join`` re-raises it).
    """
    outcomes: list[OutcomeT] = []
    outcomes_lock = threading.Lock()
    worker_semaphore = threading.Semaphore(max_concurrency)
    threads = [
        ObservableThread(
            target=_run_worker_into_outcomes,
            kwargs=dict(
                worker=worker,
                worker_kwargs=worker_kwargs,
                semaphore=worker_semaphore,
                total=len(worker_kwargs_list),
                progress_noun=progress_noun,
                describe_outcome=describe_outcome,
                outcomes=outcomes,
                outcomes_lock=outcomes_lock,
            ),
            name=f"{thread_name_prefix}-{idx}",
        )
        for idx, worker_kwargs in enumerate(worker_kwargs_list)
    ]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    except interruption_exception_types:
        # The exception always propagates after the hook + re-join.
        if on_join_interrupted is not None:
            on_join_interrupted()
            for thread in threads:
                # An interruption during the start loop can leave later threads
                # never-started; joining those would raise a second error.
                if thread.ident is not None:
                    thread.join()
        raise
    return outcomes


def _describe_bake_outcome(outcome: SliceBakeOutcome) -> str:
    return f"{outcome.host_name} {outcome.status}"


def _run_bake_fan_out(
    *,
    bake_worker_kwargs: Mapping[str, Any],
    slice_count: int,
    max_concurrency: int,
    progress_noun: str,
    is_main_thread: bool,
    termination_event: threading.Event,
) -> list[SliceBakeOutcome]:
    """Run one phase of the slice bake fan-out (the seed phase or the fill phase)."""
    worker_kwargs = {**bake_worker_kwargs, "termination_event": termination_event}
    return run_outcome_workers_in_bounded_threads(
        worker=_bake_one_slice_with_retry,
        worker_kwargs_list=[worker_kwargs for _ in range(slice_count)],
        max_concurrency=max_concurrency,
        thread_name_prefix="bake",
        progress_noun=progress_noun,
        describe_outcome=_describe_bake_outcome,
        interruption_exception_types=(SliceBakeTerminatedError,),
        on_join_interrupted=lambda: _handle_bake_join_interruption(is_main_thread, termination_event),
    )


def _handle_bake_join_interruption(is_main_thread: bool, termination_event: threading.Event) -> None:
    """React to the bake fan-out's join loop being interrupted (a SIGTERM/SIGINT-raised error).

    Without this, the in-flight ``mngr create`` workers would be reparented and keep
    carving VMs after we exit. Set the termination event first (so a killed worker's
    per-slice retry loop returns its failure instead of spawning a replacement bake),
    ignore further signals, then kill the workers so no new VM appears; the fan-out
    re-joins the worker threads afterward and the bake's ``finally`` reaps the orphans.
    """
    termination_event.set()
    if is_main_thread:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    logger.warning("Slice bake terminated by signal; killing in-flight workers before reap")
    _kill_bake_worker_processes()
    # The recursive kill took the WireGuard tunnel processes with it; dropping
    # the memoized dial makes the reap open a fresh one.
    close_box_management_tunnels()


def _raise_on_bake_termination_signal(signum: int, _frame: object) -> None:
    """SIGTERM/SIGINT handler: raise so the bake's main-thread try/except runs cleanup.

    Kept trivial (just raises) so the kill+reap logic can live in ``allocate_slices``
    where the server / key / DSN are in scope, rather than being bound into the
    handler. Raising interrupts the main thread's ``thread.join()``.
    """
    raise SliceBakeTerminatedError(f"slice bake received signal {signum}")


def _kill_bake_worker_processes(grace_seconds: float = 5.0) -> None:
    """Terminate every child process of this bake (the in-flight ``mngr create`` workers).

    On a top-level kill (e.g. the minds wrapper's subprocess timeout SIGTERMs us),
    the worker subprocesses would otherwise be reparented and keep carving VMs after
    we exit -- leaking both processes and VMs. SIGTERM them (then SIGKILL stragglers)
    so no new VM can appear on the box once the orphan reap has run.
    """
    children = psutil.Process().children(recursive=True)
    for child in children:
        try:
            child.terminate()
        except psutil.NoSuchProcess:
            pass
    _gone, alive = psutil.wait_procs(children, timeout=grace_seconds)
    for child in alive:
        try:
            child.kill()
        except psutil.NoSuchProcess:
            pass


def _reap_orphan_slice_resources(
    *,
    server: BareMetalServer,
    private_key_path: Path,
    database_url: str,
    env_name: str | None,
    is_dry_run: bool = False,
) -> OrphanReapReport:
    """Delete THIS env's slice VMs AND data disks on the box that have no pool_hosts row and are safe to reap.

    Reconciles the box's slice instances and disks against the DB, scoped to slices
    stamped for ``env_name``: a resource with no row (any status -- a live bake's
    ``baking`` row counts as tracked) is an orphan candidate: a ``mngr create`` killed
    after carving but before any row existed, a hand-carved slice, or a disk left
    behind when a rollback could not unlock it (so the VM is gone but its disk leaked,
    permanently holding the box slot). Other envs' slices and legacy un-stamped slices
    are never touched, so envs can safely share a box.

    Two guards keep a live carve safe even without its row: an instance whose VM is
    running, or whose on-box state is younger than ``ORPHAN_SLICE_MIN_AGE_SECONDS``,
    is spared (and reported), and a disk whose instance is still on the box after the
    instance reap (tracked, running, or spared) is never deleted -- unlinking it under
    a running VM would leave qemu on a deleted inode and lose its data at the next
    restart, and a spared VM would be destroyed by losing its disk just the same.
    Best-effort: logs and continues on any error so it never fails the bake.

    A bake with no owning env (``env_name`` is None) produces only legacy un-stamped
    names, which must be left untouched, so reaping is skipped entirely.
    """
    empty = OrphanReapReport(
        server_id=str(server.id),
        is_dry_run=is_dry_run,
        reaped_instances=(),
        reaped_disks=(),
        spared_instances=(),
        failed=(),
    )
    if env_name is None:
        logger.info("Orphan reap skipped on {}: no owning env to scope to", server.public_address)
        return empty
    ssh_user = box_service_user(server)
    reap_dial = resolve_server_management_dial(server)
    client = build_slice_vm_client(
        box_generation=server.box_generation,
        box_address=reap_dial.host,
        box_ssh_port=reap_dial.port,
        box_ssh_user=ssh_user,
        private_key_path=str(private_key_path),
        box_host_public_key=server.box_host_public_key,
    )
    try:
        observations = client.list_instance_observations()
    except (MngrError, OSError) as exc:
        logger.warning("Orphan reap skipped: could not list slice VMs on {}: {}", server.public_address, exc)
        return empty
    conn = psycopg2.connect(database_url)
    try:
        tracked_instance_names = fetch_slice_instance_names_for_server(conn, server.id)
        tracked_disk_names = fetch_slice_disk_names_for_server(conn, server.id)
    finally:
        conn.close()
    instance_orphans = compute_orphan_slice_instance_names(observations, tracked_instance_names, env_name)
    rowless_names = {
        observation.instance_name
        for observation in observations
        if is_slice_owned_by_env(observation.instance_name, env_name)
        and observation.instance_name not in tracked_instance_names
    }
    spared_instances = tuple(sorted(rowless_names - instance_orphans))
    for name in spared_instances:
        logger.warning(
            "Orphan reap: leaving rowless slice VM {} on {} alone (running or younger than a bake)",
            name,
            server.public_address,
        )
    verb = "would delete" if is_dry_run else "deleting"
    failed: list[str] = []
    if not instance_orphans:
        logger.info("Orphan reap: no reapable untracked slice VMs on {}", server.public_address)
    else:
        logger.info(
            "Orphan reap: {} {} untracked slice VM(s) on {}: {}",
            verb,
            len(instance_orphans),
            server.public_address,
            sorted(instance_orphans),
        )
        for instance_name in sorted(instance_orphans):
            if is_dry_run:
                continue
            try:
                client.destroy_instance(VpsInstanceId(instance_name))
            except (MngrError, OSError) as exc:
                failed.append(instance_name)
                logger.warning(
                    "Orphan reap: failed to delete VM {} on {}: {}", instance_name, server.public_address, exc
                )

    # Reap orphan data disks (a disk can outlive its instance when the rollback delete
    # could not unlock it). Done after the VM reap so a just-deleted VM's disk -- which
    # destroy_instance already removes -- is no longer present to look orphaned. A
    # disk whose VM the reap kept (tracked, running, or spared) is never touched.
    held_instance_names = {observation.instance_name for observation in observations} - instance_orphans
    try:
        box_disk_names = client.list_disk_names()
    except (MngrError, OSError) as exc:
        logger.warning("Orphan disk reap skipped: could not list slice disks on {}: {}", server.public_address, exc)
        return OrphanReapReport(
            server_id=str(server.id),
            is_dry_run=is_dry_run,
            reaped_instances=tuple(sorted(instance_orphans)),
            reaped_disks=(),
            spared_instances=spared_instances,
            failed=tuple(failed),
        )
    disk_orphans = compute_orphan_slice_disk_names(box_disk_names, tracked_disk_names, env_name, held_instance_names)
    if not disk_orphans:
        logger.info("Orphan reap: no reapable untracked slice disks on {}", server.public_address)
    else:
        logger.info(
            "Orphan reap: {} {} untracked slice disk(s) on {}: {}",
            verb,
            len(disk_orphans),
            server.public_address,
            sorted(disk_orphans),
        )
        for disk_name in sorted(disk_orphans):
            if is_dry_run:
                continue
            try:
                client.destroy_disk(disk_name)
            except (MngrError, OSError) as exc:
                failed.append(disk_name)
                logger.warning(
                    "Orphan reap: failed to delete disk {} on {}: {}", disk_name, server.public_address, exc
                )
    return OrphanReapReport(
        server_id=str(server.id),
        is_dry_run=is_dry_run,
        reaped_instances=tuple(sorted(instance_orphans)),
        reaped_disks=tuple(sorted(disk_orphans)),
        spared_instances=spared_instances,
        failed=tuple(failed),
    )


def reap_orphan_slices(
    *,
    server_id: str,
    database_url: str,
    identities: ManagementIdentityResolver,
    env_name: str | None,
    is_dry_run: bool,
) -> OrphanReapReport:
    """Operator entry point: run the bake's orphan reap against one box without baking anything."""
    if env_name is None:
        raise click.UsageError("no minds env is activated; the orphan reap is scoped to the activated env's slices")
    conn = psycopg2.connect(database_url)
    try:
        server = fetch_server_by_id(conn, BareMetalServerDbId(server_id))
    finally:
        conn.close()
    if server is None:
        raise click.UsageError(f"no bare_metal_servers row {server_id}")
    private_key_path = identities.private_key_path_for(server.box_generation)
    return _reap_orphan_slice_resources(
        server=server,
        private_key_path=private_key_path,
        database_url=database_url,
        env_name=env_name,
        is_dry_run=is_dry_run,
    )


# Max pool hosts destroyed at once by default. Destroys are light (a few seconds of
# limactl over SSH each, no box lock involved), so the bound mainly protects the
# boxes' sshd connection limits and basic IO -- higher than the bake's default 4.
DEFAULT_SLICE_DESTROY_CONCURRENCY: Final[int] = 8


@pure
def _already_gone_outcome(pool_host_id: str) -> PoolHostDestroyOutcome:
    return PoolHostDestroyOutcome(
        pool_host_id=pool_host_id,
        status=PoolHostDestroyOutcomeStatus.ALREADY_GONE,
        detail="row no longer exists (already destroyed)",
    )


def _destroy_one_pool_host(
    *,
    pool_host_id: str,
    database_url: str,
    # None only when ``is_row_drop_only`` (no box SSH happens).
    identities: ManagementIdentityResolver | None,
    eligible_statuses: tuple[str, ...],
    is_row_drop_only: bool,
) -> PoolHostDestroyOutcome:
    """Claim, tear down, and delete one pool host row; returns its outcome (never raises).

    Any expected error class (DB, SSH, mngr) becomes a per-host 'failed' outcome, so one
    bad id or a transient Neon hiccup never aborts the sibling destroys or suppresses the
    batch report.
    """
    try:
        return _run_pool_host_destroy_steps(
            pool_host_id=pool_host_id,
            database_url=database_url,
            identities=identities,
            eligible_statuses=eligible_statuses,
            is_row_drop_only=is_row_drop_only,
        )
    except (MngrError, psycopg2.Error, OSError) as exc:
        logger.warning("Destroy of pool host {} failed: {}", pool_host_id, exc)
        return PoolHostDestroyOutcome(
            pool_host_id=pool_host_id,
            status=PoolHostDestroyOutcomeStatus.FAILED,
            detail=f"destroy failed: {exc}",
        )


def _run_pool_host_destroy_steps(
    *,
    pool_host_id: str,
    database_url: str,
    identities: ManagementIdentityResolver | None,
    eligible_statuses: tuple[str, ...],
    is_row_drop_only: bool,
) -> PoolHostDestroyOutcome:
    """Run one host's claim -> VM teardown -> row delete and return its outcome.

    The atomic claim (flip to 'removing' from an eligible status, committed before any
    teardown) is what closes the destroy-vs-lease race: the connector's lease only
    selects 'available' rows, so once claimed the row can never be handed to a user.
    A teardown failure leaves the row 'removing' -- unleasable and retryable by
    re-running the destroy with the same id.
    """
    # Claim the row; a miss means it no longer exists (already destroyed) or its
    # status is not eligible (e.g. it was leased between listing and destroying).
    conn = psycopg2.connect(database_url)
    try:
        is_claimed = claim_pool_host_for_removal(conn, pool_host_id, eligible_statuses)
        if not is_claimed:
            current_status = fetch_pool_host_status(conn, pool_host_id)
            if current_status is None:
                return _already_gone_outcome(pool_host_id)
            if current_status == POOL_HOST_STATUS_LEASED:
                logger.warning(
                    "Skipping pool host {}: it is leased (likely grabbed between listing and destroying)",
                    pool_host_id,
                )
                return PoolHostDestroyOutcome(
                    pool_host_id=pool_host_id,
                    status=PoolHostDestroyOutcomeStatus.SKIPPED_LEASED,
                    detail="row is 'leased'; pass --force to destroy leased rows",
                )
            if current_status == POOL_HOST_STATUS_BAKING:
                logger.warning("Cannot claim pool host {}: its bake may still be running", pool_host_id)
                return PoolHostDestroyOutcome(
                    pool_host_id=pool_host_id,
                    status=PoolHostDestroyOutcomeStatus.FAILED,
                    detail=(
                        f"row is 'baking' and younger than {int(ORPHAN_SLICE_MIN_AGE_SECONDS)}s, so its bake may "
                        "still be running; retry once it is older (a killed bake leaves its row behind)"
                    ),
                )
            # A miss on an existing, non-leased row means a status outside the known
            # vocabulary -- report it precisely rather than guessing at a cause.
            logger.warning(
                "Cannot claim pool host {}: status '{}' is not in {}", pool_host_id, current_status, eligible_statuses
            )
            return PoolHostDestroyOutcome(
                pool_host_id=pool_host_id,
                status=PoolHostDestroyOutcomeStatus.FAILED,
                detail=f"row is in unexpected status '{current_status}' (claimable: {', '.join(eligible_statuses)})",
            )
        target = fetch_pool_host_destroy_target(conn, pool_host_id)
    finally:
        conn.close()
    if target is None:
        # Deleted between the claim and the fetch -- only a concurrent destroy of the
        # same id can do that, and the end state (row gone) is what we wanted.
        return _already_gone_outcome(pool_host_id)

    # Tear the slice VM down before dropping the row, so a failure keeps the row
    # ('removing') and the teardown stays retryable -- never a stranded VM.
    if not is_row_drop_only:
        if not target.slice_instance_name or not target.box_public_address:
            return PoolHostDestroyOutcome(
                pool_host_id=pool_host_id,
                status=PoolHostDestroyOutcomeStatus.FAILED,
                detail=(
                    "cannot locate the VM to destroy (missing slice_instance_name or the box record is gone); "
                    "pass --drop-row-only to drop the row without teardown"
                ),
            )
        if identities is None:
            return PoolHostDestroyOutcome(
                pool_host_id=pool_host_id,
                status=PoolHostDestroyOutcomeStatus.FAILED,
                detail="no management SSH identity available for the box SSH",
            )
        private_key_path = identities.private_key_path_for(target.box_generation)
        destroy_dial = resolve_box_management_dial(
            public_address=target.box_public_address,
            wireguard_address=target.box_wireguard_address,
            wireguard_public_key=target.box_wireguard_public_key,
        )
        client = build_slice_vm_client(
            box_generation=target.box_generation,
            box_address=destroy_dial.host,
            box_ssh_port=destroy_dial.port,
            box_ssh_user=target.slice_service_user or default_slice_service_user(target.box_generation),
            private_key_path=str(private_key_path),
            box_host_public_key=target.box_host_public_key,
        )
        try:
            client.destroy_instance(VpsInstanceId(target.slice_instance_name))
        except (MngrError, OSError) as exc:
            logger.warning("Failed to tear down slice {}: {}", target.slice_instance_name, exc)
            return PoolHostDestroyOutcome(
                pool_host_id=pool_host_id,
                status=PoolHostDestroyOutcomeStatus.FAILED,
                detail=f"VM teardown failed ({target.slice_instance_name} on {target.box_public_address}): {exc}",
            )

    # The VM is gone (or the operator asked for a row-only drop); drop the row. A fresh
    # connection on purpose: the SSH teardown above can take minutes, and a Neon
    # connection held idle across it may be dropped server-side by the time we delete.
    conn_for_delete = psycopg2.connect(database_url)
    try:
        delete_pool_host_row(conn_for_delete, pool_host_id)
    finally:
        conn_for_delete.close()
    logger.info("Destroyed pool host {} ({})", pool_host_id, target.slice_instance_name or "no VM")
    detail = "row dropped without VM teardown (--drop-row-only)" if is_row_drop_only else None
    return PoolHostDestroyOutcome(
        pool_host_id=pool_host_id, status=PoolHostDestroyOutcomeStatus.DESTROYED, detail=detail
    )


def _describe_destroy_outcome(outcome: PoolHostDestroyOutcome) -> str:
    return f"{outcome.pool_host_id} {outcome.status}"


def destroy_pool_hosts_in_parallel(
    *,
    pool_host_ids: Sequence[str],
    database_url: str,
    # None only when ``is_row_drop_only`` (no box SSH happens).
    identities: ManagementIdentityResolver | None,
    eligible_statuses: tuple[str, ...],
    is_row_drop_only: bool,
    max_concurrency: int,
) -> list[PoolHostDestroyOutcome]:
    """Destroy pool hosts concurrently (claim -> VM teardown -> row delete), one outcome per id.

    All targets run in parallel under one global semaphore regardless of which box each
    slice is on -- deletes never take the box's carve-time reservation lock, so
    parallelism within a single box is safe. Outcomes are returned in input order.
    """
    if max_concurrency <= 0:
        raise click.UsageError("--max-concurrency must be positive")
    unique_ids = list(dict.fromkeys(pool_host_ids))
    logger.info("Destroying {} pool host(s) ({} at a time)", len(unique_ids), max_concurrency)
    # A row-only drop never SSHes a box, so it must not require a management key.
    box_identities = None if is_row_drop_only else identities
    outcomes = run_outcome_workers_in_bounded_threads(
        worker=_destroy_one_pool_host,
        worker_kwargs_list=[
            dict(
                pool_host_id=pool_host_id,
                database_url=database_url,
                identities=box_identities,
                eligible_statuses=eligible_statuses,
                is_row_drop_only=is_row_drop_only,
            )
            for pool_host_id in unique_ids
        ],
        max_concurrency=max_concurrency,
        thread_name_prefix="destroy",
        progress_noun="Pool host destroy",
        describe_outcome=_describe_destroy_outcome,
        interruption_exception_types=(),
        on_join_interrupted=None,
    )
    outcome_by_id = {outcome.pool_host_id: outcome for outcome in outcomes}
    return [outcome_by_id[pool_host_id] for pool_host_id in unique_ids]


@pure
def build_pool_host_destroy_report(outcomes: Sequence[PoolHostDestroyOutcome]) -> PoolHostDestroyReport:
    """Aggregate per-host destroy outcomes into the summary report the destroy commands emit."""
    destroyed_count = sum(
        1
        for outcome in outcomes
        if outcome.status in (PoolHostDestroyOutcomeStatus.DESTROYED, PoolHostDestroyOutcomeStatus.ALREADY_GONE)
    )
    skipped_count = sum(1 for outcome in outcomes if outcome.status == PoolHostDestroyOutcomeStatus.SKIPPED_LEASED)
    failed_count = sum(1 for outcome in outcomes if outcome.status == PoolHostDestroyOutcomeStatus.FAILED)
    return PoolHostDestroyReport(
        requested=len(outcomes),
        destroyed=destroyed_count,
        skipped=skipped_count,
        failed=failed_count,
        hosts=tuple(outcomes),
    )


def tear_down_unleased_slices(
    database_url: str, *, identities: ManagementIdentityResolver, max_concurrency: int
) -> PoolHostDestroyReport:
    """Tear down every unleased slice VM recorded in ``database_url`` and drop its row.

    The teardown an env destroy runs (before its per-env DB is deleted) so the env's
    baked-but-unleased pool slices don't leak their VMs on the shared boxes. Leased
    slices are excluded: they are torn down via their agent's release path. Rows
    stranded in 'removing' (a crashed release) are included so they never leak. Each
    row is atomically claimed before its VM is touched, so a lease cannot race the
    teardown; each VM teardown is idempotent (an already-absent VM counts as success)
    and the row is dropped only after its VM is gone. Must-succeed: raises
    ``BareMetalProvisioningError`` listing every slice whose box could not be
    reached, so the caller can stop the destroy rather than silently leak.
    The caller supplies the management identities the boxes are dialed with.
    """
    eligible_statuses = destroy_eligible_pool_host_statuses(is_leased_destroy_allowed=False)
    conn = psycopg2.connect(database_url)
    try:
        row_ids = fetch_unleased_slice_teardown_row_ids(conn, eligible_statuses)
    finally:
        conn.close()
    if not row_ids:
        return build_pool_host_destroy_report([])
    outcomes = destroy_pool_hosts_in_parallel(
        pool_host_ids=row_ids,
        database_url=database_url,
        identities=identities,
        eligible_statuses=eligible_statuses,
        is_row_drop_only=False,
        max_concurrency=max_concurrency,
    )
    report = build_pool_host_destroy_report(outcomes)
    failures = [
        f"{outcome.pool_host_id}: {outcome.detail or 'unknown failure'}"
        for outcome in outcomes
        if outcome.status == PoolHostDestroyOutcomeStatus.FAILED
    ]
    if failures:
        raise BareMetalProvisioningError(
            f"failed to tear down {len(failures)} slice(s); their VMs may still be running: {'; '.join(failures)}"
        )
    return report


def _resolve_vendored_mngr_source(*, mngr_source: str | None, repo_root: Path, is_from_tag: bool) -> Path | None:
    """Return the mngr tree to vendor into the DEFAULT_WORKSPACE_TEMPLATE clone's ``system/vendor/mngr``, or None to keep the clone's own.

    An explicit ``--mngr-source`` always wins. Otherwise a ``--from-tag`` bake keeps
    the mngr already vendored at the pinned tag (returns None -- byte-for-byte tag
    content), while a ``--workspace-dir`` (dev) bake vendors the local checkout
    (``repo_root``). Without this, ``--from-tag`` would silently bake the operator's
    local mngr over the tag's, defeating the point of pinning a release tag.
    """
    if mngr_source is not None:
        return Path(mngr_source)
    if is_from_tag:
        return None
    return repo_root


def assert_box_is_exclusive_to_tier(
    *,
    server: BareMetalServer,
    env_name: str | None,
    box_disk_names: AbstractSet[str],
    trust: BoxManagementTrust,
    # The tier's committed SSH CA (None when it has none committed yet).
    expected_ca_public_key: str | None,
) -> None:
    """Refuse to bake unless this box belongs solely to the activated env's tier.

    Tier isolation is a stated invariant (``apps/minds/docs/deploy/reference/environments.md``:
    "There is zero cross-tier reach"), but nothing used to enforce it at the moment
    it matters. Three independent ways a box drifts across tiers, all caught here
    before a single slice is carved:

    * a **foreign-tier slice** already on the box -- each tier's operators and
      connector then hold management access over the other's workspaces (and
      neither tier's env-scoped reap reclaims the other's leaks);
    * a **static key** in the slice service user's ``authorized_keys`` beyond what
      the box's generation expects (exactly the tier's pool key on gen-1, none at
      all on gen-2, where management SSH is by certificate) -- prep writes that
      file (or removes it), so an extra key was added out of band and hands its
      holder SSH access to this box (and thus every workspace running on it);
    * on gen-2, a **trusted CA other than the tier's**: whoever holds that CA can
      mint certificates the box accepts.

    ``env_name`` is None only for a legacy un-stamped bake, whose tier is
    unknowable; the slice check is skipped in that case, but the trust checks --
    which do not depend on our tier -- still apply.
    """
    expected_key_count = expected_static_authorized_key_count(server.box_generation)
    if trust.authorized_key_count != expected_key_count:
        # Both directions refuse the bake, but they mean opposite things and have
        # opposite remedies. Re-prepping is safe when the file is short of what prep
        # writes and destructive when it holds someone else's key: prep converges
        # authorized_keys (a single-key overwrite on gen-1, removal on gen-2), so it
        # would revoke that holder's access to a box whose slices -- which this branch
        # raises before ever looking at -- are still running.
        if trust.authorized_key_count < expected_key_count:
            reason = (
                "that file is empty, so the box was never prepped (or its authorized_keys was clobbered). "
                f"Run `just prep-server {server.id}` (idempotent) to write this tier's key."
            )
        else:
            reason = (
                "`minds-admin server prep` converges that file, so the extra key(s) were added out of band and give "
                "another party SSH access to this box. Inspect them with `ssh-keygen -lf ~/.ssh/authorized_keys` on "
                "the box. Do NOT re-prep before checking the box for another tier's slices (`just server-audit`): "
                "prep rewrites authorized_keys, which would cut the other key's owner off from slices that are "
                "still running here."
            )
        expectation = "this tier's pool key" if expected_key_count else "none: gen-2 management SSH is by certificate"
        raise click.UsageError(
            f"server {server.id} ({server.public_address}) authorizes {trust.authorized_key_count} static SSH "
            f"key(s) for user {box_service_user(server)}, expected exactly {expected_key_count} ({expectation}). "
            f"{reason}"
        )
    if not is_trusted_ca_correct_for_tier(trust, server.box_generation, expected_ca_public_key):
        if expected_ca_public_key is None:
            raise click.UsageError(
                f"server {server.id} ({server.public_address}) is a gen-2 box, but the activated tier has no SSH "
                "CA public key committed in its deploy.toml [ssh_ca] block, so its trust cannot be verified. "
                "Bring the tier's Vault SSH CA up first (apps/minds/docs/deploy/setup/tier-bringup.md)."
            )
        trusted = "no SSH CA" if trust.trusted_ca_public_key is None else "an SSH CA that is not this tier's"
        raise click.UsageError(
            f"server {server.id} ({server.public_address}) trusts {trusted} for management SSH. A gen-2 box must "
            "trust exactly the tier CA committed in deploy.toml: a missing trust means the box was never prepped "
            f"for certificates (run `just prep-server {server.id}`); a foreign CA means another party can mint "
            "certificates this box accepts -- audit it with `just server-audit` before re-prepping."
        )
    if env_name is None:
        return
    foreign_names = foreign_tier_slice_names(box_disk_names, env_name)
    if is_box_exclusive_to_tier(
        authorized_key_count=trust.authorized_key_count,
        expected_authorized_key_count=expected_key_count,
        foreign_tier_slice_count=len(foreign_names),
        is_trusted_ca_correct=True,
    ):
        return
    foreign_list = ", ".join(sorted(foreign_names))
    raise click.UsageError(
        f"server {server.id} ({server.public_address}) already carries slices from another tier, so it "
        f"cannot also host '{env_name}' (tier '{tier_for_env_name(env_name)}') slices: {foreign_list}. "
        "Tiers are isolated by construction -- each has its own SSH CA and pool keypair, and there is meant to be "
        "zero cross-tier reach -- so a box serving both is a box each tier's operators can SSH (and control) the "
        "other's workspaces on, and neither tier's reap will ever reclaim the other's slices. "
        "Retire the foreign slices from their OWN env (`just pool-destroy <row-id>` with that env "
        "activated) or bake onto a box belonging to this tier."
    )


def assert_bake_box_storage_is_encrypted(server: BareMetalServer, client: SliceVmClientInterface) -> None:
    """Probe a gen-2 box's storage volume before a bake and refuse one that is not the mounted LUKS volume.

    Gen-1 boxes have no storage volume, so the probe (a management-SSH round
    trip) is skipped for them rather than made and ignored.
    """
    if server.box_generation < FIRST_QEMU_BOX_GENERATION:
        return
    assert_gen2_box_storage_is_encrypted(server, client.read_storage_volume_state())


@pure
def assert_gen2_box_storage_is_encrypted(server: BareMetalServer, storage_state: StorageVolumeState) -> None:
    """Refuse to carve on a gen-2 box whose storage root is not the mounted LUKS volume.

    A slice carved onto a plain partition would sit in plaintext for its whole
    life (the prep refuses to encrypt a partition that holds slices), and one
    carved while the volume is locked would land on the bare mountpoint of the
    root partition. A mounted mapper the probe could not confirm as a crypt
    device is refused too, but without the repave instruction: the volume is
    there, only the probe's type read is in doubt.
    """
    if storage_state.is_encrypted:
        return
    if storage_state.mounted_source is None:
        raise click.UsageError(
            f"server {server.id} ({server.public_address}) has nothing mounted at its storage root: its LUKS "
            "volume is locked (the TPM unlock failed at boot). Run `minds-admin server unlock --server-id "
            f"{server.id}` before baking on it."
        )
    if storage_state.mounted_source == GEN2_STORAGE_LUKS_MAPPER_PATH:
        raise click.UsageError(
            f"server {server.id} ({server.public_address}) has its storage root mounted from "
            f"{GEN2_STORAGE_LUKS_MAPPER_PATH}, but the probe could not confirm that mapper as a crypt device. Check "
            f"`cryptsetup status {GEN2_STORAGE_LUKS_MAPPER_NAME}` and `lsblk` on the box before baking on it."
        )
    raise click.UsageError(
        f"server {server.id} ({server.public_address}) has its storage root mounted from the unencrypted "
        f"{storage_state.mounted_source}; every gen-2 box's slices must live on the LUKS storage volume. Drain the "
        "box and repave it (`minds-admin server drain`, then `minds-admin cutover repave` or `server setup`)."
    )


def _is_seed_phase_needed(cache: BoxImageCacheInterface, cache_tag: str | None) -> bool:
    """Whether the bake must run its own seed phase (one slice baked alone) before the fan-out.

    No seed phase is needed when there is no cache tag (a plain dev bake: every slice
    builds from the Dockerfile), when the box already holds the tag's tar (warm), or
    when another seeder currently holds the build lock -- e.g. the CI cache pre-warm
    job running in parallel with this bake (specs/remote-workspaces-in-ci.md). In the
    lock-held case each fan-out slice's create blocks on that in-flight seed's tar and
    then docker-loads it (taking over the build if the seeder dies), so a local seed
    phase would only serialize one slice behind the very same wait.
    """
    if cache_tag is None:
        return False
    if cache.has_tar(cache_tag):
        return False
    if cache.is_build_locked(cache_tag):
        logger.info(
            "Box already has an in-flight seed build for {} (build lock held); skipping the local seed phase",
            cache_tag,
        )
        return False
    return True


def allocate_slices(
    *,
    count: int,
    server_id: str,
    lease_attributes: dict[str, Any],
    region: str,
    env_name: str | None,
    workspace_dir: Path,
    mngr_source: str | None,
    is_from_tag: bool,
    is_content_addressed_cache: bool,
    database_url: str,
    is_dry_run: bool,
    is_env_converge_wait_skipped: bool,
    max_concurrency: int,
    # The management keys this bake dials the box and its new VM with
    # (the operator's certificate on gen-2, the pool key on gen-1).
    identities: ManagementIdentityResolver,
    # Gen-2 machine size in units (1 unit = 1GiB guest RAM; specs/slice-fleet).
    # None bakes the fleet default; only dev/testing bakes override it.
    machine_units: int | None,
    # The workspace container's Docker runtime on a gen-2 box. None bakes the
    # fleet runtime (runsc); RUNC exists for the side-by-side comparison bake.
    container_runtime_override: SliceContainerRuntime | None,
    # The cutover's image-tar seed bakes an old tag on a gen-2 box on purpose
    # (the row never leases; it is destroyed once the tar exists), so it alone
    # bypasses the release/generation pairing guard.
    is_image_seed_bake: bool,
) -> None:
    """Bake ``count`` slices onto the explicitly chosen bare-metal server and insert their pool rows.

    The slice backend of ``minds-admin pool create``. Bakes onto the operator-named
    ``server_id`` (one server per invocation: a server's per-slice vCPU/RAM/disk
    are fixed by its registration, so a batch is homogeneous), vendors the resolved
    mngr source into the DEFAULT_WORKSPACE_TEMPLATE workspace once (see ``_resolve_vendored_mngr_source``:
    a ``--from-tag`` bake keeps the tag's own vendored mngr), then bakes the slices concurrently -- at most
    ``max_concurrency`` at a time (the rest queue) so the box isn't over-contended,
    which would push each ``mngr create`` past its timeout. Each ``mngr create``
    drives the slice provider to carve a lima VM over SSH on the box and bake the
    shared container, exactly like an OVH pool bake. Each row advertises
    ``lease_attributes`` (the operator's lease metadata) with the derived per-box
    size stamped on top, and records ``region`` (the lease-region label, not the
    box's raw datacenter code) so the connector's region-filtered lease matches.

    A cache-tag (``--from-tag``) bake onto a box with no tar for the tag yet runs a
    seed phase first: one slice baked alone builds + publishes the box image tar, so
    the fan-out only ever takes the warm docker-load path. Every requested slice
    (seeder included) is baked with bounded retries -- a failed bake destroys its VM
    and writes no row, so a retry is a clean fresh slice -- and a seed that fails all
    its attempts aborts the whole bake up front with one clear error.

    ``env_name`` (the activated minds env) is stamped into every slice's lima names
    so envs can share a box: free-slot capacity is read from the box's REAL
    occupancy (all envs + legacy), each carve reserves its slot + ports under a box
    lock, and the post-bake reap only ever touches this env's own stamped slices.

    After the bakes finish, reconciles this env's slice VMs against the DB and reaps
    any orphan (a VM with no pool_hosts row -- e.g. a create killed by its own
    timeout after carving but before the insert). ``database_url`` is already
    resolved by the caller. ``is_dry_run`` only reports placement.
    """
    if count <= 0:
        raise click.UsageError("--count must be positive")
    if max_concurrency <= 0:
        raise click.UsageError("--max-concurrency must be positive")
    # Fail fast on an env name too long for the slice lima identifiers: limactl
    # only rejects it at reserve time, deep inside the bake, with an unhelpful
    # message (CI env names sit near the cap).
    if env_name is not None:
        assert_env_name_fits_slice_names(env_name)
    conn = psycopg2.connect(database_url)
    try:
        capacities = fetch_server_capacities(conn)
    finally:
        conn.close()
    # One explicitly-chosen server per batch (homogeneous sizing): the operator names the box via
    # ``--server-id``; we never auto-select. Require it to be ready.
    chosen = find_server_capacity_by_id(capacities, BareMetalServerDbId(server_id))
    server = chosen.server
    if str(server.status) != SERVER_STATUS_READY:
        raise click.UsageError(
            f"server {server.id} is '{server.status}', not '{SERVER_STATUS_READY}'; "
            "finish `minds-admin server await-delivery` + `setup` before baking slices on it"
        )
    if not server.public_address:
        raise click.UsageError(f"server {server.id} has no public_address; cannot bake")
    if not is_image_seed_bake:
        tag_guard_error = bake_tag_generation_error_or_none(
            server.box_generation, lease_attributes.get("repo_branch_or_tag")
        )
        if tag_guard_error is not None:
            raise click.UsageError(tag_guard_error)
    sizing = compute_server_slice_sizing(server, machine_units)
    container_runtime = resolve_slice_container_runtime(server, container_runtime_override)

    ssh_user = box_service_user(server)
    management_trust, private_key_path = resolve_bake_management_trust_and_key(server, identities)
    # Free slots come from the box's REAL occupancy (every env's slices plus any
    # legacy un-stamped ones), NOT this env's DB row count -- so independent envs
    # sharing the box cannot collectively over-subscribe it. This is a fast
    # pre-check; the authoritative guard is the per-slice on-box reservation lock.
    occupancy_dial = resolve_server_management_dial(server)
    occupancy_client = build_slice_vm_client(
        box_generation=server.box_generation,
        box_address=occupancy_dial.host,
        box_ssh_port=occupancy_dial.port,
        box_ssh_user=ssh_user,
        private_key_path=str(private_key_path),
        box_host_public_key=server.box_host_public_key,
    )
    box_disk_names = occupancy_client.list_disk_names()
    # Enforce tier isolation before anything is carved: a box shared across tiers
    # is one both tiers' management credentials can SSH, so each tier's operators
    # and connector get root over the other's workspaces (the guard's docstring
    # lists the three drift shapes it refuses).
    assert_box_is_exclusive_to_tier(
        server=server,
        env_name=env_name,
        box_disk_names=box_disk_names,
        trust=occupancy_client.read_management_trust(),
        expected_ca_public_key=management_trust.trusted_user_ca_public_key,
    )
    assert_bake_box_storage_is_encrypted(server, occupancy_client)
    box_used_slots = count_slice_resource_names(box_disk_names)
    if "units" in sizing:
        # Gen-2 two-budget estimate (specs/slice-fleet): how many machines
        # of THIS bake's size the box could hold, with the existing
        # occupancy approximated at the same size (their real sizes live
        # in the on-box env files, which the authoritative reserve reads).
        estimated_capacity = estimate_gen2_machine_capacity(sizing)
        free_slots = max(0, estimated_capacity - box_used_slots)
        if free_slots < count:
            raise click.UsageError(
                f"server {server.id} fits an estimated {estimated_capacity} machine(s) of "
                f"{sizing['units']} unit(s) and {box_used_slots} are in use on the box across all envs; "
                f"cannot bake {count}"
            )
    else:
        free_slots = max(0, server.slot_count - box_used_slots)
        if free_slots < count:
            raise click.UsageError(
                f"server {server.id} has only {free_slots} of {server.slot_count} slot(s) free "
                f"({box_used_slots} in use on the box across all envs); cannot bake {count}"
            )

    if is_dry_run:
        emit_json(
            {
                "dry_run": True,
                "server_id": str(server.id),
                "public_address": server.public_address,
                "region": region,
                "env_name": env_name,
                "count": count,
                "free_slots": free_slots,
                "box_used_slots": box_used_slots,
                "per_slice_sizing": sizing,
                "container_runtime": docker_runtime_name(container_runtime) if container_runtime else None,
                "attributes": {**lease_attributes, **slice_advertised_attributes(sizing)},
            }
        )
        return

    # Every inner ``mngr create`` below runs in a throwaway mngr namespace so
    # bake-time hosts/agents/discovery-events never land in the operator's own
    # mngr data root (where e.g. the minds desktop app would render each bake as
    # a phantom workspace). Deleted on success; retained (path logged) on any
    # failure, including a partial one, and swept after the retention window.
    sweep_stale_bake_namespaces()
    with ephemeral_bake_namespace() as bake_namespace:
        # Resolve which mngr tree (if any) to vendor into the DEFAULT_WORKSPACE_TEMPLATE workspace's
        # system/vendor/mngr (the baked container builds its mngr from there). For a
        # --from-tag bake we keep the mngr already vendored at the pinned tag so the
        # slice is byte-for-byte tag content; only --workspace-dir (dev) or an
        # explicit --mngr-source overrides it. See _resolve_vendored_mngr_source.
        repo_root = Path(__file__).resolve().parents[5]
        mngr_source_to_vendor = _resolve_vendored_mngr_source(
            mngr_source=mngr_source, repo_root=repo_root, is_from_tag=is_from_tag
        )
        if mngr_source_to_vendor is not None:
            sync_mngr_into_template(mngr_source_to_vendor, workspace_dir)

        # Enable the per-box default-workspace-template image cache only when its key is
        # immutable: production (--from-tag) bakes key on the tag, and CI bakes opt in to a
        # content-addressed key (a hash of the workspace tree AFTER the vendor sync above,
        # so it covers the vendored mngr too). Plain dev (--workspace-dir) bakes have
        # mutable content under a branch label, so they always build
        # (default_workspace_template_cache_tag=None).
        repo_branch_or_tag = lease_attributes.get("repo_branch_or_tag")
        if is_content_addressed_cache:
            default_workspace_template_cache_tag: str | None = compute_content_addressed_cache_tag(workspace_dir)
            logger.info("Using content-addressed image-cache tag {}", default_workspace_template_cache_tag)
        elif is_from_tag and repo_branch_or_tag:
            default_workspace_template_cache_tag = (
                f"{DEFAULT_WORKSPACE_TEMPLATE_IMAGE_REPOSITORY}:{repo_branch_or_tag}"
            )
        else:
            default_workspace_template_cache_tag = None
        # Seed-first: a cache-tag bake onto a box that does not hold this tag's tar
        # yet runs a seed phase -- one slice baked alone -- before the fan-out. The
        # seeder builds + publishes the box tar (its bounded retries absorb transient
        # build failures), every later slice takes the warm docker-load path, and a
        # build that keeps failing aborts the whole bake up front with one clear
        # error instead of consuming one requested slice per failed build. When
        # another seeder already holds the build lock (e.g. the CI cache pre-warm
        # job), the seed phase is skipped too -- see _is_seed_phase_needed.
        is_seed_phase_needed = _is_seed_phase_needed(
            SshBoxImageCache(
                slice_client=occupancy_client,
                cache_dir=box_image_cache_dir_for_generation(server.box_generation, ssh_user),
            ),
            default_workspace_template_cache_tag,
        )
        # One worker per slice, capped at ``max_concurrency`` at once by the shared
        # fan-out: each bake blocks on the semaphore before its ``mngr create``, so
        # the box is never contended by more than K simultaneous carves+builds
        # (which would push each create past its timeout). Every bake is handed the
        # FULL box port range: the on-box reservation lock makes concurrent carves
        # (this env's and other envs') pick distinct free ports from it.
        row_ledger = BakeRowLedger()
        bake_worker_kwargs = dict(
            server=server,
            sizing=sizing,
            lease_attributes=lease_attributes,
            region=region,
            env_name=env_name,
            workspace_dir=workspace_dir,
            management_trust=management_trust,
            private_key_path=private_key_path,
            database_url=database_url,
            port_range_start=DEFAULT_SLICE_PORT_RANGE_START,
            port_range_end=DEFAULT_SLICE_PORT_RANGE_END,
            is_env_converge_wait_skipped=is_env_converge_wait_skipped,
            default_workspace_template_cache_tag=default_workspace_template_cache_tag,
            container_runtime=container_runtime,
            extra_create_env=bake_namespace.to_subprocess_env(),
            row_ledger=row_ledger,
        )
        logger.info("Baking {} slice(s) on {} ({} at a time)", count, server.public_address, max_concurrency)

        # ``signal.signal`` only works on the main thread; the admin CLI always runs
        # allocate_slices there, but guard so an off-main-thread caller falls back to
        # the finally reap rather than crashing on install.
        is_main_thread = threading.current_thread() is threading.main_thread()
        # Set by the join-interruption handler so the per-slice retry loops return
        # their (kill-induced) failures instead of spawning replacement bakes.
        bake_termination_event = threading.Event()
        previous_sigterm = signal.signal(signal.SIGTERM, _raise_on_bake_termination_signal) if is_main_thread else None
        previous_sigint = signal.signal(signal.SIGINT, _raise_on_bake_termination_signal) if is_main_thread else None
        try:
            if is_seed_phase_needed:
                logger.info(
                    "Box {} has no cached image tar for {}; seeding it with one slice before the fan-out",
                    server.public_address,
                    default_workspace_template_cache_tag,
                )
            seed_outcomes = (
                _run_bake_fan_out(
                    bake_worker_kwargs=bake_worker_kwargs,
                    slice_count=1,
                    max_concurrency=1,
                    progress_noun="Seed slice bake",
                    is_main_thread=is_main_thread,
                    termination_event=bake_termination_event,
                )
                if is_seed_phase_needed
                else []
            )
            is_seed_failed = any(outcome.status == SliceBakeOutcomeStatus.FAILED for outcome in seed_outcomes)
            if is_seed_failed:
                logger.error(
                    "Aborting the bake: the seed slice failed all {} attempts (its error is in the report); "
                    "not attempting the remaining {} slice(s)",
                    _SLICE_BAKE_ATTEMPT_COUNT,
                    count - 1,
                )
            fill_count = 0 if is_seed_failed else count - len(seed_outcomes)
            fill_outcomes = (
                _run_bake_fan_out(
                    bake_worker_kwargs=bake_worker_kwargs,
                    slice_count=fill_count,
                    max_concurrency=max_concurrency,
                    progress_noun="Slice bake",
                    is_main_thread=is_main_thread,
                    termination_event=bake_termination_event,
                )
                if fill_count > 0
                else []
            )
            outcomes = seed_outcomes + fill_outcomes
        except SliceBakeTerminatedError:
            # Top-level kill (e.g. the minds wrapper's subprocess timeout SIGTERMs us).
            # The fan-out's on_join_interrupted hook has already ignored further
            # signals, killed the in-flight workers (so no new VM is carved), and let
            # their threads settle; the finally reaps the orphans. Exit non-zero so
            # the caller sees the failure.
            raise SystemExit(1) from None
        finally:
            # Reap VMs left orphaned by a killed/timed-out create (carved but never
            # inserted, so the provider's rollback never ran). Runs after all threads
            # join -- an individual-create timeout (already a 'failed' outcome by now)
            # is cleaned here; the except above handles a top-level kill. Restore the
            # signal handlers last so the reap itself isn't interrupted.
            _delete_leftover_baking_rows(database_url, row_ledger)
            _reap_orphan_slice_resources(
                server=server, private_key_path=private_key_path, database_url=database_url, env_name=env_name
            )
            if is_main_thread:
                signal.signal(signal.SIGTERM, previous_sigterm)
                signal.signal(signal.SIGINT, previous_sigint)

        succeeded = [outcome for outcome in outcomes if outcome.status == SliceBakeOutcomeStatus.SUCCEEDED]
        report = SliceBakeReport(
            requested=count, succeeded=len(succeeded), failed=count - len(succeeded), slices=tuple(outcomes)
        )
        emit_json(report.model_dump(mode="json", exclude_none=True))
        if report.failed:
            raise SystemExit(1)


def _warm_bake_one_slice(
    *,
    server: BareMetalServer,
    sizing: dict[str, int],
    region: str,
    workspace_dir: Path,
    management_trust: SliceManagementTrust,
    private_key_path: Path,
    default_workspace_template_cache_tag: str,
    extra_create_env: Mapping[str, str],
) -> SliceBakeOutcome:
    """Bake one throwaway ci-warm slice purely so its create builds + publishes the box image tar.

    The create's own cache path does the real work (the seed build and the
    ``docker save`` to the box tar happen inside ``mngr create``); no pool row is
    written, the post-create finalize steps are skipped entirely, and the caller
    destroys the slice afterwards.
    """
    ssh_user = box_service_user(server)
    host_name = f"slice-{uuid4().hex}"
    attributes = slice_advertised_attributes(sizing)
    try:
        baked = bake_pool_host(
            provider_instance=SLICE_PROVIDER_INSTANCE_NAME,
            host_name=host_name,
            attributes=attributes,
            workspace_dir=workspace_dir,
            extra_create_args=_build_slice_create_args(
                server=server,
                sizing=sizing,
                region=region,
                env_name=CI_WARM_PSEUDO_ENV_NAME,
                management_trust=management_trust,
                private_key_path=private_key_path,
                ssh_user=ssh_user,
                port_range_start=DEFAULT_SLICE_PORT_RANGE_START,
                port_range_end=DEFAULT_SLICE_PORT_RANGE_END,
                default_workspace_template_cache_tag=default_workspace_template_cache_tag,
                container_runtime=resolve_slice_container_runtime(server, None),
                # A throwaway slice with no pool row: the warm's own reap destroys it.
                slice_host_id=HostId.generate(),
            ),
            extra_create_env=extra_create_env,
            mngr_create_timeout_seconds=_SLICE_MNGR_CREATE_TIMEOUT_SECONDS,
        )
    except (PoolBakeError, BareMetalProvisioningError, MngrError, OSError) as exc:
        logger.warning("Warm seed slice bake {} failed: {}", host_name, exc)
        return SliceBakeOutcome(
            host_name=host_name, server_id=str(server.id), status=SliceBakeOutcomeStatus.FAILED, error=str(exc)
        )
    return SliceBakeOutcome(
        host_name=host_name,
        server_id=str(server.id),
        status=SliceBakeOutcomeStatus.SUCCEEDED,
        host_id=baked.host_id,
        agent_id=baked.agent_id,
        vm_ssh_port=baked.outer_ssh_port,
        container_ssh_port=baked.ssh_port,
        attributes=attributes,
    )


def _reap_ci_warm_slice_resources(client: SliceVmClientInterface) -> None:
    """Destroy every ci-warm-stamped slice VM (then orphan disk) on the box.

    The warm verb's unconditional cleanup. ci-warm slices exist only while a
    (serialized) warm invocation runs, so destroying all of them -- rather than
    tracking the one host id this invocation carved -- also reclaims a slice a
    killed prior warm left behind. Failures are logged, not raised: the age-based
    CI slice sweep is the backstop.
    """
    try:
        instance_names = client.list_instance_names()
    except (MngrError, OSError) as exc:
        logger.warning("Could not list lima instances for the ci-warm reap: {}", exc)
        return
    for instance_name in sorted(instance_names):
        if not is_slice_owned_by_env(instance_name, CI_WARM_PSEUDO_ENV_NAME):
            continue
        logger.info("Destroying ci-warm slice VM {}", instance_name)
        try:
            client.destroy_instance(VpsInstanceId(instance_name))
        except (MngrError, OSError) as exc:
            logger.warning("Failed to destroy ci-warm slice VM {}: {}", instance_name, exc)
    # Disks second, re-listed so disks destroyed with their VM are gone; a disk
    # that outlived its VM would otherwise hold the box slot forever.
    try:
        disk_names = client.list_disk_names()
    except (MngrError, OSError) as exc:
        logger.warning("Could not list lima disks for the ci-warm reap: {}", exc)
        return
    for disk_name in sorted(disk_names):
        if not is_slice_owned_by_env(disk_name, CI_WARM_PSEUDO_ENV_NAME):
            continue
        logger.info("Destroying orphan ci-warm slice disk {}", disk_name)
        try:
            client.destroy_disk(disk_name)
        except (MngrError, OSError) as exc:
            logger.warning("Failed to destroy ci-warm slice disk {}: {}", disk_name, exc)


def warm_box_image_cache(
    *,
    server_id: str,
    workspace_dir: Path,
    mngr_source: str | None,
    database_url: str,
    identities: ManagementIdentityResolver,
) -> None:
    """Pre-warm one box's content-addressed image cache: seed the tar via a throwaway slice, then destroy it.

    The slice backend of ``minds-admin pool warm-cache`` (specs/remote-workspaces-in-ci.md).
    Reads the box row from ``database_url`` but writes nothing: slot/port reservation is
    purely on-box, no ``pool_hosts`` row is created, and the throwaway slice carries the
    reserved ``ci-warm`` pseudo-env label so the CI slice sweep reclaims a leaked one by
    age. If the box already holds the tar for the derived content tag this is a cheap
    no-op. Exits non-zero when the box does not hold the tar afterwards; the caller
    (the CI warm job) treats that as advisory.
    """
    conn = psycopg2.connect(database_url)
    try:
        server = fetch_server_by_id(conn, BareMetalServerDbId(server_id))
    finally:
        conn.close()
    if server is None:
        raise click.UsageError(f"no bare-metal server with id {server_id}; see `minds-admin server list`")
    if str(server.status) != SERVER_STATUS_READY:
        raise click.UsageError(f"server {server.id} is '{server.status}', not '{SERVER_STATUS_READY}'; cannot warm")
    if not server.public_address:
        raise click.UsageError(f"server {server.id} has no public_address; cannot warm")
    sizing = compute_server_slice_sizing(server, None)
    # The create's provider config wants the lease-region label; derive it from the
    # box's datacenter code so the verb needs no --region of its own (the label is
    # irrelevant for a slice that never becomes a pool row).
    region = US_REGION_BY_OVH_DATACENTER_CODE.get(server.region or "")
    if region is None:
        raise click.UsageError(
            f"server {server.id} is in datacenter {server.region!r}, which maps to no known lease region"
        )

    ssh_user = box_service_user(server)
    management_trust, private_key_path = resolve_bake_management_trust_and_key(server, identities)
    warm_dial = resolve_server_management_dial(server)
    client = build_slice_vm_client(
        box_generation=server.box_generation,
        box_address=warm_dial.host,
        box_ssh_port=warm_dial.port,
        box_ssh_user=ssh_user,
        private_key_path=str(private_key_path),
        box_host_public_key=server.box_host_public_key,
    )
    box_disk_names = client.list_disk_names()
    assert_box_is_exclusive_to_tier(
        server=server,
        env_name=CI_WARM_PSEUDO_ENV_NAME,
        box_disk_names=box_disk_names,
        trust=client.read_management_trust(),
        expected_ca_public_key=management_trust.trusted_user_ca_public_key,
    )
    assert_bake_box_storage_is_encrypted(server, client)
    box_used_slots = count_slice_resource_names(box_disk_names)
    if server.slot_count - box_used_slots < 1:
        raise click.UsageError(
            f"server {server.id} has no free slot ({box_used_slots}/{server.slot_count} in use); cannot "
            "carve the throwaway warm slice"
        )

    sweep_stale_bake_namespaces()
    with ephemeral_bake_namespace() as bake_namespace:
        if mngr_source is not None:
            sync_mngr_into_template(Path(mngr_source), workspace_dir)
        cache_tag = compute_content_addressed_cache_tag(workspace_dir)
        cache = SshBoxImageCache(
            slice_client=client, cache_dir=box_image_cache_dir_for_generation(server.box_generation, ssh_user)
        )
        if cache.has_tar(cache_tag):
            logger.info("Box {} already holds the tar for {}; nothing to warm", server.public_address, cache_tag)
            report = WarmCacheReport(
                cache_tag=cache_tag,
                server_id=str(server.id),
                was_tar_already_present=True,
                is_warmed=True,
                slices=(),
            )
            emit_json(report.model_dump(mode="json", exclude_none=True))
            return

        logger.info(
            "Warming box {} image cache for {} with one throwaway {} slice",
            server.public_address,
            cache_tag,
            CI_WARM_PSEUDO_ENV_NAME,
        )
        try:
            outcome = _run_bake_attempts(
                lambda: _warm_bake_one_slice(
                    server=server,
                    sizing=sizing,
                    region=region,
                    workspace_dir=workspace_dir,
                    management_trust=management_trust,
                    private_key_path=private_key_path,
                    default_workspace_template_cache_tag=cache_tag,
                    extra_create_env=bake_namespace.to_subprocess_env(),
                ),
                _SLICE_BAKE_ATTEMPT_COUNT,
                termination_event=threading.Event(),
            )
        finally:
            # The throwaway slice is destroyed unconditionally -- its only
            # purpose was publishing the tar.
            _reap_ci_warm_slice_resources(client)
        # The warm's goal is the tar, not the slice: a bake that failed after
        # the tar was published (e.g. during agent bootstrap) still warmed the
        # box, so success is judged by the tar's presence.
        is_warmed = cache.has_tar(cache_tag)
        report = WarmCacheReport(
            cache_tag=cache_tag,
            server_id=str(server.id),
            was_tar_already_present=False,
            is_warmed=is_warmed,
            slices=(outcome,),
        )
        emit_json(report.model_dump(mode="json", exclude_none=True))
        if not is_warmed:
            raise SystemExit(1)


@server.command(name="set-status")
@click.option("--server-id", required=True, help="bare_metal_servers row id.")
@click.option("--status", required=True, help="New lifecycle status.")
@click.option("--database-url", default=None)
def set_status(server_id: str, status: str, database_url: str | None) -> None:
    """Advance a server's lifecycle status (resumable order->delivered->installing->ready).

    ``draining`` is refused here: a bare status flip would leave the box's
    ``available`` rows leasable, which is what ``server drain`` exists to
    prevent (it destroys them and force-stops the leased workspaces too).
    """
    validated = BareMetalServerStatus(status)
    if str(validated) == SERVER_STATUS_DRAINING:
        raise click.UsageError(
            f"'{SERVER_STATUS_DRAINING}' is not set directly; run `minds-admin server drain --server-id {server_id}`, "
            "which also empties the box's pool and force-stops its leased workspaces"
        )
    conn = psycopg2.connect(resolve_pool_database_url(database_url))
    try:
        update_server(conn, BareMetalServerDbId(server_id), status=str(validated))
    finally:
        conn.close()
    logger.info("Set server {} status to {}", server_id, validated)


@server.command(name="drain")
@click.option("--server-id", required=True, help="bare_metal_servers row id to drain.")
@click.option("--database-url", default=None, help=DATABASE_URL_HELP)
@click.option(
    "--max-concurrency",
    type=int,
    default=DEFAULT_SLICE_DESTROY_CONCURRENCY,
    show_default=True,
    help="Max unleased slices destroyed at once; the rest queue.",
)
@paid_auth_options
def drain_server(
    server_id: str,
    database_url: str | None,
    max_concurrency: int,
    connector_url: str | None,
    api_key: str | None,
) -> None:
    """Take a box out of service for maintenance: mark it draining, empty its pool, force-stop its workspaces.

    The box-maintenance primitive (repave, kernel reboot, hardware repair,
    retirement). ``draining`` takes the box out of every new-work path (bake,
    restore candidates, restart-in-place). The box's unleased pool rows are
    then destroyed (they would otherwise still be handed out by the
    connector's lease, which selects rows, not boxes), and each leased
    workspace is force-stopped through the connector's admin stop -- the same
    data-preserving transition the owner's stop runs, so the workspace's next
    start restores it onto a surviving box. Silent by design: the user sees
    the ordinary stopped-then-restoring flow. Idempotent: re-run until the
    box reports no remaining rows, then do the maintenance. ``server undrain``
    returns the box to ``ready`` afterwards (a repave does so on its own).
    """
    if max_concurrency <= 0:
        raise click.UsageError("--max-concurrency must be positive")
    resolved_database_url = resolve_pool_database_url(database_url)
    server_row = _fetch_server_or_raise(resolved_database_url, server_id)
    if str(server_row.status) not in (SERVER_STATUS_READY, SERVER_STATUS_DRAINING):
        raise click.UsageError(
            f"server {server_id} is '{server_row.status}'; only a '{SERVER_STATUS_READY}' (or already "
            f"'{SERVER_STATUS_DRAINING}') box can be drained"
        )
    _update_server_fields(resolved_database_url, server_id, status=SERVER_STATUS_DRAINING)

    # Destroy the box's unleased rows so the connector's lease cannot hand out
    # new workspaces on the draining box (the lease selects rows, and only the
    # destroy removes them). Claimable statuses only; leased rows are stopped
    # below, never destroyed.
    conn = psycopg2.connect(resolved_database_url)
    try:
        unleased_statuses = destroy_eligible_pool_host_statuses(is_leased_destroy_allowed=False)
        unleased_row_ids = fetch_pool_host_ids_on_server_by_status(
            conn, BareMetalServerDbId(server_id), unleased_statuses
        )
        leased_row_ids = fetch_pool_host_ids_on_server_by_status(
            conn, BareMetalServerDbId(server_id), (POOL_HOST_STATUS_LEASED,)
        )
    finally:
        conn.close()
    destroy_outcomes: list[PoolHostDestroyOutcome] = []
    if unleased_row_ids:
        with box_management_identities() as identities:
            destroy_outcomes = destroy_pool_hosts_in_parallel(
                pool_host_ids=unleased_row_ids,
                database_url=resolved_database_url,
                identities=identities,
                eligible_statuses=unleased_statuses,
                is_row_drop_only=False,
                max_concurrency=max_concurrency,
            )
    destroy_report = build_pool_host_destroy_report(destroy_outcomes)

    # Force-stop every leased workspace through the connector (the transition
    # supervisor uploads the artifact and frees the slot after retention).
    # Per-row error capture, like the destroy outcomes above: one wedged
    # workspace must not abort the remaining stops or suppress the report
    # (drain is re-run until the box reports no remaining rows). Auth errors
    # are not caught -- a bad admin key fails every row identically.
    stop_result_by_row_id: dict[str, Any] = {}
    failed_stop_count = 0
    if leased_row_ids:
        client = make_admin_connector_client(connector_url)
        admin_key = resolve_admin_api_key(api_key)
        for leased_row_id in leased_row_ids:
            try:
                stop_result_by_row_id[leased_row_id] = client.admin_stop_workspace(admin_key, leased_row_id)
            except ImbueCloudConnectorError as exc:
                logger.warning("Force-stop of leased workspace {} failed: {}", leased_row_id, exc)
                stop_result_by_row_id[leased_row_id] = {"error": str(exc)}
                failed_stop_count += 1

    emit_json(
        {
            "server_id": server_id,
            "status": SERVER_STATUS_DRAINING,
            "destroyed_unleased": destroy_report.model_dump(mode="json", exclude_none=True),
            "stopping_leased": stop_result_by_row_id,
        }
    )
    if destroy_report.failed or failed_stop_count:
        raise SystemExit(1)


@server.command(name="undrain")
@click.option("--server-id", required=True, help="bare_metal_servers row id to return to service.")
@click.option("--database-url", default=None, help=DATABASE_URL_HELP)
def undrain_server(server_id: str, database_url: str | None) -> None:
    """Return a drained box to ``ready`` so bakes and restores may use it again.

    Only reverses the status flip of ``server drain``: rows the drain destroyed
    stay destroyed and its stopped workspaces restore wherever the fleet
    places them at their next start. Refuses a box in any status other than
    ``draining`` (a repave lands on ``ready`` by itself).
    """
    resolved_database_url = resolve_pool_database_url(database_url)
    server_row = _fetch_server_or_raise(resolved_database_url, server_id)
    if str(server_row.status) != SERVER_STATUS_DRAINING:
        raise click.UsageError(
            f"server {server_id} is '{server_row.status}'; only a '{SERVER_STATUS_DRAINING}' box can be undrained"
        )
    _update_server_fields(resolved_database_url, server_id, status=SERVER_STATUS_READY)
    emit_json({"server_id": server_id, "status": SERVER_STATUS_READY})


def _format_delivery(delivery_hours: int) -> str:
    """Human-readable delivery time from OVH availability hours (e.g. 1 -> '~1h', 72 -> '3d')."""
    if delivery_hours <= 0:
        return "?"
    if delivery_hours < 24:
        return f"~{delivery_hours}h"
    return f"{delivery_hours // 24}d"


def _format_storage_options(row: SlicePricingRow) -> str:
    """Render a row's storage upgrade options as a compact end-of-row string."""
    if not row.storage_options:
        return "-"
    return "  ".join(
        f"{option.label}(+{option.extra_disk_gb_per_slice}G/slice @ ${option.dollars_per_extra_gb}/GB)"
        for option in row.storage_options
    )


def _format_slice_pricing_table(rows: list[SlicePricingRow]) -> str:
    """Render the per-slice pricing rows as a plain table (already sorted cheapest-per-slice first)."""
    headers = [
        "$/SLICE/MO",
        "PLAN_CODE",
        "MODEL",
        "REGION",
        "DELIVERY",
        "STOCK",
        "RAM_GB",
        "SLOTS",
        "CPU(c/t)",
        "CPU/SLICE",
        "DISK/SLICE(GiB)",
        "UNITS_VALID",
        "$/MO",
        "SETUP",
        "BASE_STORAGE",
        "STORAGE_UPGRADES (per slice)",
    ]
    table_rows = [
        [
            f"{row.price_per_slice_usd:.2f}",
            row.plan_code,
            row.server_model,
            row.region,
            _format_delivery(row.delivery_hours),
            row.stock_level or "-",
            row.server_ram_gb,
            row.slot_count,
            f"{row.cpu_cores}c/{row.cpu_threads}t",
            row.cpus_per_slice,
            row.disk_gb_per_slice,
            "yes" if row.is_units_valid else "NO",
            f"{row.recurring_monthly_usd:.2f}",
            f"{row.one_time_setup_usd:.2f}",
            row.base_storage_label,
            _format_storage_options(row),
        ]
        for row in rows
    ]
    return tabulate(table_rows, headers=headers, tablefmt="plain")


@server.command(name="pricing")
@click.option(
    "--region",
    "regions",
    type=click.Choice(sorted(OVH_US_DATACENTER_CODES)),
    multiple=True,
    help="Restrict to a US datacenter (vin=US-EAST-VA, hil=US-WEST-OR). Repeatable; default: both.",
)
@click.option(
    "--memory-per-slice-gb",
    type=int,
    default=DEFAULT_MEMORY_PER_SLICE_GB,
    show_default=True,
    help="RAM (GB) per slice; sets slot count (floor(server_RAM / this)) and per-slice CPU/disk sizing.",
)
@click.option(
    "--cpu-overcommit",
    type=float,
    default=DEFAULT_SLICE_CPU_OVERCOMMIT_RATIO,
    show_default=True,
    help="CPU overcommit factor for sizing each slice's vCPUs.",
)
@click.option(
    "--catalog-name",
    default="eco",
    show_default=True,
    help="OVH catalog to price (eco = the RISE/SYS/KS bare-metal line we carve slices on).",
)
def pricing(regions: tuple[str, ...], memory_per_slice_gb: int, cpu_overcommit: float, catalog_name: str) -> None:
    """Print a per-slice pricing table for OVH bare-metal plans (read-only; never places an order).

    Each row is a server x RAM config; price/slice = (month-to-month + setup/12) / slots, sorted cheapest
    first, with delivery time + stock from OVH availability and storage-upgrade options at the end of each
    row. The OVH credentials come from the activated tier's ovh Vault entry (or the OVH_* env vars).
    """
    config = resolve_ovh_config()
    allowed_regions = frozenset(regions) if regions else OVH_US_DATACENTER_CODES

    client = build_ovh_client(config)
    # The OVH SDK's generic call() sends kwargs as the request body, so for GETs the query params must
    # go in the path; the availabilities endpoint takes no params here (we fetch all and filter locally).
    catalog_path = f"/order/catalog/public/{catalog_name}?{urlencode({'ovhSubsidiary': client.subsidiary})}"
    catalog = client.call_api("GET", catalog_path)
    availabilities = client.call_api("GET", "/dedicated/server/datacenter/availabilities")
    rows = compute_slice_pricing_rows(catalog, availabilities, allowed_regions, memory_per_slice_gb, cpu_overcommit)

    region_label = ",".join(sorted(allowed_regions))
    if not rows:
        write_human_line(f"No orderable plans found in region(s) {region_label} at {memory_per_slice_gb}GB/slice.")
        return
    header = (
        f"OVH bare-metal slice pricing -- {memory_per_slice_gb}GB/slice, "
        f"{cpu_overcommit}x CPU overcommit, region(s) {region_label} (catalog '{catalog_name}')"
    )
    write_human_line(f"{header}\n{_format_slice_pricing_table(rows)}")


def _probe_ssh_ready(
    server_address: str, port: int, ssh_user: str, private_key_path: Path, box_host_public_key: str
) -> bool | None:
    """One SSH-readiness probe: True once a login succeeds, else None (for poll_for_value)."""
    cg = ConcurrencyGroup(name="ssh-ready")
    with _box_ssh_host_key_options(server_address, port, box_host_public_key) as host_key_opts:
        with cg:
            result = cg.run_process_to_completion(
                command=[
                    "ssh",
                    "-i",
                    str(private_key_path),
                    "-p",
                    str(port),
                    *host_key_opts,
                    "-o",
                    "ConnectTimeout=15",
                    f"{ssh_user}@{server_address}",
                    "echo ok",
                ],
                timeout=30.0,
                is_checked_after=False,
            )
    return True if result.returncode == 0 else None


def _wait_for_ssh_ready(
    dial: BoxManagementDial,
    ssh_user: str,
    private_key_path: Path,
    timeout_seconds: float,
    box_host_public_key: str,
) -> None:
    """Poll until the box accepts an SSH login (it reboots into the freshly-installed OS). Raises on timeout."""
    server_address = dial.host
    with log_span("Waiting for SSH on {}:{} as {}", server_address, dial.port, ssh_user):
        is_ready, _polls, _elapsed = poll_for_value(
            lambda: _probe_ssh_ready(server_address, dial.port, ssh_user, private_key_path, box_host_public_key),
            timeout=timeout_seconds,
            poll_interval=10.0,
        )
    if not is_ready:
        raise BareMetalProvisioningError(f"SSH to {server_address} not ready within {timeout_seconds:.0f}s")


@server.command(name="order")
@click.option("--plan-code", required=True, help="OVH eco planCode to order (e.g. 24rise01-v1-us).")
@click.option(
    "--region",
    required=True,
    type=click.Choice(sorted(OVH_US_DATACENTER_CODES)),
    help="OVH US datacenter to order in (vin = US-EAST-VA, hil = US-WEST-OR).",
)
@click.option("--memory-gb", required=True, type=int, help="Server RAM in GB (selects the memory option).")
@click.option(
    "--storage",
    required=True,
    help="Storage option short code (the pricing table's BASE_STORAGE, e.g. softraid-2x512nvme).",
)
@click.option(
    "--memory-per-slice-gb",
    type=int,
    default=DEFAULT_MEMORY_PER_SLICE_GB,
    show_default=True,
    help="RAM (GB) each slice will advertise; sets slot_count = floor(server RAM / this).",
)
@click.option(
    "--cpu-overcommit",
    type=float,
    default=DEFAULT_SLICE_CPU_OVERCOMMIT_RATIO,
    show_default=True,
    help="CPU overcommit factor recorded for slice sizing on this box.",
)
@click.option(
    "--box-generation",
    type=int,
    default=2,
    show_default=True,
    help=(
        "Slice-fleet generation the box will be set up as (2 = trixie+qemu, the default for new orders; "
        "specs/slice-fleet). Gen-2 orders are units-validated: a storage config too small for the RAM's "
        "full complement of default-size machines is refused."
    ),
)
@click.option(
    "--option",
    "option_codes",
    multiple=True,
    help=(
        "Explicit planCode for a mandatory option family that offers more than one choice (e.g. "
        "bandwidth, vrack). Repeatable. Required when the plan offers a real choice -- run once without "
        "it and the error lists each family's offers + monthly prices so you can re-run with --option."
    ),
)
@click.option("--yes", is_flag=True, default=False, help="Skip the interactive confirmation and place the order.")
@click.option(
    "--dry-run",
    "is_dry_run",
    is_flag=True,
    default=False,
    help=(
        "Build + assign a non-committal cart, print the real OVH price preview + derived specs, then delete "
        "the cart without ordering. No charge and no prompt -- use it to confirm price/specs before ordering."
    ),
)
@click.option(
    "--uplink-mbps",
    type=click.IntRange(min=1),
    default=None,
    help=(
        "Declared public uplink rate in Mbit/s. Normally derived from the plan's selected bandwidth option "
        "code; pass it explicitly only when that code carries no rate."
    ),
)
@click.option("--database-url", default=None, help="Pool DSN (else resolved from env/activated minds env).")
def order(
    plan_code: str,
    region: str,
    memory_gb: int,
    storage: str,
    memory_per_slice_gb: int,
    cpu_overcommit: float,
    box_generation: int,
    option_codes: tuple[str, ...],
    yes: bool,
    is_dry_run: bool,
    uplink_mbps: int | None,
    database_url: str | None,
) -> None:
    """Order a bare-metal server from OVH (THIS CHARGES the account) and record it at status 'ordered'.

    Builds + assigns the eco cart, shows the real OVH price preview for confirmation, places the order, and
    inserts a bare_metal_servers row (specs derived from the catalog). Then run ``await-delivery`` + ``setup``.
    Any mandatory option family with more than one offer (e.g. bandwidth, vrack) must be chosen explicitly
    via ``--option``. The OVH credentials and the pool DSN resolve from the activated tier (OVH_* env vars /
    ``--database-url`` override). Pass ``--dry-run`` to price + preview only (no charge, no prompt, no DB
    write); ``--dry-run`` wins over ``--yes``.
    """
    config = resolve_ovh_config()
    client = build_ovh_client(config)
    catalog_path = f"/order/catalog/public/eco?{urlencode({'ovhSubsidiary': client.subsidiary})}"
    catalog = client.call_api("GET", catalog_path)
    cpu_cores, cpu_threads, disk_gb, raid_level = derive_server_specs(catalog, plan_code, storage)
    slot_count = compute_slot_count(memory_gb, memory_per_slice_gb)
    if slot_count <= 0:
        raise BareMetalProvisioningError(
            f"{memory_gb}GB RAM / {memory_per_slice_gb}GB per slice yields 0 slots; pick a smaller slice size"
        )
    # The gen-2 units-valid guard (specs/slice-fleet): refused before any cart
    # exists, so an insufficient storage config is never charged for.
    if box_generation >= FIRST_QEMU_BOX_GENERATION:
        try:
            assert_gen2_box_disk_fits_default_machines(ram_gb=memory_gb, disk_gb=disk_gb)
        except BareMetalConfigError as exc:
            raise click.UsageError(str(exc)) from exc

    cart_id, preview, selected_option_codes = build_and_assign_eco_cart(
        client,
        plan_code=plan_code,
        datacenter=region,
        memory_gb=memory_gb,
        storage_short=storage,
        explicit_option_codes=option_codes,
    )
    # The declared uplink sizes the box's fair-share shaping, its egress signal
    # and its link-speed audit, so a box is never recorded without one. It is
    # read off the selected bandwidth option; the cart is not yet checked out,
    # so a refusal here charges nothing.
    resolved_uplink_mbps = (
        uplink_mbps if uplink_mbps is not None else derive_uplink_mbps_from_option_codes(selected_option_codes)
    )
    if resolved_uplink_mbps is None:
        delete_cart_quietly(client, cart_id)
        raise click.UsageError(
            f"could not derive the uplink rate from the selected option codes {sorted(selected_option_codes)} "
            "(expected exactly one 'bandwidth-<mbps>-...' code); pass --uplink-mbps explicitly"
        )
    write_human_line(
        f"About to order {plan_code} in {region}: {memory_gb}GB RAM, {storage}, {cpu_cores}c/{cpu_threads}t, "
        f"{disk_gb}GB usable disk ({raid_level}), {resolved_uplink_mbps} Mbit/s uplink -> {slot_count} slices "
        f"of {memory_per_slice_gb}GB.\n"
        f"OVH price preview:\n{summarize_checkout_prices(preview)}"
    )
    # A dry run stops here (before any prompt or charge), deleting the non-committal cart. Checked ahead of
    # --yes so an accidental `--dry-run --yes` never charges.
    if is_dry_run:
        delete_cart_quietly(client, cart_id)
        write_human_line("Dry run: cart deleted, no order placed.")
        return
    if not yes and not click.confirm("Place this order now (this charges the account)?", default=False):
        delete_cart_quietly(client, cart_id)
        write_human_line("Aborted; cart deleted, no order placed.")
        return

    order_id = checkout_eco_cart(client, cart_id)
    now = datetime.now(timezone.utc)
    server_row = BareMetalServer(
        id=BareMetalServerDbId(str(uuid4())),
        ovh_order_id=str(order_id),
        ovh_service_name=None,
        plan_code=plan_code,
        region=region,
        public_address=None,
        cpu_cores=cpu_cores,
        cpu_threads=cpu_threads,
        ram_gb=memory_gb,
        disk_gb=disk_gb,
        memory_per_slice_gb=memory_per_slice_gb,
        cpu_overcommit_ratio=cpu_overcommit,
        slot_count=slot_count,
        raid_level=raid_level,
        slice_service_user=None,
        status=BareMetalServerStatus(SERVER_STATUS_ORDERED),
        created_at=now,
        updated_at=now,
        box_generation=box_generation,
        uplink_mbps=resolved_uplink_mbps,
    )
    conn = psycopg2.connect(resolve_pool_database_url(database_url))
    try:
        insert_bare_metal_server(conn, server_row)
    finally:
        conn.close()
    write_human_line(
        f"Ordered {plan_code} (OVH order {order_id}); recorded server {server_row.id} at status 'ordered'. "
        f"Next: `minds-admin server await-delivery --server-id {server_row.id}`."
    )


def _fetch_server_or_raise(dsn: str, server_id: str) -> BareMetalServer:
    """Read one server row with a short-lived connection (never held across a long OVH/SSH wait)."""
    conn = psycopg2.connect(dsn)
    try:
        server = fetch_server_by_id(conn, BareMetalServerDbId(server_id))
    finally:
        conn.close()
    if server is None:
        raise BareMetalProvisioningError(f"no bare_metal_servers row with id {server_id}")
    return server


def _require_box_reachable_with_pinned_host_key(server: BareMetalServer, *, action: str) -> str:
    """The box's recorded sshd host key, refusing a row with no address to dial or no host key to pin.

    Fails closed: every box SSH strictly pins the recorded host key (no
    trust-on-first-use), which ``server setup`` injects at the OS reinstall or
    the one-time ``pool backfill-host-keys`` captures.
    """
    if not server.public_address:
        raise BareMetalProvisioningError(
            f"server {server.id} has no public_address; the box cannot be reached for {action}"
        )
    if not server.box_host_public_key:
        raise BareMetalProvisioningError(
            f"server {server.id} has no recorded box host key to pin; run `minds-admin server setup` (reinstalls the "
            f"OS with our injected key) or the one-time `minds-admin pool backfill-host-keys` before {action}"
        )
    return server.box_host_public_key


def _update_server_fields(dsn: str, server_id: str, **fields: Any) -> None:
    """Update a server row with a short-lived connection (Neon drops connections idle across a long wait)."""
    conn = psycopg2.connect(dsn)
    try:
        update_server(conn, BareMetalServerDbId(server_id), **fields)
    finally:
        conn.close()


@server.command(name="await-delivery")
@click.option("--server-id", required=True, help="bare_metal_servers row id (from `order`).")
@click.option("--database-url", default=None)
def await_delivery(server_id: str, database_url: str | None) -> None:
    """Wait for OVH to deliver an ordered server (assign a serviceName + IP), then mark it 'delivered'.

    Resumable: a no-op if the server is already delivered. Delivery can take a while (often ~1h).
    """
    dsn = resolve_pool_database_url(database_url)
    server = _fetch_server_or_raise(dsn, server_id)
    if str(server.status) in (SERVER_STATUS_DELIVERED, SERVER_STATUS_INSTALLING, SERVER_STATUS_READY):
        write_human_line(f"Already delivered: {server.ovh_service_name} ({server.public_address}).")
        return
    if not server.ovh_order_id:
        raise BareMetalProvisioningError(f"server {server_id} has no ovh_order_id to wait on")
    # Resolve serviceName + IP without holding the DB connection (delivery polling can run for ~1h).
    client = build_ovh_client(resolve_ovh_config())
    service_name = wait_for_order_service_name(client, order_id=int(server.ovh_order_id))
    address = wait_for_dedicated_server_address(client, service_name=service_name)
    _update_server_fields(
        dsn,
        server_id,
        ovh_service_name=service_name,
        public_address=address,
        status=SERVER_STATUS_DELIVERED,
    )
    write_human_line(
        f"Server {server_id} delivered: {service_name} ({address}). "
        f"Next: `minds-admin server setup --server-id {server_id}`."
    )


@server.command(name="setup")
@click.option("--server-id", required=True, help="bare_metal_servers row id (delivered).")
@click.option("--ssh-user", default="debian", help="Bootstrap SSH user after reinstall (OS image's default user).")
@click.option(
    "--slice-service-user",
    default=None,
    help=(
        "Dedicated non-root user to create for the slice VMs. Defaults per generation (gen-2 boxes pin "
        f"{GEN2_SLICE_SERVICE_USER!r}; gen-1 boxes keep the row's recorded user)."
    ),
)
@click.option("--lima-version", default=DEFAULT_LIMA_VERSION, help="Lima release to install on the box (gen-1 only).")
@click.option(
    "--slice-base-image-url",
    default=None,
    help=(
        "Guest OS image to stage on the box once (slices boot from this via file://). "
        "Defaults per generation: the bookworm lima image for gen-1 boxes, the pinned trixie image from "
        "imbue's artifact mirror for gen-2 (which then also requires --slice-base-image-sha512)."
    ),
)
@click.option(
    "--slice-base-image-sha512",
    default=None,
    help="The sha512 a gen-2 --slice-base-image-url override must match; the pinned image's digest by default.",
)
@click.option(
    "--os-template",
    default=None,
    help=(
        "OVH OS template to reinstall onto the box. Defaults per generation: "
        f"{DEFAULT_REINSTALL_OS_TEMPLATE} for gen-1 boxes, {GEN2_REINSTALL_OS_TEMPLATE} for gen-2."
    ),
)
@click.option(
    "--ssh-ready-timeout",
    type=float,
    default=DEFAULT_SETUP_SSH_READY_TIMEOUT_SECONDS,
    show_default=True,
    help="Seconds to wait for SSH.",
)
@click.option("--database-url", default=None)
@click.option(
    "--extra-prep-script",
    "extra_prep_script",
    default=None,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help=(
        "Path to an additional idempotent root bash script appended to the composed box prep "
        "(the same escape hatch `server prep` offers). Runs on the box after the standard prep "
        "steps and the collector install, under the same `sudo bash` invocation."
    ),
)
def setup(
    server_id: str,
    ssh_user: str,
    slice_service_user: str | None,
    lima_version: str,
    slice_base_image_url: str | None,
    slice_base_image_sha512: str | None,
    os_template: str | None,
    ssh_ready_timeout: float,
    database_url: str | None,
    extra_prep_script: Path | None,
) -> None:
    """Provision a delivered box to 'ready': reinstall our OS (destructive), run the composed prep.

    Runs the same composed prep as ``server prep`` (dispatched on the box's
    recorded generation: qemu/lima/tooling for gen-1, the gen-2 stack --
    including the management WireGuard and, when configured, the ``:22``
    lockdown -- for gen-2), plus the observability collector when the tier has
    a boxes ingest credential, verified active. Fail-closed: a failed
    collector install or verification fails the prep, and the box is NOT
    marked 'ready'. The OVH credentials, pool DSN, and management SSH key
    resolve from the activated tier (a gen-2 box needs an activated env for
    its operator certificate; the OVH_* / --database-url / POOL_SSH_PRIVATE_KEY
    overrides remain for gen-1).

    A gen-2 reinstall uses the trixie template with the custom partition
    layout (md-mirrored ext4 root + the XFS storage partition), so the box
    comes out of the reinstall already satisfying the prep's storage check.

    Resumable via status: reinstall runs only from 'delivered'; re-running from 'installing' resumes at prep.
    """
    setup_server_to_ready(
        server_id=server_id,
        ssh_user=ssh_user,
        slice_service_user=slice_service_user,
        lima_version=lima_version,
        slice_base_image_url=slice_base_image_url,
        slice_base_image_sha512=slice_base_image_sha512,
        os_template=os_template,
        ssh_ready_timeout=ssh_ready_timeout,
        database_url=database_url,
        extra_prep_script=extra_prep_script,
    )


def setup_server_to_ready(
    *,
    server_id: str,
    ssh_user: str,
    slice_service_user: str | None,
    lima_version: str,
    slice_base_image_url: str | None,
    slice_base_image_sha512: str | None,
    os_template: str | None,
    ssh_ready_timeout: float,
    database_url: str | None,
    extra_prep_script: Path | None,
) -> None:
    """The body of ``server setup``: reinstall from ``delivered``, prep from ``installing``, land on ``ready``.

    Shared with the cutover's ``repave``, which flips a drained box to
    ``delivered`` on generation 2 and then runs exactly this.
    """
    dsn = resolve_pool_database_url(database_url)
    server = _fetch_server_or_raise(dsn, server_id)
    if str(server.status) == SERVER_STATUS_READY:
        write_human_line(f"Server {server_id} is already ready ({server.ovh_service_name}).")
        return
    if str(server.status) not in (SERVER_STATUS_DELIVERED, SERVER_STATUS_INSTALLING):
        raise BareMetalProvisioningError(
            f"server {server_id} is {server.status}; run `await-delivery` until it is 'delivered' first"
        )
    service_name = server.ovh_service_name
    address = server.public_address
    if not service_name or not address:
        raise BareMetalProvisioningError(f"server {server_id} has no serviceName/address; re-run await-delivery")
    is_gen2 = server.box_generation >= FIRST_QEMU_BOX_GENERATION
    service_user = _resolve_service_user_for_generation(
        server.box_generation, slice_service_user, server.slice_service_user
    )
    resolved_os_template = os_template or (GEN2_REINSTALL_OS_TEMPLATE if is_gen2 else DEFAULT_REINSTALL_OS_TEMPLATE)

    client = build_ovh_client(resolve_ovh_config())
    with box_management_identities() as identities:
        # Compose the full prep (base + collector + extra + verification) BEFORE the
        # destructive reinstall, so a Vault failure resolving the tier's observability
        # credential aborts up front instead of stranding a half-reinstalled box. The
        # operator identity (a Vault certificate sign on gen-2) is resolved after the
        # composition, so a tier without its committed [ssh_ca] key refuses with the
        # bring-up pointer rather than with a failed sign.
        if is_gen2:
            # A gen-2 box authorizes no static key: the reinstall's post-install
            # script installs the tier CA trust so the very first management
            # session is already a certificate login, and the OS image's
            # bootstrap key slot gets a throwaway whose private half is never
            # kept (prep removes the authorized_keys it lands in).
            ssh_ca_public_key = require_tier_ssh_ca_public_key("setting up a gen-2 box")
            _throwaway_private, bootstrap_public_key = generate_ed25519_host_keypair()
            gen2_image_url, gen2_image_sha512 = _resolve_gen2_guest_image(
                slice_base_image_url, slice_base_image_sha512
            )
            script = _build_composed_gen2_prep_script(
                dsn=dsn,
                server=server,
                ssh_ca_public_key=ssh_ca_public_key,
                slice_base_image_url=gen2_image_url,
                slice_base_image_sha512=gen2_image_sha512,
                extra_prep_script=extra_prep_script,
            )
        else:
            ssh_ca_public_key = None
            bootstrap_public_key = derive_ssh_public_key(identities.private_key_path_for(server.box_generation))
            script = _build_composed_prep_script(
                pool_public_key=bootstrap_public_key,
                slice_service_user=service_user,
                lima_version=lima_version,
                slice_base_image_url=slice_base_image_url or DEFAULT_IMAGE_URL_X86_64,
                extra_prep_script=extra_prep_script,
            )
        private_key_path = identities.private_key_path_for(server.box_generation)
        # Reinstall only from 'delivered'; re-running from 'installing' assumes the reinstall completed and
        # resumes at SSH-wait + prep. No DB connection is held across the (long) reinstall/prep waits.
        if str(server.status) == SERVER_STATUS_DELIVERED:
            reinstall = start_os_reinstall(
                client,
                service_name=service_name,
                ssh_public_key=bootstrap_public_key,
                ssh_ca_public_key=ssh_ca_public_key,
                os_template=resolved_os_template,
                # Gen-2 boxes reinstall with the custom layout (md-mirrored
                # ext4 root + the XFS storage partition the prep's storage
                # check requires); gen-1 keeps the template's default scheme.
                storage=build_gen2_reinstall_storage() if is_gen2 else None,
            )
            # Persist the injected box host key with the status flip so a resume from
            # 'installing' still has it (we discard the private half after injection).
            _update_server_fields(
                dsn,
                server_id,
                status=SERVER_STATUS_INSTALLING,
                box_host_public_key=reinstall.box_host_public_key,
            )
            wait_for_os_reinstall(client, service_name=service_name, task_id=reinstall.task_id)

        # Re-read so a resume-from-'installing' picks up the box key persisted above.
        # The reinstall always records it alongside the status flip, so a missing key
        # here means the row was tampered with -- fail closed rather than SSH without
        # strict host-key checking.
        installed_server = _fetch_server_or_raise(dsn, server_id)
        box_host_public_key = installed_server.box_host_public_key
        if not box_host_public_key:
            raise BareMetalProvisioningError(
                f"server {server_id} reached '{SERVER_STATUS_INSTALLING}' without a recorded box host key; "
                "cannot SSH the box with strict host-key checking"
            )
        # Right after the reinstall the fresh OS answers only on the public
        # address (the reinstall wiped the overlay and the lockdown); a resume
        # from 'installing' may instead find the prep's lockdown already in
        # place, so it goes through the management resolver, which falls back
        # to the public address when the box has no overlay yet.
        if str(server.status) == SERVER_STATUS_DELIVERED:
            dial = BoxManagementDial(host=address, port=MANAGEMENT_SSH_PORT)
        else:
            dial = resolve_server_management_dial(installed_server)
        _wait_for_ssh_ready(dial, ssh_user, private_key_path, ssh_ready_timeout, box_host_public_key)
        logger.info("Prepping delivered box {} ({}:{})", server_id, dial.host, dial.port)
        prep_stdout = _run_box_prep_script(
            server,
            _BoxSshTarget(
                host=dial.host,
                port=dial.port,
                ssh_user=ssh_user,
                private_key_path=private_key_path,
                box_host_public_key=box_host_public_key,
            ),
            script,
            _post_prep_target_resolver(dsn, server_id, ssh_user, private_key_path, box_host_public_key),
        )

    if is_gen2:
        # The WireGuard key was recorded by the post-script resolver, before the storage round trips.
        _record_box_storage_partition_gib(dsn, server_id, prep_stdout)
    _stamp_server_service_user(dsn, server_id, service_user, status=SERVER_STATUS_READY)
    write_human_line(
        f"Server {server_id} is READY: {service_name} ({address}), "
        f"{server.slot_count} slots. Bake a slice with `minds-admin pool create`."
    )
