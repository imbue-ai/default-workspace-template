from datetime import datetime
from pathlib import Path

from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.interfaces.data_types import HostDetails
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import ProviderInstanceName


def make_agent_details(
    name: str,
    host_name: str,
    create_time: datetime,
    labels: dict[str, str],
) -> AgentDetails:
    """Create an AgentDetails carrying only the fields host pruning reads."""
    return AgentDetails(
        id=AgentId.generate(),
        name=AgentName(name),
        type="claude",
        command=CommandString("claude"),
        work_dir=Path("/mngr/worktrees") / name,
        initial_branch=None,
        create_time=create_time,
        start_on_boot=False,
        state=AgentLifecycleState.STOPPED,
        labels=labels,
        host=HostDetails(
            id=HostId.generate(),
            name=host_name,
            provider_name=ProviderInstanceName("modal"),
        ),
    )
