import json
from pathlib import Path
from uuid import uuid4

import pytest

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
from imbue.mngr_mapreduce.bindings import find_binding_problems
from imbue.mngr_mapreduce.execution import AgentNodeProduct
from imbue.mngr_mapreduce.execution import Execution
from imbue.mngr_mapreduce.execution import ExecutionPlan
from imbue.mngr_mapreduce.execution import Job
from imbue.mngr_mapreduce.execution import JobResult
from imbue.mngr_mapreduce.execution import NodeOutcome
from imbue.mngr_mapreduce.execution import NodePlacement
from imbue.mngr_mapreduce.execution import NodeStatus
from imbue.mngr_mapreduce.execution import OrchestratorNodeProduct
from imbue.mngr_mapreduce.execution import OrchestratorOutcome
from imbue.mngr_mapreduce.primitives import NodeName
from imbue.mngr_witness.bindings import ANNEAL_JOB_SLUG
from imbue.mngr_witness.bindings import AnnealBinding
from imbue.mngr_witness.bindings import INTEGRATION_JOB_SLUG
from imbue.mngr_witness.bindings import IntegrateBinding
from imbue.mngr_witness.bindings import IntegrationMissingError
from imbue.mngr_witness.bindings import MapBinding
from imbue.mngr_witness.bindings import ReviewBinding
from imbue.mngr_witness.bindings import SCAFFOLD_JOB_SLUG
from imbue.mngr_witness.bindings import ScaffoldBinding
from imbue.mngr_witness.bindings import WitnessRun
from imbue.mngr_witness.bindings import branch_name_for
from imbue.mngr_witness.bindings import execution_context
from imbue.mngr_witness.bindings import generation_module_for
from imbue.mngr_witness.bindings import make_witness_bindings
from imbue.mngr_witness.bindings import make_witness_run
from imbue.mngr_witness.bindings import scaffold_base_ref
from imbue.mngr_witness.corpus import UnitSelection
from imbue.mngr_witness.gitops import create_branch
from imbue.mngr_witness.gitops import file_text_at
from imbue.mngr_witness.outcomes import ARCHIVE_TEST_OUTPUT_DIRNAME
from imbue.mngr_witness.outcomes import OUTCOME_FILENAME
from imbue.mngr_witness.outcomes import SCAFFOLDING_GUIDE_FILENAME
from imbue.mngr_witness.outcomes import WITNESS_LINKS_FILENAME
from imbue.mngr_witness.pipeline import INTEGRATE_NODE_NAME
from imbue.mngr_witness.pipeline import MAP_NODE_NAME
from imbue.mngr_witness.pipeline import REDUCE_NODE_NAME
from imbue.mngr_witness.pipeline import REVIEW_NODE_NAME
from imbue.mngr_witness.pipeline import SETUP_NODE_NAME
from imbue.mngr_witness.pipeline import make_witness_pipeline
from imbue.mngr_witness.prompts import RUN_PARAMETER_NAMES
from imbue.mngr_witness.prompts import no_prompt_variants

_CORPUS_ROOT = Path("libs/proj/behaviors")
_SIGNIN = """Feature: Sign-in

  @fresh-code
  Scenario: Opening a fresh login URL signs the user in
    When they open the login URL
    Then the browser lands on "/"
"""
_ROUTING = """Feature: Routing

  @agent-origin
  Scenario: Each agent is served at its own origin
    When a request names an agent
    Then it is routed to that agent
"""
_PIPELINE_NAME = "witness"
_SIGNIN_SLUG = "authentication-signin"
_ROUTING_SLUG = "forwarding-routing"


def _git(cg: ConcurrencyGroup, repo: Path, *args: str) -> str:
    return cg.run_process_to_completion(["git", *args], cwd=repo).stdout.strip()


def _commit_file(cg: ConcurrencyGroup, repo: Path, relative_path: str, text: str, subject: str) -> str:
    path = repo / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    _git(cg, repo, "add", relative_path)
    _git(cg, repo, "commit", "--quiet", "-m", subject)
    return _git(cg, repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(temp_git_repo: Path, cg: ConcurrencyGroup) -> Path:
    write_behavior_corpus(
        temp_git_repo / _CORPUS_ROOT,
        {"authentication/signin.feature": _SIGNIN, "forwarding/routing.feature": _ROUTING},
    )
    (temp_git_repo / "libs/proj/imbue/proj").mkdir(parents=True)
    (temp_git_repo / "libs/proj/imbue/proj/__init__.py").write_text("")
    _git(cg, temp_git_repo, "add", ".")
    _git(cg, temp_git_repo, "commit", "--quiet", "-m", "corpus")
    return temp_git_repo


@pytest.fixture
def run(repo: Path) -> WitnessRun:
    return make_witness_run(
        repo_root=repo,
        corpus_root=_CORPUS_ROOT,
        test_roots=(Path("libs/proj"),),
        generation_root=Path("libs/proj/imbue/proj/witnesses"),
        selection=UnitSelection(feature_paths=(), area=None, tag=None, unit_kind=None),
        changelog_branch="witness/x/reduce/integrated",
        variants=no_prompt_variants(),
    )


def _execution(repo: Path, cg: ConcurrencyGroup, run: WitnessRun, outcomes: tuple[NodeOutcome, ...] = ()) -> Execution:
    placement = NodePlacement(
        provider=ProviderInstanceName("local"),
        agent_type=AgentTypeName("claude"),
        env_options=AgentEnvironmentOptions(),
        templates=(),
        agents_per_host=PositiveInt(1),
        max_parallel_launch=PositiveInt(1),
        max_running_agents=NonNegativeInt(0),
        agent_timeout_seconds=PositiveFloat(60.0),
    )
    plan = ExecutionPlan(
        default_placement=placement,
        placement_by_node_name={},
        source_dir=repo,
        output_dir=repo.parent / "output",
        poll_interval_seconds=PositiveFloat(0.01),
        launch_delay_seconds=NonNegativeFloat(0.0),
        is_keeping_hosts=False,
    )
    execution = Execution(
        pipeline_name=_PIPELINE_NAME,
        execution_name=uuid4().hex,
        base_commit=_git(cg, repo, "rev-parse", "HEAD"),
        context=execution_context(run),
        plan=plan,
    )
    for outcome in outcomes:
        execution = execution.with_node_outcome(outcome)
    return execution


def _successful_result(
    slug: str, base_ref: str, branch_name: str, archive_dir: Path, is_branch_applied: bool = True
) -> JobResult:
    return JobResult(
        job=Job(slug=slug, base_ref=base_ref, template_variables={}, inputs_dir=None),
        agent_name=AgentName(f"agent-{slug}"),
        branch_name=branch_name,
        archive_dir=archive_dir,
        is_branch_applied=is_branch_applied,
        error_summary=None,
        gate_results=(),
    )


def _agent_outcome(node_name: NodeName, results: tuple[JobResult, ...]) -> NodeOutcome:
    return NodeOutcome(
        node_name=node_name, status=NodeStatus.SUCCEEDED, produced=AgentNodeProduct(job_results=results), detail=""
    )


def _integrate_outcome(outcome: OrchestratorOutcome) -> NodeOutcome:
    status = NodeStatus.FAILED if outcome.error_summary is not None else NodeStatus.SUCCEEDED
    return NodeOutcome(
        node_name=INTEGRATE_NODE_NAME, status=status, produced=OrchestratorNodeProduct(outcome=outcome), detail=""
    )


def _setup_archive(tmp_path: Path, guide: str, links: list[dict[str, str | None]]) -> Path:
    test_output = tmp_path / "setup-archive" / ARCHIVE_TEST_OUTPUT_DIRNAME
    test_output.mkdir(parents=True)
    (test_output / SCAFFOLDING_GUIDE_FILENAME).write_text(guide)
    (test_output / WITNESS_LINKS_FILENAME).write_text("".join(json.dumps(link) + "\n" for link in links))
    return tmp_path / "setup-archive"


def _mapper_archive(tmp_path: Path, slug: str, outcome_json: str) -> Path:
    test_output = tmp_path / f"map-{slug}" / ARCHIVE_TEST_OUTPUT_DIRNAME
    test_output.mkdir(parents=True)
    (test_output / OUTCOME_FILENAME).write_text(outcome_json)
    return tmp_path / f"map-{slug}"


def _node(run: WitnessRun, name: NodeName):
    return next(node for node in make_witness_pipeline(run.variants).nodes if node.name == name)


def test_every_node_and_gate_of_the_witness_pipeline_is_bound(
    run: WitnessRun, repo: Path, cg: ConcurrencyGroup
) -> None:
    bindings = make_witness_bindings(
        run, source_dir=repo, output_dir=repo.parent / "output", cg=cg, base_commit="abc", observers=()
    )

    assert find_binding_problems(make_witness_pipeline(run.variants), bindings) == []
    assert set(execution_context(run)) == set(RUN_PARAMETER_NAMES)


def test_branch_names_follow_the_executors_naming_including_slug_sanitization(
    run: WitnessRun, repo: Path, cg: ConcurrencyGroup
) -> None:
    execution = _execution(repo, cg, run)

    assert branch_name_for(execution, MAP_NODE_NAME, "Authentication.Sign In") == (
        f"{_PIPELINE_NAME}/{execution.execution_name}/map/authentication-sign-in"
    )


def test_scaffold_builds_one_job_from_the_base_commit_naming_every_unit(
    run: WitnessRun, repo: Path, cg: ConcurrencyGroup
) -> None:
    execution = _execution(repo, cg, run)

    job = ScaffoldBinding(witness_run=run).build_job(execution, _node(run, SETUP_NODE_NAME))

    assert (job.slug, job.base_ref, job.inputs_dir) == (SCAFFOLD_JOB_SLUG, execution.base_commit, None)
    assert job.template_variables["branch_name"] == f"{_PIPELINE_NAME}/{execution.execution_name}/setup/scaffold"
    assert job.template_variables["unit_count"] == "2"
    for task in run.tasks:
        for coordinate in task.coordinates:
            assert coordinate in job.template_variables["units_list"]


def test_map_discovers_one_job_per_feature_file_from_the_scaffolding_branch_with_the_guide_and_existing_witnesses(
    run: WitnessRun, repo: Path, cg: ConcurrencyGroup, tmp_path: Path
) -> None:
    base = _git(cg, repo, "rev-parse", "HEAD")
    scaffold_branch = f"{_PIPELINE_NAME}/x/setup/scaffold"
    create_branch(cg, repo, scaffold_branch, "HEAD")
    archive = _setup_archive(
        tmp_path,
        "Use the proxy_client fixture.",
        [
            {
                "test": "libs/proj/imbue/proj/server_test.py::test_origin",
                "coordinate": "forwarding.agent-origin",
                "partial": None,
            }
        ],
    )
    setup = _agent_outcome(SETUP_NODE_NAME, (_successful_result(SCAFFOLD_JOB_SLUG, base, scaffold_branch, archive),))

    jobs = MapBinding(witness_run=run).discover_jobs(_execution(repo, cg, run, (setup,)), _node(run, MAP_NODE_NAME))

    assert [(job.slug, job.base_ref, job.inputs_dir) for job in jobs] == [
        (_SIGNIN_SLUG, scaffold_branch, None),
        (_ROUTING_SLUG, scaffold_branch, None),
    ]
    by_slug = {job.slug: job.template_variables for job in jobs}
    assert by_slug[_SIGNIN_SLUG]["scaffolding_guide_markdown"] == "Use the proxy_client fixture."
    assert by_slug[_SIGNIN_SLUG]["feature_path"] == "libs/proj/behaviors/authentication/signin.feature"
    assert by_slug[_SIGNIN_SLUG]["generation_module"] == "libs/proj/imbue/proj/witnesses/authentication/signin_test.py"
    assert "server_test.py::test_origin" in by_slug[_ROUTING_SLUG]["units_section"]
    assert "server_test.py::test_origin" not in by_slug[_SIGNIN_SLUG]["units_section"]


def test_map_starts_from_the_base_commit_when_setup_published_no_branch(
    run: WitnessRun, repo: Path, cg: ConcurrencyGroup, tmp_path: Path
) -> None:
    base = _git(cg, repo, "rev-parse", "HEAD")
    archive = _setup_archive(tmp_path, "", [])
    setup = _agent_outcome(
        SETUP_NODE_NAME, (_successful_result(SCAFFOLD_JOB_SLUG, base, "unused", archive, is_branch_applied=False),)
    )
    execution = _execution(repo, cg, run, (setup,))

    jobs = MapBinding(witness_run=run).discover_jobs(execution, _node(run, MAP_NODE_NAME))

    assert scaffold_base_ref(execution) == base
    assert {job.base_ref for job in jobs} == {base}
    assert jobs[0].template_variables["scaffolding_guide_markdown"] == ""


def test_map_ignores_a_setup_archive_without_witness_links(
    run: WitnessRun, repo: Path, cg: ConcurrencyGroup, tmp_path: Path
) -> None:
    archive = tmp_path / "setup-archive"
    (archive / ARCHIVE_TEST_OUTPUT_DIRNAME).mkdir(parents=True)
    base = _git(cg, repo, "rev-parse", "HEAD")
    setup = _agent_outcome(SETUP_NODE_NAME, (_successful_result(SCAFFOLD_JOB_SLUG, base, base, archive),))

    jobs = MapBinding(witness_run=run).discover_jobs(_execution(repo, cg, run, (setup,)), _node(run, MAP_NODE_NAME))

    assert [job.slug for job in jobs] == [task.slug for task in run.tasks]


def test_review_builds_one_job_per_successful_mapper_from_its_branch_handed_its_archive(
    run: WitnessRun, repo: Path, cg: ConcurrencyGroup, tmp_path: Path
) -> None:
    base = _git(cg, repo, "rev-parse", "HEAD")
    mapper_branch = f"{_PIPELINE_NAME}/x/map/{_SIGNIN_SLUG}"
    archive = _mapper_archive(tmp_path, _SIGNIN_SLUG, '{"units": []}')
    execution = _execution(repo, cg, run)
    upstream = _successful_result(_SIGNIN_SLUG, base, mapper_branch, archive)

    job = ReviewBinding(witness_run=run).build_job(execution, _node(run, REVIEW_NODE_NAME), upstream)

    assert job is not None
    assert (job.slug, job.base_ref, job.inputs_dir) == (_SIGNIN_SLUG, mapper_branch, archive)
    assert job.template_variables["mapper_outcome_json"] == '{"units": []}'
    assert job.template_variables["mapper_branch_text"] == f"The mapper worked on branch `{mapper_branch}`"
    assert (
        job.template_variables["branch_name"] == f"{_PIPELINE_NAME}/{execution.execution_name}/review/{_SIGNIN_SLUG}"
    )


def test_review_still_checks_a_mapper_that_committed_nothing_from_where_it_started(
    run: WitnessRun, repo: Path, cg: ConcurrencyGroup, tmp_path: Path
) -> None:
    base = _git(cg, repo, "rev-parse", "HEAD")
    archive = _mapper_archive(tmp_path, _SIGNIN_SLUG, "{}")
    upstream = _successful_result(_SIGNIN_SLUG, base, "never-fetched", archive, is_branch_applied=False)

    job = ReviewBinding(witness_run=run).build_job(_execution(repo, cg, run), _node(run, REVIEW_NODE_NAME), upstream)

    assert job is not None
    assert job.base_ref == base
    assert job.template_variables["mapper_branch_text"].startswith("The mapper committed nothing")


def test_review_declines_a_result_it_has_no_task_for(
    run: WitnessRun, repo: Path, cg: ConcurrencyGroup, tmp_path: Path
) -> None:
    upstream = _successful_result("unknown-slug", "HEAD", "b", _mapper_archive(tmp_path, "unknown", "{}"))

    assert (
        ReviewBinding(witness_run=run).build_job(_execution(repo, cg, run), _node(run, REVIEW_NODE_NAME), upstream)
        is None
    )


def _reviewed_branches(cg: ConcurrencyGroup, repo: Path, base: str) -> tuple[str, str, str]:
    """A clean reviewed branch, a conflicting one, and a scaffold branch whose conftest the conflicting one contradicts."""
    clean_branch = f"{_PIPELINE_NAME}/x/review/{_SIGNIN_SLUG}"
    conflicting_branch = f"{_PIPELINE_NAME}/x/review/{_ROUTING_SLUG}"
    create_branch(cg, repo, clean_branch, base)
    _git(cg, repo, "checkout", "--quiet", clean_branch)
    _commit_file(
        cg,
        repo,
        "libs/proj/imbue/proj/witnesses/authentication/signin_test.py",
        "def test_a() -> None:\n    pass\n",
        "[CREATE_TEST] a",
    )
    create_branch(cg, repo, conflicting_branch, base)
    _git(cg, repo, "checkout", "--quiet", conflicting_branch)
    _commit_file(cg, repo, "libs/proj/imbue/proj/conftest.py", "SHARED = 1\n", "[CREATE_TEST] b")
    _git(cg, repo, "checkout", "--quiet", base)
    _commit_file(cg, repo, "libs/proj/imbue/proj/conftest.py", "SHARED = 2\n", "[SETUP] scaffold")
    scaffold_branch = f"{_PIPELINE_NAME}/x/setup/scaffold"
    create_branch(cg, repo, scaffold_branch, "HEAD")
    _git(cg, repo, "checkout", "--quiet", base)
    return clean_branch, conflicting_branch, scaffold_branch


def test_integrate_cherry_picks_reviewed_branches_and_the_annealer_continues_from_the_result(
    run: WitnessRun, repo: Path, cg: ConcurrencyGroup, tmp_path: Path
) -> None:
    base = _git(cg, repo, "rev-parse", "HEAD")
    clean_branch, conflicting_branch, scaffold_branch = _reviewed_branches(cg, repo, base)
    setup = _agent_outcome(SETUP_NODE_NAME, (_successful_result(SCAFFOLD_JOB_SLUG, base, scaffold_branch, tmp_path),))
    review = _agent_outcome(
        REVIEW_NODE_NAME,
        (
            _successful_result(_SIGNIN_SLUG, base, clean_branch, tmp_path),
            _successful_result(_ROUTING_SLUG, base, conflicting_branch, tmp_path),
        ),
    )
    execution = _execution(repo, cg, run, (setup, review))
    integrate = IntegrateBinding(witness_run=run, source_dir=repo, cg=cg)

    outcome = integrate.run(execution, _node(run, INTEGRATE_NODE_NAME))

    integration_branch = f"{_PIPELINE_NAME}/{execution.execution_name}/integrate/{INTEGRATION_JOB_SLUG}"
    assert outcome.error_summary is None
    assert outcome.branch_name == integration_branch
    assert integrate.integrated == [clean_branch], integrate.unintegrated
    assert [(entry.branch_name, entry.conflicting_paths) for entry in integrate.unintegrated] == [
        (conflicting_branch, ("libs/proj/imbue/proj/conftest.py",))
    ]
    assert (
        file_text_at(cg, repo, integration_branch, "libs/proj/imbue/proj/witnesses/authentication/signin_test.py")
        is not None
    )
    assert file_text_at(cg, repo, integration_branch, "libs/proj/imbue/proj/conftest.py") == "SHARED = 2\n"

    anneal = AnnealBinding(witness_run=run, source_dir=repo, cg=cg, integrate=integrate)
    job = anneal.build_job(execution.with_node_outcome(_integrate_outcome(outcome)), _node(run, REDUCE_NODE_NAME))

    assert (job.slug, job.base_ref, job.inputs_dir) == (ANNEAL_JOB_SLUG, integration_branch, execution.plan.output_dir)
    assert (
        job.template_variables["branch_name"]
        == f"{_PIPELINE_NAME}/{execution.execution_name}/reduce/{ANNEAL_JOB_SLUG}"
    )
    assert conflicting_branch in job.template_variables["unintegrated_list"]
    assert "libs/proj" in job.template_variables["changelog_paths_list"]


def test_integrate_uses_the_mapper_branch_when_the_reviewer_committed_nothing_and_skips_empty_pairs(
    run: WitnessRun, repo: Path, cg: ConcurrencyGroup, tmp_path: Path
) -> None:
    base = _git(cg, repo, "rev-parse", "HEAD")
    mapper_branch = f"{_PIPELINE_NAME}/x/map/{_SIGNIN_SLUG}"
    create_branch(cg, repo, mapper_branch, base)
    _git(cg, repo, "checkout", "--quiet", mapper_branch)
    _commit_file(
        cg, repo, "libs/proj/imbue/proj/witnesses/a_test.py", "def test_a() -> None:\n    pass\n", "[CREATE_TEST] a"
    )
    _git(cg, repo, "checkout", "--quiet", base)
    reviewed_without_commits = _successful_result(
        _SIGNIN_SLUG, mapper_branch, "unfetched", tmp_path, is_branch_applied=False
    )
    nothing_at_all = _successful_result(_ROUTING_SLUG, base, "unfetched-too", tmp_path, is_branch_applied=False)
    review = _agent_outcome(REVIEW_NODE_NAME, (reviewed_without_commits, nothing_at_all))
    integrate = IntegrateBinding(witness_run=run, source_dir=repo, cg=cg)

    outcome = integrate.run(_execution(repo, cg, run, (review,)), _node(run, INTEGRATE_NODE_NAME))

    assert outcome.error_summary is None
    assert integrate.integrated == [mapper_branch]
    assert integrate.unintegrated == []
    assert outcome.branch_name is not None
    assert file_text_at(cg, repo, outcome.branch_name, "libs/proj/imbue/proj/witnesses/a_test.py") is not None


def test_integrate_reports_an_error_when_the_integration_branch_already_exists(
    run: WitnessRun, repo: Path, cg: ConcurrencyGroup
) -> None:
    execution = _execution(repo, cg, run)
    integration_branch = f"{_PIPELINE_NAME}/{execution.execution_name}/integrate/{INTEGRATION_JOB_SLUG}"
    create_branch(cg, repo, integration_branch, "HEAD")

    outcome = IntegrateBinding(witness_run=run, source_dir=repo, cg=cg).run(execution, _node(run, INTEGRATE_NODE_NAME))

    assert outcome.branch_name is None
    assert outcome.error_summary is not None
    assert integration_branch in outcome.error_summary


def test_the_annealer_refuses_to_start_until_integration_produced_a_branch(
    run: WitnessRun, repo: Path, cg: ConcurrencyGroup
) -> None:
    integrate = IntegrateBinding(witness_run=run, source_dir=repo, cg=cg)
    anneal = AnnealBinding(witness_run=run, source_dir=repo, cg=cg, integrate=integrate)
    execution = _execution(repo, cg, run)
    failed = _integrate_outcome(OrchestratorOutcome(branch_name=None, summary="", error_summary="failed"))

    with pytest.raises(IntegrationMissingError, match="has not run"):
        anneal.build_job(execution, _node(run, REDUCE_NODE_NAME))
    with pytest.raises(IntegrationMissingError, match="made no branch"):
        anneal.build_job(execution.with_node_outcome(failed), _node(run, REDUCE_NODE_NAME))


def test_generation_module_mirrors_the_corpus_folders_under_the_generation_root(run: WitnessRun) -> None:
    by_slug = {task.slug: task for task in run.tasks}

    assert generation_module_for(run, by_slug[_SIGNIN_SLUG]) == Path(
        "libs/proj/imbue/proj/witnesses/authentication/signin_test.py"
    )
    assert generation_module_for(run, by_slug[_ROUTING_SLUG]) == Path(
        "libs/proj/imbue/proj/witnesses/forwarding/routing_test.py"
    )


def test_the_annealer_owes_a_changelog_entry_for_a_project_only_setup_touched(
    run: WitnessRun, repo: Path, cg: ConcurrencyGroup, tmp_path: Path
) -> None:
    base = _git(cg, repo, "rev-parse", "HEAD")
    scaffold_branch = f"{_PIPELINE_NAME}/x/setup/scaffold"
    create_branch(cg, repo, scaffold_branch, base)
    _git(cg, repo, "checkout", "--quiet", scaffold_branch)
    _commit_file(cg, repo, "libs/proj/imbue/proj/testing.py", "HELPER = 1\n", "[SETUP] shared helper")
    _git(cg, repo, "checkout", "--quiet", base)
    setup = _agent_outcome(SETUP_NODE_NAME, (_successful_result(SCAFFOLD_JOB_SLUG, base, scaffold_branch, tmp_path),))
    execution = _execution(repo, cg, run, (setup, _agent_outcome(REVIEW_NODE_NAME, ())))
    integrate = IntegrateBinding(witness_run=run, source_dir=repo, cg=cg)
    outcome = integrate.run(execution, _node(run, INTEGRATE_NODE_NAME))

    job = AnnealBinding(witness_run=run, source_dir=repo, cg=cg, integrate=integrate).build_job(
        execution.with_node_outcome(_integrate_outcome(outcome)), _node(run, REDUCE_NODE_NAME)
    )

    assert "libs/proj/changelog/witness-x-reduce-integrated.md" in job.template_variables["changelog_paths_list"]
