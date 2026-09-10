"""``minds-admin cutover ...`` -- the incremental gen-1 -> gen-2 workspace migration.

Four commands, each re-runnable from the state dir (``~/.minds-<env>/cutover/``):
``preflight`` (read-only inventory), ``migrate`` (workspaces onto one gen-2
target box), ``rollback`` (one migrated workspace back to gen-1) and ``repave``
(an emptied box reinstalled as gen-2). The mutating commands require the
tier's ``--yes-i-mean-<tier>`` flag. Runbook: apps/minds/docs/deploy/gen2-cutover.md;
design: blueprint/slice-fleet-cutover/phase-5.5-incremental-rollout.md. Deleted
wholesale in phase 6.
"""

from collections.abc import Callable
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

import click
from loguru import logger
from pydantic import SecretStr

from imbue.imbue_common.pure import pure
from imbue.minds.envs.paths import env_root_dir
from imbue.minds.envs.primitives import DevEnvName
from imbue.minds_admin.cli._activated_env import require_activated_env_name
from imbue.minds_admin.cli._activated_env import tier_for_env_name
from imbue.minds_admin.cli._tier_secrets import DATABASE_URL_HELP
from imbue.minds_admin.cli._tier_secrets import resolve_admin_api_key_value
from imbue.minds_admin.cli._tier_secrets import resolve_admin_connector_url
from imbue.minds_admin.cli._tier_secrets import resolve_pool_database_url
from imbue.minds_admin.cli._tier_secrets import resolve_pool_private_key_pem
from imbue.minds_admin.cli._tier_secrets import resolve_workspace_storage_config
from imbue.minds_admin.cli.cutover_drivers import CutoverContext
from imbue.minds_admin.cli.cutover_drivers import minimal_mngr_context
from imbue.minds_admin.cli.cutover_drivers import render_preflight_table
from imbue.minds_admin.cli.cutover_drivers import render_stage_table
from imbue.minds_admin.cli.cutover_drivers import run_migrate
from imbue.minds_admin.cli.cutover_drivers import run_preflight
from imbue.minds_admin.cli.cutover_drivers import run_repave
from imbue.minds_admin.cli.cutover_drivers import run_rollback
from imbue.minds_admin.cli.server import box_management_identities
from imbue.minds_admin.cli.server import derive_ssh_public_key
from imbue.minds_admin.cli.server import resolve_tier_ssh_ca_public_key_or_none
from imbue.minds_admin.slices.cutover_state import CUTOVER_STATE_DIRNAME
from imbue.minds_admin.slices.cutover_state import CutoverStateStore
from imbue.minds_admin.slices.cutover_types import StageReport
from imbue.minds_admin.slices.operator_identity import pool_private_key_path
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr_imbue_cloud.cli._common import emit_json
from imbue.mngr_imbue_cloud.primitives import DEV_TIER
from imbue.mngr_imbue_cloud.primitives import PRODUCTION_TIER
from imbue.mngr_imbue_cloud.primitives import STAGING_TIER


@click.group(name="cutover")
def cutover() -> None:
    """Incremental gen-1 -> gen-2 workspace migration for the activated env (preflight / migrate / rollback / repave)."""


# The tiers that have a ``--yes-i-mean-<tier>`` flag; every other tier (``ci``)
# is guarded by the dev flag.
_CONFIRMATION_TIERS: Final[tuple[str, ...]] = (PRODUCTION_TIER, STAGING_TIER, DEV_TIER)


@pure
def confirmation_tier_for_env_name(env_name: str) -> str:
    """The tier whose ``--yes-i-mean-<tier>`` flag a mutating stage on ``env_name`` requires."""
    tier = tier_for_env_name(env_name)
    return tier if tier in _CONFIRMATION_TIERS else DEV_TIER


def tier_confirmation_options(func: Callable[..., None]) -> Callable[..., None]:
    """Attach the three ``--yes-i-mean-<tier>`` flags; exactly the activated tier's must be passed."""
    for tier in _CONFIRMATION_TIERS:
        func = click.option(
            f"--yes-i-mean-{tier}",
            f"is_{tier}_confirmed",
            is_flag=True,
            default=False,
            help=f"Required confirmation when the activated env is on the {tier} tier.",
        )(func)
    return func


def require_tier_confirmation(
    env_name: str, *, is_production_confirmed: bool, is_staging_confirmed: bool, is_dev_confirmed: bool
) -> None:
    """Refuse a mutating stage unless the flag naming the activated env's tier was passed.

    Checked before the stage resolves the pool DSN, the pool key and the storage
    Vault entry, so a forgotten flag is refused without any Vault round trip.
    """
    tier = confirmation_tier_for_env_name(env_name)
    is_confirmed_by_tier = {
        PRODUCTION_TIER: is_production_confirmed,
        STAGING_TIER: is_staging_confirmed,
        DEV_TIER: is_dev_confirmed,
    }
    if not is_confirmed_by_tier[tier]:
        raise click.ClickException(
            f"Refusing to run the cutover against env '{env_name}' (tier '{tier}') without --yes-i-mean-{tier}."
        )


@contextmanager
def _cutover_context(database_url: str | None, *, is_connector_needed: bool = False) -> Iterator[CutoverContext]:
    env_name = require_activated_env_name()
    dsn = resolve_pool_database_url(database_url)
    pem = resolve_pool_private_key_pem()
    storage = resolve_workspace_storage_config()
    # migrate/rollback drive the product's own stop/start through the
    # connector's admin API; the other commands never need it, so a tier
    # without the key on hand can still preflight and repave.
    connector_url = resolve_admin_connector_url(None) if is_connector_needed else None
    admin_api_key = SecretStr(resolve_admin_api_key_value(None)) if is_connector_needed else None
    state_root = env_root_dir(DevEnvName(env_name)) / CUTOVER_STATE_DIRNAME
    state = CutoverStateStore(root=state_root)
    state.ensure_layout()
    # The pool key itself is only dialed through ``identities`` (gen-1 boxes and
    # VMs); its public half is what the migrate strips from harvested keys.
    with pool_private_key_path(pem) as key_path:
        pool_public_key = derive_ssh_public_key(key_path)
    with (
        box_management_identities(resolve_gen1_pool_private_key_pem=lambda: pem) as identities,
        minimal_mngr_context(state_root / "mngr-profile") as mngr_ctx,
    ):
        yield CutoverContext(
            env_name=env_name,
            dsn=dsn,
            pool_public_key=pool_public_key,
            identities=identities,
            ssh_ca_public_key=resolve_tier_ssh_ca_public_key_or_none(),
            storage=storage,
            state=state,
            mngr_ctx=mngr_ctx,
            connector_url=connector_url,
            admin_api_key=admin_api_key,
        )


def _emit_stage_report(ctx: CutoverContext, report: StageReport) -> None:
    text = render_stage_table(report)
    report_path = ctx.state.write_stage_report(report, text)
    write_human_line(text)
    emit_json(report.model_dump(mode="json"))
    logger.info("Wrote the {} report to {}", report.stage_name, report_path)
    if report.failed_count:
        raise SystemExit(1)


_SERVER_ID_OPTION_HELP: Final[str] = (
    "Scope to this bare_metal_servers row id (repeatable); default: every gen-1 box of the env's tier."
)


@cutover.command(name="preflight")
@click.option("--server-id", "server_ids", multiple=True, help=_SERVER_ID_OPTION_HELP)
@click.option(
    "--json-out",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Also write the JSON report here.",
)
@click.option("--database-url", default=None, help=DATABASE_URL_HELP)
def preflight(server_ids: tuple[str, ...], json_out: Path | None, database_url: str | None) -> None:
    """Read-only: classify every gen-1 pool row's migrate eligibility and probe versions/keys/health/disks.

    Exits non-zero unless the tier is clean (no refused rows, no probe errors)
    -- re-run it until it is before migrating.
    """
    with _cutover_context(database_url) as ctx:
        report = run_preflight(ctx, list(server_ids))
        text = render_preflight_table(report)
        report_json = report.model_dump_json(indent=2)
        report_path = ctx.state.write_report("preflight", report_json, text)
        if json_out is not None:
            json_out.parent.mkdir(parents=True, exist_ok=True)
            json_out.write_text(report_json)
        write_human_line(text)
        logger.info("Wrote the preflight report to {}", report_path)
        if not report.is_clean:
            raise SystemExit(1)


@cutover.command(name="migrate")
@click.option(
    "--target-server-id",
    required=True,
    help="The ready gen-2 bare_metal_servers row id the workspaces migrate onto.",
)
@click.option(
    "--workspace",
    "workspace_ids",
    multiple=True,
    help="A pool_hosts row id to migrate (repeatable).",
)
@click.option("--user", "user_email", default=None, help="Migrate every gen-1 workspace of this account (by email).")
@click.option(
    "--source-server-id",
    default=None,
    help="Migrate every migratable workspace on this gen-1 box (the box-emptying selector).",
)
@click.option(
    "--keep-origin-vm",
    "is_keep_origin_vm",
    is_flag=True,
    default=False,
    help=(
        "Leave the halted origin VM in place instead of destroying it after a successful migration "
        "(early-drill safety net; finalize it by hand, and avoid baking/reaping on its box meanwhile)."
    ),
)
@click.option(
    "--publish-image-tars",
    "is_publish_image_tars",
    is_flag=True,
    default=False,
    help="Pre-publish the selected workspaces' image tars before migrating (the lazy per-workspace publish remains).",
)
@click.option("--dry-run", "is_dry_run", is_flag=True, default=False, help="Print the plan; touch nothing.")
@click.option("--database-url", default=None, help=DATABASE_URL_HELP)
@tier_confirmation_options
def migrate(
    target_server_id: str,
    workspace_ids: tuple[str, ...],
    user_email: str | None,
    source_server_id: str | None,
    is_keep_origin_vm: bool,
    is_publish_image_tars: bool,
    is_dry_run: bool,
    database_url: str | None,
    is_production_confirmed: bool,
    is_staging_confirmed: bool,
    is_dev_confirmed: bool,
) -> None:
    """Migrate the selected gen-1 workspaces onto one gen-2 box, sequentially, stopping on the first failure.

    Each workspace: live-harvest its keys/inspect/version (admin-starting a
    stopped one first), run the product's own stop (verified artifact), save
    the artifact for rollback, park the row, transplant the data disk onto the
    target at freshly picked ports, replay the container, probe, re-lease.
    Run several invocations with disjoint target boxes to parallelize.
    """
    if not workspace_ids and user_email is None and source_server_id is None:
        raise click.UsageError("select workspaces with --workspace, --user, and/or --source-server-id")
    require_tier_confirmation(
        require_activated_env_name(),
        is_production_confirmed=is_production_confirmed,
        is_staging_confirmed=is_staging_confirmed,
        is_dev_confirmed=is_dev_confirmed,
    )
    with _cutover_context(database_url, is_connector_needed=True) as ctx:
        _emit_stage_report(
            ctx,
            run_migrate(
                ctx,
                target_server_id=target_server_id,
                workspace_ids=list(workspace_ids),
                user_email=user_email,
                source_server_id=source_server_id,
                is_keep_origin_vm=is_keep_origin_vm,
                is_publish_image_tars=is_publish_image_tars,
                is_dry_run=is_dry_run,
            ),
        )


@cutover.command(name="rollback")
@click.option("--workspace", "host_db_id", required=True, help="The migrated workspace's pool_hosts row id.")
@click.option("--database-url", default=None, help=DATABASE_URL_HELP)
@tier_confirmation_options
def rollback(
    host_db_id: str,
    database_url: str | None,
    is_production_confirmed: bool,
    is_staging_confirmed: bool,
    is_dev_confirmed: bool,
) -> None:
    """Roll one migrated workspace back onto gen-1 through the product's own restore.

    Destroys the gen-2 slice, restores the saved artifact pointers onto the
    row (finalized-stopped gen-1 shape), admin-starts it, and waits for
    ``leased``. Work done on gen-2 after the migration is lost by policy:
    rollback is for migrations judged failed promptly.
    """
    require_tier_confirmation(
        require_activated_env_name(),
        is_production_confirmed=is_production_confirmed,
        is_staging_confirmed=is_staging_confirmed,
        is_dev_confirmed=is_dev_confirmed,
    )
    with _cutover_context(database_url, is_connector_needed=True) as ctx:
        _emit_stage_report(ctx, run_rollback(ctx, host_db_id=host_db_id))


@cutover.command(name="repave")
@click.option(
    "--server-id",
    "server_ids",
    multiple=True,
    required=True,
    help="The emptied box to reinstall as gen-2 (repeatable; there is no default scope).",
)
@click.option("--dry-run", "is_dry_run", is_flag=True, default=False, help="Print what would happen; touch nothing.")
@click.option("--database-url", default=None, help=DATABASE_URL_HELP)
@tier_confirmation_options
def repave(
    server_ids: tuple[str, ...],
    is_dry_run: bool,
    database_url: str | None,
    is_production_confirmed: bool,
    is_staging_confirmed: bool,
    is_dev_confirmed: bool,
) -> None:
    """Reinstall an emptied gen-1 box as gen-2 (delivered, overcommit 4.0), prep it to ready, record its storage partition.

    Refused while the box holds any pool rows (migrate the workspaces off it;
    ``pool destroy`` its unleased rows) or an in-flight migration references it.
    """
    require_tier_confirmation(
        require_activated_env_name(),
        is_production_confirmed=is_production_confirmed,
        is_staging_confirmed=is_staging_confirmed,
        is_dev_confirmed=is_dev_confirmed,
    )
    with _cutover_context(database_url) as ctx:
        _emit_stage_report(ctx, run_repave(ctx, list(server_ids), is_dry_run=is_dry_run))
