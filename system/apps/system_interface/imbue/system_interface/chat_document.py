"""The chat document: the chat pages, their API, and the chat app's instances API, as a Flask app of its own.

Phase 6 of the workspace app model (``docs/system/blueprint/workspace-app-model/phase_06_chat_as_document.md``)
splits the chat out of the shell's document: every chat renders inside an iframe at the registered
``chat`` origin, served by this app from the same process the shell runs in. ``wsgi_dispatch``
picks this app for the paths only it serves; phase 10 moves it into its own process.

The routes here moved verbatim from ``server.py``; what is new is the document route, the
presence route, and the instances blueprint.
"""

import json
import os
import queue
import time
from collections.abc import Callable
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from typing import Final
from uuid import uuid4

from app_instances.blueprint import answer_typed_error
from app_instances.blueprint import build_instances_blueprint
from app_instances.blueprint import parse_request_body
from app_instances.errors import AppInstancesError
from flask import Flask
from flask import Response
from flask import request
from flask import send_file
from loguru import logger as _loguru_logger
from simple_websocket import ConnectionClosed

from imbue.concurrency_group.subprocess_utils import run_local_command_modern_version
from imbue.mngr.errors import MngrError
from imbue.mngr.primitives import AgentId
from imbue.system_interface import accounts_endpoints
from imbue.system_interface import client_activity
from imbue.system_interface import latchkey_endpoints
from imbue.system_interface import projects
from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.agent_discovery import SendFailedError
from imbue.system_interface.agent_discovery import start_agent
from imbue.system_interface.agent_manager import AgentManager
from imbue.system_interface.app_context import SystemInterfaceState
from imbue.system_interface.app_context import attach_state
from imbue.system_interface.app_context import get_state
from imbue.system_interface.attachments import delete_upload
from imbue.system_interface.attachments import get_uploads_directory
from imbue.system_interface.attachments import resolve_upload_path
from imbue.system_interface.attachments import store_uploaded_file
from imbue.system_interface.chat_instances import AGENT_ID_PATTERN
from imbue.system_interface.chat_instances import SUBAGENT_KEY_SEPARATOR
from imbue.system_interface.chat_instances import build_chat_instance_source
from imbue.system_interface.chat_instances import taken_member_titles
from imbue.system_interface.config import Config
from imbue.system_interface.documents import document_response
from imbue.system_interface.documents import inject_base_path_meta_tag
from imbue.system_interface.documents import inject_chat_identity_meta_tags
from imbue.system_interface.documents import inject_hostname_meta_tag
from imbue.system_interface.documents import inject_plugin_script_tags
from imbue.system_interface.documents import inject_primary_agent_id_meta_tag
from imbue.system_interface.event_queues import AgentEventQueues
from imbue.system_interface.harnesses.claude import auth_endpoints
from imbue.system_interface.harnesses.interrupt import restart_drain
from imbue.system_interface.harnesses.model import ModelIdentity
from imbue.system_interface.harnesses.model import ModelOption
from imbue.system_interface.harnesses.registry import HARNESS_SPECS
from imbue.system_interface.harnesses.registry import build_resolver
from imbue.system_interface.harnesses.registry import get_catalog
from imbue.system_interface.harnesses.registry import get_harness_spec
from imbue.system_interface.harnesses.session import AgentHarnessSession
from imbue.system_interface.harnesses.session import SendOutcome
from imbue.system_interface.harnesses.session_watcher import AgentSessionWatcher
from imbue.system_interface.models import AgentCreationError
from imbue.system_interface.models import AgentNameConflictError
from imbue.system_interface.models import AgentRestartError
from imbue.system_interface.models import AttachmentError
from imbue.system_interface.models import AttachmentUploadResponse
from imbue.system_interface.models import CreateAgentResponse
from imbue.system_interface.models import CreateChatRequest
from imbue.system_interface.models import DrainToComposerResponse
from imbue.system_interface.models import ErrorResponse
from imbue.system_interface.models import FastModePromptAnsweredResponse
from imbue.system_interface.models import InterruptAgentResponse
from imbue.system_interface.models import ModelOptionsResponse
from imbue.system_interface.models import PoweredByResponse
from imbue.system_interface.models import SendMessageRequest
from imbue.system_interface.models import SendMessageResponse
from imbue.system_interface.models import SetModelChoiceRequest
from imbue.system_interface.models import ShoulderTapAtomicResponse
from imbue.system_interface.presence import PresenceReport
from imbue.system_interface.request_helpers import handle_unhandled_exception
from imbue.system_interface.request_helpers import json_response
from imbue.system_interface.request_helpers import parse_json_object_body
from imbue.system_interface.wsgi import build_sock

logger = _loguru_logger

# The vite build's chat entry, beside the shell's index.html in the static directory.
CHAT_DOCUMENT_FILENAME: Final[str] = "chat.html"

# What the chat origin answers when the bundle is missing: the shell's placeholder carries the
# repair story, and a chat frame is never the page a reader is looking at on its own.
_CHAT_NOT_BUILT_HTML: Final[str] = (
    '<!doctype html><html><head><meta charset="utf-8"><title>Chat</title></head>'
    "<body><p>This workspace's chat interface is not built yet.</p></body></html>"
)


def _find_agent(agent_id: str) -> AgentInfo | None:
    """Find a specific agent by ID, from the AgentManager's already-loaded state."""
    agent_manager: AgentManager = get_state().agent_manager
    return agent_manager.get_agent_info_by_id(agent_id)


def _agent_not_found_response(agent_id: str) -> Response:
    error = ErrorResponse(detail=f"Agent '{agent_id}' not found")
    return json_response(error.model_dump(), status_code=404)


def _primary_agent_layout_dir() -> Path | None:
    return projects.primary_agent_layout_dir_from_env()


def _client_activity_events_path() -> Path | None:
    """Where the workspace-level client-activity event log lives, or None."""
    layout_dir = _primary_agent_layout_dir()
    if layout_dir is None:
        return None
    return client_activity.get_events_path(layout_dir)


# Default number of events for tail-first loading
_DEFAULT_TAIL_COUNT = 50


# `mngr label` is a metadata write (data.json merge), fast even on a busy host.
_LABEL_TIMEOUT_SECONDS = 30.0


def _get_event_detail(agent_id: str, event_id: str) -> Response:
    """The full deferred payloads for one event: tool input(s), tool output, thinking.

    Resident events are payload-free (the wire contract in ``harnesses/events``); this is
    the on-demand read behind expanding a tool row or a thinking disclosure. The read is
    stateless -- the watcher re-reads the source line (or re-queries agy's store) and
    nothing is cached backend-side; only the frontend may cache what it fetched. When the
    recorded byte range went stale the watcher falls back to scanning the source for the
    event's own identity; only if that also fails does this answer 404, which the frontend
    renders as a quiet "payload no longer available" placeholder.
    """
    agent_info = _find_agent(agent_id)
    if agent_info is None:
        return _agent_not_found_response(agent_id)
    watcher = get_state().get_or_create_watcher(agent_info)
    detail = watcher.get_event_detail(event_id)
    if detail is None:
        error = ErrorResponse(detail=f"Payload for event '{event_id}' is no longer available")
        return json_response(error.model_dump(), status_code=404)
    return json_response({"event_id": event_id, **detail})


def _get_events(agent_id: str) -> Response:
    """Get events for an agent. Supports tail-first loading and backfill."""
    agent_info = _find_agent(agent_id)
    if agent_info is None:
        return _agent_not_found_response(agent_id)

    before_event_id = request.args.get("before")
    after_event_id = request.args.get("after")
    offset_str = request.args.get("offset")
    limit_str = request.args.get("limit", str(_DEFAULT_TAIL_COUNT))
    try:
        limit = int(limit_str)
    except ValueError:
        limit = _DEFAULT_TAIL_COUNT
    # A non-positive limit would defeat the window cap and break slicing, so fall
    # back to the default.
    if limit <= 0:
        limit = _DEFAULT_TAIL_COUNT

    watcher = get_state().get_or_create_watcher(agent_info)
    if before_event_id:
        # Page older: the `limit` events immediately before the cursor.
        events = watcher.get_backfill_events(before_event_id, limit=limit)
    elif after_event_id:
        # Page newer: the `limit` events immediately after the cursor (used when
        # the loaded window has been moved off the live tail by a jump).
        events = watcher.get_forward_events(after_event_id, limit=limit)
    elif offset_str is not None:
        # Jump: a `limit`-event window starting at an arbitrary global index, so
        # the client can land at a far scroll position in one bounded read.
        try:
            offset = int(offset_str)
        except ValueError:
            offset = 0
        events = watcher.get_events_at_offset(offset, limit)
    else:
        # Initial load: the newest `limit` events (the live tail). Bounded read
        # from the end; the client pages/jumps from here.
        events = watcher.get_tail_events(limit)

    # `total` is the full transcript length and `offset` is the global index of the
    # first returned event. Together they place the loaded window in the whole
    # conversation, so the client sizes the scrollbar for the full length and
    # derives whether more history exists above (offset > 0) and below
    # (offset + len < total) -- no separate has_more flag needed.
    total = watcher.get_total_event_count()
    offset = watcher.get_event_offset(events[0]["event_id"]) if events else total
    return json_response({"events": events, "offset": offset, "total": total})


def _stream_filtered_events(
    agent_id: str,
    event_queues: AgentEventQueues,
    event_queue: "queue.Queue[dict[str, Any] | None]",
    should_forward: Callable[[dict[str, Any]], bool],
) -> Iterator[str]:
    """Yield SSE frames for queued events that pass ``should_forward``.

    Shared by the main agent stream and the per-subagent stream, which differ
    only in which events they keep: the main stream drops subagent-session
    events (they belong to the per-subagent stream, and would otherwise render
    the subagent's own prompt and tool calls inline in the parent thread),
    while the subagent stream keeps only its own session. Filtered-out events
    do not reset the keepalive counter. A ``None`` from the queue (shutdown
    sentinel) ends the stream.
    """
    keepalive_counter = 0
    _loguru_logger.info("SSE stream opened for agent {} (conn {})", agent_id, id(event_queue))
    close_reason = "event-queues shutdown"
    try:
        while not event_queues.is_shutdown:
            try:
                event = event_queue.get(timeout=1)
                if event is None:
                    close_reason = "queue shutdown sentinel"
                    break
                if not should_forward(event):
                    continue
                keepalive_counter = 0
                yield f"data: {json.dumps(event)}\n\n"
            except queue.Empty:
                keepalive_counter += 1
                if keepalive_counter >= 8:
                    keepalive_counter = 0
                    yield ": keepalive\n\n"
    except GeneratorExit:
        close_reason = "client disconnected"
    finally:
        _loguru_logger.info(
            "SSE stream closed for agent {} (conn {}, reason: {})", agent_id, id(event_queue), close_reason
        )
        event_queues.unregister(agent_id, event_queue)


def _sse_response(generator: Iterator[str]) -> Response:
    return Response(
        generator,
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _stream_events(agent_id: str) -> Response:
    """SSE stream for an agent's new events."""
    agent_info = _find_agent(agent_id)
    if agent_info is None:
        return _agent_not_found_response(agent_id)

    state = get_state()
    watcher = state.get_or_create_watcher(agent_info)

    event_queues = state.event_queues
    event_queue = event_queues.register(agent_id)

    return _sse_response(_stream_filtered_events(agent_id, event_queues, event_queue, watcher.is_main_session_event))


# A NOT_READY send's revive budget. ``start_agent`` returns once mngr has launched the
# session WITHOUT awaiting the daemon handshake (codex readiness is only awaited on
# create), so the daemon needs a few more seconds before the session can connect.
_REVIVE_RETRY_INTERVAL_SECONDS: Final[float] = 0.5


_REVIVE_RETRY_BUDGET_SECONDS: Final[float] = 15.0


def _revive_and_retry_send(
    agent_info: AgentInfo,
    agent_manager: AgentManager,
    session: AgentHarnessSession,
    send_message_request: SendMessageRequest,
    message_id: str,
    sleep: Callable[[float], None] = time.sleep,
    budget_seconds: float = _REVIVE_RETRY_BUDGET_SECONDS,
) -> SendOutcome:
    """Start a not-ready agent and retry the send, giving every harness the revive invariant.

    The file-session harnesses auto-start a STOPPED agent inside mngr's own send
    (``is_start_desired``) -- "sending the agent a message revives it". A live-connection
    harness (codex) instead reports NOT_READY when its daemon is unreachable, so this
    supplies the same behavior at the endpoint: start the agent through the exact path the
    start endpoint and terminal-open use (a no-op when it is already running), then retry
    while the daemon comes up. Returns the final outcome -- a daemon still unreachable at
    the deadline keeps the honest NOT_READY -> 503, and a ``SendFailedError`` from a retry
    propagates to the caller's handler like a first-attempt one.
    """
    try:
        start_agent(agent_info.name)
    except MngrError as e:
        logger.warning("Could not revive agent {} for a send: {}", agent_info.name, e)
        return SendOutcome.NOT_READY
    # The observe stream will not see the revival for minutes (no pid to watch while the
    # agent was stopped); reflect it now so the UI's liveness unblocks with the send.
    agent_manager.note_agent_alive(agent_info.id)
    deadline = time.monotonic() + budget_seconds
    outcome = SendOutcome.NOT_READY
    while outcome is SendOutcome.NOT_READY and time.monotonic() < deadline:
        sleep(_REVIVE_RETRY_INTERVAL_SECONDS)
        outcome = session.send(send_message_request.message, message_id)
    return outcome


def _send_message_endpoint(agent_id: str) -> Response:
    """Send a message to an agent."""
    agent_info = _find_agent(agent_id)
    if agent_info is None:
        return _agent_not_found_response(agent_id)

    send_message_request = SendMessageRequest.model_validate(request.get_json())
    state = get_state()
    agent_manager: AgentManager = state.agent_manager
    message_id = send_message_request.message_id or uuid4().hex

    # Ensure the watcher exists BEFORE the send, as the tap and stop endpoints already do. For
    # a harness that holds its own queue (antigravity), the watcher owns the only thread that
    # can ever deliver it -- so a send arriving here first (a headless client, or the first
    # request after a restart) would otherwise enqueue a message with nothing running to drain
    # it, and decide "is a turn open?" from an unpublished reading.
    state.get_or_create_watcher(agent_info)

    # The agent's session owns the whole send lifecycle (contract A1/A2): the file session
    # records the message as *Sending* around mngr's blocking delivery (greying the tap button
    # for the duration); the codex session hands it to its live ledger, passing ``message_id``
    # only as the correlation token the committed item echoes back (Fix 2).
    session = agent_manager.get_or_create_session(agent_info)
    try:
        outcome = session.send(send_message_request.message, message_id)
        if outcome is SendOutcome.NOT_READY:
            outcome = _revive_and_retry_send(agent_info, agent_manager, session, send_message_request, message_id)
    except SendFailedError as send_failure:
        # The harness said why it refused, in words written for the person who has to fix it
        # ("the agent is in shell mode with an unsubmitted command"). Pass that through rather
        # than the generic failure below -- it is the only thing here the user can act on.
        # The kind travels beside the detail so the chat can decide what to offer: trying again
        # can clear a blocked input and cannot help when there is nothing left to talk to.
        return json_response({"detail": send_failure.detail, "kind": send_failure.kind}, status_code=500)
    if outcome is SendOutcome.NOT_READY:
        failure = ErrorResponse(
            detail=f"Agent '{agent_info.name}' is not ready to receive messages yet (its daemon is starting)."
        )
        return json_response(failure.model_dump(), status_code=503)
    if outcome is SendOutcome.FAILED:
        failure = ErrorResponse(detail=f"Failed to send message to agent '{agent_info.name}' (0 successful agents)")
        return json_response(failure.model_dump(), status_code=500)

    _record_client_message_activity(agent_info, send_message_request)
    # The frontend used to report the send for the OOM prioritizer's recency ranking; the
    # send route knows the same fact and records it after the delivery, once the revived
    # process (if any) is up and its pid can be found.
    agent_manager.record_message_sent(agent_info.id)
    return json_response(SendMessageResponse(status="ok").model_dump())


def _record_client_message_activity(agent_info: AgentInfo, send_message_request: SendMessageRequest) -> None:
    """Record which client (and layout) a message came from, so agents can attribute requests to a
    client via ``layout.py context``. Legacy callers without client metadata are not recorded."""
    events_path = _client_activity_events_path()
    if events_path is not None and send_message_request.client_id:
        client_activity.append_message_event(
            events_path,
            client_id=send_message_request.client_id,
            device_kind=send_message_request.device_kind,
            layout_slug=send_message_request.active_layout,
            agent_id=agent_info.id,
            agent_name=agent_info.name,
            message_text=send_message_request.message,
        )


def _get_harnesses_endpoint() -> Response:
    """The static per-harness model catalogs -- the model bar's compile-time half.

    One response covers every harness (each catalog dumped verbatim: options,
    switch mode, picker mode, powered-by label, shoulder-tap capability); the
    frontend keys in by an agent's harness.

    Every harness is always included, deliberately: what the user has signed in to
    decides what they can LAUNCH, not what the app can render. A codex or pi agent that
    exists some other way (``mngr create``, or one left behind after its account was
    removed) still needs its catalog for the model bar to resolve, so narrowing this to
    the signed-in harnesses would strand that agent's chip on an unrecognized model.
    """
    catalogs: dict[str, Any] = {}
    for harness in HARNESS_SPECS:
        # A parsed catalog (pi) reads data files; a bad/absent one must be
        # skipped, not 500 the endpoint for every other harness.
        try:
            catalog = get_catalog(harness).model_dump()
        except (OSError, ValueError) as e:
            logger.warning("Skipping model catalog for harness {}: {}", harness.value, e)
            continue
        # The catalog model is the wire shape for the model bar; the popup declarations
        # live on the HarnessSpec and are merged in here so one response carries
        # everything the frontend keys by harness.
        spec = get_harness_spec(harness)
        catalog["popups"] = [popup.model_dump() for popup in spec.popups]
        catalogs[harness.value] = catalog
    return json_response(catalogs)


def _agent_switch_options(agent_manager: "AgentManager", agent_info: AgentInfo) -> tuple[ModelOption, ...]:
    """The option set the switch endpoint validates against: per-agent for codex, static otherwise.

    Codex has no static catalog, so its valid model/effort/fast set is per-agent -- the ONE reconciled
    set (:meth:`AgentManager.get_codex_model_options`) that the picker offered and the chip matches
    against, seeded on connect and refreshed by each picker-open (D2), falling back to the persisted
    sidecar while that in-memory set is empty (post-restart). Empty (no set and no sidecar) only until
    first populated -- a switch then fails validation, which is correct: nothing to switch to until a
    connect, a picker-open, or a persisted sidecar supplies the account's ``model/list``. Every other
    harness validates against its static catalog options.
    """
    return agent_manager.get_or_create_session(agent_info).switch_options()


def _set_model_choice_endpoint(agent_id: str) -> Response:
    """Apply a model/effort/fast selection by asking the agent's resolver to switch.

    Harness-blind: it validates the request against the agent's option set (the static catalog for
    claude/pi, the per-agent ``model/list`` set for codex), then hands a concrete identity to the
    resolver's ``switch`` (which decides how to apply it). Returns 400 for an invalid selection, 404
    for an unknown agent, 500 when the switch fails. On success it forces one authoritative
    model-choice broadcast so the frontend reconciles.
    """
    agent_info = _find_agent(agent_id)
    if agent_info is None:
        return _agent_not_found_response(agent_id)

    req = SetModelChoiceRequest.model_validate(request.get_json())
    agent_manager: AgentManager = get_state().agent_manager
    options = _agent_switch_options(agent_manager, agent_info)
    # The picker only ever sends a valid option id, so validation is an exact id lookup.
    option = next((opt for opt in options if opt.id == req.model_id), None)
    if option is None:
        return json_response(ErrorResponse(detail=f"Unknown model '{req.model_id}'").model_dump(), status_code=400)

    # Flat guards (rather than a branch per axis-presence) so effort is validated
    # against the model's declared set: required + in-set when the model has efforts,
    # and absent when it does not.
    declared_efforts = {choice.level for choice in option.efforts}
    has_effort_axis = len(option.efforts) > 0
    if has_effort_axis and req.effort is None:
        return json_response(ErrorResponse(detail="This model requires an effort level").model_dump(), 400)
    if has_effort_axis and req.effort is not None and req.effort not in declared_efforts:
        return json_response(
            ErrorResponse(detail=f"'{req.effort}' is not a valid effort for '{req.model_id}'").model_dump(), 400
        )
    if not has_effort_axis and req.effort is not None:
        return json_response(ErrorResponse(detail=f"'{req.model_id}' has no effort axis").model_dump(), 400)
    if req.fast and not option.supports_fast:
        return json_response(ErrorResponse(detail=f"'{req.model_id}' does not support fast mode").model_dump(), 400)

    # The live read is harness-neutral (shared reader), so the resolver -- which now owns
    # only the switch/offer side -- is built inline from agent_info rather than cached.
    resolver = build_resolver(agent_info)

    identity = ModelIdentity(model_id=req.model_id, effort=req.effort, fast=req.fast)
    result = resolver.switch(
        identity,
        frozenset(req.axes),
        lambda line: agent_manager.send_message_to_agent(AgentId(agent_info.id), line) is None,
    )
    if not result.ok:
        detail = result.detail or f"Failed to switch model for agent '{agent_info.name}'"
        return json_response(ErrorResponse(detail=detail).model_dump(), status_code=500)

    # Force one authoritative broadcast so the optimistic pick reconciles even when
    # the resolved value is unchanged (see H1 in the model-bar plan).
    agent_manager.refresh_model_choice(agent_info.id)
    return json_response(SendMessageResponse(status="ok").model_dump())


def _get_model_options_endpoint(agent_id: str) -> Response:
    """The models this agent should OFFER in the picker right now.

    Recomputed per request (the frontend calls it each time the picker opens). Two shapes:

    * a DYNAMIC harness (codex) has no static catalog, so it returns the FULL per-agent
      :class:`ModelOption`s (``options``) -- id, label, per-model efforts, fast support -- fetched
      fresh from ``model/list`` on this open (D2), so a subscription-tier change shows up live.
    * a static/catalog-backed harness (claude, pi) returns ``models`` -- the ids to offer, matched
      back to the static catalog for labels/efforts (``null`` = offer the whole catalog). This
      reflects an account-gated set (pi's authenticated models) on a fresh login without a refetch.
    """
    agent_info = _find_agent(agent_id)
    if agent_info is None:
        return _agent_not_found_response(agent_id)
    resolver = build_resolver(agent_info)
    dynamic_options = resolver.list_offered_options()
    if dynamic_options is not None:
        # Reconcile (D2): this fresh per-open fetch becomes the ONE per-agent set the chip-match and
        # the switch-validation also read, so immediately after this open all three agree. A failed
        # fetch (empty) is NOT stored -- it must not clobber the last-known set (seeded on connect or
        # from an earlier open) that the chip is still matching against. The RAW list behind these
        # mapped options is also written through to the codex sidecar inside the resolver's
        # ``list_offered_options`` (where the raw ``model/list`` is still in hand), so the chip
        # resolves offline after a restart.
        if dynamic_options:
            get_state().agent_manager.get_or_create_session(agent_info).note_offered_options(dynamic_options)
        return json_response(ModelOptionsResponse(options=dynamic_options).model_dump())
    return json_response(ModelOptionsResponse(models=resolver.list_offered_models()).model_dump())


def _get_powered_by_endpoint(agent_id: str) -> Response:
    """The agent's credit text -- a per-agent path decoupled from the model bar.

    The text is a pure function of the agent's harness, so it must never blink with the live
    model choice or wait on the catalog fetch. This resolves the harness backend-side and
    returns the harness's verbatim credit string, so the frontend can render it from ``agentId``
    alone, independent of ``model_choice`` and of ``GET /api/harnesses``. A harness that shows
    no credit (claude) declares "", which the frontend renders as nothing. 404 for an unknown
    agent (e.g. a proto-agent), which the frontend also treats as "no credit".
    """
    agent_info = _find_agent(agent_id)
    if agent_info is None:
        return _agent_not_found_response(agent_id)
    return json_response(PoweredByResponse(label=get_catalog(agent_info.harness).powered_by_text).model_dump())


def _build_fast_mode_answered_label_command(agent_name: str) -> list[str]:
    """Build the ``mngr label`` argv that latches the fast-mode prompt as answered.

    Pure: argv assembly only, so the repo<->mngr CLI contract is testable
    against the live CLI without a subprocess (see ``server_test.py``).
    """
    return ["mngr", "label", agent_name, "-l", "fast_mode_prompt_answered=true"]


def _mark_fast_mode_prompt_answered(agent_id: str) -> Response:
    """Latch the fast-mode prompt as answered for one agent, via an agent label.

    The prompt asks once per agent, ever: any exit from the modal routes here, so
    the label is the durable record that the question was put to the user. The
    label reaches the frontend with the next observe relist; the frontend keeps
    its own in-session mark so the prompt cannot re-fire in the meantime.
    """
    agent_manager: AgentManager = get_state().agent_manager
    agent_state = agent_manager.get_agent_by_id(agent_id)
    if agent_state is None:
        error = ErrorResponse(detail=f"Agent '{agent_id}' not found")
        return json_response(error.model_dump(), status_code=404)

    result = run_local_command_modern_version(
        command=_build_fast_mode_answered_label_command(agent_state.name),
        cwd=None,
        is_checked=False,
        timeout=_LABEL_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        detail = f"Failed to record the fast-mode answer for '{agent_state.name}': {result.stderr.strip()}"
        return json_response(ErrorResponse(detail=detail).model_dump(), status_code=500)

    return json_response(FastModePromptAnsweredResponse(status="ok").model_dump())


def _upload_attachment() -> Response:
    """Store a file the user attached to a chat message under data/uploads/.

    The frontend uploads each attachment here as soon as the user drops, pastes,
    or picks it, then appends the returned absolute path to the message text it
    sends to the agent. Returns the stored path and size so the composer can show
    a preview and reference the file.
    """
    file_storage = request.files.get("file")
    if file_storage is None or not file_storage.filename:
        error = ErrorResponse(detail="No file provided in the 'file' field")
        return json_response(error.model_dump(), status_code=400)

    uploads_directory = get_uploads_directory()
    try:
        stored_path = store_uploaded_file(uploads_directory, file_storage.filename, file_storage)
    except AttachmentError as e:
        error = ErrorResponse(detail=str(e))
        return json_response(error.model_dump(), status_code=500)

    size_bytes = stored_path.stat().st_size
    response = AttachmentUploadResponse(path=str(stored_path), size=size_bytes)
    return json_response(response.model_dump(), status_code=201)


def _serve_attachment(relative_path: str) -> Response:
    """Serve a stored attachment for inline preview, confined to data/uploads/."""
    resolved_path = resolve_upload_path(get_uploads_directory(), relative_path)
    if resolved_path is None:
        error = ErrorResponse(detail=f"Attachment '{relative_path}' not found")
        return json_response(error.model_dump(), status_code=404)
    return send_file(resolved_path)


def _delete_attachment(relative_path: str) -> Response:
    """Delete a stored attachment when the user removes it before sending.

    Idempotent: a path that is missing or escapes the uploads directory is a
    no-op, so a double-remove or a stale id still reports success.
    """
    delete_upload(get_uploads_directory(), relative_path)
    return json_response({"status": "ok"})


def _interrupt_agent_endpoint(agent_id: str) -> Response:
    """Interrupt an agent's current turn by restarting it.

    Runs ``mngr start <agent> --restart --no-resume``, which stops the agent
    (ending any in-progress turn) and starts it fresh without sending a resume
    message. Returns 404 if the agent is unknown, 400 if the agent carries the
    ``is_primary=true`` label, 500 if the restart command fails, 200 otherwise.

    Refuses to interrupt agents carrying the ``is_primary=true`` label: that's
    the services agent for the workspace, and restarting it would stop the
    bootstrap, web, share-gateway, and other supervised services. The
    frontend already hides ``is_primary=true`` agents from the visible agent
    list; this is defense-in-depth for callers that hit the endpoint directly
    (curl, scripted use, etc.).
    """
    agent_info = _find_agent(agent_id)
    if agent_info is None:
        return _agent_not_found_response(agent_id)

    if agent_info.labels.get("is_primary") == "true":
        error = ErrorResponse(
            detail=(
                f"Refusing to interrupt agent '{agent_info.name}': it carries "
                "the is_primary=true label (services agent for this workspace)"
            )
        )
        return json_response(error.model_dump(), status_code=400)

    agent_name = agent_info.name

    is_restarted, output = _restart_agent_process(agent_name)
    if not is_restarted:
        error = ErrorResponse(detail=f"Failed to interrupt agent '{agent_name}': {output}")
        return json_response(error.model_dump(), status_code=500)

    # The restart abandons the session transcript mid-turn, so the
    # transcript-derived activity state would stay pinned at THINKING /
    # TOOL_RUNNING until the user sends another message. Reset it to IDLE
    # now so the activity indicator clears immediately after the stop.
    get_state().agent_manager.reset_activity_state(agent_id)

    return json_response(InterruptAgentResponse(status="ok").model_dump())


def _restart_agent_process(agent_name: str) -> tuple[bool, str]:
    """Run ``mngr start <agent> --restart --no-resume``; return ``(is_restarted, output)``.

    Stops the agent (ending any in-progress turn) and relaunches it fresh without
    a resume prompt: conversation history is preserved (each harness resumes its
    own on-disk session) and the in-harness queue is dropped by the SIGKILL.
    ``output`` is stdout on success, stderr on failure (for the caller's message).
    Refused by mngr for an ``is_primary=true`` agent; callers guard that with a
    clearer 400 before calling.
    """
    result = run_local_command_modern_version(
        command=["mngr", "start", agent_name, "--restart", "--no-resume"],
        cwd=None,
        is_checked=False,
        timeout=60.0,
    )
    is_restarted = result.returncode == 0
    return is_restarted, (result.stdout.strip() if is_restarted else result.stderr.strip())


def _refuse_queue_action_on_primary(agent_info: AgentInfo, action: str) -> Response | None:
    """A 400 refusing a restart-based queue action on the primary services agent, or None.

    Both queue actions restart the agent; restarting the ``is_primary=true``
    services agent would tear down the workspace's supervised services. The
    frontend hides primary agents, so this is defense-in-depth for direct callers.
    """
    if agent_info.labels.get("is_primary") == "true":
        error = ErrorResponse(
            detail=(
                f"Refusing to {action} agent '{agent_info.name}': it carries the "
                "is_primary=true label (services agent for this workspace)"
            )
        )
        return json_response(error.model_dump(), status_code=400)
    return None


def _interrupt_capabilities(
    agent_info: AgentInfo,
) -> tuple[AgentSessionWatcher, Callable[[], tuple[bool, str]], Callable[[], None]]:
    """The harness-neutral capabilities a queue action binds for one agent: the queue mirror,
    a process restart (``mngr start --restart --no-resume``), and an activity-settle.

    Shared by the restart-drain flush and the (per-harness) stop button, mirroring how the
    switch endpoint binds its ``send`` callback.
    """
    state = get_state()
    watcher = state.get_or_create_watcher(agent_info)
    return (
        watcher,
        lambda: _restart_agent_process(agent_info.name),
        lambda: state.agent_manager.reset_activity_state(agent_info.id),
    )


def _flush_queue_endpoint(agent_id: str) -> Response:
    """Shoulder tap: restart the agent and resend the whole queue as one turn.

    Combining is required: after the restart the agent is idle, so sending the
    messages one at a time would let the first open a turn and the rest re-queue.
    Returns 404 for an unknown agent, 400 for the primary services agent, 500 if
    the restart or the resend fails, 200 otherwise.
    """
    agent_info = _find_agent(agent_id)
    if agent_info is None:
        return _agent_not_found_response(agent_id)
    refusal = _refuse_queue_action_on_primary(agent_info, "flush the queue of")
    if refusal is not None:
        return refusal

    watcher, restart_process, settle_activity = _interrupt_capabilities(agent_info)
    # Empty-queue short-circuit lives HERE (not in the shared restart-drain): a flush with
    # nothing queued would resend nothing, so it is a clean no-op. The stop button, by contrast,
    # still interrupts an empty-queue turn -- so the restart-drain no longer short-circuits.
    if not watcher.get_queued_block():
        return json_response(SendMessageResponse(status="ok").model_dump())

    try:
        block = restart_drain(agent_info, watcher, restart_process, settle_activity)
    except AgentRestartError as e:
        return json_response(ErrorResponse(detail=str(e)).model_dump(), status_code=500)

    if block:
        agent_manager: AgentManager = get_state().agent_manager
        resend_failure = agent_manager.send_message_to_agent(AgentId(agent_info.id), block)
        if resend_failure is not None:
            # The harness said why; passing that on rather than a generic sentence is the whole
            # point of carrying it this far.
            return json_response({"detail": resend_failure.reason, "kind": resend_failure.kind}, status_code=500)

    return json_response(SendMessageResponse(status="ok").model_dump())


def _shoulder_tap_atomic_endpoint(agent_id: str) -> Response:
    """Atomic shoulder tap: merge the queue into the live turn without restarting the agent.

    The gentle counterpart to :func:`_flush_queue_endpoint`: rather than SIGKILL-restart the
    agent and resend the queue, the agent's session delivers the harness's native tap and the
    agent stays alive. HOW each harness taps lives with its implementation -- claude's cancel
    chord in ``harnesses/claude/tap.py`` (``ClaudeAtomicShoulderTap``), pi's locked
    ``pi_inbox`` flush sentinel in ``harnesses/pi_coding/model.py`` (``PiAtomicShoulderTap``),
    codex's live-ledger interrupt+resend in ``harnesses/codex/session.py`` -- not here.

    Returns 404 for an unknown agent, 400 for a harness whose catalog declares no atomic tap
    or for the primary services agent, an error status when the tap failed (e.g. a claude
    dialog block maps to 409), and 200 otherwise with the harness's own verdict (``tapped``,
    ``no_open_turn``, or the benign ``send_in_flight`` no-op a raced send produces).
    """
    agent_info = _find_agent(agent_id)
    if agent_info is None:
        return _agent_not_found_response(agent_id)
    if not get_catalog(agent_info.harness).native_atomic_shoulder_tap_possible:
        error = ErrorResponse(
            detail=(
                f"Agent '{agent_info.name}' runs the {agent_info.harness.value} harness, which does not "
                "support an atomic shoulder tap"
            )
        )
        return json_response(error.model_dump(), status_code=400)
    refusal = _refuse_queue_action_on_primary(agent_info, "shoulder-tap the queue of")
    if refusal is not None:
        return refusal

    # The session dispatches to the harness's native tap (claude's chord executor, pi's locked
    # inbox sentinel, codex's live-ledger interrupt+resend). A retryable refusal racing an
    # in-flight send is a benign 200 no-op status, never an error dialog -- the pushed
    # ``shoulder_tap_available`` flag already greys the button while anything is Sending.
    state = get_state()
    watcher = state.get_or_create_watcher(agent_info)
    agent_manager = state.agent_manager
    outcome = agent_manager.get_or_create_session(agent_info).shoulder_tap(
        agent_info,
        watcher,
        press_chord=lambda: agent_manager.press_key_chord_on_agent(
            AgentId(agent_info.id), get_harness_spec(agent_info.harness).cancel_chord
        ),
        send_recovery=lambda text: agent_manager.send_message_to_agent(AgentId(agent_info.id), text) is None,
    )
    if outcome.error_detail is not None:
        error = ErrorResponse(detail=outcome.error_detail)
        return json_response(error.model_dump(), status_code=outcome.error_status_code)
    return json_response(ShoulderTapAtomicResponse(status=outcome.status, block=outcome.block).model_dump())


def _drain_to_composer_endpoint(agent_id: str) -> Response:
    """Interrupt to composer: interrupt the running turn and hand the queued block back, unsent.

    Dispatches through the harness's registered interrupt-to-composer implementation (the base
    restart-drain by default; native overrides for pi, codex, and claude's empty-queue chord),
    which returns the concatenated block the frontend drops into the composer for the user to
    edit and send, rather than resent. Unlike the flush there is NO empty-queue short-circuit: a
    stop mid-turn with nothing queued still interrupts (block comes back empty). The endpoint
    binds the harness-neutral capabilities -- watcher, restart, activity-settle, and the native
    cancel keypress (routed through mngr's locked message API, like the tap) -- and the
    implementation uses whichever it needs. Returns 404 for an unknown agent, 400 for the primary
    services agent, 500 if the interrupt fails, 200 with ``{block}`` otherwise.
    """
    agent_info = _find_agent(agent_id)
    if agent_info is None:
        return _agent_not_found_response(agent_id)
    refusal = _refuse_queue_action_on_primary(agent_info, "interrupt the queue of")
    if refusal is not None:
        return refusal

    agent_manager: AgentManager = get_state().agent_manager

    watcher, restart_process, settle_activity = _interrupt_capabilities(agent_info)
    try:
        block = agent_manager.get_or_create_session(agent_info).interrupt_to_composer(
            agent_info,
            watcher,
            restart_process,
            settle_activity,
            lambda: agent_manager.press_key_chord_on_agent(
                AgentId(agent_info.id), get_harness_spec(agent_info.harness).cancel_chord
            ),
        )
    except AgentRestartError as e:
        return json_response(ErrorResponse(detail=str(e)).model_dump(), status_code=500)
    except OSError as e:
        logger.opt(exception=e).error("Failed to record the interrupt for agent {}", agent_info.name)
        error = ErrorResponse(detail=f"Failed to record the interrupt for agent '{agent_info.name}'")
        return json_response(error.model_dump(), status_code=500)

    return json_response(DrainToComposerResponse(block=block).model_dump())


def _get_subagent_events(agent_id: str, subagent_session_id: str) -> Response:
    """Get events for a specific subagent session."""
    agent_info = _find_agent(agent_id)
    if agent_info is None:
        return _agent_not_found_response(agent_id)

    watcher = get_state().get_or_create_watcher(agent_info)
    events = watcher.get_all_events(session_id=subagent_session_id)

    # Include metadata in the response
    metadata = watcher.get_subagent_metadata(subagent_session_id)

    return json_response({"events": events, "metadata": metadata})


def _stream_subagent_events(agent_id: str, subagent_session_id: str) -> Response:
    """SSE stream for a subagent's new events, filtered by session_id."""
    agent_info = _find_agent(agent_id)
    if agent_info is None:
        return _agent_not_found_response(agent_id)

    state = get_state()
    state.get_or_create_watcher(agent_info)

    event_queues = state.event_queues
    event_queue = event_queues.register(agent_id)

    return _sse_response(
        _stream_filtered_events(
            agent_id,
            event_queues,
            event_queue,
            lambda event: event.get("session_id") == subagent_session_id,
        )
    )


def _get_screen_capture(agent_id: str) -> Response:
    """Capture the tmux pane content for an agent.

    Returns the visible screen content (and optionally scrollback) as plain
    text. Useful for seeing what's on an agent's terminal when it has no
    Claude session data (e.g., the agent crashed on startup).
    """
    agent_info = _find_agent(agent_id)
    if agent_info is None:
        return _agent_not_found_response(agent_id)

    prefix = os.environ.get("MNGR_PREFIX", "mngr-")
    session_name = f"{prefix}{agent_info.name}"
    include_scrollback = request.args.get("scrollback", "false").lower() == "true"
    scrollback_flag = ["-S", "-"] if include_scrollback else []
    command = ["tmux", "capture-pane", "-t", session_name, *scrollback_flag, "-p"]

    result = run_local_command_modern_version(
        command=command,
        cwd=None,
        is_checked=False,
        timeout=5.0,
    )
    success = result.returncode == 0
    if not success:
        return json_response(
            {"screen": None, "error": f"tmux session not found: {session_name}"},
            status_code=200,
        )
    return json_response({"screen": result.stdout})


def _create_chat_agent() -> Response:
    """Create a new chat agent in the primary agent's work directory.

    One endpoint for every harness: the ``chat`` role is the same, and the request's
    ``harness`` field (validated against :class:`HarnessType`, claude by default) picks
    which harness template the server stacks under it.

    The chat's display name is minted here (server-side) when the request names
    none: the first free "<word> N" for the harness, counted against every name
    on the machine -- agents, in-flight creates, and the member-title store's
    chosen names -- so simultaneous creates cannot both mint "Chat 1". An
    explicitly requested name that collides answers 409 so the caller can retry
    with another. The response carries the resulting name pair (canonical
    ``name`` + human-readable ``display_name``) beside the agent id.

    A chat created inside a project carries that project's id in the agent's
    ``project`` label, which is where chat membership lives (mngr already
    propagates the label to the agent's own children). ``project_id`` rides
    beside the request model rather than inside it for that reason: it is a
    label on the created agent, not part of the chat's identity. A create with
    no ``project_id`` leaves the chat filed in no project, which is ordinary:
    Everything enumerates the machine, so it surfaces there anyway.
    """
    agent_manager: AgentManager = get_state().agent_manager
    body = parse_json_object_body()
    if isinstance(body, Response):
        return body
    project_id = str(body.get("project_id") or "")
    request_fields = {key: value for key, value in body.items() if key != "project_id"}

    try:
        create_request = CreateChatRequest.model_validate(request_fields)
        created = agent_manager.create_chat_agent(
            create_request.name,
            # The `first` create template belongs to the workspace's own first run, not to
            # anything a client asks for -- bootstrap stacks it on its own `mngr create`.
            extra_role_templates=(),
            project_id=project_id,
            extra_taken_names=taken_member_titles(),
            account_id=create_request.account_id,
        )
        response = CreateAgentResponse(agent_id=created.agent_id, name=created.name, display_name=created.display_name)
        return json_response(response.model_dump(), status_code=201)
    except AgentNameConflictError as e:
        return json_response(ErrorResponse(detail=str(e)).model_dump(), status_code=409)
    except (AgentCreationError, OSError, ValueError) as e:
        error = ErrorResponse(detail=str(e))
        return json_response(error.model_dump(), status_code=400)


def _proto_agent_logs_endpoint(websocket: Any, agent_id: str) -> None:
    """WebSocket for streaming proto-agent creation logs."""
    agent_manager: AgentManager = get_state().agent_manager
    log_queue = agent_manager.get_log_queue(agent_id)
    _run_proto_agent_logs_loop(websocket=websocket, log_queue=log_queue)


def _run_proto_agent_logs_loop(
    websocket: Any,
    log_queue: "queue.Queue[str | None] | None",
) -> None:
    """Stream ``log_queue`` messages to ``websocket`` until the proto-agent finishes.

    If ``log_queue`` is ``None`` the proto-agent does not exist; send a
    structured not-found error and close the socket.
    """
    if log_queue is None:
        try:
            websocket.send(json.dumps({"done": True, "success": False, "error": "Proto-agent not found"}))
        except ConnectionClosed:
            pass
        return

    try:
        finished = False
        while not finished:
            try:
                message = log_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if message is None:
                finished = True
            else:
                websocket.send(message)
    except ConnectionClosed:
        pass


def _presence_endpoint(agent_id: str) -> Response:
    """Record one client's presence report about this chat's page (see ``presence.py``).

    Accepted for any well-formed agent id, a chat still being created included: the
    prioritizer ignores ids it does not manage, and a page that reports before its agent
    exists must not be told it is wrong.
    """
    if not AGENT_ID_PATTERN.fullmatch(agent_id):
        return _agent_not_found_response(agent_id)
    report = parse_request_body(PresenceReport)
    agent_manager: AgentManager = get_state().agent_manager
    agent_manager.record_presence(agent_id, report.client_id, report.state)
    return json_response({"status": "ok"})


def _chat_document(key: str) -> Response:
    """Serve the chat page for an instance key: an agent id, or ``<agent-id>.<session-id>`` for a subagent view."""
    chat_agent_id, _, session_id = key.partition(SUBAGENT_KEY_SEPARATOR)
    if not AGENT_ID_PATTERN.fullmatch(chat_agent_id):
        return _agent_not_found_response(key)
    document_path = get_state().static_directory / CHAT_DOCUMENT_FILENAME
    if not document_path.exists():
        _loguru_logger.warning("Served the chat not-built placeholder: no chat bundle at {}", document_path)
        return document_response(_CHAT_NOT_BUILT_HTML, is_frontend_built=False)
    config: Config = get_state().config
    root_path = (request.script_root or "").rstrip("/")
    html_content = document_path.read_text()
    html_content = inject_base_path_meta_tag(html_content, root_path)
    html_content = inject_hostname_meta_tag(html_content)
    html_content = inject_primary_agent_id_meta_tag(html_content)
    html_content = inject_chat_identity_meta_tags(html_content, chat_agent_id, session_id)
    if config.javascript_plugin_basenames:
        html_content = inject_plugin_script_tags(html_content, config.javascript_plugin_basenames, root_path)
    return document_response(html_content, is_frontend_built=True)


def create_chat_application(state: SystemInterfaceState) -> Flask:
    """Assemble the chat app around the same ``SystemInterfaceState`` the shell runs on.

    A pure assembler like ``server.create_application``: routes and error handling only, no
    collaborators built, nothing started. The instances blueprint is mounted here over the
    agent manager; its nudger fires whatever nudger the manager holds (``main`` installs the
    real one), so a test that builds the app posts nothing to the workspace shell.
    """
    # No static folder: Flask would otherwise add a /static/<path> route beside the document route.
    application = Flask(__name__, static_folder=None)
    attach_state(application, state)
    application.register_error_handler(Exception, handle_unhandled_exception)
    # The presence route reads its body through the library's parser, so its errors answer
    # like the blueprint's: a status from the contract with a ``{"detail"}`` body.
    application.register_error_handler(AppInstancesError, answer_typed_error)
    sock = build_sock(application)

    source, nudger = build_chat_instance_source(state.agent_manager)
    application.register_blueprint(build_instances_blueprint(source, nudger))

    application.add_url_rule("/api/agents/create-chat", view_func=_create_chat_agent, methods=["POST"])
    application.add_url_rule("/api/agents/<agent_id>/events", view_func=_get_events, methods=["GET"])
    application.add_url_rule(
        "/api/agents/<agent_id>/events/<event_id>/detail", view_func=_get_event_detail, methods=["GET"]
    )
    application.add_url_rule("/api/agents/<agent_id>/stream", view_func=_stream_events, methods=["GET"])
    application.add_url_rule("/api/agents/<agent_id>/message", view_func=_send_message_endpoint, methods=["POST"])
    application.add_url_rule("/api/agents/<agent_id>/presence", view_func=_presence_endpoint, methods=["POST"])
    application.add_url_rule("/api/harnesses", view_func=_get_harnesses_endpoint, methods=["GET"])
    application.add_url_rule("/api/agents/<agent_id>/model", view_func=_set_model_choice_endpoint, methods=["POST"])
    application.add_url_rule(
        "/api/agents/<agent_id>/model-options", view_func=_get_model_options_endpoint, methods=["GET"]
    )
    application.add_url_rule("/api/agents/<agent_id>/powered-by", view_func=_get_powered_by_endpoint, methods=["GET"])
    application.add_url_rule(
        "/api/agents/<agent_id>/fast-mode-answered",
        view_func=_mark_fast_mode_prompt_answered,
        methods=["POST"],
    )
    application.add_url_rule("/api/uploads", view_func=_upload_attachment, methods=["POST"])
    application.add_url_rule("/api/uploads/<path:relative_path>", view_func=_serve_attachment, methods=["GET"])
    application.add_url_rule(
        "/api/uploads/<path:relative_path>",
        view_func=_delete_attachment,
        methods=["DELETE"],
        endpoint="_delete_attachment",
    )
    application.add_url_rule("/api/agents/<agent_id>/interrupt", view_func=_interrupt_agent_endpoint, methods=["POST"])
    application.add_url_rule("/api/agents/<agent_id>/flush-queue", view_func=_flush_queue_endpoint, methods=["POST"])
    application.add_url_rule(
        "/api/agents/<agent_id>/shoulder-tap-atomic",
        view_func=_shoulder_tap_atomic_endpoint,
        methods=["POST"],
    )
    application.add_url_rule(
        "/api/agents/<agent_id>/drain-to-composer", view_func=_drain_to_composer_endpoint, methods=["POST"]
    )
    application.add_url_rule("/api/agents/<agent_id>/screen", view_func=_get_screen_capture, methods=["GET"])
    application.add_url_rule(
        "/api/agents/<agent_id>/subagents/<subagent_session_id>/events",
        view_func=_get_subagent_events,
        methods=["GET"],
    )
    application.add_url_rule(
        "/api/agents/<agent_id>/subagents/<subagent_session_id>/stream",
        view_func=_stream_subagent_events,
        methods=["GET"],
    )
    auth_endpoints.register_routes(application)
    accounts_endpoints.register_routes(application)
    latchkey_endpoints.register_routes(application)
    sock.route("/api/proto-agents/<agent_id>/logs")(_proto_agent_logs_endpoint)

    application.add_url_rule("/<key>", view_func=_chat_document, methods=["GET"])

    return application
