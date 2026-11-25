import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final, assert_never

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from use_ai_integration_skill.claude_p import read_workspace_ai_credentials
from use_ai_integration_skill.data_types import (
    AIAccount,
    AIAccountListing,
    AIBilling,
    AICredential,
    AICredentialKind,
    AIProvider,
)
from use_ai_integration_skill.errors import (
    GOOGLE_LANE,
    NoUsableAccountError,
    UnsupportedProviderError,
    unsupported_provider_message,
)

SUPPORTED_PROVIDER_BY_LANE: Final[Mapping[str, AIProvider]] = {
    "anthropic": AIProvider.ANTHROPIC,
    "openai": AIProvider.OPENAI,
}

_ACCOUNTS_ROOT_ENV_VAR: Final = "MINDS_ACCOUNTS_ROOT"
_INDEX_FILENAME: Final = "index.json"
_CODEX_AUTH_FILENAME: Final = "auth.json"
_CLAUDE_GLOBAL_CONFIG_FILENAME: Final = ".claude.json"
_CODEX_API_KEY_AUTH_MODE: Final = "apikey"
_CODEX_SIGN_IN_AUTH_MODE: Final = "chatgpt"
_NO_ACCOUNT_MESSAGE: Final = "No AI account is signed in to this workspace; sign in to Anthropic or OpenAI to call a model from code."


class _IndexRow(BaseModel):
    """One account row of the chat app's account index."""

    model_config = ConfigDict(extra="ignore")

    id: str
    lane: str
    display: str = ""
    name: str = ""


class _AccountIndex(BaseModel):
    """The chat app's account index, as far as choosing an account needs it."""

    model_config = ConfigDict(extra="ignore")

    accounts: list[_IndexRow] = Field(default_factory=list)
    mru: str | None = None
    default_account: str | None = None


class _CodexAuthFile(BaseModel):
    """What a codex ``auth.json`` says about how the account signs in; its tokens are never read."""

    model_config = ConfigDict(extra="ignore")

    auth_mode: str | None = None
    openai_api_key: SecretStr | None = Field(default=None, alias="OPENAI_API_KEY")


class _ClaudeGlobalConfig(BaseModel):
    """Whether claude's own config holds a Console sign-in's key."""

    model_config = ConfigDict(extra="ignore")

    primary_api_key: SecretStr | None = Field(default=None, alias="primaryApiKey")


def accounts_root() -> Path:
    override = os.environ.get(_ACCOUNTS_ROOT_ENV_VAR, "").strip()
    return Path(override) if override else Path.home() / ".minds" / "accounts"


def _read_index(root: Path) -> _AccountIndex | None:
    """The account index under ``root``, or None when there is none."""
    index_path = root / _INDEX_FILENAME
    try:
        text = index_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as e:
        raise NoUsableAccountError(
            f"The account index at {index_path} cannot be read: {e}"
        ) from e
    try:
        return _AccountIndex.model_validate_json(text)
    except ValidationError as e:
        raise NoUsableAccountError(
            f"The account index at {index_path} is not readable: {e}"
        ) from e


def _account_label(row: _IndexRow) -> str:
    return row.name or row.display or row.lane


def _provider_label(row: _IndexRow) -> str:
    return row.display or row.lane


def _choose_default_row(index: _AccountIndex) -> _IndexRow | None:
    """The pinned default, else the most recently used, else the oldest, among supported accounts."""
    supported = [
        row for row in index.accounts if row.lane in SUPPORTED_PROVIDER_BY_LANE
    ]
    if not supported:
        return None
    pinned = next((row for row in supported if row.id == index.default_account), None)
    if pinned is not None:
        return pinned
    return next((row for row in supported if row.id == index.mru), supported[0])


def _only_unsupported_accounts_message(rows: Sequence[_IndexRow]) -> str:
    labels = list(dict.fromkeys(_provider_label(row) for row in rows))
    message = (
        f"None of this workspace's AI accounts can call a model from code ({', '.join(labels)}); "
        "sign in to Anthropic or OpenAI to call a model from code."
    )
    google_row = next((row for row in rows if row.lane == GOOGLE_LANE), None)
    if google_row is None:
        return message
    return f"{message} {unsupported_provider_message(GOOGLE_LANE, _provider_label(google_row))}"


def _named_row(index: _AccountIndex, account_id: str) -> _IndexRow:
    row = next((row for row in index.accounts if row.id == account_id), None)
    if row is None:
        raise NoUsableAccountError(f"No signed-in account has the id {account_id!r}.")
    if row.lane not in SUPPORTED_PROVIDER_BY_LANE:
        raise UnsupportedProviderError(row.lane, _provider_label(row))
    return row


def resolve_ai_account(account_id: str | None, root: Path) -> AIAccount:
    """The named account, or the workspace's default among the accounts code can call."""
    index = _read_index(root)
    if index is None or not index.accounts:
        raise NoUsableAccountError(_NO_ACCOUNT_MESSAGE)
    if account_id is not None:
        row = _named_row(index, account_id)
    else:
        default_row = _choose_default_row(index)
        if default_row is None:
            raise NoUsableAccountError(
                _only_unsupported_accounts_message(index.accounts)
            )
        row = default_row
    account_dir = root / row.id
    if not account_dir.is_dir():
        raise NoUsableAccountError(
            f"The account {_account_label(row)} has no folder on disk; sign in to it again."
        )
    return AIAccount(
        account_id=row.id,
        provider=SUPPORTED_PROVIDER_BY_LANE[row.lane],
        account_dir=account_dir,
        label=_account_label(row),
    )


def list_ai_accounts(root: Path) -> list[AIAccountListing]:
    index = _read_index(root)
    if index is None:
        return []
    default_row = _choose_default_row(index)
    return [
        AIAccountListing(
            account_id=row.id,
            lane=row.lane,
            label=_account_label(row),
            is_supported=row.lane in SUPPORTED_PROVIDER_BY_LANE,
            is_default=default_row is not None and row.id == default_row.id,
        )
        for row in index.accounts
    ]


def _is_console_sign_in(account_dir: Path, *, has_oauth_token: bool) -> bool:
    """Whether a keyless Anthropic account is a Console sign-in, which claude bills per token."""
    if has_oauth_token:
        return False
    try:
        text = (account_dir / _CLAUDE_GLOBAL_CONFIG_FILENAME).read_text(
            encoding="utf-8"
        )
    except OSError:
        return False
    try:
        config = _ClaudeGlobalConfig.model_validate_json(text)
    except ValidationError:
        return False
    return config.primary_api_key is not None and len(config.primary_api_key) > 0


def _read_anthropic_credential(
    account: AIAccount, *, anthropic_snapshot_path: str
) -> AICredential:
    credentials = read_workspace_ai_credentials(
        anthropic_snapshot_path, account_dir=str(account.account_dir)
    )
    if credentials.api_key:
        return AICredential(
            kind=AICredentialKind.ANTHROPIC_API_KEY,
            billing=AIBilling.API_KEY,
            api_key=SecretStr(credentials.api_key),
            api_base=(credentials.base_url or "").rstrip("/") or None,
        )
    is_console = _is_console_sign_in(
        account.account_dir, has_oauth_token=credentials.oauth_token is not None
    )
    return AICredential(
        kind=AICredentialKind.ANTHROPIC_SIGN_IN,
        billing=AIBilling.API_KEY if is_console else AIBilling.SUBSCRIPTION,
        api_key=None,
        api_base=None,
    )


def _read_openai_credential(account: AIAccount) -> AICredential:
    auth_path = account.account_dir / _CODEX_AUTH_FILENAME
    try:
        text = auth_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise NoUsableAccountError(
            f"The OpenAI account {account.label} is signed out; sign in to it again."
        ) from None
    except OSError as e:
        raise NoUsableAccountError(
            f"The OpenAI account {account.label}'s sign-in cannot be read: {e}"
        ) from e
    try:
        auth = _CodexAuthFile.model_validate_json(text)
    except ValidationError as e:
        raise NoUsableAccountError(
            f"The OpenAI account {account.label}'s sign-in is not readable: {e}"
        ) from e
    api_key = (
        auth.openai_api_key
        if auth.openai_api_key is not None and len(auth.openai_api_key) > 0
        else None
    )
    auth_mode = auth.auth_mode or (
        _CODEX_API_KEY_AUTH_MODE if api_key is not None else _CODEX_SIGN_IN_AUTH_MODE
    )
    if auth_mode != _CODEX_API_KEY_AUTH_MODE:
        return AICredential(
            kind=AICredentialKind.OPENAI_SIGN_IN,
            billing=AIBilling.SUBSCRIPTION,
            api_key=None,
            api_base=None,
        )
    if api_key is None:
        raise NoUsableAccountError(
            f"The OpenAI account {account.label} has no API key; sign in to it again."
        )
    return AICredential(
        kind=AICredentialKind.OPENAI_API_KEY,
        billing=AIBilling.API_KEY,
        api_key=api_key,
        api_base=None,
    )


def read_ai_credential(
    account: AIAccount, *, anthropic_snapshot_path: str
) -> AICredential:
    """How a call on ``account`` authenticates; an Anthropic key in the snapshot applies to every Anthropic account."""
    match account.provider:
        case AIProvider.ANTHROPIC:
            return _read_anthropic_credential(
                account, anthropic_snapshot_path=anthropic_snapshot_path
            )
        case AIProvider.OPENAI:
            return _read_openai_credential(account)
        case _ as unreachable:
            assert_never(unreachable)
