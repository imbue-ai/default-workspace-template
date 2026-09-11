"""Tests for ``parse_task_frontmatter.py``.

Run via: ``uv run pytest
.agents/shared/scripts/parse_task_frontmatter_test.py``
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parent / "parse_task_frontmatter.py"
_spec = importlib.util.spec_from_file_location("parse_task_frontmatter", _SCRIPT)
assert _spec is not None and _spec.loader is not None
parse_task_frontmatter = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(parse_task_frontmatter)


_VALID_FRONTMATTER = """---
lead_agent: crystallize-test
finish_report_path: data/.tasks/harden/update-foo/reports/report.md
---

# Task body
Some content here.
"""


def _write_task(tmp_path: Path, text: str) -> Path:
    task = tmp_path / "task.md"
    task.write_text(text)
    return task


def test_happy_path(tmp_path: Path) -> None:
    task = _write_task(tmp_path, _VALID_FRONTMATTER)
    result = parse_task_frontmatter.parse(task)
    assert result == {
        "lead_agent": "crystallize-test",
        "finish_report_path": "data/.tasks/harden/update-foo/reports/report.md",
    }


def test_render_shell_evalable(tmp_path: Path) -> None:
    task = _write_task(tmp_path, _VALID_FRONTMATTER)
    fields = parse_task_frontmatter.parse(task)
    rendered = parse_task_frontmatter._render(fields)
    assert "LEAD_AGENT=crystallize-test\n" in rendered
    assert (
        "FINISH_REPORT_PATH=data/.tasks/harden/update-foo/reports/report.md\n"
        in rendered
    )


def test_render_quotes_unsafe_values(tmp_path: Path) -> None:
    task = _write_task(
        tmp_path,
        """---
lead_agent: agent with spaces
finish_report_path: path/with$dollar/
---
body
""",
    )
    fields = parse_task_frontmatter.parse(task)
    rendered = parse_task_frontmatter._render(fields)
    # shlex.quote wraps values containing shell metachars in single quotes
    assert "LEAD_AGENT='agent with spaces'\n" in rendered
    assert "FINISH_REPORT_PATH='path/with$dollar/'\n" in rendered


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="task file not found"):
        parse_task_frontmatter.parse(tmp_path / "nope.md")


def test_a_glob_pattern_is_not_expanded(tmp_path: Path) -> None:
    """The positional argument is an exact path: a pattern that would once have
    resolved to a single file is now just a filename that does not exist.

    The worker is handed its task file's exact path in the message that
    launched it, so searching for it is not merely unnecessary -- with nested
    dispatch it is wrong, since two levels can hold files of the same name.
    """
    (tmp_path / "crystallize").mkdir()
    task = tmp_path / "crystallize" / "task.md"
    task.write_text(_VALID_FRONTMATTER)

    with pytest.raises(ValueError, match="task file not found"):
        parse_task_frontmatter.parse(tmp_path / "*" / "task.md")

    # ...and the same file, named exactly, parses.
    assert parse_task_frontmatter.parse(task)["lead_agent"] == "crystallize-test"


def test_no_frontmatter_delimiter(tmp_path: Path) -> None:
    task = _write_task(tmp_path, "just a plain markdown body\n")
    with pytest.raises(ValueError, match="must start with `---`"):
        parse_task_frontmatter.parse(task)


def test_unterminated_frontmatter(tmp_path: Path) -> None:
    task = _write_task(tmp_path, "---\nlead_agent: x\n")
    with pytest.raises(ValueError, match="not terminated"):
        parse_task_frontmatter.parse(task)


def test_frontmatter_not_mapping(tmp_path: Path) -> None:
    task = _write_task(tmp_path, "---\n- just\n- a\n- list\n---\nbody\n")
    with pytest.raises(ValueError, match="must be a YAML mapping"):
        parse_task_frontmatter.parse(task)


def test_invalid_yaml(tmp_path: Path) -> None:
    task = _write_task(tmp_path, "---\nlead_agent: [unbalanced\n---\nbody\n")
    with pytest.raises(ValueError, match="not valid YAML"):
        parse_task_frontmatter.parse(task)


def test_missing_finish_report_path_fails_loud(tmp_path: Path) -> None:
    task = _write_task(tmp_path, "---\nlead_agent: a\n---\nbody\n")
    with pytest.raises(ValueError, match="missing required field `finish_report_path`"):
        parse_task_frontmatter.parse(task)


def test_missing_lead_agent_warns_but_parses(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A task file without `lead_agent` (old launcher, newer flow) still parses.

    The worker then has no push address, so the parser warns and emits no
    LEAD_AGENT line -- the worker falls back to the same-repo delivery in
    worker-reporting.md instead of being structurally unable to report.
    """
    task = _write_task(tmp_path, "---\nfinish_report_path: b\n---\nbody\n")
    fields = parse_task_frontmatter.parse(task)
    assert fields == {"finish_report_path": "b"}
    assert "no `lead_agent`" in capsys.readouterr().err
    rendered = parse_task_frontmatter._render(fields)
    assert "LEAD_AGENT" not in rendered
    assert "FINISH_REPORT_PATH=b" in rendered


def test_task_file_is_parsed_and_emitted(tmp_path: Path) -> None:
    """The launcher-stamped `task_file` round-trips to the worker as TASK_FILE."""
    task = _write_task(
        tmp_path,
        """---
lead_agent: a
task_file: data/.tasks/harden/update-foo/task.md
finish_report_path: data/.tasks/harden/update-foo/reports/report.md
---
body
""",
    )
    fields = parse_task_frontmatter.parse(task)
    assert fields["task_file"] == "data/.tasks/harden/update-foo/task.md"
    rendered = parse_task_frontmatter._render(fields)
    assert "TASK_FILE=data/.tasks/harden/update-foo/task.md\n" in rendered


def test_missing_task_file_parses_without_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`task_file` absent is not a problem worth mentioning: unlike a missing
    address, it costs the worker nothing it did not already have (it was handed
    the path), so it must parse silently and emit no TASK_FILE line."""
    task = _write_task(
        tmp_path, "---\nlead_agent: a\nfinish_report_path: b\n---\nbody\n"
    )
    fields = parse_task_frontmatter.parse(task)
    assert fields == {"lead_agent": "a", "finish_report_path": "b"}
    assert capsys.readouterr().err == ""
    assert "TASK_FILE" not in parse_task_frontmatter._render(fields)


def test_task_file_wrong_type_fails_loud(tmp_path: Path) -> None:
    """Present-but-malformed is still an error: `task_file` is validated exactly
    like the required fields once it is there."""
    task = _write_task(
        tmp_path,
        """---
lead_agent: a
task_file: 7
finish_report_path: b
---
body
""",
    )
    with pytest.raises(ValueError, match="task_file must be a string, got int"):
        parse_task_frontmatter.parse(task)


def test_task_file_empty_fails_loud(tmp_path: Path) -> None:
    task = _write_task(
        tmp_path,
        """---
task_file: ""
finish_report_path: b
---
body
""",
    )
    with pytest.raises(ValueError, match="task_file must not be empty"):
        parse_task_frontmatter.parse(task)


def test_wrong_type_int(tmp_path: Path) -> None:
    task = _write_task(
        tmp_path,
        """---
lead_agent: 42
finish_report_path: b
---
body
""",
    )
    with pytest.raises(ValueError, match="lead_agent must be a string, got int"):
        parse_task_frontmatter.parse(task)


def test_wrong_type_list(tmp_path: Path) -> None:
    task = _write_task(
        tmp_path,
        """---
lead_agent: a
finish_report_path: [b, c]
---
body
""",
    )
    with pytest.raises(
        ValueError, match="finish_report_path must be a string, got list"
    ):
        parse_task_frontmatter.parse(task)


def test_empty_string(tmp_path: Path) -> None:
    task = _write_task(
        tmp_path,
        """---
lead_agent: a
finish_report_path: ""
---
body
""",
    )
    with pytest.raises(ValueError, match="finish_report_path must not be empty"):
        parse_task_frontmatter.parse(task)


def test_extra_string_keys_pass_through(tmp_path: Path) -> None:
    task = _write_task(
        tmp_path,
        """---
lead_agent: a
finish_report_path: b
ticket_id: task-42
flow: verify
---
body
""",
    )
    result = parse_task_frontmatter.parse(task)
    assert result == {
        "lead_agent": "a",
        "finish_report_path": "b",
        "ticket_id": "task-42",
        "flow": "verify",
    }


def test_non_string_extra_keys_are_dropped(tmp_path: Path) -> None:
    """Only string values survive -- lists / mappings / numbers don't eval cleanly."""
    task = _write_task(
        tmp_path,
        """---
lead_agent: a
finish_report_path: b
nested:
  x: 1
inputs:
  - commit.diff
  - commit.log
count: 3
---
body
""",
    )
    result = parse_task_frontmatter.parse(task)
    assert set(result.keys()) == {"lead_agent", "finish_report_path"}


def test_extra_key_with_invalid_shell_identifier_fails_loud(tmp_path: Path) -> None:
    """Keys with dashes (or other shell-illegal chars) must fail loud, not silently drop."""
    task = _write_task(
        tmp_path,
        """---
lead_agent: a
finish_report_path: b
staged-inputs: commit.diff
---
body
""",
    )
    with pytest.raises(ValueError, match=r"staged-inputs.*shell identifier"):
        parse_task_frontmatter.parse(task)


def test_extra_key_starting_with_digit_fails_loud(tmp_path: Path) -> None:
    """POSIX shell identifiers cannot begin with a digit."""
    task = _write_task(
        tmp_path,
        """---
lead_agent: a
finish_report_path: b
1st_input: commit.diff
---
body
""",
    )
    with pytest.raises(ValueError, match=r"1st_input.*shell identifier"):
        parse_task_frontmatter.parse(task)


def test_render_orders_known_fields_first_then_extras_alphabetized(
    tmp_path: Path,
) -> None:
    """The well-known fields keep a fixed order regardless of how the task file
    happens to spell them out, so a worker's `eval` of this output reads the
    same shape every time."""
    task = _write_task(
        tmp_path,
        """---
ticket_id: task-42
finish_report_path: b
flow: verify
task_file: t/task.md
lead_agent: a
---
body
""",
    )
    fields = parse_task_frontmatter.parse(task)
    rendered = parse_task_frontmatter._render(fields)
    assert rendered == (
        "LEAD_AGENT=a\nTASK_FILE=t/task.md\nFINISH_REPORT_PATH=b\n"
        "FLOW=verify\nTICKET_ID=task-42\n"
    )


def test_cli_prints_shell_evalable_lines_for_an_exact_path(tmp_path: Path) -> None:
    """End to end through the command line the worker actually runs: the
    positional is one exact path, and stdout is what a worker's `eval` consumes."""
    task = _write_task(
        tmp_path,
        """---
lead_agent: outer-worker
task_file: data/.tasks/launch-task/inner/task.md
finish_report_path: data/.tasks/launch-task/inner/reports/report.md
---
body
""",
    )
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), str(task)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == (
        "LEAD_AGENT=outer-worker\n"
        "TASK_FILE=data/.tasks/launch-task/inner/task.md\n"
        "FINISH_REPORT_PATH=data/.tasks/launch-task/inner/reports/report.md\n"
    )


def test_cli_reports_a_missing_path_and_exits_nonzero(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), str(tmp_path / "nope.md")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert "task file not found" in result.stderr
