"""Tests for restarting the workspace's agents after an update: which chats are restarted, the
chat list's retries, and the pass's own restart from a detached helper.

The chat app is the ``fake_chat_list`` fixture over loopback; ``message_chat.py`` is a recorder
installed in the workspace's tree, as the release's own copy would be.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
import update_agent_restarts
import update_runtime

_OWN_CHAT = "agent-00000000000000000000000000000001"
_UPDATE_SELF_SCRIPT = Path(__file__).with_name("update_self.py")
_DELIVERY_DEADLINE_SECONDS = 8.0


def _chat(
    chat_id: str,
    title: str,
    status: str,
    state: str,
    name: str = "",
    handoff: dict[str, str] | None = None,
) -> dict[str, Any]:
    """One chat of ``GET /api/chats``, in the chat app's ``ChatSnapshot`` shape (the fields read here)."""
    return {
        "chat_id": chat_id,
        "title": title,
        "name": name or title.lower().replace(" ", "-"),
        "status": status,
        "handoff": handoff,
        "active_agent": {"agent_id": chat_id, "state": state},
    }


def _install_message_chat_recorder(workspace: Path) -> Path:
    """A ``message_chat.py`` that records its argv, the note it was given, and whether it ran
    with an agent id, and fails for the chat ids listed in ``$FAKE_INTERRUPT_FAILS``."""
    record = workspace / "message-chat-calls.jsonl"
    script = workspace / "system" / "scripts" / "message_chat.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "argv = sys.argv[1:]\n"
        "note = open(argv[argv.index('--message-file') + 1]).read() if '--message-file' in argv else None\n"
        f"with open({str(record)!r}, 'a') as handle:\n"
        "    handle.write(json.dumps({'argv': argv, 'note': note, 'agent_id': os.environ.get('MNGR_AGENT_ID')}) + '\\n')\n"
        "if argv[0] in os.environ.get('FAKE_INTERRUPT_FAILS', '').split(','):\n"
        "    sys.stderr.write('the chat is converging')\n"
        "    raise SystemExit(1)\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return record


def _calls(record: Path) -> list[dict[str, Any]]:
    if not record.exists():
        return []
    return [json.loads(line) for line in record.read_text().splitlines()]


def test_only_idle_chats_other_than_the_pass_and_its_worker_are_restarted(
    fake_chat_list: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = fake_chat_list.workspace
    record = _install_message_chat_recorder(workspace)
    monkeypatch.setenv("FAKE_INTERRUPT_FAILS", "agent-failing")
    fake_chat_list.answers = [
        (
            200,
            {
                "chats": [
                    _chat(_OWN_CHAT, "Update", "idle", "WAITING"),
                    _chat(
                        "agent-worker",
                        "Update worker",
                        "idle",
                        "WAITING",
                        name="update-self",
                    ),
                    _chat("agent-idle", "Trip planning", "idle", "WAITING"),
                    _chat("agent-failing", "Budget", "idle", "WAITING"),
                    _chat("agent-working", "Research", "working", "RUNNING"),
                    # Between an assistant message and its next tool call the transcript reads
                    # idle while the harness is still mid-turn.
                    _chat("agent-between-steps", "Drafts", "idle", "RUNNING"),
                    _chat("agent-dialog", "Inbox", "attention", "WAITING"),
                    _chat(
                        "agent-handoff",
                        "Notes",
                        "working",
                        "WAITING",
                        handoff={"phase": "summarizing"},
                    ),
                    _chat("agent-stopped", "Old chat", "stopped", "STOPPED"),
                ]
            },
        )
    ]

    report = update_agent_restarts.restart_idle_agents(
        workspace, _OWN_CHAT, update_runtime.HttpClient(), update_runtime.Runner()
    )

    assert [call["argv"] for call in _calls(record)] == [
        ["agent-idle", "--interrupt"],
        ["agent-failing", "--interrupt"],
    ]
    assert report["restarted"] == [{"chat_id": "agent-idle", "title": "Trip planning"}]
    assert report["failed"] == [
        {
            "chat_id": "agent-failing",
            "title": "Budget",
            "detail": "the chat is converging",
        }
    ]
    assert [(chat["chat_id"], chat["status"]) for chat in report["left_running"]] == [
        ("agent-working", "working"),
        ("agent-between-steps", "idle"),
        ("agent-dialog", "attention"),
        ("agent-handoff", "working"),
    ]
    assert (
        json.loads(
            (workspace / update_agent_restarts.AGENT_RESTARTS_REPORT_REL).read_text()
        )
        == report
    )


class _ScriptedHttp(update_runtime.HttpClient):
    """Answers the chat-list reads from a script, one page (or None for no answer) per read."""

    def __init__(self, pages: list[update_runtime.FetchedPage | None]) -> None:
        self._pages = pages
        self.reads = 0

    def get_page(self, url: str, timeout: float) -> update_runtime.FetchedPage | None:
        self.reads += 1
        return self._pages.pop(0) if len(self._pages) > 1 else self._pages[0]


class _FakeClock:
    """A clock that advances by ``step`` on every read."""

    def __init__(self, step: float) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def _registered_workspace(tmp_path: Path) -> Path:
    registry = tmp_path / "data" / ".state" / "apps.toml"
    registry.parent.mkdir(parents=True)
    registry.write_text('[[apps]]\nname = "chat"\nurl = "http://127.0.0.1:9"\n')
    return tmp_path


def _chat_list_page(*chats: dict[str, Any]) -> update_runtime.FetchedPage:
    return update_runtime.FetchedPage(
        status=200,
        body=json.dumps({"chats": list(chats)}),
        headers={"content-type": "application/json"},
    )


def test_the_chat_list_is_retried_while_the_restarted_chat_app_comes_back(
    tmp_path: Path,
) -> None:
    not_ready = update_runtime.FetchedPage(status=503, body="{}", headers={})
    http = _ScriptedHttp(
        [
            None,
            not_ready,
            _chat_list_page(_chat("agent-idle", "Trip", "idle", "WAITING")),
        ]
    )
    slept: list[float] = []

    chats = update_agent_restarts.fetch_chat_list(
        _registered_workspace(tmp_path), http, _FakeClock(0.0), slept.append
    )

    assert [chat.chat_id for chat in chats] == ["agent-idle"]
    assert slept == [update_agent_restarts.CHAT_LIST_RETRY_INTERVAL_SECONDS] * 2


def test_a_chat_app_that_never_answers_restarts_nothing_and_says_why(
    tmp_path: Path,
) -> None:
    http = _ScriptedHttp(
        [update_runtime.FetchedPage(status=503, body="{}", headers={})]
    )

    with pytest.raises(
        update_agent_restarts.ChatListUnavailableError, match="HTTP 503"
    ):
        update_agent_restarts.fetch_chat_list(
            _registered_workspace(tmp_path),
            http,
            _FakeClock(update_agent_restarts.CHAT_LIST_RETRY_WINDOW_SECONDS / 3),
            lambda _: None,
        )
    assert http.reads > 1


def test_the_pass_waits_for_its_own_turn_to_end_not_a_transcript_that_reads_idle(
    tmp_path: Path,
) -> None:
    workspace = _registered_workspace(tmp_path)
    http = _ScriptedHttp(
        [
            _chat_list_page(_chat(_OWN_CHAT, "Update", "working", "RUNNING")),
            _chat_list_page(_chat(_OWN_CHAT, "Update", "idle", "RUNNING")),
            None,
            _chat_list_page(_chat(_OWN_CHAT, "Update", "idle", "WAITING")),
        ]
    )

    is_idle = update_agent_restarts.wait_for_idle_chat(
        lambda: update_agent_restarts.read_listed_chat(workspace, _OWN_CHAT, http),
        deadline_seconds=60.0,
        poll_seconds=1.0,
        monotonic=_FakeClock(0.0),
        sleep=lambda _: None,
    )

    assert is_idle
    assert http.reads == 4


def test_the_note_stands_on_its_own_and_carries_the_chats_left_running() -> None:
    note = update_agent_restarts.compose_self_restart_note(
        "Claude Code  2.1.300\n",
        [{"chat_id": "agent-working", "title": "Research", "status": "working"}],
    )

    assert note.startswith(
        "<background-task-report>\n<summary>Restarted to finish the update</summary>\n"
    )
    assert "what the update installed: Claude Code 2.1.300." in note
    assert '"Research"' in note
    assert str(update_agent_restarts.AGENT_RESTARTS_REPORT_REL) in note


def test_restart_self_restarts_the_chat_from_a_helper_the_restart_cannot_kill(
    fake_chat_list: Any,
) -> None:
    """The helper is detached and carries no agent id, so the stop half of the restart, which
    kills every process carrying the chat's agent id, leaves it alive to send the note."""
    workspace = fake_chat_list.workspace
    record = _install_message_chat_recorder(workspace)
    report_path = workspace / update_agent_restarts.AGENT_RESTARTS_REPORT_REL
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(
            {
                "restarted": [],
                "left_running": [
                    {
                        "chat_id": "agent-working",
                        "title": "Research",
                        "status": "working",
                    }
                ],
                "failed": [],
            }
        )
    )
    fake_chat_list.answers = [
        (200, {"chats": [_chat(_OWN_CHAT, "Update", "idle", "WAITING")]})
    ]
    env = {key: value for key, value in os.environ.items() if key != "MINDS_CHAT_ID"}
    env["MNGR_AGENT_ID"] = _OWN_CHAT

    started = subprocess.run(
        [
            sys.executable,
            str(_UPDATE_SELF_SCRIPT),
            "restart-self",
            "--reason",
            "Claude Code 2.1.300",
            "--repo-root",
            str(workspace),
        ],
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert started.returncode == 0, started.stderr

    deadline = time.monotonic() + _DELIVERY_DEADLINE_SECONDS
    while not _calls(record):
        assert time.monotonic() < deadline, "the helper never restarted the chat"
        time.sleep(0.1)
    [call] = _calls(record)
    assert call["argv"][:2] == [_OWN_CHAT, "--interrupt"]
    assert call["agent_id"] is None
    assert "Claude Code 2.1.300" in call["note"]
    assert '"Research"' in call["note"]
