"""What a pipeline's names are bound to.

Each interface takes exactly what its node's fan-out shape implies and returns
exactly what that shape allows, so a binding supplies the content of one job
rather than deriving which jobs there should be.
"""

from abc import ABC
from abc import abstractmethod

from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr_mapreduce.execution import Execution
from imbue.mngr_mapreduce.execution import GateResult
from imbue.mngr_mapreduce.execution import GateSubject
from imbue.mngr_mapreduce.execution import Job
from imbue.mngr_mapreduce.execution import JobResult
from imbue.mngr_mapreduce.execution import OrchestratorOutcome
from imbue.mngr_mapreduce.pipeline import Gate
from imbue.mngr_mapreduce.pipeline import Node
from imbue.mngr_mapreduce.primitives import PartitionKey


class SingleJobBindingInterface(MutableModel, ABC):
    """Decides the one job a SINGLE node runs."""

    @abstractmethod
    def build_job(self, execution: Execution, node: Node) -> Job:
        """Build the node's one job."""


class PerUpstreamJobBindingInterface(MutableModel, ABC):
    """Decides the job each successful upstream result turns into, if any."""

    @abstractmethod
    def build_job(self, execution: Execution, node: Node, upstream_result: JobResult) -> Job | None:
        """Build the job for one successful upstream result, or None to decline it.

        Declining every result is how a filter that matches nothing is written;
        the node then has a fan-out of zero and succeeds vacuously.
        """


class PerPartitionBindingInterface(MutableModel, ABC):
    """Decides how upstream results are regrouped, and what each group's job is; the shuffle."""

    @abstractmethod
    def partition(self, execution: Execution, node: Node, upstream_result: JobResult) -> PartitionKey:
        """The partition an upstream result belongs to; one job runs per distinct key."""

    @abstractmethod
    def build_job(
        self,
        execution: Execution,
        node: Node,
        key: PartitionKey,
        upstream_results: tuple[JobResult, ...],
    ) -> Job:
        """Build the job for one partition."""


class DiscoveredJobsBindingInterface(MutableModel, ABC):
    """Decides a node's jobs from something the pipeline cannot see, like the contents of a corpus."""

    @abstractmethod
    def discover_jobs(self, execution: Execution, node: Node) -> list[Job]:
        """Return one job per agent to launch; an empty list means the node has nothing to do."""


AgentNodeBindingInterface = (
    SingleJobBindingInterface
    | PerUpstreamJobBindingInterface
    | PerPartitionBindingInterface
    | DiscoveredJobsBindingInterface
)


class OrchestratorNodeBindingInterface(MutableModel, ABC):
    """Performs an orchestrator node in the executor's own process."""

    @abstractmethod
    def run(self, execution: Execution, node: Node) -> OrchestratorOutcome:
        """Do the node's work and describe what it produced."""


class GateBindingInterface(MutableModel, ABC):
    """Checks one mechanical rule against a job's branch and archive."""

    @abstractmethod
    def check(self, gate: Gate, subject: GateSubject) -> GateResult:
        """Examine the subject and report whether it passes, with the reason when it does not."""


class ExecutionObserverInterface(MutableModel, ABC):
    """Receives the execution after every change, for manifests and reports."""

    @abstractmethod
    def on_execution_changed(self, execution: Execution) -> None:
        """React to the new execution state; keep it cheap and never raise."""


class PipelineExecutorInterface(MutableModel, ABC):
    """Runs a pipeline from its source nodes to its sinks."""

    @abstractmethod
    def execute(self) -> Execution:
        """Run every node whose dependencies are met, halting at the first node that fails."""
