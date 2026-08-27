import pytest

from imbue.system_interface.harnesses.codex.tool_labels import CODE_MODE_TOOL_NAME
from imbue.system_interface.harnesses.codex.tool_labels import keeps_full_tool_input
from imbue.system_interface.harnesses.codex.tool_labels import shell_command
from imbue.system_interface.harnesses.codex.tool_labels import tool_labels
from imbue.system_interface.harnesses.tool_output import is_pure_tk_lifecycle_command


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
            'await tools.image_gen__imagegen({"prompt":"a cat wearing a hat"})',
            ("Tool: ImageGen", 'Generating an image "a cat wearing a hat"'),
            id="image_gen",
        ),
        pytest.param(
            "exec",
            'await tools.read_mcp_resource({"server":"codex_apps","uri":"file:///docs/api.md"})',
            ("Tool: ReadMcpResource", "Reading MCP resource file:///docs/api.md"),
            id="read_mcp_resource",
        ),
        pytest.param(
            "exec",
            'await tools.list_mcp_resources({"server":"codex_apps"})',
            ("Tool: ListMcpResources", "Listing MCP resources on codex_apps"),
            id="list_mcp_resources",
        ),
        pytest.param(
            "exec",
            'await tools.mcp__codex_apps__gmail_search_emails({"query":"is:unread"})',
            ("Tool: mcp__codex_apps__gmail_search_emails", "Running gmail search emails"),
            id="mcp_server_tool",
        ),
        pytest.param("wait", "", ("Tool: Wait", "Waiting for code…"), id="wait_is_top_level"),
    ],
)
def test_codex_tool_labels(tool_name: str, input_preview: str, expected: tuple[str, str]) -> None:
    assert tool_labels(tool_name, input_preview) == expected


@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        pytest.param("Add File: hello.txt", ("Tool: Write", "Creating hello.txt"), id="add"),
        pytest.param("Update File: a/b/plugin.py", ("Tool: Edit", "Editing plugin.py"), id="update"),
        pytest.param("Delete File: a/b/gone.txt", ("Tool: Delete", "Deleting gone.txt"), id="delete"),
    ],
)
def test_apply_patch_labels_name_the_operation(operation: str, expected: tuple[str, str]) -> None:
    """The verb comes from the patch header, so a create does not read as an edit."""
    preview = f"await tools.apply_patch(`*** Begin Patch\n*** {operation}\n*** End Patch`);"
    assert tool_labels("exec", preview) == expected


def test_apply_patch_is_gated_on_the_function_not_the_string() -> None:
    """A command that merely CONTAINS a patch header is not an edit.

    Searching the whole program for the header before knowing the function would label
    this grep as "Editing x" -- the tool call has to decide first.
    """
    preview = 'tools.exec_command({"cmd":"grep -n \'*** Add File: x\' notes.txt"})'
    header, caption = tool_labels("exec", preview)
    assert header == "Tool: Bash"
    assert caption.startswith("Running grep")


def test_apply_patch_is_found_even_when_the_call_is_past_the_truncation() -> None:
    """apply_patch front-loads the patch body, pushing `tools.apply_patch(` out of the preview.

    The header sits at the START of the body, so it is detected directly -- otherwise
    every edit would read "Running code".
    """
    preview = 'const p = "*** Begin Patch\\n*** Update File: system/apps/system_interface/plugin.py\\n@@ -1'
    assert tool_labels("exec", preview) == ("Tool: Edit", "Editing plugin.py")


@pytest.mark.parametrize(
    "input_preview",
    [
        pytest.param("const x = 1; text(x);", id="no_tools_call_at_all"),
        pytest.param("", id="empty_preview"),
    ],
)
def test_unparseable_code_mode_falls_back_rather_than_guessing(input_preview: str) -> None:
    """``Tool: Code`` means exactly one thing: no tools.<fn> call could be parsed."""
    assert tool_labels("exec", input_preview) == ("Tool: Code", "Running code")


@pytest.mark.parametrize(
    "function_name",
    [
        pytest.param("some_future_thing", id="tool_added_by_a_later_codex"),
        pytest.param("update_plan", id="banned_update_plan"),
        pytest.param("create_goal", id="banned_create_goal"),
        pytest.param("get_goal", id="banned_get_goal"),
        pytest.param("update_goal", id="banned_update_goal"),
    ],
)
def test_unrecognised_function_is_named_rather_than_hidden(function_name: str) -> None:
    """A parsed-but-untabled function keeps its name.

    This is what makes a prompt-banned tool (update_plan, the goal trio) visible in the
    UI the moment it leaks, and a stale table self-reporting -- collapsing these to
    "Tool: Code" would hide both.
    """
    header, caption = tool_labels("exec", f'await tools.{function_name}({{"a":1}})')
    assert header == f"Tool: {function_name}"
    assert caption == "Running tool…"


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
    header_label, caption_label = tool_labels(tool_name, "{}")
    assert header_label == f"Tool: {tool_name}"
    assert caption_label == "Running tool…"


def test_mcp_tool_keeps_its_raw_name_in_the_header() -> None:
    """Matches how claude renders MCP calls, so the two harnesses read alike."""
    header_label, caption_label = tool_labels("exec", "await tools.mcp__deepwiki__ask_question({})")
    assert header_label == "Tool: mcp__deepwiki__ask_question"
    assert caption_label == "Running ask question"


def test_a_long_command_is_shortened() -> None:
    _, caption_label = tool_labels("exec", 'await tools.exec_command({"cmd":"%s"})' % ("x" * 200))
    assert caption_label.startswith("Running ")
    assert caption_label.endswith("…")
    assert len(caption_label) < 100


def test_keeps_full_tool_input_for_patch_and_tk() -> None:
    """The diff view (patch body) and the step timeline (tk command) need the whole
    input, so those are exempt from the preview cap; ordinary calls are not."""
    # apply_patch body -- kept whole for the diff.
    assert keeps_full_tool_input("exec", "await tools.apply_patch(`*** Begin Patch\n*** Update File: a.py`)") is True
    # a patch front-loaded into a variable shows no visible call but carries the header.
    assert keeps_full_tool_input("exec", "*** Begin Patch\n*** Add File: b.py\n+x\n*** End Patch") is True
    # a tk lifecycle command -- kept whole for the step plan.
    assert keeps_full_tool_input("exec", 'await tools.exec_command({"cmd":"tk create --step foo"})') is True
    # an ordinary command is truncatable.
    assert keeps_full_tool_input("exec", 'await tools.exec_command({"cmd":"rg --files"})') is False
    # a tk mention inside another command's argument is not a lifecycle call.
    assert keeps_full_tool_input("exec", 'await tools.exec_command({"cmd":"echo run tk close s1 later"})') is False
    # non-exec tools are never exempt.
    assert keeps_full_tool_input("wait", "anything") is False


def test_keeps_full_tool_input_handles_escaped_quotes_in_cmd() -> None:
    """codex serialises a tk command's quoted title with escaped quotes; the exemption
    must still recognise it (a bare value regex clipped at the first \\" and failed)."""
    js = 'await tools.exec_command({"cmd":"tk create --step \\"Fix the parser\\""})'
    assert keeps_full_tool_input("exec", js) is True


def test_exec_caption_unescapes_the_command() -> None:
    """The exec caption reads the real (unescaped) command, not the raw \\"-laden JS."""
    _, caption = tool_labels("exec", 'await tools.exec_command({"cmd":"tk start \\"s1\\""})')
    assert caption.startswith("Running tk start")
    assert "\\" not in caption


def test_batched_tk_command_keeps_full_input_but_is_not_hidden() -> None:
    """The truncation exemption is segment-wise (deliberately broader) while the hide rule
    is start-anchored: `cd /code && tk create ...` renders as work AND keeps its full input
    for the step timeline's fallback."""
    long_titles = " ".join(f'--step "step number {i} with a long title"' for i in range(12))
    raw_input = f'await tools.exec_command({{"cmd": "cd /code && tk create {long_titles}"}});'
    assert len(raw_input) > 200
    assert keeps_full_tool_input("exec", raw_input) is True
    command = shell_command("exec", raw_input)
    assert command is not None
    assert is_pure_tk_lifecycle_command(command) is False


def test_shell_command_finds_the_command_inside_code_mode_js() -> None:
    """codex runs the shell from inside emitted JavaScript, so the command is an argument of
    an exec_command call rather than a tool input of its own."""
    js = 'tools.exec_command({ cmd: "tk start s1", workdir: "/code" })'
    assert shell_command(CODE_MODE_TOOL_NAME, js) == "tk start s1"
    assert shell_command("some_other_tool", js) is None
    assert shell_command(CODE_MODE_TOOL_NAME, 'tools.read_file({ path: "/x" })') is None


def test_keeps_full_tool_input_still_exempts_both_apply_patch_forms() -> None:
    """codex's file-body exemption keys off the INNER function, not the tool name, and has a
    second form where the patch was front-loaded into a variable so no call is visible in the
    preview. Neither can be expressed as a set of tool names -- writing this clause from the
    shared template would truncate every codex diff at the preview limit."""
    visible = 'tools.apply_patch({ patch: "*** Begin Patch\\n*** End Patch" })'
    assert keeps_full_tool_input(CODE_MODE_TOOL_NAME, visible) is True
    front_loaded = 'const p = "*** Begin Patch\\n*** Update File: a.py\\n*** End Patch";'
    assert keeps_full_tool_input(CODE_MODE_TOOL_NAME, front_loaded) is True


def test_keeps_full_tool_input_exempts_a_batched_tk_plan() -> None:
    js = 'tools.exec_command({ cmd: "cd /code && tk create --step \\"a\\"", workdir: "/code" })'
    assert keeps_full_tool_input(CODE_MODE_TOOL_NAME, js) is True
