import json
from pathlib import Path

import pytest

from imbue.chat.harnesses.claude.web_lookup import ClaudeWebLookup
from imbue.chat.harnesses.claude.web_lookup import claude_web_lookup_argv
from imbue.chat.harnesses.claude.web_lookup import claude_web_lookup_env
from imbue.chat.harnesses.web_lookup import WebLookupError
from imbue.chat.testing import put_stand_in_cli_on_path


def test_the_argv_gives_the_run_the_web_tools_only_and_keeps_no_session() -> None:
    argv = claude_web_lookup_argv("Find the person.", "opus[1m]")

    assert argv == [
        "claude",
        "-p",
        "--output-format",
        "json",
        "--model",
        "opus[1m]",
        "--system-prompt",
        "Find the person.",
        "--tools",
        "WebSearch,WebFetch",
        "--permission-mode",
        "bypassPermissions",
        "--no-session-persistence",
    ]


def test_the_argv_leaves_the_model_to_the_account_when_none_is_named() -> None:
    assert "--model" not in claude_web_lookup_argv("Find the person.", None)


def test_the_env_runs_on_the_chats_account_and_leaves_thinking_to_it() -> None:
    ambient = {
        "PATH": "/usr/bin",
        "ANTHROPIC_API_KEY": "server-key-5813",
        "MAIN_CLAUDE_SESSION_ID": "session-5813",
    }

    env = claude_web_lookup_env(ambient, Path("/accounts/acct-5813"))

    assert env == {"PATH": "/usr/bin", "CLAUDE_CONFIG_DIR": "/accounts/acct-5813"}


_SUCCESS = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": '{"candidates": []}'})


def test_search_answers_on_the_model_it_is_given_with_only_the_web_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log_path = put_stand_in_cli_on_path(tmp_path, "claude", f"echo '{_SUCCESS}'", monkeypatch)

    answer = ClaudeWebLookup().search(tmp_path / "account", "Find the person.", "Ada Lovelace, London", "sonnet[1m]")

    assert answer == '{"candidates": []}'
    (call,) = log_path.read_text().splitlines()
    assert "--model sonnet[1m]" in call
    assert "--tools WebSearch,WebFetch" in call


def test_search_names_no_model_when_the_chat_has_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log_path = put_stand_in_cli_on_path(tmp_path, "claude", f"echo '{_SUCCESS}'", monkeypatch)

    ClaudeWebLookup().search(tmp_path / "account", "Find the person.", "Ada Lovelace, London", None)

    assert "--model" not in log_path.read_text()


def test_search_runs_from_an_empty_directory_not_the_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    where = tmp_path / "cwd.txt"
    put_stand_in_cli_on_path(tmp_path, "claude", f"pwd > \"{where}\"\necho '{_SUCCESS}'", monkeypatch)

    ClaudeWebLookup().search(tmp_path / "account", "Find the person.", "Ada Lovelace, London", None)

    ran_in = Path(where.read_text().strip())
    assert ran_in.name.startswith("chat_web_lookup_")
    assert not ran_in.exists()


def test_search_raises_with_the_clis_complaint_when_it_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    put_stand_in_cli_on_path(tmp_path, "claude", "echo 'Not logged in' >&2; exit 1", monkeypatch)

    with pytest.raises(WebLookupError, match="exited 1: Not logged in"):
        ClaudeWebLookup().search(tmp_path / "account", "Find the person.", "Ada Lovelace, London", None)


def test_search_raises_when_the_result_is_unreadable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    put_stand_in_cli_on_path(tmp_path, "claude", "echo 'not json'", monkeypatch)

    with pytest.raises(WebLookupError, match="no readable result"):
        ClaudeWebLookup().search(tmp_path / "account", "Find the person.", "Ada Lovelace, London", None)
