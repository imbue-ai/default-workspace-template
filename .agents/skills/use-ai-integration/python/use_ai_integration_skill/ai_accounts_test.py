import json
import shutil
from pathlib import Path

import pytest

from use_ai_integration_skill.ai_accounts import (
    accounts_root,
    list_ai_accounts,
    read_ai_credential,
    resolve_ai_account,
)
from use_ai_integration_skill.data_types import (
    AIAccount,
    AIBilling,
    AICredentialKind,
    AIProvider,
)
from use_ai_integration_skill.errors import (
    NoUsableAccountError,
    UnsupportedProviderError,
)
from use_ai_integration_skill.testing import write_ai_accounts

_ANTHROPIC_ROW = ("a1", "anthropic", "Anthropic")
_OPENAI_ROW = ("o1", "openai", "OpenAI")
_GOOGLE_ROW = ("g1", "google", "Google")
_OPENROUTER_ROW = ("r1", "openrouter", "OpenRouter")
_OPENCODE_GO_ROW = ("c1", "opencode-go", "Opencode Go")


def _account(tmp_path: Path, provider: AIProvider) -> AIAccount:
    account_dir = tmp_path / "accounts" / "acct"
    account_dir.mkdir(parents=True)
    return AIAccount(
        account_id="acct", provider=provider, account_dir=account_dir, label="Mine"
    )


def test_accounts_root_honours_the_override_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MINDS_ACCOUNTS_ROOT", str(tmp_path / "elsewhere"))

    assert accounts_root() == tmp_path / "elsewhere"


def test_accounts_root_defaults_to_minds_accounts_under_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MINDS_ACCOUNTS_ROOT", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    assert accounts_root() == tmp_path / ".minds" / "accounts"


def test_default_account_is_the_pinned_supported_account(tmp_path: Path) -> None:
    root = write_ai_accounts(
        tmp_path, [_ANTHROPIC_ROW, _OPENAI_ROW], mru="a1", default_account="o1"
    )

    account = resolve_ai_account(None, root)

    assert account.account_id == "o1"
    assert account.provider is AIProvider.OPENAI
    assert account.account_dir == root / "o1"


def test_default_account_falls_back_to_the_most_recently_used(tmp_path: Path) -> None:
    root = write_ai_accounts(tmp_path, [_ANTHROPIC_ROW, _OPENAI_ROW], mru="o1")

    assert resolve_ai_account(None, root).account_id == "o1"


def test_default_account_falls_back_to_the_oldest_supported_account(
    tmp_path: Path,
) -> None:
    root = write_ai_accounts(tmp_path, [_GOOGLE_ROW, _ANTHROPIC_ROW, _OPENAI_ROW])

    assert resolve_ai_account(None, root).account_id == "a1"


@pytest.mark.parametrize(
    ("default_account", "mru", "expected"),
    [
        pytest.param("g1", "o1", "o1", id="pinned-google-account"),
        pytest.param(None, "r1", "a1", id="openrouter-most-recently-used"),
    ],
)
def test_default_account_skips_unsupported_lanes(
    tmp_path: Path, default_account: str | None, mru: str, expected: str
) -> None:
    root = write_ai_accounts(
        tmp_path,
        [_GOOGLE_ROW, _ANTHROPIC_ROW, _OPENAI_ROW, _OPENROUTER_ROW],
        mru=mru,
        default_account=default_account,
    )

    assert resolve_ai_account(None, root).account_id == expected


def test_named_account_is_used_over_the_pinned_one(tmp_path: Path) -> None:
    root = write_ai_accounts(
        tmp_path, [_ANTHROPIC_ROW, _OPENAI_ROW], default_account="o1"
    )

    account = resolve_ai_account("a1", root)

    assert account.account_id == "a1"
    assert account.provider is AIProvider.ANTHROPIC


def test_unknown_account_id_raises_no_usable_account(tmp_path: Path) -> None:
    root = write_ai_accounts(tmp_path, [_ANTHROPIC_ROW])

    with pytest.raises(NoUsableAccountError, match="acct-gone"):
        resolve_ai_account("acct-gone", root)


def test_named_google_account_raises_unsupported_provider_with_googles_terms(
    tmp_path: Path,
) -> None:
    root = write_ai_accounts(tmp_path, [_ANTHROPIC_ROW, _GOOGLE_ROW])

    with pytest.raises(UnsupportedProviderError) as raised:
        resolve_ai_account("g1", root)

    message = str(raised.value)
    assert raised.value.lane == "google"
    for phrase in ("Google", "never", "apps", "Gemini API key"):
        assert phrase in message


def test_named_openrouter_account_raises_unsupported_provider_naming_it(
    tmp_path: Path,
) -> None:
    root = write_ai_accounts(tmp_path, [_ANTHROPIC_ROW, _OPENROUTER_ROW])

    with pytest.raises(UnsupportedProviderError, match="OpenRouter.*not supported yet"):
        resolve_ai_account("r1", root)


def test_missing_index_raises_no_usable_account(tmp_path: Path) -> None:
    with pytest.raises(NoUsableAccountError, match="No AI account is signed in"):
        resolve_ai_account(None, tmp_path)


def test_empty_index_raises_no_usable_account(tmp_path: Path) -> None:
    (tmp_path / "index.json").write_text(json.dumps({"accounts": []}))

    with pytest.raises(NoUsableAccountError, match="No AI account is signed in"):
        resolve_ai_account(None, tmp_path)


def test_unreadable_index_raises_no_usable_account(tmp_path: Path) -> None:
    (tmp_path / "index.json").write_text("{not json")

    with pytest.raises(NoUsableAccountError, match="not readable"):
        resolve_ai_account(None, tmp_path)


def test_only_unsupported_accounts_raise_no_usable_account_naming_them(
    tmp_path: Path,
) -> None:
    root = write_ai_accounts(tmp_path, [_GOOGLE_ROW, _OPENCODE_GO_ROW])

    with pytest.raises(NoUsableAccountError) as raised:
        resolve_ai_account(None, root)

    message = str(raised.value)
    for phrase in ("Google", "Opencode Go", "Gemini API key"):
        assert phrase in message


def test_account_without_a_folder_raises_no_usable_account(tmp_path: Path) -> None:
    root = write_ai_accounts(tmp_path, [_ANTHROPIC_ROW])
    shutil.rmtree(root / "a1")

    with pytest.raises(NoUsableAccountError, match="sign in to it again"):
        resolve_ai_account(None, root)


def test_list_ai_accounts_marks_support_and_the_default(tmp_path: Path) -> None:
    root = write_ai_accounts(
        tmp_path, [_GOOGLE_ROW, _ANTHROPIC_ROW, _OPENAI_ROW], mru="o1"
    )

    listing = list_ai_accounts(root)

    assert [(row.account_id, row.is_supported) for row in listing] == [
        ("g1", False),
        ("a1", True),
        ("o1", True),
    ]
    assert [row.account_id for row in listing if row.is_default] == [
        resolve_ai_account(None, root).account_id
    ]


def test_list_ai_accounts_without_an_index_is_empty(tmp_path: Path) -> None:
    assert list_ai_accounts(tmp_path) == []


def test_anthropic_key_in_the_account_settings_is_an_api_key_credential(
    tmp_path: Path, anthropic_snapshot_path: str
) -> None:
    account = _account(tmp_path, AIProvider.ANTHROPIC)
    (account.account_dir / "settings.json").write_text(
        json.dumps(
            {
                "env": {
                    "ANTHROPIC_API_KEY": "sk-ant-settings",
                    "ANTHROPIC_BASE_URL": "https://proxy/",
                }
            }
        )
    )

    credential = read_ai_credential(
        account, anthropic_snapshot_path=anthropic_snapshot_path
    )

    assert credential.kind is AICredentialKind.ANTHROPIC_API_KEY
    assert credential.billing is AIBilling.API_KEY
    assert (
        credential.api_key is not None
        and credential.api_key.get_secret_value() == "sk-ant-settings"
    )
    assert credential.api_base == "https://proxy"


def test_anthropic_snapshot_key_wins_over_a_signed_in_account(
    tmp_path: Path, anthropic_snapshot_path: str
) -> None:
    account = _account(tmp_path, AIProvider.ANTHROPIC)
    (account.account_dir / "settings.json").write_text(
        json.dumps({"env": {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-token"}})
    )
    snapshot = Path(anthropic_snapshot_path)
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text("ANTHROPIC_API_KEY=sk-ant-pinned\n")

    credential = read_ai_credential(
        account, anthropic_snapshot_path=anthropic_snapshot_path
    )

    assert credential.kind is AICredentialKind.ANTHROPIC_API_KEY
    assert (
        credential.api_key is not None
        and credential.api_key.get_secret_value() == "sk-ant-pinned"
    )
    assert credential.api_base is None


def test_anthropic_long_lived_token_is_a_subscription_sign_in(
    tmp_path: Path, anthropic_snapshot_path: str
) -> None:
    account = _account(tmp_path, AIProvider.ANTHROPIC)
    (account.account_dir / "settings.json").write_text(
        json.dumps({"env": {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-token"}})
    )
    (account.account_dir / ".claude.json").write_text(
        json.dumps({"primaryApiKey": "sk-ant-stale"})
    )

    credential = read_ai_credential(
        account, anthropic_snapshot_path=anthropic_snapshot_path
    )

    assert credential.kind is AICredentialKind.ANTHROPIC_SIGN_IN
    assert credential.billing is AIBilling.SUBSCRIPTION
    assert credential.api_key is None


def test_anthropic_browser_sign_in_is_a_subscription_sign_in(
    tmp_path: Path, anthropic_snapshot_path: str
) -> None:
    account = _account(tmp_path, AIProvider.ANTHROPIC)

    credential = read_ai_credential(
        account, anthropic_snapshot_path=anthropic_snapshot_path
    )

    assert credential.kind is AICredentialKind.ANTHROPIC_SIGN_IN
    assert credential.billing is AIBilling.SUBSCRIPTION


def test_anthropic_console_sign_in_bills_per_token(
    tmp_path: Path, anthropic_snapshot_path: str
) -> None:
    account = _account(tmp_path, AIProvider.ANTHROPIC)
    (account.account_dir / ".claude.json").write_text(
        json.dumps({"primaryApiKey": "sk-ant-console"})
    )

    credential = read_ai_credential(
        account, anthropic_snapshot_path=anthropic_snapshot_path
    )

    assert credential.kind is AICredentialKind.ANTHROPIC_SIGN_IN
    assert credential.billing is AIBilling.API_KEY
    assert credential.api_key is None


def test_anthropic_credential_ignores_claude_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, anthropic_snapshot_path: str
) -> None:
    chat_account_dir = tmp_path / "chats-own-account"
    chat_account_dir.mkdir()
    (chat_account_dir / "settings.json").write_text(
        json.dumps({"env": {"ANTHROPIC_API_KEY": "sk-ant-chat"}})
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(chat_account_dir))
    account = _account(tmp_path, AIProvider.ANTHROPIC)

    credential = read_ai_credential(
        account, anthropic_snapshot_path=anthropic_snapshot_path
    )

    assert credential.kind is AICredentialKind.ANTHROPIC_SIGN_IN


def test_openai_pasted_key_is_an_api_key_credential(tmp_path: Path) -> None:
    account = _account(tmp_path, AIProvider.OPENAI)
    (account.account_dir / "auth.json").write_text(
        json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "sk-openai"})
    )

    credential = read_ai_credential(
        account, anthropic_snapshot_path=str(tmp_path / "unused.env")
    )

    assert credential.kind is AICredentialKind.OPENAI_API_KEY
    assert credential.billing is AIBilling.API_KEY
    assert (
        credential.api_key is not None
        and credential.api_key.get_secret_value() == "sk-openai"
    )


def test_openai_key_without_a_mode_is_an_api_key_credential(tmp_path: Path) -> None:
    account = _account(tmp_path, AIProvider.OPENAI)
    (account.account_dir / "auth.json").write_text(
        json.dumps({"OPENAI_API_KEY": "sk-openai"})
    )

    credential = read_ai_credential(
        account, anthropic_snapshot_path=str(tmp_path / "unused.env")
    )

    assert credential.kind is AICredentialKind.OPENAI_API_KEY


def test_openai_chatgpt_sign_in_is_a_subscription_sign_in(tmp_path: Path) -> None:
    account = _account(tmp_path, AIProvider.OPENAI)
    (account.account_dir / "auth.json").write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "OPENAI_API_KEY": None,
                "tokens": {"access_token": "x"},
            }
        )
    )

    credential = read_ai_credential(
        account, anthropic_snapshot_path=str(tmp_path / "unused.env")
    )

    assert credential.kind is AICredentialKind.OPENAI_SIGN_IN
    assert credential.billing is AIBilling.SUBSCRIPTION
    assert credential.api_key is None


def test_openai_api_key_mode_without_a_key_raises_no_usable_account(
    tmp_path: Path,
) -> None:
    account = _account(tmp_path, AIProvider.OPENAI)
    (account.account_dir / "auth.json").write_text(
        json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": ""})
    )

    with pytest.raises(NoUsableAccountError, match="has no API key"):
        read_ai_credential(
            account, anthropic_snapshot_path=str(tmp_path / "unused.env")
        )


def test_openai_account_without_auth_json_raises_no_usable_account(
    tmp_path: Path,
) -> None:
    account = _account(tmp_path, AIProvider.OPENAI)

    with pytest.raises(NoUsableAccountError, match="signed out"):
        read_ai_credential(
            account, anthropic_snapshot_path=str(tmp_path / "unused.env")
        )
