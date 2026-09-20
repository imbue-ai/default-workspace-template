"""``minds-admin archives ...`` -- archive the workspaces the gen-2 migration cannot take, for their owners.

A workspace below the cutover's version floor (or with no ``minds-v*``
release tag at all) is archived into the tier's workspace-storage bucket as
one zip (``create``), retired once the archive has been checked
(``minds-admin workspaces retire``), and released once its owner is confirmed
covered. ``candidates`` lists the gen-1 rows with their verdicts; ``list``,
``links`` and ``download`` work from the manifests in the bucket, so they run
from any operator machine. Runbook: apps/minds/docs/deploy/gen2-cutover.md.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Final
from uuid import UUID

import click
from loguru import logger
from pydantic import SecretStr

from imbue.minds.envs.paths import env_root_dir
from imbue.minds.envs.primitives import DevEnvName
from imbue.minds_admin.cli._activated_env import require_activated_env_name
from imbue.minds_admin.cli._tier_secrets import DATABASE_URL_HELP
from imbue.minds_admin.cli._tier_secrets import WorkspaceStorageConfig
from imbue.minds_admin.cli._tier_secrets import make_workspace_storage_s3_client
from imbue.minds_admin.cli._tier_secrets import resolve_admin_api_key_value
from imbue.minds_admin.cli._tier_secrets import resolve_admin_connector_url
from imbue.minds_admin.cli._tier_secrets import resolve_pool_database_url
from imbue.minds_admin.cli._tier_secrets import resolve_pool_private_key_pem
from imbue.minds_admin.cli._tier_secrets import resolve_supertokens_core_credentials
from imbue.minds_admin.cli._tier_secrets import resolve_workspace_storage_config
from imbue.minds_admin.cli.archive_drivers import ArchiveContext
from imbue.minds_admin.cli.archive_drivers import ArchiveLink
from imbue.minds_admin.cli.archive_drivers import download_archive
from imbue.minds_admin.cli.archive_drivers import latest_manifest_by_workspace
from imbue.minds_admin.cli.archive_drivers import list_bucket_archives
from imbue.minds_admin.cli.archive_drivers import manifest_summary_payload
from imbue.minds_admin.cli.archive_drivers import presigned_archive_url
from imbue.minds_admin.cli.archive_drivers import render_candidates_table
from imbue.minds_admin.cli.archive_drivers import render_links_csv
from imbue.minds_admin.cli.archive_drivers import render_links_markdown
from imbue.minds_admin.cli.archive_drivers import render_manifests_table
from imbue.minds_admin.cli.archive_drivers import run_archive_candidates
from imbue.minds_admin.cli.archive_drivers import run_archive_create
from imbue.minds_admin.cli.cutover import immediate_sigint_termination
from imbue.minds_admin.cli.cutover import require_tier_confirmation
from imbue.minds_admin.cli.cutover import tier_confirmation_options
from imbue.minds_admin.cli.cutover_drivers import minimal_mngr_context
from imbue.minds_admin.cli.server import box_management_identities
from imbue.minds_admin.envs.r2_cleanup import collect_owner_emails_by_prefix
from imbue.minds_admin.slices.archive_state import ARCHIVE_STATE_DIRNAME
from imbue.minds_admin.slices.archive_state import ArchiveStateStore
from imbue.minds_admin.slices.archive_types import DEFAULT_ARCHIVE_EXCLUDES
from imbue.minds_admin.slices.archive_types import DEFAULT_PRESIGNED_LINK_SECONDS
from imbue.minds_admin.slices.archive_types import WorkspaceArchiveManifest
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr_imbue_cloud.cli._common import emit_json

_SECONDS_PER_DAY: Final[int] = 24 * 3600


@click.group(name="archives")
def archives() -> None:
    """Archive the workspaces the gen-2 migration cannot take, and hand their owners download links."""


def _archive_state_store(env_name: str) -> ArchiveStateStore:
    state = ArchiveStateStore(root=env_root_dir(DevEnvName(env_name)) / ARCHIVE_STATE_DIRNAME)
    state.ensure_layout()
    return state


@contextmanager
def _archive_context(
    database_url: str | None, *, is_connector_needed: bool, is_owner_lookup: bool
) -> Iterator[ArchiveContext]:
    env_name = require_activated_env_name()
    dsn = resolve_pool_database_url(database_url)
    pem = resolve_pool_private_key_pem()
    storage = resolve_workspace_storage_config()
    connector_url = resolve_admin_connector_url(None) if is_connector_needed else None
    admin_api_key = SecretStr(resolve_admin_api_key_value(None)) if is_connector_needed else None
    # Resolved before any box is touched, so a core the operator cannot reach
    # refuses the command up front rather than after a long stream.
    owner_email_by_prefix = (
        collect_owner_emails_by_prefix(resolve_supertokens_core_credentials()) if is_owner_lookup else None
    )
    state = _archive_state_store(env_name)
    with (
        immediate_sigint_termination(),
        box_management_identities(resolve_gen1_pool_private_key_pem=lambda: pem) as identities,
        minimal_mngr_context(state.root / "mngr-profile") as mngr_ctx,
    ):
        yield ArchiveContext(
            env_name=env_name,
            dsn=dsn,
            identities=identities,
            mngr_ctx=mngr_ctx,
            connector_url=connector_url,
            admin_api_key=admin_api_key,
            storage=storage,
            state=state,
            owner_email_by_prefix=owner_email_by_prefix,
        )


@archives.command(name="candidates")
@click.option(
    "--live-probe/--no-live-probe",
    "is_live_probe",
    default=True,
    help="Read each running workspace's version off its container (the authoritative verdict); off reads bake versions only.",
)
@click.option("--database-url", default=None, help=DATABASE_URL_HELP)
def candidates(is_live_probe: bool, database_url: str | None) -> None:
    """Read-only: every gen-1 pool row of the env with its verdict (RETIRE / MIGRATE / UNKNOWN_UNTIL_STARTED / SKIP).

    RETIRE rows are the ones to archive and retire: their version is below
    the cutover floor or names no release tag. A stopped row baked below the
    floor is UNKNOWN_UNTIL_STARTED (it may have updated itself): archive it
    with --start-stopped, whose manifest records the live version.
    """
    with _archive_context(database_url, is_connector_needed=False, is_owner_lookup=False) as ctx:
        found = run_archive_candidates(ctx, is_live_probe=is_live_probe)
    write_human_line(render_candidates_table(found))
    emit_json([candidate.model_dump(mode="json") for candidate in found])


@archives.command(name="create")
@click.option("--workspace", "host_db_id", required=True, type=click.UUID, help="The pool_hosts row id to archive.")
@click.option(
    "--start-stopped",
    "is_start_allowed",
    is_flag=True,
    default=False,
    help="Admin-start a stopped workspace for the archive (it is left running afterwards; retire it separately).",
)
@click.option(
    "--owner-email",
    default=None,
    help="Record this owner email instead of resolving it from the SuperTokens core.",
)
@click.option(
    "--skip-owner-lookup",
    "is_owner_lookup_skipped",
    is_flag=True,
    default=False,
    help="Record no owner email (the row's user prefix is always recorded).",
)
@click.option(
    "--volume-path",
    default=None,
    help="The VM path of the workspace's volume subvolume, when the docker volume lookup cannot find it.",
)
@click.option(
    "--exclude",
    "excludes",
    multiple=True,
    help="Glob to leave out of the zip (repeatable); default: the regenerable caches host_backup excludes.",
)
@click.option("--database-url", default=None, help=DATABASE_URL_HELP)
@tier_confirmation_options
def create(
    host_db_id: UUID,
    is_start_allowed: bool,
    owner_email: str | None,
    is_owner_lookup_skipped: bool,
    volume_path: str | None,
    excludes: tuple[str, ...],
    database_url: str | None,
    is_production_confirmed: bool,
    is_staging_confirmed: bool,
    is_dev_confirmed: bool,
) -> None:
    """Archive one workspace into the tier bucket: a zip of its volume and its container's writable layer, plus a manifest.

    The workspace is read, not changed (its volume is read from a read-only
    btrfs snapshot taken for the run); only --start-stopped changes anything,
    and only by starting the workspace. Check the archive (`archives list`,
    `archives download`) before `minds-admin workspaces retire`.
    """
    require_tier_confirmation(
        require_activated_env_name(),
        is_production_confirmed=is_production_confirmed,
        is_staging_confirmed=is_staging_confirmed,
        is_dev_confirmed=is_dev_confirmed,
    )
    is_owner_lookup = owner_email is None and not is_owner_lookup_skipped
    with _archive_context(database_url, is_connector_needed=is_start_allowed, is_owner_lookup=is_owner_lookup) as ctx:
        manifest = run_archive_create(
            ctx,
            host_db_id=str(host_db_id),
            is_start_allowed=is_start_allowed,
            excludes=tuple(excludes) or DEFAULT_ARCHIVE_EXCLUDES,
            volume_path_override=volume_path,
            owner_email_override=owner_email,
        )
    write_human_line(render_manifests_table([manifest]))
    emit_json(manifest_summary_payload(manifest))
    logger.info("Archived {} to s3://{}/{}", manifest.host_id, manifest.bucket, manifest.zip_key)


def _bucket_access() -> tuple[str, WorkspaceStorageConfig, Any]:
    """The read-only commands' prologue: the activated env's name, its workspace-storage bucket and a client on it."""
    env_name = require_activated_env_name()
    storage = resolve_workspace_storage_config()
    return env_name, storage, make_workspace_storage_s3_client(storage)


def _bucket_manifests(
    s3_client: Any, host_db_id: UUID | None, storage: WorkspaceStorageConfig
) -> list[WorkspaceArchiveManifest]:
    every_manifest = list_bucket_archives(s3_client, storage, None)
    if host_db_id is None:
        return every_manifest
    return [manifest for manifest in every_manifest if manifest.host_db_id == str(host_db_id)]


@archives.command(name="list")
@click.option("--workspace", "host_db_id", default=None, type=click.UUID, help="Only this pool_hosts row's archives.")
def list_archives(host_db_id: UUID | None) -> None:
    """Every archive in the tier bucket (read from the manifests beside the zips), oldest first."""
    _env_name, storage, s3_client = _bucket_access()
    manifests = _bucket_manifests(s3_client, host_db_id, storage)
    write_human_line(render_manifests_table(manifests))
    emit_json([manifest_summary_payload(manifest) for manifest in manifests])


@archives.command(name="links")
@click.option(
    "--workspace",
    "host_db_ids",
    multiple=True,
    type=click.UUID,
    help="A pool_hosts row id to link (repeatable); default: every archived workspace.",
)
@click.option(
    "--expires-days",
    type=click.FloatRange(min=0.0, min_open=True, max=7.0),
    default=DEFAULT_PRESIGNED_LINK_SECONDS / _SECONDS_PER_DAY,
    show_default=True,
    help="How long the links last (at most 7 days, the presigning ceiling).",
)
def links(host_db_ids: tuple[UUID, ...], expires_days: float) -> None:
    """Mint time-limited download links for the newest archive of each workspace, as a support document.

    Writes `reports/archive-links-<stamp>.md` and `.csv` to the env's archive
    state dir (`~/.minds-<env>/archives/`) and prints the markdown.
    """
    env_name, storage, s3_client = _bucket_access()
    wanted = {str(host_db_id) for host_db_id in host_db_ids}
    manifests = [
        manifest
        for manifest in latest_manifest_by_workspace(list_bucket_archives(s3_client, storage, None))
        if not wanted or manifest.host_db_id in wanted
    ]
    missing = wanted - {manifest.host_db_id for manifest in manifests}
    if missing:
        raise click.ClickException(f"no archive exists for workspace(s) {', '.join(sorted(missing))}")
    expires_seconds = int(expires_days * _SECONDS_PER_DAY)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_seconds)
    minted = [
        ArchiveLink(
            manifest=manifest,
            url=presigned_archive_url(s3_client, manifest, expires_seconds=expires_seconds),
            expires_at=expires_at,
        )
        for manifest in manifests
    ]
    markdown = render_links_markdown(minted, env_name=env_name)
    paths = _archive_state_store(env_name).write_report(
        "archive-links", (("md", markdown), ("csv", render_links_csv(minted)))
    )
    write_human_line(markdown)
    for path in paths:
        logger.info("Wrote {}", path)


@archives.command(name="download")
@click.option("--workspace", "host_db_id", required=True, type=click.UUID, help="The pool_hosts row id.")
@click.option(
    "--out",
    "out_dir",
    required=True,
    type=click.Path(file_okay=False, path_type=Path),
    help="Directory the zip is downloaded into.",
)
def download(host_db_id: UUID, out_dir: Path) -> None:
    """Download the newest archive of one workspace (to inspect it before retiring the workspace)."""
    _env_name, storage, s3_client = _bucket_access()
    manifests = latest_manifest_by_workspace(_bucket_manifests(s3_client, host_db_id, storage))
    if not manifests:
        raise click.ClickException(f"no archive exists for workspace {host_db_id}")
    target = download_archive(s3_client, manifests[0], out_dir)
    write_human_line(str(target))
    emit_json({"path": str(target), **manifest_summary_payload(manifests[0])})
