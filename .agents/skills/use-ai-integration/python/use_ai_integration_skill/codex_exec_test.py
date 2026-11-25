from pathlib import Path

import pytest

from use_ai_integration_skill.claude_p import Usage
from use_ai_integration_skill.codex_exec import (
    CODEX_TASK_SANDBOX,
    codex_exec_argv,
    codex_exec_environment,
    parse_codex_exec_output,
)
from use_ai_integration_skill.data_types import CodexSandboxMode
from use_ai_integration_skill.errors import ProviderCallError

_SUCCESS_OUTPUT = """\
{"type":"thread.started","thread_id":"0199a213-81c0-7800-8aa1-bbab2a035a53"}
{"type":"turn.started"}
{"type":"item.completed","item":{"id":"item_0","type":"reasoning","text":"**Classifying the email**"}}
{"type":"item.completed","item":{"id":"item_1","type":"agent_message","text":"billing"}}
{"type":"turn.completed","usage":{"input_tokens":2104,"cached_input_tokens":1024,"cache_write_input_tokens":0,"output_tokens":12,"reasoning_output_tokens":0}}
"""

_RECOVERED_RETRY_OUTPUT = """\
{"type":"thread.started","thread_id":"t1"}
{"type":"turn.started"}
{"type":"error","message":"Reconnecting... 1/5 (stream disconnected before completion)"}
{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"ok"}}
{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":2,"reasoning_output_tokens":0}}
"""

_FAILED_TURN_OUTPUT = """\
{"type":"thread.started","thread_id":"t1"}
{"type":"turn.started"}
{"type":"turn.failed","error":{"message":"unexpected status 401 Unauthorized"}}
"""


def test_completion_argv_is_the_proven_one_shot_argv_with_json() -> None:
    assert codex_exec_argv(model=None, sandbox_mode=CodexSandboxMode.READ_ONLY) == [
        "codex",
        "exec",
        "--ignore-user-config",
        "--ignore-rules",
        "--ephemeral",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--color",
        "never",
        "--json",
        "-",
    ]


def test_argv_names_the_model_when_given() -> None:
    argv = codex_exec_argv(model="gpt-x", sandbox_mode=CodexSandboxMode.READ_ONLY)

    assert argv[:4] == ["codex", "exec", "--model", "gpt-x"]


def test_task_sandbox_is_full_access() -> None:
    argv = codex_exec_argv(model=None, sandbox_mode=CODEX_TASK_SANDBOX)

    assert argv[argv.index("--sandbox") + 1] == "danger-full-access"


def test_environment_binds_codex_home_and_strips_ambient_credentials(
    tmp_path: Path,
) -> None:
    base_environment = {
        "OPENAI_API_KEY": "sk-ambient",
        "CODEX_API_KEY": "sk-codex",
        "CODEX_ACCESS_TOKEN": "token",
        "MNGR_AGENT_STATE_DIR": "/agents/lead",
        "PATH": "/usr/bin",
        "CODEX_HOME": "/elsewhere",
    }
    original = dict(base_environment)

    environment = codex_exec_environment(tmp_path / "acct", base_environment)

    assert environment == {"PATH": "/usr/bin", "CODEX_HOME": str(tmp_path / "acct")}
    assert base_environment == original


def test_parse_returns_the_last_agent_message_and_its_usage() -> None:
    outcome = parse_codex_exec_output(_SUCCESS_OUTPUT)

    assert outcome.text == "billing"
    assert outcome.usage == Usage(
        input_tokens=1080,
        output_tokens=12,
        cache_read_tokens=1024,
        cache_write_tokens=0,
    )
    assert len(outcome.events) == 5
    assert outcome.events[0] == {
        "type": "thread.started",
        "thread_id": "0199a213-81c0-7800-8aa1-bbab2a035a53",
    }


def test_parse_ignores_an_error_the_turn_recovered_from() -> None:
    outcome = parse_codex_exec_output(_RECOVERED_RETRY_OUTPUT)

    assert outcome.text == "ok"
    assert outcome.events[2]["type"] == "error"


def test_parse_ignores_non_fatal_error_items() -> None:
    output = (
        '{"type":"item.completed","item":{"id":"item_0","type":"error","message":"a tool was refused"}}\n'
        + _SUCCESS_OUTPUT
    )

    assert parse_codex_exec_output(output).text == "billing"


def test_parse_raises_on_a_failed_turn_with_its_message() -> None:
    with pytest.raises(ProviderCallError, match="401 Unauthorized"):
        parse_codex_exec_output(_FAILED_TURN_OUTPUT)


def test_parse_raises_when_the_turn_never_completes() -> None:
    output = '{"type":"turn.started"}\n{"type":"error","message":"stream disconnected for good"}\n'

    with pytest.raises(
        ProviderCallError,
        match="without completing its turn: stream disconnected for good",
    ):
        parse_codex_exec_output(output)


def test_parse_raises_without_an_agent_message() -> None:
    output = (
        '{"type":"item.completed","item":{"id":"item_0","type":"reasoning","text":"thinking"}}\n'
        '{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}\n'
    )

    with pytest.raises(ProviderCallError, match="no answer"):
        parse_codex_exec_output(output)


@pytest.mark.parametrize(
    "line", ["Reading prompt from stdin...", "[1, 2]", '{"no_type": true}']
)
def test_parse_raises_on_a_line_that_is_not_an_event(line: str) -> None:
    with pytest.raises(ProviderCallError, match="not an event"):
        parse_codex_exec_output(f"{line}\n{_SUCCESS_OUTPUT}")
