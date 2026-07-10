"""Unit tests for pr_review.agent_stream (the shared stream-event renderer).

These cover only the pure rendering of ``claude -p`` stream-json events into log
lines; the subprocess-spawning ``run_streaming_agent`` is exercised by hand / in
the release check (it launches a real agent).
"""

from pr_review import agent_stream


def test_render_stream_event_logs_bash_command() -> None:
    ev = {"type": "assistant", "message": {"content": [
        {"type": "thinking", "thinking": "hmm"},
        {"type": "tool_use", "name": "Bash", "input": {"command": "grep -rn widget", "description": "Search"}},
    ]}}
    assert agent_stream.render_stream_event(ev) == ["$ grep -rn widget"]


def test_render_stream_event_logs_assistant_text() -> None:
    ev = {"type": "assistant", "message": {"content": [{"type": "text", "text": "Reading the file now."}]}}
    assert agent_stream.render_stream_event(ev) == ["● Reading the file now."]


def test_render_stream_event_logs_other_tool_call() -> None:
    ev = {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Read", "input": {"file_path": "main.py"}},
    ]}}
    assert agent_stream.render_stream_event(ev) == ["» Read main.py"]


def test_render_stream_event_logs_tool_output_tail() -> None:
    ev = {"type": "user", "tool_use_result": {"stdout": "line1\nline2\nline3", "stderr": ""}}
    assert agent_stream.render_stream_event(ev) == ["line1", "line2", "line3"]


def test_render_stream_event_ignores_noise() -> None:
    assert agent_stream.render_stream_event({"type": "system", "subtype": "init"}) == []
    assert agent_stream.render_stream_event({"type": "result", "result": "done"}) == []


def test_first_line_truncates() -> None:
    assert agent_stream.first_line("hello\nworld", 100) == "hello"
    assert agent_stream.first_line("x" * 10, 4) == "xxxx …"
    assert agent_stream.first_line("   ", 10) == ""


def test_tail_lines_keeps_last_nonblank() -> None:
    text = "a\n\nb\nc\n"
    assert agent_stream.tail_lines(text, 2, 100) == ["b", "c"]
