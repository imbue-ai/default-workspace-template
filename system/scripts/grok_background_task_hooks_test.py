"""Tests for grok_background_task_hooks.py: what the Stop, PostToolUse and SessionStart hooks record
and clear, the grok pid record they share with mngr, and the hooks run exactly as
``.grok/hooks/policy.json`` wires them.

The payloads are grok 1.0.62's, captured against a stub model."""

from __future__ import annotations

import io
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from chat_background_tasks import markers
from chat_background_tasks.markers import BackgroundTask, TaskSource
from imbue.chat.harnesses.harness_type import HarnessType
from imbue.mngr_grok.api import read_grok_pid
from script_modules_testing import load_script_module

hooks = load_script_module(
    "grok_background_task_hooks_for_tests", "grok_background_task_hooks.py"
)

_CHAT_ID = "agent-0123456789abcdef0123456789abcdef"
_OTHER_CHAT_ID = "agent-fedcba9876543210fedcba9876543210"
_ROOT = "01a167b5-09ed-72a1-a5f6-3bb9df144205"
_OTHER_ROOT = "01a167ce-5208-7860-976d-a4281abf0472"
_SHELL_ID = "01a167b6-080d-7853-8a83-7d57c21f0c32"
_MONITOR_ID = "01a167be-406c-7360-8182-629a61d00cee"
_SUBAGENT_ID = "01a167cc-243e-7c83-8c3b-dc345146fd5e"
_SCRIPT_NAME = "grok_background_task_hooks.py"
_REPO_ROOT = Path(__file__).resolve().parents[2]

_SHELL_ENTRY = {
    "id": _SHELL_ID,
    "type": "shell",
    "status": "running",
    "command": "sleep 6; echo bg-done",
}
_MONITOR_ENTRY = {
    "id": _MONITOR_ID,
    "type": "monitor",
    "status": "running",
    "description": "[monitor] watch ticks",
}
_SUBAGENT_ENTRY = {
    "id": _SUBAGENT_ID,
    "type": "subagent",
    "status": "running",
    "description": "slow bg sub",
    "agentType": "general-purpose",
}


def _stop_input(*entries: dict[str, Any], session_id: str = _ROOT) -> dict[str, Any]:
    """A top-level session's ``Stop{end_turn}`` input listing ``entries`` as its background tasks."""
    return {
        "hookEventName": "stop",
        "sessionId": session_id,
        "reason": "end_turn",
        "stopHookActive": False,
        "backgroundTasks": list(entries),
        "sessionCrons": [],
        "hook_event_name": "Stop",
        "session_id": session_id,
    }


def _spawn_post_tool_use_input(
    result_text: str, session_id: str = _ROOT
) -> dict[str, Any]:
    """The PostToolUse input of a ``spawn_subagent`` call whose result is ``result_text``."""
    tool_input = {
        "prompt": "say hi",
        "description": "slow bg sub",
        "background": True,
    }
    result = {"type": "Text", "text": result_text}
    return {
        "hookEventName": "post_tool_use",
        "sessionId": session_id,
        "toolName": "spawn_subagent",
        "toolUseId": "call_stub_1",
        "toolInput": tool_input,
        "toolResult": result,
        "isBackgrounded": False,
        "hook_event_name": "PostToolUse",
        "session_id": session_id,
        "tool_name": "spawn_subagent",
        "tool_input": tool_input,
        "tool_response": result,
    }


_BACKGROUND_SPAWN_RESULT = (
    "Subagent started in background.\n"
    f"subagent_id: {_SUBAGENT_ID}\n"
    "description: slow bg sub\n\n"
    "When you need its result, use get_command_or_subagent_output with "
    f'task_ids=["{_SUBAGENT_ID}"] and a positive timeout_ms.'
)


def _record_grok_pid(state_dir: Path, pid: int) -> None:
    """Record ``pid`` as the agent's grok, as mngr's SessionStart hook does."""
    pid_path = state_dir / hooks.GROK_PID_RELATIVE_PATH
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(f"{pid}\n")


def _grok_env(
    state_dir: Path, grok_pid: int | None = None, **extra: str
) -> dict[str, str]:
    """The environment of a hook of the agent's main grok, whose pid is recorded as ``grok_pid``
    (this test process by default). It names the marker root the test's own fixture points at."""
    state_dir.mkdir(parents=True, exist_ok=True)
    _record_grok_pid(state_dir, os.getpid() if grok_pid is None else grok_pid)
    return {
        "MNGR_AGENT_ID": _CHAT_ID,
        "MNGR_AGENT_STATE_DIR": str(state_dir),
        "MINDS_CHAT_ID": "",
        "GROK_AGENT": "",
        markers.MARKER_ROOT_ENV: os.environ[markers.MARKER_ROOT_ENV],
        **extra,
    }


def _run(command: str, env: dict[str, str], payload: object) -> None:
    """What the hook ``command`` does with ``env`` and ``payload`` on stdin."""
    assert hooks.main([command], env, io.StringIO(json.dumps(payload))) == 0


def _live(root: Path, chat_id: str = _CHAT_ID) -> dict[str, BackgroundTask]:
    return {task.id: task for task in markers.list_live_tasks(root, chat_id)}


def test_the_harness_name_is_the_chat_apps_name_for_grok() -> None:
    assert hooks.GROK_HARNESS == HarnessType.GROK.value


@pytest.mark.parametrize(
    "pid_text", ["{pid}\n", "{pid}", "{dead}\n", "1\n", "0\n", "-5\n", "abc\n", ""]
)
def test_the_hooks_read_the_grok_pid_mngr_grok_records_as_read_grok_pid_does(
    tmp_path: Path, exited_pid: int, pid_text: str
) -> None:
    """A hook's system python3 cannot import ``imbue.mngr_grok``, so it reads the record itself: the
    same file, with the same answer for every form the record can take."""
    pid_path = tmp_path / hooks.GROK_PID_RELATIVE_PATH
    pid_path.parent.mkdir(parents=True)
    pid_path.write_text(pid_text.format(pid=os.getpid(), dead=exited_pid))

    assert hooks.recorded_grok_pid(tmp_path) == read_grok_pid(tmp_path)


def test_the_hooks_read_a_record_older_than_its_live_process_as_read_grok_pid_does(
    tmp_path: Path,
) -> None:
    _record_grok_pid(tmp_path, os.getpid())
    os.utime(tmp_path / hooks.GROK_PID_RELATIVE_PATH, (0, 0))

    assert hooks.recorded_grok_pid(tmp_path) == read_grok_pid(tmp_path)


def test_the_grok_pid_record_is_read_from_where_mngr_grok_keeps_it(
    tmp_path: Path,
) -> None:
    assert hooks.recorded_grok_pid(tmp_path) is None
    _record_grok_pid(tmp_path, os.getpid())

    assert read_grok_pid(tmp_path) == os.getpid()
    assert hooks.recorded_grok_pid(tmp_path) == os.getpid()


def test_a_stop_records_every_task_grok_lists(
    background_task_markers: Path, tmp_path: Path
) -> None:
    _run(
        "stop",
        _grok_env(tmp_path / "state"),
        _stop_input(
            _SHELL_ENTRY,
            _MONITOR_ENTRY,
            _SUBAGENT_ENTRY,
            {"id": "cron-1", "type": "cron", "status": "running"},
            {"type": "shell", "status": "running", "command": "no id"},
        ),
    )

    live = _live(background_task_markers)
    assert {
        (task.id, task.kind, task.description, task.command) for task in live.values()
    } == {
        (_SHELL_ID, "shell", "sleep 6; echo bg-done", "sleep 6; echo bg-done"),
        (_MONITOR_ID, "monitor", "[monitor] watch ticks", ""),
        (_SUBAGENT_ID, "subagent", "slow bg sub", ""),
    }
    assert all(
        hooks.is_grok_task(task) and task.pid == os.getpid() for task in live.values()
    )


def test_a_stop_records_a_shell_command_as_the_agent_wrote_it(
    background_task_markers: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """grok lists each shell command as the policy hook's rewrite passed it on, with the commit
    identity and OOM band in front; the marker keeps only what the agent wrote."""
    monkeypatch.setenv("MNGR_AGENT_ID", _CHAT_ID)
    monkeypatch.setenv("MNGR_AGENT_NAME", "chat-3")
    monkeypatch.delenv("MNGR_AGENT_STATE_DIR", raising=False)
    monkeypatch.delenv("MNGR_HOST_DIR", raising=False)
    rewritten = hooks.agent_rewrite_bash_command.build_rewritten_command(
        "sleep 6; echo bg-done"
    )
    assert rewritten != "sleep 6; echo bg-done"

    _run(
        "stop",
        _grok_env(tmp_path / "state"),
        _stop_input(_SHELL_ENTRY | {"command": rewritten}),
    )

    shell = _live(background_task_markers)[_SHELL_ID]
    assert (shell.command, shell.description) == (
        "sleep 6; echo bg-done",
        "sleep 6; echo bg-done",
    )


def test_a_later_stop_keeps_a_listed_tasks_start_and_prunes_the_unlisted_ones(
    background_task_markers: Path, tmp_path: Path
) -> None:
    env = _grok_env(tmp_path / "state")
    _run("stop", env, _stop_input(_SHELL_ENTRY, _MONITOR_ENTRY))
    # Its start is backdated, so a rewrite with a fresh start would show.
    markers.write_marker(
        background_task_markers,
        _CHAT_ID,
        _live(background_task_markers)[_SHELL_ID]._replace(
            started_at="2026-10-08T09:00:00+00:00"
        ),
    )

    _run("stop", env, _stop_input(_SHELL_ENTRY))

    live = _live(background_task_markers)
    assert set(live) == {_SHELL_ID}
    assert live[_SHELL_ID].started_at == "2026-10-08T09:00:00+00:00"


def test_a_sessions_stop_leaves_the_tasks_another_session_of_the_same_grok_marked(
    background_task_markers: Path, tmp_path: Path
) -> None:
    """``/new`` opens a second top-level session in the same grok, and each session's Stop lists
    only its own tasks: the other's run on, and wake the other session when they end."""
    env = _grok_env(tmp_path / "state")
    _run("stop", env, _stop_input(_SHELL_ENTRY))

    _run("stop", env, _stop_input(session_id=_OTHER_ROOT))

    assert set(_live(background_task_markers)) == {_SHELL_ID}
    _run("stop", env, _stop_input())
    assert _live(background_task_markers) == {}


@pytest.mark.parametrize(
    "payload",
    [
        {
            "hookEventName": "stop",
            "sessionId": _ROOT,
            "reason": "shutdown",
            "stopHookActive": False,
        },
        "not an object",
    ],
    ids=["shutdown-stop", "not-an-object"],
)
def test_a_stop_with_no_task_list_leaves_the_markers_alone(
    background_task_markers: Path, tmp_path: Path, payload: object
) -> None:
    env = _grok_env(tmp_path / "state")
    _run("stop", env, _stop_input(_SHELL_ENTRY))

    _run("stop", env, payload)

    assert set(_live(background_task_markers)) == {_SHELL_ID}


def test_a_stop_leaves_another_live_groks_markers_and_prunes_dead_ones(
    background_task_markers: Path, tmp_path: Path, live_other_pid: int, exited_pid: int
) -> None:
    """After a handoff the chat's other agent may be a live grok whose markers are its own."""
    for task_id, pid in (("successor", live_other_pid), ("gone", exited_pid)):
        markers.write_marker(
            background_task_markers,
            _CHAT_ID,
            BackgroundTask(
                source=TaskSource.HARNESS,
                id=task_id,
                description="",
                started_at="2026-10-08T09:00:00+00:00",
                pid=pid,
                harness=hooks.GROK_HARNESS,
            ),
        )

    _run("stop", _grok_env(tmp_path / "state"), _stop_input())

    assert [
        task.id
        for task in markers.read_markers(
            markers.chat_dir(background_task_markers, _CHAT_ID)
        )
    ] == ["successor"]


def test_post_tool_use_marks_a_subagent_spawned_in_the_background_and_its_stop_keeps_it(
    background_task_markers: Path, tmp_path: Path
) -> None:
    """A turn cut short runs no Stop, and a background subagent it started still wakes the session
    when it ends, so it is marked from the moment it starts."""
    env = _grok_env(tmp_path / "state")
    _run("post-tool-use", env, _spawn_post_tool_use_input(_BACKGROUND_SPAWN_RESULT))
    spawned = _live(background_task_markers)[_SUBAGENT_ID]
    assert (spawned.kind, spawned.description, spawned.pid) == (
        "subagent",
        "slow bg sub",
        os.getpid(),
    )

    _run("stop", env, _stop_input(_SUBAGENT_ENTRY))

    assert _live(background_task_markers) == {_SUBAGENT_ID: spawned}


@pytest.mark.parametrize(
    "payload",
    [
        _spawn_post_tool_use_input(
            f"STUB_REPLY: hi from the subagent\n\nsubagent_id: {_SUBAGENT_ID}"
        ),
        _spawn_post_tool_use_input("Subagent started in background.\nno id here"),
        {
            "hookEventName": "post_tool_use",
            "sessionId": _ROOT,
            "toolName": "run_terminal_command",
            "toolInput": {
                "command": "sleep 6",
                "description": "bg",
                "block_until_ms": 0,
            },
            "toolResult": {
                "type": "BackgroundTaskStarted",
                "task_id": _SHELL_ID,
                "status": "running",
            },
        },
        "not an object",
    ],
    ids=["foreground-subagent", "no-id", "background-shell", "not-an-object"],
)
def test_post_tool_use_marks_nothing_else(
    background_task_markers: Path, tmp_path: Path, payload: object
) -> None:
    """A shell or monitor left running by a turn that is then cut short never wakes the session, so
    only a Stop that lists it marks it."""
    _run("post-tool-use", _grok_env(tmp_path / "state"), payload)

    assert _live(background_task_markers) == {}


@pytest.mark.parametrize(
    "overrides",
    [
        {"GROK_AGENT": "1"},
        {"MNGR_AGENT_STATE_DIR": ""},
        {"MNGR_AGENT_ID": ""},
    ],
    ids=["nested-grok", "not-an-agent", "no-chat"],
)
def test_a_grok_that_is_not_the_agents_main_grok_writes_nothing(
    background_task_markers: Path, tmp_path: Path, overrides: dict[str, str]
) -> None:
    env = _grok_env(tmp_path / "state") | overrides

    _run("post-tool-use", env, _spawn_post_tool_use_input(_BACKGROUND_SPAWN_RESULT))
    _run("stop", env, _stop_input(_SHELL_ENTRY))

    assert _live(background_task_markers) == {}


def test_a_hook_writes_nothing_without_a_live_recorded_grok(
    background_task_markers: Path, tmp_path: Path, exited_pid: int
) -> None:
    env = _grok_env(tmp_path / "state", grok_pid=exited_pid)

    _run("post-tool-use", env, _spawn_post_tool_use_input(_BACKGROUND_SPAWN_RESULT))
    _run("stop", env, _stop_input(_SHELL_ENTRY))

    assert _live(background_task_markers) == {}


def test_a_subagents_own_session_writes_nothing(
    background_task_markers: Path, tmp_path: Path
) -> None:
    env = _grok_env(tmp_path / "state")
    child = {"subagentType": "general-purpose", "sessionId": _SUBAGENT_ID}

    _run(
        "post-tool-use",
        env,
        _spawn_post_tool_use_input(_BACKGROUND_SPAWN_RESULT) | child,
    )
    _run("stop", env, _stop_input(_SHELL_ENTRY) | child)

    assert _live(background_task_markers) == {}


def test_the_chat_app_stamped_chat_id_keys_the_grok_markers(
    background_task_markers: Path, tmp_path: Path
) -> None:
    _run(
        "stop",
        _grok_env(tmp_path / "state", MINDS_CHAT_ID=_OTHER_CHAT_ID),
        _stop_input(_SHELL_ENTRY),
    )

    assert markers.is_busy(background_task_markers, _OTHER_CHAT_ID)
    assert not markers.is_busy(background_task_markers, _CHAT_ID)


def test_session_start_clears_only_the_markers_of_a_grok_that_is_gone(
    background_task_markers: Path, tmp_path: Path, exited_pid: int
) -> None:
    """``/new`` starts a session in the same grok, whose earlier session's tasks still run."""
    env = _grok_env(tmp_path / "state")
    _run("stop", env, _stop_input(_SHELL_ENTRY))
    markers.write_marker(
        background_task_markers,
        _CHAT_ID,
        _live(background_task_markers)[_SHELL_ID]._replace(id="gone", pid=exited_pid),
    )
    markers.write_marker(
        background_task_markers,
        _CHAT_ID,
        BackgroundTask(
            source=TaskSource.HARNESS,
            id="other-harness",
            description="",
            started_at="2026-10-08T09:00:00+00:00",
            pid=exited_pid,
            harness="claude",
        ),
    )

    _run(
        "session-start",
        env,
        {"hookEventName": "session_start", "sessionId": _OTHER_ROOT, "source": "new"},
    )

    assert {
        task.id
        for task in markers.read_markers(
            markers.chat_dir(background_task_markers, _CHAT_ID)
        )
    } == {_SHELL_ID, "other-harness"}


def _wired_handler(event: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """The matcher group and handler ``.grok/hooks/policy.json`` runs this script with for ``event``."""
    config = json.loads((_REPO_ROOT / ".grok" / "hooks" / "policy.json").read_text())
    [(group, handler)] = [
        (group, handler)
        for group in config["hooks"][event]
        for handler in group["hooks"]
        if _SCRIPT_NAME in handler["command"]
    ]
    return group, handler


def _run_wired_hook(
    event: str, payload: object, env: dict[str, str], cwd: Path = _REPO_ROOT
) -> subprocess.CompletedProcess[str]:
    """Run the wired command as grok does: its variables expanded by ``sh``, from the work dir."""
    return subprocess.run(
        ["sh", "-c", _wired_handler(event)[1]["command"]],
        input=json.dumps(payload),
        text=True,
        env=env,
        cwd=cwd,
        capture_output=True,
        timeout=30,
    )


def test_the_wired_grok_hooks_record_a_turn_and_a_new_grok_clears_it_silently(
    background_task_markers: Path, tmp_path: Path
) -> None:
    old_grok = subprocess.Popen(["sleep", "60"])
    try:
        env = os.environ | _grok_env(
            tmp_path / "state",
            grok_pid=old_grok.pid,
            MNGR_AGENT_WORK_DIR=str(_REPO_ROOT),
        )
        for event, payload in (
            ("PostToolUse", _spawn_post_tool_use_input(_BACKGROUND_SPAWN_RESULT)),
            ("Stop", _stop_input(_SHELL_ENTRY, _SUBAGENT_ENTRY)),
        ):
            finished = _run_wired_hook(event, payload, env)
            assert (finished.returncode, finished.stdout + finished.stderr) == (0, "")
        live = _live(background_task_markers)
        assert set(live) == {_SHELL_ID, _SUBAGENT_ID}
        assert {task.pid for task in live.values()} == {old_grok.pid}
    finally:
        old_grok.kill()
        old_grok.wait()

    started = _run_wired_hook(
        "SessionStart",
        {"hookEventName": "session_start", "sessionId": _ROOT, "source": "load"},
        os.environ | _grok_env(tmp_path / "state", MNGR_AGENT_WORK_DIR=str(_REPO_ROOT)),
    )

    assert (started.returncode, started.stdout) == (0, "")
    assert list(markers.chat_dir(background_task_markers, _CHAT_ID).iterdir()) == []


@pytest.mark.parametrize("event", ["Stop", "PostToolUse", "SessionStart"])
def test_a_wired_grok_hook_whose_script_is_gone_still_exits_0(
    tmp_path: Path, event: str
) -> None:
    """grok keeps the hooks it started with, so a session can outlive the script (a worktree
    checked out at an older commit). python3 exits 2 on a missing script, and a Stop hook's exit 2
    keeps grok's turn going."""
    finished = _run_wired_hook(
        event,
        {},
        os.environ | _grok_env(tmp_path / "state", MNGR_AGENT_WORK_DIR=str(tmp_path)),
        cwd=tmp_path,
    )

    assert finished.returncode == 0, finished.stderr


def test_the_post_tool_use_hook_runs_only_for_spawn_subagent() -> None:
    """Every other tool's call would pay for a python3 start and record nothing."""
    group, _ = _wired_handler("PostToolUse")

    assert group["matcher"] == hooks.SPAWN_SUBAGENT_TOOL_NAME
