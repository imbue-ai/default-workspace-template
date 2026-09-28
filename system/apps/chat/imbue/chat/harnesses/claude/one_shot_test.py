import json
from pathlib import Path

import pytest

from imbue.chat.harnesses.claude.one_shot import claude_one_shot_argv
from imbue.chat.harnesses.claude.one_shot import claude_one_shot_env
from imbue.chat.harnesses.claude.one_shot import parse_claude_print_result
from imbue.chat.harnesses.one_shot import OneShotCompletionError


def test_claude_one_shot_argv_reads_the_prompt_from_stdin_with_no_tools_and_no_session() -> None:
    argv = claude_one_shot_argv("Name this chat.")

    assert argv == [
        "claude",
        "-p",
        "--output-format",
        "json",
        "--model",
        "haiku",
        "--system-prompt",
        "Name this chat.",
        "--tools",
        "",
        "--no-session-persistence",
    ]


def test_claude_one_shot_env_runs_on_the_chats_account_not_the_servers_credentials() -> None:
    ambient = {
        "PATH": "/usr/bin",
        "ANTHROPIC_API_KEY": "server-key-5813",
        "ANTHROPIC_BASE_URL": "https://proxy.example",
        "CLAUDE_CODE_OAUTH_TOKEN": "server-token-5813",
        "MAIN_CLAUDE_SESSION_ID": "session-5813",
        "MNGR_AGENT_NAME": "Chat-4",
        "CLAUDE_CONFIG_DIR": "/somewhere/else",
    }

    env = claude_one_shot_env(ambient, Path("/accounts/acct-5813"))

    assert env == {"PATH": "/usr/bin", "CLAUDE_CONFIG_DIR": "/accounts/acct-5813"}


def test_parse_claude_print_result_returns_the_answer_text() -> None:
    stdout = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "Rome trip: plan"})

    assert parse_claude_print_result(stdout) == "Rome trip: plan"


@pytest.mark.parametrize(
    "stdout",
    [
        "not json",
        json.dumps({"subtype": "error_during_execution", "is_error": True}),
        json.dumps({"subtype": "success", "is_error": True, "result": "Credit balance is too low"}),
        json.dumps({"subtype": "success", "is_error": False}),
    ],
)
def test_parse_claude_print_result_raises_for_an_error_or_unreadable_result(stdout: str) -> None:
    with pytest.raises(OneShotCompletionError):
        parse_claude_print_result(stdout)
