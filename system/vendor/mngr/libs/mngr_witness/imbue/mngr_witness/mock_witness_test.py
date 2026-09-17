"""A scripted executor that plays every witness node's agent in-process, so the pipeline runs end to end without mngr."""

import json
import shutil
import time
from pathlib import Path

from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.pure import pure
from imbue.mngr.primitives import AgentName
from imbue.mngr_mapreduce.execution import AgentIdentity
from imbue.mngr_mapreduce.execution import BranchFetch
from imbue.mngr_mapreduce.execution import Job
from imbue.mngr_mapreduce.execution import LaunchedAgent
from imbue.mngr_mapreduce.execution import NodeHosts
from imbue.mngr_mapreduce.executor import AbstractPipelineExecutor
from imbue.mngr_mapreduce.pipeline import Node
from imbue.mngr_witness.bindings import WitnessRun
from imbue.mngr_witness.bindings import generation_module_for
from imbue.mngr_witness.conventions import changelog_entry_path
from imbue.mngr_witness.corpus import FeatureTask
from imbue.mngr_witness.gitops import checked_out_worktree
from imbue.mngr_witness.gitops import create_branch
from imbue.mngr_witness.outcomes import ARCHIVE_TEST_OUTPUT_DIRNAME
from imbue.mngr_witness.outcomes import Change
from imbue.mngr_witness.outcomes import ChangeStatus
from imbue.mngr_witness.outcomes import JUNIT_FILENAME
from imbue.mngr_witness.outcomes import MapperOutcome
from imbue.mngr_witness.outcomes import OUTCOME_FILENAME
from imbue.mngr_witness.outcomes import ReduceOutcome
from imbue.mngr_witness.outcomes import ReviewOutcome
from imbue.mngr_witness.outcomes import SCAFFOLDING_GUIDE_FILENAME
from imbue.mngr_witness.outcomes import ScaffoldingEntry
from imbue.mngr_witness.outcomes import ScaffoldingKind
from imbue.mngr_witness.outcomes import SetupOutcome
from imbue.mngr_witness.outcomes import TraceEntry
from imbue.mngr_witness.outcomes import UnitRecord
from imbue.mngr_witness.outcomes import UnitVerdict
from imbue.mngr_witness.outcomes import WITNESS_LINKS_FILENAME
from imbue.mngr_witness.outcomes import WitnessChangeKind
from imbue.mngr_witness.outcomes import WitnessedTest
from imbue.mngr_witness.outcomes import load_mapper_outcome
from imbue.mngr_witness.outcomes import write_outcome_json
from imbue.mngr_witness.pipeline import MAP_NODE_NAME
from imbue.mngr_witness.pipeline import REDUCE_NODE_NAME
from imbue.mngr_witness.pipeline import REVIEW_NODE_NAME
from imbue.mngr_witness.pipeline import SETUP_NODE_NAME


@pure
def witness_test_function_name(coordinate: str) -> str:
    return "test_" + coordinate.replace(".", "_").replace("-", "_")


@pure
def witness_assertion(coordinate: str) -> str:
    return f'assert "{coordinate}" == "{coordinate}"'


@pure
def render_witness_module(task: FeatureTask) -> str:
    """A ruff-clean test module with one witnessing test per unit, asserting the string its trace names."""
    blocks = [
        f'@pytest.mark.witnesses("{unit.coordinate}")\ndef {witness_test_function_name(unit.coordinate)}() -> None:\n    {witness_assertion(unit.coordinate)}\n'
        for unit in task.units
    ]
    return "import pytest\n\n\n" + "\n\n".join(blocks)


@pure
def unit_records(task: FeatureTask, module_path: str) -> tuple[UnitRecord, ...]:
    """FULL records whose trace names every clause of each unit against its test's assertion."""
    return tuple(
        UnitRecord(
            coordinate=unit.coordinate,
            verdict=UnitVerdict.FULL,
            tests=(
                WitnessedTest(
                    node_id=f"{module_path}::{witness_test_function_name(unit.coordinate)}",
                    partial=None,
                    trace=tuple(
                        TraceEntry(clause=clause, assertion=witness_assertion(unit.coordinate))
                        for clause in unit.clauses
                    ),
                ),
            ),
            open_world_clauses=(),
            blockers=(),
            behavior_problems=(),
            summary_markdown="scripted",
        )
        for unit in task.units
    )


@pure
def junit_xml(node_ids: list[str]) -> str:
    cases = "".join(
        f'<testcase classname="c" name="{node_id.split("::")[1]}" file="{node_id.split("::")[0]}"/>'
        for node_id in node_ids
    )
    return f"<testsuites><testsuite>{cases}</testsuite></testsuites>"


@pure
def links_jsonl(units: tuple[UnitRecord, ...]) -> str:
    return "".join(
        json.dumps({"test": test.node_id, "coordinate": unit.coordinate, "partial": test.partial}) + "\n"
        for unit in units
        for test in unit.tests
    )


class ScriptedWitnessExecutor(AbstractPipelineExecutor):
    """Plays each node's agent in-process: commits what a good agent would commit and publishes a well-formed archive."""

    repo: Path = Field(frozen=True, description="The operator checkout")
    cg: ConcurrencyGroup = Field(frozen=True, description="Runs git")
    witness_run: WitnessRun = Field(frozen=True, description="The run whose tasks the scripts play")
    published_root: Path = Field(frozen=True, description="Where each scripted agent leaves its archive")
    launched: list[LaunchedAgent] = Field(default_factory=list, description="Every agent launched, in order")
    prompt_by_agent_name: dict[AgentName, str] = Field(
        default_factory=dict, description="The prompt each agent was handed, as the executor rendered it"
    )

    def provision_hosts(self, node: Node, job_count: int) -> NodeHosts:
        return NodeHosts(node_name=node.name, snapshot=None, host_count=0)

    def release_hosts(self, hosts: NodeHosts) -> None:
        return None

    def launch_agent(
        self, node: Node, job: Job, prompt: str, identity: AgentIdentity, hosts: NodeHosts, job_idx: int
    ) -> LaunchedAgent:
        create_branch(self.cg, self.repo, identity.branch_name, job.base_ref)
        archive = self.published_root / str(identity.agent_name) / ARCHIVE_TEST_OUTPUT_DIRNAME
        archive.mkdir(parents=True)
        with checked_out_worktree(self.cg, self.repo, identity.branch_name, is_detached=False) as worktree:
            if node.name == SETUP_NODE_NAME:
                self._play_scaffold(worktree, archive)
            elif node.name == MAP_NODE_NAME:
                self._play_mapper(worktree, archive, job)
            elif node.name == REVIEW_NODE_NAME:
                self._play_reviewer(archive, job)
            elif node.name == REDUCE_NODE_NAME:
                self._play_annealer(worktree, archive)
            else:
                raise AssertionError(f"no script for node {node.name}")
        self.prompt_by_agent_name[identity.agent_name] = prompt
        agent = LaunchedAgent(
            node_name=node.name,
            job=job,
            agent_name=identity.agent_name,
            agent_handle=str(identity.agent_name),
            branch_name=identity.branch_name,
            created_at_monotonic=time.monotonic(),
        )
        self.launched.append(agent)
        return agent

    def is_published(self, agent: LaunchedAgent) -> bool:
        return True

    def pull_outputs(self, agent: LaunchedAgent, destination_dir: Path) -> Path | None:
        source = self.published_root / str(agent.agent_name)
        destination = destination_dir / str(agent.agent_name)
        shutil.copytree(source, destination)
        return destination

    def fetch_branch(self, agent: LaunchedAgent, archive_dir: Path) -> BranchFetch:
        """The branch is already in the repo; what the real executor learns from the bundle is whether it moved."""
        tip = self._rev_parse(agent.branch_name)
        return BranchFetch.APPLIED if tip != self._rev_parse(agent.job.base_ref) else BranchFetch.NONE_PUBLISHED

    def _rev_parse(self, ref: str) -> str:
        return self.cg.run_process_to_completion(["git", "rev-parse", ref], cwd=self.repo).stdout.strip()

    def stop_agent(self, agent: LaunchedAgent) -> None:
        return None

    def _commit(self, worktree: Path, files: dict[str, str], subject: str) -> str:
        for relative_path, text in files.items():
            path = worktree / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            self.cg.run_process_to_completion(["git", "add", relative_path], cwd=worktree)
        self.cg.run_process_to_completion(["git", "commit", "--quiet", "-m", subject], cwd=worktree)
        return self.cg.run_process_to_completion(["git", "rev-parse", "HEAD"], cwd=worktree).stdout.strip()

    def _play_scaffold(self, worktree: Path, archive: Path) -> None:
        root = self.witness_run.generation_root
        files = {f"{root.as_posix()}/__init__.py": ""}
        for task in self.witness_run.tasks:
            files[f"{(root / task.relative_path.parent).as_posix()}/__init__.py"] = ""
        files[f"{root.parent.as_posix()}/conftest.py"] = (
            "import pytest\n\n\n@pytest.fixture\ndef proxy_client() -> str:\n    return 'client'\n".replace("'", '"')
        )
        commit = self._commit(worktree, files, "[SETUP] scaffold the witnesses package and the proxy client")
        outcome = SetupOutcome(
            scaffolding=(
                ScaffoldingEntry(
                    name="proxy_client",
                    kind=ScaffoldingKind.FIXTURE,
                    location=f"{root.parent.as_posix()}/conftest.py",
                    provides="a client",
                    use_when="HTTP flows",
                ),
            ),
            changes=(
                Change(
                    kind=WitnessChangeKind.SETUP,
                    status=ChangeStatus.SUCCEEDED,
                    commit_hash=commit,
                    summary="scaffolded",
                ),
            ),
            summary_markdown="scripted setup",
        )
        (archive / OUTCOME_FILENAME).write_text(write_outcome_json(outcome))
        (archive / SCAFFOLDING_GUIDE_FILENAME).write_text("Use the proxy_client fixture for HTTP flows.\n")
        (archive / WITNESS_LINKS_FILENAME).write_text("")
        (archive / JUNIT_FILENAME).write_text(junit_xml([]))

    def _play_mapper(self, worktree: Path, archive: Path, job: Job) -> None:
        task = next(task for task in self.witness_run.tasks if task.slug == job.slug)
        module_path = generation_module_for(self.witness_run, task).as_posix()
        commit = self._commit(
            worktree,
            {module_path: render_witness_module(task)},
            f"[CREATE_TEST] {', '.join(task.coordinates)}: witness",
        )
        units = unit_records(task, module_path)
        outcome = MapperOutcome(
            units=units,
            changes=(
                Change(
                    kind=WitnessChangeKind.CREATE_TEST,
                    status=ChangeStatus.SUCCEEDED,
                    commit_hash=commit,
                    summary="witnessed",
                ),
            ),
            scaffolding_added=(),
            errored=False,
            summary_markdown="scripted map",
        )
        (archive / OUTCOME_FILENAME).write_text(write_outcome_json(outcome))
        (archive / JUNIT_FILENAME).write_text(junit_xml([test.node_id for unit in units for test in unit.tests]))
        (archive / WITNESS_LINKS_FILENAME).write_text(links_jsonl(units))

    def _play_reviewer(self, archive: Path, job: Job) -> None:
        assert job.inputs_dir is not None
        mapper_outcome = load_mapper_outcome(job.inputs_dir)
        outcome = ReviewOutcome(
            units=mapper_outcome.units,
            findings=(),
            branch_accepted=True,
            changes=(),
            summary_markdown="scripted review: nothing to fix",
        )
        (archive / OUTCOME_FILENAME).write_text(write_outcome_json(outcome))
        (archive / JUNIT_FILENAME).write_text(
            junit_xml([test.node_id for unit in mapper_outcome.units for test in unit.tests])
        )
        (archive / WITNESS_LINKS_FILENAME).write_text(links_jsonl(mapper_outcome.units))

    def _play_annealer(self, worktree: Path, archive: Path) -> None:
        project = self.witness_run.generation_root.parts[0] + "/" + self.witness_run.generation_root.parts[1]
        entry = changelog_entry_path(project, self.witness_run.changelog_branch)
        commit = self._commit(
            worktree, {entry: "Add the witnesses the scripted run generated.\n"}, "[CHANGELOG] entry"
        )
        all_units = tuple(
            unit
            for task in self.witness_run.tasks
            for unit in unit_records(task, generation_module_for(self.witness_run, task).as_posix())
        )
        outcome = ReduceOutcome(
            integrated_branches=(),
            conflicts_resolved=(),
            normalizations=(),
            escalations=(),
            changelog_paths=(entry,),
            changes=(
                Change(
                    kind=WitnessChangeKind.CHANGELOG,
                    status=ChangeStatus.SUCCEEDED,
                    commit_hash=commit,
                    summary="changelog",
                ),
            ),
            summary_markdown="scripted anneal",
        )
        (archive / OUTCOME_FILENAME).write_text(write_outcome_json(outcome))
        (archive / JUNIT_FILENAME).write_text(junit_xml([test.node_id for unit in all_units for test in unit.tests]))
        (archive / WITNESS_LINKS_FILENAME).write_text(links_jsonl(all_units))
