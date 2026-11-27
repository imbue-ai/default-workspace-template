from datetime import timezone
from zoneinfo import ZoneInfo

import pytest

from workspace_update_control.settings import (
    InvalidUpdateSettingsError,
    UpdateSettings,
    is_backup_configured,
    read_update_settings,
    read_user_zone,
    settings_window,
    take_update_settings,
    utc_update_window,
    write_update_settings,
)
from workspace_update_control.testing import (
    StagedUpdateWorkspace,
    epoch_at,
    the_accounts_settings,
    update_settings_document,
)


def _settings(**fields: object) -> UpdateSettings:
    return UpdateSettings(**update_settings_document(**fields))


def test_settings_are_stored_in_a_file_of_their_own_and_read_back_as_written(
    staged: StagedUpdateWorkspace,
) -> None:
    settings = _settings(channel="beta", is_auto_update_enabled_here=False)

    assert write_update_settings(settings) == settings

    assert staged.read_state("settings.json") == {
        "schema": 1,
        "channel": "beta",
        "feed_base_url": "https://feed.example/updates",
        "is_auto_update_enabled": True,
        "is_auto_update_enabled_here": False,
        "window_start_hour": 2,
        "window_end_hour": 5,
        "window_start_hour_here": None,
        "window_end_hour_here": None,
    }
    assert read_update_settings() == settings
    assert staged.read_state("schedule.json") is None


def test_settings_that_did_not_change_are_not_written_again(
    staged: StagedUpdateWorkspace,
) -> None:
    write_update_settings(_settings())
    written = staged.state_path("settings.json").stat()

    write_update_settings(_settings())

    assert staged.state_path("settings.json").stat().st_ino == written.st_ino


@pytest.mark.parametrize(
    "fields",
    [
        {"channel": ""},
        {"channel": "../stable"},
        {"channel": "Stable"},
        {"feed_base_url": "file:///etc/passwd"},
        {"feed_base_url": "https://feed.example/a b"},
        {"window_start_hour": 24},
        {"window_end_hour": -1},
        {"window_start_hour": 3, "window_end_hour": 3},
        # The workspace's own hours come as a pair, and are a window as the account's are.
        {"window_start_hour_here": 22},
        {"window_end_hour_here": 4},
        {"window_start_hour_here": 24, "window_end_hour_here": 4},
        {"window_start_hour_here": 4, "window_end_hour_here": 4},
        {"window_start_hour_here": True, "window_end_hour_here": 4},
    ],
)
def test_settings_that_cannot_be_the_workspaces_are_refused_and_not_stored(
    staged: StagedUpdateWorkspace, fields: dict[str, object]
) -> None:
    with pytest.raises(InvalidUpdateSettingsError):
        write_update_settings(_settings(**fields))

    assert staged.read_state("settings.json") is None


def test_a_tier_that_publishes_no_feed_is_stored_with_an_empty_address(
    staged: StagedUpdateWorkspace,
) -> None:
    assert write_update_settings(_settings(feed_base_url="")).feed_base_url == ""


def test_a_workspace_no_client_wrote_settings_into_has_none(
    staged: StagedUpdateWorkspace,
) -> None:
    assert read_update_settings() is None


@pytest.mark.parametrize(
    "stored",
    [
        {"channel": "stable"},
        update_settings_document(is_auto_update_enabled="yes"),
        update_settings_document(window_start_hour=30),
    ],
)
def test_a_settings_file_that_does_not_hold_usable_settings_reads_as_none(
    staged: StagedUpdateWorkspace, stored: dict[str, object]
) -> None:
    staged.write_state("settings.json", stored)

    assert read_update_settings() is None


@pytest.mark.parametrize(
    ("is_enabled", "is_enabled_here", "is_arming"),
    [
        (True, None, True),
        (False, None, False),
        # The workspace's own answer wins over the account's, either way.
        (True, False, False),
        (False, True, True),
    ],
)
def test_the_workspaces_own_answer_wins_over_the_accounts(
    is_enabled: bool, is_enabled_here: bool | None, is_arming: bool
) -> None:
    settings = _settings(
        is_auto_update_enabled=is_enabled, is_auto_update_enabled_here=is_enabled_here
    )

    assert settings.is_arming_enabled is is_arming


@pytest.mark.parametrize(
    ("now", "zone", "start_hour", "end_hour", "expected"),
    [
        ("2026-10-20T12:00:00+00:00", timezone.utc, 2, 5, (2 * 60, 3 * 60)),
        # 02:00 in Los Angeles is 09:00 UTC in summer and 10:00 UTC in winter.
        (
            "2026-10-20T12:00:00+00:00",
            ZoneInfo("America/Los_Angeles"),
            2,
            5,
            (9 * 60, 3 * 60),
        ),
        (
            "2026-12-20T12:00:00+00:00",
            ZoneInfo("America/Los_Angeles"),
            2,
            5,
            (10 * 60, 3 * 60),
        ),
        # A half-hour zone starts off the hour in UTC.
        (
            "2026-10-20T12:00:00+00:00",
            ZoneInfo("Asia/Kolkata"),
            2,
            5,
            (20 * 60 + 30, 3 * 60),
        ),
        # An end before the start is the next day's.
        ("2026-10-20T12:00:00+00:00", timezone.utc, 22, 2, (22 * 60, 4 * 60)),
    ],
)
def test_the_local_hours_become_a_utc_start_minute_and_a_length(
    now: str,
    zone: timezone | ZoneInfo,
    start_hour: int,
    end_hour: int,
    expected: tuple[int, int],
) -> None:
    assert utc_update_window(start_hour, end_hour, zone, epoch_at(now)) == expected


def test_the_window_in_force_is_the_workspaces_own_where_it_has_one(
    staged: StagedUpdateWorkspace,
) -> None:
    now = epoch_at("2026-10-20T12:00:00+00:00")
    of_the_account = _settings()
    of_the_workspace = _settings(window_start_hour_here=22, window_end_hour_here=23)

    assert of_the_account.window_hours == (2, 5)
    assert of_the_workspace.window_hours == (22, 23)
    assert settings_window(of_the_account, now) == (2 * 60, 3 * 60)
    assert settings_window(of_the_workspace, now) == (22 * 60, 60)

    write_update_settings(of_the_workspace)

    assert read_update_settings() == of_the_workspace


def test_a_settings_file_from_before_the_workspaces_own_hours_reads_with_none(
    staged: StagedUpdateWorkspace,
) -> None:
    staged.write_state(
        "settings.json",
        {
            "schema": 1,
            "channel": "stable",
            "feed_base_url": "",
            "is_auto_update_enabled": True,
            "is_auto_update_enabled_here": False,
            "window_start_hour": 2,
            "window_end_hour": 5,
        },
    )

    stored = read_update_settings()

    assert stored is not None
    assert (stored.window_start_hour_here, stored.window_end_hour_here) == (None, None)
    assert stored.is_auto_update_enabled_here is False


def test_a_setting_of_the_workspaces_own_that_a_client_leaves_out_keeps_its_stored_value(
    staged: StagedUpdateWorkspace,
) -> None:
    take_update_settings(
        update_settings_document(
            is_auto_update_enabled_here=False,
            window_start_hour_here=22,
            window_end_hour_here=23,
        )
    )

    stored = take_update_settings(
        the_accounts_settings(channel="beta", window_start_hour=1)
    )

    assert stored == _settings(
        channel="beta",
        window_start_hour=1,
        is_auto_update_enabled_here=False,
        window_start_hour_here=22,
        window_end_hour_here=23,
    )
    assert read_update_settings() == stored


def test_a_setting_of_the_workspaces_own_sent_as_null_is_cleared_and_the_others_are_kept(
    staged: StagedUpdateWorkspace,
) -> None:
    take_update_settings(
        update_settings_document(
            is_auto_update_enabled_here=False,
            window_start_hour_here=22,
            window_end_hour_here=23,
        )
    )

    stored = take_update_settings(
        {
            **the_accounts_settings(),
            "window_start_hour_here": None,
            "window_end_hour_here": None,
        }
    )

    assert (stored.window_start_hour_here, stored.window_end_hour_here) == (None, None)
    assert stored.is_auto_update_enabled_here is False
    assert stored.window_hours == (2, 5)

    assert (
        take_update_settings(
            {**the_accounts_settings(), "is_auto_update_enabled_here": None}
        ).is_auto_update_enabled_here
        is None
    )


def test_a_workspace_with_nothing_stored_takes_the_accounts_settings_with_none_of_its_own(
    staged: StagedUpdateWorkspace,
) -> None:
    assert take_update_settings(the_accounts_settings()) == _settings()


def test_one_of_the_workspaces_own_hours_sent_alone_is_held_to_the_stored_other(
    staged: StagedUpdateWorkspace,
) -> None:
    with pytest.raises(InvalidUpdateSettingsError):
        take_update_settings({**the_accounts_settings(), "window_start_hour_here": 22})
    assert read_update_settings() is None

    take_update_settings(
        update_settings_document(window_start_hour_here=22, window_end_hour_here=23)
    )

    assert take_update_settings(
        {**the_accounts_settings(), "window_end_hour_here": 1}
    ).window_hours == (22, 1)


def test_settings_that_leave_out_one_that_is_not_the_workspaces_own_are_refused(
    staged: StagedUpdateWorkspace,
) -> None:
    sent = the_accounts_settings()
    del sent["feed_base_url"]

    with pytest.raises(InvalidUpdateSettingsError, match="feed_base_url"):
        take_update_settings(sent)

    assert read_update_settings() is None


@pytest.mark.parametrize(
    "fields",
    [
        {"channel": 7},
        {"feed_base_url": None},
        {"is_auto_update_enabled": "yes"},
        {"is_auto_update_enabled_here": 1},
        {"window_start_hour": "2"},
        {"window_end_hour": 5.0},
        {"window_start_hour": True},
        {"window_start_hour_here": "22", "window_end_hour_here": 23},
    ],
)
def test_a_setting_that_is_not_of_its_kind_is_refused_and_nothing_is_stored(
    staged: StagedUpdateWorkspace, fields: dict[str, object]
) -> None:
    # The initial data hands a document here that no route's request model has checked.
    with pytest.raises(InvalidUpdateSettingsError):
        take_update_settings(update_settings_document(**fields))

    assert read_update_settings() is None


def test_the_window_is_read_in_the_recorded_timezone_and_in_utc_when_none_is_recorded(
    staged: StagedUpdateWorkspace,
) -> None:
    now = epoch_at("2026-10-20T12:00:00+00:00")

    assert read_user_zone() is timezone.utc
    assert settings_window(_settings(), now) == (2 * 60, 3 * 60)

    (staged.root / "data" / ".state").mkdir(parents=True, exist_ok=True)
    (staged.root / "data" / ".state" / "user_timezone").write_text(
        "America/Los_Angeles\n"
    )

    assert settings_window(_settings(), now) == (9 * 60, 3 * 60)


def test_a_recorded_timezone_this_machine_does_not_know_reads_as_utc(
    staged: StagedUpdateWorkspace,
) -> None:
    (staged.root / "data" / ".state").mkdir(parents=True, exist_ok=True)
    (staged.root / "data" / ".state" / "user_timezone").write_text("Mars/Olympus\n")

    assert read_user_zone() is timezone.utc


def test_the_workspace_has_backups_when_it_holds_backup_credentials(
    staged: StagedUpdateWorkspace,
) -> None:
    assert is_backup_configured() is False

    staged.write_backup_credentials()

    assert is_backup_configured() is True
