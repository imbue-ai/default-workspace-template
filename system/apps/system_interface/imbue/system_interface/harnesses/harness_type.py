"""The closed set of harnesses, and the one place a raw mngr type becomes one.

mngr's ``AgentDetails.type`` is an open string -- it names agent types that are not
harnesses at all (``main``, ``wait``). :func:`parse_harness` is the single boundary that
narrows it, so every field, dict key and function parameter downstream is a
:class:`HarnessType` and no component has to re-check or fall back again.

Lives in its own module rather than in ``registry.py`` because ``agent_discovery`` -- the
place the narrowing happens -- is imported *by* the registry, so the enum has to sit below
both to avoid a cycle.
"""

from enum import StrEnum

from loguru import logger


class HarnessType(StrEnum):
    """A harness the system interface knows how to watch."""

    CLAUDE = "claude"
    CODEX = "codex"
    # The mngr agent type is ``pi-coding`` (``pi`` is only an alias); this value MUST
    # match it so ``parse_harness`` resolves a pi agent here and not the default.
    PI_CODING = "pi-coding"
    OPENCODE = "opencode"
    # The mngr agent type is ``antigravity`` (``agy`` is only an alias); as with pi above,
    # this value MUST match the type, not the alias, or ``parse_harness`` falls through.
    ANTIGRAVITY = "antigravity"


# What an agent whose mngr type is not a harness is treated as. Such agents still get a
# readable transcript and lifecycle-plus-tail activity rather than a dead chat tab.
DEFAULT_HARNESS = HarnessType.CLAUDE


def parse_harness(agent_type: str | None) -> HarnessType:
    """Narrow a raw mngr agent type to a :class:`HarnessType`, defaulting when it is not one."""
    try:
        return HarnessType(agent_type)
    except ValueError:
        logger.debug("Agent type {!r} is not a harness; treating it as {}", agent_type, DEFAULT_HARNESS)
        return DEFAULT_HARNESS
