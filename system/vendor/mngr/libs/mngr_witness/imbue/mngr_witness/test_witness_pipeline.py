"""The whole witness pipeline, end to end, with scripted agents standing in for the real ones."""

from pathlib import Path
from uuid import uuid4

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.primitives import NonNegativeFloat
from imbue.imbue_common.primitives import NonNegativeInt
from imbue.imbue_common.primitives import PositiveFloat
from imbue.imbue_common.primitives import PositiveInt
from imbue.mngr.interfaces.host import AgentEnvironmentOptions
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_behaviors.testing import write_behavior_corpus
from imbue.mngr_mapreduce.execution import AgentNodeProduct
from imbue.mngr_mapreduce.execution import Execution
from imbue.mngr_mapreduce.execution import ExecutionPlan
from imbue.mngr_mapreduce.execution import NodePlacement
from imbue.mngr_mapreduce.execution import NodeStatus
from imbue.mngr_mapreduce.execution import OrchestratorNodeProduct
from imbue.mngr_mapreduce.manifest import MANIFEST_FILENAME
from imbue.mngr_witness.bindings import execution_context
from imbue.mngr_witness.bindings import make_witness_bindings
from imbue.mngr_witness.bindings import make_witness_run
from imbue.mngr_witness.corpus import UnitSelection
from imbue.mngr_witness.gitops import file_text_at
from imbue.mngr_witness.mock_witness_test import ScriptedWitnessExecutor
from imbue.mngr_witness.pipeline import make_witness_pipeline
from imbue.mngr_witness.prompts import no_prompt_variants

_CORPUS_ROOT = Path("libs/proj/behaviors")
_SIGNIN = """Feature: Sign-in

  @fresh-code
  Scenario: Opening a fresh login URL signs the user in
    When they open the login URL
    Then the browser lands on "/"
    And the user is signed in
"""
_INVARIANTS = """Feature: Invariants

  @single-use-codes
  Rule: A one-time code grants at most one session, ever
"""


def _failure_summary(execution: Execution) -> str:
    """Every job that did not succeed, with its error and failed gates, so a failing run explains itself."""
    lines = [execution.stopped_reason or ""]
    for node_name, outcome in execution.node_outcome_by_node_name.items():
        produced = outcome.produced
        if isinstance(produced, OrchestratorNodeProduct):
            if produced.outcome.error_summary:
                lines.append(f"{node_name}: {produced.outcome.error_summary}")
            continue
        for job_result in produced.job_results:
            if job_result.is_successful:
                continue
            failed = [f"{gate.gate_name}: {gate.detail}" for gate in job_result.gate_results if not gate.is_passed]
            lines.append(f"{node_name}/{job_result.job.slug}: {job_result.error_summary} {failed}")
    return "\n".join(lines)


def _git(cg: ConcurrencyGroup, repo: Path, *args: str) -> str:
    return cg.run_process_to_completion(["git", *args], cwd=repo).stdout.strip()


def test_the_witness_pipeline_runs_every_node_and_lands_an_integrated_branch(
    temp_git_repo: Path, cg: ConcurrencyGroup, tmp_path: Path
) -> None:
    repo = temp_git_repo
    write_behavior_corpus(
        repo / _CORPUS_ROOT, {"authentication/signin.feature": _SIGNIN, "invariants.feature": _INVARIANTS}
    )
    (repo / "libs/proj/imbue/proj").mkdir(parents=True)
    (repo / "libs/proj/imbue/proj/__init__.py").write_text("")
    _git(cg, repo, "add", ".")
    _git(cg, repo, "commit", "--quiet", "-m", "corpus and package")
    base_commit = _git(cg, repo, "rev-parse", "HEAD")
    execution_name = uuid4().hex
    output_dir = tmp_path / "output"
    run = make_witness_run(
        repo_root=repo,
        corpus_root=_CORPUS_ROOT,
        test_roots=(Path("libs/proj"),),
        generation_root=Path("libs/proj/imbue/proj/witnesses"),
        selection=UnitSelection(feature_paths=(), area=None, tag=None, unit_kind=None),
        changelog_branch=f"witness/{execution_name}/reduce/integrated",
        variants=no_prompt_variants(),
    )
    plan = ExecutionPlan(
        default_placement=NodePlacement(
            provider=ProviderInstanceName("local"),
            agent_type=AgentTypeName("claude"),
            env_options=AgentEnvironmentOptions(),
            templates=(),
            agents_per_host=PositiveInt(1),
            max_parallel_launch=PositiveInt(2),
            max_running_agents=NonNegativeInt(0),
            agent_timeout_seconds=PositiveFloat(60.0),
        ),
        placement_by_node_name={},
        source_dir=repo,
        output_dir=output_dir,
        poll_interval_seconds=PositiveFloat(0.01),
        launch_delay_seconds=NonNegativeFloat(0.0),
        is_keeping_hosts=False,
    )
    executor = ScriptedWitnessExecutor(
        pipeline=make_witness_pipeline(run.variants),
        bindings=make_witness_bindings(
            run, source_dir=repo, output_dir=output_dir, cg=cg, base_commit=base_commit, observers=()
        ),
        concurrency_group=cg,
        execution=Execution(
            pipeline_name="witness",
            execution_name=execution_name,
            base_commit=base_commit,
            context=execution_context(run),
            plan=plan,
        ),
        repo=repo,
        cg=cg,
        witness_run=run,
        published_root=tmp_path / "published",
    )

    execution = executor.execute()

    assert execution.stopped_reason is None, _failure_summary(execution)
    assert [str(name) for name in execution.node_outcome_by_node_name] == [
        "setup",
        "map",
        "review",
        "integrate",
        "reduce",
    ]
    for outcome in execution.node_outcome_by_node_name.values():
        assert outcome.status is NodeStatus.SUCCEEDED, _failure_summary(execution)
        if isinstance(outcome.produced, AgentNodeProduct):
            for job_result in outcome.produced.job_results:
                assert job_result.is_successful, (
                    outcome.node_name,
                    job_result.job.slug,
                    job_result.error_summary,
                    job_result.gate_results,
                )
    deliverable = f"witness/{execution_name}/reduce/integrated"
    assert (
        file_text_at(cg, repo, deliverable, "libs/proj/imbue/proj/witnesses/authentication/signin_test.py") is not None
    )
    assert file_text_at(cg, repo, deliverable, "libs/proj/imbue/proj/witnesses/invariants_test.py") is not None
    assert (
        file_text_at(cg, repo, deliverable, f"libs/proj/changelog/witness-{execution_name}-reduce-integrated.md")
        is not None
    )
    assert (output_dir / MANIFEST_FILENAME).exists()
    assert [str(agent.node_name) for agent in executor.launched] == [
        "setup",
        "map",
        "map",
        "review",
        "review",
        "reduce",
    ]
    mapper_prompt = executor.prompt_by_agent_name[AgentName(f"witness-{execution_name}-map-authentication-signin")]
    assert "Your task is the behavior file: libs/proj/behaviors/authentication/signin.feature" in mapper_prompt
    assert "Use the proxy_client fixture for HTTP flows." in mapper_prompt
    assert f"You are on branch `witness/{execution_name}/map/authentication-signin`" in mapper_prompt
