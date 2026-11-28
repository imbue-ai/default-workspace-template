"""What the workspace says its update is doing, read from a staged workspace's files and a listing of its agents."""

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from workspace_update_control import control
from workspace_update_control.agents import TURN_ENDED_AT_KEY, read_observed_agents
from workspace_update_control.dispatcher import AgentEnvironment, BusyAgents
from workspace_update_control.state import (
    UpdateAction,
    UpdateActivity,
    UpdateRun,
    UpdateSchedule,
    UpdateSkipReason,
    UpdateStartedBy,
    UpdateStartFailure,
    UpdateStartFailureCode,
    UpdateState,
    UpdateVerdict,
    read_update_state,
)
from workspace_update_control.testing import (
    STAGED_CREATED_CHAT,
    RecordingFeed,
    ScriptedToolAnswer,
    StagedUpdateWorkspace,
    agent_state_line,
    arm_schedule_in,
    armed_schedule,
    epoch_at,
    full_state_line,
    listed_agent,
    observed_agent,
    observed_services_agent,
    run_in_place,
    stage_update_workspace,
    update_settings_document,
    write_event_lines,
)

_NOW = 1_792_000_000.0
_CEILING = "minds-v0.9.0"
_LEAD = "update-to-minds-v0-9-0"
_LEAD_ID = f"agent-{uuid4().hex}"
_CHAT_ID = f"agent-{uuid4().hex}"
_WORKER = "update-self-worker"
_WORKER_ID = f"agent-{uuid4().hex}"
_RUN_STARTED_AT = _NOW - 600.0
_OPEN_RUN: dict[str, Any] = {"chat_agent_name": _LEAD, "started_at": _RUN_STARTED_AT}

AgentsReader = Callable[[], Sequence[Mapping[str, str]] | None]


def _agent(
    name: str,
    state: str,
    agent_id: str,
    *,
    chat_id: str = "",
    turn_ended_at: float | None = None,
) -> dict[str, str]:
    """An agent as the observer's listing gives it, with when its last turn ended where that is known."""
    listed = {"id": agent_id, "name": name, "state": state, "labels.chat_id": chat_id}
    if turn_ended_at is not None:
        listed[TURN_ENDED_AT_KEY] = repr(turn_ended_at)
    return listed


def _lead(state: str, *, turn_ended_at: float | None = None) -> dict[str, str]:
    return _agent(_LEAD, state, _LEAD_ID, chat_id=_CHAT_ID, turn_ended_at=turn_ended_at)


def _listing(*agents: Mapping[str, str]) -> AgentsReader:
    return lambda: list(agents)


def _unreadable_agents() -> None:
    return None


def _agents_are_not_asked() -> list[dict[str, str]]:
    raise AssertionError("nothing here turns on the workspace's agents")


def _nobody_is_busy() -> BusyAgents:
    return frozenset(), frozenset(), frozenset()


def _busy_chats_are_not_asked() -> BusyAgents:
    raise AssertionError("nothing here turns on the chats waiting on background tasks")


def _read(
    staged: StagedUpdateWorkspace,
    *,
    now: float = _NOW,
    read_agents: AgentsReader = _agents_are_not_asked,
    read_busy_agents: Callable[[], BusyAgents] = _busy_chats_are_not_asked,
    read_installed_ref: Callable[[], str] = lambda: "",
) -> UpdateState:
    return read_update_state(
        now=now,
        proc_root=str(staged.proc_root),
        live_cron_dir=str(staged.live_cron_dir),
        agent_environment=AgentEnvironment(),
        read_agents=read_agents,
        read_busy_agents=read_busy_agents,
        read_installed_ref=read_installed_ref,
    )


def _start(
    staged: StagedUpdateWorkspace,
    *,
    override_ref: str = "",
    now: float = _NOW,
    run_in_background: Callable[[Callable[[], None]], None] = run_in_place,
    read_agents: AgentsReader = _unreadable_agents,
) -> dict[str, Any]:
    return control.start_update(
        ceiling=_CEILING,
        override_ref=override_ref,
        is_backup_configured=True,
        now=now,
        live_cron_dir=str(staged.live_cron_dir),
        proc_root=str(staged.proc_root),
        agent_environment=AgentEnvironment(),
        read_agents=read_agents,
        run_in_background=run_in_background,
        clock=lambda: now + 7.0,
        fetch_channel_file=RecordingFeed(None),
    )


def _install_the_watchdog(staged: StagedUpdateWorkspace) -> None:
    control.install_cron_entry(str(staged.live_cron_dir))


def _write_dispatch(
    staged: StagedUpdateWorkspace, dispatched_at: float, **fields: Any
) -> None:
    staged.write_state(
        "last-dispatch.json",
        {
            "schema": 1,
            "chat_agent_name": _LEAD,
            "chat_id": _CHAT_ID,
            "started_by": "scheduled",
            "ceiling": _CEILING,
            "target_ref": _CEILING,
            "dispatched_at": dispatched_at,
            **fields,
        },
    )


def _state_files(staged: StagedUpdateWorkspace) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in staged.state_path("").iterdir()}


# A workspace with nothing of an update


def test_a_workspace_with_nothing_of_an_update_is_idle_and_the_read_writes_nothing(
    staged: StagedUpdateWorkspace,
) -> None:
    state = _read(staged, read_installed_ref=lambda: "minds-v0.8.0")

    assert state == UpdateState(
        as_of=_NOW,
        installed_version="minds-v0.8.0",
        settings=None,
        channel_version=None,
        activity=UpdateActivity.IDLE,
        run=None,
        start_failure=None,
        schedule=None,
        needs_sign_in=False,
        is_update_skipped=False,
        will_self_arm=False,
        is_backup_configured=False,
        allowed_actions=(UpdateAction.START, UpdateAction.SCHEDULE),
        run_record=None,
        last_start=None,
    )
    assert not (staged.root / "data").exists()


def test_the_state_as_json_values_spells_every_nested_part_out(
    staged: StagedUpdateWorkspace,
) -> None:
    staged.write_state("run.json", {**_OPEN_RUN, "verdict": "STUCK", "detail": "No."})
    staged.write_state("schedule.json", armed_schedule(_NOW))

    state = _read(staged, read_agents=_unreadable_agents)
    document = state.document()

    assert state.run is not None and state.schedule is not None
    assert document == {
        **state._asdict(),
        "activity": "IDLE",
        "run": {
            "started_at": _RUN_STARTED_AT,
            "chat_agent_name": _LEAD,
            "chat_id": "",
            "is_chat_gone": False,
            "started_by": None,
            "target_ref": "",
            "override_ref": "",
            "is_holding": False,
            "hold_detail": "",
            "verdict": "STUCK",
            "verdict_detail": "No.",
            "in_place_compatible_ref": "",
            "updated_to": "",
        },
        "start_failure": None,
        "schedule": state.schedule._asdict(),
        "allowed_actions": ["START", "SCHEDULE", "CANCEL_SCHEDULE", "DISMISS_OUTCOME"],
    }


# A run on the record with no verdict


def test_a_run_whose_lead_is_working_is_running_with_its_chat(
    staged: StagedUpdateWorkspace,
) -> None:
    staged.write_state("run.json", _OPEN_RUN)
    _write_dispatch(staged, _RUN_STARTED_AT - 120.0)

    state = _read(staged, read_agents=_listing(_lead("RUNNING")))

    assert state.activity is UpdateActivity.RUNNING
    assert state.run == UpdateRun(
        started_at=_RUN_STARTED_AT,
        chat_agent_name=_LEAD,
        chat_id=_CHAT_ID,
        is_chat_gone=False,
        started_by=UpdateStartedBy.SCHEDULED,
        target_ref=_CEILING,
        override_ref="",
        is_holding=False,
        hold_detail="",
        verdict=None,
        verdict_detail="",
        in_place_compatible_ref="",
        updated_to="",
    )
    assert state.run_record == _OPEN_RUN
    # A run in flight leaves nothing to start, schedule or dismiss.
    assert state.allowed_actions == ()


def test_a_recorded_hold_reads_as_waiting_at_once_with_its_detail(
    staged: StagedUpdateWorkspace,
) -> None:
    staged.write_state(
        "run.json",
        {**_OPEN_RUN, "is_holding": True, "hold_detail": "Your widget has no place."},
    )

    state = _read(staged, read_agents=_listing(_lead("RUNNING")))

    assert state.activity is UpdateActivity.WAITING
    assert state.run is not None
    assert (state.run.is_holding, state.run.hold_detail) == (
        True,
        "Your widget has no place.",
    )


@pytest.mark.parametrize(
    ("idle_seconds", "activity"),
    [
        # A listing can catch a lead between two turns.
        (39.0, UpdateActivity.RUNNING),
        (40.0, UpdateActivity.WAITING),
        (3600.0, UpdateActivity.WAITING),
    ],
)
def test_an_idle_lead_reads_as_waiting_once_its_last_turn_ended_long_enough_ago(
    staged: StagedUpdateWorkspace, idle_seconds: float, activity: UpdateActivity
) -> None:
    staged.write_state("run.json", _OPEN_RUN)

    state = _read(
        staged,
        read_agents=_listing(_lead("WAITING", turn_ended_at=_NOW - idle_seconds)),
        read_busy_agents=_nobody_is_busy,
    )

    assert state.activity is activity
    assert state.run is not None
    # Waiting because the lead sits idle is not a hold the lead recorded.
    assert (state.run.is_holding, state.run.hold_detail) == (False, "")


def test_the_busy_chats_are_read_only_for_a_lead_idle_long_enough_to_read_as_waiting(
    staged: StagedUpdateWorkspace,
) -> None:
    staged.write_state("run.json", _OPEN_RUN)
    asked: list[bool] = []

    def read_busy_agents() -> BusyAgents:
        asked.append(True)
        return _nobody_is_busy()

    _read(
        staged,
        read_agents=_listing(_lead("RUNNING")),
        read_busy_agents=read_busy_agents,
    )
    _read(
        staged,
        read_agents=_listing(_lead("WAITING", turn_ended_at=_NOW - 5.0)),
        read_busy_agents=read_busy_agents,
    )
    assert asked == []

    _read(
        staged,
        read_agents=_listing(_lead("WAITING", turn_ended_at=_NOW - 90.0)),
        read_busy_agents=read_busy_agents,
    )
    assert asked == [True]


@pytest.mark.parametrize(
    "busy",
    [
        (frozenset({_LEAD}), frozenset(), frozenset()),
        (frozenset(), frozenset({_LEAD_ID}), frozenset()),
        # The chat's id, which an agent the chat was handed off to carries too.
        (frozenset(), frozenset(), frozenset({_CHAT_ID})),
    ],
    ids=("by-name", "by-agent-id", "by-chat-id"),
)
def test_an_idle_lead_a_background_task_will_resume_is_not_waiting_on_the_user(
    staged: StagedUpdateWorkspace, busy: BusyAgents
) -> None:
    staged.write_state("run.json", _OPEN_RUN)

    state = _read(
        staged,
        read_agents=_listing(_lead("WAITING", turn_ended_at=_NOW - 600.0)),
        read_busy_agents=lambda: busy,
    )

    assert state.activity is UpdateActivity.RUNNING


@pytest.mark.parametrize(
    ("worker_state", "is_worker_busy", "activity"),
    [
        ("RUNNING", False, UpdateActivity.RUNNING),
        ("WAITING", True, UpdateActivity.RUNNING),
        ("WAITING", False, UpdateActivity.WAITING),
        ("STOPPED", False, UpdateActivity.WAITING),
        # A state the listing could not read is no evidence that the run is idle.
        ("UNKNOWN", False, UpdateActivity.RUNNING),
    ],
)
def test_an_idle_lead_is_waiting_only_when_the_worker_its_record_names_is_not_moving(
    staged: StagedUpdateWorkspace,
    worker_state: str,
    is_worker_busy: bool,
    activity: UpdateActivity,
) -> None:
    staged.write_state("run.json", {**_OPEN_RUN, "worker_agent_name": _WORKER})
    busy_names = frozenset({_WORKER}) if is_worker_busy else frozenset()

    state = _read(
        staged,
        read_agents=_listing(
            _lead("WAITING", turn_ended_at=_NOW - 600.0),
            _agent(_WORKER, worker_state, _WORKER_ID),
        ),
        read_busy_agents=lambda: (busy_names, frozenset(), frozenset()),
    )

    assert state.activity is activity


def test_an_idle_lead_with_no_recorded_turn_end_waits_once_the_watchdog_counted_it_idle(
    staged: StagedUpdateWorkspace,
) -> None:
    staged.write_state("run.json", _OPEN_RUN)
    staged.set_agents([listed_agent(_LEAD, "WAITING", _LEAD_ID)])

    before_the_watchdog = _read(
        staged, read_agents=staged.read_listed_agents, read_busy_agents=_nobody_is_busy
    )
    staged.tick(_NOW)
    after_the_watchdog = _read(
        staged, read_agents=staged.read_listed_agents, read_busy_agents=_nobody_is_busy
    )

    assert before_the_watchdog.activity is UpdateActivity.RUNNING
    assert after_the_watchdog.activity is UpdateActivity.WAITING


def test_a_run_whose_chat_was_deleted_is_stalled_with_no_chat_left_to_open(
    staged: StagedUpdateWorkspace,
) -> None:
    staged.write_state("run.json", _OPEN_RUN)
    _write_dispatch(staged, _RUN_STARTED_AT - 120.0)

    state = _read(staged, read_agents=_listing())

    assert state.activity is UpdateActivity.STALLED
    assert state.run is not None
    assert (state.run.chat_id, state.run.is_chat_gone) == ("", True)
    assert state.allowed_actions == (
        UpdateAction.START,
        UpdateAction.SCHEDULE,
        UpdateAction.DISMISS_OUTCOME,
    )


def test_a_stopped_lead_is_stalled_unless_the_watchdog_is_there_to_restart_it(
    staged: StagedUpdateWorkspace,
) -> None:
    staged.write_state("run.json", _OPEN_RUN)
    read_agents = _listing(_lead("STOPPED"))

    without_the_watchdog = _read(staged, read_agents=read_agents)
    _install_the_watchdog(staged)
    with_the_watchdog = _read(staged, read_agents=read_agents)

    assert without_the_watchdog.activity is UpdateActivity.STALLED
    assert without_the_watchdog.run is not None
    # Its chat is still there to open and resume.
    assert (
        without_the_watchdog.run.chat_id,
        without_the_watchdog.run.is_chat_gone,
    ) == (_CHAT_ID, False)
    assert with_the_watchdog.activity is UpdateActivity.RUNNING


def test_a_stalled_run_reads_as_under_way_again_once_its_lead_is_back(
    staged: StagedUpdateWorkspace,
) -> None:
    staged.write_state("run.json", _OPEN_RUN)

    assert (
        _read(staged, read_agents=_listing(_lead("STOPPED"))).activity
        is UpdateActivity.STALLED
    )
    assert (
        _read(staged, read_agents=_listing(_lead("RUNNING"))).activity
        is UpdateActivity.RUNNING
    )


def test_a_stalled_run_the_user_dismissed_is_idle_with_nothing_to_show(
    staged: StagedUpdateWorkspace,
) -> None:
    staged.write_state("run.json", _OPEN_RUN)
    stalled = _read(staged, read_agents=_listing())
    assert stalled.run is not None and stalled.run.started_at is not None

    control.record_dismissals(
        note_run_started_at=None, outcome_run_started_at=stalled.run.started_at
    )
    dismissed = _read(staged, read_agents=_listing())

    assert (dismissed.activity, dismissed.run) == (UpdateActivity.IDLE, None)
    assert UpdateAction.DISMISS_OUTCOME not in dismissed.allowed_actions


@pytest.mark.parametrize(
    ("run_fields", "activity"),
    [
        ({}, UpdateActivity.RUNNING),
        ({"applied_at": _NOW - 30.0}, UpdateActivity.FINISHING),
        ({"is_holding": True}, UpdateActivity.WAITING),
        ({"apply_phase": "merged"}, UpdateActivity.APPLYING),
    ],
)
def test_agents_that_cannot_be_read_count_as_there_and_working(
    staged: StagedUpdateWorkspace,
    run_fields: dict[str, Any],
    activity: UpdateActivity,
) -> None:
    staged.write_state("run.json", {**_OPEN_RUN, **run_fields})
    _write_dispatch(staged, _RUN_STARTED_AT - 120.0)

    state = _read(staged, read_agents=_unreadable_agents)

    assert state.activity is activity
    assert state.run is not None
    # The chat is the one the dispatch named, and nothing says that it is gone.
    assert (state.run.chat_id, state.run.is_chat_gone) == (_CHAT_ID, False)


@pytest.mark.parametrize(
    "read_agents",
    [_listing(_lead("RUNNING")), _listing(), _unreadable_agents],
    ids=("lead-working", "lead-gone", "agents-unreadable"),
)
def test_an_apply_under_way_reads_as_applying_whatever_became_of_the_lead(
    staged: StagedUpdateWorkspace, read_agents: AgentsReader
) -> None:
    staged.write_state(
        "run.json",
        {**_OPEN_RUN, "apply_phase": "merged", "apply_updated_at": _NOW - 5.0},
    )

    assert _read(staged, read_agents=read_agents).activity is UpdateActivity.APPLYING


@pytest.mark.parametrize(
    ("lead_state", "activity"),
    [("RUNNING", UpdateActivity.FINISHING), ("STOPPED", UpdateActivity.STALLED)],
)
def test_a_run_whose_apply_landed_is_finishing_while_its_lead_is_there(
    staged: StagedUpdateWorkspace, lead_state: str, activity: UpdateActivity
) -> None:
    staged.write_state("run.json", {**_OPEN_RUN, "applied_at": _NOW - 30.0})

    assert _read(staged, read_agents=_listing(_lead(lead_state))).activity is activity


def test_a_lead_whose_chat_was_renamed_is_found_by_the_chat_the_dispatch_named(
    staged: StagedUpdateWorkspace,
) -> None:
    staged.write_state("run.json", _OPEN_RUN)
    _write_dispatch(staged, _RUN_STARTED_AT - 120.0)
    renamed = _agent("My-update", "RUNNING", _LEAD_ID, chat_id=_CHAT_ID)

    state = _read(staged, read_agents=_listing(renamed))

    assert state.activity is UpdateActivity.RUNNING
    assert state.run is not None
    assert state.run.chat_id == _CHAT_ID


def test_a_record_with_no_verdict_and_no_chat_name_is_no_run(
    staged: StagedUpdateWorkspace,
) -> None:
    staged.write_state("run.json", {"started_at": _RUN_STARTED_AT})

    state = _read(staged)

    assert (state.activity, state.run) == (UpdateActivity.IDLE, None)


# A run that ended


@pytest.mark.parametrize("verdict", list(UpdateVerdict))
def test_a_run_with_a_verdict_is_idle_and_shows_how_it_ended(
    staged: StagedUpdateWorkspace, verdict: UpdateVerdict
) -> None:
    staged.write_state(
        "run.json",
        {
            **_OPEN_RUN,
            "verdict": verdict.value,
            "detail": "How it went.",
            "in_place_compatible_ref": "minds-v0.8.9",
            "resulting_ref": "minds-v0.9.0",
            "started_by": "immediate",
        },
    )
    _write_dispatch(staged, _RUN_STARTED_AT - 120.0, started_by="scheduled")
    is_landed = verdict in (
        UpdateVerdict.UPDATED,
        UpdateVerdict.UPDATED_WITH_REBUILD_ITEMS,
    )

    state = _read(staged)

    assert state.activity is UpdateActivity.IDLE
    assert state.run == UpdateRun(
        started_at=_RUN_STARTED_AT,
        chat_agent_name=_LEAD,
        chat_id=_CHAT_ID,
        is_chat_gone=False,
        # The record's own word outranks the dispatch's.
        started_by=UpdateStartedBy.IMMEDIATE,
        target_ref=_CEILING,
        override_ref="",
        is_holding=False,
        hold_detail="",
        verdict=verdict,
        verdict_detail="How it went.",
        in_place_compatible_ref="minds-v0.8.9",
        updated_to="minds-v0.9.0" if is_landed else "",
    )
    is_decision_pending = verdict is UpdateVerdict.NEEDS_DECISION
    assert (UpdateAction.START in state.allowed_actions) is not is_decision_pending
    assert (UpdateAction.SCHEDULE in state.allowed_actions) is not is_decision_pending
    assert UpdateAction.DISMISS_OUTCOME in state.allowed_actions
    assert (UpdateAction.DISMISS_NOTE in state.allowed_actions) is is_landed


@pytest.mark.parametrize(
    ("dismissed", "verdict", "updated_to", "is_run_shown"),
    [
        ({}, UpdateVerdict.UPDATED, "minds-v0.9.0", True),
        # The note outlives its outcome's dismissal, and the outcome its note's.
        ({"outcome": 0.0}, None, "minds-v0.9.0", True),
        ({"note": 0.0}, UpdateVerdict.UPDATED, "", True),
        ({"outcome": 0.0, "note": 0.0}, None, "", False),
        # A dismissal of an earlier run covers nothing of this one.
        ({"outcome": -1.0, "note": -1.0}, UpdateVerdict.UPDATED, "minds-v0.9.0", True),
        # One of a later run covers this one too.
        ({"outcome": 500.0, "note": 500.0}, None, "", False),
    ],
)
def test_what_the_user_dismissed_of_a_landed_run_is_left_out(
    staged: StagedUpdateWorkspace,
    dismissed: dict[str, float],
    verdict: UpdateVerdict | None,
    updated_to: str,
    is_run_shown: bool,
) -> None:
    staged.write_state(
        "run.json",
        {**_OPEN_RUN, "verdict": "UPDATED", "resulting_ref": "minds-v0.9.0"},
    )
    control.record_dismissals(
        note_run_started_at=_RUN_STARTED_AT + dismissed["note"]
        if "note" in dismissed
        else None,
        outcome_run_started_at=_RUN_STARTED_AT + dismissed["outcome"]
        if "outcome" in dismissed
        else None,
    )

    state = _read(staged, read_agents=_unreadable_agents)

    assert (state.run is not None) is is_run_shown
    if state.run is not None:
        assert (state.run.verdict, state.run.updated_to) == (verdict, updated_to)
    assert (UpdateAction.DISMISS_OUTCOME in state.allowed_actions) is (
        verdict is not None
    )
    assert (UpdateAction.DISMISS_NOTE in state.allowed_actions) is bool(updated_to)


def test_a_dismissed_failure_shows_nothing_and_asks_nothing_of_the_agents(
    staged: StagedUpdateWorkspace,
) -> None:
    staged.write_state("run.json", {**_OPEN_RUN, "verdict": "STUCK"})
    control.record_dismissals(
        note_run_started_at=None, outcome_run_started_at=_RUN_STARTED_AT
    )

    state = _read(staged)

    assert (state.activity, state.run) == (UpdateActivity.IDLE, None)
    assert state.run_record == {**_OPEN_RUN, "verdict": "STUCK"}


def test_a_verdict_this_template_does_not_know_ends_the_run_and_shows_none(
    staged: StagedUpdateWorkspace,
) -> None:
    staged.write_state("run.json", {**_OPEN_RUN, "verdict": "POSTPONED"})

    state = _read(staged)

    assert (state.activity, state.run) == (UpdateActivity.IDLE, None)
    assert UpdateAction.START in state.allowed_actions


def test_a_run_with_no_readable_start_cannot_be_dismissed(
    staged: StagedUpdateWorkspace,
) -> None:
    staged.write_state(
        "run.json",
        {"chat_agent_name": _LEAD, "verdict": "UPDATED", "resulting_ref": "v9"},
    )
    control.record_dismissals(note_run_started_at=_NOW, outcome_run_started_at=_NOW)

    state = _read(staged, read_agents=_unreadable_agents)

    assert state.run is not None
    assert (state.run.started_at, state.run.verdict) == (None, UpdateVerdict.UPDATED)
    assert UpdateAction.DISMISS_OUTCOME not in state.allowed_actions
    assert UpdateAction.DISMISS_NOTE not in state.allowed_actions


@pytest.mark.parametrize(
    ("read_agents", "chat_id", "is_chat_gone"),
    [
        (_listing(_agent(_LEAD, "STOPPED", _LEAD_ID)), _LEAD_ID, False),
        (_listing(), "", True),
        (_unreadable_agents, "", False),
    ],
    ids=("listed", "deleted", "unreadable"),
)
def test_the_chat_of_a_run_no_dispatch_made_is_looked_up_among_the_agents(
    staged: StagedUpdateWorkspace,
    read_agents: AgentsReader,
    chat_id: str,
    is_chat_gone: bool,
) -> None:
    """A run started by hand in a chat, or one whose dispatch a later one replaced."""
    staged.write_state("run.json", {**_OPEN_RUN, "verdict": "STUCK"})
    _write_dispatch(staged, _NOW - 3600.0, chat_agent_name="another-chat", chat_id="x")

    state = _read(staged, read_agents=read_agents)

    assert state.run is not None
    assert (state.run.chat_id, state.run.is_chat_gone) == (chat_id, is_chat_gone)
    # The dispatch is another run's, so it says nothing of what this one aimed at or who began it.
    assert (state.run.target_ref, state.run.started_by) == ("", None)


def test_a_run_aimed_at_a_version_the_user_named_says_so(
    staged: StagedUpdateWorkspace,
) -> None:
    staged.write_state("run.json", {**_OPEN_RUN, "verdict": "STUCK"})
    _write_dispatch(staged, _RUN_STARTED_AT - 120.0, target_ref="minds-v0.8.7")

    state = _read(staged)

    assert state.run is not None
    assert (state.run.target_ref, state.run.override_ref) == (
        "minds-v0.8.7",
        "minds-v0.8.7",
    )


# A start, and the chat it makes


def test_a_start_reads_as_starting_until_its_chat_is_made_then_as_running_with_the_chat(
    staged: StagedUpdateWorkspace,
) -> None:
    queued: list[Callable[[], None]] = []
    staged.write_state("run.json", {**_OPEN_RUN, "verdict": "STUCK"})

    answer = _start(
        staged, override_ref="minds-v0.8.7", run_in_background=queued.append
    )
    starting = _read(staged)

    assert answer["outcome"] == "STARTING"
    assert starting.activity is UpdateActivity.STARTING
    # The run before it is no longer what is shown.
    assert starting.run == UpdateRun(
        started_at=None,
        chat_agent_name="",
        chat_id="",
        is_chat_gone=False,
        started_by=UpdateStartedBy.IMMEDIATE,
        target_ref="minds-v0.8.7",
        override_ref="minds-v0.8.7",
        is_holding=False,
        hold_detail="",
        verdict=None,
        verdict_detail="",
        in_place_compatible_ref="",
        updated_to="",
    )
    assert starting.start_failure is None
    assert starting.allowed_actions == ()

    queued[0]()
    running = _read(staged, read_agents=_unreadable_agents)

    assert running.activity is UpdateActivity.RUNNING
    assert running.run == UpdateRun(
        started_at=_NOW,
        chat_agent_name=STAGED_CREATED_CHAT["name"],
        chat_id=STAGED_CREATED_CHAT["chat_id"],
        is_chat_gone=False,
        started_by=UpdateStartedBy.IMMEDIATE,
        target_ref="minds-v0.8.7",
        override_ref="minds-v0.8.7",
        is_holding=False,
        hold_detail="",
        verdict=None,
        verdict_detail="",
        in_place_compatible_ref="",
        updated_to="",
    )
    assert running.last_start is not None
    assert running.last_start["outcome"] == "CREATED"


def test_a_start_at_the_channels_version_names_no_version_of_the_users(
    staged: StagedUpdateWorkspace,
) -> None:
    _start(staged)

    state = _read(staged, read_agents=_unreadable_agents)

    assert state.run is not None
    assert (state.run.target_ref, state.run.override_ref) == (_CEILING, "")


def test_a_chat_made_moments_ago_is_the_run_before_it_has_recorded_one(
    staged: StagedUpdateWorkspace,
) -> None:
    """Update chats are named for their target, so the record may still hold an earlier run under the same name."""
    staged.write_state(
        "run.json",
        {"chat_agent_name": _LEAD, "started_at": _NOW - 9000.0, "verdict": "STUCK"},
    )
    _write_dispatch(staged, _NOW - 60.0)

    state = _read(staged, read_agents=_listing(_lead("RUNNING")))

    assert state.activity is UpdateActivity.RUNNING
    assert state.run is not None
    assert (state.run.started_at, state.run.verdict) == (_NOW - 60.0, None)
    assert state.allowed_actions == ()

    staged.write_state(
        "run.json", {"chat_agent_name": _LEAD, "started_at": _NOW - 30.0}
    )
    recorded = _read(staged, read_agents=_listing(_lead("RUNNING")))

    assert recorded.activity is UpdateActivity.RUNNING
    assert recorded.run is not None
    assert recorded.run.started_at == _NOW - 30.0


def test_a_chat_made_moments_ago_that_sits_idle_reads_as_waiting(
    staged: StagedUpdateWorkspace,
) -> None:
    _write_dispatch(staged, _NOW - 300.0)

    state = _read(
        staged,
        read_agents=_listing(_lead("WAITING", turn_ended_at=_NOW - 200.0)),
        read_busy_agents=_nobody_is_busy,
    )

    assert state.activity is UpdateActivity.WAITING


@pytest.mark.parametrize(
    ("read_agents", "chat_id", "is_chat_gone"),
    [
        (_listing(_lead("STOPPED")), _CHAT_ID, False),
        (_listing(), "", True),
    ],
    ids=("stopped", "deleted"),
)
def test_a_chat_made_moments_ago_that_ended_before_it_recorded_a_run_is_stalled(
    staged: StagedUpdateWorkspace,
    read_agents: AgentsReader,
    chat_id: str,
    is_chat_gone: bool,
) -> None:
    _write_dispatch(staged, _NOW - 60.0)
    # The watchdog restarts a stopped lead only once that lead has put its run on the record.
    _install_the_watchdog(staged)

    state = _read(staged, read_agents=read_agents)

    assert state.activity is UpdateActivity.STALLED
    assert state.run is not None
    assert (state.run.started_at, state.run.chat_id, state.run.is_chat_gone) == (
        _NOW - 60.0,
        chat_id,
        is_chat_gone,
    )

    control.record_dismissals(
        note_run_started_at=None, outcome_run_started_at=state.run.started_at
    )
    dismissed = _read(staged, read_agents=read_agents)

    assert (dismissed.activity, dismissed.run) == (UpdateActivity.IDLE, None)


def test_a_chat_made_with_no_id_yet_is_on_its_way_while_it_is_unlisted(
    staged: StagedUpdateWorkspace,
) -> None:
    _write_dispatch(staged, _NOW - 60.0, chat_id="")

    state = _read(staged, read_agents=_listing())

    assert state.activity is UpdateActivity.RUNNING
    assert state.run is not None
    assert (state.run.chat_id, state.run.is_chat_gone) == ("", False)


def test_a_chat_made_long_ago_that_never_recorded_a_run_is_no_run(
    staged: StagedUpdateWorkspace,
) -> None:
    staged.write_state("run.json", {**_OPEN_RUN, "verdict": "REFUSED"})
    _write_dispatch(
        staged, _NOW - 16 * 60.0, chat_agent_name="update-2", chat_id="agent-x"
    )

    state = _read(staged, read_agents=_listing(_lead("STOPPED")))

    assert state.activity is UpdateActivity.IDLE
    assert state.run is not None
    assert (state.run.chat_agent_name, state.run.verdict) == (
        _LEAD,
        UpdateVerdict.REFUSED,
    )


# A start that made no chat


def _fail_the_create(staged: StagedUpdateWorkspace, stderr: str) -> None:
    staged.script_tool(
        "system/scripts/message_chat.py",
        [ScriptedToolAnswer(exit_code=1, stderr=stderr)],
    )


def test_a_start_that_made_no_chat_is_told_until_the_user_dismisses_it(
    staged: StagedUpdateWorkspace,
) -> None:
    _fail_the_create(staged, "the chat app fell over")

    attempt_id = _start(staged)["attempt_id"]
    failed = _read(staged)

    assert failed.activity is UpdateActivity.IDLE
    assert failed.start_failure == UpdateStartFailure(
        attempt_id=attempt_id,
        code=UpdateStartFailureCode.CREATE_FAILED,
        detail="the chat app fell over",
    )
    assert failed.allowed_actions == (
        UpdateAction.START,
        UpdateAction.SCHEDULE,
        UpdateAction.DISMISS_OUTCOME,
    )

    assert control.dismiss_start_failure("some-other-attempt") is False
    assert _read(staged).start_failure == failed.start_failure

    assert control.dismiss_start_failure(attempt_id) is True
    dismissed = _read(staged)

    assert dismissed.start_failure is None
    assert UpdateAction.DISMISS_OUTCOME not in dismissed.allowed_actions


def test_a_start_whose_process_is_gone_is_told_as_a_failed_create(
    staged: StagedUpdateWorkspace, exited_pid: int
) -> None:
    attempt_id = _start(staged, run_in_background=lambda work: None)["attempt_id"]
    record = staged.read_state("last-start.json")
    assert record is not None
    staged.write_state("last-start.json", {**record, "pid": exited_pid})

    state = _read(staged)

    assert state.activity is UpdateActivity.IDLE
    assert state.start_failure is not None
    assert (state.start_failure.attempt_id, state.start_failure.code) == (
        attempt_id,
        UpdateStartFailureCode.CREATE_FAILED,
    )


@pytest.mark.parametrize(
    "later_file",
    ["run.json", "last-dispatch.json"],
    ids=("a-run-began", "a-chat-was-made"),
)
def test_a_failed_start_is_no_longer_told_once_an_update_started_after_it(
    staged: StagedUpdateWorkspace, later_file: str
) -> None:
    _fail_the_create(staged, "the chat app fell over")
    _start(staged)
    assert _read(staged).start_failure is not None

    if later_file == "run.json":
        staged.write_state(
            "run.json", {"chat_agent_name": _LEAD, "started_at": _NOW + 300.0}
        )
    else:
        _write_dispatch(staged, _NOW + 300.0)

    state = _read(staged, now=_NOW + 360.0, read_agents=_listing(_lead("RUNNING")))

    assert state.start_failure is None
    assert state.activity is UpdateActivity.RUNNING


def test_a_failed_start_from_before_the_last_run_stands_beside_that_runs_outcome(
    staged: StagedUpdateWorkspace,
) -> None:
    staged.write_state("run.json", {**_OPEN_RUN, "verdict": "STUCK"})
    _fail_the_create(staged, "the chat app fell over")
    _start(staged)

    state = _read(staged, read_agents=_unreadable_agents)

    assert state.start_failure is not None
    assert state.run is not None
    assert state.run.verdict is UpdateVerdict.STUCK


# Signing in


def test_a_start_refused_for_want_of_an_account_is_told_until_one_is_signed_in(
    staged: StagedUpdateWorkspace,
) -> None:
    local_settings = staged.root / ".mngr" / "settings.local.toml"
    signed_in = local_settings.read_text()
    assert _read(staged).needs_sign_in is False

    local_settings.unlink()
    # Nothing was refused yet, so nothing says that a sign-in is wanted.
    assert _read(staged).needs_sign_in is False

    assert _start(staged)["outcome"] == "NEEDS_SIGN_IN"
    refused = _read(staged)

    assert refused.needs_sign_in is True
    # A refusal for want of an account is told as that, not as a start that failed.
    assert refused.start_failure is None

    local_settings.write_text(signed_in)
    assert _read(staged).needs_sign_in is False

    local_settings.unlink()
    assert _read(staged).needs_sign_in is True

    local_settings.write_text(signed_in)
    _start(staged)
    local_settings.unlink()
    # The start that went through took the refusal off the record.
    assert _read(staged, read_agents=_unreadable_agents).needs_sign_in is False


def test_an_arm_refused_for_want_of_an_account_is_told_the_same_way(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staged = stage_update_workspace(tmp_path, monkeypatch, is_signed_in=False)

    answer = arm_schedule_in(
        staged, RecordingFeed(None), now=_NOW, ceiling=_CEILING, window=(0, 60)
    )
    again = arm_schedule_in(
        staged, RecordingFeed(None), now=_NOW + 60.0, ceiling=_CEILING, window=(0, 60)
    )

    assert (answer["outcome"], again["outcome"]) == ("NEEDS_SIGN_IN", "NEEDS_SIGN_IN")
    assert _read(staged).needs_sign_in is True
    # The first of a row of refusals stands.
    assert staged.read_state("sign-in-refusal.json") == {
        "schema": 1,
        "refused_at": _NOW,
    }


def test_a_create_refused_for_want_of_an_account_needs_a_sign_in_and_is_no_failed_start(
    staged: StagedUpdateWorkspace,
) -> None:
    _fail_the_create(staged, "Connect an AI provider to start a chat")

    _start(staged)
    last_start = staged.read_state("last-start.json")
    (staged.root / ".mngr" / "settings.local.toml").unlink()
    state = _read(staged)

    assert last_start is not None
    assert last_start["outcome"] == "NEEDS_SIGN_IN"
    assert state.start_failure is None
    assert state.needs_sign_in is True


@pytest.mark.parametrize("skip_field", ["pending_skip_reason", "last_skip_reason"])
def test_a_window_skipped_for_want_of_an_account_needs_a_sign_in_until_one_is_there(
    staged: StagedUpdateWorkspace, skip_field: str
) -> None:
    staged.write_state(
        "schedule.json", armed_schedule(_NOW, **{skip_field: "NEEDS_SIGN_IN"})
    )

    assert _read(staged).needs_sign_in is False

    (staged.root / ".mngr" / "settings.local.toml").unlink()

    assert _read(staged).needs_sign_in is True


# The schedule

# The staged schedule's window is three hours from 09:00 UTC.
_BEFORE_THE_WINDOW = epoch_at("2026-10-20T07:30:00+00:00")
_INSIDE_THE_WINDOW = epoch_at("2026-10-20T10:15:00+00:00")
_AFTER_THE_WINDOW = epoch_at("2026-10-20T12:00:00+00:00")


@pytest.mark.parametrize(
    ("now", "opens_at"),
    [
        (_BEFORE_THE_WINDOW, "2026-10-20T09:00:00+00:00"),
        # Inside its window the schedule names the window that is open.
        (_INSIDE_THE_WINDOW, "2026-10-20T09:00:00+00:00"),
        # A window that closes at the moment asked is over.
        (_AFTER_THE_WINDOW, "2026-10-21T09:00:00+00:00"),
    ],
    ids=("before", "inside", "after"),
)
def test_a_schedule_names_the_window_open_now_or_the_next_to_open(
    staged: StagedUpdateWorkspace, now: float, opens_at: str
) -> None:
    staged.write_state(
        "schedule.json", armed_schedule(now - 3600.0, is_auto_armed=True)
    )

    state = _read(staged, now=now)

    assert state.schedule == UpdateSchedule(
        target_ref=_CEILING,
        override_ref="",
        is_auto_armed=True,
        window_opens_at=epoch_at(opens_at),
        window_closes_at=epoch_at(opens_at) + 3 * 3600.0,
        last_skip_reason=None,
    )
    assert UpdateAction.CANCEL_SCHEDULE in state.allowed_actions


def test_a_window_that_runs_past_midnight_is_open_on_the_day_after_it_opened(
    staged: StagedUpdateWorkspace,
) -> None:
    schedule = armed_schedule(
        _NOW, window_start_minute_utc=23 * 60, window_length_minutes=120
    )
    staged.write_state("schedule.json", schedule)

    state = _read(staged, now=epoch_at("2026-10-21T00:30:00+00:00"))

    assert state.schedule is not None
    assert state.schedule.window_opens_at == epoch_at("2026-10-20T23:00:00+00:00")
    assert state.schedule.window_closes_at == epoch_at("2026-10-21T01:00:00+00:00")


@pytest.mark.parametrize("reason", list(UpdateSkipReason))
def test_a_schedule_says_why_its_last_window_closed_without_a_start(
    staged: StagedUpdateWorkspace, reason: UpdateSkipReason
) -> None:
    staged.write_state(
        "schedule.json",
        armed_schedule(_NOW, last_skip_reason=reason.value, last_skip_at=_NOW - 60.0),
    )

    state = _read(staged)

    assert state.schedule is not None
    assert state.schedule.last_skip_reason is reason


@pytest.mark.parametrize("recorded", ["", "A_REASON_OF_A_LATER_TEMPLATE", None])
def test_a_schedule_with_no_skip_or_one_this_template_does_not_know_names_none(
    staged: StagedUpdateWorkspace, recorded: str | None
) -> None:
    staged.write_state("schedule.json", armed_schedule(_NOW, last_skip_reason=recorded))

    state = _read(staged)

    assert state.schedule is not None
    assert state.schedule.last_skip_reason is None


def test_a_schedule_at_a_version_the_user_named_goes_to_that_version(
    staged: StagedUpdateWorkspace,
) -> None:
    staged.write_state(
        "schedule.json", armed_schedule(_NOW, override_ref="minds-v0.8.7")
    )

    state = _read(staged)

    assert state.schedule is not None
    assert (
        state.schedule.target_ref,
        state.schedule.override_ref,
        state.schedule.is_auto_armed,
    ) == ("minds-v0.8.7", "minds-v0.8.7", False)


def test_a_schedule_file_with_no_window_is_armed_and_names_no_window(
    staged: StagedUpdateWorkspace,
) -> None:
    staged.write_state(
        "schedule.json", {"armed_at": _NOW, "ceiling": _CEILING, "override_ref": ""}
    )

    state = _read(staged)

    assert state.schedule is not None
    assert (state.schedule.window_opens_at, state.schedule.window_closes_at) == (
        None,
        None,
    )
    assert UpdateAction.CANCEL_SCHEDULE in state.allowed_actions


def test_a_workspace_with_no_cron_is_not_offered_a_schedule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staged = stage_update_workspace(tmp_path, monkeypatch, has_cron=False)

    assert _read(staged).allowed_actions == (UpdateAction.START,)


# The channel's version, the skip, and the workspace arming itself

_INSTALLED = "minds-v0.9.0"
_ON_THE_CHANNEL = "minds-v0.10.0"


def _follow_the_channel(staged: StagedUpdateWorkspace, **settings: Any) -> None:
    """Leave the workspace holding update settings and the version its channel was last read at."""
    document = update_settings_document(**settings)
    staged.write_state("settings.json", document)
    staged.write_state(
        "channel-version.json",
        {
            "schema": 1,
            "channel": document["channel"],
            "feed_base_url": document["feed_base_url"],
            "template_ref": _ON_THE_CHANNEL,
            "read_at": _NOW - 60.0,
            "checked_at": _NOW - 60.0,
            "error": "",
        },
    )


def test_the_read_answers_the_settings_the_channels_version_and_the_installed_one(
    staged: StagedUpdateWorkspace,
) -> None:
    _follow_the_channel(staged, is_auto_update_enabled_here=False)
    staged.write_backup_credentials()
    before = _state_files(staged)

    state = _read(staged, read_installed_ref=lambda: _INSTALLED)

    assert state.installed_version == _INSTALLED
    assert state.settings == update_settings_document(is_auto_update_enabled_here=False)
    assert state.channel_version is not None
    assert state.channel_version["template_ref"] == _ON_THE_CHANNEL
    assert state.is_backup_configured is True
    assert _state_files(staged) == before


def test_the_version_of_the_checkout_is_read_once_for_an_answer(
    staged: StagedUpdateWorkspace,
) -> None:
    _follow_the_channel(staged)
    asked: list[bool] = []

    def read_installed_ref() -> str:
        asked.append(True)
        return _INSTALLED

    state = _read(staged, read_installed_ref=read_installed_ref)

    assert state.will_self_arm is True
    assert asked == [True]


def test_a_skip_of_the_channels_version_is_told_and_a_skip_of_another_is_not(
    staged: StagedUpdateWorkspace,
) -> None:
    _follow_the_channel(staged)
    assert UpdateAction.SKIP in _read(staged).allowed_actions

    control.skip_target(target_ref="minds-v0.9.5", skipped_at=_NOW - 10.0)
    assert _read(staged).is_update_skipped is False

    control.skip_target(target_ref=_ON_THE_CHANNEL, skipped_at=_NOW)
    skipped = _read(staged)

    assert skipped.is_update_skipped is True
    assert UpdateAction.SKIP not in skipped.allowed_actions


def test_a_workspace_that_has_read_no_version_for_its_channel_has_nothing_to_skip(
    staged: StagedUpdateWorkspace,
) -> None:
    staged.write_state("settings.json", update_settings_document())

    state = _read(staged)

    assert state.channel_version is None
    assert (state.is_update_skipped, state.will_self_arm) == (False, False)
    assert UpdateAction.SKIP not in state.allowed_actions


@pytest.mark.parametrize(
    ("installed_ref", "settings", "will_self_arm"),
    [
        (_INSTALLED, {}, True),
        (_ON_THE_CHANNEL, {}, False),
        # A checkout that names no release is not known to be behind.
        ("", {}, False),
        (_INSTALLED, {"is_auto_update_enabled": False}, False),
        (_INSTALLED, {"is_auto_update_enabled_here": False}, False),
        (
            _INSTALLED,
            {"is_auto_update_enabled": False, "is_auto_update_enabled_here": True},
            True,
        ),
    ],
)
def test_the_workspace_says_whether_its_next_minute_arms_the_update_by_itself(
    staged: StagedUpdateWorkspace,
    installed_ref: str,
    settings: dict[str, Any],
    will_self_arm: bool,
) -> None:
    _follow_the_channel(staged, **settings)

    state = _read(staged, read_installed_ref=lambda: installed_ref)

    assert state.will_self_arm is will_self_arm


def test_nothing_is_armed_by_itself_over_a_skip_a_schedule_a_run_or_a_version_already_tried(
    staged: StagedUpdateWorkspace,
) -> None:
    _follow_the_channel(staged)

    def will_self_arm() -> bool:
        return _read(
            staged,
            read_agents=_unreadable_agents,
            read_installed_ref=lambda: _INSTALLED,
        ).will_self_arm

    assert will_self_arm() is True

    control.skip_target(target_ref=_ON_THE_CHANNEL, skipped_at=_NOW)
    assert will_self_arm() is False
    staged.state_path("skipped-target.json").unlink()

    staged.write_state("schedule.json", armed_schedule(_NOW))
    assert will_self_arm() is False
    staged.state_path("schedule.json").unlink()

    staged.write_state("run.json", _OPEN_RUN)
    assert will_self_arm() is False
    staged.state_path("run.json").unlink()

    staged.write_failed_run(_ON_THE_CHANNEL, _NOW - 7200.0)
    assert will_self_arm() is False


def test_a_workspace_with_no_cron_arms_nothing_by_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staged = stage_update_workspace(tmp_path, monkeypatch, has_cron=False)
    _follow_the_channel(staged)

    assert _read(staged, read_installed_ref=lambda: _INSTALLED).will_self_arm is False


# The observer's listing


def test_when_a_leads_turn_ended_is_read_from_the_observers_events(
    staged: StagedUpdateWorkspace,
) -> None:
    staged.write_state("run.json", _OPEN_RUN)
    lead = observed_agent(_LEAD_ID, _LEAD, "WAITING", chat_id=_CHAT_ID)
    idle_since = {
        "kind": "COMPLETED",
        "native_type": "",
        "ended_at": "2026-10-14T12:00:00.123456+00:00",
    }
    turn_ended_at = epoch_at("2026-10-14T12:00:00.123456+00:00")
    write_event_lines(
        staged.events_path,
        [
            full_state_line(
                [observed_services_agent(), {**lead, "state": "RUNNING"}],
                turn_ended_at - 120.0,
            ),
            agent_state_line({**lead, "turn_end": idle_since}, turn_ended_at),
        ],
    )

    def activity_at(now: float) -> UpdateActivity:
        return _read(
            staged,
            now=now,
            read_agents=lambda: read_observed_agents(str(staged.events_path), now),
            read_busy_agents=_nobody_is_busy,
        ).activity

    assert activity_at(turn_ended_at + 20.0) is UpdateActivity.RUNNING
    assert activity_at(turn_ended_at + 45.0) is UpdateActivity.WAITING
