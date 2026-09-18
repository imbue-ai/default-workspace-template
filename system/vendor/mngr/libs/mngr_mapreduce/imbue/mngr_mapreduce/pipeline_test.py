import pytest
from pydantic import ValidationError

from imbue.imbue_common.model_update import to_update
from imbue.mngr.primitives import InvalidName
from imbue.mngr_mapreduce.execution import AgentNodeProduct
from imbue.mngr_mapreduce.execution import NodeOutcome
from imbue.mngr_mapreduce.execution import NodeStatus
from imbue.mngr_mapreduce.execution import OrchestratorNodeProduct
from imbue.mngr_mapreduce.execution import OrchestratorOutcome
from imbue.mngr_mapreduce.pipeline import AgentWork
from imbue.mngr_mapreduce.pipeline import DiscoveredFanout
from imbue.mngr_mapreduce.pipeline import FanoutKind
from imbue.mngr_mapreduce.pipeline import OrchestratorWork
from imbue.mngr_mapreduce.pipeline import make_topological_sorter
from imbue.mngr_mapreduce.pipeline import predecessor_names
from imbue.mngr_mapreduce.pipeline import producing_node_name_by_artifact_name
from imbue.mngr_mapreduce.primitives import NodeName
from imbue.mngr_mapreduce.testing import make_artifact
from imbue.mngr_mapreduce.testing import make_node
from imbue.mngr_mapreduce.testing import make_parameter
from imbue.mngr_mapreduce.testing import make_pipeline


def test_pipeline_derives_execution_order_from_needs_not_declaration_order() -> None:
    """Nodes declared out of order must still sort into dependency order."""
    pipeline = make_pipeline(
        (
            make_node("reduce", needs=("mapped",), produces=("reduced",), kind=FanoutKind.SINGLE),
            make_node("map", needs=("seed",), produces=("mapped",)),
        )
    )

    assert tuple(make_topological_sorter(pipeline).static_order()) == (NodeName("map"), NodeName("reduce"))


def test_pipeline_treats_an_input_as_available_without_making_it_a_predecessor() -> None:
    """A need that is a pipeline input contributes no edge, because the operator supplied it."""
    node = make_node("map", needs=("seed",), produces=("mapped",))
    pipeline = make_pipeline((node,))

    assert predecessor_names(pipeline, node) == frozenset()
    assert producing_node_name_by_artifact_name(pipeline) == {"mapped": NodeName("map")}


def test_pipeline_allows_two_nodes_that_do_not_depend_on_each_other() -> None:
    """Independent nodes are both ready at once, which is the topology E2 requires."""
    pipeline = make_pipeline(
        (make_node("left", needs=("seed",), produces=("a",)), make_node("right", needs=("seed",), produces=("b",)))
    )

    sorter = make_topological_sorter(pipeline)
    sorter.prepare()
    assert set(sorter.get_ready()) == {NodeName("left"), NodeName("right")}


def test_pipeline_rejects_a_cycle() -> None:
    pipeline_nodes = (
        make_node("first", needs=("second_out",), produces=("first_out",)),
        make_node("second", needs=("first_out",), produces=("second_out",)),
    )

    with pytest.raises(ValidationError, match="has a cycle"):
        make_pipeline(pipeline_nodes)


def test_pipeline_rejects_a_need_that_nothing_defines() -> None:
    with pytest.raises(ValidationError, match="needs 'ghost', which is neither a pipeline input nor produced"):
        make_pipeline((make_node("map", needs=("ghost",), produces=("mapped",)),))


def test_pipeline_rejects_two_nodes_with_the_same_name() -> None:
    with pytest.raises(ValidationError, match="Node 'map' is listed more than once"):
        make_pipeline((make_node("map", needs=("seed",), produces=("a",)), make_node("map", produces=("b",))))


def test_pipeline_rejects_an_artifact_produced_by_two_nodes() -> None:
    with pytest.raises(ValidationError, match="Artifact 'shared' is listed more than once"):
        make_pipeline((make_node("left", produces=("shared",)), make_node("right", produces=("shared",))))


def test_pipeline_rejects_two_parameters_with_the_same_name() -> None:
    with pytest.raises(ValidationError, match="Parameter 'root' is listed more than once"):
        make_pipeline(
            (make_node("map", produces=("a",)),), parameters=(make_parameter("root"), make_parameter("root"))
        )


def test_pipeline_rejects_an_output_that_contradicts_its_artifact() -> None:
    node = make_node("map", produces=("result",))
    contradicting = make_artifact("result")
    contradicting = contradicting.model_copy_update(to_update(contradicting.field_ref().description, "different"))

    with pytest.raises(ValidationError, match="Output 'result' does not match the artifact of that name"):
        make_pipeline((node,), outputs=(contradicting,))


def test_a_node_may_fan_out_over_one_producer_and_still_need_another() -> None:
    """Naming the fan-out source is what lets a reviewer read a setup node's guide as well."""
    pipeline = make_pipeline(
        (
            make_node("setup", needs=("seed",), produces=("guide",)),
            make_node("map", needs=("seed",), produces=("branches",)),
            make_node(
                "review",
                needs=("branches", "guide"),
                produces=("reviews",),
                kind=FanoutKind.PER_UPSTREAM_JOB,
                over="branches",
            ),
        )
    )

    review = pipeline.nodes[2]
    assert predecessor_names(pipeline, review) == {NodeName("map"), NodeName("setup")}


def test_node_rejects_a_fanout_over_something_it_does_not_need() -> None:
    """The artifact fanned over is a dependency, so it must appear among the node's needs."""
    with pytest.raises(ValidationError, match="fans out over 'absent', which is not among its needs"):
        make_node("review", needs=("branches",), kind=FanoutKind.PER_UPSTREAM_JOB, over="absent")


def test_pipeline_rejects_a_fanout_over_an_operator_supplied_input() -> None:
    """An input has no upstream results to fan out over; only a node's product does."""
    with pytest.raises(ValidationError, match="fans out over 'seed', which no node"):
        make_pipeline((make_node("map", needs=("seed",), produces=("a",), kind=FanoutKind.PER_PARTITION),))


def test_pipeline_allows_discovered_fanout_to_join_two_producers() -> None:
    """DISCOVERED is how a node fans out over what several producers made, rather than over one."""
    pipeline = make_pipeline(
        (
            make_node("left", produces=("a",)),
            make_node("right", produces=("b",)),
            make_node("join", needs=("a", "b"), produces=("joined",), kind=FanoutKind.DISCOVERED),
        )
    )

    assert predecessor_names(pipeline, pipeline.nodes[2]) == {NodeName("left"), NodeName("right")}


def test_node_rejects_a_fanout_source_when_it_needs_nothing() -> None:
    with pytest.raises(ValidationError, match="which is not among its needs"):
        make_node("shuffle", produces=("grouped",), kind=FanoutKind.PER_PARTITION)


def test_an_empty_agent_fanout_is_distinguishable_from_an_orchestrator_node() -> None:
    """The two cases the vacuous-success rule depends on telling apart carry different product types."""
    agent_outcome = NodeOutcome(node_name=NodeName("filter"), status=NodeStatus.SUCCEEDED, produced=AgentNodeProduct())
    orchestrator_outcome = NodeOutcome(
        node_name=NodeName("integrate"),
        status=NodeStatus.SUCCEEDED,
        produced=OrchestratorNodeProduct(
            outcome=OrchestratorOutcome(branch_name=None, summary="merged", error_summary=None)
        ),
    )

    assert isinstance(agent_outcome.produced, AgentNodeProduct)
    assert agent_outcome.produced.job_results == ()
    assert isinstance(orchestrator_outcome.produced, OrchestratorNodeProduct)


def test_orchestrator_work_cannot_carry_a_fanout() -> None:
    """An orchestrator node has no fan-out to declare, so there is no wrong value to give it."""
    with pytest.raises(ValidationError, match="fanout"):
        OrchestratorWork.model_validate({"fanout": DiscoveredFanout(description="one agent")})


def test_orchestrator_work_cannot_carry_a_prompt_template() -> None:
    """An orchestrator node launches no agent, so it has no prompt field to fill."""
    with pytest.raises(ValidationError, match="prompt_template"):
        OrchestratorWork.model_validate({"prompt_template": "do a thing"})


def test_agent_work_rejects_an_empty_prompt_template() -> None:
    """The template is a NonEmptyStr, so emptiness is refused by the type rather than a validator.

    Built through ``model_validate`` because a literal ``""`` does not type-check
    as a ``NonEmptyStr``, which is the same guarantee seen from the other side.
    """
    with pytest.raises(ValidationError, match="prompt_template"):
        AgentWork.model_validate(
            {
                "fanout": DiscoveredFanout(description="one agent"),
                "prompt_template": "",
                "job_variables": (),
                "gates": (),
            }
        )


def test_node_rejects_a_min_job_count_its_fanout_cannot_reach() -> None:
    with pytest.raises(ValidationError, match="requires at least 2 jobs but fans out SINGLE"):
        make_node("reduce", needs=("seed",), kind=FanoutKind.SINGLE, min_job_count=2)


def test_node_rejects_a_duplicate_gate() -> None:
    with pytest.raises(ValidationError, match="Gate 'tests_run' is listed more than once in node 'map'"):
        make_node("map", gates=("tests_run", "tests_run"))


def test_pipeline_rejects_a_template_variable_nothing_supplies() -> None:
    with pytest.raises(ValidationError, match="reads missing_name, which is neither a parameter"):
        make_pipeline((make_node("map", prompt_template="use {{ missing_name }}"),))


def test_pipeline_accepts_a_template_variable_from_a_parameter_or_a_job_variable() -> None:
    pipeline = make_pipeline(
        (make_node("map", prompt_template="{{ root }} and {{ item }}", job_variables=("item",)),),
        parameters=(make_parameter("root"),),
    )

    work = pipeline.nodes[0].work
    assert isinstance(work, AgentWork)
    assert work.prompt_template == "{{ root }} and {{ item }}"


def test_pipeline_rejects_a_malformed_template() -> None:
    with pytest.raises(ValidationError, match="not valid Jinja"):
        make_pipeline((make_node("map", prompt_template="{{ unclosed "),))


def test_node_name_rejects_a_name_that_cannot_be_an_agent_name() -> None:
    """The name becomes a segment of every agent and branch name, so M8 says catch it at construction."""
    with pytest.raises(InvalidName):
        NodeName("Map Review")


def test_node_name_accepts_the_shape_agent_and_branch_names_allow() -> None:
    assert NodeName("map-review") == "map-review"
    assert NodeName("map_review") == "map_review"


def test_a_node_template_may_extend_one_the_pipeline_carries() -> None:
    """The template family lives in the pipeline, so composition needs no loader reaching outside it."""
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
        template_by_name={"base.j2": "header {{ shared }}: {% block body %}{% endblock %}"},
    )

    assert pipeline.template_by_name["base.j2"].startswith("header")


def test_pipeline_rejects_a_template_that_extends_a_name_it_does_not_carry() -> None:
    with pytest.raises(ValidationError, match="extends or includes 'absent.j2', which the pipeline lacks"):
        make_pipeline(
            (make_node("map", needs=("seed",), produces=("a",), prompt_template='{% extends "absent.j2" %}'),)
        )


def test_a_variable_read_only_by_an_inherited_template_is_still_checked() -> None:
    """The construction-time check must see through extends, or it checks half the family."""
    with pytest.raises(ValidationError, match="reads shared"):
        make_pipeline(
            (
                make_node(
                    "map",
                    needs=("seed",),
                    produces=("a",),
                    prompt_template='{% extends "base.j2" %}',
                ),
            ),
            template_by_name={"base.j2": "{{ shared }}"},
        )
