"""The workspace archive drivers: candidates, create, list, links, download (``minds-admin archives``).

An archive reads a running workspace without changing it: the VM root takes
a read-only btrfs snapshot of the workspace's volume, streams that snapshot
and the container's writable layer as one zip through the box's loopback SSH
into the tier's workspace-storage bucket, and drops the snapshot again. The
manifest written beside the zip (and into the operator's state dir) is what
``list`` / ``links`` / ``download`` work from. Retiring the workspace
afterwards is a separate, deliberate step (``minds-admin workspaces retire``).
"""

import csv
import io
import posixpath
import shlex
from collections.abc import Iterator
from collections.abc import Mapping
from collections.abc import Sequence
from contextlib import contextmanager
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Final

import psycopg2
from botocore.exceptions import BotoCoreError
from botocore.exceptions import ClientError
from loguru import logger
from pydantic import Field
from pydantic import ValidationError
from tabulate import tabulate

from imbue.concurrency_group.errors import ProcessTimeoutError
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.pure import pure
from imbue.minds_admin.cli._tier_secrets import WorkspaceStorageConfig
from imbue.minds_admin.cli._tier_secrets import make_workspace_storage_s3_client
from imbue.minds_admin.cli.cutover_drivers import OperatorBoxContext
from imbue.minds_admin.cli.cutover_drivers import box_client_for_server
from imbue.minds_admin.cli.cutover_drivers import box_transfer_dir
from imbue.minds_admin.cli.cutover_drivers import container_id_on_vm
from imbue.minds_admin.cli.cutover_drivers import ensure_workspace_running
from imbue.minds_admin.cli.cutover_drivers import fetch_row_or_raise
from imbue.minds_admin.cli.cutover_drivers import pool_connection
from imbue.minds_admin.cli.cutover_drivers import remove_box_transfer_dirs
from imbue.minds_admin.cli.cutover_drivers import run_on_vm_checked
from imbue.minds_admin.cli.cutover_drivers import served_vm_host_key
from imbue.minds_admin.cli.cutover_drivers import transfer_env_text
from imbue.minds_admin.cli.cutover_drivers import vm_root_outer
from imbue.minds_admin.cli.cutover_drivers import vm_transfer_key
from imbue.minds_admin.cli.cutover_drivers import write_box_file
from imbue.minds_admin.slices.archive_scripts import BOX_ARCHIVE_SCRIPT_FILENAME
from imbue.minds_admin.slices.archive_scripts import VM_ARCHIVE_README_PATH
from imbue.minds_admin.slices.archive_scripts import VM_ARCHIVE_SCRIPT_PATH
from imbue.minds_admin.slices.archive_scripts import archive_snapshot_path
from imbue.minds_admin.slices.archive_scripts import build_container_layer_path_command
from imbue.minds_admin.slices.archive_scripts import build_snapshot_create_command
from imbue.minds_admin.slices.archive_scripts import build_snapshot_delete_command
from imbue.minds_admin.slices.archive_scripts import build_vm_archive_cleanup_command
from imbue.minds_admin.slices.archive_scripts import build_vm_stream_command
from imbue.minds_admin.slices.archive_scripts import build_volume_device_path_command
from imbue.minds_admin.slices.archive_scripts import load_archive_stream_script
from imbue.minds_admin.slices.archive_scripts import render_archive_readme
from imbue.minds_admin.slices.archive_scripts import render_box_archive_upload_script
from imbue.minds_admin.slices.archive_state import ArchiveStateStore
from imbue.minds_admin.slices.archive_types import ARCHIVE_CONTAINER_LAYER_PREFIX
from imbue.minds_admin.slices.archive_types import ARCHIVE_MANIFEST_FILENAME
from imbue.minds_admin.slices.archive_types import ARCHIVE_STAMP_FORMAT
from imbue.minds_admin.slices.archive_types import ARCHIVE_VOLUME_PREFIX
from imbue.minds_admin.slices.archive_types import ArchiveCandidate
from imbue.minds_admin.slices.archive_types import ArchiveError
from imbue.minds_admin.slices.archive_types import ArchiveStreamSummary
from imbue.minds_admin.slices.archive_types import MAX_PRESIGNED_LINK_SECONDS
from imbue.minds_admin.slices.archive_types import RetireVerdict
from imbue.minds_admin.slices.archive_types import WorkspaceArchiveManifest
from imbue.minds_admin.slices.archive_types import WorkspaceArchiveSource
from imbue.minds_admin.slices.archive_types import archive_manifest_key
from imbue.minds_admin.slices.archive_types import archive_object_prefix
from imbue.minds_admin.slices.archive_types import archive_zip_key
from imbue.minds_admin.slices.archive_types import archives_listing_prefix
from imbue.minds_admin.slices.archive_types import classify_retire_verdict
from imbue.minds_admin.slices.archive_types import parse_archive_stream_summary
from imbue.minds_admin.slices.archive_types import parse_archive_upload_output
from imbue.minds_admin.slices.archive_types import render_manifest_json
from imbue.minds_admin.slices.bare_metal_db import fetch_server_by_id
from imbue.minds_admin.slices.cutover_db import CutoverPoolRow
from imbue.minds_admin.slices.cutover_db import fetch_gen1_servers
from imbue.minds_admin.slices.cutover_db import fetch_pool_rows_on_server
from imbue.minds_admin.slices.cutover_db import fetch_unplaced_gen1_pool_rows
from imbue.minds_admin.slices.cutover_scripts import build_docker_inspect_command
from imbue.minds_admin.slices.cutover_scripts import build_git_describe_command
from imbue.minds_admin.slices.cutover_scripts import build_home_layout_probe_command
from imbue.minds_admin.slices.cutover_scripts import container_name_from_inspect
from imbue.minds_admin.slices.cutover_scripts import parse_docker_inspect
from imbue.minds_admin.slices.cutover_types import CutoverError
from imbue.mngr.cli.output_helpers import format_size
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.primitives import HostId
from imbue.mngr_imbue_cloud.data_types import BareMetalServer
from imbue.mngr_imbue_cloud.errors import BareMetalProvisioningError
from imbue.mngr_imbue_cloud.interfaces import SliceVmClientInterface
from imbue.mngr_imbue_cloud.primitives import BareMetalServerDbId
from imbue.mngr_vps.container_setup import host_volume_name_for

_SHORT_TIMEOUT_SECONDS: Final[float] = 120.0
_SNAPSHOT_TIMEOUT_SECONDS: Final[float] = 300.0
# The whole stream: a 28 GiB workspace at the box's uplink is tens of minutes.
_UPLOAD_TIMEOUT_SECONDS: Final[float] = 6000.0
_VM_SCRIPT_MODE: Final[str] = "0700"
_VM_README_MODE: Final[str] = "0600"
# The ways a live version probe can fail; a candidates run records them in
# the row's reason rather than aborting the whole listing.
_LIVE_PROBE_FAILURE_EXCEPTIONS: Final[tuple[type[Exception], ...]] = (
    MngrError,
    CutoverError,
    ArchiveError,
    BareMetalProvisioningError,
    ProcessTimeoutError,
    psycopg2.Error,
    OSError,
)


class ArchiveContext(OperatorBoxContext):
    """Everything the archive commands need from the activated env, resolved once per command."""

    storage: WorkspaceStorageConfig = Field(description="The tier's workspace-storage bucket")
    state: ArchiveStateStore = Field(description="The state dir")
    owner_email_by_prefix: Mapping[str, str] | None = Field(
        default=None, description="Every account's email by its 16-hex prefix, when the lookup ran"
    )


def _server_for_row(ctx: OperatorBoxContext, row: CutoverPoolRow) -> BareMetalServer:
    if row.bare_metal_server_id is None:
        raise ArchiveError(f"row {row.id} is {row.status} but sits on no box")
    with pool_connection(ctx) as conn:
        server = fetch_server_by_id(conn, BareMetalServerDbId(row.bare_metal_server_id))
    if server is None or not server.public_address:
        raise ArchiveError(f"the box {row.bare_metal_server_id} of row {row.id} has no public address")
    return server


def _version_probe(outer: OuterHostInterface, container_id: str) -> str:
    """The container's version probe output: '' when it names no version (the probe script never fails on that); raises when the probe cannot run in the container."""
    result = outer.execute_idempotent_command(
        build_git_describe_command(container_id), timeout_seconds=_SHORT_TIMEOUT_SECONDS
    )
    if not result.success:
        raise ArchiveError(f"the version probe failed inside the container: {result.stderr.strip()[:200]}")
    return result.stdout.strip()


def _probe_live_version(ctx: OperatorBoxContext, row: CutoverPoolRow) -> str:
    server = _server_for_row(ctx, row)
    vm_port, served_key = served_vm_host_key(server, row)
    with vm_root_outer(
        ctx,
        address=str(server.public_address),
        port=vm_port,
        host_public_key=served_key,
        box_generation=server.box_generation,
    ) as outer:
        return _version_probe(outer, container_id_on_vm(outer, row.host_id))


def _candidate(ctx: OperatorBoxContext, row: CutoverPoolRow, *, is_live_probe: bool) -> ArchiveCandidate:
    live_version: str | None = None
    probe_failure: str | None = None
    if is_live_probe and row.status == "leased":
        try:
            live_version = _probe_live_version(ctx, row)
        except _LIVE_PROBE_FAILURE_EXCEPTIONS as exc:
            logger.warning("Live version probe of row {} failed: {}", row.id, exc)
            probe_failure = str(exc)
    verdict, verdict_reason = classify_retire_verdict(row.status, row.baked_version, live_version)
    reason = verdict_reason if probe_failure is None else f"{verdict_reason} (live probe failed: {probe_failure})"
    return ArchiveCandidate(
        host_db_id=row.id,
        host_id=row.host_id,
        host_name=row.host_name,
        leased_to_user=row.leased_to_user,
        status=row.status,
        baked_version=row.baked_version,
        live_version=live_version,
        verdict=verdict,
        reason=reason,
    )


def run_archive_candidates(ctx: OperatorBoxContext, *, is_live_probe: bool) -> list[ArchiveCandidate]:
    """Every gen-1 pool row of the env with its retire verdict (running rows probed live when asked)."""
    with pool_connection(ctx) as conn:
        rows: list[CutoverPoolRow] = []
        for server in fetch_gen1_servers(conn):
            rows.extend(fetch_pool_rows_on_server(conn, server.id))
        rows.extend(fetch_unplaced_gen1_pool_rows(conn))
    return [_candidate(ctx, row, is_live_probe=is_live_probe) for row in rows]


@pure
def render_candidates_table(candidates: Sequence[ArchiveCandidate]) -> str:
    rows = [
        [
            candidate.host_db_id,
            candidate.host_name,
            candidate.leased_to_user or "-",
            candidate.status,
            candidate.baked_version or "-",
            candidate.live_version if candidate.live_version is not None else "-",
            str(candidate.verdict),
            candidate.reason,
        ]
        for candidate in candidates
    ]
    table = tabulate(
        rows, headers=["ROW", "NAME", "USER", "STATUS", "BAKED", "LIVE", "VERDICT", "REASON"], tablefmt="plain"
    )
    retire_count = sum(1 for candidate in candidates if candidate.verdict == RetireVerdict.RETIRE)
    unknown_count = sum(1 for candidate in candidates if candidate.verdict == RetireVerdict.UNKNOWN_UNTIL_STARTED)
    return f"{table}\n\nretire {retire_count}; unknown until started {unknown_count}; total {len(candidates)}"


def _read_archive_source(
    outer: OuterHostInterface, row: CutoverPoolRow, *, volume_path_override: str | None
) -> WorkspaceArchiveSource:
    """What the archive reads off the running workspace: its container, its volume's VM path, its writable layer."""
    container_id = container_id_on_vm(outer, row.host_id)
    inspect_entry = parse_docker_inspect(
        run_on_vm_checked(
            outer, build_docker_inspect_command(container_id), timeout=_SHORT_TIMEOUT_SECONDS, label="inspect"
        )
    )
    container_layer_path = run_on_vm_checked(
        outer, build_container_layer_path_command(container_id), timeout=_SHORT_TIMEOUT_SECONDS, label="upper-dir"
    ).strip()
    if volume_path_override is not None:
        volume_subvolume_path = volume_path_override
    else:
        volume_subvolume_path = run_on_vm_checked(
            outer,
            build_volume_device_path_command(host_volume_name_for(HostId(row.host_id))),
            timeout=_SHORT_TIMEOUT_SECONDS,
            label="volume-device",
        ).strip()
    for label, path, remedy in (
        ("container layer", container_layer_path, "is the container's root an overlay mount?"),
        ("volume", volume_subvolume_path, "pass --volume-path"),
    ):
        if not path.startswith("/"):
            raise ArchiveError(f"the workspace's {label} path is not absolute: {path!r} ({remedy})")
        run_on_vm_checked(
            outer, f"test -d {shlex.quote(path)}", timeout=_SHORT_TIMEOUT_SECONDS, label=f"{label}-exists"
        )
    layout = run_on_vm_checked(
        outer, build_home_layout_probe_command(container_id), timeout=_SHORT_TIMEOUT_SECONDS, label="home-layout"
    ).strip()
    return WorkspaceArchiveSource(
        container_id=container_id,
        container_name=container_name_from_inspect(inspect_entry),
        volume_subvolume_path=volume_subvolume_path,
        container_layer_path=container_layer_path,
        home_layout=layout,
        version_probe=_version_probe(outer, container_id),
    )


def _cleanup_vm_after_archive(outer: OuterHostInterface, snapshot_path: str, *, what: str) -> None:
    """Drop the snapshot and the run's files on the VM; failures are logged, never raised (nothing user-facing depends on them)."""
    for label, command in (
        ("snapshot", build_snapshot_delete_command(snapshot_path)),
        ("files", build_vm_archive_cleanup_command()),
    ):
        result = outer.execute_idempotent_command(command, timeout_seconds=_SNAPSHOT_TIMEOUT_SECONDS)
        if not result.success:
            logger.warning("Could not remove the archive {} of {} on the VM: {}", label, what, result.stderr.strip())


def _stream_archive_to_bucket(
    ctx: ArchiveContext,
    client: SliceVmClientInterface,
    outer: OuterHostInterface,
    server: BareMetalServer,
    row: CutoverPoolRow,
    *,
    vm_ssh_port: int,
    vm_command: str,
    object_prefix: str,
) -> tuple[str, int, str]:
    """Run the box-side upload of the VM's zip stream; returns ``(sha256, size, the run's stderr)``."""
    transfer_dir = box_transfer_dir(client.box_ssh_user, f"archive-{row.host_id}")
    script_path = f"{transfer_dir}/{BOX_ARCHIVE_SCRIPT_FILENAME}"
    try:
        with vm_transfer_key(client, outer, server, what=f"the archive of {row.host_id}") as transfer_key:
            write_box_file(
                client,
                f"{transfer_dir}/env",
                transfer_env_text(
                    ctx.storage,
                    key_prefix=object_prefix,
                    instance_name=f"archive-{row.host_id}",
                    age_recipient="",
                    age_identity="",
                ),
                label="write-env",
            )
            write_box_file(
                client,
                script_path,
                render_box_archive_upload_script(
                    transfer_dir_path=transfer_dir,
                    transfer_key_path=transfer_key.private_key_path_on_box,
                    vm_ssh_port=vm_ssh_port,
                    vm_command=vm_command,
                    zip_object_key=archive_zip_key(object_prefix),
                ),
                label="write-archive-script",
            )
            exit_code, stdout, stderr = client.run_on_box(
                f"bash {shlex.quote(script_path)}", timeout=_UPLOAD_TIMEOUT_SECONDS, label=f"archive:{row.host_id}"
            )
    finally:
        remove_box_transfer_dirs(client, (transfer_dir,), what=f"the archive of {row.host_id}")
    if exit_code != 0:
        raise ArchiveError(f"the archive upload failed (exit {exit_code}): {stderr.strip()[-2000:]}")
    sha256, size = parse_archive_upload_output(stdout)
    return sha256, size, stderr


def _uploaded_object_size(s3_client: Any, bucket: str, key: str) -> int:
    try:
        response = s3_client.head_object(Bucket=bucket, Key=key)
    except (ClientError, BotoCoreError) as exc:
        raise ArchiveError(f"could not head the uploaded zip s3://{bucket}/{key}: {exc}") from exc
    return int(response["ContentLength"])


def _delete_object_best_effort(s3_client: Any, bucket: str, key: str, *, what: str) -> None:
    """Remove an object a failed run left behind; a failure here is logged, never raised (it must not mask the run's own error)."""
    try:
        s3_client.delete_object(Bucket=bucket, Key=key)
    except (ClientError, BotoCoreError) as exc:
        logger.warning("Could not remove the {} s3://{}/{} left by the failed archive: {}", what, bucket, key, exc)


@contextmanager
def zip_object_removed_unless_completed(s3_client: Any, bucket: str, zip_key: str) -> Iterator[None]:
    """Remove the run's zip object unless the block completes.

    A stream that dies part-way still lands a zip: ``s5cmd pipe`` completes
    its upload on EOF, and only ``pipefail`` reports the failure afterwards.
    Each run uses a fresh prefix and the manifest is written last, so a zip
    left behind by a failed run would sit in the bucket unlisted for good.
    """
    is_completed = False
    try:
        yield
        is_completed = True
    finally:
        if not is_completed:
            _delete_object_best_effort(s3_client, bucket, zip_key, what="zip")


def _put_manifest(s3_client: Any, manifest: WorkspaceArchiveManifest) -> None:
    try:
        s3_client.put_object(
            Bucket=manifest.bucket,
            Key=manifest.manifest_key,
            Body=render_manifest_json(manifest).encode("utf-8"),
            ContentType="application/json",
        )
    except (ClientError, BotoCoreError) as exc:
        raise ArchiveError(
            f"could not write the manifest s3://{manifest.bucket}/{manifest.manifest_key}: {exc}"
        ) from exc


def _owner_email_for(ctx: ArchiveContext, row: CutoverPoolRow, owner_email_override: str | None) -> str | None:
    if owner_email_override is not None:
        return owner_email_override
    if ctx.owner_email_by_prefix is None or row.leased_to_user is None:
        return None
    email = ctx.owner_email_by_prefix.get(row.leased_to_user)
    if email is None:
        raise ArchiveError(
            f"the SuperTokens core lists no email for an account with prefix {row.leased_to_user} "
            "(the account is gone, or has no email); pass --owner-email to record the owner by hand, "
            "or --skip-owner-lookup"
        )
    return email


def _snapshot_and_stream(
    ctx: ArchiveContext,
    client: SliceVmClientInterface,
    outer: OuterHostInterface,
    server: BareMetalServer,
    row: CutoverPoolRow,
    *,
    source: WorkspaceArchiveSource,
    created_at: datetime,
    owner_email: str | None,
    excludes: Sequence[str],
    vm_ssh_port: int,
    object_prefix: str,
) -> tuple[str, int, str]:
    """The VM-side stage: stage the streamer and README, snapshot the volume, stream the zip, then clean the VM up.

    Returns ``(sha256, size, the stream's stderr)``; the snapshot and the
    staged files are removed whether or not the stream succeeded.
    """
    snapshot_path = archive_snapshot_path(source.volume_subvolume_path, created_at.strftime(ARCHIVE_STAMP_FORMAT))
    readme = render_archive_readme(
        host_name=row.host_name,
        host_id=row.host_id,
        owner_email=owner_email,
        created_at_text=created_at.isoformat(),
        home_layout=source.home_layout,
        version_probe=source.version_probe,
        excludes=excludes,
    )
    vm_command = build_vm_stream_command(
        {ARCHIVE_VOLUME_PREFIX: snapshot_path, ARCHIVE_CONTAINER_LAYER_PREFIX: source.container_layer_path},
        excludes,
        is_readme_included=True,
    )
    try:
        outer.write_file(
            Path(VM_ARCHIVE_SCRIPT_PATH), load_archive_stream_script().encode("utf-8"), mode=_VM_SCRIPT_MODE
        )
        outer.write_file(Path(VM_ARCHIVE_README_PATH), readme.encode("utf-8"), mode=_VM_README_MODE)
        run_on_vm_checked(
            outer,
            build_snapshot_create_command(source.volume_subvolume_path, snapshot_path),
            timeout=_SNAPSHOT_TIMEOUT_SECONDS,
            label="snapshot",
        )
        with log_span("Streaming the archive of {} into s3://{}/{}", row.host_id, ctx.storage.bucket, object_prefix):
            return _stream_archive_to_bucket(
                ctx,
                client,
                outer,
                server,
                row,
                vm_ssh_port=vm_ssh_port,
                vm_command=vm_command,
                object_prefix=object_prefix,
            )
    finally:
        _cleanup_vm_after_archive(outer, snapshot_path, what=row.host_id)


def _manifest_for(
    ctx: ArchiveContext,
    row: CutoverPoolRow,
    running: CutoverPoolRow,
    *,
    source: WorkspaceArchiveSource,
    created_at: datetime,
    owner_email: str | None,
    object_prefix: str,
    sha256: str,
    size: int,
    excludes: Sequence[str],
    summary: ArchiveStreamSummary,
) -> WorkspaceArchiveManifest:
    """The manifest of a streamed archive (``row`` is the row as the command found it, ``running`` as it was archived)."""
    return WorkspaceArchiveManifest(
        env_name=ctx.env_name,
        host_db_id=running.id,
        host_id=running.host_id,
        agent_id=running.agent_id,
        host_name=running.host_name,
        owner_user_id_prefix=running.leased_to_user,
        owner_email=owner_email,
        created_at=created_at,
        workspace_status_before_archive=row.status,
        box_generation=running.box_generation,
        version_probe=source.version_probe,
        baked_version=running.baked_version,
        home_layout=source.home_layout,
        volume_subvolume_path=source.volume_subvolume_path,
        container_layer_path=source.container_layer_path,
        bucket=ctx.storage.bucket,
        zip_key=archive_zip_key(object_prefix),
        manifest_key=archive_manifest_key(object_prefix),
        zip_size_bytes=size,
        zip_sha256=sha256,
        excludes=tuple(excludes),
        summary=summary,
    )


def run_archive_create(
    ctx: ArchiveContext,
    *,
    host_db_id: str,
    is_start_allowed: bool,
    excludes: Sequence[str],
    volume_path_override: str | None,
    owner_email_override: str | None,
) -> WorkspaceArchiveManifest:
    """Archive one workspace into the tier bucket and return its manifest.

    The workspace is read, not changed: a running one keeps running (its
    volume is read from a read-only snapshot taken for the duration); a
    stopped one is admin-started first when ``is_start_allowed`` and left
    running afterwards.
    """
    with ctx.state.acquire_workspace_lock(host_db_id):
        row = fetch_row_or_raise(ctx, host_db_id)
        if row.status == "stopped" and not is_start_allowed:
            raise ArchiveError(f"row {row.id} is stopped; pass --start-stopped to admin-start it for the archive")
        owner_email = _owner_email_for(ctx, row, owner_email_override)
        running = ensure_workspace_running(ctx, row)
        server = _server_for_row(ctx, running)
        vm_port, served_key = served_vm_host_key(server, running)
        client = box_client_for_server(ctx, server)
        created_at = datetime.now(timezone.utc)
        object_prefix = archive_object_prefix(ctx.storage.key_prefix, running.host_id, created_at)
        zip_key = archive_zip_key(object_prefix)
        s3_client = make_workspace_storage_s3_client(ctx.storage)
        with vm_root_outer(
            ctx,
            address=str(server.public_address),
            port=vm_port,
            host_public_key=served_key,
            box_generation=server.box_generation,
        ) as outer:
            source = _read_archive_source(outer, running, volume_path_override=volume_path_override)
            with zip_object_removed_unless_completed(s3_client, ctx.storage.bucket, zip_key):
                sha256, size, stream_stderr = _snapshot_and_stream(
                    ctx,
                    client,
                    outer,
                    server,
                    running,
                    source=source,
                    created_at=created_at,
                    owner_email=owner_email,
                    excludes=excludes,
                    vm_ssh_port=vm_port,
                    object_prefix=object_prefix,
                )
                summary = parse_archive_stream_summary(stream_stderr)
                uploaded_size = _uploaded_object_size(s3_client, ctx.storage.bucket, zip_key)
                if uploaded_size != size:
                    raise ArchiveError(f"the uploaded zip is {uploaded_size} bytes but the stream measured {size}")
                manifest = _manifest_for(
                    ctx,
                    row,
                    running,
                    source=source,
                    created_at=created_at,
                    owner_email=owner_email,
                    object_prefix=object_prefix,
                    sha256=sha256,
                    size=size,
                    excludes=excludes,
                    summary=summary,
                )
                _put_manifest(s3_client, manifest)
        ctx.state.write_manifest(manifest)
        return manifest


def _manifest_keys_under(s3_client: Any, storage: WorkspaceStorageConfig, host_id: str | None) -> list[str]:
    """The manifest object keys under the env's archives prefix (or one workspace's)."""
    prefix = archives_listing_prefix(storage.key_prefix, host_id)
    try:
        pages = list(s3_client.get_paginator("list_objects_v2").paginate(Bucket=storage.bucket, Prefix=prefix))
    except (ClientError, BotoCoreError) as exc:
        raise ArchiveError(f"could not list the archives in s3://{storage.bucket}/{prefix}: {exc}") from exc
    return [
        str(entry["Key"])
        for page in pages
        for entry in page.get("Contents", [])
        if posixpath.basename(str(entry["Key"])) == ARCHIVE_MANIFEST_FILENAME
    ]


def _read_manifest(s3_client: Any, bucket: str, key: str) -> WorkspaceArchiveManifest:
    try:
        body = s3_client.get_object(Bucket=bucket, Key=key)["Body"].read()
    except (ClientError, BotoCoreError) as exc:
        raise ArchiveError(f"could not read the manifest s3://{bucket}/{key}: {exc}") from exc
    try:
        return WorkspaceArchiveManifest.model_validate_json(body)
    except ValidationError as exc:
        raise ArchiveError(f"the manifest s3://{bucket}/{key} is not one this build can read: {exc}") from exc


def list_bucket_archives(
    s3_client: Any, storage: WorkspaceStorageConfig, host_id: str | None
) -> list[WorkspaceArchiveManifest]:
    """Every archive in the bucket (of one workspace, or of the env), read from the manifests beside the zips."""
    manifests = [
        _read_manifest(s3_client, storage.bucket, key) for key in _manifest_keys_under(s3_client, storage, host_id)
    ]
    return sorted(manifests, key=lambda manifest: (manifest.host_id, manifest.created_at))


def presigned_archive_url(s3_client: Any, manifest: WorkspaceArchiveManifest, *, expires_seconds: int) -> str:
    """A time-limited download URL for the archive's zip (at most seven days, the SigV4 ceiling)."""
    if not 0 < expires_seconds <= MAX_PRESIGNED_LINK_SECONDS:
        raise ArchiveError(f"a presigned link lasts 1..{MAX_PRESIGNED_LINK_SECONDS} seconds, not {expires_seconds}")
    try:
        return str(
            s3_client.generate_presigned_url(
                "get_object",
                Params={
                    "Bucket": manifest.bucket,
                    "Key": manifest.zip_key,
                    "ResponseContentDisposition": f'attachment; filename="{manifest.host_name}-archive.zip"',
                },
                ExpiresIn=expires_seconds,
            )
        )
    except (ClientError, BotoCoreError) as exc:
        raise ArchiveError(f"could not presign s3://{manifest.bucket}/{manifest.zip_key}: {exc}") from exc


def download_archive(s3_client: Any, manifest: WorkspaceArchiveManifest, out_dir: Path) -> Path:
    """Download the archive's zip to ``out_dir`` (named by workspace and stamp); returns the local path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{manifest.host_name}-{manifest.created_at.strftime(ARCHIVE_STAMP_FORMAT)}.zip"
    try:
        s3_client.download_file(manifest.bucket, manifest.zip_key, str(target))
    except (ClientError, BotoCoreError) as exc:
        raise ArchiveError(f"could not download s3://{manifest.bucket}/{manifest.zip_key}: {exc}") from exc
    return target


class ArchiveLink(FrozenModel):
    """One archive with the download link minted for it."""

    manifest: WorkspaceArchiveManifest = Field(description="The archive")
    url: str = Field(description="The presigned download URL")
    expires_at: datetime = Field(description="When the URL stops working")


@pure
def latest_manifest_by_workspace(manifests: Sequence[WorkspaceArchiveManifest]) -> list[WorkspaceArchiveManifest]:
    """The newest archive of each workspace, ordered by owner then workspace name."""
    latest_by_host_db_id: dict[str, WorkspaceArchiveManifest] = {}
    for manifest in manifests:
        current = latest_by_host_db_id.get(manifest.host_db_id)
        if current is None or manifest.created_at > current.created_at:
            latest_by_host_db_id[manifest.host_db_id] = manifest
    return sorted(
        latest_by_host_db_id.values(),
        key=lambda manifest: (manifest.owner_email or "", manifest.host_name, manifest.host_id),
    )


@pure
def render_links_markdown(links: Sequence[ArchiveLink], *, env_name: str) -> str:
    """The support document: one row per archived workspace with its owner, size, checksum and link."""
    lines = [
        f"# Archived workspaces ({env_name})",
        "",
        "Each link downloads the workspace's archive (a zip) and expires at the time shown. "
        "Mint a fresh one with `minds-admin archives links --workspace <row id>`.",
        "",
        "| Owner | Workspace | Row id | Taken | Size | SHA-256 | Link expires | Link |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for link in links:
        manifest = link.manifest
        lines.append(
            f"| {manifest.owner_email or manifest.owner_user_id_prefix or '-'} | {manifest.host_name} "
            f"| `{manifest.host_db_id}` | {manifest.created_at.isoformat(timespec='seconds')} "
            f"| {format_size(manifest.zip_size_bytes)} | `{manifest.zip_sha256}` "
            f"| {link.expires_at.isoformat(timespec='seconds')} | {link.url} |"
        )
    return "\n".join(lines) + "\n"


@pure
def render_links_csv(links: Sequence[ArchiveLink]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "owner_email",
            "workspace_name",
            "host_db_id",
            "host_id",
            "created_at",
            "size_bytes",
            "sha256",
            "expires_at",
            "url",
        ]
    )
    for link in links:
        manifest = link.manifest
        writer.writerow(
            [
                manifest.owner_email or "",
                manifest.host_name,
                manifest.host_db_id,
                manifest.host_id,
                manifest.created_at.isoformat(),
                manifest.zip_size_bytes,
                manifest.zip_sha256,
                link.expires_at.isoformat(),
                link.url,
            ]
        )
    return buffer.getvalue()


@pure
def render_manifests_table(manifests: Sequence[WorkspaceArchiveManifest]) -> str:
    rows = [
        [
            manifest.host_db_id,
            manifest.host_name,
            manifest.owner_email or manifest.owner_user_id_prefix or "-",
            manifest.created_at.isoformat(timespec="seconds"),
            manifest.version_probe or "-",
            format_size(manifest.zip_size_bytes),
            manifest.summary.entry_count,
            manifest.summary.skipped_count,
            f"s3://{manifest.bucket}/{manifest.zip_key}",
        ]
        for manifest in manifests
    ]
    return tabulate(
        rows,
        headers=["ROW", "NAME", "OWNER", "TAKEN", "VERSION", "SIZE", "ENTRIES", "SKIPPED", "OBJECT"],
        tablefmt="plain",
    )


@pure
def manifest_summary_payload(manifest: WorkspaceArchiveManifest) -> dict[str, Any]:
    return manifest.model_dump(mode="json")
