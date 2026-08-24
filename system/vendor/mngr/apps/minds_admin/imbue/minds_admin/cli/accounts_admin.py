"""``minds-admin account ...`` -- operator-only per-account entitlements management.

Addressed by *email* (the connector resolves the SuperTokens user); all
commands authenticate with the fixed ``MINDS_ADMIN_KEY`` API key, like
the paid-list CRUD. ``show`` lazily materializes the account's entitlements
row; ``set-plan`` resets the row wholesale to the plan's defaults (the way to
wipe manual bumps -- it deliberately skips the ally eligibility check);
``set-quota`` bumps a single entitlement value.
"""

import click

from imbue.minds_admin.cli._tier_secrets import make_admin_connector_client
from imbue.minds_admin.cli.paid import paid_auth_options
from imbue.minds_admin.cli.paid import resolve_admin_api_key
from imbue.mngr_imbue_cloud.cli._common import emit_json
from imbue.mngr_imbue_cloud.cli._common import handle_imbue_cloud_errors


@click.group(name="account")
def account_admin() -> None:
    """Show / set plans and quotas for a user account (requires MINDS_ADMIN_KEY)."""


@account_admin.command(name="show")
@click.argument("email")
@paid_auth_options
@handle_imbue_cloud_errors
def admin_show_account(email: str, connector_url: str | None, api_key: str | None) -> None:
    """Show one account's plan, entitlement values, live usage, and suspension state."""
    client = make_admin_connector_client(connector_url)
    info = client.admin_get_account(resolve_admin_api_key(api_key), email)
    emit_json(info.model_dump(mode="json"))


@account_admin.command(name="set-plan")
@click.argument("email")
@click.argument("plan")
@paid_auth_options
@handle_imbue_cloud_errors
def admin_set_plan(email: str, plan: str, connector_url: str | None, api_key: str | None) -> None:
    """Assign PLAN to the account, resetting its entitlements to the plan's defaults."""
    client = make_admin_connector_client(connector_url)
    emit_json(client.admin_set_account_plan(resolve_admin_api_key(api_key), email, plan))


@account_admin.command(name="revoke-sessions")
@click.argument("email")
@paid_auth_options
@handle_imbue_cloud_errors
def admin_revoke_sessions(email: str, connector_url: str | None, api_key: str | None) -> None:
    """Revoke every session of the account (the lockout button; sign-in stays possible).

    State-modifying requests with a revoked token are refused within one
    round-trip; read access drains out over the access token's remaining
    lifetime (~1h). Use ``suspend`` to also block sign-in and freeze the
    account's resources.
    """
    client = make_admin_connector_client(connector_url)
    emit_json(client.admin_revoke_sessions(resolve_admin_api_key(api_key), email))


@account_admin.command(name="suspend")
@click.argument("email")
@click.option("--reason", required=True, help="Why the account is being suspended (internal; never shown to the user)")
@click.option(
    "--block-storage",
    is_flag=True,
    default=False,
    help="Also disable the account's R2 tokens outright (reads included). Re-running with this flag escalates.",
)
@paid_auth_options
@handle_imbue_cloud_errors
def admin_suspend_account(
    email: str, reason: str, block_storage: bool, connector_url: str | None, api_key: str | None
) -> None:
    """Suspend the account: block sign-in, revoke sessions, stop workspaces, block keys, pause shares.

    Reversible and data-preserving (see ``unsuspend``). Idempotent: re-run on
    a partial failure (the JSON report shows per-step outcomes) or with
    ``--block-storage`` to escalate. By default R2 keys go read-only so the
    user can still retrieve backups.
    """
    client = make_admin_connector_client(connector_url)
    report = client.admin_suspend_account(resolve_admin_api_key(api_key), email, reason, block_storage)
    emit_json(report)
    if report.get("status") != "ok":
        raise SystemExit(1)


@account_admin.command(name="unsuspend")
@click.argument("email")
@paid_auth_options
@handle_imbue_cloud_errors
def admin_unsuspend_account(email: str, connector_url: str | None, api_key: str | None) -> None:
    """Lift the account's suspension: restore sign-in, keys, and shares.

    Workspaces stay stopped until the user starts them, and the user signs in
    fresh. Idempotent -- re-run on a partial failure.
    """
    client = make_admin_connector_client(connector_url)
    report = client.admin_unsuspend_account(resolve_admin_api_key(api_key), email)
    emit_json(report)
    if report.get("status") != "ok":
        raise SystemExit(1)


@account_admin.command(name="set-quota")
@click.argument("email")
@click.argument("entitlement")
@click.argument("value", type=float)
@paid_auth_options
@handle_imbue_cloud_errors
def admin_set_quota(
    email: str, entitlement: str, value: float, connector_url: str | None, api_key: str | None
) -> None:
    """Set a single entitlement VALUE on the account (an operator bump).

    ENTITLEMENT is one of: max_remote_workspaces, max_total_workspaces,
    max_buckets, max_total_bucket_bytes, monthly_llm_spend_usd,
    max_active_synced_workspaces.
    """
    client = make_admin_connector_client(connector_url)
    emit_json(client.admin_set_account_quota(resolve_admin_api_key(api_key), email, entitlement, value))
