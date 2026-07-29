import pytest

from imbue.system_interface.codex_tool_labels import codex_tool_labels


@pytest.mark.parametrize(
    "tool_name, input_preview, expected",
    [
        pytest.param(
            "exec",
            'const r = await tools.exec_command({"cmd":"uv run pytest"}); text(r.output);',
            ("Tool: Bash", "Running uv run pytest"),
            id="exec_command",
        ),
        pytest.param(
            "exec",
            'await tools.web__run({"q":"codex sdk"})',
            ("Tool: WebSearch", 'Searching the web "codex sdk"'),
            id="web__run",
        ),
        pytest.param(
            "exec",
            'await tools.view_image({"path":"/home/user/diagram.png"})',
            ("Tool: ViewImage", "Viewing image diagram.png"),
            id="view_image",
        ),
        pytest.param(
            "exec",
            'await tools.write_stdin({"chars":"y\\n"})',
            ("Tool: WriteStdin", "Typing into terminal…"),
            id="write_stdin_no_target",
        ),
        pytest.param(
            "exec",
            'await tools.create_goal({"goal":"ship it"})',
            ("Tool: CreateGoal", "Setting a goal…"),
            id="create_goal",
        ),
        pytest.param(
            "exec",
            "await tools.get_goal({})",
            ("Tool: GetGoal", "Checking the goal…"),
            id="get_goal",
        ),
        pytest.param(
            "exec",
            "await tools.update_goal({})",
            ("Tool: UpdateGoal", "Updating the goal…"),
            id="update_goal",
        ),
        pytest.param("wait", "", ("Tool: Wait", "Waiting for code…"), id="wait_is_top_level"),
    ],
)
def test_codex_tool_labels(tool_name: str, input_preview: str, expected: tuple[str, str]) -> None:
    assert codex_tool_labels(tool_name, input_preview) == expected


def test_apply_patch_is_found_even_when_the_call_is_past_the_truncation() -> None:
    """apply_patch front-loads the patch body, pushing `tools.apply_patch(` out of the preview.

    The header sits at the START of the body, so it is detected directly -- otherwise
    every edit would read "Running code".
    """
    preview = 'const p = "*** Begin Patch\\n*** Update File: system/apps/system_interface/plugin.py\\n@@ -1'
    assert codex_tool_labels("exec", preview) == ("Tool: Edit", "Editing plugin.py")


@pytest.mark.parametrize(
    "input_preview",
    [
        pytest.param("const x = 1; text(x);", id="no_tools_call_at_all"),
        pytest.param('await tools.some_future_thing({"a":1})', id="unrecognised_function"),
        pytest.param("", id="empty_preview"),
    ],
)
def test_unrecognised_code_mode_falls_back_rather_than_guessing(input_preview: str) -> None:
    assert codex_tool_labels("exec", input_preview) == ("Tool: Code", "Running code")


@pytest.mark.parametrize(
    "tool_name",
    [
        pytest.param("update_plan", id="update_plan"),
        pytest.param("request_user_input", id="request_user_input"),
    ],
)
def test_prompt_banned_tools_are_deliberately_uncased(tool_name: str) -> None:
    """Both are forbidden by the codex prompt, so they get no label of their own.

    If one ever shows up in a transcript, the generic fallback IS the signal that
    the ban leaked -- a tailored caption would hide that.
    """
    header_label, caption_label = codex_tool_labels(tool_name, "{}")
    assert header_label == f"Tool: {tool_name}"
    assert caption_label == "Running tool…"


def test_mcp_tool_keeps_its_raw_name_in_the_header() -> None:
    """Matches how claude renders MCP calls, so the two harnesses read alike."""
    header_label, caption_label = codex_tool_labels("exec", "await tools.mcp__deepwiki__ask_question({})")
    assert header_label == "Tool: mcp__deepwiki__ask_question"
    assert caption_label == "Running ask question"


def test_a_long_command_is_shortened() -> None:
    _, caption_label = codex_tool_labels("exec", 'await tools.exec_command({"cmd":"%s"})' % ("x" * 200))
    assert caption_label.startswith("Running ")
    assert caption_label.endswith("…")
    assert len(caption_label) < 100
