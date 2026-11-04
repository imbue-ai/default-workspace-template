"""A chat waiting on a Claude-native background command, with the real claude binary and the repo's own Stop hook.

Claude Code hands its Stop hook the turn's still-running tasks (``background_tasks`` in the hook input),
and the template's Stop hook (``claude_background_task_hooks.py stop``, wired in ``.claude/settings.json``)
copies them into the chat's markers. Nothing documents that field, so this runs the pinned claude with the
hook wired exactly as the repo wires it: one turn starts a background Bash command and ends, the command's
completion starts the next turn, and that turn's Stop hands over an empty list. The chat app must read the
chat as waiting on the command between the two, and idle once the second turn has ended. The command's end is
also recorded in the session transcript as an enqueued ``<task-notification>``, which the chat app reads to drop
a finished task's marker when no Stop follows; it must name the task by the id the Stop hook recorded.

``claude -p`` stays up while its background command runs and runs the turn its completion starts, as an
agent's Claude does. It is started through ``sh -c 'CLAUDE_PID=$$ exec claude ...'`` so the hook's
``CLAUDE_PID`` names the Claude process itself.

Marked ``real_claude``: it runs two real turns, so it needs the pinned claude on PATH and a signed-in Claude
account in the workspace's account store. A test run carries no agent's ``CLAUDE_CONFIG_DIR`` (pytest-workspace-env
clears it), so the account's folder is named to claude here. Skipped when the binary is missing or is not the
pinned version, or when no Claude account is signed in.
"""

import json
import os
import shlex
import shutil
import subprocess
import tomllib
import urllib.request
import uuid
from pathlib import Path
from typing import Any

import pytest

from imbue.chat.accounts import account_dir
from imbue.chat.accounts import choose_default_account
from imbue.chat.accounts import harness_for
from imbue.chat.accounts import read_index
from imbue.chat.activity_state import ActivityState
from imbue.chat.harnesses.claude.auth import MANAGED_AUTH_ENV_KEYS
from imbue.chat.harnesses.claude.session_files import find_session_file
from imbue.chat.harnesses.claude.session_parser import finished_task_id
from imbue.chat.harnesses.claude.session_parser import parse_queue_signals
from imbue.chat.harnesses.harness_type import HarnessType
from imbue.chat.harnesses.registry import build_account_binding
from imbue.chat.harnesses.signed_in import SignedIn
from imbue.chat.harnesses.signed_in import is_signed_in
from imbue.chat.testing import FIXTURE_AGENT_ID
from imbue.chat.testing import running_workspace
from imbue.chat.testing import seed_agent_state
from imbue.imbue_common.model_update import to_update
from imbue.mngr.utils.polling import wait_for
from imbue.system_interface.testing import find_free_port

pytestmark = pytest.mark.real_claude

_REPO_ROOT = Path(__file__).resolve().parents[5]
_HOOK_SCRIPT_MARK = "claude_background_task_hooks.py stop"
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


def _signed_in_claude_account_or_skip() -> Path:
    """The folder of the Claude account the default-account rule picks among the workspace's Claude accounts.

    Named by home: the conftest points ``MINDS_ACCOUNTS_ROOT`` at an empty store of the test's own, and the default
    lookup follows it."""
    home = Path.home()
    index = read_index(home)
    claude_accounts = tuple(account for account in index.accounts if harness_for(account) is HarnessType.CLAUDE)
    account = choose_default_account(index.model_copy_update(to_update(index.field_ref().accounts, claude_accounts)))
    if account is None:
        pytest.skip("no Claude account in this workspace's account store")
    folder = account_dir(account.id, home)
    signed_in = is_signed_in(HarnessType.CLAUDE, folder)
    if signed_in is not SignedIn.YES:
        pytest.skip(
            f"the Claude account in {folder} is not confirmed signed in (its sign-in check answered {signed_in})"
        )
    return folder


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


def _finished_task_ids_in_transcript(claude_config_dir: Path, session_id: str) -> set[str]:
    """The ids of the tasks the session's queue ledger reports as over, read as the chat app's Claude watcher reads them."""
    transcript = find_session_file(claude_config_dir / "projects", session_id)
    assert transcript is not None, f"no transcript for session {session_id}"
    signals = (parse_queue_signals(line) for line in transcript.read_text().splitlines())
    return {
        task_id
        for task_id in (finished_task_id(signal.content) for signal in signals if signal is not None)
        if task_id is not None
    }


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
    claude_config_dir = _signed_in_claude_account_or_skip()
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
        manager._background_tasks_poller.start()

        # The account folder alone authenticates the run, not a credential the test session happens to carry.
        env = {
            key: value
            for key, value in os.environ.items()
            if key != "MNGR_CLAUDE_SUBAGENT_PROXY_CHILD" and key not in MANAGED_AUTH_ENV_KEYS
        }
        env.update(build_account_binding(HarnessType.CLAUDE).account_env(claude_config_dir))
        env.update(
            {
                "MINDS_CHAT_ID": FIXTURE_AGENT_ID,
                "CHAT_BACKGROUND_TASKS_DIR": str(marker_root),
                "MAIN_CLAUDE_SESSION_ID": "real-claude-background-task-test",
                "MNGR_AGENT_WORK_DIR": str(project),
            }
        )
        session_id = str(uuid.uuid4())
        argv = [
            claude,
            "-p",
            "--model",
            "haiku",
            "--permission-mode",
            "bypassPermissions",
            "--session-id",
            session_id,
            prompt,
        ]
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
            assert (task["source"], task["harness"], task["kind"]) == ("harness", "claude", "shell")
            (marker_path,) = (marker_root / FIXTURE_AGENT_ID).glob("harness-claude-*.json")
            marker = json.loads(marker_path.read_text())
            assert marker["pid"] == run.pid
            assert marker["command"] == f"sleep {_COMMAND_SECONDS}"
            assert marker_path.name == f"harness-claude-{task['id']}.json"

            output, _ = run.communicate(timeout=_RUN_TIMEOUT_SECONDS)
            assert run.returncode == 0, output
            # The command's completion started a turn, whose Stop handed over no tasks.
            assert list((marker_root / FIXTURE_AGENT_ID).glob("harness-claude-*.json")) == []
            wait_for(lambda: _chat(workspace.chat_url)["status"] == "idle", timeout=3.0, poll_interval=0.1)
            assert task["id"] in _finished_task_ids_in_transcript(claude_config_dir, session_id)
        finally:
            if run.poll() is None:
                run.kill()
                run.wait()
