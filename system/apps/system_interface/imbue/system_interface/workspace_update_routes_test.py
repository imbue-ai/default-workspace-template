import time
from pathlib import Path

import pytest
from flask import Flask
from flask.testing import FlaskClient
from workspace_update_control.control import TICK_ENTRY_PATH
from workspace_update_control.protocol import DISPATCHER_CRON_NAME
from workspace_update_control.protocol import DURABLE_CRON_PATH
from workspace_update_control.testing import RecordingFeed
from workspace_update_control.protocol import UPDATE_STATE_DIR
from workspace_update_control.testing import STAGED_CREATED_CHAT
from workspace_update_control.testing import STAGED_FEED_BASE_URL
from workspace_update_control.testing import StagedUpdateWorkspace
from workspace_update_control.testing import full_state_line
from workspace_update_control.testing import observed_agent
from workspace_update_control.testing import observed_services_agent
from workspace_update_control.testing import stage_update_workspace
from workspace_update_control.testing import the_accounts_settings
from workspace_update_control.testing import update_settings_document
from workspace_update_control.testing import write_event_lines

from imbue.mngr.utils.polling import wait_for
from imbue.system_interface.app_context import state_of
from imbue.system_interface.workspace_routes import UPDATE_DISMISSALS_ROUTE
from imbue.system_interface.workspace_routes import UPDATE_LIVENESS_ROUTE
from imbue.system_interface.workspace_routes import UPDATE_ROUTE
from imbue.system_interface.workspace_routes import UPDATE_SCHEDULE_ROUTE
from imbue.system_interface.workspace_routes import UPDATE_SETTINGS_ROUTE
from imbue.system_interface.workspace_routes import UPDATE_SKIP_ROUTE
from imbue.system_interface.workspace_routes import UPDATE_START_ROUTE

_SCHEDULE_REQUEST = {
    "ceiling": "minds-v0.11.0",
    "override_ref": "",
    "window_start_minute_utc": 420,
    "window_length_minutes": 240,
    "is_backup_configured": True,
    "is_auto_armed": False,
}
_START_REQUEST = {"ceiling": "minds-v0.11.0", "override_ref": "", "is_backup_configured": True}


def _client(update_workspace_app: Flask) -> FlaskClient:
    return update_workspace_app.test_client()


def _stand_in_for_the_feed(update_workspace_app: Flask, template_ref: str | None) -> RecordingFeed:
    """Have the shell read its release channel's file from a feed that names ``template_ref``, or cannot be read."""
    feed = RecordingFeed(template_ref)
    state_of(update_workspace_app).channel_file_fetcher = feed
    return feed


def test_a_workspace_with_no_update_state_answers_nothing_and_writes_nothing(
    update_workspace_app: Flask, staged_update_workspace: StagedUpdateWorkspace
) -> None:
    response = _client(update_workspace_app).get(UPDATE_ROUTE)

    assert response.status_code == 200
    assert response.get_json() == {
        "settings": None,
        "channel_version": None,
        "schedule": None,
        "run": None,
        "last_dispatch": None,
        "run_chat_id": None,
        "is_signed_in": None,
        "skipped_target": None,
        "dismissals": None,
        "last_start": None,
    }
    assert not (staged_update_workspace.root / "data").exists()


def test_asking_whether_an_account_is_signed_in_rides_the_read(update_workspace_app: Flask) -> None:
    assert (
        _client(update_workspace_app).get(UPDATE_ROUTE, query_string={"check_sign_in": "1"}).get_json()["is_signed_in"]
        is True
    )


def test_a_schedule_is_armed_refreshed_and_cancelled_and_its_cron_line_runs_the_templates_dispatcher(
    update_workspace_app: Flask, staged_update_workspace: StagedUpdateWorkspace
) -> None:
    client = _client(update_workspace_app)

    armed = client.put(UPDATE_SCHEDULE_ROUTE, json={**_SCHEDULE_REQUEST, "override_ref": "minds-v0.10.5"})

    assert armed.status_code == 200
    assert armed.get_json()["outcome"] == "ARMED"
    schedule = armed.get_json()["schedule"]
    assert (schedule["ceiling"], schedule["override_ref"], schedule["window_start_minute_utc"]) == (
        "minds-v0.11.0",
        "minds-v0.10.5",
        420,
    )
    assert client.get(UPDATE_ROUTE).get_json()["schedule"] == schedule
    cron_line = (staged_update_workspace.live_cron_dir / DISPATCHER_CRON_NAME).read_text()
    assert f"cd {staged_update_workspace.root.resolve()} && python3 {TICK_ENTRY_PATH} >>" in cron_line

    refreshed = client.patch(
        UPDATE_SCHEDULE_ROUTE,
        json={
            "ceiling": "minds-v0.12.0",
            "window_start_minute_utc": 60,
            "window_length_minutes": 120,
            "is_backup_configured": False,
        },
    )

    assert refreshed.get_json()["is_armed"] is True
    refreshed_schedule = refreshed.get_json()["schedule"]
    assert (refreshed_schedule["ceiling"], refreshed_schedule["window_start_minute_utc"]) == ("minds-v0.12.0", 60)
    # What the user typed and when the schedule was armed are the arm's, not the refresh's.
    assert (refreshed_schedule["override_ref"], refreshed_schedule["armed_at"]) == (
        "minds-v0.10.5",
        schedule["armed_at"],
    )

    assert client.delete(UPDATE_SCHEDULE_ROUTE).get_json() == {"is_cancelled": True}
    assert client.get(UPDATE_ROUTE).get_json()["schedule"] is None
    assert client.delete(UPDATE_SCHEDULE_ROUTE).get_json() == {"is_cancelled": False}
    assert client.patch(
        UPDATE_SCHEDULE_ROUTE,
        json={
            "ceiling": "minds-v0.12.0",
            "window_start_minute_utc": 60,
            "window_length_minutes": 120,
            "is_backup_configured": False,
        },
    ).get_json() == {"is_armed": False, "schedule": None}


def test_a_start_is_answered_at_once_and_the_read_then_says_how_creating_the_chat_went(
    update_workspace_app: Flask, staged_update_workspace: StagedUpdateWorkspace
) -> None:
    client = _client(update_workspace_app)

    started = client.post(UPDATE_START_ROUTE, json=_START_REQUEST)

    assert started.status_code == 202
    assert started.get_json()["outcome"] == "STARTING"
    attempt_id = started.get_json()["attempt_id"]
    assert attempt_id
    wait_for(
        lambda: (client.get(UPDATE_ROUTE).get_json()["last_start"] or {}).get("outcome") is not None,
        error_message="the start attempt never recorded an outcome",
    )
    last_start = client.get(UPDATE_ROUTE).get_json()["last_start"]
    assert (last_start["attempt_id"], last_start["outcome"]) == (attempt_id, "CREATED")
    assert (last_start["chat_agent_name"], last_start["chat_id"]) == (
        STAGED_CREATED_CHAT["name"],
        STAGED_CREATED_CHAT["chat_id"],
    )
    assert len(staged_update_workspace.chat_creates()) == 1
    # The watchdog's cron line is installed by a start too, so a stopped lead is brought back.
    assert (staged_update_workspace.live_cron_dir / DISPATCHER_CRON_NAME).exists()


def test_a_start_with_no_account_signed_in_is_declined_with_its_outcome_and_creates_nothing(
    tmp_path: Path, update_workspace_app: Flask, staged_update_workspace: StagedUpdateWorkspace
) -> None:
    (staged_update_workspace.root / ".mngr" / "settings.local.toml").unlink()

    declined = _client(update_workspace_app).post(UPDATE_START_ROUTE, json=_START_REQUEST)

    assert declined.status_code == 200
    assert declined.get_json()["outcome"] == "NEEDS_SIGN_IN"
    assert staged_update_workspace.chat_creates() == []
    assert staged_update_workspace.read_state("last-start.json") is None


def test_skipping_a_version_cancels_the_schedule_and_keeps_the_later_skip(
    update_workspace_app: Flask, staged_update_workspace: StagedUpdateWorkspace
) -> None:
    client = _client(update_workspace_app)
    client.put(UPDATE_SCHEDULE_ROUTE, json=_SCHEDULE_REQUEST)

    skipped = client.post(UPDATE_SKIP_ROUTE, json={"target_ref": "minds-v0.11.0", "skipped_at": 200.0})
    earlier = client.post(UPDATE_SKIP_ROUTE, json={"target_ref": "minds-v0.10.0", "skipped_at": 100.0})

    assert skipped.get_json() == {
        "is_cancelled": True,
        "skipped_target": {"schema": 1, "target_ref": "minds-v0.11.0", "skipped_at": 200.0},
    }
    assert earlier.get_json()["is_cancelled"] is False
    assert earlier.get_json()["skipped_target"]["target_ref"] == "minds-v0.11.0"
    state = client.get(UPDATE_ROUTE).get_json()
    assert (state["schedule"], state["skipped_target"]["target_ref"]) == (None, "minds-v0.11.0")


def test_a_dismissal_is_recorded_by_the_runs_start_and_a_later_run_covers_an_earlier_one(
    update_workspace_app: Flask,
) -> None:
    client = _client(update_workspace_app)

    client.post(UPDATE_DISMISSALS_ROUTE, json={"note_run_started_at": 200.0})
    answered = client.post(
        UPDATE_DISMISSALS_ROUTE, json={"note_run_started_at": 100.0, "outcome_run_started_at": 50.0}
    )

    assert answered.get_json() == {
        "dismissals": {"schema": 1, "note_run_started_at": 200.0, "outcome_run_started_at": 50.0}
    }
    assert client.get(UPDATE_ROUTE).get_json()["dismissals"] == answered.get_json()["dismissals"]


def test_liveness_answers_the_agents_the_observers_file_lists_and_null_when_it_cannot_vouch_for_them(
    update_workspace_app: Flask, staged_update_workspace: StagedUpdateWorkspace
) -> None:
    client = _client(update_workspace_app)
    staged_update_workspace.write_state("run.json", {"chat_agent_name": "Update-to-v0110", "started_at": 5.0})

    without_events = client.get(UPDATE_LIVENESS_ROUTE).get_json()

    assert without_events["agents"] is None
    assert without_events["run"] == {"chat_agent_name": "Update-to-v0110", "started_at": 5.0}
    assert without_events["is_watchdog_installed"] is False

    lead = observed_agent("agent-lead", "Update-to-v0110", "RUNNING", chat_id="chat-of-the-update")
    write_event_lines(
        staged_update_workspace.events_path, [full_state_line([observed_services_agent(), lead], time.time())]
    )

    with_events = client.get(UPDATE_LIVENESS_ROUTE).get_json()

    assert {
        "name": "Update-to-v0110",
        "state": "RUNNING",
        "agent_id": "agent-lead",
        "chat_id": "chat-of-the-update",
    } in (with_events["agents"])


def test_a_request_whose_values_are_out_of_range_is_refused_and_arms_nothing(
    update_workspace_app: Flask, staged_update_workspace: StagedUpdateWorkspace
) -> None:
    refused = _client(update_workspace_app).put(
        UPDATE_SCHEDULE_ROUTE, json={**_SCHEDULE_REQUEST, "window_start_minute_utc": 24 * 60}
    )

    assert refused.status_code == 400
    assert refused.get_json()["code"] == "INVALID_REQUEST"
    assert staged_update_workspace.read_state("schedule.json") is None


def test_a_version_that_is_not_valid_unicode_is_refused_and_arms_nothing(
    update_workspace_app: Flask, staged_update_workspace: StagedUpdateWorkspace
) -> None:
    refused = _client(update_workspace_app).put(
        UPDATE_SCHEDULE_ROUTE, json={**_SCHEDULE_REQUEST, "override_ref": "minds-v0.10.5\ud800"}
    )

    assert refused.status_code == 400
    assert refused.get_json()["code"] == "INVALID_REQUEST"
    assert staged_update_workspace.read_state("schedule.json") is None
    assert not (staged_update_workspace.live_cron_dir / DISPATCHER_CRON_NAME).exists()


# ``1e999`` is how JSON itself spells a number too large to be finite; ``NaN`` is what Python's parser also takes.
@pytest.mark.parametrize("time_as_sent", ["1e999", "NaN", "true", '"200.0"'])
@pytest.mark.parametrize(
    ("route", "body_template", "state_file"),
    [
        (UPDATE_SKIP_ROUTE, '{"target_ref": "minds-v0.11.0", "skipped_at": %s}', "skipped-target.json"),
        (UPDATE_DISMISSALS_ROUTE, '{"note_run_started_at": %s}', "dismissals.json"),
        (UPDATE_DISMISSALS_ROUTE, '{"outcome_run_started_at": %s}', "dismissals.json"),
    ],
)
def test_a_time_that_is_not_a_finite_number_is_refused_and_records_nothing(
    update_workspace_app: Flask,
    staged_update_workspace: StagedUpdateWorkspace,
    route: str,
    body_template: str,
    state_file: str,
    time_as_sent: str,
) -> None:
    refused = _client(update_workspace_app).post(
        route, data=body_template % time_as_sent, content_type="application/json"
    )

    assert refused.status_code == 400
    assert refused.get_json()["code"] == "INVALID_REQUEST"
    assert staged_update_workspace.read_state(state_file) is None


def test_a_time_sent_as_a_whole_number_is_taken(update_workspace_app: Flask) -> None:
    client = _client(update_workspace_app)

    skipped = client.post(UPDATE_SKIP_ROUTE, json={"target_ref": "minds-v0.11.0", "skipped_at": 200})
    dismissed = client.post(UPDATE_DISMISSALS_ROUTE, json={"note_run_started_at": 200})

    assert skipped.get_json()["skipped_target"]["skipped_at"] == 200.0
    assert dismissed.get_json()["dismissals"]["note_run_started_at"] == 200.0


@pytest.mark.parametrize(
    ("method", "route", "body"),
    [
        ("POST", UPDATE_START_ROUTE, _START_REQUEST),
        ("PUT", UPDATE_SCHEDULE_ROUTE, _SCHEDULE_REQUEST),
        ("DELETE", UPDATE_SCHEDULE_ROUTE, None),
        ("POST", UPDATE_SKIP_ROUTE, {"target_ref": "minds-v0.11.0", "skipped_at": 200.0}),
        ("POST", UPDATE_DISMISSALS_ROUTE, {"note_run_started_at": 200.0}),
    ],
)
def test_update_state_that_cannot_be_written_is_answered_with_the_routes_own_error_object(
    update_workspace_app: Flask,
    staged_update_workspace: StagedUpdateWorkspace,
    method: str,
    route: str,
    body: dict[str, object] | None,
) -> None:
    # A file where the directory of the update's state belongs, so nothing under it can be written.
    state_directory = staged_update_workspace.root / UPDATE_STATE_DIR
    state_directory.parent.mkdir(parents=True, exist_ok=True)
    state_directory.write_text("")

    failed = _client(update_workspace_app).open(route, method=method, json=body)

    assert failed.status_code == 500
    assert failed.get_json()["code"] == "WRITE_FAILED"
    assert "update state could not be changed" in failed.get_json()["detail"]
    assert staged_update_workspace.chat_creates() == []


def test_a_shell_that_does_not_run_from_the_workspaces_root_refuses_to_act_on_its_updates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, client: FlaskClient
) -> None:
    stage_update_workspace(tmp_path, monkeypatch)

    refused = client.get(UPDATE_ROUTE)

    assert refused.status_code == 500
    assert refused.get_json()["code"] == "NOT_AT_WORKSPACE_ROOT"


# The update settings, and a request that leaves its values to the workspace


def test_the_update_settings_are_stored_and_answered_as_stored_and_the_read_answers_them(
    update_workspace_app: Flask, staged_update_workspace: StagedUpdateWorkspace
) -> None:
    client = _client(update_workspace_app)
    settings = update_settings_document(channel="beta", is_auto_update_enabled_here=False)

    stored = client.put(UPDATE_SETTINGS_ROUTE, json=settings)

    assert stored.status_code == 200
    assert stored.get_json() == settings
    assert staged_update_workspace.read_state("settings.json") == {"schema": 1, **settings}
    # The schedule is a file of its own, which a desktop from before the settings reads and writes.
    assert staged_update_workspace.read_state("schedule.json") is None
    state = client.get(UPDATE_ROUTE).get_json()
    assert (state["settings"], state["channel_version"]) == (settings, None)


def test_a_write_of_the_update_settings_puts_the_templates_dispatcher_back_on_the_cron_line(
    update_workspace_app: Flask, staged_update_workspace: StagedUpdateWorkspace
) -> None:
    # A desktop from before the update routes writes the line for the dispatcher it ships, which arms nothing.
    live_cron_line = staged_update_workspace.live_cron_dir / DISPATCHER_CRON_NAME
    live_cron_line.write_text("* * * * * root python3 data/.state/update-apply/dispatcher.pyz tick\n")

    stored = _client(update_workspace_app).put(UPDATE_SETTINGS_ROUTE, json=update_settings_document())

    assert stored.status_code == 200
    cron_line = live_cron_line.read_text()
    assert f"cd {staged_update_workspace.root.resolve()} && python3 {TICK_ENTRY_PATH} >>" in cron_line
    assert "dispatcher.pyz" not in cron_line
    # The durable copy is what a boot reinstalls.
    assert (staged_update_workspace.root / DURABLE_CRON_PATH).read_text() == cron_line


def test_settings_with_none_of_the_workspaces_own_follow_the_accounts(update_workspace_app: Flask) -> None:
    stored = _client(update_workspace_app).put(UPDATE_SETTINGS_ROUTE, json=the_accounts_settings())

    assert stored.get_json() == update_settings_document()


def test_a_setting_of_the_workspaces_own_is_kept_when_a_request_leaves_it_out_and_cleared_by_a_null(
    update_workspace_app: Flask, staged_update_workspace: StagedUpdateWorkspace
) -> None:
    client = _client(update_workspace_app)
    of_the_workspace = {
        "is_auto_update_enabled_here": False,
        "window_start_hour_here": 22,
        "window_end_hour_here": 23,
    }
    client.put(UPDATE_SETTINGS_ROUTE, json=update_settings_document(**of_the_workspace))

    # A client that writes the account's settings names none of the three, and so does one from before the hours.
    kept = client.put(UPDATE_SETTINGS_ROUTE, json=the_accounts_settings(channel="beta"))

    assert kept.status_code == 200
    assert kept.get_json() == update_settings_document(channel="beta", **of_the_workspace)
    assert client.get(UPDATE_ROUTE).get_json()["settings"] == kept.get_json()

    cleared = client.put(
        UPDATE_SETTINGS_ROUTE,
        json={**the_accounts_settings(channel="beta"), "window_start_hour_here": None, "window_end_hour_here": None},
    )

    assert cleared.get_json() == update_settings_document(channel="beta", is_auto_update_enabled_here=False)
    assert staged_update_workspace.read_state("settings.json") == {"schema": 1, **cleared.get_json()}


def test_an_armed_schedule_takes_the_workspaces_own_hours_where_it_has_them(update_workspace_app: Flask) -> None:
    client = _client(update_workspace_app)
    _stand_in_for_the_feed(update_workspace_app, "minds-v0.12.0")
    client.put(UPDATE_SETTINGS_ROUTE, json=update_settings_document(window_start_hour_here=22, window_end_hour_here=1))

    schedule = client.put(UPDATE_SCHEDULE_ROUTE, json={}).get_json()["schedule"]

    assert (schedule["window_start_minute_utc"], schedule["window_length_minutes"]) == (22 * 60, 3 * 60)


@pytest.mark.parametrize(
    ("fields", "code"),
    [
        ({"window_start_hour": 24}, "INVALID_REQUEST"),
        ({"is_auto_update_enabled": "yes"}, "INVALID_REQUEST"),
        ({"channel": "../stable"}, "INVALID_UPDATE_SETTINGS"),
        ({"feed_base_url": "file:///etc/passwd"}, "INVALID_UPDATE_SETTINGS"),
        ({"window_start_hour": 4, "window_end_hour": 4}, "INVALID_UPDATE_SETTINGS"),
        ({"window_start_hour_here": 24, "window_end_hour_here": 4}, "INVALID_REQUEST"),
        ({"window_start_hour_here": 22}, "INVALID_UPDATE_SETTINGS"),
        ({"window_start_hour_here": 4, "window_end_hour_here": 4}, "INVALID_UPDATE_SETTINGS"),
    ],
)
def test_settings_that_cannot_be_the_workspaces_are_refused_and_change_nothing(
    update_workspace_app: Flask, staged_update_workspace: StagedUpdateWorkspace, fields: dict[str, object], code: str
) -> None:
    refused = _client(update_workspace_app).put(UPDATE_SETTINGS_ROUTE, json=update_settings_document(**fields))

    assert refused.status_code == 400
    assert refused.get_json()["code"] == code
    assert staged_update_workspace.read_state("settings.json") is None


def test_settings_that_cannot_be_written_fail_the_write_and_say_so(
    update_workspace_app: Flask, staged_update_workspace: StagedUpdateWorkspace, loguru_records: list[str]
) -> None:
    # A directory where the settings are stored, which a write cannot replace.
    staged_update_workspace.state_path("settings.json").mkdir(parents=True)

    failed = _client(update_workspace_app).put(UPDATE_SETTINGS_ROUTE, json=update_settings_document())

    assert failed.status_code == 500
    assert failed.get_json()["code"] == "WRITE_FAILED"
    assert failed.get_json()["detail"].startswith("The update settings could not be written: ")
    assert [record for record in loguru_records if record.startswith("ERROR")] == [
        "ERROR Could not write the update settings"
    ]


def test_a_schedule_armed_with_nothing_named_takes_the_channels_version_and_the_settings_window(
    update_workspace_app: Flask, staged_update_workspace: StagedUpdateWorkspace
) -> None:
    client = _client(update_workspace_app)
    feed = _stand_in_for_the_feed(update_workspace_app, "minds-v0.12.0")
    client.put(UPDATE_SETTINGS_ROUTE, json=update_settings_document())

    armed = client.put(UPDATE_SCHEDULE_ROUTE, json={})

    assert armed.status_code == 200
    assert armed.get_json()["outcome"] == "ARMED"
    schedule = armed.get_json()["schedule"]
    assert (
        schedule["ceiling"],
        schedule["window_start_minute_utc"],
        schedule["window_length_minutes"],
        schedule["is_backup_configured"],
        schedule["is_auto_armed"],
    ) == ("minds-v0.12.0", 2 * 60, 3 * 60, False, False)
    assert feed.asked == [f"{STAGED_FEED_BASE_URL}/stable-web.json"]
    channel_version = client.get(UPDATE_ROUTE).get_json()["channel_version"]
    assert (channel_version["channel"], channel_version["template_ref"], channel_version["error"]) == (
        "stable",
        "minds-v0.12.0",
        "",
    )
    assert channel_version["read_at"] == channel_version["checked_at"]

    # A refresh that names nothing keeps the workspace's own values, and the feed is not asked again within the hour.
    refreshed = client.patch(UPDATE_SCHEDULE_ROUTE, json={})

    assert refreshed.get_json() == {"is_armed": True, "schedule": schedule}
    assert len(feed.asked) == 1


def test_a_schedule_armed_with_no_version_named_and_none_read_is_declined_with_that_reason(
    update_workspace_app: Flask, staged_update_workspace: StagedUpdateWorkspace
) -> None:
    client = _client(update_workspace_app)

    with_no_settings = client.put(UPDATE_SCHEDULE_ROUTE, json={})

    assert with_no_settings.status_code == 200
    assert with_no_settings.get_json()["outcome"] == "NO_CHANNEL_VERSION"
    assert "holds no update settings" in with_no_settings.get_json()["detail"]

    _stand_in_for_the_feed(update_workspace_app, None)
    client.put(UPDATE_SETTINGS_ROUTE, json=update_settings_document())
    with_the_feed_down = client.put(UPDATE_SCHEDULE_ROUTE, json={})

    assert with_the_feed_down.get_json()["outcome"] == "NO_CHANNEL_VERSION"
    assert "the feed is down" in with_the_feed_down.get_json()["detail"]
    assert staged_update_workspace.read_state("schedule.json") is None
    channel_version = client.get(UPDATE_ROUTE).get_json()["channel_version"]
    assert (channel_version["template_ref"], channel_version["read_at"], channel_version["error"]) == (
        "",
        None,
        "the feed is down",
    )


def test_a_schedule_that_names_its_version_and_no_window_in_a_workspace_with_no_settings_is_declined(
    update_workspace_app: Flask, staged_update_workspace: StagedUpdateWorkspace
) -> None:
    declined = _client(update_workspace_app).put(UPDATE_SCHEDULE_ROUTE, json={"ceiling": "minds-v0.11.0"})

    assert declined.status_code == 200
    assert declined.get_json()["outcome"] == "NO_UPDATE_SETTINGS"
    assert staged_update_workspace.read_state("schedule.json") is None


def test_a_window_named_by_half_is_refused(
    update_workspace_app: Flask, staged_update_workspace: StagedUpdateWorkspace
) -> None:
    refused = _client(update_workspace_app).put(
        UPDATE_SCHEDULE_ROUTE, json={"ceiling": "minds-v0.11.0", "window_start_minute_utc": 420}
    )

    assert refused.status_code == 400
    assert refused.get_json()["code"] == "INVALID_REQUEST"
    assert staged_update_workspace.read_state("schedule.json") is None


def test_a_start_with_no_version_named_updates_to_the_channels_version(
    update_workspace_app: Flask, staged_update_workspace: StagedUpdateWorkspace
) -> None:
    client = _client(update_workspace_app)
    _stand_in_for_the_feed(update_workspace_app, "minds-v0.12.0")
    client.put(UPDATE_SETTINGS_ROUTE, json=update_settings_document())

    started = client.post(UPDATE_START_ROUTE, json={})

    assert started.status_code == 202
    assert started.get_json()["outcome"] == "STARTING"
    wait_for(
        lambda: (client.get(UPDATE_ROUTE).get_json()["last_start"] or {}).get("outcome") is not None,
        error_message="the start attempt never recorded an outcome",
    )
    assert client.get(UPDATE_ROUTE).get_json()["last_dispatch"]["target_ref"] == "minds-v0.12.0"
    assert "- Target: minds-v0.12.0" in staged_update_workspace.chat_creates()[0][-1]


def test_a_start_with_no_version_named_and_none_read_is_declined_and_creates_nothing(
    update_workspace_app: Flask, staged_update_workspace: StagedUpdateWorkspace
) -> None:
    declined = _client(update_workspace_app).post(UPDATE_START_ROUTE, json={})

    assert declined.status_code == 200
    assert declined.get_json()["outcome"] == "NO_CHANNEL_VERSION"
    assert staged_update_workspace.chat_creates() == []
    assert staged_update_workspace.read_state("last-start.json") is None


def test_a_start_at_a_version_the_user_named_needs_no_channel(
    update_workspace_app: Flask, staged_update_workspace: StagedUpdateWorkspace
) -> None:
    client = _client(update_workspace_app)

    started = client.post(UPDATE_START_ROUTE, json={"override_ref": "minds-v0.11.3"})

    assert started.status_code == 202
    wait_for(
        lambda: (client.get(UPDATE_ROUTE).get_json()["last_start"] or {}).get("outcome") is not None,
        error_message="the start attempt never recorded an outcome",
    )
    assert client.get(UPDATE_ROUTE).get_json()["last_dispatch"]["target_ref"] == "minds-v0.11.3"


def test_a_client_that_names_its_newest_version_is_served_as_before_and_recorded_as_holding_the_workspace_to_it(
    update_workspace_app: Flask, staged_update_workspace: StagedUpdateWorkspace
) -> None:
    client = _client(update_workspace_app)
    feed = _stand_in_for_the_feed(update_workspace_app, "minds-v0.12.0")
    client.put(UPDATE_SETTINGS_ROUTE, json=update_settings_document())

    armed = client.put(UPDATE_SCHEDULE_ROUTE, json=_SCHEDULE_REQUEST)

    schedule = armed.get_json()["schedule"]
    assert (schedule["ceiling"], schedule["window_start_minute_utc"], schedule["is_backup_configured"]) == (
        "minds-v0.11.0",
        420,
        True,
    )
    assert feed.asked == []
    recorded = staged_update_workspace.read_state("client-ceiling.json")
    assert recorded is not None
    assert recorded["ceiling"] == "minds-v0.11.0"
