from pathlib import Path

from imbue.mngr_mapreduce.pipeline import AgentWork
from imbue.mngr_mapreduce.pipeline import FanoutKind
from imbue.mngr_mapreduce.pipeline import OrchestratorWork
from imbue.mngr_mapreduce.pipeline import Pipeline
from imbue.mngr_mapreduce.pipeline import UpstreamFanout
from imbue.mngr_mapreduce.pipeline import predecessor_names
from imbue.mngr_mapreduce.pipeline_svg import render_pipeline_svg
from imbue.mngr_mapreduce.primitives import NodeName
from imbue.mngr_witness.pipeline import PARTIAL_HARVEST_REQUIRED_COMPLETION
from imbue.mngr_witness.pipeline import WITNESS_PIPELINE
from imbue.mngr_witness.pipeline import make_witness_pipeline
from imbue.mngr_witness.prompts import MAPPER_TEMPLATE_NAME
from imbue.mngr_witness.prompts import PACKAGED_TEMPLATE_BY_NAME
from imbue.mngr_witness.prompts import PromptVariants
from imbue.mngr_witness.prompts import RUN_PARAMETER_NAMES

# The gate table of specs/behaviors-mapreduce/spec.md, "Mechanical gates".
_SPEC_GATE_TABLE = frozenset(
    {"corpus_untouched", "lint", "paths", "no_changelog", "changelog", "units_reported", "links", "junit", "trace"}
)

_SPEC_DIAGRAM_PATH = Path("specs") / "behaviors-mapreduce" / "pipeline.svg"
_REGENERATE_COMMAND = (
    "uv run python libs/mngr_mapreduce/scripts/render_pipeline_svg.py "
    "imbue.mngr_witness.pipeline:WITNESS_PIPELINE specs/behaviors-mapreduce/pipeline.svg"
)


def _agent_work(node_name: str) -> AgentWork:
    node = next(node for node in WITNESS_PIPELINE.nodes if node.name == node_name)
    return AgentWork.model_validate(node.work.model_dump())


def test_witness_pipeline_has_the_spec_nodes_with_their_fanouts() -> None:
    fanouts = [
        (str(node.name), node.work.fanout.kind if isinstance(node.work, AgentWork) else "orchestrator")
        for node in WITNESS_PIPELINE.nodes
    ]
    assert fanouts == [
        ("setup", FanoutKind.SINGLE),
        ("map", FanoutKind.DISCOVERED),
        ("review", FanoutKind.PER_UPSTREAM_JOB),
        ("integrate", "orchestrator"),
        ("reduce", FanoutKind.SINGLE),
    ]


def test_review_fans_out_over_the_mapper_branches_and_still_needs_the_mapper_evidence() -> None:
    review = next(node for node in WITNESS_PIPELINE.nodes if node.name == "review")
    fanout = _agent_work("review").fanout
    assert isinstance(fanout, UpstreamFanout)
    assert fanout.over == "mapper_branch"
    assert review.needs == ("mapper_branch", "mapper_outcome", "mapper_junit", "mapper_witness_links")


def test_witness_pipeline_gates_per_node_match_the_spec() -> None:
    gate_names_by_node = {
        str(node.name): tuple(gate.name for gate in node.work.gates)
        for node in WITNESS_PIPELINE.nodes
        if isinstance(node.work, AgentWork)
    }
    assert gate_names_by_node == {
        "setup": ("corpus_untouched", "lint", "paths", "links"),
        "map": ("corpus_untouched", "lint", "paths", "no_changelog", "units_reported", "links", "junit"),
        "review": ("corpus_untouched", "lint", "paths", "no_changelog", "units_reported", "links", "junit", "trace"),
        "reduce": ("corpus_untouched", "lint", "paths", "links", "junit", "changelog"),
    }


def test_witness_pipeline_uses_every_gate_of_the_spec_gate_table_and_no_other() -> None:
    used_gate_names = {
        gate.name for node in WITNESS_PIPELINE.nodes if isinstance(node.work, AgentWork) for gate in node.work.gates
    }
    assert used_gate_names == _SPEC_GATE_TABLE


def test_witness_pipeline_nodes_wait_for_each_other_in_the_spec_order() -> None:
    predecessors = {
        str(node.name): sorted(str(name) for name in predecessor_names(WITNESS_PIPELINE, node))
        for node in WITNESS_PIPELINE.nodes
    }
    assert predecessors == {
        "setup": [],
        "map": ["setup"],
        "review": ["map"],
        "integrate": ["review", "setup"],
        "reduce": ["integrate", "setup"],
    }
    integrate = next(node for node in WITNESS_PIPELINE.nodes if node.name == "integrate")
    assert isinstance(integrate.work, OrchestratorWork)


def test_witness_pipeline_node_products_match_the_spec() -> None:
    products = {str(node.name): tuple(artifact.name for artifact in node.produces) for node in WITNESS_PIPELINE.nodes}
    assert products == {
        "setup": ("scaffolding_branch", "setup_outcome", "scaffolding_guide", "base_witness_links", "setup_junit"),
        "map": ("mapper_branch", "mapper_outcome", "mapper_junit", "mapper_witness_links"),
        "review": ("reviewed_branch", "review_outcome", "review_junit", "review_witness_links"),
        "integrate": ("integration_branch", "cherry_pick_conflicts"),
        "reduce": ("integrated_branch", "reduce_outcome", "reduce_junit", "reduce_witness_links", "changelog_entries"),
    }


def test_witness_pipeline_takes_the_base_commit_and_corpus_and_delivers_the_integrated_branch_and_report() -> None:
    assert [artifact.name for artifact in WITNESS_PIPELINE.inputs] == ["base_commit", "corpus"]
    assert [artifact.name for artifact in WITNESS_PIPELINE.outputs] == ["integrated_branch", "report"]


def test_a_failed_mapper_or_reviewer_is_an_escalation_but_a_failed_setup_or_annealer_ends_the_run() -> None:
    assert _agent_work("map").required_completion == PARTIAL_HARVEST_REQUIRED_COMPLETION
    assert _agent_work("map").min_job_count == 1
    assert _agent_work("review").required_completion == PARTIAL_HARVEST_REQUIRED_COMPLETION
    assert _agent_work("setup").required_completion == 1.0
    assert _agent_work("reduce").required_completion == 1.0


def test_the_pipeline_declares_exactly_the_parameters_the_run_supplies() -> None:
    assert {str(parameter.name) for parameter in WITNESS_PIPELINE.parameters} == set(RUN_PARAMETER_NAMES)


def test_the_pipeline_carries_the_packaged_template_family() -> None:
    assert WITNESS_PIPELINE.template_by_name == PACKAGED_TEMPLATE_BY_NAME
    assert {
        "scaffold.j2",
        "mapper.j2",
        "reviewer.j2",
        "annealer.j2",
        "_ground_rules.j2",
        "_evidence.j2",
        "_publish.j2",
    } <= set(WITNESS_PIPELINE.template_by_name)
    assert _agent_work("map").prompt_template == PACKAGED_TEMPLATE_BY_NAME[MAPPER_TEMPLATE_NAME].strip()


def test_a_project_variant_becomes_the_node_template_and_still_extends_the_packaged_one(tmp_path: Path) -> None:
    variant = tmp_path / MAPPER_TEMPLATE_NAME
    variant.write_text('{% extends "mapper.j2" %}\n{% block project_guidance %}PROJECT FACTS{% endblock %}\n')

    pipeline = make_witness_pipeline(PromptVariants(scaffold=None, mapper=variant, reviewer=None, annealer=None))

    map_node = next(node for node in pipeline.nodes if node.name == NodeName("map"))
    assert isinstance(map_node.work, AgentWork)
    assert map_node.work.prompt_template == variant.read_text().strip()
    assert pipeline.template_by_name[MAPPER_TEMPLATE_NAME] == PACKAGED_TEMPLATE_BY_NAME[MAPPER_TEMPLATE_NAME]


def test_witness_pipeline_round_trips_through_its_dump() -> None:
    assert Pipeline.model_validate(WITNESS_PIPELINE.model_dump()) == WITNESS_PIPELINE


def test_committed_spec_diagram_matches_the_model(repo_root: Path) -> None:
    committed = (repo_root / _SPEC_DIAGRAM_PATH).read_text(encoding="utf-8")

    assert committed == render_pipeline_svg(WITNESS_PIPELINE), (
        f"{_SPEC_DIAGRAM_PATH} has drifted from WITNESS_PIPELINE; regenerate it with `{_REGENERATE_COMMAND}`"
    )
