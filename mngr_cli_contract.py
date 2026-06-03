"""Validate that a ``mngr <subcommand> ...`` argv is accepted by the *live* mngr CLI.

This is the antidote to the failure mode that let PR #77 ship a broken
``mngr push`` invocation: every repo-side test that exercised a ``mngr`` CLI
call did so by asserting the emitted argv against a *hand-written expected
argv* using a stubbed subprocess runner. The expected argv was authored from
the same assumption as the production code, so the test only confirmed "the
code emits the bytes we told it to emit" -- it never confronted that argv with
the real ``mngr`` command surface. When vendor/mngr renamed ``push`` -> ``rsync``
(and dropped ``--source`` in favour of a positional ``SOURCE DESTINATION``),
both the production string and its mirrored test string still said ``push``,
so nothing went red.

``assert_mngr_argv_valid`` closes that hole by resolving the argv against the
actual ``imbue.mngr.main.cli`` click command tree. It checks *shape* only --
the subcommand must exist and every option token must be recognized -- using
click's low-level ``OptionParser`` so value validators (``Path(exists=True)``,
callbacks, type coercion, required-option enforcement) do NOT run. We are
verifying the CLI surface the repo depends on, not the runtime values a
particular invocation carries.

The repo already depends on ``imbue-mngr`` (editable, pointing at
``vendor/mngr/libs/mngr``), so importing the live tree costs nothing extra and
keeps the check in lockstep with whatever mngr the repo is pinned to.
"""

from __future__ import annotations

from typing import Sequence

import click
from imbue.mngr.main import cli


class MngrArgvContractError(AssertionError):
    """Raised when an argv is not accepted by the live mngr CLI surface."""


def assert_mngr_argv_valid(argv: Sequence[str]) -> None:
    """Assert that ``argv`` is structurally accepted by the live mngr CLI.

    ``argv`` is a full command line whose first element is the mngr binary
    (``"mngr"`` or an absolute path -- it is ignored, only ``argv[1:]`` is
    validated). Resolves the (possibly nested) subcommand against the live
    click tree and parses the remaining tokens with each command's low-level
    option parser.

    Raises ``MngrArgvContractError`` when the subcommand does not exist or any
    option token is unrecognized -- i.e. exactly the drift that a vendor/mngr
    CLI change would introduce. Does not raise on value-level problems
    (nonexistent paths, missing required options): those are not CLI-surface
    drift and would make the contract check brittle.
    """
    tokens = list(argv[1:])
    group: click.Command = cli
    parent_ctx = click.Context(cli, info_name="mngr")
    try:
        while True:
            name, command, rest = group.resolve_command(parent_ctx, tokens)
            assert command is not None  # resolve_command raises rather than returning None
            sub_ctx = click.Context(command, info_name=name, parent=parent_ctx)
            if isinstance(command, click.Group):
                group, parent_ctx, tokens = command, sub_ctx, rest
                continue
            # Leaf command: the low-level parser recognizes/rejects option
            # tokens and handles arity without running click's value
            # converters (which would, e.g., reject a not-yet-created file).
            command.make_parser(sub_ctx).parse_args(args=list(rest))
            return
    except click.exceptions.ClickException as exc:
        raise MngrArgvContractError(
            f"mngr argv not accepted by the live CLI: {list(argv)!r}\n"
            f"  {type(exc).__name__}: {exc.format_message()}\n"
            f"  The vendored mngr CLI surface changed under this invocation. "
            f"Update the producing code to match the current mngr CLI."
        ) from exc
