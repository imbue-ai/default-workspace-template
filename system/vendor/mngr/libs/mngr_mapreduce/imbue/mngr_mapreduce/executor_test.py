from uuid import uuid4

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.model_update import to_update
from imbue.mngr_mapreduce.bindings import PipelineBindings
from imbue.mngr_mapreduce.bindings import UnboundPipelineError
from imbue.mngr_mapreduce.execution import Execution
from imbue.mngr_mapreduce.execution import NodeStatus
from imbue.mngr_mapreduce.executor import PlanMismatchError
from imbue.mngr_mapreduce.executor import UnsuppliedParameterError
from imbue.mngr_mapreduce.executor import assert_context_supplies_every_parameter
from imbue.mngr_mapreduce.executor import resolve_context
from imbue.mngr_mapreduce.mock_executor_test import MockBranchRecordingGate
from imbue.mngr_mapreduce.mock_executor_test import MockDiscoveredJobsBinding
from imbue.mngr_mapreduce.mock_executor_test import MockGateBinding
from imbue.mngr_mapreduce.mock_executor_test import MockOrchestratorBinding
from imbue.mngr_mapreduce.mock_executor_test import MockPerPartitionBinding
from imbue.mngr_mapreduce.mock_executor_test import MockPerUpstreamJobBinding
from imbue.mngr_mapreduce.mock_executor_test import MockPipelineExecutor
from imbue.mngr_mapreduce.mock_executor_test import MockRaisingJobsBinding
from imbue.mngr_mapreduce.mock_executor_test import MockSingleJobBinding
from imbue.mngr_mapreduce.pipeline import FanoutKind
from imbue.mngr_mapreduce.pipeline import Pipeline
from imbue.mngr_mapreduce.primitives import NodeName
from imbue.mngr_mapreduce.testing import job_results_of
from imbue.mngr_mapreduce.testing import make_execution
from imbue.mngr_mapreduce.testing import make_execution_plan
from imbue.mngr_mapreduce.testing import make_node
from imbue.mngr_mapreduce.testing import make_parameter
from imbue.mngr_mapreduce.testing import make_pipeline


def _run(
    pipeline: Pipeline,
    bindings: PipelineBindings,
    execution: Execution | None = None,
    never_publishing_slugs: frozenset[str] = frozenset(),
    polls_before_publishing: int = 2,
    unfetchable_slugs: frozenset[str] = frozenset(),
    branchless_slugs: frozenset[str] = frozenset(),
    unpullable_slugs: frozenset[str] = frozenset(),
) -> tuple[Execution, MockPipelineExecutor]:
    with ConcurrencyGroup(name=f"executor-test-{uuid4().hex}") as cg:
        executor = MockPipelineExecutor(
            pipeline=pipeline,
            bindings=bindings,
            concurrency_group=cg,
            execution=execution if execution is not None else make_execution(),
            never_publishing_slugs=never_publishing_slugs,
            polls_before_publishing=polls_before_publishing,
            unfetchable_slugs=unfetchable_slugs,
            branchless_slugs=branchless_slugs,
            unpullable_slugs=unpullable_slugs,
        )
        return executor.execute(), executor


def _expect_error(pipeline: Pipeline, bindings: PipelineBindings, error_type: type[Exception], match: str) -> None:
    """Run and assert the given error surfaced, unwrapping the group the concurrency group raises it inside."""
    with pytest.raises(BaseException) as exc_info:
        _run(pipeline, bindings)
    raised = _flatten(exc_info.value)
    assert any(isinstance(error, error_type) and match in str(error) for error in raised), raised


def _flatten(error: BaseException) -> list[BaseException]:
    if isinstance(error, BaseExceptionGroup):
        return [nested for child in error.exceptions for nested in _flatten(child)]
    return [error]


def _diamond() -> Pipeline:
    """source -> (left, right) -> sink."""
    return make_pipeline(
        (
            make_node("source", needs=("seed",), produces=("a",)),
            make_node("left", needs=("a",), produces=("b",), kind=FanoutKind.PER_UPSTREAM_JOB),
            make_node("right", needs=("a",), produces=("c",), kind=FanoutKind.PER_UPSTREAM_JOB),
            make_node("sink", needs=("b", "c"), produces=("d",)),
        )
    )


def _diamond_bindings() -> PipelineBindings:
    return PipelineBindings(
        agent_binding_by_node_name={
            NodeName("source"): MockDiscoveredJobsBinding(slugs=("one", "two")),
            NodeName("left"): MockPerUpstreamJobBinding(),
            NodeName("right"): MockPerUpstreamJobBinding(),
            NodeName("sink"): MockDiscoveredJobsBinding(slugs=("final",)),
        }
    )


def test_executor_runs_independent_nodes_at_the_same_time() -> None:
    """The two middle nodes of a diamond must be in flight together, which is what E2 asks for."""
    execution, executor = _run(_diamond(), _diamond_bindings())

    assert execution.stopped_reason is None
    assert all(outcome.status is NodeStatus.SUCCEEDED for outcome in execution.node_outcome_by_node_name.values())
    assert any(snapshot == {NodeName("left"), NodeName("right")} for snapshot in executor.live_node_name_snapshots)


def test_executor_serializes_the_same_diamond_under_a_parallelism_cap_of_one() -> None:
    """Concurrency is structural; parallelism is the plan's to limit, and limiting it must not change the result."""
    execution, executor = _run(
        _diamond(), _diamond_bindings(), execution=make_execution(make_execution_plan(max_concurrent_nodes=1))
    )

    assert execution.stopped_reason is None
    assert all(outcome.status is NodeStatus.SUCCEEDED for outcome in execution.node_outcome_by_node_name.values())
    assert all(len(snapshot) <= 1 for snapshot in executor.live_node_name_snapshots)


def test_executor_respects_the_execution_wide_agent_ceiling() -> None:
    """Per-node caps multiply once nodes run together, so the execution-wide ceiling is what actually bounds the fleet."""
    pipeline = make_pipeline(
        (
            make_node("left", needs=("seed",), produces=("a",)),
            make_node("right", needs=("seed",), produces=("b",)),
        )
    )
    bindings = PipelineBindings(
        agent_binding_by_node_name={
            NodeName("left"): MockDiscoveredJobsBinding(slugs=("l1", "l2", "l3")),
            NodeName("right"): MockDiscoveredJobsBinding(slugs=("r1", "r2", "r3")),
        }
    )

    _, executor = _run(pipeline, bindings, execution=make_execution(make_execution_plan(max_running_agents=2)))

    assert executor.max_live_agent_count <= 2
    assert len(executor.prompt_by_agent_name) == 6


def test_executor_renders_each_prompt_from_the_node_template_and_the_context() -> None:
    """The prompt an agent receives is the pipeline's, not something a binding composed."""
    pipeline = make_pipeline(
        (
            make_node(
                "map",
                needs=("seed",),
                produces=("a",),
                prompt_template="fix {{ item }} using {{ guide }}",
                job_variables=("item",),
            ),
        ),
        parameters=(make_parameter("guide"),),
    )
    bindings = PipelineBindings(
        agent_binding_by_node_name={NodeName("map"): MockDiscoveredJobsBinding(slugs=("alpha",))}
    )

    _, executor = _run(pipeline, bindings, execution=make_execution(guide="the style guide"))

    assert list(executor.prompt_by_agent_name.values()) == ["fix alpha using the style guide"]


def test_executor_fails_a_node_whose_binding_omits_a_declared_job_variable() -> None:
    """A prompt with a hole in it must never reach an agent."""
    pipeline = make_pipeline(
        (
            make_node(
                "map",
                needs=("seed",),
                produces=("a",),
                prompt_template="fix {{ missing }}",
                job_variables=("missing",),
            ),
        )
    )
    bindings = PipelineBindings(
        agent_binding_by_node_name={NodeName("map"): MockDiscoveredJobsBinding(slugs=("alpha",))}
    )

    execution, executor = _run(pipeline, bindings)

    outcome = execution.node_outcome_by_node_name[NodeName("map")]
    assert outcome.status is NodeStatus.FAILED
    assert executor.prompt_by_agent_name == {}
    assert "did not supply missing" in (job_results_of(outcome)[0].error_summary or "")


def test_executor_succeeds_vacuously_when_a_filter_declines_everything() -> None:
    """A filter that matches nothing propagates emptiness to a green finish."""
    pipeline = make_pipeline(
        (
            make_node("source", needs=("seed",), produces=("a",)),
            make_node("filter", needs=("a",), produces=("b",), kind=FanoutKind.PER_UPSTREAM_JOB),
            make_node("fix", needs=("b",), produces=("c",), kind=FanoutKind.PER_UPSTREAM_JOB),
        )
    )
    bindings = PipelineBindings(
        agent_binding_by_node_name={
            NodeName("source"): MockDiscoveredJobsBinding(slugs=("one", "two")),
            NodeName("filter"): MockPerUpstreamJobBinding(declined_slugs=frozenset({"one", "two"})),
            NodeName("fix"): MockPerUpstreamJobBinding(),
        }
    )

    execution, executor = _run(pipeline, bindings)

    assert execution.stopped_reason is None
    assert execution.node_outcome_by_node_name[NodeName("filter")].status is NodeStatus.SUCCEEDED
    assert execution.node_outcome_by_node_name[NodeName("fix")].status is NodeStatus.SUCCEEDED
    assert job_results_of(execution.node_outcome_by_node_name[NodeName("fix")]) == ()
    assert NodeName("fix") not in executor.provisioned_node_names


def test_executor_fails_an_empty_fanout_that_declared_a_minimum() -> None:
    """min_job_count is the assertion that finding nothing is a mistake."""
    pipeline = make_pipeline((make_node("discover", needs=("seed",), produces=("a",), min_job_count=1),))
    bindings = PipelineBindings(agent_binding_by_node_name={NodeName("discover"): MockDiscoveredJobsBinding(slugs=())})

    execution, _ = _run(pipeline, bindings)

    assert execution.node_outcome_by_node_name[NodeName("discover")].status is NodeStatus.FAILED
    assert "requires at least 1" in execution.node_outcome_by_node_name[NodeName("discover")].detail


def test_executor_partitions_upstream_results_into_one_job_per_key() -> None:
    """The shuffle: many upstream results regrouped by key into fewer downstream jobs."""
    pipeline = make_pipeline(
        (
            make_node("source", needs=("seed",), produces=("a",)),
            make_node("shuffle", needs=("a",), produces=("b",), kind=FanoutKind.PER_PARTITION),
        )
    )
    partition_binding = MockPerPartitionBinding()
    bindings = PipelineBindings(
        agent_binding_by_node_name={
            NodeName("source"): MockDiscoveredJobsBinding(slugs=("a1", "a2", "b1")),
            NodeName("shuffle"): partition_binding,
        }
    )

    execution, _ = _run(pipeline, bindings)

    assert partition_binding.upstream_slugs_by_key == {"a": ("a1", "a2"), "b": ("b1",)}
    shuffle_results = job_results_of(execution.node_outcome_by_node_name[NodeName("shuffle")])
    assert sorted(result.job.slug for result in shuffle_results) == ["partition-a", "partition-b"]


def test_executor_halts_the_whole_run_when_a_node_falls_short() -> None:
    """E3: an intermediate failure invalidates everything downstream, so nothing else runs."""
    pipeline = make_pipeline(
        (
            make_node("source", needs=("seed",), produces=("a",)),
            make_node("sink", needs=("a",), produces=("b",), kind=FanoutKind.SINGLE),
        )
    )
    bindings = PipelineBindings(
        agent_binding_by_node_name={
            NodeName("source"): MockDiscoveredJobsBinding(slugs=("one", "two")),
            NodeName("sink"): MockSingleJobBinding(),
        }
    )

    execution, executor = _run(
        pipeline,
        bindings,
        execution=make_execution(make_execution_plan(agent_timeout_seconds=0.001)),
        never_publishing_slugs=frozenset({"one"}),
    )

    assert execution.node_outcome_by_node_name[NodeName("source")].status is NodeStatus.FAILED
    assert execution.node_outcome_by_node_name[NodeName("sink")].status is NodeStatus.SKIPPED
    assert execution.stopped_reason is not None
    assert NodeName("sink") not in executor.provisioned_node_names


def test_executor_tolerates_a_straggler_under_a_lowered_completion_requirement() -> None:
    """required_completion is what absorbs straggler loss until retries exist."""
    pipeline = make_pipeline((make_node("map", needs=("seed",), produces=("a",), required_completion=0.5),))
    bindings = PipelineBindings(
        agent_binding_by_node_name={NodeName("map"): MockDiscoveredJobsBinding(slugs=("one", "two"))}
    )

    # One job fails by publishing an archive that cannot be pulled, rather than by
    # running out a clock: a wall-clock timeout small enough to fail one agent also
    # races the other one's first poll under load.
    execution, _ = _run(pipeline, bindings, unpullable_slugs=frozenset({"one"}))

    assert execution.node_outcome_by_node_name[NodeName("map")].status is NodeStatus.SUCCEEDED
    assert execution.stopped_reason is None


def test_executor_collects_an_agent_that_published_before_the_executor_looked() -> None:
    """E10: a deadline says how long we will wait, not what time it is."""
    pipeline = make_pipeline((make_node("map", needs=("seed",), produces=("a",)),))
    bindings = PipelineBindings(
        agent_binding_by_node_name={NodeName("map"): MockDiscoveredJobsBinding(slugs=("one",))}
    )

    # The deadline is already past on the very first poll, but the archive is there.
    execution, _ = _run(
        pipeline,
        bindings,
        execution=make_execution(make_execution_plan(agent_timeout_seconds=0.0001)),
        polls_before_publishing=1,
    )

    assert execution.node_outcome_by_node_name[NodeName("map")].status is NodeStatus.SUCCEEDED


def test_executor_runs_an_orchestrator_node_and_halts_when_it_fails() -> None:
    pipeline = make_pipeline(
        (
            make_node("source", needs=("seed",), produces=("a",)),
            make_node("integrate", needs=("a",), produces=("b",), is_orchestrator=True),
        )
    )
    failing = MockOrchestratorBinding(error_summary="merge conflict")
    bindings = PipelineBindings(
        agent_binding_by_node_name={NodeName("source"): MockDiscoveredJobsBinding(slugs=("one",))},
        orchestrator_binding_by_node_name={NodeName("integrate"): failing},
    )

    execution, _ = _run(pipeline, bindings)

    assert failing.run_count == 1
    assert execution.node_outcome_by_node_name[NodeName("integrate")].status is NodeStatus.FAILED
    assert "merge conflict" in (execution.stopped_reason or "")


def test_executor_fails_a_node_whose_gate_raises() -> None:
    """A gate that cannot render a verdict leaves every job undecided; treating that as success would let work through."""
    pipeline = make_pipeline((make_node("map", needs=("seed",), produces=("a",), gates=("tests_run",)),))
    bindings = PipelineBindings(
        agent_binding_by_node_name={NodeName("map"): MockDiscoveredJobsBinding(slugs=("one",))},
        gate_binding_by_gate_name={"tests_run": MockGateBinding(is_raising=True)},
    )

    execution, _ = _run(pipeline, bindings)

    assert execution.node_outcome_by_node_name[NodeName("map")].status is NodeStatus.FAILED
    assert "undecided" in execution.node_outcome_by_node_name[NodeName("map")].detail


def test_executor_counts_a_failed_gate_against_the_completion_requirement() -> None:
    pipeline = make_pipeline(
        (make_node("map", needs=("seed",), produces=("a",), gates=("tests_run",), required_completion=0.5),)
    )
    bindings = PipelineBindings(
        agent_binding_by_node_name={NodeName("map"): MockDiscoveredJobsBinding(slugs=("one", "two"))},
        gate_binding_by_gate_name={"tests_run": MockGateBinding(failing_slugs=frozenset({"one"}))},
    )

    execution, _ = _run(pipeline, bindings)

    assert execution.node_outcome_by_node_name[NodeName("map")].status is NodeStatus.SUCCEEDED
    assert "1 of 2 jobs succeeded" in execution.node_outcome_by_node_name[NodeName("map")].detail


def test_executor_releases_every_host_it_provisioned() -> None:
    execution, executor = _run(_diamond(), _diamond_bindings())

    assert sorted(executor.released_node_names) == sorted(executor.provisioned_node_names)


def test_executor_refuses_a_pipeline_with_an_unbound_node() -> None:
    pipeline = make_pipeline((make_node("map", needs=("seed",), produces=("a",)),))

    _expect_error(pipeline, PipelineBindings(), UnboundPipelineError, "no binding for agent node 'map'")


def test_executor_refuses_a_binding_of_the_wrong_shape() -> None:
    """A SINGLE node bound to a discovered-jobs binding is a mistake worth catching before anything launches."""
    pipeline = make_pipeline((make_node("reduce", needs=("seed",), produces=("a",), kind=FanoutKind.SINGLE),))
    bindings = PipelineBindings(
        agent_binding_by_node_name={NodeName("reduce"): MockDiscoveredJobsBinding(slugs=("one",))}
    )

    _expect_error(
        pipeline, bindings, UnboundPipelineError, "fans out SINGLE but its binding is a MockDiscoveredJobsBinding"
    )


def test_executor_refuses_a_plan_that_places_a_node_the_pipeline_lacks() -> None:
    pipeline = make_pipeline((make_node("map", needs=("seed",), produces=("a",)),))
    bindings = PipelineBindings(
        agent_binding_by_node_name={NodeName("map"): MockDiscoveredJobsBinding(slugs=("one",))}
    )
    plan = make_execution_plan()
    plan = plan.model_copy_update(
        to_update(plan.field_ref().placement_by_node_name, {NodeName("ghost"): plan.default_placement})
    )

    with pytest.raises(BaseException) as exc_info:
        _run(pipeline, bindings, execution=make_execution(plan))
    assert any(isinstance(error, PlanMismatchError) for error in _flatten(exc_info.value))


def test_executor_fails_a_job_whose_branch_bundle_could_not_be_applied() -> None:
    """A bundle that fails to apply loses the agent's work, so the job must not count as a success."""
    pipeline = make_pipeline((make_node("map", needs=("seed",), produces=("a",)),))
    bindings = PipelineBindings(
        agent_binding_by_node_name={NodeName("map"): MockDiscoveredJobsBinding(slugs=("one",))}
    )

    execution, _ = _run(pipeline, bindings, unfetchable_slugs=frozenset({"one"}))

    outcome = execution.node_outcome_by_node_name[NodeName("map")]
    assert outcome.status is NodeStatus.FAILED
    job_result = job_results_of(outcome)[0]
    assert job_result.is_successful is False
    assert "could not be applied" in (job_result.error_summary or "")


def test_executor_succeeds_a_job_whose_agent_committed_nothing() -> None:
    """Publishing an archive without committing is a legitimate no-op, not a lost branch."""
    pipeline = make_pipeline((make_node("map", needs=("seed",), produces=("a",)),))
    bindings = PipelineBindings(
        agent_binding_by_node_name={NodeName("map"): MockDiscoveredJobsBinding(slugs=("one",))}
    )

    execution, _ = _run(pipeline, bindings, branchless_slugs=frozenset({"one"}))

    outcome = execution.node_outcome_by_node_name[NodeName("map")]
    assert outcome.status is NodeStatus.SUCCEEDED
    job_result = job_results_of(outcome)[0]
    assert job_result.is_successful is True
    assert job_result.is_branch_applied is False
    assert job_result.error_summary is None


def test_a_gate_is_told_when_a_job_produced_no_branch() -> None:
    """A gate that inspects the branch must be able to see there is not one."""
    pipeline = make_pipeline((make_node("map", needs=("seed",), produces=("a",), gates=("looks",)),))
    gate = MockBranchRecordingGate()
    bindings = PipelineBindings(
        agent_binding_by_node_name={NodeName("map"): MockDiscoveredJobsBinding(slugs=("one",))},
        gate_binding_by_gate_name={"looks": gate},
    )

    _run(pipeline, bindings, branchless_slugs=frozenset({"one"}))

    assert gate.seen_is_branch_applied == [False]


def test_halting_keeps_the_results_an_interrupted_node_already_produced() -> None:
    """Their archives are on disk, so dropping them would make the manifest disagree with the run."""
    pipeline = make_pipeline(
        (
            make_node("doomed", needs=("seed",), produces=("a",), gates=("strict",)),
            make_node("bystander", needs=("seed",), produces=("b",)),
        )
    )
    bindings = PipelineBindings(
        agent_binding_by_node_name={
            NodeName("doomed"): MockDiscoveredJobsBinding(slugs=("bad",)),
            NodeName("bystander"): MockDiscoveredJobsBinding(slugs=("collected", "still-running")),
        },
        gate_binding_by_gate_name={"strict": MockGateBinding(failing_slugs=frozenset({"bad"}))},
    )

    execution, _ = _run(pipeline, bindings, never_publishing_slugs=frozenset({"still-running"}))

    assert execution.node_outcome_by_node_name[NodeName("doomed")].status is NodeStatus.FAILED
    bystander = execution.node_outcome_by_node_name[NodeName("bystander")]
    assert bystander.status is NodeStatus.SKIPPED
    assert [result.job.slug for result in job_results_of(bystander)] == ["collected"]


def test_a_binding_that_raises_fails_its_node_and_stops_everything_in_flight() -> None:
    """Plugin code raising must not kill the run with agents still alive."""
    pipeline = make_pipeline(
        (
            make_node("broken", needs=("seed",), produces=("a",)),
            make_node("bystander", needs=("seed",), produces=("b",)),
        )
    )
    bindings = PipelineBindings(
        agent_binding_by_node_name={
            NodeName("broken"): MockRaisingJobsBinding(),
            NodeName("bystander"): MockDiscoveredJobsBinding(slugs=("one",)),
        }
    )

    execution, executor = _run(pipeline, bindings, never_publishing_slugs=frozenset({"one"}))

    broken = execution.node_outcome_by_node_name[NodeName("broken")]
    assert broken.status is NodeStatus.FAILED
    assert "cannot decide its jobs" in broken.detail
    assert execution.stopped_reason is not None
    assert executor.live_node_names_by_agent_name == {}


def test_a_parameter_without_a_default_must_be_supplied() -> None:
    """Caught beside the other pre-flight checks, before a snapshot or a host pool exists."""
    pipeline = make_pipeline(
        (make_node("map", needs=("seed",), produces=("a",), prompt_template="use {{ root }}"),),
        parameters=(make_parameter("root"),),
    )

    with pytest.raises(UnsuppliedParameterError, match="does not supply root"):
        assert_context_supplies_every_parameter(pipeline, {})


def test_a_parameter_with_a_default_need_not_be_supplied() -> None:
    """A default is what makes a parameter optional, so the pre-flight check must honour it."""
    pipeline = make_pipeline(
        (make_node("map", needs=("seed",), produces=("a",), prompt_template="use {{ root }}"),),
        parameters=(make_parameter("root", default="/fallback"),),
    )

    assert_context_supplies_every_parameter(pipeline, resolve_context(pipeline, {}))
    assert resolve_context(pipeline, {}) == {"root": "/fallback"}
    assert resolve_context(pipeline, {"root": "/given"}) == {"root": "/given"}


def test_a_parameter_default_is_used_when_the_execution_omits_it() -> None:
    """Parameter.default exists to be applied, not merely declared."""
    pipeline = make_pipeline(
        (make_node("map", needs=("seed",), produces=("a",), prompt_template="use {{ root }}"),),
        parameters=(make_parameter("root", default="/fallback"),),
    )
    bindings = PipelineBindings(
        agent_binding_by_node_name={NodeName("map"): MockDiscoveredJobsBinding(slugs=("one",))}
    )

    execution, executor = _run(pipeline, bindings)

    assert execution.context["root"] == "/fallback"
    assert list(executor.prompt_by_agent_name.values()) == ["use /fallback"]


def test_an_agent_receives_a_prompt_rendered_through_the_pipelines_own_templates() -> None:
    """An inherited template must reach the agent rendered, not as a pointer."""
    pipeline = make_pipeline(
        (
            make_node(
                "map",
                needs=("seed",),
                produces=("a",),
                prompt_template='{% extends "base.j2" %}{% block body %}{{ item }}{% endblock %}',
                job_variables=("item",),
            ),
        ),
        parameters=(make_parameter("shared"),),
        template_by_name={"base.j2": "for {{ shared }}: {% block body %}{% endblock %}"},
    )
    bindings = PipelineBindings(
        agent_binding_by_node_name={NodeName("map"): MockDiscoveredJobsBinding(slugs=("one",))}
    )
    execution = make_execution().model_copy_update(
        to_update(make_execution().field_ref().context, {"shared": "the corpus"})
    )

    _, executor = _run(pipeline, bindings, execution=execution)

    assert list(executor.prompt_by_agent_name.values()) == ["for the corpus: one"]
