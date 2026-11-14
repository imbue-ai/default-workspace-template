"""The agents' resource notice hook prints the monitor's notice while it is fresh, and nothing otherwise."""

import json
import os
import subprocess
import time
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent / "agent_resource_notice.sh"
_NOTICE = "Workspace resource notice (from the workspace's resource monitor):\n- Disk space is low: 3.2 GB left.\n"


def _run(notice_file: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(_SCRIPT), *arguments],
        input="{}",
        env={**os.environ, "RESOURCE_AGENT_NOTICE_FILE": str(notice_file)},
        capture_output=True,
        text=True,
        check=True,
    )


def test_a_fresh_notice_is_printed_for_claude_and_wrapped_for_codex(
    tmp_path: Path,
) -> None:
    notice_file = tmp_path / "notice.txt"
    notice_file.write_text(_NOTICE)
    assert _run(notice_file).stdout == _NOTICE
    wrapped = json.loads(_run(notice_file, "--codex").stdout)
    assert wrapped == {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": _NOTICE.rstrip("\n"),
        }
    }


def test_no_notice_an_empty_one_and_one_a_dead_monitor_left_print_nothing(
    tmp_path: Path,
) -> None:
    notice_file = tmp_path / "notice.txt"
    assert _run(notice_file).stdout == ""
    notice_file.write_text("")
    assert _run(notice_file).stdout == ""
    notice_file.write_text(_NOTICE)
    ten_minutes_ago = time.time() - 600
    os.utime(notice_file, (ten_minutes_ago, ten_minutes_ago))
    assert _run(notice_file).stdout == ""
