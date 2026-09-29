import json
from pathlib import Path

import pytest

from imbue.chat.harnesses.claude.one_shot import ClaudeOneShotCompletion
from imbue.chat.harnesses.claude.one_shot import claude_one_shot_argv
from imbue.chat.harnesses.claude.one_shot import claude_one_shot_env
from imbue.chat.harnesses.claude.one_shot import parse_claude_print_result
from imbue.chat.harnesses.one_shot import OneShotCompletionError
from imbue.chat.testing import put_stand_in_cli_on_path


def test_claude_one_shot_argv_reads_the_prompt_from_stdin_with_no_tools_and_no_session() -> None:
    argv = claude_one_shot_argv("Name this chat.", "haiku")

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


def test_claude_one_shot_argv_leaves_the_model_to_the_account_when_none_is_named() -> None:
    assert "--model" not in claude_one_shot_argv("Name this chat.", None)


_SUCCESS_RESULT = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "Rome trip: plan"})


def test_complete_answers_on_haiku_when_the_account_has_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log_path = put_stand_in_cli_on_path(tmp_path, "claude", f"echo '{_SUCCESS_RESULT}'", monkeypatch)

    answer = ClaudeOneShotCompletion().complete(tmp_path / "account", "Name this chat.", "Plan Rome")

    assert answer == "Rome trip: plan"
    assert "--model haiku" in log_path.read_text()
    assert len(log_path.read_text().splitlines()) == 1


def test_complete_moves_down_the_preference_list_to_the_accounts_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = (
        'case " $* " in *" --model "*) echo "There\'s an issue with the selected model" >&2; exit 1;; esac\n'
        f"echo '{_SUCCESS_RESULT}'"
    )
    log_path = put_stand_in_cli_on_path(tmp_path, "claude", body, monkeypatch)

    answer = ClaudeOneShotCompletion().complete(tmp_path / "account", "Name this chat.", "Plan Rome")

    assert answer == "Rome trip: plan"
    calls = log_path.read_text().splitlines()
    assert ["--model haiku" in calls[0], "--model sonnet" in calls[1], "--model" in calls[2]] == [True, True, False]


def test_complete_raises_with_every_models_failure_when_none_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    put_stand_in_cli_on_path(tmp_path, "claude", "echo 'Not logged in' >&2; exit 1", monkeypatch)

    with pytest.raises(OneShotCompletionError, match="haiku: .*sonnet: .*default model: .*Not logged in"):
        ClaudeOneShotCompletion().complete(tmp_path / "account", "Name this chat.", "Plan Rome")


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

    assert env == {"PATH": "/usr/bin", "CLAUDE_CONFIG_DIR": "/accounts/acct-5813", "MAX_THINKING_TOKENS": "0"}


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
