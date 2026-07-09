"""The coding-agent harness a given mngr agent runs (claude/codex/antigravity/opencode).

mngr's own `AgentDetails.type` already carries this (e.g. "claude",
"claude-worker", "codex-main") -- this module just parses that raw string
into a base `Harness`, stripping the `-main`/`-worker` role suffix that
`.mngr/settings.toml`'s per-harness agent_types add. No new mngr-side data
is needed; the type was always there, just never threaded through
system_interface's own response models.
"""

from __future__ import annotations

from enum import Enum


class Harness(str, Enum):
    CLAUDE = "claude"
    CODEX = "codex"
    ANTIGRAVITY = "antigravity"
    OPENCODE = "opencode"


def parse_harness(raw_agent_type: str) -> Harness | None:
    """Parse mngr's raw `AgentDetails.type` string into a base `Harness`.

    Strips the `-main`/`-worker` role suffix `.mngr/settings.toml`'s
    per-harness agent_types add (e.g. "claude-worker" -> "claude"). Returns
    None for an unrecognized value (e.g. a future harness this module
    hasn't been updated for yet, or a non-harness agent type like mngr's
    own service/proxy types) rather than raising -- callers decide whether
    an unknown harness is fatal or just means "don't show a
    harness-specific badge for this one."

    A more general fix would resolve the raw type through mngr's own
    parent_type chain + plugin-ownership registry (`resolve_agent_type` /
    `get_agent_type_owner` in vendor/mngr/libs/mngr/imbue/mngr/config/),
    which would correctly handle arbitrary custom agent-type names instead
    of only the two suffixes this repo happens to use today. Not done here:
    the call site in agent_manager.py's discovery-event handler is a hot
    per-event loop with no cheap MngrConfig on hand, so routing through the
    registry would need to be paired with real caching to avoid making that
    loop's existing cost problem worse, not just moved.
    """
    base = raw_agent_type.removesuffix("-main").removesuffix("-worker")
    try:
        return Harness(base)
    except ValueError:
        return None
