"""A chat waiting on a background task, end to end: the real runner, the real chat app, the real CLI.

``system/scripts/run_in_background.py`` runs a command detached, writes its marker into the chat's
directory, and delivers the report through ``message_chat.py`` to the chat app, which sends it on to
the agent. Here the chat app is served the way a workspace serves it (``running_workspace``: real
routes and a real state poller, over a recording messenger in place of ``mngr message``), so the
report's turn itself is not run: what is pinned is everything the chat app and its readers see from
the moment the runner returns to the moment its report is handed to the agent. The Claude-native
path (the Stop hook copying Claude's own task list) needs a live Claude agent and is covered in
``test_background_tasks_real_claude.py``.
"""

import json
import os
import queue
import signal
import subprocess
import sys
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from imbue.chat.activity_state import ActivityState
from imbue.chat.chat_records import ChatRecord
from imbue.chat.primitives import ChatId
from imbue.chat.testing import FIXTURE_AGENT_ID
from imbue.chat.testing import RecordingMngrMessenger
from imbue.chat.testing import RunningWorkspace
from imbue.chat.testing import make_chat_agent_entry
from imbue.chat.testing import running_workspace
from imbue.chat.testing import seed_agent_state
from imbue.mngr.utils.polling import wait_for
from imbue.system_interface.testing import find_free_port

_REPO_ROOT = Path(__file__).resolve().parents[5]
_RUNNER = _REPO_ROOT / "system" / "scripts" / "run_in_background.py"
_CLI = _REPO_ROOT / "system" / "scripts" / "background_tasks.py"
_REPORT_TAG = "<background-task-report>"
_SUCCESSOR_ID = "agent-5ecc5ecc5ecc5ecc5ecc5ecc5ecc5ecc"

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the runner detaches with POSIX sessions")


@contextmanager
def _waiting_workspace(tmp_path: Path, messenger: RecordingMngrMessenger) -> Iterator[RunningWorkspace]:
    """The served workspace with its fixture chat between turns, its markers under ``tmp_path``, and its poller on."""
    with running_workspace(
        tmp_path,
        find_free_port(),
        find_free_port(),
        additional_agents=((_SUCCESSOR_ID, "Chat-successor"),),
        messenger=messenger,
        background_tasks_root=tmp_path / "background_tasks",
    ) as workspace:
        manager = workspace.chat_state.agent_manager
        for agent_id in (FIXTURE_AGENT_ID, _SUCCESSOR_ID):
            agent = manager.get_agent_by_id(agent_id)
            assert agent is not None
            seed_agent_state(
                manager,
                agent_id,
                name=agent.name,
                state="WAITING",
                labels=agent.labels,
                activity_state=ActivityState.IDLE,
            )
        manager._agent_state_poller.start()
        yield workspace


def _start_runner(workspace: RunningWorkspace, tmp_path: Path, description: str, *command: str) -> None:
    """Run the real runner for the fixture chat, as an agent's tool call does: it returns once the command is detached."""
    finished = subprocess.run(
        [sys.executable, str(_RUNNER), "--chat-id", FIXTURE_AGENT_ID, "--description", description, "--", *command],
        cwd=tmp_path / "work",
        env={**os.environ, "MINDS_CHAT_ID": FIXTURE_AGENT_ID},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert finished.returncode == 0, finished.stderr


def _chat(workspace: RunningWorkspace) -> dict[str, Any]:
    with urllib.request.urlopen(f"{workspace.chat_url}/api/chats", timeout=5) as response:
        chats = json.loads(response.read())["chats"]
    (chat,) = [chat for chat in chats if chat["chat_id"] == FIXTURE_AGENT_ID]
    return chat


def _cli_says_busy() -> bool:
    """``background_tasks.py is-busy``, the in-workspace CLI Studio and the skills run, asking the served chat app."""
    finished = subprocess.run(
        [sys.executable, str(_CLI), "is-busy", FIXTURE_AGENT_ID],
        env=dict(os.environ),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert finished.returncode in (0, 1), finished.stderr
    return finished.returncode == 0


def _cli_listing() -> dict[str, Any]:
    finished = subprocess.run(
        [sys.executable, str(_CLI), "list", "--chat", FIXTURE_AGENT_ID, "--format", "json"],
        env=dict(os.environ),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert finished.returncode == 0, finished.stderr
    return json.loads(finished.stdout)


def _reports_sent(messenger: RecordingMngrMessenger) -> list[tuple[str, str]]:
    return [(agent_id, text) for agent_id, text in messenger.sent if _REPORT_TAG in text]


def _statuses_pushed(client_queue: "queue.Queue[str | None]") -> list[str]:
    statuses: list[str] = []
    while not client_queue.empty():
        raw = client_queue.get_nowait()
        if raw is None:
            break
        message = json.loads(raw)
        if message.get("type") != "chats_updated":
            continue
        statuses.extend(chat["status"] for chat in message["chats"] if chat["chat_id"] == FIXTURE_AGENT_ID)
    return statuses


@pytest.mark.timeout(90)
def test_a_chat_waiting_on_the_runner_reads_background_until_the_report_is_handed_over(tmp_path: Path) -> None:
    messenger = RecordingMngrMessenger(sent=[])
    with _waiting_workspace(tmp_path, messenger) as workspace:
        client_queue = workspace.chat_state.agent_manager.broadcaster.register()
        _start_runner(workspace, tmp_path, "Wait for the image build", "sleep", "6")

        wait_for(lambda: _chat(workspace)["status"] == "background", timeout=10.0, poll_interval=0.2)
        chat = _chat(workspace)
        assert chat["active_agent"]["is_busy"] is True
        assert [task["description"] for task in chat["active_agent"]["background_tasks"]] == [
            "Wait for the image build"
        ]
        assert _cli_says_busy()
        listing = _cli_listing()
        assert listing["source"] == "chat_app"
        assert [
            (chat["chat_id"], chat["active_agent_id"], [task["description"] for task in chat["background_tasks"]])
            for chat in listing["chats"]
        ] == [(FIXTURE_AGENT_ID, FIXTURE_AGENT_ID, ["Wait for the image build"])]

        wait_for(
            lambda: _reports_sent(messenger) != [] and _chat(workspace)["status"] == "idle",
            timeout=40.0,
            poll_interval=0.2,
            error_message="the report was never handed over, or the chat stayed busy after it",
        )
        assert [agent_id for agent_id, _text in _reports_sent(messenger)] == [FIXTURE_AGENT_ID]
        assert _chat(workspace)["active_agent"]["background_tasks"] == []
        assert not _cli_says_busy()

        # The chat never read idle while it waited: the green check could not have been earned early.
        statuses = _statuses_pushed(client_queue)
        first_wait, last_wait = statuses.index("background"), len(statuses) - 1 - statuses[::-1].index("background")
        assert "idle" not in statuses[first_wait:last_wait]
        assert statuses[-1] == "idle"


@pytest.mark.timeout(90)
def test_a_chat_whose_runner_is_killed_returns_to_idle_within_a_poll(tmp_path: Path) -> None:
    messenger = RecordingMngrMessenger(sent=[])
    with _waiting_workspace(tmp_path, messenger) as workspace:
        _start_runner(workspace, tmp_path, "A long wait", "sleep", "30")
        wait_for(lambda: _chat(workspace)["status"] == "background", timeout=10.0, poll_interval=0.2)
        (marker,) = (tmp_path / "background_tasks" / FIXTURE_AGENT_ID).glob("*.json")
        runner_pid = json.loads(marker.read_text())["pid"]

        # What the memory shedder does: SIGKILL, so the runner removes nothing and reports nothing.
        os.killpg(os.getpgid(runner_pid), signal.SIGKILL)

        wait_for(
            lambda: _chat(workspace)["status"] == "idle",
            timeout=3.0,
            poll_interval=0.1,
            error_message="the chat still read busy after its runner was killed",
        )
        assert marker.exists()
        assert _reports_sent(messenger) == []


@pytest.mark.timeout(90)
def test_a_handoff_during_the_wait_leaves_the_successor_waiting_and_the_report_lands_on_it(tmp_path: Path) -> None:
    messenger = RecordingMngrMessenger(sent=[])
    with _waiting_workspace(tmp_path, messenger) as workspace:
        manager = workspace.chat_state.agent_manager
        _start_runner(workspace, tmp_path, "Wait for the migration", "sleep", "8")
        wait_for(lambda: _chat(workspace)["status"] == "background", timeout=10.0, poll_interval=0.2)

        # The handoff lands: the chat's record names the successor as its active agent.
        manager._chat_record_store.write(
            ChatRecord(
                chat_id=ChatId(FIXTURE_AGENT_ID),
                agents=(
                    make_chat_agent_entry(1, FIXTURE_AGENT_ID, is_archived=True, final_event_count=2),
                    make_chat_agent_entry(2, _SUCCESSOR_ID, is_archived=False),
                ),
            )
        )
        manager.refresh_chat_records()

        wait_for(
            lambda: _chat(workspace)["active_agent"]["agent_id"] == _SUCCESSOR_ID,
            timeout=5.0,
            poll_interval=0.1,
        )
        chat = _chat(workspace)
        assert chat["status"] == "background"
        assert [task["description"] for task in chat["active_agent"]["background_tasks"]] == ["Wait for the migration"]

        wait_for(
            lambda: _reports_sent(messenger) != [] and _chat(workspace)["status"] == "idle",
            timeout=40.0,
            poll_interval=0.2,
            error_message="the report was never handed to the successor, or the chat stayed busy after it",
        )
        assert [agent_id for agent_id, _text in _reports_sent(messenger)] == [_SUCCESSOR_ID]
