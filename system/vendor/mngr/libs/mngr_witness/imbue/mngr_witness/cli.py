from abc import ABC
from abc import abstractmethod
from pathlib import Path
from typing import Final

import click
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.mngr.api.providers import get_local_host
from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr_behaviors.corpus import behavior_unit_kind_record_value
from imbue.mngr_behaviors.data_types import BehaviorUnitKind
from imbue.mngr_mapreduce.execution import Execution
from imbue.mngr_mapreduce.execution import ExecutionPlan
from imbue.mngr_mapreduce.interfaces import PipelineExecutorInterface
from imbue.mngr_mapreduce.manifest import MANIFEST_FILENAME
from imbue.mngr_mapreduce.mngr_executor import MngrPipelineExecutor
from imbue.mngr_mapreduce.utils import get_base_commit
from imbue.mngr_mapreduce.utils import make_run_name
from imbue.mngr_witness.agent_type import WITNESS_CLAUDE_AGENT_TYPE_NAME
from imbue.mngr_witness.agent_type import warn_about_attended_agent_types
from imbue.mngr_witness.bindings import ANNEAL_JOB_SLUG
from imbue.mngr_witness.bindings import WitnessRun
from imbue.mngr_witness.bindings import execution_context
from imbue.mngr_witness.bindings import make_witness_bindings
from imbue.mngr_witness.bindings import make_witness_run
from imbue.mngr_witness.corpus import UnitSelection
from imbue.mngr_witness.pipeline import REDUCE_NODE_NAME
from imbue.mngr_witness.pipeline import make_witness_pipeline
from imbue.mngr_witness.plan import DEFAULT_AGENT_TIMEOUT_SECONDS
from imbue.mngr_witness.plan import NodeOverride
from imbue.mngr_witness.plan import make_execution_plan_from_flags
from imbue.mngr_witness.plan import parse_node_env_flag
from imbue.mngr_witness.plan import parse_node_provider_flag
from imbue.mngr_witness.prompts import discover_prompt_variants

_UNIT_KIND_BY_CLI_VALUE: Final[dict[str, BehaviorUnitKind]] = {
    behavior_unit_kind_record_value(kind): kind for kind in BehaviorUnitKind
}
_DEFAULT_PIPELINE_NAME: Final[str] = "witness"
_GENERATION_ROOT_DIRNAME: Final[str] = "witnesses"


class ExecutionIncompleteError(MngrError):
    """Raised when an execution stopped before its last node, so the command exits non-zero."""

    ...


class WitnessCliOptions(CommonCliOptions):
    """Options for the ``mngr witness`` command; the help text of each lives on its click option."""

    root: str
    tests: tuple[str, ...]
    feature: tuple[str, ...]
    area: str | None
    tag: str | None
    unit: str | None
    provider: str
    node_provider: tuple[str, ...]
    node_env: tuple[str, ...]
    env: tuple[str, ...]
    agent_template: tuple[str, ...]
    agent_type: str
    max_running_agents: int | None
    timeout: float
    output_dir: str | None
    name: str
    changelog_branch: str | None
    generation_root: str | None
    keep_hosts: bool


class WitnessInvocation(FrozenModel):
    """Everything one invocation resolved before launching: the run, the execution to perform, and where its results go."""

    repo_root: Path = Field(description="The operator checkout")
    output_dir: Path = Field(description="Where archives and the manifest go")
    run: WitnessRun = Field(description="The corpus, its selected units, and the prompt variants")
    execution: Execution = Field(description="The execution to perform")
    deliverable_branch: str = Field(description="The branch the operator reviews when the execution completes")


class WitnessExecutorFactoryInterface(MutableModel, ABC):
    """Builds the executor an invocation runs on; the command uses mngr, tests use scripted agents."""

    @abstractmethod
    def make_executor(self, invocation: WitnessInvocation, mngr_ctx: MngrContext) -> PipelineExecutorInterface:
        """Build an executor for the invocation's pipeline, bindings, and execution."""


class MngrWitnessExecutorFactory(WitnessExecutorFactoryInterface):
    """Runs the invocation on mngr agents placed by its execution plan."""

    def make_executor(self, invocation: WitnessInvocation, mngr_ctx: MngrContext) -> PipelineExecutorInterface:
        return MngrPipelineExecutor(
            pipeline=make_witness_pipeline(invocation.run.variants),
            bindings=make_witness_bindings(
                invocation.run,
                source_dir=invocation.repo_root,
                output_dir=invocation.output_dir,
                base_commit=invocation.execution.base_commit,
                cg=mngr_ctx.concurrency_group,
                observers=(),
            ),
            concurrency_group=mngr_ctx.concurrency_group,
            execution=invocation.execution,
            mngr_ctx=mngr_ctx,
            source_host=get_local_host(mngr_ctx),
        )


@pure
def effective_test_roots(corpus_root: Path, test_roots: tuple[Path, ...]) -> tuple[Path, ...]:
    """The test roots, defaulting to the corpus root's parent as ``mngr behaviors matrix`` does."""
    return test_roots if test_roots else (corpus_root.parent,)


def resolve_generation_root(corpus_root: Path, repo_root: Path, override: str | None) -> Path:
    """``<project>/imbue/<package>/witnesses`` when the project has exactly one package there, else ``<project>/witnesses``."""
    if override is not None:
        return Path(override)
    project = corpus_root.parent
    package_parent = repo_root / project / "imbue"
    if package_parent.is_dir():
        packages = sorted(
            child for child in package_parent.iterdir() if child.is_dir() and not child.name.startswith(".")
        )
        if len(packages) == 1:
            return project / "imbue" / packages[0].name / _GENERATION_ROOT_DIRNAME
    return project / _GENERATION_ROOT_DIRNAME


def build_plan(opts: WitnessCliOptions, source_dir: Path, output_dir: Path) -> ExecutionPlan:
    overrides: list[NodeOverride] = [parse_node_provider_flag(value) for value in opts.node_provider]
    overrides.extend(parse_node_env_flag(value) for value in opts.node_env)
    return make_execution_plan_from_flags(
        provider=opts.provider,
        agent_type=opts.agent_type,
        env=opts.env,
        templates=opts.agent_template,
        node_overrides=overrides,
        max_running_agents=opts.max_running_agents,
        agent_timeout_seconds=opts.timeout,
        source_dir=source_dir,
        output_dir=output_dir,
        is_keeping_hosts=opts.keep_hosts,
    )


def prepare_witness_invocation(opts: WitnessCliOptions, repo_root: Path, mngr_ctx: MngrContext) -> WitnessInvocation:
    """Resolve everything an invocation needs before any agent launches: the plan, the run, and the execution."""
    corpus_root = Path(opts.root)
    execution_name = make_run_name()
    output_dir = Path(opts.output_dir) if opts.output_dir is not None else repo_root / f"{opts.name}_{execution_name}"
    output_dir.mkdir(parents=True, exist_ok=True)
    deliverable_branch = f"{opts.name}/{execution_name}/{REDUCE_NODE_NAME}/{ANNEAL_JOB_SLUG}"
    plan = build_plan(opts, source_dir=repo_root, output_dir=output_dir)
    warn_about_attended_agent_types(mngr_ctx, plan)
    run = make_witness_run(
        repo_root=repo_root,
        corpus_root=corpus_root,
        test_roots=effective_test_roots(corpus_root, tuple(Path(root) for root in opts.tests)),
        generation_root=resolve_generation_root(corpus_root, repo_root, opts.generation_root),
        selection=UnitSelection(
            feature_paths=tuple(Path(feature) for feature in opts.feature),
            area=opts.area,
            tag=opts.tag,
            unit_kind=None if opts.unit is None else _UNIT_KIND_BY_CLI_VALUE[opts.unit],
        ),
        changelog_branch=opts.changelog_branch if opts.changelog_branch is not None else deliverable_branch,
        variants=discover_prompt_variants(repo_root / corpus_root.parent),
    )
    execution = Execution(
        pipeline_name=opts.name,
        execution_name=execution_name,
        base_commit=get_base_commit(repo_root, mngr_ctx.concurrency_group),
        context=execution_context(run),
        plan=plan,
    )
    return WitnessInvocation(
        repo_root=repo_root, output_dir=output_dir, run=run, execution=execution, deliverable_branch=deliverable_branch
    )


def run_witness_command(
    opts: WitnessCliOptions, mngr_ctx: MngrContext, factory: WitnessExecutorFactoryInterface
) -> Execution:
    """Prepare the invocation, execute it, and report; raises ExecutionIncompleteError when it stopped early."""
    invocation = prepare_witness_invocation(opts, repo_root=Path.cwd(), mngr_ctx=mngr_ctx)
    write_human_line(
        "Selected {} unit(s) in {} feature file(s)",
        sum(len(task.units) for task in invocation.run.tasks),
        len(invocation.run.tasks),
    )
    final = factory.make_executor(invocation, mngr_ctx).execute()
    write_human_line(
        "Execution {} finished; manifest at {}",
        invocation.execution.execution_name,
        invocation.output_dir / MANIFEST_FILENAME,
    )
    if final.stopped_reason is not None:
        raise ExecutionIncompleteError(
            f"Execution {invocation.execution.execution_name} stopped early: {final.stopped_reason}"
        )
    write_human_line("Deliverable branch: {}", invocation.deliverable_branch)
    return final


@click.command("witness")
@click.option(
    "--root",
    required=True,
    type=click.Path(exists=True, file_okay=False),
    help="Behavior corpus root, conventionally <project>/behaviors; repo-relative, run from the repo root.",
)
@click.option(
    "--tests",
    multiple=True,
    type=click.Path(exists=True, file_okay=False),
    help="Test root whose witnesses markers count [repeatable; default: the corpus root's parent]",
)
@click.option(
    "--feature", multiple=True, help="Root-relative .feature path to run; composes with the other filters [repeatable]"
)
@click.option("--area", default=None, help="Only units under this folder area of the corpus")
@click.option("--tag", default=None, help="Only units carrying this tag or coordinate")
@click.option(
    "--unit", default=None, type=click.Choice(sorted(_UNIT_KIND_BY_CLI_VALUE)), help="Only units of this kind"
)
@click.option(
    "--provider",
    default="local",
    show_default=True,
    help="Provider every node runs on unless --node-provider says otherwise",
)
@click.option("--node-provider", multiple=True, help="Place one node elsewhere, as <node>=<provider> [repeatable]")
@click.option(
    "--node-env", multiple=True, help="Environment for one node's agents only, as <node>:KEY=VALUE [repeatable]"
)
@click.option("--env", multiple=True, help="Environment variable KEY=VALUE for every agent [repeatable]")
@click.option(
    "-t", "--agent-template", multiple=True, help="Create template to apply to every host and agent [repeatable]"
)
@click.option(
    "--agent-type",
    default=str(WITNESS_CLAUDE_AGENT_TYPE_NAME),
    show_default=True,
    help="Agent type to launch; the default is claude with permission prompts and startup dialogs switched off",
)
@click.option(
    "--max-running-agents",
    default=None,
    type=int,
    help="How many of a node's agents may run at once [default: 6 on local, unbounded elsewhere]",
)
@click.option(
    "--timeout",
    default=DEFAULT_AGENT_TIMEOUT_SECONDS,
    show_default=True,
    type=float,
    help="Seconds each agent may run before it is stopped",
)
@click.option(
    "--output-dir",
    default=None,
    type=click.Path(),
    help="Where archives, the manifest, and the report go [default: witness_<execution name>/]",
)
@click.option(
    "--name", default=_DEFAULT_PIPELINE_NAME, show_default=True, help="Prefix of every agent, host, and branch name"
)
@click.option(
    "--changelog-branch", default=None, help="Name the changelog entries carry [default: the deliverable branch]"
)
@click.option(
    "--generation-root", default=None, help="Where new witness modules go [default: the project's witnesses package]"
)
@click.option(
    "--keep-hosts",
    is_flag=True,
    default=False,
    help="Leave each node's hosts alive after it ends, for live debugging",
)
@add_common_options
@click.pass_context
def witness(ctx: click.Context, **kwargs: object) -> None:
    mngr_ctx, _output_opts, opts = setup_command_context(
        ctx=ctx, command_name="witness", command_class=WitnessCliOptions
    )
    run_witness_command(opts, mngr_ctx, MngrWitnessExecutorFactory())


CommandHelpMetadata(
    key="witness",
    one_line_description="Converge the tests witnessing a behavior corpus (setup, map, review, integrate, reduce)",
    synopsis="mngr witness --root <CORPUS> [--tests <PATH>...] [--feature <PATH>...] [--area <AREA>] [--tag <TAG>] [--unit <KIND>] [--provider <PROVIDER>] [--node-provider <NODE>=<PROVIDER>...] [--node-env <NODE>:KEY=VALUE...]",
    description="""Runs the witness pipeline over a behavior corpus, in five nodes:

1. setup: one agent finds or makes the shared test scaffolding every mapper
   will use, and writes a guide to it.
2. map: one agent per feature file converges that file's units to full
   coverage, tracing every assertion to the clause it witnesses.
3. review: one adversarial agent per mapper checks the trace, the verdicts,
   and the partial notes, and fixes what it can.
4. integrate: the orchestrator cherry-picks the reviewed branches together,
   recording what conflicted.
5. reduce: one agent integrates what conflicted, collapses duplicated
   scaffolding, verifies, and writes the changelog.

Mechanical gates run on every agent node; a branch that fails one is not
carried forward. The corpus is read-only to every agent. The execution plan
(where each node runs, with what environment) comes from --provider,
--node-provider, and --node-env.""",
    examples=(
        ("Run all locally", "mngr witness --root libs/mngr_forward/behaviors"),
        (
            "Mappers on modal, reduce on this machine",
            "mngr witness --root libs/mngr_forward/behaviors --provider modal --node-provider reduce=local",
        ),
        (
            "Give only the reduce node a token",
            "mngr witness --root libs/mngr_forward/behaviors --node-env reduce:GH_TOKEN=...",
        ),
        (
            "One feature file",
            "mngr witness --root libs/mngr_forward/behaviors --feature authentication/signin.feature",
        ),
    ),
    see_also=(("behaviors", "Inspect and validate a behavior corpus"),),
).register()

add_pager_help_option(witness)
