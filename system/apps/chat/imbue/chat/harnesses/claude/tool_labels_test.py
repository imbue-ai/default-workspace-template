import pytest

from imbue.chat.harnesses.claude.tool_labels import action_note
from imbue.chat.harnesses.claude.tool_labels import action_parts
from imbue.chat.harnesses.claude.tool_labels import shell_command
from imbue.chat.harnesses.claude.tool_labels import tool_labels


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


@pytest.mark.parametrize(
    "tool_name, input_preview, expected",
    [
        # A file is NAMED on a chip, never pathed: the chip is a phrase to read,
        # and the whole path is one click away in its panel.
        pytest.param("Read", '{"file_path":"/home/user/ws/src/views/midnight.ts"}', ("read", "midnight.ts"), id="read"),
        pytest.param("Edit", '{"file_path":"a/b/plugin.py"}', ("edited", "plugin.py"), id="edit"),
        pytest.param("Write", '{"file_path":"notes.md"}', ("wrote", "notes.md"), id="write"),
        # A search names what it looked FOR first; the path is only the scope, and
        # "searched src/views" says nothing about what was being sought.
        pytest.param(
            "Grep", '{"pattern":"harness","path":"system/apps/chat/src"}', ("searched", '"harness" in src'), id="grep"
        ),
        pytest.param("Glob", '{"pattern":"**/*.ts"}', ("searched", '"**/*.ts"'), id="glob_without_scope"),
        pytest.param("Skill", '{"skill":"commit"}', ("loaded skill", "commit"), id="skill"),
        pytest.param("Monitor", "{}", ("monitored", ""), id="known_verb_without_target"),
        pytest.param("Agent", '{"description":"explore it"}', ("delegated", "explore it"), id="agent"),
        pytest.param("Task", "{}", ("delegated to a sub-agent", ""), id="delegation_without_a_description"),
        # No verb to offer, so the tool names itself -- the one case a chip still
        # says which tool ran, because nothing more useful can be said.
        pytest.param("Unheard", '{"file_path":"x.ts"}', ("Unheard", "x.ts"), id="unknown_tool"),
        pytest.param("mcp__notion__search", '{"query":"roadmap"}', ("called search", '"roadmap"'), id="mcp"),
        pytest.param("", "{}", ("ran a tool", ""), id="nameless"),
    ],
)
def test_claude_action_parts(tool_name: str, input_preview: str, expected: tuple[str, str]) -> None:
    assert action_parts(tool_name, input_preview) == expected


def test_a_shell_chip_shows_the_command_while_its_caption_shows_the_description() -> None:
    """The one tool where the chip's target and the strip's caption diverge, deliberately.

    The chip carries the description separately as its note, so repeating it as the
    target would make both halves of the chip say the same thing.
    """
    raw = '{"command":"uv run pytest -q","description":"Run the tests"}'
    assert action_parts("Bash", raw) == ("ran", "uv run pytest -q")
    assert action_note("Bash", raw) == "Run the tests"


@pytest.mark.parametrize(
    "tool_name, input_preview",
    [
        # Every tool that records no reason anywhere: the note is absent rather
        # than guessed at.
        pytest.param("Read", '{"file_path":"a.ts"}', id="read"),
        pytest.param("Edit", '{"file_path":"a.ts","description":"not a real field"}', id="edit_ignores_a_stray_key"),
        pytest.param("Bash", '{"command":"ls"}', id="shell_without_one"),
        pytest.param("Bash", '{"command":"ls","description":"   "}', id="blank"),
    ],
)
def test_claude_action_note_is_absent_rather_than_invented(tool_name: str, input_preview: str) -> None:
    assert action_note(tool_name, input_preview) is None
