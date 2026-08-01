"""CLI command for behavior-anchored test-mapreduce (``mngr tmr-behaviors``).

The framework (:mod:`imbue.mngr_mapreduce`) does the heavy lifting. This
module defines the click command, parses the behavior-recipe options on top of
the framework's common options, and hands off to ``run_mapreduce``.
"""

from pathlib import Path
from typing import Final

import click

from imbue.imbue_common.pure import pure
from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr_behaviors.corpus import behavior_unit_kind_record_value
from imbue.mngr_behaviors.data_types import BehaviorUnitKind
from imbue.mngr_mapreduce.cli import MapReduceCliOptions
from imbue.mngr_mapreduce.cli import add_mapreduce_options
from imbue.mngr_mapreduce.cli import run_mapreduce
from imbue.mngr_tmr.behavior_recipe import BehaviorMapReduceRecipe
from imbue.mngr_tmr.cli import SplitTestingFlagsCommand

_UNIT_KIND_BY_CLI_VALUE: Final[dict[str, BehaviorUnitKind]] = {
    behavior_unit_kind_record_value(kind): kind for kind in BehaviorUnitKind
}


class BehaviorTmrCliOptions(MapReduceCliOptions):
    """Options for the ``mngr tmr-behaviors`` command (behavior-recipe specifics on top of the framework's)."""

    name: str
    root: str
    tests: tuple[str, ...]
    area: str | None
    tag: str | None
    unit: str | None
    mapper_prompt: str | None
    reducer_prompt: str | None
    testing_flags: tuple[str, ...]


@pure
def effective_test_roots(corpus_root: Path, test_roots: tuple[Path, ...]) -> tuple[Path, ...]:
    """Resolve the test roots, defaulting to the corpus root's parent (matrix's convention)."""
    if test_roots:
        return test_roots
    return (corpus_root.parent,)


@click.command("tmr-behaviors", cls=SplitTestingFlagsCommand)
@click.option(
    "--root",
    required=True,
    type=click.Path(exists=True, file_okay=False),
    help="Behavior corpus root, conventionally <project>/behaviors (e.g. apps/minds/behaviors). "
    "Repo-relative; run from the repo root.",
)
@click.option(
    "--tests",
    multiple=True,
    type=click.Path(exists=True),
    help="Test root witnessing the corpus; repeatable. Defaults to the corpus root's parent "
    "(a corpus at <project>/behaviors is witnessed by <project>'s tests).",
)
@click.option(
    "--area",
    default=None,
    help="Only fan out units in this folder subtree, named as a dot-joined folder path from the "
    "corpus root (e.g. 'browser-authorization').",
)
@click.option(
    "--tag",
    default=None,
    help="Only fan out units with this exact raw tag or exact coordinate.",
)
@click.option(
    "--unit",
    type=click.Choice(sorted(_UNIT_KIND_BY_CLI_VALUE)),
    default=None,
    help="Only fan out units of this kind.",
)
@click.option(
    "--name",
    default="tmr-behaviors",
    show_default=True,
    help="Variant name, used as the prefix for this run's agent/branch/host names "
    "(e.g. tmr-behaviors-minds) so distinct corpora stay separable and reviewable on their own. "
    "Distinct from --run-name, which identifies one run within a variant.",
)
@click.option(
    "--mapper-prompt",
    "mapper_prompt",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Override the packaged mapper prompt with this Jinja template file. It may "
    "'{% extends %}' the packaged behavior_mapper.j2 by name and fill its "
    "project_guidance / infra_blockers blocks.",
)
@click.option(
    "--reducer-prompt",
    "reducer_prompt",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Override the packaged reducer prompt with this Jinja template file. It may "
    "'{% extends %}' or '{% include %}' the packaged behavior_reducer.j2 by name.",
)
@add_mapreduce_options
@add_common_options
@click.pass_context
def tmr_behaviors(ctx: click.Context, **kwargs: object) -> None:
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="tmr-behaviors",
        command_class=BehaviorTmrCliOptions,
    )
    corpus_root = Path(opts.root)
    recipe = BehaviorMapReduceRecipe(
        name=opts.name,
        corpus_root=corpus_root,
        test_roots=effective_test_roots(corpus_root, tuple(Path(t) for t in opts.tests)),
        area=opts.area,
        tag=opts.tag,
        unit_kind=None if opts.unit is None else _UNIT_KIND_BY_CLI_VALUE[opts.unit],
        testing_flags=opts.testing_flags,
        mapper_prompt_path=Path(opts.mapper_prompt) if opts.mapper_prompt is not None else None,
        reducer_prompt_path=Path(opts.reducer_prompt) if opts.reducer_prompt is not None else None,
    )
    run_mapreduce(recipe, opts, mngr_ctx, output_opts)


CommandHelpMetadata(
    key="tmr-behaviors",
    one_line_description="Create and update the tests witnessing a behavior corpus (behavior map-reduce)",
    synopsis="mngr tmr-behaviors --root <CORPUS> [--tests <PATH>...] [--area <AREA>] [--tag <TAG>] [--unit <KIND>] [-- TESTING_FLAGS...] [--name <VARIANT>] [--mapper-prompt <FILE>] [--reducer-prompt <FILE>] [--provider <PROVIDER>]",
    description="""This command implements a map-reduce pattern anchored on behaviors:

1. Scans the corpus at --root (see `mngr behaviors`), fail-fasting on language
   violations, and groups its units (scenarios, scenario outlines, and
   invariant Rules) into one task per .feature file.
2. Launches one agent per feature file. Each agent converges the tests
   witnessing that file's units to the units' scope: creating missing
   witnesses, extending or trimming existing ones, fixing the
   implementation where it diverges from the behavior, and keeping the
   `witnesses(coordinate, partial=...)` markers honest.
3. Polls agents until all finish or individually time out. An HTML report
   (task sections plus a per-coordinate coverage matrix) is updated
   continuously during polling.
4. Pulls each agent's changes into branches named <name>/<run>/*.
5. If any work succeeded, launches a reducer agent that integrates the
   branches (squash the test kinds, cherry-pick FIX_IMPL by priority),
   dedupes fixtures parallel mappers created independently, and audits the
   witness links by running `mngr behaviors matrix` over the integrated tree.
6. The corpus itself is read-only to the whole pipeline: mappers may only
   propose corpus edits via the report's behavior-escalations section, and an
   integrated branch that touches the corpus is mechanically refused.

Arguments after -- are pytest flags appended to the mappers' test runs.

Use --area/--tag/--unit to scope a run to part of the corpus, e.g.:

  mngr tmr-behaviors --root apps/minds/behaviors --area browser-authorization

Use --name to give a corpus variant its own prefix, and --mapper-prompt to
point it at a variant template that extends the packaged behavior_mapper.j2:

  mngr tmr-behaviors --root apps/minds/behaviors --name tmr-behaviors-minds --mapper-prompt apps/minds/tmr/behaviors_mapper.j2""",
    examples=(
        ("Fan out the whole minds corpus", "mngr tmr-behaviors --root apps/minds/behaviors"),
        ("Scope to one area", "mngr tmr-behaviors --root apps/minds/behaviors --area browser-authorization"),
        ("Only the invariant Rules", "mngr tmr-behaviors --root apps/minds/behaviors --unit rule"),
        (
            "The minds variant",
            "mngr tmr-behaviors --root apps/minds/behaviors --name tmr-behaviors-minds --mapper-prompt apps/minds/tmr/behaviors_mapper.j2",
        ),
        ("Modal provider (snapshot is automatic)", "mngr tmr-behaviors --root apps/minds/behaviors --provider modal"),
    ),
    see_also=(
        ("tmr", "Run and fix tests in parallel using agents (docstring-anchored)"),
        ("behaviors", "Inspect and validate a behavior corpus"),
        ("create", "Create a new agent"),
    ),
).register()

add_pager_help_option(tmr_behaviors)
