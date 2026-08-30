import json
import os
import queue
import re
import shlex
import threading
import tomllib
from collections.abc import Callable
from datetime import datetime
from datetime import timedelta
from datetime import timezone
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
from imbue.concurrency_group.subprocess_utils import FinishedProcess
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
from imbue.mngr.primitives import HostName
from imbue.system_interface import client_activity
from imbue.system_interface import projects
from imbue.system_interface.activity_state import ActivityState
from imbue.system_interface.activity_state import RUNNING_LIFECYCLE_STATES
from imbue.system_interface.activity_state import is_lifecycle_dead
from imbue.system_interface.activity_state import parse_iso_timestamp_to_epoch
from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.agent_discovery import MngrMessenger
from imbue.system_interface.agent_discovery import SendFailure
from imbue.system_interface.agent_discovery import delivered_or_raise
from imbue.system_interface.agent_discovery import discover_agents
from imbue.system_interface.agent_discovery import get_host_dir
from imbue.system_interface.agent_discovery import read_claude_config_dir_from_env_file
from imbue.system_interface.auto_open import AutoOpenLedger
from imbue.system_interface.harnesses.activity import HarnessActivityTracker
from imbue.system_interface.harnesses.auth_check import HarnessAuthCheck
from imbue.system_interface.harnesses.auth_check import find_unauthenticated_harness_reason
from imbue.system_interface.harnesses.events import SPECIAL_EVENT_TYPE
from imbue.system_interface.harnesses.harness_type import DEFAULT_HARNESS
from imbue.system_interface.harnesses.harness_type import HarnessType
from imbue.system_interface.harnesses.harness_type import parse_harness
from imbue.system_interface.harnesses.model import ModelChoice
from imbue.system_interface.harnesses.model import ModelOption
from imbue.system_interface.harnesses.model import read_model_identity
from imbue.system_interface.harnesses.model import resolve_model_choice
from imbue.system_interface.harnesses.path_watch import PathWatcher
from imbue.system_interface.harnesses.registry import build_interrupt_to_composer
from imbue.system_interface.harnesses.registry import build_shoulder_tap
from imbue.system_interface.harnesses.registry import build_tracker
from imbue.system_interface.harnesses.registry import get_catalog
from imbue.system_interface.harnesses.registry import get_harness_spec
from imbue.system_interface.harnesses.registry import get_model_state_path
from imbue.system_interface.harnesses.session import AgentHarnessSession
from imbue.system_interface.harnesses.session import SessionDeps
from imbue.system_interface.liveness import probe_app_liveness
from imbue.system_interface.models import AgentCreationError
from imbue.system_interface.models import AgentNameConflictError
from imbue.system_interface.models import AgentRenameError
from imbue.system_interface.models import AgentStateItem
from imbue.system_interface.models import AppEntry
from imbue.system_interface.models import CreatedChatAgent
from imbue.system_interface.models import QueuedMessageState
from imbue.system_interface.naming import AUTO_NAME_WORD_BY_HARNESS
from imbue.system_interface.naming import canonical_agent_name
from imbue.system_interface.naming import first_free_numbered_name
from imbue.system_interface.naming import is_name_conflict
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

# How often the liveness sweep re-derives each app's ``is_running``. Stop and
# start land through our own endpoints (which nudge the sweep), so the poll
# only has to catch out-of-band transitions -- supervisorctl from a terminal,
# a crashed program -- for which a few seconds of lag is fine.
_LIVENESS_POLL_INTERVAL_SECONDS: Final[float] = 3.0

# How long one ``mngr rename`` / ``mngr label`` may take. Both edit the
# provider's persisted agent data (rename also moves the tmux session on a live
# host), so they are short local operations -- but they run inside a request the
# user is waiting on, so they are bounded rather than left to hang.
_RENAME_TIMEOUT_SECONDS: Final[float] = 30.0

# Labels that ask for a chat's tab to be surfaced when the chat appears, so the
# user lands on it without hunting. ``assist`` is the original, set by the minds
# "get help -> have an agent help" flow; ``auto_open`` is the purpose-neutral
# form any spawner can set (the minds update flow sets both, for interfaces
# that predate the second).
_ASSIST_AUTO_OPEN_LABEL = "assist"
_AUTO_OPEN_LABEL = "auto_open"
_AUTO_OPEN_LABELS: Final[tuple[str, ...]] = (_AUTO_OPEN_LABEL, _ASSIST_AUTO_OPEN_LABEL)

# How recently a chat found at startup must have been created for its
# undelivered open to still be owed. Older than this and a restart restores
# the saved layout as it always has -- the chat was either surfaced by an
# interface that kept no ledger, or has been sitting there long enough that
# popping it now would be a surprise rather than a hand-off.
_AUTO_OPEN_STARTUP_FRESHNESS: Final[timedelta] = timedelta(hours=12)


def _is_auto_open_labeled(labels: dict[str, str]) -> bool:
    return any(labels.get(label) == "true" for label in _AUTO_OPEN_LABELS)


# An app's icon is SVG markup carried verbatim on its registry row and handed to
# the browser, which inlines it. ``system/scripts/forward_port.py`` is the real
# validator (it parses the markup and rejects anything that is not a single,
# inert ``<svg>`` element); the checks below are a backstop for a hand-edited or
# otherwise unvalidated ``apps.toml``, so a bad icon is dropped here instead of
# reaching the DOM. They deliberately mirror, and are never looser than, that
# validator. Keep the cap in step with its ``MAX_ICON_LENGTH``.
_MAX_ICON_LENGTH = 16384
_FORBIDDEN_ICON_SUBSTRINGS = ("<script", "<style", "<foreignobject", "javascript:", "<!", "<?")
# An ``on*=`` attribute anywhere in a tag, e.g. ``<svg onload="...">``.
_ICON_EVENT_HANDLER_PATTERN = re.compile(r"<[^>]*\son[a-z]+\s*=", re.IGNORECASE)


def _accepted_icon(raw_icon: str) -> str:
    """Return ``raw_icon`` when it is safe to inline as an app icon, else ''."""
    icon = raw_icon.strip()
    if not icon or len(icon) > _MAX_ICON_LENGTH:
        return ""
    if not icon.startswith("<svg") or not icon.endswith(">"):
        return ""
    lowered = icon.lower()
    if any(forbidden in lowered for forbidden in _FORBIDDEN_ICON_SUBSTRINGS):
        return ""
    if _ICON_EVENT_HANDLER_PATTERN.search(icon) is not None:
        return ""
    return icon


def _chat_project_label(primary_labels: dict[str, str], project_id: str) -> str:
    """The ``project`` label value a new chat agent should carry.

    Chats are agents, and mngr already propagates an agent's ``project`` label
    to the children it spawns, so a chat created inside a project records that
    project here and its children inherit it. The label names the chat's
    *originating* project rather than an owner: membership is many-to-many, and
    what a view shows is its own member list, so this is where a chat starts out
    filed and not where it is stuck. A chat created outside any project inherits
    whatever the primary agent carries, and one left with no label at all is
    filed nowhere -- which costs it nothing, since Everything lists every object
    on the machine.
    """
    if project_id:
        return project_id
    return primary_labels.get("project", "")


def _build_chat_create_command(
    mngr_binary: str,
    name: str,
    agent_id: str,
    primary_labels: dict[str, str],
    harness: HarnessType,
    extra_role_templates: tuple[str, ...] = (),
    project_id: str = "",
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
        canonical_agent_name(name) or name,
        "--id",
        agent_id,
        "--transfer",
        "none",
        "--type",
        harness,
        "--template",
        "chat",
        *[arg for role in extra_role_templates for arg in ("--template", role)],
        # Tags this as a user-created agent so the OOM launch wrapper puts it in the
        # dynamic chat band (re-tagged from live UI engagement), not the worker band.
        "--label",
        "user_created=true",
        # The name the user sees. Its canonical form is the agent name above --
        # the pairing newer mngr enforces -- and it is what ``mngr list`` and
        # the workspace's own surfaces show.
        "--label",
        f"display_name={name}",
        "--no-connect",
    ]
    # The project the chat starts out filed in: the one it was created inside
    # when there is one, else whatever the primary agent carries. The chat agent
    # belongs to its workspace by sharing the host; it carries no workspace
    # label. (Fast-mode launch settings ride the ``first`` create template now,
    # so the builder no longer takes a fast-mode flag.)
    project_label = _chat_project_label(primary_labels, project_id)
    if project_label:
        cmd.extend(["--label", f"project={project_label}"])
    return cmd


def _build_chat_rename_command(mngr_binary: str, agent_id: str, name: str) -> list[str]:
    """Build the ``mngr rename`` argv that renames a chat agent to a typed name.

    The mirror image of ``_build_chat_create_command``'s naming: the agent is
    addressed by id (an agent address accepts either an id or a name, and the id
    cannot go stale under a rename), and it is given the *canonical* form of the
    typed name plus the typed name itself as a ``display_name`` label. Sending
    the pair explicitly is what makes this work against a vendored mngr that
    predates free-form names, exactly as the create path does; the label rides
    the same atomic write as the rename, so no observer sees the renamed agent
    without it.

    Pure: argv assembly only, so the repo<->mngr CLI contract is testable
    against the live CLI without a subprocess (see ``agent_manager_test.py``).
    """
    return [
        mngr_binary,
        "rename",
        agent_id,
        canonical_agent_name(name) or name,
        "--label",
        f"display_name={name}",
    ]


def _rename_failure_detail(cmd: list[str], result: FinishedProcess) -> str:
    """Why a rename subprocess failed, in terms the user can act on.

    ``run_local_command_modern_version`` reports a killed process as a NEGATIVE
    return code carrying the signal number, so the plain "exited with code"
    wording turned our own timeout into "exited with code -15" -- a number that
    says nothing about what happened or what to do next. The timeout is named
    off ``result.is_timed_out`` -- the flag the runner sets exactly when the
    ``timeout`` it was given ran out -- rather than inferred from the SIGTERM
    it sends, so a timeout that had to escalate to SIGKILL still reads as the
    timeout it was, and a signal from anything else (the OOM shedder, say)
    does not. It is the common case by far (see ``_RENAME_TIMEOUT_SECONDS``):
    the rename shells out to the mngr CLI, whose startup alone is seconds, so
    a loaded host reaches the cap without anything being wrong with the name.
    It also outranks whatever partial stderr escaped before the kill, since
    the timeout is the actual cause.

    Its wording deliberately does not promise the rename did not happen. The
    subprocess is stopped partway, and a rename both rewrites the provider's
    persisted agent data and moves the tmux session on a live host, so which of
    those landed is genuinely unknown from here.
    """
    if result.is_timed_out:
        return (
            f"'{cmd[1]}' did not finish within {_RENAME_TIMEOUT_SECONDS:.0f}s and was stopped, "
            "so the new name may or may not have been applied -- reopen the workspace to see which"
        )
    stderr = result.stderr.strip()
    if stderr:
        return stderr
    if result.returncode is not None and result.returncode < 0:
        return f"'{cmd[1]}' was stopped by signal {-result.returncode}"
    return f"'{cmd[1]}' exited with code {result.returncode}"


def _build_chat_display_label_command(mngr_binary: str, agent_id: str, name: str) -> list[str]:
    """Build the ``mngr label`` argv for a display-only rename.

    Used when the new name's canonical form IS the agent's current true name
    (e.g. "chat 2" -> "Chat 2"): only the human-readable half moves, so the
    label is rewritten without renaming anything. Pure (see above).
    """
    return [
        mngr_binary,
        "label",
        agent_id,
        "--label",
        f"display_name={name}",
    ]


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


def _assert_special_kinds_declared(harness: HarnessType, events: list[dict[str, Any]]) -> None:
    """Fail fast when a harness emits a ``special`` kind it never declared.

    ``HarnessSpec.special_kinds`` is the harness's statement of which turn markers its
    parser can produce; ``events.py`` calls an undeclared kind a bug. This is the one
    funnel every harness's events pass through, so checking here is what makes that
    statement enforced rather than documentation. Cheap: only ``special`` events are
    looked at, and the declaration is a frozenset.
    """
    declared = get_harness_spec(harness).special_kinds
    for event in events:
        if event.get("type") != SPECIAL_EVENT_TYPE:
            continue
        kind = event.get("kind")
        if kind not in {k.value for k in declared}:
            raise AssertionError(
                f"{harness.value} emitted undeclared special kind {kind!r} (declared: {sorted(k.value for k in declared)})"
            )


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
    # Derives each app's ``is_running`` (see ``liveness.probe_app_liveness``);
    # injectable so tests control liveness without a supervisord or open ports.
    _liveness_prober: Callable[[str, str], bool]
    # The liveness sweep's own lifecycle: ``_liveness_stop`` ends the loop,
    # ``_liveness_wake`` cuts the current poll interval short (a registry
    # change, a stop/start endpoint) so the next probe lands promptly.
    _liveness_stop: threading.Event
    _liveness_wake: threading.Event
    _liveness_thread: threading.Thread | None
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
    # Per-agent live harness session (``HarnessSpec.session_class``): the control surface that
    # owns the send + its Sending records, tap availability, the native tap/interrupt dispatch,
    # daemon liveness (codex's app-server connection + ledger live inside its session), and the
    # per-agent model option set. Built when activity tracking starts (or on first endpoint
    # touch) and closed when tracking stops. Neither the manager nor the server names a harness
    # around it -- per-harness behavior is the session implementation's.
    _session_by_agent: dict[str, AgentHarnessSession]
    # The alt-harness sign-in preflight (injectable so tests skip the real CLI).
    _auth_gate: Callable[[HarnessAuthCheck | None], str | None]
    # The last computed model choice per agent, and the filesystem watcher that
    # re-derives it when the agent's model_state.json changes. The live read is
    # harness-neutral (the shared reader + the harness's registered state-file path), so
    # there is no per-agent resolver to cache -- the switch endpoint builds one inline.
    # None = the harness has recorded no model yet -> the bar renders no slots.
    _model_choice_by_agent: dict[str, ModelChoice | None]
    _model_watcher_by_agent: dict[str, PathWatcher]
    # Auto-open chats whose open has reached a client (persisted, so a restart
    # never reopens them) and those still owed one because no registered client
    # was there to take it, keyed by id -> chat name. Between them, both
    # discovery paths -- the per-agent delta and the full snapshot -- open each
    # chat exactly once.
    _auto_open_ledger: AutoOpenLedger
    _pending_auto_open_name_by_id: dict[str, str]
    # Re-tags chat agents' OOM ``oom_score_adj`` from live activity: UI presence and
    # messages (via ``record_activity``, from the ``/api/activity`` endpoint),
    # lifecycle changes (via ``record_running_agents``, from the observe stream),
    # and elapsed idle time (via its own slow sweep, started in ``start``). A chat
    # is protected while engaged and climbs past the worker band once it has been
    # left alone long enough.
    _oom_prioritizer: ChatOomPrioritizer
    # Broadcasts committed codex user-turns emitted by a ledger to the agent's transcript stream
    # (the same SSE fan-out the session watcher's events use). The ledger owns live user-turns and
    # the file reader suppresses them (Fix 1), so this is how a ledger-owned user-turn reaches the
    # UI. Set once at composition (``set_transcript_broadcaster``) because the manager is built
    # before the event-queue fan-out exists; ``None`` (tests) makes ledger user-turns a no-op.
    _transcript_broadcaster: Callable[[str, list[dict[str, Any]]], None] | None

    @classmethod
    def build(
        cls,
        broadcaster: WebSocketBroadcaster,
        messenger: MngrMessenger = _DEFAULT_MESSENGER,
        mngr_binary: str = _DEFAULT_MNGR_BINARY,
        auth_gate: Callable[[HarnessAuthCheck | None], str | None] = find_unauthenticated_harness_reason,
        liveness_prober: Callable[[str, str], bool] = probe_app_liveness,
        auto_open_ledger: AutoOpenLedger | None = None,
    ) -> "AgentManager":
        """Build an AgentManager with the given broadcaster.

        ``auto_open_ledger`` remembers which chats' tabs have been surfaced; the
        default keeps that in memory only, so a real server passes one backed
        by the workspace's layout dir.

        ``messenger`` is the agent-messaging collaborator; it defaults to the
        real mngr discover/send. Tests pass one whose ``discover``/``send`` are
        fakes to avoid touching mngr. ``mngr_binary`` is the path or name of the
        mngr executable used for the stream-events observe subprocess and for
        agent-creation commands. ``liveness_prober`` derives one app's
        ``is_running`` from its (program, url); tests inject a fake so liveness
        needs neither a supervisord nor an open port.
        """
        manager = cls.__new__(cls)
        manager._broadcaster = broadcaster
        manager._messenger = messenger
        manager._lock = threading.Lock()
        manager._liveness_prober = liveness_prober
        manager._liveness_stop = threading.Event()
        manager._liveness_wake = threading.Event()
        manager._liveness_thread = None
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
        manager._session_by_agent = {}
        manager._auth_gate = auth_gate
        manager._model_choice_by_agent = {}
        manager._model_watcher_by_agent = {}
        manager._auto_open_ledger = auto_open_ledger if auto_open_ledger is not None else AutoOpenLedger(path=None)
        manager._pending_auto_open_name_by_id = {}
        manager._transcript_broadcaster = None
        # Built last: its ``list_chat_agent_ids`` / ``resolve_process_started_at``
        # callbacks read ``_agents`` / ``_lock`` / ``_host_dir``, which are set above.
        manager._oom_prioritizer = ChatOomPrioritizer(
            list_chat_agent_ids=manager.get_chat_agent_ids,
            resolve_pid=lookup_pid_by_agent_id,
            set_adj=set_oom_score_adj,
            resolve_process_started_at=manager._read_agent_process_started_at,
        )
        return manager

    def start(self) -> None:
        """Start the observe subprocess and perform initial agent discovery.

        Also seeds and starts the OOM prioritizer. Seeding happens before the
        sweep so the first pass ranks chats against their real message history
        rather than treating a restart as "nothing has ever been messaged".
        """
        self._initial_discover()
        self._seed_oom_prioritizer()
        self._oom_prioritizer.start()
        self._start_liveness_sweep()
        self._start_observe()

    def start_without_observe(self) -> None:
        """Start with initial discovery only, no observe subprocess. For testing."""
        self._initial_discover()

    def stop(self) -> None:
        """Stop the observe subprocess, file watchers, and creation threads."""
        self._shutdown_event.set()
        self._oom_prioritizer.stop()

        self._liveness_stop.set()
        self._liveness_wake.set()
        if self._liveness_thread is not None:
            self._liveness_thread.join(timeout=5)
            self._liveness_thread = None

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
            sessions = list(self._session_by_agent.values())
            self._session_by_agent.clear()
            self._activity_tracked_agents.clear()
            self._activity_tracker_by_agent.clear()
            self._activity_state_by_agent.clear()
            self._queued_messages_by_agent.clear()
            self._queue_idle_handler_by_agent.clear()
            self._model_choice_by_agent.clear()
        for session in sessions:
            session.close()

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

    def _seed_oom_prioritizer(self) -> None:
        """Seed the prioritizer's per-chat message times from the client-activity log.

        The prioritizer's own recency state is in-memory, so without this a
        restart of the system interface would forget which chats are in active use
        and start every one of them aging from its process-start time. Quietly
        does nothing when the log is unavailable (a dev/test setup with no layout
        dir, or a workspace where nothing has been messaged yet).
        """
        if not self._own_agent_id:
            return
        layout_dir = projects.primary_agent_layout_dir(self._host_dir, self._own_agent_id)
        events = client_activity.read_client_activity_events(client_activity.get_events_path(layout_dir))
        last_message_at_by_agent_id: dict[str, float] = {}
        for agent_id, timestamp in client_activity.last_message_time_by_agent(events).items():
            messaged_at = parse_iso_timestamp_to_epoch(timestamp)
            if messaged_at is not None:
                last_message_at_by_agent_id[agent_id] = messaged_at
        self._oom_prioritizer.seed_last_message_times(last_message_at_by_agent_id)

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

    def is_agent_alive(self, agent_id: str) -> bool:
        """Whether the agent's process is not POSITIVELY dead.

        Same rule the activity gate uses: everything outside the dead states counts as alive,
        and an unknown/unobservable lifecycle is non-evidence rather than death. An agent we
        have no record of is treated as dead -- the safe direction for the one caller, the
        antigravity flush, which must never resurrect a stopped agent to deliver its queue.
        """
        with self._lock:
            agent_state = self._agents.get(agent_id)
        return agent_state is not None and not is_lifecycle_dead(agent_state.state)

    def send_message_to_agent(self, agent_id: AgentId, message: str) -> SendFailure | None:
        """Send a message to the agent with ``agent_id``, using the live location cache.

        The single entry point for messaging an agent: it reads this manager's
        event-fed location for the id and hands it to the `MngrMessenger`, so the
        message skips a fresh mngr discovery whenever the location is already known.
        Returns None when the message was delivered, or the failure -- the harness's own words
        plus mngr's classification of them, which is what lets the chat decide what to offer.
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

    def rename_chat_agent(self, agent_ref: str, display_name: str) -> None:
        """Give a chat agent the name the user just typed, keeping its name pair matched.

        ``agent_ref`` is what a ``chat:`` member ref carries -- an agent id from
        every UI surface, or an agent name from the agent-facing layout listing --
        so both are resolved here. The display name's canonical form becomes the
        agent's true name and the typed form its ``display_name`` label, the same
        pairing ``mngr create`` establishes. When the canonical form is already
        the agent's name (a display-only change, e.g. "chat 2" -> "Chat 2"),
        only the label is rewritten -- no rename, so nothing embedded in tmux
        sessions or refs moves for a cosmetic change.

        Raises ``AgentRenameError`` when the rename could not be made (so the
        caller can refuse to record the new name anywhere else -- the workspace
        and mngr must never disagree about what a chat is called), and its
        subclass ``AgentNameConflictError`` when the new name collides with
        another agent's (by canonical form; the caller answers 409 so the user
        can retry with a different name).

        An id this manager does not track is not an mngr agent it can rename:
        an agent still being created already carries the typed name on its
        ``mngr create`` (and renaming it to something *else* mid-create would
        race that create, so it is refused), and an id belonging to no agent at
        all has no name to diverge from. Both return without running anything.
        """
        if not canonical_agent_name(display_name):
            raise AgentRenameError(f"Chat name '{display_name}' contains no usable characters")

        with self._lock:
            agent_state = self._agents.get(agent_ref) or next(
                (agent for agent in self._agents.values() if agent.name == agent_ref), None
            )
            proto_agent = self._proto_agents.get(agent_ref)
            taken_names = () if agent_state is None else tuple(self._taken_names_locked(agent_state.id))

        if agent_state is None:
            if proto_agent is not None and str(proto_agent.get("name", "")) != display_name:
                raise AgentRenameError(
                    f"Chat '{agent_ref}' is still being created; it cannot be renamed to '{display_name}' yet"
                )
            if proto_agent is None:
                _loguru_logger.warning("No tracked agent for chat ref {}; leaving mngr alone", agent_ref)
            return

        # The services agent runs the workspace itself; its name is the minds
        # app's to manage (alongside the host's), not a chat tab's.
        if agent_state.labels.get("is_primary") == "true":
            raise AgentRenameError("The workspace's services agent cannot be renamed from a chat tab")

        new_canonical_name = canonical_agent_name(display_name)
        is_display_only = new_canonical_name == agent_state.name
        if not is_display_only and is_name_conflict(display_name, taken_names):
            raise AgentNameConflictError(f"A chat named '{display_name}' already exists; pick another name")

        if is_display_only:
            cmd = _build_chat_display_label_command(self._mngr_binary, agent_state.id, display_name)
        else:
            cmd = _build_chat_rename_command(self._mngr_binary, agent_state.id, display_name)
        try:
            result = run_local_command_modern_version(
                command=cmd,
                cwd=None,
                is_checked=False,
                timeout=_RENAME_TIMEOUT_SECONDS,
            )
        except (OSError, ConcurrencyGroupError) as e:
            _loguru_logger.opt(exception=e).error("Error renaming agent {}", agent_state.id)
            raise AgentRenameError(f"Failed to rename agent '{agent_state.name}': {e}") from e
        if result.returncode != 0:
            raise AgentRenameError(
                f"Failed to rename agent '{agent_state.name}': {_rename_failure_detail(cmd, result)}"
            )

        # Reflect the new name pair immediately rather than waiting for the
        # observe stream to relist, exactly as destroy drops the agent immediately.
        with self._lock:
            renamed = self._agents.get(agent_state.id)
            if renamed is not None:
                self._agents[agent_state.id] = renamed.model_copy_update(
                    to_update(renamed.field_ref().name, new_canonical_name),
                    to_update(renamed.field_ref().labels, {**renamed.labels, "display_name": display_name}),
                )
        self._broadcaster.broadcast_agents_updated(self.get_agents_serialized())

    def get_apps(self) -> list[AppEntry]:
        """Return the primary agent's app list."""
        with self._lock:
            return list(self._apps)

    def get_apps_serialized(self) -> list[dict[str, str | bool]]:
        """Return the primary agent's app list serialized for JSON."""
        with self._lock:
            return [
                {
                    "name": app.name,
                    "url": app.url,
                    "label": app.label,
                    "icon": app.icon,
                    "internal": app.internal,
                    "program": app.program,
                    "is_running": app.is_running,
                }
                for app in self._apps
            ]

    def get_app_by_name(self, app_name: str) -> AppEntry | None:
        """Look up one registered app by its service name."""
        with self._lock:
            for app in self._apps:
                if app.name == app_name:
                    return app
            return None

    def _start_liveness_sweep(self) -> None:
        """Start the background sweep that keeps ``AppEntry.is_running`` honest."""
        thread = threading.Thread(target=self._run_liveness_sweep, daemon=True, name="app-liveness-sweep")
        self._liveness_thread = thread
        thread.start()

    def _run_liveness_sweep(self) -> None:
        while not self._liveness_stop.is_set():
            self.refresh_app_liveness()
            self._liveness_wake.wait(timeout=_LIVENESS_POLL_INTERVAL_SECONDS)
            self._liveness_wake.clear()

    def refresh_app_liveness(self) -> None:
        """Re-derive every app's ``is_running`` and broadcast when any changed.

        The probes run outside the lock (each is a blocking connect or RPC
        call), against a snapshot of the list; results are folded back in by
        name so a registry change mid-probe simply keeps its carried-over state
        until the next pass. Public so the stop/start endpoints can land the
        transition in the same request that caused it.
        """
        with self._lock:
            probe_targets = [(app.name, app.program, app.url) for app in self._apps]
        is_running_by_name = {name: self._liveness_prober(program, url) for name, program, url in probe_targets}
        is_changed = False
        with self._lock:
            updated_apps: list[AppEntry] = []
            for app in self._apps:
                probed = is_running_by_name.get(app.name)
                if probed is None or probed == app.is_running:
                    updated_apps.append(app)
                else:
                    updated_apps.append(app.model_copy_update(to_update(app.field_ref().is_running, probed)))
                    is_changed = True
            self._apps = updated_apps
        if is_changed:
            self._broadcaster.broadcast_apps_updated(self.get_apps_serialized())

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
        """Return agent list serialized for JSON.

        ``project`` is lifted out of the labels because it is the chat-to-project
        linkage the workspace UI reads on every agent it lists (see
        ``_chat_project_label``); null means the chat was created inside no
        project, which Everything lists all the same.

        ``display_name`` is lifted for the same reason: it is the human-readable
        name mngr holds for the agent, and ``name`` is its canonical form, so the
        UI can show what the user typed while still addressing the agent by
        ``name``. Null means mngr has no such label and ``name`` is all there is.
        """
        with self._lock:
            return [
                {
                    "id": a.id,
                    "name": a.name,
                    "state": a.state,
                    "labels": a.labels,
                    "project": a.labels.get("project"),
                    "display_name": a.labels.get("display_name"),
                    "work_dir": a.work_dir,
                    "harness": a.harness,
                    "activity_state": a.activity_state,
                    "model_choice": a.model_choice.model_dump() if a.model_choice else None,
                    "queued_messages": [queued.model_dump() for queued in a.queued_messages],
                    # Backend-computed shoulder-tap availability (contract Shoulder-tap): available
                    # iff something is queued AND no send is in flight. Derived here from the
                    # authoritative live state rather than stored on the item, so it can never go
                    # stale against it. The frontend renders/greys the button from this alone -- it
                    # computes nothing and there is no error path.
                    "shoulder_tap_available": self._shoulder_tap_available(a),
                }
                for a in self._agents.values()
            ]

    def _shoulder_tap_available(self, agent_state: AgentStateItem) -> bool:
        """Whether the shoulder-tap button is offered for ``agent_state`` (contract Shoulder-tap).

        The agent's session answers: the shared rule is queued AND nothing Sending; a
        live-connection session (codex) reads its own ledger, which also GREYS the button
        through the interrupt+resend of a tap (the re-sent chips are Sending). No session yet
        (tracking not started) means nothing queued and nothing to tap. Caller holds ``_lock``;
        the session's reads are leaf-locked/lock-free, so there is no ordering between the two.
        """
        session = self._session_by_agent.get(agent_state.id)
        if session is None:
            return False
        return session.is_tap_available(has_queued=bool(agent_state.queued_messages))

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

    def _taken_names_locked(self, exclude_agent_id: str | None = None) -> list[str]:
        """Every name in use on the machine's agents. Must be called with lock held.

        Both halves of each agent's name pair count (the ``display_name`` label
        and the true name), plus every in-flight create's name, so a fresh
        allocation or a rename can collide with neither. ``exclude_agent_id``
        leaves one agent out, which is how a rename avoids colliding with the
        agent being renamed.
        """
        taken: list[str] = []
        for agent in self._agents.values():
            if agent.id == exclude_agent_id:
                continue
            taken.append(agent.name)
            display_label = agent.labels.get("display_name")
            if display_label:
                taken.append(display_label)
        for proto_agent_id, proto in self._proto_agents.items():
            if proto_agent_id == exclude_agent_id:
                continue
            proto_name = str(proto.get("name", ""))
            if proto_name:
                taken.append(proto_name)
        return taken

    def create_chat_agent(
        self,
        requested_name: str,
        harness: HarnessType = HarnessType.CLAUDE,
        extra_role_templates: tuple[str, ...] = (),
        project_id: str = "",
        extra_taken_names: tuple[str, ...] = (),
    ) -> CreatedChatAgent:
        """Create a chat agent in the primary agent's work dir on the given harness.

        Returns the pre-generated agent ID together with the chat's name pair: the
        human-readable display name and its canonical true name (see
        ``imbue.system_interface.naming``). An empty ``requested_name`` mints the
        first free "<word> N" for the harness ("Chat 1", "Codex 2", ...) here,
        server-side, under the same lock that registers the in-flight create --
        so two simultaneous creates cannot both mint "Chat 1".
        ``extra_taken_names`` widens the taken set beyond the machine's agents
        (the caller passes the member-title store's chosen names, so a terminal
        someone renamed to "Chat 2" blocks that slot too).

        ``harness`` is also the name of the harness create template it stacks; the
        `chat` role template supplies everything else, so a new harness needs no new
        method here. ``project_id`` is the project the chat was created inside, which
        becomes the agent's ``project`` label -- the project it starts out filed in
        (see ``_chat_project_label``); empty keeps the primary agent's inherited label.

        Raises ``AgentNameConflictError`` when an explicitly requested name collides
        with an existing agent or an in-flight create (by canonical form -- the same
        collision mngr itself would reject).

        An alt harness authenticates through its own CLI; if that CLI is signed out, refuse
        the create up front (raising ``AgentCreationError``) rather than launch a chat that
        can never take a turn. Claude is not gated -- its auth is the shared workspace login.
        """
        unauthenticated_reason = self._auth_gate(get_harness_spec(harness).auth_check)
        if unauthenticated_reason is not None:
            raise AgentCreationError(unauthenticated_reason)

        explicit_name = requested_name.strip()
        if explicit_name and not canonical_agent_name(explicit_name):
            raise AgentCreationError(f"Chat name '{explicit_name}' contains no usable characters")

        agent_id = str(AgentId())
        log_queue: queue.Queue[str | None] = queue.Queue(maxsize=10000)

        # Name resolution and proto registration happen under one lock hold, so a
        # concurrent create sees this one's name as taken (and vice versa).
        with self._lock:
            work_dir = self._resolve_agent_work_dir(self._own_agent_id)
            if work_dir is None:
                raise AgentCreationError(f"Cannot determine work directory for primary agent {self._own_agent_id}")
            primary = self._agents.get(self._own_agent_id)
            primary_labels = dict(primary.labels) if primary else {}

            taken_names = self._taken_names_locked() + list(extra_taken_names)
            if explicit_name:
                if is_name_conflict(explicit_name, taken_names):
                    raise AgentNameConflictError(f"A chat named '{explicit_name}' already exists; pick another name")
                display_name = explicit_name
            else:
                display_name = first_free_numbered_name(AUTO_NAME_WORD_BY_HARNESS[harness], taken_names)

            proto_info = {
                "agent_id": agent_id,
                "name": display_name,
                "creation_type": CHAT_CREATION_TYPE,
                "parent_agent_id": None,
            }
            self._proto_agents[agent_id] = proto_info
            self._log_queues[agent_id] = log_queue

        cmd = _build_chat_create_command(
            self._mngr_binary,
            display_name,
            agent_id,
            primary_labels,
            harness,
            extra_role_templates,
            project_id,
        )

        self._broadcaster.broadcast_proto_agent_created(
            agent_id=agent_id,
            name=display_name,
            creation_type=CHAT_CREATION_TYPE,
            parent_agent_id=None,
        )

        # Mirror the labels the created mngr agent will carry (see
        # ``_build_chat_create_command``), so the pre-observe AgentStateItem below
        # renders exactly like the observed agent will.
        labels: dict[str, str] = {"user_created": "true", "display_name": display_name}
        project_label = _chat_project_label(primary_labels, project_id)
        if project_label:
            labels["project"] = project_label
        canonical_name = canonical_agent_name(display_name)
        self._launch_creation_thread(agent_id, canonical_name, cmd, Path(work_dir), log_queue, labels, harness)

        return CreatedChatAgent(agent_id=agent_id, name=canonical_name, display_name=display_name)

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
            self._seed_auto_opens_at_startup(agents)

            for agent_info in agents:
                if agent_info.id == self._own_agent_id and agent_info.work_dir:
                    self._start_app_watcher(agent_info.id, Path(agent_info.work_dir))
                self._ensure_activity_tracking(agent_info.id)
                self._ensure_model_tracking(agent_info.id)
        except (OSError, ValueError, RuntimeError, MngrError) as e:
            _loguru_logger.opt(exception=e).error("Initial agent discovery failed")

    def _seed_auto_opens_at_startup(self, agents: list[AgentInfo]) -> None:
        """Queue or record the auto-open of every labeled chat found at startup.

        The decisions are taken under the manager lock (they write the pending
        map); the ledger writes they imply are file I/O and happen after it is
        released.
        """
        with self._lock:
            ids_to_record_as_delivered = [
                agent_info.id
                for agent_info in agents
                if _is_auto_open_labeled(agent_info.labels) and self._seed_auto_open_at_startup(agent_info)
            ]
        for agent_id in ids_to_record_as_delivered:
            self._auto_open_ledger.mark_delivered(agent_id)

    def _seed_auto_open_at_startup(self, agent_info: AgentInfo) -> bool:
        """Decide what a labeled chat found at startup is owed: its open, or nothing.

        Returns whether the chat must now be recorded as delivered; the caller
        writes that outside the lock this runs under.

        A restart normally restores the saved layout rather than reopening
        tabs, so a chat already delivered stays as the user left it. The one
        chat still owed its open is a recent one that was never delivered
        anywhere -- an update run started while no client was connected, whose
        apply then restarted this interface before anyone looked. Everything
        else is recorded as delivered, which is also how a chat surfaced by an
        interface that kept no ledger is kept from popping again.
        """
        if self._auto_open_ledger.is_delivered(agent_info.id):
            return False
        is_fresh = (
            agent_info.create_time is not None
            and datetime.now(timezone.utc) - agent_info.create_time < _AUTO_OPEN_STARTUP_FRESHNESS
        )
        if is_fresh and agent_info.state in RUNNING_LIFECYCLE_STATES:
            self._pending_auto_open_name_by_id[agent_info.id] = agent_info.name
            return False
        return True

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
            with self._lock:
                self._pending_auto_open_name_by_id.pop(agent_id, None)
            self._auto_open_ledger.forget(agent_id)

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

        # Hand the OOM prioritizer the current mid-turn set. This is its only view
        # of a chat messaged outside the workspace UI (by mngr or another agent):
        # entering a running state is the observable consequence of such a message,
        # and it keeps a chat exempt from its staleness climb for the turn's
        # duration. After the broadcast because it writes to /proc, which the UI
        # update should not wait on.
        self._oom_prioritizer.record_running_agents(
            [agent_id for agent_id, agent in new_agents.items() if agent.state in RUNNING_LIFECYCLE_STATES]
        )

        # A newly-created chat usually surfaces as a freshly-added agent here, so
        # surface the labeled chats that have appeared. ``_maybe_auto_open``
        # dedupes, so a chat already delivered or already owed is not reopened.
        for agent_id in added_agent_ids:
            appeared_agent_state = new_agents.get(agent_id)
            if appeared_agent_state is not None:
                self._maybe_auto_open(appeared_agent_state)

    def _maybe_auto_open(self, agent_state: AgentStateItem) -> None:
        """Surface ``agent_state``'s tab if it is a labeled chat not yet delivered.

        Delivered means a registered client was there to take the open: one
        that has reported its ``client_state``, which the frontend sends once
        its dock is mounted and able to apply ops. With no such client the open
        is held until one registers (``flush_pending_auto_opens``), rather than
        broadcast to nobody and lost -- which is what happens to a chat created
        from outside the workspace, before the user's frame has connected.
        """
        if not _is_auto_open_labeled(agent_state.labels):
            return
        if self._auto_open_ledger.is_delivered(agent_state.id):
            return
        with self._lock:
            if agent_state.id in self._pending_auto_open_name_by_id:
                return
            is_client_registered = len(self._broadcaster.get_connected_client_infos()) > 0
            if not is_client_registered:
                self._pending_auto_open_name_by_id[agent_state.id] = agent_state.name
                return
        self._deliver_auto_open(agent_state.id, agent_state.name)

    def flush_pending_auto_opens(self) -> None:
        """Surface every chat still owed its tab; called when a client registers."""
        with self._lock:
            pending = list(self._pending_auto_open_name_by_id.items())
            self._pending_auto_open_name_by_id.clear()
        for agent_id, name in pending:
            self._deliver_auto_open(agent_id, name)

    def _deliver_auto_open(self, agent_id: str, name: str) -> None:
        self._auto_open_ledger.mark_delivered(agent_id)
        self._broadcaster.broadcast_layout_op(
            op="open",
            args={"ref": f"chat:{name}"},
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
            # The create path calls this before the observe stream has reported the agent, so
            # the harness can be the DEFAULT guess; the tracker and session below both heal on
            # the next call once the real harness is known.
            harness = agent_state.harness if agent_state is not None else DEFAULT_HARNESS
            # Every harness -- codex included -- builds its transcript-derived tracker here, from
            # the agent's harness. codex's dot is its tracker's turn latch; its ledger (inside
            # the session below) owns only the queue + message-lifecycle chips.
            tracker = self._activity_tracker_by_agent.get(agent_id)
            if tracker is None or type(tracker) is not get_harness_spec(harness).tracker_class:
                self._activity_tracker_by_agent[agent_id] = build_tracker(harness)
        session = self._get_or_heal_session(agent_id, harness)
        # Bring up whatever live backend the harness needs (codex's app-server connection;
        # a no-op for file harnesses). Blocking I/O, so outside the lock; idempotent, so the
        # observe tick re-invoking it is the self-healing retry path.
        session.ensure_live()
        self._recompute_activity_state(agent_id, broadcast_on_change=False)

    def _stop_activity_tracking(self, agent_id: str) -> None:
        """Stop activity tracking and clear cached activity + queued state.

        The session is QUIESCED (its live backend reaped), not destroyed: a transient
        discovery blip must not lose the Sending records an in-flight send is holding --
        the same lifetime the watcher registry has (``SystemInterfaceState.watchers`` is
        never popped either). Terminal teardown happens in :meth:`stop`.
        """
        with self._lock:
            session = self._session_by_agent.get(agent_id)
            self._activity_tracked_agents.discard(agent_id)
            self._activity_tracker_by_agent.pop(agent_id, None)
            self._activity_state_by_agent.pop(agent_id, None)
            self._queued_messages_by_agent.pop(agent_id, None)
            self._queue_idle_handler_by_agent.pop(agent_id, None)
        # Reap the live backend outside the lock (codex's join blocks on its reader thread);
        # idempotent, and a re-track rebuilds it via ensure_live.
        if session is not None:
            session.on_lifecycle_dead()

    def _build_session(self, agent_id: str, harness: HarnessType) -> AgentHarnessSession:
        """Build the harness session for one agent, binding every capability it may need.

        The one place session dependencies are assembled: registry dispatch, the send/notify
        callbacks, and the codex connection fan-outs all bind here, so the session modules
        never import the registry or the manager.
        """
        state_dir = self._get_agent_state_dir(agent_id)
        spec = get_harness_spec(harness)
        deps = SessionDeps(
            harness=harness,
            state_dir=state_dir,
            send_to_harness=lambda text: delivered_or_raise(self.send_message_to_agent(AgentId(agent_id), text)),
            notify_agents_changed=lambda: self._broadcaster.broadcast_agents_updated(self.get_agents_serialized()),
            is_tracked=lambda: self.is_activity_tracked(agent_id),
            on_queue_snapshot=lambda snapshot: self.update_queued_messages(agent_id, snapshot),
            on_user_turn=lambda event: self._broadcast_codex_user_turn(agent_id, event),
            recompute_activity=lambda: self._recompute_activity_state(agent_id, broadcast_on_change=True),
            clear_queue_state=lambda: self._clear_queue_state(agent_id),
            catalog_options=lambda: get_catalog(harness).options,
            build_interrupter=build_interrupt_to_composer,
            build_shoulder_tap=build_shoulder_tap,
            model_state_path=get_model_state_path(harness, state_dir),
        )
        return spec.session_class.build(deps)

    def get_or_create_session(self, agent_info: AgentInfo) -> AgentHarnessSession:
        """The agent's live harness session, built on first touch (the endpoint entry point).

        Idempotent and cheap (a build does no I/O; liveness is ``ensure_live``'s job), so a
        request landing before the observe tick starts tracking still gets a working session.
        """
        return self._get_or_heal_session(agent_info.id, agent_info.harness)

    def _get_or_heal_session(self, agent_id: str, harness: HarnessType) -> AgentHarnessSession:
        """The ONE insertion point into ``_session_by_agent``, self-healing on harness.

        Tracking can start before the observe stream has told us an agent's harness (the
        create path calls it immediately), in which case the session is built for the
        DEFAULT harness. The old per-request ``agent_info.harness`` dispatch healed that on
        the next endpoint touch; this preserves that property -- a cached session built for
        the wrong harness is replaced the first time a caller shows up knowing the real one.
        """
        with self._lock:
            existing = self._session_by_agent.get(agent_id)
            if existing is not None and existing.harness == harness:
                return existing
            session = self._build_session(agent_id, harness)
            self._session_by_agent[agent_id] = session
        # A mismatched predecessor is torn down outside the lock (codex join blocks).
        if existing is not None:
            existing.close()
        return session

    def is_activity_tracked(self, agent_id: str) -> bool:
        """Whether activity tracking is live for ``agent_id`` (sessions gate connects on it)."""
        with self._lock:
            return agent_id in self._activity_tracked_agents

    def _clear_queue_state(self, agent_id: str) -> None:
        """Drop an agent's cached queue chips (its ephemeral queue died with its daemon).

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

    def set_transcript_broadcaster(self, broadcaster: Callable[[str, list[dict[str, Any]]], None]) -> None:
        """Wire the transcript-event fan-out (the composition root calls this once).

        The manager is built before the event-queue fan-out exists, so the codex ledger's live
        user-turn broadcast (Fix 1) is injected here rather than at ``build``. A codex agent's
        ledger emits each committed user-turn through :meth:`_broadcast_codex_user_turn`, which
        routes to this."""
        self._transcript_broadcaster = broadcaster

    def _broadcast_codex_user_turn(self, agent_id: str, event: dict[str, Any]) -> None:
        """Broadcast one ledger-owned committed user-turn to the agent's transcript stream.

        Fired from the ledger (its reader thread, or a send/interrupt request thread) after it has
        removed the message's chip -- the A3b ordered handoff. A no-op when no broadcaster is wired
        (tests) so the ledger stays independently testable."""
        if self._transcript_broadcaster is None:
            return
        self._transcript_broadcaster(agent_id, [event])

    def _model_options_for(self, agent_state: AgentStateItem) -> tuple[ModelOption, ...]:
        """The option set an agent's live identity matches against (chip-match + switch-validation).

        The session answers: the static harness catalog for file harnesses, the ONE reconciled
        per-agent set for codex (seeded on connect, refreshed by each picker-open, falling back
        to the persisted sidecar -- see ``CodexHarnessSession.switch_options``). Never holds
        ``_lock`` while asking (a codex fallback reads the sidecar off disk), so callers must
        not invoke it while holding the lock. No session yet falls back to the static catalog.
        """
        with self._lock:
            session = self._session_by_agent.get(agent_state.id)
        if session is None:
            return get_catalog(agent_state.harness).options
        return session.switch_options()

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
        registered ``model_state.json`` -- so there is nothing to build per agent;
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
        # The disk read (model_state.json) stays outside the lock. Only harness +
        # state dir are needed -- not claude_config_dir, which would cost an env-file read.
        identity = read_model_identity(
            get_model_state_path(harness_state.harness, self._get_agent_state_dir(agent_id))
        )
        # The match SOURCE is per-agent for a dynamic harness (codex): its options come from the
        # cached model/list, not a static catalog. Computed OUTSIDE the lock (it takes the lock
        # itself), then matched below -- the matcher, read, and broadcast are otherwise unchanged.
        options = self._model_options_for(harness_state)
        with self._lock:
            agent_state = self._agents.get(agent_id)
            if agent_state is None:
                return
            # identity is None when the harness has recorded no model yet (e.g. before a
            # session's first statusline fire, or a remote agent) -> no choice, no slots.
            if identity is None:
                choice: ModelChoice | None = None
            else:
                choice = resolve_model_choice(identity, options)
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

    def _read_agent_process_started_at(self, agent_id: str) -> float | None:
        """Return the agent's process-start mtime, resolving its marker by harness.

        The OOM prioritizer knows only an agent id, but the marker filename is
        harness-specific (see ``_read_process_started_at``), so it comes from the
        agent's ``HarnessSpec`` -- harness identity, known as soon as the agent is
        known. This deliberately does NOT ask the agent's activity tracker: a
        tracker is an instance registered by ``_ensure_activity_tracking``, which
        skips any agent with no local state dir and has not necessarily run for a
        just-discovered agent, so the prioritizer silently lost its aging for
        exactly the agents it most needs to age. Returns ``None`` only when the
        agent itself is unknown.
        """
        # Lock-free ``dict.get`` (atomic under the GIL), matching what this method did
        # before: it is injected as a callback into the OOM prioritizer and so can be
        # invoked from a thread that already holds ``_lock``, which is not reentrant.
        agent_state = self._agents.get(agent_id)
        if agent_state is None:
            return None
        marker_filename = get_harness_spec(agent_state.harness).process_started_marker_filename
        return self._read_process_started_at(agent_id, marker_filename)

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
        # A positively-dead lifecycle is the one signal a live backend cannot self-observe (an
        # abrupt daemon kill emits no idle sweep), so tell the session -- level-triggered on
        # every recompute and idempotent (codex reaps its connection + ephemeral queue chips;
        # a file session has nothing to drop). The tracker path below then settles the dot to
        # IDLE via the dead override.
        if recompute_agent_state is not None and is_lifecycle_dead(recompute_agent_state.state):
            with self._lock:
                dead_session = self._session_by_agent.get(agent_id)
            if dead_session is not None:
                dead_session.on_lifecycle_dead()
        if tracker is None:
            return
        # Re-read on every recompute so a restart that touches the marker is
        # reflected even when no new transcript events arrive -- the post-restart
        # observe snapshot drives the recompute.
        process_started_at = self._read_process_started_at(agent_id, tracker.marker_filename)
        # The turn-in-flight marker flips promptly at turn start/end, whereas the observe-reported
        # lifecycle state can miss a short turn -- so read it for a timely signal (stat outside the
        # lock). The tracker declares which file that is; ``None`` = the harness keeps no marker
        # (codex -- its daemon is the turn authority) and the lifecycle state stands alone.
        active_marker_filename = tracker.active_marker_filename
        is_active_marker_present = (
            active_marker_filename is not None
            and (self._get_agent_state_dir(agent_id) / active_marker_filename).exists()
        )
        with self._lock:
            if agent_id not in self._activity_tracked_agents:
                return
            agent_state = self._agents.get(agent_id)
            if agent_state is None:
                return
            # The universal gates (dead lifecycle -> IDLE, stale tail -> IDLE) are the base
            # tracker's own first steps -- structural, not a caller-side override -- and a dead
            # agent's IDLE still fires the level-triggered stale-queue sweep below.
            new_state = tracker.derive(
                lifecycle_state=agent_state.state,
                is_active_marker_present=is_active_marker_present,
                process_started_at=process_started_at,
            )
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
            agent_state = self._agents.get(agent_id)
            if agent_state is not None:
                _assert_special_kinds_declared(agent_state.harness, events)
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
        """Read and parse data/.state/apps.toml for the primary agent.

        ``is_running`` is derived state rather than registry state, so each
        re-read carries the previous probe result forward (new rows default to
        running) and nudges the liveness sweep to re-probe promptly.
        """
        apps: list[AppEntry] = []
        if toml_path.exists():
            try:
                data = tomllib.loads(toml_path.read_text())
                for entry in data.get("apps", []):
                    name = entry.get("name", "")
                    url = entry.get("url", "")
                    label = entry.get("label", "")
                    icon = _accepted_icon(str(entry.get("icon", "")))
                    internal = bool(entry.get("internal", False))
                    program = str(entry.get("program", ""))
                    if name and url:
                        apps.append(
                            AppEntry(
                                name=name,
                                url=url,
                                label=label,
                                icon=icon,
                                internal=internal,
                                program=program,
                            )
                        )
            except (OSError, tomllib.TOMLDecodeError, KeyError, ValueError) as e:
                _loguru_logger.opt(exception=e).error("Failed to parse {}", toml_path)

        with self._lock:
            previous_is_running_by_name = {app.name: app.is_running for app in self._apps}
            self._apps = [
                app.model_copy_update(to_update(app.field_ref().is_running, previous_is_running_by_name[app.name]))
                if app.name in previous_is_running_by_name
                else app
                for app in apps
            ]
        self._liveness_wake.set()
