"""THROWAWAY harness parts, for a harness that is registered but not yet implemented.

A harness cannot be registered without one: :class:`~imbue.system_interface.harnesses.registry.HarnessSpec`
requires a watcher, a tracker, a resolver and a catalog factory, all four abstract. So a
harness whose LAUNCH path is ready but whose transcript/model work has not started yet
would otherwise be unregisterable -- and an unregistered harness falls back to
``DEFAULT_HARNESS`` (claude) in :func:`~imbue.system_interface.harnesses.harness_type.parse_harness`,
which is worse than an honest blank: it would point claude's watcher at another harness's
state dir.

These are deliberately inert, not merely minimal. The watcher parses nothing and reports an
empty transcript; the resolver switches nothing; the catalog is empty (so the model bar
renders the unrecognized-model marker). The one thing that IS live is the activity dot,
because it costs two lines: both plugins that use this maintain mngr's ``active`` marker
already, so the dot tracks whether a turn is in flight even with no transcript behind it.

DELETION CRITERION: when a harness lands its real watcher/tracker/resolver/catalog, it drops
its ``placeholder.py`` and its registry entry stops naming anything here. When the last
harness stops importing this module, delete the module. Nothing here is a base class to
build on -- a real implementation replaces it outright rather than subclassing it.
"""

from collections.abc import Callable
from typing import Any
from typing import Final

from imbue.system_interface.activity_state import ActivityState
from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.harnesses.activity import HarnessActivityTracker
from imbue.system_interface.harnesses.model import HarnessCatalog
from imbue.system_interface.harnesses.model import HarnessModelResolver
from imbue.system_interface.harnesses.model import ModelAxis
from imbue.system_interface.harnesses.model import ModelIdentity
from imbue.system_interface.harnesses.model import PickerMode
from imbue.system_interface.harnesses.model import SwitchMode
from imbue.system_interface.harnesses.model import SwitchResult
from imbue.system_interface.harnesses.session_watcher import AgentSessionWatcher
from imbue.system_interface.harnesses.session_watcher import OnEventsCallback


class PlaceholderSessionWatcher(AgentSessionWatcher):
    """A watcher that watches nothing and reports an empty transcript.

    Every read returns the empty answer for its shape, so the chat tab renders as a
    blank conversation rather than erroring. ``on_events`` is retained but never
    called -- there is no source to call it from.
    """

    _on_events: OnEventsCallback

    @classmethod
    def build(cls, agent_info: AgentInfo, on_events: OnEventsCallback) -> "PlaceholderSessionWatcher":
        watcher = cls.__new__(cls)
        watcher._on_events = on_events
        return watcher

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def get_all_events(self, session_id: str | None = None) -> list[dict[str, Any]]:
        return []

    def get_tail_events(self, limit: int, session_id: str | None = None) -> list[dict[str, Any]]:
        return []

    def get_backfill_events(
        self, before_event_id: str, limit: int, session_id: str | None = None
    ) -> list[dict[str, Any]]:
        return []

    def get_forward_events(self, after_event_id: str, limit: int, session_id: str | None = None) -> list[dict[str, Any]]:
        return []

    def get_events_at_offset(self, offset: int, limit: int, session_id: str | None = None) -> list[dict[str, Any]]:
        return []

    def get_event_offset(self, event_id: str, session_id: str | None = None) -> int:
        # -1 is the interface's "not present", which is always true of an empty transcript.
        return -1

    def get_total_event_count(self, session_id: str | None = None) -> int:
        return 0

    def get_subagent_metadata(self, subagent_session_id: str) -> dict[str, str] | None:
        return None

    def is_main_session_event(self, event: dict[str, Any]) -> bool:
        # No events are produced, so this is unreachable; True is the answer that would
        # be right for a single-session harness if it ever were reached.
        return True


class PlaceholderActivityTracker(HarnessActivityTracker):
    """The dot, driven by mngr's ``active`` marker alone.

    Subclasses supply ``marker_filename``. There is no transcript, so the two signals a
    real tracker adds on top of the lifecycle -- an unmatched tool call and the shape of
    the transcript tail -- are both unavailable: a turn in flight reads as THINKING and
    never as TOOL_RUNNING.
    """

    def _derive_working(
        self, *, lifecycle_state: str, is_active_marker_present: bool, process_started_at: float | None
    ) -> ActivityState:
        return ActivityState.THINKING if is_active_marker_present else ActivityState.IDLE


class PlaceholderModelResolver(HarnessModelResolver):
    """A resolver that applies nothing. Pairs with :data:`EMPTY_CATALOG`."""

    @classmethod
    def build(cls, agent_info: AgentInfo) -> "PlaceholderModelResolver":
        return cls()

    def switch(
        self, identity: ModelIdentity, axes: frozenset[ModelAxis], send: Callable[[str], bool]
    ) -> SwitchResult:
        return SwitchResult(ok=False, detail="Model switching is not available for this harness yet.")


# An empty catalog. ``registry.get_catalog`` deliberately does NOT cache an empty result, so
# swapping a harness onto a real ``catalog_factory`` needs no cache invalidation.
# ``switch_mode`` is inert with no options to click; LIST is the honest presentation of an
# empty set (SEARCH would render a search box over nothing).
EMPTY_CATALOG: Final[HarnessCatalog] = HarnessCatalog(
    options=(),
    switch_mode=SwitchMode.EAGER_THEN_RECONCILE,
    picker_mode=PickerMode.LIST,
    powered_by_text="",
)
