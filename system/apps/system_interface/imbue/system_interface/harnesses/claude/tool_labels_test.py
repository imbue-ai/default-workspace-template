import pytest

from imbue.system_interface.harnesses.claude.tool_labels import keeps_full_tool_input
from imbue.system_interface.harnesses.claude.tool_labels import shell_command
from imbue.system_interface.harnesses.claude.tool_labels import tool_labels


@pytest.mark.parametrize(
    "tool_name, input_preview, expected",
    [
        pytest.param("Read", '{"file_path":"src/midnight.ts"}', ("Tool: Read", "Reading midnight.ts"), id="read"),
        pytest.param("Edit", '{"file_path":"a/b/plugin.py"}', ("Tool: Edit", "Editing plugin.py"), id="edit"),
        # The header keeps the real tool name while the caption collapses to the
        # same verb as Edit -- the case the two-field split exists for.
        pytest.param(
            "MultiEdit", '{"file_path":"plugin.py"}', ("Tool: MultiEdit", "Editing plugin.py"), id="multi_edit"
        ),
        pytest.param("Write", '{"file_path":"notes.md"}', ("Tool: Write", "Writing notes.md"), id="write"),
        pytest.param("Grep", '{"pattern":"harness"}', ("Tool: Grep", 'Searching "harness"'), id="grep_is_quoted"),
        pytest.param(
            "WebSearch", '{"query":"codex sdk"}', ("Tool: WebSearch", 'Searching the web "codex sdk"'), id="web_search"
        ),
        pytest.param("Skill", '{"skill":"commit"}', ("Tool: Skill", "Loading skill commit"), id="skill"),
        pytest.param("Monitor", "{}", ("Tool: Monitor", "Monitoring…"), id="known_verb_without_target"),
        pytest.param("Agent", '{"description":"go"}', ("Tool: Agent", "Delegating to sub-agent…"), id="agent"),
        pytest.param("Task", "{}", ("Tool: Task", "Delegating to sub-agent…"), id="task"),
    ],
)
def test_claude_tool_labels(tool_name: str, input_preview: str, expected: tuple[str, str]) -> None:
    assert tool_labels(tool_name, input_preview) == expected


def test_bash_prefers_the_agents_description_over_the_raw_command() -> None:
    """The description says what the command is FOR; the command may be clipped mid-word."""
    _, caption_label = tool_labels("Bash", '{"command":"uv run pytest -q","description":"Run the tests"}')
    assert caption_label == "Running Run the tests"


def test_bash_falls_back_to_the_command_when_undescribed() -> None:
    _, caption_label = tool_labels("Bash", '{"command":"ls -la"}')
    assert caption_label == "Running ls -la"


def test_target_key_order_is_load_bearing() -> None:
    """A WebFetch carries both url and description; the url is the better target."""
    _, caption_label = tool_labels("WebFetch", '{"url":"https://example.com","description":"read it"}')
    assert caption_label == "Fetching page https://example.com"


@pytest.mark.parametrize(
    "input_preview",
    [
        pytest.param('{"file_path":"a.ts"', id="truncated_json"),
        pytest.param("", id="empty"),
        pytest.param("[1,2,3]", id="not_an_object"),
    ],
)
def test_an_unparseable_preview_degrades_to_the_bare_verb(input_preview: str) -> None:
    """Previews are clipped at a fixed length, so invalid JSON is expected, not exceptional."""
    assert tool_labels("Read", input_preview) == ("Tool: Read", "Reading…")


def test_unknown_tool_with_a_target_still_says_something_useful() -> None:
    assert tool_labels("SomeNewTool", '{"path":"x/y.txt"}') == ("Tool: SomeNewTool", "Running y.txt")


def test_unknown_tool_without_a_target_is_generic() -> None:
    assert tool_labels("SomeNewTool", "{}") == ("Tool: SomeNewTool", "Running tool…")


def test_mcp_tool_keeps_its_raw_name_in_the_header() -> None:
    header_label, caption_label = tool_labels("mcp__deepwiki__ask_question", "{}")
    assert header_label == "Tool: mcp__deepwiki__ask_question"
    assert caption_label == "Running ask question"


def test_mcp_server_name_may_itself_contain_the_separator() -> None:
    """Split on the LAST separator, so a compound server name does not eat the tool."""
    _, caption_label = tool_labels("mcp__plugin_playwright_playwright__browser_click", "{}")
    assert caption_label == "Running browser click"


def test_shell_command_reads_claudes_command_key_and_ignores_other_tools() -> None:
    assert shell_command("Bash", '{"command":"ls -la"}') == "ls -la"
    assert shell_command("Read", '{"file_path":"/x"}') is None
    assert shell_command("Bash", '{"command":123}') is None


def test_keeps_full_tool_input_exempts_a_batched_tk_plan() -> None:
    """Moved out of session_parser, which no longer knows claude's tool names. A batched
    create must survive truncation even though it renders as work rather than hiding."""
    assert keeps_full_tool_input("Bash", '{"command":"cd /code && tk create --step \\"a\\""}') is True
    assert keeps_full_tool_input("Bash", '{"command":"ls -la"}') is False
    assert keeps_full_tool_input("Read", '{"file_path":"/x"}') is False
