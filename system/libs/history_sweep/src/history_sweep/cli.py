"""``history-sweep [--dry-run]``: remove what deleted chats leave on disk.

The ``history-sweep`` routine runs it once a day from the repo root. Each removal is
printed as one line (the routine's run log keeps them), and a routine run gets a
one-line summary in its ``result.json``.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

import click

from history_sweep.sweep import (
    Removal,
    SweepRefusedError,
    SweepRoots,
    apply_sweep,
    plan_sweep,
)

_DEFAULT_CHATS_ROOT: Final[Path] = Path("data/.apps/chat/chats")
_MIB: Final[int] = 1024**2


def default_roots(environ: dict[str, str], home: Path) -> SweepRoots:
    """Where mngr, the accounts and the chat records live, as the workspace's own programs resolve them."""
    host_dir = environ.get("MNGR_HOST_DIR", "").strip()
    accounts_root = environ.get("MINDS_ACCOUNTS_ROOT", "").strip()
    return SweepRoots(
        host_dir=Path(host_dir) if host_dir else home / ".mngr",
        accounts_root=Path(accounts_root)
        if accounts_root
        else home / ".minds" / "accounts",
        chats_root=_DEFAULT_CHATS_ROOT,
    )


def summarize(removals: tuple[Removal, ...], is_dry_run: bool) -> str:
    if not removals:
        return "Nothing to remove."
    agent_count = len({removal.agent_id for removal in removals})
    freed_mib = sum(removal.size_bytes for removal in removals) / _MIB
    verb = "Would free" if is_dry_run else "Freed"
    return f"{verb} {freed_mib:.1f} MiB from {agent_count} deleted chat agent(s)."


@click.command(
    help="Remove what deleted chats leave on disk; never anything a listed chat can open."
)
@click.option(
    "--dry-run", is_flag=True, help="Print what would be removed and remove nothing."
)
def main(dry_run: bool) -> None:
    roots = default_roots(dict(os.environ), Path.home())
    try:
        planned = plan_sweep(roots, now=datetime.now(timezone.utc))
    except SweepRefusedError as e:
        raise click.ClickException(f"removed nothing: {e}") from e
    if dry_run:
        removed, failed = planned, ()
    else:
        removed, failed = apply_sweep(planned)
    prefix = "would remove" if dry_run else "removed"
    for removal in removed:
        click.echo(
            f"{prefix} {removal.reason.lower()} {removal.agent_id} {removal.size_bytes} {removal.path}"
        )
    for removal, error in failed:
        click.echo(f"could not remove {removal.path}: {error}", err=True)
    summary = summarize(removed, dry_run)
    click.echo(summary)
    run_dir = os.environ.get("ROUTINES_RUN_DIR", "").strip()
    if run_dir:
        (Path(run_dir) / "result.json").write_text(
            json.dumps({"summary": summary}) + "\n"
        )
    if failed:
        raise click.ClickException(f"{len(failed)} path(s) could not be removed")
