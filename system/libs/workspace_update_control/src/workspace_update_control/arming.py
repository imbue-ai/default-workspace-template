"""The workspace arms its own updates: the part of the dispatcher's minute that needs no client.

When the update settings turn arming on, nothing is armed or running, and no decision waits for the user, the
tick reads the version the workspace's release channel is on and arms a schedule for it, unless the workspace is
not behind that version, the user skipped it, or a run already went out for it. An armed schedule is then
started inside its window by the dispatcher's own tick, like one a client armed.

The decision is the one the Imbue Studio desktop's sweep made for a workspace (``should_auto_arm`` in the mngr
repository's ``workspace_update_state.py``), with the newest version taken from the channel instead of from a
client's build. Two things differ, because the workspace has only its own files to go by: a run with no verdict
holds an arm back for as long as it has none (the watchdog closes out a run whose lead is gone), and a workspace
behind its channel by a patch alone is armed like any other.

A checkout that names no version cannot be behind anything. In the minute the channel's version is read, the tick
fetches the template's release tags into such a checkout (``release_tags``), unless an update is running.
"""

import os

from collections.abc import Callable, Mapping
from typing import Final, NamedTuple

from workspace_update_control.channel import (
    ChannelFileFetcher,
    bounded_by_client_ceiling,
    channel_template_ref,
    is_channel_version_current,
    read_fresh_client_ceiling,
    refresh_channel_version,
)
from workspace_update_control.control import (
    arm_schedule,
    cancel_schedule_armed_at,
    read_self_armed_schedule,
    refresh_followed_schedule,
)
from workspace_update_control.dispatcher import (
    UPDATE_PROGRAM_SCHEMA,
    AgentEnvironment,
    pending_dispatched_chat,
    read_json_object,
    run_started_at,
    write_json_atomically,
    write_log_line,
)
from workspace_update_control.installed_version import (
    is_release_behind,
    read_installed_ref,
    release_sort_key,
)
from workspace_update_control.protocol import (
    DISMISSALS_FILE_PATH,
    INSTALLED_VERSION_FILE_PATH,
    LAST_DISPATCH_FILE_PATH,
    OUTCOME_ALREADY_ARMED,
    OUTCOME_ARMED,
    RUN_RECORD_PATH,
    SCHEDULE_FILE_PATH,
    SKIPPED_TARGET_FILE_PATH,
)
from workspace_update_control.release_tags import fetch_release_tags
from workspace_update_control.settings import read_update_settings

# How a run that ended short is recorded (the update-self skill's verdicts, the desktop's
# ``FAILURE_UPDATE_VERDICTS``), and the verdict of a run that waits for the user's answer.
_FAILURE_VERDICTS: Final[tuple[str, ...]] = ("STUCK", "REFUSED", "NEEDS_RECREATION")
_NEEDS_DECISION_VERDICT: Final[str] = "NEEDS_DECISION"
# The version of the checkout is read from git, which the tick cannot afford every minute; it changes when an
# update lands, which the run record shows.
_INSTALLED_VERSION_MAX_AGE_SECONDS: Final[float] = 60 * 60


class LastDispatch(NamedTuple):
    """The update chat last created: what it aimed at, and its name."""

    target_ref: str
    chat_agent_name: str


class ArmingFacts(NamedTuple):
    """What the decision to arm turns on, apart from whether the workspace is behind its channel."""

    # Whether the settings have the workspace arm its own updates.
    is_enabled: bool
    is_scheduled: bool
    is_run_in_flight: bool
    # The last run's verdict as the user still sees it: '' once they dismissed it, or when there is none.
    shown_verdict: str
    # The chat of the run the verdict is of.
    run_chat_agent_name: str
    # The version the user skipped for this workspace; '' when they skipped none.
    skipped_target_ref: str
    last_dispatch: LastDispatch | None


def _is_version_held_back(facts: ArmingFacts, target_ref: str) -> bool:
    """Whether the workspace does not aim at ``target_ref`` by itself: the user skipped it, or a run already
    went out for it."""
    return facts.skipped_target_ref == target_ref or (
        facts.last_dispatch is not None and facts.last_dispatch.target_ref == target_ref
    )


def should_auto_arm(
    facts: ArmingFacts,
    target_ref: str,
    # Asked only when the answer turns on it, as it may read the checkout's version from git.
    is_update_offered: Callable[[], bool],
) -> bool:
    """Whether the workspace arms its update to ``target_ref`` for the user.

    Only a workspace behind ``target_ref``, with nothing armed or running, no decision pending, and this version
    not skipped. A version a run already went out for is not armed again: the workspace is still behind, so that
    run failed, and only a newer version is a reason to try again unwatched. Reading the failure dismisses it, so
    the dispatch record, not the verdict, is what remembers the attempt. A failure still shown holds the version
    back unless it is the last dispatch's run and that run aimed elsewhere: at an older version, or at one the
    user named.
    """
    if not facts.is_enabled or facts.is_scheduled or facts.is_run_in_flight:
        return False
    if facts.shown_verdict == _NEEDS_DECISION_VERDICT or _is_version_held_back(
        facts, target_ref
    ):
        return False
    last_dispatch = facts.last_dispatch
    if facts.shown_verdict in _FAILURE_VERDICTS and (
        # A failure the last dispatch did not make says nothing of what it aimed at.
        last_dispatch is None
        or last_dispatch.chat_agent_name != facts.run_chat_agent_name
    ):
        return False
    return is_update_offered()


def _shown_verdict(
    run: Mapping[str, object] | None, dismissals: Mapping[str, object] | None
) -> str:
    """The run's verdict unless the user dismissed that run's outcome."""
    verdict = run.get("verdict") if run is not None else None
    if not isinstance(verdict, str):
        return ""
    started_at = run_started_at(run)
    dismissed_at = (dismissals or {}).get("outcome_run_started_at")
    is_dismissed = (
        started_at is not None
        and isinstance(dismissed_at, (int, float))
        and not isinstance(dismissed_at, bool)
        and dismissed_at >= started_at
    )
    return "" if is_dismissed else verdict


def _read_skipped_target_ref() -> str:
    """The version the user skipped for this workspace; '' when they skipped none."""
    skipped = read_json_object(SKIPPED_TARGET_FILE_PATH) or {}
    return str(skipped.get("target_ref") or "")


def read_arming_facts(is_enabled: bool, now: float) -> ArmingFacts:
    """The facts of the workspace's own files, with no listing of its agents.

    A run counts as in flight for as long as its record has no verdict, and while the last dispatch's chat may
    still be on its way to recording its run.
    """
    run = read_json_object(RUN_RECORD_PATH)
    dispatch = read_json_object(LAST_DISPATCH_FILE_PATH)
    is_run_open = (
        run is not None
        and run.get("verdict") is None
        and bool(run.get("chat_agent_name"))
    )
    return ArmingFacts(
        is_enabled=is_enabled,
        is_scheduled=read_json_object(SCHEDULE_FILE_PATH) is not None,
        is_run_in_flight=is_run_open
        or pending_dispatched_chat(run, None, now) is not None,
        shown_verdict=_shown_verdict(run, read_json_object(DISMISSALS_FILE_PATH)),
        run_chat_agent_name=str((run or {}).get("chat_agent_name") or ""),
        skipped_target_ref=_read_skipped_target_ref(),
        last_dispatch=LastDispatch(
            target_ref=str(dispatch.get("target_ref") or ""),
            chat_agent_name=str(dispatch.get("chat_agent_name") or ""),
        )
        if dispatch is not None
        else None,
    )


def _run_identity(run: Mapping[str, object] | None) -> list[object]:
    """What tells one state of the run record from the next: a new run, or a verdict, moves it."""
    return [run_started_at(run), run.get("verdict") if run is not None else None]


def _read_installed_version(now: float) -> str | None:
    """The version of the checkout, read from git at most once an hour while the run record stands still; None
    when git could not be asked. A read git could not make is not kept, so the next minute asks again."""
    identity = _run_identity(read_json_object(RUN_RECORD_PATH))
    kept = read_json_object(INSTALLED_VERSION_FILE_PATH)
    if kept is not None and kept.get("run_identity") == identity:
        read_at = kept.get("read_at")
        template_ref = kept.get("template_ref")
        if (
            isinstance(template_ref, str)
            and isinstance(read_at, (int, float))
            and not isinstance(read_at, bool)
            and 0 <= now - read_at < _INSTALLED_VERSION_MAX_AGE_SECONDS
        ):
            return template_ref
    template_ref = read_installed_ref(".")
    if template_ref is None:
        write_log_line(
            "could not read the checkout's version from git; asking again next minute"
        )
        return None
    write_json_atomically(
        INSTALLED_VERSION_FILE_PATH,
        {
            "schema": UPDATE_PROGRAM_SCHEMA,
            "template_ref": template_ref,
            "read_at": now,
            "run_identity": identity,
        },
    )
    return template_ref


def read_installed_version(now: float) -> str:
    """The version of the checkout, read from git at most once an hour while the run record stands still.

    A read git could not make is not kept, so the next minute asks again; it answers '' for this one.
    """
    return _read_installed_version(now) or ""


def _fetch_release_tags_where_no_version_is_named(now: float) -> None:
    """Fetch the template's release tags into a checkout that names no version, and have its version read again."""
    if _read_installed_version(now) != "":
        return
    if not fetch_release_tags(now):
        return
    try:
        os.remove(INSTALLED_VERSION_FILE_PATH)
    except FileNotFoundError:
        pass


def _why_not_moved_to(facts: ArmingFacts, moved_to: str, now: float) -> str:
    """Why the workspace's own schedule does not follow its channel to ``moved_to``; '' when it does.

    A checkout whose version could not be read, or names no release, says nothing of whether the workspace is
    behind, and the schedule follows.
    """
    if _is_version_held_back(facts, moved_to):
        return "was skipped or already tried"
    installed_ref = read_installed_version(now)
    if release_sort_key(installed_ref) is not None and not is_release_behind(
        installed_ref, moved_to
    ):
        return "the workspace is not behind"
    return ""


def tick_self_arming(
    *,
    now: float,
    live_cron_dir: str,
    agent_environment: AgentEnvironment,
    fetch_channel_file: ChannelFileFetcher,
) -> None:
    """One minute of the workspace arming its own updates.

    Reads the channel's version when the last read is an hour old, keeps current the values of an armed schedule
    that are the workspace's own, and arms a schedule for the channel's version when ``should_auto_arm`` says
    so. A channel whose file could not be read arms nothing. Neither does a version past the one a client from
    before the workspace armed its own updates named, while that client keeps asking. An arm that is refused,
    as one is while no AI account is signed in, is tried again every minute, and is written to the dispatcher's
    log in the minute the channel's version was read.

    A checkout that names no version has the template's release tags fetched into it in the minute the channel's
    version is read, and again a day later at the earliest while it still names none. Nothing is fetched while an
    update is running, which fetches into the same checkout.

    A schedule the workspace armed itself is cancelled, not moved, when its channel moves to a version the user
    skipped, a run already went out for, or the workspace is not behind: the workspace would not have armed that
    version. One that stands at a version the user skipped is cancelled as well. A skip cancels whatever is armed
    when it is recorded, so one recorded while a minute was deciding finds nothing to cancel: that minute reads
    the skip again once it has armed, and cancels what it armed. A schedule armed while a minute was deciding
    stands as it was armed: the minute arms only where none is armed when it comes to write, and cancels only
    the schedule it read.
    """
    settings = read_update_settings()
    if settings is None:
        return
    channel_version = refresh_channel_version(
        settings, now, fetch_channel_file, is_due_when_unread=False
    )
    facts = read_arming_facts(settings.is_arming_enabled, now)
    is_channel_read_this_minute = (
        channel_version is not None and channel_version.get("checked_at") == now
    )
    if is_channel_read_this_minute and not facts.is_run_in_flight:
        _fetch_release_tags_where_no_version_is_named(now)
    if facts.is_scheduled:
        self_armed = read_self_armed_schedule(now)
        if self_armed is None:
            refresh_followed_schedule(now)
            return
        armed_for = self_armed.target_ref
        if armed_for is not None and armed_for == facts.skipped_target_ref:
            if cancel_schedule_armed_at(self_armed.armed_at):
                write_log_line(
                    "cancelled the workspace's own schedule: %s was skipped" % armed_for
                )
            return
        moved_to = self_armed.moved_to
        why_not_armed = (
            _why_not_moved_to(facts, moved_to, now) if moved_to is not None else ""
        )
        if why_not_armed:
            if cancel_schedule_armed_at(self_armed.armed_at):
                write_log_line(
                    "cancelled the workspace's own schedule: its channel moved to %s, which %s"
                    % (moved_to, why_not_armed)
                )
            return
        refresh_followed_schedule(now)
        return
    if not is_channel_version_current(channel_version):
        return
    target_ref = bounded_by_client_ceiling(
        channel_template_ref(channel_version), read_fresh_client_ceiling(now)
    )
    if target_ref is None:
        return
    if not should_auto_arm(
        facts,
        target_ref,
        lambda: is_release_behind(read_installed_version(now), target_ref),
    ):
        return
    answer = arm_schedule(
        ceiling=None,
        override_ref="",
        window_start_minute_utc=None,
        window_length_minutes=None,
        is_backup_configured=None,
        is_auto_armed=True,
        is_armed_schedule_kept=True,
        now=now,
        live_cron_dir=live_cron_dir,
        agent_environment=agent_environment,
        fetch_channel_file=fetch_channel_file,
    )
    if answer.get("outcome") != OUTCOME_ARMED:
        # Said in the minute the channel's version was read, which comes once an hour, and not in each of the
        # minutes between. A schedule that stands says so itself where it is found.
        if answer.get("outcome") != OUTCOME_ALREADY_ARMED and is_channel_read_this_minute:
            write_log_line(
                "did not arm the update to %s for the workspace itself: %s (%s)"
                % (target_ref, answer.get("outcome"), answer.get("detail"))
            )
        return
    if _read_skipped_target_ref() == target_ref:
        armed = answer.get("schedule")
        if isinstance(armed, Mapping) and cancel_schedule_armed_at(
            armed.get("armed_at")
        ):
            write_log_line(
                "cancelled the update to %s the workspace had just armed: it was skipped meanwhile"
                % target_ref
            )
        return
    write_log_line("armed the update to %s for the workspace itself" % target_ref)
