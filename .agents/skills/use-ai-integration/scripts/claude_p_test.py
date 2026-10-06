"""Unit tests for the copyable ``claude_p`` helper.

These guard the parts that are easy to get wrong and that the module docstring
promises: flag emission per scenario, the success-vs-error JSON arm handling, the
session-var scrub, and how a call from a service launches ``claude`` (against a
stub ``claude`` on ``PATH``, since the real binary needs a live sign-in).
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parent / "claude_p.py"
_spec = importlib.util.spec_from_file_location("claude_p", _SCRIPT)
assert _spec is not None and _spec.loader is not None
claude_p = importlib.util.module_from_spec(_spec)
# Register before exec so the module's frozen (``from __future__ import
# annotations``) dataclasses can resolve their own module via sys.modules.
sys.modules[_spec.name] = claude_p
_spec.loader.exec_module(claude_p)


def test_completion_argv_disables_tools_and_sets_system() -> None:
    argv = claude_p._completion_argv(
        "classify this",
        model="claude-haiku-4-5",
        system="You are a classifier.",
    )
    assert argv[:3] == ["claude", "-p", "classify this"]
    assert (
        "--output-format" in argv and argv[argv.index("--output-format") + 1] == "json"
    )
    assert argv[argv.index("--system-prompt") + 1] == "You are a classifier."
    # tools="" is the meaningful "disable every tool" value and must be emitted.
    assert argv[argv.index("--tools") + 1] == ""
    assert "--append-system-prompt" not in argv
    assert "--permission-mode" not in argv


def test_completion_argv_does_not_persist_its_session() -> None:
    argv = claude_p._completion_argv(
        "classify this", model="claude-haiku-4-5", system="You are a classifier."
    )
    assert "--no-session-persistence" in argv


def test_task_argv_keeps_tools_and_sets_permission_mode() -> None:
    argv = claude_p._task_argv(
        "do work",
        model="claude-haiku-4-5",
        system=None,
        append_system="Only touch data/.",
        permission_mode="bypassPermissions",
    )
    # tools=None leaves the flag off entirely, inheriting the default tool set.
    assert "--tools" not in argv
    assert argv[argv.index("--append-system-prompt") + 1] == "Only touch data/."
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"
    assert "--system-prompt" not in argv
    # A task runs in the repo and its session is resumable like any other.
    assert "--no-session-persistence" not in argv


def _success_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "subtype": "success",
        "is_error": False,
        "result": "the answer",
        "total_cost_usd": 0.0123,
        "usage": {
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_input_tokens": 5,
            "cache_creation_input_tokens": 7,
        },
    }
    payload.update(overrides)
    return payload


def test_parse_result_rejects_non_object_payloads() -> None:
    # claude -p is asked for --output-format json, but a list / number / null is
    # still possible external output; validation must surface it as ClaudeCLIError
    # rather than letting a non-object reach the field reads.
    for payload in ([1, 2, 3], "a string", 42, None):
        with pytest.raises(claude_p.ClaudeCLIError, match="expected result shape"):
            claude_p._parse_result(payload)


def test_parse_result_rejects_wrong_typed_fields() -> None:
    # A present-but-wrong-typed field is malformed output: a non-string result or
    # a non-numeric cost fails validation rather than being silently accepted.
    with pytest.raises(claude_p.ClaudeCLIError, match="expected result shape"):
        claude_p._parse_result(_success_payload(result=123))
    with pytest.raises(claude_p.ClaudeCLIError, match="expected result shape"):
        claude_p._parse_result(_success_payload(total_cost_usd="free"))


def test_parse_result_success_extracts_text_cost_and_usage() -> None:
    result = claude_p._parse_result(_success_payload())
    assert result.text == "the answer"
    assert result.cost_usd == 0.0123
    assert result.usage.input_tokens == 100
    assert result.usage.output_tokens == 20
    assert result.usage.cache_read_tokens == 5
    assert result.usage.cache_write_tokens == 7
    assert result.raw["subtype"] == "success"


def test_parse_result_raises_on_error_arm() -> None:
    payload = {
        "subtype": "error_max_turns",
        "is_error": True,
        "errors": ["hit the turn limit"],
        "total_cost_usd": 0.5,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    with pytest.raises(claude_p.ClaudeCLIError, match="error_max_turns"):
        claude_p._parse_result(payload)


def test_parse_result_error_arm_tolerates_non_string_errors() -> None:
    # claude -p output is external JSON: a non-string element in 'errors' must
    # still raise ClaudeCLIError (with the detail stringified), not a TypeError
    # from str.join inside the error path.
    payload = {
        "subtype": "error_during_execution",
        "is_error": True,
        "errors": [{"code": 42}, "and a string"],
        "total_cost_usd": 0.5,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    with pytest.raises(claude_p.ClaudeCLIError, match="and a string"):
        claude_p._parse_result(payload)


def test_parse_result_raises_when_subtype_not_success() -> None:
    # is_error may be absent/false but a non-success subtype must still raise,
    # rather than be treated as an empty-text success.
    with pytest.raises(claude_p.ClaudeCLIError):
        claude_p._parse_result(_success_payload(subtype="error_during_execution"))


def test_parse_result_raises_on_missing_result_text() -> None:
    payload = _success_payload()
    del payload["result"]
    with pytest.raises(claude_p.ClaudeCLIError, match="result"):
        claude_p._parse_result(payload)


def test_parse_result_raises_on_missing_cost() -> None:
    payload = _success_payload()
    del payload["total_cost_usd"]
    with pytest.raises(claude_p.ClaudeCLIError, match="total_cost_usd"):
        claude_p._parse_result(payload)


def test_parse_result_defaults_cache_tokens_to_zero() -> None:
    result = claude_p._parse_result(
        _success_payload(usage={"input_tokens": 3, "output_tokens": 4})
    )
    assert result.usage.cache_read_tokens == 0
    assert result.usage.cache_write_tokens == 0


def test_parse_result_rejects_malformed_usage_tokens() -> None:
    # The usage block is validated too: a non-integer token count is malformed
    # output and raises rather than silently reading as some default.
    with pytest.raises(claude_p.ClaudeCLIError, match="expected result shape"):
        claude_p._parse_result(_success_payload(usage={"input_tokens": "lots"}))


def test_child_env_unsets_session_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(claude_p._MAIN_CLAUDE_SESSION_ID, "main-session")
    monkeypatch.setenv("MNGR_AGENT_NAME", "lead")
    env = claude_p._child_env()
    assert claude_p._MAIN_CLAUDE_SESSION_ID not in env
    # Without the opt-in, the mngr identity vars are left in place.
    assert env.get("MNGR_AGENT_NAME") == "lead"


def test_child_env_strips_mngr_vars_when_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MNGR_AGENT_NAME", "lead")
    monkeypatch.setenv("MNGR_AGENT_STATE_DIR", "/tmp/state")
    env = claude_p._child_env(strip_mngr_agent_vars=True)
    assert "MNGR_AGENT_NAME" not in env
    assert "MNGR_AGENT_STATE_DIR" not in env
    # The real environment is untouched (we only scrub the copy).
    assert os.environ.get("MNGR_AGENT_NAME") == "lead"


def _write_claude_accounts(
    home: Path,
    account_ids: tuple[str, ...],
    *,
    mru: str,
    default_account: str | None = None,
    codex_account_ids: tuple[str, ...] = (),
) -> Path:
    """Give ``home`` these claude accounts, in index order; return the accounts root.

    Any ``codex_account_ids`` are written ahead of them, as the oldest accounts.
    """
    accounts = home / ".minds" / "accounts"
    for account_id in (*codex_account_ids, *account_ids):
        (accounts / account_id).mkdir(parents=True)
    index: dict[str, object] = {
        "accounts": [{"id": a, "lane": "openai"} for a in codex_account_ids]
        + [{"id": a, "lane": "anthropic"} for a in account_ids],
        "mru": mru,
    }
    if default_account is not None:
        index["default_account"] = default_account
    (accounts / "index.json").write_text(json.dumps(index))
    return accounts


def _make_default_account(home: Path) -> Path:
    """Give ``home`` one claude account, recorded as the most recently used; return its folder."""
    return (
        _write_claude_accounts(home, ("acct-claude",), mru="acct-claude")
        / "acct-claude"
    )


def test_child_env_keeps_the_agents_own_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inside an agent, the child runs on the chat's account, not the workspace default."""
    monkeypatch.setenv("HOME", str(tmp_path))
    _make_default_account(tmp_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/accounts/the-chats-own")

    assert claude_p._child_env()["CLAUDE_CONFIG_DIR"] == "/accounts/the-chats-own"


@pytest.mark.parametrize(
    ("default_account", "mru", "expected"),
    [
        pytest.param(
            "acct-oldest", "acct-recent", "acct-oldest", id="pinned-claude-account"
        ),
        pytest.param(
            "acct-codex", "acct-recent", "acct-recent", id="pinned-codex-account"
        ),
        pytest.param(None, "acct-codex", "acct-oldest", id="codex-most-recently-used"),
    ],
)
def test_child_env_runs_on_the_default_claude_account(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    default_account: str | None,
    mru: str,
    expected: str,
) -> None:
    """Outside an agent, the child runs on the Claude account a chat created now would get:
    the pinned default when it is a Claude account, else the most recently used one when
    it is, else the oldest."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    accounts = _write_claude_accounts(
        tmp_path,
        ("acct-oldest", "acct-recent"),
        mru=mru,
        default_account=default_account,
        codex_account_ids=("acct-codex",),
    )

    assert claude_p._child_env()["CLAUDE_CONFIG_DIR"] == str(accounts / expected)


def test_child_env_leaves_config_dir_unset_without_an_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no account to name, the child keeps claude's own default, ~/.claude."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

    assert "CLAUDE_CONFIG_DIR" not in claude_p._child_env()


# Stands in for claude: reports the config dir it was given and whether its stdin is
# at EOF (readable at once and empty), as the result text of a success payload.
_STUB_CLAUDE = """#!/usr/bin/env python3
import json, os, select
stdin_at_eof = bool(select.select([0], [], [], 1.0)[0]) and os.read(0, 1) == b""
observed = {"config_dir": os.environ.get("CLAUDE_CONFIG_DIR"), "stdin_at_eof": stdin_at_eof}
print(json.dumps({"subtype": "success", "result": json.dumps(observed), "total_cost_usd": 0}))
"""

_SERVICE_CALL = """
import sys
sys.path.insert(0, sys.argv[1])
from claude_p import claude_p_completion
print(claude_p_completion("hello", system="Answer briefly.").text)
"""


def _complete_from_a_service(tmp_path: Path, home: Path) -> dict[str, object]:
    """Run one completion the way a supervisord service does; return what claude saw.

    The caller's environment has no ``CLAUDE_CONFIG_DIR``, and its stdin is a pipe
    nothing ever closes.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "claude"
    stub.write_text(_STUB_CLAUDE)
    stub.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CONFIG_DIR"}
    env["HOME"] = str(home)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    stdin_read, stdin_write = os.pipe()
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _SERVICE_CALL, str(_SCRIPT.parent)],
            stdin=stdin_read,
            capture_output=True,
            text=True,
            env=env,
            timeout=8,
        )
    finally:
        os.close(stdin_read)
        os.close(stdin_write)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_completion_from_a_service_runs_claude_on_the_default_account(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    account_dir = _make_default_account(home)

    observed = _complete_from_a_service(tmp_path, home)

    assert observed["config_dir"] == str(account_dir)


def test_completion_from_a_service_gives_claude_a_closed_stdin(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _make_default_account(home)

    observed = _complete_from_a_service(tmp_path, home)

    assert observed["stdin_at_eof"] is True


def _isolate_credential_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Point every credential source at an empty tmp dir; return a settings path.

    chdir isolates the data/.secrets/anthropic.env snapshot (a repo-root
    relative path); CLAUDE_CONFIG_DIR points at the tmp dir for settings; the
    process-env credential vars are cleared.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    _clear_credential_env_vars(monkeypatch)
    return tmp_path / "settings.json"


def _isolate_credential_sources_in_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Like ``_isolate_credential_sources``, but as a service sees it: no
    CLAUDE_CONFIG_DIR, and HOME (so ~/.claude and ~/.minds) is the tmp dir."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    _clear_credential_env_vars(monkeypatch)


def _clear_credential_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(var, raising=False)


def test_credentials_prefer_snapshot_over_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The setup-time snapshot pins a keyed integration across later auth changes."""
    settings = _isolate_credential_sources(tmp_path, monkeypatch)
    settings.write_text(
        '{"env": {"ANTHROPIC_API_KEY": "sk-new-key", "ANTHROPIC_BASE_URL": "https://new/"}}'
    )
    snapshot = tmp_path / "data" / ".secrets" / "anthropic.env"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text(
        "ANTHROPIC_API_KEY=sk-pinned-key\nANTHROPIC_BASE_URL=https://pinned/\n"
    )

    creds = claude_p.read_workspace_ai_credentials()

    assert creds.api_key == "sk-pinned-key"
    assert creds.base_url == "https://pinned/"


def test_credentials_fall_back_to_settings_without_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _isolate_credential_sources(tmp_path, monkeypatch)
    settings.write_text('{"env": {"ANTHROPIC_API_KEY": "sk-settings-key"}}')

    creds = claude_p.read_workspace_ai_credentials()

    assert creds.api_key == "sk-settings-key"
    assert creds.base_url is None


def test_credentials_read_default_claude_dir_without_config_dir_or_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With CLAUDE_CONFIG_DIR unset and no provider account, the resolver reads
    the shared ~/.claude/settings.json instead of skipping settings entirely."""
    _isolate_credential_sources_in_home(tmp_path, monkeypatch)
    default_dir = tmp_path / ".claude"
    default_dir.mkdir()
    (default_dir / "settings.json").write_text(
        '{"env": {"ANTHROPIC_API_KEY": "sk-default-dir-key"}}'
    )

    creds = claude_p.read_workspace_ai_credentials()

    assert creds.api_key == "sk-default-dir-key"


def test_credentials_read_the_default_account_when_config_dir_env_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A service or cron job reads the workspace's default account, not ~/.claude."""
    _isolate_credential_sources_in_home(tmp_path, monkeypatch)
    account_dir = _make_default_account(tmp_path)
    (account_dir / "settings.json").write_text(
        '{"env": {"ANTHROPIC_API_KEY": "sk-account-key"}}'
    )
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(
        '{"env": {"ANTHROPIC_API_KEY": "sk-shared-key"}}'
    )

    assert claude_p.read_workspace_ai_credentials().api_key == "sk-account-key"


def test_credentials_never_take_oauth_token_from_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hand-edited token line in the snapshot is ignored: tokens cannot auth API calls."""
    _isolate_credential_sources(tmp_path, monkeypatch)
    snapshot = tmp_path / "data" / ".secrets" / "anthropic.env"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text("CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-handwritten\n")

    assert claude_p.read_workspace_ai_credentials().oauth_token is None


def test_write_snapshot_captures_key_and_base_url_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The snapshot writer records the key + base URL, owner-only, and never the token."""
    settings = _isolate_credential_sources(tmp_path, monkeypatch)
    settings.write_text(
        '{"env": {"ANTHROPIC_API_KEY": "sk-live-key", "ANTHROPIC_BASE_URL": "https://proxy/",'
        ' "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-secret"}}'
    )

    written = claude_p.write_anthropic_env_snapshot()

    snapshot = tmp_path / written
    content = snapshot.read_text()
    assert (
        content == "ANTHROPIC_API_KEY=sk-live-key\nANTHROPIC_BASE_URL=https://proxy/\n"
    )
    assert (snapshot.stat().st_mode & 0o777) == 0o600


def test_write_snapshot_raises_without_a_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A keyless workspace has nothing to snapshot; the writer says so instead of writing junk."""
    settings = _isolate_credential_sources(tmp_path, monkeypatch)
    settings.write_text('{"env": {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-secret"}}')

    with pytest.raises(claude_p.ClaudeCLIError, match="nothing to snapshot"):
        claude_p.write_anthropic_env_snapshot()
    assert not (tmp_path / "data" / ".secrets" / "anthropic.env").exists()
