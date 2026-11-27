"""The dispatcher: its window, its dispatch decisions, its watchdog, and whole cron ticks."""

import json
import shutil
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from workspace_update_control import control, dispatcher
from workspace_update_control.dispatcher import (
    AgentEnvironment,
    ScheduleAction,
    WatchdogAction,
    _is_agent_busy,
)
from workspace_update_control.testing import (
    STAGED_CREATED_CHAT,
    RecordingFeed,
    ScriptedToolAnswer,
    StagedUpdateWorkspace,
    armed_schedule,
    epoch_at,
    labels_of,
    listed_agent,
    run_in_place,
    stage_update_workspace,
)

_LEAD = STAGED_CREATED_CHAT["name"]
_LEAD_ID = f"agent-{uuid4().hex}"
_WORKER = "update-self"
_WORKER_ID = f"agent-{uuid4().hex}"


def _utc(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M")


@pytest.mark.parametrize(
    ("now", "start_minute_utc", "length_minutes", "opened_at_utc"),
    [
        ("2026-10-20T10:30:00+00:00", 9 * 60, 3 * 60, "2026-10-20T09:00"),
        # A start off the hour, as a half-hour zone's window has.
        ("2026-10-20T21:00:00+00:00", 20 * 60 + 30, 3 * 60, "2026-10-20T20:30"),
        # A window that runs past UTC midnight, read after midnight, opened the day before.
        ("2026-10-21T01:00:00+00:00", 22 * 60, 4 * 60, "2026-10-20T22:00"),
    ],
)
def test_the_window_opens_daily_at_its_utc_start(
    now: str, start_minute_utc: int, length_minutes: int, opened_at_utc: str
) -> None:
    bounds = dispatcher.current_window(epoch_at(now), start_minute_utc, length_minutes)

    assert bounds is not None
    assert _utc(bounds[0]) == opened_at_utc
    assert bounds[1] - bounds[0] == length_minutes * 60


@pytest.mark.parametrize(
    ("now", "start_minute_utc", "length_minutes"),
    [
        ("2026-10-20T12:00:00+00:00", 9 * 60, 3 * 60),
        ("2026-10-20T08:59:00+00:00", 9 * 60, 3 * 60),
        ("2026-10-20T12:00:00+00:00", 22 * 60, 4 * 60),
        ("2026-10-20T09:00:00+00:00", 9 * 60, 0),
    ],
)
def test_outside_the_window_there_is_no_window(
    now: str, start_minute_utc: int, length_minutes: int
) -> None:
    assert (
        dispatcher.current_window(epoch_at(now), start_minute_utc, length_minutes)
        is None
    )


_IN_WINDOW = epoch_at("2026-10-20T03:00:00-07:00")
_WINDOW_OPENED = epoch_at("2026-10-20T02:00:00-07:00")
_AFTER_WINDOW = epoch_at("2026-10-20T06:00:00-07:00")


def _schedule(**fields: Any) -> dict[str, Any]:
    """A schedule armed the day before the window the tests tick in."""
    return {**armed_schedule(_WINDOW_OPENED - 86400), **fields}


def _never_gone() -> bool:
    raise AssertionError(
        "the decision should not have asked whether the marked lead is gone"
    )


@pytest.mark.parametrize(
    ("schedule", "run", "now", "is_gone", "action"),
    [
        # No run yet this window: start one.
        (_schedule(), None, _IN_WINDOW, _never_gone, ScheduleAction.DISPATCH),
        # A run that started before this window opened does not count against it.
        (
            _schedule(),
            {"chat_agent_name": "old", "started_at": _WINDOW_OPENED - 3600},
            _IN_WINDOW,
            _never_gone,
            ScheduleAction.DISPATCH,
        ),
        # A run started since the window opened is this window's run.
        (
            _schedule(),
            {"chat_agent_name": "x", "started_at": _WINDOW_OPENED + 60},
            _IN_WINDOW,
            _never_gone,
            ScheduleAction.NOTE_WINDOW,
        ),
        # A chat dispatched this window whose lead never recorded a start, and is gone: start again.
        (
            _schedule(marked_chat=_LEAD, marked_at=_WINDOW_OPENED + 60),
            None,
            _IN_WINDOW,
            lambda: True,
            ScheduleAction.DISPATCH,
        ),
        # The same, with its lead still there: leave it be.
        (
            _schedule(marked_chat=_LEAD, marked_at=_WINDOW_OPENED + 60),
            None,
            _IN_WINDOW,
            lambda: False,
            ScheduleAction.NOTE_WINDOW,
        ),
        # Gone, but dispatched too recently to replace yet.
        (
            _schedule(
                marked_chat=_LEAD,
                marked_at=_WINDOW_OPENED + 60,
                next_attempt_at=_IN_WINDOW + 60,
            ),
            None,
            _IN_WINDOW,
            lambda: True,
            ScheduleAction.WAIT,
        ),
        # The run the file dispatched has a verdict, though the file was re-armed after it started: retire it.
        (
            _schedule(
                armed_at=_WINDOW_OPENED + 120,
                marked_chat=_LEAD,
                marked_at=_WINDOW_OPENED + 60,
            ),
            {
                "chat_agent_name": _LEAD,
                "started_at": _WINDOW_OPENED + 90,
                "verdict": "UPDATED",
            },
            _IN_WINDOW,
            _never_gone,
            ScheduleAction.REMOVE,
        ),
        # A verdict under the marked name from a run that started before the mark is an earlier chat's.
        (
            _schedule(marked_chat=_LEAD, marked_at=_WINDOW_OPENED + 60),
            {
                "chat_agent_name": _LEAD,
                "started_at": _WINDOW_OPENED - 99999,
                "verdict": "UPDATED",
            },
            _IN_WINDOW,
            lambda: False,
            ScheduleAction.NOTE_WINDOW,
        ),
        # A verdict from a run that predates the arm says nothing about this schedule.
        (
            _schedule(),
            {
                "chat_agent_name": "old",
                "started_at": _WINDOW_OPENED - 99999 * 2,
                "verdict": "UPDATED",
            },
            _IN_WINDOW,
            _never_gone,
            ScheduleAction.DISPATCH,
        ),
        # A window that closed after being seen open, with nothing started in it: record why.
        (
            _schedule(window_seen_open_at=_WINDOW_OPENED),
            None,
            _AFTER_WINDOW,
            _never_gone,
            ScheduleAction.RECORD_SKIP,
        ),
        # One that closed with this window's run started in it: nothing to record.
        (
            _schedule(window_seen_open_at=_WINDOW_OPENED),
            {"chat_agent_name": "x", "started_at": _WINDOW_OPENED + 60},
            _AFTER_WINDOW,
            _never_gone,
            ScheduleAction.NOTE_WINDOW,
        ),
        # A workspace stopped through the whole window never saw it open, so nothing is recorded.
        (_schedule(), None, _AFTER_WINDOW, _never_gone, ScheduleAction.NOTHING),
    ],
)
def test_one_tick_decides_from_the_window_and_the_run_record(
    schedule: dict[str, Any],
    run: dict[str, Any] | None,
    now: float,
    is_gone: Callable[[], bool],
    action: ScheduleAction,
) -> None:
    assert dispatcher.decide_schedule_action(schedule, run, now, is_gone) is action


def test_a_schedule_file_without_its_window_is_left_alone() -> None:
    schedule = _schedule()
    del schedule["window_length_minutes"]

    action = dispatcher.decide_schedule_action(schedule, None, _IN_WINDOW, _never_gone)

    assert action is ScheduleAction.NOTHING


_NOTHING_BUSY: tuple[set[str], set[str], set[str]] = (set(), set(), set())
_OPEN_RUN = {
    "chat_agent_name": _LEAD,
    "started_at": _IN_WINDOW,
    "worker_agent_name": _WORKER,
}


@pytest.mark.parametrize(
    ("run", "agents", "busy", "state", "action"),
    [
        # The lead's chat was deleted with no verdict: close the run out.
        (_OPEN_RUN, [], _NOTHING_BUSY, {}, WatchdogAction.CLOSE_OUT),
        # The lead stopped or exited with no verdict (a workspace restart, a crash, a stop): bring it back.
        (
            _OPEN_RUN,
            [listed_agent(_LEAD, "DONE")],
            _NOTHING_BUSY,
            {},
            WatchdogAction.REVIVE,
        ),
        (
            _OPEN_RUN,
            [listed_agent(_LEAD, "STOPPED")],
            _NOTHING_BUSY,
            {},
            WatchdogAction.REVIVE,
        ),
        # A listing that could not be read is no evidence of anything.
        (_OPEN_RUN, None, _NOTHING_BUSY, {}, WatchdogAction.NOTHING),
        # The worker is moving: nothing is owed yet.
        (
            _OPEN_RUN,
            [listed_agent(_LEAD, "WAITING"), listed_agent(_WORKER, "RUNNING")],
            _NOTHING_BUSY,
            {},
            WatchdogAction.RESET,
        ),
        # The lead waits on a background task the workspace reports busy.
        (
            _OPEN_RUN,
            [listed_agent(_LEAD, "WAITING"), listed_agent(_WORKER, "WAITING")],
            ({_LEAD}, set(), set()),
            {},
            WatchdogAction.RESET,
        ),
        # Both idle, briefly: count it.
        (
            _OPEN_RUN,
            [listed_agent(_LEAD, "WAITING"), listed_agent(_WORKER, "WAITING")],
            _NOTHING_BUSY,
            {},
            WatchdogAction.COUNT_IDLE,
        ),
        # Both idle for long enough, and not reminded lately: remind.
        (
            _OPEN_RUN,
            [listed_agent(_LEAD, "WAITING")],
            _NOTHING_BUSY,
            {"idle_ticks": 2, "last_reminded_at": 0.0},
            WatchdogAction.REMIND,
        ),
        # A lead or worker whose state could not be read may be mid-turn: neither count nor remind.
        (
            _OPEN_RUN,
            [listed_agent(_LEAD, "UNKNOWN")],
            _NOTHING_BUSY,
            {"idle_ticks": 2, "last_reminded_at": 0.0},
            WatchdogAction.NOTHING,
        ),
        (
            _OPEN_RUN,
            [listed_agent(_LEAD, "WAITING"), listed_agent(_WORKER, "UNKNOWN")],
            _NOTHING_BUSY,
            {"idle_ticks": 2, "last_reminded_at": 0.0},
            WatchdogAction.NOTHING,
        ),
        # Reminded a minute ago: wait out the interval.
        (
            _OPEN_RUN,
            [listed_agent(_LEAD, "WAITING")],
            _NOTHING_BUSY,
            {"idle_ticks": 9, "last_reminded_at": _IN_WINDOW - 60},
            WatchdogAction.COUNT_IDLE,
        ),
        # A run that is landing belongs to the apply's own recovery.
        (
            {**_OPEN_RUN, "apply_phase": "merged"},
            [],
            _NOTHING_BUSY,
            {},
            WatchdogAction.RESET,
        ),
        # A record holding for the user (an older template's hold) is waiting on purpose.
        (
            {**_OPEN_RUN, "is_holding": True},
            [listed_agent(_LEAD, "WAITING")],
            _NOTHING_BUSY,
            {"idle_ticks": 9},
            WatchdogAction.RESET,
        ),
        # A run with a verdict owes nothing.
        (
            {**_OPEN_RUN, "verdict": "UPDATED"},
            [],
            _NOTHING_BUSY,
            {},
            WatchdogAction.NOTHING,
        ),
    ],
)
def test_the_watchdog_decides_from_the_lead_and_worker_states(
    run: dict[str, Any],
    agents: list[dict[str, str]] | None,
    busy: tuple[set[str], set[str], set[str]],
    state: dict[str, Any],
    action: WatchdogAction,
) -> None:
    assert (
        dispatcher.decide_watchdog_action(run, agents, busy, state, _IN_WINDOW)
        is action
    )


def test_a_working_agent_is_busy_whatever_its_type() -> None:
    """A chat working past its turn, or one whose type mngr's config does not name while its process
    runs, must hold the update watchdog off; nothing else about it needs to be busy."""
    no_busy_chats = (frozenset[str](), frozenset[str](), frozenset[str]())
    for state in ("RUNNING", "RUNNING_BACKGROUND", "RUNNING_UNKNOWN_AGENT_TYPE"):
        assert _is_agent_busy({"name": "chat", "state": state}, no_busy_chats), state
    for state in ("WAITING", "UNKNOWN", "REPLACED", "DONE", "STOPPED"):
        assert not _is_agent_busy({"name": "chat", "state": state}, no_busy_chats), (
            state
        )


def test_a_tick_inside_the_window_starts_the_run_once_and_marks_the_file(
    staged: StagedUpdateWorkspace,
) -> None:
    staged.write_state("schedule.json", _schedule())
    staged.tick(_IN_WINDOW)
    staged.set_agents([listed_agent(_LEAD, "RUNNING")])
    staged.tick(_IN_WINDOW + 60)

    creates = staged.chat_creates()
    assert len(creates) == 1
    # A scheduled run surfaces no window of its own.
    assert not any(
        label.startswith(("assist=", "auto_open=")) for label in labels_of(creates[0])
    )
    schedule = staged.read_state("schedule.json")
    assert schedule is not None
    assert (schedule["marked_chat"], schedule["marked_at"]) == (_LEAD, _IN_WINDOW)
    dispatch = staged.read_state("last-dispatch.json")
    assert dispatch is not None and dispatch["started_by"] == "scheduled"


def test_a_tick_that_cannot_start_the_run_keeps_trying_and_records_why_at_window_close(
    staged: StagedUpdateWorkspace,
) -> None:
    staged.write_state("schedule.json", _schedule())
    staged.write_lease("w-1")

    staged.tick(_IN_WINDOW)
    in_window = staged.read_state("schedule.json")
    staged.tick(_AFTER_WINDOW)

    assert in_window is not None and in_window["pending_skip_reason"] == "LEASE_HELD"
    closed = staged.read_state("schedule.json")
    assert closed is not None
    assert (closed["last_skip_reason"], closed["pending_skip_reason"]) == (
        "LEASE_HELD",
        "",
    )
    assert closed["last_skip_at"] == _AFTER_WINDOW


def test_a_tick_held_back_logs_why_once_until_the_reason_changes(
    staged: StagedUpdateWorkspace,
) -> None:
    staged.write_state("schedule.json", _schedule())
    lease = staged.write_lease("w-1")
    skill = staged.root / ".agents" / "skills" / "update-self" / "SKILL.md"
    skill_text = skill.read_text()

    staged.tick(_IN_WINDOW)
    staged.tick(_IN_WINDOW + 60)
    lease.unlink()
    skill.unlink()
    staged.tick(_IN_WINDOW + 120)
    staged.tick(_IN_WINDOW + 180)
    skill.write_text(skill_text)
    staged.tick(_IN_WINDOW + 240)

    held = [
        line.rsplit(": ", 1)[-1]
        for line in staged.dispatcher_log_lines()
        if "held the scheduled update back" in line
    ]
    assert held == ["LEASE_HELD", "DISPATCH_FAILED"]
    assert len(staged.chat_creates()) == 1


@pytest.mark.parametrize(
    ("holder_state", "is_dispatched"),
    [("RUNNING", False), (None, True), ("STOPPED", True)],
)
def test_a_lease_counts_as_held_only_while_its_holder_is_there(
    staged: StagedUpdateWorkspace, holder_state: str | None, is_dispatched: bool
) -> None:
    """A lease whose holder is gone is abandoned: the update's lead takes it over, so it must not stop the start."""
    staged.write_state("schedule.json", _schedule())
    staged.set_agents(
        [listed_agent("old-lead", holder_state)] if holder_state is not None else []
    )
    staged.write_lease("w-1", "\nHeld by old-lead across the update-self pass.\n")

    staged.tick(_IN_WINDOW)

    schedule = staged.read_state("schedule.json")
    assert schedule is not None
    assert len(staged.chat_creates()) == (1 if is_dispatched else 0)
    assert schedule["pending_skip_reason"] == ("" if is_dispatched else "LEASE_HELD")


def test_a_sign_in_refusal_at_dispatch_time_is_the_windows_skip_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staged = stage_update_workspace(tmp_path, monkeypatch, is_signed_in=False)
    staged.write_state("schedule.json", _schedule())

    staged.tick(_IN_WINDOW)

    schedule = staged.read_state("schedule.json")
    assert schedule is not None and schedule["pending_skip_reason"] == "NEEDS_SIGN_IN"
    assert staged.chat_creates() == []


def test_a_tick_retires_the_schedule_once_its_run_has_a_verdict(
    staged: StagedUpdateWorkspace,
) -> None:
    staged.write_state(
        "schedule.json", _schedule(marked_chat=_LEAD, marked_at=_IN_WINDOW)
    )
    staged.write_state(
        "run.json",
        {
            "chat_agent_name": _LEAD,
            "started_at": _IN_WINDOW + 30,
            "verdict": "UPDATED",
        },
    )

    staged.tick(_IN_WINDOW + 3600)

    assert staged.read_state("schedule.json") is None


def _messenger_that_first(
    staged: StagedUpdateWorkspace, change_to_schedule: str, exit_code: int
) -> None:
    """A chat messenger whose create first changes the schedule file, as a request served meanwhile would."""
    (staged.root / "system" / "scripts" / "message_chat.py").write_text(
        "import json, os, sys\n"
        "path = 'data/.state/update-apply/schedule.json'\n"
        f"{change_to_schedule}\n"
        f"print(json.dumps({STAGED_CREATED_CHAT!r}))\n"
        f"sys.exit({exit_code})\n"
    )


def test_a_cancel_that_lands_while_a_tick_creates_the_chat_stays_cancelled(
    staged: StagedUpdateWorkspace,
) -> None:
    """A failed create would otherwise put the cancelled schedule back, and retry it later in the window."""
    staged.write_state("schedule.json", _schedule())
    _messenger_that_first(staged, "os.remove(path)", exit_code=1)

    staged.tick(_IN_WINDOW)

    assert staged.read_state("schedule.json") is None


def test_a_refresh_that_lands_while_a_tick_creates_the_chat_keeps_the_clients_fields(
    staged: StagedUpdateWorkspace,
) -> None:
    staged.write_state("schedule.json", _schedule())
    refresh = "s = json.load(open(path)); s['ceiling'] = 'minds-v0.9.1'; json.dump(s, open(path, 'w'))"
    _messenger_that_first(staged, refresh, exit_code=0)

    staged.tick(_IN_WINDOW)

    schedule = staged.read_state("schedule.json")
    assert schedule is not None
    assert (schedule["ceiling"], schedule["marked_chat"]) == ("minds-v0.9.1", _LEAD)


def test_a_re_arm_that_lands_while_a_tick_creates_the_chat_keeps_only_the_marks(
    staged: StagedUpdateWorkspace,
) -> None:
    """The arm started the dispatcher's record over; the chat the tick made still has to be marked."""
    staged.write_state("schedule.json", _schedule())
    re_arm = (
        "s = json.load(open(path)); s['armed_at'] = %r; s['next_attempt_at'] = None; "
        "json.dump(s, open(path, 'w'))" % (_IN_WINDOW - 1)
    )
    _messenger_that_first(staged, re_arm, exit_code=0)

    staged.tick(_IN_WINDOW)

    schedule = staged.read_state("schedule.json")
    assert schedule is not None
    assert (schedule["marked_chat"], schedule["marked_at"]) == (_LEAD, _IN_WINDOW)
    assert schedule["armed_at"] == _IN_WINDOW - 1
    assert schedule["next_attempt_at"] is None


def test_the_watchdog_closes_out_a_run_whose_lead_is_gone(
    staged: StagedUpdateWorkspace,
) -> None:
    """Recorded STUCK, the worker stopped (its branch kept), and only the gone lead's leases released."""
    staged.write_state("run.json", {**_OPEN_RUN, "verdict": None})
    staged.set_agents([listed_agent(_WORKER, "RUNNING")])
    staged.write_lease("w-1", f"\nHeld by {_LEAD} across the update-self pass.\n")
    (staged.root / "data" / ".tickets" / "w-2.md").write_text(
        "---\nid: w-2\nstatus: in_progress\nagent: someone-else\n---\n# updating workspace\n"
    )

    staged.tick(_IN_WINDOW)

    assert [call[:6] for call in staged.tool_calls("update_self.py")] == [
        ["run-status", "verdict", "STUCK", "--chat", _LEAD, "--detail"]
    ]
    calls = staged.recorded_calls()
    assert ["stop", _WORKER] in calls
    closes = [call for call in calls if call[:1] == ["close"]]
    assert [call[1] for call in closes] == ["w-1"]


def test_a_run_whose_chat_was_renamed_is_neither_closed_out_nor_counted_as_over(
    staged: StagedUpdateWorkspace,
) -> None:
    """Renaming a chat renames its agent; the chat id the last dispatch recorded still names it."""
    staged.write_state("run.json", {**_OPEN_RUN, "verdict": None})
    staged.write_state(
        "last-dispatch.json",
        {
            "chat_agent_name": _LEAD,
            "chat_id": _LEAD_ID,
            "started_by": "scheduled",
            "dispatched_at": _IN_WINDOW - 7200,
        },
    )
    staged.set_agents(
        [
            listed_agent("my-update", "WAITING", _LEAD_ID),
            listed_agent(_WORKER, "RUNNING"),
        ]
    )

    staged.tick(_IN_WINDOW)
    answer = control.start_update(
        ceiling="minds-v0.9.0",
        override_ref="",
        is_backup_configured=True,
        now=_IN_WINDOW,
        live_cron_dir=str(staged.live_cron_dir),
        proc_root=str(staged.proc_root),
        agent_environment=AgentEnvironment(),
        read_agents=staged.read_listed_agents,
        run_in_background=run_in_place,
        clock=lambda: _IN_WINDOW,
        fetch_channel_file=RecordingFeed(None),
    )

    assert staged.tool_calls("update_self.py") == []
    assert ["stop", _WORKER] not in staged.recorded_calls()
    assert (answer["outcome"], answer["chat_id"]) == ("ALREADY_RUNNING", _LEAD_ID)
    assert staged.chat_creates() == []


def test_a_close_out_leaves_alone_a_record_a_new_lead_wrote_meanwhile(
    staged: StagedUpdateWorkspace,
) -> None:
    staged.write_state(
        "run.json",
        {"chat_agent_name": "Update-to-v090-2", "started_at": _IN_WINDOW + 30},
    )

    dispatcher.close_out_run(
        {**_OPEN_RUN, "verdict": None},
        [listed_agent(_WORKER, "RUNNING")],
        AgentEnvironment(),
    )

    assert staged.tool_calls("update_self.py") == []
    assert ["stop", _WORKER] not in staged.recorded_calls()


def test_the_watchdog_reminds_an_idle_lead_through_its_chat_and_then_waits(
    staged: StagedUpdateWorkspace,
) -> None:
    staged.write_state("run.json", {"chat_agent_name": _LEAD, "started_at": _IN_WINDOW})
    staged.set_agents([listed_agent(_LEAD, "WAITING", _LEAD_ID)])

    for minute in range(5):
        staged.tick(_IN_WINDOW + 60 * minute)

    messages = staged.tool_calls("message_chat.py")
    assert len(messages) == 1
    assert messages[0][:2] == [_LEAD_ID, "-m"]
    assert "no recorded result yet" in messages[0][2]


def test_the_watchdog_revives_a_stopped_lead_every_tick_saying_how_many_times_it_has(
    staged: StagedUpdateWorkspace,
) -> None:
    """A workspace restart stops every chat, and the user wants the update to carry on; only the lead judges a loop."""
    staged.write_state("run.json", {**_OPEN_RUN, "verdict": None})
    staged.set_agents(
        [
            listed_agent(_LEAD, "STOPPED", _LEAD_ID),
            listed_agent(_WORKER, "STOPPED"),
        ]
    )

    for minute in range(3):
        staged.tick(_IN_WINDOW + 60 * minute)

    messages = staged.tool_calls("message_chat.py")
    assert [message[:2] for message in messages] == [[_LEAD_ID, "-m"]] * 3
    assert [
        f"Restarts so far: {count}." in message[2]
        for count, message in enumerate(messages, 1)
    ] == [True] * 3
    assert staged.tool_calls("update_self.py") == []
    assert ["stop", _WORKER] not in staged.recorded_calls()


def test_without_the_chat_messenger_a_revive_starts_the_stopped_lead_through_mngr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``mngr message`` only delivers to a running agent unless told to start it."""
    staged = stage_update_workspace(tmp_path, monkeypatch, has_message_chat=False)
    staged.write_state("run.json", {**_OPEN_RUN, "verdict": None})
    staged.set_agents([listed_agent(_LEAD, "STOPPED", _LEAD_ID)])

    staged.tick(_IN_WINDOW)

    messages = [call for call in staged.recorded_calls() if call[:1] == ["message"]]
    assert [message[:4] for message in messages] == [
        ["message", _LEAD, "--start", "-m"]
    ]
    assert "Restarts so far: 1." in messages[0][4]


def test_a_revive_leaves_alone_a_lead_that_recorded_its_verdict_meanwhile(
    staged: StagedUpdateWorkspace,
) -> None:
    staged.write_state("run.json", {**_OPEN_RUN, "verdict": "UPDATED"})

    dispatcher.revive_stopped_lead(
        {**_OPEN_RUN, "verdict": None},
        [listed_agent(_LEAD, "STOPPED", _LEAD_ID)],
        {},
        AgentEnvironment(),
    )

    assert staged.tool_calls("message_chat.py") == []


@pytest.mark.parametrize(
    "wrapper_dir", ["system/libs/routines", "system/libs/automations"]
)
def test_under_cron_the_agent_environment_is_restored_through_the_templates_wrapper_wherever_it_lives(
    staged: StagedUpdateWorkspace, monkeypatch: pytest.MonkeyPatch, wrapper_dir: str
) -> None:
    """The routines rework moved the wrapper; a template from before it keeps it where it was."""
    monkeypatch.delenv("MNGR_HOST_DIR")
    wrapper = staged.root / wrapper_dir / "with_agent_env.sh"
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    agent_id = f"agent-{uuid4().hex}"
    wrapper.write_text(f'export MNGR_AGENT_ID={agent_id}\nexec "$@"\n')

    assert AgentEnvironment().variables()["MNGR_AGENT_ID"] == agent_id


def test_a_failed_create_that_made_no_chat_is_tried_again_at_the_next_tick(
    staged: StagedUpdateWorkspace,
) -> None:
    staged.write_state("schedule.json", _schedule())
    staged.script_tool(
        "system/scripts/message_chat.py",
        [
            ScriptedToolAnswer(
                exit_code=1,
                stderr="The chat app did not create the chat: mngr create exited 1\n",
            ),
            ScriptedToolAnswer(stdout=json.dumps(STAGED_CREATED_CHAT) + "\n"),
        ],
    )

    staged.tick(_IN_WINDOW)
    staged.tick(_IN_WINDOW + 60)

    assert len(staged.chat_creates()) == 2
    schedule = staged.read_state("schedule.json")
    assert schedule is not None and schedule["marked_chat"] == _LEAD


def test_a_chat_a_failed_create_left_behind_is_adopted_rather_than_made_again(
    staged: StagedUpdateWorkspace,
) -> None:
    """A create can fail or time out after its chat exists; the chat carries the attempt's label."""
    staged.write_state("schedule.json", _schedule())
    staged.script_tool(
        "system/scripts/message_chat.py",
        [
            ScriptedToolAnswer(
                exit_code=1, stderr="The chat app did not create the chat: timed out\n"
            )
        ],
    )
    staged.tick(_IN_WINDOW)
    attempt = staged.read_state("dispatch-attempt.json")
    assert attempt is not None
    left_behind = {
        **listed_agent(_LEAD, "RUNNING", _LEAD_ID),
        "update_dispatch_attempt": attempt["attempt_id"],
    }
    staged.set_agents([left_behind])

    staged.tick(_IN_WINDOW + 60)

    assert len(staged.chat_creates()) == 1
    assert f"update_dispatch_attempt={attempt['attempt_id']}" in labels_of(
        staged.chat_creates()[0]
    )
    schedule = staged.read_state("schedule.json")
    assert schedule is not None and schedule["marked_chat"] == _LEAD
    dispatch = staged.read_state("last-dispatch.json")
    assert dispatch is not None
    assert (dispatch["chat_agent_name"], dispatch["chat_id"]) == (_LEAD, _LEAD_ID)
    log_lines = staged.dispatcher_log_lines()
    assert any(
        line.endswith(f"adopted update chat {_LEAD} (scheduled, target minds-v0.9.0)")
        for line in log_lines
    )
    assert not any("created update chat" in line for line in log_lines)


def test_a_failed_create_is_not_made_again_while_the_chats_cannot_be_listed_to_find_its_chat(
    staged: StagedUpdateWorkspace,
) -> None:
    staged.write_state("schedule.json", _schedule())
    staged.script_tool(
        "system/scripts/message_chat.py",
        [
            ScriptedToolAnswer(
                exit_code=1, stderr="The chat app did not create the chat: timed out\n"
            )
        ],
    )
    staged.tick(_IN_WINDOW)
    staged.set_agents([], is_services_agent_listed=False)

    staged.tick(_IN_WINDOW + 60)

    assert len(staged.chat_creates()) == 1
    schedule = staged.read_state("schedule.json")
    assert schedule is not None and schedule["pending_skip_reason"] == "DISPATCH_FAILED"


@pytest.mark.parametrize(
    ("attempt_target", "attempt_age_seconds", "leftover_state"),
    [
        ("minds-v0.8.0", 60, "RUNNING"),
        ("minds-v0.9.0", 16 * 60, "RUNNING"),
        ("minds-v0.9.0", 60, "STOPPED"),
    ],
    ids=("another-target", "too-old", "ended"),
)
def test_a_left_behind_chat_from_another_target_an_old_attempt_or_ended_is_not_adopted(
    staged: StagedUpdateWorkspace,
    attempt_target: str,
    attempt_age_seconds: int,
    leftover_state: str,
) -> None:
    staged.write_state("schedule.json", _schedule())
    old_attempt_id = uuid4().hex
    staged.write_state(
        "dispatch-attempt.json",
        {
            "schema": 1,
            "attempt_id": old_attempt_id,
            "target_ref": attempt_target,
            "attempted_at": _IN_WINDOW - attempt_age_seconds,
        },
    )
    left_behind = {
        **listed_agent("update-to-old", leftover_state),
        "update_dispatch_attempt": old_attempt_id,
    }
    staged.set_agents([left_behind])

    staged.tick(_IN_WINDOW)

    creates = staged.chat_creates()
    assert len(creates) == 1
    assert f"update_dispatch_attempt={old_attempt_id}" not in labels_of(creates[0])
    schedule = staged.read_state("schedule.json")
    assert schedule is not None and schedule["marked_chat"] == _LEAD


def test_a_tick_leaves_alone_a_chat_an_immediate_start_made_moments_ago(
    staged: StagedUpdateWorkspace,
) -> None:
    """Starting an update now keeps the schedule armed, and the started chat records no run for minutes."""
    staged.write_state("schedule.json", _schedule())
    staged.write_state(
        "run.json",
        {
            "chat_agent_name": "old",
            "started_at": _WINDOW_OPENED - 2 * 86400,
            "verdict": "UPDATED",
        },
    )
    staged.write_state(
        "last-dispatch.json",
        {
            "chat_agent_name": _LEAD,
            "chat_id": _LEAD_ID,
            "started_by": "immediate",
            "dispatched_at": _IN_WINDOW - 60,
        },
    )
    staged.set_agents([listed_agent(_LEAD, "RUNNING", _LEAD_ID)])

    staged.tick(_IN_WINDOW)

    assert staged.chat_creates() == []
    schedule = staged.read_state("schedule.json")
    assert (
        schedule is not None and schedule["pending_skip_reason"] == "UPDATE_IN_FLIGHT"
    )


def test_a_lead_that_ends_before_recording_a_start_is_not_replaced_every_minute(
    staged: StagedUpdateWorkspace,
) -> None:
    staged.write_state("schedule.json", _schedule())
    staged.tick(_IN_WINDOW)
    staged.set_agents([listed_agent(_LEAD, "DONE")])

    for minute in range(1, 6):
        staged.tick(_IN_WINDOW + 60 * minute)
    assert len(staged.chat_creates()) == 1

    staged.tick(_IN_WINDOW + 16 * 60)
    assert len(staged.chat_creates()) == 2


def test_a_chat_the_chat_app_is_still_creating_is_not_dispatched_again_before_it_is_listed(
    staged: StagedUpdateWorkspace,
) -> None:
    staged.write_state("schedule.json", _schedule())
    staged.script_tool(
        "system/scripts/message_chat.py",
        [
            ScriptedToolAnswer(
                exit_code=1,
                stderr=f"The chat app did not create the chat: Chat '{_LEAD}' is still being created; "
                "its window will show how that ends\n",
            ),
            ScriptedToolAnswer(stdout=json.dumps(STAGED_CREATED_CHAT) + "\n"),
        ],
    )
    staged.tick(_IN_WINDOW)
    staged.tick(_IN_WINDOW + 60)
    assert len(staged.chat_creates()) == 1

    # Never listed long after its create was answered: that create came to nothing.
    staged.tick(_IN_WINDOW + 16 * 60)
    assert len(staged.chat_creates()) == 2


def test_a_listing_without_the_services_agent_never_closes_a_run_out(
    staged: StagedUpdateWorkspace,
) -> None:
    """Such a listing read the wrong host dir; it is no evidence that the run's agent is gone."""
    staged.write_state("run.json", {"chat_agent_name": _LEAD, "started_at": _IN_WINDOW})
    staged.set_agents([], is_services_agent_listed=False)

    staged.tick(_IN_WINDOW)

    assert staged.tool_calls("update_self.py") == []


def test_a_listing_that_exited_non_zero_still_counts_when_it_lists_the_services_agent(
    staged: StagedUpdateWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--on-error continue`` exits non-zero for one provider's failure while still listing every agent it could."""
    staged.write_state("run.json", {**_OPEN_RUN, "verdict": None})
    staged.set_agents([listed_agent(_WORKER, "RUNNING")])
    monkeypatch.setenv("STUB_EXIT_LIST", "1")

    staged.tick(_IN_WINDOW)

    assert [call[:3] for call in staged.tool_calls("update_self.py")] == [
        ["run-status", "verdict", "STUCK"]
    ]


def test_the_watchdog_closes_nothing_out_until_the_verdict_is_recorded(
    staged: StagedUpdateWorkspace,
) -> None:
    """The template's recorder is the run record's one writer; while it fails, the worker and leases are left be."""
    staged.write_state("run.json", {**_OPEN_RUN, "verdict": None})
    staged.set_agents([listed_agent(_WORKER, "RUNNING")])
    staged.script_tool(
        ".agents/skills/update-self/scripts/update_self.py",
        [ScriptedToolAnswer(exit_code=1, stderr="broken\n")],
    )

    staged.tick(_IN_WINDOW)

    assert len(staged.tool_calls("update_self.py")) == 1
    assert ["stop", _WORKER] not in staged.recorded_calls()


def test_a_tick_keeps_the_log_bounded_even_when_it_logs_nothing(
    staged: StagedUpdateWorkspace,
) -> None:
    """Cron appends each tick's stderr to the log, so a quiet tick has to keep the file bounded too."""
    log_path = staged.state_path("dispatcher.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        "mngr list failed: something long\n"
        * (dispatcher.DISPATCHER_LOG_MAX_BYTES // 20)
    )

    staged.tick(_IN_WINDOW)

    assert log_path.stat().st_size <= dispatcher.DISPATCHER_LOG_MAX_BYTES
    assert log_path.read_text().endswith("mngr list failed: something long\n")


def _busy_row(chat_id: str, agent_id: str, agent_name: str, is_busy: bool) -> str:
    """One line of the background-task CLI's ``list --format tsv``, with one pending task."""
    return f"{chat_id}\t{agent_id}\t{agent_name}\t{'true' if is_busy else 'false'}\t1\n"


def test_a_lead_waiting_on_a_background_task_is_not_reminded(
    staged: StagedUpdateWorkspace,
) -> None:
    staged.write_state("run.json", {"chat_agent_name": _LEAD, "started_at": _IN_WINDOW})
    staged.set_agents([listed_agent(_LEAD, "WAITING", _LEAD_ID)])
    staged.script_tool(
        "system/scripts/background_tasks.py",
        [ScriptedToolAnswer(stdout=_busy_row(_LEAD_ID, _LEAD_ID, _LEAD, True))],
    )

    for minute in range(5):
        staged.tick(_IN_WINDOW + 60 * minute)

    assert staged.tool_calls("message_chat.py") == []
    assert staged.tool_calls("background_tasks.py")[0] == ["list", "--format", "tsv"]


def test_the_busy_chats_are_the_clis_rows_whether_busy_or_not(
    staged: StagedUpdateWorkspace,
) -> None:
    other_chat_id = f"agent-{uuid4().hex}"
    staged.script_tool(
        "system/scripts/background_tasks.py",
        [
            ScriptedToolAnswer(
                stdout=_busy_row(_LEAD_ID, _WORKER_ID, _LEAD, True)
                + _busy_row(other_chat_id, other_chat_id, "", False)
                # A line short of the four columns is no row.
                + f"{other_chat_id}\t{other_chat_id}\n"
            )
        ],
    )

    rows = dispatcher.busy_chat_rows(AgentEnvironment())

    assert rows == [
        {
            "chat_id": _LEAD_ID,
            "agent_id": _WORKER_ID,
            "agent_name": _LEAD,
            "is_busy": True,
        },
        {
            "chat_id": other_chat_id,
            "agent_id": other_chat_id,
            "agent_name": "",
            "is_busy": False,
        },
    ]


def test_the_busy_agents_are_named_by_name_or_else_by_id_and_always_by_chat(
    staged: StagedUpdateWorkspace,
) -> None:
    nameless_chat_id = f"agent-{uuid4().hex}"
    idle_chat_id = f"agent-{uuid4().hex}"
    staged.script_tool(
        "system/scripts/background_tasks.py",
        [
            ScriptedToolAnswer(
                stdout=_busy_row(_LEAD_ID, _WORKER_ID, _LEAD, True)
                + _busy_row(nameless_chat_id, nameless_chat_id, "", True)
                + _busy_row(idle_chat_id, idle_chat_id, "idle-chat", False)
            )
        ],
    )

    names, agent_ids, chat_ids = dispatcher.busy_agents(AgentEnvironment())

    assert (names, agent_ids, chat_ids) == (
        {_LEAD},
        {nameless_chat_id},
        {_LEAD_ID, nameless_chat_id},
    )


@pytest.mark.parametrize("has_cli", [False, True], ids=["no-cli", "failing-cli"])
def test_no_chat_is_busy_without_the_background_task_cli_or_when_it_fails(
    staged: StagedUpdateWorkspace, has_cli: bool
) -> None:
    if has_cli:
        staged.script_tool(
            "system/scripts/background_tasks.py",
            [
                ScriptedToolAnswer(
                    exit_code=1, stdout=_busy_row(_LEAD_ID, _LEAD_ID, _LEAD, True)
                )
            ],
        )

    assert dispatcher.busy_chat_rows(AgentEnvironment()) == []
    assert dispatcher.busy_agents(AgentEnvironment()) == (set(), set(), set())


_LEAD_PID = 4100
_WORKER_PID = 4200
_HELD_AGENT_ID_KEY = "agent_id_held_by_update_dispatcher"


def _stage_running_update(staged: StagedUpdateWorkspace) -> None:
    """A run with no verdict whose lead and worker are both working, each registered at its launch band."""
    staged.write_state("run.json", {**_OPEN_RUN, "verdict": None})
    staged.set_agents(
        [
            listed_agent(_LEAD, "RUNNING", _LEAD_ID),
            listed_agent(_WORKER, "RUNNING", _WORKER_ID),
        ]
    )
    staged.register_agent_process(
        _LEAD_PID, _LEAD, is_worker=False, agent_id=_LEAD_ID, oom_score_adj=560
    )
    staged.register_agent_process(
        _WORKER_PID, _WORKER, is_worker=True, agent_id=_WORKER_ID, oom_score_adj=600
    )


def test_an_in_flight_runs_lead_and_worker_are_held_and_the_lead_is_hidden_from_the_chat_prioritizer(
    staged: StagedUpdateWorkspace,
) -> None:
    _stage_running_update(staged)
    other_chat_id = f"agent-{uuid4().hex}"
    staged.register_agent_process(
        4300, "my-chat", is_worker=False, agent_id=other_chat_id, oom_score_adj=560
    )

    for minute in range(2):
        staged.tick(_IN_WINDOW + 60 * minute)

    assert (staged.oom_score_adj(_LEAD_PID), staged.oom_score_adj(_WORKER_PID)) == (
        90,
        90,
    )
    assert staged.registry_entry(_LEAD_PID) == {
        "agent_name": _LEAD,
        "is_worker": False,
        _HELD_AGENT_ID_KEY: _LEAD_ID,
    }
    assert staged.registry_entry(_WORKER_PID) == {
        "agent_name": _WORKER,
        "is_worker": True,
        "agent_id": _WORKER_ID,
    }
    assert staged.oom_score_adj(4300) == 560
    assert staged.registry_entry(4300)["agent_id"] == other_chat_id
    # Logged when the hold began, not again on the tick that found it in place.
    assert (
        len(
            [
                line
                for line in staged.dispatcher_log_lines()
                if "holding update agent" in line
            ]
        )
        == 2
    )


@pytest.mark.parametrize(
    ("environ_agent_name", "tgid"),
    [
        # A stale entry whose pid a different agent now has.
        ("my-chat", None),
        # A stale entry whose pid is now a thread of another process.
        (None, 77),
    ],
)
def test_an_entry_whose_process_is_not_that_agent_is_left_alone(
    staged: StagedUpdateWorkspace, environ_agent_name: str | None, tgid: int | None
) -> None:
    _stage_running_update(staged)
    staged.register_agent_process(
        4101,
        _LEAD,
        is_worker=False,
        agent_id=_LEAD_ID,
        oom_score_adj=300,
        environ_agent_name=environ_agent_name,
        tgid=tgid,
    )

    staged.tick(_IN_WINDOW)

    assert staged.oom_score_adj(4101) == 300
    assert staged.registry_entry(4101) == {
        "agent_name": _LEAD,
        "is_worker": False,
        "agent_id": _LEAD_ID,
    }
    assert staged.oom_score_adj(_LEAD_PID) == 90


def test_a_process_whose_identity_cannot_be_read_is_left_alone_and_the_failure_logged(
    staged: StagedUpdateWorkspace,
) -> None:
    _stage_running_update(staged)
    environ_path = staged.proc_root / str(_LEAD_PID) / "environ"
    environ_path.unlink()
    environ_path.mkdir()

    staged.tick(_IN_WINDOW)

    assert staged.oom_score_adj(_LEAD_PID) == 560
    assert staged.registry_entry(_LEAD_PID)["agent_id"] == _LEAD_ID
    assert any(
        f"could not read {environ_path}" in line
        for line in staged.dispatcher_log_lines()
    )


def test_once_the_run_has_a_verdict_its_agents_are_handed_back_to_the_template(
    staged: StagedUpdateWorkspace,
) -> None:
    _stage_running_update(staged)
    staged.tick(_IN_WINDOW)

    staged.write_state("run.json", {**_OPEN_RUN, "verdict": "UPDATED"})
    for minute in range(1, 3):
        staged.tick(_IN_WINDOW + 60 * minute)

    assert staged.registry_entry(_LEAD_PID) == {
        "agent_name": _LEAD,
        "is_worker": False,
        "agent_id": _LEAD_ID,
    }
    assert (staged.oom_score_adj(_LEAD_PID), staged.oom_score_adj(_WORKER_PID)) == (
        560,
        600,
    )
    assert (
        len(
            [
                line
                for line in staged.dispatcher_log_lines()
                if "released update" in line
            ]
        )
        == 2
    )


def test_a_lead_registered_without_an_agent_id_is_released_once_the_run_has_a_verdict(
    staged: StagedUpdateWorkspace,
) -> None:
    _stage_running_update(staged)
    staged.register_agent_process(
        _LEAD_PID, _LEAD, is_worker=False, agent_id=None, oom_score_adj=560
    )
    staged.tick(_IN_WINDOW)
    assert staged.oom_score_adj(_LEAD_PID) == 90

    staged.write_state("run.json", {**_OPEN_RUN, "verdict": "UPDATED"})
    staged.tick(_IN_WINDOW + 60)

    assert staged.oom_score_adj(_LEAD_PID) == 560
    assert staged.registry_entry(_LEAD_PID) == {"agent_name": _LEAD, "is_worker": False}


def test_a_lead_restarted_mid_run_is_held_again_and_both_its_entries_released_after(
    staged: StagedUpdateWorkspace,
) -> None:
    """A restart registers the lead afresh under its new pid; the old pid's entry stays until the template prunes it."""
    _stage_running_update(staged)
    staged.tick(_IN_WINDOW)
    shutil.rmtree(staged.proc_root / str(_LEAD_PID))
    staged.register_agent_process(
        4101, _LEAD, is_worker=False, agent_id=_LEAD_ID, oom_score_adj=560
    )

    staged.tick(_IN_WINDOW + 60)

    assert staged.oom_score_adj(4101) == 90
    assert staged.registry_entry(4101) == {
        "agent_name": _LEAD,
        "is_worker": False,
        _HELD_AGENT_ID_KEY: _LEAD_ID,
    }

    staged.write_state("run.json", {**_OPEN_RUN, "verdict": "UPDATED"})
    staged.tick(_IN_WINDOW + 120)

    assert staged.oom_score_adj(4101) == 560
    for pid in (_LEAD_PID, 4101):
        assert staged.registry_entry(pid) == {
            "agent_name": _LEAD,
            "is_worker": False,
            "agent_id": _LEAD_ID,
        }


def test_every_process_of_a_lead_that_registers_two_is_held(
    staged: StagedUpdateWorkspace,
) -> None:
    """codex registers its TUI and its app-server under one agent."""
    _stage_running_update(staged)
    staged.register_agent_process(
        4101, _LEAD, is_worker=False, agent_id=_LEAD_ID, oom_score_adj=560
    )

    staged.tick(_IN_WINDOW)

    for pid in (_LEAD_PID, 4101):
        assert staged.oom_score_adj(pid) == 90
        assert staged.registry_entry(pid) == {
            "agent_name": _LEAD,
            "is_worker": False,
            _HELD_AGENT_ID_KEY: _LEAD_ID,
        }


def test_the_state_lock_admits_its_holder_again_and_keeps_another_thread_waiting(
    staged: StagedUpdateWorkspace,
) -> None:
    is_taken_by_the_other = threading.Event()

    def take_it() -> None:
        with dispatcher.state_lock():
            is_taken_by_the_other.set()

    other = threading.Thread(target=take_it)
    with dispatcher.state_lock():
        with dispatcher.state_lock():
            pass
        other.start()

        # The hold taken again has ended, and the first one still stands.
        assert is_taken_by_the_other.wait(0.3) is False

    other.join(10)
    assert is_taken_by_the_other.is_set()
