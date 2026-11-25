from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final, assert_never

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    SecretStr,
    ValidationError,
)

from use_ai_integration_skill.claude_p import (
    minds_accounts_root,
    read_workspace_ai_credentials,
)
from use_ai_integration_skill.data_types import (
    AIAccount,
    AIAccountListing,
    AIBilling,
    AICredential,
    AICredentialKind,
    AIProvider,
)
from use_ai_integration_skill.errors import (
    SIGN_IN_ADVICE,
    NoUsableAccountError,
    UnsupportedProviderError,
)

SUPPORTED_PROVIDER_BY_LANE: Final[Mapping[str, AIProvider]] = {
    provider.value: provider for provider in AIProvider
}

_INDEX_FILENAME: Final = "index.json"
_CODEX_AUTH_FILENAME: Final = "auth.json"
_CLAUDE_GLOBAL_CONFIG_FILENAME: Final = ".claude.json"
_CODEX_API_KEY_AUTH_MODE: Final = "apikey"
_CODEX_SIGN_IN_AUTH_MODE: Final = "chatgpt"
_AGY_OAUTH_TOKEN_RELATIVE_PATH: Final = Path(
    ".gemini", "antigravity-cli", "antigravity-oauth-token"
)
_PI_AUTH_FILENAME: Final = "auth.json"
_PI_API_KEY_ENTRY_TYPE: Final = "api_key"
_ANTHROPIC_PI_PROVIDER_ID: Final = "anthropic"
_CLAUDE_SUBSCRIPTION_TOKEN_MARKER: Final = "sk-ant-oat"
_NO_ACCOUNT_MESSAGE: Final = "No AI account is signed in to this workspace; sign in to one to call a model from code."


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


class _PiAuthEntry(BaseModel):
    """One provider's entry in pi's ``auth.json``; its key is checked, never passed on."""

    model_config = ConfigDict(extra="ignore")

    type: str
    key: SecretStr | None = None


class _PiAuthFile(RootModel[dict[str, _PiAuthEntry]]):
    """pi's ``auth.json``: each provider's credential, keyed by pi's provider id."""


def accounts_root() -> Path:
    return Path(minds_accounts_root())


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


def _provider_name(row: _IndexRow) -> str:
    return row.display or row.lane


def _numbered_labels(rows: Sequence[_IndexRow]) -> dict[str, str]:
    """Each account's label, numbered among the accounts on its lane that read the same: "Anthropic", "Anthropic 2"."""
    counts: dict[tuple[str, str], int] = {}
    labels: dict[str, str] = {}
    for row in rows:
        name = row.name or _provider_name(row)
        number = counts.get((name, row.lane), 0) + 1
        counts[(name, row.lane)] = number
        labels[row.id] = name if number == 1 else f"{name} {number}"
    return labels


def _choose_default_row(index: _AccountIndex) -> _IndexRow | None:
    """The pinned default, else the most recently used, else the oldest, among accounts of a known type."""
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
    names = ", ".join(dict.fromkeys(_provider_name(row) for row in rows))
    return f"None of this workspace's AI accounts can call a model from code ({names}); {SIGN_IN_ADVICE}"


def _named_row(index: _AccountIndex, account_id: str) -> _IndexRow:
    row = next((row for row in index.accounts if row.id == account_id), None)
    if row is None:
        raise NoUsableAccountError(f"No signed-in account has the id {account_id!r}.")
    if row.lane not in SUPPORTED_PROVIDER_BY_LANE:
        raise UnsupportedProviderError(row.lane, _provider_name(row))
    return row


def _account_from_row(root: Path, row: _IndexRow, label: str) -> AIAccount:
    """The account a supported row names; raises when its folder is gone."""
    account_dir = root / row.id
    if not account_dir.is_dir():
        raise NoUsableAccountError(
            f"The account {label} has no folder on disk; sign in to it again."
        )
    return AIAccount(
        account_id=row.id,
        provider=SUPPORTED_PROVIDER_BY_LANE[row.lane],
        account_dir=account_dir,
        label=label,
    )


def resolve_ai_account(account_id: str | None, root: Path) -> AIAccount:
    """The named account, or the workspace's default among the accounts of a known type."""
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
    return _account_from_row(root, row, _numbered_labels(index.accounts)[row.id])


def _listed_billing(
    root: Path, row: _IndexRow, label: str, *, anthropic_snapshot_path: str
) -> AIBilling | None:
    if row.lane not in SUPPORTED_PROVIDER_BY_LANE:
        return None
    try:
        account = _account_from_row(root, row, label)
        return read_ai_credential(
            account, anthropic_snapshot_path=anthropic_snapshot_path
        ).billing
    except NoUsableAccountError:
        return None


def list_ai_accounts(
    root: Path, *, anthropic_snapshot_path: str
) -> list[AIAccountListing]:
    index = _read_index(root)
    if index is None:
        return []
    default_row = _choose_default_row(index)
    labels = _numbered_labels(index.accounts)
    return [
        AIAccountListing(
            account_id=row.id,
            lane=row.lane,
            label=labels[row.id],
            is_supported=row.lane in SUPPORTED_PROVIDER_BY_LANE,
            is_default=default_row is not None and row.id == default_row.id,
            billing=_listed_billing(
                root,
                row,
                labels[row.id],
                anthropic_snapshot_path=anthropic_snapshot_path,
            ),
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
            pi_provider_id=None,
        )
    is_console = _is_console_sign_in(
        account.account_dir, has_oauth_token=credentials.oauth_token is not None
    )
    return AICredential(
        kind=AICredentialKind.ANTHROPIC_SIGN_IN,
        billing=AIBilling.API_KEY if is_console else AIBilling.SUBSCRIPTION,
        api_key=None,
        api_base=None,
        pi_provider_id=None,
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
    except ValidationError:
        # The validation error quotes the file, key included.
        raise NoUsableAccountError(
            f"The OpenAI account {account.label}'s sign-in file is not valid; sign in to it again."
        ) from None
    api_key = (
        auth.openai_api_key
        if auth.openai_api_key is not None and len(auth.openai_api_key) > 0
        else None
    )
    auth_mode = auth.auth_mode or (
        _CODEX_API_KEY_AUTH_MODE if api_key is not None else _CODEX_SIGN_IN_AUTH_MODE
    )
    if auth_mode == _CODEX_SIGN_IN_AUTH_MODE:
        return AICredential(
            kind=AICredentialKind.OPENAI_SIGN_IN,
            billing=AIBilling.SUBSCRIPTION,
            api_key=None,
            api_base=None,
            pi_provider_id=None,
        )
    if auth_mode != _CODEX_API_KEY_AUTH_MODE:
        raise NoUsableAccountError(
            f"The OpenAI account {account.label} signs in with codex's {auth_mode!r} mode, "
            "which code cannot call yet; sign in to it with ChatGPT or an API key."
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
        pi_provider_id=None,
    )


def _read_google_credential(account: AIAccount) -> AICredential:
    if not (account.account_dir / _AGY_OAUTH_TOKEN_RELATIVE_PATH).is_file():
        raise NoUsableAccountError(
            f"The Google account {account.label} is signed out; sign in to it again."
        )
    return AICredential(
        kind=AICredentialKind.GOOGLE_SIGN_IN,
        billing=AIBilling.SUBSCRIPTION,
        api_key=None,
        api_base=None,
        pi_provider_id=None,
    )


def _read_pi_credential(account: AIAccount, *, billing: AIBilling) -> AICredential:
    """The one provider whose key the account's pi ``auth.json`` holds; the key itself stays in the file."""
    auth_path = account.account_dir / _PI_AUTH_FILENAME
    try:
        text = auth_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise NoUsableAccountError(
            f"The account {account.label} is signed out; sign in to it again."
        ) from None
    except OSError as e:
        raise NoUsableAccountError(
            f"The account {account.label}'s sign-in cannot be read: {e}"
        ) from e
    try:
        auth = _PiAuthFile.model_validate_json(text)
    except ValidationError:
        # The validation error quotes the file, key included.
        raise NoUsableAccountError(
            f"The account {account.label}'s key file is not valid; sign in to it again."
        ) from None
    keys_by_provider_id = {
        provider_id: entry.key
        for provider_id, entry in auth.root.items()
        if entry.type == _PI_API_KEY_ENTRY_TYPE
        and entry.key is not None
        and len(entry.key) > 0
    }
    if not keys_by_provider_id:
        raise NoUsableAccountError(
            f"The account {account.label} has no API key; sign in to it again."
        )
    if len(keys_by_provider_id) > 1:
        raise NoUsableAccountError(
            f"The account {account.label} holds keys for several providers "
            f"({', '.join(keys_by_provider_id)}); sign in to it again."
        )
    provider_id, key = next(iter(keys_by_provider_id.items()))
    # pi sends such a token as a Claude plan sign-in, not as a key billed per token
    if (
        provider_id == _ANTHROPIC_PI_PROVIDER_ID
        and _CLAUDE_SUBSCRIPTION_TOKEN_MARKER in key.get_secret_value()
    ):
        raise NoUsableAccountError(
            f"The account {account.label} holds a Claude subscription token, not an API key; "
            'add it under Anthropic\'s "Get a long-lived token" option.'
        )
    return AICredential(
        kind=AICredentialKind.PI_KEY,
        billing=billing,
        api_key=None,
        api_base=None,
        pi_provider_id=provider_id,
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
        case AIProvider.GOOGLE:
            return _read_google_credential(account)
        case AIProvider.OPENCODE_GO:
            return _read_pi_credential(account, billing=AIBilling.SUBSCRIPTION)
        case AIProvider.OPENROUTER | AIProvider.API_KEY:
            return _read_pi_credential(account, billing=AIBilling.API_KEY)
        case _ as unreachable:
            assert_never(unreachable)
