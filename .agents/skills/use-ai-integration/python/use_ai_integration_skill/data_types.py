from enum import StrEnum
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from use_ai_integration_skill.claude_p import Usage


class AIProvider(StrEnum):
    """A provider that code can call; the value is the chat app's lane id."""

    ANTHROPIC = "anthropic"
    OPENAI = "openai"


class AIBilling(StrEnum):
    """How a call is paid for."""

    SUBSCRIPTION = "subscription"
    API_KEY = "api_key"


class AICredentialKind(StrEnum):
    """What an account holds, which decides how it is called."""

    ANTHROPIC_API_KEY = "anthropic_api_key"
    ANTHROPIC_SIGN_IN = "anthropic_sign_in"
    OPENAI_API_KEY = "openai_api_key"
    OPENAI_SIGN_IN = "openai_sign_in"


class CodexSandboxMode(StrEnum):
    """The values of ``codex exec --sandbox``."""

    READ_ONLY = "read-only"
    WORKSPACE_WRITE = "workspace-write"
    DANGER_FULL_ACCESS = "danger-full-access"


class AIAccount(BaseModel):
    """A signed-in account a call can run on."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    account_id: str = Field(
        description="The account's id, which is also its folder's name"
    )
    provider: AIProvider = Field(description="The provider the account signs in to")
    account_dir: Path = Field(description="The account's folder")
    label: str = Field(
        description="The user's name for the account, else its provider's name"
    )


class AIAccountListing(BaseModel):
    """One row of the workspace's accounts, as the skill lists them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    account_id: str = Field(description="The account's id")
    lane: str = Field(
        description="The chat app's lane id, e.g. anthropic, openai, google"
    )
    label: str = Field(
        description="The user's name for the account, else its provider's name"
    )
    is_supported: bool = Field(
        description="Whether code can call a model on this account"
    )
    is_default: bool = Field(
        description="Whether a call that names no account runs on this one"
    )


class AICredential(BaseModel):
    """How an account authenticates a call, and how that call is billed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: AICredentialKind = Field(description="What the account holds")
    billing: AIBilling = Field(description="How a call on this credential is paid for")
    api_key: SecretStr | None = Field(
        description="The API key, for the API-key kinds only"
    )
    api_base: str | None = Field(
        description="The base URL an Anthropic key is sent to, for a proxy"
    )

    @model_validator(mode="after")
    def _check_fields_match_kind(self) -> Self:
        is_api_key_kind = self.kind in (
            AICredentialKind.ANTHROPIC_API_KEY,
            AICredentialKind.OPENAI_API_KEY,
        )
        if is_api_key_kind != (self.api_key is not None):
            raise ValueError(
                f"a {self.kind} credential holds an API key exactly when it is a key kind"
            )
        if (
            self.api_base is not None
            and self.kind is not AICredentialKind.ANTHROPIC_API_KEY
        ):
            raise ValueError(f"a {self.kind} credential has no API base URL")
        if is_api_key_kind and self.billing is not AIBilling.API_KEY:
            raise ValueError(f"a {self.kind} credential is billed per token")
        return self


class AIResult(BaseModel):
    """The answer to one model call from code."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = Field(description="The model's answer")
    provider: AIProvider = Field(description="The provider the call ran on")
    account_id: str = Field(description="The account the call ran on")
    model: str | None = Field(
        description="The model that ran; None when the account's default ran unreported"
    )
    usage: Usage | None = Field(
        description="Token counts; input_tokens excludes cache reads and writes"
    )
    billing: AIBilling = Field(description="How the call is paid for")
    cost_usd: float | None = Field(
        description="Dollars charged, for API-key billing only; None when unpriced"
    )
    raw: dict[str, object] = Field(
        description="What the provider's CLI or API returned, verbatim"
    )

    @model_validator(mode="after")
    def _check_cost_matches_billing(self) -> Self:
        if self.billing is AIBilling.SUBSCRIPTION and self.cost_usd is not None:
            raise ValueError("a call billed to a subscription has no dollar cost")
        return self


class CodexExecOutcome(BaseModel):
    """What one ``codex exec --json`` run answered."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = Field(description="The last agent message")
    usage: Usage = Field(description="Token counts from the completed turn")
    events: list[dict[str, object]] = Field(description="Every JSONL event, in order")


class LiteLLMRequest(BaseModel):
    """One litellm completion, fully specified."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str = Field(
        description="The provider-prefixed model, e.g. anthropic/claude-haiku-4-5"
    )
    system: str = Field(description="The system prompt")
    prompt: str = Field(description="The user prompt")
    api_key: SecretStr = Field(description="The API key")
    api_base: str | None = Field(
        description="The base URL, for a proxy; None for the provider's own"
    )
    timeout_seconds: float = Field(description="How long the call may take")


class LiteLLMReply(BaseModel):
    """What one litellm completion answered."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = Field(description="The model's answer")
    usage: Usage = Field(
        description="Token counts; input_tokens excludes cache reads and writes"
    )
    cost_usd: float | None = Field(
        description="Dollars litellm prices the call at; None when unpriced"
    )
    raw: dict[str, object] = Field(description="The response, as litellm returned it")
