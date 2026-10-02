"""What the memory guard closed, in words a user recognizes, from the shed ledger its kill hook writes.

earlyoom's kill hook (system/services/oom_priority/bin/earlyoom_record_shed.py) appends one ``process_shed`` record
per kill to ``data/.state/oom_priority/events/shed.jsonl``: when, the process's pid and name, the agent when it was an
agent's own process, and the memory it held. The process is gone by then, so a browser renderer can only be called
"a browser tab": the ledger holds no tab. Closures the kernel makes at the container's limit (a local workspace's
cgroup) bypass earlyoom and are not recorded, which the page says.
"""

from collections.abc import Mapping
from collections.abc import Sequence
from datetime import datetime
from enum import auto
from typing import Any

from pydantic import Field

from activity.processes import BROWSER_COMMAND_NAMES
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from oom_priority.ledger import RECORD_TYPE_PROCESS_SHED


class ClosedKind(UpperCaseStrEnum):
    """What kind of thing was closed."""

    CHAT_AGENT = auto()
    BROWSER_TAB = auto()
    PROGRAM = auto()


class ClosedProcess(FrozenModel):
    """One thing the memory guard closed."""

    at: datetime = Field(description="When it was closed")
    kind: ClosedKind = Field(description="What kind of thing it was")
    what: str = Field(description="What was closed, in plain words")
    next_step: str = Field(description="What it means for the user, or what to do")
    freed_kib: int | None = Field(description="The memory it held, when earlyoom reported it")
    command_name: str = Field(description="The process's short name, for the details")
    pid: int = Field(description="Its process id, for the details")
    agent_name: str | None = Field(description="The agent, when it was an agent's own process")


@pure
def describe_closure(record: Mapping[str, Any]) -> ClosedProcess | None:
    """A ``process_shed`` record in plain words, or None for any other record or a malformed one."""
    if record.get("type") != RECORD_TYPE_PROCESS_SHED:
        return None
    try:
        at = datetime.fromisoformat(str(record["timestamp"]).replace("Z", "+00:00"))
        pid = int(record["pid"])
    except (KeyError, TypeError, ValueError):
        return None
    if at.tzinfo is None:
        return None
    command_name = str(record.get("comm") or "a process")
    agent_name = record.get("agent_name")
    freed = record.get("vm_rss_kib")
    freed_kib = int(freed) if isinstance(freed, int) else None
    if isinstance(agent_name, str) and agent_name:
        kind = ClosedKind.CHAT_AGENT
        what = f'the agent of "{agent_name}"'
        next_step = "Its conversation is kept; send it a message to carry on."
    elif command_name in BROWSER_COMMAND_NAMES:
        kind = ClosedKind.BROWSER_TAB
        what = "a browser tab"
        next_step = "An agent that was using it opens the page again when it needs it."
    else:
        kind = ClosedKind.PROGRAM
        what = f"a program an agent was running ({command_name})"
        next_step = "The agent that ran it may need to try again, ideally in a way that uses less memory."
    return ClosedProcess(
        at=at,
        kind=kind,
        what=what,
        next_step=next_step,
        freed_kib=freed_kib,
        command_name=command_name,
        pid=pid,
        agent_name=agent_name if isinstance(agent_name, str) and agent_name else None,
    )


@pure
def closures_since(records: Sequence[Mapping[str, Any]], since: datetime) -> list[ClosedProcess]:
    """Every closure at or after ``since``, newest first."""
    closures = [closure for closure in (describe_closure(record) for record in records) if closure is not None]
    return sorted(
        (closure for closure in closures if closure.at >= since), key=lambda closure: closure.at, reverse=True
    )
