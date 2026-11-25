import json
from collections.abc import Callable
from pathlib import Path

import pydantic
import pytest

from use_ai_integration_skill.ai import (
    DEFAULT_OPENAI_API_MODEL,
    complete,
    complete_on_account,
    run_task,
    run_task_on_account,
)
from use_ai_integration_skill.ai_accounts import read_ai_credential, resolve_ai_account
from use_ai_integration_skill.claude_p import Usage
from use_ai_integration_skill.data_types import (
    AIAccount,
    AIBilling,
    AICredential,
    AIProvider,
    AIResult,
    LiteLLMReply,
    LiteLLMRequest,
)
from use_ai_integration_skill.errors import ProviderCallError, ProviderTimeoutError
from use_ai_integration_skill.testing import write_ai_accounts, write_stub_executable

# Stands in for claude: reports what it was run with as the result text of a success payload.
_STUB_CLAUDE = """
import json, os, sys
observed = {
    "argv": sys.argv[1:],
    "config_dir": os.environ.get("CLAUDE_CONFIG_DIR"),
    "api_key": os.environ.get("ANTHROPIC_API_KEY"),
    "base_url": os.environ.get("ANTHROPIC_BASE_URL"),
    "cwd": os.getcwd(),
}
print(json.dumps({
    "subtype": "success",
    "is_error": False,
    "result": json.dumps(observed),
    "total_cost_usd": 0.42,
    "usage": {"input_tokens": 5, "output_tokens": 3},
}))
"""

# Stands in for codex exec --json: reports what it was run with as its agent message.
_STUB_CODEX = """
import json, os, sys
observed = {
    "argv": sys.argv[1:],
    "codex_home": os.environ.get("CODEX_HOME"),
    "has_openai_api_key": "OPENAI_API_KEY" in os.environ,
    "has_codex_api_key": "CODEX_API_KEY" in os.environ,
    "cwd": os.getcwd(),
    "stdin": sys.stdin.read(),
}
for event in (
    {"type": "thread.started", "thread_id": "t1"},
    {"type": "turn.started"},
    {"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": json.dumps(observed)}},
    {"type": "turn.completed", "usage": {"input_tokens": 40, "cached_input_tokens": 10, "output_tokens": 6}},
):
    print(json.dumps(event))
"""

_FAILING_STUB_CODEX = """
import sys
print('{"type":"thread.started","thread_id":"t1"}')
print('{"type":"turn.started"}')
print('{"type":"turn.failed","error":{"message":"unexpected status 401 Unauthorized"}}')
sys.exit(1)
"""

_HANGING_STUB = """
import signal
signal.pause()
"""

_CHATGPT_AUTH = {
    "auth_mode": "chatgpt",
    "OPENAI_API_KEY": None,
    "tokens": {"access_token": "token"},
}


def _unexpected_litellm_call(request: LiteLLMRequest) -> LiteLLMReply:
    raise AssertionError(f"litellm was called for {request.model}")


def _recording_litellm(
    requests: list[LiteLLMRequest],
) -> Callable[[LiteLLMRequest], LiteLLMReply]:
    def caller(request: LiteLLMRequest) -> LiteLLMReply:
        requests.append(request)
        return LiteLLMReply(
            text="billing",
            usage=Usage(input_tokens=9, output_tokens=2),
            cost_usd=0.003,
            raw={},
        )

    return caller


def _anthropic_account(tmp_path: Path) -> AIAccount:
    root = write_ai_accounts(tmp_path / "accounts", [("a1", "anthropic", "Anthropic")])
    return resolve_ai_account("a1", root)


def _openai_sign_in_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An accounts root holding one ChatGPT sign-in, ``o1``, that the public functions read."""
    root = write_ai_accounts(tmp_path / "accounts", [("o1", "openai", "OpenAI")])
    (root / "o1" / "auth.json").write_text(json.dumps(_CHATGPT_AUTH))
    monkeypatch.setenv("MINDS_ACCOUNTS_ROOT", str(root))
    return root


def _complete_on(
    account: AIAccount,
    credential: AICredential,
    *,
    model: str | None = None,
    timeout_seconds: float = 8.0,
    litellm_caller: Callable[[LiteLLMRequest], LiteLLMReply] = _unexpected_litellm_call,
) -> AIResult:
    return complete_on_account(
        "Classify: invoice overdue",
        system="You are a classifier.",
        model=model,
        account=account,
        credential=credential,
        timeout_seconds=timeout_seconds,
        litellm_caller=litellm_caller,
    )


def test_complete_on_an_anthropic_key_calls_litellm_with_the_key_and_proxy(
    tmp_path: Path, anthropic_snapshot_path: str
) -> None:
    account = _anthropic_account(tmp_path)
    (account.account_dir / "settings.json").write_text(
        json.dumps(
            {
                "env": {
                    "ANTHROPIC_API_KEY": "sk-ant-key",
                    "ANTHROPIC_BASE_URL": "https://proxy/",
                }
            }
        )
    )
    credential = read_ai_credential(
        account, anthropic_snapshot_path=anthropic_snapshot_path
    )
    requests: list[LiteLLMRequest] = []

    result = _complete_on(
        account,
        credential,
        timeout_seconds=12.5,
        litellm_caller=_recording_litellm(requests),
    )

    [request] = requests
    assert request.model == "anthropic/claude-haiku-4-5"
    assert request.api_key.get_secret_value() == "sk-ant-key"
    assert request.api_base == "https://proxy"
    assert (request.system, request.prompt) == (
        "You are a classifier.",
        "Classify: invoice overdue",
    )
    assert request.timeout_seconds == 12.5
    assert result.provider is AIProvider.ANTHROPIC
    assert result.model == "claude-haiku-4-5"
    assert result.billing is AIBilling.API_KEY
    assert result.cost_usd == 0.003
    assert result.text == "billing"


def test_complete_on_an_openai_key_uses_the_default_openai_model(
    tmp_path: Path,
) -> None:
    root = write_ai_accounts(tmp_path / "accounts", [("o1", "openai", "OpenAI")])
    (root / "o1" / "auth.json").write_text(
        json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "sk-openai"})
    )
    account = resolve_ai_account("o1", root)
    credential = read_ai_credential(
        account, anthropic_snapshot_path=str(tmp_path / "unused.env")
    )
    requests: list[LiteLLMRequest] = []

    result = _complete_on(
        account, credential, litellm_caller=_recording_litellm(requests)
    )

    [request] = requests
    assert request.model == f"openai/{DEFAULT_OPENAI_API_MODEL}"
    assert request.api_key.get_secret_value() == "sk-openai"
    assert request.api_base is None
    assert result.provider is AIProvider.OPENAI
    assert result.model == DEFAULT_OPENAI_API_MODEL
    assert result.billing is AIBilling.API_KEY


def test_complete_passes_the_callers_model_through(
    tmp_path: Path, anthropic_snapshot_path: str
) -> None:
    account = _anthropic_account(tmp_path)
    (account.account_dir / "settings.json").write_text(
        json.dumps({"env": {"ANTHROPIC_API_KEY": "sk-ant-key"}})
    )
    credential = read_ai_credential(
        account, anthropic_snapshot_path=anthropic_snapshot_path
    )
    requests: list[LiteLLMRequest] = []

    result = _complete_on(
        account,
        credential,
        model="claude-sonnet-4-5",
        litellm_caller=_recording_litellm(requests),
    )

    assert requests[0].model == "anthropic/claude-sonnet-4-5"
    assert result.model == "claude-sonnet-4-5"


def test_complete_on_an_anthropic_sign_in_runs_claude_on_that_account(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    anthropic_snapshot_path: str,
    stub_bin_dir: Path,
) -> None:
    write_stub_executable(stub_bin_dir, "claude", _STUB_CLAUDE)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "the-chats-own-account"))
    account = _anthropic_account(tmp_path)
    credential = read_ai_credential(
        account, anthropic_snapshot_path=anthropic_snapshot_path
    )

    result = _complete_on(account, credential)

    observed = json.loads(result.text)
    assert observed["config_dir"] == str(account.account_dir)
    argv = observed["argv"]
    assert argv[argv.index("--model") + 1] == "claude-haiku-4-5"
    assert argv[argv.index("--tools") + 1] == ""
    assert result.billing is AIBilling.SUBSCRIPTION
    assert result.cost_usd is None
    assert result.usage is not None and result.usage.input_tokens == 5
    assert result.raw["total_cost_usd"] == 0.42


def test_complete_on_a_console_sign_in_reports_claudes_cost(
    tmp_path: Path, anthropic_snapshot_path: str, stub_bin_dir: Path
) -> None:
    write_stub_executable(stub_bin_dir, "claude", _STUB_CLAUDE)
    account = _anthropic_account(tmp_path)
    (account.account_dir / ".claude.json").write_text(
        json.dumps({"primaryApiKey": "sk-ant-console"})
    )
    credential = read_ai_credential(
        account, anthropic_snapshot_path=anthropic_snapshot_path
    )

    result = _complete_on(account, credential)

    assert result.billing is AIBilling.API_KEY
    assert result.cost_usd == 0.42


def test_run_task_on_an_anthropic_key_gives_claude_the_key_in_the_given_directory(
    tmp_path: Path, anthropic_snapshot_path: str, stub_bin_dir: Path
) -> None:
    write_stub_executable(stub_bin_dir, "claude", _STUB_CLAUDE)
    account = _anthropic_account(tmp_path)
    (account.account_dir / "settings.json").write_text(
        json.dumps(
            {
                "env": {
                    "ANTHROPIC_API_KEY": "sk-ant-key",
                    "ANTHROPIC_BASE_URL": "https://proxy/",
                }
            }
        )
    )
    credential = read_ai_credential(
        account, anthropic_snapshot_path=anthropic_snapshot_path
    )
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    result = run_task_on_account(
        "Tidy the notes.",
        instructions="Only touch notes/.",
        model=None,
        account=account,
        credential=credential,
        working_dir=work_dir,
        timeout_seconds=8.0,
    )

    observed = json.loads(result.text)
    assert observed["api_key"] == "sk-ant-key"
    assert observed["base_url"] == "https://proxy"
    assert Path(observed["cwd"]).resolve() == work_dir.resolve()
    argv = observed["argv"]
    assert argv[argv.index("--append-system-prompt") + 1] == "Only touch notes/."
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"
    assert result.billing is AIBilling.API_KEY
    assert result.cost_usd == 0.42


def test_claude_that_outlives_its_timeout_raises_provider_timeout(
    tmp_path: Path, anthropic_snapshot_path: str, stub_bin_dir: Path
) -> None:
    write_stub_executable(stub_bin_dir, "claude", _HANGING_STUB)
    account = _anthropic_account(tmp_path)
    credential = read_ai_credential(
        account, anthropic_snapshot_path=anthropic_snapshot_path
    )

    with pytest.raises(ProviderTimeoutError, match="did not finish within 1s"):
        _complete_on(account, credential, timeout_seconds=1.0)


def test_complete_runs_codex_on_the_default_openai_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_bin_dir: Path
) -> None:
    write_stub_executable(stub_bin_dir, "codex", _STUB_CODEX)
    root = _openai_sign_in_root(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-ambient")
    monkeypatch.setenv("CODEX_API_KEY", "sk-ambient-codex")

    result = complete("hello", system="Answer briefly.", timeout_seconds=8.0)

    observed = json.loads(result.text)
    assert observed["codex_home"] == str(root / "o1")
    assert observed["has_openai_api_key"] is False
    assert observed["has_codex_api_key"] is False
    assert observed["stdin"] == "Answer briefly.\n\nhello"
    argv = observed["argv"]
    assert argv[-2:] == ["--json", "-"]
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert Path(observed["cwd"]).resolve() != Path.cwd().resolve()
    assert result.provider is AIProvider.OPENAI
    assert result.account_id == "o1"
    assert result.billing is AIBilling.SUBSCRIPTION
    assert result.cost_usd is None
    assert result.model is None
    assert result.usage == Usage(
        input_tokens=30, output_tokens=6, cache_read_tokens=10, cache_write_tokens=0
    )


def test_run_task_runs_codex_in_the_given_directory_with_instructions_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_bin_dir: Path
) -> None:
    write_stub_executable(stub_bin_dir, "codex", _STUB_CODEX)
    _openai_sign_in_root(tmp_path, monkeypatch)
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    result = run_task(
        "Tidy the notes.",
        instructions="Only touch notes/.",
        cwd=work_dir,
        timeout_seconds=8.0,
    )

    observed = json.loads(result.text)
    assert Path(observed["cwd"]).resolve() == work_dir.resolve()
    assert observed["stdin"] == "Only touch notes/.\n\nTidy the notes."
    argv = observed["argv"]
    assert argv[argv.index("--sandbox") + 1] == "danger-full-access"


def test_codex_that_fails_raises_provider_call_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_bin_dir: Path
) -> None:
    write_stub_executable(stub_bin_dir, "codex", _FAILING_STUB_CODEX)
    _openai_sign_in_root(tmp_path, monkeypatch)

    with pytest.raises(
        ProviderCallError,
        match="exited 1: codex exec turn failed: unexpected status 401 Unauthorized",
    ):
        complete("hello", system="Answer briefly.", timeout_seconds=8.0)


def test_codex_that_outlives_its_timeout_raises_provider_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_bin_dir: Path
) -> None:
    write_stub_executable(stub_bin_dir, "codex", _HANGING_STUB)
    _openai_sign_in_root(tmp_path, monkeypatch)

    with pytest.raises(ProviderTimeoutError, match="did not finish within 1s"):
        complete("hello", system="Answer briefly.", timeout_seconds=1.0)


def test_a_missing_cli_raises_provider_call_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _openai_sign_in_root(tmp_path, monkeypatch)
    empty_bin_dir = tmp_path / "empty-bin"
    empty_bin_dir.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin_dir))

    with pytest.raises(ProviderCallError, match="could not start codex"):
        complete("hello", system="Answer briefly.", timeout_seconds=8.0)


def test_ai_result_rejects_a_cost_on_subscription_billing() -> None:
    with pytest.raises(pydantic.ValidationError, match="no dollar cost"):
        AIResult(
            text="billing",
            provider=AIProvider.ANTHROPIC,
            account_id="a1",
            model="claude-haiku-4-5",
            usage=None,
            billing=AIBilling.SUBSCRIPTION,
            cost_usd=0.1,
            raw={},
        )
