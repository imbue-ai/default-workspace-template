"""What a client asks of a workspace's updates: start one, schedule one, skip a version, dismiss a notice, read it all.

Each function is one operation of the workspace's update routes (the shell's ``/api/workspace/update``).
Nothing here reads anything a client keeps. A client may name the values an update runs with: the newest version
it knows (the ceiling), the update window already converted to UTC, and whether it holds backup credentials. A
value it leaves out is the workspace's own: the version its release channel is on (``channel``), the window of
its update settings (``settings``), and whether it holds backup credentials itself. A schedule armed with such
values keeps them current (``refresh_followed_schedule``, which the dispatcher runs every minute); one armed
with a client's values is that client's to refresh.

They are the modes of the update program the Imbue Studio desktop app ships into a workspace
(``workspace_programs/update_workspace.py`` in the mngr repository), with three differences: nothing is copied
into the workspace, since the dispatcher is this tree's own code; reading changes nothing, since the refresh, the
skip and the dismissals each have an operation of their own; and starting an update answers at once, with the
create of the update's chat recorded in ``last-start.json`` when it ends.

Like the dispatcher, they act on the workspace whose root is the current directory.
"""

import fcntl
import os
import uuid
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from typing import Final

from workspace_update_control.channel import (
    ChannelFileFetcher,
    bounded_by_client_ceiling,
    channel_template_ref,
    is_channel_version_current,
    read_channel_version,
    read_fresh_client_ceiling,
    record_client_ceiling,
    refresh_channel_version,
)
from workspace_update_control.dispatcher import (
    DISPATCHER_LOG_PATH,
    UPDATE_PROGRAM_SCHEMA,
    AgentEnvironment,
    CreateResult,
    busy_chat_rows,
    chat_id_of,
    create_result,
    create_update_chat,
    find_run_lead,
    is_cron_available,
    is_run_in_flight,
    is_update_lease_held,
    is_update_skill_present,
    pending_dispatched_chat,
    read_json_object,
    resolve_account,
    run_started_at,
    write_file_if_changed,
    write_json_atomically,
    write_log_line,
)
from workspace_update_control.protocol import (
    DISMISSALS_FILE_PATH,
    DISPATCHER_CRON_NAME,
    DURABLE_CRON_PATH,
    LAST_DISPATCH_FILE_PATH,
    LAST_START_FILE_PATH,
    OUTCOME_ALREADY_RUNNING,
    OUTCOME_ARMED,
    OUTCOME_CREATE_FAILED,
    OUTCOME_NEEDS_SIGN_IN,
    OUTCOME_NO_CHANNEL_VERSION,
    OUTCOME_NO_CRON,
    OUTCOME_NO_UPDATE_SETTINGS,
    OUTCOME_STARTING,
    OUTCOME_TOO_OLD_TO_UPDATE,
    RUN_RECORD_PATH,
    SCHEDULE_FILE_PATH,
    SCHEDULE_ORIGIN_FILE_PATH,
    SKIP_NEEDS_SIGN_IN,
    SKIPPED_TARGET_FILE_PATH,
    STARTED_BY_IMMEDIATE,
)
from workspace_update_control.settings import (
    UpdateSettings,
    read_update_settings,
    settings_window,
)
from workspace_update_control.settings import (
    is_backup_configured as is_backup_configured_here,
)

# The workspace's agents as a caller reads them (``agents.read_observed_agents``, or the dispatcher's own
# listing): asked only when an operation turns on them, and None when they cannot be read.
AgentsReader = Callable[[], Sequence[Mapping[str, str]] | None]
# Runs the create of the update's chat off the caller's thread; a test passes one that runs it in place.
BackgroundRunner = Callable[[Callable[[], None]], None]

# The entry cron runs the tick through, relative to the workspace root.
# own-process: other-tier -- cron runs the tick with no venv, which a running update may have broken
TICK_ENTRY_PATH: Final[str] = "system/scripts/update_dispatcher_tick.py"
_CRON_LOCK_PATH: Final[str] = "/run/update-self-dispatch.lock"
_CRON_PATH_LINE: Final[str] = (
    "PATH=/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
)
_CRON_FILE_MODE: Final[int] = 0o644
_START_LOCK_PATH: Final[str] = LAST_START_FILE_PATH + ".lock"
# ``/proc/<pid>/stat`` field 22, the process's start time, counted among the fields after the command name.
_START_TIME_FIELD_INDEX: Final[int] = 19

_GONE_STARTER_DETAIL: Final[str] = "The process that was starting the update is gone."
_NO_WINDOW_DETAIL: Final[str] = (
    "The request named no update window, and this workspace holds no update settings to take one from."
)
# The values of a schedule the workspace may fill in itself, by the key the origin file marks each under.
_FOLLOWED_CEILING: Final[str] = "is_ceiling_followed"
_FOLLOWED_WINDOW: Final[str] = "is_window_followed"
_FOLLOWED_BACKUP: Final[str] = "is_backup_configured_followed"
_FAILED_CREATE_DETAIL: Final[str] = (
    "The update's chat could not be created: the create ended with an error."
)


def cron_line(workspace_root: str) -> str:
    """The drop-in cron runs the dispatcher from: its own PATH and working directory, one tick at a time."""
    tick = "cd %s && python3 %s >> %s 2>&1" % (
        workspace_root,
        TICK_ENTRY_PATH,
        DISPATCHER_LOG_PATH,
    )
    return "%s\n* * * * * root flock -n %s -c '%s'\n" % (
        _CRON_PATH_LINE,
        _CRON_LOCK_PATH,
        tick,
    )


def install_cron_entry(live_cron_dir: str) -> bool:
    """Write the cron line's durable copy (reinstalled at boot) and its live copy; whether cron will run it."""
    if not is_cron_available(live_cron_dir):
        return False
    line = cron_line(os.path.realpath(os.getcwd())).encode("utf-8")
    write_file_if_changed(DURABLE_CRON_PATH, line, _CRON_FILE_MODE)
    try:
        write_file_if_changed(
            os.path.join(live_cron_dir, DISPATCHER_CRON_NAME), line, _CRON_FILE_MODE
        )
    except OSError as exc:
        write_log_line("could not install the live cron entry: %s" % exc)
        return False
    return True


def _readiness_refusal(
    live_cron_dir: str, agent_environment: AgentEnvironment, is_cron_needed: bool
) -> tuple[CreateResult | None, tuple[bool, list[str]] | None]:
    """``(refusal, account)``: the outcome that stops an arm or a start before it does anything (None to go
    ahead), and what ``resolve_account`` answered once the checks got that far (None before)."""
    if is_cron_needed and not is_cron_available(live_cron_dir):
        detail = "This workspace has no cron program, so nothing in it could start a scheduled update."
        return create_result(OUTCOME_NO_CRON, detail), None
    if not is_update_skill_present():
        return create_result(
            OUTCOME_TOO_OLD_TO_UPDATE, "This workspace predates the update-self skill."
        ), None
    account = resolve_account(agent_environment)
    if not account[0]:
        return create_result(
            OUTCOME_NEEDS_SIGN_IN, "No AI account is signed in to this workspace."
        ), account
    return None, account


def _channel_version_for_request(
    now: float, fetch_channel_file: ChannelFileFetcher
) -> str:
    """The version a request that named none means: the one the workspace's release channel is on.

    Read off the feed when the last read is an hour old or none has succeeded, and otherwise as last read. It is
    held to the version a client from before the workspace read the feed named, while that client keeps asking,
    where the two can be compared. '' when the workspace has read no version for its channel and cannot.
    """
    settings = read_update_settings()
    if settings is None:
        return ""
    channel_ref = channel_template_ref(
        refresh_channel_version(
            settings, now, fetch_channel_file, is_due_when_unread=True
        )
    )
    if not channel_ref:
        return ""
    bounded = bounded_by_client_ceiling(channel_ref, read_fresh_client_ceiling(now))
    return channel_ref if bounded is None else bounded


def _no_channel_version() -> CreateResult:
    """The answer to a request that named no version, in a workspace that has read none for its channel."""
    settings = read_update_settings()
    if settings is None:
        reason = (
            "this workspace holds no update settings, so it follows no release channel"
        )
    elif not settings.feed_base_url:
        reason = "this workspace's update settings name no release feed to read a channel's version from"
    else:
        state = read_channel_version(settings) or {}
        reason = "the version of the %s channel could not be read (%s)" % (
            settings.channel,
            state.get("error") or "no read has been made",
        )
    return create_result(
        OUTCOME_NO_CHANNEL_VERSION,
        "The request named no version to update to, and %s." % reason,
    )


def _settings_window_for_request(now: float) -> tuple[int, int] | None:
    """The window a request that named none means: the update settings', in the workspace's timezone."""
    settings = read_update_settings()
    return settings_window(settings, now) if settings is not None else None


def _write_schedule_origin(armed_at: float, followed: Mapping[str, bool]) -> None:
    """Record which values of the schedule armed at ``armed_at`` the workspace filled in itself."""
    if not any(followed.values()):
        try:
            os.remove(SCHEDULE_ORIGIN_FILE_PATH)
        except FileNotFoundError:
            pass
        return
    write_json_atomically(
        SCHEDULE_ORIGIN_FILE_PATH,
        {"schema": UPDATE_PROGRAM_SCHEMA, "armed_at": armed_at, **followed},
    )


def arm_schedule(
    *,
    ceiling: str | None,
    override_ref: str,
    window_start_minute_utc: int | None,
    window_length_minutes: int | None,
    is_backup_configured: bool | None,
    is_auto_armed: bool,
    now: float,
    live_cron_dir: str,
    agent_environment: AgentEnvironment,
    fetch_channel_file: ChannelFileFetcher,
) -> dict[str, object]:
    """Arm the schedule: check the update-self skill, cron and a signed-in account, write the cron line, and
    write the schedule file, keeping the marks of the file it replaces and starting the dispatcher's record over.

    A value passed as None is the workspace's own (the channel's version, the settings' window, whether it holds
    backup credentials), and the schedule then keeps it current. With no version to be had the answer is
    ``NO_CHANNEL_VERSION``, unless the user named one, and with no window ``NO_UPDATE_SETTINGS``.
    """
    if ceiling is not None:
        record_client_ceiling(ceiling, now)
    refusal, _ = _readiness_refusal(
        live_cron_dir, agent_environment, is_cron_needed=True
    )
    if refusal is not None:
        return {**refusal, "schedule": read_json_object(SCHEDULE_FILE_PATH)}
    channel_ref = (
        _channel_version_for_request(now, fetch_channel_file) if ceiling is None else ""
    )
    # A version the user named needs no other: it bounds itself.
    armed_ceiling = ceiling if ceiling is not None else channel_ref or override_ref
    if not armed_ceiling:
        return {
            **_no_channel_version(),
            "schedule": read_json_object(SCHEDULE_FILE_PATH),
        }
    named_window = (
        (window_start_minute_utc, window_length_minutes)
        if window_start_minute_utc is not None and window_length_minutes is not None
        else None
    )
    window = (
        named_window if named_window is not None else _settings_window_for_request(now)
    )
    if window is None:
        return {
            **create_result(OUTCOME_NO_UPDATE_SETTINGS, _NO_WINDOW_DETAIL),
            "schedule": read_json_object(SCHEDULE_FILE_PATH),
        }
    armed_window_start_minute_utc, armed_window_length_minutes = window
    is_armed_with_backups = (
        is_backup_configured
        if is_backup_configured is not None
        else is_backup_configured_here()
    )
    if not install_cron_entry(live_cron_dir):
        detail = "This workspace's cron could not be given the update's schedule line."
        return {
            **create_result(OUTCOME_NO_CRON, detail),
            "schedule": read_json_object(SCHEDULE_FILE_PATH),
        }
    existing = read_json_object(SCHEDULE_FILE_PATH) or {}
    schedule: dict[str, object] = {
        "schema": UPDATE_PROGRAM_SCHEMA,
        "armed_at": now,
        "marked_chat": existing.get("marked_chat", ""),
        "marked_at": existing.get("marked_at"),
        "window_seen_open_at": existing.get("window_seen_open_at"),
        "pending_skip_reason": "",
        "next_attempt_at": None,
        "last_skip_reason": "",
        "last_skip_at": None,
        "ceiling": armed_ceiling,
        "window_start_minute_utc": armed_window_start_minute_utc,
        "window_length_minutes": armed_window_length_minutes,
        "is_backup_configured": is_armed_with_backups,
        "override_ref": override_ref,
        "is_auto_armed": is_auto_armed,
    }
    write_json_atomically(SCHEDULE_FILE_PATH, schedule)
    _write_schedule_origin(
        now,
        {
            _FOLLOWED_CEILING: ceiling is None and bool(channel_ref),
            _FOLLOWED_WINDOW: named_window is None,
            _FOLLOWED_BACKUP: is_backup_configured is None,
        },
    )
    write_log_line(
        "armed a schedule (%d minutes from %02d:%02d UTC, target %s)"
        % (
            armed_window_length_minutes,
            armed_window_start_minute_utc // 60,
            armed_window_start_minute_utc % 60,
            override_ref or armed_ceiling,
        )
    )
    return {**create_result(OUTCOME_ARMED), "schedule": schedule}


def _write_schedule_fields(
    schedule: dict[str, object], fields: Mapping[str, object]
) -> bool:
    """Write ``fields`` into the armed ``schedule`` where they differ; whether anything was written."""
    if all(schedule.get(key) == value for key, value in fields.items()):
        return False
    schedule.update(fields)
    write_json_atomically(SCHEDULE_FILE_PATH, schedule)
    return True


def refresh_schedule(
    *,
    ceiling: str | None,
    window_start_minute_utc: int | None,
    window_length_minutes: int | None,
    is_backup_configured: bool | None,
    now: float,
    live_cron_dir: str,
    fetch_channel_file: ChannelFileFetcher,
) -> dict[str, object]:
    """Rewrite the client's values in an armed schedule and bring the cron line current; nothing when none is armed.

    A value passed as None is rewritten with the workspace's own, and left as it is where the workspace has none
    (no version read for its channel, no update settings). The version the user typed, whether the client armed
    it by itself, and the dispatcher's own record stay as they are.
    """
    if ceiling is not None:
        record_client_ceiling(ceiling, now)
    schedule = read_json_object(SCHEDULE_FILE_PATH)
    if schedule is None:
        return {"is_armed": False, "schedule": None}
    fields: dict[str, object] = {}
    refreshed_ceiling = (
        ceiling
        if ceiling is not None
        else _channel_version_for_request(now, fetch_channel_file)
    )
    if refreshed_ceiling:
        fields["ceiling"] = refreshed_ceiling
    window = (
        (window_start_minute_utc, window_length_minutes)
        if window_start_minute_utc is not None and window_length_minutes is not None
        else _settings_window_for_request(now)
    )
    if window is not None:
        fields["window_start_minute_utc"], fields["window_length_minutes"] = window
    fields["is_backup_configured"] = (
        is_backup_configured
        if is_backup_configured is not None
        else is_backup_configured_here()
    )
    _write_schedule_fields(schedule, fields)
    install_cron_entry(live_cron_dir)
    return {"is_armed": True, "schedule": schedule}


def _read_followed_schedule(
    now: float,
) -> tuple[dict[str, object], dict[str, object]] | None:
    """The armed schedule and the record of which of its values the workspace filled in itself; None when there
    is no such schedule, or a client from before the workspace armed its own updates keeps asking."""
    schedule = read_json_object(SCHEDULE_FILE_PATH)
    origin = read_json_object(SCHEDULE_ORIGIN_FILE_PATH)
    if schedule is None or origin is None:
        return None
    if origin.get("armed_at") != schedule.get("armed_at"):
        return None
    if read_fresh_client_ceiling(now) is not None:
        return None
    return schedule, origin


def _followed_ceiling(
    origin: Mapping[str, object], settings: UpdateSettings | None
) -> str | None:
    """The version a schedule that follows the channel is kept at; None when it follows none, or the channel's
    version is not current."""
    channel_version = read_channel_version(settings)
    if origin.get(_FOLLOWED_CEILING) is True and is_channel_version_current(
        channel_version
    ):
        return channel_template_ref(channel_version)
    return None


def read_self_armed_schedule_move(now: float) -> str | None:
    """The version ``refresh_followed_schedule`` is about to move a schedule the workspace armed itself to; None
    when no such schedule is armed, or it stays at its version."""
    followed = _read_followed_schedule(now)
    if followed is None:
        return None
    schedule, origin = followed
    if schedule.get("is_auto_armed") is not True:
        return None
    ceiling = _followed_ceiling(origin, read_update_settings())
    return ceiling if ceiling != schedule.get("ceiling") else None


def refresh_followed_schedule(now: float) -> None:
    """Bring current the values of the armed schedule that the workspace filled in itself, from what it holds
    now: the version its channel was last read at, the window of its settings, whether it holds backup
    credentials. What the dispatcher does every minute for a schedule no client refreshes.

    A value a client named is left alone, and so is the whole schedule while a client from before the workspace
    armed its own updates keeps asking: that client rewrites the schedule with its own values, and the two
    would undo each other.
    """
    followed = _read_followed_schedule(now)
    if followed is None:
        return
    schedule, origin = followed
    settings = read_update_settings()
    fields: dict[str, object] = {}
    ceiling = _followed_ceiling(origin, settings)
    if ceiling is not None:
        fields["ceiling"] = ceiling
    if origin.get(_FOLLOWED_WINDOW) is True and settings is not None:
        fields["window_start_minute_utc"], fields["window_length_minutes"] = (
            settings_window(settings, now)
        )
    if origin.get(_FOLLOWED_BACKUP) is True:
        fields["is_backup_configured"] = is_backup_configured_here()
    if _write_schedule_fields(schedule, fields):
        write_log_line(
            "brought the armed schedule current (target %s)"
            % (schedule.get("override_ref") or schedule.get("ceiling"))
        )


def cancel_schedule() -> dict[str, object]:
    """Delete the schedule file, and the record of which of its values were the workspace's; whether there was one."""
    try:
        os.remove(SCHEDULE_FILE_PATH)
    except FileNotFoundError:
        return {"is_cancelled": False}
    try:
        os.remove(SCHEDULE_ORIGIN_FILE_PATH)
    except FileNotFoundError:
        pass
    write_log_line("cancelled the schedule")
    return {"is_cancelled": True}


def _epoch(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def skip_target(*, target_ref: str, skipped_at: float) -> dict[str, object]:
    """Cancel the schedule and record ``target_ref`` as skipped; of two skips the later ``skipped_at`` is kept."""
    is_cancelled = bool(cancel_schedule()["is_cancelled"])
    existing = read_json_object(SKIPPED_TARGET_FILE_PATH)
    existing_at = _epoch(existing.get("skipped_at")) if existing is not None else None
    if existing is not None and existing_at is not None and existing_at > skipped_at:
        return {"is_cancelled": is_cancelled, "skipped_target": existing}
    recorded: dict[str, object] = {
        "schema": UPDATE_PROGRAM_SCHEMA,
        "target_ref": target_ref,
        "skipped_at": skipped_at,
    }
    write_json_atomically(SKIPPED_TARGET_FILE_PATH, recorded)
    write_log_line("recorded the skipped update target %s" % target_ref)
    return {"is_cancelled": is_cancelled, "skipped_target": recorded}


def record_dismissals(
    *, note_run_started_at: float | None, outcome_run_started_at: float | None
) -> dict[str, object]:
    """Record a run's note or outcome as dismissed, by the run's start; a later run's start covers an earlier one's."""
    existing = read_json_object(DISMISSALS_FILE_PATH) or {}
    merged = dict(existing)
    for key, dismissed_at in (
        ("note_run_started_at", note_run_started_at),
        ("outcome_run_started_at", outcome_run_started_at),
    ):
        if dismissed_at is None:
            continue
        existing_at = _epoch(merged.get(key))
        if existing_at is None or existing_at < dismissed_at:
            merged[key] = dismissed_at
    if merged != existing:
        merged["schema"] = UPDATE_PROGRAM_SCHEMA
        write_json_atomically(DISMISSALS_FILE_PATH, merged)
        write_log_line("recorded update-row dismissals")
    return {"dismissals": merged if merged else None}


def _process_start_time(proc_root: str, pid: int) -> str:
    """When the process started, in clock ticks since boot, or '' where ``/proc`` does not have it."""
    try:
        with open(
            os.path.join(proc_root, str(pid), "stat"),
            encoding="utf-8",
            errors="replace",
        ) as handle:
            stat_line = handle.read()
    except OSError:
        return ""
    # The command name is parenthesized and may itself hold spaces and parentheses.
    fields = stat_line.rpartition(")")[2].split()
    return (
        fields[_START_TIME_FIELD_INDEX] if len(fields) > _START_TIME_FIELD_INDEX else ""
    )


def _is_starter_live(record: Mapping[str, object], proc_root: str) -> bool:
    """Whether the process a start attempt names is still there: alive, and started when the record says."""
    pid = record.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    recorded_start = str(record.get("pid_start_time") or "")
    return not recorded_start or _process_start_time(proc_root, pid) in (
        "",
        recorded_start,
    )


def read_last_start(proc_root: str) -> dict[str, object] | None:
    """The last start attempt; one with no outcome whose process is gone reads as ``CREATE_FAILED``."""
    record = read_json_object(LAST_START_FILE_PATH)
    if record is None:
        return None
    if record.get("outcome") is None and not _is_starter_live(record, proc_root):
        return {
            **record,
            "outcome": OUTCOME_CREATE_FAILED,
            "detail": _GONE_STARTER_DETAIL,
        }
    return record


@contextmanager
def _start_lock() -> Generator[None, None, None]:
    """Serialize the check for a start under way with the write that begins one."""
    os.makedirs(os.path.dirname(_START_LOCK_PATH), exist_ok=True)
    with open(_START_LOCK_PATH, "a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _finish_start(
    attempt_id: str,
    fields: Mapping[str, object],
    agents: Sequence[Mapping[str, str]] | None,
    now: float,
    account: tuple[bool, Sequence[str]],
    agent_environment: AgentEnvironment,
    clock: Callable[[], float],
) -> None:
    """Create the update's chat and record how it went, unless a later attempt has replaced this one's record."""
    # What the record says when the create raises: left with no outcome, this live process would hold every
    # later start back as one still under way.
    created = create_result(OUTCOME_CREATE_FAILED, _FAILED_CREATE_DETAIL)
    try:
        created = create_update_chat(
            fields,
            STARTED_BY_IMMEDIATE,
            is_auto_open=True,
            agents=agents,
            now=now,
            account=account,
            agent_environment=agent_environment,
        )
    except OSError as exc:
        write_log_line("the start of an update failed: %s" % exc)
        created = create_result(
            OUTCOME_CREATE_FAILED, "The update's chat could not be created: %s" % exc
        )
    finally:
        with _start_lock():
            record = read_json_object(LAST_START_FILE_PATH)
            if record is not None and record.get("attempt_id") == attempt_id:
                write_json_atomically(
                    LAST_START_FILE_PATH,
                    {**record, **created, "finished_at": clock()},
                )


def start_update(
    *,
    ceiling: str | None,
    override_ref: str,
    is_backup_configured: bool | None,
    now: float,
    live_cron_dir: str,
    proc_root: str,
    agent_environment: AgentEnvironment,
    read_agents: AgentsReader,
    run_in_background: BackgroundRunner,
    clock: Callable[[], float],
    fetch_channel_file: ChannelFileFetcher,
) -> dict[str, object]:
    """Start an update now: write the cron line, make the quick checks, and begin creating the update's chat.

    Answers a refusal (the skill is missing, no account is signed in, no version was named and the workspace has
    read none for its channel, an update is running or about to) as its outcome. Otherwise answers ``STARTING``
    with the attempt's id, and the create runs on through ``run_in_background``; ``last-start.json`` holds how it
    went once it ends.

    A ``ceiling`` passed as None is the channel's version, and an ``is_backup_configured`` passed as None is
    whether the workspace holds backup credentials.
    """
    if ceiling is not None:
        record_client_ceiling(ceiling, now)
    # Only where cron runs the watchdog is a stopped lead brought back.
    is_watchdog_installed = install_cron_entry(live_cron_dir)
    refusal, account = _readiness_refusal(
        live_cron_dir, agent_environment, is_cron_needed=False
    )
    if refusal is not None:
        return {**refusal}
    assert account is not None
    # A version the user named needs no other: it bounds itself.
    started_ceiling = (
        ceiling
        if ceiling is not None
        else _channel_version_for_request(now, fetch_channel_file) or override_ref
    )
    if not started_ceiling:
        return {**_no_channel_version()}
    agents = read_agents()
    run = read_json_object(RUN_RECORD_PATH)
    # An ALREADY_RUNNING answer names when the run it found began, which tells its record apart from an
    # earlier run's under the same chat name.
    if run is not None and is_run_in_flight(
        run, agents, is_stopped_lead_revived=is_watchdog_installed
    ):
        lead = find_run_lead(agents or [], run)
        already_running = create_result(
            OUTCOME_ALREADY_RUNNING,
            "An update is already running in this workspace.",
            str(run["chat_agent_name"]),
            chat_id_of(lead) if lead else "",
        )
        return {**already_running, "run_started_at": run_started_at(run)}
    if is_update_lease_held(agents, agent_environment):
        return {
            **create_result(
                OUTCOME_ALREADY_RUNNING,
                "Another update flow is working in this workspace.",
            )
        }
    pending = pending_dispatched_chat(run, agents, now)
    if pending is not None:
        name, chat_id, dispatched_at = pending
        starting = create_result(
            OUTCOME_ALREADY_RUNNING,
            "An update is already starting in this workspace.",
            name,
            chat_id,
        )
        return {**starting, "run_started_at": dispatched_at}
    fields: dict[str, object] = {
        "ceiling": started_ceiling,
        "override_ref": override_ref,
        "is_backup_configured": is_backup_configured
        if is_backup_configured is not None
        else is_backup_configured_here(),
    }
    with _start_lock():
        under_way = read_json_object(LAST_START_FILE_PATH)
        if (
            under_way is not None
            and under_way.get("outcome") is None
            and _is_starter_live(under_way, proc_root)
        ):
            already_starting = create_result(
                OUTCOME_ALREADY_RUNNING,
                "An update is already starting in this workspace.",
            )
            return {
                **already_starting,
                "attempt_id": str(under_way.get("attempt_id") or ""),
            }
        attempt_id = uuid.uuid4().hex
        own_pid = os.getpid()
        write_json_atomically(
            LAST_START_FILE_PATH,
            {
                "schema": UPDATE_PROGRAM_SCHEMA,
                "attempt_id": attempt_id,
                "started_at": now,
                "pid": own_pid,
                "pid_start_time": _process_start_time(proc_root, own_pid),
                "outcome": None,
                "detail": "",
                "chat_agent_name": "",
                "chat_id": "",
                "finished_at": None,
            },
        )
    run_in_background(
        lambda: _finish_start(
            attempt_id, fields, agents, now, account, agent_environment, clock
        )
    )
    return {**create_result(OUTCOME_STARTING), "attempt_id": attempt_id}


def _run_chat_id(
    run: Mapping[str, object] | None,
    last_dispatch: Mapping[str, object] | None,
    read_agents: AgentsReader,
) -> str | None:
    """The chat the run record names, looked up when the last dispatch does not name it.

    That is a run started by hand in a chat, or one whose dispatch a later one replaced. None when there is
    nothing to look up or the workspace's agents could not be read; "" when no such chat is listed.
    """
    if run is None:
        return None
    chat_name = run.get("chat_agent_name")
    if not chat_name or (last_dispatch or {}).get("chat_agent_name") == chat_name:
        return None
    agents = read_agents()
    if agents is None:
        return None
    lead = find_run_lead(agents, run)
    return chat_id_of(lead) if lead is not None else ""


def read_update_state(
    *,
    is_sign_in_checked: bool,
    proc_root: str,
    agent_environment: AgentEnvironment,
    read_agents: AgentsReader,
) -> dict[str, object]:
    """The schedule, the run record, the last dispatch, the last start, the skipped version, the dismissals, the
    update settings, and the version the settings' channel was last read at.

    Whether an account is signed in rides along when asked, or when the schedule's skip says none was, and the id
    of the chat the run record names, when the last dispatch does not name it. Nothing is written, and the feed
    is not read.
    """
    schedule = read_json_object(SCHEDULE_FILE_PATH)
    is_skipped_for_sign_in = schedule is not None and SKIP_NEEDS_SIGN_IN in (
        schedule.get("pending_skip_reason"),
        schedule.get("last_skip_reason"),
    )
    is_sign_in_asked = is_sign_in_checked or is_skipped_for_sign_in
    run = read_json_object(RUN_RECORD_PATH)
    last_dispatch = read_json_object(LAST_DISPATCH_FILE_PATH)
    settings = read_update_settings()
    return {
        "settings": settings.document() if settings is not None else None,
        "channel_version": read_channel_version(settings),
        "schedule": schedule,
        "run": run,
        "last_dispatch": last_dispatch,
        "run_chat_id": _run_chat_id(run, last_dispatch, read_agents),
        "is_signed_in": resolve_account(agent_environment)[0]
        if is_sign_in_asked
        else None,
        "skipped_target": read_json_object(SKIPPED_TARGET_FILE_PATH),
        "dismissals": read_json_object(DISMISSALS_FILE_PATH),
        "last_start": read_last_start(proc_root),
    }


def read_liveness(
    *,
    live_cron_dir: str,
    agent_environment: AgentEnvironment,
    read_agents: AgentsReader,
) -> dict[str, object]:
    """What tells whether a running update is still alive: the run record, the workspace's agents with their
    states and chats (None when they cannot be read), the chats with a live background task, and whether the
    dispatcher's cron line is installed."""
    agents = read_agents()
    return {
        "run": read_json_object(RUN_RECORD_PATH),
        "agents": None
        if agents is None
        else [
            {
                "name": str(agent.get("name") or ""),
                "state": str(agent.get("state") or ""),
                "agent_id": str(agent.get("id") or ""),
                "chat_id": str(agent.get("labels.chat_id") or ""),
            }
            for agent in agents
        ],
        "busy_chats": busy_chat_rows(agent_environment),
        "is_watchdog_installed": os.path.isfile(
            os.path.join(live_cron_dir, DISPATCHER_CRON_NAME)
        ),
    }
