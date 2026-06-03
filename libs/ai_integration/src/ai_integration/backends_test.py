import pytest

from ai_integration.backends import build_claude_cli_argv, parse_cli_result
from ai_integration.data_types import BillingPath
from ai_integration.errors import ClaudeCLIError


def test_build_argv_emits_system_and_disabled_tools() -> None:
    argv = build_claude_cli_argv(
        prompt="hi",
        model="claude-haiku-4-5",
        system="You are terse.",
        append_system=None,
        tools="",
        extra_args=None,
    )
    assert argv[:3] == ["claude", "-p", "hi"]
    assert "--system-prompt" in argv
    assert argv[argv.index("--system-prompt") + 1] == "You are terse."
    # tools="" must still emit the flag (disable all tools), distinct from None.
    assert "--tools" in argv
    assert argv[argv.index("--tools") + 1] == ""
    assert "--append-system-prompt" not in argv


def test_build_argv_omits_tools_flag_when_none() -> None:
    # None means "inherit the default agent tool set" -- the flag is left off
    # entirely (this is the run_task / agentic path).
    argv = build_claude_cli_argv(
        prompt="do work",
        model="claude-haiku-4-5",
        system=None,
        append_system="Extra instructions.",
        tools=None,
        extra_args=["--add-dir", "/repo"],
    )
    assert "--tools" not in argv
    assert "--system-prompt" not in argv
    assert argv[argv.index("--append-system-prompt") + 1] == "Extra instructions."
    assert argv[-2:] == ["--add-dir", "/repo"]


def test_parse_cli_result_extracts_text_usage_cost() -> None:
    data = {
        "result": "hi",
        "total_cost_usd": 0.01,
        "usage": {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_read_input_tokens": 2,
        },
    }
    result = parse_cli_result(data, "claude-haiku-4-5")
    assert result.text == "hi"
    assert result.billing_path is BillingPath.CLAUDE_CLI
    assert result.cost_usd == 0.01
    assert result.usage is not None
    assert result.usage.input_tokens == 10
    assert result.usage.cache_read_tokens == 2


def test_parse_cli_result_missing_cost_is_none() -> None:
    result = parse_cli_result({"result": "x"}, "claude-haiku-4-5")
    assert result.cost_usd is None
    assert result.text == "x"


def test_parse_cli_result_non_dict_raises() -> None:
    with pytest.raises(ClaudeCLIError):
        parse_cli_result(["not", "a", "dict"], "m")
