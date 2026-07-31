"""The one place a harness is registered.

A harness is resolved EXACTLY ONCE, in :mod:`agent_discovery`, from mngr's
``AgentDetails.type``, and carried on ``AgentInfo.harness``. Every component downstream
receives an already-resolved object from this module and never re-derives, re-checks, or
branches on the harness name.

Adding a harness is one :class:`HarnessSpec` entry here, one subclass per concern, and --
if it emits markers of its own -- one member of
:class:`~imbue.system_interface.harnesses.events.SpecialEventKind`. Nothing else changes:
``app_context`` builds watchers through :func:`build_watcher`, ``agent_manager`` builds
trackers through :func:`build_tracker`, and neither names a harness.

One spec per harness rather than a dict per concern, deliberately: parallel registries
would mean two places to edit for one harness, which is how the split drifts.
"""

from typing import Final

from loguru import logger

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.harnesses.codex.watcher import CodexSessionWatcher
from imbue.system_interface.harnesses.activity import HarnessActivityTracker
from imbue.system_interface.harnesses.claude.activity import ClaudeActivityTracker
from imbue.system_interface.harnesses.codex.activity import CodexActivityTracker
from imbue.system_interface.harnesses.events import SpecialEventKind
from imbue.system_interface.harnesses.session_watcher import AgentSessionWatcher
from imbue.system_interface.harnesses.session_watcher import OnEventsCallback
from imbue.system_interface.harnesses.claude.watcher import ClaudeSessionWatcher


class HarnessSpec(FrozenModel):
    """Everything the system interface needs to run one harness."""

    # The watcher/tracker fields hold CLASSES, which pydantic cannot validate structurally.
    model_config = {"arbitrary_types_allowed": True}

    name: str
    watcher_class: type[AgentSessionWatcher]
    tracker_class: type[HarnessActivityTracker]
    # The special-event kinds this harness may emit. A parser emitting a kind outside its
    # own declaration is a bug; an empty set is the honest statement that a harness's
    # transcript carries no markers, not an omission.
    special_kinds: frozenset[SpecialEventKind]


# Fallback for an agent whose type has no spec -- mngr agent types with no harness of
# their own (e.g. ``wait``) still get a readable transcript and lifecycle-plus-tail
# activity rather than a dead chat tab.
DEFAULT_HARNESS: Final[str] = "claude"

HARNESS_SPECS: Final[dict[str, HarnessSpec]] = {
    "claude": HarnessSpec(
        name="claude",
        watcher_class=ClaudeSessionWatcher,
        tracker_class=ClaudeActivityTracker,
        # Claude Code's transcript has no turn boundaries; activity is inferred from an
        # unmatched tool_use plus the transcript tail.
        special_kinds=frozenset(),
    ),
    "codex": HarnessSpec(
        name="codex",
        watcher_class=CodexSessionWatcher,
        tracker_class=CodexActivityTracker,
        special_kinds=frozenset(
            {
                SpecialEventKind.TURN_STARTED,
                SpecialEventKind.TURN_COMPLETED,
                SpecialEventKind.TURN_ABORTED,
            }
        ),
    ),
}


def get_harness_spec(harness: str) -> HarnessSpec:
    """The spec for ``harness``, falling back to :data:`DEFAULT_HARNESS`."""
    spec = HARNESS_SPECS.get(harness)
    if spec is not None:
        return spec
    logger.warning("No harness spec for {!r}; falling back to {}", harness, DEFAULT_HARNESS)
    return HARNESS_SPECS[DEFAULT_HARNESS]


def build_watcher(agent_info: AgentInfo, on_events: OnEventsCallback) -> AgentSessionWatcher:
    """Build the session watcher for ``agent_info``'s harness, not yet started."""
    return get_harness_spec(agent_info.harness).watcher_class.build(agent_info, on_events)


def build_tracker(harness: str) -> HarnessActivityTracker:
    """Build the activity tracker for ``harness``."""
    return get_harness_spec(harness).tracker_class.build()
