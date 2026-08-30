"""Parse a codex agent's raw rollout JSONL into the web-UI event schema.

Codex writes its conversation as a "rollout" -- append-only JSONL where each line
is ``{"timestamp", "type", "payload": {"type", ...}}``. mngr_codex mirrors the live
rollout verbatim (no reschematising) to a stable per-agent path
``<agent_state_dir>/logs/codex_transcript/events.jsonl`` (its ``stream_transcript.sh``),
which is what :class:`CodexSessionWatcher` tails.

This module maps those raw rollout lines into the *exact* dict shape the web UI
consumes -- the same shape ``claude_session_parser`` emits for claude -- so the
transport (SSE), the frontend, and the activity tracker need no codex-specific
branches. It is the codex analogue of ``claude_session_parser``.

Sourcing rule (confirmed against codex ``policy.rs`` + real rollouts):
``response_item`` lines are the canonical
conversation state; ``event_msg`` lines are a derived live-display stream. We build
the body from ``response_item`` -- **except** two things taken from ``event_msg``:
(1) user bubbles, the clean human-typed prompt; and (2) the ``turn_aborted`` marker
(a user interrupt), used to clear a stuck activity dot. We take the user bubble from
the display stream rather than the canonical one because ``response_item`` role=user
is the *model-facing* user role: the human prompt PLUS injected ``AGENTS.md`` /
``<environment_context>`` / ``<turn_aborted>`` / ``<subagent_notification>`` content
with no field marking which is which, so it cannot be shown as-is. The display stream
labels the human turn explicitly, which is the clean signal.

Codex has emitted that human turn under two shapes across versions: older codex as
``event_msg`` ``user_message``; newer codex folds every display echo into
``event_msg`` ``item_completed`` with a typed ``item`` (``UserMessage`` for the human
turn, plus ``AgentMessage`` / ``CommandExecution`` / ``Reasoning`` display duplicates
of the canonical ``response_item`` lines). We accept both user-turn shapes and ignore
the rest of ``item_completed`` (already covered by ``response_item``). Everything else
in ``event_msg`` (``agent_message`` echoes, ``token_count``) is skipped in this core cut.

Lossy by design for this first cut -- all deferred to later slices: ``usage``
(``token_count`` -> Phase 2, and coarse), ``is_auth_error`` (lives in codex's
``logs_2.sqlite``, never the transcript), subagent linkage, tk step-progress.
``stop_reason`` is left null.

Event ids prefer codex's own stable identity (the assistant message ``id``, or a
tool call's ``call_id``) so the watcher dedups codex 0.144.3's re-serialised
duplicates (the same message written to the rollout more than once by the
"paginated" / world_state persistence). Where codex gives no id (an ``event_msg``
``user_message``), we synthesise one from its timestamp + text (see
``_stable_user_event_id``) rather than the physical line index -- position-independent,
so if a rollout is compressed and re-materialised (repointing the marker and forcing a
re-read from byte 0) the same user bubble dedups instead of duplicating.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import Final

from loguru import logger as _loguru_logger

from imbue.system_interface.harnesses.auth_errors import is_auth_error_text
from imbue.system_interface.harnesses.codex.tool_labels import is_single_delegated_call
from imbue.system_interface.harnesses.codex.tool_labels import shell_command
from imbue.system_interface.harnesses.codex.tool_labels import tool_labels
from imbue.system_interface.harnesses.error_patterns import classify_api_error
from imbue.system_interface.harnesses.error_patterns import is_provider_fault
from imbue.system_interface.harnesses.events import SPECIAL_EVENT_TYPE
from imbue.system_interface.harnesses.events import SpecialEventKind
from imbue.system_interface.harnesses.message_display import stamp_user_message_display
from imbue.system_interface.harnesses.tool_output import classify_tool_call_display
from imbue.system_interface.harnesses.tool_output import error_snippet
from imbue.system_interface.harnesses.tool_output import find_permission_request
from imbue.system_interface.harnesses.tool_output import is_pure_tk_lifecycle_command
from imbue.system_interface.harnesses.tool_output import is_tk_lifecycle_anywhere
from imbue.system_interface.harnesses.tool_output import tk_stamp

logger = _loguru_logger

# Kept as ``codex/common_transcript`` to match the ``<harness>/common_transcript``
# label ``claude_session_parser`` stamps -- "common" here means the normalized/common
# event *form*, not the on-disk common-transcript file (which we do NOT read).
# Nothing in the pipeline branches on this string.
SOURCE = "codex/common_transcript"

# Codex rollout messages never carry a per-message model slug, so surface the same
# placeholder ``claude_session_parser`` uses when the model is absent, keeping the
# frontend's non-optional ``model`` field populated.
_UNKNOWN_MODEL = "unknown"

# codex's own `codex_error_info.type` tags, mapped to the shared kind vocabulary. Preferred over
# reading the prose: the tag is the part that survives codex rewording its messages. Quota
# exhaustion is deliberately absent -- `usage_limit_exceeded` belongs to the auth family, whose
# recovery is different credentials, and `auth_errors` claims it before this table is consulted.
_CODEX_ERROR_KINDS: Final[dict[str, str]] = {
    "server_error": "api_error",
    "internal_server_error": "api_error",
    "rate_limit_exceeded": "rate_limit",
    "rate_limit": "rate_limit",
    "overloaded": "overloaded",
    "context_window_exceeded": "request_too_large",
}


def _join_output_text(content: Any) -> str:
    """Join the ``text`` of ``content`` blocks whose ``type`` is ``output_text``."""
    if not isinstance(content, list):
        return ""
    return "".join(
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "output_text" and block.get("text")
    )


def _output_text(output: Any) -> str:
    """A ``*_output.output`` is either a string or a list of content items; flatten to the
    UNTRUNCATED text (truncation happens at the emit site, after the structured facts the
    chat needs are lifted out whole -- see ``harnesses/tool_output``)."""
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        parts: list[str] = []
        for item in output:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("output") or ""))
            elif isinstance(item, str):
                parts.append(item)
            else:
                # other item shapes carry no text
                continue
        return "".join(parts)
    return "" if output is None else str(output)


def _tool_call_raw_input(payload: dict[str, Any]) -> str:
    """The tool call's raw, untruncated input. ``function_call`` carries ``arguments``
    (a JSON string); ``custom_tool_call`` carries ``input`` (raw text, e.g. an
    apply_patch body)."""
    raw = payload.get("arguments")
    if raw is None:
        raw = payload.get("input")
    return "" if raw is None else str(raw)


def _labelled_tool_call(call_id: str, tool_name: str, raw_input: str) -> dict[str, Any]:
    """A tool call carrying its own human labels.

    Labelled here, where the harness is known, so the frontend renders a string
    rather than having to understand that a codex ``exec`` hides its real operation
    in a JavaScript argument. Labels come from the FULL input; the input itself stays
    off the event (the payload-free wire contract) and is served whole by the detail
    endpoint on expand.
    """
    header_label, caption_label = tool_labels(tool_name, raw_input)
    tool_call: dict[str, Any] = {
        "tool_call_id": call_id,
        "tool_name": tool_name,
        "input_chars": len(raw_input),
        "header_label": header_label,
        "caption_label": caption_label,
    }
    # The render decision ships with the call (a hidden tk marker, or the permission
    # card), recognised from the UNTRUNCATED input backend-side.
    #
    # Both verdicts claim the call is ONLY the thing they name, and both are derived from the
    # FIRST delegated call in the program -- so on a batched program they are claims we cannot
    # check. Getting it wrong is not cosmetic: `tk start s1` followed by real work classified
    # HIDDEN and the real work vanished from the chat entirely, which is the exact failure the
    # standalone policies exist to prevent. A batched program renders as ordinary work instead.
    command = shell_command(tool_name, raw_input)
    is_pure_tk = command is not None and is_pure_tk_lifecycle_command(command)
    if is_single_delegated_call(raw_input):
        display = classify_tool_call_display(is_pure_tk=is_pure_tk, raw_input=raw_input)
    else:
        display = None
    if display is not None:
        tool_call["display"] = display.value
    # The step progress view reads step titles/summaries out of a tk lifecycle command
    # itself, so that one command is stamped resident.
    if command is not None and is_tk_lifecycle_anywhere(command):
        tool_call["tk_command"] = command
    return tool_call


def _assistant_event(
    timestamp: str, event_id: str, *, text: str, tool_calls: list[dict[str, Any]], model: str = _UNKNOWN_MODEL
) -> dict[str, Any]:
    return {
        "timestamp": timestamp,
        "type": "assistant_message",
        "event_id": event_id,
        "source": SOURCE,
        "role": "assistant",
        # The EFFECTIVE per-turn model, sourced from the rollout's ``turn_context.model`` (the model
        # the turn actually RAN on -- which can be a framework fallback differing from the selected
        # setting). ``_UNKNOWN_MODEL`` only until the first ``turn_context`` is seen (§4b).
        "model": model,
        "text": text,
        "tool_calls": tool_calls,
        # deferred (derive from task_complete later)
        "stop_reason": None,
        # deferred (token_count -> Phase 2)
        "usage": None,
        "message_uuid": event_id,
        # A codex failure never arrives as an assistant message -- it arrives on the turn's
        # `task_complete`, which `_turn_error_event` turns into its own message. So an ordinary
        # assistant message is never an error, and these are facts rather than deferrals.
        "is_auth_error": False,
        "is_api_error": False,
        "api_error_kind": None,
        "is_provider_fault": False,
    }


def _turn_error_event(
    marker_event_id: str,
    timestamp: str,
    detail: str,
    payload: dict[str, Any],
    turn_state: dict[str, Any] | None,
) -> dict[str, Any]:
    """The failure that ended a turn, as a message the transcript can show.

    Classified off ``codex_error_info`` where codex tells us the kind outright, and off the
    prose otherwise -- the structured tag is the part that survives codex rewording its
    messages. Auth wins either way: `classify_api_error` yields to the auth vocabulary, so a
    message never carries both subtexts.
    """
    error = payload.get("error")
    info = error.get("codex_error_info") if isinstance(error, dict) else None
    tag = info.get("type", "") if isinstance(info, dict) else ""
    is_auth = is_auth_error_text(detail) or is_auth_error_text(str(tag))
    api_error_kind = None if is_auth else (_CODEX_ERROR_KINDS.get(str(tag)) or classify_api_error(detail))
    return {
        "timestamp": timestamp,
        "type": "assistant_message",
        # Distinct from the marker's id, which the marker still uses: two events from one
        # record need two ids or the frontend dedupes one of them away.
        "event_id": f"{marker_event_id}:error",
        "source": SOURCE,
        "role": "assistant",
        "model": (turn_state or {}).get("model") or _UNKNOWN_MODEL,
        "text": detail,
        "tool_calls": [],
        "stop_reason": "error",
        "usage": None,
        "message_uuid": f"{marker_event_id}:error",
        "is_auth_error": is_auth,
        "is_api_error": api_error_kind is not None,
        "api_error_kind": api_error_kind,
        "is_provider_fault": is_provider_fault(api_error_kind),
    }


def iso_timestamp_to_epoch_ms(timestamp: str) -> int | None:
    """Parse an ISO-8601 rollout timestamp into integer epoch-milliseconds, or ``None``.

    The rollout's user-bubble timestamp (e.g. ``2026-08-12T09:20:13.296Z``) equals the
    app-server ``userMessage`` item's ``completedAtMs`` to the millisecond (verified live),
    so normalising both channels to epoch-ms lets the file reader and the live ledger derive
    the SAME anonymous user-turn id for an untagged (no ``client_id``) message. A naive/empty
    timestamp yields ``None`` (the id then omits the ms component)."""
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def codex_user_turn_event_id(client_id: str | None, epoch_ms: int | None, content: str) -> str:
    """The web-UI event id for a user turn -- the shared join key across the two channels.

    A user message is the one thing that is both a live edge (owned by the subscribed ledger)
    and a committed transcript turn (owned by the rollout file reader), so both must derive the
    SAME id or the message double-shows when the live copy meets the file copy on backfill (A3b).

    Primary key: codex's echoed ``client_id``. It is the ONLY identity present in BOTH the
    app-server ``userMessage`` item (``clientId``) and the rollout ``event_msg`` user bubble
    (``client_id``); the app-server ``item.id`` is deliberately not written to the rollout, so
    it cannot join the two (verified live against codex 0.147). An untagged foreign turn carries
    no ``client_id``, so the id falls back to the commit epoch-ms plus a content hash -- stable
    across a re-read of the same rollout (codex re-materialises compressed rollouts), and equal
    across the two channels because the rollout timestamp equals the item's ``completedAtMs``."""
    if client_id:
        return f"codex-user-cid-{client_id}"
    digest = hashlib.sha1(content.encode("utf-8", "replace")).hexdigest()[:16]
    if epoch_ms is not None:
        return f"codex-user-anon-{epoch_ms}-{digest}"
    return f"codex-user-anon-{digest}"


def build_user_turn_event(timestamp: str, content: str, event_id: str) -> dict[str, Any]:
    """The web-UI ``user_message`` event dict -- the single shape both channels emit.

    The rollout file reader builds it on hydration; the live ledger builds an identical one at
    commit. Keeping one builder guarantees the two are byte-identical apart from the (matching)
    ``event_id``, so the frontend renders and dedups them as one message."""
    event: dict[str, Any] = {
        "timestamp": timestamp,
        "type": "user_message",
        "event_id": event_id,
        "source": SOURCE,
        "role": "user",
        "content": content,
        "message_uuid": event_id,
    }
    # The shared render decision (fleet nudges, task notifications, latchkey verdicts,
    # model-bar traffic) -- codex gets the same detector table as claude and pi.
    stamp_user_message_display(event, content)
    return event


def _item_content_text(content: Any) -> str | None:
    """Join the text of an ``item_completed`` item's ``content`` blocks, or None.

    The new item schema carries the human prompt as ``content: [{type, text}, ...]``.
    We take any block's ``text`` (a user turn is a single text block in practice).
    """
    if not isinstance(content, list):
        return None
    text = "".join(
        block.get("text", "") for block in content if isinstance(block, dict) and isinstance(block.get("text"), str)
    )
    return text or None


def _marker_event_id(payload: dict[str, Any], payload_type: str, line_index: int) -> str:
    """The event id for a turn-lifecycle marker, keyed on codex's own ``turn_id``.

    The spine's rule: an ``event_id`` must be the harness's own STABLE id, never a
    physical counter (a counter changes when a rollout is re-materialised from byte 0,
    duplicating the marker, and makes truncation/supersession inexpressible). Codex's
    ``task_started`` / ``task_complete`` / ``turn_aborted`` carry a ``turn_id`` (the
    started/complete pair share one, so the ``payload_type`` suffix keeps them
    distinct). Falls back to the line index only if a marker ever arrives without one.
    """
    turn_id = payload.get("turn_id")
    if isinstance(turn_id, str) and turn_id:
        return f"codex-turn-{turn_id}-{payload_type}"
    return f"codex-{line_index}-{payload_type}"


def _marker_turn_id(payload: dict[str, Any]) -> str | None:
    """Codex's own ``turn_id`` for a turn-lifecycle marker, or None when absent.

    The id already rides inside the event_id (``codex-turn-<turn_id>-<payload_type>``), but
    the atomic shoulder-tap needs it directly to ABA-gate the flush against the live open
    turn (see ``activity_state.current_open_turn_id``), so it is surfaced as an explicit
    field on the ``turn_started`` / ``turn_completed`` / ``turn_aborted`` special events.
    """
    turn_id = payload.get("turn_id")
    return turn_id if isinstance(turn_id, str) and turn_id else None


def _user_message_events(timestamp: str, text: str | None, client_id: str | None = None) -> list[dict[str, Any]]:
    """The single user-bubble event for a human prompt, or ``[]`` when there is no text.

    Both the old ``event_msg`` ``user_message`` and the new ``item_completed``
    ``UserMessage`` forms route here, sharing one event id (see
    :func:`codex_user_turn_event_id`) so a rollout that somehow carried both dedups to one
    bubble -- and so the SAME message emitted live by the subscribed ledger dedups against this
    committed copy. The id keys on the echoed ``client_id`` when present (the cross-channel join
    key), else on the commit epoch-ms + content.
    """
    if not text:
        return []
    event_id = codex_user_turn_event_id(client_id, iso_timestamp_to_epoch_ms(timestamp), text)
    return [build_user_turn_event(timestamp, text, event_id)]


# --- Queue ledger (the codex analogue of Claude's queue-operation records) ---
#
# A message the user submits while a turn is running is held in codex's TUI queue and
# does not reach the rollout until the turn ends. The patched codex binary writes a full
# queue LEDGER to ``$CODEX_HOME/queued_input.jsonl`` (a sidecar, not the rollout), one
# JSON object per line, keyed by a stable ``queued_id`` (see the fork's
def parse_lines(
    record: dict[str, Any],
    line_index: int,
    tool_name_by_call_id: dict[str, str],
    turn_state: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Map one codex rollout line to zero or more UI event dicts (``[]`` to skip).

    Returns a *list* because one rollout line can expand to more than one event.

    ``line_index`` is the stable physical line number (for event-id synthesis).
    ``tool_name_by_call_id`` is a mutable cross-line map so a ``function_call_output``
    can recover its tool name from the earlier ``function_call``.
    ``turn_state`` is a mutable cross-line dict carrying the EFFECTIVE per-turn model/effort read
    from each ``turn_context`` line (§4b): a ``turn_context`` updates it and every following
    assistant message is stamped with it, so the bar can reflect the model the turn actually ran on
    (a framework fallback, not just the selected setting). ``None`` disables the tracking (the model
    stays ``_UNKNOWN_MODEL``) -- used by callers that only want the transcript events.
    """
    outer = record.get("type")
    payload = record.get("payload")
    timestamp = record.get("timestamp", "")
    if not isinstance(payload, dict) or not isinstance(timestamp, str):
        return []
    payload_type = payload.get("type")

    # --- turn_context: the per-turn effective model/effort (§4b) ---
    # Not a transcript event (returns []), but its ``model`` / ``effort`` are the truth of what the
    # turn ran on. Record them in ``turn_state`` so the following assistant messages are stamped and
    # the watcher can reflect a fallback in the model bar.
    if outer == "turn_context":
        if turn_state is not None:
            context_model = payload.get("model")
            if isinstance(context_model, str) and context_model:
                turn_state["model"] = context_model
                context_effort = payload.get("effort")
                turn_state["effort"] = context_effort if isinstance(context_effort, str) and context_effort else None
        return []

    # --- event_msg: the clean human prompt + the turn-abort marker ---
    if outer == "event_msg":
        # The clean human prompt. Older codex emitted it as ``user_message``; newer
        # codex folds every display echo into ``item_completed`` carrying a typed
        # ``item``, so the human turn is now ``item_completed`` with
        # ``item.type == "UserMessage"``. We handle both forms: they do not co-occur
        # in a given codex version, and both derive the same content-based event id,
        # so a transitional rollout that carried both would still dedup to one bubble.
        # (Only the user bubble ever came from this display stream -- assistant text
        # and tool calls are sourced from the canonical ``response_item`` lines, which
        # this codex version left unchanged, so nothing else here needs to move.)
        if payload_type == "user_message":
            text = payload.get("message")
            client_id = payload.get("client_id")
            return _user_message_events(
                timestamp,
                text if isinstance(text, str) else None,
                client_id if isinstance(client_id, str) else None,
            )
        if payload_type == "item_completed":
            item = payload.get("item")
            if isinstance(item, dict) and item.get("type") == "UserMessage":
                client_id = item.get("clientId")
                return _user_message_events(
                    timestamp,
                    _item_content_text(item.get("content")),
                    client_id if isinstance(client_id, str) else None,
                )
            # Other item_completed items (AgentMessage, CommandExecution, Reasoning)
            # are display duplicates of the response_item lines we already parse; skip.
            return []
        # A user interrupt aborts the turn. Codex does NOT persist the synthetic
        # aborted tool output, so an in-flight tool call would otherwise stay
        # unmatched forever and pin the activity dot at "Running". Emit a lightweight
        # turn_aborted marker; the activity layer treats it as resolving every
        # still-open tool call (see ``activity_state.pending_tool_call``).
        if payload_type == "turn_aborted":
            event_id = _marker_event_id(payload, "turn_aborted", line_index)
            return [
                {
                    "timestamp": timestamp,
                    "type": SPECIAL_EVENT_TYPE,
                    "kind": SpecialEventKind.TURN_ABORTED.value,
                    "event_id": event_id,
                    "turn_id": _marker_turn_id(payload),
                    "source": SOURCE,
                    "message_uuid": event_id,
                }
            ]
        # Turn-lifecycle markers (task == turn). Codex writes these to the rollout in
        # real time -- ``task_started`` the instant the turn begins, ``task_complete``
        # when it ends -- so the activity layer can bracket "the agent is working"
        # (the codex tracker's folded turn latch). ``task_complete`` lands just after
        # the final assistant message, so the dot clears only once the text is already
        # on screen.
        if payload_type in ("task_started", "task_complete"):
            kind = SpecialEventKind.TURN_STARTED if payload_type == "task_started" else SpecialEventKind.TURN_COMPLETED
            event_id = _marker_event_id(payload, payload_type, line_index)
            marker: dict[str, Any] = {
                "timestamp": timestamp,
                "type": SPECIAL_EVENT_TYPE,
                "kind": kind.value,
                "event_id": event_id,
                "turn_id": _marker_turn_id(payload),
                "source": SOURCE,
                "message_uuid": event_id,
            }
            # A turn that ended on a failure carries the reason here, and this is the ONLY
            # durable copy of it: codex classes its live ``EventMsg::Error`` non-persistent, so
            # it never reaches the rollout. Keeping it on the marker alone meant the reason
            # existed but was never shown, and once the turn ended it was unrecoverable.
            #
            # Measured: a bogus key ends the turn with `error.message = "unexpected status 401
            # Unauthorized: Incorrect API key provided: ... auth error code: invalid_api_key"`.
            error = payload.get("error")
            detail = error.get("message", "") if isinstance(error, dict) else ""
            if not detail:
                return [marker]
            marker["error_text"] = detail
            marker["is_auth_error"] = is_auth_error_text(detail)
            # Surfaced as an assistant message ordered BEFORE the marker, so the transcript
            # reads in the order things happened: the failure, then the turn ending.
            return [_turn_error_event(event_id, timestamp, detail, payload, turn_state), marker]
        return []

    if outer != "response_item":
        # session_meta / other non-content records -> drop (turn_context handled above).
        return []

    # --- reasoning: codex's readable thinking summaries ---
    # A reasoning item precedes the assistant output it belongs to. It is not itself a
    # transcript event; the watcher consumes this internal marker to remember the line as
    # the NEXT assistant event's thinking source (has_thinking + the detail endpoint).
    # Only a summary with readable text counts -- the encrypted content is useless.
    if payload_type == "reasoning":
        return [{"type": THINKING_SOURCE_MARKER_TYPE, "readable": bool(_reasoning_summary_text(payload))}]

    # The effective model to stamp on this response's assistant events -- the latest turn_context's
    # model (the model the turn ran on), or the placeholder until one is seen (§4b).
    effective_model = turn_state.get("model") if turn_state is not None else None
    effective_model = effective_model if isinstance(effective_model, str) and effective_model else _UNKNOWN_MODEL

    # --- response_item: assistant messages + tool calls/results ---
    if payload_type == "message":
        if payload.get("role") == "assistant":
            # codex re-serialises history; each copy shares the message ``id``, so
            # keying the event id on it dedups the copies (fall back to line index).
            msg_id = payload.get("id")
            event_id = f"codex-{msg_id}" if isinstance(msg_id, str) and msg_id else f"codex-{line_index}-assistant"
            return [
                _assistant_event(
                    timestamp,
                    event_id,
                    text=_join_output_text(payload.get("content")),
                    tool_calls=[],
                    model=effective_model,
                )
            ]
        # role=user (and developer/system) -> skip; user bubbles come from event_msg.
        return []

    if payload_type in ("function_call", "custom_tool_call"):
        call_id = str(payload.get("call_id", ""))
        tool_name = str(payload.get("name", ""))
        if call_id and tool_name:
            tool_name_by_call_id[call_id] = tool_name
        # Same dedup rationale: a re-serialised tool call keeps its ``call_id``.
        event_id = f"codex-call-{call_id}" if call_id else f"codex-{line_index}-assistant"
        return [
            _assistant_event(
                timestamp,
                event_id,
                text="",
                tool_calls=[_labelled_tool_call(call_id, tool_name, _tool_call_raw_input(payload))],
                model=effective_model,
            )
        ]

    if payload_type in ("function_call_output", "custom_tool_call_output"):
        call_id = str(payload.get("call_id", ""))
        event_id = f"codex-result-{call_id}" if call_id else f"codex-{line_index}-tool_result"
        raw_output = _output_text(payload.get("output"))
        # The structured facts lifted from the full output, which itself stays off the
        # event (the payload-free wire contract): the permission-request object the card
        # renders from, the tk stamp the step view reads, and the error snippet.
        permission_request = find_permission_request(raw_output)
        # A failed code-mode script writes output starting with "Script failed".
        is_error = raw_output.startswith("Script failed")
        event: dict[str, Any] = {
            "timestamp": timestamp,
            "type": "tool_result",
            "event_id": event_id,
            "source": SOURCE,
            "tool_call_id": call_id,
            "tool_name": tool_name_by_call_id.get(call_id, ""),
            "output_chars": len(raw_output),
            "is_error": is_error,
            "message_uuid": event_id,
        }
        if permission_request is not None:
            event["permission_request"] = permission_request.details
        snippet = error_snippet(raw_output) if is_error else ""
        if snippet:
            event["error_snippet"] = snippet
        stamped_tk = tk_stamp(raw_output)
        if stamped_tk:
            event["tk_stamp"] = stamped_tk
        return [event]

    return []


# Internal marker (never ingested into the store, never on the wire): tells the watcher
# that this rollout line carries readable reasoning for the next assistant event.
THINKING_SOURCE_MARKER_TYPE: Final[str] = "_codex_thinking_source"


def _reasoning_summary_text(payload: dict[str, Any]) -> str:
    """The readable text of a reasoning item's ``summary`` blocks ('' = none)."""
    summary = payload.get("summary")
    if not isinstance(summary, list):
        return ""
    parts = [
        block.get("text", "")
        for block in summary
        if isinstance(block, dict) and isinstance(block.get("text"), str) and block.get("text")
    ]
    return "\n\n".join(parts)


def parse_line_detail(record: dict[str, Any], line_index: int) -> dict[str, dict[str, Any]]:
    """Full deferred payloads by event_id for one raw codex rollout line.

    The read half of the payload-free wire contract: tool inputs and outputs are re-read
    whole from the rollout on expand. Thinking is handled separately (the reasoning item is
    a DIFFERENT line than the assistant event; see :func:`parse_reasoning_detail`).
    """
    payload = record.get("payload")
    if record.get("type") != "response_item" or not isinstance(payload, dict):
        return {}
    payload_type = payload.get("type")
    if payload_type in ("function_call", "custom_tool_call"):
        call_id = str(payload.get("call_id", ""))
        event_id = f"codex-call-{call_id}" if call_id else f"codex-{line_index}-assistant"
        return {
            event_id: {
                "inputs_by_tool_call_id": {call_id: _tool_call_raw_input(payload)},
                "output": None,
                "thinking": None,
            }
        }
    if payload_type in ("function_call_output", "custom_tool_call_output"):
        call_id = str(payload.get("call_id", ""))
        event_id = f"codex-result-{call_id}" if call_id else f"codex-{line_index}-tool_result"
        return {
            event_id: {"inputs_by_tool_call_id": {}, "output": _output_text(payload.get("output")), "thinking": None}
        }
    return {}


def parse_reasoning_detail(record: dict[str, Any]) -> str | None:
    """The readable reasoning text of one rollout reasoning line, or None."""
    payload = record.get("payload")
    if record.get("type") != "response_item" or not isinstance(payload, dict):
        return None
    if payload.get("type") != "reasoning":
        return None
    text = _reasoning_summary_text(payload)
    return text or None
