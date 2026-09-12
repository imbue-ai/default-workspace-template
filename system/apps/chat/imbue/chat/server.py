"""The chat app's Flask app: the chat pages, their API, the chat's WebSocket, and the instances API.

Every chat renders inside an iframe at the registered ``chat`` origin (the workspace app model,
``docs/system/blueprint/workspace-app-model/``), served by this process at its own port; the shell
reaches it only through the instances API and the browser-side contract.
"""

import json
import os
import queue
import threading
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
from app_instances.nudge import post_to_shell
from app_instances.nudge import shell_base_url
from app_manifest.errors import RegistryReadError
from app_manifest.registry import read_registry
from app_manifest.registry import registry_path
from flask import Flask
from flask import Response
from flask import request
from flask import send_file
from flask import send_from_directory
from loguru import logger as _loguru_logger
from simple_websocket import ConnectionClosed
from werkzeug.exceptions import NotFound

from imbue.chat import accounts_endpoints
from imbue.chat import latchkey_endpoints
from imbue.chat.agent_discovery import AgentInfo
from imbue.chat.agent_discovery import SendFailedError
from imbue.chat.agent_discovery import discover_agents
from imbue.chat.agent_discovery import start_agent
from imbue.chat.agent_manager import AgentManager
from imbue.chat.attachments import delete_upload
from imbue.chat.attachments import get_uploads_directory
from imbue.chat.attachments import resolve_upload_path
from imbue.chat.attachments import store_uploaded_file
from imbue.chat.chat_transcript import ChatTranscript
from imbue.chat.config import Config
from imbue.chat.documents import document_response
from imbue.chat.documents import inject_base_path_meta_tag
from imbue.chat.documents import inject_chat_identity_meta_tags
from imbue.chat.documents import inject_hostname_meta_tag
from imbue.chat.documents import inject_plugin_script_tags
from imbue.chat.documents import inject_terminal_label_meta_tag
from imbue.chat.event_queues import AgentEventQueues
from imbue.chat.file_serving import try_serve_file
from imbue.chat.harnesses.claude import auth_endpoints
from imbue.chat.harnesses.interrupt import restart_drain
from imbue.chat.harnesses.model import ModelIdentity
from imbue.chat.harnesses.model import ModelOption
from imbue.chat.harnesses.registry import HARNESS_SPECS
from imbue.chat.harnesses.registry import build_resolver
from imbue.chat.harnesses.registry import get_catalog
from imbue.chat.harnesses.registry import get_harness_spec
from imbue.chat.harnesses.session import AgentHarnessSession
from imbue.chat.harnesses.session import SendOutcome
from imbue.chat.harnesses.session_watcher import AgentSessionWatcher
from imbue.chat.instances import CHAT_APP_NAME
from imbue.chat.instances import build_chat_instance_source
from imbue.chat.instances import parse_subagent_key
from imbue.chat.models import AgentCreationError
from imbue.chat.models import AgentDestroyError
from imbue.chat.models import AgentListItem
from imbue.chat.models import AgentListResponse
from imbue.chat.models import AgentNameConflictError
from imbue.chat.models import AgentRestartError
from imbue.chat.models import AgentStopError
from imbue.chat.models import AttachmentError
from imbue.chat.models import AttachmentUploadResponse
from imbue.chat.models import ChatListResponse
from imbue.chat.models import CreateAgentResponse
from imbue.chat.models import CreateChatRequest
from imbue.chat.models import CreateChatResponse
from imbue.chat.models import CreatedChat
from imbue.chat.models import DestroyAgentResponse
from imbue.chat.models import DrainToComposerResponse
from imbue.chat.models import ErrorResponse
from imbue.chat.models import FastModePromptAnsweredResponse
from imbue.chat.models import InterruptAgentResponse
from imbue.chat.models import ModelOptionsResponse
from imbue.chat.models import PoweredByResponse
from imbue.chat.models import SendMessageRequest
from imbue.chat.models import SendMessageResponse
from imbue.chat.models import SetModelChoiceRequest
from imbue.chat.models import ShoulderTapAtomicResponse
from imbue.chat.models import StartAgentResponse
from imbue.chat.models import StopAgentResponse
from imbue.chat.presence import PresenceReport
from imbue.chat.primitives import AGENT_ID_PATTERN
from imbue.chat.primitives import ChatId
from imbue.chat.request_helpers import handle_unhandled_exception
from imbue.chat.request_helpers import json_response
from imbue.chat.request_helpers import parse_json_object_body
from imbue.chat.state import ChatAppState
from imbue.chat.state import attach_state
from imbue.chat.state import get_state
from imbue.chat.ws_broadcaster import chats_updated_message
from imbue.chat.ws_broadcaster import provisional_chat_created_message
from imbue.chat.wsgi import build_sock
from imbue.concurrency_group.subprocess_utils import run_local_command_modern_version
from imbue.imbue_common.pure import pure
from imbue.mngr.errors import MngrError
from imbue.mngr.primitives import AgentId

logger = _loguru_logger

# The vite build's chat entry: the document the chat's own static/ directory serves, beside its
# assets/ and favicon.
CHAT_DOCUMENT_FILENAME: Final[str] = "chat.html"

# What the chat origin answers when the bundle is missing: the shell's placeholder carries the
# repair story, and a chat frame is never the page a reader is looking at on its own.
_CHAT_NOT_BUILT_HTML: Final[str] = (
    '<!doctype html><html><head><meta charset="utf-8"><title>Chat</title></head>'
    "<body><p>This workspace's chat interface is not built yet.</p></body></html>"
)


def _find_active_agent(chat_id: str) -> AgentInfo | None:
    """The agent a chat runs on, from the AgentManager's already-loaded state; None for an id that names no chat."""
    agent_manager: AgentManager = get_state().agent_manager
    return agent_manager.get_active_agent_info(chat_id)


def _chat_not_found_response(chat_id: str) -> Response:
    error = ErrorResponse(detail=f"Chat '{chat_id}' not found")
    return json_response(error.model_dump(), status_code=404)


def _agent_list_not_known_response() -> Response:
    failure = ErrorResponse(detail="The chat app has not read its agent list from mngr yet; try again shortly.")
    return json_response(failure.model_dump(), status_code=503)


# Default number of events for tail-first loading
_DEFAULT_TAIL_COUNT = 50


# `mngr label` is a metadata write (data.json merge), fast even on a busy host.
_LABEL_TIMEOUT_SECONDS = 30.0


def _get_event_detail(chat_id: str, event_id: str) -> Response:
    """The full deferred payloads for one event: tool input(s), tool output, thinking.

    Resident events are payload-free (the wire contract in ``harnesses/events``); this is
    the on-demand read behind expanding a tool row or a thinking disclosure. The read is
    stateless -- the watcher re-reads the source line (or re-queries agy's store) and
    nothing is cached backend-side; only the frontend may cache what it fetched. When the
    recorded byte range went stale the watcher falls back to scanning the source for the
    event's own identity; only if that also fails does this answer 404, which the frontend
    renders as a quiet "payload no longer available" placeholder.
    """
    transcript = _chat_transcript(chat_id)
    if transcript is None:
        return _chat_not_found_response(chat_id)
    detail = transcript.get_event_detail(event_id)
    if detail is None:
        error = ErrorResponse(detail=f"Payload for event '{event_id}' is no longer available")
        return json_response(error.model_dump(), status_code=404)
    return json_response({"event_id": event_id, **detail})


def _chat_transcript(chat_id: str) -> ChatTranscript | None:
    """The chat's transcript, or None for a chat the app does not list.

    One segment, the active agent's watcher: the chat-agent split's phase 3 adds the
    earlier agents' segments here, behind the same reads.
    """
    agent_info = _find_active_agent(chat_id)
    if agent_info is None:
        return None
    watcher = get_state().get_or_create_watcher(agent_info)
    return ChatTranscript.of_single_segment(ChatId(chat_id), agent_info.id, watcher)


def _get_events(chat_id: str) -> Response:
    """Get a chat's events. Supports tail-first loading and backfill."""
    transcript = _chat_transcript(chat_id)
    if transcript is None:
        return _chat_not_found_response(chat_id)

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

    if before_event_id:
        # Page older: the `limit` events immediately before the cursor.
        events = transcript.get_backfill_events(before_event_id, limit)
    elif after_event_id:
        # Page newer: the `limit` events immediately after the cursor (used when
        # the loaded window has been moved off the live tail by a jump).
        events = transcript.get_forward_events(after_event_id, limit)
    elif offset_str is not None:
        # Jump: a `limit`-event window starting at an arbitrary global index, so
        # the client can land at a far scroll position in one bounded read.
        try:
            offset = int(offset_str)
        except ValueError:
            offset = 0
        events = transcript.get_events_at_offset(offset, limit)
    else:
        # Initial load: the newest `limit` events (the live tail). Bounded read
        # from the end; the client pages/jumps from here.
        events = transcript.get_tail_events(limit)

    # `total` is the full transcript length and `offset` is the global index of the
    # first returned event. Together they place the loaded window in the whole
    # conversation, so the client sizes the scrollbar for the full length and
    # derives whether more history exists above (offset > 0) and below
    # (offset + len < total) -- no separate has_more flag needed.
    total = transcript.get_total_event_count()
    offset = transcript.get_event_offset(events[0]["event_id"]) if events else total
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


def _stream_events(chat_id: str) -> Response:
    """SSE stream for a chat's new events."""
    agent_info = _find_active_agent(chat_id)
    if agent_info is None:
        return _chat_not_found_response(chat_id)

    state = get_state()
    watcher = state.get_or_create_watcher(agent_info)

    event_queues = state.event_queues
    event_queue = event_queues.register(agent_info.id)

    return _sse_response(
        _stream_filtered_events(agent_info.id, event_queues, event_queue, watcher.is_main_session_event)
    )


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


def _send_message_endpoint(chat_id: str) -> Response:
    """Send a message to a chat: its active agent receives it."""
    state = get_state()
    agent_manager: AgentManager = state.agent_manager
    # Until the first agent list has been read, an unknown id says nothing about the agent, so
    # the answer is "not ready" rather than 404: an in-workspace sender backs off to `mngr
    # message` on a 404 (`system/scripts/message_chat.py`), and a 404 during the seconds after
    # a chat-app boot would route messages around the app instead of waiting for it.
    if not agent_manager.is_agent_list_known():
        return _agent_list_not_known_response()
    agent_info = _find_active_agent(chat_id)
    if agent_info is None:
        return _chat_not_found_response(chat_id)

    send_message_request = SendMessageRequest.model_validate(request.get_json())
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
    # only as the correlation token the committed item echoes back.
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

    _record_client_message_activity(ChatId(chat_id), send_message_request)
    # Recorded after the delivery, once the revived process (if any) is up and its pid can be
    # found.
    agent_manager.record_message_sent(ChatId(chat_id))
    return json_response(SendMessageResponse(status="ok").model_dump())


@pure
def is_client_activity_reportable(send_message_request: SendMessageRequest) -> bool:
    """Whether a send names the client, its device kind, and the view the shell's activity log keys on (a legacy or unframed caller names none)."""
    return (
        send_message_request.client_id != ""
        and send_message_request.device_kind != ""
        and send_message_request.active_layout != ""
    )


@pure
def client_activity_report(chat_id: ChatId, send_message_request: SendMessageRequest) -> dict[str, str]:
    """The body of the shell's ``POST /api/client-activity`` for one send (contracts.md section 5), keyed by chat."""
    return {
        "client_id": send_message_request.client_id,
        "device_kind": send_message_request.device_kind,
        "view_id": send_message_request.active_layout,
        "kind": "message",
        "app": str(CHAT_APP_NAME),
        "key": chat_id,
        "text": send_message_request.message,
    }


def _record_client_message_activity(chat_id: ChatId, send_message_request: SendMessageRequest) -> None:
    """Tell the shell which client (and view) a message came from, so agents can attribute requests through
    ``layout.py context``. Callers naming no client or no view are not recorded. Posted on its own thread:
    the shell is a separate app, and a send must not wait on it."""
    if not is_client_activity_reportable(send_message_request):
        return
    body = client_activity_report(chat_id, send_message_request)
    threading.Thread(
        target=post_to_shell,
        args=(f"{shell_base_url()}/api/client-activity", body),
        name="client-activity-report",
        daemon=True,
    ).start()


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


def _set_model_choice_endpoint(chat_id: str) -> Response:
    """Apply a model/effort/fast selection by asking the agent's resolver to switch.

    Harness-blind: it validates the request against the agent's option set (the static catalog for
    claude/pi, the per-agent ``model/list`` set for codex), then hands a concrete identity to the
    resolver's ``switch`` (which decides how to apply it). Returns 400 for an invalid selection, 404
    for an unknown agent, 500 when the switch fails. On success it forces one authoritative
    model-choice broadcast so the frontend reconciles.
    """
    agent_info = _find_active_agent(chat_id)
    if agent_info is None:
        return _chat_not_found_response(chat_id)

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

    # The live read is harness-neutral (shared reader), so the resolver -- which owns only
    # the switch/offer side -- is built inline from agent_info rather than cached.
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


def _get_model_options_endpoint(chat_id: str) -> Response:
    """The models this agent should OFFER in the picker right now.

    Recomputed per request (the frontend calls it each time the picker opens). Two shapes:

    * a DYNAMIC harness (codex) has no static catalog, so it returns the FULL per-agent
      :class:`ModelOption`s (``options``) -- id, label, per-model efforts, fast support -- fetched
      fresh from ``model/list`` on this open (D2), so a subscription-tier change shows up live.
    * a static/catalog-backed harness (claude, pi) returns ``models`` -- the ids to offer, matched
      back to the static catalog for labels/efforts (``null`` = offer the whole catalog). This
      reflects an account-gated set (pi's authenticated models) on a fresh login without a refetch.
    """
    agent_info = _find_active_agent(chat_id)
    if agent_info is None:
        return _chat_not_found_response(chat_id)
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


def _get_powered_by_endpoint(chat_id: str) -> Response:
    """The agent's credit text -- a per-agent path decoupled from the model bar.

    The text is a pure function of the agent's harness, so it must never blink with the live
    model choice or wait on the catalog fetch. This resolves the harness backend-side and
    returns the harness's verbatim credit string, so the frontend can render it from ``chatId``
    alone, independent of ``model_choice`` and of ``GET /api/harnesses``. A harness that shows
    no credit (claude) declares "", which the frontend renders as nothing. 404 for an unknown
    chat (e.g. a provisional one), which the frontend also treats as "no credit".
    """
    agent_info = _find_active_agent(chat_id)
    if agent_info is None:
        return _chat_not_found_response(chat_id)
    return json_response(PoweredByResponse(label=get_catalog(agent_info.harness).powered_by_text).model_dump())


def _build_fast_mode_answered_label_command(agent_name: str) -> list[str]:
    """Build the ``mngr label`` argv that latches the fast-mode prompt as answered.

    Pure: argv assembly only, so the repo<->mngr CLI contract is testable
    against the live CLI without a subprocess.
    """
    return ["mngr", "label", agent_name, "-l", "fast_mode_prompt_answered=true"]


def _mark_fast_mode_prompt_answered(chat_id: str) -> Response:
    """Latch the fast-mode prompt as answered for one agent, via an agent label.

    The prompt asks once per agent, ever: any exit from the modal routes here, so
    the label is the durable record that the question was put to the user. The
    label reaches the frontend with the next observe relist; the frontend keeps
    its own in-session mark so the prompt cannot re-fire in the meantime.
    """
    agent_info = _find_active_agent(chat_id)
    if agent_info is None:
        return _chat_not_found_response(chat_id)

    result = run_local_command_modern_version(
        command=_build_fast_mode_answered_label_command(agent_info.name),
        cwd=None,
        is_checked=False,
        timeout=_LABEL_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        detail = f"Failed to record the fast-mode answer for '{agent_info.name}': {result.stderr.strip()}"
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


def _interrupt_agent_endpoint(chat_id: str) -> Response:
    """Interrupt an agent's current turn by restarting it.

    Runs ``mngr start <agent> --restart --no-resume``, which stops the agent
    (ending any in-progress turn) and starts it fresh without sending a resume
    message. Returns 404 if the agent is unknown, 400 if the agent carries the
    ``is_primary=true`` label, 500 if the restart command fails, 200 otherwise.

    Refuses to interrupt agents carrying the ``is_primary=true`` label: that's
    the services agent for the workspace, and restarting it would stop the
    bootstrap, web, share-gateway, and other supervised services. The chat
    list the app pushes already omits ``is_primary=true`` agents (they are
    never a chat), so this is defense-in-depth for callers that hit the
    endpoint directly (curl, scripted use, etc.).
    """
    agent_info = _find_active_agent(chat_id)
    if agent_info is None:
        return _chat_not_found_response(chat_id)

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
    get_state().agent_manager.reset_activity_state(agent_info.id)

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
    services agent would tear down the workspace's supervised services. The chat
    list the app pushes omits primary agents, so this is defense-in-depth for
    direct callers.
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


def _flush_queue_endpoint(chat_id: str) -> Response:
    """Shoulder tap: restart the agent and resend the whole queue as one turn.

    Combining is required: after the restart the agent is idle, so sending the
    messages one at a time would let the first open a turn and the rest re-queue.
    Returns 404 for an unknown agent, 400 for the primary services agent, 500 if
    the restart or the resend fails, 200 otherwise.
    """
    agent_info = _find_active_agent(chat_id)
    if agent_info is None:
        return _chat_not_found_response(chat_id)
    refusal = _refuse_queue_action_on_primary(agent_info, "flush the queue of")
    if refusal is not None:
        return refusal

    watcher, restart_process, settle_activity = _interrupt_capabilities(agent_info)
    # Empty-queue short-circuit lives HERE (not in the shared restart-drain): a flush with
    # nothing queued would resend nothing, so it is a clean no-op. The stop button, by contrast,
    # interrupts an empty-queue turn too, so the restart-drain itself never short-circuits.
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


def _shoulder_tap_atomic_endpoint(chat_id: str) -> Response:
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
    agent_info = _find_active_agent(chat_id)
    if agent_info is None:
        return _chat_not_found_response(chat_id)
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


def _drain_to_composer_endpoint(chat_id: str) -> Response:
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
    agent_info = _find_active_agent(chat_id)
    if agent_info is None:
        return _chat_not_found_response(chat_id)
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


def _chat_agent_not_found_response(chat_id: str, agent_id: str) -> Response:
    error = ErrorResponse(detail=f"Chat '{chat_id}' has no agent '{agent_id}'")
    return json_response(error.model_dump(), status_code=404)


def _find_chat_agent(chat_id: str, agent_id: str) -> AgentInfo | Response:
    """The agent ``agent_id`` of chat ``chat_id``, or the 404 that says which of the two is missing."""
    agent_info = _find_active_agent(chat_id)
    if agent_info is None:
        return _chat_not_found_response(chat_id)
    # CLEANUP: resolve any member of the chat (an archived one included) through the chat
    # record store once phase 3 of the chat-agent split lands; today a chat's one agent is
    # its active agent.
    if agent_info.id != agent_id:
        return _chat_agent_not_found_response(chat_id, agent_id)
    return agent_info


def _get_subagent_events(chat_id: str, agent_id: str, subagent_session_id: str) -> Response:
    """Get events for one subagent session of one agent of a chat."""
    agent_info = _find_chat_agent(chat_id, agent_id)
    if isinstance(agent_info, Response):
        return agent_info

    watcher = get_state().get_or_create_watcher(agent_info)
    events = watcher.get_all_events(session_id=subagent_session_id)

    # Include metadata in the response
    metadata = watcher.get_subagent_metadata(subagent_session_id)

    return json_response({"events": events, "metadata": metadata})


def _stream_subagent_events(chat_id: str, agent_id: str, subagent_session_id: str) -> Response:
    """SSE stream for a subagent's new events, filtered by session_id."""
    agent_info = _find_chat_agent(chat_id, agent_id)
    if isinstance(agent_info, Response):
        return agent_info

    state = get_state()
    state.get_or_create_watcher(agent_info)

    event_queues = state.event_queues
    event_queue = event_queues.register(agent_info.id)

    return _sse_response(
        _stream_filtered_events(
            agent_info.id,
            event_queues,
            event_queue,
            lambda event: event.get("session_id") == subagent_session_id,
        )
    )


def _get_subagent_events_alias(chat_id: str, subagent_session_id: str) -> Response:
    """The agent-keyed subagent route: the session belongs to the chat's active agent."""
    # CLEANUP: drop with the /api/agents/... aliases in phase 7 of the chat-agent split.
    agent_info = _find_active_agent(chat_id)
    if agent_info is None:
        return _chat_not_found_response(chat_id)
    return _get_subagent_events(chat_id, agent_info.id, subagent_session_id)


def _stream_subagent_events_alias(chat_id: str, subagent_session_id: str) -> Response:
    """The agent-keyed subagent stream: the session belongs to the chat's active agent."""
    # CLEANUP: drop with the /api/agents/... aliases in phase 7 of the chat-agent split.
    agent_info = _find_active_agent(chat_id)
    if agent_info is None:
        return _chat_not_found_response(chat_id)
    return _stream_subagent_events(chat_id, agent_info.id, subagent_session_id)


def _get_screen_capture(chat_id: str) -> Response:
    """Capture the tmux pane content for an agent.

    Returns the visible screen content (and optionally scrollback) as plain
    text. Useful for seeing what's on an agent's terminal when it has no
    Claude session data (e.g., the agent crashed on startup).
    """
    agent_info = _find_active_agent(chat_id)
    if agent_info is None:
        return _chat_not_found_response(chat_id)

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


def _run_create_chat(chat_id_field: str) -> CreatedChat | Response:
    """Create a new chat, as an agent in the primary agent's work directory.

    One endpoint for every harness: the ``chat`` role is the same, and the account the
    chat is bound to (the request's ``account_id``, else the most recently used one)
    decides which harness template the server stacks under it. A request naming an
    ``chat_id`` launches a chat minted earlier -- one that waited for an account, or one
    whose create failed -- under that id, keeping the name it was minted with.

    The chat's display name is minted here (server-side) when the request names
    none: the first free "<word> N" for the harness, counted against every name
    on the machine -- agents and in-flight creates -- so simultaneous creates
    cannot both mint "Chat 1". An
    explicitly requested name that collides answers 409 so the caller can retry
    with another. The response carries the resulting name pair (canonical
    ``name`` + human-readable ``display_name``) beside the agent id.

    A chat created inside a project carries that project's id in the agent's
    ``project`` label, which records where it was started (mngr propagates the
    label to the agent's own children); membership itself is the project's tab
    list, which the shell writes when it docks the chat. ``project_id`` rides
    beside the request model rather than inside it for that reason: it is a
    label on the created agent, not part of the chat's identity.

    ``chat_id_field`` names the body field that carries a minted chat's id: ``chat_id`` on the
    chat route, ``agent_id`` on its agent-keyed alias.
    """
    agent_manager: AgentManager = get_state().agent_manager
    body = parse_json_object_body()
    if isinstance(body, Response):
        return body
    project_id = str(body.get("project_id") or "")
    request_fields = {key: value for key, value in body.items() if key not in ("project_id", chat_id_field)}
    if chat_id_field in body:
        request_fields["chat_id"] = body[chat_id_field]

    try:
        create_request = CreateChatRequest.model_validate(request_fields)
        return agent_manager.create_chat(
            create_request.name,
            # The `first` create template belongs to the workspace's own first run, not to
            # anything a client asks for -- bootstrap stacks it on its own `mngr create`.
            extra_role_templates=(),
            project_id=project_id,
            account_id=create_request.account_id,
            chat_id=create_request.chat_id,
            message=create_request.message,
        )
    except AgentNameConflictError as e:
        return json_response(ErrorResponse(detail=str(e)).model_dump(), status_code=409)
    except (AgentCreationError, OSError, ValueError) as e:
        error = ErrorResponse(detail=str(e))
        return json_response(error.model_dump(), status_code=400)


def _create_chat() -> Response:
    """``POST /api/chats/create``: the created chat's id and name pair, or the refusal."""
    created = _run_create_chat("chat_id")
    if isinstance(created, Response):
        return created
    response = CreateChatResponse(chat_id=created.chat_id, name=created.name, display_name=created.display_name)
    return json_response(response.model_dump(), status_code=201)


def _create_chat_alias() -> Response:
    """``POST /api/agents/create-chat``: the same create, with the chat's id under the alias's ``agent_id`` field."""
    # CLEANUP: drop with the /api/agents/... aliases in phase 7 of the chat-agent split.
    created = _run_create_chat("agent_id")
    if isinstance(created, Response):
        return created
    response = CreateAgentResponse(agent_id=created.chat_id, name=created.name, display_name=created.display_name)
    return json_response(response.model_dump(), status_code=201)


def _discover_with_filters() -> list[AgentInfo]:
    """Discover agents using the app-level filter configuration."""
    state = get_state()
    return discover_agents(
        provider_names=state.provider_names,
        include_filters=state.include_filters,
        exclude_filters=state.exclude_filters,
    )


def _list_agents_endpoint() -> Response:
    """List all mngr-managed agents (the loopback callers' listing: the evals bridge, the deployment tests)."""
    agents = _discover_with_filters()
    items = [AgentListItem(id=agent.id, name=agent.name, state=agent.state) for agent in agents]
    return json_response(AgentListResponse(agents=items).model_dump())


def _list_chats_endpoint() -> Response:
    """List every chat this app lists, as the snapshots the pages see."""
    agent_manager: AgentManager = get_state().agent_manager
    if not agent_manager.is_agent_list_known():
        return _agent_list_not_known_response()
    response = ChatListResponse(chats=tuple(agent_manager.get_chat_snapshots()))
    return json_response(response.model_dump(mode="json"))


def _refuse_primary_agent(agent_state_name: str, labels: dict[str, str], verb: str) -> Response | None:
    """A 400 refusing to destroy or stop the ``is_primary=true`` services agent, or None.

    That agent runs the workspace's supervised services; the frontend never offers it, so this
    is defense in depth for direct callers.
    """
    if labels.get("is_primary") != "true":
        return None
    error = ErrorResponse(
        detail=f"Refusing to {verb} agent '{agent_state_name}': it carries the is_primary=true label (services agent for this workspace)"
    )
    return json_response(error.model_dump(), status_code=400)


def _destroy_chat(chat_id: str) -> Response:
    """Destroy a chat by running ``mngr destroy --force`` on its agent (the instances API's delete does the same)."""
    agent_manager: AgentManager = get_state().agent_manager
    agent_info = _find_active_agent(chat_id)
    if agent_info is None:
        return _chat_not_found_response(chat_id)
    refusal = _refuse_primary_agent(agent_info.name, agent_info.labels, "destroy")
    if refusal is not None:
        return refusal
    try:
        agent_manager.destroy_chat(ChatId(chat_id))
    except AgentDestroyError as e:
        return json_response(ErrorResponse(detail=str(e)).model_dump(), status_code=500)
    return json_response(DestroyAgentResponse(status="ok").model_dump())


def _stop_chat(chat_id: str) -> Response:
    """Stop a chat's agent with ``mngr stop``, the reversible counterpart to a destroy (the instances API's stop does the same)."""
    agent_manager: AgentManager = get_state().agent_manager
    agent_info = _find_active_agent(chat_id)
    if agent_info is None:
        return _chat_not_found_response(chat_id)
    refusal = _refuse_primary_agent(agent_info.name, agent_info.labels, "stop")
    if refusal is not None:
        return refusal
    try:
        agent_manager.stop_chat(ChatId(chat_id))
    except AgentStopError as e:
        return json_response(ErrorResponse(detail=str(e)).model_dump(), status_code=500)
    return json_response(StopAgentResponse(status="ok").model_dump())


def _start_chat(chat_id: str) -> Response:
    """Ensure a chat's agent is running so its terminal session is attachable (the chat's terminal back face calls this).

    The same in-process mngr start path a send uses, so opening the terminal and messaging the
    agent succeed or fail together; a no-op for an already-running agent.
    """
    agent_info = _find_active_agent(chat_id)
    if agent_info is None:
        return _chat_not_found_response(chat_id)
    try:
        start_agent(agent_info.name)
    except MngrError as e:
        error = ErrorResponse(detail=f"Failed to start agent '{agent_info.name}': {e}")
        return json_response(error.model_dump(), status_code=500)
    # The observe stream will not see the revival for minutes (no pid to watch while the
    # agent was stopped); reflect it now so the UI's liveness unblocks with the start.
    get_state().agent_manager.note_agent_alive(agent_info.id)
    return json_response(StartAgentResponse(status="ok").model_dump())


def _presence_endpoint(chat_id: str) -> Response:
    """Record one client's presence report about this chat's page (see ``presence.py``).

    Accepted for any well-formed chat id, a chat still being created included: the
    prioritizer ignores ids it does not manage, and a page that reports before its agent
    exists must not be told it is wrong.
    """
    if not AGENT_ID_PATTERN.fullmatch(chat_id):
        return _chat_not_found_response(chat_id)
    report = parse_request_body(PresenceReport)
    agent_manager: AgentManager = get_state().agent_manager
    agent_manager.record_presence(ChatId(chat_id), report.client_id, report.state)
    return json_response({"status": "ok"})


# The terminal app's registered name: the chat's terminal back face is served from its origin.
_TERMINAL_APP_NAME: Final[str] = "terminal"


def _terminal_origin_label() -> str:
    """The terminal app's origin label from the registry, or "" when no terminal is registered.

    Read per page load rather than watched: a label is minted once per workspace and the
    registry is one small file, and an unreadable registry costs the page its terminal face,
    never the page.
    """
    try:
        rows = read_registry(registry_path())
    except RegistryReadError as e:
        logger.warning("Could not read the app registry for the terminal label: {}", e)
        return ""
    for row in rows:
        if row.name == _TERMINAL_APP_NAME:
            return row.label
    return ""


def _chat_document(key: str) -> Response:
    """Serve the chat page for an instance key: a chat id, or ``<chat-id>.<agent-id>.<session-id>`` for a subagent view."""
    subagent = parse_subagent_key(key)
    if subagent is not None:
        chat_id, agent_id, session_id = subagent.chat_id, subagent.agent_id, subagent.session_id
    elif AGENT_ID_PATTERN.fullmatch(key):
        chat_id, agent_id, session_id = key, "", ""
    else:
        return _chat_not_found_response(key)
    document_path = get_state().static_directory / CHAT_DOCUMENT_FILENAME
    if not document_path.exists():
        _loguru_logger.warning("Served the chat not-built placeholder: no chat bundle at {}", document_path)
        return document_response(_CHAT_NOT_BUILT_HTML, is_frontend_built=False)
    config: Config = get_state().config
    root_path = (request.script_root or "").rstrip("/")
    html_content = document_path.read_text()
    html_content = inject_base_path_meta_tag(html_content, root_path)
    html_content = inject_hostname_meta_tag(html_content)
    html_content = inject_chat_identity_meta_tags(html_content, chat_id, agent_id, session_id)
    html_content = inject_terminal_label_meta_tag(html_content, _terminal_origin_label())
    if config.javascript_plugin_basenames:
        html_content = inject_plugin_script_tags(html_content, config.javascript_plugin_basenames, root_path)
    return document_response(html_content, is_frontend_built=True)


def _serve_file_or_document(path: str) -> Response:
    """Serve an agent-authored file by its absolute on-disk path, else the chat page for a one-segment key.

    A chat page's markdown names files by their absolute path (``/home/user/...``), which the
    browser resolves against this origin; ``try_serve_file`` answers those (an image inline,
    any other existing file as a download). A single path segment that is no file is a chat
    page's key; anything else is not found.
    """
    file_response = try_serve_file(path)
    if file_response is not None:
        return file_response
    if "/" in path:
        return json_response(ErrorResponse(detail=f"Nothing is served at '/{path}'").model_dump(), status_code=404)
    return _chat_document(path)


def _favicon() -> Response:
    favicon_path = get_state().static_directory / "favicon.ico"
    if favicon_path.exists():
        return send_file(favicon_path, mimetype="image/x-icon")
    return Response(status=404)


def _serve_asset(filename: str) -> Response:
    """The chat page's built bundle (``static/assets/``), by the hashed name the document links."""
    assets_directory = get_state().static_directory / "assets"
    # A missing asset is a plain 404 rather than the HTML error page ``send_from_directory``
    # would raise. Existence and safety are both left to ``send_from_directory``: ``filename``
    # arrives with any ``..`` segments intact, so joining it onto the directory ourselves would
    # stat paths outside it -- an existence oracle for the whole filesystem.
    try:
        return send_from_directory(assets_directory, filename)
    except NotFound:
        return Response(status=404)


def _health_endpoint() -> Response:
    """The probe route (contracts.md section 5): alive, and whether the built chat page is being served."""
    is_frontend_built = (get_state().static_directory / CHAT_DOCUMENT_FILENAME).exists()
    return json_response({"status": "ok", "is_frontend_built": is_frontend_built})


def _serve_static_file(basename: str) -> Response:
    """Serve one of the configured JavaScript plugins or static files by basename."""
    config: Config = get_state().config
    file_path_string = config.static_file_basename_to_path.get(basename)
    if file_path_string is None:
        return json_response({"detail": f"Static file '{basename}' not found"}, status_code=404)
    file_path = Path(file_path_string)
    if not file_path.is_file():
        return json_response({"detail": f"Static file not found on disk: {file_path}"}, status_code=404)
    return send_file(file_path)


def _ws_endpoint(websocket: Any) -> None:
    """The chat pages' socket: the chat snapshots and the provisional-chat events, as the manager broadcasts them."""
    _run_ws_broadcast_loop(websocket=websocket, agent_manager=get_state().agent_manager)


def _run_ws_broadcast_loop(websocket: Any, agent_manager: AgentManager) -> None:
    """Stream the broadcaster's messages to ``websocket`` until the page disconnects.

    Each connection owns its own thread (flask-sock + the threaded WSGI server), so this loop
    blocks on the per-client queue and forwards messages. flask-sock's keepalive closes a
    half-dead peer, surfacing as ``ConnectionClosed`` from ``send``; the broadcaster can also
    evict a hopelessly-behind client by pushing the shutdown sentinel (``None``).
    """
    ws_broadcaster = agent_manager.broadcaster
    client_queue = ws_broadcaster.register()
    _loguru_logger.info("WS /api/ws connection opened (conn {})", id(client_queue))
    disconnect_reason = "handler exited"
    try:
        # The connect-time replay: every provisional chat this process holds, then the chat
        # list. The list comes last on purpose -- it is how a page knows the replay is over,
        # so a record it still holds that this process did not replay (a create the previous
        # process was running) can be dropped rather than waited on forever.
        for provisional in agent_manager.get_provisional_chats():
            websocket.send(json.dumps(provisional_chat_created_message(provisional)))
        websocket.send(json.dumps(chats_updated_message(agent_manager.get_chat_snapshots())))
        shutdown = False
        while not shutdown:
            # The pages send nothing; anything that arrives is drained and ignored.
            while websocket.receive(timeout=0) is not None:
                pass
            try:
                message = client_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if message is None:
                shutdown = True
                disconnect_reason = "shutdown sentinel (evicted by broadcaster or server shutdown)"
            else:
                websocket.send(message)
    except ConnectionClosed:
        disconnect_reason = "connection closed"
    finally:
        _loguru_logger.info("WS /api/ws connection closed (conn {}, reason: {})", id(client_queue), disconnect_reason)
        ws_broadcaster.unregister(client_queue)


# Every per-chat route, as ``(suffix, view, methods)``: served at ``/api/chats/<chat_id>/<suffix>``
# and, until phase 7 of the chat-agent split, at the agent-keyed alias ``/api/agents/<chat_id>/<suffix>``,
# whose path parameter is a chat id too.
_PER_CHAT_ROUTES: Final[tuple[tuple[str, Callable[..., Response], tuple[str, ...]], ...]] = (
    ("destroy", _destroy_chat, ("POST",)),
    ("start", _start_chat, ("POST",)),
    ("stop", _stop_chat, ("POST",)),
    ("events", _get_events, ("GET",)),
    ("events/<event_id>/detail", _get_event_detail, ("GET",)),
    ("stream", _stream_events, ("GET",)),
    ("message", _send_message_endpoint, ("POST",)),
    ("presence", _presence_endpoint, ("POST",)),
    ("model", _set_model_choice_endpoint, ("POST",)),
    ("model-options", _get_model_options_endpoint, ("GET",)),
    ("powered-by", _get_powered_by_endpoint, ("GET",)),
    ("fast-mode-answered", _mark_fast_mode_prompt_answered, ("POST",)),
    ("interrupt", _interrupt_agent_endpoint, ("POST",)),
    ("flush-queue", _flush_queue_endpoint, ("POST",)),
    ("shoulder-tap-atomic", _shoulder_tap_atomic_endpoint, ("POST",)),
    ("drain-to-composer", _drain_to_composer_endpoint, ("POST",)),
    ("screen", _get_screen_capture, ("GET",)),
)


def _add_chat_route(
    application: Flask, suffix: str, view_func: Callable[..., Response], methods: tuple[str, ...]
) -> None:
    """Register one per-chat route under ``/api/chats/`` and its agent-keyed alias, on one view function."""
    application.add_url_rule(f"/api/chats/<chat_id>/{suffix}", view_func=view_func, methods=list(methods))
    # CLEANUP: drop the alias in phase 7 of the chat-agent split, once every caller (the minds
    # evals bridge, the deployment tests, the e2e runner) targets /api/chats/.
    application.add_url_rule(f"/api/agents/<chat_id>/{suffix}", view_func=view_func, methods=list(methods))


def create_application(state: ChatAppState) -> Flask:
    """Assemble the chat app around an already-built ``ChatAppState``.

    A pure assembler: routes and error handling only, no collaborators built, nothing
    started. The instances blueprint is mounted here over the agent manager; its nudger
    fires whatever nudger the manager holds (``main`` installs the real one), so a test that
    builds the app posts nothing to the workspace shell.
    """
    # No static folder: Flask would otherwise add a /static/<path> route beside the document route.
    application = Flask(__name__, static_folder=None)
    attach_state(application, state)
    application.register_error_handler(Exception, handle_unhandled_exception)
    # The presence route reads its body through the library's parser, so its errors answer
    # like the blueprint's: a status from the contract with a ``{"detail"}`` body.
    application.register_error_handler(AppInstancesError, answer_typed_error)
    sock = build_sock(application)

    source, nudger = build_chat_instance_source(state.agent_manager, start_agent)
    application.register_blueprint(build_instances_blueprint(source, nudger))

    application.add_url_rule("/favicon.ico", view_func=_favicon, methods=["GET"])
    application.add_url_rule("/assets/<path:filename>", view_func=_serve_asset, methods=["GET"])
    application.add_url_rule("/api/health", view_func=_health_endpoint, methods=["GET"])
    sock.route("/api/ws")(_ws_endpoint)
    application.add_url_rule("/plugins/<basename>", view_func=_serve_static_file, methods=["GET"])
    application.add_url_rule("/api/agents", view_func=_list_agents_endpoint, methods=["GET"])
    application.add_url_rule("/api/chats", view_func=_list_chats_endpoint, methods=["GET"])
    application.add_url_rule("/api/chats/create", view_func=_create_chat, methods=["POST"])
    application.add_url_rule("/api/agents/create-chat", view_func=_create_chat_alias, methods=["POST"])
    application.add_url_rule("/api/harnesses", view_func=_get_harnesses_endpoint, methods=["GET"])
    application.add_url_rule("/api/uploads", view_func=_upload_attachment, methods=["POST"])
    application.add_url_rule("/api/uploads/<path:relative_path>", view_func=_serve_attachment, methods=["GET"])
    application.add_url_rule(
        "/api/uploads/<path:relative_path>",
        view_func=_delete_attachment,
        methods=["DELETE"],
        endpoint="_delete_attachment",
    )
    for suffix, view_func, methods in _PER_CHAT_ROUTES:
        _add_chat_route(application, suffix, view_func, methods)
    application.add_url_rule(
        "/api/chats/<chat_id>/agents/<agent_id>/subagents/<subagent_session_id>/events",
        view_func=_get_subagent_events,
        methods=["GET"],
    )
    application.add_url_rule(
        "/api/chats/<chat_id>/agents/<agent_id>/subagents/<subagent_session_id>/stream",
        view_func=_stream_subagent_events,
        methods=["GET"],
    )
    application.add_url_rule(
        "/api/agents/<chat_id>/subagents/<subagent_session_id>/events",
        view_func=_get_subagent_events_alias,
        methods=["GET"],
    )
    application.add_url_rule(
        "/api/agents/<chat_id>/subagents/<subagent_session_id>/stream",
        view_func=_stream_subagent_events_alias,
        methods=["GET"],
    )
    auth_endpoints.register_routes(application)
    accounts_endpoints.register_routes(application)
    latchkey_endpoints.register_routes(application)

    application.add_url_rule("/<path:path>", view_func=_serve_file_or_document, methods=["GET"])

    return application
