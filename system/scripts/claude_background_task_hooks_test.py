"""Tests for claude_background_task_hooks.py: what the Stop and SessionStart hooks record and
clear, and the hooks run exactly as ``.claude/settings.json`` wires them."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from chat_background_tasks import markers
from chat_background_tasks.markers import BackgroundTask, TaskSource
from chat_background_tasks.testing import runner_task
from script_modules_testing import claude_background_task_hooks as hooks

_CHAT_ID = "agent-0123456789abcdef0123456789abcdef"
_OTHER_CHAT_ID = "agent-fedcba9876543210fedcba9876543210"
_SCRIPT_NAME = "claude_background_task_hooks.py"
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _main_claude_env(work_dir: Path, **extra: str) -> dict[str, str]:
    """The environment of an agent's main Claude, whose hooks may write: this test process is its
    Claude. It names the marker root the test's own fixture points at, and blanks the agent
    identity a suite run inside an mngr agent inherits, whose recorded main claude would make
    this one read as nested."""
    return {
        "MAIN_CLAUDE_SESSION_ID": "session-1",
        "MNGR_AGENT_ID": _CHAT_ID,
        "MNGR_AGENT_WORK_DIR": str(work_dir),
        "MNGR_AGENT_STATE_DIR": "",
        "MNGR_CLAUDE_SUBAGENT_PROXY_CHILD": "",
        "CLAUDE_PROJECT_DIR": str(work_dir),
        "CLAUDE_PID": str(os.getpid()),
        markers.MARKER_ROOT_ENV: os.environ[markers.MARKER_ROOT_ENV],
        **extra,
    }


def _shell_entry(task_id: str, description: str = "tail logs") -> dict[str, Any]:
    """One running shell task as Claude Code lists it in a Stop input's ``background_tasks``."""
    return {
        "id": task_id,
        "type": "shell",
        "status": "running",
        "description": description,
        "command": "tail -f log",
    }


def _env_with_recorded_main_claude(
    tmp_path: Path, main_pid: int, **extra: str
) -> dict[str, str]:
    state_dir = tmp_path / "agent-state"
    state_dir.mkdir(exist_ok=True)
    (state_dir / hooks.CLAUDE_MAIN_PID_FILE_NAME).write_text(f"{main_pid}\n")
    return _main_claude_env(tmp_path, MNGR_AGENT_STATE_DIR=str(state_dir), **extra)


def _stop(root: Path, env: dict[str, str], *entries: dict[str, Any]) -> None:
    """What the Stop hook does with ``env`` and a Stop input listing ``entries``."""
    caller = hooks.hook_caller(env)
    if caller is not None:
        hooks.record_stop(root, caller, {"background_tasks": list(entries)})


def _clear(root: Path, env: dict[str, str]) -> None:
    """What the SessionStart hook does with ``env``."""
    caller = hooks.hook_caller(env)
    if caller is not None:
        hooks.clear(root, caller)


def _live_ids(root: Path, chat_id: str = _CHAT_ID) -> list[str]:
    return [task.id for task in markers.list_live_tasks(root, chat_id)]


def test_a_stop_records_every_waking_kind_and_skips_the_rest(
    background_task_markers: Path, tmp_path: Path
) -> None:
    _stop(
        background_task_markers,
        _main_claude_env(tmp_path),
        _shell_entry("shell-1"),
        {"id": "monitor-1", "type": "monitor", "description": "watch CI"},
        {"id": "workflow-1", "type": "workflow", "description": "review"},
        {"id": "agent-1", "type": "subagent", "description": "explore"},
        {"id": "cloud-1", "type": "cloud session", "description": "elsewhere"},
        {"type": "shell", "description": "no id"},
    )

    live = markers.list_live_tasks(background_task_markers, _CHAT_ID)
    assert {(task.id, task.kind) for task in live} == {
        ("shell-1", "shell"),
        ("monitor-1", "monitor"),
        ("workflow-1", "workflow"),
        ("agent-1", "subagent"),
    }
    assert all(hooks.is_claude_task(task) and task.pid == os.getpid() for task in live)
    [shell] = [task for task in live if task.id == "shell-1"]
    assert (shell.description, shell.command) == ("tail logs", "tail -f log")


def test_a_stop_records_a_command_as_the_agent_wrote_it(
    background_task_markers: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude's Stop input carries each command as the PreToolUse rewrite hook passed it on, with
    the commit identity and OOM band in front; the marker keeps only what the agent wrote. The
    identity names the agent as it was when the command ran, which the chat's automatic name,
    landing during its first turn, changes before that turn's Stop."""
    host_dir = tmp_path / "host"
    host_dir.mkdir()
    (host_dir / "data.json").write_text(json.dumps({"host_id": "host-1"}))
    monkeypatch.setenv("MNGR_AGENT_ID", _CHAT_ID)
    monkeypatch.setenv("MNGR_AGENT_NAME", "chat-3")
    monkeypatch.setenv("MNGR_HOST_DIR", str(host_dir))
    monkeypatch.delenv("MNGR_AGENT_STATE_DIR", raising=False)
    rewritten = hooks.agent_rewrite_bash_command.build_rewritten_command("tail -f log")
    assert rewritten.startswith("export GIT_AUTHOR_NAME=chat-3 ")
    monkeypatch.setenv("MNGR_AGENT_NAME", "Fix the login page")

    _stop(
        background_task_markers,
        _main_claude_env(tmp_path),
        _shell_entry("shell-1") | {"command": rewritten},
    )

    [task] = markers.list_live_tasks(background_task_markers, _CHAT_ID)
    assert task.command == "tail -f log"


def test_a_later_stop_keeps_a_listed_tasks_start_and_prunes_the_unlisted_ones(
    background_task_markers: Path, tmp_path: Path
) -> None:
    env = _main_claude_env(tmp_path)
    _stop(background_task_markers, env, _shell_entry("kept"), _shell_entry("done"))
    [kept_before] = [
        task
        for task in markers.list_live_tasks(background_task_markers, _CHAT_ID)
        if task.id == "kept"
    ]
    markers.write_marker(
        background_task_markers,
        _CHAT_ID,
        runner_task("runner", "2026-10-08T10:00:00+00:00"),
    )
    # Its start is backdated, so a rewrite with a fresh start would show.
    markers.write_marker(
        background_task_markers,
        _CHAT_ID,
        kept_before._replace(started_at="2026-10-08T09:00:00+00:00"),
    )

    _stop(background_task_markers, env, _shell_entry("kept"))

    live = {
        task.id: task
        for task in markers.list_live_tasks(background_task_markers, _CHAT_ID)
    }
    assert set(live) == {"kept", "runner"}
    assert live["kept"].started_at == "2026-10-08T09:00:00+00:00"


@pytest.mark.parametrize("payload", [{"hook_event_name": "Stop"}, "not an object"])
def test_a_stop_with_no_task_list_leaves_the_markers_alone(
    background_task_markers: Path, tmp_path: Path, payload: object
) -> None:
    env = _main_claude_env(tmp_path)
    _stop(background_task_markers, env, _shell_entry("one"))

    hooks.record_stop(
        background_task_markers, hooks.HookCaller(_CHAT_ID, os.getpid()), payload
    )

    assert _live_ids(background_task_markers) == ["one"]


def test_clearing_and_stopping_leave_the_runners_and_other_harnesses_markers(
    background_task_markers: Path, tmp_path: Path
) -> None:
    env = _main_claude_env(tmp_path)
    _stop(background_task_markers, env, _shell_entry("one"))
    markers.write_marker(
        background_task_markers,
        _CHAT_ID,
        runner_task("runner", "2026-10-08T10:00:00+00:00"),
    )
    # Same id and same process as Claude's own task: only the harness tells them apart.
    markers.write_marker(
        background_task_markers,
        _CHAT_ID,
        BackgroundTask(
            source=TaskSource.HARNESS,
            id="one",
            description="",
            started_at="2026-10-08T10:00:00+00:00",
            pid=os.getpid(),
            harness="other",
        ),
    )

    _clear(background_task_markers, env)
    _stop(background_task_markers, env)

    assert {
        (task.source, task.harness)
        for task in markers.list_live_tasks(background_task_markers, _CHAT_ID)
    } == {(TaskSource.RUN_IN_BACKGROUND, ""), (TaskSource.HARNESS, "other")}


@pytest.mark.parametrize(
    "overrides",
    [
        {"MNGR_CLAUDE_SUBAGENT_PROXY_CHILD": "1"},
        {"MAIN_CLAUDE_SESSION_ID": ""},
        {"CLAUDE_PROJECT_DIR": "/somewhere/else"},
        {"MNGR_AGENT_ID": ""},
        {"CLAUDE_PID": ""},
    ],
    ids=[
        "subagent-proxy-child",
        "no-main-session",
        "other-project-dir",
        "not-an-agent",
        "no-claude-pid",
    ],
)
def test_a_claude_that_is_not_the_agents_main_session_writes_nothing(
    tmp_path: Path, overrides: dict[str, str]
) -> None:
    assert hooks.hook_caller(_main_claude_env(tmp_path) | overrides) is None


def test_the_chat_app_stamped_chat_id_keys_the_claude_markers(
    background_task_markers: Path, tmp_path: Path
) -> None:
    _stop(
        background_task_markers,
        _main_claude_env(tmp_path, MINDS_CHAT_ID=_OTHER_CHAT_ID),
        _shell_entry("one"),
    )

    assert markers.is_busy(background_task_markers, _OTHER_CHAT_ID)
    assert not markers.is_busy(background_task_markers, _CHAT_ID)


def test_a_nested_claude_neither_records_nor_clears_while_the_main_claude_lives(
    background_task_markers: Path, tmp_path: Path, live_claude_pid: int
) -> None:
    _stop(
        background_task_markers,
        _main_claude_env(tmp_path, CLAUDE_PID=str(live_claude_pid)),
        _shell_entry("main-task"),
    )
    nested_env = _env_with_recorded_main_claude(tmp_path, live_claude_pid)

    _clear(background_task_markers, nested_env)
    _stop(background_task_markers, nested_env, _shell_entry("nested-task"))

    assert _live_ids(background_task_markers) == ["main-task"]


@pytest.mark.parametrize("owner", ["exited", "not-claude"])
def test_a_recorded_main_claude_that_is_gone_does_not_block_its_successor(
    background_task_markers: Path,
    tmp_path: Path,
    exited_pid: int,
    live_other_pid: int,
    owner: str,
) -> None:
    recorded = exited_pid if owner == "exited" else live_other_pid

    _stop(
        background_task_markers,
        _env_with_recorded_main_claude(tmp_path, recorded),
        _shell_entry("one"),
    )

    assert markers.is_busy(background_task_markers, _CHAT_ID)


def test_a_stop_leaves_another_live_claudes_markers_and_prunes_dead_ones(
    background_task_markers: Path, tmp_path: Path, live_claude_pid: int, exited_pid: int
) -> None:
    for task_id, pid in (("successor", live_claude_pid), ("gone", exited_pid)):
        markers.write_marker(
            background_task_markers,
            _CHAT_ID,
            BackgroundTask(
                source=TaskSource.HARNESS,
                id=task_id,
                description="",
                started_at="2026-10-08T09:00:00+00:00",
                pid=pid,
                harness=hooks.CLAUDE_HARNESS,
            ),
        )

    _stop(background_task_markers, _main_claude_env(tmp_path))

    assert [
        task.id
        for task in markers.read_markers(
            markers.chat_dir(background_task_markers, _CHAT_ID)
        )
    ] == ["successor"]


def test_a_stop_does_not_record_a_task_it_lists_as_over(
    background_task_markers: Path, tmp_path: Path
) -> None:
    _stop(
        background_task_markers,
        _main_claude_env(tmp_path),
        _shell_entry("running"),
        {**_shell_entry("completed"), "status": "completed"},
        {**_shell_entry("killed"), "status": "killed"},
    )

    assert _live_ids(background_task_markers) == ["running"]


def _wired_command(event: str) -> str:
    """The hook command ``.claude/settings.json`` runs for ``event``."""
    settings = json.loads((_REPO_ROOT / ".claude" / "settings.json").read_text())
    [command] = [
        hook["command"]
        for matcher in settings["hooks"][event]
        for hook in matcher["hooks"]
        if _SCRIPT_NAME in hook["command"]
    ]
    return command


def _run_wired_hook(
    event: str, payload: dict[str, Any], env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", "-c", _wired_command(event)],
        input=json.dumps(payload),
        text=True,
        env=env,
        cwd=_REPO_ROOT,
        capture_output=True,
        check=True,
    )


@pytest.mark.parametrize("source", ["resume", "startup"])
def test_the_wired_claude_hooks_record_a_stop_and_a_restarted_claude_clears_it_silently(
    background_task_markers: Path, source: str
) -> None:
    """Runs the hook commands exactly as ``.claude/settings.json`` spells them, as Claude does. A
    restarted Claude's markers of the dead one go, and SessionStart prints nothing: Claude Code
    itself tells a resumed session which of its tasks the restart stopped."""
    old_claude = subprocess.Popen(["sleep", "60"])
    try:
        _run_wired_hook(
            "Stop",
            {"background_tasks": [_shell_entry("one", "Build the site")]},
            os.environ | _main_claude_env(_REPO_ROOT, CLAUDE_PID=str(old_claude.pid)),
        )
        [task] = markers.list_live_tasks(background_task_markers, _CHAT_ID)
        assert (task.description, task.pid) == ("Build the site", old_claude.pid)
    finally:
        old_claude.kill()
        old_claude.wait()

    started = _run_wired_hook(
        "SessionStart",
        {"hook_event_name": "SessionStart", "source": source},
        os.environ | _main_claude_env(_REPO_ROOT),
    )

    assert list(markers.chat_dir(background_task_markers, _CHAT_ID).iterdir()) == []
    assert started.stdout == ""


@pytest.mark.parametrize("event", ["Stop", "SessionStart"])
def test_a_wired_claude_hook_whose_script_is_gone_still_exits_0(
    tmp_path: Path, event: str
) -> None:
    """Claude keeps the hooks it started with, so a session can outlive the script (a worktree
    checked out at an older commit). python3 exits 2 on a missing script, and a Stop hook's exit 2
    blocks Claude's stop."""
    env = os.environ | _main_claude_env(tmp_path)

    finished = subprocess.run(
        ["sh", "-c", _wired_command(event)],
        input="{}",
        text=True,
        env=env,
        cwd=tmp_path,
        capture_output=True,
    )

    assert finished.returncode == 0, finished.stderr
