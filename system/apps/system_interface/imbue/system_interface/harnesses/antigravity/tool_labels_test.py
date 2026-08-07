"""Unit tests for antigravity tool labels -- confirming the shared claude/codex vocabulary."""

from __future__ import annotations

from imbue.system_interface.harnesses.antigravity.tool_labels import keeps_full_tool_input
from imbue.system_interface.harnesses.antigravity.tool_labels import tool_labels


def test_run_command_reads_like_bash() -> None:
    header, caption = tool_labels("run_command", '{"CommandLine":"python3 showcase.py"}', "Running python3 showcase.py")
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
    header, caption = tool_labels("schedule", '{"Cron":"0 9 * * *"}', "Scheduling daily digest")
    assert header == "Tool: schedule"
    assert caption == "Scheduling daily digest"


def test_no_target_and_no_native_caption_uses_bare_verb() -> None:
    header, caption = tool_labels("run_command", "{}", "")
    assert header == "Tool: Bash"
    assert caption == "Running…"


def test_malformed_args_falls_back_to_native_caption() -> None:
    header, caption = tool_labels("run_command", "{not json", "Running something")
    assert caption == "Running something"


def test_keeps_full_input_for_file_bodies() -> None:
    assert keeps_full_tool_input("write_to_file", '{"CodeContent":"..."}') is True
    assert keeps_full_tool_input("multi_replace_file_content", "{}") is True
    assert keeps_full_tool_input("view_file", '{"AbsolutePath":"/x"}') is False


def test_keeps_full_input_for_tk_command() -> None:
    assert keeps_full_tool_input("run_command", '{"CommandLine":"tk create --step \\"plan\\""}') is True
    assert keeps_full_tool_input("run_command", '{"CommandLine":"ls -la"}') is False
