import json
from collections.abc import Callable
from pathlib import Path

import pytest

from use_ai_integration_skill.agy_exec import (
    agy_completion_argv,
    agy_environment,
    agy_print_timeout_seconds,
    agy_run_dir,
    agy_task_argv,
    parse_agy_output,
)
from use_ai_integration_skill.claude_p import Usage
from use_ai_integration_skill.errors import ProviderCallError, ProviderTimeoutError

_SUCCESS_ENVELOPE = {
    "conversation_id": "055a398f-db14-4c5f-abbb-1bf03f8120a7",
    "status": "SUCCESS",
    "response": "billing",
    "duration_seconds": 7.16,
    "num_turns": 1,
    "usage": {
        "input_tokens": 10415,
        "output_tokens": 657,
        "thinking_tokens": 616,
        "cache_read_tokens": 8113,
        "total_tokens": 11072,
    },
}


def _envelope(**overrides: object) -> str:
    return json.dumps({**_SUCCESS_ENVELOPE, **overrides}) + "\n"


def test_completion_argv_pins_default_mode_with_strict_permissions() -> None:
    argv = agy_completion_argv(model=None, print_timeout_seconds=290.0)

    assert argv == [
        "agy",
        "--output-format=json",
        "--disable-slash-commands",
        "--print-timeout=290.000s",
        '--settings={"agentMode":"","toolPermission":"strict"}',
    ]


def test_task_argv_approves_every_tool() -> None:
    argv = agy_task_argv(model=None, print_timeout_seconds=290.0)

    assert "--dangerously-skip-permissions" in argv
    assert not any(arg.startswith("--settings") for arg in argv)
    assert "-p" not in argv and "--print" not in argv


@pytest.mark.parametrize("argv_builder", [agy_completion_argv, agy_task_argv])
def test_argv_names_the_model_only_when_given(
    argv_builder: Callable[..., list[str]],
) -> None:
    with_model = argv_builder(model="gemini-3.8-flash", print_timeout_seconds=50.0)
    without_model = argv_builder(model=None, print_timeout_seconds=50.0)

    assert with_model[-1] == "--model=gemini-3.8-flash"
    assert not any(arg.startswith("--model") for arg in without_model)


@pytest.mark.parametrize(
    ("timeout_seconds", "expected"), [(300.0, 290.0), (1.0, 0.5), (15.0, 7.5)]
)
def test_print_timeout_leaves_a_margin_under_the_call_timeout(
    timeout_seconds: float, expected: float
) -> None:
    assert agy_print_timeout_seconds(timeout_seconds) == expected


def test_environment_sets_home_disables_auto_update_and_strips_other_google_credentials() -> (
    None
):
    base_environment = {
        "PATH": "/usr/bin",
        "HOME": "/home/user",
        "PWD": "/home/user/workspace",
        "GEMINI_API_KEY": "ambient",
        "GOOGLE_API_KEY": "ambient",
        "GOOGLE_APPLICATION_CREDENTIALS": "/adc.json",
        "GOOGLE_GEMINI_BASE_URL": "https://proxy",
        "GOOGLE_GENAI_USE_ENTERPRISE": "true",
        "AGY_ADC_AUTH": "1",
        "MNGR_AGENT_STATE_DIR": "/agent/state",
    }

    environment = agy_environment(
        Path("/accounts/g1"), Path("/tmp/agy_x/cwd"), base_environment
    )

    assert environment == {
        "PATH": "/usr/bin",
        "HOME": "/accounts/g1",
        "PWD": "/tmp/agy_x/cwd",
        "AGY_CLI_DISABLE_AUTO_UPDATE": "true",
    }


def test_run_dir_is_the_working_dir_without_a_dot_segment(tmp_path: Path) -> None:
    scratch_dir = tmp_path / "scratch"
    scratch_dir.mkdir()

    assert agy_run_dir(Path("/srv/projects/work"), scratch_dir) == Path(
        "/srv/projects/work"
    )
    assert list(scratch_dir.iterdir()) == []


def test_run_dir_links_a_working_dir_with_a_dot_segment(tmp_path: Path) -> None:
    working_dir = tmp_path / ".hidden" / "work"
    working_dir.mkdir(parents=True)
    scratch_dir = tmp_path / "scratch"
    scratch_dir.mkdir()

    run_dir = agy_run_dir(working_dir, scratch_dir)

    assert run_dir.parent == scratch_dir
    assert run_dir.is_symlink()
    assert run_dir.resolve() == working_dir.resolve()


def test_parse_returns_the_response_and_its_usage() -> None:
    outcome = parse_agy_output(_envelope())

    assert outcome.text == "billing"
    assert outcome.usage == Usage(
        input_tokens=2302,
        output_tokens=1273,
        cache_read_tokens=8113,
        cache_write_tokens=0,
    )
    assert outcome.envelope == _SUCCESS_ENVELOPE


def test_parse_reads_the_result_after_other_lines() -> None:
    stdout = "Using project /tmp/agy_x/cwd\n" + _envelope(response="ok") + "\n"

    assert parse_agy_output(stdout).text == "ok"


def test_parse_raises_on_an_error_status_with_its_message() -> None:
    stdout = _envelope(
        status="ERROR", response="", error="quota exhausted", finished_reason="error"
    )

    with pytest.raises(
        ProviderCallError, match="agy ended with status ERROR: quota exhausted"
    ):
        parse_agy_output(stdout)


def test_parse_raises_a_timeout_on_a_print_timeout_result() -> None:
    stdout = _envelope(response="partial", finished_reason="print_timeout")

    with pytest.raises(ProviderTimeoutError, match="print timeout"):
        parse_agy_output(stdout)


@pytest.mark.parametrize("response", [None, "  \n"])
def test_parse_raises_without_a_response(response: str | None) -> None:
    envelope = {**_SUCCESS_ENVELOPE, "response": response}
    if response is None:
        del envelope["response"]

    with pytest.raises(ProviderCallError, match="agy gave no answer"):
        parse_agy_output(json.dumps(envelope))


def test_parse_raises_when_no_line_is_a_result() -> None:
    stdout = "CLI error: bubbletea: error opening TTY: could not open TTY\n"

    with pytest.raises(ProviderCallError, match="agy printed no result"):
        parse_agy_output(stdout)
