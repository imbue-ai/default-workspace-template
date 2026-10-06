#!/usr/bin/env python3
"""SessionStart hook: tell a revived agent it was stopped for memory pressure.

When earlyoom sheds an agent's own main process, the kill hook records it in the
shed ledger. The agent stays down until the user (or its lead) next messages it,
which restarts the claude process and fires this hook. The hook looks for shed
records naming this agent that have not yet been acknowledged, prints a notice
(SessionStart stdout becomes session context), and appends a delivery marker so
the same notice is not injected again.

Records name an agent, and a name outlives its agent: the chat app destroys a
shed spare chat agent, and the next agent minted under its "Chat N" (a new chat,
or the spare's replacement) inherits the name. A record older than the agent's
own creation is an earlier agent's, and so is every record under an agent still
labelled a spare (``chat_spare=true``), which is never a revived one; those are
acknowledged without the notice.

Self-contained beyond the stdlib-only ``oom_priority`` package (imported via a
``sys.path`` insert), since claude runs SessionStart hooks under a plain
``python3``.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oom_priority.agent_identity import agent_created_at, is_spare_agent
from oom_priority.ledger import (
    append_notice_delivered,
    format_timestamp,
    pending_shed_timestamps,
    read_records,
)


def _is_own_shed_pending(agent_name: str, pending: list[str]) -> bool:
    """Whether any of ``pending`` is a shed of the agent now under ``agent_name``,
    rather than of an earlier agent that had the name."""
    if is_spare_agent(agent_name):
        return False
    created_at = agent_created_at(agent_name)
    if created_at is None:
        return True
    return max(pending) >= format_timestamp(created_at)


def main() -> None:
    agent_name = os.environ.get("MNGR_AGENT_NAME", "")
    if not agent_name:
        return
    pending = pending_shed_timestamps(read_records(), agent_name)
    if not pending:
        return
    if not _is_own_shed_pending(agent_name, pending):
        append_notice_delivered(agent_name, max(pending))
        return

    print(
        "Note: you were previously stopped to relieve a memory-pressure "
        "(out-of-memory) situation in this workspace. Any background tasks you "
        "had running -- for example polling loops waiting on another agent or an "
        "external event -- were cancelled and were NOT automatically restarted. "
        "If you were in the middle of multi-step work, re-check the current state "
        "before continuing rather than assuming your last action completed. If you "
        "were running a memory-intensive task, do not simply re-run it as-is -- it "
        "will likely be stopped again; first find a way to do it using less memory "
        "(smaller batches, streaming instead of loading everything at once, "
        "releasing data you no longer need), and only retry if you can."
    )
    append_notice_delivered(agent_name, max(pending))


if __name__ == "__main__":
    main()
