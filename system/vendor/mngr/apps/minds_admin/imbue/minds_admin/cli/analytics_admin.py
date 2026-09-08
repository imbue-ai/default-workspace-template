"""``minds-admin analytics ...`` -- operator management of analyst access to the analytics lakes.

Operates on the activated tier (``minds-admin env activate <env>`` first).
``analyst add`` grants a person read-only access to the tier's metrics and
transcripts lakes and prints a self-documenting credentials TOML to hand
them; ``analyst remove`` revokes everything; ``analyst list`` reconstructs
the current access state from the backends. See
``apps/analytics/reports/README.md`` for the analyst-facing documentation.
"""

from datetime import datetime
from datetime import timezone
from pathlib import Path

import click
from tabulate import tabulate

from imbue.minds_admin.cli._tier_secrets import resolve_analytics_analyst_admin_context
from imbue.minds_admin.envs.providers.analytics_analysts import AnalystListing
from imbue.minds_admin.envs.providers.analytics_analysts import list_analyst_access
from imbue.minds_admin.envs.providers.analytics_analysts import provision_analyst_access
from imbue.minds_admin.envs.providers.analytics_analysts import render_analyst_credentials_toml
from imbue.minds_admin.envs.providers.analytics_analysts import revoke_analyst_access
from imbue.minds_admin.primitives import AnalystName
from imbue.mngr.cli.output_helpers import write_human_line


@click.group(name="analytics")
def analytics_admin() -> None:
    """Manage the activated tier's analytics stack (analyst access)."""


@analytics_admin.group(name="analyst")
def analyst() -> None:
    """Add / remove / list people with read-only access to the analytics lakes."""


@analyst.command(name="add")
@click.argument("name")
@click.option(
    "--no-transcripts",
    "is_transcripts_excluded",
    is_flag=True,
    default=False,
    help="Grant only the metrics lake, and remove any existing transcripts access for this analyst.",
)
@click.option(
    "--output",
    "output",
    type=click.Path(dir_okay=False),
    default=None,
    help="Write the credentials TOML to this file (0600) instead of printing it to stdout.",
)
def add_analyst(name: str, is_transcripts_excluded: bool, output: str | None) -> None:
    """Grant NAME read-only access to the tier's analytics lakes and emit their credentials.

    Creates (or refreshes) the ``analyst_<name>`` Postgres role on the
    metrics and transcripts DuckLake catalogs and mints one read-only,
    bucket-scoped R2 token per lake. Re-running ROTATES the credentials
    (fresh password and tokens; previously handed-out values stop working).
    The emitted TOML is the hand-off artifact: it documents itself, including
    a copy-pasteable DuckDB quick start with the real values substituted.
    """
    analyst_name = AnalystName(name)
    context = resolve_analytics_analyst_admin_context()
    credentials = provision_analyst_access(context, analyst_name, is_transcripts_included=not is_transcripts_excluded)
    document = render_analyst_credentials_toml(credentials, context.env_name, datetime.now(timezone.utc))
    if output is None:
        write_human_line(document.rstrip("\n"))
    else:
        output_path = Path(output)
        output_path.touch(mode=0o600, exist_ok=True)
        # touch() only applies the mode to a newly created file; chmod so a
        # pre-existing file also ends up 0600 before the secrets land in it.
        output_path.chmod(0o600)
        output_path.write_text(document)
        write_human_line(f"Wrote credentials for {analyst_name!r} to {output_path} -- deliver privately.")


@analyst.command(name="remove")
@click.argument("name")
def remove_analyst(name: str) -> None:
    """Revoke NAME's analytics access: drop their role and revoke their R2 tokens.

    Idempotent -- pieces that are already gone are skipped.
    """
    analyst_name = AnalystName(name)
    context = resolve_analytics_analyst_admin_context()
    report = revoke_analyst_access(context, analyst_name)
    role_summary = "dropped role" if report.is_role_dropped else "no role to drop"
    write_human_line(
        f"Removed analyst {analyst_name!r}: {role_summary}, revoked {report.revoked_token_count} token(s)."
    )


def _listing_table(listings: list[AnalystListing]) -> str:
    headers = ["ANALYST", "ROLE", "METRICS", "TRANSCRIPTS", "METRICS TOKENS", "TRANSCRIPTS TOKENS"]
    rows = [
        [
            listing.analyst_name,
            "yes" if listing.is_role_present else "MISSING",
            "yes" if listing.is_metrics_granted else "no",
            "yes" if listing.is_transcripts_granted else "no",
            listing.metrics_token_count,
            listing.transcripts_token_count,
        ]
        for listing in listings
    ]
    return tabulate(rows, headers=headers, tablefmt="plain")


@analyst.command(name="list")
def list_analysts() -> None:
    """List everyone with analytics access, reconstructed from the roles, grants, and tokens."""
    context = resolve_analytics_analyst_admin_context()
    listings = list_analyst_access(context)
    if not listings:
        write_human_line("No analysts have analytics access on this tier.")
        return
    write_human_line(_listing_table(listings))
