import threading
from pathlib import Path
from typing import Any

import httpx
from flask import Flask
from flask import current_app
from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.chat.agent_discovery import AgentInfo
from imbue.chat.agent_manager import AgentManager
from imbue.chat.config import Config
from imbue.chat.event_queues import AgentEventQueues
from imbue.chat.harnesses.auth_flows import AuthFlowService
from imbue.chat.harnesses.claude.auth import ClaudeAuthService
from imbue.chat.harnesses.registry import build_watcher
from imbue.chat.harnesses.session_watcher import AgentSessionWatcher
from imbue.chat.ws_broadcaster import WebSocketBroadcaster
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.primitives import AgentId

# Key under which the single ChatAppState is stored on ``app.config`` so handlers can fetch it
# via ``get_state()``.
_STATE_CONFIG_KEY = "CHAT_APP_STATE"


class ChatAppStateError(RuntimeError):
    """Raised when the ChatAppState is not attached to a Flask app."""


# The frontend build's output, inside the package: what the chat routes serve in production.
DEFAULT_STATIC_DIRECTORY = Path(__file__).parent / "static"


class ChatAppState(MutableModel):
    """Holds every shared service handle and config for one chat app.

    Built once in ``main.build_production_state`` (or by a test) and stored on the Flask
    app; handlers read it via ``get_state()``. Owns the per-agent session-watcher registry
    and the latchkey catalog cache (both guarded for concurrent access under the threaded
    WSGI server).
    """

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    config: Config
    provider_names: tuple[str, ...] | None
    include_filters: tuple[str, ...]
    exclude_filters: tuple[str, ...]
    agent_manager: AgentManager
    event_queues: AgentEventQueues
    claude_auth_service: ClaudeAuthService
    auth_flows: AuthFlowService
    http_client: httpx.Client
    latchkey_http_client: httpx.Client
    watchers: dict[str, AgentSessionWatcher] = {}
    latchkey_catalog_cache: dict[str, Any] = {}
    static_directory: Path = Field(
        default=DEFAULT_STATIC_DIRECTORY,
        description="The bundle directory the chat routes serve from: the package's own static/ unless the "
        "state is built with another (a test serving a document it wrote)",
    )

    _watchers_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _latchkey_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _is_shut_down: bool = PrivateAttr(default=False)

    @property
    def broadcaster(self) -> WebSocketBroadcaster:
        """The one WebSocket broadcaster of the process, as the agent manager holds it."""
        return self.agent_manager.broadcaster

    @property
    def latchkey_lock(self) -> threading.Lock:
        """Serializes concurrent latchkey catalog fetches across request threads."""
        return self._latchkey_lock

    def get_or_create_watcher(self, agent_info: AgentInfo) -> AgentSessionWatcher:
        """Get the existing session watcher for an agent, or create and start one.

        The watcher comes from the harness registry, keyed on the harness resolved once
        at discovery, so neither this method nor the server knows which one it got.

        Guarded by a lock so two concurrent request threads cannot both build a
        watcher for the same agent under the threaded server.
        """
        with self._watchers_lock:
            existing = self.watchers.get(agent_info.id)
            if existing is not None:
                return existing

            def on_events(agent_id: str, events: list[dict[str, Any]]) -> None:
                # Deliver-live-only: session events are persisted in JSONL and recoverable
                # via the REST /events endpoint, so nothing is buffered for replay.
                self.event_queues.broadcast_batch(agent_id, events)
                # Fold the delta into the per-agent activity signals. The tracker is
                # incremental (seeded with the full backlog below), so it only ever
                # needs the newly parsed events.
                self.agent_manager.update_session_events(agent_id, events)

            # The harness was resolved once at discovery; the registry turns it into a
            # watcher, so nothing here knows which harness is running.
            watcher = build_watcher(agent_info, on_events)
            # Bridge the watcher's live queued-message snapshot onto the agents WS
            # state, and register its working->IDLE queue backstop with the manager.
            # Both are no-ops for a harness without a queue populator. The manager
            # de-dupes/broadcasts, so pushing the full snapshot on each change is
            # cheap.
            watcher.set_queue_snapshot_callback(
                lambda snapshot: self.agent_manager.update_queued_messages(agent_info.id, snapshot)
            )
            self.agent_manager.register_queue_idle_handler(agent_info.id, watcher.notify_idle)
            # A harness that holds the queue on its agent's behalf (antigravity) also needs to
            # DELIVER it, which needs the manager's send path and a liveness check. No-op for
            # every other harness, whose queue its own harness consumes.
            watcher.set_flush_hooks(
                # `is None` is the delivery test: send_message_to_agent returns the FAILURE
                # (or None on success), while FlushSendCallback is declared to return True for
                # delivered. Passing the result straight through inverts it, and antigravity's
                # flush would count every failed send as delivered and drop the queue.
                lambda text: self.agent_manager.send_message_to_agent(AgentId(agent_info.id), text) is None,
                lambda: self.agent_manager.is_agent_alive(agent_info.id),
            )
            self.watchers[agent_info.id] = watcher

        # Seed transcript-derived activity signals BEFORE starting the watcher
        # thread (seeding needs no running thread -- ``get_all_events`` reads
        # synchronously). The watcher's priming pass may broadcast a queued
        # snapshot as soon as the thread runs, and the manager's pre-broadcast
        # sweep derives activity from these signals -- an unseeded tracker would
        # derive IDLE for a live mid-turn agent and sweep its genuine queue.
        # Seeding also keeps the indicator from lagging a turn behind on first
        # connect. Done outside the watchers lock to avoid holding it across the
        # agent manager's own lock.
        self.agent_manager.update_session_events(agent_info.id, watcher.get_all_events())
        watcher.start()
        return watcher

    def stop_and_remove_watcher(self, agent_id: str) -> None:
        """Evict one agent's watcher, releasing its resident transcript, thread, and
        filesystem watches.

        The memory half of the chat lifecycle: called when an agent is destroyed or its
        lifecycle transitions to positively dead (stopped from the UI, `mngr stop`, an OOM
        shed, idle shutdown), so a chat that is not running holds no chat-backend memory.
        Cheap no-op when no watcher exists. Rebuild-on-demand is `get_or_create_watcher`:
        viewing a stopped chat re-reads its transcript from disk transparently.

        The watcher is popped under the lock but stopped outside it -- `stop` joins the
        watch thread, and holding the lock across that join would stall every other
        watcher creation for the duration.
        """
        with self._watchers_lock:
            watcher = self.watchers.pop(agent_id, None)
        if watcher is not None:
            logger.debug("Evicting the session watcher for agent {}", agent_id)
            watcher.stop()

    def stop_all_watchers(self) -> None:
        with self._watchers_lock:
            for watcher in self.watchers.values():
                watcher.stop()
            self.watchers.clear()

    def shutdown(self) -> None:
        """Tear down every owned resource. Idempotent."""
        if self._is_shut_down:
            return
        self._is_shut_down = True
        self.event_queues.shutdown()
        self.broadcaster.shutdown()
        self.agent_manager.stop()
        self.stop_all_watchers()
        try:
            self.http_client.close()
        except (httpx.HTTPError, RuntimeError) as e:
            logger.debug("Skipped closing service http client during shutdown: {}", e)
        try:
            self.latchkey_http_client.close()
        except (httpx.HTTPError, RuntimeError) as e:
            logger.debug("Skipped closing latchkey http client during shutdown: {}", e)


def attach_state(app: Flask, state: ChatAppState) -> None:
    app.config[_STATE_CONFIG_KEY] = state


def get_state() -> ChatAppState:
    """Return the ChatAppState for the current Flask app."""
    state = current_app.config.get(_STATE_CONFIG_KEY)
    if not isinstance(state, ChatAppState):
        raise ChatAppStateError("ChatAppState is not attached to the current app")
    return state


def state_of(app: Flask) -> ChatAppState:
    """Return the ChatAppState attached to ``app`` without needing an app context."""
    state = app.config.get(_STATE_CONFIG_KEY)
    if not isinstance(state, ChatAppState):
        raise ChatAppStateError("ChatAppState is not attached to the app")
    return state
