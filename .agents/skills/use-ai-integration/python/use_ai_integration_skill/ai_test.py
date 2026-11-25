import json
import os
import select
import signal
import time
from collections.abc import Callable
from pathlib import Path

import pydantic
import pytest

from use_ai_integration_skill.ai import (
    DEFAULT_OPENAI_API_MODEL,
    complete,
    complete_on_account,
    pin_anthropic_key_of_account,
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
from use_ai_integration_skill.errors import (
    NoUsableAccountError,
    ProviderCallError,
    ProviderTimeoutError,
)
from use_ai_integration_skill.testing import write_ai_accounts, write_stub_executable

# Stands in for claude: reports what it was run with as the result text of a success payload.
_STUB_CLAUDE = """
import json, os, sys
argv = sys.argv[1:]
def flag_file(flag):
    if flag not in argv:
        return None
    with open(argv[argv.index(flag) + 1], encoding="utf-8") as f:
        return f.read()
stdin_text = sys.stdin.read()
observed = {
    "argv": argv,
    "config_dir": os.environ.get("CLAUDE_CONFIG_DIR"),
    "ambient_credentials": sorted(
        name
        for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_OAUTH_TOKEN")
        if name in os.environ
    ),
    "settings": json.loads(flag_file("--settings") or "null"),
    "system_prompt": flag_file("--system-prompt-file"),
    "append_system_prompt": flag_file("--append-system-prompt-file"),
    "stdin_length": len(stdin_text),
    "stdin_head": stdin_text[:100],
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

# Starts a grandchild that holds the fifo STUB_GRANDCHILD_FIFO names open, writes its pid there, then hangs.
_STUB_LEAVING_A_GRANDCHILD = """
import os, signal, subprocess
fd = os.open(os.environ["STUB_GRANDCHILD_FIFO"], os.O_WRONLY)
grandchild = subprocess.Popen(["sleep", "36284"], stdin=subprocess.DEVNULL, stdout=fd, stderr=subprocess.DEVNULL)
os.write(fd, f"{grandchild.pid}\\n".encode())
os.close(fd)
signal.pause()
"""

_STUB_PRINTING_NON_UTF8 = """
import sys
sys.stderr.buffer.write(b"\\xff\\xfe bad bytes")
sys.exit(1)
"""

_AMBIENT_CREDENTIALS = {
    "ANTHROPIC_API_KEY": "sk-ant-ambient",
    "ANTHROPIC_AUTH_TOKEN": "ambient-auth-token",
    "ANTHROPIC_BASE_URL": "https://ambient/",
    "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-ambient",
}

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
    for name, value in _AMBIENT_CREDENTIALS.items():
        monkeypatch.setenv(name, value)

    result = _complete_on(account, credential)

    observed = json.loads(result.text)
    assert observed["config_dir"] == str(account.account_dir)
    assert observed["ambient_credentials"] == []
    assert observed["system_prompt"] == "You are a classifier."
    assert observed["stdin_head"] == "Classify: invoice overdue"
    argv = observed["argv"]
    assert "--" not in argv and "Classify: invoice overdue" not in argv
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
    assert observed["settings"] == {
        "env": {
            "ANTHROPIC_API_KEY": "sk-ant-key",
            "ANTHROPIC_BASE_URL": "https://proxy",
        }
    }
    assert observed["ambient_credentials"] == []
    assert Path(observed["cwd"]).resolve() == work_dir.resolve()
    assert observed["append_system_prompt"] == "Only touch notes/."
    assert observed["stdin_head"] == "Tidy the notes."
    argv = observed["argv"]
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"
    assert result.billing is AIBilling.API_KEY
    assert result.cost_usd == 0.42


def test_run_task_on_a_pinned_key_overrides_the_accounts_own_proxy(
    tmp_path: Path, anthropic_snapshot_path: str, stub_bin_dir: Path
) -> None:
    write_stub_executable(stub_bin_dir, "claude", _STUB_CLAUDE)
    account = _anthropic_account(tmp_path)
    (account.account_dir / "settings.json").write_text(
        json.dumps(
            {
                "env": {
                    "ANTHROPIC_API_KEY": "sk-ant-rekeyed",
                    "ANTHROPIC_BASE_URL": "https://new-proxy/",
                }
            }
        )
    )
    snapshot = Path(anthropic_snapshot_path)
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text("ANTHROPIC_API_KEY=sk-ant-pinned\n")
    credential = read_ai_credential(
        account, anthropic_snapshot_path=anthropic_snapshot_path
    )

    result = run_task_on_account(
        "Tidy the notes.",
        instructions=None,
        model=None,
        account=account,
        credential=credential,
        working_dir=tmp_path,
        timeout_seconds=8.0,
    )

    assert json.loads(result.text)["settings"] == {
        "env": {
            "ANTHROPIC_API_KEY": "sk-ant-pinned",
            "ANTHROPIC_BASE_URL": "https://api.anthropic.com",
        }
    }


def test_a_prompt_longer_than_any_argv_allows_reaches_claude_on_stdin(
    tmp_path: Path, anthropic_snapshot_path: str, stub_bin_dir: Path
) -> None:
    write_stub_executable(stub_bin_dir, "claude", _STUB_CLAUDE)
    account = _anthropic_account(tmp_path)
    credential = read_ai_credential(
        account, anthropic_snapshot_path=anthropic_snapshot_path
    )
    long_text = "x" * 2_000_000

    result = complete_on_account(
        long_text,
        system=long_text,
        model=None,
        account=account,
        credential=credential,
        timeout_seconds=8.0,
        litellm_caller=_unexpected_litellm_call,
    )

    observed = json.loads(result.text)
    assert observed["stdin_length"] == len(long_text)
    assert observed["system_prompt"] == long_text


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


def _complete_through(
    cli: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    anthropic_snapshot_path: str,
    *,
    timeout_seconds: float,
) -> AIResult:
    """One completion on a sign-in whose provider runs ``cli``."""
    if cli == "codex":
        _openai_sign_in_root(tmp_path, monkeypatch)
        return complete(
            "hello", system="Answer briefly.", timeout_seconds=timeout_seconds
        )
    else:
        account = _anthropic_account(tmp_path)
        credential = read_ai_credential(
            account, anthropic_snapshot_path=anthropic_snapshot_path
        )
        return _complete_on(account, credential, timeout_seconds=timeout_seconds)


def _read_until_closed(fd: int, *, timeout_seconds: float) -> tuple[bytes, bool]:
    """What a fifo's writers wrote to it, and whether all of them closed it within the timeout."""
    deadline = time.monotonic() + timeout_seconds
    received = b""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
            return received, False
        chunk = os.read(fd, 4096)
        if not chunk:
            return received, True
        received += chunk


@pytest.mark.parametrize("cli", ["claude", "codex"])
def test_a_call_that_times_out_ends_every_process_its_cli_started(
    cli: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    anthropic_snapshot_path: str,
    stub_bin_dir: Path,
) -> None:
    write_stub_executable(stub_bin_dir, cli, _STUB_LEAVING_A_GRANDCHILD)
    fifo = tmp_path / "grandchild.fifo"
    os.mkfifo(fifo)
    monkeypatch.setenv("STUB_GRANDCHILD_FIFO", str(fifo))
    fifo_fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    try:
        with pytest.raises(ProviderTimeoutError, match="did not finish within 3s"):
            _complete_through(
                cli, tmp_path, monkeypatch, anthropic_snapshot_path, timeout_seconds=3.0
            )
        received, is_closed = _read_until_closed(fifo_fd, timeout_seconds=5.0)
    finally:
        os.close(fifo_fd)

    grandchild_pid = int(received)
    if not is_closed:
        os.kill(grandchild_pid, signal.SIGKILL)
    assert is_closed


@pytest.mark.parametrize("cli", ["claude", "codex"])
def test_a_cli_printing_bytes_that_are_not_utf8_raises_provider_call_error(
    cli: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    anthropic_snapshot_path: str,
    stub_bin_dir: Path,
) -> None:
    write_stub_executable(stub_bin_dir, cli, _STUB_PRINTING_NON_UTF8)

    with pytest.raises(ProviderCallError, match="exited 1: .*bad bytes"):
        _complete_through(
            cli, tmp_path, monkeypatch, anthropic_snapshot_path, timeout_seconds=8.0
        )


@pytest.mark.parametrize("cli", ["claude", "codex"])
def test_a_cli_that_cannot_be_run_raises_provider_call_error(
    cli: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    anthropic_snapshot_path: str,
    stub_bin_dir: Path,
) -> None:
    (stub_bin_dir / cli).write_text("not a program")
    monkeypatch.setenv("PATH", str(stub_bin_dir))

    with pytest.raises(ProviderCallError, match=f"could not start {cli}"):
        _complete_through(
            cli, tmp_path, monkeypatch, anthropic_snapshot_path, timeout_seconds=8.0
        )


def test_pin_anthropic_key_snapshots_the_accounts_key_and_proxy(
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

    written = pin_anthropic_key_of_account(
        account, anthropic_snapshot_path=anthropic_snapshot_path
    )

    assert written == anthropic_snapshot_path
    assert (
        Path(written).read_text()
        == "ANTHROPIC_API_KEY=sk-ant-key\nANTHROPIC_BASE_URL=https://proxy/\n"
    )


@pytest.mark.parametrize(
    ("lane", "display"), [("anthropic", "Anthropic"), ("openai", "OpenAI")]
)
def test_pin_anthropic_key_refuses_an_account_without_an_anthropic_key(
    lane: str, display: str, tmp_path: Path, anthropic_snapshot_path: str
) -> None:
    root = write_ai_accounts(tmp_path / "accounts", [("acct", lane, display)])
    (root / "acct" / "auth.json").write_text(json.dumps(_CHATGPT_AUTH))
    account = resolve_ai_account("acct", root)

    with pytest.raises(NoUsableAccountError, match="no key to pin"):
        pin_anthropic_key_of_account(
            account, anthropic_snapshot_path=anthropic_snapshot_path
        )

    assert not Path(anthropic_snapshot_path).exists()


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
