from collections.abc import Sequence

import click

from imbue.mngr import hookimpl
from imbue.mngr_tmr.behavior_cli import tmr_behaviors
from imbue.mngr_tmr.cli import tmr


@hookimpl
def register_cli_commands() -> Sequence[click.Command] | None:
    """Register the tmr-family commands with mngr."""
    return [tmr, tmr_behaviors]
