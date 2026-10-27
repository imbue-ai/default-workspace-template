"""Tests for how the run record says a run was started, the needs-decision verdict, and the
chats ``run-status stop-chats`` pauses for an apply and ``resume-chats`` brings back.

The pause and resume run through ``update_self.main`` against the ``fake_agents`` fixture: a fake
``mngr`` keeping the workspace's agents, and a recording chat messenger in the test's tree.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import update_apply_contract
import update_chat_pause
import update_resume_note
import update_self
from agents_testing import FakeAgents, chat, worker

_LEAD = "update-to-minds-v0-9-0"


def _main(tmp_path: Path, *args: str) -> int:
    return update_self.main([*args, "--repo-root", str(tmp_path)])


def _record(tmp_path: Path) -> update_apply_contract.RunStatus:
    status = update_apply_contract.read_run_status(tmp_path)
    assert status is not None
    return status


def _workspace_agents() -> list[dict[str, object]]:
    """The lead and its worker, the services agent, an automation, and other chats in every state."""
    return [
        chat("agent-lead", _LEAD, "RUNNING"),
        worker("agent-update-self", "update-self", "RUNNING", lead_agent="agent-lead"),
        worker(
            "agent-sub", "update-self-tests", "RUNNING", lead_agent="agent-update-self"
        ),
        {
            "id": "agent-services",
            "name": "main",
            "state": "RUNNING",
            "labels": {"is_primary": "true", "user_created": "true"},
        },
        {"id": "agent-nightly", "name": "nightly", "state": "RUNNING", "labels": {}},
        chat("agent-busy", "Fix-the-login", "RUNNING"),
        worker("agent-helper", "login-tests", "RUNNING", lead_agent="agent-busy"),
        chat("agent-idle", "Plan-the-trip", "WAITING"),
        chat("agent-asleep", "Old-chat", "STOPPED"),
    ]


def test_stop_chats_pauses_what_is_busy_and_resume_brings_each_back_with_one_note(
    tmp_path: Path, fake_agents: FakeAgents
) -> None:
    fake_agents.set_agents(_workspace_agents())
    assert _main(tmp_path, "run-status", "start") == 0

    assert _main(tmp_path, "run-status", "stop-chats") == 0

    # Only the busy chat and the other chat's busy worker are paused: never the lead, anything
    # in the dispatch tree under it, the services agent, an automation, or a chat that has
    # ended its turn.
    assert fake_agents.states() == {
        _LEAD: "RUNNING",
        "update-self": "RUNNING",
        "update-self-tests": "RUNNING",
        "main": "RUNNING",
        "nightly": "RUNNING",
        "Fix-the-login": "STOPPED",
        "login-tests": "STOPPED",
        "Plan-the-trip": "WAITING",
        "Old-chat": "STOPPED",
    }
    assert [
        (entry.id, entry.chat_id, entry.is_worker, entry.lead_chat_id)
        for entry in _record(tmp_path).stopped_agents
    ] == [
        ("agent-busy", "agent-busy", False, ""),
        ("agent-helper", "agent-helper", True, "agent-busy"),
    ]

    assert (
        _main(
            tmp_path,
            "run-status",
            "resume-chats",
            "--landed",
            "minds-v0.9.0",
            "--wip-commit",
            "0123abc",
        )
        == 0
    )

    assert fake_agents.states()["Fix-the-login"] == "WAITING"
    assert fake_agents.states()["login-tests"] == "WAITING"
    # One note, to the chat: its own pause, and its worker, which does not pick itself back up.
    assert fake_agents.notes() == {
        "agent-busy": update_resume_note.compose_resume_note(
            landed_ref="minds-v0.9.0",
            is_chat_paused=True,
            wip_commit="0123abc",
            interrupted_tasks=[],
            restarted_workers=["login-tests"],
        )
    }

    fake_agents.clear_calls()
    assert _main(tmp_path, "run-status", "resume-chats") == 0
    assert fake_agents.mngr_verbs() == []
    assert fake_agents.notes() == {}


def test_a_start_the_lead_records_again_keeps_the_chats_it_has_not_resumed(
    tmp_path: Path, fake_agents: FakeAgents
) -> None:
    """Re-taking its lease, or resuming after an override, records the start again."""
    fake_agents.set_agents(
        [chat("agent-lead", _LEAD, "RUNNING"), chat("agent-a", "Chat-A", "RUNNING")]
    )
    assert _main(tmp_path, "run-status", "start") == 0
    assert _main(tmp_path, "run-status", "stop-chats") == 0

    assert _main(tmp_path, "run-status", "start") == 0
    assert _main(tmp_path, "run-status", "resume-chats") == 0

    assert fake_agents.states()["Chat-A"] == "WAITING"
    assert set(fake_agents.notes()) == {"agent-a"}


def test_the_next_run_brings_back_the_chats_a_lead_that_ended_left_paused(
    tmp_path: Path, fake_agents: FakeAgents
) -> None:
    """A lead that dies between its pause and its resume is closed out without resuming them."""
    fake_agents.set_agents(
        [chat("agent-lead", _LEAD, "RUNNING"), chat("agent-a", "Chat-A", "RUNNING")]
    )
    assert _main(tmp_path, "run-status", "start") == 0
    assert _main(tmp_path, "run-status", "stop-chats") == 0
    assert (
        _main(
            tmp_path,
            "run-status",
            "verdict",
            "STUCK",
            "--chat",
            _LEAD,
            "--detail",
            "gone",
        )
        == 0
    )

    next_lead = f"{_LEAD}-2"
    assert _main(tmp_path, "run-status", "start", "--chat", next_lead) == 0
    assert _main(tmp_path, "run-status", "resume-chats", "--chat", next_lead) == 0

    assert fake_agents.states()["Chat-A"] == "WAITING"
    assert set(fake_agents.notes()) == {"agent-a"}


def test_a_second_pause_tells_a_chat_only_about_what_it_has_not_heard() -> None:
    told = update_apply_contract.StoppedAgent(
        id="agent-a",
        name="Chat-A",
        chat_id="agent-a",
        interrupted_tasks=["Run the test suite"],
        is_restarted=True,
        is_told=True,
    )
    untold = update_apply_contract.StoppedAgent(
        id="agent-b",
        name="Chat-B",
        chat_id="agent-b",
        interrupted_tasks=["Build the docs"],
    )
    record = [told, untold]

    update_chat_pause.merge_stopped(
        record,
        update_apply_contract.StoppedAgent(
            id="agent-a", name="Chat-A", chat_id="agent-a"
        ),
    )
    update_chat_pause.merge_stopped(
        record,
        update_apply_contract.StoppedAgent(
            id="agent-b", name="Chat-B", chat_id="agent-b", interrupted_tasks=["Lint"]
        ),
    )

    assert [(entry.id, entry.interrupted_tasks, entry.is_told) for entry in record] == [
        ("agent-a", [], False),
        ("agent-b", ["Build the docs", "Lint"], False),
    ]


def test_a_paused_chat_deleted_before_the_resume_does_not_hold_it_open(
    tmp_path: Path, fake_agents: FakeAgents
) -> None:
    fake_agents.set_agents(
        [
            chat("agent-lead", _LEAD, "RUNNING"),
            chat("agent-a", "Chat-A", "RUNNING"),
            chat("agent-b", "Chat-B", "RUNNING"),
        ]
    )
    assert _main(tmp_path, "run-status", "start") == 0
    assert _main(tmp_path, "run-status", "stop-chats") == 0
    fake_agents.set_agents(
        [chat("agent-lead", _LEAD, "RUNNING"), chat("agent-b", "Chat-B", "STOPPED")]
    )
    fake_agents.fail({"start": ["agent-a"]})

    assert _main(tmp_path, "run-status", "resume-chats") == 0

    assert set(fake_agents.notes()) == {"agent-b"}
    assert all(entry.is_told for entry in _record(tmp_path).stopped_agents)


def test_stop_chats_pauses_from_a_listing_that_exited_non_zero(
    tmp_path: Path, fake_agents: FakeAgents
) -> None:
    """``--on-error continue`` exits non-zero for one provider's failure, still listing the rest."""
    fake_agents.set_agents(
        [chat("agent-lead", _LEAD, "RUNNING"), chat("agent-a", "Chat-A", "RUNNING")]
    )
    fake_agents.fail({"list": []})
    assert _main(tmp_path, "run-status", "start") == 0

    assert _main(tmp_path, "run-status", "stop-chats") == 0

    assert fake_agents.states()["Chat-A"] == "STOPPED"


def test_a_worker_whose_lead_chat_is_gone_is_resumed_without_holding_the_resume_open(
    tmp_path: Path, fake_agents: FakeAgents
) -> None:
    """No later try can tell a chat that no longer exists, so that is not left for a re-run."""
    fake_agents.set_agents(
        [
            chat("agent-lead", _LEAD, "RUNNING"),
            worker("agent-w", "orphan-tests", "RUNNING", lead_agent="agent-gone"),
        ]
    )
    assert _main(tmp_path, "run-status", "start") == 0
    assert _main(tmp_path, "run-status", "stop-chats") == 0
    fake_agents.fail({"gone": ["agent-gone"]})

    assert (
        _main(tmp_path, "run-status", "resume-chats", "--landed", "minds-v0.9.0") == 0
    )

    assert fake_agents.states()["orphan-tests"] == "WAITING"
    assert [
        (entry.is_restarted, entry.is_told)
        for entry in _record(tmp_path).stopped_agents
    ] == [(True, True)]


def test_resume_chats_finishes_only_what_an_earlier_run_left_undone(
    tmp_path: Path, fake_agents: FakeAgents
) -> None:
    fake_agents.set_agents(
        [
            chat("agent-lead", _LEAD, "RUNNING"),
            chat("agent-a", "Chat-A", "RUNNING"),
            chat("agent-b", "Chat-B", "RUNNING"),
        ]
    )
    assert _main(tmp_path, "run-status", "start") == 0
    assert _main(tmp_path, "run-status", "stop-chats") == 0
    fake_agents.fail({"start": ["agent-b"], "message": ["agent-a"]})

    assert (
        _main(tmp_path, "run-status", "resume-chats", "--landed", "minds-v0.9.0") == 1
    )

    progress = {
        entry.id: (entry.is_restarted, entry.is_told)
        for entry in _record(tmp_path).stopped_agents
    }
    assert progress == {"agent-a": (True, False), "agent-b": (False, False)}

    fake_agents.fail({})
    fake_agents.clear_calls()
    assert (
        _main(tmp_path, "run-status", "resume-chats", "--landed", "minds-v0.9.0") == 0
    )

    # Chat A, already running again, is only told; chat B is started and told.
    assert fake_agents.mngr_verbs() == [["start", "agent-b"]]
    assert set(fake_agents.notes()) == {"agent-a", "agent-b"}


def test_a_worker_naming_no_lead_chat_is_resumed_without_holding_the_resume_open(
    tmp_path: Path, fake_agents: FakeAgents
) -> None:
    """A worker from before the lead_agent label has no chat to tell; that is not left for a re-run."""
    fake_agents.set_agents(
        [
            chat("agent-lead", _LEAD, "RUNNING"),
            {
                "id": "agent-orphan",
                "name": "old-worker",
                "state": "RUNNING",
                "labels": {"agent_created": "true"},
            },
        ]
    )
    assert _main(tmp_path, "run-status", "start") == 0
    assert _main(tmp_path, "run-status", "stop-chats") == 0
    assert fake_agents.states()["old-worker"] == "STOPPED"

    assert _main(tmp_path, "run-status", "resume-chats") == 0

    assert fake_agents.states()["old-worker"] == "WAITING"
    assert fake_agents.notes() == {}
    fake_agents.clear_calls()
    assert _main(tmp_path, "run-status", "resume-chats") == 0
    assert fake_agents.mngr_verbs() == []


def test_stop_chats_pauses_a_named_agent_for_memory_and_refuses_one_it_may_not(
    tmp_path: Path, fake_agents: FakeAgents
) -> None:
    fake_agents.set_agents(_workspace_agents())
    assert _main(tmp_path, "run-status", "start") == 0

    # A chat that ended its turn is paused when named, the way the lead frees memory mid-run.
    assert _main(tmp_path, "run-status", "stop-chats", "--agent", "Plan-the-trip") == 0
    assert fake_agents.states()["Plan-the-trip"] == "STOPPED"

    # The update's own worker and a name nothing carries are refused outright, pausing nothing.
    fake_agents.clear_calls()
    assert _main(tmp_path, "run-status", "stop-chats", "--agent", "update-self") == 1
    assert _main(tmp_path, "run-status", "stop-chats", "--agent", "no-such-chat") == 1
    assert fake_agents.mngr_verbs() == []
    assert [entry.id for entry in _record(tmp_path).stopped_agents] == ["agent-idle"]


def test_stop_chats_refuses_to_run_without_the_leads_identity(
    tmp_path: Path, fake_agents: FakeAgents, monkeypatch
) -> None:
    """Without its own id the lead cannot tell itself from the chats to pause."""
    fake_agents.set_agents(_workspace_agents())
    monkeypatch.delenv("MNGR_AGENT_ID")

    assert _main(tmp_path, "run-status", "stop-chats") == 1
    assert fake_agents.mngr_verbs() == []


def _runner_cmdline(*options: str) -> bytes:
    return b"\0".join(
        word.encode()
        for word in (
            "/usr/bin/python3",
            "/home/user/workspace/system/scripts/run_in_background.py",
            *options,
            "--",
            "uv",
            "run",
            "pytest",
        )
    )


def test_a_waiting_agent_whose_background_command_still_runs_is_paused_with_it(
    tmp_path: Path,
) -> None:
    """The command's report would start the agent's next turn mid-apply, so the agent is busy; its
    runner is stopped before the agent, and the command goes into the resume note."""
    proc = tmp_path / "proc"
    for pid, cmdline in {
        "4242": _runner_cmdline(
            "--foreground",
            "--task-dir",
            "data/.tasks/run-in-background/x",
            "--chat-id",
            "agent-idle",
            "--description",
            "Run the test suite",
        ),
        # The caller's copy, which exits at once and runs nothing.
        "4243": _runner_cmdline("--description", "Build", "--chat-id", "agent-other"),
        "4244": b"\0".join([b"python3", b"serve.py", b"--chat-id", b"agent-other"]),
    }.items():
        (proc / pid).mkdir(parents=True)
        (proc / pid / "cmdline").write_bytes(cmdline)
    (proc / "self").mkdir()

    runners = update_chat_pause.read_background_runners(proc)
    assert runners == [
        update_chat_pause.BackgroundRunner(4242, "agent-idle", "Run the test suite")
    ]

    def listed(agent_id: str, state: str) -> update_chat_pause.ListedAgent:
        return update_chat_pause.ListedAgent(
            id=agent_id,
            name=agent_id,
            state=state,
            is_primary=False,
            is_chat=True,
            is_worker=False,
            lead_agent_id="",
            chat_label="",
        )

    lead = update_chat_pause.LeadIdentity("agent-lead", _LEAD, "agent-lead")
    targets = update_chat_pause.select_pause_targets(
        [listed("agent-idle", "WAITING"), listed("agent-other", "WAITING")],
        runners,
        lead,
        [],
    )
    assert [target.agent.id for target in targets] == ["agent-idle"]

    events: list[object] = []

    def run(argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
        events.append(list(argv))
        return subprocess.CompletedProcess(list(argv), 0, "", "")

    stopped: list[update_apply_contract.StoppedAgent] = []
    problems = update_chat_pause.pause(
        targets, run, lambda pid: events.append(("kill", pid)), stopped.append
    )

    assert problems == []
    assert events == [("kill", 4242), ["mngr", "stop", "agent-idle"]]
    assert stopped[0].interrupted_tasks == ["Run the test suite"]

    delivered: dict[str, str] = {}

    def deliver(chat_id: str, note: str) -> subprocess.CompletedProcess[str]:
        delivered[chat_id] = note
        return subprocess.CompletedProcess([], 0, "", "")

    assert (
        update_chat_pause.resume(
            stopped, run, deliver, landed_ref=None, wip_commit="", save=lambda: None
        )
        == []
    )
    assert delivered == {
        "agent-idle": update_resume_note.compose_resume_note(
            landed_ref=None,
            is_chat_paused=True,
            wip_commit="",
            interrupted_tasks=["Run the test suite"],
            restarted_workers=[],
        )
    }


def _write_last_dispatch(tmp_path: Path, chat_agent_name: str, started_by: str) -> None:
    path = update_apply_contract.last_dispatch_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "chat_agent_name": chat_agent_name,
                "chat_id": "agent-lead",
                "started_by": started_by,
                "ceiling": "minds-v0.9.0",
                "target_ref": "minds-v0.9.0",
                "dispatched_at": 1760000000.0,
            }
        )
    )


def test_run_status_start_takes_how_it_was_started_from_the_dispatch_that_made_this_chat(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("MNGR_AGENT_NAME", _LEAD)
    _write_last_dispatch(tmp_path, _LEAD, "scheduled")

    assert _main(tmp_path, "run-status", "start") == 0
    assert _record(tmp_path).started_by == "scheduled"

    # A start the lead names wins over the file.
    assert _main(tmp_path, "run-status", "start", "--started-by", "immediate") == 0
    assert _record(tmp_path).started_by == "immediate"

    # The last dispatch made some other chat: this run was not started by it, and the
    # record says nothing rather than guessing.
    _write_last_dispatch(tmp_path, "update-to-minds-v0-8-0", "scheduled")
    assert _main(tmp_path, "run-status", "start") == 0
    assert _record(tmp_path).started_by is None
    raw = json.loads(update_apply_contract.run_status_path(tmp_path).read_text())
    assert "started_by" not in raw


def test_a_needs_decision_verdict_ends_the_run_and_the_resume_still_follows_it(
    tmp_path: Path, fake_agents: FakeAgents
) -> None:
    """Step 4's order: the verdict, the worker stopped, then the chats the run paused come back."""
    fake_agents.set_agents(
        [chat("agent-lead", _LEAD, "RUNNING"), chat("agent-a", "Chat-A", "RUNNING")]
    )
    assert _main(tmp_path, "run-status", "start") == 0
    assert _main(tmp_path, "run-status", "delegate", "update-self") == 0
    assert _main(tmp_path, "run-status", "stop-chats", "--agent", "Chat-A") == 0

    assert (
        _main(
            tmp_path,
            "run-status",
            "verdict",
            "NEEDS_DECISION",
            "--detail",
            "Your weather widget has no place in the new layout.",
        )
        == 0
    )
    ended = _record(tmp_path)
    assert ended.verdict == update_apply_contract.RUN_VERDICT_NEEDS_DECISION
    assert ended.detail == "Your weather widget has no place in the new layout."
    assert ended.worker_agent_name is None

    assert _main(tmp_path, "run-status", "resume-chats") == 0
    assert fake_agents.states()["Chat-A"] == "WAITING"
    assert set(fake_agents.notes()) == {"agent-a"}
    assert _record(tmp_path).verdict == update_apply_contract.RUN_VERDICT_NEEDS_DECISION


def test_a_run_record_from_before_the_additions_still_reads(tmp_path: Path) -> None:
    """The app and older passes write records without these fields, or with values this
    release does not know; the read takes what it can and drops the rest."""
    path = update_apply_contract.run_status_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "chat_agent_name": _LEAD,
                "started_at": 1.0,
                "updated_at": 2.0,
                "started_by": "by-hand",
                "stopped_agents": [
                    {"name": "no id"},
                    {"id": "agent-a", "name": "Chat-A"},
                ],
            }
        )
    )

    status = _record(tmp_path)
    assert status.started_by is None
    assert [(entry.id, entry.is_restarted) for entry in status.stopped_agents] == [
        ("agent-a", False)
    ]
