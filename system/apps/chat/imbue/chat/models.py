from datetime import datetime
from enum import auto

from app_instances.data_types import InstanceStatus
from pydantic import Field
from pydantic import SecretStr

from imbue.chat.activity_state import ActivityState
from imbue.chat.agent_discovery import AgentInfo
from imbue.chat.chat_fast_mode import ChatFastModeState
from imbue.chat.chat_seed import SeedTurn
from imbue.chat.chat_settings import ChatSettings
from imbue.chat.harnesses.harness_type import DEFAULT_HARNESS
from imbue.chat.harnesses.harness_type import HarnessType
from imbue.chat.harnesses.model import ModelAxis
from imbue.chat.harnesses.model import ModelChoice
from imbue.chat.harnesses.model import ModelOption
from imbue.chat.primitives import ChatId
from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel


class AgentCreationError(ValueError):
    """Raised when agent creation fails due to invalid input."""

    ...


class AgentRenameError(ValueError):
    """Raised when an agent cannot be renamed in mngr.

    A chat's name lives on the mngr agent (the true name plus its
    ``display_name`` label), so a rename that cannot reach mngr must stop the
    whole rename rather than leave the workspace showing a name mngr does not
    hold. This is what the rename path turns into an error response.
    """

    ...


class AgentNameConflictError(AgentRenameError, AgentCreationError):
    """Raised when a chosen chat name collides with another agent's.

    Names collide by canonical form -- the same per-host uniqueness rule mngr
    enforces on true names -- and the endpoints answer 409 so the caller can
    retry with a different name.
    """

    ...


class AttachmentError(ValueError):
    """Raised when a chat attachment cannot be stored or located."""

    ...


class AgentListItem(FrozenModel):
    """An agent entry in the agent list response."""

    id: str = Field(description="The agent's unique identifier")
    name: str = Field(description="The agent's human-readable name")
    state: str = Field(description="The agent's lifecycle state")


class AgentListResponse(FrozenModel):
    """Response from the /api/agents endpoint."""

    agents: list[AgentListItem] = Field(description="List of discovered agents")


class SendMessageRequest(FrozenModel):
    """Request body for sending a message to a chat."""

    message: str = Field(description="The message text to send")
    message_id: str = Field(
        default="",
        description=(
            "Stable per-message id the sender mints at send time (contract A4), keying the backend "
            "'Sending' record so an interrupt can reconcile this message per id and return it to the "
            "composer if it never committed. '' for legacy callers, which the backend then mints for."
        ),
    )
    client_id: str = Field(default="", description="Per-browser client id of the sender ('' for legacy callers)")
    active_layout: str = Field(
        default="", description="The id of the view the sender was on at send time ('' for legacy callers)"
    )
    device_kind: str = Field(default="", description="'mobile' or 'desktop', derived from the sender's user agent")


class SendMessageResponse(FrozenModel):
    """Response from the message endpoint."""

    status: str = Field(description="Status of the send operation")


class SetModelChoiceRequest(FrozenModel):
    """Request body for POST /api/chats/{id}/model.

    One shape covering all three axes. ``effort`` is omitted for a model with no
    effort axis, and defaults to None; ``fast`` is the intended fast state.
    """

    model_id: str = Field(description="Model id to switch to; must be one of the harness catalog option ids")
    effort: str | None = Field(default=None, description="Reasoning effort to set; None for a no-effort model")
    fast: bool = Field(default=False, description="Whether fast mode should be on")
    axes: tuple[ModelAxis, ...] = Field(
        default=(),
        description="Which axes this click changed (against the value the user saw); the switch applies only these",
    )


class ModelOptionsResponse(FrozenModel):
    """Response from GET /api/chats/{id}/model-options.

    Two shapes, one per picker kind. A static/catalog-backed harness (claude, pi) returns ``models``
    -- the ids to offer, matched back to the static catalog for labels/efforts (or null = offer the
    whole catalog). A DYNAMIC harness (codex) has no static catalog, so it returns ``options`` --
    the FULL per-agent :class:`ModelOption`s (id, label, per-model efforts, fast support), fetched
    fresh from ``model/list`` on this open. Exactly one of the two is populated for a given harness.
    """

    models: tuple[str, ...] | None = Field(
        default=None,
        description="Model ids to offer in the picker right now, or null to offer the whole catalog",
    )
    options: tuple[ModelOption, ...] | None = Field(
        default=None,
        description="The full per-agent options for a DYNAMIC picker (codex), or null for a static harness",
    )


class PoweredByResponse(FrozenModel):
    """Response from GET /api/chats/{id}/powered-by."""

    label: str = Field(description="The agent harness's verbatim credit text, or '' when that harness shows no credit")


class ChatSettingsResponse(FrozenModel):
    """Response from GET and PUT /api/settings: the workspace-wide chat settings as they stand."""

    settings: ChatSettings = Field(description="The settings")


class FastModeStateResponse(FrozenModel):
    """Response from GET and PUT /api/chats/<chat_id>/fast-mode: the chat's fast mode."""

    state: ChatFastModeState = Field(description="The chat's fast mode")


class AttachmentUploadResponse(FrozenModel):
    """Response from the chat attachment upload endpoint."""

    path: str = Field(description="Absolute path to the stored upload on the agent VM")
    size: int = Field(description="Size of the stored upload in bytes")


class InterruptAgentResponse(FrozenModel):
    """Response from the /api/chats/{id}/interrupt endpoint."""

    status: str = Field(description="Status of the interrupt operation")


class DrainToComposerResponse(FrozenModel):
    """Response from POST /api/chats/{id}/drain-to-composer.

    Carries the concatenated queued block the frontend drops into the composer
    (unsent) for the user to edit and send. Empty when the queue was already
    drained by the time the action fired.
    """

    block: str = Field(description="The queued messages as one concatenated block, or '' if the queue was empty")


class ShoulderTapAtomicResponse(FrozenModel):
    """Response from POST /api/chats/{id}/shoulder-tap-atomic.

    ``status`` is ``"tapped"`` when a control line targeting the live open turn was written
    (the patched codex will merge the parked messages into that turn), ``"no_open_turn"``
    when no turn was running, so nothing was interrupted and no control line was written, or
    ``"send_in_flight"`` when a message send held the lock past the bounded wait so nothing was
    written -- a benign no-op (200), never an error, since the availability flag greys the button
    while a send is in flight.

    ``block`` is normally empty. It is non-empty ONLY when a native tap's combined resend failed to
    submit: the parked text is handed back to the composer through this response (in send order),
    the same drain-to-composer hand-off Stop uses, so it is never swallowed (contract A1a).
    """

    status: str = Field(description="'tapped', 'no_open_turn', or 'send_in_flight' (all benign 200 outcomes)")
    block: str = Field(
        default="",
        description="Returned text handed back to the composer when a native tap's resend failed; '' otherwise",
    )


class AgentRestartError(RuntimeError):
    """Raised when the ``mngr start --restart`` a queue action depends on fails."""

    ...


class AgentDestroyError(RuntimeError):
    """Raised when ``mngr destroy`` refuses or fails for a chat agent."""

    ...


class ModelApplyError(RuntimeError):
    """A model pick could not be applied to a running agent: the pick was not one of the agent's options, or
    the harness refused the switch. The message is what the user sees."""


class AgentStopError(RuntimeError):
    """Raised when ``mngr stop`` refuses or fails for a chat agent."""

    ...


class ChatConvergingError(RuntimeError):
    """Raised when a verb is refused because the chat is in the middle of a switch, a handoff or a rebind (a 409)."""

    ...


class HandoffError(ValueError):
    """Raised when a switch (a handoff or a rebind) cannot begin, be cancelled, or be retried as asked (a 400)."""

    ...


class ErrorResponse(FrozenModel):
    """Error response body."""

    detail: str = Field(description="Human-readable error description")


class QueuedMessageState(FrozenModel):
    """One currently-queued message on the per-agent WebSocket state.

    The harness-agnostic wire shape of a queued message: the frontend renders the
    queued group from a full snapshot of these, minted by the harness's queue
    populator (see ``harnesses.queued_set``).
    """

    queued_id: str = Field(description="Stable id the populator minted; keys the rendered bubble")
    content: str = Field(description="Verbatim text the user queued")
    timestamp: str = Field(description="Enqueue timestamp (ISO string from the harness ledger)")
    is_sending: bool = Field(
        default=False,
        description=(
            "True while this chip is a message the backend is actively re-sending (a codex "
            "shoulder-tap's interrupt+resend, Fix 3): it stays continuously visible but is rendered "
            "'Sending...' rather than as a plain queued chip, so it never blinks out (contract A1a). "
            "False for an ordinary parked queue chip."
        ),
    )


class AgentStateItem(FrozenModel):
    """One tracked mngr agent: the agent-level record a chat's snapshot is built from."""

    id: str = Field(description="The agent's unique identifier")
    name: str = Field(description="The agent's human-readable name")
    state: str = Field(description="The agent's lifecycle state")
    labels: dict[str, str] = Field(description="Agent labels (e.g., user_created, display_name, account)")
    work_dir: str | None = Field(description="The agent's working directory path")
    harness: HarnessType = Field(
        default=DEFAULT_HARNESS,
        description=(
            "The agent's harness, narrowed from mngr's ``AgentDetails.type`` in "
            "``agent_discovery``. Drives activity derivation and caption routing."
        ),
    )
    activity_state: ActivityState | None = Field(
        default=None,
        description=(
            "Per-agent chat activity state value (THINKING / TOOL_RUNNING / "
            "IDLE), or None when no activity tracking is available for this "
            "agent."
        ),
    )
    model_choice: ModelChoice | None = Field(
        default=None,
        description=(
            "The agent's live model/effort/fast selection plus the catalog option "
            "it matched, or None when no model resolution is available for this "
            "agent. Twin of ``activity_state``; drives the composer's model bar."
        ),
    )
    queued_messages: tuple[QueuedMessageState, ...] = Field(
        default=(),
        description=(
            "Full snapshot of the messages currently parked in the agent's harness "
            "queue, in enqueue order. Empty when nothing is queued (or the harness "
            "has no queue populator). A sibling of ``activity_state``: ephemeral "
            "live state pushed on the agents WebSocket, replaced wholesale each push."
        ),
    )


class HandoffFailedStep(LowerCaseStrEnum):
    """Which step of a switch left it in the failed phase: what the failed page names and what a retry reruns."""

    # The successor's ``mngr create`` (a handoff) or the agent's restart (a rebind).
    START = auto()
    # The successor was created, but the model the user picked for it could not be applied.
    MODEL = auto()


class ModelPick(FrozenModel):
    """A model, effort, and fast-mode selection made for an agent that does not run yet: the successor a
    switch creates, or a new chat. Validated against the agent's option set once it exists, exactly as
    the model bar's own pick is (``validate_model_pick``)."""

    model_id: str = Field(description="Model id to run on; must be one of the harness's option ids")
    effort: str | None = Field(default=None, description="Reasoning effort; None for a model with no effort axis")
    fast: bool = Field(default=False, description="Whether fast mode should be on")


class HandoffPhase(LowerCaseStrEnum):
    """Where a chat that is moving to another agent or account stands (``null`` on the wire while it is not).

    A handoff runs draining, summarizing, switching; a rebind runs draining, restarting. Both
    end in failed when the agent the chat continues on (the successor, or the rebound one)
    cannot be started.
    """

    DRAINING = auto()
    SUMMARIZING = auto()
    SWITCHING = auto()
    RESTARTING = auto()
    FAILED = auto()


class TransitionKind(LowerCaseStrEnum):
    """What a converging chat is doing: moving to another harness, or changing account in place."""

    HANDOFF = auto()
    REBIND = auto()


class SummaryOutcome(LowerCaseStrEnum):
    """How the summarizing phase of a handoff ended."""

    # A fresh summary already existed, so none was requested.
    REUSED = auto()
    # The retiring agent wrote one on request.
    WRITTEN = auto()
    # No summary: the request failed, the turn ended without a file, the wait ran out, or the
    # agent was gone.
    MISSING = auto()
    # None was asked for: the retiring agent never received a user turn, so there was nothing to
    # summarize and the successor starts fresh, with no handoff prompt either.
    SKIPPED = auto()


class HeldSendOrigin(LowerCaseStrEnum):
    """Who sent a message the chat app held while converging."""

    # A chat page (the send named its client).
    CLIENT = auto()
    # An in-workspace sender through ``system/scripts/message_chat.py``, or any other caller.
    SCRIPT = auto()


class HeldSend(FrozenModel):
    """One message the chat app accepted while converging and will deliver once the switch is done."""

    message_id: str = Field(description="The sender's stable send-time id (contract A4)")
    text: str = Field(description="The message, verbatim")
    origin: HeldSendOrigin = Field(description="Who sent it")
    received_at: datetime = Field(description="When the chat app accepted it")


class HeldSendSnapshot(FrozenModel):
    """One message the chat app is holding for the successor, as the chat pages render it while converging."""

    message_id: str = Field(description="The sender's stable send-time id, which the page's own bubble carries too")
    text: str = Field(description="The message, verbatim")


class HandoffState(FrozenModel):
    """The in-progress switch a chat snapshot carries while the chat is converging: a handoff or a rebind."""

    kind: TransitionKind = Field(description="A handoff (another harness) or a rebind (another account, same agent)")
    phase: HandoffPhase = Field(description="Which step of the switch the chat is in")
    started_at: datetime = Field(
        description=(
            "When the switch was confirmed, so the page can tell this switch's summary request in the "
            "transcript from an earlier one that was called off"
        )
    )
    target_lane: str = Field(description="The lane the chat is moving to")
    target_account_id: str = Field(description="The account the chat is moving to")
    target_harness: HarnessType = Field(description="The harness the chat is moving to, for the page's phase text")
    target_label: str = Field(
        description="What the phase text names the destination by: the harness for a handoff, the account for a rebind"
    )
    held_sends: tuple[HeldSendSnapshot, ...] = Field(
        default=(),
        description=(
            "The messages held for after the switch, the confirming message first, so a page (a reloaded one "
            "too) keeps showing them until they land in the transcript"
        ),
    )
    error: str | None = Field(default=None, description="Why the switch failed, in the failed phase")
    failed_step: HandoffFailedStep | None = Field(
        default=None, description="Which step failed, in the failed phase: the agent's start, or the model pick"
    )


class SwitchChatRequest(SendMessageRequest):
    """Request body for POST /api/chats/{id}/handoff: a send (the first message the chat sends after the
    switch, with the sender's client fields; empty for a switch made with nothing to say yet) plus the
    account the chat moves to, and for a handoff the model the successor should run on."""

    account_id: str = Field(description="The signed-in account the chat moves to")
    model: ModelPick | None = Field(
        default=None,
        description=(
            "The model the successor runs on, applied before its first message; None for the harness's default. "
            "Refused for a rebind, which keeps the agent's own settings"
        ),
    )


class SwitchChatResponse(FrozenModel):
    """Response from POST /api/chats/{id}/handoff."""

    status: str = Field(description="'converging' once the switch has begun")
    kind: TransitionKind = Field(description="Whether the target made the switch a handoff or a rebind")
    phase: HandoffPhase = Field(description="The phase the chat is in when the route answers")
    returned_block: str = Field(
        description="The queued text draining took off the agent the chat was on, for the composer ('' for none)"
    )


class HeldSendResponse(FrozenModel):
    """Response from the message route while the chat is converging: the send is held for the successor."""

    status: str = Field(description="'held'")
    phase: HandoffPhase = Field(description="The phase the chat is in")


class HandoffCancelResponse(FrozenModel):
    """Response from POST /api/chats/{id}/handoff/cancel."""

    status: str = Field(description="'cancelled'")
    returned_block: str = Field(description="The message that confirmed the handoff, back for the composer")


class HandoffRetryRequest(FrozenModel):
    """Request body for POST /api/chats/{id}/handoff/retry: run a failed switch's last step again on an account
    (a handoff's create, or a rebind's restart)."""

    account_id: str = Field(description="The signed-in account to try; may differ from the failed attempt's")


class HandoffRetryResponse(FrozenModel):
    """Response from POST /api/chats/{id}/handoff/retry."""

    status: str = Field(description="'converging' once the retry has begun")
    phase: HandoffPhase = Field(description="The phase the chat is in when the route answers")


class ActiveAgentSnapshot(FrozenModel):
    """The agent-level facts about a chat's active agent that the chat pages render."""

    agent_id: str = Field(description="The active agent's mngr id")
    name: str = Field(description="The active agent's mngr name (the chat's canonical name)")
    harness: HarnessType = Field(description="The harness the active agent runs")
    account_id: str | None = Field(description="The account the active agent is bound to (its ``account`` label)")
    state: str = Field(description="The active agent's mngr lifecycle state")
    activity_state: ActivityState | None = Field(description="THINKING / TOOL_RUNNING / IDLE, or None when untracked")
    model_choice: ModelChoice | None = Field(description="The live model/effort/fast selection, or None")
    queued_messages: tuple[QueuedMessageState, ...] = Field(description="The harness queue, in enqueue order")
    shoulder_tap_available: bool = Field(description="Whether something is queued and no send is in flight")


class ChatSnapshot(FrozenModel):
    """One chat as the chat pages see it: what the ``chats_updated`` WebSocket message carries."""

    chat_id: ChatId = Field(description="The chat's id")
    title: str = Field(description="The name the user sees (the ``display_name`` label, else the mngr name)")
    name: str = Field(description="The chat's canonical mngr name")
    project: str | None = Field(description="The project the chat was created in, or None")
    status: InstanceStatus = Field(description="The chat's status, as its instance record reports it")
    labels: dict[str, str] = Field(description="The active agent's mngr labels")
    agent_ids: tuple[str, ...] = Field(description="Every agent of the chat, in order; the last is the active one")
    handoff: HandoffState | None = Field(
        description="The in-progress handoff, or None while the chat is not converging"
    )
    active_agent: ActiveAgentSnapshot = Field(description="The agent the chat currently runs on")
    last_messaged_at: float | None = Field(
        description="Epoch seconds of the chat's most recent message, or None when it has never been messaged; the chat list orders on it",
    )


class ChatSegmentInfo(FrozenModel):
    """One agent of a chat as the transcript reads it: which agent, its place, and whether it is the live one."""

    agent: AgentInfo = Field(description="The agent, with its resolved state and config dirs")
    seq: int = Field(ge=1, description="The agent's 1-based position in the chat")
    is_active: bool = Field(description="Whether this is the agent the chat runs on (its segment is the live one)")
    recorded_event_count: int | None = Field(
        description="The segment's main-transcript event count recorded when the agent was archived; None for the live one"
    )
    ended_at: datetime | None = Field(description="When the agent was archived; None for the live one")
    opening_message_id: str | None = Field(
        default=None,
        description="The send-time id of the message the user switched to this agent with, if folded into its prompt",
    )
    opening_message: str | None = Field(default=None, description="That message's text, for the switch marker")
    is_fresh_start: bool = Field(
        default=False, description="Whether the handoff that started this agent asked for no summary (a fresh start)"
    )


class ChatListResponse(FrozenModel):
    """Response from GET /api/chats: every chat this app lists, as the pages see it."""

    chats: tuple[ChatSnapshot, ...] = Field(description="One snapshot per listed chat")


class CreateChatRequest(FrozenModel):
    """Request body for creating a chat agent. The account decides which harness it runs on."""

    name: str = Field(
        default="",
        description='Display name for the new chat agent; empty mints the first free "Chat N" server-side, '
        "whatever harness the account runs on",
    )
    account_id: str = Field(
        default="",
        description="Signed-in account to bind the chat to; empty picks the most recently used one",
    )
    chat_id: str = Field(
        default="",
        description="A chat minted earlier while nothing was signed in (or one whose create failed) to launch now",
    )
    message: str = Field(
        default="",
        description="The first message the chat sends once it is running; empty sends none "
        "(a chat minted earlier keeps the message it was minted with)",
    )
    model: ModelPick | None = Field(
        default=None,
        description="The model the chat runs on, applied once the agent is up and before its first message; "
        "None for the harness's default",
    )


class ProvisionalChatPhase(LowerCaseStrEnum):
    """Where a chat that is not an agent yet stands."""

    # Minted with nothing signed in: the page shows the provider chooser, and the launch waits.
    AWAITING_ACCOUNT = auto()
    # A seeded chat (``chat_seed.py``) whose transcript is on the page with a composer: the
    # user's first send is what picks the account (the chooser opens then) and launches it.
    AWAITING_FIRST_SEND = auto()
    # Its ``mngr create`` is running.
    CREATING = auto()
    # Its ``mngr create`` failed; ``error`` says how, and the page can try again.
    FAILED = auto()


class ProvisionalChat(FrozenModel):
    """A chat the app has minted but whose first agent mngr does not know yet.

    Listed as a referenced instance under its chat id (the id mngr will give its first agent),
    and pushed to the chat pages verbatim as the ``provisional_chat_created`` message.
    """

    chat_id: ChatId = Field(description="The chat's id, which its first agent will carry")
    name: str = Field(description="The display name minted for it")
    project_id: str = Field(default="", description="The project it was started in, for the agent's label")
    account_id: str = Field(default="", description="The account it launches on; empty while awaiting one")
    message: str = Field(default="", description="The first message the chat sends once it launches; empty for none")
    phase: ProvisionalChatPhase = Field(description="Where the creation stands")
    error: str | None = Field(default=None, description="Why the creation failed, in the failed phase")
    is_seeded: bool = Field(
        default=False,
        description="Whether the chat has a seed segment to show while it is created (``chat_seed.py``)",
    )


class SeedChatRequest(FrozenModel):
    """Request body for POST /api/chats/seed: the conversation the Mind app had before the workspace existed."""

    title: str = Field(default="", description='The chat\'s display name; empty mints the first free "Chat N"')
    turns: tuple[SeedTurn, ...] = Field(min_length=1, description="The turns, in order")


class CreatedChat(FrozenModel):
    """A freshly-created chat's identity: its id and its name pair."""

    chat_id: ChatId = Field(description="The chat's id (its first agent's id, minted before the create)")
    name: str = Field(description="The chat's true (canonical) name, e.g. 'Chat-2'")
    display_name: str = Field(description="The human-readable display name, e.g. 'Chat 2'")


class CreateChatResponse(FrozenModel):
    """Response from POST /api/chats/create."""

    chat_id: str = Field(description="The chat's id (its first agent's id, minted before the create)")
    name: str = Field(description="The chat's true (canonical) name, e.g. 'Chat-2'")
    display_name: str = Field(description="The human-readable display name, e.g. 'Chat 2'")


class RenameChatRequest(FrozenModel):
    """The body of ``POST /api/chats/<chat_id>/rename``: the name the user typed."""

    title: str = Field(min_length=1, max_length=256, description="The chat's new display name")


class RenameChatResponse(FrozenModel):
    """Response from ``POST /api/chats/<chat_id>/rename``."""

    status: str = Field(description="Always 'ok'")


class DestroyAgentResponse(FrozenModel):
    """Response from the agent destroy endpoint."""

    status: str = Field(description="Result of the destroy operation")


class StartAgentResponse(FrozenModel):
    """Response from the agent start endpoint."""

    status: str = Field(description="Result of the start operation")


class StopAgentResponse(FrozenModel):
    """Response from the agent stop endpoint."""

    status: str = Field(description="Result of the stop operation")


class ClaudeAuthStatusResponse(FrozenModel):
    """Response from /api/claude-auth/status."""

    logged_in: bool = Field(description="Whether claude is currently authenticated")
    auth_method: str | None = Field(default=None, description="e.g. 'oauth', 'api_key', 'oauth_token'")
    api_provider: str | None = Field(default=None, description="e.g. 'anthropic', 'claudeai', 'firstParty'")
    email: str | None = Field(default=None, description="The authenticated user's email, if any")
    org_id: str | None = Field(default=None, description="Anthropic organization ID, if any")
    org_name: str | None = Field(default=None, description="Anthropic organization name, if any")
    subscription_type: str | None = Field(
        default=None, description="Subscription tier (e.g. 'Max'); absent for token/Console sessions"
    )
    auth_mode: str = Field(
        default="none",
        description="Effective auth mode: 'subscription', 'console', 'imbue', 'api_key', or 'none'. Derived from "
        "the managed settings-env keys when any are present, otherwise folded from `claude auth status`.",
    )
    masked_key_suffix: str | None = Field(
        default=None, description="Last few characters of the managed key/token, for display"
    )
    workspace_id: str | None = Field(
        default=None,
        description=(
            "This workspace's id (its services agent id; the machine's host id as a fallback), "
            "for the desktop app's key-mint page link"
        ),
    )


class ClaudeOAuthLoginStartRequest(FrozenModel):
    """Request body for POST /api/claude-auth/oauth/start."""

    provider: str = Field(description="Which browser sign-in to run: 'claudeai' or 'console'")


class ClaudeSetupTokenStartResponse(FrozenModel):
    """Response from POST /api/claude-auth/setup-token/start."""

    session_id: str = Field(description="Opaque token identifying the in-flight setup-token session")
    oauth_url: str = Field(description="URL the user opens to authorize the login")


class ClaudeSetupTokenPollRequest(FrozenModel):
    """Request body for POST /api/claude-auth/setup-token/poll."""

    session_id: str = Field(description="session_id returned by /setup-token/start")


class ClaudeSetupTokenPollResponse(FrozenModel):
    """Response from POST /api/claude-auth/setup-token/poll."""

    is_complete: bool = Field(description="Whether the token was minted and written")
    status: ClaudeAuthStatusResponse | None = Field(
        default=None, description="Auth status after completion; None while still pending"
    )


class ClaudeSetupTokenSubmitCodeRequest(FrozenModel):
    """Request body for POST /api/claude-auth/setup-token/submit-code."""

    session_id: str = Field(description="session_id returned by /setup-token/start")
    code: str = Field(description="The CODE#STATE the user pasted from the browser")


class ClaudeAuthCredentialsRequest(FrozenModel):
    """Request body for POST /api/claude-auth/submit-credentials.

    `credentials` is env-var-style lines covering the managed auth keys:
    an `ANTHROPIC_API_KEY=...` line (optionally with `ANTHROPIC_BASE_URL=...`
    for the Imbue/LiteLLM case), or a `CLAUDE_CODE_OAUTH_TOKEN=...` line.
    """

    credentials: SecretStr = Field(description="Env-var-style credential lines (KEY=VALUE per line)")


class LatchkeyPermissionInfo(FrozenModel):
    """A grantable permission within a latchkey scope, from the gateway catalog."""

    name: str = Field(description="Permission schema name, e.g. 'slack-read-all'")
    description: str | None = Field(default=None, description="Plain-English summary of the permission")


class LatchkeyScopeInfo(FrozenModel):
    """Display info for a latchkey permission scope, from the gateway catalog.

    Returned by GET /api/latchkey/scopes/{scope}; the frontend uses
    `display_name` to label a permission-request card and the per-permission
    descriptions for hover tooltips.
    """

    scope: str = Field(description="Detent scope schema name, e.g. 'slack-api'")
    display_name: str = Field(description="Human-readable service name, e.g. 'Slack'")
    description: str | None = Field(default=None, description="Plain-English summary of the scope")
    permissions: tuple[LatchkeyPermissionInfo, ...] = Field(
        default=(), description="Permissions grantable under the scope"
    )
