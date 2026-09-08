"""``minds-admin pool ...`` -- env-aware pool provisioning.

``pool create`` bakes pre-provisioned pool hosts as lima-VM "slices" carved on one
of our registered bare-metal boxes (the shared implementation is
``cli.server.allocate_slices``). The bake writes a leasable row to the connector's
Neon ``pool_hosts`` table.

Env-aware: the activated minds env supplies the owning env name (stamped into
each slice's lima names), the host_pool DSN, and the tier's pool SSH private key
(from Vault) -- so operators never hand-export them. ``--database-url`` /
``MINDS_HOST_POOL_DSN`` / ``POOL_SSH_PRIVATE_KEY`` remain as overrides for
non-activated one-off use.

Authentication: these commands talk to Neon directly via the resolved DSN. They do
NOT use the operator's SuperTokens session; the connector is not involved in pool
provisioning at all.
"""

import json as _json
from pathlib import Path
from typing import Any
from typing import Final

import click
import psycopg2
from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.envs.paths import active_env_name_or_none
from imbue.minds_admin.bake.bake_source import BakeSourceError
from imbue.minds_admin.bake.bake_source import DEFAULT_WORKSPACE_TEMPLATE_REPO_URL
from imbue.minds_admin.bake.bake_source import merge_bake_identity_attributes
from imbue.minds_admin.bake.bake_source import resolved_bake_source
from imbue.minds_admin.bake.bake_source import validate_bake_source_selectors
from imbue.minds_admin.cli._tier_secrets import DATABASE_URL_HELP
from imbue.minds_admin.cli._tier_secrets import read_pool_private_key_from_vault_or_fail
from imbue.minds_admin.cli._tier_secrets import resolve_pool_database_url
from imbue.minds_admin.cli._tier_secrets import resolve_pool_private_key_pem
from imbue.minds_admin.cli.server import DEFAULT_SLICE_BAKE_CONCURRENCY
from imbue.minds_admin.cli.server import DEFAULT_SLICE_DESTROY_CONCURRENCY
from imbue.minds_admin.cli.server import allocate_slices
from imbue.minds_admin.cli.server import build_pool_host_destroy_report
from imbue.minds_admin.cli.server import destroy_pool_hosts_in_parallel
from imbue.minds_admin.cli.server import tear_down_unleased_slices
from imbue.minds_admin.cli.server import warm_box_image_cache
from imbue.minds_admin.slices.bare_metal_db import destroy_eligible_pool_host_statuses
from imbue.mngr_imbue_cloud.cli._common import emit_json
from imbue.mngr_imbue_cloud.cli._common import fail_with_json
from imbue.mngr_imbue_cloud.errors import RepoIdentityError
from imbue.mngr_imbue_cloud.primitives import KNOWN_OVH_US_REGIONS


@click.group(name="pool")
def pool() -> None:
    """Pool host provisioning for the activated minds env (bare-metal slices + Neon)."""


@pool.command(name="create")
@click.option("--count", required=True, type=int, help="Number of pool hosts to create")
@click.option(
    "--region",
    required=True,
    type=str,
    help=(
        "Lease-region label stamped on every new row (e.g. ``US-EAST-VA``, ``US-WEST-OR``) -- this is "
        "what the connector's region-filtered lease matches. It is the lease-region label only (NOT the "
        "box's raw datacenter code)."
    ),
)
@click.option(
    "--from-tag",
    "from_tag",
    default=None,
    help=(
        "[production bake] Clone --repo-url at exactly this tag into a fresh temp dir and bake from it. "
        "Stamps repo_url=canonical(--repo-url) and repo_branch_or_tag=<tag>; the content provably equals the "
        "tag. Mutually exclusive with --workspace-dir; errors if <tag> is not a real tag."
    ),
)
@click.option(
    "--repo-url",
    "repo_url",
    default=DEFAULT_WORKSPACE_TEMPLATE_REPO_URL,
    help="[--from-tag only] Canonical repo to clone the tag from (default: the DEFAULT_WORKSPACE_TEMPLATE remote).",
)
@click.option(
    "--workspace-dir",
    required=False,
    default=None,
    type=click.Path(exists=True),
    help=(
        "[dev bake] Bake content from this working tree (uncommitted changes included). Stamps "
        "repo_url=canonical(origin of the folder) and repo_branch_or_tag=<folder's current branch> "
        "(override with --repo-branch-or-tag). Mutually exclusive with --from-tag; errors without an origin."
    ),
)
@click.option(
    "--repo-branch-or-tag",
    "repo_branch_or_tag_override",
    default=None,
    help="[--workspace-dir only] Override the branch label stamped (default: the folder's current branch).",
)
@click.option(
    "--attributes",
    "attributes_json",
    required=False,
    default=None,
    help=(
        "Optional non-identity lease-attributes JSON for the new pool rows. The identity keys repo_url and "
        "repo_branch_or_tag are NOT allowed here -- they are derived from the bake source (--from-tag / "
        "--workspace-dir). The per-box size (memory_gb / cpus) is computed and stamped automatically."
    ),
)
@click.option(
    "--database-url",
    required=False,
    default=None,
    type=str,
    help=DATABASE_URL_HELP,
)
@click.option(
    "--mngr-source",
    type=click.Path(exists=True),
    default=None,
    help="Path to the mngr monorepo root. If provided, rsyncs into the template's system/vendor/mngr/ before creating hosts.",
)
@click.option(
    "--server-id",
    "server_id",
    default=None,
    help=(
        "[required] The bare_metal_servers row id to bake the slices onto (from "
        "`minds-admin server list`). Slice baking targets an explicitly-chosen, ready box -- it never "
        "auto-selects one."
    ),
)
@click.option(
    "--dry-run",
    "is_dry_run",
    is_flag=True,
    default=False,
    help="Report placement + per-slice sizing; do not bake.",
)
@click.option(
    "--max-concurrency",
    "max_concurrency",
    type=int,
    default=DEFAULT_SLICE_BAKE_CONCURRENCY,
    show_default=True,
    help=(
        "Max slices baked at once; the rest queue and start as slots free. "
        "Bounds box CPU/IO/network contention so each `mngr create` stays under its timeout."
    ),
)
@click.option(
    "--skip-deferred-install-wait",
    "is_env_converge_wait_skipped",
    is_flag=True,
    default=False,
    help=(
        "[dev only] Don't wait for the DEFAULT_WORKSPACE_TEMPLATE env-converge slow phase (heavy apt + "
        "Fortress/Chromium install, environment-record capture, rootfs stamp) to finish before stopping "
        "the baked services agent. Saves a few minutes per bake, but the baked container's converge may be "
        "left incomplete (no apt.json/rootfs stamp; stopping mid-apt can corrupt dpkg). Safe for "
        "dev/throwaway bakes; NEVER use for production pool hosts."
    ),
)
@click.option(
    "--content-addressed-cache",
    "is_content_addressed_cache",
    is_flag=True,
    default=False,
    help=(
        "[--workspace-dir only; CI bakes] Key the per-box image cache on a hash of the workspace "
        "content (computed after the --mngr-source vendor sync) instead of disabling it: the first "
        "slice builds + seeds the box tar, the rest docker-load it, and a re-bake of identical content "
        "is warm. --from-tag bakes already key on the tag, so combining is refused."
    ),
)
def pool_create(
    count: int,
    region: str,
    from_tag: str | None,
    repo_url: str,
    workspace_dir: str | None,
    repo_branch_or_tag_override: str | None,
    attributes_json: str | None,
    database_url: str | None,
    mngr_source: str | None,
    server_id: str | None,
    is_dry_run: bool,
    max_concurrency: int,
    is_env_converge_wait_skipped: bool,
    is_content_addressed_cache: bool,
) -> None:
    """Create pre-provisioned bare-metal slice pool hosts for the activated minds env.

    The bake source -- exactly one of ``--from-tag`` (production, clones a tag) or
    ``--workspace-dir`` (dev, a working tree) -- determines the content baked and
    the canonical ``repo_url`` / ``repo_branch_or_tag`` stamped into each row, so
    the advertised identity always describes what is actually baked.

    The activated env supplies the owning env name (stamped into each slice's
    lima names so multiple envs can share one box and the post-bake reap only
    touches this env's own slices) and the tier's pool SSH key from Vault. A
    non-activated invocation bakes legacy un-stamped slices and needs
    ``$POOL_SSH_PRIVATE_KEY``.
    """
    # The region is the lease-region label the connector region-matches at lease
    # time (e.g. US-EAST-VA), NOT a box's raw OVH datacenter code (e.g. 'vin',
    # which `minds-admin server list` prints). Stamping a datacenter code onto the row
    # would make every baked host permanently unleasable: the create form only
    # ever requests a lease label and the connector's region filter is an exact,
    # never-relaxed string match. Reject anything outside the known lease regions
    # up front, before any (clone-heavy) bake work.
    if region not in KNOWN_OVH_US_REGIONS:
        fail_with_json(
            f"--region {region!r} is not a known lease region. Pass one of "
            f"{sorted(KNOWN_OVH_US_REGIONS)} (the lease-region label, e.g. US-EAST-VA) -- "
            "NOT the box's OVH datacenter code (e.g. 'vin' from `minds-admin server list`).",
            error_class="UsageError",
        )

    resolved_database_url = resolve_pool_database_url(database_url)
    parsed_attributes = _parse_optional_attributes_json(attributes_json)

    if not server_id:
        fail_with_json(
            "--server-id is required (the bare-metal box to bake onto; see `minds-admin server list`)",
            error_class="UsageError",
        )

    # Fail fast on the cheap usage error before any Vault read or clone.
    try:
        validate_bake_source_selectors(from_tag=from_tag, workspace_dir=workspace_dir)
    except BakeSourceError as exc:
        fail_with_json(str(exc), error_class="UsageError")

    if is_content_addressed_cache and from_tag is not None:
        fail_with_json(
            "--content-addressed-cache only applies to --workspace-dir bakes; --from-tag bakes already "
            "key the image cache on the tag",
            error_class="UsageError",
        )

    # The activated env stamps slice ownership; the tier's pool key comes from
    # Vault (or the POOL_SSH_PRIVATE_KEY override for non-activated use).
    # Resolved up front, before any clone-heavy bake work.
    slice_env_name = active_env_name_or_none()
    pool_private_key_pem = resolve_pool_private_key_pem()

    # Resolve the bake source and derive the identity attributes to stamp. The
    # context manager cleans up any temp clone (--from-tag) on exit; both the
    # dry-run report and the real bake go through it, so they cannot disagree.
    try:
        with resolved_bake_source(
            from_tag=from_tag,
            workspace_dir=workspace_dir,
            repo_url=repo_url,
            repo_branch_or_tag_override=repo_branch_or_tag_override,
        ) as bake_source:
            attributes = merge_bake_identity_attributes(parsed_attributes, bake_source)
            # ``server_id`` presence is enforced above.
            assert server_id is not None
            allocate_slices(
                count=count,
                server_id=server_id,
                lease_attributes=attributes,
                region=region,
                env_name=slice_env_name,
                workspace_dir=bake_source.workspace_dir,
                mngr_source=mngr_source,
                # A --from-tag bake must keep the tag's own vendored mngr (byte-for-byte
                # release content); only --workspace-dir / --mngr-source override it.
                is_from_tag=from_tag is not None,
                is_content_addressed_cache=is_content_addressed_cache,
                database_url=resolved_database_url,
                pool_private_key_pem=pool_private_key_pem,
                is_dry_run=is_dry_run,
                is_env_converge_wait_skipped=is_env_converge_wait_skipped,
                max_concurrency=max_concurrency,
            )
    except (BakeSourceError, RepoIdentityError) as exc:
        fail_with_json(str(exc), error_class="UsageError")


def _parse_optional_attributes_json(attributes_json: str | None) -> dict[str, Any]:
    """Parse the optional --attributes JSON object, defaulting to empty when absent."""
    if not attributes_json:
        return {}
    try:
        parsed = _json.loads(attributes_json)
    except _json.JSONDecodeError as exc:
        logger.error("Invalid --attributes JSON: {}", exc)
        fail_with_json(f"Invalid --attributes JSON: {exc}", error_class="UsageError")
    if not isinstance(parsed, dict):
        fail_with_json("--attributes must be a JSON object", error_class="UsageError")
    return parsed


@pool.command(name="warm-cache")
@click.option(
    "--server-id",
    "server_id",
    required=True,
    help="The bare_metal_servers row id of the box to warm (from `minds-admin server list`).",
)
@click.option(
    "--workspace-dir",
    required=True,
    type=click.Path(exists=True),
    help="The default-workspace-template working tree whose content the tar is built from.",
)
@click.option(
    "--mngr-source",
    type=click.Path(exists=True),
    default=None,
    help="Path to the mngr monorepo root. If provided, rsyncs into the template's system/vendor/mngr/ first.",
)
@click.option(
    "--content-addressed-cache",
    "is_content_addressed_cache",
    is_flag=True,
    default=False,
    help="Required: key the tar on a hash of the workspace content (computed after the --mngr-source sync).",
)
@click.option(
    "--database-url",
    required=False,
    default=None,
    type=str,
    help=DATABASE_URL_HELP,
)
def pool_warm_cache(
    server_id: str,
    workspace_dir: str,
    mngr_source: str | None,
    is_content_addressed_cache: bool,
    database_url: str | None,
) -> None:
    """Pre-warm a box's image cache for the given workspace content, without provisioning any pool host.

    The DB-free seed-only verb of the CI release flow (specs/remote-workspaces-in-ci.md):
    if the box already holds the tar for the derived content tag this exits 0 immediately;
    otherwise one throwaway ``ci-warm`` slice is carved, the existing seed path builds and
    publishes the box tar, and the slice is destroyed unconditionally. Run in parallel with
    the per-run env deploy so a cold seed build overlaps it instead of following it. The
    database (typically the CI infra DB) is only read, for the box row; failure is advisory
    to the CI workflow (the bake stage's own seed phase is the fallback) but still exits
    non-zero so operators see it.
    """
    if not is_content_addressed_cache:
        fail_with_json(
            "--content-addressed-cache is required: warm-cache exists to pre-seed the content-addressed "
            "tar (a tag-keyed production bake seeds its own cache via `pool create --from-tag`)",
            error_class="UsageError",
        )
    warm_box_image_cache(
        server_id=server_id,
        workspace_dir=Path(workspace_dir).resolve(),
        mngr_source=mngr_source,
        database_url=resolve_pool_database_url(database_url),
        pool_private_key_pem=resolve_pool_private_key_pem(),
    )


# Every pool_hosts column, in a stable display order, used to build BOTH the
# `pool list` SELECT and the keys of each emitted JSON row -- so the two can
# never drift. Hand-maintaining a subset is what silently dropped region and the
# slice identifiers (bare_metal_server_id / lima_instance_name / lima_disk_name)
# from the output. emit_json serialises the UUID and datetime values via its
# default=str, so no per-column coercion is needed.
_POOL_HOST_LIST_COLUMNS: Final[tuple[str, ...]] = (
    "id",
    "host_name",
    "status",
    "region",
    "attributes",
    "vps_address",
    "vps_instance_id",
    "agent_id",
    "host_id",
    "ssh_user",
    "ssh_port",
    "container_ssh_port",
    "bare_metal_server_id",
    "lima_instance_name",
    "lima_disk_name",
    "leased_to_user",
    "leased_at",
    "released_at",
    "created_at",
)


@pool.command(name="list")
@click.option(
    "--database-url",
    required=False,
    default=None,
    type=str,
    help=DATABASE_URL_HELP,
)
def pool_list(database_url: str | None) -> None:
    """List rows in pool_hosts."""
    resolved_database_url = resolve_pool_database_url(database_url)
    conn = psycopg2.connect(resolved_database_url)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT {', '.join(_POOL_HOST_LIST_COLUMNS)} FROM pool_hosts ORDER BY created_at DESC")
            rows = cur.fetchall()
    finally:
        conn.close()
    emit_json([dict(zip(_POOL_HOST_LIST_COLUMNS, row, strict=True)) for row in rows])


@pool.command(name="destroy")
@click.argument("pool_host_ids", nargs=-1, required=True)
@click.option(
    "--database-url",
    required=False,
    default=None,
    type=str,
    help=DATABASE_URL_HELP,
)
@click.option(
    "--force",
    "is_leased_destroy_allowed",
    is_flag=True,
    help="Also destroy rows that are currently leased (tears down the leasing user's live workspace).",
)
@click.option(
    "--drop-row-only",
    "is_row_drop_only",
    is_flag=True,
    default=False,
    help=(
        "Only drop the DB rows; do NOT attempt VM teardown. Exclusively for rows whose "
        "bare-metal box record is gone or whose machine is permanently dead -- the default "
        "path already tolerates a VM that is merely absent."
    ),
)
@click.option(
    "--max-concurrency",
    "max_concurrency",
    type=int,
    default=DEFAULT_SLICE_DESTROY_CONCURRENCY,
    show_default=True,
    help="Max hosts destroyed at once; the rest queue and start as slots free.",
)
def pool_destroy(
    pool_host_ids: tuple[str, ...],
    database_url: str | None,
    is_leased_destroy_allowed: bool,
    is_row_drop_only: bool,
    max_concurrency: int,
) -> None:
    """Destroy pool hosts: claim each row, tear down its slice lima VM, then drop the row.

    All named hosts are destroyed concurrently (at most ``--max-concurrency`` at a
    time, regardless of which box each slice is on). Each row is first atomically
    claimed (flipped to 'removing' in a committed transaction), so a user lease can
    never race the teardown: a row that got leased first is skipped and reported.
    'available' and stale 'removing' rows are destroyed by default; a 'leased' row
    needs ``--force``. The VM is destroyed *before* the row is deleted, so a failure
    keeps the row ('removing', unleasable) and re-running the same command retries
    it. The teardown SSHes the boxes with the activated tier's pool key from Vault
    (or the $POOL_SSH_PRIVATE_KEY override); ``--drop-row-only`` never SSHes, so it
    needs no key. Exits non-zero only when a teardown actually failed.
    """
    # Mirror destroy_pool_hosts_in_parallel's guard up front, before any
    # Vault-touching key resolution, so the usage error surfaces first.
    if max_concurrency <= 0:
        raise click.UsageError("--max-concurrency must be positive")
    resolved_database_url = resolve_pool_database_url(database_url)
    pool_private_key_pem = None if is_row_drop_only else resolve_pool_private_key_pem()
    outcomes = destroy_pool_hosts_in_parallel(
        pool_host_ids=list(pool_host_ids),
        database_url=resolved_database_url,
        pool_private_key_pem=pool_private_key_pem,
        eligible_statuses=destroy_eligible_pool_host_statuses(is_leased_destroy_allowed=is_leased_destroy_allowed),
        is_row_drop_only=is_row_drop_only,
        max_concurrency=max_concurrency,
    )
    report = build_pool_host_destroy_report(outcomes)
    emit_json(report.model_dump(mode="json", exclude_none=True))
    if report.failed:
        raise SystemExit(1)


@pool.command(name="teardown-slices")
@click.option(
    "--database-url",
    required=False,
    default=None,
    type=str,
    help=DATABASE_URL_HELP,
)
@click.option(
    "--max-concurrency",
    "max_concurrency",
    type=int,
    default=DEFAULT_SLICE_DESTROY_CONCURRENCY,
    show_default=True,
    help="Max slices torn down at once; the rest queue and start as slots free.",
)
def pool_teardown_slices(database_url: str | None, max_concurrency: int) -> None:
    """Tear down every unleased slice VM in the pool DB and drop its row.

    Used by ``minds-admin env destroy`` (before the per-env DB is deleted) so the
    env's baked-but-unleased pool slices don't leak their VMs on the shared
    bare-metal boxes. Leased slices are excluded -- they are torn down via their
    agent's release path; rows stranded in 'removing' by a crashed release ARE
    included. Each row is atomically claimed before its VM is touched, so a lease
    cannot race the teardown, and the slices are torn down concurrently. The boxes
    are SSHed with the activated tier's pool key from Vault (or the
    $POOL_SSH_PRIVATE_KEY override). Idempotent per VM; fails (non-zero) if any
    box could not be reached, so the caller can stop rather than silently leak.
    """
    resolved_database_url = resolve_pool_database_url(database_url)
    result = tear_down_unleased_slices(
        resolved_database_url,
        pool_private_key_pem=resolve_pool_private_key_pem(),
        max_concurrency=max_concurrency,
    )
    emit_json(result.model_dump(mode="json", exclude_none=True))


def tear_down_env_pool_slices(env_name: str) -> None:
    """Tear down the env's unleased pool slices on their boxes before the env's DB is deleted.

    The in-process teardown ``minds-admin env destroy`` runs. Resolves the pool
    SSH key (Vault) + host_pool DSN exactly like ``pool teardown-slices``. Leased
    slices are left to their agent's release path. A missing pool SSH key is a bad
    state, not a "nothing to clean up" signal -- it raises (failing the destroy) so
    we never silently leak the env's slice VMs; a genuine teardown failure (an
    unreachable box) likewise raises rather than leaking.
    """
    pool_private_key_pem = read_pool_private_key_from_vault_or_fail(env_name)
    resolved_database_url = resolve_pool_database_url(None)
    result = tear_down_unleased_slices(
        resolved_database_url,
        pool_private_key_pem=pool_private_key_pem,
        max_concurrency=DEFAULT_SLICE_DESTROY_CONCURRENCY,
    )
    emit_json(result.model_dump(mode="json", exclude_none=True))


_KEYSCAN_TIMEOUT_SECONDS: Final[int] = 15

# SELECT pool rows still missing either pinned host key (pre-host-key-column bakes).
_SELECT_POOL_HOSTS_MISSING_KEYS_SQL: Final[str] = (
    "SELECT id, vps_address, ssh_port, container_ssh_port, outer_host_public_key, container_host_public_key "
    "FROM pool_hosts WHERE outer_host_public_key IS NULL OR container_host_public_key IS NULL"
)
_SELECT_BOXES_MISSING_KEY_SQL: Final[str] = (
    "SELECT id, public_address FROM bare_metal_servers "
    "WHERE box_host_public_key IS NULL AND public_address IS NOT NULL"
)


def _keyscan_host_public_key(host: str, port: int) -> str | None:
    """One-time TOFU scan of a host's ed25519 sshd key, for the migration backfill only.

    Returns ``"ssh-ed25519 <base64>"`` or None on failure. This is the single
    sanctioned trust-on-first-use in the system; all steady-state SSH pins a
    recorded key.
    """
    cg = ConcurrencyGroup(name="keyscan")
    with cg:
        result = cg.run_process_to_completion(
            command=["ssh-keyscan", "-t", "ed25519", "-T", str(_KEYSCAN_TIMEOUT_SECONDS), "-p", str(port), host],
            timeout=float(_KEYSCAN_TIMEOUT_SECONDS + 5),
            is_checked_after=False,
        )
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        # ssh-keyscan prints "<hostspec> ssh-ed25519 <base64>"; we want the key only.
        if len(parts) >= 3 and parts[1] == "ssh-ed25519":
            return f"{parts[1]} {parts[2]}"
    return None


@pool.command(name="backfill-host-keys")
@click.option(
    "--database-url",
    required=False,
    default=None,
    type=str,
    help=DATABASE_URL_HELP,
)
def pool_backfill_host_keys(database_url: str | None) -> None:
    """One-time: keyscan + record SSH host public keys for pre-existing pool rows and boxes.

    The single sanctioned trust-on-first-use in the system, used ONLY to migrate
    rows baked before the host-key columns existed. Run once after deploying the
    host-key-pinning version of the connector; afterward leasing and teardown
    enforce strict pinning with no scan fallback. Idempotent: rows that already have
    keys are skipped, and a row whose host cannot be scanned is left null (logged)
    for a later re-run.
    """
    resolved_database_url = resolve_pool_database_url(database_url)
    conn = psycopg2.connect(resolved_database_url)
    # Keyscans are slow network ops; autocommit each UPDATE rather than hold one
    # long transaction open across them.
    conn.autocommit = True
    pool_updated = 0
    box_updated = 0
    skipped: list[str] = []
    try:
        with conn.cursor() as cur:
            cur.execute(_SELECT_POOL_HOSTS_MISSING_KEYS_SQL)
            pool_rows = cur.fetchall()
        for row_id, vps_address, ssh_port, container_ssh_port, outer_key, container_key in pool_rows:
            new_outer = outer_key or _keyscan_host_public_key(vps_address, ssh_port)
            new_container = container_key or _keyscan_host_public_key(vps_address, container_ssh_port)
            if new_outer and new_container:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE pool_hosts SET outer_host_public_key = %s, container_host_public_key = %s WHERE id = %s",
                        (new_outer, new_container, str(row_id)),
                    )
                pool_updated += 1
            else:
                skipped.append(f"pool host {row_id} ({vps_address})")
                logger.warning("Could not keyscan host keys for pool host {} ({}); left null", row_id, vps_address)

        with conn.cursor() as cur:
            cur.execute(_SELECT_BOXES_MISSING_KEY_SQL)
            box_rows = cur.fetchall()
        for server_id, public_address in box_rows:
            box_key = _keyscan_host_public_key(public_address, 22)
            if box_key:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE bare_metal_servers SET box_host_public_key = %s, updated_at = NOW() WHERE id = %s",
                        (box_key, str(server_id)),
                    )
                box_updated += 1
            else:
                skipped.append(f"box {server_id} ({public_address})")
                logger.warning("Could not keyscan box key for server {} ({}); left null", server_id, public_address)
    finally:
        conn.close()
    emit_json({"pool_hosts_backfilled": pool_updated, "boxes_backfilled": box_updated, "skipped": skipped})
