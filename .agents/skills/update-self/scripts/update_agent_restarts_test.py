"""Tests for restarting the workspace's agents after an update: which chats are asked to restart
and how each answer lands in the report, the chat list's retries, the self-restart's wait, and
the pass's own restart from a detached helper.

The chat app is the ``fake_chat_app`` fixture over loopback; ``message_chat.py`` is a recorder
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
import update_layout
import update_runtime

_OWN_CHAT = "agent-00000000000000000000000000000001"
_UPDATE_SELF_SCRIPT = Path(__file__).with_name("update_self.py")
_DELIVERY_DEADLINE_SECONDS = 8.0
_ONLY_IF_IDLE = {"only_if_idle": True}


def _chat(chat_id: str, title: str, status: str, name: str = "") -> dict[str, Any]:
    """One chat of ``GET /api/chats``, in the chat app's ``ChatSnapshot`` shape (the fields read here)."""
    return {
        "chat_id": chat_id,
        "title": title,
        "name": name or title.lower().replace(" ", "-"),
        "status": status,
    }


def _install_message_chat_recorder(workspace: Path) -> Path:
    """A ``message_chat.py`` that records its argv, the note it was given, and whether it ran
    with an agent id."""
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
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return record


def _calls(record: Path) -> list[dict[str, Any]]:
    if not record.exists():
        return []
    return [json.loads(line) for line in record.read_text().splitlines()]


def test_every_chat_but_the_pass_its_worker_and_stopped_ones_is_asked_and_its_answer_reported(
    fake_chat_app: Any,
) -> None:
    fake_chat_app.answers = [
        (
            200,
            {
                "chats": [
                    _chat(_OWN_CHAT, "Update", "idle"),
                    _chat("agent-worker", "Update worker", "idle", name="update-self"),
                    _chat("agent-idle", "Trip planning", "idle"),
                    _chat("agent-working", "Research", "working"),
                    _chat("agent-dialog", "Inbox", "attention"),
                    _chat("agent-handoff", "Notes", "working"),
                    _chat("agent-stopped", "Old chat", "stopped"),
                    _chat("agent-deleted", "Gone", "idle"),
                    _chat("agent-failing", "Budget", "idle"),
                ]
            },
        )
    ]
    fake_chat_app.interrupt_answers = {
        "agent-working": [(409, {"detail": "busy", "busy_with": "working"})],
        "agent-dialog": [(409, {"detail": "busy", "busy_with": "waiting on a dialog"})],
        "agent-handoff": [(409, {"detail": "switching", "phase": "summarizing"})],
        "agent-deleted": [(404, {"detail": "Chat 'agent-deleted' not found"})],
        "agent-failing": [
            (500, {"detail": "Failed to interrupt agent 'budget': mngr start failed"})
        ],
    }

    report = update_agent_restarts.restart_idle_agents(
        fake_chat_app.workspace, _OWN_CHAT, update_runtime.HttpClient()
    )

    assert fake_chat_app.interrupts == [
        (chat_id, _ONLY_IF_IDLE)
        for chat_id in (
            "agent-idle",
            "agent-working",
            "agent-dialog",
            "agent-handoff",
            "agent-deleted",
            "agent-failing",
        )
    ]
    assert report == {
        "restarted": [{"chat_id": "agent-idle", "title": "Trip planning"}],
        "left_running": [
            {"chat_id": "agent-working", "title": "Research", "busy_with": "working"},
            {
                "chat_id": "agent-dialog",
                "title": "Inbox",
                "busy_with": "waiting on a dialog",
            },
            {
                "chat_id": "agent-handoff",
                "title": "Notes",
                "busy_with": "switching to another agent",
            },
        ],
        "failed": [
            {
                "chat_id": "agent-failing",
                "title": "Budget",
                "detail": "Failed to interrupt agent 'budget': mngr start failed",
            }
        ],
    }


class _ScriptedHttp(update_runtime.HttpClient):
    """Answers the chat-list reads and the interrupt posts from scripts, one page (or None for no
    answer) per call, the last one repeating."""

    def __init__(
        self,
        pages: list[update_runtime.FetchedPage | None],
        posts: list[update_runtime.FetchedPage | None] | None = None,
    ) -> None:
        self._pages = pages
        self._posts = posts or [None]
        self.reads = 0
        self.posted: list[str] = []

    def get_page(self, url: str, timeout: float) -> update_runtime.FetchedPage | None:
        self.reads += 1
        return self._pages.pop(0) if len(self._pages) > 1 else self._pages[0]

    def post_json(
        self, url: str, body: Any, timeout: float
    ) -> update_runtime.FetchedPage | None:
        self.posted.append(url)
        return self._posts.pop(0) if len(self._posts) > 1 else self._posts[0]


class _FakeClock:
    """A clock that advances by ``step`` on every read."""

    def __init__(self, step: float) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def _registered_workspace(tmp_path: Path) -> Path:
    registry = tmp_path / update_layout.APPS_REGISTRY_PATH
    registry.parent.mkdir(parents=True)
    registry.write_text('[[apps]]\nname = "chat"\nurl = "http://127.0.0.1:9"\n')
    return tmp_path


def _page(status: int, body: object) -> update_runtime.FetchedPage:
    return update_runtime.FetchedPage(
        status=status,
        body=json.dumps(body),
        headers={"content-type": "application/json"},
    )


def test_a_restart_the_chat_app_cannot_take_is_reported_failed_not_forced(
    tmp_path: Path,
) -> None:
    """Nothing here restarts an agent behind the chat app's back: with the app gone between the
    list and the restart, the chat is reported failed for the user to decide about."""
    http = _ScriptedHttp(
        [_page(200, {"chats": [_chat("agent-idle", "Trip", "idle")]})], [None]
    )

    report = update_agent_restarts.restart_idle_agents(
        _registered_workspace(tmp_path), _OWN_CHAT, http
    )

    assert report["restarted"] == [] and report["left_running"] == []
    assert [entry["chat_id"] for entry in report["failed"]] == ["agent-idle"]
    assert "did not answer" in report["failed"][0]["detail"]


def test_the_chat_list_is_retried_while_the_restarted_chat_app_comes_back(
    tmp_path: Path,
) -> None:
    http = _ScriptedHttp(
        [
            None,
            _page(503, {}),
            _page(200, {"chats": [_chat("agent-idle", "Trip", "idle")]}),
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
    http = _ScriptedHttp([_page(503, {})])

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
    assert http.posted == []


def test_the_pass_is_restarted_once_the_chat_app_finds_it_idle(tmp_path: Path) -> None:
    """Busy and unanswered restarts are asked again; the chat app decides when the turn is over."""
    http = _ScriptedHttp(
        [_page(200, {"chats": []})],
        [_page(409, {"busy_with": "working"}), None, _page(200, {"status": "ok"})],
    )

    problem = update_agent_restarts.restart_when_idle(
        _registered_workspace(tmp_path),
        _OWN_CHAT,
        http,
        _FakeClock(0.0),
        lambda _: None,
    )

    assert problem is None
    assert len(http.posted) == 3


def test_a_pass_still_busy_at_the_deadline_is_not_restarted(tmp_path: Path) -> None:
    http = _ScriptedHttp(
        [_page(200, {"chats": []})], [_page(409, {"busy_with": "working"})]
    )

    problem = update_agent_restarts.restart_when_idle(
        _registered_workspace(tmp_path),
        _OWN_CHAT,
        http,
        _FakeClock(update_agent_restarts.SELF_RESTART_DEADLINE_SECONDS),
        lambda _: None,
    )

    assert problem == "it was still working after 30 minutes"


@pytest.mark.parametrize(
    "restart_report, expected_line",
    [
        (
            {
                "restarted": [],
                "left_running": [
                    {
                        "chat_id": "agent-working",
                        "title": "Research",
                        "busy_with": "working",
                    }
                ],
                "failed": [
                    {"chat_id": "agent-failing", "title": "Budget", "detail": "x"}
                ],
            },
            'still running the previous version: "Research" (agent-working), "Budget" (agent-failing).',
        ),
        (None, "Restarting the workspace's other chats did not run"),
    ],
    ids=["some-not-restarted", "restart-agents-failed"],
)
def test_the_note_names_every_chat_not_restarted_or_that_none_were(
    restart_report: dict[str, list[dict[str, str]]] | None, expected_line: str
) -> None:
    note = update_agent_restarts.compose_self_restart_note(
        "Claude Code  2.1.300\n", restart_report
    )

    assert note.startswith(
        "<background-task-report>\n<summary>Restarted to finish the update</summary>\n"
    )
    assert "what the update installed: Claude Code 2.1.300." in note
    assert expected_line in note


def test_restart_self_restarts_the_chat_from_a_helper_the_restart_cannot_kill(
    fake_chat_app: Any,
) -> None:
    """The helper is detached and carries no agent id, so the stop half of the restart, which
    kills every process carrying the chat's agent id, leaves it alive to send the note."""
    workspace = fake_chat_app.workspace
    record = _install_message_chat_recorder(workspace)
    report_path = workspace / "agent-restarts.json"
    report_path.write_text(
        json.dumps(
            {
                "restarted": [],
                "left_running": [
                    {
                        "chat_id": "agent-working",
                        "title": "Research",
                        "busy_with": "working",
                    }
                ],
                "failed": [],
            }
        )
    )
    env = {key: value for key, value in os.environ.items() if key != "MINDS_CHAT_ID"}
    env["MNGR_AGENT_ID"] = _OWN_CHAT

    started = subprocess.run(
        [
            sys.executable,
            str(_UPDATE_SELF_SCRIPT),
            "restart-self",
            "--reason",
            "Claude Code 2.1.300",
            "--restart-report",
            str(report_path),
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
        assert time.monotonic() < deadline, "the helper never sent the note"
        time.sleep(0.1)
    assert fake_chat_app.interrupts == [(_OWN_CHAT, _ONLY_IF_IDLE)]
    [note_call] = _calls(record)
    assert note_call["argv"][:2] == [_OWN_CHAT, "--message-file"]
    assert note_call["agent_id"] is None
    assert "Claude Code 2.1.300" in note_call["note"]
    assert '"Research" (agent-working)' in note_call["note"]


def test_a_self_restart_that_never_happens_tells_the_chat_so(
    fake_chat_app: Any,
) -> None:
    """The results message has told the user this chat restarts; when it does not, the chat
    is told so instead of the success note."""
    workspace = fake_chat_app.workspace
    record = _install_message_chat_recorder(workspace)
    fake_chat_app.interrupt_answers = {
        _OWN_CHAT: [(500, {"detail": "mngr start failed"})]
    }

    rc = update_agent_restarts.restart_self_when_idle(
        workspace,
        _OWN_CHAT,
        update_runtime.HttpClient(),
        update_runtime.Runner(),
        _FakeClock(update_agent_restarts.SELF_RESTART_DEADLINE_SECONDS),
        lambda _: None,
    )

    assert rc == 1
    [failure_call] = _calls(record)
    assert failure_call["argv"][:2] == [_OWN_CHAT, "--message-file"]
    assert "the restart kept failing (mngr start failed)" in failure_call["note"]
