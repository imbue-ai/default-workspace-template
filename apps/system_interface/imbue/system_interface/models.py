import re

from pydantic import Field
from pydantic import SecretStr
from pydantic import field_validator

from imbue.imbue_common.frozen_model import FrozenModel

_REPO_NAME_REGEX = re.compile(r"^[A-Za-z0-9._-]+$")
_ALLOWED_VISIBILITIES = frozenset({"private", "public"})


class InvalidInspirationFieldError(ValueError):
    """Raised when an inspiration publish field fails validation.

    Inherits from ValueError so pydantic field validators surface it as a
    normal validation error, while remaining a named (non-built-in) type.
    """


def _validate_repo_name_str(value: str) -> str:
    """Reject repo/slug names that don't match ^[A-Za-z0-9._-]+$ or start with '-' (argument-injection guard)."""
    if not _REPO_NAME_REGEX.match(value) or value.startswith("-"):
        raise InvalidInspirationFieldError("must match ^[A-Za-z0-9._-]+$ and not start with '-'")
    return value


def _validate_visibility_str(value: str) -> str:
    """Reject a visibility that is not 'private' or 'public'."""
    if value not in _ALLOWED_VISIBILITIES:
        raise InvalidInspirationFieldError("visibility must be 'private' or 'public'")
    return value


class AgentCreationError(ValueError):
    """Raised when agent creation fails due to invalid input."""

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


class SendMessageResponse(FrozenModel):
    """Response from the message endpoint."""

    status: str = Field(description="Status of the send operation")


class InterruptAgentResponse(FrozenModel):
    """Response from the /api/agents/{id}/interrupt endpoint."""

    status: str = Field(description="Status of the interrupt operation")


class ErrorResponse(FrozenModel):
    """Error response body."""

    detail: str = Field(description="Human-readable error description")


class AgentStateItem(FrozenModel):
    """Agent state for the unified WebSocket stream."""

    id: str = Field(description="The agent's unique identifier")
    name: str = Field(description="The agent's human-readable name")
    state: str = Field(description="The agent's lifecycle state")
    labels: dict[str, str] = Field(description="Agent labels (e.g., user_created, chat_parent_id)")
    work_dir: str | None = Field(description="The agent's working directory path")
    activity_state: str | None = Field(
        default=None,
        description=(
            "Per-agent chat activity state value (THINKING / TOOL_RUNNING / "
            "IDLE), or None when no activity tracking is available for this "
            "agent."
        ),
    )


class ApplicationEntry(FrozenModel):
    """An application registered in runtime/applications.toml."""

    name: str = Field(description="Application name (e.g., 'web', 'terminal')")
    url: str = Field(description="Local URL where the application is accessible")


class CreateWorktreeRequest(FrozenModel):
    """Request body for creating a worktree agent."""

    name: str = Field(description="Name for the new worktree agent")
    selected_agent_id: str = Field(
        default="",
        description="ID of the agent whose work dir to create the worktree from",
    )


class CreateChatRequest(FrozenModel):
    """Request body for creating a chat agent."""

    name: str = Field(description="Name for the new chat agent")


class CreateAgentResponse(FrozenModel):
    """Response from agent creation endpoints."""

    agent_id: str = Field(description="The pre-generated agent ID")


class RandomNameResponse(FrozenModel):
    """Response from the random name endpoint."""

    name: str = Field(description="A random agent name")


class DestroyAgentResponse(FrozenModel):
    """Response from the agent destroy endpoint."""

    status: str = Field(description="Result of the destroy operation")


class StartAgentResponse(FrozenModel):
    """Response from the agent start endpoint."""

    status: str = Field(description="Result of the start operation")


class ClaudeAuthStatusResponse(FrozenModel):
    """Response from /api/claude-auth/status."""

    logged_in: bool = Field(description="Whether claude is currently authenticated")
    auth_method: str | None = Field(default=None, description="e.g. 'oauth', 'api_key'")
    api_provider: str | None = Field(default=None, description="e.g. 'anthropic', 'claudeai'")
    email: str | None = Field(default=None, description="The authenticated user's email, if any")
    org_id: str | None = Field(default=None, description="Anthropic organization ID, if any")
    org_name: str | None = Field(default=None, description="Anthropic organization name, if any")
    subscription_type: str | None = Field(
        default=None, description="Subscription tier (e.g. 'Max'); absent for Console accounts"
    )


class ClaudeOAuthStartRequest(FrozenModel):
    """Request body for POST /api/claude-auth/start."""

    provider: str = Field(description="Either 'claudeai' (subscription) or 'console'")


class ClaudeOAuthStartResponse(FrozenModel):
    """Response from POST /api/claude-auth/start."""

    session_id: str = Field(description="Opaque token identifying the in-flight OAuth session")
    oauth_url: str = Field(description="URL the user opens to authorize the login")


class ClaudeOAuthSubmitCodeRequest(FrozenModel):
    """Request body for POST /api/claude-auth/submit-code."""

    session_id: str = Field(description="session_id returned by /start")
    code: str = Field(description="The CODE#STATE the user pasted from the browser")


class ClaudeAuthApiKeyRequest(FrozenModel):
    """Request body for POST /api/claude-auth/submit-api-key."""

    api_key: SecretStr = Field(description="A raw `sk-ant-...` API key")


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


class InspirationPublishRequest(FrozenModel):
    """POST /api/inspiration/publish-request body (posted by the /publish-inspiration skill)."""

    slug: str = Field(description="Inspiration slug; identifies this proposal (e.g. 'slack-inbox')")
    title: str = Field(description="Proposed human-readable inspiration title")
    description: str = Field(default="", description="Proposed description / summary")
    repo_name: str = Field(description="Proposed GitHub repo name (defaults to a slug of the title)")
    visibility: str = Field(default="private", description="'private' or 'public'")
    thumbnail_svg: str = Field(default="", description="Untrusted SVG thumbnail markup to preview")

    @field_validator("slug")
    @classmethod
    def _validate_slug(cls, value: str) -> str:
        return _validate_repo_name_str(value)

    @field_validator("repo_name")
    @classmethod
    def _validate_repo_name(cls, value: str) -> str:
        return _validate_repo_name_str(value)

    @field_validator("visibility")
    @classmethod
    def _validate_visibility(cls, value: str) -> str:
        return _validate_visibility_str(value)


class InspirationPublishConfirm(FrozenModel):
    """POST /api/inspiration/publish-confirm body (posted by the frontend modal on Publish)."""

    slug: str = Field(description="Slug of the proposal being confirmed; must match the pending request")
    title: str = Field(description="User-edited title")
    description: str = Field(default="", description="User-edited description")
    repo_name: str = Field(description="User-edited repo name")
    visibility: str = Field(default="private", description="'private' or 'public'")
    thumbnail_svg: str = Field(default="", description="User-confirmed (possibly edited) SVG thumbnail")

    @field_validator("slug")
    @classmethod
    def _validate_slug(cls, value: str) -> str:
        return _validate_repo_name_str(value)

    @field_validator("repo_name")
    @classmethod
    def _validate_repo_name(cls, value: str) -> str:
        return _validate_repo_name_str(value)

    @field_validator("visibility")
    @classmethod
    def _validate_visibility(cls, value: str) -> str:
        return _validate_visibility_str(value)


class InspirationPublishResponse(FrozenModel):
    """Written to the response file for the skill to poll; also the /publish-confirm reply body."""

    status: str = Field(description="'confirmed' or 'aborted'")
    slug: str = Field(description="Slug this response pertains to")
    title: str = Field(default="")
    description: str = Field(default="")
    repo_name: str = Field(default="")
    visibility: str = Field(default="private")
    thumbnail_svg: str = Field(default="")


class InspirationStatusResponse(FrozenModel):
    """GET /api/inspiration/status reply."""

    pending_slug: str | None = Field(default=None, description="Slug of the currently-open proposal, or null")
    has_pending: bool = Field(default=False, description="Whether a publish proposal is awaiting user action")


class GitHubAuthStatusResponse(FrozenModel):
    """GET /api/github-auth/status reply (parsed `gh auth status`)."""

    logged_in: bool = Field(description="Whether gh is authenticated for github.com")
    username: str | None = Field(default=None, description="Authenticated GitHub login, if any")
    host: str = Field(default="github.com", description="gh host checked")


class GitHubAuthStartRequest(FrozenModel):
    """POST /api/github-auth/start body. Web/device flow; no fields required today."""

    host: str = Field(default="github.com", description="gh host to authenticate against")


class GitHubAuthStartResponse(FrozenModel):
    """POST /api/github-auth/start reply (device/web flow)."""

    session_id: str = Field(description="Opaque token for the in-flight gh login session")
    user_code: str = Field(description="Device user code the user types into GitHub")
    verification_url: str = Field(description="URL the user opens to enter the code")


class GitHubAuthSubmitCodeRequest(FrozenModel):
    """POST /api/github-auth/submit-code body — completes the device/web flow."""

    session_id: str = Field(description="session_id returned by /start")


class GitHubAuthRawTokenRequest(FrozenModel):
    """POST /api/github-auth/submit-raw-token body — paste-a-PAT path."""

    token: SecretStr = Field(description="A GitHub personal access token (ghp_.../github_pat_...)")
    host: str = Field(default="github.com", description="gh host to authenticate against")
