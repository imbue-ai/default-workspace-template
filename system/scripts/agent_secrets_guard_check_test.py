"""Tests for the secret-file guard (policy P8).

A corpus of shell commands and file-tool payloads with the verdict each must get,
driven through the real wrapper so the prefilter and the checker are both exercised.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from conftest import _load_script_module

checker = _load_script_module(
    "agent_secrets_guard_check_for_tests", "agent_secrets_guard_check.py"
)

_SCRIPTS = Path(__file__).resolve().parent
_GUARD = _SCRIPTS / "agent_secrets_guard.sh"

_WRAPPER = (
    "python3 system/scripts/with_secrets.py data/.secrets/svc.env -- svc --port 8090"
)
_REQUEST = (
    "python3 .agents/skills/connect-external-service/scripts/request_secret.py "
    "--file svc --var SVC_TOKEN --rationale 'I need the key from data/.secrets/svc.env to call the API'"
)
# The shape build-app writes into a supervisord program: an OOM tag in front of a
# `bash -c` whose string chains the port registration with the wrapped entry point.
_SUPERVISORD = (
    'python3 system/services/oom_priority/bin/oom_tag_service.py user bash -c "python3 '
    "system/scripts/forward_port.py --manifest system/apps/svc/app.toml --url http://localhost:8090 && "
    'python3 system/scripts/with_secrets.py data/.secrets/svc.env -- svc"'
)

_ALLOWED_COMMANDS = [
    _WRAPPER,
    f"uv run {_WRAPPER.removeprefix('python3 ')}",
    f"uv run python3 {_WRAPPER.removeprefix('python3 ')}",
    "system/scripts/with_secrets.py data/.secrets/svc.env -- svc",
    "cd /home/user/workspace && " + _WRAPPER,
    "FOO=bar " + _WRAPPER,
    _REQUEST,
    _SUPERVISORD,
    "ls data/.secrets",
    "ls -la data/.secrets/",
    "rm data/.secrets/svc.env",
    "rm -f /home/user/workspace/data/.secrets/svc.env",
    "cat data/.secrets/README.md",
    # A command that never names the directory is not this guard's business.
    "cat data/.state/apps.toml",
    "echo secrets are in .secrets",
]

_BLOCKED_COMMANDS = [
    "cat data/.secrets/svc.env",
    "source data/.secrets/svc.env && svc",
    ". data/.secrets/svc.env",
    "sed -n 1p data/.secrets/svc.env",
    "grep TOKEN data/.secrets/svc.env",
    "cp data/.secrets/svc.env /tmp/x",
    "echo \"SVC_TOKEN='abc'\" > data/.secrets/svc.env",
    "printf 'A=1\\n' >> data/.secrets/svc.env",
    "python3 -c \"print(open('data/.secrets/svc.env').read())\"",
    "python3 -m json.tool data/.secrets/svc.env",
    "export $(cat data/.secrets/svc.env | xargs) && svc",
    "svc --env-file data/.secrets/svc.env",
    "cat /home/user/workspace/data/.secrets/svc.env",
    # The wrapper in front does not launder a read chained after it, nor one it
    # is asked to run.
    f"{_WRAPPER} && cat data/.secrets/svc.env",
    "python3 system/scripts/with_secrets.py data/.secrets/svc.env -- cat data/.secrets/svc.env",
    "python3 system/scripts/with_secrets.py data/.secrets/svc.env -- bash -c 'cat data/.secrets/svc.env'",
    # A shell string is judged by the same rule as a top-level command.
    'bash -c "cat data/.secrets/svc.env"',
    "python3 system/services/oom_priority/bin/oom_tag_service.py user bash -c 'cat data/.secrets/svc.env'",
    # A read inside an unparseable command is refused rather than waved through.
    "cat data/.secrets/svc.env 'unbalanced",
]


def _run_guard(payload: dict[str, object]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(_GUARD)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _bash(command: str) -> dict[str, object]:
    return {"tool_name": "Bash", "tool_input": {"command": command}}


def test_allowed_commands_pass() -> None:
    for command in _ALLOWED_COMMANDS:
        assert checker.classify_command(command) is None, (
            f"should be allowed: {command!r}"
        )


def test_blocked_commands_are_refused() -> None:
    for command in _BLOCKED_COMMANDS:
        assert checker.classify_command(command) is not None, (
            f"should be blocked: {command!r}"
        )


def test_the_wrapper_blocks_through_the_hook_with_the_reason_and_without_the_command() -> (
    None
):
    completed = _run_guard(_bash("cat data/.secrets/svc.env"))
    assert completed.returncode == 2
    assert "with_secrets.py" in completed.stderr
    # The command may carry a value, so the refusal never echoes it.
    assert "svc.env" not in completed.stderr


def test_the_wrapper_allows_the_supervisord_shape_through_the_hook() -> None:
    assert _run_guard(_bash(_SUPERVISORD)).returncode == 0


@pytest.mark.parametrize("tool_name", ["", "bash"])
def test_a_shell_call_under_another_harnesss_tool_name_is_judged_by_its_command(
    tool_name: str,
) -> None:
    assert (
        _run_guard(
            {
                "tool_name": tool_name,
                "tool_input": {"command": "cat data/.secrets/svc.env"},
            }
        ).returncode
        == 2
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"tool_name": "Read", "tool_input": {"file_path": "data/.secrets/svc.env"}},
        {
            "tool_name": "Read",
            "tool_input": {"file_path": "/home/user/workspace/data/.secrets/svc.env"},
        },
        {
            "tool_name": "Edit",
            "tool_input": {
                "file_path": "data/.secrets/svc.env",
                "old_string": "a",
                "new_string": "b",
            },
        },
        {
            "tool_name": "Write",
            "tool_input": {"file_path": "data/.secrets/new.env", "content": "X='1'"},
        },
        {
            "tool_name": "Grep",
            "tool_input": {"pattern": "TOKEN", "path": "data/.secrets"},
        },
        {"tool_name": "Glob", "tool_input": {"pattern": "data/.secrets/*.env"}},
        {
            "tool_name": "NotebookEdit",
            "tool_input": {"notebook_path": "data/.secrets/x.ipynb"},
        },
        # pi's spellings.
        {"tool_name": "read", "tool_input": {"path": "data/.secrets/svc.env"}},
        {
            "tool_name": "write",
            "tool_input": {"path": "data/.secrets/svc.env", "content": "X"},
        },
        {
            "tool_name": "find",
            "tool_input": {"pattern": "*.env", "path": "data/.secrets"},
        },
        # codex edits through a patch whose file lines name the target.
        {
            "tool_name": "apply_patch",
            "tool_input": {
                "command": "*** Begin Patch\n*** Update File: data/.secrets/svc.env\n+X='1'\n*** End Patch"
            },
        },
    ],
)
def test_file_tools_on_a_secret_file_are_refused(payload: dict[str, object]) -> None:
    completed = _run_guard(payload)
    assert completed.returncode == 2
    assert "svc.env" not in completed.stderr


@pytest.mark.parametrize(
    "payload",
    [
        {"tool_name": "Read", "tool_input": {"file_path": "data/.secrets/README.md"}},
        {"tool_name": "Read", "tool_input": {"file_path": "data/.state/apps.toml"}},
        # Grep's pattern is a regex, not a path: searching the code for the directory name is fine.
        {
            "tool_name": "Grep",
            "tool_input": {"pattern": "data/.secrets", "path": "system/"},
        },
        {
            "tool_name": "apply_patch",
            "tool_input": {
                "command": "*** Begin Patch\n*** Update File: README.md\n+see data/.secrets/\n*** End Patch"
            },
        },
        # pi's `ls` is the shell's `ls`.
        {"tool_name": "ls", "tool_input": {"path": "data/.secrets"}},
    ],
)
def test_file_tools_off_the_secret_files_pass(payload: dict[str, object]) -> None:
    assert _run_guard(payload).returncode == 0


def test_a_payload_that_never_names_the_directory_skips_the_checker_entirely() -> None:
    completed = _run_guard(_bash("cat README.md"))
    assert completed.returncode == 0
    assert completed.stderr == ""
