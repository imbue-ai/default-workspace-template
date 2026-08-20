"""The top-level ``minds-admin`` click group.

Assembles the consolidated operator surface: the minds env lifecycle plus the
bare-metal pool / server provisioning and the connector admin commands that
used to live under ``mngr imbue_cloud admin`` and ``minds {env,pool,server,paid}``.
"""

from pathlib import Path

import click

from imbue.minds.primitives import OutputFormat
from imbue.minds.utils.logging import console_level_from_verbose_and_quiet
from imbue.minds.utils.logging import setup_logging
from imbue.minds_admin.cli.accounts_admin import account_admin
from imbue.minds_admin.cli.env import env
from imbue.minds_admin.cli.paid import paid
from imbue.minds_admin.cli.pool import pool
from imbue.minds_admin.cli.relays_admin import relays_admin
from imbue.minds_admin.cli.repair_keys_admin import repair_keys
from imbue.minds_admin.cli.server import server
from imbue.minds_admin.cli.sweep_admin import sweep_admin
from imbue.minds_admin.cli.workspaces_admin import workspaces_admin


@click.group()
@click.option("-v", "--verbose", count=True, help="Increase verbosity; -v for DEBUG, -vv for TRACE")
@click.option("-q", "--quiet", is_flag=True, default=False, help="Suppress all console output")
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["human", "json", "jsonl"], case_sensitive=False),
    default="human",
    help="Output format for results on stdout",
)
@click.option(
    "--log-file",
    type=click.Path(),
    default=None,
    help="Path to a JSONL log file for persistent logging",
)
@click.pass_context
def cli(ctx: click.Context, verbose: int, quiet: bool, output_format: str, log_file: str | None) -> None:
    """minds-admin: operator lifecycle tooling for the minds stack."""
    console_level = console_level_from_verbose_and_quiet(verbose, quiet)
    command_name = ctx.invoked_subcommand or "unknown"
    log_file_path = Path(log_file) if log_file else None
    setup_logging(console_level, command=command_name, log_file=log_file_path)
    ctx.ensure_object(dict)
    ctx.obj["console_level"] = console_level
    ctx.obj["output_format"] = OutputFormat(output_format.upper())


cli.add_command(env)
cli.add_command(pool)
cli.add_command(server)
cli.add_command(paid)
cli.add_command(account_admin)
cli.add_command(workspaces_admin)
cli.add_command(sweep_admin)
cli.add_command(relays_admin)
cli.add_command(repair_keys)
