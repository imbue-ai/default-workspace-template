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
import shlex
import shutil
import signal
import tempfile
import threading
from collections.abc import Callable
from collections.abc import Iterator
from collections.abc import Mapping
from collections.abc import Sequence
from contextlib import AbstractContextManager
from contextlib import contextmanager
from contextlib import nullcontext
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
from tabulate import tabulate

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.concurrency_group import ObservableThread
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.pure import pure
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
from imbue.minds_admin.cli._tier_secrets import resolve_boxes_collector_install_config_or_none
from imbue.minds_admin.cli._tier_secrets import resolve_ovh_config
from imbue.minds_admin.cli._tier_secrets import resolve_pool_database_url
from imbue.minds_admin.cli._tier_secrets import resolve_pool_private_key_pem
from imbue.minds_admin.slices.bare_metal_db import POOL_HOST_STATUS_LEASED
from imbue.minds_admin.slices.bare_metal_db import build_slice_pool_host_insert_values
from imbue.minds_admin.slices.bare_metal_db import claim_pool_host_for_removal
from imbue.minds_admin.slices.bare_metal_db import delete_pool_host_row
from imbue.minds_admin.slices.bare_metal_db import destroy_eligible_pool_host_statuses
from imbue.minds_admin.slices.bare_metal_db import fetch_pool_host_destroy_target
from imbue.minds_admin.slices.bare_metal_db import fetch_pool_host_status
from imbue.minds_admin.slices.bare_metal_db import fetch_server_by_id
from imbue.minds_admin.slices.bare_metal_db import fetch_server_capacities
from imbue.minds_admin.slices.bare_metal_db import fetch_servers
from imbue.minds_admin.slices.bare_metal_db import fetch_slice_disk_names_for_server
from imbue.minds_admin.slices.bare_metal_db import fetch_slice_instance_names_for_server
from imbue.minds_admin.slices.bare_metal_db import fetch_unleased_slice_teardown_row_ids
from imbue.minds_admin.slices.bare_metal_db import insert_bare_metal_server
from imbue.minds_admin.slices.bare_metal_db import insert_slice_pool_host
from imbue.minds_admin.slices.bare_metal_db import update_server
from imbue.minds_admin.slices.bare_metal_db import upsert_bare_metal_server
from imbue.minds_admin.slices.bare_metal_prep import DEFAULT_LIMA_VERSION
from imbue.minds_admin.slices.bare_metal_prep import build_box_prep_script
from imbue.minds_admin.slices.ci_slice_sweep import CiSliceSweepBoxReport
from imbue.minds_admin.slices.ci_slice_sweep import CiSliceSweepReport
from imbue.minds_admin.slices.ci_slice_sweep import DEFAULT_CI_SLICE_MAX_AGE_HOURS
from imbue.minds_admin.slices.ci_slice_sweep import sweep_ci_slices_on_box
from imbue.minds_admin.slices.ordering import DEFAULT_REINSTALL_OS_TEMPLATE
from imbue.minds_admin.slices.ordering import build_and_assign_eco_cart
from imbue.minds_admin.slices.ordering import checkout_eco_cart
from imbue.minds_admin.slices.ordering import delete_cart_quietly
from imbue.minds_admin.slices.ordering import derive_server_specs
from imbue.minds_admin.slices.ordering import start_os_reinstall
from imbue.minds_admin.slices.ordering import summarize_checkout_prices
from imbue.minds_admin.slices.ordering import wait_for_dedicated_server_address
from imbue.minds_admin.slices.ordering import wait_for_order_service_name
from imbue.minds_admin.slices.ordering import wait_for_os_reinstall
from imbue.minds_admin.slices.pricing import compute_slice_pricing_rows
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.errors import MngrError
from imbue.mngr.primitives import HostId
from imbue.mngr.providers.ssh_utils import add_host_to_known_hosts
from imbue.mngr.utils.polling import poll_for_value
from imbue.mngr_imbue_cloud.cli._common import emit_json
from imbue.mngr_imbue_cloud.data_types import BareMetalServer
from imbue.mngr_imbue_cloud.data_types import BareMetalServerCapacity
from imbue.mngr_imbue_cloud.data_types import BoxTierAudit
from imbue.mngr_imbue_cloud.data_types import BoxTierAuditReport
from imbue.mngr_imbue_cloud.data_types import PoolHostDestroyOutcome
from imbue.mngr_imbue_cloud.data_types import PoolHostDestroyReport
from imbue.mngr_imbue_cloud.data_types import SliceBakeOutcome
from imbue.mngr_imbue_cloud.data_types import SliceBakeReport
from imbue.mngr_imbue_cloud.data_types import SlicePricingRow
from imbue.mngr_imbue_cloud.data_types import UnauditedBox
from imbue.mngr_imbue_cloud.data_types import WarmCacheReport
from imbue.mngr_imbue_cloud.errors import BareMetalProvisioningError
from imbue.mngr_imbue_cloud.errors import SliceBakeTerminatedError
from imbue.mngr_imbue_cloud.primitives import BareMetalServerDbId
from imbue.mngr_imbue_cloud.primitives import BareMetalServerStatus
from imbue.mngr_imbue_cloud.primitives import EXPECTED_AUTHORIZED_KEY_COUNT
from imbue.mngr_imbue_cloud.primitives import OVH_US_DATACENTER_CODES
from imbue.mngr_imbue_cloud.primitives import PoolHostDestroyOutcomeStatus
from imbue.mngr_imbue_cloud.primitives import SERVER_STATUS_DELIVERED
from imbue.mngr_imbue_cloud.primitives import SERVER_STATUS_INSTALLING
from imbue.mngr_imbue_cloud.primitives import SERVER_STATUS_ORDERED
from imbue.mngr_imbue_cloud.primitives import SERVER_STATUS_READY
from imbue.mngr_imbue_cloud.primitives import SliceBakeOutcomeStatus
from imbue.mngr_imbue_cloud.primitives import US_REGION_BY_OVH_DATACENTER_CODE
from imbue.mngr_imbue_cloud.primitives import is_box_exclusive_to_tier
from imbue.mngr_imbue_cloud.primitives import tier_for_env_name
from imbue.mngr_imbue_cloud.slices.bare_metal import DEFAULT_MEMORY_PER_SLICE_GB
from imbue.mngr_imbue_cloud.slices.bare_metal import DEFAULT_SLICE_CPU_OVERCOMMIT_RATIO
from imbue.mngr_imbue_cloud.slices.bare_metal import DEFAULT_SLICE_PORT_RANGE_END
from imbue.mngr_imbue_cloud.slices.bare_metal import DEFAULT_SLICE_PORT_RANGE_START
from imbue.mngr_imbue_cloud.slices.bare_metal import assert_env_name_fits_slice_names
from imbue.mngr_imbue_cloud.slices.bare_metal import box_default_workspace_template_cache_dir
from imbue.mngr_imbue_cloud.slices.bare_metal import compute_orphan_slice_disk_names
from imbue.mngr_imbue_cloud.slices.bare_metal import compute_orphan_slice_instance_names
from imbue.mngr_imbue_cloud.slices.bare_metal import compute_slice_disk_gib
from imbue.mngr_imbue_cloud.slices.bare_metal import compute_slice_memory_mib
from imbue.mngr_imbue_cloud.slices.bare_metal import compute_slice_vcpus
from imbue.mngr_imbue_cloud.slices.bare_metal import compute_slot_count
from imbue.mngr_imbue_cloud.slices.bare_metal import count_slice_resource_names
from imbue.mngr_imbue_cloud.slices.bare_metal import find_server_capacity_by_id
from imbue.mngr_imbue_cloud.slices.bare_metal import foreign_tier_slice_names
from imbue.mngr_imbue_cloud.slices.bare_metal import is_slice_owned_by_env
from imbue.mngr_imbue_cloud.slices.bare_metal import parse_degraded_md_arrays
from imbue.mngr_imbue_cloud.slices.bare_metal import parse_raw_swap_devices
from imbue.mngr_imbue_cloud.slices.bare_metal import slice_lima_disk_name
from imbue.mngr_imbue_cloud.slices.bare_metal import slice_lima_instance_name
from imbue.mngr_imbue_cloud.slices.box_image_cache import BoxImageCacheInterface
from imbue.mngr_imbue_cloud.slices.lima_box_image_cache import LimaBoxImageCache
from imbue.mngr_imbue_cloud.slices.lima_slice_client import LimaSliceVpsClient
from imbue.mngr_lima.constants import DEFAULT_IMAGE_URL_X86_64
from imbue.mngr_lima.errors import LimaCommandError
from imbue.mngr_ovh.client import build_ovh_client
from imbue.mngr_vps.primitives import VpsInstanceId
from imbue.observability.collector_install import render_collector_install_script


def _format_capacity_table(capacities: list[BareMetalServerCapacity]) -> str:
    """Render the server capacity table (one row per box + a fleet total)."""
    header = f"{'ID':<38}{'PLAN':<20}{'REGION':<8}{'STATUS':<12}{'ADDRESS':<18}{'SLOTS(used/total)':>18}"
    lines = [header]
    total_slots = 0
    total_used = 0
    for capacity in capacities:
        server = capacity.server
        total_slots += server.slot_count
        total_used += capacity.used_slots
        lines.append(
            f"{str(server.id):<38}{server.plan_code[:19]:<20}{server.region[:7]:<8}"
            f"{str(server.status):<12}{str(server.public_address or '-')[:17]:<18}"
            f"{f'{capacity.used_slots}/{server.slot_count}':>18}"
        )
    lines.append(
        f"\nFLEET: {len(capacities)} servers, {total_used}/{total_slots} slots used, {total_slots - total_used} free"
    )
    return "\n".join(lines)


@click.group(name="server")
def server() -> None:
    """Bare-metal server fleet management for the activated minds env (pricing / order / setup / prep / list / ...)."""


@contextmanager
def pool_private_key_path(private_key_pem: str) -> Iterator[Path]:
    """Yield a 0600 temp file holding the given pool management private key PEM.

    The PEM is resolved by the caller (the activated tier's Vault entry, or the
    ``POOL_SSH_PRIVATE_KEY`` env-var override -- see
    :func:`imbue.minds_admin.cli._tier_secrets.resolve_pool_private_key_pem`).
    The temp directory is removed on exit so the sensitive private key never
    lingers on the operator's disk after the command finishes.
    """
    pem = private_key_pem
    key_dir = Path(tempfile.mkdtemp(prefix="mngr-pool-key-"))
    try:
        key_path = key_dir / "id"
        key_path.write_text(pem if pem.endswith("\n") else pem + "\n")
        key_path.chmod(0o600)
        yield key_path
    finally:
        shutil.rmtree(key_dir, ignore_errors=True)


def _derive_public_key(private_key_path: Path) -> str:
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


@contextmanager
def _box_ssh_host_key_options(server_address: str, box_host_public_key: str) -> Iterator[list[str]]:
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
        add_host_to_known_hosts(Path(known_hosts_path), server_address, 22, box_host_public_key)
        yield ["-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={known_hosts_path}"]
    finally:
        Path(known_hosts_path).unlink(missing_ok=True)


def _run_root_script_over_ssh(
    server_address: str,
    ssh_user: str,
    private_key_path: Path,
    script: str,
    box_host_public_key: str,
) -> None:
    """Pipe a bash script to ``sudo bash`` on the box over SSH (base64 to dodge quoting)."""
    encoded = base64.b64encode(script.encode()).decode()
    remote = f"echo {encoded} | base64 -d | sudo bash"
    cg = ConcurrencyGroup(name="box-prep-ssh")
    with _box_ssh_host_key_options(server_address, box_host_public_key) as host_key_opts:
        with cg:
            result = cg.run_process_to_completion(
                command=[
                    "ssh",
                    "-i",
                    str(private_key_path),
                    *host_key_opts,
                    "-o",
                    "ConnectTimeout=30",
                    f"{ssh_user}@{server_address}",
                    remote,
                ],
                timeout=_BOX_PREP_SSH_TIMEOUT_SECONDS,
                is_checked_after=False,
                on_output=lambda line, _is_stdout: logger.info("  [box] {}", line.rstrip()),
            )
    if result.returncode != 0:
        raise BareMetalProvisioningError(
            f"box prep on {server_address} failed (exit {result.returncode}): {result.stderr.strip()}"
        )


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


def _build_composed_prep_script(
    *,
    pool_public_key: str,
    lima_service_user: str,
    lima_version: str,
    slice_base_image_url: str,
    extra_prep_script: Path | None,
) -> str:
    """Build the composed prep script `prep` and `setup` share (base + collector + extra + verification).

    Resolves the activated tier's box observability collector in-process (see
    :func:`resolve_boxes_collector_install_config_or_none`): a missing/empty
    ingest credential skips the collector cleanly, a present one makes the
    composed prep fail-closed on it.
    """
    base_script = build_box_prep_script(
        pool_public_key=pool_public_key,
        lima_service_user=lima_service_user,
        lima_version=lima_version,
        slice_base_image_url=slice_base_image_url,
    )
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


@server.command(name="prep")
@click.option(
    "--server-id", required=True, help="bare_metal_servers row id (from `register`/`order`) of the box to prep."
)
@click.option("--ssh-user", default="debian", help="Bootstrap SSH user (the OS image's default cloud user).")
@click.option("--lima-service-user", default="limahost", help="Dedicated non-root user to create for the lima VMs.")
@click.option("--lima-version", default=DEFAULT_LIMA_VERSION, help="Lima release to install on the box.")
@click.option(
    "--slice-base-image-url",
    default=DEFAULT_IMAGE_URL_X86_64,
    show_default=True,
    help="Guest OS image to stage on the box once (slices boot from this via file://, never the mirror).",
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
    lima_service_user: str,
    lima_version: str,
    slice_base_image_url: str,
    database_url: str | None,
    extra_prep_script: Path | None,
) -> None:
    """Install QEMU + lima + tooling on a delivered box, create the lima user, stage the OS image.

    Idempotent. Authorizes the pool management key (POOL_SSH_PRIVATE_KEY) for the
    service user so the admin CLI can bake slices and the connector can tear them
    down, and stages the slice guest OS image once so bakes never depend on the
    Debian mirror. Run after the OS install, before ``minds-admin pool create``.

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
    if not server.public_address:
        raise BareMetalProvisioningError(f"server {server_id} has no public_address; cannot reach the box to prep it")
    if not server.box_host_public_key:
        raise BareMetalProvisioningError(
            f"server {server_id} has no recorded box host key to pin; run `minds-admin server setup` (reinstalls the OS "
            "with our injected key) or the one-time `minds-admin pool backfill-host-keys` before prepping"
        )
    server_address = server.public_address
    with pool_private_key_path(resolve_pool_private_key_pem()) as private_key_path:
        pool_public_key = _derive_public_key(private_key_path)
        script = _build_composed_prep_script(
            pool_public_key=pool_public_key,
            lima_service_user=lima_service_user,
            lima_version=lima_version,
            slice_base_image_url=slice_base_image_url,
            extra_prep_script=extra_prep_script,
        )
        logger.info(
            "Prepping box {} as {} (lima user {}, lima {})", server_address, ssh_user, lima_service_user, lima_version
        )
        _run_root_script_over_ssh(server_address, ssh_user, private_key_path, script, server.box_host_public_key)
    logger.info("Box {} prepped: qemu+lima installed, {} ready, OS image staged", server_address, lima_service_user)


def audit_box_against_tier(
    *,
    server_to_audit: BareMetalServer,
    env_name: str | None,
    private_key_path: Path,
) -> BoxTierAudit:
    """Report a box's REAL occupancy plus any cross-tier contamination on it.

    The DB-derived slot accounting in ``list`` counts only the querying env's own
    rows, so a slice belonging to another env -- and in particular another *tier* --
    is invisible to it. This SSHes the box and reports what is actually there, which
    is the only way to see a foreign-tier slice or a hand-added SSH key short of a
    bake refusing to run.
    """
    client = LimaSliceVpsClient(
        box_address=str(server_to_audit.public_address),
        box_ssh_user=server_to_audit.lima_service_user or "limahost",
        private_key_path=str(private_key_path),
        box_host_public_key=server_to_audit.box_host_public_key,
    )
    disk_names = client.list_disk_names()
    mdstat_text, proc_swaps_text = client.read_box_health_texts()
    return BoxTierAudit(
        server_id=str(server_to_audit.id),
        public_address=str(server_to_audit.public_address),
        slot_count=server_to_audit.slot_count,
        box_used_slots=count_slice_resource_names(disk_names),
        authorized_key_count=client.count_authorized_keys(),
        foreign_tier_slices=tuple(sorted(foreign_tier_slice_names(disk_names, env_name)))
        if env_name is not None
        else (),
        degraded_md_arrays=tuple(parse_degraded_md_arrays(mdstat_text)),
        raw_swap_devices=tuple(parse_raw_swap_devices(proc_swaps_text)),
    )


def audit_fleet_against_tier(
    *,
    capacities: Sequence[BareMetalServerCapacity],
    env_name: str | None,
    private_key_path: Path,
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
                    server_to_audit=server_to_audit, env_name=env_name, private_key_path=private_key_path
                )
            )
        # ``OSError`` too: auditing a box is not purely a remote call. It writes the
        # box's pinned host key to a known_hosts file and spawns ``ssh``, so a local
        # I/O failure on one box would otherwise cost every other box its verdict --
        # which is precisely what this command promises never to do. (The same
        # reason ``LimaSliceVpsClient._best_effort_destroy`` catches it.)
        except (LimaCommandError, BareMetalProvisioningError, OSError) as exc:
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
        "(foreign-tier slices, extra authorized SSH keys). The plain table counts only "
        "this env's own DB rows, so it undercounts a shared box. The pool SSH key is "
        "resolved from the activated tier's Vault entry (or $POOL_SSH_PRIVATE_KEY)."
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
    finally:
        conn.close()
    logger.info("\n{}", _format_capacity_table(capacities))
    if not is_occupancy_verified:
        return
    env_name = active_env_name_or_none()
    with pool_private_key_path(resolve_pool_private_key_pem()) as private_key_path:
        report = audit_fleet_against_tier(capacities=capacities, env_name=env_name, private_key_path=private_key_path)
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


@server.command(name="register")
@click.option("--ovh-service-name", required=True, help="OVH dedicated serviceName of the delivered box.")
@click.option("--plan-code", required=True, help="Catalog planCode the box was ordered as.")
@click.option("--region", required=True, help="OVH datacenter code (e.g. vin).")
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
@click.option("--lima-service-user", default="limahost", help="Non-root OS user that owns the box's lima VMs.")
@click.option("--ovh-order-id", default=None, help="OVH order id, if known.")
@click.option("--status", default=SERVER_STATUS_READY, help="Initial lifecycle status.")
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
    lima_service_user: str,
    ovh_order_id: str | None,
    status: str,
    database_url: str | None,
) -> None:
    """Record an already-provisioned bare-metal box in the pool DB."""
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
        lima_service_user=lima_service_user,
        ovh_order_id=ovh_order_id,
        status=status,
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

    Reads each ready box's real lima resources over SSH (with the tier's pool key) and
    destroys every slice stamped for a ``ci-*`` env that is older than the threshold --
    the crash backstop for release runs whose per-run env (and its DB) died before the
    normal teardown. See specs/remote-workspaces-in-ci.md.
    """
    if max_age_hours <= 0:
        raise click.UsageError("--max-age-hours must be positive")
    pool_private_key_pem = resolve_pool_private_key_pem()
    conn = psycopg2.connect(resolve_pool_database_url(database_url))
    try:
        ready_servers = [server for server in fetch_servers(conn) if str(server.status) == SERVER_STATUS_READY]
    finally:
        conn.close()
    box_reports: list[CiSliceSweepBoxReport] = []
    unreachable: list[str] = []
    with pool_private_key_path(pool_private_key_pem) as private_key_path:
        for server_row in ready_servers:
            if not server_row.public_address:
                unreachable.append(str(server_row.id))
                continue
            client = LimaSliceVpsClient(
                box_address=str(server_row.public_address),
                box_ssh_user=server_row.lima_service_user or "limahost",
                private_key_path=str(private_key_path),
                box_host_public_key=server_row.box_host_public_key,
            )
            try:
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
    report = CiSliceSweepReport(
        max_age_hours=max_age_hours,
        boxes=tuple(box_reports),
        unreachable_boxes=tuple(sorted(unreachable)),
    )
    emit_json(report.model_dump(mode="json"))
    if unreachable or any(box.failed for box in box_reports):
        raise click.ClickException(
            "the CI slice sweep could not fully clean the fleet (unreachable boxes or failed destroys above); "
            "stale slices will be retried on the next sweep"
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
    target_conn = psycopg2.connect(resolve_pool_database_url(database_url))
    try:
        for server_row in ready_servers:
            upsert_bare_metal_server(target_conn, server_row)
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
                for server_row in ready_servers
            ]
        }
    )


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
    lima_service_user: str,
    ovh_order_id: str | None,
    status: str,
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
        lima_service_user=lima_service_user,
        status=BareMetalServerStatus(status),
        created_at=now,
        updated_at=now,
    )


def compute_server_slice_sizing(server: BareMetalServer) -> dict[str, int]:
    """Compute the per-slice VM sizing for ``server`` from its stored inputs + specs.

    Returns ``{vcpus, memory_mib, disk_gib, advertised_memory_gb}`` -- identical for
    every slice on this box (so a single ``minds-admin pool create`` batch is one server).
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
    return {
        "advertised_memory_gb": server.memory_per_slice_gb,
        "vcpus": compute_slice_vcpus(server.cpu_threads, server.slot_count, server.cpu_overcommit_ratio),
        "memory_mib": compute_slice_memory_mib(server.memory_per_slice_gb),
        "disk_gib": compute_slice_disk_gib(server.disk_gb, server.slot_count),
    }


def slice_advertised_attributes(sizing: dict[str, int]) -> dict[str, Any]:
    """The lease attributes a slice advertises (so a lease matches a slice or a VPS identically)."""
    return {"memory_gb": sizing["advertised_memory_gb"], "cpus": sizing["vcpus"]}


# Provider instance name the slice bake targets; -S overrides under this key
# carry the box address + per-slice carve sizing into the create.
_SLICE_PROVIDER_INSTANCE: str = "imbue_cloud_slice"

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


def _build_slice_create_args(
    *,
    server: BareMetalServer,
    sizing: dict[str, int],
    region: str,
    env_name: str | None,
    pool_public_key: str,
    private_key_path: Path,
    ssh_user: str,
    port_range_start: int,
    port_range_end: int,
    default_workspace_template_cache_tag: str | None,
) -> list[str]:
    """Render the ``-S`` provider-config overrides that point one slice bake at this box.

    The carve knobs (vcpus / memory / disk) are computed per box so the leased
    host's actual size matches its advertised attributes; the box address + lima
    user + pool key + the owning env + the box's slot count + the full box port
    range are passed the same way. The on-box reservation lock makes concurrent
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
    prefix = f"providers.{_SLICE_PROVIDER_INSTANCE}"
    overrides = {
        "box_public_address": str(server.public_address),
        "box_ssh_user": ssh_user,
        "pool_private_key_path": str(private_key_path),
        "pool_authorized_public_key": pool_public_key,
        # The box's pinned sshd host key (same -S-with-spaces pattern as the pool key).
        "box_host_public_key": server.box_host_public_key,
        # Lease-region label (the app's region code, e.g. US-EAST-VA), NOT the
        # box's raw datacenter code -- so the connector's region-filtered lease
        # matches what the minds create form requests.
        "slice_region": region,
        "slice_vcpus": str(sizing["vcpus"]),
        "slice_memory_mib": str(sizing["memory_mib"]),
        "slice_disk_gib": str(sizing["disk_gib"]),
        # The box's total slot count: the on-box reservation refuses to carve once
        # the box already holds this many slices (the cross-env over-allocation guard).
        "slice_slot_count": str(server.slot_count),
        "slice_port_range_start": str(port_range_start),
        "slice_port_range_end": str(port_range_end),
    }
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


def _rollback_slice_vm(
    *, server: BareMetalServer, ssh_user: str, private_key_path: Path, host_id: str, env_name: str | None
) -> None:
    """Best-effort: destroy a carved slice VM whose later bake/bookkeeping failed, so it does not leak.

    Drives ``limactl delete`` / ``disk delete`` over SSH on the box (via the same
    SSH-backed client the carve uses) for the deterministic instance/disk names
    derived from ``host_id`` and the owning ``env_name``. Swallows + logs any
    failure -- the caller is already on a failure path -- so it never masks the
    original error.
    """
    client = LimaSliceVpsClient(
        box_address=str(server.public_address),
        box_ssh_user=ssh_user,
        private_key_path=str(private_key_path),
        box_host_public_key=server.box_host_public_key,
    )
    instance_id = VpsInstanceId(slice_lima_instance_name(HostId(host_id), env_name))
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
            f"UserKnownHostsFile={known_hosts_path}",
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
    pool_public_key: str,
    private_key_path: Path,
    database_url: str,
    port_range_start: int,
    port_range_end: int,
    is_env_converge_wait_skipped: bool,
    default_workspace_template_cache_tag: str | None,
    # The invocation's ephemeral bake namespace overrides (MNGR_HOST_DIR / MNGR_PREFIX),
    # so the inner ``mngr create`` never touches the operator's own mngr data root.
    extra_create_env: Mapping[str, str],
) -> SliceBakeOutcome:
    """Bake one slice (laptop-driven ``mngr create`` against the slice provider) + insert its pool row.

    Returns an outcome (never raises). ``bake_pool_host`` carves the VM (over
    SSH on the box, inside the slice provider) and bakes the shared container; the
    shared :func:`finalize_baked_pool_host` then hardens the container sshd and
    clears the baked git identity over the slice (direct-SSH) transport. Any
    failure once the VM exists rolls the VM back so it does not leak its box
    slot/ports (a ``mngr create`` failure is already rolled back by the provider).
    """
    ssh_user = server.lima_service_user or "limahost"
    host_name = f"slice-{uuid4().hex}"
    # The slice advertises the operator's lease attributes (e.g. repo_branch_or_tag,
    # so the minds fast-path lease matches) with the derived per-box size stamped on
    # top (authoritative). Mirrors how OVH pool hosts carry the operator's attributes.
    attributes = {**lease_attributes, **slice_advertised_attributes(sizing)}
    attributes_json = json.dumps(attributes)
    try:
        baked = bake_pool_host(
            provider_instance=_SLICE_PROVIDER_INSTANCE,
            host_name=host_name,
            attributes=attributes,
            workspace_dir=workspace_dir,
            extra_create_args=_build_slice_create_args(
                server=server,
                sizing=sizing,
                region=region,
                env_name=env_name,
                pool_public_key=pool_public_key,
                private_key_path=private_key_path,
                ssh_user=ssh_user,
                port_range_start=port_range_start,
                port_range_end=port_range_end,
                default_workspace_template_cache_tag=default_workspace_template_cache_tag,
            ),
            extra_create_env=extra_create_env,
            mngr_create_timeout_seconds=_SLICE_MNGR_CREATE_TIMEOUT_SECONDS,
        )
        # The VM now exists; any failure in the post-create steps or the insert must
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
            host_id_obj = HostId(baked.host_id)
            if not baked.outer_host_public_key or not baked.container_host_public_key:
                raise BareMetalProvisioningError(
                    f"baked slice {host_name} did not surface its sshd host public keys "
                    "(needs a slice provider that emits them in `mngr create --format json`); cannot insert pool row"
                )
            values = build_slice_pool_host_insert_values(
                row_id=str(uuid4()),
                box_public_address=str(server.public_address),
                agent_id=baked.agent_id,
                host_id=baked.host_id,
                host_name=host_name,
                vm_ssh_host_port=baked.outer_ssh_port,
                container_ssh_host_port=baked.ssh_port,
                attributes_json=attributes_json,
                region=region,
                bare_metal_server_id=str(server.id),
                lima_instance_name=slice_lima_instance_name(host_id_obj, env_name),
                lima_disk_name=slice_lima_disk_name(host_id_obj, env_name),
                outer_host_public_key=baked.outer_host_public_key,
                container_host_public_key=baked.container_host_public_key,
            )
            conn = psycopg2.connect(database_url)
            try:
                insert_slice_pool_host(conn, values)
            finally:
                conn.close()
        except (PoolBakeError, BareMetalProvisioningError, MngrError, psycopg2.Error, OSError):
            _rollback_slice_vm(
                server=server,
                ssh_user=ssh_user,
                private_key_path=private_key_path,
                host_id=baked.host_id,
                env_name=env_name,
            )
            raise
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
    *, server: BareMetalServer, private_key_path: Path, database_url: str, env_name: str | None
) -> None:
    """Delete THIS env's slice VMs AND data disks on the box that have no pool_hosts row.

    Reconciles the box's lima instances and disks against the DB, scoped to slices
    stamped for ``env_name``: any such resource with no row (any status) is an orphan
    -- a ``mngr create`` killed by its own timeout after carving but before the row
    insert (the provider's rollback never ran), or a disk left behind when a rollback
    ``limactl delete`` could not unlock it (so the VM is gone but its disk leaked,
    permanently holding the box slot). Other envs' slices and legacy un-stamped slices
    are never touched, so envs can safely share a box. Disks are reconciled
    independently of instances so a disk that outlived its VM is still reaped.
    Best-effort: logs and continues on any error so it never fails the bake. Assumes no
    other bake invocation OF THIS ENV is concurrently mid-carve against the box (an
    in-flight resource not yet inserted would otherwise look orphaned).

    A bake with no owning env (``env_name`` is None) produces only legacy un-stamped
    names, which must be left untouched, so reaping is skipped entirely.
    """
    if env_name is None:
        logger.info("Orphan reap skipped on {}: no owning env to scope to", server.public_address)
        return
    ssh_user = server.lima_service_user or "limahost"
    client = LimaSliceVpsClient(
        box_address=str(server.public_address),
        box_ssh_user=ssh_user,
        private_key_path=str(private_key_path),
        box_host_public_key=server.box_host_public_key,
    )

    # Reap orphan VM instances.
    try:
        box_instance_names = client.list_instance_names()
    except (MngrError, OSError) as exc:
        logger.warning("Orphan reap skipped: could not list slice VMs on {}: {}", server.public_address, exc)
        box_instance_names = None
    if box_instance_names is not None:
        conn = psycopg2.connect(database_url)
        try:
            tracked_instance_names = fetch_slice_instance_names_for_server(conn, server.id)
        finally:
            conn.close()
        instance_orphans = compute_orphan_slice_instance_names(box_instance_names, tracked_instance_names, env_name)
        if not instance_orphans:
            logger.info("Orphan reap: no untracked slice VMs on {}", server.public_address)
        else:
            logger.info(
                "Orphan reap: deleting {} untracked slice VM(s) on {}: {}",
                len(instance_orphans),
                server.public_address,
                sorted(instance_orphans),
            )
            for instance_name in sorted(instance_orphans):
                try:
                    client.destroy_instance(VpsInstanceId(instance_name))
                except (MngrError, OSError) as exc:
                    logger.warning(
                        "Orphan reap: failed to delete VM {} on {}: {}", instance_name, server.public_address, exc
                    )

    # Reap orphan data disks (a disk can outlive its instance when the rollback delete
    # could not unlock it). Done after the VM reap so a just-deleted VM's disk -- which
    # destroy_instance already removes -- is no longer present to look orphaned.
    try:
        box_disk_names = client.list_disk_names()
    except (MngrError, OSError) as exc:
        logger.warning("Orphan disk reap skipped: could not list slice disks on {}: {}", server.public_address, exc)
        return
    conn = psycopg2.connect(database_url)
    try:
        tracked_disk_names = fetch_slice_disk_names_for_server(conn, server.id)
    finally:
        conn.close()
    disk_orphans = compute_orphan_slice_disk_names(box_disk_names, tracked_disk_names, env_name)
    if not disk_orphans:
        logger.info("Orphan reap: no untracked slice disks on {}", server.public_address)
        return
    logger.info(
        "Orphan reap: deleting {} untracked slice disk(s) on {}: {}",
        len(disk_orphans),
        server.public_address,
        sorted(disk_orphans),
    )
    for disk_name in sorted(disk_orphans):
        try:
            client.destroy_disk(disk_name)
        except (MngrError, OSError) as exc:
            logger.warning("Orphan reap: failed to delete disk {} on {}: {}", disk_name, server.public_address, exc)


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
    private_key_path: Path | None,
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
            private_key_path=private_key_path,
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
    private_key_path: Path | None,
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
        if not target.lima_instance_name or not target.box_public_address:
            return PoolHostDestroyOutcome(
                pool_host_id=pool_host_id,
                status=PoolHostDestroyOutcomeStatus.FAILED,
                detail=(
                    "cannot locate the VM to destroy (missing lima_instance_name or the box record is gone); "
                    "pass --drop-row-only to drop the row without teardown"
                ),
            )
        if private_key_path is None:
            return PoolHostDestroyOutcome(
                pool_host_id=pool_host_id,
                status=PoolHostDestroyOutcomeStatus.FAILED,
                detail="no pool management key available for the box SSH (POOL_SSH_PRIVATE_KEY)",
            )
        client = LimaSliceVpsClient(
            box_address=target.box_public_address,
            box_ssh_user=target.lima_service_user or "limahost",
            private_key_path=str(private_key_path),
            box_host_public_key=target.box_host_public_key,
        )
        try:
            client.destroy_instance(VpsInstanceId(target.lima_instance_name))
        except (MngrError, OSError) as exc:
            logger.warning("Failed to tear down slice {}: {}", target.lima_instance_name, exc)
            return PoolHostDestroyOutcome(
                pool_host_id=pool_host_id,
                status=PoolHostDestroyOutcomeStatus.FAILED,
                detail=f"VM teardown failed ({target.lima_instance_name} on {target.box_public_address}): {exc}",
            )

    # The VM is gone (or the operator asked for a row-only drop); drop the row. A fresh
    # connection on purpose: the SSH teardown above can take minutes, and a Neon
    # connection held idle across it may be dropped server-side by the time we delete.
    conn_for_delete = psycopg2.connect(database_url)
    try:
        delete_pool_host_row(conn_for_delete, pool_host_id)
    finally:
        conn_for_delete.close()
    logger.info("Destroyed pool host {} ({})", pool_host_id, target.lima_instance_name or "no VM")
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
    pool_private_key_pem: str | None,
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
    # A row-only drop never SSHes a box, so it must not require a pool key.
    if is_row_drop_only or pool_private_key_pem is None:
        key_path_context: AbstractContextManager[Path | None] = nullcontext(None)
    else:
        key_path_context = pool_private_key_path(pool_private_key_pem)
    with key_path_context as private_key_path:
        outcomes = run_outcome_workers_in_bounded_threads(
            worker=_destroy_one_pool_host,
            worker_kwargs_list=[
                dict(
                    pool_host_id=pool_host_id,
                    database_url=database_url,
                    private_key_path=private_key_path,
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
    database_url: str, *, pool_private_key_pem: str, max_concurrency: int
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
    The caller supplies the tier's pool management key PEM.
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
        pool_private_key_pem=pool_private_key_pem,
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
    authorized_key_count: int,
) -> None:
    """Refuse to bake unless this box belongs solely to the activated env's tier.

    Tier isolation is a stated invariant (``apps/minds/docs/deploy/environments.md``:
    "There is zero cross-tier reach"), but nothing used to enforce it at the moment
    it matters. Two independent ways a box drifts across tiers, both caught here
    before a single slice is carved:

    * a **foreign-tier slice** already on the box -- the box is then reachable by
      both tiers' pool keys, which is the "zero cross-tier reach" boundary itself:
      each tier's operators and connector gain ``limactl``, and so root, over the
      other's workspaces (and neither tier's env-scoped reap reclaims the other's
      leaks); and
    * a **foreign key** in the lima service user's ``authorized_keys`` -- prep
      writes that file with a single-key overwrite, so a second key can only have
      been added out of band, and it hands another tier SSH access to this box (and
      thus, via ``limactl``, to every workspace running on it).

    The key check needs no comparison against our own public key: reaching this
    point means we already authenticated with *this* tier's pool key, so if exactly
    one key is authorized, that key is necessarily ours.

    ``env_name`` is None only for a legacy un-stamped bake, whose tier is
    unknowable; the slice check is skipped in that case, but the key check -- which
    does not depend on our tier -- still applies.
    """
    if authorized_key_count != EXPECTED_AUTHORIZED_KEY_COUNT:
        # Both directions refuse the bake, but they mean opposite things and have
        # opposite remedies, so say which one happened. Re-prepping is safe when the
        # file is empty (there is no other key to destroy) and destructive when it
        # holds someone else's key: prep writes authorized_keys with a single-key
        # OVERWRITE, so it would revoke that holder's access to a box whose slices --
        # which this branch raises before ever looking at -- are still running.
        if authorized_key_count < EXPECTED_AUTHORIZED_KEY_COUNT:
            reason = (
                "that file is empty, so the box was never prepped (or its authorized_keys was clobbered). "
                f"Run `just prep-server {server.id}` (idempotent) to write this tier's key."
            )
        else:
            reason = (
                "`minds-admin server prep` writes that file with a single-key overwrite, so the extra key(s) were "
                "added out of band and give another tier SSH access to this box. Inspect them with "
                "`ssh-keygen -lf ~/.ssh/authorized_keys` on the box. Do NOT re-prep before checking the box "
                "for another tier's slices (`just audit-boxes`): prep overwrites authorized_keys, which would "
                "cut the other key's owner off from slices that are still running here."
            )
        raise click.UsageError(
            f"server {server.id} ({server.public_address}) authorizes {authorized_key_count} SSH keys for "
            f"user {server.lima_service_user or 'limahost'}, expected exactly "
            f"{EXPECTED_AUTHORIZED_KEY_COUNT} (this tier's pool key). {reason}"
        )
    if env_name is None:
        return
    foreign_names = foreign_tier_slice_names(box_disk_names, env_name)
    if is_box_exclusive_to_tier(
        authorized_key_count=authorized_key_count, foreign_tier_slice_count=len(foreign_names)
    ):
        return
    foreign_list = ", ".join(sorted(foreign_names))
    raise click.UsageError(
        f"server {server.id} ({server.public_address}) already carries slices from another tier, so it "
        f"cannot also host '{env_name}' (tier '{tier_for_env_name(env_name)}') slices: {foreign_list}. "
        "Tiers are isolated by construction -- each has its own pool keypair, and there is meant to be zero "
        "cross-tier reach -- so a box serving both is a box each tier's operators can SSH (and via limactl "
        "control) the other's workspaces on, and neither tier's reap will ever reclaim the other's slices. "
        "Retire the foreign slices from their OWN env (`just destroy-pool-hosts <row-id>` with that env "
        "activated) or bake onto a box belonging to this tier."
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
    pool_private_key_pem: str,
    is_dry_run: bool,
    is_env_converge_wait_skipped: bool,
    max_concurrency: int,
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
    sizing = compute_server_slice_sizing(server)

    ssh_user = server.lima_service_user or "limahost"
    with pool_private_key_path(pool_private_key_pem) as private_key_path:
        # Free slots come from the box's REAL occupancy (every env's slices plus any
        # legacy un-stamped ones), NOT this env's DB row count -- so independent envs
        # sharing the box cannot collectively over-subscribe it. This is a fast
        # pre-check; the authoritative guard is the per-slice on-box reservation lock.
        occupancy_client = LimaSliceVpsClient(
            box_address=str(server.public_address),
            box_ssh_user=ssh_user,
            private_key_path=str(private_key_path),
            box_host_public_key=server.box_host_public_key,
        )
        box_disk_names = occupancy_client.list_disk_names()
        # Enforce tier isolation before anything is carved: a box shared across tiers
        # is reachable by both tiers' pool keys, so each tier's operators and connector
        # get limactl -- and so root -- over the other's workspaces.
        assert_box_is_exclusive_to_tier(
            server=server,
            env_name=env_name,
            box_disk_names=box_disk_names,
            authorized_key_count=occupancy_client.count_authorized_keys(),
        )
        box_used_slots = count_slice_resource_names(box_disk_names)
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

            pool_public_key = _derive_public_key(private_key_path)
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
                LimaBoxImageCache(
                    slice_client=occupancy_client,
                    cache_dir=box_default_workspace_template_cache_dir(ssh_user),
                ),
                default_workspace_template_cache_tag,
            )
            # One worker per slice, capped at ``max_concurrency`` at once by the shared
            # fan-out: each bake blocks on the semaphore before its ``mngr create``, so
            # the box is never contended by more than K simultaneous carves+builds
            # (which would push each create past its timeout). Every bake is handed the
            # FULL box port range: the on-box reservation lock makes concurrent carves
            # (this env's and other envs') pick distinct free ports from it.
            bake_worker_kwargs = dict(
                server=server,
                sizing=sizing,
                lease_attributes=lease_attributes,
                region=region,
                env_name=env_name,
                workspace_dir=workspace_dir,
                pool_public_key=pool_public_key,
                private_key_path=private_key_path,
                database_url=database_url,
                port_range_start=DEFAULT_SLICE_PORT_RANGE_START,
                port_range_end=DEFAULT_SLICE_PORT_RANGE_END,
                is_env_converge_wait_skipped=is_env_converge_wait_skipped,
                default_workspace_template_cache_tag=default_workspace_template_cache_tag,
                extra_create_env=bake_namespace.to_subprocess_env(),
            )
            logger.info("Baking {} slice(s) on {} ({} at a time)", count, server.public_address, max_concurrency)

            # ``signal.signal`` only works on the main thread; the admin CLI always runs
            # allocate_slices there, but guard so an off-main-thread caller falls back to
            # the finally reap rather than crashing on install.
            is_main_thread = threading.current_thread() is threading.main_thread()
            # Set by the join-interruption handler so the per-slice retry loops return
            # their (kill-induced) failures instead of spawning replacement bakes.
            bake_termination_event = threading.Event()
            previous_sigterm = (
                signal.signal(signal.SIGTERM, _raise_on_bake_termination_signal) if is_main_thread else None
            )
            previous_sigint = (
                signal.signal(signal.SIGINT, _raise_on_bake_termination_signal) if is_main_thread else None
            )
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
    pool_public_key: str,
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
    ssh_user = server.lima_service_user or "limahost"
    host_name = f"slice-{uuid4().hex}"
    attributes = slice_advertised_attributes(sizing)
    try:
        baked = bake_pool_host(
            provider_instance=_SLICE_PROVIDER_INSTANCE,
            host_name=host_name,
            attributes=attributes,
            workspace_dir=workspace_dir,
            extra_create_args=_build_slice_create_args(
                server=server,
                sizing=sizing,
                region=region,
                env_name=CI_WARM_PSEUDO_ENV_NAME,
                pool_public_key=pool_public_key,
                private_key_path=private_key_path,
                ssh_user=ssh_user,
                port_range_start=DEFAULT_SLICE_PORT_RANGE_START,
                port_range_end=DEFAULT_SLICE_PORT_RANGE_END,
                default_workspace_template_cache_tag=default_workspace_template_cache_tag,
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


def _reap_ci_warm_slice_resources(client: LimaSliceVpsClient) -> None:
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
    pool_private_key_pem: str,
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
    sizing = compute_server_slice_sizing(server)
    # The create's provider config wants the lease-region label; derive it from the
    # box's datacenter code so the verb needs no --region of its own (the label is
    # irrelevant for a slice that never becomes a pool row).
    region = US_REGION_BY_OVH_DATACENTER_CODE.get(server.region or "")
    if region is None:
        raise click.UsageError(
            f"server {server.id} is in datacenter {server.region!r}, which maps to no known lease region"
        )

    ssh_user = server.lima_service_user or "limahost"
    with pool_private_key_path(pool_private_key_pem) as private_key_path:
        client = LimaSliceVpsClient(
            box_address=str(server.public_address),
            box_ssh_user=ssh_user,
            private_key_path=str(private_key_path),
            box_host_public_key=server.box_host_public_key,
        )
        box_disk_names = client.list_disk_names()
        assert_box_is_exclusive_to_tier(
            server=server,
            env_name=CI_WARM_PSEUDO_ENV_NAME,
            box_disk_names=box_disk_names,
            authorized_key_count=client.count_authorized_keys(),
        )
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
            cache = LimaBoxImageCache(
                slice_client=client, cache_dir=box_default_workspace_template_cache_dir(ssh_user)
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
            pool_public_key = _derive_public_key(private_key_path)
            try:
                outcome = _run_bake_attempts(
                    lambda: _warm_bake_one_slice(
                        server=server,
                        sizing=sizing,
                        region=region,
                        workspace_dir=workspace_dir,
                        pool_public_key=pool_public_key,
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
    """Advance a server's lifecycle status (resumable order->delivered->installing->ready)."""
    validated = BareMetalServerStatus(status)
    conn = psycopg2.connect(resolve_pool_database_url(database_url))
    try:
        update_server(conn, BareMetalServerDbId(server_id), status=str(validated))
    finally:
        conn.close()
    logger.info("Set server {} status to {}", server_id, validated)


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
    server_address: str, ssh_user: str, private_key_path: Path, box_host_public_key: str
) -> bool | None:
    """One SSH-readiness probe: True once a login succeeds, else None (for poll_for_value)."""
    cg = ConcurrencyGroup(name="ssh-ready")
    with _box_ssh_host_key_options(server_address, box_host_public_key) as host_key_opts:
        with cg:
            result = cg.run_process_to_completion(
                command=[
                    "ssh",
                    "-i",
                    str(private_key_path),
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
    server_address: str,
    ssh_user: str,
    private_key_path: Path,
    timeout_seconds: float,
    box_host_public_key: str,
) -> None:
    """Poll until the box accepts an SSH login (it reboots into the freshly-installed OS). Raises on timeout."""
    with log_span("Waiting for SSH on {} as {}", server_address, ssh_user):
        is_ready, _polls, _elapsed = poll_for_value(
            lambda: _probe_ssh_ready(server_address, ssh_user, private_key_path, box_host_public_key),
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
@click.option("--database-url", default=None, help="Pool DSN (else resolved from env/activated minds env).")
def order(
    plan_code: str,
    region: str,
    memory_gb: int,
    storage: str,
    memory_per_slice_gb: int,
    cpu_overcommit: float,
    option_codes: tuple[str, ...],
    yes: bool,
    is_dry_run: bool,
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

    cart_id, preview, _option_codes = build_and_assign_eco_cart(
        client,
        plan_code=plan_code,
        datacenter=region,
        memory_gb=memory_gb,
        storage_short=storage,
        explicit_option_codes=option_codes,
    )
    write_human_line(
        f"About to order {plan_code} in {region}: {memory_gb}GB RAM, {storage}, {cpu_cores}c/{cpu_threads}t, "
        f"{disk_gb}GB usable disk ({raid_level}) -> {slot_count} slices of {memory_per_slice_gb}GB.\n"
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
        lima_service_user=None,
        status=BareMetalServerStatus(SERVER_STATUS_ORDERED),
        created_at=now,
        updated_at=now,
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
@click.option("--lima-service-user", default="limahost", help="Dedicated non-root user to create for the lima VMs.")
@click.option("--lima-version", default=DEFAULT_LIMA_VERSION, help="Lima release to install on the box.")
@click.option(
    "--slice-base-image-url",
    default=DEFAULT_IMAGE_URL_X86_64,
    show_default=True,
    help="Guest OS image to stage on the box once (slices boot from this via file://).",
)
@click.option(
    "--os-template",
    default=DEFAULT_REINSTALL_OS_TEMPLATE,
    show_default=True,
    help="OVH OS template to reinstall onto the box.",
)
@click.option("--ssh-ready-timeout", type=float, default=900.0, show_default=True, help="Seconds to wait for SSH.")
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
    lima_service_user: str,
    lima_version: str,
    slice_base_image_url: str,
    os_template: str,
    ssh_ready_timeout: float,
    database_url: str | None,
    extra_prep_script: Path | None,
) -> None:
    """Provision a delivered box to 'ready': reinstall our OS (destructive), run the composed prep.

    Runs the same composed prep as ``server prep`` (qemu/lima/tooling + image
    staging + the observability collector when the tier has a boxes ingest
    credential, verified active). Fail-closed: a failed collector install or
    verification fails the prep, and the box is NOT marked 'ready'. The OVH
    credentials, pool DSN, and pool SSH key resolve from the activated tier
    (OVH_* / --database-url / POOL_SSH_PRIVATE_KEY overrides preserved).

    Resumable via status: reinstall runs only from 'delivered'; re-running from 'installing' resumes at prep.
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

    client = build_ovh_client(resolve_ovh_config())
    with pool_private_key_path(resolve_pool_private_key_pem()) as private_key_path:
        pool_public_key = _derive_public_key(private_key_path)
        # Compose the full prep (base + collector + extra + verification) BEFORE the
        # destructive reinstall, so a Vault failure resolving the tier's observability
        # credential aborts up front instead of stranding a half-reinstalled box.
        script = _build_composed_prep_script(
            pool_public_key=pool_public_key,
            lima_service_user=lima_service_user,
            lima_version=lima_version,
            slice_base_image_url=slice_base_image_url,
            extra_prep_script=extra_prep_script,
        )
        # Reinstall only from 'delivered'; re-running from 'installing' assumes the reinstall completed and
        # resumes at SSH-wait + prep. No DB connection is held across the (long) reinstall/prep waits.
        if str(server.status) == SERVER_STATUS_DELIVERED:
            reinstall = start_os_reinstall(
                client,
                service_name=service_name,
                ssh_public_key=pool_public_key,
                os_template=os_template,
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
        box_host_public_key = _fetch_server_or_raise(dsn, server_id).box_host_public_key
        if not box_host_public_key:
            raise BareMetalProvisioningError(
                f"server {server_id} reached '{SERVER_STATUS_INSTALLING}' without a recorded box host key; "
                "cannot SSH the box with strict host-key checking"
            )
        _wait_for_ssh_ready(address, ssh_user, private_key_path, ssh_ready_timeout, box_host_public_key)
        logger.info("Prepping delivered box {} ({})", server_id, address)
        _run_root_script_over_ssh(address, ssh_user, private_key_path, script, box_host_public_key)

    _update_server_fields(dsn, server_id, lima_service_user=lima_service_user, status=SERVER_STATUS_READY)
    write_human_line(
        f"Server {server_id} is READY: {service_name} ({address}), "
        f"{server.slot_count} slots. Bake a slice with `minds-admin pool create`."
    )
