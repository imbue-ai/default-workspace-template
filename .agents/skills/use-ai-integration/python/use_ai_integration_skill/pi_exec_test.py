import json
from pathlib import Path

import pytest

from use_ai_integration_skill.claude_p import Usage
from use_ai_integration_skill.errors import ProviderCallError
from use_ai_integration_skill.pi_exec import (
    parse_pi_json_output,
    pi_completion_argv,
    pi_environment,
    pi_task_argv,
)

# The key variable of every provider the chat app's pi lanes can hold.
_LANE_KEY_VARIABLES = (
    "AI_GATEWAY_API_KEY",
    "ANT_LING_API_KEY",
    "ANTHROPIC_API_KEY",
    "CEREBRAS_API_KEY",
    "DEEPSEEK_API_KEY",
    "FIREWORKS_API_KEY",
    "GEMINI_API_KEY",
    "GROQ_API_KEY",
    "KIMI_API_KEY",
    "MINIMAX_API_KEY",
    "MINIMAX_CN_API_KEY",
    "MISTRAL_API_KEY",
    "MOONSHOT_API_KEY",
    "NVIDIA_API_KEY",
    "OPENAI_API_KEY",
    "OPENCODE_API_KEY",
    "OPENROUTER_API_KEY",
    "QWEN_TOKEN_PLAN_API_KEY",
    "QWEN_TOKEN_PLAN_CN_API_KEY",
    "TOGETHER_API_KEY",
    "XAI_API_KEY",
    "XIAOMI_API_KEY",
    "XIAOMI_TOKEN_PLAN_AMS_API_KEY",
    "XIAOMI_TOKEN_PLAN_CN_API_KEY",
    "XIAOMI_TOKEN_PLAN_SGP_API_KEY",
    "ZAI_API_KEY",
    "ZAI_CODING_CN_API_KEY",
)

_SESSION_HEADER: dict[str, object] = {
    "type": "session",
    "version": 3,
    "id": "01a2143a-4062-770b-b9f3-bc6ed60c47e8",
    "timestamp": "2026-11-25T19:41:31.618Z",
    "cwd": "/tmp/pi_x/cwd",
}


def _assistant_message(
    content: list[dict[str, object]],
    *,
    stop_reason: str = "stop",
    usage: dict[str, object] | None = None,
    error_message: str | None = None,
) -> dict[str, object]:
    message: dict[str, object] = {
        "role": "assistant",
        "content": content,
        "api": "openai-completions",
        "provider": "groq",
        "model": "openai/gpt-oss-120b",
        "usage": usage
        or {
            "input": 0,
            "output": 0,
            "cacheRead": 0,
            "cacheWrite": 0,
            "totalTokens": 0,
            "cost": {"total": 0},
        },
        "stopReason": stop_reason,
        "timestamp": 1795635692001,
    }
    if error_message is not None:
        message["errorMessage"] = error_message
    return message


def _stream(*records: dict[str, object]) -> str:
    return "".join(json.dumps(record) + "\n" for record in records)


def _message_end(message: dict[str, object]) -> dict[str, object]:
    return {"type": "message_end", "message": message}


def test_completion_argv_disables_tools_session_context_and_resources() -> None:
    argv = pi_completion_argv(
        provider_id="groq",
        model="openai/gpt-oss-120b",
        system_prompt_path=Path("/scratch/system-prompt.txt"),
    )

    assert argv == [
        "pi",
        "--mode",
        "json",
        "--no-session",
        "--no-tools",
        "--no-context-files",
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-approve",
        "--thinking",
        "off",
        "--provider",
        "groq",
        "--model",
        "openai/gpt-oss-120b",
        "--system-prompt",
        "/scratch/system-prompt.txt",
    ]


def test_task_argv_keeps_tools_and_context_files_and_appends_instructions() -> None:
    argv = pi_task_argv(
        provider_id="openrouter",
        model="moonshotai/kimi-k2.6",
        instructions_path=Path("/scratch/instructions.txt"),
    )

    assert argv == [
        "pi",
        "--mode",
        "json",
        "--no-session",
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-approve",
        "--provider",
        "openrouter",
        "--model",
        "moonshotai/kimi-k2.6",
        "--append-system-prompt",
        "/scratch/instructions.txt",
    ]


def test_task_argv_without_instructions_has_no_append_flag() -> None:
    argv = pi_task_argv(
        provider_id="openrouter", model="moonshotai/kimi-k2.6", instructions_path=None
    )

    assert "--append-system-prompt" not in argv
    assert argv[-2:] == ["--model", "moonshotai/kimi-k2.6"]


def test_environment_binds_pi_to_the_account_and_strips_every_provider_credential() -> (
    None
):
    base_environment = {
        "PATH": "/usr/bin",
        "LANG": "C.UTF-8",
        "ANTHROPIC_AUTH_TOKEN": "ambient",
        "ANTHROPIC_OAUTH_TOKEN": "ambient",
        "COPILOT_GITHUB_TOKEN": "ambient",
        "GOOGLE_APPLICATION_CREDENTIALS": "/adc.json",
        "GOOGLE_CLOUD_PROJECT": "project",
        "GOOGLE_CLOUD_LOCATION": "us-central1",
        "HF_TOKEN": "ambient",
        "MNGR_AGENT_STATE_DIR": "/agent/state",
        "AWS_PROFILE": "default",
        "AWS_SESSION_TOKEN": "ambient",
        "PI_CODING_AGENT_DIR": "/elsewhere",
    }

    environment = pi_environment(Path("/accounts/k1"), base_environment)

    assert environment == {
        "PATH": "/usr/bin",
        "LANG": "C.UTF-8",
        "PI_CODING_AGENT_DIR": "/accounts/k1",
    }


@pytest.mark.parametrize("name", _LANE_KEY_VARIABLES)
def test_environment_strips_the_key_variable_of_every_lane_provider(name: str) -> None:
    environment = pi_environment(Path("/accounts/k1"), {name: "ambient-key"})

    assert name not in environment


def test_parse_returns_the_answer_usage_and_cost_of_a_single_message() -> None:
    stdout = _stream(
        _SESSION_HEADER,
        {"type": "agent_start"},
        _message_end(
            {"role": "user", "content": [{"type": "text", "text": "Classify"}]}
        ),
        _message_end(
            _assistant_message(
                [{"type": "text", "text": "billing"}],
                usage={
                    "input": 812,
                    "output": 3,
                    "cacheRead": 0,
                    "cacheWrite": 0,
                    "totalTokens": 815,
                    "cost": {
                        "input": 0.0000609,
                        "output": 0.0000009,
                        "total": 0.0000618,
                    },
                },
            )
        ),
        {"type": "agent_settled"},
    )

    outcome = parse_pi_json_output(stdout)

    assert outcome.text == "billing"
    assert outcome.usage == Usage(
        input_tokens=812, output_tokens=3, cache_read_tokens=0, cache_write_tokens=0
    )
    assert outcome.cost_usd == pytest.approx(0.0000618)
    assert outcome.events[0] == _SESSION_HEADER
    assert len(outcome.events) == 5


def test_parse_sums_usage_and_cost_over_every_assistant_message() -> None:
    tool_turn = _assistant_message(
        [
            {"type": "thinking", "thinking": "I should read it."},
            {"type": "toolCall", "id": "c1", "name": "read", "arguments": {}},
        ],
        stop_reason="toolUse",
        usage={
            "input": 100,
            "output": 20,
            "cacheRead": 5,
            "cacheWrite": 7,
            "cost": {"total": 0.25},
        },
    )
    answer = _assistant_message(
        [{"type": "text", "text": "first"}, {"type": "text", "text": "second"}],
        usage={
            "input": 30,
            "output": 4,
            "cacheRead": 105,
            "cacheWrite": 0,
            "cost": {"total": 0.5},
        },
    )
    stdout = _stream(
        _SESSION_HEADER,
        _message_end(tool_turn),
        _message_end(
            {
                "role": "toolResult",
                "toolCallId": "c1",
                "content": [{"type": "text", "text": "file text"}],
            }
        ),
        {"type": "turn_end", "message": tool_turn, "toolResults": []},
        _message_end(answer),
        {"type": "turn_end", "message": answer, "toolResults": []},
        {"type": "agent_end", "messages": [tool_turn, answer], "willRetry": False},
    )

    outcome = parse_pi_json_output(stdout)

    assert outcome.text == "first\nsecond"
    assert outcome.usage == Usage(
        input_tokens=130, output_tokens=24, cache_read_tokens=110, cache_write_tokens=7
    )
    assert outcome.cost_usd == pytest.approx(0.75)


def test_parse_returns_the_answer_after_a_retried_failure() -> None:
    stdout = _stream(
        _SESSION_HEADER,
        _message_end(
            _assistant_message([], stop_reason="error", error_message="529 overloaded")
        ),
        {"type": "auto_retry_start", "attempt": 1, "maxAttempts": 3},
        _message_end(_assistant_message([{"type": "text", "text": "billing"}])),
        {"type": "auto_retry_end", "success": True, "attempt": 2},
    )

    assert parse_pi_json_output(stdout).text == "billing"


@pytest.mark.parametrize("stop_reason", ["error", "aborted"])
def test_parse_raises_when_the_last_assistant_message_failed(stop_reason: str) -> None:
    stdout = _stream(
        _SESSION_HEADER,
        _message_end(
            _assistant_message(
                [], stop_reason=stop_reason, error_message="Connection error."
            )
        ),
        {"type": "auto_retry_end", "success": False, "finalError": "Connection error."},
    )

    with pytest.raises(
        ProviderCallError, match="pi's model call failed: Connection error."
    ):
        parse_pi_json_output(stdout)


def test_parse_raises_without_an_assistant_message() -> None:
    with pytest.raises(ProviderCallError, match="pi gave no answer"):
        parse_pi_json_output(_stream(_SESSION_HEADER))


def test_parse_raises_when_the_answer_has_no_text() -> None:
    stdout = _stream(
        _SESSION_HEADER,
        _message_end(_assistant_message([{"type": "thinking", "thinking": "Hmm."}])),
    )

    with pytest.raises(ProviderCallError, match="pi gave no answer"):
        parse_pi_json_output(stdout)


def test_parse_ignores_system_and_user_messages_with_string_content() -> None:
    stdout = _stream(
        _SESSION_HEADER,
        _message_end(
            {"role": "system", "content": "", "sections": {"preamble": "Be terse."}}
        ),
        _message_end({"role": "user", "content": "hi"}),
        _message_end(_assistant_message([{"type": "text", "text": "hello"}])),
    )

    assert parse_pi_json_output(stdout).text == "hello"


@pytest.mark.parametrize("line", ["not json", "[]"])
def test_parse_raises_on_a_line_that_is_not_an_event(line: str) -> None:
    with pytest.raises(ProviderCallError, match="not an event"):
        parse_pi_json_output(_stream(_SESSION_HEADER) + line + "\n")
