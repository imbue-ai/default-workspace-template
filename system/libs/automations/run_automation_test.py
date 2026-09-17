"""Tests for run_automation.sh: the `mngr create` argv of an automation's first run, and the
messages a later run sends the existing agent.

The script is exercised as a real subprocess over a fake `uv` on PATH that records the
`mngr create` argv it is asked to run and answers `mngr list` with the ids a test scripts
(none by default, so a run is the first run and reaches the create), and over a fake
`system/scripts/message_chat.py` under the run's cwd that records the argv it is invoked with.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_SCRIPT = Path(__file__).parent / "run_automation.sh"

_FAKE_UV = """#!/bin/sh
# `uv run mngr <verb> ...`: record a create, answer a list with the scripted ids.
shift
shift
case "$1" in
  create) printf '%s\\n' "$@" >> "$RECORDED_CREATE" ;;
  list) [ -n "${LISTED_IDS:-}" ] && printf '%s\\n' "$LISTED_IDS" ;;
esac
exit 0
"""

_FAKE_MESSAGE_CHAT = """import json, os, sys
with open(os.environ["RECORDED_MESSAGES"], "a") as handle:
    handle.write(json.dumps(sys.argv[1:]) + "\\n")
"""


def _create_record(tmp_path: Path) -> Path:
    return tmp_path / "create.argv"


def _messages_record(tmp_path: Path) -> Path:
    return tmp_path / "messages.jsonl"


def _run(
    tmp_path: Path, *args: str, listed_ids: tuple[str, ...] = ()
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake_uv = bin_dir / "uv"
    fake_uv.write_text(_FAKE_UV)
    fake_uv.chmod(0o755)
    # The script resolves the workspace label with python3, and runs the messenger with it.
    (bin_dir / "python3").symlink_to(sys.executable)
    # The messenger is a repo-relative path, resolved against the run's cwd.
    fake_messenger = tmp_path / "system" / "scripts" / "message_chat.py"
    fake_messenger.parent.mkdir(parents=True)
    fake_messenger.write_text(_FAKE_MESSAGE_CHAT)
    return subprocess.run(
        ["bash", str(_SCRIPT), *args],
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "RECORDED_CREATE": str(_create_record(tmp_path)),
            "RECORDED_MESSAGES": str(_messages_record(tmp_path)),
            "LISTED_IDS": "\n".join(listed_ids),
        },
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
    )


def _recorded_create_argv(tmp_path: Path) -> list[str]:
    """The argv the run handed `mngr create`, or `[]` when it created nothing."""
    record = _create_record(tmp_path)
    return record.read_text().splitlines() if record.exists() else []


def _recorded_message_argvs(tmp_path: Path) -> list[list[str]]:
    """The argvs the run invoked the messenger with, in order, or `[]` when it sent nothing."""
    record = _messages_record(tmp_path)
    if not record.exists():
        return []
    return [json.loads(line) for line in record.read_text().splitlines()]


def _values_of(argv: list[str], flag: str) -> list[str]:
    return [argv[i + 1] for i, token in enumerate(argv) if token == flag]


def test_the_create_names_the_role_template_alone_and_no_harness(
    tmp_path: Path,
) -> None:
    """The harness and the account come from the workspace's create defaults, not from a template
    that stopped existing when harnesses moved to `--type`."""
    process = _run(tmp_path, "news")

    assert process.returncode == 0, process.stderr
    create_argv = _recorded_create_argv(tmp_path)
    assert create_argv[:2] == ["create", "news"]
    assert _values_of(create_argv, "--template") == ["automation"]
    assert "--type" not in create_argv
    assert _values_of(create_argv, "--label") == ["automation=news"]
    assert _values_of(create_argv, "--message") == ["/news"]
    assert _recorded_message_argvs(tmp_path) == []


def test_a_type_override_rides_the_create_and_a_template_override_replaces_the_role(
    tmp_path: Path,
) -> None:
    process = _run(tmp_path, "caretaker", "--template", "caretaker", "--type", "codex")

    assert process.returncode == 0, process.stderr
    create_argv = _recorded_create_argv(tmp_path)
    assert _values_of(create_argv, "--template") == ["caretaker"]
    assert _values_of(create_argv, "--type") == ["codex"]


def test_a_later_run_clears_and_retriggers_the_listed_agent_through_the_messenger(
    tmp_path: Path,
) -> None:
    """With an automation agent already listed, the run creates nothing and sends `/clear` then
    `/<skill>` to that agent's chat through `system/scripts/message_chat.py`, addressed by the
    id `mngr list` reported (the first one, should more than one exist)."""
    process = _run(
        tmp_path,
        "news",
        listed_ids=(
            "agent-0123456789abcdef0123456789abcdef",
            "agent-fedcba9876543210fedcba9876543210",
        ),
    )

    assert process.returncode == 0, process.stderr
    assert _recorded_create_argv(tmp_path) == []
    assert _recorded_message_argvs(tmp_path) == [
        ["agent-0123456789abcdef0123456789abcdef", "--message", "/clear"],
        ["agent-0123456789abcdef0123456789abcdef", "--message", "/news"],
    ]
