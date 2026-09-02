"""Unit tests for antigravity tool labels -- confirming the shared claude/codex vocabulary."""

from __future__ import annotations

from imbue.system_interface.harnesses.antigravity.tool_labels import shell_command
from imbue.system_interface.harnesses.antigravity.tool_labels import tool_labels


def test_run_command_reads_like_bash() -> None:
    header, caption = tool_labels(
        "run_command", '{"CommandLine":"python3 showcase.py"}', "Running python3 showcase.py"
    )
    assert header == "Tool: Bash"
    assert caption == "Running python3 showcase.py"


def test_edit_uses_basename_target() -> None:
    header, caption = tool_labels("replace_file_content", '{"TargetFile":"/home/user/showcase.py"}', "ignored")
    assert header == "Tool: Edit"
    assert caption == "Editing showcase.py"


def test_write_reads_like_write() -> None:
    header, caption = tool_labels("write_to_file", '{"TargetFile":"/a/b/notes.md","CodeContent":"..."}', "x")
    assert (header, caption) == ("Tool: Write", "Writing notes.md")


def test_grep_quotes_the_query() -> None:
    header, caption = tool_labels("grep_search", '{"Query":"magic"}', "Grep search showcase.py")
    assert header == "Tool: Grep"
    assert caption == 'Searching "magic"'


def test_list_dir_diverges_naturally() -> None:
    header, caption = tool_labels("list_dir", '{"DirectoryPath":"/home/user"}', "Listing directory /home/user")
    assert (header, caption) == ("Tool: List", "Listing user")


def test_web_search_matches_codex_phrase() -> None:
    # agy's real search_web uses a lowercase "query" key (grep_search uses "Query");
    # the label match is case-insensitive so both read the same.
    header, caption = tool_labels("search_web", '{"query":"gemini docs"}', "Web search")
    assert (header, caption) == ("Tool: WebSearch", 'Searching the web "gemini docs"')


def test_invoke_subagent_matches_claude_fixed_caption() -> None:
    header, caption = tool_labels("invoke_subagent", '{"Name":"researcher"}', "x")
    assert (header, caption) == ("Tool: Agent", "Delegating to sub-agent…")


def test_unmapped_agy_only_tool_falls_back_to_native_caption() -> None:
    """A tool agy ships that we have not mapped: the header names it and the caption is agy's
    own. Uses a deliberately fictional name -- agy's whole declared set is mapped, so a real
    one would stop exercising this path the moment the table caught up (which is exactly what
    happened to ``schedule``)."""
    header, caption = tool_labels("some_future_tool", '{"Whatever":"x"}', "Doing a new thing")
    assert header == "Tool: some_future_tool"
    assert caption == "Doing a new thing"


def test_the_rest_of_agys_declared_tools_are_mapped_to_shared_vocabulary() -> None:
    """Every tool agy declares has a header noun. Without these the header read
    "Tool: manage_task" and the caption fell through to the generic placeholder."""
    assert tool_labels("find_by_name", '{"Pattern":"*.py"}', "")[0] == "Tool: Glob"
    assert tool_labels("manage_task", '{"Action":"list"}', "")[0] == "Tool: Task"
    assert tool_labels("schedule", '{"Prompt":"digest"}', "")[0] == "Tool: Schedule"
    assert tool_labels("define_subagent", '{"name":"researcher"}', "")[0] == "Tool: Agent"
    assert tool_labels("manage_subagents", '{"Action":"list"}', "")[0] == "Tool: Agent"
    assert tool_labels("send_message", '{"Recipient":"conv-1"}', "")[0] == "Tool: Message"
    assert tool_labels("ask_question", "{}", "")[0] == "Tool: Question"


def test_no_target_and_no_native_caption_uses_bare_verb() -> None:
    header, caption = tool_labels("run_command", "{}", "")
    assert header == "Tool: Bash"
    assert caption == "Running…"


def test_malformed_args_falls_back_to_native_caption() -> None:
    header, caption = tool_labels("run_command", "{not json", "Running something")
    assert caption == "Running something"


def test_shell_command_reads_agys_command_key_and_ignores_other_tools() -> None:
    """The one question a harness answers for itself. Whether the command is tk is decided
    centrally, so this must not know anything about tk."""
    assert shell_command("run_command", '{"CommandLine":"ls -la"}') == "ls -la"
    assert shell_command("view_file", '{"AbsolutePath":"/x"}') is None
    assert shell_command("run_command", "not json") is None
