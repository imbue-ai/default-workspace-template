"""Turning definitions, state and events into what the list, the detail page and the command show."""

from collections.abc import Sequence
from datetime import datetime
from datetime import tzinfo
from typing import Final
from typing import assert_never

from imbue.imbue_common.event_envelope import parse_iso_timestamp
from imbue.imbue_common.pure import pure
from routines.data_types import ListingSummary
from routines.data_types import LoadedRoutine
from routines.data_types import RoutineListing
from routines.data_types import RoutineRow
from routines.data_types import RoutineState
from routines.data_types import RunBlockedEvent
from routines.data_types import RunEvent
from routines.data_types import RunFinishedEvent
from routines.data_types import RunRecord
from routines.data_types import RunStartedEvent
from routines.primitives import RoutineHealth
from routines.primitives import RoutineId
from routines.primitives import RunId
from routines.primitives import RunOutcome
from routines.primitives import RunTrigger
from routines.schedule import describe_age
from routines.schedule import describe_duration
from routines.schedule import describe_moment
from routines.schedule import describe_schedule
from routines.schedule import is_overdue
from routines.schedule import is_switched_on
from routines.schedule import next_due

_HEALTH_RANK: Final[dict[RoutineHealth, int]] = {
    RoutineHealth.NEEDS_USER: 0,
    RoutineHealth.FAILING: 1,
    RoutineHealth.OVERDUE: 2,
    RoutineHealth.BROKEN: 3,
    RoutineHealth.NOT_RUN_YET: 4,
    RoutineHealth.WORKING: 5,
    RoutineHealth.DONE: 6,
    RoutineHealth.OFF: 7,
}
ATTENTION_HEALTHS: Final[frozenset[RoutineHealth]] = frozenset(
    (
        RoutineHealth.NEEDS_USER,
        RoutineHealth.FAILING,
        RoutineHealth.OVERDUE,
        RoutineHealth.BROKEN,
    )
)
DUE_NOW_TEXT: Final[str] = "any minute"


@pure
def health_text(health: RoutineHealth) -> str:
    match health:
        case RoutineHealth.BROKEN:
            return "Broken"
        case RoutineHealth.OFF:
            return "Off"
        case RoutineHealth.DONE:
            return "Done"
        case RoutineHealth.NEEDS_USER:
            return "Needs you"
        case RoutineHealth.FAILING:
            return "Failing"
        case RoutineHealth.OVERDUE:
            return "Overdue"
        case RoutineHealth.NOT_RUN_YET:
            return "Not run yet"
        case RoutineHealth.WORKING:
            return "Working"
        case _ as unreachable:
            assert_never(unreachable)


@pure
def outcome_text(outcome: RunOutcome | None, is_blocked: bool) -> str:
    if outcome is None:
        return "Waiting for you" if is_blocked else "Running"
    match outcome:
        case RunOutcome.SUCCEEDED:
            return "Worked"
        case RunOutcome.FAILED:
            return "Failed"
        case RunOutcome.TIMED_OUT:
            return "Took too long"
        case RunOutcome.INTERRUPTED:
            return "Stopped early"
        case _ as unreachable:
            assert_never(unreachable)


@pure
def trigger_text(trigger: RunTrigger) -> str:
    match trigger:
        case RunTrigger.SCHEDULED:
            return "automatic"
        case RunTrigger.MANUAL:
            return "manual"
        case _ as unreachable:
            assert_never(unreachable)


@pure
def health_of(loaded: LoadedRoutine, state: RoutineState, due: datetime | None, now: datetime) -> RoutineHealth:
    """The first rule that holds, in the order the enum lists them."""
    if loaded.definition is None:
        return RoutineHealth.BROKEN
    if not is_switched_on(loaded, state):
        return RoutineHealth.OFF
    if loaded.definition.once is not None and state.last_succeeded_at is not None:
        return RoutineHealth.DONE
    if state.current_run is not None and state.current_run.is_blocked:
        return RoutineHealth.NEEDS_USER
    if state.consecutive_failures > 0:
        return RoutineHealth.FAILING
    if state.current_run is None and is_overdue(loaded.definition, due, now):
        return RoutineHealth.OVERDUE
    if state.last_run is None:
        return RoutineHealth.NOT_RUN_YET
    return RoutineHealth.WORKING


@pure
def health_detail(health: RoutineHealth, loaded: LoadedRoutine, state: RoutineState, now: datetime) -> str:
    match health:
        case RoutineHealth.BROKEN:
            return loaded.error or "The definition does not validate."
        case RoutineHealth.OFF:
            return "Turned off; nothing will run."
        case RoutineHealth.DONE:
            return f"Ran {describe_age(state.last_succeeded_at, now)}." if state.last_succeeded_at else "Ran once."
        case RoutineHealth.NEEDS_USER:
            return "The agent is waiting for an answer from you."
        case RoutineHealth.FAILING:
            count = int(state.consecutive_failures)
            tries = "1 failed try" if count == 1 else f"{count} failed tries in a row"
            worked = (
                f"Last worked {describe_age(state.last_succeeded_at, now)}"
                if state.last_succeeded_at
                else "Never worked yet"
            )
            return f"{worked}; {tries}."
        case RoutineHealth.OVERDUE:
            return "Should have run by now."
        case RoutineHealth.NOT_RUN_YET:
            return "Waiting for its first run."
        case RoutineHealth.WORKING:
            return ""
        case _ as unreachable:
            assert_never(unreachable)


@pure
def last_run_record(state: RoutineState, routine_id: RoutineId, now: datetime) -> RunRecord | None:
    if state.last_run is None:
        return None
    last = state.last_run
    duration = (last.finished_at - last.started_at).total_seconds()
    return RunRecord(
        run_id=last.run_id,
        routine_id=routine_id,
        trigger=last.trigger,
        kind=last.kind,
        outcome=last.outcome,
        started_at=last.started_at,
        finished_at=last.finished_at,
        duration_seconds=duration,
        summary=last.summary,
        chat_id=state.chat_id,
        warnings=last.warnings,
        is_blocked=False,
        exit_code=None,
        when_text=describe_age(last.started_at, now),
        duration_text=describe_duration(duration),
        outcome_text=outcome_text(last.outcome, False),
    )


@pure
def build_row(loaded: LoadedRoutine, state: RoutineState, now: datetime, zone: tzinfo) -> RoutineRow:
    definition = loaded.definition
    due = next_due(definition, state, now, zone) if definition is not None else None
    health = health_of(loaded, state, due, now)
    is_running = state.current_run is not None
    is_enabled = is_switched_on(loaded, state)
    if not is_enabled or due is None or definition is None:
        next_run_text = ""
    elif is_running:
        next_run_text = "running now"
    elif due <= now:
        next_run_text = DUE_NOW_TEXT
    else:
        next_run_text = describe_moment(due, now, zone)
    last_run = last_run_record(state, loaded.routine_id, now)
    if last_run is None:
        last_run_text = "Never run"
    else:
        last_run_text = f"{last_run.outcome_text} {last_run.when_text}, {trigger_text(last_run.trigger)}"
    return RoutineRow(
        routine_id=loaded.routine_id,
        title=str(definition.title) if definition is not None else loaded.routine_id.as_title,
        what=str(definition.what) if definition is not None else "",
        health=health,
        health_text=health_text(health),
        health_detail=health_detail(health, loaded, state, now),
        is_running=is_running,
        is_enabled=is_enabled,
        kind=definition.kind if definition is not None else None,
        app=str(definition.app) if definition is not None and definition.app is not None else None,
        schedule_text=describe_schedule(definition, zone) if definition is not None else "",
        next_due_at=due,
        next_run_text=next_run_text,
        last_run=last_run,
        last_run_text=last_run_text,
        error=loaded.error,
        skill=str(definition.skill) if definition is not None and definition.skill is not None else None,
        command=definition.command if definition is not None and definition.command is not None else (),
        chat_id=state.chat_id,
    )


@pure
def sort_rows(rows: Sequence[RoutineRow]) -> list[RoutineRow]:
    return sorted(rows, key=lambda row: (_HEALTH_RANK[row.health], row.title.lower()))


@pure
def build_listing(
    rows: Sequence[RoutineRow], app_names: dict[str, str], now: datetime, zone: tzinfo
) -> RoutineListing:
    ordered = sort_rows(rows)
    upcoming = [row.next_due_at for row in ordered if row.next_due_at is not None and row.is_enabled]
    if not upcoming:
        next_run_text = "nothing scheduled"
    else:
        soonest = min(upcoming)
        next_run_text = DUE_NOW_TEXT if soonest <= now else describe_moment(soonest, now, zone)
    summary = ListingSummary(
        total=len(ordered),
        needs_attention=sum(1 for row in ordered if row.health in ATTENTION_HEALTHS),
        running=sum(1 for row in ordered if row.is_running),
        next_run_text=next_run_text,
    )
    return RoutineListing(routines=tuple(ordered), summary=summary, app_names=app_names, generated_at=now)


@pure
def fold_run_events(events: Sequence[RunEvent], now: datetime) -> list[RunRecord]:
    """One record per run the events mention a start of, newest first."""
    started_by_run: dict[RunId, RunStartedEvent] = {}
    finished_by_run: dict[RunId, RunFinishedEvent] = {}
    blocked_runs: set[RunId] = set()
    order: list[RunId] = []
    for event in events:
        if isinstance(event, RunStartedEvent):
            started_by_run[event.run_id] = event
            if event.run_id not in order:
                order.append(event.run_id)
        elif isinstance(event, RunFinishedEvent):
            finished_by_run[event.run_id] = event
        elif isinstance(event, RunBlockedEvent):
            blocked_runs.add(event.run_id)
        else:
            # The ledger parses only these three kinds.
            pass
    records: list[RunRecord] = []
    for run_id in order:
        started = started_by_run[run_id]
        records.append(_record_of(started, finished_by_run.get(run_id), run_id in blocked_runs, now))
    records.sort(key=lambda record: record.started_at, reverse=True)
    return records


@pure
def _parse_timestamp(event: RunEvent) -> datetime:
    parsed = parse_iso_timestamp(event.timestamp)
    assert parsed is not None
    return parsed


@pure
def _record_of(
    started: RunStartedEvent,
    finished: RunFinishedEvent | None,
    is_blocked: bool,
    now: datetime,
) -> RunRecord:
    started_at = _parse_timestamp(started)
    finished_at = _parse_timestamp(finished) if finished is not None else None
    duration = finished.duration_seconds if finished is not None else None
    outcome = finished.outcome if finished is not None else None
    is_still_blocked = is_blocked and finished is None
    return RunRecord(
        run_id=started.run_id,
        routine_id=started.routine_id,
        trigger=started.trigger,
        kind=started.kind,
        outcome=outcome,
        started_at=started_at,
        finished_at=finished_at,
        duration_seconds=duration,
        summary=finished.summary if finished is not None else "",
        chat_id=finished.chat_id if finished is not None else None,
        warnings=finished.warnings if finished is not None else (),
        is_blocked=is_still_blocked,
        exit_code=finished.exit_code if finished is not None else None,
        when_text=describe_age(started_at, now),
        duration_text=describe_duration(duration) if duration is not None else "",
        outcome_text=outcome_text(outcome, is_still_blocked),
    )
