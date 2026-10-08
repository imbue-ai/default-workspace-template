"""A chat waiting on a Claude-native background command, with the real claude binary and the repo's own Stop hook.

Claude Code hands its Stop hook the turn's still-running tasks (``background_tasks`` in the hook input),
and the template's Stop hook (``background_tasks.py record-claude-stop``, wired in ``.claude/settings.json``)
copies them into the chat's markers. Nothing documents that field, so this runs the pinned claude with the
hook wired exactly as the repo wires it: one turn starts a background Bash command and ends, the command's
completion starts the next turn, and that turn's Stop hands over an empty list. The chat app must read the
chat as waiting on the command between the two, and idle once the second turn has ended.

``claude -p`` stays up while its background command runs and runs the turn its completion starts, as an
agent's Claude does. It is started through ``sh -c 'CLAUDE_PID=$$ exec claude ...'`` so the hook's
``CLAUDE_PID`` names the Claude process itself.

Marked ``real_claude``: it runs two real turns, so it needs the pinned claude on PATH, signed in. Skipped when
the binary is missing or is not the pinned version.
"""

import json
import os
import shlex
import shutil
import subprocess
import tomllib
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from imbue.chat.activity_state import ActivityState
from imbue.chat.testing import FIXTURE_AGENT_ID
from imbue.chat.testing import running_workspace
from imbue.chat.testing import seed_agent_state
from imbue.mngr.utils.polling import wait_for
from imbue.system_interface.testing import find_free_port

pytestmark = pytest.mark.real_claude

_REPO_ROOT = Path(__file__).resolve().parents[5]
_HOOK_SCRIPT_MARK = "background_tasks.py record-claude-stop"
_COMMAND_SECONDS = 20
_RUN_TIMEOUT_SECONDS = 240


def _pinned_claude_or_skip() -> str:
    """The real claude binary's path, resolved before the served workspace puts its stand-in on PATH."""
    claude = shutil.which("claude")
    if claude is None:
        pytest.skip("claude binary not on PATH")
    pinned = tomllib.loads((_REPO_ROOT / ".mngr" / "settings.toml").read_text())["agent_types"]["claude"]["version"]
    installed = subprocess.run([claude, "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
    if not installed.startswith(pinned):
        pytest.skip(f"claude on PATH is {installed!r}, not the pinned {pinned!r}")
    return claude


def _repo_stop_hook() -> dict[str, Any]:
    """The repo's own Stop hook entry that records Claude's background tasks, as ``.claude/settings.json`` has it."""
    settings = json.loads((_REPO_ROOT / ".claude" / "settings.json").read_text())
    hooks = [hook for group in settings["hooks"]["Stop"] for hook in group["hooks"]]
    (hook,) = [hook for hook in hooks if _HOOK_SCRIPT_MARK in hook["command"]]
    return hook


def _project_with_only_the_stop_hook(tmp_path: Path) -> Path:
    """A work dir whose ``.claude/settings.json`` wires the repo's Stop hook alone, with the repo's scripts beside it."""
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    (project / "system").mkdir()
    (project / "system" / "scripts").symlink_to(_REPO_ROOT / "system" / "scripts")
    (project / ".claude" / "settings.json").write_text(
        json.dumps({"hooks": {"Stop": [{"hooks": [_repo_stop_hook()]}]}})
    )
    return project


def _chat(chat_url: str) -> dict[str, Any]:
    with urllib.request.urlopen(f"{chat_url}/api/chats", timeout=5) as response:
        chats = json.loads(response.read())["chats"]
    (chat,) = [chat for chat in chats if chat["chat_id"] == FIXTURE_AGENT_ID]
    return chat


@pytest.mark.timeout(_RUN_TIMEOUT_SECONDS + 60)
def test_a_claude_turn_that_ends_on_a_background_bash_command_leaves_its_chat_waiting_until_the_next_turn(
    tmp_path: Path,
) -> None:
    claude = _pinned_claude_or_skip()
    project = _project_with_only_the_stop_hook(tmp_path)
    marker_root = tmp_path / "background_tasks"
    prompt = (
        f"Use the Bash tool with run_in_background set to true to run exactly `sleep {_COMMAND_SECONDS}`. "
        "Do not wait for it or check on it. Reply with the single word started. "
        "When it finishes, reply with the single word finished."
    )
    with running_workspace(
        tmp_path, find_free_port(), find_free_port(), background_tasks_root=marker_root
    ) as workspace:
        manager = workspace.chat_state.agent_manager
        agent = manager.get_agent_by_id(FIXTURE_AGENT_ID)
        assert agent is not None
        seed_agent_state(
            manager,
            FIXTURE_AGENT_ID,
            name=agent.name,
            state="WAITING",
            labels=agent.labels,
            activity_state=ActivityState.IDLE,
        )
        manager._agent_state_poller.start()

        env = {key: value for key, value in os.environ.items() if key != "MNGR_CLAUDE_SUBAGENT_PROXY_CHILD"}
        env.update(
            {
                "MINDS_CHAT_ID": FIXTURE_AGENT_ID,
                "MINDS_BACKGROUND_TASKS_DIR": str(marker_root),
                "MAIN_CLAUDE_SESSION_ID": "real-claude-background-task-test",
                "MNGR_AGENT_WORK_DIR": str(project),
            }
        )
        argv = [claude, "-p", "--model", "haiku", "--permission-mode", "bypassPermissions", prompt]
        run = subprocess.Popen(
            ["sh", "-c", f"CLAUDE_PID=$$ exec {shlex.join(argv)}"],
            cwd=project,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            wait_for(
                lambda: _chat(workspace.chat_url)["status"] == "background" or run.poll() is not None,
                timeout=_RUN_TIMEOUT_SECONDS / 2,
                poll_interval=0.2,
                error_message="the chat never read as waiting on the background command",
            )
            assert run.poll() is None, f"claude exited before its command finished:\n{run.communicate()[0]}"
            (task,) = _chat(workspace.chat_url)["active_agent"]["background_tasks"]
            assert (task["source"], task["kind"]) == ("claude", "shell")
            (marker_path,) = (marker_root / FIXTURE_AGENT_ID).glob("claude-*.json")
            marker = json.loads(marker_path.read_text())
            assert marker["pid"] == run.pid
            assert marker["command"] == f"sleep {_COMMAND_SECONDS}"

            output, _ = run.communicate(timeout=_RUN_TIMEOUT_SECONDS)
            assert run.returncode == 0, output
            # The command's completion started a turn, whose Stop handed over no tasks.
            assert list((marker_root / FIXTURE_AGENT_ID).glob("claude-*.json")) == []
            wait_for(lambda: _chat(workspace.chat_url)["status"] == "idle", timeout=3.0, poll_interval=0.1)
        finally:
            if run.poll() is None:
                run.kill()
                run.wait()
