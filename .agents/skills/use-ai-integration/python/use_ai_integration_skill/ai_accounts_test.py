import json
import os
import pwd
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
from use_ai_integration_skill.testing import (
    write_agy_sign_in,
    write_ai_accounts,
    write_pi_key,
)

_ANTHROPIC_ROW = ("a1", "anthropic", "Anthropic")
_OPENAI_ROW = ("o1", "openai", "OpenAI")
_GOOGLE_ROW = ("g1", "google", "Google")
_OPENROUTER_ROW = ("r1", "openrouter", "OpenRouter")
_OPENCODE_GO_ROW = ("c1", "opencode-go", "Opencode Go")
_ANTHROPIC_KEY_OPTION_ROW = ("k1", "api-key", "Anthropic")
_KEY_OPTION_ROW = ("k2", "api-key", "Groq")
_UNKNOWN_ROW = ("x1", "future-lane", "Future")


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


def test_accounts_root_is_in_the_passwd_home_whatever_home_says(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MINDS_ACCOUNTS_ROOT", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    root = accounts_root()

    assert root == Path(pwd.getpwuid(os.getuid()).pw_dir) / ".minds" / "accounts"
    assert not root.is_relative_to(tmp_path)


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


def test_default_account_falls_back_to_the_oldest_account(tmp_path: Path) -> None:
    root = write_ai_accounts(tmp_path, [_GOOGLE_ROW, _ANTHROPIC_ROW])

    assert resolve_ai_account(None, root).account_id == "g1"


@pytest.mark.parametrize(
    ("default_account", "mru", "expected"),
    [
        pytest.param("g1", "a1", "g1", id="pinned-google-account"),
        pytest.param(None, "r1", "r1", id="openrouter-most-recently-used"),
        pytest.param("k2", "o1", "k2", id="pinned-key-from-the-api-key-option"),
        pytest.param(None, None, "c1", id="oldest-is-opencode-go"),
    ],
)
def test_default_account_considers_every_lane(
    tmp_path: Path, default_account: str | None, mru: str | None, expected: str
) -> None:
    root = write_ai_accounts(
        tmp_path,
        [
            _OPENCODE_GO_ROW,
            _ANTHROPIC_ROW,
            _OPENAI_ROW,
            _GOOGLE_ROW,
            _OPENROUTER_ROW,
            _KEY_OPTION_ROW,
        ],
        mru=mru,
        default_account=default_account,
    )

    assert resolve_ai_account(None, root).account_id == expected


def test_default_account_skips_an_unknown_lane(tmp_path: Path) -> None:
    root = write_ai_accounts(
        tmp_path, [_UNKNOWN_ROW, _ANTHROPIC_ROW], mru="a1", default_account="x1"
    )

    assert resolve_ai_account(None, root).account_id == "a1"


@pytest.mark.parametrize(
    "lane", ["anthropic", "openai", "google", "opencode-go", "openrouter", "api-key"]
)
def test_each_lane_resolves_to_its_account_type(lane: str, tmp_path: Path) -> None:
    root = write_ai_accounts(tmp_path, [("acct", lane, "Mine")])

    assert resolve_ai_account("acct", root).provider is AIProvider(lane)


def test_named_account_of_an_unknown_lane_raises_unsupported_provider_naming_it(
    tmp_path: Path,
) -> None:
    root = write_ai_accounts(tmp_path, [_ANTHROPIC_ROW, _UNKNOWN_ROW])

    with pytest.raises(
        UnsupportedProviderError, match="Future.*not supported yet"
    ) as raised:
        resolve_ai_account("x1", root)

    assert raised.value.lane == "future-lane"


def test_only_unknown_lane_accounts_raise_no_usable_account_naming_them(
    tmp_path: Path,
) -> None:
    root = write_ai_accounts(tmp_path, [_UNKNOWN_ROW, ("x2", "other-lane", "Other")])

    with pytest.raises(NoUsableAccountError, match=r"\(Future, Other\)"):
        resolve_ai_account(None, root)


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


def test_account_without_a_folder_raises_no_usable_account(tmp_path: Path) -> None:
    root = write_ai_accounts(tmp_path, [_ANTHROPIC_ROW])
    shutil.rmtree(root / "a1")

    with pytest.raises(NoUsableAccountError, match="sign in to it again"):
        resolve_ai_account(None, root)


def test_list_ai_accounts_marks_support_billing_and_the_default(
    tmp_path: Path, anthropic_snapshot_path: str
) -> None:
    root = write_ai_accounts(
        tmp_path / "accounts",
        [
            _GOOGLE_ROW,
            _ANTHROPIC_ROW,
            _OPENAI_ROW,
            _OPENCODE_GO_ROW,
            _KEY_OPTION_ROW,
            _UNKNOWN_ROW,
        ],
        mru="o1",
    )
    (root / "a1" / "settings.json").write_text(
        json.dumps({"env": {"ANTHROPIC_API_KEY": "sk-ant-settings"}})
    )
    write_pi_key(root / "c1", "opencode-go")
    write_pi_key(root / "k2", "groq")

    listing = list_ai_accounts(root, anthropic_snapshot_path=anthropic_snapshot_path)

    assert [(row.account_id, row.is_supported, row.billing) for row in listing] == [
        ("g1", True, None),
        ("a1", True, AIBilling.API_KEY),
        ("o1", True, None),
        ("c1", True, AIBilling.SUBSCRIPTION),
        ("k2", True, AIBilling.API_KEY),
        ("x1", False, None),
    ]
    assert [row.account_id for row in listing if row.is_default] == [
        resolve_ai_account(None, root).account_id
    ]


def test_list_ai_accounts_numbers_accounts_that_read_the_same(
    tmp_path: Path, anthropic_snapshot_path: str
) -> None:
    root = write_ai_accounts(
        tmp_path / "accounts",
        [
            _ANTHROPIC_ROW,
            _OPENAI_ROW,
            ("a2", "anthropic", "Anthropic"),
            _ANTHROPIC_KEY_OPTION_ROW,
        ],
    )

    listing = list_ai_accounts(root, anthropic_snapshot_path=anthropic_snapshot_path)

    assert [(row.account_id, row.label) for row in listing] == [
        ("a1", "Anthropic"),
        ("o1", "OpenAI"),
        ("a2", "Anthropic 2"),
        ("k1", "Anthropic"),
    ]
    assert resolve_ai_account("a2", root).label == "Anthropic 2"


def test_list_ai_accounts_without_an_index_is_empty(tmp_path: Path) -> None:
    assert (
        list_ai_accounts(tmp_path, anthropic_snapshot_path=str(tmp_path / "none.env"))
        == []
    )


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


def test_anthropic_credential_ignores_credentials_in_the_process_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, anthropic_snapshot_path: str
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-the-chats-own")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://the-chats-proxy/")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-the-chats-own")
    account = _account(tmp_path, AIProvider.ANTHROPIC)

    credential = read_ai_credential(
        account, anthropic_snapshot_path=anthropic_snapshot_path
    )

    assert credential.kind is AICredentialKind.ANTHROPIC_SIGN_IN
    assert credential.api_key is None


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


def test_openai_sign_in_of_another_codex_mode_raises_no_usable_account(
    tmp_path: Path,
) -> None:
    account = _account(tmp_path, AIProvider.OPENAI)
    (account.account_dir / "auth.json").write_text(
        json.dumps({"auth_mode": "bedrockApiKey", "OPENAI_API_KEY": None})
    )

    with pytest.raises(NoUsableAccountError, match="'bedrockApiKey' mode"):
        read_ai_credential(
            account, anthropic_snapshot_path=str(tmp_path / "unused.env")
        )


def test_an_unreadable_openai_key_file_raises_without_any_of_the_key(
    tmp_path: Path,
) -> None:
    secret_tail = "SECRETTAIL1234567890abcdef"
    account = _account(tmp_path, AIProvider.OPENAI)
    (account.account_dir / "auth.json").write_text(
        f'{{\n  "auth_mode": "apikey",\n  "OPENAI_API_KEY": "sk-proj-{secret_tail}'
    )

    with pytest.raises(NoUsableAccountError) as raised:
        read_ai_credential(
            account, anthropic_snapshot_path=str(tmp_path / "unused.env")
        )

    assert "not valid" in str(raised.value)
    assert "SECRET" not in str(raised.value)
    assert raised.value.__cause__ is None and raised.value.__suppress_context__


def test_openai_account_without_auth_json_raises_no_usable_account(
    tmp_path: Path,
) -> None:
    account = _account(tmp_path, AIProvider.OPENAI)

    with pytest.raises(NoUsableAccountError, match="signed out"):
        read_ai_credential(
            account, anthropic_snapshot_path=str(tmp_path / "unused.env")
        )


def test_google_sign_in_is_a_subscription_credential(tmp_path: Path) -> None:
    account = _account(tmp_path, AIProvider.GOOGLE)
    write_agy_sign_in(account.account_dir)

    credential = read_ai_credential(
        account, anthropic_snapshot_path=str(tmp_path / "unused.env")
    )

    assert credential.kind is AICredentialKind.GOOGLE_SIGN_IN
    assert credential.billing is AIBilling.SUBSCRIPTION
    assert credential.api_key is None
    assert credential.pi_provider_id is None


def test_google_account_without_its_token_is_signed_out(tmp_path: Path) -> None:
    account = _account(tmp_path, AIProvider.GOOGLE)

    with pytest.raises(NoUsableAccountError, match="Google account Mine is signed out"):
        read_ai_credential(
            account, anthropic_snapshot_path=str(tmp_path / "unused.env")
        )


@pytest.mark.parametrize(
    ("provider", "provider_id"),
    [
        (AIProvider.OPENROUTER, "openrouter"),
        (AIProvider.API_KEY, "groq"),
        (AIProvider.API_KEY, "anthropic"),
    ],
)
def test_pi_key_credential_names_its_provider_and_bills_per_token(
    provider: AIProvider, provider_id: str, tmp_path: Path
) -> None:
    account = _account(tmp_path, provider)
    write_pi_key(account.account_dir, provider_id)

    credential = read_ai_credential(
        account, anthropic_snapshot_path=str(tmp_path / "unused.env")
    )

    assert credential.kind is AICredentialKind.PI_KEY
    assert credential.pi_provider_id == provider_id
    assert credential.billing is AIBilling.API_KEY
    assert credential.api_key is None


def test_opencode_go_key_bills_as_a_subscription(tmp_path: Path) -> None:
    account = _account(tmp_path, AIProvider.OPENCODE_GO)
    write_pi_key(account.account_dir, "opencode-go")

    credential = read_ai_credential(
        account, anthropic_snapshot_path=str(tmp_path / "unused.env")
    )

    assert credential.billing is AIBilling.SUBSCRIPTION
    assert credential.pi_provider_id == "opencode-go"


def test_pi_account_without_auth_json_is_signed_out(tmp_path: Path) -> None:
    account = _account(tmp_path, AIProvider.OPENROUTER)

    with pytest.raises(NoUsableAccountError, match="Mine is signed out"):
        read_ai_credential(
            account, anthropic_snapshot_path=str(tmp_path / "unused.env")
        )


def test_pi_auth_json_without_a_key_raises_no_usable_account(tmp_path: Path) -> None:
    account = _account(tmp_path, AIProvider.API_KEY)
    (account.account_dir / "auth.json").write_text(
        json.dumps(
            {
                "groq": {"type": "api_key", "key": ""},
                "openrouter": {"type": "oauth", "refresh": "r"},
            }
        )
    )

    with pytest.raises(NoUsableAccountError, match="has no API key"):
        read_ai_credential(
            account, anthropic_snapshot_path=str(tmp_path / "unused.env")
        )


def test_pi_auth_json_with_several_keys_raises_no_usable_account_naming_them(
    tmp_path: Path,
) -> None:
    account = _account(tmp_path, AIProvider.API_KEY)
    (account.account_dir / "auth.json").write_text(
        json.dumps(
            {
                "groq": {"type": "api_key", "key": "gsk-one"},
                "xai": {"type": "api_key", "key": "xai-two"},
            }
        )
    )

    with pytest.raises(NoUsableAccountError, match=r"several providers \(groq, xai\)"):
        read_ai_credential(
            account, anthropic_snapshot_path=str(tmp_path / "unused.env")
        )


def test_an_unreadable_pi_key_file_raises_without_any_of_the_key(
    tmp_path: Path,
) -> None:
    secret_tail = "SECRETTAIL0987654321fedcba"
    account = _account(tmp_path, AIProvider.API_KEY)
    (account.account_dir / "auth.json").write_text(
        f'{{"groq": {{"type": "api_key", "key": "gsk_{secret_tail}'
    )

    with pytest.raises(NoUsableAccountError) as raised:
        read_ai_credential(
            account, anthropic_snapshot_path=str(tmp_path / "unused.env")
        )

    assert "not valid" in str(raised.value)
    assert "SECRET" not in str(raised.value)
    assert raised.value.__cause__ is None and raised.value.__suppress_context__
