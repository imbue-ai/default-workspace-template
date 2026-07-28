"""Validate that a ``mngr <subcommand> ...`` argv is accepted by the *live* mngr CLI.

Repo code shells out to the ``mngr`` CLI by constructing argvs. A test that
pins such an argv against a *hand-written expected argv* (via a stubbed
subprocess runner) only confirms "the code emits the bytes we told it to
emit" -- the expected argv is authored from the same assumption as the
production code, so the two drift together and the test can never notice when
system/vendor/mngr renames or removes the subcommand or one of its flags. That
divergence then surfaces only at runtime.

``assert_mngr_argv_valid`` closes that gap by resolving the argv against the
actual ``imbue.mngr.main.cli`` click command tree. It checks *shape* only --
the subcommand must exist and every option token must be recognized -- using
click's low-level ``OptionParser`` so value validators (``Path(exists=True)``,
callbacks, type coercion, required-option enforcement) do NOT run. We are
verifying the CLI surface the repo depends on, not the runtime values a
particular invocation carries.

``-S KEY=VALUE`` config overrides are the one exception to the shape-only rule:
click sees an opaque string there, but mngr resolves the key path against its
config model at startup and hard-fails the command when it does not exist. Those
are checked too, through mngr's own resolution (see ``assert_mngr_settings_valid``).

This lives in its own workspace package so both repo-side pytest passes (the
root pass and the isolated system/libs/system_interface pass, which share one
workspace venv) import a single copy rather than duplicating the validator.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import click
from imbue.mngr.cli.common_opts import apply_settings_to_config
from imbue.mngr.config.data_types import MngrConfig, TmuxConfig
from imbue.mngr.errors import MngrError

# Importing the CLI is also what loads the mngr plugins, and therefore what
# registers the per-agent-type config classes that a
# ``-S agent_types.<type>....`` override is resolved against.
from imbue.mngr.main import cli
from imbue.mngr.utils.logging import LoggingConfig

# click parameter name of mngr's ``-S`` / ``--setting`` option, i.e. the key its
# values arrive under in a parsed option dict.
_SETTING_PARAM_NAME = "setting"


class MngrArgvContractError(AssertionError):
    """Raised when an argv is not accepted by the live mngr CLI surface."""


class MngrSettingContractError(MngrArgvContractError):
    """Raised when a ``-S KEY=VALUE`` override does not resolve against mngr's config."""


def assert_mngr_argv_valid(argv: Sequence[str]) -> None:
    """Assert that ``argv`` is structurally accepted by the live mngr CLI.

    ``argv`` is a full command line whose first element is the mngr binary
    (``"mngr"`` or an absolute path -- it is ignored, only ``argv[1:]`` is
    validated). Resolves the (possibly nested) subcommand against the live
    click tree, parses the remaining tokens with each command's low-level
    option parser, and resolves any ``-S`` overrides against the config model.

    Raises ``MngrArgvContractError`` when the subcommand does not exist, an
    option token is unrecognized, or a ``-S`` key path does not resolve -- i.e.
    exactly the drift that a system/vendor/mngr change would introduce. Does not
    raise on other value-level problems (nonexistent paths, missing required
    options): those are not CLI-surface drift and would make the contract check
    brittle.
    """
    try:
        options = _resolve_against_cli(
            cli, click.Context(cli, info_name="mngr"), list(argv[1:])
        )
    except click.exceptions.ClickException as exc:
        raise MngrArgvContractError(
            f"mngr argv not accepted by the live CLI: {list(argv)!r}\n"
            f"  {type(exc).__name__}: {exc.format_message()}"
        ) from exc
    assert_mngr_settings_valid(options.get(_SETTING_PARAM_NAME, ()))


def assert_mngr_settings_valid(settings: Sequence[str]) -> None:
    """Assert that every ``KEY=VALUE`` in ``settings`` resolves against mngr's config.

    Each override is applied through mngr's own ``apply_settings_to_config`` --
    the call ``setup_command_context`` makes for the CLI flags -- so the key
    path, the value's scalar parse, and the owning section's field validation
    all run exactly as they will at create time. This is the part click cannot
    check: to it a ``-S`` value is an opaque string, while mngr rejects an
    unresolvable key path outright and fails the whole command.

    ``settings`` is the ``-S`` / ``--setting`` payload list as click's own parser
    reported it, so every spelling click accepts (``-S K=V``, ``-SK=V``,
    ``--setting=K=V``) is covered without this module re-deriving any of them.

    The overrides are applied to a bare constructed config rather than the
    repo's loaded settings, so an ``__extend`` suffix extends from nothing. That
    does not affect whether the key path resolves, which is what is pinned here.
    """
    if not settings:
        return
    base_config = MngrConfig.model_construct(
        prefix="mngr-",
        default_host_dir=Path("~/.mngr"),
        agent_types={},
        providers={},
        plugins={},
        logging=LoggingConfig(),
        tmux=TmuxConfig(),
        commands={},
    )
    for setting in settings:
        try:
            apply_settings_to_config(base_config, [setting], frozenset())
        except MngrError as exc:
            raise MngrSettingContractError(
                f"mngr --setting not accepted by the live config model: {setting!r}\n"
                f"  {type(exc).__name__}: {exc}"
            ) from exc


def _resolve_against_cli(
    command: click.Command, ctx: click.Context, tokens: list[str]
) -> dict[str, Any]:
    """Descend the click tree for ``tokens``, raising on an unknown subcommand
    or option, and return the leaf command's parsed options.

    Recurses through nested groups (mngr's tree is shallow); a leaf command's
    low-level parser recognizes/rejects option tokens and handles arity without
    running click's value converters (which would, e.g., reject a
    not-yet-created file). The returned dict is keyed by click parameter name --
    it is how the ``-S`` payloads reach ``assert_mngr_settings_valid`` already
    split out of the argv, in every spelling click accepts."""
    if isinstance(command, click.Group):
        name, subcommand, rest = command.resolve_command(ctx, tokens)
        if subcommand is None:
            raise click.exceptions.UsageError(f"No such command {name!r}.")
        return _resolve_against_cli(
            subcommand, click.Context(subcommand, info_name=name, parent=ctx), rest
        )
    options, _arguments, _param_order = command.make_parser(ctx).parse_args(
        args=list(tokens)
    )
    return options
