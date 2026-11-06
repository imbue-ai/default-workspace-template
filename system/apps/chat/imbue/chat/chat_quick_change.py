"""Routing quick look-and-feel changes to a separate Sonnet agent.

On every message sent to a chat, the chat app asks Jev (TypeSafe's decision model) two yes/no
questions in one call, about the message and the last few messages of the conversation (the
user's and the agent's):

1. Is this a quick look-and-feel change to an app (a colour, font size, spacing, showing or hiding an
   element, light/dark styling, restyling a page) that needs no new features, data or backend work?
2. Does it change or depend on something the user asked for earlier that the agent has not yet said
   is done? (A follow-up on unfinished work stays with the chat's own agent, which is doing that work.)

The send waits for the answer before anything is delivered (at most ``ROUTING_TIMEOUT_SECONDS``), so
a routed message never enters the chat agent's conversation:

- Yes to 1 and no to 2: the message is NOT delivered to the chat's own agent. It is forwarded word
  for word to the workspace's quick-change agent (one chat per workspace, created on first use and
  reused, running the ``quick-look-and-feel-change`` skill on Sonnet), together with the app named
  by the current editing lease (the in-progress ``editing service <name>`` tk ticket the update-app
  skill has an agent hold). That agent edits the live app under the lease, refreshes the window, and
  reports with ``system/scripts/report_quick_change.py``.
- Anything else, or Jev cannot be reached or times out: the message goes to the chat's agent as usual.

Two logs live in the chat app's data folder (``data/.apps/chat/`` in a workspace):

- ``quick_changes.jsonl`` (``QUICK_CHANGES_LOG_FILENAME``): the record the chat's own agents read.
  Per routed message, a ``request`` line (request id, chat, time, text, app, Jev's answers), a
  ``forward`` line (handed over, or failed to), and a ``report`` line from the quick-change agent
  (done or failed, and its one-line summary). The update-app skill tells agents to check it. The
  chat shows the request and the report as a turn of its own (``chat_quick_change_feed.py``).
- ``quick_change_routing.jsonl`` (``ROUTING_LOG_FILENAME``): every routing decision for every message,
  with the full Jev request and answer and timestamps, for measuring routing.

Jev is reached with an OpenRouter key saved in the chat app's data folder (``openrouter_api_key``)
when there is one, else through the Imbue LiteLLM proxy the chat's account is signed in to, at its
Jev route (``/jev/v1/systemone``). A chat with neither is never routed. The quick-change agent's
chat id is kept in ``quick_change_agent.json`` beside the logs.
"""

import json
import os
import re
import threading
import time
import uuid
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from enum import auto
from pathlib import Path
from typing import Any
from typing import Final

import httpx
from loguru import logger as _loguru_logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.chat.chat_quick_change_feed import FORWARD_FAILED_TEXT
from imbue.chat.chat_quick_change_feed import overlay_events_for_chat
from imbue.chat.chat_quick_change_feed import read_quick_change_log
from imbue.chat.chat_quick_change_feed import reply_event
from imbue.chat.chat_quick_change_feed import request_event
from imbue.chat.errors import ChatAppError
from imbue.chat.harnesses.claude.auth import ANTHROPIC_API_KEY_ENV_VAR
from imbue.chat.harnesses.claude.auth import ANTHROPIC_BASE_URL_ENV_VAR
from imbue.chat.harnesses.claude.auth import read_managed_auth_env
from imbue.chat.harnesses.events import DisplayKind
from imbue.chat.harnesses.message_display import classify_user_message
from imbue.chat.models import AgentCreationError
from imbue.chat.models import ChatAccountBinding
from imbue.chat.primitives import ChatId
from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure

logger = _loguru_logger

ROUTING_LOG_FILENAME: Final[str] = "quick_change_routing.jsonl"
# Shared with system/scripts/report_quick_change.py, which appends the quick-change agent's reports.
QUICK_CHANGES_LOG_FILENAME: Final[str] = "quick_changes.jsonl"
QUICK_CHANGE_AGENT_FILENAME: Final[str] = "quick_change_agent.json"
QUICK_CHANGE_SKILL: Final[str] = "quick-look-and-feel-change"
QUICK_CHANGE_CHAT_NAME: Final[str] = "Quick look-and-feel changes"
# The label the quick-change chat carries, so it can be found in `mngr list`.
QUICK_CHANGE_LABEL: Final[str] = "quick_change_agent"
# The model the quick-change chat runs on: the claude harness's Sonnet option id.
QUICK_CHANGE_MODEL_ID: Final[str] = "sonnet[1m]"

JEV_ROUTE_PATH: Final[str] = "/jev/v1/systemone"
# A file in the chat app's data folder holding an OpenRouter key, which sends Jev calls to
# OpenRouter itself rather than through the Imbue proxy.
OPENROUTER_KEY_FILENAME: Final[str] = "openrouter_api_key"
OPENROUTER_JEV_URL: Final[str] = "https://openrouter.ai/api/v1/systemone"
JEV_MODEL: Final[str] = "typesafe/jev-1.13"
# The longest a send waits for Jev before it is delivered to the chat's agent unrouted.
ROUTING_TIMEOUT_SECONDS: Final[float] = 3.0
# Messages of recent conversation (user and agent, newest last) Jev reads beside the new message.
CONTEXT_MESSAGE_COUNT: Final[int] = 6
# Each context message is cut to its last this-many words, so one long reply cannot crowd out the rest.
_CONTEXT_MESSAGE_WORD_LIMIT: Final[int] = 300
# The newest transcript events read to find those messages; events are payload-free, so this many
# comfortably holds CONTEXT_MESSAGE_COUNT messages between tool calls.
_CONTEXT_TAIL_EVENT_LIMIT: Final[int] = 200
# How likely Jev must judge a yes before the message is routed.
_YES_CUTOFF: Final[float] = 0.5
# A just-created quick-change chat is not taking messages until its agent is up.
_DELIVERY_ATTEMPTS: Final[int] = 36
_DELIVERY_RETRY_SECONDS: Final[float] = 5.0

QUICK_CHANGE_QUESTION: Final[str] = "quick_look_and_feel_change"
PENDING_WORK_QUESTION: Final[str] = "depends_on_unfinished_work"
JEV_QUESTIONS: Final[dict[str, dict[str, Any]]] = {
    QUICK_CHANGE_QUESTION: {
        "type": "noul",
        "instructions": (
            "A user sent new_message to an AI agent that builds and runs apps on their computer. Is new_message "
            "a request for a quick look-and-feel change to an existing app: a colour, font or font size, "
            "spacing, showing or hiding an element, light or dark styling, or restyling a page, that can be "
            "made without new features, new data or backend work?"
        ),
        "criteria": {
            "true": (
                "A quick visual or styling change to an existing app, and nothing more: no new feature, "
                "no new data, no backend or behaviour change."
            ),
            "false": (
                "Anything else: building a new app, adding a feature or data, fixing behaviour, a question, "
                "conversation, or a styling request bundled with other work."
            ),
        },
    },
    PENDING_WORK_QUESTION: {
        "type": "noul",
        "instructions": (
            "Read recent_conversation, which holds the user's earlier messages and the agent's replies. Does "
            "new_message change or depend on something the user asked for earlier in this conversation that "
            "the agent has not yet said is done?"
        ),
        "criteria": {
            "true": (
                "new_message adjusts, extends or relies on earlier requested work that the agent has not "
                "reported as finished, or refers to something that does not exist yet."
            ),
            "false": ("new_message stands on its own, or only touches things the agent has already said are done."),
        },
    },
}

# Where tk keeps tickets: the agents' TICKETS_DIR (pinned in .mngr/settings.toml), else its
# workspace default relative to the repo root the chat app runs from.
_DEFAULT_TICKETS_DIR: Final[Path] = Path("data/.tickets")
# The title of the editing lease the update-app skill has an agent take before editing an app.
_EDITING_LEASE_TITLE_RE: Final[re.Pattern[str]] = re.compile(r"^# editing service (\S+)\s*$", re.MULTILINE)
_FRONTMATTER_FIELD_RE: Final[re.Pattern[str]] = re.compile(r"^(\w[\w-]*):\s*(.*?)\s*$", re.MULTILINE)


class QuickChangeDeliveryError(ChatAppError):
    """A message could not be delivered to a chat (refused by the send path)."""


class JevCredentials(FrozenModel):
    """Where Jev is asked and the key that endpoint accepts."""

    endpoint_url: str = Field(description="The URL Jev's requests are posted to")
    api_key: str = Field(description="The proxy's LiteLLM virtual key, or an OpenRouter key", repr=False)
    is_openrouter_key: bool = Field(
        default=False,
        description="Whether this is the saved OpenRouter key rather than a proxy sign-in",
    )


class JevOutcomeStatus(LowerCaseStrEnum):
    ANSWERED = auto()
    # Jev is not there for this chat (no route, or a refused key).
    UNAVAILABLE = auto()
    # A timeout, a network error, or an answer that cannot be read.
    FAILED = auto()


class JevOutcome(FrozenModel):
    status: JevOutcomeStatus = Field(description="How the call ended")
    response: dict[str, Any] | None = Field(default=None, description="Jev's whole answer when it answered")
    detail: str = Field(default="", description="What went wrong, for the log")


class RoutingDecision(LowerCaseStrEnum):
    """What happened to one message."""

    ROUTED = auto()
    NOT_ROUTED = auto()
    # A quick look-and-feel change, but to work the agent has not finished: the chat's agent keeps it.
    PENDING_WORK = auto()
    NO_CREDENTIALS = auto()
    JEV_UNAVAILABLE = auto()
    JEV_FAILED = auto()
    # Jev answered, but without a usable answer to one of the questions.
    UNUSABLE_ANSWER = auto()


class RoutingResult(FrozenModel):
    """What one message's routing came to."""

    is_routed: bool = Field(
        default=False,
        description="Whether the message goes to the quick-change agent instead",
    )
    request_id: str = Field(
        default="",
        description="The id the quick-change log and report use (routed only)",
    )
    routed_at: str = Field(default="", description="When the message was sent, ISO UTC (routed only)")
    user_message: str = Field(default="", description="The user's words, forwarded as they are (routed only)")
    recent_conversation: str = Field(default="", description="The context Jev read, forwarded too (routed only)")
    quick_change_probability: float | None = Field(default=None, description="Jev's answer to question 1")
    pending_work_probability: float | None = Field(default=None, description="Jev's answer to question 2")


class QuickChangeReportStatus(LowerCaseStrEnum):
    DONE = auto()
    FAILED = auto()


class QuickChangeReportRequest(FrozenModel):
    """``POST /api/quick-changes/<request_id>/report``: the quick-change agent's outcome for one request."""

    status: QuickChangeReportStatus = Field(description="Whether the change was made")
    report: str = Field(
        min_length=1,
        description="One or two plain sentences: what changed, in which app",
    )


# Answers that mean Jev is not there for this chat.
_UNAVAILABLE_STATUS_CODES: Final[frozenset[int]] = frozenset({401, 403, 404})


@pure
def resolve_jev_credentials(managed_env: Mapping[str, str]) -> JevCredentials | None:
    """The proxy credentials in one account's managed env, or None unless it is signed in to an Imbue proxy."""
    api_key = managed_env.get(ANTHROPIC_API_KEY_ENV_VAR, "")
    base_url = managed_env.get(ANTHROPIC_BASE_URL_ENV_VAR, "").rstrip("/")
    if api_key == "" or base_url == "":
        return None
    return JevCredentials(endpoint_url=f"{base_url}{JEV_ROUTE_PATH}", api_key=api_key)


def read_openrouter_jev_credentials(key_path: Path) -> JevCredentials | None:
    """OpenRouter credentials from the saved key file, or None when there is no key in it."""
    try:
        api_key = key_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError as e:
        logger.warning("Ignoring an unreadable OpenRouter key file at {}: {}", key_path, e)
        return None
    if api_key == "":
        return None
    return JevCredentials(endpoint_url=OPENROUTER_JEV_URL, api_key=api_key, is_openrouter_key=True)


def read_chat_jev_credentials(binding: ChatAccountBinding | None, openrouter_key_path: Path) -> JevCredentials | None:
    """The saved OpenRouter key when there is one, else the chat's proxy sign-in (its account's, else the workspace's)."""
    openrouter_credentials = read_openrouter_jev_credentials(openrouter_key_path)
    if openrouter_credentials is not None:
        return openrouter_credentials
    if binding is not None:
        account_settings = binding.account_dir / "settings.json"
        if account_settings.is_file():
            credentials = resolve_jev_credentials(read_managed_auth_env(settings_path_override=account_settings))
            if credentials is not None:
                return credentials
    return resolve_jev_credentials(read_managed_auth_env())


def ask_jev(
    credentials: JevCredentials,
    jev_request: dict[str, Any],
    http_client: httpx.Client,
    timeout_seconds: float,
) -> JevOutcome:
    """One call to Jev, through the proxy or straight to OpenRouter."""
    try:
        response = http_client.post(
            credentials.endpoint_url,
            json=jev_request,
            headers={"Authorization": f"Bearer {credentials.api_key}"},
            timeout=timeout_seconds,
        )
    except httpx.HTTPError as e:
        return JevOutcome(
            status=JevOutcomeStatus.FAILED,
            detail=f"request failed: {type(e).__name__}: {e}",
        )
    if response.status_code in _UNAVAILABLE_STATUS_CODES:
        return JevOutcome(status=JevOutcomeStatus.UNAVAILABLE, detail=f"HTTP {response.status_code}")
    if response.status_code != 200:
        return JevOutcome(
            status=JevOutcomeStatus.FAILED,
            detail=f"HTTP {response.status_code}: {response.text[:200]}",
        )
    try:
        payload = response.json()
    except ValueError as e:
        return JevOutcome(status=JevOutcomeStatus.FAILED, detail=f"unreadable answer: {e}")
    if not isinstance(payload, dict):
        return JevOutcome(status=JevOutcomeStatus.FAILED, detail="answer is not an object")
    return JevOutcome(status=JevOutcomeStatus.ANSWERED, response=payload)


@pure
def _normalize_text(text: str) -> str:
    return " ".join(text.split())


@pure
def _event_text(event: Mapping[str, Any]) -> str:
    """The words of a user or assistant event as the conversation shows them."""
    if event.get("type") == "assistant_message":
        return str(event.get("text") or "")
    body = event.get("display_body")
    if isinstance(body, str) and body:
        return body
    return str(event.get("content") or "")


@pure
def _is_user_prompt_event(event: Mapping[str, Any]) -> bool:
    """Whether an event is a message the user wrote (a framework line, chip, notice or status is not)."""
    if event.get("type") != "user_message" or event.get("role") != "user":
        return False
    display = event.get("display")
    return display is None or display == DisplayKind.PROMPT_WITH_CONTEXT


@pure
def build_recent_conversation(events: Sequence[Mapping[str, Any]], new_message: str, message_count: int) -> str:
    """The last ``message_count`` user messages and agent replies before a new message, oldest first.

    Tool calls and framework lines are left out. The new message itself is skipped if it has already
    reached the transcript (the newest user message carrying its text). Each message keeps only its
    last ``_CONTEXT_MESSAGE_WORD_LIMIT`` words.
    """
    new_message_text = _normalize_text(new_message)
    is_new_message_skipped = new_message_text == ""
    lines: list[str] = []
    for event in reversed(events):
        if len(lines) >= message_count:
            break
        if _is_user_prompt_event(event):
            if not is_new_message_skipped and new_message_text in _normalize_text(_event_text(event)):
                is_new_message_skipped = True
                continue
            speaker = "User"
        elif event.get("type") == "assistant_message":
            speaker = "Agent"
        else:
            continue
        words = _event_text(event).split()
        if not words:
            continue
        lines.append(f"{speaker}: {' '.join(words[-_CONTEXT_MESSAGE_WORD_LIMIT:])}")
    return "\n\n".join(reversed(lines))


@pure
def build_jev_request(new_message: str, recent_conversation: str) -> dict[str, Any]:
    return {
        "model": JEV_MODEL,
        "state": {
            "new_message": new_message,
            "recent_conversation": recent_conversation,
        },
        "questions": dict(JEV_QUESTIONS),
    }


@pure
def read_probability(response: Mapping[str, Any], question: str) -> float | None:
    """How likely Jev judged ``question`` true, or None for an answer without it."""
    answers = response.get("answers")
    if not isinstance(answers, Mapping):
        return None
    answer = answers.get(question)
    if not isinstance(answer, Mapping):
        return None
    probability = answer.get("noul")
    if isinstance(probability, bool) or not isinstance(probability, (int, float)):
        return None
    return float(probability)


@pure
def decide_routing(
    outcome: JevOutcome,
    quick_change_probability: float | None,
    pending_work_probability: float | None,
) -> RoutingDecision:
    """The routing decision one Jev call comes to: routed only for a quick change that touches no unfinished work."""
    if outcome.status is JevOutcomeStatus.UNAVAILABLE:
        return RoutingDecision.JEV_UNAVAILABLE
    if outcome.status is JevOutcomeStatus.FAILED:
        return RoutingDecision.JEV_FAILED
    if quick_change_probability is None:
        return RoutingDecision.UNUSABLE_ANSWER
    if quick_change_probability <= _YES_CUTOFF:
        return RoutingDecision.NOT_ROUTED
    if pending_work_probability is None:
        return RoutingDecision.UNUSABLE_ANSWER
    if pending_work_probability > _YES_CUTOFF:
        return RoutingDecision.PENDING_WORK
    return RoutingDecision.ROUTED


@pure
def is_routable_message(message: str) -> bool:
    """Whether a sent text is the user's own words (a report, notice, slash command or chip is not)."""
    if _normalize_text(message) == "" or message.lstrip().startswith("/"):
        return False
    return classify_user_message(message) is None


@pure
def quick_change_agent_message(
    request_id: str,
    main_chat_id: str,
    user_message: str,
    recent_conversation: str,
    lease_app: str | None,
) -> str:
    """What the quick-change agent receives for one routed message: the skill, the request id it reports under,
    the app the editing lease names (if any), and the request."""
    return (
        f"/{QUICK_CHANGE_SKILL}\n\n"
        f"Request id: {request_id}\n"
        f"Main chat: {main_chat_id}\n"
        f"Editing lease app: {lease_app or '(none)'}\n\n"
        "Recent conversation in the main chat, for context (oldest first):\n"
        f"{recent_conversation or '(none)'}\n\n"
        "The user's request, word for word:\n"
        f"{user_message}"
    )


def tickets_dir_from_env() -> Path:
    """The tk tickets folder the workspace's agents use."""
    configured = os.environ.get("TICKETS_DIR", "")
    return Path(configured) if configured else _DEFAULT_TICKETS_DIR


class EditingLease(FrozenModel):
    app_name: str = Field(description="The app the lease is for")
    holder: str = Field(description="The ticket's agent field: the holder's chat id or agent name ('' when absent)")
    created: str = Field(description="The ticket's creation timestamp (fixed-width ISO, so it sorts by time)")


@pure
def parse_editing_lease(ticket_text: str) -> EditingLease | None:
    """The in-progress editing lease a ticket file holds, or None for any other ticket."""
    parts = ticket_text.split("---", 2)
    if len(parts) < 3 or parts[0].strip() != "":
        return None
    fields = dict(_FRONTMATTER_FIELD_RE.findall(parts[1]))
    if fields.get("status") != "in_progress" or fields.get("step") == "true":
        return None
    title = _EDITING_LEASE_TITLE_RE.search(parts[2])
    if title is None:
        return None
    return EditingLease(
        app_name=title.group(1),
        holder=fields.get("agent", ""),
        created=fields.get("created", ""),
    )


def find_editing_lease_app(tickets_dir: Path, main_chat_id: str) -> str | None:
    """The app named by the current editing lease: the main chat's own when it holds one, else the newest; None
    when no lease is held."""
    leases: list[EditingLease] = []
    try:
        paths = sorted(tickets_dir.glob("*.md"))
    except OSError as e:
        logger.warning("Could not list tickets in {}: {}", tickets_dir, e)
        return None
    for path in paths:
        try:
            lease = parse_editing_lease(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        if lease is not None:
            leases.append(lease)
    if not leases:
        return None
    own = [lease for lease in leases if lease.holder == main_chat_id]
    return max(own or leases, key=lambda lease: lease.created).app_name


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class QuickChangeRouter(MutableModel):
    """Asks Jev about each sent message and forwards quick look-and-feel changes to the quick-change agent."""

    model_config = {"arbitrary_types_allowed": True}

    data_dir: Path = Field(
        frozen=True,
        description="The chat app's data folder (routing log, agent record)",
    )
    resolve_credentials: Callable[[ChatId | None], JevCredentials | None] = Field(
        frozen=True,
        description="The Jev credentials a chat's calls use, or None when it has none",
    )
    get_conversation_tail: Callable[[ChatId, int], list[dict[str, Any]]] = Field(
        frozen=True,
        description="The newest events (up to the limit) of the agent the chat runs on, oldest first",
    )
    call_jev: Callable[[JevCredentials, dict[str, Any], float], JevOutcome] = Field(
        frozen=True, description="One call to Jev, with its timeout in seconds"
    )
    create_agent_chat: Callable[[str, str, dict[str, str]], str] = Field(
        frozen=True,
        description="Creates the quick-change chat from a name, first message and labels; returns its id",
    )
    deliver: Callable[[ChatId, str], bool] = Field(
        frozen=True,
        description="Sends a message into a chat through the send path; False when the chat is gone, "
        "QuickChangeDeliveryError when it refuses",
    )
    timeout_seconds: float = Field(default=ROUTING_TIMEOUT_SECONDS, frozen=True, description="Jev's time limit")
    context_message_count: int = Field(default=CONTEXT_MESSAGE_COUNT, frozen=True, ge=0)
    find_lease_app: Callable[[str], str | None] = Field(
        default=lambda main_chat_id: find_editing_lease_app(tickets_dir_from_env(), main_chat_id),
        frozen=True,
        description="The app the current editing lease names (given the main chat's id), or None",
    )
    run_in_background: Callable[[Callable[[], None]], None] = Field(
        default=lambda fn: threading.Thread(target=fn, name="quick-change-forward", daemon=True).start(),
        frozen=True,
        description="Where the forward runs, off the send's thread",
    )
    wait: Callable[[float], object] = Field(
        default_factory=lambda: threading.Event().wait,
        frozen=True,
        description="Waits between delivery attempts",
    )
    get_active_agent_id: Callable[[ChatId], str] = Field(
        default=lambda _chat_id: "",
        frozen=True,
        description="The id of the agent a chat runs on ('' when none)",
    )
    on_feed_event: Callable[[ChatId, dict[str, Any]], None] = Field(
        default=lambda _chat_id, _event: None,
        frozen=True,
        description="Shows a quick-change event in the chat's open pages (the stream)",
    )

    _agent_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _log_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def quick_change_chat_id(self) -> str | None:
        """The quick-change agent's chat id, or None before its first use."""
        path = self.data_dir / QUICK_CHANGE_AGENT_FILENAME
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            logger.warning("Ignoring an unreadable quick-change agent record at {}: {}", path, e)
            return None
        chat_id = payload.get("chat_id") if isinstance(payload, dict) else None
        return chat_id if isinstance(chat_id, str) and chat_id else None

    def route(self, chat_id: ChatId | None, message: str) -> RoutingResult:
        """Ask Jev about ``message`` and say whether it goes to the quick-change agent instead of the chat's own.

        ``chat_id`` is None for a chat's first message sent with its create, before the chat has an id.
        Never raises: anything that goes wrong leaves the message unrouted.
        """
        unrouted = RoutingResult()
        if not is_routable_message(message) or (chat_id is not None and str(chat_id) == self.quick_change_chat_id()):
            return unrouted
        try:
            return self._route(chat_id, message)
        except (OSError, ValueError, httpx.HTTPError) as e:
            logger.opt(exception=e).error("Quick-change routing failed for chat {}; delivering unrouted", chat_id)
            return unrouted

    def forward(self, main_chat_id: ChatId, result: RoutingResult) -> None:
        """Record a routed message and show it in its chat at once, then hand it to the quick-change agent off the
        caller's thread; nothing for an unrouted one."""
        if not result.is_routed:
            return
        lease_app = self.find_lease_app(str(main_chat_id))
        self._append(
            QUICK_CHANGES_LOG_FILENAME,
            {
                "type": "request",
                "request_id": result.request_id,
                "chat_id": str(main_chat_id),
                "requested_at": result.routed_at,
                "message": result.user_message,
                "app": lease_app,
                "jev": {
                    QUICK_CHANGE_QUESTION: result.quick_change_probability,
                    PENDING_WORK_QUESTION: result.pending_work_probability,
                },
            },
        )
        agent_id = self.get_active_agent_id(main_chat_id)
        self.on_feed_event(
            main_chat_id,
            request_event(result.request_id, result.user_message, result.routed_at, agent_id),
        )
        self.run_in_background(lambda: self._forward(main_chat_id, result, lease_app))

    def record_report(self, request_id: str, status: str, report: str) -> bool:
        """Log the quick-change agent's report and show it as the reply in the request's chat; False for an
        unknown request id."""
        records = read_quick_change_log(self.data_dir / QUICK_CHANGES_LOG_FILENAME)
        chat_id = next(
            (
                str(record["chat_id"])
                for record in records
                if record.get("type") == "request" and record.get("request_id") == request_id and record.get("chat_id")
            ),
            None,
        )
        if chat_id is None:
            return False
        reported_at = _now_iso()
        self._append(
            QUICK_CHANGES_LOG_FILENAME,
            {
                "type": "report",
                "request_id": request_id,
                "status": status,
                "report": report,
                "reported_at": reported_at,
            },
        )
        self._log(
            {
                "event": "reported",
                "request_id": request_id,
                "status": status,
                "finished_at": reported_at,
            }
        )
        parsed = ChatId(chat_id)
        self.on_feed_event(
            parsed,
            reply_event(request_id, report, reported_at, self.get_active_agent_id(parsed)),
        )
        return True

    def overlay_events(self, chat_id: ChatId, agent_id: str) -> list[dict[str, Any]]:
        """The quick-change events a chat's feed carries (empty for a chat with none)."""
        log_path = self.data_dir / QUICK_CHANGES_LOG_FILENAME
        if not log_path.is_file():
            return []
        return overlay_events_for_chat(read_quick_change_log(log_path), str(chat_id), agent_id)

    def _route(self, chat_id: ChatId | None, message: str) -> RoutingResult:
        started = time.monotonic()
        record: dict[str, Any] = {
            "event": "jev_decision",
            "request_id": uuid.uuid4().hex,
            "chat_id": None if chat_id is None else str(chat_id),
            "message": message,
            "started_at": _now_iso(),
        }
        credentials = self.resolve_credentials(chat_id)
        if credentials is None:
            self._log(
                {
                    **record,
                    "decision": str(RoutingDecision.NO_CREDENTIALS),
                    "finished_at": _now_iso(),
                }
            )
            return RoutingResult()
        tail = [] if chat_id is None else self.get_conversation_tail(chat_id, _CONTEXT_TAIL_EVENT_LIMIT)
        recent_conversation = build_recent_conversation(tail, message, self.context_message_count)
        jev_request = build_jev_request(message, recent_conversation)
        record.update(
            jev_endpoint=credentials.endpoint_url,
            is_openrouter_key=credentials.is_openrouter_key,
            jev_request=jev_request,
            jev_sent_at=_now_iso(),
        )
        outcome = self.call_jev(credentials, jev_request, self.timeout_seconds)
        jev_answered_at = _now_iso()
        response = outcome.response or {}
        quick_change_probability = read_probability(response, QUICK_CHANGE_QUESTION)
        pending_work_probability = read_probability(response, PENDING_WORK_QUESTION)
        decision = decide_routing(outcome, quick_change_probability, pending_work_probability)
        elapsed_ms = round((time.monotonic() - started) * 1000)
        self._log(
            {
                **record,
                "jev_answered_at": jev_answered_at,
                "jev_outcome": outcome.model_dump(mode="json"),
                "quick_change_probability": quick_change_probability,
                "pending_work_probability": pending_work_probability,
                "decision": str(decision),
                "elapsed_ms": elapsed_ms,
                "finished_at": _now_iso(),
            }
        )
        logger.info(
            "Quick-change routing for chat {}: {} ({} ms, quick change p={}, unfinished work p={})",
            chat_id,
            decision,
            elapsed_ms,
            quick_change_probability,
            pending_work_probability,
        )
        if decision is not RoutingDecision.ROUTED:
            return RoutingResult()
        return RoutingResult(
            is_routed=True,
            request_id=record["request_id"],
            routed_at=record["started_at"],
            user_message=message,
            recent_conversation=recent_conversation,
            quick_change_probability=quick_change_probability,
            pending_work_probability=pending_work_probability,
        )

    def _forward(self, main_chat_id: ChatId, result: RoutingResult, lease_app: str | None) -> None:
        """Hand the request to the quick-change agent, creating it on first use; logged, never raised."""
        started = time.monotonic()
        text = quick_change_agent_message(
            result.request_id,
            str(main_chat_id),
            result.user_message,
            result.recent_conversation,
            lease_app,
        )
        try:
            quick_chat_id, is_created = self._deliver_to_agent(text)
        except (AgentCreationError, ChatAppError, OSError) as e:
            logger.opt(exception=e).error(
                "Could not forward chat {}'s message to the quick-change agent",
                main_chat_id,
            )
            error = f"{type(e).__name__}: {e}"
            failed_at = _now_iso()
            self._append(
                QUICK_CHANGES_LOG_FILENAME,
                {
                    "type": "forward",
                    "request_id": result.request_id,
                    "status": "forward_failed",
                    "error": error,
                    "at": failed_at,
                },
            )
            self._log(
                {
                    "event": "forward_failed",
                    "request_id": result.request_id,
                    "chat_id": str(main_chat_id),
                    "error": error,
                    "finished_at": failed_at,
                }
            )
            agent_id = self.get_active_agent_id(main_chat_id)
            self.on_feed_event(
                main_chat_id,
                reply_event(result.request_id, FORWARD_FAILED_TEXT, failed_at, agent_id),
            )
            return
        forwarded_at = _now_iso()
        self._append(
            QUICK_CHANGES_LOG_FILENAME,
            {
                "type": "forward",
                "request_id": result.request_id,
                "status": "forwarded",
                "quick_change_chat_id": quick_chat_id,
                "at": forwarded_at,
            },
        )
        self._log(
            {
                "event": "forwarded",
                "request_id": result.request_id,
                "chat_id": str(main_chat_id),
                "quick_change_chat_id": quick_chat_id,
                "is_agent_created": is_created,
                "lease_app": lease_app,
                "elapsed_ms": round((time.monotonic() - started) * 1000),
                "finished_at": forwarded_at,
            }
        )

    def _deliver_to_agent(self, text: str) -> tuple[str, bool]:
        """Deliver to the existing quick-change chat, or create it with ``text`` as its first message."""
        with self._agent_lock:
            existing = self.quick_change_chat_id()
            if existing is not None and self._deliver_with_retries(ChatId(existing), text):
                return existing, False
            created = self.create_agent_chat(QUICK_CHANGE_CHAT_NAME, text, {QUICK_CHANGE_LABEL: "true"})
            self._write_agent_record(created)
            logger.info("Created the quick-change chat {}", created)
            return created, True

    def _deliver_with_retries(self, chat_id: ChatId, text: str) -> bool:
        """True once delivered (or held for a chat coming up), False when the chat is gone."""
        for attempt in range(1, _DELIVERY_ATTEMPTS + 1):
            try:
                return self.deliver(chat_id, text)
            except QuickChangeDeliveryError as e:
                if attempt == _DELIVERY_ATTEMPTS:
                    raise
                logger.debug(
                    "Quick-change chat {} is not taking messages yet ({}); retrying",
                    chat_id,
                    e,
                )
                self.wait(_DELIVERY_RETRY_SECONDS)
        return False

    def _write_agent_record(self, chat_id: str) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        path = self.data_dir / QUICK_CHANGE_AGENT_FILENAME
        temp_path = path.with_suffix(".json.tmp")
        temp_path.write_text(
            json.dumps({"chat_id": chat_id, "created_at": _now_iso()}, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temp_path.replace(path)

    def _log(self, record: dict[str, Any]) -> None:
        self._append(ROUTING_LOG_FILENAME, record)

    def _append(self, filename: str, record: dict[str, Any]) -> None:
        """Append one JSON line; a single write of one line, so a report appended by another process never splits it."""
        line = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
        with self._log_lock:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.data_dir / filename, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
            try:
                os.write(fd, line)
            finally:
                os.close(fd)
