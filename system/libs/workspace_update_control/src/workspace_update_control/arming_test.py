"""The workspace arming its own updates: the decision, and the minute that makes it against a staged workspace."""

import os
from collections.abc import Callable
from datetime import datetime
from typing import Any

import pytest

from workspace_update_control import control
from workspace_update_control.arming import (
    ArmingFacts,
    LastDispatch,
    read_installed_version,
    should_auto_arm,
    tick_self_arming,
)
from workspace_update_control.channel import (
    CHANNEL_READ_INTERVAL_SECONDS,
    CLIENT_CEILING_HOLD_SECONDS,
)
from workspace_update_control.dispatcher import AgentEnvironment
from workspace_update_control.testing import (
    STAGED_FEED_BASE_URL,
    RecordingFeed,
    StagedUpdateWorkspace,
    stage_checkout_at,
    stage_update_workspace,
    update_settings_document,
)

_INSTALLED = "minds-v0.9.0"
_ON_THE_CHANNEL = "minds-v0.10.0"


def _epoch(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


# The staged settings' window is 02:00 to 05:00, read in UTC while the workspace records no timezone.
_OUTSIDE_THE_WINDOW = _epoch("2026-10-20T12:00:00+00:00")
_INSIDE_THE_WINDOW = _epoch("2026-10-21T03:00:00+00:00")
_NOW = _OUTSIDE_THE_WINDOW

_NOTHING_IN_THE_WAY = ArmingFacts(
    is_enabled=True,
    is_scheduled=False,
    is_run_in_flight=False,
    shown_verdict="",
    run_chat_agent_name="",
    skipped_target_ref="",
    last_dispatch=None,
)
_UPDATE_CHAT = "update-to-minds-v0-4-1"


def _dispatch_to(target_ref: str, chat: str = _UPDATE_CHAT) -> LastDispatch:
    return LastDispatch(target_ref=target_ref, chat_agent_name=chat)


def _offered() -> bool:
    return True


def _not_offered() -> bool:
    return False


@pytest.mark.parametrize(
    ("facts", "is_armed"),
    [
        (_NOTHING_IN_THE_WAY, True),
        (_NOTHING_IN_THE_WAY._replace(is_enabled=False), False),
        (_NOTHING_IN_THE_WAY._replace(is_scheduled=True), False),
        (_NOTHING_IN_THE_WAY._replace(is_run_in_flight=True), False),
        (_NOTHING_IN_THE_WAY._replace(shown_verdict="NEEDS_DECISION"), False),
        (_NOTHING_IN_THE_WAY._replace(skipped_target_ref="minds-v0.4.1"), False),
        # A release skipped earlier does not hold back a newer one.
        (_NOTHING_IN_THE_WAY._replace(skipped_target_ref="minds-v0.4.0"), True),
        # A run that failed after its lead's best effort is not retried unwatched.
        (
            _NOTHING_IN_THE_WAY._replace(
                shown_verdict="STUCK", run_chat_agent_name=_UPDATE_CHAT
            ),
            False,
        ),
        # A run that landed is no reason to hold the next release back.
        (
            _NOTHING_IN_THE_WAY._replace(
                shown_verdict="UPDATED", run_chat_agent_name="update-to-minds-v0-4-0"
            ),
            True,
        ),
    ],
)
def test_the_workspace_arms_only_with_arming_on_and_nothing_in_the_way(
    facts: ArmingFacts, is_armed: bool
) -> None:
    assert should_auto_arm(facts, "minds-v0.4.1", _offered) is is_armed


def test_a_workspace_that_is_not_behind_the_version_is_not_armed() -> None:
    assert should_auto_arm(_NOTHING_IN_THE_WAY, "minds-v0.4.1", _not_offered) is False


@pytest.mark.parametrize(
    "facts",
    [
        _NOTHING_IN_THE_WAY._replace(is_enabled=False),
        _NOTHING_IN_THE_WAY._replace(is_scheduled=True),
        _NOTHING_IN_THE_WAY._replace(skipped_target_ref="minds-v0.4.1"),
        _NOTHING_IN_THE_WAY._replace(last_dispatch=_dispatch_to("minds-v0.4.1")),
    ],
)
def test_the_checkouts_version_is_not_asked_for_when_something_else_already_decides(
    facts: ArmingFacts,
) -> None:
    asked: list[bool] = []

    assert (
        should_auto_arm(facts, "minds-v0.4.1", lambda: bool(asked.append(True)))
        is False
    )
    assert asked == []


@pytest.mark.parametrize("shown_verdict", ("STUCK", ""), ids=("shown", "dismissed"))
def test_a_failed_release_is_tried_again_only_once_a_newer_one_is_out(
    shown_verdict: str,
) -> None:
    """Reading the failure dismisses it; the attempt is still on record, so dismissing arms nothing again."""
    facts = _NOTHING_IN_THE_WAY._replace(
        shown_verdict=shown_verdict,
        run_chat_agent_name=_UPDATE_CHAT,
        last_dispatch=_dispatch_to("minds-v0.4.1"),
    )

    assert should_auto_arm(facts, "minds-v0.4.1", _offered) is False
    assert should_auto_arm(facts, "minds-v0.4.2", _offered) is True


def test_a_shown_failure_the_last_dispatch_did_not_make_holds_the_release_back() -> (
    None
):
    failed_by_hand = _NOTHING_IN_THE_WAY._replace(
        shown_verdict="REFUSED",
        run_chat_agent_name="update-by-hand",
        last_dispatch=_dispatch_to("minds-v0.4.1"),
    )

    assert should_auto_arm(failed_by_hand, "minds-v0.4.2", _offered) is False


@pytest.mark.parametrize("shown_verdict", ("", "STUCK"), ids=("dismissed", "shown"))
def test_the_release_on_the_channel_is_armed_after_a_run_to_a_version_the_user_named(
    shown_verdict: str,
) -> None:
    facts = _NOTHING_IN_THE_WAY._replace(
        shown_verdict=shown_verdict,
        run_chat_agent_name=_UPDATE_CHAT,
        last_dispatch=_dispatch_to("minds-v0.3.15"),
    )

    assert should_auto_arm(facts, "minds-v0.4.1", _offered) is True


def _stage_a_workspace_behind_its_channel(
    staged: StagedUpdateWorkspace, **settings: Any
) -> RecordingFeed:
    """A checkout at ``_INSTALLED`` with update settings (arming on unless ``settings`` say otherwise), and a feed
    whose stable channel is on ``_ON_THE_CHANNEL``."""
    stage_checkout_at(staged.root, ("the template at a release", _INSTALLED))
    staged.write_state("settings.json", update_settings_document(**settings))
    return RecordingFeed(_ON_THE_CHANNEL)


def _tick(staged: StagedUpdateWorkspace, feed: RecordingFeed, now: float) -> None:
    tick_self_arming(
        now=now,
        live_cron_dir=str(staged.live_cron_dir),
        agent_environment=AgentEnvironment(),
        fetch_channel_file=feed,
    )


def _armed_target(staged: StagedUpdateWorkspace) -> str | None:
    schedule = staged.read_state("schedule.json")
    return schedule["ceiling"] if schedule is not None else None


def test_a_workspace_behind_its_channel_arms_its_own_update_with_no_client_present(
    staged: StagedUpdateWorkspace,
) -> None:
    feed = _stage_a_workspace_behind_its_channel(staged)

    _tick(staged, feed, _NOW)

    assert feed.asked == [f"{STAGED_FEED_BASE_URL}/stable-web.json"]
    assert staged.read_state("schedule.json") == {
        "schema": 1,
        "armed_at": _NOW,
        "marked_chat": "",
        "marked_at": None,
        "window_seen_open_at": None,
        "pending_skip_reason": "",
        "next_attempt_at": None,
        "last_skip_reason": "",
        "last_skip_at": None,
        "ceiling": _ON_THE_CHANNEL,
        "window_start_minute_utc": 2 * 60,
        "window_length_minutes": 3 * 60,
        "is_backup_configured": False,
        "override_ref": "",
        "is_auto_armed": True,
    }
    assert (staged.live_cron_dir / "update-self-dispatch").exists()
    assert any(
        line.endswith(f"armed the update to {_ON_THE_CHANNEL} for the workspace itself")
        for line in staged.dispatcher_log_lines()
    )


def test_the_armed_window_is_the_settings_hours_in_the_workspaces_recorded_timezone(
    staged: StagedUpdateWorkspace,
) -> None:
    feed = _stage_a_workspace_behind_its_channel(staged)
    (staged.root / "data" / ".state" / "user_timezone").write_text(
        "America/Los_Angeles\n"
    )
    (staged.root / "data" / ".secrets").mkdir(parents=True)
    (staged.root / "data" / ".secrets" / "restic.env").write_text(
        "RESTIC_REPOSITORY=x\n"
    )

    _tick(staged, feed, _NOW)

    schedule = staged.read_state("schedule.json")
    assert schedule is not None
    # 02:00 in Los Angeles is 09:00 UTC in October.
    assert (schedule["window_start_minute_utc"], schedule["window_length_minutes"]) == (
        9 * 60,
        3 * 60,
    )
    assert schedule["is_backup_configured"] is True


def test_the_armed_window_is_the_workspaces_own_hours_where_it_has_them(
    staged: StagedUpdateWorkspace,
) -> None:
    feed = _stage_a_workspace_behind_its_channel(
        staged, window_start_hour_here=22, window_end_hour_here=1
    )

    _tick(staged, feed, _NOW)

    schedule = staged.read_state("schedule.json")
    assert schedule is not None
    assert (schedule["window_start_minute_utc"], schedule["window_length_minutes"]) == (
        22 * 60,
        3 * 60,
    )

    # Hours of its own that the owner takes away again leave the account's in force.
    staged.write_state("settings.json", update_settings_document())
    _tick(staged, feed, _NOW + 60)

    schedule = staged.read_state("schedule.json")
    assert schedule is not None
    assert (schedule["window_start_minute_utc"], schedule["window_length_minutes"]) == (
        2 * 60,
        3 * 60,
    )


@pytest.mark.parametrize(
    ("is_enabled", "is_enabled_here", "is_armed"),
    [
        (False, None, False),
        (True, False, False),
        (False, True, True),
    ],
)
def test_arming_follows_the_workspaces_own_answer_and_then_the_accounts(
    staged: StagedUpdateWorkspace,
    is_enabled: bool,
    is_enabled_here: bool | None,
    is_armed: bool,
) -> None:
    feed = _stage_a_workspace_behind_its_channel(
        staged,
        is_auto_update_enabled=is_enabled,
        is_auto_update_enabled_here=is_enabled_here,
    )

    _tick(staged, feed, _NOW)

    assert (staged.read_state("schedule.json") is not None) is is_armed
    # The channel's version is read for display whether or not the workspace arms.
    channel_version = staged.read_state("channel-version.json")
    assert channel_version is not None
    assert channel_version["template_ref"] == _ON_THE_CHANNEL


def test_a_workspace_with_no_update_settings_reads_no_feed_and_arms_nothing(
    staged: StagedUpdateWorkspace,
) -> None:
    stage_checkout_at(staged.root, ("the template at a release", _INSTALLED))
    feed = RecordingFeed(_ON_THE_CHANNEL)

    _tick(staged, feed, _NOW)

    assert feed.asked == []
    assert not (staged.root / "data").exists()


@pytest.mark.parametrize(
    "on_the_channel",
    [_INSTALLED, "minds-v0.8.0"],
    ids=("the-version-installed", "an-older-version"),
)
def test_a_workspace_that_is_not_behind_its_channel_arms_nothing(
    staged: StagedUpdateWorkspace, on_the_channel: str
) -> None:
    feed = _stage_a_workspace_behind_its_channel(staged)
    feed.template_ref = on_the_channel

    _tick(staged, feed, _NOW)

    assert staged.read_state("schedule.json") is None


def test_a_checkout_that_names_no_version_arms_nothing(
    staged: StagedUpdateWorkspace,
) -> None:
    staged.write_state("settings.json", update_settings_document())

    _tick(staged, RecordingFeed(_ON_THE_CHANNEL), _NOW)

    assert staged.read_state("schedule.json") is None


def test_a_version_the_user_skipped_is_not_armed_and_the_next_one_is(
    staged: StagedUpdateWorkspace,
) -> None:
    feed = _stage_a_workspace_behind_its_channel(staged)
    control.skip_target(target_ref=_ON_THE_CHANNEL, skipped_at=_NOW - 60)

    _tick(staged, feed, _NOW)

    assert staged.read_state("schedule.json") is None

    feed.template_ref = "minds-v0.10.1"
    _tick(staged, feed, _NOW + CHANNEL_READ_INTERVAL_SECONDS)

    assert _armed_target(staged) == "minds-v0.10.1"


def test_a_version_a_run_already_went_out_for_is_not_armed_again(
    staged: StagedUpdateWorkspace,
) -> None:
    feed = _stage_a_workspace_behind_its_channel(staged)
    staged.write_state(
        "last-dispatch.json",
        {
            "chat_agent_name": "update-to-v0100",
            "chat_id": "",
            "target_ref": _ON_THE_CHANNEL,
            "dispatched_at": _NOW - 24 * 60 * 60,
        },
    )
    staged.write_state(
        "run.json",
        {
            "chat_agent_name": "update-to-v0100",
            "started_at": _NOW - 24 * 60 * 60 + 60,
            "verdict": "STUCK",
        },
    )

    _tick(staged, feed, _NOW)

    assert staged.read_state("schedule.json") is None

    control.record_dismissals(
        note_run_started_at=None, outcome_run_started_at=_NOW - 24 * 60 * 60 + 60
    )
    _tick(staged, feed, _NOW + 60)

    assert staged.read_state("schedule.json") is None

    feed.template_ref = "minds-v0.10.1"
    _tick(staged, feed, _NOW + CHANNEL_READ_INTERVAL_SECONDS)

    assert _armed_target(staged) == "minds-v0.10.1"


def test_a_decision_waiting_for_the_user_holds_arming_back_until_they_dismiss_it(
    staged: StagedUpdateWorkspace,
) -> None:
    feed = _stage_a_workspace_behind_its_channel(staged)
    run_started_at = _NOW - 2 * 60 * 60
    staged.write_state(
        "run.json",
        {
            "chat_agent_name": "update-by-hand",
            "started_at": run_started_at,
            "verdict": "NEEDS_DECISION",
        },
    )

    _tick(staged, feed, _NOW)

    assert staged.read_state("schedule.json") is None

    control.record_dismissals(
        note_run_started_at=None, outcome_run_started_at=run_started_at
    )
    _tick(staged, feed, _NOW + 60)

    assert _armed_target(staged) == _ON_THE_CHANNEL


def test_a_run_with_no_verdict_holds_arming_back(
    staged: StagedUpdateWorkspace,
) -> None:
    feed = _stage_a_workspace_behind_its_channel(staged)
    staged.write_state(
        "run.json", {"chat_agent_name": "update-by-hand", "started_at": _NOW - 60}
    )

    _tick(staged, feed, _NOW)

    assert staged.read_state("schedule.json") is None


def test_a_channel_file_that_cannot_be_read_arms_nothing(
    staged: StagedUpdateWorkspace,
) -> None:
    feed = _stage_a_workspace_behind_its_channel(staged)
    feed.template_ref = None

    _tick(staged, feed, _NOW)

    assert staged.read_state("schedule.json") is None
    channel_version = staged.read_state("channel-version.json")
    assert channel_version is not None
    assert (channel_version["template_ref"], channel_version["error"]) == (
        "",
        "the feed is down",
    )


def test_a_read_that_fails_keeps_the_last_version_for_display_and_arms_nothing_on_it(
    staged: StagedUpdateWorkspace,
) -> None:
    """The version read an hour ago is skipped, so nothing is armed then; once the feed is down, the skip's
    removal still arms nothing, because the last read is not one that succeeded."""
    feed = _stage_a_workspace_behind_its_channel(staged)
    control.skip_target(target_ref=_ON_THE_CHANNEL, skipped_at=_NOW - 60)
    _tick(staged, feed, _NOW)
    staged.state_path("skipped-target.json").unlink()
    feed.template_ref = None
    later = _NOW + CHANNEL_READ_INTERVAL_SECONDS

    _tick(staged, feed, later)

    assert staged.read_state("schedule.json") is None
    channel_version = staged.read_state("channel-version.json")
    assert channel_version is not None
    assert (
        channel_version["template_ref"],
        channel_version["read_at"],
        channel_version["checked_at"],
        channel_version["error"],
    ) == (_ON_THE_CHANNEL, _NOW, later, "the feed is down")


def test_the_feed_is_read_at_most_once_an_hour_however_often_the_minute_runs(
    staged: StagedUpdateWorkspace,
) -> None:
    feed = _stage_a_workspace_behind_its_channel(staged, is_auto_update_enabled=False)

    for minute in range(60):
        _tick(staged, feed, _NOW + minute * 60)

    assert len(feed.asked) == 1

    _tick(staged, feed, _NOW + CHANNEL_READ_INTERVAL_SECONDS)

    assert len(feed.asked) == 2


def test_the_checkouts_version_is_read_once_while_the_run_record_stands_still(
    staged: StagedUpdateWorkspace,
) -> None:
    stage_checkout_at(staged.root, ("the template at a release", _INSTALLED))

    assert read_installed_version(_NOW) == _INSTALLED

    stage_checkout_at(
        staged.root, ("update-self: merge upstream template (minds-v0.10.0)", "")
    )

    # The checkout moved without a run saying so, which is read again only once the hour is out.
    assert read_installed_version(_NOW + 60) == _INSTALLED
    assert read_installed_version(_NOW + 60 * 60) == "minds-v0.10.0"


def test_a_read_of_the_checkouts_version_that_git_could_not_make_is_not_kept(
    staged: StagedUpdateWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage_checkout_at(staged.root, ("the template at a release", _INSTALLED))
    path_with_git = os.environ["PATH"]
    # The stub tools alone, so there is no git to run.
    monkeypatch.setenv("PATH", str(staged.bin_dir))

    assert read_installed_version(_NOW) == ""
    assert staged.read_state("installed-version.json") is None

    monkeypatch.setenv("PATH", path_with_git)

    assert read_installed_version(_NOW + 60) == _INSTALLED


def test_the_checkouts_version_is_read_again_once_a_run_records_a_verdict(
    staged: StagedUpdateWorkspace,
) -> None:
    stage_checkout_at(staged.root, ("the template at a release", _INSTALLED))
    staged.write_state("run.json", {"chat_agent_name": "update", "started_at": _NOW})
    assert read_installed_version(_NOW) == _INSTALLED
    stage_checkout_at(
        staged.root, ("update-self: merge upstream template (minds-v0.10.0)", "")
    )

    staged.write_state(
        "run.json",
        {"chat_agent_name": "update", "started_at": _NOW, "verdict": "UPDATED"},
    )

    assert read_installed_version(_NOW + 60) == "minds-v0.10.0"


def test_a_self_armed_update_starts_inside_the_window_and_not_outside_it(
    staged: StagedUpdateWorkspace,
) -> None:
    feed = _stage_a_workspace_behind_its_channel(staged)
    _tick(staged, feed, _OUTSIDE_THE_WINDOW)

    staged.tick(_OUTSIDE_THE_WINDOW + 60)

    assert staged.chat_creates() == []

    staged.tick(_INSIDE_THE_WINDOW)

    creates = staged.chat_creates()
    assert len(creates) == 1
    assert f"- Target: {_ON_THE_CHANNEL}" in creates[0][-1]
    dispatch = staged.read_state("last-dispatch.json")
    assert dispatch is not None
    assert (dispatch["target_ref"], dispatch["started_by"]) == (
        _ON_THE_CHANNEL,
        "scheduled",
    )


def test_a_self_armed_schedule_follows_the_channel_and_the_settings(
    staged: StagedUpdateWorkspace,
) -> None:
    feed = _stage_a_workspace_behind_its_channel(staged)
    _tick(staged, feed, _NOW)
    armed = staged.read_state("schedule.json")
    assert armed is not None
    armed_at = armed["armed_at"]
    feed.template_ref = "minds-v0.10.1"
    staged.write_state(
        "settings.json",
        update_settings_document(window_start_hour=22, window_end_hour=23),
    )

    _tick(staged, feed, _NOW + CHANNEL_READ_INTERVAL_SECONDS)

    schedule = staged.read_state("schedule.json")
    assert schedule is not None
    assert (
        schedule["ceiling"],
        schedule["window_start_minute_utc"],
        schedule["window_length_minutes"],
        schedule["armed_at"],
        schedule["is_auto_armed"],
    ) == ("minds-v0.10.1", 22 * 60, 60, armed_at, True)


def test_a_schedule_a_client_armed_with_its_own_values_is_left_to_that_client(
    staged: StagedUpdateWorkspace,
) -> None:
    feed = _stage_a_workspace_behind_its_channel(staged)
    armed = control.arm_schedule(
        ceiling="minds-v0.9.5",
        override_ref="",
        window_start_minute_utc=9 * 60,
        window_length_minutes=3 * 60,
        is_backup_configured=True,
        is_auto_armed=False,
        now=_NOW - CLIENT_CEILING_HOLD_SECONDS,
        live_cron_dir=str(staged.live_cron_dir),
        agent_environment=AgentEnvironment(),
        fetch_channel_file=feed,
    )["schedule"]

    _tick(staged, feed, _NOW)

    assert staged.read_state("schedule.json") == armed
    assert staged.read_state("schedule-origin.json") is None


def _skip(staged: StagedUpdateWorkspace, target_ref: str) -> None:
    control.skip_target(target_ref=target_ref, skipped_at=_NOW - 60)


def _fail_a_run_to(staged: StagedUpdateWorkspace, target_ref: str) -> None:
    """A run that went out for ``target_ref`` a day ago and ended stuck."""
    staged.write_state(
        "last-dispatch.json",
        {
            "chat_agent_name": "update-that-failed",
            "chat_id": "",
            "target_ref": target_ref,
            "dispatched_at": _NOW - 24 * 60 * 60,
        },
    )
    staged.write_state(
        "run.json",
        {
            "chat_agent_name": "update-that-failed",
            "started_at": _NOW - 24 * 60 * 60 + 60,
            "verdict": "STUCK",
        },
    )


@pytest.mark.parametrize(
    "hold_back", [_skip, _fail_a_run_to], ids=["skipped", "already-tried"]
)
def test_a_self_armed_schedule_is_cancelled_when_its_channel_moves_to_a_version_the_workspace_does_not_arm(
    staged: StagedUpdateWorkspace,
    hold_back: Callable[[StagedUpdateWorkspace, str], None],
) -> None:
    feed = _stage_a_workspace_behind_its_channel(staged)
    hold_back(staged, "minds-v0.10.1")
    _tick(staged, feed, _NOW)
    assert _armed_target(staged) == _ON_THE_CHANNEL

    feed.template_ref = "minds-v0.10.1"
    _tick(staged, feed, _NOW + CHANNEL_READ_INTERVAL_SECONDS)

    assert staged.read_state("schedule.json") is None
    assert staged.read_state("schedule-origin.json") is None
    assert any(
        "minds-v0.10.1" in line and "cancelled" in line
        for line in staged.dispatcher_log_lines()
    )

    _tick(staged, feed, _NOW + CHANNEL_READ_INTERVAL_SECONDS + 60)

    assert staged.read_state("schedule.json") is None


def test_a_self_armed_schedule_follows_its_channel_past_a_version_skipped_earlier(
    staged: StagedUpdateWorkspace,
) -> None:
    feed = _stage_a_workspace_behind_its_channel(staged)
    _skip(staged, "minds-v0.9.9")
    _tick(staged, feed, _NOW)

    feed.template_ref = "minds-v0.10.1"
    _tick(staged, feed, _NOW + CHANNEL_READ_INTERVAL_SECONDS)

    assert _armed_target(staged) == "minds-v0.10.1"


def test_a_schedule_a_client_armed_follows_the_channel_to_a_version_the_workspace_would_not_arm(
    staged: StagedUpdateWorkspace,
) -> None:
    feed = _stage_a_workspace_behind_its_channel(staged)
    _skip(staged, "minds-v0.10.1")
    control.arm_schedule(
        ceiling=None,
        override_ref="",
        window_start_minute_utc=None,
        window_length_minutes=None,
        is_backup_configured=None,
        is_auto_armed=False,
        now=_NOW,
        live_cron_dir=str(staged.live_cron_dir),
        agent_environment=AgentEnvironment(),
        fetch_channel_file=feed,
    )
    assert _armed_target(staged) == _ON_THE_CHANNEL

    feed.template_ref = "minds-v0.10.1"
    _tick(staged, feed, _NOW + CHANNEL_READ_INTERVAL_SECONDS)

    schedule = staged.read_state("schedule.json")
    assert schedule is not None
    assert (schedule["ceiling"], schedule["is_auto_armed"]) == ("minds-v0.10.1", False)


def _sweep_as_a_client_that_names_its_version(
    staged: StagedUpdateWorkspace, feed: RecordingFeed, now: float
) -> dict[str, Any] | None:
    """The refresh a desktop's sweep sends: its newest version, its window, and its backup flag; the schedule
    as the refresh left it."""
    answer: dict[str, Any] = control.refresh_schedule(
        ceiling="minds-v0.9.5",
        window_start_minute_utc=9 * 60,
        window_length_minutes=3 * 60,
        is_backup_configured=True,
        now=now,
        live_cron_dir=str(staged.live_cron_dir),
        fetch_channel_file=feed,
    )
    return answer["schedule"]


def test_a_client_that_names_its_newest_version_holds_the_workspace_to_it_while_it_keeps_asking(
    staged: StagedUpdateWorkspace,
) -> None:
    """A desktop from before the workspace armed its own updates names its newest version in every request."""
    feed = _stage_a_workspace_behind_its_channel(staged)
    _sweep_as_a_client_that_names_its_version(staged, feed, _NOW)

    _tick(staged, feed, _NOW + 60)

    assert _armed_target(staged) == "minds-v0.9.5"

    # That client's sweep refreshes the schedule with its own values, which the dispatcher then leaves alone.
    swept_at = _NOW + 300
    refreshed = _sweep_as_a_client_that_names_its_version(staged, feed, swept_at)
    _tick(staged, feed, swept_at + 60)

    assert refreshed is not None
    assert staged.read_state("schedule.json") == refreshed
    assert refreshed["window_start_minute_utc"] == 9 * 60

    # Once it stops asking, the workspace follows its channel and its own settings again.
    _tick(staged, feed, swept_at + CLIENT_CEILING_HOLD_SECONDS)

    schedule = staged.read_state("schedule.json")
    assert schedule is not None
    assert (schedule["ceiling"], schedule["window_start_minute_utc"]) == (
        _ON_THE_CHANNEL,
        2 * 60,
    )


def test_a_client_whose_newest_version_is_the_one_installed_holds_the_workspace_where_it_is(
    staged: StagedUpdateWorkspace,
) -> None:
    feed = _stage_a_workspace_behind_its_channel(staged)
    control.start_update(
        ceiling=_INSTALLED,
        override_ref="",
        is_backup_configured=True,
        now=_NOW,
        live_cron_dir=str(staged.live_cron_dir),
        proc_root=str(staged.proc_root),
        agent_environment=AgentEnvironment(),
        read_agents=staged.read_listed_agents,
        run_in_background=lambda work: None,
        clock=lambda: _NOW,
        fetch_channel_file=feed,
    )
    staged.state_path("last-start.json").unlink()

    _tick(staged, feed, _NOW + CLIENT_CEILING_HOLD_SECONDS - 60)

    assert staged.read_state("schedule.json") is None

    _tick(staged, feed, _NOW + CLIENT_CEILING_HOLD_SECONDS)

    assert _armed_target(staged) == _ON_THE_CHANNEL


def test_a_client_that_names_no_release_holds_arming_back_altogether(
    staged: StagedUpdateWorkspace,
) -> None:
    feed = _stage_a_workspace_behind_its_channel(staged)
    control.refresh_schedule(
        ceiling="main",
        window_start_minute_utc=9 * 60,
        window_length_minutes=3 * 60,
        is_backup_configured=True,
        now=_NOW,
        live_cron_dir=str(staged.live_cron_dir),
        fetch_channel_file=feed,
    )

    _tick(staged, feed, _NOW + 60)

    assert staged.read_state("schedule.json") is None


def test_a_workspace_with_no_account_signed_in_arms_nothing_and_reads_git_once(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    staged = _stage_signed_out(tmp_path_factory, monkeypatch)
    feed = _stage_a_workspace_behind_its_channel(staged)

    _tick(staged, feed, _NOW)
    _tick(staged, feed, _NOW + 60)

    assert staged.read_state("schedule.json") is None
    installed = staged.read_state("installed-version.json")
    assert installed is not None
    assert (installed["template_ref"], installed["read_at"]) == (_INSTALLED, _NOW)


def _stage_signed_out(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> StagedUpdateWorkspace:
    return stage_update_workspace(
        tmp_path_factory.mktemp("signed-out"), monkeypatch, is_signed_in=False
    )
