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

from collections.abc import Callable
from typing import Final

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.harnesses.antigravity.activity import AntigravityActivityTracker
from imbue.system_interface.harnesses.antigravity.model import ANTIGRAVITY_CATALOG
from imbue.system_interface.harnesses.antigravity.model import AntigravityModelResolver
from imbue.system_interface.harnesses.antigravity.watcher import AntigravityPlaceholderSessionWatcher
from imbue.system_interface.harnesses.codex.watcher import CodexSessionWatcher
from imbue.system_interface.harnesses.activity import HarnessActivityTracker
from imbue.system_interface.harnesses.claude.activity import ClaudeActivityTracker
from imbue.system_interface.harnesses.claude.model import CLAUDE_CATALOG
from imbue.system_interface.harnesses.claude.model import ClaudeModelResolver
from imbue.system_interface.harnesses.codex.activity import CodexActivityTracker
from imbue.system_interface.harnesses.codex.model import CODEX_CATALOG
from imbue.system_interface.harnesses.codex.model import CodexModelResolver
from imbue.system_interface.harnesses.events import SpecialEventKind
from imbue.system_interface.harnesses.harness_type import HarnessType
from imbue.system_interface.harnesses.opencode.activity import OpenCodeActivityTracker
from imbue.system_interface.harnesses.opencode.model import OpenCodeModelResolver
from imbue.system_interface.harnesses.opencode.model import get_catalog as get_opencode_catalog
from imbue.system_interface.harnesses.opencode.watcher import OpenCodePlaceholderSessionWatcher
from imbue.system_interface.harnesses.pi_coding.activity import PiActivityTracker
from imbue.system_interface.harnesses.pi_coding.model import PiModelResolver
from imbue.system_interface.harnesses.pi_coding.model import get_catalog as get_pi_catalog
from imbue.system_interface.harnesses.pi_coding.watcher import PiPlaceholderSessionWatcher
from imbue.system_interface.harnesses.model import HarnessCatalog
from imbue.system_interface.harnesses.model import HarnessModelResolver
from imbue.system_interface.harnesses.session_watcher import AgentSessionWatcher
from imbue.system_interface.harnesses.session_watcher import OnEventsCallback
from imbue.system_interface.harnesses.claude.watcher import ClaudeSessionWatcher


class HarnessSpec(FrozenModel):
    """Everything the system interface needs to run one harness."""

    # The watcher/tracker fields hold CLASSES, which pydantic cannot validate structurally.
    model_config = {"arbitrary_types_allowed": True}

    name: HarnessType
    watcher_class: type[AgentSessionWatcher]
    tracker_class: type[HarnessActivityTracker]
    # The model resolver class -- a true peer of watcher_class/tracker_class, so it
    # sits flat here and AgentManager calls ``.build(agent_info)`` on it the same way.
    resolver_class: type[HarnessModelResolver]
    # A factory for the per-harness model catalog, called (once, cached) by ``get_catalog``.
    # Behind a factory rather than a value because pi/opencode PARSE thousands of models off
    # disk -- too much for import time, and importing this module must not fail on an image
    # where a harness's data is absent. claude/codex just return their hand-written constant.
    catalog_factory: Callable[[], HarnessCatalog]
    # The special-event kinds this harness may emit. A parser emitting a kind outside its
    # own declaration is a bug; an empty set is the honest statement that a harness's
    # transcript carries no markers, not an omission.
    special_kinds: frozenset[SpecialEventKind]


HARNESS_SPECS: Final[dict[HarnessType, HarnessSpec]] = {
    HarnessType.CLAUDE: HarnessSpec(
        name=HarnessType.CLAUDE,
        watcher_class=ClaudeSessionWatcher,
        tracker_class=ClaudeActivityTracker,
        resolver_class=ClaudeModelResolver,
        catalog_factory=lambda: CLAUDE_CATALOG,
        # Claude Code's transcript has no turn boundaries; activity is inferred from an
        # unmatched tool_use plus the transcript tail.
        special_kinds=frozenset(),
    ),
    HarnessType.CODEX: HarnessSpec(
        name=HarnessType.CODEX,
        watcher_class=CodexSessionWatcher,
        tracker_class=CodexActivityTracker,
        resolver_class=CodexModelResolver,
        catalog_factory=lambda: CODEX_CATALOG,
        special_kinds=frozenset(
            {
                SpecialEventKind.TURN_STARTED,
                SpecialEventKind.TURN_COMPLETED,
                SpecialEventKind.TURN_ABORTED,
            }
        ),
    ),
    HarnessType.PI_CODING: HarnessSpec(
        name=HarnessType.PI_CODING,
        # First cut: the model bar is wired, the transcript is not -- so a placeholder
        # watcher (empty transcript) and claude-style activity. The real watcher lands next.
        watcher_class=PiPlaceholderSessionWatcher,
        tracker_class=PiActivityTracker,
        resolver_class=PiModelResolver,
        catalog_factory=get_pi_catalog,
        special_kinds=frozenset(),
    ),
    HarnessType.OPENCODE: HarnessSpec(
        name=HarnessType.OPENCODE,
        # First cut: the model bar is wired, the transcript is not -- so a placeholder
        # watcher (empty transcript) and claude-style activity. The real watcher (tailing
        # the plugin's raw transcript) lands next.
        watcher_class=OpenCodePlaceholderSessionWatcher,
        tracker_class=OpenCodeActivityTracker,
        resolver_class=OpenCodeModelResolver,
        catalog_factory=get_opencode_catalog,
        special_kinds=frozenset(),
    ),
    HarnessType.ANTIGRAVITY: HarnessSpec(
        name=HarnessType.ANTIGRAVITY,
        # First cut: the model bar is wired, the transcript is not -- so a placeholder
        # watcher (empty transcript) and claude-style activity. The real watcher (tailing
        # agy's raw transcript) lands next.
        watcher_class=AntigravityPlaceholderSessionWatcher,
        tracker_class=AntigravityActivityTracker,
        resolver_class=AntigravityModelResolver,
        catalog_factory=lambda: ANTIGRAVITY_CATALOG,
        special_kinds=frozenset(),
    ),
}


def get_harness_spec(harness: HarnessType) -> HarnessSpec:
    """The spec for ``harness``. Total: every member has one, checked by ``test_every_harness_has_a_spec``."""
    return HARNESS_SPECS[harness]


def build_watcher(agent_info: AgentInfo, on_events: OnEventsCallback) -> AgentSessionWatcher:
    """Build the session watcher for ``agent_info``'s harness, not yet started."""
    return get_harness_spec(agent_info.harness).watcher_class.build(agent_info, on_events)


def build_tracker(harness: HarnessType) -> HarnessActivityTracker:
    """Build the activity tracker for ``harness``."""
    return get_harness_spec(harness).tracker_class.build()


def build_resolver(agent_info: AgentInfo) -> HarnessModelResolver:
    """Build the model resolver for ``agent_info``'s harness."""
    return get_harness_spec(agent_info.harness).resolver_class.build(agent_info)


# Built once per process from each harness's factory. The parsed catalogs (pi/opencode)
# read data files that ship in the image, so a non-empty catalog is immutable for the
# container's life and cached unconditionally; an empty one (harness data absent) is NOT
# cached, so a later call retries rather than freezing the blank.
_CATALOG_CACHE: dict[HarnessType, HarnessCatalog] = {}


def get_catalog(harness: HarnessType) -> HarnessCatalog:
    """The model catalog for ``harness``, built via its factory and cached when non-empty."""
    cached = _CATALOG_CACHE.get(harness)
    if cached is not None:
        return cached
    catalog = get_harness_spec(harness).catalog_factory()
    if catalog.options:
        _CATALOG_CACHE[harness] = catalog
    return catalog
