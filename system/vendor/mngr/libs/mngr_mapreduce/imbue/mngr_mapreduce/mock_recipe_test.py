"""A concrete ``MapReduceRecipe`` that records the framework's callbacks.

The orchestrator hands every state change back to the recipe, so tests of the
framework need a real recipe implementation. This one interprets nothing: it
records what it was given so tests can assert on the framework's behavior.
"""

from collections.abc import Sequence
from pathlib import Path

from pydantic import Field

from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr_mapreduce.data_types import AgentMetadata
from imbue.mngr_mapreduce.data_types import MapReduceContext
from imbue.mngr_mapreduce.data_types import MapReduceRecipe
from imbue.mngr_mapreduce.data_types import MapReduceTask
from imbue.mngr_mapreduce.data_types import MapperInfo


class RecordingRecipe(MutableModel, MapReduceRecipe):
    """Records every framework callback instead of acting on it."""

    name: str = "mock-mapreduce"
    finalized_mapper_dirs: list[Path] = Field(
        default_factory=list,
        description="The agent_dir handed to each on_mapper_finalized call, in call order",
    )
    rendered_agents: list[tuple[AgentMetadata, ...]] = Field(
        default_factory=list,
        description="The agent metadata handed to each render_report call, in call order",
    )

    def discover(self, ctx: MapReduceContext) -> list[MapReduceTask]:
        return []

    def build_mapper_prompt(self, ctx: MapReduceContext, task: MapReduceTask) -> str:
        return f"map {task.id}"

    def build_reducer_prompt(self, ctx: MapReduceContext) -> str:
        return "reduce"

    def on_mapper_finalized(self, ctx: MapReduceContext, agent_dir: Path, info: MapperInfo) -> None:
        self.finalized_mapper_dirs.append(agent_dir)

    def render_report(
        self,
        ctx: MapReduceContext,
        agents: Sequence[AgentMetadata],
        reducer: AgentMetadata | None,
    ) -> Path | None:
        self.rendered_agents.append(tuple(agents))
        return None
