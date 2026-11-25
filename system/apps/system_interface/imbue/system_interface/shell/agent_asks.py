"""The task an "Ask an agent ..." button gives the agent, for the pages the shell's server renders itself (its recovery
page, an app's could-not-start page). They cannot import workspace_ui's ``agentFixTask`` (``app_contract.ts``): each is
served exactly when a bundle may be missing or an app is not there to serve one. So the same text is worded here, and
a ``frontend`` test pins it to that source."""

import re
from typing import Final

from imbue.imbue_common.pure import pure

AGENT_FIX_TASK_LEAD: Final[str] = "Imbue Studio showed the user this failure: "
AGENT_FIX_TASK_DETAILS_LABEL: Final[str] = "Details:"
AGENT_FIX_INSTRUCTION: Final[str] = (
    "Find out why it failed and fix it, then tell the user what was wrong and what you changed."
)
_SHORTEST_FENCE_LENGTH: Final[int] = 3


@pure
def agent_fix_task(what_failed: str, details: str | None, instruction: str | None = None) -> str:
    """The task every "Ask an agent ..." button gives the agent, as ``agentFixTask`` words it: what the person was
    shown, the details fenced as they are, and what to do, said by Imbue Studio about the user. ``instruction`` says
    what to do where that is not to find and fix a fault."""
    stem = what_failed.strip()
    parts = [f"{AGENT_FIX_TASK_LEAD}{stem if re.search(r'[.!?]$', stem) else f'{stem}.'}"]
    if details is not None and details.strip() != "":
        longest_run = max((len(run) for run in re.findall(r"`+", details)), default=0)
        fence = "`" * max(_SHORTEST_FENCE_LENGTH, longest_run + 1)
        parts.append(f"{AGENT_FIX_TASK_DETAILS_LABEL}\n{fence}\n{details}\n{fence}")
    parts.append(AGENT_FIX_INSTRUCTION if instruction is None else instruction)
    return "\n\n".join(parts)
