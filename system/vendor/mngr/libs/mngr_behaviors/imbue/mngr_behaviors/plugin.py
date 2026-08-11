from collections.abc import Sequence

import click

from imbue.mngr import hookimpl
from imbue.mngr_behaviors.cli import behaviors


@hookimpl
def register_cli_commands() -> Sequence[click.Command] | None:
    """Register the behaviors command group with mngr."""
    return [behaviors]
