"""Tests for agent_grok_policy_guards.py, grok's PreToolUse and UserPromptSubmit hook.

The hook runs as grok runs it: a subprocess handed a grok payload on stdin, against the real guard
scripts beside it. The payloads start from envelopes grok 1.0.62 sent its hooks (captured against a
stub model, paths rewritten to the workspace's), with the tool fields set per test. Routing and
fail-open tests run a copy of the hook beside stand-in guards, so they can see which guard was
started and with what.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, NamedTuple

import pytest
from oom_priority import bands
from script_modules_testing import load_script_module

# Every test starts the hook, which starts bash guards and their python checkers; that is well
# under a second warm, but the suite's 10 s default is for pure Python.
pytestmark = pytest.mark.timeout(60)
_SUBPROCESS_TIMEOUT_SECONDS = 30

_SCRIPTS_DIR = Path(__file__).parent
_HOOK = _SCRIPTS_DIR / "agent_grok_policy_guards.py"
hook = load_script_module(
    "agent_grok_policy_guards_for_tests", "agent_grok_policy_guards.py"
)

_ROOT_SESSION = "01a156da-57b6-7c61-9b3e-59f3b802c4f5"
_CHILD_SESSION = "01a156d7-6157-7851-87b0-49868470aa97"
_TRANSCRIPT = f"/home/user/.grok/sessions/%2Fhome%2Fuser%2Fworkspace/{_ROOT_SESSION}/updates.jsonl"

# grok's PreToolUse for a backgrounded `run_terminal_command`.
_CAPTURED_PRE_TOOL_USE: dict[str, Any] = {
    "hookEventName": "pre_tool_use",
    "sessionId": _ROOT_SESSION,
    "cwd": "/home/user/workspace",
    "workspaceRoot": "/home/user/workspace",
    "timestamp": "2026-10-20T01:08:34.320874+00:00",
    "transcriptPath": _TRANSCRIPT,
    "permissionMode": "bypassPermissions",
    "toolName": "run_terminal_command",
    "toolUseId": "call_stub_2",
    "toolInput": {
        "command": "sleep 8; echo bg-done > bg_done.txt",
        "description": "background sleep",
        "block_until_ms": 0,
    },
    "toolInputTruncated": False,
    "hook_event_name": "PreToolUse",
    "session_id": _ROOT_SESSION,
    "transcript_path": _TRANSCRIPT,
    "permission_mode": "bypassPermissions",
    "tool_name": "run_terminal_command",
    "tool_input": {
        "command": "sleep 8; echo bg-done > bg_done.txt",
        "description": "background sleep",
        "block_until_ms": 0,
    },
    "tool_use_id": "call_stub_2",
}
# grok's UserPromptSubmit for a prompt the user typed into the root session.
_CAPTURED_ROOT_PROMPT: dict[str, Any] = {
    "hookEventName": "user_prompt_submit",
    "sessionId": _ROOT_SESSION,
    "cwd": "/home/user/workspace",
    "workspaceRoot": "/home/user/workspace",
    "timestamp": "2026-10-20T01:07:13.708454+00:00",
    "transcriptPath": _TRANSCRIPT,
    "promptId": "5afcb831-3a33-4520-b719-73a13ea3adcd",
    "permissionMode": "bypassPermissions",
    "prompt": "TOOLTEST please",
    "hook_event_name": "UserPromptSubmit",
    "session_id": _ROOT_SESSION,
    "transcript_path": _TRANSCRIPT,
    "permission_mode": "bypassPermissions",
}
# grok's UserPromptSubmit for a subagent's own session.
_CAPTURED_CHILD_PROMPT: dict[str, Any] = {
    "hookEventName": "user_prompt_submit",
    "sessionId": _CHILD_SESSION,
    "cwd": "/home/user/workspace",
    "workspaceRoot": "/home/user/workspace",
    "timestamp": "2026-10-20T01:05:18.294847+00:00",
    "promptId": "01a156d7-61b5-7341-9fbd-3f48b9133f31",
    "permissionMode": "bypassPermissions",
    "prompt": "say hi from the subagent",
    "subagentType": "general-purpose",
    "hook_event_name": "UserPromptSubmit",
    "session_id": _CHILD_SESSION,
    "permission_mode": "bypassPermissions",
}

_HOST = "http://latchkey-self.invalid/permission-requests"
_REQUEST = f"latchkey curl -XPOST {_HOST} -H 'Content-Type: application/json' -d '{{\"agent_id\": \"a1\"}}'"
_OOM_PREFIX = bands.oom_tag_shell_prefix(bands.AGENT_SUBPROCESS)

# A `ticket` whose `steps` output the environment drives, for the step reminders: it lists
# $STUB_STEPS (or, with --status=in_progress, $STUB_INPROGRESS) and exits 2 when that is empty.
_STUB_TICKET = """#!/usr/bin/env bash
if [[ "$1" == "steps" && "${2:-}" == "--status=in_progress" ]]; then
  printf '%s' "${STUB_INPROGRESS:-}"
  [[ -n "${STUB_INPROGRESS:-}" ]] && exit 0 || exit 2
fi
if [[ "$1" == "steps" ]]; then
  printf '%s' "${STUB_STEPS:-}"
  [[ -n "${STUB_STEPS:-}" ]] && exit 0 || exit 2
fi
exit 0
"""
# A guard stand-in that appends its stdin to $GUARD_RECORD as one JSON line naming itself, then
# exits with $GUARD_EXIT (default 0), writing $GUARD_STDERR to stderr.
_RECORDING_GUARD = """#!/usr/bin/env bash
payload=$(cat)
printf '{"script": "%s", "payload": %s}\\n' "$(basename "$0")" "$payload" >> "$GUARD_RECORD"
printf '%s' "${GUARD_STDERR:-}" >&2
exit "${GUARD_EXIT:-0}"
"""


class HookRun(NamedTuple):
    exit_code: int
    stdout: str
    stderr: str

    @property
    def hook_output(self) -> dict[str, Any] | None:
        return json.loads(self.stdout)["hookSpecificOutput"] if self.stdout else None

    @property
    def deny_reason(self) -> str | None:
        output = self.hook_output
        if output is None or output.get("permissionDecision") != "deny":
            return None
        return output["permissionDecisionReason"]

    @property
    def updated_input(self) -> dict[str, Any] | None:
        output = self.hook_output
        return None if output is None else output.get("updatedInput")

    @property
    def additional_context(self) -> str | None:
        output = self.hook_output
        return None if output is None else output.get("additionalContext")


def _pre_tool_use(
    tool_name: str,
    tool_input: dict[str, Any],
    *,
    session_id: str = _ROOT_SESSION,
    is_truncated: bool = False,
) -> dict[str, Any]:
    return {
        **_CAPTURED_PRE_TOOL_USE,
        "sessionId": session_id,
        "session_id": session_id,
        "toolName": tool_name,
        "tool_name": tool_name,
        "toolInput": tool_input,
        "tool_input": tool_input,
        "toolInputTruncated": is_truncated,
    }


def _shell(command: str, **extra: Any) -> dict[str, Any]:
    return _pre_tool_use(
        "run_terminal_command", {"command": command, "description": "a probe", **extra}
    )


def _environment(tmp_path: Path, extra: dict[str, str] | None) -> dict[str, str]:
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "MNGR_AGENT_STATE_DIR": str(state_dir),
        "TICKETS_DIR": str(tmp_path / "no-tickets"),
        **(extra or {}),
    }


def _run_hook(
    tmp_path: Path,
    command: str,
    payload: dict[str, Any] | str,
    *,
    env: dict[str, str] | None = None,
    hook_path: Path = _HOOK,
) -> HookRun:
    result = subprocess.run(
        [sys.executable, str(hook_path), command],
        input=payload if isinstance(payload, str) else json.dumps(payload),
        capture_output=True,
        text=True,
        env=_environment(tmp_path, env),
        cwd=tmp_path,
        timeout=_SUBPROCESS_TIMEOUT_SECONDS,
    )
    return HookRun(
        exit_code=result.returncode, stdout=result.stdout, stderr=result.stderr
    )


def _pre(
    tmp_path: Path, payload: dict[str, Any], env: dict[str, str] | None = None
) -> HookRun:
    return _run_hook(tmp_path, hook.PRE_TOOL_USE_COMMAND, payload, env=env)


def _log(tmp_path: Path) -> str:
    return (tmp_path / "state" / hook.LOG_FILE_NAME).read_text()


def _assert_denied(run: HookRun, expected_in_reason: str) -> None:
    assert run.exit_code == 2
    assert run.deny_reason is not None and expected_in_reason in run.deny_reason
    assert run.stderr == run.deny_reason + "\n"
    assert run.updated_input is None


def _tk_work_dir(tmp_path: Path) -> Path:
    """A work dir holding the stand-in ``ticket`` and its tickets dir."""
    work_dir = tmp_path / "work"
    tk_dir = work_dir / "system" / "vendor" / "tk"
    tk_dir.mkdir(parents=True)
    ticket = tk_dir / "ticket"
    ticket.write_text(_STUB_TICKET)
    ticket.chmod(0o755)
    (work_dir / ".tickets").mkdir()
    return work_dir


def _step_env(work_dir: Path, *, steps: str, in_progress: str) -> dict[str, str]:
    return {
        "MNGR_AGENT_WORK_DIR": str(work_dir),
        "TICKETS_DIR": str(work_dir / ".tickets"),
        "STUB_STEPS": steps,
        "STUB_INPROGRESS": in_progress,
    }


def _hook_beside_stand_ins(tmp_path: Path, guards: dict[str, str]) -> Path:
    """A copy of the hook in a scripts dir of its own, beside ``guards`` (script name to source)."""
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    copied = scripts_dir / _HOOK.name
    shutil.copy(_HOOK, copied)
    for name, source in guards.items():
        (scripts_dir / name).write_text(source)
    return copied


def _recorded(record: Path) -> list[dict[str, Any]]:
    return (
        [json.loads(line) for line in record.read_text().splitlines()]
        if record.exists()
        else []
    )


@pytest.mark.parametrize("tool_name", ["run_terminal_command", "monitor"])
@pytest.mark.parametrize(
    ("command", "expected_in_reason"),
    [
        pytest.param(
            "git rebase -i HEAD~2",
            "git rebase commands are not allowed",
            id="history-rewrite",
        ),
        pytest.param(
            "git commit --amend -m x",
            "--amend or --fixup is not allowed",
            id="commit-amend",
        ),
        pytest.param(
            "pytest | " + "head -5",
            "Do not pipe commands through tail or head",
            id="pipe-into-head",
        ),
        pytest.param(
            "cd /tmp && tk start wor-1", "runs before it", id="chained-tk-start"
        ),
        pytest.param(
            "cat data/.secrets/svc.env",
            "read only by with_secrets.py",
            id="secret-file-read",
        ),
    ],
)
def test_a_shell_call_a_guard_refuses_is_denied_with_the_guards_whole_reason(
    tmp_path: Path, tool_name: str, command: str, expected_in_reason: str
) -> None:
    payload = _pre_tool_use(tool_name, {"command": command, "description": "a probe"})
    _assert_denied(_pre(tmp_path, payload), expected_in_reason)


def test_a_multi_line_refusal_reaches_grok_whole_in_the_json_reason(
    tmp_path: Path,
) -> None:
    """grok shows the model only the first stderr line of a plain exit 2, so the JSON carries it all."""
    run = _pre(tmp_path, _shell("cat data/.secrets/svc.env"))
    assert run.deny_reason is not None
    assert "\n\n" in run.deny_reason
    assert "with_secrets.py data/.secrets/<name>.env -- <command...>" in run.deny_reason


def test_an_allowed_shell_call_is_rewritten_once_keeping_its_other_fields(
    tmp_path: Path,
) -> None:
    run = _pre(tmp_path, _shell("pytest -q", block_until_ms=20000))
    assert run.exit_code == 0
    assert run.updated_input == {
        "command": _OOM_PREFIX + "pytest -q",
        "description": "a probe",
        "block_until_ms": 20000,
    }
    assert run.additional_context is None


def test_an_allowed_monitor_is_rewritten_keeping_its_other_fields(
    tmp_path: Path,
) -> None:
    tool_input = {
        "command": "tail -f build.log",
        "description": "watch the build",
        "timeout_ms": 60000,
        "persistent": False,
    }
    run = _pre(tmp_path, _pre_tool_use("monitor", tool_input))
    assert run.exit_code == 0
    assert run.updated_input == {
        **tool_input,
        "command": _OOM_PREFIX + "tail -f build.log",
    }


def test_the_prefix_carries_the_agents_git_identity(tmp_path: Path) -> None:
    host_dir = tmp_path / "host"
    host_dir.mkdir()
    (host_dir / "data.json").write_text(json.dumps({"host_id": "host-9"}))
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "data.json").write_text(json.dumps({"name": "grok-agent"}))
    run = _pre(
        tmp_path,
        _shell("tk start wor-1"),
        env={"MNGR_AGENT_ID": "agent-x", "MNGR_HOST_DIR": str(host_dir)},
    )
    assert run.exit_code == 0
    assert run.updated_input is not None
    prefix = run.updated_input["command"].removesuffix("tk start wor-1")
    assert "GIT_AUTHOR_NAME=grok-agent" in prefix
    assert "GIT_AUTHOR_EMAIL=agent-x@host-9" in prefix
    assert prefix.endswith(_OOM_PREFIX)


@pytest.mark.parametrize(
    "background_arguments",
    [
        pytest.param({"block_until_ms": 0}, id="block_until_ms-0"),
        pytest.param({"is_background": True}, id="is_background"),
        pytest.param({"timeout": 0}, id="timeout-0"),
        pytest.param(
            {"block_until_ms": None, "is_background": True},
            id="null-block_until_ms-is-absent",
        ),
    ],
)
def test_a_permission_request_filed_from_a_backgrounded_call_is_refused(
    tmp_path: Path, background_arguments: dict[str, Any]
) -> None:
    _assert_denied(
        _pre(tmp_path, _shell(_REQUEST, **background_arguments)),
        "runs in the background",
    )


@pytest.mark.parametrize(
    "foreground_arguments",
    [
        pytest.param({}, id="no-background-argument"),
        pytest.param(
            {"block_until_ms": 5000, "is_background": True},
            id="block_until_ms-wins-over-is_background",
        ),
        pytest.param(
            {"block_until_ms": 5000, "timeout": 0},
            id="block_until_ms-wins-over-timeout",
        ),
        pytest.param({"run_in_background": True}, id="claudes-flag-is-not-groks"),
    ],
)
def test_a_permission_request_grok_runs_in_the_foreground_is_allowed(
    tmp_path: Path, foreground_arguments: dict[str, Any]
) -> None:
    run = _pre(tmp_path, _shell(_REQUEST, **foreground_arguments))
    assert run.exit_code == 0
    assert run.updated_input is not None and run.updated_input["command"].endswith(
        _REQUEST
    )


def test_a_permission_request_filed_through_a_monitor_is_refused(
    tmp_path: Path,
) -> None:
    payload = _pre_tool_use(
        "monitor", {"command": _REQUEST, "description": "file a request"}
    )
    _assert_denied(_pre(tmp_path, payload), "runs in the background")


@pytest.mark.parametrize(
    ("tool_name", "tool_input"),
    [
        pytest.param(
            "write",
            {"file_path": "data/.secrets/svc.env", "content": "A=1"},
            id="write",
        ),
        pytest.param("grep", {"pattern": "TOKEN", "path": "data/.secrets"}, id="grep"),
    ],
)
def test_a_file_tool_on_a_secret_file_is_refused(
    tmp_path: Path, tool_name: str, tool_input: dict[str, Any]
) -> None:
    _assert_denied(
        _pre(tmp_path, _pre_tool_use(tool_name, tool_input)),
        "read only by with_secrets.py",
    )


@pytest.mark.parametrize(
    ("tool_name", "tool_input"),
    [
        pytest.param(
            "write",
            {
                "file_path": "notes.md",
                "content": "run pytest | head -5, then git rebase -i",
            },
            id="write",
        ),
        pytest.param(
            "search_replace",
            {
                "file_path": "README.md",
                "old_string": "x",
                "new_string": "cd /tmp && tk start a; git rebase -i HEAD~2",
            },
            id="search_replace",
        ),
        pytest.param(
            "write",
            {"file_path": "data/.secrets/README.md", "content": "notes"},
            id="secrets-readme",
        ),
    ],
)
def test_a_file_tool_whose_content_looks_like_a_refused_command_is_allowed_unrewritten(
    tmp_path: Path, tool_name: str, tool_input: dict[str, Any]
) -> None:
    run = _pre(tmp_path, _pre_tool_use(tool_name, tool_input))
    assert run.exit_code == 0
    assert run.stdout == ""


@pytest.mark.parametrize(
    ("tool_name", "tool_input"),
    [
        pytest.param(
            "read_file", {"target_file": "data/.secrets/svc.env"}, id="read_file"
        ),
        pytest.param("list_dir", {"target_directory": "data/.secrets"}, id="list_dir"),
        pytest.param(
            "search_replace",
            {
                "file_path": "data/.secrets/svc.env",
                "old_string": "a",
                "new_string": f"git rebase -i | tail -3; tk start x; {_REQUEST}",
            },
            id="search_replace-whose-text-every-shell-prefilter-matches",
        ),
    ],
)
def test_a_non_shell_tool_reaches_only_the_guards_for_every_tool_under_its_grok_name(
    tmp_path: Path, tool_name: str, tool_input: dict[str, Any]
) -> None:
    guards = {
        name: _RECORDING_GUARD
        for name in hook.PRETOOLUSE_SCRIPTS
        if name.endswith(".sh")
    }
    copied = _hook_beside_stand_ins(tmp_path, guards)
    record = tmp_path / "record.jsonl"
    run = _run_hook(
        tmp_path,
        hook.PRE_TOOL_USE_COMMAND,
        _pre_tool_use(tool_name, tool_input),
        env={"GUARD_RECORD": str(record)},
        hook_path=copied,
    )
    assert run.exit_code == 0
    expected_payload = {"tool_name": tool_name, "tool_input": tool_input}
    assert _recorded(record) == [
        {"script": "agent_secrets_guard.sh", "payload": expected_payload},
        {"script": hook.STEP_NUDGE_SCRIPT, "payload": expected_payload},
    ]


def test_a_shell_call_reaches_the_guards_as_claudes_bash_and_only_those_its_text_could_trip(
    tmp_path: Path,
) -> None:
    guards = {
        name: _RECORDING_GUARD
        for name in hook.PRETOOLUSE_SCRIPTS
        if name.endswith(".sh")
    }
    copied = _hook_beside_stand_ins(tmp_path, guards)
    record = tmp_path / "record.jsonl"
    command = "git status | " + "tail -3"
    run = _run_hook(
        tmp_path,
        hook.PRE_TOOL_USE_COMMAND,
        _shell(command, block_until_ms=0),
        env={"GUARD_RECORD": str(record)},
        hook_path=copied,
    )
    assert run.exit_code == 0
    expected_payload = {
        "tool_name": "Bash",
        "tool_input": {"command": command, "run_in_background": True},
    }
    assert _recorded(record) == [
        {"script": "agent_prevent_commit_rewrite.sh", "payload": expected_payload},
        {"script": "agent_block_pipe_tail_head.sh", "payload": expected_payload},
        {"script": hook.STEP_NUDGE_SCRIPT, "payload": expected_payload},
    ]


def test_a_stand_in_guards_refusal_is_the_one_grok_gets(tmp_path: Path) -> None:
    copied = _hook_beside_stand_ins(
        tmp_path, {"agent_secrets_guard.sh": _RECORDING_GUARD}
    )
    run = _run_hook(
        tmp_path,
        hook.PRE_TOOL_USE_COMMAND,
        _pre_tool_use("list_dir", {"target_directory": "data/.secrets"}),
        env={
            "GUARD_RECORD": str(tmp_path / "record.jsonl"),
            "GUARD_EXIT": "2",
            "GUARD_STDERR": "NO\nNOT THIS ONE",
        },
        hook_path=copied,
    )
    _assert_denied(run, "NO\nNOT THIS ONE")


def test_a_use_tool_wrapper_is_judged_by_the_call_inside_it(tmp_path: Path) -> None:
    """grok reports use_tool under the inner tool's name with the wrapper as the input."""
    wrapper = {
        "tool_name": "run_terminal_command",
        "tool_input": {"command": "git rebase -i HEAD~2", "description": "x"},
    }
    _assert_denied(
        _pre(tmp_path, _pre_tool_use("run_terminal_command", wrapper)),
        "git rebase commands are not allowed",
    )
    _assert_denied(
        _pre(tmp_path, _pre_tool_use("use_tool", wrapper)),
        "git rebase commands are not allowed",
    )
    secret_write = {
        "tool_name": "write",
        "tool_input": {"file_path": "data/.secrets/svc.env", "content": "A=1"},
    }
    _assert_denied(
        _pre(tmp_path, _pre_tool_use("write", secret_write)),
        "read only by with_secrets.py",
    )


def test_an_allowed_use_tool_shell_call_is_rewritten_inside_the_wrapper(
    tmp_path: Path,
) -> None:
    inner = {"command": "pytest -q", "description": "x"}
    run = _pre(
        tmp_path,
        _pre_tool_use(
            "run_terminal_command",
            {"tool_name": "run_terminal_command", "tool_input": inner},
        ),
    )
    assert run.exit_code == 0
    assert run.updated_input == {
        "tool_name": "run_terminal_command",
        "tool_input": {**inner, "command": _OOM_PREFIX + "pytest -q"},
    }


def test_an_mcp_call_through_use_tool_is_allowed_unrewritten(tmp_path: Path) -> None:
    payload = _pre_tool_use(
        "probe__echo", {"tool_name": "probe__echo", "tool_input": {"text": "inline"}}
    )
    run = _pre(tmp_path, payload)
    assert run.exit_code == 0
    assert run.stdout == ""


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(
            _pre_tool_use(
                "workflow", {"source": {"type": "name", "name": "deep-research"}}
            ),
            id="workflow",
        ),
        pytest.param(
            _pre_tool_use(
                "workflow",
                {
                    "tool_name": "workflow",
                    "tool_input": {"source": {"type": "name", "name": "x"}},
                },
            ),
            id="workflow-through-use_tool",
        ),
    ],
)
def test_a_workflow_is_refused(tmp_path: Path, payload: dict[str, Any]) -> None:
    run = _pre(tmp_path, payload)
    _assert_denied(run, "workflow tool is not available")
    assert run.deny_reason == hook.WORKFLOW_REFUSAL


def test_a_truncated_shell_call_is_refused_without_running_any_guard(
    tmp_path: Path,
) -> None:
    guards = {
        name: _RECORDING_GUARD
        for name in hook.PRETOOLUSE_SCRIPTS
        if name.endswith(".sh")
    }
    copied = _hook_beside_stand_ins(tmp_path, guards)
    record = tmp_path / "record.jsonl"
    cut_command = "echo " + "x" * 4096 + " #f1862b7fed82f129 [truncated]"
    run = _run_hook(
        tmp_path,
        hook.PRE_TOOL_USE_COMMAND,
        _pre_tool_use(
            "run_terminal_command",
            {"command": cut_command, "description": "x"},
            is_truncated=True,
        ),
        env={"GUARD_RECORD": str(record)},
        hook_path=copied,
    )
    _assert_denied(run, "too long for the workspace's policy checks")
    assert _recorded(record) == []


def test_a_truncated_file_write_is_judged_on_its_path_which_grok_never_cuts(
    tmp_path: Path,
) -> None:
    content = "y" * 4096 + " #0123456789abcdef [truncated]"
    allowed = _pre(
        tmp_path,
        _pre_tool_use(
            "write", {"file_path": "big.json", "content": content}, is_truncated=True
        ),
    )
    assert allowed.exit_code == 0
    refused = _pre(
        tmp_path,
        _pre_tool_use(
            "write",
            {"file_path": "data/.secrets/svc.env", "content": content},
            is_truncated=True,
        ),
    )
    _assert_denied(refused, "read only by with_secrets.py")


def test_a_broken_guard_or_rewrite_fails_open_and_is_logged(tmp_path: Path) -> None:
    """Only exit 2 refuses. A guard that crashes, one that is missing and a missing rewriter let
    the command run as written, and each leaves a line in the state dir's log."""
    copied = _hook_beside_stand_ins(
        tmp_path,
        {"agent_prevent_commit_rewrite.sh": "echo 'guard crashed' >&2\nexit 1\n"},
    )
    run = _run_hook(
        tmp_path,
        hook.PRE_TOOL_USE_COMMAND,
        _shell("git rebase -i HEAD~2 | " + "head -1"),
        hook_path=copied,
    )
    assert run.exit_code == 0
    assert run.stdout == ""
    log = _log(tmp_path)
    assert (
        "agent_prevent_commit_rewrite.sh exited 1: guard crashed; failing open" in log
    )
    assert "agent_block_pipe_tail_head.sh is missing; skipping it" in log
    assert "agent_rewrite_bash_command.py failed" in log


def test_a_guard_that_outruns_the_budget_fails_open_and_is_logged(
    tmp_path: Path,
) -> None:
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "agent_prevent_commit_rewrite.sh").write_text("sleep 61729\n")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    context = hook.DispatchContext(
        scripts_dir=scripts_dir,
        state_dir=state_dir,
        environ={
            "PATH": os.environ.get("PATH", ""),
            "TICKETS_DIR": str(tmp_path / "no-tickets"),
        },
        deadline=time.monotonic() + 0.5,
    )
    started = time.monotonic()
    reply = hook.handle_pre_tool_use(_shell("git rebase -i HEAD~2"), context)
    assert time.monotonic() - started < 15
    assert reply.exit_code == 0
    log = _log(tmp_path)
    assert (
        "agent_prevent_commit_rewrite.sh ran past the hook's budget; failing open"
        in log
    )
    assert f"{hook.STEP_NUDGE_SCRIPT} is missing; skipping it" in log


class _UnreadableStdin(io.StringIO):
    def read(self, size: int | None = -1) -> str:
        raise RuntimeError("stdin broke")


def test_anything_unexpected_lets_the_call_through_and_is_logged(
    tmp_path: Path,
) -> None:
    (tmp_path / "state").mkdir()
    stdout = io.StringIO()
    stderr = io.StringIO()
    exit_code = hook.main(
        [hook.PRE_TOOL_USE_COMMAND],
        _UnreadableStdin(),
        stdout,
        stderr,
        {"MNGR_AGENT_STATE_DIR": str(tmp_path / "state")},
    )
    assert exit_code == 0
    assert stdout.getvalue() == "" and stderr.getvalue() == ""
    assert (
        "pre-tool-use failed: RuntimeError('stdin broke'); allowing the call"
        in _log(tmp_path)
    )


@pytest.mark.parametrize(
    ("command", "stdin_text", "expected_in_log"),
    [
        pytest.param(
            hook.PRE_TOOL_USE_COMMAND,
            "not json",
            "the hook input is not JSON",
            id="malformed-input",
        ),
        pytest.param(
            "post-tool-use",
            "{}",
            "unknown hook command 'post-tool-use'",
            id="unknown-command",
        ),
    ],
)
def test_input_the_hook_cannot_use_lets_the_call_through_and_is_logged(
    tmp_path: Path, command: str, stdin_text: str, expected_in_log: str
) -> None:
    run = _run_hook(tmp_path, command, stdin_text)
    assert (run.exit_code, run.stdout, run.stderr) == (0, "", "")
    assert expected_in_log in _log(tmp_path)


def test_the_open_steps_reminder_rides_the_first_allowed_call_of_the_turn_once(
    tmp_path: Path,
) -> None:
    work_dir = _tk_work_dir(tmp_path)
    env = _step_env(
        work_dir,
        steps="s-1  [open] - Tidy the report",
        in_progress="s-1  [in_progress] - Tidy",
    )
    prompt = _run_hook(
        tmp_path, hook.USER_PROMPT_SUBMIT_COMMAND, _CAPTURED_ROOT_PROMPT, env=env
    )
    assert (prompt.exit_code, prompt.stdout) == (0, "")
    stamp = json.loads((tmp_path / "state" / hook.TURN_STAMP_FILE_NAME).read_text())
    assert stamp == {
        "sessionId": _ROOT_SESSION,
        "promptId": _CAPTURED_ROOT_PROMPT["promptId"],
    }

    first = _pre(tmp_path, _shell("echo one"), env=env)
    assert first.additional_context is not None
    assert (
        "[Open task reminder from default-workspace-template]"
        in first.additional_context
    )
    assert "s-1  [open] - Tidy the report" in first.additional_context
    assert first.updated_input is not None

    second = _pre(tmp_path, _shell("echo two"), env=env)
    assert second.additional_context is None


def test_a_refused_first_call_leaves_the_reminder_for_the_next_allowed_one(
    tmp_path: Path,
) -> None:
    work_dir = _tk_work_dir(tmp_path)
    env = _step_env(
        work_dir,
        steps="s-1  [open] - Tidy the report",
        in_progress="s-1  [in_progress] - Tidy",
    )
    _run_hook(tmp_path, hook.USER_PROMPT_SUBMIT_COMMAND, _CAPTURED_ROOT_PROMPT, env=env)
    refused = _pre(tmp_path, _shell("git rebase -i HEAD~2"), env=env)
    assert (
        refused.deny_reason is not None
        and "Open task reminder" not in refused.deny_reason
    )
    allowed = _pre(tmp_path, _shell("echo after"), env=env)
    assert (
        allowed.additional_context is not None
        and "Open task reminder" in allowed.additional_context
    )


def test_a_subagents_prompt_and_tool_calls_neither_open_nor_take_the_reminder(
    tmp_path: Path,
) -> None:
    work_dir = _tk_work_dir(tmp_path)
    env = _step_env(
        work_dir,
        steps="s-1  [open] - Tidy the report",
        in_progress="s-1  [in_progress] - Tidy",
    )
    stamp = tmp_path / "state" / hook.TURN_STAMP_FILE_NAME

    _run_hook(
        tmp_path, hook.USER_PROMPT_SUBMIT_COMMAND, _CAPTURED_CHILD_PROMPT, env=env
    )
    assert not stamp.exists()

    _run_hook(tmp_path, hook.USER_PROMPT_SUBMIT_COMMAND, _CAPTURED_ROOT_PROMPT, env=env)
    child_call = _pre(
        tmp_path,
        _pre_tool_use("read_file", {"target_file": "a.txt"}, session_id=_CHILD_SESSION),
        env=env,
    )
    assert child_call.additional_context is None
    assert stamp.exists()
    root_call = _pre(
        tmp_path, _pre_tool_use("read_file", {"target_file": "a.txt"}), env=env
    )
    assert (
        root_call.additional_context is not None
        and "Open task reminder" in root_call.additional_context
    )


@pytest.mark.parametrize(
    ("steps", "expected_in_nudge"),
    [
        pytest.param("", "without declaring any step records", id="no-steps"),
        pytest.param(
            "s-1  [open] - Tidy the report",
            "none is currently in_progress",
            id="declared-not-started",
        ),
    ],
)
def test_the_step_nudge_rides_additional_context_when_no_step_is_in_progress(
    tmp_path: Path, steps: str, expected_in_nudge: str
) -> None:
    env = _step_env(_tk_work_dir(tmp_path), steps=steps, in_progress="")
    run = _pre(tmp_path, _shell("echo working"), env=env)
    assert run.exit_code == 0
    assert (
        run.additional_context is not None
        and expected_in_nudge in run.additional_context
    )
    assert run.updated_input is not None


def test_managing_steps_is_not_nudged(tmp_path: Path) -> None:
    env = _step_env(_tk_work_dir(tmp_path), steps="", in_progress="")
    run = _pre(tmp_path, _shell('tk create --step "Tidy the report"'), env=env)
    assert run.exit_code == 0
    assert run.additional_context is None
