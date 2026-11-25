import time
from pathlib import Path

import pytest
from flask import Flask
from flask.testing import FlaskClient
from workspace_update_control.control import TICK_ENTRY_PATH
from workspace_update_control.protocol import DISPATCHER_CRON_NAME
from workspace_update_control.protocol import UPDATE_STATE_DIR
from workspace_update_control.testing import STAGED_CREATED_CHAT
from workspace_update_control.testing import StagedUpdateWorkspace
from workspace_update_control.testing import full_state_line
from workspace_update_control.testing import observed_agent
from workspace_update_control.testing import observed_services_agent
from workspace_update_control.testing import stage_update_workspace
from workspace_update_control.testing import write_event_lines

from imbue.mngr.utils.polling import wait_for
from imbue.system_interface.workspace_routes import UPDATE_DISMISSALS_ROUTE
from imbue.system_interface.workspace_routes import UPDATE_LIVENESS_ROUTE
from imbue.system_interface.workspace_routes import UPDATE_ROUTE
from imbue.system_interface.workspace_routes import UPDATE_SCHEDULE_ROUTE
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


def test_a_workspace_with_no_update_state_answers_nothing_and_writes_nothing(
    update_workspace_app: Flask, staged_update_workspace: StagedUpdateWorkspace
) -> None:
    response = _client(update_workspace_app).get(UPDATE_ROUTE)

    assert response.status_code == 200
    assert response.get_json() == {
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
