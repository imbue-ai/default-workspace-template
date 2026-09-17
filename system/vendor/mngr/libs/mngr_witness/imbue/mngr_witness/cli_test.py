from pathlib import Path

import pluggy
import pytest
from click.testing import CliRunner
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr_behaviors.testing import write_behavior_corpus
from imbue.mngr_mapreduce.execution import Execution
from imbue.mngr_mapreduce.interfaces import PipelineExecutorInterface
from imbue.mngr_mapreduce.mngr_executor import MngrPipelineExecutor
from imbue.mngr_mapreduce.primitives import NodeName
from imbue.mngr_witness.bindings import make_witness_bindings
from imbue.mngr_witness.cli import ExecutionIncompleteError
from imbue.mngr_witness.cli import MngrWitnessExecutorFactory
from imbue.mngr_witness.cli import WitnessCliOptions
from imbue.mngr_witness.cli import WitnessExecutorFactoryInterface
from imbue.mngr_witness.cli import WitnessInvocation
from imbue.mngr_witness.cli import effective_test_roots
from imbue.mngr_witness.cli import prepare_witness_invocation
from imbue.mngr_witness.cli import resolve_generation_root
from imbue.mngr_witness.cli import run_witness_command
from imbue.mngr_witness.cli import witness
from imbue.mngr_witness.corpus import CorpusInvalidError
from imbue.mngr_witness.gitops import file_text_at
from imbue.mngr_witness.mock_witness_test import ScriptedWitnessExecutor
from imbue.mngr_witness.pipeline import make_witness_pipeline

_CORPUS_ROOT = "libs/proj/behaviors"
_SIGNIN = """Feature: Sign-in

  @fresh-code
  Scenario: Opening a fresh login URL signs the user in
    When they open the login URL
    Then the browser lands on "/"
"""


def _git(cg: ConcurrencyGroup, repo: Path, *args: str) -> str:
    return cg.run_process_to_completion(["git", *args], cwd=repo).stdout.strip()


def _options(root: str = _CORPUS_ROOT, output_dir: str | None = None) -> WitnessCliOptions:
    return WitnessCliOptions(
        output_format="human",
        quiet=False,
        verbose=0,
        log_file=None,
        log_commands=None,
        plugin=(),
        disable_plugin=(),
        root=root,
        tests=(),
        feature=(),
        area=None,
        tag=None,
        unit=None,
        provider="local",
        node_provider=("reduce=local",),
        node_env=("reduce:GH_TOKEN=secret",),
        env=(),
        agent_template=(),
        agent_type="witness-claude",
        max_running_agents=None,
        timeout=60.0,
        output_dir=output_dir,
        name="witness",
        changelog_branch=None,
        generation_root=None,
        keep_hosts=False,
    )


@pytest.fixture
def corpus_repo(temp_git_repo: Path, cg: ConcurrencyGroup) -> Path:
    write_behavior_corpus(temp_git_repo / _CORPUS_ROOT, {"authentication/signin.feature": _SIGNIN})
    (temp_git_repo / "libs/proj/imbue/proj").mkdir(parents=True)
    (temp_git_repo / "libs/proj/imbue/proj/__init__.py").write_text("")
    _git(cg, temp_git_repo, "add", ".")
    _git(cg, temp_git_repo, "commit", "--quiet", "-m", "corpus")
    return temp_git_repo


class _ScriptedFactory(WitnessExecutorFactoryInterface):
    """Runs the invocation on scripted agents instead of mngr."""

    cg: ConcurrencyGroup = Field(frozen=True, description="Runs git")
    published_root: Path = Field(frozen=True, description="Where scripted agents leave their archives")

    def make_executor(self, invocation: WitnessInvocation, mngr_ctx: MngrContext) -> PipelineExecutorInterface:
        return ScriptedWitnessExecutor(
            pipeline=make_witness_pipeline(invocation.run.variants),
            bindings=make_witness_bindings(
                invocation.run,
                source_dir=invocation.repo_root,
                output_dir=invocation.output_dir,
                base_commit=invocation.execution.base_commit,
                cg=self.cg,
                observers=(),
            ),
            concurrency_group=self.cg,
            execution=invocation.execution,
            repo=invocation.repo_root,
            cg=self.cg,
            witness_run=invocation.run,
            published_root=self.published_root,
        )


class _StoppingExecutor(PipelineExecutorInterface):
    """Reports an execution that stopped before its last node."""

    execution: Execution = Field(frozen=True, description="The execution to hand back, stopped")

    def execute(self) -> Execution:
        return self.execution.with_stopped_reason("Node 'map' failed: nothing to continue from")


class _StoppingFactory(WitnessExecutorFactoryInterface):
    def make_executor(self, invocation: WitnessInvocation, mngr_ctx: MngrContext) -> PipelineExecutorInterface:
        return _StoppingExecutor(execution=invocation.execution)


def test_help_lists_the_placement_flags(cli_runner: CliRunner) -> None:
    result = cli_runner.invoke(witness, ["--help"])

    assert result.exit_code == 0, result.output
    for flag in ("--root", "--node-provider", "--node-env", "--max-running-agents", "--keep-hosts"):
        assert flag in result.output


def test_the_command_refuses_a_corpus_with_violations_before_launching_anything(
    cli_runner: CliRunner, plugin_manager: pluggy.PluginManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = write_behavior_corpus(
        tmp_path / "behaviors", {"area/untagged.feature": "Feature: X\n\n  Scenario: no tag\n    Then it happens\n"}
    )
    monkeypatch.chdir(tmp_path)

    result = cli_runner.invoke(
        witness, ["--root", str(broken.relative_to(tmp_path))], obj=plugin_manager, catch_exceptions=True
    )

    assert result.exit_code != 0
    assert isinstance(result.exception, CorpusInvalidError) or "language violations" in result.output


def test_prepare_resolves_the_plan_the_run_and_the_execution(corpus_repo: Path, temp_mngr_ctx: MngrContext) -> None:
    invocation = prepare_witness_invocation(_options(), repo_root=corpus_repo, mngr_ctx=temp_mngr_ctx)

    assert [task.slug for task in invocation.run.tasks] == ["authentication-signin"]
    assert invocation.run.generation_root == Path("libs/proj/imbue/proj/witnesses")
    assert invocation.run.changelog_branch == invocation.deliverable_branch
    assert invocation.deliverable_branch == f"witness/{invocation.execution.execution_name}/reduce/integrated"
    assert invocation.output_dir == corpus_repo / f"witness_{invocation.execution.execution_name}"
    plan = invocation.execution.plan
    assert {var.key for var in plan.placement_for(NodeName("reduce")).env_options.env_vars} == {"GH_TOKEN"}
    assert plan.placement_for(NodeName("map")).agent_type == "witness-claude"


def test_run_witness_command_executes_the_pipeline_and_names_the_deliverable(
    corpus_repo: Path,
    temp_mngr_ctx: MngrContext,
    cg: ConcurrencyGroup,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(corpus_repo)
    factory = _ScriptedFactory(cg=cg, published_root=tmp_path / "published")

    final = run_witness_command(_options(output_dir=str(tmp_path / "out")), temp_mngr_ctx, factory)

    assert final.stopped_reason is None
    deliverable = f"witness/{final.execution_name}/reduce/integrated"
    assert (
        file_text_at(cg, corpus_repo, deliverable, "libs/proj/imbue/proj/witnesses/authentication/signin_test.py")
        is not None
    )


def test_run_witness_command_raises_when_the_execution_stops_early(
    corpus_repo: Path, temp_mngr_ctx: MngrContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(corpus_repo)

    with pytest.raises(ExecutionIncompleteError, match="stopped early"):
        run_witness_command(_options(output_dir=str(tmp_path / "out")), temp_mngr_ctx, _StoppingFactory())


def test_the_mngr_factory_builds_an_executor_without_launching(corpus_repo: Path, temp_mngr_ctx: MngrContext) -> None:
    invocation = prepare_witness_invocation(_options(), repo_root=corpus_repo, mngr_ctx=temp_mngr_ctx)

    executor = MngrWitnessExecutorFactory().make_executor(invocation, temp_mngr_ctx)

    assert isinstance(executor, MngrPipelineExecutor)
    assert executor.execution == invocation.execution


def test_test_roots_default_to_the_corpus_roots_parent() -> None:
    corpus_root = Path("libs/mngr_forward/behaviors")

    assert effective_test_roots(corpus_root, ()) == (Path("libs/mngr_forward"),)
    assert effective_test_roots(corpus_root, (Path("apps/x"),)) == (Path("apps/x"),)


def test_generation_root_lands_in_the_projects_single_package_when_there_is_one(tmp_path: Path) -> None:
    (tmp_path / "libs/proj/imbue/proj").mkdir(parents=True)
    (tmp_path / "libs/other/imbue/one").mkdir(parents=True)
    (tmp_path / "libs/other/imbue/two").mkdir(parents=True)

    assert resolve_generation_root(Path("libs/proj/behaviors"), tmp_path, None) == Path(
        "libs/proj/imbue/proj/witnesses"
    )
    assert resolve_generation_root(Path("libs/other/behaviors"), tmp_path, None) == Path("libs/other/witnesses")
    assert resolve_generation_root(Path("libs/proj/behaviors"), tmp_path, "custom/dir") == Path("custom/dir")
