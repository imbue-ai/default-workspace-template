"""What a workspace's update is doing, worked out by the workspace at the moment it is asked.

``read_update_state`` is the read of the shell's ``GET /api/workspace/update``, and what each of the update routes'
writes answers with. It is worked out from the workspace's own files, its own listing of its agents and its own
clock, so every client that asks is told the same thing, whichever of them started the run and however long ago
it opened. Nothing is written, and the feed is not read.

The decisions are the ones the dispatcher and the start of an update make for themselves (``is_run_in_flight``,
``pending_dispatched_chat``, ``decide_watchdog_action``, ``should_auto_arm``), so what a client is told is under way
is what a start would find under way.

What it leaves to a client is whether an update is on offer at all: that compares the installed version with the
channel's against release floors only a current client knows, so the read answers the two versions and the
settings, and no verdict on them.

The two things that can take long are asked only when the answer turns on them. The chats waiting on background
tasks are read (a program of up to a minute) only for a lead that has sat idle long enough to read as waiting,
and whether an account is signed in only while a refusal for want of one is on record.
"""

import functools
import os
from collections.abc import Callable, Mapping, Sequence
from enum import StrEnum
from typing import Final, NamedTuple, TypeVar

from workspace_update_control.agents import TURN_ENDED_AT_KEY
from workspace_update_control.arming import (
    armable_target,
    is_run_dismissed,
    read_arming_facts,
    shown_verdict,
)
from workspace_update_control.channel import channel_template_ref, read_channel_version
from workspace_update_control.control import (
    DISMISSED_NOTE_KEY,
    DISMISSED_OUTCOME_KEY,
    START_FAILURE_DISMISSED_KEY,
    AgentsReader,
    read_last_start,
    run_chat_id,
)
from workspace_update_control.dispatcher import (
    AgentEnvironment,
    BusyAgents,
    WatchdogAction,
    chat_id_of,
    decide_watchdog_action,
    epoch_or_none,
    find_chat,
    find_run_lead,
    is_cron_available,
    is_run_in_flight,
    next_window,
    pending_dispatched_chat,
    read_json_object,
    resolve_account,
    run_started_at,
    schedule_window,
    unrecorded_dispatch,
    watchdog_idle_ticks,
)
from workspace_update_control.protocol import (
    DISMISSALS_FILE_PATH,
    DISPATCHER_CRON_NAME,
    LAST_DISPATCH_FILE_PATH,
    OUTCOME_CHAT_APP_UNREACHABLE,
    OUTCOME_CREATE_FAILED,
    RUN_RECORD_PATH,
    SCHEDULE_FILE_PATH,
    SIGN_IN_REFUSAL_FILE_PATH,
    SKIP_DISPATCH_FAILED,
    SKIP_LEASE_HELD,
    SKIP_NEEDS_SIGN_IN,
    SKIP_UPDATE_IN_FLIGHT,
    SKIP_WINDOW_CLOSED,
    SKIPPED_TARGET_FILE_PATH,
    STARTED_BY_IMMEDIATE,
    STARTED_BY_SCHEDULED,
)
from workspace_update_control.settings import (
    is_backup_configured,
    read_update_settings,
)

# A listing can catch a lead between two turns, so a lead reads as waiting on the user only once its last turn
# ended this long ago: two of the twenty-second looks a client used to take before it said so.
_IDLE_SECONDS_BEFORE_WAITING: Final[float] = 40.0
_NOBODY_BUSY: Final[BusyAgents] = (frozenset(), frozenset(), frozenset())
_IDLE_WATCHDOG_ACTIONS: Final[tuple[WatchdogAction, ...]] = (
    WatchdogAction.COUNT_IDLE,
    WatchdogAction.REMIND,
)

_Member = TypeVar("_Member", bound=StrEnum)


class UpdateActivity(StrEnum):
    """What an update run is doing, as far as the workspace can tell."""

    # No run in flight.
    IDLE = "IDLE"
    # A start was accepted and the run's chat is still being created.
    STARTING = "STARTING"
    # The run's chat exists and is preparing; the live workspace is untouched.
    RUNNING = "RUNNING"
    # The run's lead is alive and idle: its record says it is holding for the user, or nothing but the user will
    # move it or its worker.
    WAITING = "WAITING"
    # The merge-and-reveal is landing.
    APPLYING = "APPLYING"
    # The apply has landed and the run's lead is finishing its checks before it records a verdict.
    FINISHING = "FINISHING"
    # The run has no verdict and nothing will carry it on: its chat is gone, or stopped where no watchdog restarts
    # it. Not final: a lead that comes back reads as under way again.
    STALLED = "STALLED"


class UpdateVerdict(StrEnum):
    """How an update run ended, as the update-self skill records it."""

    UPDATED = "UPDATED"
    UPDATED_WITH_REBUILD_ITEMS = "UPDATED_WITH_REBUILD_ITEMS"
    ALREADY_CURRENT = "ALREADY_CURRENT"
    NEEDS_RECREATION = "NEEDS_RECREATION"
    STUCK = "STUCK"
    REFUSED = "REFUSED"
    NEEDS_DECISION = "NEEDS_DECISION"


class UpdateStartedBy(StrEnum):
    """How an update run was started."""

    SCHEDULED = "SCHEDULED"
    IMMEDIATE = "IMMEDIATE"


class UpdateStartFailureCode(StrEnum):
    """Why a start that was accepted made no chat."""

    CREATE_FAILED = OUTCOME_CREATE_FAILED
    CHAT_APP_UNREACHABLE = OUTCOME_CHAT_APP_UNREACHABLE


class UpdateSkipReason(StrEnum):
    """Why an update window closed without its scheduled run starting."""

    UPDATE_IN_FLIGHT = SKIP_UPDATE_IN_FLIGHT
    LEASE_HELD = SKIP_LEASE_HELD
    NEEDS_SIGN_IN = SKIP_NEEDS_SIGN_IN
    DISPATCH_FAILED = SKIP_DISPATCH_FAILED
    WINDOW_CLOSED = SKIP_WINDOW_CLOSED


class UpdateAction(StrEnum):
    """Something a client may ask of the workspace's updates as they stand."""

    START = "START"
    SCHEDULE = "SCHEDULE"
    CANCEL_SCHEDULE = "CANCEL_SCHEDULE"
    SKIP = "SKIP"
    DISMISS_OUTCOME = "DISMISS_OUTCOME"
    DISMISS_NOTE = "DISMISS_NOTE"


_IN_FLIGHT_ACTIVITIES: Final[frozenset[UpdateActivity]] = frozenset(
    {
        UpdateActivity.STARTING,
        UpdateActivity.RUNNING,
        UpdateActivity.WAITING,
        UpdateActivity.APPLYING,
        UpdateActivity.FINISHING,
    }
)
# The verdicts after which the workspace serves the new version.
_LANDED_VERDICTS: Final[tuple[str, ...]] = (
    UpdateVerdict.UPDATED,
    UpdateVerdict.UPDATED_WITH_REBUILD_ITEMS,
)
_STARTER_BY_RECORDED_WORD: Final[Mapping[str, UpdateStartedBy]] = {
    STARTED_BY_SCHEDULED: UpdateStartedBy.SCHEDULED,
    STARTED_BY_IMMEDIATE: UpdateStartedBy.IMMEDIATE,
}


class UpdateRun(NamedTuple):
    """The run a client shows: the one under way, or the last one while its outcome or its note is not dismissed."""

    # The run's start as its record holds it, which is the name a dismissal gives the run: for a chat that has
    # recorded no run yet, when the chat was made. None while the chat is still being created, and for a record
    # with no readable start, which nothing can dismiss.
    started_at: float | None
    chat_agent_name: str
    # '' when it is unknown, and when the chat is gone.
    chat_id: str
    is_chat_gone: bool
    started_by: UpdateStartedBy | None
    # The version the run aims at; '' for a run nothing here started, whose aim only its chat knows.
    target_ref: str
    # The version the user named for it; '' when it aims at the channel's.
    override_ref: str
    # Whether the run waits on a hold its lead recorded, which ``hold_detail`` says in a line.
    is_holding: bool
    hold_detail: str
    # None while the run goes, once its outcome is dismissed, and for a verdict this template does not know.
    verdict: UpdateVerdict | None
    verdict_detail: str
    in_place_compatible_ref: str
    # The version a landed run left the workspace on, until that note is dismissed; '' otherwise.
    updated_to: str


class UpdateStartFailure(NamedTuple):
    """The last accepted start that made no chat, until it is dismissed or an update starts."""

    attempt_id: str
    code: UpdateStartFailureCode
    detail: str


class UpdateSchedule(NamedTuple):
    """The armed schedule."""

    # The version the scheduled update goes to.
    target_ref: str
    # The version the user named for it; '' when it follows the channel's.
    override_ref: str
    is_auto_armed: bool
    # The window the update may start in: the one open now, else the next to open. None for a schedule file
    # that holds no window, which starts nothing.
    window_opens_at: float | None
    window_closes_at: float | None
    # None when the last window did not close without a start, and for a reason this template does not know.
    last_skip_reason: UpdateSkipReason | None


class UpdateState(NamedTuple):
    """Everything the workspace answers about its updates."""

    # When this was worked out, by the workspace's clock: of two answers, the later is the newer.
    as_of: float
    # The version of the checkout; '' when it names none.
    installed_version: str
    settings: dict[str, object] | None
    channel_version: dict[str, object] | None
    activity: UpdateActivity
    run: UpdateRun | None
    start_failure: UpdateStartFailure | None
    schedule: UpdateSchedule | None
    needs_sign_in: bool
    # Whether the user skipped the version the channel is on.
    is_update_skipped: bool
    # Whether the dispatcher's next minute arms this workspace's update by itself.
    will_self_arm: bool
    is_backup_configured: bool
    allowed_actions: tuple[UpdateAction, ...]
    # The run's own record and the last start attempt, as they are on disk.
    run_record: dict[str, object] | None
    last_start: dict[str, object] | None

    def document(self) -> dict[str, object]:
        """The state as plain JSON values."""
        return {
            **self._asdict(),
            "run": self.run._asdict() if self.run is not None else None,
            "start_failure": self.start_failure._asdict()
            if self.start_failure is not None
            else None,
            "schedule": self.schedule._asdict() if self.schedule is not None else None,
            "allowed_actions": list(self.allowed_actions),
        }


class _RunView(NamedTuple):
    activity: UpdateActivity
    run: UpdateRun | None


_NO_RUN: Final[_RunView] = _RunView(UpdateActivity.IDLE, None)


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _member(enum_type: type[_Member], value: object) -> _Member | None:
    """The member of ``enum_type`` that ``value`` spells; None for anything else, a word of a later template
    among them."""
    if not isinstance(value, str):
        return None
    try:
        return enum_type(value)
    except ValueError:
        return None


def _dispatch_of_run(
    dispatch: Mapping[str, object] | None, run: Mapping[str, object]
) -> Mapping[str, object]:
    """The last dispatch when it is the one that made this run's chat; nothing otherwise.

    A lead records its run after its chat is made, so a dispatch later than the run's start made a later chat
    under the same name.
    """
    if dispatch is None or dispatch.get("chat_agent_name") != run.get(
        "chat_agent_name"
    ):
        return {}
    started_at = run_started_at(run)
    dispatched_at = epoch_or_none(dispatch.get("dispatched_at"))
    is_later_chat = (
        started_at is not None
        and dispatched_at is not None
        and dispatched_at > started_at
    )
    return {} if is_later_chat else dispatch


def _target_of(
    dispatch: Mapping[str, object], last_start: Mapping[str, object] | None
) -> tuple[str, str]:
    """``(the version the dispatched run aims at, the version the user named for it)``."""
    target_ref = _text(dispatch.get("target_ref"))
    # A start's chat is dispatched at the moment the start was asked for, which is how its attempt is told.
    if (
        last_start is not None
        and "dispatched_at" in dispatch
        and dispatch.get("dispatched_at") == last_start.get("started_at")
        and isinstance(last_start.get("override_ref"), str)
    ):
        return target_ref, _text(last_start.get("override_ref"))
    # A scheduled run's dispatch does not say what the user named: a target other than the ceiling was.
    ceiling = _text(dispatch.get("ceiling"))
    return target_ref, target_ref if ceiling and target_ref != ceiling else ""


def _is_idle(
    run: Mapping[str, object],
    agents: Sequence[Mapping[str, str]],
    busy: BusyAgents,
    now: float,
) -> bool:
    """Whether the watchdog would count the run's lead and worker as idle."""
    return decide_watchdog_action(run, agents, busy, {}, now) in _IDLE_WATCHDOG_ACTIONS


def _has_been_idle_long_enough(
    run: Mapping[str, object], lead: Mapping[str, str] | None, now: float
) -> bool:
    """Whether the lead's last turn ended long enough ago to say that it waits on the user.

    Where the listing does not say when the turn ended (a harness that records none), the watchdog's own count
    stands in: a minute of it that found the run idle, which only a workspace with cron has.
    """
    try:
        turn_ended_at = float((lead or {}).get(TURN_ENDED_AT_KEY) or "")
    except ValueError:
        return watchdog_idle_ticks(run) >= 1
    return now - turn_ended_at >= _IDLE_SECONDS_BEFORE_WAITING


def _is_waiting_on_the_user(
    run: Mapping[str, object],
    agents: Sequence[Mapping[str, str]],
    read_busy_agents: Callable[[], BusyAgents],
    now: float,
) -> bool:
    """Whether nothing but the user will move the run: its lead and worker are idle, have been for long enough,
    and no background task is about to start a turn of theirs."""
    if not _is_idle(run, agents, _NOBODY_BUSY, now):
        return False
    if not _has_been_idle_long_enough(run, find_run_lead(agents, run), now):
        return False
    return _is_idle(run, agents, read_busy_agents(), now)


def _open_run_activity(
    run: Mapping[str, object],
    agents: Sequence[Mapping[str, str]] | None,
    is_watchdog_installed: bool,
    read_busy_agents: Callable[[], BusyAgents],
    now: float,
) -> UpdateActivity:
    """What a recorded run with no verdict is doing; agents that could not be read count as there and working."""
    if run.get("apply_phase"):
        return UpdateActivity.APPLYING
    if not is_run_in_flight(run, agents, is_stopped_lead_revived=is_watchdog_installed):
        return UpdateActivity.STALLED
    if run.get("is_holding"):
        return UpdateActivity.WAITING
    if agents is not None and _is_waiting_on_the_user(
        run, agents, read_busy_agents, now
    ):
        return UpdateActivity.WAITING
    is_landed = epoch_or_none(run.get("applied_at")) is not None
    return UpdateActivity.FINISHING if is_landed else UpdateActivity.RUNNING


def _starting_view(last_start: Mapping[str, object]) -> _RunView:
    return _RunView(
        UpdateActivity.STARTING,
        UpdateRun(
            started_at=None,
            chat_agent_name="",
            chat_id="",
            is_chat_gone=False,
            started_by=UpdateStartedBy.IMMEDIATE,
            target_ref=_text(last_start.get("target_ref")),
            override_ref=_text(last_start.get("override_ref")),
            is_holding=False,
            hold_detail="",
            verdict=None,
            verdict_detail="",
            in_place_compatible_ref="",
            updated_to="",
        ),
    )


def _unrecorded_dispatch_view(
    unrecorded: tuple[str, str, float],
    run: Mapping[str, object] | None,
    dispatch: Mapping[str, object],
    last_start: Mapping[str, object] | None,
    dismissals: Mapping[str, object] | None,
    agents: Sequence[Mapping[str, str]] | None,
    read_busy_agents: Callable[[], BusyAgents],
    now: float,
) -> _RunView | None:
    """The run of a chat made moments ago that has put no run on the record; None once the user dismissed a chat
    that ended without one."""
    name, dispatched_chat_id, dispatched_at = unrecorded
    lead = find_chat(agents, name, dispatched_chat_id) if agents is not None else None
    if pending_dispatched_chat(run, agents, now) is not None:
        is_waiting = agents is not None and _is_waiting_on_the_user(
            {"chat_agent_name": name}, agents, read_busy_agents, now
        )
        activity = UpdateActivity.WAITING if is_waiting else UpdateActivity.RUNNING
        chat_id = chat_id_of(lead) if lead is not None else dispatched_chat_id
    elif is_run_dismissed(dismissals, DISMISSED_OUTCOME_KEY, dispatched_at):
        return None
    else:
        activity = UpdateActivity.STALLED
        chat_id = chat_id_of(lead) if lead is not None else ""
    target_ref, override_ref = _target_of(dispatch, last_start)
    return _RunView(
        activity,
        UpdateRun(
            started_at=dispatched_at,
            chat_agent_name=name,
            chat_id=chat_id,
            is_chat_gone=activity is UpdateActivity.STALLED and lead is None,
            started_by=_STARTER_BY_RECORDED_WORD.get(
                _text(dispatch.get("started_by")).lower()
            ),
            target_ref=target_ref,
            override_ref=override_ref,
            is_holding=False,
            hold_detail="",
            verdict=None,
            verdict_detail="",
            in_place_compatible_ref="",
            updated_to="",
        ),
    )


def _recorded_run_view(
    run: Mapping[str, object] | None,
    dispatch: Mapping[str, object] | None,
    last_start: Mapping[str, object] | None,
    dismissals: Mapping[str, object] | None,
    # Asked only when the answer turns on the workspace's agents.
    read_agents: AgentsReader,
    is_watchdog_installed: bool,
    read_busy_agents: Callable[[], BusyAgents],
    now: float,
) -> _RunView:
    """The run the record names: under way, stalled, or over with an outcome or a note the user has not put away."""
    if run is None:
        return _NO_RUN
    started_at = run_started_at(run)
    dispatch_of_run = _dispatch_of_run(dispatch, run)
    dispatched_chat_id = _text(dispatch_of_run.get("chat_id"))
    if run.get("verdict") is None:
        # A record with no chat name names no agent anything could be read of.
        if not run.get("chat_agent_name"):
            return _NO_RUN
        agents = read_agents()
        activity = _open_run_activity(
            run, agents, is_watchdog_installed, read_busy_agents, now
        )
        if activity is UpdateActivity.STALLED and is_run_dismissed(
            dismissals, DISMISSED_OUTCOME_KEY, started_at
        ):
            return _NO_RUN
        lead = find_run_lead(agents, run) if agents is not None else None
        is_chat_gone = agents is not None and lead is None
        chat_id = chat_id_of(lead) if lead is not None else dispatched_chat_id
        verdict = None
        updated_to = ""
    else:
        activity = UpdateActivity.IDLE
        verdict = _member(UpdateVerdict, shown_verdict(run, dismissals))
        is_note_showing = run.get(
            "verdict"
        ) in _LANDED_VERDICTS and not is_run_dismissed(
            dismissals, DISMISSED_NOTE_KEY, started_at
        )
        updated_to = _text(run.get("resulting_ref")) if is_note_showing else ""
        if verdict is None and not updated_to:
            return _NO_RUN
        looked_up_chat_id = run_chat_id(run, dispatch, read_agents)
        is_chat_gone = looked_up_chat_id == ""
        chat_id = dispatched_chat_id if looked_up_chat_id is None else looked_up_chat_id
    is_holding = activity is UpdateActivity.WAITING and bool(run.get("is_holding"))
    started_by = _text(run.get("started_by")) or _text(
        dispatch_of_run.get("started_by")
    )
    target_ref, override_ref = _target_of(dispatch_of_run, last_start)
    return _RunView(
        activity,
        UpdateRun(
            started_at=started_at,
            chat_agent_name=_text(run.get("chat_agent_name")),
            chat_id="" if is_chat_gone else chat_id,
            is_chat_gone=is_chat_gone,
            started_by=_STARTER_BY_RECORDED_WORD.get(started_by.lower()),
            target_ref=target_ref,
            override_ref=override_ref,
            is_holding=is_holding,
            hold_detail=_text(run.get("hold_detail")) if is_holding else "",
            verdict=verdict,
            verdict_detail=_text(run.get("detail")) if verdict is not None else "",
            in_place_compatible_ref=_text(run.get("in_place_compatible_ref"))
            if verdict is not None
            else "",
            updated_to=updated_to,
        ),
    )


def _run_view(
    run: Mapping[str, object] | None,
    dispatch: Mapping[str, object] | None,
    last_start: Mapping[str, object] | None,
    dismissals: Mapping[str, object] | None,
    read_agents: AgentsReader,
    is_watchdog_installed: bool,
    read_busy_agents: Callable[[], BusyAgents],
    now: float,
) -> _RunView:
    """The newest sign of a run decides: a start still creating its chat, then a chat made moments ago that has
    recorded no run, then the run on the record."""
    unrecorded = unrecorded_dispatch(run, now)
    if last_start is not None and last_start.get("outcome") is None:
        attempted_at = epoch_or_none(last_start.get("started_at"))
        is_chat_made_since = unrecorded is not None and (
            attempted_at is None or unrecorded[2] >= attempted_at
        )
        if not is_chat_made_since:
            return _starting_view(last_start)
    if unrecorded is not None and dispatch is not None:
        view = _unrecorded_dispatch_view(
            unrecorded,
            run,
            dispatch,
            last_start,
            dismissals,
            read_agents(),
            read_busy_agents,
            now,
        )
        if view is not None:
            return view
    return _recorded_run_view(
        run,
        dispatch,
        last_start,
        dismissals,
        read_agents,
        is_watchdog_installed,
        read_busy_agents,
        now,
    )


def _start_failure(
    last_start: Mapping[str, object] | None,
    run: Mapping[str, object] | None,
    dispatch: Mapping[str, object] | None,
) -> UpdateStartFailure | None:
    """The last start attempt when it made no chat, the user has not put that away, and no chat has been made
    and no run begun since.

    A start that found no account signed in is told as the workspace needing a sign-in, not as a failure.
    """
    if last_start is None or last_start.get(START_FAILURE_DISMISSED_KEY) is True:
        return None
    code = _member(UpdateStartFailureCode, last_start.get("outcome"))
    if code is None:
        return None
    attempted_at = epoch_or_none(last_start.get("started_at"))
    later_signs = (
        run_started_at(run),
        epoch_or_none((dispatch or {}).get("dispatched_at")),
    )
    if attempted_at is not None and any(
        sign is not None and sign >= attempted_at for sign in later_signs
    ):
        return None
    return UpdateStartFailure(
        attempt_id=_text(last_start.get("attempt_id")),
        code=code,
        detail=_text(last_start.get("detail")),
    )


def _schedule_state(
    schedule: Mapping[str, object] | None, now: float
) -> UpdateSchedule | None:
    if schedule is None:
        return None
    window = schedule_window(schedule)
    opens_at, closes_at = (
        next_window(now, *window) if window is not None else (None, None)
    )
    override_ref = _text(schedule.get("override_ref"))
    return UpdateSchedule(
        target_ref=override_ref or _text(schedule.get("ceiling")),
        override_ref=override_ref,
        is_auto_armed=schedule.get("is_auto_armed") is True,
        window_opens_at=opens_at,
        window_closes_at=closes_at,
        last_skip_reason=_member(UpdateSkipReason, schedule.get("last_skip_reason")),
    )


def _needs_sign_in(
    schedule: Mapping[str, object] | None, agent_environment: AgentEnvironment
) -> bool:
    """Whether a start, an arm or a scheduled dispatch found no AI account signed in, and none is signed in now."""
    is_refusal_on_record = read_json_object(SIGN_IN_REFUSAL_FILE_PATH) is not None or (
        schedule is not None
        and SKIP_NEEDS_SIGN_IN
        in (schedule.get("pending_skip_reason"), schedule.get("last_skip_reason"))
    )
    return is_refusal_on_record and not resolve_account(agent_environment)[0]


def _allowed_actions(
    view: _RunView,
    start_failure: UpdateStartFailure | None,
    is_scheduled: bool,
    is_skippable: bool,
    is_cron_there: bool,
) -> tuple[UpdateAction, ...]:
    activity, run = view
    # A fresh run would set aside the work a pending decision is about.
    is_run_startable = activity not in _IN_FLIGHT_ACTIVITIES and (
        run is None or run.verdict is not UpdateVerdict.NEEDS_DECISION
    )
    is_run_named = run is not None and run.started_at is not None
    is_outcome_showing = run is not None and (
        run.verdict is not None or activity is UpdateActivity.STALLED
    )
    is_allowed_by_action = {
        UpdateAction.START: is_run_startable,
        # A schedule armed during a run outlives it, and would start a second one.
        UpdateAction.SCHEDULE: is_run_startable and is_cron_there,
        UpdateAction.CANCEL_SCHEDULE: is_scheduled,
        UpdateAction.SKIP: is_skippable,
        UpdateAction.DISMISS_OUTCOME: start_failure is not None
        or (is_run_named and is_outcome_showing),
        UpdateAction.DISMISS_NOTE: is_run_named
        and run is not None
        and bool(run.updated_to),
    }
    return tuple(
        action for action, is_allowed in is_allowed_by_action.items() if is_allowed
    )


def read_update_state(
    *,
    now: float,
    proc_root: str,
    live_cron_dir: str,
    agent_environment: AgentEnvironment,
    read_agents: AgentsReader,
    # The chats waiting on background tasks, which can take a program of up to a minute to read.
    read_busy_agents: Callable[[], BusyAgents],
    # The version of the checkout, read from git; '' when it names none.
    read_installed_ref: Callable[[], str],
) -> UpdateState:
    """What the workspace's update is doing at ``now``, with what a client compares to tell whether one is on
    offer. Each of the three readers is asked once at most, and only when the answer turns on it, but for the
    version of the checkout, which is part of every answer."""
    run = read_json_object(RUN_RECORD_PATH)
    dispatch = read_json_object(LAST_DISPATCH_FILE_PATH)
    last_start = read_last_start(proc_root)
    schedule = read_json_object(SCHEDULE_FILE_PATH)
    settings = read_update_settings()
    channel_version = read_channel_version(settings)
    installed_ref = functools.cache(read_installed_ref)
    is_cron_there = is_cron_available(live_cron_dir)
    view = _run_view(
        run,
        dispatch,
        last_start,
        read_json_object(DISMISSALS_FILE_PATH),
        functools.cache(read_agents),
        os.path.isfile(os.path.join(live_cron_dir, DISPATCHER_CRON_NAME)),
        read_busy_agents,
        now,
    )
    start_failure = _start_failure(last_start, run, dispatch)
    channel_ref = channel_template_ref(channel_version)
    skipped = read_json_object(SKIPPED_TARGET_FILE_PATH) or {}
    is_update_skipped = bool(channel_ref) and skipped.get("target_ref") == channel_ref
    will_self_arm = (
        settings is not None
        and is_cron_there
        and armable_target(
            read_arming_facts(settings.is_arming_enabled, now),
            channel_version,
            now,
            installed_ref,
        )
        is not None
    )
    return UpdateState(
        as_of=now,
        installed_version=installed_ref(),
        settings=settings.document() if settings is not None else None,
        channel_version=channel_version,
        activity=view.activity,
        run=view.run,
        start_failure=start_failure,
        schedule=_schedule_state(schedule, now),
        needs_sign_in=_needs_sign_in(schedule, agent_environment),
        is_update_skipped=is_update_skipped,
        will_self_arm=will_self_arm,
        is_backup_configured=is_backup_configured(),
        allowed_actions=_allowed_actions(
            view,
            start_failure,
            schedule is not None,
            bool(channel_ref) and not is_update_skipped,
            is_cron_there,
        ),
        run_record=run,
        last_start=last_start,
    )
