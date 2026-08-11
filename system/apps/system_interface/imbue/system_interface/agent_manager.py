import json
import os
import queue
import shlex
import threading
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger as _loguru_logger
from oom_priority.bands import set_oom_score_adj
from oom_priority.registry import lookup_pid_by_agent_id
from pydantic import Field
from watchdog.events import FileMovedEvent
from watchdog.events import FileSystemEvent
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer as _Observer

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.concurrency_group import InvalidConcurrencyGroupStateError
from imbue.concurrency_group.errors import ConcurrencyGroupError
from imbue.concurrency_group.errors import EnvironmentStoppedError
from imbue.concurrency_group.errors import ProcessError
from imbue.concurrency_group.event_utils import ShutdownEvent
from imbue.concurrency_group.local_process import RunningProcess
from imbue.concurrency_group.subprocess_utils import run_local_command_modern_version
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.api.find import AgentMatch
from imbue.mngr.api.observe import AgentRemovedEvent
from imbue.mngr.api.observe import AgentStateEvent
from imbue.mngr.api.observe import FullAgentStateEvent
from imbue.mngr.api.observe import parse_observe_event_line
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentNameStyle
from imbue.mngr.primitives import HostName
from imbue.mngr.utils.name_generator import generate_agent_name
from imbue.system_interface.activity_state import ACTIVE_MARKER_FILENAME
from imbue.system_interface.activity_state import ActivityState
from imbue.system_interface.activity_state import is_lifecycle_dead
from imbue.system_interface.activity_state import resolve_is_agent_running
from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.agent_discovery import MngrMessenger
from imbue.system_interface.agent_discovery import discover_agents
from imbue.system_interface.agent_discovery import get_host_dir
from imbue.system_interface.agent_discovery import read_claude_config_dir_from_env_file
from imbue.system_interface.harnesses.activity import HarnessActivityTracker
from imbue.system_interface.harnesses.auth_check import find_unauthenticated_harness_reason
from imbue.system_interface.harnesses.claude.launch_defaults import FAST_MODE_BEFORE_DECISION
from imbue.system_interface.harnesses.claude.launch_defaults import get_workspace_fast_mode_decision_path
from imbue.system_interface.harnesses.claude.launch_defaults import read_workspace_fast_mode_decision
from imbue.system_interface.harnesses.codex.ledger import CodexMessageLedger
from imbue.system_interface.harnesses.codex.live_connection import CodexLiveConnection
from imbue.system_interface.harnesses.harness_type import DEFAULT_HARNESS
from imbue.system_interface.harnesses.harness_type import HarnessType
from imbue.system_interface.harnesses.harness_type import parse_harness
from imbue.system_interface.harnesses.model import ModelChoice
from imbue.system_interface.harnesses.model import match_option
from imbue.system_interface.harnesses.model import read_model_identity
from imbue.system_interface.harnesses.path_watch import PathWatcher
from imbue.system_interface.harnesses.registry import build_tracker
from imbue.system_interface.harnesses.registry import get_catalog
from imbue.system_interface.harnesses.registry import get_model_state_path
from imbue.system_interface.models import AgentCreationError
from imbue.system_interface.models import AgentStateItem
from imbue.system_interface.models import AppEntry
from imbue.system_interface.models import QueuedMessageState
from imbue.system_interface.oom_prioritizer import ChatOomPrioritizer
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster

# The role template every UI-created agent gets. The harness is chosen separately via
# `--type` (see `_build_chat_create_command`); only the role varies in the template list,
# and it travels as `harness`, not folded into the role name.
CHAT_ROLE_TEMPLATE: Final[str] = "chat"

# What the UI is told it just created. Always the role: both menu entries make a chat,
# on different harnesses.
CHAT_CREATION_TYPE: Final[str] = CHAT_ROLE_TEMPLATE

_APPS_TOML_FILENAME = "data/.state/apps.toml"
_APPS_TOML_BASENAME = "apps.toml"
_DEFAULT_MNGR_BINARY = "mngr"
# The production messenger: a stateless, frozen value whose discover/send are the
# real mngr calls, so one shared instance is the default for every built manager.
_DEFAULT_MESSENGER: Final[MngrMessenger] = MngrMessenger()


_COMPLETION_SIGNAL_PUT_TIMEOUT_SECONDS = 5.0

# A chat spawned by the minds "get help -> have an agent help" flow carries this
# label (set on its ``mngr create``). When such an agent is first discovered, we
# auto-open its tab so the user lands on it without hunting.
_ASSIST_AUTO_OPEN_LABEL = "assist"


def _build_chat_create_command(
    mngr_binary: str,
    name: str,
    agent_id: str,
    primary_labels: dict[str, str],
    harness: HarnessType,
    is_fast_mode_enabled: bool,
    extra_role_templates: tuple[str, ...] = (),
) -> list[str]:
    """Build the ``mngr create`` argv for a chat agent on a given harness.

    The harness is selected with ``--type <harness>`` (which resolves
    ``[agent_types.<harness>]`` directly), and the `chat` role template supplies
    everything else -- the shared work directory and the output style. Every harness
    shares this one builder: adding a harness means passing a different name here, not
    writing another near-identical builder, and not adding a per-harness create template.

    Pure: argv assembly only, so the repo<->mngr CLI contract is testable against the
    live CLI without constructing an ``AgentManager`` or running a subprocess (see
    ``agent_manager_test.py``).
    """
    cmd = [
        mngr_binary,
        "create",
        name,
        "--id",
        agent_id,
        "--type",
        harness,
        "--template",
        "chat",
        *[arg for role in extra_role_templates for arg in ("--template", role)],
        # Tags this as a user-created agent so the OOM launch wrapper puts it in the
        # dynamic chat band (re-tagged from live UI engagement), not the worker band.
        "--label",
        "user_created=true",
        "--no-connect",
    ]
    # Chat is the one interactive agent type, so it is the only one that starts fast;
    # .mngr/settings.toml defaults every other type to standard speed. Claude-specific:
    # fast mode is a claude setting, so it is meaningless under any other harness.
    if harness == HarnessType.CLAUDE:
        cmd.extend(["-S", f"agent_types.claude.settings_overrides.fastMode={str(is_fast_mode_enabled).lower()}"])
    # Inherit the project label from the primary agent. The chat agent belongs to
    # its workspace by sharing the host; it carries no workspace label.
    if "project" in primary_labels:
        cmd.extend(["--label", f"project={primary_labels['project']}"])
    return cmd


def _build_observe_command_argv(mngr_binary: str) -> list[str]:
    """Build the ``mngr observe --stream-events`` argv. Pure (see above).

    ``--stream-events`` runs the full observer and additionally echoes each
    agents-stream event (AGENT_STATE / AGENTS_FULL_STATE / AGENT_REMOVED) as
    JSONL to stdout, which we consume directly. Unlike the old ``--discovery-only``
    stream, these events carry real probed lifecycle state (including event-driven
    detection of an agent process dying on its own), which is what drives each
    agent's real ``state`` below.
    """
    return [
        mngr_binary,
        "observe",
        "--stream-events",
    ]


# AgentMatch requires a host_name, but the send path never reads it -- it groups
# and resolves hosts by host_id + provider_name (see mngr's group_agents_by_host /
# send_message_to_agents). So we don't track real host names: the cached match
# carries this placeholder, which only ever flows back into send_message_to_agents.
_UNUSED_HOST_NAME: Final[HostName] = HostName("unknown")


def _build_agent_match(agent: AgentDetails) -> AgentMatch:
    """Assemble the messaging-location AgentMatch for an observed agent.

    Addressed by agent_id + host_id + provider_name (sourced from the agent's
    nested host details); host_name is a placeholder (see `_UNUSED_HOST_NAME`).
    """
    return AgentMatch(
        agent_id=agent.id,
        agent_name=agent.name,
        host_id=agent.host.id,
        host_name=_UNUSED_HOST_NAME,
        provider_name=agent.host.provider_name,
    )


def _safe_log_put(log_queue: queue.Queue[str | None], message: str | None) -> None:
    """Non-blocking put for a creation-log queue.

    The creation thread must never block on individual log lines. If the
    WebSocket client streaming proto-agent logs disconnects mid-creation,
    nothing is draining the queue, and a blocking ``put`` would hang the
    thread at the next log line -- which in turn prevents
    ``proto_agent_completed`` from ever firing. We drop log lines on a
    full queue; callers that need delivery guarantees for sentinels
    (``done: True`` + the ``None`` terminator) should use
    :func:`_completion_signal_put` instead.
    """
    try:
        log_queue.put_nowait(message)
    except queue.Full:
        _loguru_logger.trace("Creation log queue full; dropping line")


def _completion_signal_put(log_queue: queue.Queue[str | None], message: str | None) -> None:
    """Blocking put (with timeout) for completion sentinels.

    Unlike per-line log writes, the completion sentinel + None terminator
    must reach the consumer -- otherwise ``_proto_agent_logs_endpoint``
    loops forever on ``queue.get()`` and the log WebSocket never closes.
    We therefore block briefly (bounded by
    ``_COMPLETION_SIGNAL_PUT_TIMEOUT_SECONDS``) to give a slow consumer
    time to drain. If the queue is still full at the deadline, log at
    warning level and drop -- the out-of-band
    ``broadcast_proto_agent_completed`` WS broadcast is the authoritative
    signal to the main UI, so the log-channel sentinel being dropped
    only degrades the dedicated log view, not overall correctness.
    """
    try:
        log_queue.put(message, block=True, timeout=_COMPLETION_SIGNAL_PUT_TIMEOUT_SECONDS)
    except queue.Full:
        _loguru_logger.warning(
            "Creation log queue full; dropping completion sentinel. "
            "The log WebSocket consumer may hang until the queue is garbage-collected."
        )


class _LogQueueCallback(MutableModel):
    """Callable that appends process output lines as JSON to a queue."""

    model_config = {"arbitrary_types_allowed": True}

    log_queue: queue.Queue[str | None] = Field(description="Queue to write log lines into")

    def __call__(self, line: str, _is_stdout: bool) -> None:
        _safe_log_put(self.log_queue, json.dumps({"line": line.rstrip("\n")}))


class _AppsFileHandler(FileSystemEventHandler):
    """Watchdog handler that triggers on mutating changes to apps.toml.

    Subscribes to mutation events (modified/created/deleted/moved/closed)
    rather than ``on_any_event`` because watchdog's default inotify mask also
    includes ``IN_OPEN`` / ``IN_CLOSE_NOWRITE``. Reacting to those would form
    a feedback loop -- the handler reads the file, the read triggers fresh
    open/close-no-write events, and one CPU core is pinned per agent watcher.

    ``on_modified`` alone is insufficient because system/scripts/forward_port.py
    upserts atomically via ``tempfile.mkstemp`` + ``os.replace``, which
    surfaces as a moved/created event, not a modified event. ``on_closed``
    (``IN_CLOSE_WRITE``) is included so that direct writers which don't go
    through an atomic rename still trigger a re-read on close.

    Events are filtered to only those whose src or dest path basename is
    ``apps.toml``. Without this filter we'd also fire on every write
    to forward_port.py's ``apps.toml.*.tmp`` scratch files, which is
    correctness-neutral (the re-read is idempotent) but produces a broadcast
    storm per upsert.
    """

    agent_id: str
    on_change: Any

    def _maybe_fire(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        paths = [event.src_path]
        if isinstance(event, FileMovedEvent):
            paths.append(event.dest_path)
        if any(os.path.basename(p) == _APPS_TOML_BASENAME for p in paths):
            self.on_change(self.agent_id)

    on_modified = _maybe_fire
    on_created = _maybe_fire
    on_deleted = _maybe_fire
    on_moved = _maybe_fire
    on_closed = _maybe_fire


def _make_apps_file_handler(
    agent_id: str,
    on_change: Any,
) -> _AppsFileHandler:
    """Create an apps-registry file handler for the given agent."""
    handler = _AppsFileHandler()
    handler.agent_id = agent_id
    handler.on_change = on_change
    return handler


class AgentManager:
    """Manages agent lifecycle detection, app-registry watching, and agent creation.

    Runs mngr observe as a subprocess for event-driven agent lifecycle detection.
    Watches data/.state/apps.toml for each agent.
    Handles agent creation via local mngr create calls.
    """

    _broadcaster: WebSocketBroadcaster
    _messenger: MngrMessenger
    _lock: threading.Lock
    # The live view of observed agents keyed by id, folded from the observe
    # stream: an AGENTS_FULL_STATE snapshot rebuilds it wholesale, an AGENT_STATE
    # upserts one agent, and an AGENT_REMOVED drops one. ``_agents`` /
    # ``_match_by_agent_id`` are rebuilt from it after each event, and its
    # before/after key diff drives the per-agent membership side-effects.
    _agent_details_by_id: dict[str, AgentDetails]
    _agents: dict[str, AgentStateItem]
    # agent id -> its discovered location (host/provider), maintained from the
    # observe snapshot/discovered/destroy events so messaging can resolve an
    # agent's location without a fresh find_all_agents discovery. Best-effort:
    # paths that mutate _agents without a discovery event (creation/refresh) skip
    # it, and a miss in get_agent_matches_by_id just falls back to discovery.
    _match_by_agent_id: dict[str, AgentMatch]
    _apps: list[AppEntry]
    _app_observers: dict[str, Any]
    _proto_agents: dict[str, dict[str, Any]]
    _log_queues: dict[str, queue.Queue[str | None]]
    _own_agent_id: str
    _own_work_dir: str
    _shutdown_event: ShutdownEvent
    _observe_cg: ConcurrencyGroup | None
    _observe_process: RunningProcess | None
    _creation_cg: ConcurrencyGroup
    _mngr_binary: str
    _host_dir: Path
    _activity_tracked_agents: set[str]
    # Per-agent activity tracker, built from the agent's harness when tracking
    # starts. Owns that harness's cached transcript signals and its derivation;
    # see :mod:`harness_activity`.
    _activity_tracker_by_agent: dict[str, HarnessActivityTracker]
    _activity_state_by_agent: dict[str, ActivityState]
    # Per-agent live queued-message snapshot (a sibling of ``_activity_state_by_agent``),
    # pushed to the frontend on the agents WebSocket. Fed by the agent's watcher via
    # ``update_queued_messages`` and cleared on a working->IDLE transition through the
    # per-agent idle handler the watcher registers (its ``notify_idle`` -- the queue
    # backstop). Both are dropped when activity tracking stops.
    _queued_messages_by_agent: dict[str, tuple[QueuedMessageState, ...]]
    _queue_idle_handler_by_agent: dict[str, Callable[[], list[dict[str, Any]]]]
    # Per-codex-agent live app-server connection: the persistent client + ledger + background
    # reader thread that make the ledger the backend authority for that agent's five message
    # states (send/queue/deliver/return), its activity dot, and its model-bar mirror. Built lazily
    # once the agent's daemon socket is reachable (``_ensure_codex_connection``) and torn down when
    # activity tracking stops. A codex agent drives activity + queue through its ledger, NOT the
    # transcript-derived tracker, so it has no ``_activity_tracker_by_agent`` entry.
    _codex_connection_by_agent: dict[str, CodexLiveConnection]
    # The last computed model choice per agent, and the filesystem watcher that
    # re-derives it when the agent's minds_model_state.json changes. The live read is
    # harness-neutral (the shared reader + the harness's registered state-file path), so
    # there is no per-agent resolver to cache -- the switch endpoint builds one inline.
    # None = the harness has recorded no model yet -> the bar renders logo-only.
    _model_choice_by_agent: dict[str, ModelChoice | None]
    _model_watcher_by_agent: dict[str, PathWatcher]
    # Assist chats whose tab we have already auto-opened (or that existed at
    # startup, seeded by ``_initial_discover`` so we never auto-open them). Lets
    # both discovery paths -- the per-agent delta and the full snapshot -- open
    # each new assist chat exactly once without reopening it on later snapshots.
    _auto_opened_assist_ids: set[str]
    # Re-tags chat agents' OOM ``oom_score_adj`` from live UI activity (open /
    # visible / recently-messaged). Driven purely by the ``/api/activity`` endpoint
    # (via ``record_activity``): a chat launches at the expendable band and is
    # protected only as engagement is reported, so no periodic re-tag is needed.
    # The messaged-revive path is race-free without one -- the send blocks until the
    # revived process is ready (and its pid is registered before that), and the
    # frontend posts activity only after the send returns.
    _oom_prioritizer: ChatOomPrioritizer

    @classmethod
    def build(
        cls,
        broadcaster: WebSocketBroadcaster,
        messenger: MngrMessenger = _DEFAULT_MESSENGER,
        mngr_binary: str = _DEFAULT_MNGR_BINARY,
    ) -> "AgentManager":
        """Build an AgentManager with the given broadcaster.

        ``messenger`` is the agent-messaging collaborator; it defaults to the
        real mngr discover/send. Tests pass one whose ``discover``/``send`` are
        fakes to avoid touching mngr. ``mngr_binary`` is the path or name of the
        mngr executable used for the stream-events observe subprocess and for
        agent-creation commands.
        """
        manager = cls.__new__(cls)
        manager._broadcaster = broadcaster
        manager._messenger = messenger
        manager._lock = threading.Lock()
        manager._agent_details_by_id = {}
        manager._agents = {}
        manager._match_by_agent_id = {}
        manager._apps = []
        manager._app_observers = {}
        manager._proto_agents = {}
        manager._log_queues = {}
        manager._own_agent_id = os.environ.get("MNGR_AGENT_ID", "")
        manager._own_work_dir = os.environ.get("MNGR_AGENT_WORK_DIR", "")
        manager._shutdown_event = ShutdownEvent.build_root()
        manager._observe_cg = None
        manager._observe_process = None
        manager._creation_cg = ConcurrencyGroup(name="agent-creation")
        manager._creation_cg.__enter__()
        manager._mngr_binary = mngr_binary
        manager._host_dir = get_host_dir()
        manager._activity_tracked_agents = set()
        manager._activity_tracker_by_agent = {}
        manager._activity_state_by_agent = {}
        manager._queued_messages_by_agent = {}
        manager._queue_idle_handler_by_agent = {}
        manager._codex_connection_by_agent = {}
        manager._model_choice_by_agent = {}
        manager._model_watcher_by_agent = {}
        manager._auto_opened_assist_ids = set()
        # Built last: its ``list_chat_agent_ids`` callback reads ``_agents`` /
        # ``_lock``, which are set above.
        manager._oom_prioritizer = ChatOomPrioritizer(
            list_chat_agent_ids=manager.get_chat_agent_ids,
            resolve_pid=lookup_pid_by_agent_id,
            set_adj=set_oom_score_adj,
        )
        return manager

    def start(self) -> None:
        """Start the observe subprocess and perform initial agent discovery."""
        self._initial_discover()
        self._start_observe()

    def start_without_observe(self) -> None:
        """Start with initial discovery only, no observe subprocess. For testing."""
        self._initial_discover()

    def stop(self) -> None:
        """Stop the observe subprocess, file watchers, and creation threads."""
        self._shutdown_event.set()

        if self._observe_cg is not None:
            self._observe_cg.shutdown()
            self._observe_cg.__exit__(None, None, None)
            self._observe_cg = None

        self._creation_cg.__exit__(None, None, None)

        for observer in self._app_observers.values():
            observer.stop()
        for observer in self._app_observers.values():
            observer.join(timeout=5)
        self._app_observers.clear()

        with self._lock:
            model_watchers = list(self._model_watcher_by_agent.values())
            self._model_watcher_by_agent.clear()
        for watcher in model_watchers:
            watcher.stop()

        with self._lock:
            codex_connections = list(self._codex_connection_by_agent.values())
            self._codex_connection_by_agent.clear()
            self._activity_tracked_agents.clear()
            self._activity_tracker_by_agent.clear()
            self._activity_state_by_agent.clear()
            self._queued_messages_by_agent.clear()
            self._queue_idle_handler_by_agent.clear()
            self._model_choice_by_agent.clear()
        for connection in codex_connections:
            connection.stop()

    @property
    def broadcaster(self) -> WebSocketBroadcaster:
        """The WebSocketBroadcaster this manager owns. Primarily useful to
        callers that need to reuse the same broadcaster across related
        application state (e.g. the system_interface lifespan when an
        externally-constructed AgentManager is injected for tests)."""
        return self._broadcaster

    def get_agents(self) -> list[AgentStateItem]:
        """Return current agent list."""
        with self._lock:
            return list(self._agents.values())

    def get_agent_by_id(self, agent_id: str) -> AgentStateItem | None:
        """Look up a single agent by ID."""
        with self._lock:
            return self._agents.get(agent_id)

    def get_chat_agent_ids(self) -> list[str]:
        """Ids of the agents the OOM prioritizer manages: chat agents only.

        Excludes workers (``agent_created=true``) and the primary services agent
        (``is_primary=true``); those keep their launch bands -- workers maximally
        expendable, the primary pinned -- so no UI activity moves their score.
        Remote agents are left in (they have no local pid, so the prioritizer's
        pid lookup skips them harmlessly).
        """
        with self._lock:
            return [
                agent.id
                for agent in self._agents.values()
                if agent.labels.get("agent_created") != "true" and agent.labels.get("is_primary") != "true"
            ]

    def record_activity(
        self,
        *,
        open_ids: list[str],
        visible_ids: list[str],
        messaged_id: str | None = None,
    ) -> None:
        """Feed a frontend activity report to the OOM prioritizer (re-tags chats)."""
        self._oom_prioritizer.record_activity(
            open_ids=open_ids,
            visible_ids=visible_ids,
            messaged_id=messaged_id,
        )

    def get_agent_info_by_id(self, agent_id: str) -> AgentInfo | None:
        """Resolve an agent id to its web-UI :class:`AgentInfo` (with resolved dirs), or None."""
        agent_state = self.get_agent_by_id(agent_id)
        if agent_state is None:
            return None
        agent_state_dir = self._get_agent_state_dir(agent_state.id)
        return AgentInfo(
            id=agent_state.id,
            name=agent_state.name,
            state=agent_state.state,
            agent_state_dir=agent_state_dir,
            claude_config_dir=read_claude_config_dir_from_env_file(agent_state_dir),
            labels=agent_state.labels,
            work_dir=agent_state.work_dir,
            harness=agent_state.harness,
        )

    def get_agent_matches_by_id(self, agent_id: str) -> list[AgentMatch]:
        """Return the discovered location of the agent with this id (0- or 1-element).

        Sourced from the live observe stream, so a caller can message the agent
        without running a fresh discovery. Empty when the id is not (yet) in the
        latest snapshot -- the caller falls back to discovery in that case.
        """
        with self._lock:
            match = self._match_by_agent_id.get(agent_id)
            return [match] if match is not None else []

    def send_message_to_agent(self, agent_id: AgentId, message: str) -> bool:
        """Send a message to the agent with ``agent_id``, using the live location cache.

        The single entry point for messaging an agent: it reads this manager's
        event-fed location for the id and hands it to the `MngrMessenger`, so the
        message skips a fresh mngr discovery whenever the location is already known.
        Returns True on success.
        """
        return self._messenger.send_to_agent(agent_id, message, self.get_agent_matches_by_id(str(agent_id)))

    def press_key_chord_on_agent(self, agent_id: AgentId, key: str) -> bool:
        """Press a tmux key token (e.g. ``"M-q"``) into the agent's pane, using the live cache.

        The key-chord peer of ``send_message_to_agent``: it reads this manager's event-fed
        location for the id and hands it to the ``MngrMessenger``, which delivers the press
        through mngr's in-process message API (holding the per-agent ``message.lock``, so the
        chord never interleaves with a text send). Returns True on success.
        """
        return self._messenger.press_key_chord_to_agent(agent_id, key, self.get_agent_matches_by_id(str(agent_id)))

    def remove_agent(self, agent_id: str) -> None:
        """Remove an agent from the tracked state and broadcast the update.

        Called after a successful mngr destroy to immediately reflect
        the destruction without waiting for the observe subprocess.
        """
        with self._lock:
            self._agents.pop(agent_id, None)
            self._match_by_agent_id.pop(agent_id, None)

        self._stop_app_watcher(agent_id)
        self._stop_activity_tracking(agent_id)
        self._stop_model_tracking(agent_id)
        self._broadcaster.broadcast_agents_updated(self.get_agents_serialized())

    def get_apps(self) -> list[AppEntry]:
        """Return the primary agent's app list."""
        with self._lock:
            return list(self._apps)

    def get_apps_serialized(self) -> list[dict[str, str]]:
        """Return the primary agent's app list serialized for JSON."""
        with self._lock:
            return [{"name": app.name, "url": app.url, "label": app.label} for app in self._apps]

    def get_service_url(self, service_name: str) -> str | None:
        """Return the local backend URL for a service, or None if it isn't registered."""
        with self._lock:
            for app in self._apps:
                if app.name == service_name:
                    return app.url
            return None

    def list_service_names(self) -> tuple[str, ...]:
        """Return the names of all currently registered services, sorted alphabetically."""
        with self._lock:
            return tuple(sorted(app.name for app in self._apps))

    def get_agents_serialized(self) -> list[dict[str, Any]]:
        """Return agent list serialized for JSON."""
        with self._lock:
            return [
                {
                    "id": a.id,
                    "name": a.name,
                    "state": a.state,
                    "labels": a.labels,
                    "work_dir": a.work_dir,
                    "harness": a.harness,
                    "activity_state": a.activity_state,
                    "model_choice": a.model_choice.model_dump() if a.model_choice else None,
                    "queued_messages": [queued.model_dump() for queued in a.queued_messages],
                }
                for a in self._agents.values()
            ]

    def get_proto_agents(self) -> list[dict[str, Any]]:
        """Return list of proto-agents (agents being created)."""
        with self._lock:
            return list(self._proto_agents.values())

    def get_log_queue(self, agent_id: str) -> queue.Queue[str | None] | None:
        """Get the log queue for a proto-agent creation process."""
        with self._lock:
            return self._log_queues.get(agent_id)

    def get_own_agent_id(self) -> str:
        """Return this server's own agent ID from the environment."""
        return self._own_agent_id

    def generate_random_name(self) -> str:
        """Generate a random agent name using mngr's name generator."""
        return str(generate_agent_name(AgentNameStyle.COOLNAME))

    def create_chat_agent(
        self,
        name: str,
        harness: HarnessType = HarnessType.CLAUDE,
        extra_role_templates: tuple[str, ...] = (),
    ) -> str:
        """Create a chat agent in the primary agent's work dir on the given harness.

        Returns the pre-generated agent ID. ``harness`` is also the name of the harness
        create template it stacks; the `chat` role template supplies everything else, so a
        new harness needs no new method here.

        An alt harness authenticates through its own CLI; if that CLI is signed out, refuse
        the create up front (raising ``AgentCreationError``) rather than launch a chat that
        can never take a turn. Claude is not gated -- its auth is the shared workspace login.
        """
        unauthenticated_reason = find_unauthenticated_harness_reason(harness)
        if unauthenticated_reason is not None:
            raise AgentCreationError(unauthenticated_reason)

        agent_id = str(AgentId())

        with self._lock:
            work_dir = self._resolve_agent_work_dir(self._own_agent_id)
            primary = self._agents.get(self._own_agent_id)
            primary_labels = dict(primary.labels) if primary else {}

        if work_dir is None:
            msg = f"Cannot determine work directory for primary agent {self._own_agent_id}"
            raise AgentCreationError(msg)

        # New chats launch at the workspace's fast-mode setting: fast until the
        # user answers the prompt, then whatever they chose. Only claude reads it.
        decision = read_workspace_fast_mode_decision(get_workspace_fast_mode_decision_path(Path(work_dir)))
        is_fast_mode_enabled = FAST_MODE_BEFORE_DECISION if decision is None else decision
        cmd = _build_chat_create_command(
            self._mngr_binary,
            name,
            agent_id,
            primary_labels,
            harness,
            is_fast_mode_enabled,
            extra_role_templates,
        )

        log_queue: queue.Queue[str | None] = queue.Queue(maxsize=10000)

        proto_info = {
            "agent_id": agent_id,
            "name": name,
            "creation_type": CHAT_CREATION_TYPE,
            "parent_agent_id": None,
        }
        with self._lock:
            self._proto_agents[agent_id] = proto_info
            self._log_queues[agent_id] = log_queue

        self._broadcaster.broadcast_proto_agent_created(
            agent_id=agent_id,
            name=name,
            creation_type=CHAT_CREATION_TYPE,
            parent_agent_id=None,
        )

        labels: dict[str, str] = {}
        if "project" in primary_labels:
            labels["project"] = primary_labels["project"]
        self._launch_creation_thread(agent_id, name, cmd, Path(work_dir), log_queue, labels, harness)

        return agent_id

    def _launch_creation_thread(
        self,
        agent_id: str,
        agent_name: str,
        cmd: list[str],
        work_dir: Path,
        log_queue: queue.Queue[str | None],
        labels: dict[str, str],
        harness: HarnessType,
    ) -> None:
        """Start a background thread to run agent creation and stream logs."""
        self._creation_cg.start_new_thread(
            target=self._run_creation,
            args=(agent_id, agent_name, cmd, work_dir, log_queue, labels, harness),
            name=f"create-{agent_id[:8]}",
            is_checked=False,
        )

    def _resolve_agent_work_dir(self, agent_id: str) -> str | None:
        """Resolve an agent's work directory. Must be called with lock held."""
        agent = self._agents.get(agent_id)
        if agent is not None and agent.work_dir is not None:
            return agent.work_dir
        if agent_id == self._own_agent_id and self._own_work_dir:
            return self._own_work_dir
        return None

    def _run_creation(
        self,
        agent_id: str,
        agent_name: str,
        cmd: list[str],
        work_dir: Path,
        log_queue: queue.Queue[str | None],
        labels: dict[str, str],
        harness: HarnessType,
    ) -> None:
        """Run mngr create in the background, capture output, and always emit completion.

        This thread is started with ``is_checked=False``, so any exception
        that escaped here was silently swallowed -- which left the client's
        ChatPanel stuck on "Creating agent..." forever, because neither the
        log stream's ``{done: true}`` sentinel nor the WS
        ``proto_agent_completed`` broadcast fired.

        The whole body runs inside a single catch-all so that *no matter
        what* the subprocess, its callbacks, or the pydantic / broadcaster
        calls below throw, the proto-agent entry is always cleared on the
        client and any error is surfaced as a string to the UI. The
        catch-all is intentional belt-and-suspenders: see
        ``test_prevent_broad_exception_catch``'s snapshot bump.
        """
        success = False
        error: str | None = None

        try:
            cmd_str = shlex.join(cmd)
            header_line = f"[cwd: {work_dir}] {cmd_str}"
            _safe_log_put(log_queue, json.dumps({"line": header_line}))

            try:
                result = run_local_command_modern_version(
                    command=cmd,
                    cwd=work_dir,
                    is_checked=False,
                    trace_output=True,
                    trace_on_line_callback=_LogQueueCallback(log_queue=log_queue),
                    shutdown_event=self._shutdown_event,
                )
                success = result.returncode == 0
                if not success:
                    error = f"mngr create exited with code {result.returncode}"
            except (OSError, ConcurrencyGroupError) as e:
                error = str(e)
                _loguru_logger.opt(exception=e).error("Error creating agent {}", agent_id)

            with self._lock:
                self._proto_agents.pop(agent_id, None)
                self._log_queues.pop(agent_id, None)
                if success:
                    self._agents[agent_id] = AgentStateItem(
                        id=agent_id,
                        name=agent_name,
                        state="RUNNING",
                        labels=labels,
                        work_dir=str(work_dir),
                        harness=harness,
                    )
        except Exception as e:
            # Force-demote success: the happy path sets success=True before
            # constructing AgentStateItem, so if pydantic validation (or
            # anything else after the subprocess returned 0) raises, success
            # would still be True while _agents was never populated. That
            # would broadcast a contradictory proto_agent_completed(success=
            # True, error="Unexpected ..."). The catch-all's contract is
            # "something unexpected happened, surface it as a clean
            # failure", so force success=False regardless of prior state.
            success = False
            error = f"Unexpected {type(e).__name__}: {e}"
            _loguru_logger.opt(exception=e).error("Unexpected error creating agent {}", agent_id)
            # The proto-agent entry may still be sitting in _proto_agents if
            # the exception fired before the cleanup block. Try once more,
            # safely, before we broadcast completion.
            try:
                with self._lock:
                    self._proto_agents.pop(agent_id, None)
                    self._log_queues.pop(agent_id, None)
            except (OSError, RuntimeError) as cleanup_exc:
                _loguru_logger.opt(exception=cleanup_exc).error("Failed to clean proto-agent entry for {}", agent_id)

        _completion_signal_put(log_queue, json.dumps({"done": True, "success": success, "error": error}))
        _completion_signal_put(log_queue, None)

        if success:
            self._ensure_activity_tracking(agent_id)
            self._ensure_model_tracking(agent_id)
            self._broadcaster.broadcast_agents_updated(self.get_agents_serialized())
        self._broadcaster.broadcast_proto_agent_completed(agent_id=agent_id, success=success, error=error)

    def _initial_discover(self) -> None:
        """Perform initial agent discovery and start app-registry watchers."""
        try:
            agents = discover_agents()
            with self._lock:
                for agent_info in agents:
                    agent_state = AgentStateItem(
                        id=agent_info.id,
                        name=agent_info.name,
                        state=agent_info.state,
                        labels=agent_info.labels,
                        work_dir=agent_info.work_dir,
                        harness=agent_info.harness,
                    )
                    self._agents[agent_info.id] = agent_state
                    # Treat assist chats that already exist at startup as already-handled
                    # so a restart restores the saved layout instead of reopening their tabs.
                    if agent_info.labels.get(_ASSIST_AUTO_OPEN_LABEL) == "true":
                        self._auto_opened_assist_ids.add(agent_info.id)

            for agent_info in agents:
                if agent_info.id == self._own_agent_id and agent_info.work_dir:
                    self._start_app_watcher(agent_info.id, Path(agent_info.work_dir))
                self._ensure_activity_tracking(agent_info.id)
                self._ensure_model_tracking(agent_info.id)
        except (OSError, ValueError, RuntimeError, MngrError) as e:
            _loguru_logger.opt(exception=e).error("Initial agent discovery failed")

    def _refresh_agents(self) -> None:
        """Re-discover all agents and broadcast updates."""
        try:
            agents = discover_agents()
            new_agents: dict[str, AgentStateItem] = {}
            for agent_info in agents:
                new_agents[agent_info.id] = AgentStateItem(
                    id=agent_info.id,
                    name=agent_info.name,
                    state=agent_info.state,
                    labels=agent_info.labels,
                    work_dir=agent_info.work_dir,
                    harness=agent_info.harness,
                )

            with self._lock:
                old_ids = set(self._agents.keys())
                new_ids = set(new_agents.keys())
                self._agents = new_agents

            for agent_id in new_ids:
                self._ensure_activity_tracking(agent_id)
                self._ensure_model_tracking(agent_id)
            for agent_id in old_ids - new_ids:
                self._stop_app_watcher(agent_id)
                self._stop_activity_tracking(agent_id)
                self._stop_model_tracking(agent_id)

            self._broadcaster.broadcast_agents_updated(self.get_agents_serialized())

        except (OSError, ValueError, RuntimeError, MngrError) as e:
            _loguru_logger.opt(exception=e).error("Agent refresh failed")

    def _resolve_observe_cwd(self) -> Path:
        """Return the cwd for the mngr observe subprocess.

        Prefers ``MNGR_AGENT_WORK_DIR`` so observe picks up the same
        project-local ``.mngr/settings.toml`` that agent-creation commands
        run against -- the things observe lists should match what the
        primary agent could create. Falls back to ``$HOME`` when the work
        dir is unset or does not exist (e.g. tests that stub the env var
        with a non-existent path); ``$HOME`` avoids inheriting whatever
        project config happens to live under the spawning process's cwd.
        """
        work_dir = os.environ.get("MNGR_AGENT_WORK_DIR", "")
        if work_dir:
            candidate = Path(work_dir)
            if candidate.is_dir():
                return candidate
        return Path.home()

    def _build_observe_command(self) -> list[str]:
        """Build the argv for the mngr observe --stream-events subprocess. Pure."""
        return _build_observe_command_argv(self._mngr_binary)

    def _start_observe(self) -> None:
        """Start the mngr observe subprocess and a watchdog for early exit."""
        cmd = self._build_observe_command()

        self._observe_cg = ConcurrencyGroup(name="agent-manager-observe")
        self._observe_cg.__enter__()

        try:
            # Run from the primary agent's work dir so observe inherits the
            # same project-local .mngr/settings.toml that mngr create uses --
            # otherwise observe picks up ~/.mngr config, which inside a Docker
            # agent typically has providers enabled (e.g. modal) that are not
            # authenticated. `mngr observe` itself now tolerates unauthenticated
            # providers (its discovery runs under ErrorBehavior.CONTINUE, so a
            # failing provider is surfaced per-provider and still emits a
            # DISCOVERY_FULL snapshot); scoping to the project providers via cwd
            # is kept only to avoid that noise and the wasted credential probes.
            # `is_checked_by_group=False` because we terminate this long-running
            # subprocess explicitly via `.terminate()` in `stop()`; that SIGTERM
            # produces a non-zero exit code that should not surface as a
            # ProcessError when the concurrency group exits. The watchdog thread
            # below is responsible for distinguishing graceful shutdown from
            # unexpected early exit.
            process = self._observe_cg.run_process_in_background(
                command=cmd,
                cwd=self._resolve_observe_cwd(),
                on_output=self._handle_observe_output_line,
                shutdown_event=self._shutdown_event,
                is_checked_by_group=False,
            )
        except (OSError, InvalidConcurrencyGroupStateError):
            _loguru_logger.warning(
                "Could not start mngr observe subprocess. Agent lifecycle events will not be detected."
            )
            self._observe_cg.__exit__(None, None, None)
            self._observe_cg = None
            return

        self._observe_process = process

        # ``run_process_in_background`` returns immediately even if the spawned
        # binary exits with a non-zero code (e.g. import failure). Attach a
        # watchdog so a silently-dying subprocess surfaces as a loud error
        # instead of a stale agent list.
        self._observe_cg.start_new_thread(
            target=self._watch_observe_process,
            args=(process,),
            name="observe-watchdog",
            is_checked=False,
        )

    def _watch_observe_process(self, process: RunningProcess) -> None:
        """Log an error if the observe subprocess exits before shutdown."""
        try:
            process.wait()
        except (ProcessError, EnvironmentStoppedError) as e:
            if self._shutdown_event.is_set():
                return
            _loguru_logger.opt(exception=e).error("mngr observe subprocess failed")
            return

        if self._shutdown_event.is_set():
            return

        stderr = process.read_stderr().strip()
        _loguru_logger.error(
            "mngr observe subprocess exited unexpectedly (returncode={}). "
            "Agent lifecycle events will no longer be detected. stderr: {}",
            process.returncode,
            stderr if stderr else "(empty)",
        )

    def _handle_observe_output_line(self, line: str, is_stdout: bool) -> None:
        """Parse and dispatch a single line of output from mngr observe.

        stderr lines are surfaced as warnings so startup failures from the
        subprocess (import errors, bad flags, etc.) are not lost.
        """
        stripped = line.strip()
        if not stripped:
            return
        if not is_stdout:
            _loguru_logger.warning("mngr observe stderr: {}", stripped)
            return
        event = parse_observe_event_line(stripped)
        if event is None:
            # The agents stream carries only AGENT_STATE / AGENTS_FULL_STATE /
            # AGENT_REMOVED; parse_observe_event_line returns None for empty lines
            # (filtered above) and for any other/forward-compatible type, which we
            # simply ignore. (Malformed JSON raises out of the parser.)
            return
        self._handle_observe_event(event)

    def _handle_observe_event(self, event: AgentStateEvent | FullAgentStateEvent | AgentRemovedEvent) -> None:
        """Fold one observe agents-stream event into the tracked agent view.

        ``AGENTS_FULL_STATE`` rebuilds the whole set, ``AGENT_STATE`` upserts one
        agent, and ``AGENT_REMOVED`` drops one. ``self._agents`` and
        ``self._match_by_agent_id`` are then rebuilt from the folded view -- now
        carrying each agent's real lifecycle ``state`` (``AgentDetails.state``)
        rather than a hardcoded literal -- while the before/after key diff drives
        per-agent resource start/stop (app watcher, activity tracking) and assist
        auto-open, exactly as the discovery membership delta used to.
        """
        with self._lock:
            before_details = dict(self._agent_details_by_id)
            match event:
                case FullAgentStateEvent():
                    self._agent_details_by_id = {str(agent.id): agent for agent in event.agents}
                case AgentStateEvent():
                    self._agent_details_by_id[str(event.agent.id)] = event.agent
                case AgentRemovedEvent():
                    self._agent_details_by_id.pop(str(event.agent_id), None)
            details_by_id = dict(self._agent_details_by_id)

        before_ids = set(before_details)
        after_ids = set(details_by_id)
        added_agent_ids = after_ids - before_ids
        removed_agent_ids = before_ids - after_ids
        # Persisting agents whose lifecycle state changed (e.g. RUNNING -> STOPPED
        # when a process dies) need their activity indicator re-gated below.
        state_changed_ids = {
            agent_id
            for agent_id, agent in details_by_id.items()
            if agent_id in before_details and before_details[agent_id].state != agent.state
        }

        new_agents: dict[str, AgentStateItem] = {}
        new_matches: dict[str, AgentMatch] = {}
        for agent_id, agent in details_by_id.items():
            new_agents[agent_id] = AgentStateItem(
                id=agent_id,
                name=str(agent.name),
                state=agent.state.value,
                labels=dict(agent.labels),
                work_dir=str(agent.work_dir),
                harness=parse_harness(str(agent.type)),
            )
            new_matches[agent_id] = _build_agent_match(agent)

        with self._lock:
            # Rebuilding ``_agents`` wholesale drops the per-agent derived fields
            # (the observe payload carries neither ``activity_state`` nor
            # ``model_choice``). Re-apply the cached values via ``model_copy`` so the
            # broadcast below does not blank them for already-tracked agents; the
            # recompute passes just below then re-derive from current disk/lifecycle.
            for agent_id, agent_state in new_agents.items():
                updates: list[tuple[str, Any]] = []
                cached_state = self._activity_state_by_agent.get(agent_id)
                if cached_state is not None:
                    updates.append(to_update(agent_state.field_ref().activity_state, cached_state))
                cached_choice = self._model_choice_by_agent.get(agent_id)
                if cached_choice is not None:
                    updates.append(to_update(agent_state.field_ref().model_choice, cached_choice))
                cached_queued = self._queued_messages_by_agent.get(agent_id)
                if cached_queued:
                    updates.append(to_update(agent_state.field_ref().queued_messages, cached_queued))
                if updates:
                    new_agents[agent_id] = agent_state.model_copy_update(*updates)
            self._agents = new_agents
            self._match_by_agent_id = new_matches

        for agent_id in added_agent_ids:
            added_agent_state = new_agents.get(agent_id)
            if added_agent_state is None:
                continue
            if agent_id == self._own_agent_id and added_agent_state.work_dir:
                self._start_app_watcher(agent_id, Path(added_agent_state.work_dir))
            self._ensure_activity_tracking(agent_id)
            self._ensure_model_tracking(agent_id)

        for agent_id in removed_agent_ids:
            self._stop_app_watcher(agent_id)
            self._stop_activity_tracking(agent_id)
            self._stop_model_tracking(agent_id)

        # Re-derive activity for persisting agents whose lifecycle state changed,
        # so a RUNNING -> STOPPED transition (e.g. a process dying) re-gates the
        # activity indicator through the unchanged ``is_agent_running`` gate --
        # otherwise a stopped agent would keep a stale "Thinking..." indicator.
        # Added agents were already recomputed via _ensure_activity_tracking above;
        # unchanged agents keep their re-applied cached state. broadcast_on_change
        # is False so the single broadcast below stays authoritative.
        with self._lock:
            recompute_ids = [agent_id for agent_id in state_changed_ids if agent_id in self._activity_tracked_agents]
        for agent_id in recompute_ids:
            self._recompute_activity_state(agent_id, broadcast_on_change=False)

        # Broadcast the updated agent list BEFORE any auto-open: the frontend's open
        # handler resolves ``chat:<name>`` against its known-agents list and drops the
        # open if the agent is not there yet, so the chat must be known first.
        self._broadcaster.broadcast_agents_updated(self.get_agents_serialized())

        # A newly-created chat usually surfaces as a freshly-added agent here, so
        # auto-open assist chats that have appeared. ``_maybe_auto_open_assist``
        # dedupes, so an assist chat already present (including at startup) is not
        # reopened.
        for agent_id in added_agent_ids:
            appeared_agent_state = new_agents.get(agent_id)
            if appeared_agent_state is not None:
                self._maybe_auto_open_assist(appeared_agent_state)

    def _maybe_auto_open_assist(self, agent_state: AgentStateItem) -> None:
        """Auto-open ``agent_state``'s tab if it is an assist chat we have not opened yet.

        Idempotent via ``_auto_opened_assist_ids``: assist chats present at startup are
        seeded into that set by ``_initial_discover`` (so a restart never reopens them),
        and each later-appearing assist chat is opened exactly once -- regardless of
        whether it arrives via the per-agent delta or a full snapshot.
        """
        if agent_state.labels.get(_ASSIST_AUTO_OPEN_LABEL) != "true":
            return
        with self._lock:
            if agent_state.id in self._auto_opened_assist_ids:
                return
            self._auto_opened_assist_ids.add(agent_state.id)
        self._broadcaster.broadcast_layout_op(
            op="open",
            args={"ref": f"chat:{agent_state.name}"},
            requester_agent_id=self._own_agent_id,
        )

    def _start_app_watcher(self, agent_id: str, work_dir: Path) -> None:
        """Start watching data/.state/apps.toml for an agent."""
        with self._lock:
            if agent_id in self._app_observers:
                return

        toml_path = work_dir / _APPS_TOML_FILENAME
        watch_dir = toml_path.parent

        if not watch_dir.exists():
            watch_dir.mkdir(parents=True, exist_ok=True)

        self._read_apps(toml_path)

        handler = _make_apps_file_handler(agent_id, self._on_apps_changed)
        observer = _Observer()
        observer.schedule(handler, str(watch_dir), recursive=False)
        observer.daemon = True
        try:
            observer.start()
            with self._lock:
                if agent_id in self._app_observers:
                    observer.stop()
                    return
                self._app_observers[agent_id] = observer
        except OSError as e:
            _loguru_logger.opt(exception=e).error("Failed to start app-registry watcher for agent {}", agent_id)

    def _stop_app_watcher(self, agent_id: str) -> None:
        """Stop watching apps.toml for an agent."""
        with self._lock:
            observer = self._app_observers.pop(agent_id, None)
        if observer is not None:
            observer.stop()

    def _on_apps_changed(self, agent_id: str) -> None:
        """Called when the primary agent's apps.toml changes."""
        with self._lock:
            agent = self._agents.get(agent_id)
            work_dir = agent.work_dir if agent is not None else None

        if work_dir is None:
            return

        toml_path = Path(work_dir) / _APPS_TOML_FILENAME
        self._read_apps(toml_path)
        self._broadcaster.broadcast_apps_updated(self.get_apps_serialized())

    def _get_agent_state_dir(self, agent_id: str) -> Path:
        """Return the per-agent state directory under the local mngr host dir.

        Mirrors ``server._find_agent`` so the readiness-hook marker files and
        the activity tracker agree on the same path.
        """
        return self._host_dir / "agents" / agent_id

    def _ensure_activity_tracking(self, agent_id: str) -> None:
        """Start activity tracking for ``agent_id`` if its local state dir exists.

        Skips agents whose state directory is not present on this host -- those
        are tracked on a remote host and have no local transcript to watch.
        Idempotent: a second call does not duplicate work. The cached activity
        state is re-applied to ``_agents`` on every call, which matters because
        the lifecycle handlers (``_handle_observe_event``, ``_refresh_agents``)
        rebuild ``_agents`` entries from raw observe data with
        ``activity_state=None`` and rely on this method (for newly-added agents)
        or on ``_handle_observe_event``'s own cached-state re-application (for
        agents that persist across events) to repopulate it.
        """
        state_dir = self._get_agent_state_dir(agent_id)
        if not state_dir.exists():
            return
        with self._lock:
            self._activity_tracked_agents.add(agent_id)
            agent_state = self._agents.get(agent_id)
            harness = agent_state.harness if agent_state is not None else DEFAULT_HARNESS
            # codex drives activity + queue through its live ledger (below), not the
            # transcript-derived tracker, so it gets NO tracker entry -- which is exactly what
            # makes ``_recompute_activity_state`` / ``update_session_events`` / ``reset`` no-op
            # for codex (they short-circuit on a missing tracker). Every other harness builds its
            # tracker here, once, from the harness it was created with -- the single place a
            # harness name selects the transcript-derived behavior.
            needs_tracker = harness != HarnessType.CODEX and agent_id not in self._activity_tracker_by_agent
            if needs_tracker:
                self._activity_tracker_by_agent[agent_id] = build_tracker(harness)
        if harness == HarnessType.CODEX:
            # Give the agent its one live app-server connection + ledger (idempotent; rebuilds a
            # connection whose daemon generation died). The ledger's callbacks own codex's
            # activity dot and queue chips from here on.
            self._ensure_codex_connection(agent_id)
        self._recompute_activity_state(agent_id, broadcast_on_change=False)

    def _stop_activity_tracking(self, agent_id: str) -> None:
        """Stop activity tracking and clear cached activity + queued state."""
        with self._lock:
            connection = self._codex_connection_by_agent.pop(agent_id, None)
            self._activity_tracked_agents.discard(agent_id)
            self._activity_tracker_by_agent.pop(agent_id, None)
            self._activity_state_by_agent.pop(agent_id, None)
            self._queued_messages_by_agent.pop(agent_id, None)
            self._queue_idle_handler_by_agent.pop(agent_id, None)
        # Stop the reader + close the client outside the lock (join blocks on the reader thread).
        if connection is not None:
            connection.stop()

    def _ensure_codex_connection(self, agent_id: str) -> None:
        """Ensure a live app-server connection + ledger for a codex ``agent_id``.

        Idempotent and self-healing: a live connection is left alone; a connection whose daemon
        generation died (reader saw the transport close) is reaped and rebuilt, whose fresh ledger
        starts with an empty queue (the queue is EPHEMERAL -- nothing from the dead generation is
        revived). When the daemon is not yet reachable (a just-created agent still starting), the
        build returns ``None`` and this simply retries on the next observe tick.

        The connect + handshake is blocking network I/O, so it runs OUTSIDE the manager lock
        (mirroring ``_ensure_model_tracking``); the built connection is stored under the lock, and
        a concurrent build that lost the race is stopped rather than leaked.
        """
        with self._lock:
            existing = self._codex_connection_by_agent.get(agent_id)
            if existing is not None and existing.is_alive:
                return
            dead = existing if existing is not None else None
            if dead is not None:
                self._codex_connection_by_agent.pop(agent_id, None)
        if dead is not None:
            dead.stop()
        state_dir = self._get_agent_state_dir(agent_id)
        connection = CodexLiveConnection.build(
            state_dir,
            on_queue_snapshot=lambda snapshot: self.update_queued_messages(agent_id, snapshot),
            on_activity=lambda activity: self.set_codex_activity(agent_id, activity),
            model_state_path=get_model_state_path(HarnessType.CODEX, state_dir),
        )
        if connection is None:
            return
        with self._lock:
            # Lost the build race, or tracking stopped while we connected: don't leak the new one.
            if agent_id not in self._activity_tracked_agents or self._codex_connection_by_agent.get(agent_id) is not None:
                stale = connection
            else:
                self._codex_connection_by_agent[agent_id] = connection
                stale = None
        if stale is not None:
            stale.stop()
            return
        # Seed the activity slot from the freshly-bound thread's live state (IDLE, or a turn still
        # running after a UI reconnect) so the dot is correct before the first notification.
        self.set_codex_activity(agent_id, connection.ledger.activity_state())

    def _reap_codex_connection(self, agent_id: str) -> None:
        """Drop and stop a codex agent's live connection (its daemon generation is gone)."""
        with self._lock:
            connection = self._codex_connection_by_agent.pop(agent_id, None)
        if connection is not None:
            connection.stop()

    def _clear_codex_queue(self, agent_id: str) -> None:
        """Drop a codex agent's cached queue chips (its ephemeral queue died with the daemon).

        Broadcasts the emptied state only when it actually changed; the caller's own activity
        broadcast (if any) then carries the same cleared snapshot.
        """
        with self._lock:
            if agent_id not in self._activity_tracked_agents:
                return
            agent_state = self._agents.get(agent_id)
            if agent_state is None or not agent_state.queued_messages:
                self._queued_messages_by_agent[agent_id] = ()
                return
            self._queued_messages_by_agent[agent_id] = ()
            self._agents[agent_id] = agent_state.model_copy_update(
                to_update(agent_state.field_ref().queued_messages, ())
            )
        self._broadcaster.broadcast_agents_updated(self.get_agents_serialized())

    def get_codex_ledger(self, agent_id: str) -> CodexMessageLedger | None:
        """The live message ledger for a codex ``agent_id``, or ``None`` if none is up.

        ``None`` means no reachable daemon connection right now (agent not codex, not tracked, or
        the daemon is down/starting). The send / interrupt / shoulder-tap endpoints read this.
        """
        with self._lock:
            connection = self._codex_connection_by_agent.get(agent_id)
        if connection is None or not connection.is_alive:
            return None
        return connection.ledger

    def ensure_codex_ledger(self, agent_id: str) -> CodexMessageLedger | None:
        """Build the codex connection if needed, then return its ledger (or ``None``).

        The send endpoint uses this so the very first message to a just-ready agent does not race
        the observe-driven connection build.
        """
        self._ensure_codex_connection(agent_id)
        return self.get_codex_ledger(agent_id)

    def set_codex_activity(self, agent_id: str, activity: ActivityState) -> None:
        """Apply an activity state pushed by a codex agent's ledger, broadcasting on a real change.

        This is codex's activity path (contract A6: RUNNING until ``turn/completed``), replacing
        the transcript-derived tracker for codex. No-op for an untracked agent (a callback racing
        teardown) or when the state did not change.
        """
        with self._lock:
            if agent_id not in self._activity_tracked_agents:
                return
            agent_state = self._agents.get(agent_id)
            if agent_state is None:
                return
            old_state = self._activity_state_by_agent.get(agent_id)
            if old_state == activity and agent_state.activity_state == activity.value:
                return
            self._activity_state_by_agent[agent_id] = activity
            self._agents[agent_id] = agent_state.model_copy_update(
                to_update(agent_state.field_ref().activity_state, activity)
            )
        self._broadcaster.broadcast_agents_updated(self.get_agents_serialized())

    def register_queue_idle_handler(self, agent_id: str, handler: Callable[[], list[dict[str, Any]]]) -> None:
        """Register the agent watcher's working->IDLE queue backstop.

        Called once when the watcher is created. On a working->IDLE transition
        ``_recompute_activity_state`` invokes it: the handler clears the harness
        queue populator and returns the resulting (empty) snapshot, which the same
        broadcast that carries the IDLE state also carries.
        """
        with self._lock:
            self._queue_idle_handler_by_agent[agent_id] = handler

    def update_queued_messages(self, agent_id: str, snapshot: list[dict[str, Any]]) -> None:
        """Cache and broadcast a fresh queued-message snapshot from the agent's watcher.

        The full snapshot replaces the cached one wholesale (the frontend does the
        same). No-op for an agent that is no longer tracked (a callback racing with
        destruction). Only broadcasts when the snapshot actually changed.

        A replayed snapshot can arrive with no recompute ever following it (e.g. a
        priming replay for a stopped agent, whose lifecycle never changes again),
        so the level-triggered idle sweep is run here, after caching and BEFORE the
        broadcast: an idle agent's stale snapshot is drained via its idle handler
        and the single broadcast below carries the post-sweep state, so phantoms
        are never rendered. A live mid-turn agent derives non-IDLE (its transcript
        signals are seeded before the watcher starts) and the snapshot stands.
        """
        queued = tuple(QueuedMessageState.model_validate(entry) for entry in snapshot)
        with self._lock:
            if agent_id not in self._activity_tracked_agents:
                return
            agent_state = self._agents.get(agent_id)
            if agent_state is None:
                return
            if self._queued_messages_by_agent.get(agent_id, ()) == queued and agent_state.queued_messages == queued:
                return
            self._queued_messages_by_agent[agent_id] = queued
            self._agents[agent_id] = agent_state.model_copy_update(
                to_update(agent_state.field_ref().queued_messages, queued)
            )
        # Evaluate the level-triggered idle sweep before this snapshot is ever rendered:
        # a replayed snapshot can arrive with no later recompute trigger (the event
        # fan-out runs strictly before the snapshot push, and a permanently-dead agent
        # never re-enters the observe delta), so a dead generation's orphans would
        # otherwise broadcast and stick. ``broadcast_on_change=False`` keeps the single
        # broadcast below authoritative -- it carries the post-sweep state.
        self._recompute_activity_state(agent_id, broadcast_on_change=False)
        self._broadcaster.broadcast_agents_updated(self.get_agents_serialized())

    def _ensure_model_tracking(self, agent_id: str) -> None:
        """Watch the agent's live model-state file once its state dir exists.

        The live read is harness-neutral -- the shared reader over the harness's
        registered ``minds_model_state.json`` -- so there is nothing to build per agent;
        this just derives the current choice and, when the local state dir is present,
        starts the one watch that drives every later recompute. Idempotent (the watch is
        retried on later calls until the dir appears).
        """
        agent_state = self.get_agent_by_id(agent_id)
        if agent_state is None:
            return
        with self._lock:
            needs_watcher = agent_id not in self._model_watcher_by_agent
        self._recompute_model_choice(agent_id, broadcast_on_change=False)
        if needs_watcher and self._get_agent_state_dir(agent_id).exists():
            state_path = get_model_state_path(agent_state.harness, self._get_agent_state_dir(agent_id))
            new_watcher = PathWatcher.build(
                (state_path,),
                lambda: self._recompute_model_choice(agent_id, broadcast_on_change=True),
            )
            with self._lock:
                already_watched = agent_id in self._model_watcher_by_agent
                if not already_watched:
                    self._model_watcher_by_agent[agent_id] = new_watcher
            if not already_watched:
                new_watcher.start()

    def _stop_model_tracking(self, agent_id: str) -> None:
        """Stop the model watcher and clear the cached choice for an agent."""
        with self._lock:
            watcher = self._model_watcher_by_agent.pop(agent_id, None)
            self._model_choice_by_agent.pop(agent_id, None)
        if watcher is not None:
            watcher.stop()

    def _recompute_model_choice(self, agent_id: str, *, broadcast_on_change: bool, force: bool = False) -> None:
        """Recompute an agent's model choice from its live state file, then cache/broadcast it.

        Mirrors ``_recompute_activity_state``: the disk read runs outside the lock,
        the no-op guard suppresses an unchanged broadcast, and ``model_copy`` updates
        only the ``model_choice`` slot. ``force`` bypasses the no-op guard so the
        switch endpoint can push one authoritative choice even when the switch left
        the derived value unchanged -- otherwise an optimistic pending pick that
        resolves to the same value would never be superseded on the frontend.
        """
        # Resolve the harness first (like the activity recompute resolves its tracker): it
        # names the state file to read, and the read must stay outside the lock.
        with self._lock:
            harness_state = self._agents.get(agent_id)
        if harness_state is None:
            return
        # The disk read (minds_model_state.json) stays outside the lock. Only harness +
        # state dir are needed -- not claude_config_dir, which would cost an env-file read.
        identity = read_model_identity(get_model_state_path(harness_state.harness, self._get_agent_state_dir(agent_id)))
        with self._lock:
            agent_state = self._agents.get(agent_id)
            if agent_state is None:
                return
            # identity is None when the harness has recorded no model yet (e.g. before a
            # session's first statusline fire, or a remote agent) -> no choice, logo-only.
            if identity is None:
                choice: ModelChoice | None = None
            else:
                matched = match_option(identity, get_catalog(agent_state.harness).options)
                choice = ModelChoice(identity=identity, matched=matched)
            old_choice = self._model_choice_by_agent.get(agent_id)
            if not force and old_choice == choice and agent_state.model_choice == choice:
                return
            self._model_choice_by_agent[agent_id] = choice
            self._agents[agent_id] = agent_state.model_copy_update(
                to_update(agent_state.field_ref().model_choice, choice)
            )
        if broadcast_on_change:
            self._broadcaster.broadcast_agents_updated(self.get_agents_serialized())

    def refresh_model_choice(self, agent_id: str) -> None:
        """Force one authoritative model-choice broadcast (bypassing the no-op guard).

        Called after a switch so the optimistic frontend reconciles even when the
        switch left the derived value unchanged.
        """
        self._recompute_model_choice(agent_id, broadcast_on_change=True, force=True)

    def _read_process_started_at(self, agent_id: str, marker_filename: str) -> float | None:
        """Return the mtime of the agent's ``*_process_started`` marker, or None.

        mngr touches this marker on every startup/resume (a fresh, not-mid-turn
        agent process), so its mtime is the boundary the activity tracker
        compares transcript timestamps against. The filename is harness-specific
        (``HarnessActivityTracker.marker_filename``) because each mngr plugin
        writes its own -- ``claude_process_started`` / ``codex_process_started``.
        Returns ``None`` when the marker is absent (e.g. an agent that has not
        restarted since the marker was introduced) so the staleness override
        simply does not fire.
        """
        marker = self._get_agent_state_dir(agent_id) / marker_filename
        try:
            return marker.stat().st_mtime
        except OSError:
            return None

    def _recompute_activity_state(self, agent_id: str, *, broadcast_on_change: bool) -> None:
        """Recompute activity state for ``agent_id`` from cached transcript signals.

        If the derived state differs from the previously cached state, the
        ``_agents`` entry is updated and (when ``broadcast_on_change`` is True)
        an ``agents_updated`` event is broadcast.

        Quietly does nothing when the agent is not being tracked for activity
        (e.g. a remote agent) or is no longer in ``_agents``.
        """
        # Resolve the tracker first: it names the marker to stat, and the stat
        # must stay outside the lock (it is a filesystem call, not shared state).
        with self._lock:
            tracker = self._activity_tracker_by_agent.get(agent_id)
            recompute_agent_state = self._agents.get(agent_id)
        # codex has NO tracker -- its ledger drives the dot (contract A6, RUNNING until
        # ``turn/completed``). The one thing the ledger cannot observe is its own daemon dying (no
        # ``turn/completed`` ever arrives), so a positively-dead lifecycle here forces the dot IDLE
        # and reaps the dead connection; otherwise the ledger stays in charge and this is a no-op.
        if recompute_agent_state is not None and recompute_agent_state.harness == HarnessType.CODEX:
            if is_lifecycle_dead(recompute_agent_state.state):
                # The daemon generation is gone: reap the connection, drop any queue chips it left
                # (the queue is EPHEMERAL -- it dies with the session, and an abrupt daemon kill
                # emits no idle sweep to clear it), and settle the dot to IDLE.
                self._reap_codex_connection(agent_id)
                self._clear_codex_queue(agent_id)
                self.set_codex_activity(agent_id, ActivityState.IDLE)
            return
        if tracker is None:
            return
        # Re-read on every recompute so a restart that touches the marker is
        # reflected even when no new transcript events arrive -- the post-restart
        # observe snapshot drives the recompute.
        process_started_at = self._read_process_started_at(agent_id, tracker.marker_filename)
        # The `active` marker flips promptly at turn start/end, whereas the observe-reported
        # lifecycle state can miss a short turn entirely -- so read the marker directly for a
        # timely "is a turn in flight" signal (stat outside the lock, like the one above).
        is_active_marker_present = (self._get_agent_state_dir(agent_id) / ACTIVE_MARKER_FILENAME).exists()
        with self._lock:
            if agent_id not in self._activity_tracked_agents:
                return
            agent_state = self._agents.get(agent_id)
            if agent_state is None:
                return
            new_state = tracker.derive(
                is_agent_running=resolve_is_agent_running(agent_state.state, is_active_marker_present),
                process_started_at=process_started_at,
            )
            # A positively-dead lifecycle (STOPPED and friends -- never UNKNOWN, which is
            # non-evidence) overrides whatever the transcript derives: the process's
            # in-memory queue and in-flight turn died with it. Claude and pi already
            # gate their derive on ``is_agent_running``; codex deliberately ignores the
            # lifecycle there (its turn latch owns the RUNNING/WAITING flap, not the
            # dead/alive axis), so without this override a dead codex agent keeps a
            # phantom "Thinking" dot and the level-triggered sweep below never fires.
            if is_lifecycle_dead(agent_state.state):
                new_state = ActivityState.IDLE
            old_state = self._activity_state_by_agent.get(agent_id)
            # The queued-message backstop is LEVEL-triggered, not edge-triggered: an
            # IDLE agent's harness queue is drained by definition, so ANY queued
            # survivor while idle is stale -- an interrupt, our flush-restart SIGKILL,
            # a crash, a hole in the harness's own ledger (an enqueue with no matching
            # leave), or a stale entry re-surfaced by a backend restart's full replay
            # (which sees no new working->IDLE transition to sweep it). So sweep
            # whenever the agent is idle with a non-empty queue, even if the activity
            # state itself did not change this cycle -- an edge-only backstop leaves
            # such survivors stranded on an idle agent forever.
            is_idle = new_state == ActivityState.IDLE
            has_stale_queue = is_idle and bool(self._queued_messages_by_agent.get(agent_id))
            if old_state == new_state and agent_state.activity_state == new_state.value and not has_stale_queue:
                return
            self._activity_state_by_agent[agent_id] = new_state
            # Update just this slot so any cached ``model_choice`` stays intact --
            # each derived field updates its own field without knowing the others'.
            self._agents[agent_id] = agent_state.model_copy_update(
                to_update(agent_state.field_ref().activity_state, new_state)
            )
            idle_handler = self._queue_idle_handler_by_agent.get(agent_id) if has_stale_queue else None

        # The idle handler clears the watcher's queue populator and returns the
        # resulting (empty) snapshot; it calls into the watcher, so it runs outside
        # the lock, and its snapshot is folded into the same broadcast as the IDLE
        # state below. Runs regardless of ``broadcast_on_change`` (it is a state
        # mutation); only the broadcast itself is gated.
        if idle_handler is not None:
            drained = tuple(QueuedMessageState.model_validate(entry) for entry in idle_handler())
            with self._lock:
                idle_agent_state = self._agents.get(agent_id)
                if idle_agent_state is not None and idle_agent_state.queued_messages != drained:
                    self._queued_messages_by_agent[agent_id] = drained
                    self._agents[agent_id] = idle_agent_state.model_copy_update(
                        to_update(idle_agent_state.field_ref().queued_messages, drained)
                    )

        if broadcast_on_change:
            self._broadcaster.broadcast_agents_updated(self.get_agents_serialized())

    def update_session_events(self, agent_id: str, events: list[dict[str, Any]]) -> None:
        """Recompute transcript-derived activity signals from the full event list.

        Called by ``server._get_or_create_watcher`` whenever the
        :class:`AgentSessionWatcher` learns of new events. Cheap to call: the
        tracker short circuits when none of its derived signals changed, so a
        streamed line that moves nothing skips both the recompute and its
        per-event marker stat.

        No-op for agents not being tracked for activity (e.g. remote agents, or
        stale callbacks for an agent that was just destroyed).
        """
        with self._lock:
            if agent_id not in self._activity_tracked_agents:
                return
            tracker = self._activity_tracker_by_agent.get(agent_id)
            if tracker is None or not tracker.observe(events):
                return

        self._recompute_activity_state(agent_id, broadcast_on_change=True)

    def reset_activity_state(self, agent_id: str) -> None:
        """Force ``agent_id`` back to IDLE after an interrupt/restart.

        Interrupting an agent restarts its harness process. The restart abandons
        the session transcript mid-turn -- the last recorded event is still an
        unmatched ``tool_use`` or a ``tool_result`` -- so the transcript-derived
        activity state stays pinned at TOOL_RUNNING / THINKING until the user
        sends another message. The restart is a backend action that the
        transcript never records, so the backend must reset the derived signals
        explicitly; ``HarnessActivityTracker.reset`` clears whichever signals
        that harness caches, making its derive settle on IDLE.

        No-op for agents not being tracked for activity (remote agents, or a
        callback racing with destruction).
        """
        with self._lock:
            if agent_id not in self._activity_tracked_agents:
                return
            tracker = self._activity_tracker_by_agent.get(agent_id)
            if tracker is None:
                return
            tracker.reset()
        self._recompute_activity_state(agent_id, broadcast_on_change=True)

    def _read_apps(self, toml_path: Path) -> None:
        """Read and parse data/.state/apps.toml for the primary agent."""
        apps: list[AppEntry] = []
        if toml_path.exists():
            try:
                data = tomllib.loads(toml_path.read_text())
                for entry in data.get("apps", []):
                    name = entry.get("name", "")
                    url = entry.get("url", "")
                    label = entry.get("label", "")
                    if name and url:
                        apps.append(AppEntry(name=name, url=url, label=label))
            except (OSError, tomllib.TOMLDecodeError, KeyError, ValueError) as e:
                _loguru_logger.opt(exception=e).error("Failed to parse {}", toml_path)

        with self._lock:
            self._apps = apps
