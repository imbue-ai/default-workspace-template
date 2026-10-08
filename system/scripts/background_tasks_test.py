"""Tests for background_tasks.py: the markers, the Claude hooks that write them, and the CLI that
reads them through the chat app or, failing that, the files."""

from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from script_modules_testing import background_tasks

_CHAT_ID = "agent-0123456789abcdef0123456789abcdef"
_OTHER_CHAT_ID = "agent-fedcba9876543210fedcba9876543210"
_SCRIPT = Path(__file__).parent / "background_tasks.py"
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _task(
    task_id: str,
    started_at: str,
    pid: int | None = None,
    source: str = background_tasks.SOURCE_RUN_IN_BACKGROUND,
) -> Any:
    return background_tasks.BackgroundTask(
        source=source,
        id=task_id,
        description=f"Task {task_id}",
        started_at=started_at,
        pid=os.getpid() if pid is None else pid,
    )


def _main_claude_env(work_dir: Path, **extra: str) -> dict[str, str]:
    return {
        "MAIN_CLAUDE_SESSION_ID": "session-1",
        "MNGR_AGENT_ID": _CHAT_ID,
        "MNGR_AGENT_WORK_DIR": str(work_dir),
        "CLAUDE_PROJECT_DIR": str(work_dir),
        "CLAUDE_PID": str(os.getpid()),
        background_tasks.MARKER_ROOT_ENV: os.environ[background_tasks.MARKER_ROOT_ENV],
        **extra,
    }


def _stop_input(*entries: dict[str, Any]) -> io.StringIO:
    return io.StringIO(
        json.dumps({"hook_event_name": "Stop", "background_tasks": list(entries)})
    )


def _shell_entry(task_id: str, description: str = "tail logs") -> dict[str, Any]:
    return {
        "id": task_id,
        "type": "shell",
        "status": "running",
        "description": description,
        "command": "tail -f log",
    }


def test_only_live_well_formed_markers_count_oldest_first(
    background_task_markers: Path, exited_pid: int
) -> None:
    root = background_task_markers
    background_tasks.write_marker(
        root, _CHAT_ID, _task("newer", "2026-10-08T10:05:00+00:00")
    )
    background_tasks.write_marker(
        root, _CHAT_ID, _task("older", "2026-10-08T10:00:00+00:00")
    )
    background_tasks.write_marker(
        root, _CHAT_ID, _task("stale", "2026-10-08T09:00:00+00:00", pid=exited_pid)
    )
    chat_dir = background_tasks.chat_dir(root, _CHAT_ID)
    (chat_dir / "run_in_background-garbled.json").write_text("{not json")
    (chat_dir / "run_in_background-nopid.json").write_text(
        json.dumps(
            {
                "source": "run_in_background",
                "id": "nopid",
                "description": "",
                "started_at": "",
            }
        )
    )

    live = background_tasks.list_live_tasks(root, _CHAT_ID)

    assert [task.id for task in live] == ["older", "newer"]
    assert background_tasks.is_busy(root, _CHAT_ID)
    assert not background_tasks.is_busy(root, _OTHER_CHAT_ID)
    # A reader never deletes a stale marker; its writer does.
    assert (
        chat_dir / background_tasks.marker_file_name("run_in_background", "stale")
    ).exists()


def test_a_chat_whose_only_marker_is_stale_is_not_busy_and_not_listed(
    background_task_markers: Path, exited_pid: int
) -> None:
    root = background_task_markers
    background_tasks.write_marker(
        root, _CHAT_ID, _task("stale", "2026-10-08T09:00:00+00:00", pid=exited_pid)
    )
    background_tasks.write_marker(
        root, _OTHER_CHAT_ID, _task("live", "2026-10-08T09:00:00+00:00")
    )

    assert not background_tasks.is_busy(root, _CHAT_ID)
    assert list(background_tasks.live_tasks_by_chat(root)) == [_OTHER_CHAT_ID]


def test_a_rewritten_marker_replaces_the_old_one_and_leaves_no_temp_file(
    background_task_markers: Path,
) -> None:
    root = background_task_markers
    background_tasks.write_marker(
        root, _CHAT_ID, _task("one", "2026-10-08T10:00:00+00:00")
    )
    replacement = background_tasks.BackgroundTask(
        source="run_in_background",
        id="one",
        description="Renamed",
        started_at="2026-10-08T10:00:00+00:00",
        pid=os.getpid(),
    )
    background_tasks.write_marker(root, _CHAT_ID, replacement)

    assert background_tasks.list_live_tasks(root, _CHAT_ID) == [replacement]
    assert [
        path.name for path in background_tasks.chat_dir(root, _CHAT_ID).iterdir()
    ] == ["run_in_background-one.json"]


def test_a_stop_records_every_waking_kind_and_skips_the_rest(
    background_task_markers: Path, tmp_path: Path
) -> None:
    stdin = _stop_input(
        _shell_entry("shell-1"),
        {"id": "monitor-1", "type": "monitor", "description": "watch CI"},
        {"id": "workflow-1", "type": "workflow", "description": "review"},
        {"id": "agent-1", "type": "subagent", "description": "explore"},
        {"id": "cloud-1", "type": "cloud session", "description": "elsewhere"},
        {"type": "shell", "description": "no id"},
    )

    assert (
        background_tasks.main(["record-claude-stop"], _main_claude_env(tmp_path), stdin)
        == 0
    )

    live = background_tasks.list_live_tasks(background_task_markers, _CHAT_ID)
    assert {(task.id, task.kind) for task in live} == {
        ("shell-1", "shell"),
        ("monitor-1", "monitor"),
        ("workflow-1", "workflow"),
        ("agent-1", "subagent"),
    }
    assert all(task.source == "claude" and task.pid == os.getpid() for task in live)
    [shell] = [task for task in live if task.id == "shell-1"]
    assert (shell.description, shell.command) == ("tail logs", "tail -f log")


def test_a_later_stop_keeps_a_listed_tasks_start_and_prunes_the_unlisted_ones(
    background_task_markers: Path, tmp_path: Path
) -> None:
    env = _main_claude_env(tmp_path)
    root = background_task_markers
    background_tasks.main(
        ["record-claude-stop"],
        env,
        _stop_input(_shell_entry("kept"), _shell_entry("done")),
    )
    [kept_before] = [
        t for t in background_tasks.list_live_tasks(root, _CHAT_ID) if t.id == "kept"
    ]
    background_tasks.write_marker(
        root, _CHAT_ID, _task("runner", "2026-10-08T10:00:00+00:00")
    )
    # Its start is backdated, so a rewrite with a fresh start would show.
    background_tasks.write_marker(
        root,
        _CHAT_ID,
        background_tasks.BackgroundTask(
            source="claude",
            id="kept",
            description=kept_before.description,
            started_at="2026-10-08T09:00:00+00:00",
            pid=kept_before.pid,
            kind="shell",
            command=kept_before.command,
        ),
    )

    background_tasks.main(
        ["record-claude-stop"], env, _stop_input(_shell_entry("kept"))
    )

    live = {task.id: task for task in background_tasks.list_live_tasks(root, _CHAT_ID)}
    assert set(live) == {"kept", "runner"}
    assert live["kept"].started_at == "2026-10-08T09:00:00+00:00"


def test_a_stop_with_no_task_list_leaves_the_markers_alone(
    background_task_markers: Path, tmp_path: Path
) -> None:
    env = _main_claude_env(tmp_path)
    background_tasks.main(["record-claude-stop"], env, _stop_input(_shell_entry("one")))

    background_tasks.main(
        ["record-claude-stop"],
        env,
        io.StringIO(json.dumps({"hook_event_name": "Stop"})),
    )
    background_tasks.main(["record-claude-stop"], env, io.StringIO("not json"))

    assert [
        t.id
        for t in background_tasks.list_live_tasks(background_task_markers, _CHAT_ID)
    ] == ["one"]


def test_clearing_removes_only_the_claude_markers(
    background_task_markers: Path, tmp_path: Path
) -> None:
    env = _main_claude_env(tmp_path)
    root = background_task_markers
    background_tasks.main(["record-claude-stop"], env, _stop_input(_shell_entry("one")))
    background_tasks.write_marker(
        root, _CHAT_ID, _task("runner", "2026-10-08T10:00:00+00:00")
    )

    assert background_tasks.main(["clear-claude"], env) == 0

    assert [t.source for t in background_tasks.list_live_tasks(root, _CHAT_ID)] == [
        "run_in_background"
    ]


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
    background_task_markers: Path, tmp_path: Path, overrides: dict[str, str]
) -> None:
    env = _main_claude_env(tmp_path) | overrides

    assert (
        background_tasks.main(
            ["record-claude-stop"], env, _stop_input(_shell_entry("one"))
        )
        == 0
    )

    assert not background_task_markers.exists()


def test_the_chat_app_stamped_chat_id_keys_the_claude_markers(
    background_task_markers: Path, tmp_path: Path
) -> None:
    env = _main_claude_env(tmp_path, MINDS_CHAT_ID=_OTHER_CHAT_ID)

    background_tasks.main(["record-claude-stop"], env, _stop_input(_shell_entry("one")))

    assert background_tasks.is_busy(background_task_markers, _OTHER_CHAT_ID)
    assert not background_tasks.is_busy(background_task_markers, _CHAT_ID)


def _wired_command(event: str) -> str:
    """The background_tasks.py command ``.claude/settings.json`` runs for ``event``."""
    settings = json.loads((_REPO_ROOT / ".claude" / "settings.json").read_text())
    [command] = [
        hook["command"]
        for matcher in settings["hooks"][event]
        for hook in matcher["hooks"]
        if "background_tasks.py" in hook["command"]
    ]
    return command


def test_the_wired_claude_hooks_record_a_stop_and_clear_at_the_next_session_start(
    background_task_markers: Path,
) -> None:
    """Runs the hook commands exactly as ``.claude/settings.json`` spells them, as Claude does."""
    env = os.environ | _main_claude_env(_REPO_ROOT)
    stop_input = json.dumps(
        {"background_tasks": [_shell_entry("one", "Build the site")]}
    )

    subprocess.run(
        ["sh", "-c", _wired_command("Stop")],
        input=stop_input,
        text=True,
        env=env,
        cwd=_REPO_ROOT,
        check=True,
    )
    [task] = background_tasks.list_live_tasks(background_task_markers, _CHAT_ID)
    assert (task.description, task.pid) == ("Build the site", os.getpid())

    subprocess.run(
        ["sh", "-c", _wired_command("SessionStart")],
        input=json.dumps({"hook_event_name": "SessionStart", "source": "resume"}),
        text=True,
        env=env,
        cwd=_REPO_ROOT,
        check=True,
    )
    assert background_tasks.list_live_tasks(background_task_markers, _CHAT_ID) == []


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


def _run_cli(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def _agents_answer(*agents: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    return 200, {"agents": list(agents)}


def test_list_answers_from_the_chat_app_with_agent_names(
    fake_chat_app: Any, background_task_markers: Path, tmp_path: Path
) -> None:
    marker = _task("from-app", "2026-10-08T10:00:00+00:00").to_json()
    fake_chat_app.get_answers[background_tasks.AGENTS_PATH] = _agents_answer(
        {
            "id": "agent-active",
            "name": "lead",
            "state": "WAITING",
            "chat_id": _CHAT_ID,
            "is_busy": True,
            "background_tasks": [marker],
        },
        {
            "id": "agent-idle",
            "name": "other",
            "state": "WAITING",
            "chat_id": "agent-idle",
            "is_busy": False,
            "background_tasks": [],
        },
    )
    # The files disagree; the chat app is the authority while it answers.
    background_tasks.write_marker(
        background_task_markers,
        _OTHER_CHAT_ID,
        _task("on-disk", "2026-10-08T10:00:00+00:00"),
    )

    listed = _run_cli("list", "--format", "tsv", cwd=tmp_path)

    assert listed.returncode == 0, listed.stderr
    assert listed.stdout.splitlines() == [f"{_CHAT_ID}\tagent-active\tlead\ttrue\t1"]


def test_is_busy_counts_a_turn_in_flight_the_chat_app_reports(
    fake_chat_app: Any, tmp_path: Path
) -> None:
    fake_chat_app.get_answers[background_tasks.AGENTS_PATH] = _agents_answer(
        {
            "id": _CHAT_ID,
            "name": "lead",
            "state": "RUNNING",
            "chat_id": _CHAT_ID,
            "is_busy": True,
            "background_tasks": [],
        },
    )

    assert _run_cli("is-busy", _CHAT_ID, cwd=tmp_path).returncode == 0
    assert _run_cli("is-busy", _OTHER_CHAT_ID, cwd=tmp_path).returncode == 1


def test_a_chat_app_from_before_the_busy_fields_falls_back_to_the_files(
    fake_chat_app: Any, background_task_markers: Path, tmp_path: Path
) -> None:
    fake_chat_app.get_answers[background_tasks.AGENTS_PATH] = _agents_answer(
        {"id": _CHAT_ID, "name": "lead", "state": "WAITING"}
    )
    background_tasks.write_marker(
        background_task_markers, _CHAT_ID, _task("on-disk", "2026-10-08T10:00:00+00:00")
    )

    listed = _run_cli("list", cwd=tmp_path)

    assert listed.returncode == 0, listed.stderr
    answer = json.loads(listed.stdout)
    assert answer["source"] == "files"
    [chat] = answer["chats"]
    assert (chat["chat_id"], chat["active_agent_id"], chat["active_agent_name"]) == (
        _CHAT_ID,
        _CHAT_ID,
        "",
    )
    assert [task["id"] for task in chat["background_tasks"]] == ["on-disk"]
    assert _run_cli("is-busy", _CHAT_ID, cwd=tmp_path).returncode == 0


def test_an_unreachable_chat_app_falls_back_to_the_files(
    background_task_markers: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = tmp_path / "apps.toml"
    # A port nothing listens on: bind one, then let it go.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    registry.write_text(f'[[apps]]\nname = "chat"\nurl = "http://127.0.0.1:{port}"\n')
    monkeypatch.setenv("MINDS_APPS_FILE", str(registry))
    background_tasks.write_marker(
        background_task_markers, _CHAT_ID, _task("on-disk", "2026-10-08T10:00:00+00:00")
    )

    listed = _run_cli("list", "--chat", _CHAT_ID, "--format", "tsv", cwd=tmp_path)

    assert listed.stdout.splitlines() == [f"{_CHAT_ID}\t{_CHAT_ID}\t\ttrue\t1"]
    assert _run_cli("is-busy", _OTHER_CHAT_ID, cwd=tmp_path).returncode == 1


def test_a_worktrees_markers_resolve_to_the_main_checkout(
    tmp_path: Path, main_checkout_and_worktree: tuple[Path, Path]
) -> None:
    """A worker runs the scripts from its own worktree; its markers must land in the checkout the
    chat app reads."""
    main, worktree = main_checkout_and_worktree

    assert background_tasks.main_checkout(worktree) == main.resolve()
    assert background_tasks.main_checkout(main) == main
    assert background_tasks.main_checkout(tmp_path) == tmp_path


@pytest.mark.skipif(
    not Path("/proc/self/stat").exists(),
    reason="needs Linux's /proc/<pid>/stat for a process's start time",
)
def test_a_marker_whose_pid_now_belongs_to_another_process_is_stale(
    background_task_markers: Path,
) -> None:
    """After a restart pids are handed out again; a marker naming a recycled pid must not count."""
    own_start = background_tasks.process_start_time(os.getpid())
    for task_id, pid_start in (
        ("same", own_start),
        ("recycled", str(int(own_start) + 1)),
    ):
        background_tasks.write_marker(
            background_task_markers,
            _CHAT_ID,
            background_tasks.BackgroundTask(
                source="run_in_background",
                id=task_id,
                description="",
                started_at="2026-10-08T10:00:00+00:00",
                pid=os.getpid(),
                pid_start=pid_start,
            ),
        )

    assert [
        t.id
        for t in background_tasks.list_live_tasks(background_task_markers, _CHAT_ID)
    ] == ["same"]
