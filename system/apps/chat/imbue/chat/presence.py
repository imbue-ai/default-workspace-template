"""Per-chat, per-page presence: which pages have a chat open, whether it is showing, and who is watching it.

The chat page reports its own presence to the chat app (desktop-interface contracts.md
section 7, "Chat presence"): ``hidden`` once the shell has handed it its handshake,
``visible`` on ``shell:shown``, ``hidden`` again on ``shell:hidden``, ``closed`` on
``pagehide``, whether its document has focus on every ``focus`` and ``blur``, and a heartbeat
of all of it every thirty seconds. Each report names the page instance that sent it (minted
once per page load), and one standing report per chat and instance is kept here, so two pages
of one chat in the same client (a pulled-out chat and its hidden copy in the main window)
never overwrite each other. Only the chat's own page reports, never a subagent view.

A page numbers its reports in the order it sends them. Each is its own request, and the
threaded server can record two sent a moment apart in either order, so a report numbered at
or below the instance's standing one is stale and dropped. A ``closed`` report stays as the
instance's standing report, counting as neither open nor visible, so a report it overtook
cannot reopen the page.

Two readers. The OOM prioritizer reads the aggregate: a chat is *open* while any instance has
an unexpired visible or hidden report, and *visible* while any instance's last report says so;
a report expires after ten minutes, so a page that vanished without its ``pagehide`` (a
crashed tab, a lost laptop) stops counting on its own. The notify path reads the *watchers*:
the instances whose last report is visible and focused and younger than ninety seconds, three
heartbeats, so a watcher that went away unannounced stops suppressing notifications quickly.
"""

import threading
import time
from collections.abc import Callable
from enum import auto
from typing import Final

from pydantic import Field
from pydantic import PrivateAttr

from imbue.chat.primitives import ChatId
from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel

# A report that has not been refreshed for this long no longer counts as open or visible.
PRESENCE_EXPIRY_SECONDS: Final[float] = 600.0

# How often the page re-reports its current state.
WATCH_HEARTBEAT_SECONDS: Final[float] = 30.0

# A report older than this no longer makes its page a watcher.
WATCHING_STALE_SECONDS: Final[float] = 90.0


class PresenceState(LowerCaseStrEnum):
    """What one page instance last said about its chat (a wire value of the presence route)."""

    VISIBLE = auto()
    HIDDEN = auto()
    CLOSED = auto()


class PresenceReport(FrozenModel):
    """The body of ``POST /api/chats/<id>/presence``."""

    instance_id: str = Field(min_length=1, description="The reporting page, minted once per page load")
    client_id: str = Field(
        min_length=1, description="The client the page is in, as the shell's handshake named it; informational"
    )
    state: PresenceState = Field(description="The page's state")
    is_focused: bool = Field(description="Whether the page's document has focus")
    sequence: int = Field(ge=1, description="The report's place in the order the page sent its reports")


class PresenceTransition(FrozenModel):
    """What one report changed about its chat, judged under the tracker's lock."""

    is_newly_visible: bool = Field(description="The chat was not visible before the report and is now")
    is_newly_watched: bool = Field(description="The chat had no watcher before the report and has one now")


class _InstancePresence(FrozenModel):
    """One page instance's standing report about its chat."""

    state: PresenceState = Field(description="The page's last state; closed counts as neither open nor visible")
    is_focused: bool = Field(description="Whether the page's document had focus")
    reported_at: float = Field(description="Wall-clock epoch seconds of the report, for expiry and staleness")
    sequence: int = Field(description="The report's sequence number, which a later report must exceed")


class PresenceTracker(MutableModel):
    """Holds every page instance's last presence report per chat and answers the aggregate questions.

    Thread-safe: reports arrive on request threads while the prioritizer's sweep and the watchers
    route read. ``clock`` supplies wall-clock epoch seconds so tests can advance time explicitly.
    """

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    clock: Callable[[], float] = Field(default=time.time, frozen=True, description="Wall-clock epoch seconds")
    expiry_seconds: float = Field(
        default=PRESENCE_EXPIRY_SECONDS, frozen=True, description="How long an unrefreshed report counts"
    )
    watching_stale_seconds: float = Field(
        default=WATCHING_STALE_SECONDS, frozen=True, description="How long a report can make its page a watcher"
    )

    _presence_by_instance_by_chat: dict[ChatId, dict[str, _InstancePresence]] = PrivateAttr(default_factory=dict)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def record(self, chat_id: ChatId, report: PresenceReport) -> PresenceTransition:
        """Replace the instance's standing report about ``chat_id`` unless it is stale, and say what changed."""
        now = self.clock()
        with self._lock:
            by_instance = self._live_reports_locked(chat_id, now)
            standing = by_instance.get(report.instance_id)
            if standing is not None and report.sequence <= standing.sequence:
                return PresenceTransition(is_newly_visible=False, is_newly_watched=False)
            was_visible = _is_any_visible(by_instance)
            was_watched = len(self._watchers_among(by_instance, now)) > 0
            by_instance[report.instance_id] = _InstancePresence(
                state=report.state, is_focused=report.is_focused, reported_at=now, sequence=report.sequence
            )
            self._presence_by_instance_by_chat[chat_id] = by_instance
            return PresenceTransition(
                is_newly_visible=not was_visible and _is_any_visible(by_instance),
                is_newly_watched=not was_watched and len(self._watchers_among(by_instance, now)) > 0,
            )

    def forget_chat(self, chat_id: ChatId) -> None:
        with self._lock:
            self._presence_by_instance_by_chat.pop(chat_id, None)

    def is_open(self, chat_id: ChatId) -> bool:
        """Whether any instance holds an unexpired report about the chat, visible or hidden."""
        with self._lock:
            return any(
                report.state is not PresenceState.CLOSED
                for report in self._live_reports_locked(chat_id, self.clock()).values()
            )

    def is_visible(self, chat_id: ChatId) -> bool:
        """Whether any instance's unexpired last report says the chat is showing."""
        with self._lock:
            return _is_any_visible(self._live_reports_locked(chat_id, self.clock()))

    def watchers(self, chat_id: ChatId) -> list[str]:
        """The instances watching the chat: last report visible and focused, and fresher than the staleness bound."""
        now = self.clock()
        with self._lock:
            return self._watchers_among(self._live_reports_locked(chat_id, now), now)

    def open_chat_ids(self) -> set[ChatId]:
        with self._lock:
            chat_ids = list(self._presence_by_instance_by_chat)
        return {chat_id for chat_id in chat_ids if self.is_open(chat_id)}

    def visible_chat_ids(self) -> set[ChatId]:
        with self._lock:
            chat_ids = list(self._presence_by_instance_by_chat)
        return {chat_id for chat_id in chat_ids if self.is_visible(chat_id)}

    def _live_reports_locked(self, chat_id: ChatId, now: float) -> dict[str, _InstancePresence]:
        """The unexpired reports about ``chat_id``, dropping the expired ones as they are found."""
        by_instance = self._presence_by_instance_by_chat.get(chat_id)
        if by_instance is None:
            return {}
        for instance_id in [
            instance_id
            for instance_id, report in by_instance.items()
            if now - report.reported_at > self.expiry_seconds
        ]:
            del by_instance[instance_id]
        if not by_instance:
            del self._presence_by_instance_by_chat[chat_id]
        return by_instance

    def _watchers_among(self, by_instance: dict[str, _InstancePresence], now: float) -> list[str]:
        return sorted(
            instance_id
            for instance_id, report in by_instance.items()
            if report.state is PresenceState.VISIBLE
            and report.is_focused
            and now - report.reported_at < self.watching_stale_seconds
        )


def _is_any_visible(by_instance: dict[str, _InstancePresence]) -> bool:
    return any(report.state is PresenceState.VISIBLE for report in by_instance.values())
