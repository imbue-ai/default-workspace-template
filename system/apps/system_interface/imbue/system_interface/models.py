from pydantic import Field
from pydantic import SecretStr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.system_interface.activity_state import ActivityState
from imbue.system_interface.harnesses.harness_type import DEFAULT_HARNESS
from imbue.system_interface.harnesses.harness_type import HarnessType
from imbue.system_interface.harnesses.model import ModelAxis
from imbue.system_interface.harnesses.model import ModelChoice
from imbue.system_interface.harnesses.model import ModelOption


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
    """Request body for sending a message to an agent."""

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
    """Request body for POST /api/agents/{id}/model.

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
    """Response from GET /api/agents/{id}/model-options.

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
    """Response from GET /api/agents/{id}/powered-by."""

    label: str = Field(description="The agent harness's verbatim credit text, or '' when that harness shows no credit")


class FastModePromptAnsweredResponse(FrozenModel):
    """Response from POST /api/agents/<id>/fast-mode-answered."""

    status: str = Field(description="'ok' when the answered label was recorded")


class AttachmentUploadResponse(FrozenModel):
    """Response from the chat attachment upload endpoint."""

    path: str = Field(description="Absolute path to the stored upload on the agent VM")
    size: int = Field(description="Size of the stored upload in bytes")


class InterruptAgentResponse(FrozenModel):
    """Response from the /api/agents/{id}/interrupt endpoint."""

    status: str = Field(description="Status of the interrupt operation")


class DrainToComposerResponse(FrozenModel):
    """Response from POST /api/agents/{id}/drain-to-composer.

    Carries the concatenated queued block the frontend drops into the composer
    (unsent) for the user to edit and send. Empty when the queue was already
    drained by the time the action fired.
    """

    block: str = Field(description="The queued messages as one concatenated block, or '' if the queue was empty")


class ShoulderTapAtomicResponse(FrozenModel):
    """Response from POST /api/agents/{id}/shoulder-tap-atomic.

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
    """Agent state for the unified WebSocket stream."""

    id: str = Field(description="The agent's unique identifier")
    name: str = Field(description="The agent's human-readable name")
    state: str = Field(description="The agent's lifecycle state")
    labels: dict[str, str] = Field(description="Agent labels (e.g., user_created, chat_parent_id)")
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


class CreateChatRequest(FrozenModel):
    """Request body for creating a chat agent. The account decides which harness it runs on."""

    name: str = Field(
        default="",
        description="Display name for the new chat agent; empty mints the first free "
        '"<word> N" for the account\'s harness server-side ("Chat 1", "Codex 2", ...)',
    )
    account_id: str = Field(
        default="",
        description="Signed-in account to bind the chat to; empty picks the most recently used one",
    )


class CreatedChatAgent(FrozenModel):
    """A freshly-created chat agent's identity: its id and its name pair."""

    agent_id: str = Field(description="The pre-generated agent ID")
    name: str = Field(description="The agent's true (canonical) name, e.g. 'Chat-2'")
    display_name: str = Field(description="The human-readable display name, e.g. 'Chat 2'")


class CreateAgentResponse(FrozenModel):
    """Response from agent creation endpoints."""

    agent_id: str = Field(description="The pre-generated agent ID")
    name: str = Field(description="The agent's true (canonical) name, e.g. 'Chat-2'")
    display_name: str = Field(description="The human-readable display name, e.g. 'Chat 2'")


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
