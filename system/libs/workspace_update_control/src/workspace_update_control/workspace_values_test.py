"""The update operations asked for by a client that leaves the version, the window or the backup flag to the workspace."""

from typing import Any

import pytest

from workspace_update_control import control
from workspace_update_control.channel import CHANNEL_READ_INTERVAL_SECONDS
from workspace_update_control.dispatcher import AgentEnvironment
from workspace_update_control.protocol import NO_BACKUP_NOTE
from workspace_update_control.testing import (
    STAGED_FEED_BASE_URL,
    RecordingFeed,
    StagedUpdateWorkspace,
    run_in_place,
    update_settings_document,
)

_NOW = 1_792_000_000.0
_ON_THE_CHANNEL = "minds-v0.10.0"


def _arm(
    staged: StagedUpdateWorkspace,
    feed: RecordingFeed,
    *,
    ceiling: str | None = None,
    override_ref: str = "",
    window: tuple[int, int] | None = None,
    is_backup_configured: bool | None = None,
    now: float = _NOW,
) -> dict[str, Any]:
    return control.arm_schedule(
        ceiling=ceiling,
        override_ref=override_ref,
        window_start_minute_utc=window[0] if window is not None else None,
        window_length_minutes=window[1] if window is not None else None,
        is_backup_configured=is_backup_configured,
        is_auto_armed=False,
        now=now,
        live_cron_dir=str(staged.live_cron_dir),
        agent_environment=AgentEnvironment(),
        fetch_channel_file=feed,
    )


def _start(
    staged: StagedUpdateWorkspace,
    feed: RecordingFeed,
    *,
    ceiling: str | None = None,
    override_ref: str = "",
    is_backup_configured: bool | None = None,
) -> dict[str, Any]:
    return control.start_update(
        ceiling=ceiling,
        override_ref=override_ref,
        is_backup_configured=is_backup_configured,
        now=_NOW,
        live_cron_dir=str(staged.live_cron_dir),
        proc_root=str(staged.proc_root),
        agent_environment=AgentEnvironment(),
        read_agents=staged.read_listed_agents,
        run_in_background=run_in_place,
        clock=lambda: _NOW + 7.0,
        fetch_channel_file=feed,
    )


def _refresh(
    staged: StagedUpdateWorkspace,
    feed: RecordingFeed,
    *,
    ceiling: str | None = None,
    window: tuple[int, int] | None = None,
    is_backup_configured: bool | None = None,
    now: float = _NOW,
) -> dict[str, Any]:
    return control.refresh_schedule(
        ceiling=ceiling,
        window_start_minute_utc=window[0] if window is not None else None,
        window_length_minutes=window[1] if window is not None else None,
        is_backup_configured=is_backup_configured,
        now=now,
        live_cron_dir=str(staged.live_cron_dir),
        fetch_channel_file=feed,
    )


def _write_backup_credentials(staged: StagedUpdateWorkspace) -> None:
    (staged.root / "data" / ".secrets").mkdir(parents=True, exist_ok=True)
    (staged.root / "data" / ".secrets" / "restic.env").write_text(
        "RESTIC_REPOSITORY=x\n"
    )


def test_arming_with_nothing_named_takes_the_channels_version_the_settings_window_and_the_workspaces_own_backups(
    staged: StagedUpdateWorkspace,
) -> None:
    staged.write_state("settings.json", update_settings_document())
    _write_backup_credentials(staged)
    feed = RecordingFeed(_ON_THE_CHANNEL)

    answer = _arm(staged, feed)

    assert answer["outcome"] == "ARMED"
    schedule = answer["schedule"]
    assert (
        schedule["ceiling"],
        schedule["window_start_minute_utc"],
        schedule["window_length_minutes"],
        schedule["is_backup_configured"],
        schedule["is_auto_armed"],
    ) == (_ON_THE_CHANNEL, 2 * 60, 3 * 60, True, False)
    assert feed.asked == [f"{STAGED_FEED_BASE_URL}/stable-web.json"]
    assert staged.read_state("schedule-origin.json") == {
        "schema": 1,
        "armed_at": _NOW,
        "is_ceiling_followed": True,
        "is_window_followed": True,
        "is_backup_configured_followed": True,
    }
    # Only a client that names its newest version is recorded as one that holds the workspace to it.
    assert staged.read_state("client-ceiling.json") is None


def test_arming_with_every_value_named_is_served_as_before_and_records_who_named_the_version(
    staged: StagedUpdateWorkspace,
) -> None:
    staged.write_state("settings.json", update_settings_document())
    feed = RecordingFeed(_ON_THE_CHANNEL)

    answer = _arm(
        staged,
        feed,
        ceiling="minds-v0.9.5",
        window=(9 * 60, 3 * 60),
        is_backup_configured=True,
    )

    schedule = answer["schedule"]
    assert (
        schedule["ceiling"],
        schedule["window_start_minute_utc"],
        schedule["is_backup_configured"],
    ) == ("minds-v0.9.5", 9 * 60, True)
    assert feed.asked == []
    assert staged.read_state("schedule-origin.json") is None
    assert staged.read_state("client-ceiling.json") == {
        "schema": 1,
        "ceiling": "minds-v0.9.5",
        "asserted_at": _NOW,
    }


def test_arming_with_no_version_named_in_a_workspace_with_no_settings_says_so_and_arms_nothing(
    staged: StagedUpdateWorkspace,
) -> None:
    answer = _arm(staged, RecordingFeed(_ON_THE_CHANNEL), window=(9 * 60, 3 * 60))

    assert answer["outcome"] == "NO_CHANNEL_VERSION"
    assert "holds no update settings" in answer["detail"]
    assert answer["schedule"] is None
    assert staged.read_state("schedule.json") is None


def test_arming_with_no_version_named_when_the_feed_was_never_read_says_why_and_invents_none(
    staged: StagedUpdateWorkspace,
) -> None:
    staged.write_state("settings.json", update_settings_document())

    answer = _arm(staged, RecordingFeed(None))

    assert answer["outcome"] == "NO_CHANNEL_VERSION"
    assert (
        "the version of the stable channel could not be read (the feed is down)"
        in (answer["detail"])
    )
    assert staged.read_state("schedule.json") is None


def test_arming_with_no_version_named_takes_the_last_version_read_when_the_feed_is_down(
    staged: StagedUpdateWorkspace,
) -> None:
    staged.write_state("settings.json", update_settings_document())
    feed = RecordingFeed(_ON_THE_CHANNEL)
    _arm(staged, feed)
    control.cancel_schedule()
    feed.template_ref = None

    answer = _arm(staged, feed, now=_NOW + CHANNEL_READ_INTERVAL_SECONDS)

    assert answer["outcome"] == "ARMED"
    assert answer["schedule"]["ceiling"] == _ON_THE_CHANNEL


def test_a_version_the_user_named_is_armed_with_no_channel_to_read(
    staged: StagedUpdateWorkspace,
) -> None:
    answer = _arm(
        staged, RecordingFeed(None), override_ref="minds-v0.9.7", window=(9 * 60, 60)
    )

    assert answer["outcome"] == "ARMED"
    schedule = answer["schedule"]
    assert (schedule["override_ref"], schedule["ceiling"]) == (
        "minds-v0.9.7",
        "minds-v0.9.7",
    )
    # The version is the user's and stays theirs: no channel is followed.
    origin = staged.read_state("schedule-origin.json")
    assert origin is not None
    assert origin["is_ceiling_followed"] is False


def test_arming_with_no_window_named_in_a_workspace_with_no_settings_says_so(
    staged: StagedUpdateWorkspace,
) -> None:
    answer = _arm(staged, RecordingFeed(None), ceiling="minds-v0.9.5")

    assert answer["outcome"] == "NO_UPDATE_SETTINGS"
    assert staged.read_state("schedule.json") is None


def test_a_version_left_to_the_workspace_is_held_to_the_one_a_client_still_asking_named(
    staged: StagedUpdateWorkspace,
) -> None:
    staged.write_state("settings.json", update_settings_document())
    feed = RecordingFeed(_ON_THE_CHANNEL)
    _refresh(staged, feed, ceiling="minds-v0.9.5", window=(9 * 60, 60))

    answer = _arm(staged, feed, now=_NOW + 60)

    assert answer["schedule"]["ceiling"] == "minds-v0.9.5"


def test_cancelling_forgets_which_values_were_the_workspaces(
    staged: StagedUpdateWorkspace,
) -> None:
    staged.write_state("settings.json", update_settings_document())
    _arm(staged, RecordingFeed(_ON_THE_CHANNEL))

    assert control.cancel_schedule() == {"is_cancelled": True}

    assert staged.read_state("schedule-origin.json") is None


def test_starting_with_no_version_named_updates_to_the_channels_version(
    staged: StagedUpdateWorkspace,
) -> None:
    staged.write_state("settings.json", update_settings_document())
    _write_backup_credentials(staged)

    answer = _start(staged, RecordingFeed(_ON_THE_CHANNEL))

    assert answer["outcome"] == "STARTING"
    message = staged.chat_creates()[0][-1]
    assert f"- Target: {_ON_THE_CHANNEL}\n- Ceiling: {_ON_THE_CHANNEL}" in message
    assert NO_BACKUP_NOTE not in message
    dispatch = staged.read_state("last-dispatch.json")
    assert dispatch is not None
    assert (dispatch["target_ref"], dispatch["ceiling"]) == (
        _ON_THE_CHANNEL,
        _ON_THE_CHANNEL,
    )


def test_starting_with_no_backup_flag_named_looks_for_the_workspaces_own_credentials(
    staged: StagedUpdateWorkspace,
) -> None:
    _start(staged, RecordingFeed(None), ceiling="minds-v0.9.5")

    assert NO_BACKUP_NOTE in staged.chat_creates()[0][-1]


def test_starting_with_no_version_named_and_none_read_says_so_and_starts_nothing(
    staged: StagedUpdateWorkspace,
) -> None:
    staged.write_state("settings.json", update_settings_document(feed_base_url=""))

    answer = _start(staged, RecordingFeed(_ON_THE_CHANNEL))

    assert answer["outcome"] == "NO_CHANNEL_VERSION"
    assert "name no release feed" in answer["detail"]
    assert staged.chat_creates() == []
    assert staged.read_state("last-start.json") is None


def test_starting_at_a_version_the_user_named_needs_no_channel(
    staged: StagedUpdateWorkspace,
) -> None:
    answer = _start(staged, RecordingFeed(None), override_ref="minds-v0.9.7")

    assert answer["outcome"] == "STARTING"
    message = staged.chat_creates()[0][-1]
    assert "- Target: minds-v0.9.7\n- Ceiling: minds-v0.9.7" in message


def test_a_refresh_with_nothing_named_rewrites_the_schedule_with_the_workspaces_own_values(
    staged: StagedUpdateWorkspace,
) -> None:
    staged.write_state("settings.json", update_settings_document())
    feed = RecordingFeed(_ON_THE_CHANNEL)
    armed = _arm(
        staged,
        feed,
        ceiling="minds-v0.9.5",
        window=(9 * 60, 60),
        is_backup_configured=True,
    )["schedule"]

    answer = _refresh(staged, feed, now=_NOW + 24 * 60 * 60)

    assert answer == {
        "is_armed": True,
        "schedule": {
            **armed,
            "ceiling": _ON_THE_CHANNEL,
            "window_start_minute_utc": 2 * 60,
            "window_length_minutes": 3 * 60,
            "is_backup_configured": False,
        },
    }
    assert staged.read_state("schedule.json") == answer["schedule"]


def test_a_refresh_with_nothing_named_leaves_what_the_workspace_has_no_value_for(
    staged: StagedUpdateWorkspace,
) -> None:
    feed = RecordingFeed(None)
    armed = _arm(
        staged,
        feed,
        ceiling="minds-v0.9.5",
        window=(9 * 60, 60),
        is_backup_configured=False,
    )["schedule"]

    answer = _refresh(staged, feed, now=_NOW + 24 * 60 * 60)

    assert answer == {"is_armed": True, "schedule": armed}


@pytest.mark.parametrize("is_feed_up", (True, False))
def test_reading_answers_the_settings_and_the_channels_version_as_last_read_and_asks_no_feed(
    staged: StagedUpdateWorkspace, is_feed_up: bool
) -> None:
    settings = update_settings_document(is_auto_update_enabled_here=False)
    staged.write_state("settings.json", settings)
    feed = RecordingFeed(_ON_THE_CHANNEL if is_feed_up else None)
    _arm(staged, feed)
    state_dir = staged.state_path("")
    before = {path.name: path.read_bytes() for path in state_dir.iterdir()}

    answer = control.read_update_state(
        is_sign_in_checked=False,
        proc_root=str(staged.proc_root),
        agent_environment=AgentEnvironment(),
        read_agents=staged.read_listed_agents,
    )

    assert answer["settings"] == settings
    assert answer["channel_version"] == {
        "schema": 1,
        "channel": "stable",
        "feed_base_url": STAGED_FEED_BASE_URL,
        "template_ref": _ON_THE_CHANNEL if is_feed_up else "",
        "read_at": _NOW if is_feed_up else None,
        "checked_at": _NOW,
        "error": "" if is_feed_up else "the feed is down",
    }
    assert len(feed.asked) == 1
    assert {path.name: path.read_bytes() for path in state_dir.iterdir()} == before
