"""The gen-1 -> gen-2 migration drivers: preflight, migrate, rollback, repave (``minds-admin cutover``).

Workspaces move one at a time: ``migrate`` live-harvests a gen-1 workspace,
runs the product's own admin stop (a verified three-object artifact), parks
the row, transplants the artifact's data disk onto an operator-named gen-2
target box, replays the container, and re-leases the row at the new
coordinates; ``rollback`` puts a migrated workspace back on gen-1 through the
product's own restore. Boxes are driven over SSH (the slice service user
through the generation's slice client, VM root through an ``OuterHost`` pinned
to the workspace's own host key, and root over the management dial for the
disk transplant), and progress lands in the state dir so a re-run resumes per
workspace. Design: blueprint/slice-fleet-cutover/phase-5.5-incremental-rollout.md
(superseding the flag-day stages of phase-4-cutover-tooling.md). One-time
tooling, deleted in phase 6.
"""

import base64
import json
import shlex
import shutil
import tempfile
from collections.abc import Iterator
from collections.abc import Mapping
from collections.abc import Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import AbstractSet
from typing import Any
from typing import Final

import boto3
import click
import pluggy
import psycopg2
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError
from botocore.exceptions import ClientError
from loguru import logger
from pydantic import AnyUrl
from pydantic import Field
from pydantic import SecretStr
from tabulate import tabulate

from imbue.concurrency_group.errors import ProcessTimeoutError
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.imbue_common.secret_wrapping import unwrap_dek
from imbue.minds_admin.bake.bake_source import BakeSource
from imbue.minds_admin.bake.bake_source import DEFAULT_WORKSPACE_TEMPLATE_REPO_URL
from imbue.minds_admin.bake.bake_source import merge_bake_identity_attributes
from imbue.minds_admin.bake.bake_source import resolved_bake_source
from imbue.minds_admin.bake.content_tag import DEFAULT_WORKSPACE_TEMPLATE_IMAGE_REPOSITORY
from imbue.minds_admin.cli._tier_secrets import WorkspaceStorageConfig
from imbue.minds_admin.cli.server import DEFAULT_SETUP_SSH_READY_TIMEOUT_SECONDS
from imbue.minds_admin.cli.server import allocate_slices
from imbue.minds_admin.cli.server import compute_server_slice_sizing
from imbue.minds_admin.cli.server import destroy_pool_hosts_in_parallel
from imbue.minds_admin.cli.server import estimate_gen2_machine_capacity
from imbue.minds_admin.cli.server import run_outcome_workers_in_bounded_threads
from imbue.minds_admin.cli.server import run_root_script_over_ssh
from imbue.minds_admin.cli.server import setup_server_to_ready
from imbue.minds_admin.slices.bare_metal_db import destroy_eligible_pool_host_statuses
from imbue.minds_admin.slices.bare_metal_db import fetch_server_by_id
from imbue.minds_admin.slices.bare_metal_db import update_server
from imbue.minds_admin.slices.bare_metal_prep import DEFAULT_LIMA_VERSION
from imbue.minds_admin.slices.bare_metal_prep import build_storage_partition_size_bytes_command
from imbue.minds_admin.slices.box_access import resolve_server_management_dial
from imbue.minds_admin.slices.cutover_db import CutoverPoolRow
from imbue.minds_admin.slices.cutover_db import fetch_gen1_pool_rows_for_user
from imbue.minds_admin.slices.cutover_db import fetch_gen1_servers
from imbue.minds_admin.slices.cutover_db import fetch_pool_row
from imbue.minds_admin.slices.cutover_db import fetch_pool_rows_on_server
from imbue.minds_admin.slices.cutover_db import fetch_unplaced_gen1_pool_rows
from imbue.minds_admin.slices.cutover_db import finish_restore_pool_host
from imbue.minds_admin.slices.cutover_db import park_pool_host
from imbue.minds_admin.slices.cutover_db import rollback_park_pool_host
from imbue.minds_admin.slices.cutover_db import rollback_restore_artifact
from imbue.minds_admin.slices.cutover_scripts import ROOT_AUTHORIZED_KEYS_PATH
from imbue.minds_admin.slices.cutover_scripts import SSHD_HOST_KEY_PATH
from imbue.minds_admin.slices.cutover_scripts import TRANSPLANT_DONE_MARKER
from imbue.minds_admin.slices.cutover_scripts import authorized_keys_without
from imbue.minds_admin.slices.cutover_scripts import build_banner_wait_command
from imbue.minds_admin.slices.cutover_scripts import build_container_id_command
from imbue.minds_admin.slices.cutover_scripts import build_container_key_harvest_command
from imbue.minds_admin.slices.cutover_scripts import build_container_running_command
from imbue.minds_admin.slices.cutover_scripts import build_disk_materialize_command
from imbue.minds_admin.slices.cutover_scripts import build_docker_create_args
from imbue.minds_admin.slices.cutover_scripts import build_docker_inspect_command
from imbue.minds_admin.slices.cutover_scripts import build_gen1_datadisk_info_command
from imbue.minds_admin.slices.cutover_scripts import build_git_describe_command
from imbue.minds_admin.slices.cutover_scripts import build_image_load_command
from imbue.minds_admin.slices.cutover_scripts import build_image_publish_command
from imbue.minds_admin.slices.cutover_scripts import build_replayed_container_files
from imbue.minds_admin.slices.cutover_scripts import build_stage_replayed_container_files_command
from imbue.minds_admin.slices.cutover_scripts import build_supervisorctl_status_command
from imbue.minds_admin.slices.cutover_scripts import build_system_interface_probe_command
from imbue.minds_admin.slices.cutover_scripts import build_transplant_clear_command
from imbue.minds_admin.slices.cutover_scripts import build_transplant_rescue_command
from imbue.minds_admin.slices.cutover_scripts import build_unit_enable_command
from imbue.minds_admin.slices.cutover_scripts import build_vm_key_harvest_command
from imbue.minds_admin.slices.cutover_scripts import container_name_from_inspect
from imbue.minds_admin.slices.cutover_scripts import cutover_image_object_key
from imbue.minds_admin.slices.cutover_scripts import cutover_transplant_dir
from imbue.minds_admin.slices.cutover_scripts import extract_template_replay_inputs
from imbue.minds_admin.slices.cutover_scripts import migration_rollback_key_prefix
from imbue.minds_admin.slices.cutover_scripts import parse_docker_inspect
from imbue.minds_admin.slices.cutover_scripts import parse_marked_files
from imbue.minds_admin.slices.cutover_scripts import parse_qemu_img_info
from imbue.minds_admin.slices.cutover_scripts import parse_supervisorctl_unhealthy
from imbue.minds_admin.slices.cutover_scripts import render_gen2_disk_transplant_script
from imbue.minds_admin.slices.cutover_scripts import replayed_container_dirs
from imbue.minds_admin.slices.cutover_scripts import staged_container_dir_path
from imbue.minds_admin.slices.cutover_state import CutoverStateStore
from imbue.minds_admin.slices.cutover_types import BoxOutcome
from imbue.minds_admin.slices.cutover_types import BoxPreflight
from imbue.minds_admin.slices.cutover_types import CutoverBoxStage
from imbue.minds_admin.slices.cutover_types import CutoverBoxState
from imbue.minds_admin.slices.cutover_types import CutoverError
from imbue.minds_admin.slices.cutover_types import CutoverStage
from imbue.minds_admin.slices.cutover_types import CutoverWorkspaceState
from imbue.minds_admin.slices.cutover_types import HarvestedKeys
from imbue.minds_admin.slices.cutover_types import PreflightReport
from imbue.minds_admin.slices.cutover_types import RowClassification
from imbue.minds_admin.slices.cutover_types import RowVerdict
from imbue.minds_admin.slices.cutover_types import SavedProductArtifact
from imbue.minds_admin.slices.cutover_types import StageReport
from imbue.minds_admin.slices.cutover_types import TemplateReplayInputs
from imbue.minds_admin.slices.cutover_types import WorkspaceOutcome
from imbue.minds_admin.slices.cutover_types import WorkspacePreflight
from imbue.minds_admin.slices.cutover_types import classify_pool_row
from imbue.minds_admin.slices.cutover_types import classify_unplaced_gen1_row
from imbue.minds_admin.slices.cutover_types import gen1_data_disk_size_error_or_none
from imbue.minds_admin.slices.cutover_types import parse_version_tag
from imbue.minds_admin.slices.cutover_types import version_tag_error_or_none
from imbue.minds_admin.slices.operator_identity import ManagementIdentityResolver
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr.hosts.outer_host import OuterHost
from imbue.mngr.interfaces.data_types import PyinfraConnector
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.primitives import HostId
from imbue.mngr.providers.ssh_utils import add_host_to_known_hosts
from imbue.mngr.providers.ssh_utils import create_pyinfra_host
from imbue.mngr.providers.ssh_utils import read_served_host_key_or_none
from imbue.mngr.utils.polling import poll_for_value
from imbue.mngr_imbue_cloud.connector.client import ImbueCloudConnectorClient
from imbue.mngr_imbue_cloud.data_types import BareMetalServer
from imbue.mngr_imbue_cloud.data_types import PoolHostDestroyOutcome
from imbue.mngr_imbue_cloud.errors import BareMetalProvisioningError
from imbue.mngr_imbue_cloud.interfaces import SliceVmClientInterface
from imbue.mngr_imbue_cloud.primitives import BareMetalServerDbId
from imbue.mngr_imbue_cloud.primitives import PoolHostDestroyOutcomeStatus
from imbue.mngr_imbue_cloud.primitives import SERVER_STATUS_DELIVERED
from imbue.mngr_imbue_cloud.primitives import SERVER_STATUS_DRAINING
from imbue.mngr_imbue_cloud.primitives import SERVER_STATUS_READY
from imbue.mngr_imbue_cloud.primitives import US_REGION_BY_OVH_DATACENTER_CODE
from imbue.mngr_imbue_cloud.providers.slice_provider import wait_for_guest_cloud_init_to_finish
from imbue.mngr_imbue_cloud.slices.bare_metal import DEFAULT_SLICE_CPU_OVERCOMMIT_RATIO
from imbue.mngr_imbue_cloud.slices.bare_metal import box_image_cache_dir_for_generation
from imbue.mngr_imbue_cloud.slices.bare_metal import box_service_user
from imbue.mngr_imbue_cloud.slices.bare_metal import count_slice_resource_names
from imbue.mngr_imbue_cloud.slices.box_image_cache import box_image_tar_name
from imbue.mngr_imbue_cloud.slices.gen2_scripts.box_commands import build_qemu_slice_env_file
from imbue.mngr_imbue_cloud.slices.gen2_scripts.errors import Gen2ScriptError
from imbue.mngr_imbue_cloud.slices.gen2_scripts.errors import InvalidMachineSizeError
from imbue.mngr_imbue_cloud.slices.gen2_scripts.guest import Gen2SliceCidata
from imbue.mngr_imbue_cloud.slices.gen2_scripts.guest import build_qemu_slice_meta_data
from imbue.mngr_imbue_cloud.slices.gen2_scripts.guest import build_qemu_slice_network_config
from imbue.mngr_imbue_cloud.slices.gen2_scripts.guest import build_qemu_slice_user_data
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import FIRST_QEMU_BOX_GENERATION
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_CONTAINER_SSH_PORT_PLACEHOLDER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_NO_DISK_MARKER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_NO_UNITS_MARKER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_SLICE_SERVICE_USER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_STORAGE_ROOT
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_VM_SSH_PORT_PLACEHOLDER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import DEFAULT_MACHINE_UNITS
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import GEN2_BOOT_DISK_GIB
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import compute_box_total_units
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import compute_box_unit_budget_mib
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import compute_gen2_disk_budget_gib
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import compute_machine_guest_memory_mib
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import compute_machine_vcpus
from imbue.mngr_imbue_cloud.slices.gen2_scripts.ssh_ca import is_same_ssh_public_key
from imbue.mngr_imbue_cloud.slices.gen2_scripts.transfer import DATADISK_OBJECT
from imbue.mngr_imbue_cloud.slices.gen2_scripts.transfer import DISK_OBJECT
from imbue.mngr_imbue_cloud.slices.gen2_scripts.transfer import META_OBJECT
from imbue.mngr_imbue_cloud.slices.gen2_scripts.transfer import RESTORE_NO_PORTS_MARKER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.transfer import RESTORE_NO_SPACE_MARKER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.transfer import TRANSFER_DIR_ROOT
from imbue.mngr_imbue_cloud.slices.gen2_scripts.transfer import TransferEnv
from imbue.mngr_imbue_cloud.slices.gen2_scripts.transfer import parse_gen2_restore_reserved_line
from imbue.mngr_imbue_cloud.slices.gen2_scripts.transfer import render_gen2_restore_reserve_script
from imbue.mngr_imbue_cloud.slices.gen2_scripts.transfer import render_transfer_env
from imbue.mngr_imbue_cloud.slices.qemu_slice import build_qemu_start_command
from imbue.mngr_imbue_cloud.slices.slice_client import build_slice_vm_client
from imbue.mngr_imbue_cloud.slices.ssh_box_image_cache import SshBoxImageCache
from imbue.mngr_vps.container_setup import HOST_VOLUME_HOME_PATH
from imbue.mngr_vps.container_setup import build_home_volume_symlink_command
from imbue.mngr_vps.container_setup import create_bind_volume_on_outer
from imbue.mngr_vps.container_setup import exec_in_container
from imbue.mngr_vps.container_setup import host_volume_name_for
from imbue.mngr_vps.container_setup import provision_snapshot_helper_on_outer
from imbue.mngr_vps.container_setup import remove_container
from imbue.mngr_vps.container_setup import run_docker
from imbue.mngr_vps.container_setup import snapshot_trigger_volume_name_for
from imbue.mngr_vps.container_setup import start_container
from imbue.mngr_vps.container_setup import start_container_sshd
from imbue.mngr_vps.primitives import VpsInstanceId

# The guest-side btrfs mount the workspace volume binds from (the slice
# provider's ``btrfs_mount_path`` default, which host_state.json records).
_GUEST_BTRFS_MOUNT: Final[Path] = Path("/mngr-btrfs")
_SHORT_TIMEOUT_SECONDS: Final[float] = 120.0
_SLOW_COMMAND_TIMEOUT_SECONDS: Final[float] = 300.0
_RESERVE_TIMEOUT_SECONDS: Final[float] = 600.0
_IMAGE_LOAD_TIMEOUT_SECONDS: Final[float] = 1800.0
_ROW_POLL_SECONDS: Final[float] = 15.0
_TRANSFER_WAIT_SECONDS: Final[float] = 6000.0
_BANNER_WAIT_SECONDS: Final[int] = 600
_HEALTH_PROBE_TIMEOUT_SECONDS: Final[float] = 600.0
_HEALTH_PROBE_INTERVAL_SECONDS: Final[float] = 15.0
_HOST_KEY_PROBE_TIMEOUT_SECONDS: Final[float] = 10.0
_VM_ROOT_USER: Final[str] = "root"
_MANAGEMENT_SSH_USER: Final[str] = "debian"
# Where the transfer env for the image-tar publish lives on the box (not tied
# to one instance).
_IMAGES_TRANSFER_INSTANCE: Final[str] = "cutover-images"
# What a per-box worker turns into a failed BoxOutcome: the thread fan-out
# (``run_outcome_workers_in_bounded_threads``) aborts the whole batch, and with
# it the other boxes' in-flight work, when a worker raises.
_STAGE_FAILURE_EXCEPTIONS: Final[tuple[type[Exception], ...]] = (
    MngrError,
    CutoverError,
    BareMetalProvisioningError,
    ProcessTimeoutError,
    psycopg2.Error,
    OSError,
    click.ClickException,
)


class CutoverContext(FrozenModel):
    """Everything a cutover stage needs from the activated env, resolved once per command."""

    env_name: str = Field(description="The activated env")
    dsn: str = Field(description="The pool DB DSN")
    pool_public_key: str = Field(
        description=(
            "The tier's gen-1 pool management public key: stripped from a harvested gen-1 workspace's "
            "authorized_keys when it lands on a gen-2 target, which authorizes no static management key"
        )
    )
    identities: ManagementIdentityResolver = Field(
        description="The keys boxes and VMs are dialed with: the operator certificate on gen-2, the pool key on gen-1"
    )
    ssh_ca_public_key: str | None = Field(
        description="The tier's committed SSH CA public key (deploy.toml [ssh_ca]); a gen-2 target needs it"
    )
    storage: WorkspaceStorageConfig = Field(description="The tier bucket + KEK")
    state: CutoverStateStore = Field(description="The state dir")
    mngr_ctx: MngrContext = Field(description="A minimal mngr context for OuterHost (its concurrency group)")
    connector_url: str | None = Field(
        default=None, description="The tier's connector base URL (resolved for migrate/rollback only)"
    )
    admin_api_key: SecretStr | None = Field(
        default=None, description="The connector admin API key (resolved for migrate/rollback only)"
    )


def _admin_client(ctx: CutoverContext) -> tuple[ImbueCloudConnectorClient, SecretStr]:
    """The connector admin client + key; migrate/rollback commands resolve them into the context."""
    if ctx.connector_url is None or ctx.admin_api_key is None:
        raise CutoverError("this stage needs the connector admin API; the command did not resolve it")
    return ImbueCloudConnectorClient(base_url=AnyUrl(ctx.connector_url)), ctx.admin_api_key


@contextmanager
def minimal_mngr_context(profile_dir: Path) -> Iterator[MngrContext]:
    """The bare context ``OuterHost`` and the container helpers need: an active concurrency group, no config or plugins.

    The group is entered for the block (``make_concurrency_group`` refuses an
    unentered parent, and the snapshot-helper provisioning runs its steps in
    child groups) and closed with ``__exit__(None, None, None)`` like
    ``mngr``'s ``setup_command_context``, so a click exception or ``SystemExit``
    leaving the block is not wrapped in a ``ConcurrencyExceptionGroup``.
    """
    profile_dir.mkdir(parents=True, exist_ok=True)
    mngr_ctx = MngrContext(config=MngrConfig(), pm=pluggy.PluginManager("mngr"), profile_dir=profile_dir)
    mngr_ctx.concurrency_group.__enter__()
    try:
        yield mngr_ctx
    finally:
        mngr_ctx.concurrency_group.__exit__(None, None, None)


# Reaching things: the pool DB, the box (slice client), the VM root (OuterHost), the bucket (boto3).


@contextmanager
def _pool_connection(ctx: CutoverContext) -> Iterator[Any]:
    """A pool DB connection for one short read or CAS, closed on the way out."""
    conn = psycopg2.connect(ctx.dsn)
    try:
        yield conn
    finally:
        conn.close()


def _box_client(ctx: CutoverContext, server: BareMetalServer) -> SliceVmClientInterface:
    dial = resolve_server_management_dial(server)
    return build_slice_vm_client(
        box_generation=server.box_generation,
        box_address=dial.host,
        box_ssh_port=dial.port,
        box_ssh_user=box_service_user(server),
        private_key_path=str(ctx.identities.private_key_path_for(server.box_generation)),
        box_host_public_key=server.box_host_public_key,
    )


def _run_on_box_checked(client: SliceVmClientInterface, command: str, *, timeout: float, label: str) -> str:
    exit_code, stdout, stderr = client.run_on_box(command, timeout=timeout, label=label)
    if exit_code != 0:
        raise CutoverError(f"box command {label!r} failed (exit {exit_code}): {stderr.strip() or stdout.strip()}")
    return stdout


def _write_box_file(client: SliceVmClientInterface, path: str, content: str, *, label: str) -> None:
    encoded = base64.b64encode(content.encode()).decode()
    quoted = shlex.quote(path)
    _run_on_box_checked(
        client,
        f"umask 077 && mkdir -p {shlex.quote(str(Path(path).parent))} && echo {shlex.quote(encoded)} | base64 -d > {quoted}",
        timeout=_SHORT_TIMEOUT_SECONDS,
        label=label,
    )


def _remove_box_transfer_dirs(client: SliceVmClientInterface, dirs: Sequence[str], *, what: str) -> None:
    """Best-effort removal of transfer dirs (the env file holds S3 creds + identity) once their work is done."""
    try:
        exit_code, _out, stderr = client.run_on_box(
            "rm -rf " + " ".join(shlex.quote(path) for path in dirs), timeout=_SHORT_TIMEOUT_SECONDS, label="rm-td"
        )
    except (MngrError, ProcessTimeoutError, OSError) as exc:
        logger.warning("Could not remove the transfer dirs of {} on the box: {}", what, exc)
        return
    if exit_code != 0:
        logger.warning("Could not remove the transfer dirs of {} on the box: {}", what, stderr.strip())


@pure
def box_transfer_dir(box_ssh_user: str, instance_name: str) -> str:
    """The instance's transfer dir as an absolute path (the root-run transplant cannot rely on ``$HOME``).

    ``box_ssh_user`` is the user the slice client dials the box as: the
    transfer scripts find the same dir through that user's ``$HOME``.
    """
    return f"/home/{box_ssh_user}/{TRANSFER_DIR_ROOT}/{instance_name}"


@pure
def unreachable_vm_error(public_address: str, ssh_port: int, row_status: str, row_id: str) -> str:
    """The preflight/migrate error for a VM whose root sshd does not answer; a stopped row names its remedy."""
    message = f"VM root sshd at {public_address}:{ssh_port} unreachable"
    if row_status == "stopped":
        return f"{message} (stopped row; `cutover migrate` admin-starts it, or start it with `minds-admin workspaces start {row_id}`)"
    return message


@contextmanager
def vm_root_outer(
    ctx: CutoverContext, *, address: str, port: int, host_public_key: str, box_generation: int
) -> Iterator[OuterHost]:
    """An ``OuterHost`` on the slice VM's root sshd, strictly pinned to ``host_public_key``.

    A gen-1 VM authorizes the pool key; a gen-2 VM trusts the tier CA, so it is
    dialed with the operator's certificate.
    """
    known_hosts_dir = Path(tempfile.mkdtemp(prefix="mngr-cutover-known-hosts-"))
    outer: OuterHost | None = None
    try:
        known_hosts_path = known_hosts_dir / "known_hosts"
        add_host_to_known_hosts(known_hosts_path, address, port, host_public_key)
        pyinfra_host = create_pyinfra_host(
            hostname=address,
            port=port,
            private_key_path=ctx.identities.private_key_path_for(box_generation),
            known_hosts_path=known_hosts_path,
            ssh_user=_VM_ROOT_USER,
        )
        outer = OuterHost(id=HostId.generate(), connector=PyinfraConnector(pyinfra_host), mngr_ctx=ctx.mngr_ctx)
        yield outer
    finally:
        if outer is not None:
            outer.disconnect()
        shutil.rmtree(known_hosts_dir, ignore_errors=True)


def _run_on_vm_checked(outer: OuterHostInterface, command: str, *, timeout: float, label: str) -> str:
    result = outer.execute_idempotent_command(command, timeout_seconds=timeout)
    if not result.success:
        raise CutoverError(f"VM command {label!r} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout


def _s3_client(storage: WorkspaceStorageConfig) -> Any:
    # A session per client: the drain's worker threads build clients concurrently,
    # and boto3's process-global default session is not thread-safe.
    return boto3.Session().client(
        "s3",
        endpoint_url=storage.s3_endpoint,
        region_name=storage.s3_region,
        aws_access_key_id=storage.access_key_id.get_secret_value(),
        aws_secret_access_key=storage.secret_access_key.get_secret_value(),
        config=BotoConfig(retries={"max_attempts": 3, "mode": "standard"}),
    )


# The botocore error codes that mean "no such object" (a head answers 404 /
# NotFound, a get NoSuchKey) rather than a failed request.
_MISSING_OBJECT_ERROR_CODES: Final[frozenset[str]] = frozenset({"404", "NoSuchKey", "NotFound"})


@pure
def _is_missing_object_error(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") in _MISSING_OBJECT_ERROR_CODES


def _s3_object_size_or_none(storage: WorkspaceStorageConfig, key: str) -> int | None:
    try:
        response = _s3_client(storage).head_object(Bucket=storage.bucket, Key=key)
    except ClientError as exc:
        if _is_missing_object_error(exc):
            return None
        raise CutoverError(f"could not head s3://{storage.bucket}/{key}: {exc}") from exc
    except BotoCoreError as exc:
        raise CutoverError(f"could not head s3://{storage.bucket}/{key}: {exc}") from exc
    return int(response["ContentLength"])


def unwrap_age_identity(storage: WorkspaceStorageConfig, wrapped: str) -> str:
    """Unwrap a KEK-wrapped age identity (the connector's envelope: base64 of 12-byte nonce + AES-256-GCM)."""
    kek = base64.b64decode(storage.kek_base64.get_secret_value())
    return unwrap_dek(kek, base64.b64decode(wrapped)).decode("utf-8")


def _transfer_env_text(
    storage: WorkspaceStorageConfig, *, key_prefix: str, instance_name: str, age_recipient: str, age_identity: str
) -> str:
    return render_transfer_env(
        TransferEnv(
            s3_endpoint=storage.s3_endpoint,
            s3_region=storage.s3_region,
            access_key_id=storage.access_key_id.get_secret_value(),
            secret_access_key=storage.secret_access_key.get_secret_value(),
            bucket=storage.bucket,
            key_prefix=key_prefix,
            instance_name=instance_name,
            age_recipient=age_recipient,
            age_identity=age_identity,
        )
    )


# Probes shared by preflight and restore.


def _container_id_on_vm(outer: OuterHostInterface, host_id: str) -> str:
    stdout = _run_on_vm_checked(
        outer, build_container_id_command(host_id), timeout=_SHORT_TIMEOUT_SECONDS, label="container-id"
    )
    container_ids = stdout.split()
    if len(container_ids) != 1:
        raise CutoverError(
            f"expected exactly one container labeled with host id {host_id}, found {len(container_ids)}"
        )
    return container_ids[0]


def probe_workspace_health(outer: OuterHostInterface, container_name: str) -> list[str]:
    """One health pass inside the workspace: running container, every supervisord program RUNNING or EXITED, the UI answering."""
    warnings: list[str] = []
    running = outer.execute_idempotent_command(
        build_container_running_command(container_name), timeout_seconds=_SHORT_TIMEOUT_SECONDS
    )
    if not running.success or running.stdout.strip() != "true":
        warnings.append(f"container {container_name} is not running")
        return warnings
    supervisor = outer.execute_idempotent_command(
        build_supervisorctl_status_command(container_name), timeout_seconds=_SHORT_TIMEOUT_SECONDS
    )
    unhealthy = parse_supervisorctl_unhealthy(supervisor.stdout)
    if not supervisor.success and not supervisor.stdout.strip():
        warnings.append(f"supervisorctl status failed: {supervisor.stderr.strip()}")
    for entry in unhealthy:
        warnings.append(f"supervisord not healthy: {entry}")
    ui = outer.execute_idempotent_command(
        build_system_interface_probe_command(container_name), timeout_seconds=_SHORT_TIMEOUT_SECONDS
    )
    if not ui.success:
        warnings.append("system_interface is not answering on :8000")
    return warnings


class _HealthProbeHistory(MutableModel):
    """The last health pass's findings, kept across the poll so a timeout can report them."""

    last_warnings: list[str] = Field(default_factory=list, description="The most recent probe's warnings")


def _probe_workspace_health_once(
    outer: OuterHostInterface, container_name: str, history: _HealthProbeHistory
) -> bool | None:
    history.last_warnings = probe_workspace_health(outer, container_name)
    return True if not history.last_warnings else None


def _wait_for_workspace_health(outer: OuterHostInterface, container_name: str) -> list[str]:
    """Poll the health pass until it is clean or the budget runs out; returns the last warnings."""
    history = _HealthProbeHistory()
    is_healthy, _polls, _elapsed = poll_for_value(
        lambda: _probe_workspace_health_once(outer, container_name, history),
        timeout=_HEALTH_PROBE_TIMEOUT_SECONDS,
        poll_interval=_HEALTH_PROBE_INTERVAL_SECONDS,
    )
    return [] if is_healthy else history.last_warnings


def _preflight_candidate(
    ctx: CutoverContext, client: SliceVmClientInterface, server: BareMetalServer, row: CutoverPoolRow
) -> WorkspacePreflight:
    """Probe one candidate row: served key, version, health, data disk."""
    base = WorkspacePreflight(
        host_db_id=row.id,
        host_id=row.host_id,
        host_name=row.host_name,
        leased_to_user=row.leased_to_user,
        status=row.status,
        verdict=RowVerdict.CANDIDATE,
        baked_version=row.baked_version,
        disk_gb=row.disk_gb,
    )
    if (
        row.ssh_port is None
        or row.container_ssh_port is None
        or row.slice_instance_name is None
        or row.slice_disk_name is None
    ):
        return base.model_copy_update(
            to_update(base.field_ref().error, "row lacks its ports, slice instance name or slice disk name")
        )
    served_key = read_served_host_key_or_none(
        str(server.public_address), row.ssh_port, timeout_seconds=_HOST_KEY_PROBE_TIMEOUT_SECONDS
    )
    if served_key is None:
        return base.model_copy_update(
            to_update(
                base.field_ref().error,
                unreachable_vm_error(str(server.public_address), row.ssh_port, row.status, row.id),
            )
        )
    is_rotated = not is_same_ssh_public_key(served_key, row.outer_host_public_key or "")
    try:
        with vm_root_outer(
            ctx,
            address=str(server.public_address),
            port=row.ssh_port,
            host_public_key=served_key,
            box_generation=server.box_generation,
        ) as outer:
            container_id = _container_id_on_vm(outer, row.host_id)
            describe = outer.execute_idempotent_command(
                build_git_describe_command(container_id), timeout_seconds=_SHORT_TIMEOUT_SECONDS
            )
            inspect_output = _run_on_vm_checked(
                outer, build_docker_inspect_command(container_id), timeout=_SHORT_TIMEOUT_SECONDS, label="inspect"
            )
            container_name = container_name_from_inspect(parse_docker_inspect(inspect_output))
            health_warnings = probe_workspace_health(outer, container_name)
    except (MngrError, CutoverError, OSError) as exc:
        return base.model_copy_update(
            to_update(base.field_ref().is_host_key_rotated, is_rotated),
            to_update(base.field_ref().error, f"VM probe failed: {exc}"),
        )
    describe_text = describe.stdout.strip()
    version = parse_version_tag(describe_text) if describe.success else None
    if describe.success:
        version_error = version_tag_error_or_none(describe_text)
    else:
        version_error = f"git describe failed inside the container: {describe.stderr.strip()[:200]}"
    is_version_ok = version_error is None
    disk_error = None
    disk_format: str | None = None
    disk_virtual_gib: int | None = None
    try:
        info_output = _run_on_box_checked(
            client,
            build_gen1_datadisk_info_command(row.slice_disk_name),
            timeout=_SHORT_TIMEOUT_SECONDS,
            label=f"qemu-img-info:{row.slice_disk_name}",
        )
        disk_format, virtual_bytes = parse_qemu_img_info(info_output)
        disk_virtual_gib = virtual_bytes // 1024**3
        disk_error = gen1_data_disk_size_error_or_none(disk_virtual_gib, row.disk_gb)
    except (CutoverError, ProcessTimeoutError) as exc:
        disk_error = str(exc)
    error = version_error or disk_error
    return base.model_copy_update(
        to_update(base.field_ref().version_tag, describe_text if version is not None else None),
        to_update(base.field_ref().is_version_ok, is_version_ok),
        to_update(base.field_ref().is_host_key_rotated, is_rotated),
        to_update(base.field_ref().data_disk_format, disk_format),
        to_update(base.field_ref().data_disk_virtual_gib, disk_virtual_gib),
        to_update(base.field_ref().health_warnings, tuple(health_warnings)),
        to_update(base.field_ref().error, error),
    )


@pure
def _classified_workspace_preflight(row: CutoverPoolRow, classification: RowClassification) -> WorkspacePreflight:
    """The preflight entry of a row that is not probed (destroyed or refused on its status alone)."""
    return WorkspacePreflight(
        host_db_id=row.id,
        host_id=row.host_id,
        host_name=row.host_name,
        leased_to_user=row.leased_to_user,
        status=row.status,
        verdict=classification.verdict,
        remedy=classification.remedy,
        baked_version=row.baked_version,
        disk_gb=row.disk_gb,
    )


def _preflight_box(ctx: CutoverContext, server: BareMetalServer, rows: Sequence[CutoverPoolRow]) -> BoxPreflight:
    workspaces: list[WorkspacePreflight] = []
    try:
        client = _box_client(ctx, server)
        # A cheap reachability probe before the per-row work.
        _run_on_box_checked(client, "true", timeout=_SHORT_TIMEOUT_SECONDS, label="box-reachability")
    except (MngrError, CutoverError, ProcessTimeoutError, OSError) as exc:
        return BoxPreflight(
            server_id=str(server.id),
            public_address=str(server.public_address),
            status=str(server.status),
            workspaces=(),
            error=f"box unreachable: {exc}",
        )
    for row in rows:
        classification = classify_pool_row(row.status, row.bare_metal_server_id)
        if classification.verdict == RowVerdict.CANDIDATE and row.status == "leased":
            workspaces.append(_preflight_candidate(ctx, client, server, row))
        else:
            # A stopped candidate's VM is unreachable (or absent), so there is
            # nothing to probe: the migrate admin-starts it and harvests then.
            workspaces.append(_classified_workspace_preflight(row, classification))
    return BoxPreflight(
        server_id=str(server.id),
        public_address=str(server.public_address),
        status=str(server.status),
        workspaces=tuple(workspaces),
    )


def run_preflight(ctx: CutoverContext, server_ids: Sequence[str]) -> PreflightReport:
    """Probe every gen-1 box (or the named ones) and report each row's migrate eligibility.

    The whole-tier run also lists the gen-1 rows on no box at all (finalized
    stops -- migratable, the migrate admin-starts them first). ``--server-id``
    scopes the run to boxes, so they are left out of it.
    """
    with _pool_connection(ctx) as conn:
        servers = [
            server
            for server in fetch_gen1_servers(conn)
            if (not server_ids or str(server.id) in server_ids) and server.public_address
        ]
        rows_by_server_id = {str(server.id): fetch_pool_rows_on_server(conn, server.id) for server in servers}
        unplaced_rows = fetch_unplaced_gen1_pool_rows(conn) if not server_ids else []
    require_named_servers_selected(server_ids, servers, reason="not a generation-1 box with a public address")
    boxes = [_preflight_box(ctx, server, rows_by_server_id[str(server.id)]) for server in servers]
    # The rows this tooling parked mid-migration are on no box either; they are
    # its own in-flight work, not part of the inventory. A terminal record
    # (rolled back) does not hide the row: it is an ordinary gen-1 row again.
    unplaced_workspaces = [
        _classified_workspace_preflight(row, classify_unplaced_gen1_row(row.status))
        for row in unplaced_rows
        if not _is_migration_state_in_flight(ctx.state.read_workspace(row.id))
    ]
    return build_preflight_report(ctx.env_name, boxes, unplaced_workspaces)


@pure
def build_preflight_report(
    env_name: str, boxes: Sequence[BoxPreflight], unplaced_workspaces: Sequence[WorkspacePreflight] = ()
) -> PreflightReport:
    workspaces = [workspace for box in boxes for workspace in box.workspaces]
    return PreflightReport(
        env_name=env_name,
        boxes=tuple(boxes),
        unplaced_workspaces=tuple(unplaced_workspaces),
        distinct_version_tags=tuple(
            sorted(
                {w.version_tag for w in workspaces if w.version_tag is not None and w.verdict == RowVerdict.CANDIDATE}
            )
        ),
        refused_count=sum(1 for w in [*workspaces, *unplaced_workspaces] if w.verdict == RowVerdict.REFUSED),
        error_count=sum(1 for w in workspaces if w.error is not None)
        + sum(1 for box in boxes if box.error is not None),
    )


_UNPLACED_BOX_LABEL: Final[str] = "(none)"


@pure
def _preflight_table_row(box_label: str, workspace: WorkspacePreflight) -> list[str]:
    return [
        box_label,
        workspace.host_db_id[:8],
        workspace.host_name,
        workspace.leased_to_user or "-",
        workspace.status,
        str(workspace.verdict),
        workspace.version_tag or workspace.baked_version or "-",
        "ok" if workspace.is_version_ok else "-",
        "rotated" if workspace.is_host_key_rotated else "bake",
        f"{workspace.data_disk_virtual_gib or '-'}->{workspace.disk_gb}",
        "; ".join(workspace.health_warnings) or "-",
        workspace.error or workspace.remedy or "-",
    ]


@pure
def render_preflight_table(report: PreflightReport) -> str:
    """The human report: one row per pool row (the box-less ones last), then the box errors and the summary."""
    rows = [
        _preflight_table_row(box.public_address, workspace) for box in report.boxes for workspace in box.workspaces
    ]
    rows.extend(_preflight_table_row(_UNPLACED_BOX_LABEL, workspace) for workspace in report.unplaced_workspaces)
    table = tabulate(
        rows,
        headers=[
            "BOX",
            "ROW",
            "NAME",
            "USER",
            "STATUS",
            "VERDICT",
            "VERSION",
            "FLOOR",
            "VM KEY",
            "DISK GiB",
            "HEALTH",
            "ERROR / REMEDY",
        ],
        tablefmt="plain",
    )
    box_error_lines = [f"{box.public_address}: ERROR {box.error}" for box in report.boxes if box.error is not None]
    summary = (
        f"versions: {', '.join(report.distinct_version_tags) or '-'}; refused {report.refused_count}; "
        f"errors {report.error_count}; "
        f"{'CLEAN' if report.is_clean else 'NOT CLEAN'}"
    )
    return "\n".join([table, "", *box_error_lines, summary])


def _harvest_workspace(
    ctx: CutoverContext,
    client: SliceVmClientInterface,
    server: BareMetalServer,
    row: CutoverPoolRow,
    *,
    target_server_id: str,
) -> tuple[CutoverWorkspaceState, HarvestedKeys, dict[str, Any]]:
    """Read the keys, the container inspect and the version off a live gen-1 workspace."""
    if row.ssh_port is None or row.container_ssh_port is None or row.slice_instance_name is None:
        raise CutoverError(f"row {row.id} lacks its ports or slice instance name")
    if row.slice_disk_name is None:
        raise CutoverError(f"row {row.id} lacks its slice disk name")
    served_key = read_served_host_key_or_none(
        str(server.public_address), row.ssh_port, timeout_seconds=_HOST_KEY_PROBE_TIMEOUT_SECONDS
    )
    if served_key is None:
        raise CutoverError(unreachable_vm_error(str(server.public_address), row.ssh_port, row.status, row.id))
    is_rotated = not is_same_ssh_public_key(served_key, row.outer_host_public_key or "")
    with vm_root_outer(
        ctx,
        address=str(server.public_address),
        port=row.ssh_port,
        host_public_key=served_key,
        box_generation=server.box_generation,
    ) as outer:
        vm_files = parse_marked_files(
            _run_on_vm_checked(outer, build_vm_key_harvest_command(), timeout=_SHORT_TIMEOUT_SECONDS, label="vm-keys")
        )
        container_id = _container_id_on_vm(outer, row.host_id)
        container_files = parse_marked_files(
            _run_on_vm_checked(
                outer,
                build_container_key_harvest_command(container_id),
                timeout=_SHORT_TIMEOUT_SECONDS,
                label="container-keys",
            )
        )
        inspect_entry = parse_docker_inspect(
            _run_on_vm_checked(
                outer, build_docker_inspect_command(container_id), timeout=_SHORT_TIMEOUT_SECONDS, label="inspect"
            )
        )
        describe_text = _run_on_vm_checked(
            outer, build_git_describe_command(container_id), timeout=_SHORT_TIMEOUT_SECONDS, label="git-describe"
        ).strip()
    try:
        keys = HarvestedKeys(
            vm_host_private_key=SecretStr(vm_files[SSHD_HOST_KEY_PATH]),
            vm_host_public_key=vm_files[f"{SSHD_HOST_KEY_PATH}.pub"],
            vm_authorized_keys=vm_files[ROOT_AUTHORIZED_KEYS_PATH],
            container_host_private_key=SecretStr(container_files[SSHD_HOST_KEY_PATH]),
            container_host_public_key=container_files[f"{SSHD_HOST_KEY_PATH}.pub"],
            container_authorized_keys=container_files[ROOT_AUTHORIZED_KEYS_PATH],
        )
    except KeyError as exc:
        raise CutoverError(f"harvest output lacked {exc}") from exc
    if not is_same_ssh_public_key(keys.vm_host_public_key, served_key):
        raise CutoverError("the VM's on-disk host key differs from the key its sshd serves; refusing to harvest")
    version_error = version_tag_error_or_none(describe_text)
    if version_error is not None:
        raise CutoverError(version_error)
    info_output = _run_on_box_checked(
        client,
        build_gen1_datadisk_info_command(row.slice_disk_name),
        timeout=_SHORT_TIMEOUT_SECONDS,
        label=f"qemu-img-info:{row.slice_disk_name}",
    )
    disk_format, virtual_bytes = parse_qemu_img_info(info_output)
    disk_size_error = gen1_data_disk_size_error_or_none(virtual_bytes // 1024**3, row.disk_gb)
    if disk_size_error is not None:
        raise CutoverError(disk_size_error)
    state = CutoverWorkspaceState(
        host_db_id=row.id,
        host_id=row.host_id,
        agent_id=row.agent_id,
        host_name=row.host_name,
        leased_to_user=row.leased_to_user,
        origin_server_id=str(server.id),
        origin_public_address=str(server.public_address),
        origin_vm_ssh_port=row.ssh_port,
        origin_container_ssh_port=row.container_ssh_port,
        target_server_id=target_server_id,
        is_host_key_rotated=is_rotated,
        slice_instance_name=row.slice_instance_name,
        slice_disk_name=row.slice_disk_name,
        version_tag=describe_text,
        gen1_data_disk_virtual_gib=virtual_bytes // 1024**3,
        gen1_data_disk_format=disk_format,
        migrated_data_disk_gib=row.disk_gb,
        memory_units=DEFAULT_MACHINE_UNITS,
        stage=CutoverStage.HARVESTED,
    )
    return state, keys, inspect_entry


def _poll_row_status_once(
    ctx: CutoverContext,
    row_id: str,
    success_statuses: tuple[str, ...],
    failure_statuses: tuple[str, ...],
    what: str,
) -> CutoverPoolRow | None:
    """One poll of the row: the row once it reaches a success status, None while it works; raises on failure."""
    with _pool_connection(ctx) as conn:
        row = fetch_pool_row(conn, row_id)
    if row is None:
        raise CutoverError(f"pool row {row_id} vanished while waiting for {what}")
    if row.status in failure_statuses:
        raise CutoverError(f"row {row_id} reached {row.status!r} while waiting for {what}")
    return row if row.status in success_statuses else None


def _await_row_status(
    ctx: CutoverContext,
    row_id: str,
    *,
    success_statuses: tuple[str, ...],
    failure_statuses: tuple[str, ...],
    timeout_seconds: float,
    what: str,
) -> CutoverPoolRow:
    """Poll the pool row until it reaches a success status; raise on a failure status or timeout."""
    row, _polls, _elapsed = poll_for_value(
        lambda: _poll_row_status_once(ctx, row_id, success_statuses, failure_statuses, what),
        timeout=timeout_seconds,
        poll_interval=_ROW_POLL_SECONDS,
    )
    if row is None:
        raise CutoverError(
            f"row {row_id} did not reach {'/'.join(success_statuses)} within {timeout_seconds:.0f}s ({what})"
        )
    return row


def _ensure_workspace_running(ctx: CutoverContext, row: CutoverPoolRow) -> CutoverPoolRow:
    """Admin-start a stopped gen-1 row and wait for ``leased``; a leased row passes through.

    The migrate needs a live VM and container to harvest keys and the
    ``docker inspect`` from (no offline boot-disk harvest exists), so every
    stopped candidate goes through the product's own start first.
    """
    if row.status == "leased":
        return row
    if row.status != "stopped":
        raise CutoverError(f"row {row.id} is {row.status}; only leased or stopped workspaces can be migrated")
    client, admin_key = _admin_client(ctx)
    client.admin_start_workspace(admin_key, row.id)
    return _await_row_status(
        ctx,
        row.id,
        success_statuses=("leased",),
        failure_statuses=("crashed",),
        timeout_seconds=_TRANSFER_WAIT_SECONDS,
        what="the pre-migration start",
    )


def _stop_workspace_via_product(ctx: CutoverContext, row_id: str) -> CutoverPoolRow:
    """Run the product's own stop (verified three-object artifact upload) and wait for ``stopped``."""
    client, admin_key = _admin_client(ctx)
    client.admin_stop_workspace(admin_key, row_id)
    return _await_row_status(
        ctx,
        row_id,
        success_statuses=("stopped",),
        failure_statuses=("crashed",),
        timeout_seconds=_TRANSFER_WAIT_SECONDS,
        what="the product stop's artifact upload",
    )


# Objects at most this large are copied with one CopyObject call; larger ones
# get a multipart copy (CopyObject itself caps out at 5 GiB, and artifact disk
# objects routinely exceed it).
_S3_SINGLE_COPY_MAX_BYTES: Final[int] = 256 * 1024 * 1024
_S3_COPY_PART_BYTES: Final[int] = 256 * 1024 * 1024


@pure
def s3_copy_part_ranges(size: int, part_bytes: int) -> list[tuple[int, int]]:
    """The inclusive byte ranges of a multipart copy, in part order (1 part per ``part_bytes``)."""
    if size <= 0 or part_bytes <= 0:
        raise CutoverError(f"cannot split a {size}-byte object into {part_bytes}-byte copy parts")
    return [(start, min(start + part_bytes, size) - 1) for start in range(0, size, part_bytes)]


def _s3_copy_object(storage: WorkspaceStorageConfig, source_key: str, dest_key: str) -> None:
    """Server-side copy within the tier bucket, without ETag preconditions.

    Hand-rolled rather than boto3's managed ``copy``: that pins every
    ``UploadPartCopy`` to the source's HEAD ETag via ``CopySourceIfMatch``,
    which OVH's S3 gateway answers with 412 for multipart-uploaded sources
    (drilled 2026-09-06). Copy-time pinning is not what guards integrity
    anyway: the restore verifies the artifact's recorded sha256, and the
    parked row blocks the only legitimate writer (the product's next stop).
    """
    client = _s3_client(storage)
    try:
        size = int(client.head_object(Bucket=storage.bucket, Key=source_key)["ContentLength"])
        if size <= _S3_SINGLE_COPY_MAX_BYTES:
            client.copy_object(
                CopySource={"Bucket": storage.bucket, "Key": source_key}, Bucket=storage.bucket, Key=dest_key
            )
        else:
            _s3_multipart_copy(client, storage.bucket, source_key=source_key, dest_key=dest_key, size=size)
    except (ClientError, BotoCoreError) as exc:
        raise CutoverError(f"could not copy s3://{storage.bucket}/{source_key} to {dest_key}: {exc}") from exc


def _s3_multipart_copy(client: Any, bucket: str, *, source_key: str, dest_key: str, size: int) -> None:
    """Multipart server-side copy (no ``CopySourceIfMatch``; see ``_s3_copy_object``)."""
    upload_id = client.create_multipart_upload(Bucket=bucket, Key=dest_key)["UploadId"]
    is_completed = False
    try:
        parts = []
        for part_number, (start, end) in enumerate(s3_copy_part_ranges(size, _S3_COPY_PART_BYTES), start=1):
            response = client.upload_part_copy(
                Bucket=bucket,
                Key=dest_key,
                UploadId=upload_id,
                PartNumber=part_number,
                CopySource={"Bucket": bucket, "Key": source_key},
                CopySourceRange=f"bytes={start}-{end}",
            )
            parts.append({"ETag": response["CopyPartResult"]["ETag"], "PartNumber": part_number})
        client.complete_multipart_upload(
            Bucket=bucket, Key=dest_key, UploadId=upload_id, MultipartUpload={"Parts": parts}
        )
        is_completed = True
    finally:
        if not is_completed:
            _abort_multipart_upload_quietly(client, bucket, dest_key, upload_id)


def _abort_multipart_upload_quietly(client: Any, bucket: str, key: str, upload_id: str) -> None:
    """Best-effort abort so a failed copy does not also mask its own error with the abort's."""
    try:
        client.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)
    except (ClientError, BotoCoreError) as exc:
        logger.warning("Could not abort the failed multipart copy to {} (upload {}): {}", key, upload_id, exc)


# The product stop artifact's three object filenames (shared transfer conventions).
_ARTIFACT_OBJECT_FILENAMES: Final[tuple[str, ...]] = (DISK_OBJECT, DATADISK_OBJECT, META_OBJECT)


@pure
def build_saved_product_artifact(
    manifest: Mapping[str, Any], wrapped_dek: str, *, rollback_key_prefix: str, fallback_generation: int
) -> SavedProductArtifact:
    """The rollback record for one product artifact manifest, rewritten onto the rollback copy's prefix."""
    source_prefix = str(manifest.get("key_prefix") or "")
    age_recipient = str(manifest.get("age_recipient") or "")
    objects = manifest.get("object_by_name") or {}
    datadisk_sha = str((objects.get("DATADISK") or {}).get("sha256") or "")
    if not source_prefix or not age_recipient or not datadisk_sha:
        raise CutoverError("the artifact manifest lacks its key prefix / recipient / data-disk sha")
    return SavedProductArtifact(
        generation=int(manifest.get("generation") or fallback_generation),
        key_prefix=rollback_key_prefix,
        age_recipient=age_recipient,
        datadisk_sha256=datadisk_sha,
        wrapped_dek=wrapped_dek,
        manifest_json={**manifest, "key_prefix": rollback_key_prefix},
    )


def _save_product_artifact(ctx: CutoverContext, row: CutoverPoolRow) -> SavedProductArtifact:
    """Copy the product stop artifact to the rollback prefix and record its coordinates.

    The product deletes the previous generation's objects on the workspace's
    next stop, so the migrate's copy is what keeps rollback possible after the
    workspace has run (and stopped) on gen-2. The copy is server-side; the
    saved manifest is the row's, rewritten to point at the copy.
    """
    manifest = row.artifact_manifest
    if manifest is None or row.wrapped_dek is None:
        raise CutoverError(f"row {row.id} is stopped but has no artifact manifest / wrapped DEK to save")
    saved = build_saved_product_artifact(
        manifest,
        row.wrapped_dek,
        rollback_key_prefix=migration_rollback_key_prefix(ctx.storage.key_prefix, row.host_id),
        fallback_generation=row.artifact_generation,
    )
    source_prefix = str(manifest["key_prefix"])
    for filename in _ARTIFACT_OBJECT_FILENAMES:
        _s3_copy_object(ctx.storage, f"{source_prefix}/{filename}", f"{saved.key_prefix}/{filename}")
    return saved


def _destroy_gen1_instance_on_box(ctx: CutoverContext, server_id: str, instance_name: str) -> None:
    """Destroy a gen-1 lima instance (and its disk) on its box; a box or instance already gone counts as done."""
    with _pool_connection(ctx) as conn:
        server = fetch_server_by_id(conn, BareMetalServerDbId(server_id))
    if server is None or not server.public_address:
        logger.info("Origin box {} is gone; nothing to destroy for {}", server_id, instance_name)
        return
    if server.box_generation >= FIRST_QEMU_BOX_GENERATION:
        logger.info("Origin box {} was repaved; the gen-1 instance {} went with it", server_id, instance_name)
        return
    _box_client(ctx, server).destroy_instance(VpsInstanceId(instance_name))


def _destroy_gen2_slice_on_box(ctx: CutoverContext, server_id: str, instance_name: str) -> None:
    """Destroy a gen-2 slice (and its staged transplant dir) on its target box; one already gone counts as done."""
    with _pool_connection(ctx) as conn:
        server = fetch_server_by_id(conn, BareMetalServerDbId(server_id))
    if server is None or not server.public_address:
        logger.info("Target box {} is gone; nothing to destroy for {}", server_id, instance_name)
        return
    if server.box_generation < FIRST_QEMU_BOX_GENERATION:
        logger.info("Target box {} is not gen-2; nothing to destroy for {}", server_id, instance_name)
        return
    client = _box_client(ctx, server)
    client.destroy_instance(VpsInstanceId(instance_name))
    # The transplant staging dir lives outside the slice dir the destroy
    # removes; a rollback must not leave the decrypted data disk behind (a
    # re-migration would target a fresh artifact anyway).
    _run_on_box_checked(
        client,
        build_transplant_clear_command(cutover_transplant_dir(instance_name)),
        timeout=_SHORT_TIMEOUT_SECONDS,
        label=f"clear-transplant:{instance_name}",
    )


def _target_capacity_error_or_none(
    ctx: CutoverContext, target_server: BareMetalServer, client: SliceVmClientInterface
) -> str | None:
    """A soft capacity check before the workspace is stopped, or None when the target looks roomy.

    The reserve script (under the box allocation lock) is the authoritative
    guard; this estimate exists so an obviously full target refuses the
    migration BEFORE the user's workspace is stopped. Existing occupancy is
    approximated at the default machine size, exactly like the bake's
    pre-check.
    """
    try:
        sizing = compute_server_slice_sizing(target_server, None)
    except (BareMetalProvisioningError, InvalidMachineSizeError) as exc:
        return f"target box sizing is unusable: {exc}"
    if "units" not in sizing:
        return "target box has no gen-2 sizing (units); is it really a gen-2 box?"
    used_slots = count_slice_resource_names(client.list_disk_names())
    estimated_capacity = estimate_gen2_machine_capacity(sizing)
    if used_slots >= estimated_capacity:
        return (
            f"target box fits an estimated {estimated_capacity} machine(s) and {used_slots} are in use; "
            "pick another target (the reserve would refuse after the stop)"
        )
    return None


@pure
def require_named_servers_selected(
    server_ids: Sequence[str], selected: Sequence[BareMetalServer | None], *, reason: str
) -> None:
    """Refuse explicitly named boxes the stage's scope excluded, instead of silently skipping them.

    Without ``--server-id`` the scope filter defines the batch; with it, a box the
    operator named but the filter dropped would otherwise vanish from the report
    with a clean exit code.
    """
    if not server_ids:
        return
    selected_ids = {str(server.id) for server in selected if server is not None}
    excluded = [server_id for server_id in server_ids if server_id not in selected_ids]
    if excluded:
        raise CutoverError(f"--server-id {', '.join(excluded)}: {reason}")


@pure
def undestroyed_pool_host_ids(destroy_outcomes: Sequence[PoolHostDestroyOutcome]) -> list[str]:
    """The rows a destroy pass left behind (a failed teardown, or a row leased out from under it)."""
    return [
        outcome.pool_host_id
        for outcome in destroy_outcomes
        if outcome.status not in (PoolHostDestroyOutcomeStatus.DESTROYED, PoolHostDestroyOutcomeStatus.ALREADY_GONE)
    ]


@pure
def repave_scope_refusal_or_none(server: BareMetalServer) -> str | None:
    """Why the repave must leave this box alone, or None when it may proceed.

    A gen-1 box repaves only once it is EMPTY (the caller checks its pool rows
    and in-flight migrations separately); a gen-2 box that is already ready
    needs nothing, and one that is not is a crashed repave the explicitly
    named re-run resumes.
    """
    if server.box_generation < FIRST_QEMU_BOX_GENERATION and str(server.status) not in (
        SERVER_STATUS_READY,
        SERVER_STATUS_DRAINING,
    ):
        return (
            f"box is {server.status} on generation {server.box_generation}; "
            "only a ready (or draining) gen-1 box repaves"
        )
    return None


def _repave_box(ctx: CutoverContext, server: BareMetalServer, *, is_dry_run: bool) -> BoxOutcome:
    """Repave one box, reporting a failure of the box-level steps as a failed outcome instead of raising."""
    try:
        return _repave_box_unguarded(ctx, server, is_dry_run=is_dry_run)
    except _STAGE_FAILURE_EXCEPTIONS as exc:
        logger.warning("Repave of {} failed: {}", server.id, exc)
        return BoxOutcome(server_id=str(server.id), stage=None, detail=f"repave failed: {exc}")


@pure
def _is_migration_state_in_flight(state: CutoverWorkspaceState | None) -> bool:
    """Whether a workspace record is an unfinished migration (RESTORED and ROLLED_BACK are terminal)."""
    return state is not None and state.stage not in (CutoverStage.RESTORED, CutoverStage.ROLLED_BACK)


def _in_flight_migration_ids_touching_box(ctx: CutoverContext, server_id: str) -> list[str]:
    """The state dir's unfinished migrations whose origin or target is this box (a repave would break them)."""
    return sorted(
        state.host_db_id
        for state in ctx.state.list_workspaces()
        if _is_migration_state_in_flight(state) and server_id in (state.origin_server_id, state.target_server_id)
    )


def _repave_box_unguarded(ctx: CutoverContext, server: BareMetalServer, *, is_dry_run: bool) -> BoxOutcome:
    if server.box_generation >= FIRST_QEMU_BOX_GENERATION and str(server.status) == SERVER_STATUS_READY:
        return BoxOutcome(server_id=str(server.id), stage=CutoverBoxStage.REPAVED, detail="already repaved")
    scope_refusal = repave_scope_refusal_or_none(server)
    if scope_refusal is not None:
        return BoxOutcome(server_id=str(server.id), stage=None, detail=scope_refusal)
    in_flight = _in_flight_migration_ids_touching_box(ctx, str(server.id))
    if in_flight:
        return BoxOutcome(
            server_id=str(server.id),
            stage=None,
            detail=f"in-flight migrations reference this box ({', '.join(in_flight)}); finish or roll them back first",
        )
    with _pool_connection(ctx) as conn:
        remaining_rows = fetch_pool_rows_on_server(conn, server.id)
    if remaining_rows:
        return BoxOutcome(
            server_id=str(server.id),
            stage=None,
            detail=(
                f"box still holds pool rows ({', '.join(row.id for row in remaining_rows)}); "
                "migrate the workspaces off it and `pool destroy` its unleased rows first"
            ),
        )
    if is_dry_run:
        return BoxOutcome(
            server_id=str(server.id),
            stage=CutoverBoxStage.REPAVED,
            detail="dry run: would set box_generation=2 / delivered / overcommit 4.0, reinstall + prep, measure the partition",
        )
    with _pool_connection(ctx) as conn:
        if server.box_generation < FIRST_QEMU_BOX_GENERATION:
            update_server(
                conn,
                server.id,
                box_generation=FIRST_QEMU_BOX_GENERATION,
                status=SERVER_STATUS_DELIVERED,
                cpu_overcommit_ratio=DEFAULT_SLICE_CPU_OVERCOMMIT_RATIO,
            )
    setup_server_to_ready(
        server_id=str(server.id),
        ssh_user=_MANAGEMENT_SSH_USER,
        slice_service_user=GEN2_SLICE_SERVICE_USER,
        lima_version=DEFAULT_LIMA_VERSION,
        slice_base_image_url=None,
        slice_base_image_sha512=None,
        os_template=None,
        ssh_ready_timeout=DEFAULT_SETUP_SSH_READY_TIMEOUT_SECONDS,
        database_url=ctx.dsn,
        extra_prep_script=None,
    )
    with _pool_connection(ctx) as conn:
        repaved = fetch_server_by_id(conn, server.id)
    if repaved is None:
        raise CutoverError(f"box row {server.id} vanished during the repave")
    size_text = _run_on_box_checked(
        _box_client(ctx, repaved),
        build_storage_partition_size_bytes_command(),
        timeout=_SHORT_TIMEOUT_SECONDS,
        label="storage-df",
    ).strip()
    if not size_text.isdigit():
        raise CutoverError(f"df of {GEN2_STORAGE_ROOT} on box {server.id} printed no byte size: {size_text!r}")
    storage_partition_bytes = int(size_text)
    ctx.state.write_box(
        CutoverBoxState(
            server_id=str(server.id), stage=CutoverBoxStage.REPAVED, storage_partition_bytes=storage_partition_bytes
        )
    )
    return BoxOutcome(
        server_id=str(server.id),
        stage=CutoverBoxStage.REPAVED,
        detail=f"gen-2 ready; storage partition {storage_partition_bytes} bytes",
    )


def run_repave(ctx: CutoverContext, server_ids: Sequence[str], *, is_dry_run: bool) -> StageReport:
    """Repave the explicitly named boxes (in parallel): flip each to gen-2, reinstall, prep, measure.

    There is no default scope: repaving destroys the box's OS, so the operator
    names each box. A box still holding pool rows (destroy ``available`` ones
    via ``pool destroy``; migrate the rest) or referenced by an in-flight
    migration is refused.
    """
    if not server_ids:
        raise CutoverError("repave requires --server-id (there is no default scope; repaving reinstalls the box)")
    with _pool_connection(ctx) as conn:
        candidates = [fetch_server_by_id(conn, BareMetalServerDbId(server_id)) for server_id in server_ids]
    servers = [server for server in candidates if server is not None and server.public_address]
    require_named_servers_selected(server_ids, servers, reason="no bare_metal_servers row with a public address")
    outcomes = run_outcome_workers_in_bounded_threads(
        worker=_repave_box,
        worker_kwargs_list=[dict(ctx=ctx, server=server, is_dry_run=is_dry_run) for server in servers],
        max_concurrency=max(1, len(servers)),
        thread_name_prefix="repave",
        progress_noun="Box repave",
        describe_outcome=lambda outcome: f"{outcome.server_id} {outcome.stage}",
        interruption_exception_types=(),
        on_join_interrupted=None,
    )
    return StageReport(stage_name="repave", env_name=ctx.env_name, is_dry_run=is_dry_run, boxes=tuple(outcomes))


@contextmanager
def _template_checkout_for_tag(version_tag: str) -> Iterator[BakeSource]:
    """A temporary default-workspace-template clone at ``version_tag`` (the bake source of every tag-keyed step)."""
    with resolved_bake_source(
        from_tag=version_tag,
        workspace_dir=None,
        repo_url=DEFAULT_WORKSPACE_TEMPLATE_REPO_URL,
        repo_branch_or_tag_override=None,
    ) as bake_source:
        yield bake_source


def _bake_pool_rows_from_tag(
    ctx: CutoverContext,
    server: BareMetalServer,
    *,
    region: str,
    version_tag: str,
    count: int,
    is_image_seed_bake: bool,
) -> None:
    """A ``pool create --from-tag`` style bake of ``count`` rows on the box; a failed bake is a ``CutoverError``.

    Its seed phase leaves the ``default-workspace-template:<tag>`` tar in the
    box's image cache, which is why the image publish bakes one row too (and
    destroys it again once the tar is there).
    """
    try:
        with _template_checkout_for_tag(version_tag) as bake_source:
            allocate_slices(
                count=count,
                server_id=str(server.id),
                lease_attributes=merge_bake_identity_attributes({}, bake_source),
                region=region,
                env_name=ctx.env_name,
                workspace_dir=bake_source.workspace_dir,
                mngr_source=None,
                is_from_tag=True,
                is_content_addressed_cache=False,
                database_url=ctx.dsn,
                identities=ctx.identities,
                is_dry_run=False,
                is_env_converge_wait_skipped=False,
                max_concurrency=min(4, count),
                machine_units=None,
                container_runtime_override=None,
                is_image_seed_bake=is_image_seed_bake,
            )
    except (
        MngrError,
        BareMetalProvisioningError,
        ProcessTimeoutError,
        # allocate_slices ends a bake with any failed row by exiting non-zero.
        SystemExit,
        OSError,
        click.ClickException,
    ) as exc:
        raise CutoverError(f"bake of {count} row(s) at {version_tag} on box {server.id} failed: {exc}") from exc


def _read_template_replay_inputs_for_tag(version_tag: str) -> TemplateReplayInputs:
    """Clone the tag and read the autostart installer and the slice provider's home path out of its settings.toml."""
    with _template_checkout_for_tag(version_tag) as bake_source:
        settings_text = (bake_source.workspace_dir / ".mngr" / "settings.toml").read_text()
    return extract_template_replay_inputs(settings_text)


def _pool_row_ids_on_server(ctx: CutoverContext, server: BareMetalServer) -> set[str]:
    with _pool_connection(ctx) as conn:
        return {row.id for row in fetch_pool_rows_on_server(conn, server.id)}


def _seed_image_tar_on_box(
    ctx: CutoverContext, server: BareMetalServer, cache: SshBoxImageCache, *, image_tag: str, version_tag: str
) -> None:
    """Bake one row at ``version_tag`` so its seed phase leaves the tag's tar in the box cache, then destroy the row.

    The bake's carve claims the lowest free host ports and a share of both
    budgets; kept, it would sit on exactly the ports the box's first drained
    workspace comes back at (gen-1 picked ports the same way) and on budget
    the preflight fit never counted. The tar lives in the image-cache dir, not
    the slice dir, so the destroy leaves it in place.
    """
    region = US_REGION_BY_OVH_DATACENTER_CODE.get(server.region or "")
    if region is None:
        raise CutoverError(f"box {server.id} is in datacenter {server.region!r}, which maps to no lease region")
    row_ids_before = _pool_row_ids_on_server(ctx, server)
    _bake_pool_rows_from_tag(ctx, server, region=region, version_tag=version_tag, count=1, is_image_seed_bake=True)
    seed_row_ids = sorted(_pool_row_ids_on_server(ctx, server) - row_ids_before)
    if seed_row_ids:
        destroy_outcomes = destroy_pool_hosts_in_parallel(
            pool_host_ids=seed_row_ids,
            database_url=ctx.dsn,
            identities=ctx.identities,
            eligible_statuses=destroy_eligible_pool_host_statuses(is_leased_destroy_allowed=False),
            is_row_drop_only=False,
            max_concurrency=1,
        )
        undestroyed = undestroyed_pool_host_ids(destroy_outcomes)
        if undestroyed:
            raise CutoverError(
                f"the seed bake for {version_tag} left pool row(s) {', '.join(undestroyed)} on box {server.id} that "
                "could not be destroyed; `minds-admin pool destroy` them before re-running (they hold ports and "
                "budget the restores need)"
            )
    if not cache.has_tar(image_tag):
        raise CutoverError(f"the seed bake did not leave a tar for {image_tag} on box {server.id}")


def ensure_image_tar_published(ctx: CutoverContext, server: BareMetalServer, version_tag: str) -> str:
    """Make ``cutover/images/<tag>.tar.zst`` exist in the bucket, building the tar on ``server`` when needed.

    The tar comes from the existing seed path: a ``pool create --from-tag``
    style bake of one row on the box publishes ``default-workspace-template:<tag>``
    into the box's image cache (the row is destroyed again right after); the
    cached tar is then compressed and streamed to the bucket. Idempotent per tag.
    """
    object_key = cutover_image_object_key(ctx.storage.key_prefix, version_tag)
    if _s3_object_size_or_none(ctx.storage, object_key) is not None:
        logger.info("Image tar for {} already published", version_tag)
        return object_key
    image_tag = f"{DEFAULT_WORKSPACE_TEMPLATE_IMAGE_REPOSITORY}:{version_tag}"
    client = _box_client(ctx, server)
    cache_dir = box_image_cache_dir_for_generation(server.box_generation, box_service_user(server))
    cache = SshBoxImageCache(slice_client=client, cache_dir=cache_dir)
    if not cache.has_tar(image_tag):
        _seed_image_tar_on_box(ctx, server, cache, image_tag=image_tag, version_tag=version_tag)
    transfer_dir = box_transfer_dir(client.box_ssh_user, _IMAGES_TRANSFER_INSTANCE)
    _write_box_file(
        client,
        f"{transfer_dir}/env",
        _transfer_env_text(
            ctx.storage,
            key_prefix=ctx.storage.key_prefix,
            instance_name=_IMAGES_TRANSFER_INSTANCE,
            age_recipient="",
            age_identity="",
        ),
        label="write-images-env",
    )
    try:
        _run_on_box_checked(
            client,
            build_image_publish_command(
                transfer_dir_path=transfer_dir,
                tar_path=f"{cache_dir}/{box_image_tar_name(image_tag)}",
                image_object_key=object_key,
            ),
            timeout=_IMAGE_LOAD_TIMEOUT_SECONDS,
            label=f"publish-image:{version_tag}",
        )
    finally:
        _remove_box_transfer_dirs(client, (transfer_dir,), what=f"the image publish of {version_tag}")
    return object_key


def _load_workspace_image(
    ctx: CutoverContext,
    client: SliceVmClientInterface,
    outer: OuterHostInterface,
    server: BareMetalServer,
    state: CutoverWorkspaceState,
) -> None:
    """Stream the workspace's version tar from the bucket into the new VM's dockerd over the box loopback."""
    if state.target_vm_ssh_port is None:
        raise CutoverError(f"workspace {state.host_db_id} has no reserved target ports")
    target_vm_ssh_port = state.target_vm_ssh_port
    cache = SshBoxImageCache(
        slice_client=client,
        cache_dir=box_image_cache_dir_for_generation(server.box_generation, box_service_user(server)),
    )
    transfer_key = cache.create_transfer_key()
    try:
        _run_on_vm_checked(
            outer,
            f"install -d -m 700 /root/.ssh && printf '%s\\n' {shlex.quote(transfer_key.public_key)} >> /root/.ssh/authorized_keys",
            timeout=_SHORT_TIMEOUT_SECONDS,
            label="authorize-transfer-key",
        )
        _run_on_box_checked(
            client,
            build_image_load_command(
                transfer_dir_path=box_transfer_dir(client.box_ssh_user, state.slice_instance_name),
                image_object_key=cutover_image_object_key(ctx.storage.key_prefix, state.version_tag),
                transfer_key_path=transfer_key.private_key_path_on_box,
                vm_ssh_port=target_vm_ssh_port,
            ),
            timeout=_IMAGE_LOAD_TIMEOUT_SECONDS,
            label=f"load-image:{state.host_id}",
        )
    finally:
        cache.destroy_transfer_key(transfer_key)
        deauthorize = outer.execute_idempotent_command(
            "if [ -f /root/.ssh/authorized_keys ]; then "
            f"grep -vF {shlex.quote(transfer_key.public_key)} /root/.ssh/authorized_keys > /root/.ssh/authorized_keys.tmp "
            "&& mv /root/.ssh/authorized_keys.tmp /root/.ssh/authorized_keys; fi",
            timeout_seconds=_SHORT_TIMEOUT_SECONDS,
        )
        if not deauthorize.success:
            logger.warning(
                "Could not remove the image transfer key from the VM root's authorized_keys of {}: {}",
                state.host_id,
                deauthorize.stderr.strip(),
            )


def _recreate_container(
    ctx: CutoverContext,
    outer: OuterHost,
    state: CutoverWorkspaceState,
    keys: HarvestedKeys,
    inspect_entry: Mapping[str, Any],
    replay_inputs: TemplateReplayInputs,
) -> str:
    """Replay the harvested container on the new VM (volumes, snapshot helper, create, keys, start, home link, autostart)."""
    host_id = HostId(state.host_id)
    host_hex = host_id.get_uuid().hex
    subvolume_path = _GUEST_BTRFS_MOUNT / host_hex
    create_bind_volume_on_outer(outer, volume_name=host_volume_name_for(host_id), device_path=subvolume_path)
    provision_snapshot_helper_on_outer(
        outer,
        ctx.mngr_ctx.concurrency_group,
        host_id=host_id,
        btrfs_mount_path=_GUEST_BTRFS_MOUNT,
        subvolume_path=subvolume_path,
        trigger_volume_name=snapshot_trigger_volume_name_for(host_id),
    )
    container_name = container_name_from_inspect(inspect_entry)
    # A re-run after a failed probe replaces the half-configured container.
    remove_container(outer, container_name, force=True, tolerate_missing=True)
    run_docker(
        outer,
        [
            "create",
            *build_docker_create_args(
                inspect_entry,
                image_tag=f"{DEFAULT_WORKSPACE_TEMPLATE_IMAGE_REPOSITORY}:{state.version_tag}",
                guest_memory_mib=compute_machine_guest_memory_mib(state.memory_units),
            ),
        ],
        timeout_seconds=_SHORT_TIMEOUT_SECONDS,
    )
    # docker cp preserves the staged files' modes; a created-but-not-started
    # container accepts copies into its rootfs. Each container directory is
    # copied as a whole (``<staged>/.``) so a directory the image lacks
    # (``/root/.ssh``) is created rather than refused.
    staging_dir = f"/tmp/mngr-cutover-keys-{host_hex}"
    replayed_files = build_replayed_container_files(
        keys,
        ssh_ca_public_key=_require_ssh_ca_public_key(ctx),
        pool_public_key=ctx.pool_public_key,
    )
    _run_on_vm_checked(
        outer,
        build_stage_replayed_container_files_command(staging_dir, replayed_files),
        timeout=_SHORT_TIMEOUT_SECONDS,
        label="stage-container-files",
    )
    for container_dir in replayed_container_dirs(replayed_files):
        run_docker(
            outer,
            ["cp", f"{staged_container_dir_path(staging_dir, container_dir)}/.", f"{container_name}:{container_dir}"],
        )
    _run_on_vm_checked(outer, f"rm -rf {shlex.quote(staging_dir)}", timeout=_SHORT_TIMEOUT_SECONDS, label="rm-staging")
    start_container(outer, container_name)
    # mngr's container setup, not the image, points the home directory at the
    # volume, so the replay recreates that link.
    exec_in_container(
        outer,
        container_name,
        build_home_volume_symlink_command(replay_inputs.container_home_path, HOST_VOLUME_HOME_PATH),
    )
    start_container_sshd(outer, container_name)
    for index, command in enumerate(replay_inputs.installer_commands):
        _run_on_vm_checked(
            outer,
            f"bash -c {shlex.quote(command)}",
            timeout=_SLOW_COMMAND_TIMEOUT_SECONDS,
            label=f"autostart-installer-{index}",
        )
    return container_name


def _require_ssh_ca_public_key(ctx: CutoverContext) -> str:
    if ctx.ssh_ca_public_key is None:
        raise CutoverError(
            "a gen-2 target trusts the tier's SSH CA for management access, but the activated tier has no [ssh_ca] "
            "block in its deploy.toml; bring the tier's Vault SSH CA up and commit its public key first "
            "(apps/minds/docs/deploy/setup/tier-bringup.md)"
        )
    return ctx.ssh_ca_public_key


def _render_migrate_reserve_script(
    server: BareMetalServer,
    state: CutoverWorkspaceState,
    keys: HarvestedKeys,
    *,
    ssh_ca_public_key: str,
    # The gen-1 pool public key to strip from the harvested VM root keys; the
    # gen-2 target trusts the CA and authorizes no static management key.
    pool_public_key: str,
) -> str:
    """The gen-2 reserve for a migrated workspace: free ports, fresh trixie boot, harvested VM trust material."""
    if (
        server.ram_gb is None
        or server.disk_gb is None
        or server.cpu_threads is None
        or server.cpu_overcommit_ratio is None
    ):
        raise CutoverError(f"box row {server.id} lacks ram_gb / disk_gb / cpu_threads / cpu_overcommit_ratio")
    if state.saved_artifact is None:
        raise CutoverError(f"workspace {state.host_db_id} has no saved artifact; run the stop half first")
    total_units = compute_box_total_units(server.ram_gb)
    unit_budget_mib = compute_box_unit_budget_mib(server.ram_gb)
    disk_budget_gib = compute_gen2_disk_budget_gib(server.disk_gb)
    vcpus = compute_machine_vcpus(server.cpu_threads, server.cpu_overcommit_ratio, state.memory_units, total_units)
    user_data = build_qemu_slice_user_data(
        host_dir=str(_GUEST_BTRFS_MOUNT),
        root_authorized_public_keys=tuple(
            line
            for line in authorized_keys_without(keys.vm_authorized_keys, pool_public_key).splitlines()
            if line.strip() and not line.startswith("#")
        ),
        host_private_key_pem=keys.vm_host_private_key.get_secret_value(),
        host_public_key_openssh=keys.vm_host_public_key,
        trusted_user_ca_public_key=ssh_ca_public_key,
    )
    env_template = build_qemu_slice_env_file(
        instance_name=state.slice_instance_name,
        ordinal=None,
        vcpus=vcpus,
        units=state.memory_units,
        total_units=total_units,
        data_disk_gib=state.migrated_data_disk_gib,
        vm_ssh_host_port=GEN2_VM_SSH_PORT_PLACEHOLDER,
        container_ssh_host_port=GEN2_CONTAINER_SSH_PORT_PLACEHOLDER,
        uplink_mbps=server.uplink_mbps,
    )
    return render_gen2_restore_reserve_script(
        instance_name=state.slice_instance_name,
        units=state.memory_units,
        data_disk_gib=state.migrated_data_disk_gib,
        unit_budget_mib=unit_budget_mib,
        disk_budget_gib=disk_budget_gib,
        required_free_bytes=(GEN2_BOOT_DISK_GIB + state.migrated_data_disk_gib + 2) * 1024**3,
        expected_meta_sha="",
        env_template_b64=base64.b64encode(env_template.encode()).decode(),
        cidata=Gen2SliceCidata(
            user_data=user_data,
            meta_data=build_qemu_slice_meta_data(state.slice_instance_name),
            network_config=build_qemu_slice_network_config(),
        ),
    )


def _reserve_slice_on_target(
    ctx: CutoverContext,
    client: SliceVmClientInterface,
    server: BareMetalServer,
    state: CutoverWorkspaceState,
    keys: HarvestedKeys,
    *,
    transfer_dir: str,
) -> tuple[int, int, int]:
    """Run the reserve on the target box; returns the (vm_port, container_port, ordinal) it claimed."""
    _write_box_file(
        client,
        f"{transfer_dir}/reserve.sh",
        _render_migrate_reserve_script(
            server,
            state,
            keys,
            ssh_ca_public_key=_require_ssh_ca_public_key(ctx),
            pool_public_key=ctx.pool_public_key,
        ),
        label="write-reserve",
    )
    exit_code, reserve_out, reserve_err = client.run_on_box(
        f"bash {shlex.quote(transfer_dir)}/reserve.sh",
        timeout=_RESERVE_TIMEOUT_SECONDS,
        label=f"reserve:{state.host_id}",
    )
    if exit_code != 0:
        if any(marker in reserve_err for marker in _RESERVE_REFUSAL_MARKERS):
            raise CutoverError(f"reserve refused: {reserve_err.strip()}")
        raise CutoverError(f"reserve failed (exit {exit_code}): {reserve_err.strip()}")
    reservation = parse_gen2_restore_reserved_line(reserve_out)
    if reservation is None:
        raise CutoverError(f"reserve printed no reserved marker: {reserve_out[-500:]!r}")
    return reservation


def _fetch_row_or_raise(ctx: CutoverContext, row_id: str) -> CutoverPoolRow:
    with _pool_connection(ctx) as conn:
        row = fetch_pool_row(conn, row_id)
    if row is None:
        raise CutoverError(f"pool row {row_id} is gone")
    return row


@pure
def is_artifact_resave_due(state: CutoverWorkspaceState, row: CutoverPoolRow) -> bool:
    """Whether a stopped, still-unparked row carries a newer stop artifact than the saved copy.

    The 409 guard engages only at the park, so between an earlier run's save
    and its park the owner can start and stop the workspace again; the row's
    artifact generation then moves past the saved rollback copy, and the
    migration must re-save (and transplant) the current disk, never the stale
    copy.
    """
    return (
        state.saved_artifact is not None
        and row.status == "stopped"
        and row.artifact_manifest is not None
        and row.artifact_generation != state.saved_artifact.generation
    )


@pure
def rollback_would_clobber_newer_artifact(saved: SavedProductArtifact, row: CutoverPoolRow) -> bool:
    """Whether the row is an ordinary gen-1 stop carrying a newer artifact than the saved rollback copy.

    The same save-to-park window ``is_artifact_resave_due`` covers: the owner
    ran (and stopped) the workspace after an interrupted migrate's save, so
    the row's own artifact is the current disk. The rollback's park + artifact
    flip would replace it with the stale saved copy, so it must refuse. A
    finalized gen-2 stop (a completed migration the operator is rolling back)
    is not this shape: losing its post-migration artifact is the rollback's
    stated policy.
    """
    return (
        row.box_generation < FIRST_QEMU_BOX_GENERATION
        and row.status == "stopped"
        and row.artifact_manifest is not None
        and row.artifact_generation != saved.generation
    )


@pure
def is_parked_row_shape(row: CutoverPoolRow) -> bool:
    """Whether the row is in the parked-mid-migration shape the connector's 409 guard covers.

    A retention-finalized stop also has no placement but keeps its
    ``artifact_manifest``; only the park's manifest clear engages the
    ``workspace_migrating`` guard, so a finalized stop is not parked (a resume
    that finds one must still run the park CAS).
    """
    return (
        row.status == "stopped"
        and row.bare_metal_server_id is None
        and row.vps_address is None
        and row.artifact_manifest is None
    )


def _require_parked_row_or_none_when_done(ctx: CutoverContext, state: CutoverWorkspaceState) -> CutoverPoolRow | None:
    """The workspace's parked row; None when it is already leased on gen-2 (an earlier run finished it)."""
    row = _fetch_row_or_raise(ctx, state.host_db_id)
    if row.status == "leased" and row.box_generation >= FIRST_QEMU_BOX_GENERATION:
        return None
    if row.status != "stopped" or row.bare_metal_server_id is not None or row.vps_address is not None:
        raise CutoverError(
            f"pool row {state.host_db_id} is {row.status} with a placement; it was never parked (re-run migrate)"
        )
    return row


def _ensure_transplanted_disk(
    ctx: CutoverContext,
    client: SliceVmClientInterface,
    server: BareMetalServer,
    state: CutoverWorkspaceState,
    saved: SavedProductArtifact,
    *,
    transplant_dir: str,
    transfer_dir: str,
) -> None:
    """Leave the rebuilt gen-2 data disk at ``<transplant_dir>/datadisk.qcow2``, transplanting unless a prior attempt did.

    A crashed attempt may have moved the prepared disk into the slice dir, which
    the reserve reclaims with ``rm -rf``: move it back first. The transplant
    itself (root over the management dial) is then skipped when the prepared
    disk is present (the script gives it that name only once it is complete).
    """
    _run_on_box_checked(
        client,
        build_transplant_rescue_command(state.slice_instance_name, transplant_dir),
        timeout=_SLOW_COMMAND_TIMEOUT_SECONDS,
        label="transplant-rescue",
    )
    has_disk_code, _out, _err = client.run_on_box(
        f"test -f {shlex.quote(f'{transplant_dir}/datadisk.qcow2')}",
        timeout=_SHORT_TIMEOUT_SECONDS,
        label="transplant-present",
    )
    if has_disk_code == 0:
        return
    dial = resolve_server_management_dial(server)
    transplant_script = render_gen2_disk_transplant_script(
        transfer_dir_path=transfer_dir,
        transplant_dir_path=transplant_dir,
        host_hex=HostId(state.host_id).get_uuid().hex,
        migrated_data_disk_gib=state.migrated_data_disk_gib,
        expected_datadisk_sha256=saved.datadisk_sha256,
    )
    try:
        stdout = run_root_script_over_ssh(
            dial.host,
            dial.port,
            _MANAGEMENT_SSH_USER,
            ctx.identities.private_key_path_for(server.box_generation),
            transplant_script,
            server.box_host_public_key or "",
            # The transplant moves the same data the product stop uploaded;
            # give it the stop's transfer budget rather than the box prep's.
            run_timeout_seconds=_TRANSFER_WAIT_SECONDS,
        )
    except BareMetalProvisioningError as exc:
        # The helper's messages are worded for the box prep it was written for.
        raise CutoverError(f"disk transplant of {state.host_id} on box {server.id} failed: {exc}") from exc
    if TRANSPLANT_DONE_MARKER not in stdout:
        raise CutoverError(f"disk transplant printed no completion marker: {stdout[-500:]!r}")


_RESERVE_REFUSAL_MARKERS: Final[tuple[str, ...]] = (
    GEN2_NO_UNITS_MARKER,
    GEN2_NO_DISK_MARKER,
    RESTORE_NO_PORTS_MARKER,
    RESTORE_NO_SPACE_MARKER,
)


def _boot_and_replay_workspace(
    ctx: CutoverContext,
    client: SliceVmClientInterface,
    server: BareMetalServer,
    state: CutoverWorkspaceState,
    keys: HarvestedKeys,
    inspect_entry: Mapping[str, Any],
    replay_inputs: TemplateReplayInputs,
    *,
    ordinal: int,
    transplant_dir: str,
) -> None:
    """Materialize the disks, start the unit, then in the VM: load the image, recreate the container, probe it."""
    if state.target_vm_ssh_port is None or state.target_container_ssh_port is None:
        raise CutoverError(f"workspace {state.host_db_id} has no reserved target ports")
    _run_on_box_checked(
        client,
        build_disk_materialize_command(state.slice_instance_name, transplant_dir),
        timeout=_RESERVE_TIMEOUT_SECONDS,
        label="materialize",
    )
    _run_on_box_checked(client, build_unit_enable_command(ordinal), timeout=_SHORT_TIMEOUT_SECONDS, label="enable")
    _run_on_box_checked(
        client, build_qemu_start_command(ordinal), timeout=_SLOW_COMMAND_TIMEOUT_SECONDS, label="start"
    )
    _run_on_box_checked(
        client,
        build_banner_wait_command(state.target_vm_ssh_port, _BANNER_WAIT_SECONDS),
        timeout=_BANNER_WAIT_SECONDS + 60,
        label="vm-banner",
    )
    with vm_root_outer(
        ctx,
        address=str(server.public_address),
        port=state.target_vm_ssh_port,
        host_public_key=keys.vm_host_public_key.strip(),
        box_generation=server.box_generation,
    ) as outer:
        wait_for_guest_cloud_init_to_finish(outer)
        _load_workspace_image(ctx, client, outer, server, state)
        container_name = _recreate_container(ctx, outer, state, keys, inspect_entry, replay_inputs)
        _run_on_box_checked(
            client,
            build_banner_wait_command(state.target_container_ssh_port, _BANNER_WAIT_SECONDS),
            timeout=_BANNER_WAIT_SECONDS + 60,
            label="container-banner",
        )
        warnings = _wait_for_workspace_health(outer, container_name)
    if warnings:
        raise CutoverError("health probe did not converge: " + "; ".join(warnings))


def _finish_restore(
    ctx: CutoverContext, target_server: BareMetalServer, state: CutoverWorkspaceState, keys: HarvestedKeys
) -> None:
    """The final CAS: land the row on leased at the target box's coordinates, refusing when the parked row changed.

    The harvested public keys are stamped onto the row (see
    ``_FINISH_RESTORE_POOL_HOST_SQL`` for why).
    """
    if state.target_vm_ssh_port is None or state.target_container_ssh_port is None:
        raise CutoverError(f"workspace {state.host_db_id} has no reserved target ports")
    with _pool_connection(ctx) as conn:
        is_finished = finish_restore_pool_host(
            conn,
            state.host_db_id,
            vps_address=str(target_server.public_address),
            vm_ssh_port=state.target_vm_ssh_port,
            container_ssh_port=state.target_container_ssh_port,
            server_id=str(target_server.id),
            box_generation=FIRST_QEMU_BOX_GENERATION,
            memory_units=state.memory_units,
            outer_host_public_key=keys.vm_host_public_key.strip(),
            container_host_public_key=keys.container_host_public_key.strip(),
            disk_gb=state.migrated_data_disk_gib,
        )
    if not is_finished:
        raise CutoverError(
            f"final CAS matched no parked row for {state.host_db_id} at disk_gb {state.migrated_data_disk_gib}"
        )


@pure
def target_box_refusal_or_none(server: BareMetalServer | None) -> str | None:
    """Why this box cannot receive migrated workspaces, or None when it is a ready gen-2 box."""
    if server is None or not server.public_address:
        return "no bare_metal_servers row with a public address"
    elif server.box_generation < FIRST_QEMU_BOX_GENERATION:
        return f"generation-{server.box_generation} box; migrations target a ready gen-2 box (repave one first)"
    elif str(server.status) != SERVER_STATUS_READY:
        return f"box is {server.status}, expected ready"
    else:
        return None


def _replay_inputs_for_version(
    ctx: CutoverContext,
    target_server: BareMetalServer,
    version_tag: str,
    cache: dict[str, TemplateReplayInputs],
) -> TemplateReplayInputs:
    """The version's replay inputs, publishing its image tar lazily on first use (idempotent per tag)."""
    if version_tag not in cache:
        ensure_image_tar_published(ctx, target_server, version_tag)
        cache[version_tag] = _read_template_replay_inputs_for_tag(version_tag)
    return cache[version_tag]


def _restore_workspace_on_target(
    ctx: CutoverContext,
    target_server: BareMetalServer,
    state: CutoverWorkspaceState,
    replay_inputs: TemplateReplayInputs,
) -> CutoverWorkspaceState:
    """Transplant -> reserve -> boot -> image -> container -> probe -> CAS on the target box; returns the updated state."""
    saved = state.saved_artifact
    keys = ctx.state.read_keys(state.host_db_id)
    inspect_entry = ctx.state.read_inspect(state.host_db_id)
    if saved is None or keys is None or inspect_entry is None:
        raise CutoverError(f"state dir lacks the saved artifact / keys / inspect for {state.host_db_id}")
    client = _box_client(ctx, target_server)
    transfer_dir = box_transfer_dir(client.box_ssh_user, state.slice_instance_name)
    transplant_dir = cutover_transplant_dir(state.slice_instance_name)
    try:
        _write_box_file(
            client,
            f"{transfer_dir}/env",
            _transfer_env_text(
                ctx.storage,
                key_prefix=saved.key_prefix,
                instance_name=state.slice_instance_name,
                age_recipient=saved.age_recipient,
                age_identity=unwrap_age_identity(ctx.storage, saved.wrapped_dek),
            ),
            label="write-env",
        )
        _ensure_transplanted_disk(
            ctx, client, target_server, state, saved, transplant_dir=transplant_dir, transfer_dir=transfer_dir
        )
        vm_port, container_port, ordinal = _reserve_slice_on_target(
            ctx, client, target_server, state, keys, transfer_dir=transfer_dir
        )
        state = state.model_copy_update(
            to_update(state.field_ref().target_vm_ssh_port, vm_port),
            to_update(state.field_ref().target_container_ssh_port, container_port),
        )
        ctx.state.write_workspace(state)
        _boot_and_replay_workspace(
            ctx,
            client,
            target_server,
            state,
            keys,
            inspect_entry,
            replay_inputs,
            ordinal=ordinal,
            transplant_dir=transplant_dir,
        )
        _finish_restore(ctx, target_server, state, keys)
    except (
        MngrError,
        CutoverError,
        Gen2ScriptError,
        BareMetalProvisioningError,
        ProcessTimeoutError,
        psycopg2.Error,
        OSError,
    ):
        # The staged env holds the unwrapped age identity; a re-run rewrites it,
        # so it must not linger on the box behind a FAILED workspace. The
        # transplant dir stays: the re-run resumes from its prepared disk.
        _remove_box_transfer_dirs(client, (transfer_dir,), what=f"the failed restore of {state.host_id}")
        raise
    _remove_box_transfer_dirs(client, (transfer_dir, transplant_dir), what=f"the restore of {state.host_id}")
    return state


def _migrate_workspace(
    ctx: CutoverContext,
    target_server: BareMetalServer,
    row: CutoverPoolRow,
    *,
    is_keep_origin_vm: bool,
    replay_inputs_cache: dict[str, TemplateReplayInputs],
) -> WorkspaceOutcome:
    """Migrate one workspace onto the target box, resuming from its state file; never raises."""
    try:
        state = ctx.state.read_workspace(row.id)
        if state is not None and state.stage == CutoverStage.RESTORED:
            # Idempotent: covers a crash between the RESTORED write and the
            # shred, which every later re-run resolves down this branch.
            ctx.state.shred_keys(row.id)
            return WorkspaceOutcome(
                host_db_id=row.id, host_id=row.host_id, stage=state.stage, detail="already migrated"
            )
        if state is not None and state.stage == CutoverStage.ROLLED_BACK:
            # A re-migration after a rollback starts from scratch: the row is an
            # ordinary gen-1 workspace again and every recorded coordinate is stale.
            state = None
        if state is not None and state.target_server_id != str(target_server.id):
            raise CutoverError(
                f"workspace {row.id} is mid-migration onto box {state.target_server_id}; "
                "finish it there (or roll it back) instead of retargeting"
            )
        target_client = _box_client(ctx, target_server)
        if state is None:
            capacity_error = _target_capacity_error_or_none(ctx, target_server, target_client)
            if capacity_error is not None:
                raise CutoverError(capacity_error)
            fresh = _ensure_workspace_running(ctx, _fetch_row_or_raise(ctx, row.id))
            if fresh.slice_instance_name is not None:
                # A rolled-back earlier migration may have left its prepared
                # disk in the target's transplant dir, which the transplant
                # would silently reuse (stale pre-rollback data). Cleared
                # before any state is recorded, so a failure here is retried.
                _run_on_box_checked(
                    target_client,
                    build_transplant_clear_command(cutover_transplant_dir(fresh.slice_instance_name)),
                    timeout=_SHORT_TIMEOUT_SECONDS,
                    label=f"clear-stale-transplant:{fresh.host_id}",
                )
            if fresh.bare_metal_server_id is None:
                raise CutoverError(f"row {row.id} is leased but has no box; cannot harvest")
            with _pool_connection(ctx) as conn:
                origin_server = fetch_server_by_id(conn, BareMetalServerDbId(fresh.bare_metal_server_id))
            if origin_server is None or not origin_server.public_address:
                raise CutoverError(f"origin box {fresh.bare_metal_server_id} of row {row.id} is unreachable")
            origin_client = _box_client(ctx, origin_server)
            state, keys, inspect_entry = _harvest_workspace(
                ctx, origin_client, origin_server, fresh, target_server_id=str(target_server.id)
            )
            ctx.state.write_keys(row.id, keys)
            ctx.state.write_inspect(row.id, inspect_entry)
            state = state.model_copy_update(to_update(state.field_ref().is_origin_vm_kept, is_keep_origin_vm))
            ctx.state.write_workspace(state)
        fresh = _fetch_row_or_raise(ctx, row.id)
        # A row leased on gen-1 with an artifact already saved means the owner
        # restarted the workspace after an earlier run's stop: the saved copy
        # is stale, so the stop half re-runs and re-saves over it -- the
        # migration must move the current disk, never the stale artifact. The
        # same applies when the owner already stopped it again: the row's
        # artifact generation has moved past the saved copy.
        is_running_on_gen1 = fresh.status == "leased" and fresh.box_generation < FIRST_QEMU_BOX_GENERATION
        if state.saved_artifact is None or is_running_on_gen1 or is_artifact_resave_due(state, fresh):
            if is_running_on_gen1:
                fresh = _stop_workspace_via_product(ctx, row.id)
            elif fresh.status == "stopped":
                # A crash between an earlier run's stop and its artifact save:
                # the row already carries the manifest to save.
                pass
            else:
                raise CutoverError(f"row {row.id} is {fresh.status}; cannot stop it for migration")
            saved = _save_product_artifact(ctx, fresh)
            state = state.model_copy_update(
                to_update(state.field_ref().saved_artifact, saved),
                to_update(state.field_ref().stage, CutoverStage.STOPPED),
                to_update(state.field_ref().last_error, None),
            )
            ctx.state.write_workspace(state)
        current = _fetch_row_or_raise(ctx, row.id)
        is_already_migrated = current.status == "leased" and current.box_generation >= FIRST_QEMU_BOX_GENERATION
        if not is_already_migrated and not is_parked_row_shape(current):
            # Reached both on the normal path (right after the save) and on a
            # resume that crashed between the save and the park.
            with _pool_connection(ctx) as conn:
                is_parked = park_pool_host(conn, row.id)
            if not is_parked:
                raise CutoverError(f"park CAS matched no row for {row.id} (status changed underneath the migrate)")
        if state.stage in (CutoverStage.STOPPED, CutoverStage.HARVESTED):
            state = state.model_copy_update(to_update(state.field_ref().stage, CutoverStage.PARKED))
            ctx.state.write_workspace(state)
        parked_row = _require_parked_row_or_none_when_done(ctx, state)
        if parked_row is not None:
            replay_inputs = _replay_inputs_for_version(ctx, target_server, state.version_tag, replay_inputs_cache)
            state = _restore_workspace_on_target(ctx, target_server, state, replay_inputs)
        if not state.is_origin_vm_kept:
            try:
                _destroy_gen1_instance_on_box(ctx, state.origin_server_id, state.slice_instance_name)
            except (
                MngrError,
                CutoverError,
                BareMetalProvisioningError,
                ProcessTimeoutError,
                psycopg2.Error,
                OSError,
            ) as exc:
                # The halted origin VM is rowless on its box now, so the bake's
                # orphan reap will collect it; the migration itself is done.
                logger.warning(
                    "Could not destroy the origin VM {} on box {}: {} (the orphan reap will collect it)",
                    state.slice_instance_name,
                    state.origin_server_id,
                    exc,
                )
        state = state.model_copy_update(
            to_update(state.field_ref().stage, CutoverStage.RESTORED),
            to_update(state.field_ref().last_error, None),
        )
        ctx.state.write_workspace(state)
        # Shredding here (not inside the restore) covers every completion
        # path: a crash between the finish CAS and the shred resumes down the
        # already-migrated branch, which skips the restore entirely.
        ctx.state.shred_keys(row.id)
        detail = (
            f"migrated to {target_server.public_address}:{state.target_vm_ssh_port}/{state.target_container_ssh_port}"
        )
        if state.is_origin_vm_kept:
            detail += "; origin VM kept (finalize it by hand before baking on its box)"
        return WorkspaceOutcome(host_db_id=row.id, host_id=row.host_id, stage=CutoverStage.RESTORED, detail=detail)
    except (
        MngrError,
        CutoverError,
        Gen2ScriptError,
        BareMetalProvisioningError,
        ProcessTimeoutError,
        psycopg2.Error,
        OSError,
        click.ClickException,
    ) as exc:
        logger.warning("Migration of {} failed: {}", row.id, exc)
        failed_state = ctx.state.read_workspace(row.id)
        # A terminal record stays terminal: a re-migration of a ROLLED_BACK
        # workspace that dies before writing its own HARVESTED state (and a
        # post-completion failure, e.g. in the shred) would otherwise
        # resurrect the record as in-flight -- refusing retargets against its
        # stale target box and blocking repaves of both recorded boxes.
        if failed_state is not None and _is_migration_state_in_flight(failed_state):
            ctx.state.write_workspace(
                failed_state.model_copy_update(
                    to_update(failed_state.field_ref().stage, CutoverStage.FAILED),
                    to_update(failed_state.field_ref().last_error, str(exc)),
                )
            )
        return WorkspaceOutcome(host_db_id=row.id, host_id=row.host_id, stage=CutoverStage.FAILED, detail=str(exc))


def _resolve_user_id_prefix(ctx: CutoverContext, email: str) -> str:
    """The 16-hex lease-namespace prefix of an account, resolved through the admin accounts API."""
    client, admin_key = _admin_client(ctx)
    account = client.admin_get_account(admin_key, email)
    return str(account.user_id).replace("-", "")[:16]


def resolve_migration_rows(
    ctx: CutoverContext,
    *,
    workspace_ids: Sequence[str],
    user_email: str | None,
    source_server_id: str | None,
) -> tuple[list[CutoverPoolRow], list[WorkspaceOutcome]]:
    """The selected candidate rows (in selection order, deduplicated) plus outcomes for the unmigratable ones.

    An explicitly named ``--workspace`` id must exist (a missing one is an
    error); ``--user`` and ``--source-server-id`` sweep whatever gen-1 rows
    they find, reporting the unmigratable ones (unleased rows to destroy,
    rows wedged mid-transition) instead of silently skipping them.
    """
    user_prefix = _resolve_user_id_prefix(ctx, user_email) if user_email is not None else None
    rows_by_id: dict[str, CutoverPoolRow] = {}
    with _pool_connection(ctx) as conn:
        for workspace_id in workspace_ids:
            row = fetch_pool_row(conn, workspace_id)
            if row is None:
                raise CutoverError(f"--workspace {workspace_id}: no pool row")
            rows_by_id.setdefault(row.id, row)
        if user_prefix is not None:
            for row in fetch_gen1_pool_rows_for_user(conn, user_prefix):
                rows_by_id.setdefault(row.id, row)
        if source_server_id is not None:
            for row in fetch_pool_rows_on_server(conn, BareMetalServerDbId(source_server_id)):
                rows_by_id.setdefault(row.id, row)
    migrated_row_ids: set[str] = set()
    for row in rows_by_id.values():
        if row.box_generation >= FIRST_QEMU_BOX_GENERATION:
            state = ctx.state.read_workspace(row.id)
            if state is not None and state.stage == CutoverStage.RESTORED:
                migrated_row_ids.add(row.id)
    return partition_migration_rows(list(rows_by_id.values()), migrated_row_ids)


@pure
def partition_migration_rows(
    rows: Sequence[CutoverPoolRow], migrated_row_ids: AbstractSet[str]
) -> tuple[list[CutoverPoolRow], list[WorkspaceOutcome]]:
    """Split the selected rows into migration candidates and per-row outcomes for the rest.

    ``migrated_row_ids`` names the gen-2 rows this tooling's state dir records
    as RESTORED (its own completed migrations, reported without failing the
    invocation); any other gen-2 row is not a gen-1 workspace and fails.
    """
    candidates: list[CutoverPoolRow] = []
    unmigratable: list[WorkspaceOutcome] = []
    for row in rows:
        if row.box_generation >= FIRST_QEMU_BOX_GENERATION:
            unmigratable.append(
                WorkspaceOutcome(
                    host_db_id=row.id, host_id=row.host_id, stage=CutoverStage.RESTORED, detail="already migrated"
                )
                if row.id in migrated_row_ids
                else WorkspaceOutcome(
                    host_db_id=row.id,
                    host_id=row.host_id,
                    stage=CutoverStage.FAILED,
                    detail="already on gen-2 (not a gen-1 workspace)",
                )
            )
            continue
        classification = classify_pool_row(row.status, row.bare_metal_server_id)
        if classification.verdict == RowVerdict.CANDIDATE:
            candidates.append(row)
        elif classification.verdict == RowVerdict.DESTROY:
            unmigratable.append(
                WorkspaceOutcome(
                    host_db_id=row.id,
                    host_id=row.host_id,
                    stage=CutoverStage.FAILED,
                    detail=f"unleased ({row.status}) row: destroy it (`minds-admin pool destroy {row.id}`)",
                )
            )
        else:
            unmigratable.append(
                WorkspaceOutcome(
                    host_db_id=row.id,
                    host_id=row.host_id,
                    stage=CutoverStage.FAILED,
                    detail=classification.remedy or f"cannot migrate a {row.status} row",
                )
            )
    return candidates, unmigratable


def run_migrate(
    ctx: CutoverContext,
    *,
    target_server_id: str,
    workspace_ids: Sequence[str],
    user_email: str | None,
    source_server_id: str | None,
    is_keep_origin_vm: bool,
    is_publish_image_tars: bool,
    is_dry_run: bool,
) -> StageReport:
    """Migrate the selected gen-1 workspaces onto one gen-2 target box, sequentially, stopping on the first failure.

    Parallelism is running several invocations with disjoint target boxes: the
    per-target-box and per-workspace locks refuse an overlap instead of racing
    it (the target box is also the serialization domain of its management dial).
    """
    if source_server_id is not None and source_server_id == target_server_id:
        raise CutoverError("--source-server-id equals --target-server-id; a migration moves between boxes")
    with _pool_connection(ctx) as conn:
        target_server = fetch_server_by_id(conn, BareMetalServerDbId(target_server_id))
    target_refusal = target_box_refusal_or_none(target_server)
    if target_refusal is not None:
        raise CutoverError(f"--target-server-id {target_server_id}: {target_refusal}")
    assert target_server is not None
    candidates, unmigratable = resolve_migration_rows(
        ctx, workspace_ids=workspace_ids, user_email=user_email, source_server_id=source_server_id
    )
    if not candidates and not unmigratable:
        raise CutoverError("the --workspace/--user/--source-server-id selectors matched no pool rows")
    if is_dry_run:
        planned = [
            WorkspaceOutcome(
                host_db_id=row.id,
                host_id=row.host_id,
                stage=CutoverStage.RESTORED,
                detail=(
                    f"would harvest, product-stop, park, transplant onto box {target_server_id} at fresh ports, "
                    f"replay, re-lease ({row.status} row{'; admin-start first' if row.status != 'leased' else ''})"
                ),
            )
            for row in candidates
        ]
        return StageReport(
            stage_name="migrate",
            env_name=ctx.env_name,
            is_dry_run=True,
            boxes=(
                BoxOutcome(
                    server_id=target_server_id,
                    stage=CutoverBoxStage.FINISHED,
                    workspaces=tuple([*planned, *unmigratable]),
                    detail="dry run",
                ),
            ),
        )
    outcomes: list[WorkspaceOutcome] = []
    lock_names = [f"box-{target_server_id}", *(f"workspace-{row.id}" for row in candidates)]
    with ctx.state.acquire_locks(lock_names):
        if is_publish_image_tars:
            # Pre-warm from the best version signal available without touching
            # the workspaces (the lazy per-workspace publish is authoritative:
            # a self-updated workspace's real version is read at harvest). A
            # baked_version can be a dev branch, which no migration can use
            # (the harvest refuses non-release describes), so only release
            # tags are worth a seed bake.
            prewarm_tags: set[str] = set()
            for candidate in candidates:
                recorded = ctx.state.read_workspace(candidate.id)
                version_signal = recorded.version_tag if recorded is not None else candidate.baked_version
                if version_signal is not None and parse_version_tag(version_signal) is not None:
                    prewarm_tags.add(version_signal)
            for version_tag in sorted(prewarm_tags):
                ensure_image_tar_published(ctx, target_server, version_tag)
        replay_inputs_cache: dict[str, TemplateReplayInputs] = {}
        for index, row in enumerate(candidates):
            outcome = _migrate_workspace(
                ctx,
                target_server,
                row,
                is_keep_origin_vm=is_keep_origin_vm,
                replay_inputs_cache=replay_inputs_cache,
            )
            outcomes.append(outcome)
            if outcome.stage == CutoverStage.FAILED:
                outcomes.extend(
                    WorkspaceOutcome(
                        host_db_id=remaining.id,
                        host_id=remaining.host_id,
                        stage=CutoverStage.FAILED,
                        detail="not attempted (an earlier workspace failed; fix it and re-run)",
                    )
                    for remaining in candidates[index + 1 :]
                )
                break
    all_outcomes = [*outcomes, *unmigratable]
    is_all_migrated = all(outcome.stage == CutoverStage.RESTORED for outcome in all_outcomes)
    return StageReport(
        stage_name="migrate",
        env_name=ctx.env_name,
        is_dry_run=False,
        boxes=(
            BoxOutcome(
                server_id=target_server_id,
                stage=CutoverBoxStage.FINISHED if is_all_migrated else None,
                workspaces=tuple(all_outcomes),
            ),
        ),
    )


def _rollback_workspace(ctx: CutoverContext, state: CutoverWorkspaceState) -> WorkspaceOutcome:
    """Roll one migrated workspace back onto gen-1 through the product's own restore; never raises."""
    try:
        if state.stage == CutoverStage.ROLLED_BACK:
            # Terminal, like the migrate treats it: the row may since have run
            # and stopped again on gen-1 with a newer artifact (post-rollback
            # work), which a re-run's park + artifact flip would clobber with
            # the stale saved copy. The shred is the only idempotent cleanup
            # left (a crash between the ROLLED_BACK write and the shred).
            ctx.state.shred_keys(state.host_db_id)
            return WorkspaceOutcome(
                host_db_id=state.host_db_id,
                host_id=state.host_id,
                stage=CutoverStage.ROLLED_BACK,
                detail="already rolled back",
            )
        saved = state.saved_artifact
        if saved is None:
            raise CutoverError(f"workspace {state.host_db_id} has no saved artifact; nothing to roll back to")
        row = _fetch_row_or_raise(ctx, state.host_db_id)
        if row.status == "leased" and row.box_generation < FIRST_QEMU_BOX_GENERATION:
            # The product restore already brought it back (a crash after the
            # start but before the state write, or a plain re-run).
            outcome_detail = f"already back on gen-1 at {row.vps_address}:{row.ssh_port}"
        else:
            if rollback_would_clobber_newer_artifact(saved, row):
                raise CutoverError(
                    f"row {state.host_db_id} is a stopped gen-1 row whose own artifact (generation "
                    f"{row.artifact_generation}) is not the saved copy (generation {saved.generation}): "
                    "the owner ran the workspace after the migrate's save, and rolling back would clobber "
                    "that work with the stale copy; re-run migrate (it re-saves the current artifact) or "
                    "leave the row as it is"
                )
            with _pool_connection(ctx) as conn:
                is_parked = rollback_park_pool_host(conn, state.host_db_id, memory_units=DEFAULT_MACHINE_UNITS)
            if not is_parked:
                raise CutoverError(
                    f"rollback park CAS matched no row for {state.host_db_id} (status {row.status}); "
                    "only stopped or leased-on-gen-2 rows roll back"
                )
            # The gen-2 slice (when the restore half got that far) and the
            # origin leftover (when --keep-origin-vm skipped its deletion) must
            # both be gone before the product restore reserves gen-1 ports and
            # recreates the instance under the same name.
            _destroy_gen2_slice_on_box(ctx, state.target_server_id, state.slice_instance_name)
            _destroy_gen1_instance_on_box(ctx, state.origin_server_id, state.slice_instance_name)
            with _pool_connection(ctx) as conn:
                is_artifact_restored = rollback_restore_artifact(
                    conn,
                    state.host_db_id,
                    artifact_manifest_json=json.dumps(saved.manifest_json),
                    wrapped_dek=saved.wrapped_dek,
                    artifact_generation=saved.generation,
                )
            if not is_artifact_restored:
                raise CutoverError(f"rollback artifact CAS matched no parked gen-1 row for {state.host_db_id}")
            client, admin_key = _admin_client(ctx)
            client.admin_start_workspace(admin_key, state.host_db_id)
            row = _await_row_status(
                ctx,
                state.host_db_id,
                success_statuses=("leased",),
                failure_statuses=("crashed",),
                timeout_seconds=_TRANSFER_WAIT_SECONDS,
                what="the rollback's gen-1 restore",
            )
            outcome_detail = f"back on gen-1 at {row.vps_address}:{row.ssh_port}"
        ctx.state.write_workspace(
            state.model_copy_update(
                to_update(state.field_ref().stage, CutoverStage.ROLLED_BACK),
                to_update(state.field_ref().last_error, None),
            )
        )
        ctx.state.shred_keys(state.host_db_id)
        return WorkspaceOutcome(
            host_db_id=state.host_db_id,
            host_id=state.host_id,
            stage=CutoverStage.ROLLED_BACK,
            detail=outcome_detail,
        )
    except (
        MngrError,
        CutoverError,
        BareMetalProvisioningError,
        ProcessTimeoutError,
        psycopg2.Error,
        OSError,
        click.ClickException,
    ) as exc:
        logger.warning("Rollback of {} failed: {}", state.host_db_id, exc)
        failed_state = ctx.state.read_workspace(state.host_db_id)
        # A completed rollback stays terminal: a failure after the ROLLED_BACK
        # write (e.g. in the shred) must not resurrect the record as in-flight
        # -- its stale coordinates would refuse re-migration retargets and
        # block repaves of both recorded boxes. A RESTORED record does flip to
        # FAILED: a mid-rollback failure has already parked the row, so the
        # repave/preflight guards must see the migration as in flight.
        if failed_state is not None and failed_state.stage != CutoverStage.ROLLED_BACK:
            ctx.state.write_workspace(
                failed_state.model_copy_update(
                    to_update(failed_state.field_ref().stage, CutoverStage.FAILED),
                    to_update(failed_state.field_ref().last_error, str(exc)),
                )
            )
        return WorkspaceOutcome(
            host_db_id=state.host_db_id, host_id=state.host_id, stage=CutoverStage.FAILED, detail=str(exc)
        )


def run_rollback(ctx: CutoverContext, *, host_db_id: str) -> StageReport:
    """Roll one migrated workspace back to gen-1 and wait for the product restore to re-lease it."""
    recorded = ctx.state.read_workspace(host_db_id)
    if recorded is None:
        raise CutoverError(
            f"no migration record for {host_db_id} in the state dir; only workspaces migrated by this "
            "tooling (from this operator machine) can be rolled back"
        )
    with ctx.state.acquire_locks([f"box-{recorded.target_server_id}", f"workspace-{host_db_id}"]):
        # The pre-lock read only named the locks; a migrate finishing in
        # between may have rewritten the record, so the rollback acts on what
        # is under the locks (and refuses when it now targets another box --
        # this invocation holds the wrong box lock).
        state = ctx.state.read_workspace(host_db_id)
        if state is None:
            raise CutoverError(f"the migration record for {host_db_id} vanished while acquiring the locks")
        if state.target_server_id != recorded.target_server_id:
            raise CutoverError(
                f"workspace {host_db_id} was re-migrated onto box {state.target_server_id} "
                "while this rollback started; re-run it"
            )
        outcome = _rollback_workspace(ctx, state)
    return StageReport(
        stage_name="rollback",
        env_name=ctx.env_name,
        is_dry_run=False,
        boxes=(
            BoxOutcome(
                server_id=state.target_server_id,
                stage=CutoverBoxStage.FINISHED if outcome.stage == CutoverStage.ROLLED_BACK else None,
                workspaces=(outcome,),
            ),
        ),
    )


@pure
def render_stage_table(report: StageReport) -> str:
    rows = []
    for box in report.boxes:
        rows.append(
            [box.server_id[:8], "box", str(box.stage) if box.stage is not None else "FAILED", box.detail or "-"]
        )
        for workspace in box.workspaces:
            rows.append([box.server_id[:8], workspace.host_id, str(workspace.stage), workspace.detail or "-"])
    table = tabulate(rows, headers=["BOX", "ITEM", "STAGE", "DETAIL"], tablefmt="plain")
    prefix = "DRY RUN -- " if report.is_dry_run else ""
    return f"{prefix}{report.stage_name} ({report.env_name}): {report.failed_count} failed\n{table}"
