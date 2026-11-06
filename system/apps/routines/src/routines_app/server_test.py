import os
from pathlib import Path

import pytest
from flask.testing import FlaskClient

from app_schema.testing import assert_api_schema_describes_app
from imbue.imbue_common.model_update import to_update
from routines.data_types import CurrentRun
from routines.mock_chat_app_test import ScriptedChatApp
from routines.mock_clock_test import FakeClock
from routines.mock_notifier_test import RecordingNotifier
from routines.mock_repository_test import RecordingRepository
from routines.mock_state_store_test import RefusingRemovalStateStore
from routines.paths import RoutinesPaths
from routines.primitives import RoutineId
from routines.primitives import RunId
from routines.primitives import RunTrigger
from routines.service import RoutinesService
from routines.testing import TEST_NOW
from routines.testing import build_test_service
from routines.testing import wait_for_last_run
from routines_app.mock_fix_chat_test import RecordingFixChatStarter
from routines_app.server import create_app

MANIFEST_PATH = Path(__file__).resolve().parents[2] / "app.toml"
# A schedule change as the page sends it: every field, the unused ones null.
DAILY_SCHEDULE_BODY: dict[str, object] = {"every": "1d", "at": "07:30", "days": [], "day_of_month": None, "once": None}


def test_the_api_document_describes_every_route(client: FlaskClient) -> None:
    assert_api_schema_describes_app(client, MANIFEST_PATH)


def test_every_page_path_serves_the_one_document_and_assets_their_file(
    client: FlaskClient, service: RoutinesService
) -> None:
    service.registry_path.parent.mkdir(parents=True, exist_ok=True)
    service.registry_path.write_text(
        '[[apps]]\nname = "chat"\nurl = "http://127.0.0.1:8010"\nlabel = "chat-x7k9q2w1"\n'
    )
    for path in ("/", "/news-sync", "/news-sync/runs/run-1", "/news-sync/runs/run-1/log"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert b"<title>Routines</title>" in response.data
        # The chat app's minted origin label rides in the document, so the pages can link a run's chat.
        assert b'<meta name="routines-chat-label" content="chat-x7k9q2w1">' in response.data
        assert response.headers["Cache-Control"] == "no-store"
    unhashed = client.get("/assets/app.js")
    assert unhashed.data == b"console.log('built');"
    assert unhashed.headers["Cache-Control"] == "no-cache"
    hashed = client.get("/assets/app-Ab12cd34.js")
    assert hashed.status_code == 200
    assert "max-age=31536000" in hashed.headers["Cache-Control"]
    assert client.get("/assets/missing.js").status_code == 404


def test_the_document_carries_no_chat_label_before_the_chat_app_registers(client: FlaskClient) -> None:
    assert b'<meta name="routines-chat-label" content="">' in client.get("/").data


def test_the_listing_and_the_detail_answer_the_library_shapes(
    client: FlaskClient, service: RoutinesService, sync_routine: RoutineId
) -> None:
    listing = client.get("/api/routines").get_json()
    assert [row["routine_id"] for row in listing["routines"]] == [str(sync_routine)]
    assert listing["routines"][0]["health"] == "NOT_RUN_YET"
    assert listing["summary"]["total"] == 1

    detail = client.get(f"/api/routines/{sync_routine}").get_json()
    assert detail["row"]["schedule_text"] == "Every hour"
    assert detail["definition"]["command"] == ["sh", "-c", "echo ran"]
    assert detail["runs"] == []

    assert client.get("/api/routines/nothing").status_code == 404
    assert client.get("/api/routines/Not%20An%20Id").status_code == 404


def test_the_switch_and_the_schedule_change_answer_the_updated_row(
    client: FlaskClient, service: RoutinesService, sync_routine: RoutineId
) -> None:
    off = client.put(f"/api/routines/{sync_routine}/enabled", json={"is_enabled": False})
    assert off.status_code == 200
    assert off.get_json()["health"] == "OFF"

    changed = client.put(
        f"/api/routines/{sync_routine}/schedule",
        json={**DAILY_SCHEDULE_BODY, "days": ["mon", "fri"]},
    )
    assert changed.status_code == 200, changed.get_json()
    assert changed.get_json()["schedule_text"] == "Every Monday and Friday at 7:30 am"
    assert (service.paths.definitions_dir / f"{sync_routine}.toml").read_text().count("07:30") == 1

    refused = client.put(
        f"/api/routines/{sync_routine}/schedule",
        json={"every": "15m", "at": "07:30", "days": [], "day_of_month": None, "once": None},
    )
    assert refused.status_code == 400
    assert "a day or longer" in refused.get_json()["detail"]


def test_a_run_and_its_log_are_read_back(
    client: FlaskClient, service: RoutinesService, sync_routine: RoutineId
) -> None:
    event = service.runner.run(sync_routine, RunTrigger.MANUAL, None)

    run = client.get(f"/api/routines/{sync_routine}/runs/{event.run_id}")
    assert run.status_code == 200
    assert run.get_json()["run"]["outcome"] == "SUCCEEDED"
    assert "ran" in run.get_json()["log"]
    assert client.get(f"/api/routines/{sync_routine}/runs/{event.run_id}/log").data == b"ran\n"
    unknown_run = "run-00000000000000000000000000000000"
    assert client.get(f"/api/routines/{sync_routine}/runs/{unknown_run}").status_code == 404
    assert client.get(f"/api/routines/{sync_routine}/runs/{unknown_run}/log").status_code == 404
    assert client.get(f"/api/routines/nothing/runs/{event.run_id}/log").status_code == 404

    detail = client.get(f"/api/routines/{sync_routine}").get_json()
    assert [run["run_id"] for run in detail["runs"]] == [event.run_id]
    assert detail["row"]["last_run_text"].startswith("Worked")


@pytest.mark.timeout(30)
def test_run_now_answers_a_receipt_and_refuses_a_second_run_while_one_is_going(
    client: FlaskClient, service: RoutinesService, sync_routine: RoutineId
) -> None:
    in_flight = CurrentRun(
        run_id=RunId.generate(), pid=os.getpid(), pid_start="", trigger=RunTrigger.MANUAL, started_at=TEST_NOW
    )
    service.states.update(
        sync_routine, lambda state: state.model_copy_update(to_update(state.field_ref().current_run, in_flight))
    )
    refused = client.post(f"/api/routines/{sync_routine}/run", json={})
    assert refused.status_code == 409
    assert "already going" in refused.get_json()["detail"]
    service.states.update(
        sync_routine, lambda state: state.model_copy_update(to_update(state.field_ref().current_run, None))
    )

    started = client.post(f"/api/routines/{sync_routine}/run", json={})

    assert started.status_code == 200, started.get_json()
    receipt = started.get_json()
    assert receipt["routine_id"] == str(sync_routine)
    assert receipt["run_id"].startswith("run-")
    # The run goes on in a process of its own; its books close without the app.
    last_run = wait_for_last_run(service.states, sync_routine, 15)
    assert str(last_run.run_id) == receipt["run_id"]
    assert last_run.trigger == "MANUAL"
    assert client.post("/api/routines/nothing/run", json={}).status_code == 404


def test_the_switch_and_the_schedule_of_an_unknown_routine_are_not_found(client: FlaskClient) -> None:
    assert client.put("/api/routines/nothing/enabled", json={"is_enabled": False}).status_code == 404
    assert client.put("/api/routines/nothing/schedule", json=DAILY_SCHEDULE_BODY).status_code == 404


def test_a_broken_definition_cannot_be_run_and_the_reason_is_a_bad_request(
    client: FlaskClient, service: RoutinesService, sync_routine: RoutineId
) -> None:
    (service.paths.definitions_dir / f"{sync_routine}.toml").write_text('title = "News sync"\n')
    refused = client.post(f"/api/routines/{sync_routine}/run", json={})
    assert refused.status_code == 400
    assert "cannot run" in refused.get_json()["detail"]


def test_removing_a_routine_deletes_it_once_no_run_is_going(
    client: FlaskClient, service: RoutinesService, sync_routine: RoutineId
) -> None:
    in_flight = CurrentRun(
        run_id=RunId.generate(), pid=os.getpid(), pid_start="", trigger=RunTrigger.MANUAL, started_at=TEST_NOW
    )
    service.states.update(
        sync_routine, lambda state: state.model_copy_update(to_update(state.field_ref().current_run, in_flight))
    )
    refused = client.delete(f"/api/routines/{sync_routine}")
    assert refused.status_code == 409
    assert "is going" in refused.get_json()["detail"]
    assert client.get(f"/api/routines/{sync_routine}").status_code == 200

    service.states.update(
        sync_routine, lambda state: state.model_copy_update(to_update(state.field_ref().current_run, None))
    )
    assert client.delete(f"/api/routines/{sync_routine}").status_code == 204
    assert client.get(f"/api/routines/{sync_routine}").status_code == 404
    assert client.delete(f"/api/routines/{sync_routine}").status_code == 404


def test_a_removal_that_cannot_delete_the_state_says_so_and_keeps_the_routine(
    git_paths: RoutinesPaths,
    repository: RecordingRepository,
    clock: FakeClock,
    chat_app: ScriptedChatApp,
    notifier: RecordingNotifier,
    static_directory: Path,
    fix_chat_starter: RecordingFixChatStarter,
    sync_routine: RoutineId,
) -> None:
    """The routine's definition file is the one the fixture's service wrote; this service only refuses the removal."""
    states = RefusingRemovalStateStore(paths=git_paths)
    service = build_test_service(git_paths, repository, states, clock, chat_app, notifier)
    client = create_app(service, static_directory, is_preview=False, fix_chat_starter=fix_chat_starter).test_client()

    response = client.delete(f"/api/routines/{sync_routine}")

    assert response.status_code == 500
    assert "Could not remove all of the state" in response.get_json()["detail"]
    assert client.get(f"/api/routines/{sync_routine}").status_code == 200


def test_a_removal_whose_commit_is_refused_is_still_a_removal(
    client: FlaskClient, repository: RecordingRepository, sync_routine: RoutineId
) -> None:
    """The service drops the routine before it commits, so a refused commit leaves nothing to retry on."""
    repository.is_refusing = True

    assert client.delete(f"/api/routines/{sync_routine}").status_code == 204
    assert client.get(f"/api/routines/{sync_routine}").status_code == 404


def test_a_schedule_change_whose_commit_is_refused_is_a_conflict_in_the_repositorys_words(
    client: FlaskClient, repository: RecordingRepository, sync_routine: RoutineId
) -> None:
    repository.is_refusing = True

    refused = client.put(f"/api/routines/{sync_routine}/schedule", json=DAILY_SCHEDULE_BODY)

    assert refused.status_code == 409
    assert "something else is saving" in refused.get_json()["detail"]


def test_the_fix_route_opens_a_chat_briefed_on_the_routine_and_answers_its_id(
    client: FlaskClient, service: RoutinesService, fix_chat_starter: RecordingFixChatStarter, sync_routine: RoutineId
) -> None:
    service.runner.run(sync_routine, RunTrigger.MANUAL, None)
    response = client.post(f"/api/routines/{sync_routine}/fix", json={})
    assert response.status_code == 200
    assert response.get_json() == {"chat_id": "agent-fix"}
    ((brief, name),) = fix_chat_starter.started
    assert name == "Fix News sync"
    assert '"News sync"' in brief
    assert f"/{sync_routine}" in brief
    # The last finished run's output rides in the brief, so the agent sees the evidence at once.
    assert "## Last run's output\n\n```\nran\n```" in brief
    assert client.post("/api/routines/nothing/fix", json={}).status_code == 404


def test_a_fix_chat_that_could_not_start_is_a_bad_gateway_with_the_reason(
    client: FlaskClient, service: RoutinesService, fix_chat_starter: RecordingFixChatStarter, sync_routine: RoutineId
) -> None:
    fix_chat_starter.refusal = "sign in to a provider first"
    response = client.post(f"/api/routines/{sync_routine}/fix", json={})
    assert response.status_code == 502
    assert "sign in" in response.get_json()["detail"]


def test_a_preview_refuses_every_change(
    preview_client: FlaskClient,
    service: RoutinesService,
    fix_chat_starter: RecordingFixChatStarter,
    sync_routine: RoutineId,
) -> None:
    assert preview_client.get("/api/routines").status_code == 200
    assert preview_client.post(f"/api/routines/{sync_routine}/run", json={}).status_code == 403
    assert preview_client.put(f"/api/routines/{sync_routine}/enabled", json={"is_enabled": False}).status_code == 403
    assert preview_client.put(f"/api/routines/{sync_routine}/schedule", json=DAILY_SCHEDULE_BODY).status_code == 403
    assert preview_client.post(f"/api/routines/{sync_routine}/fix", json={}).status_code == 403
    assert preview_client.delete(f"/api/routines/{sync_routine}").status_code == 403
    assert service.detail(sync_routine).row.is_enabled
    assert service.detail(sync_routine).row.schedule_text == "Every hour"
    assert fix_chat_starter.started == []


def test_the_health_probe_says_whether_the_pages_are_built(client: FlaskClient) -> None:
    assert client.get("/api/health").get_json() == {"status": "ok", "is_frontend_built": True}


def test_an_unbuilt_frontend_serves_the_not_built_page_and_says_so_in_the_health_probe(
    service: RoutinesService, tmp_path: Path, fix_chat_starter: RecordingFixChatStarter
) -> None:
    unbuilt = create_app(service, tmp_path / "nothing-built", is_preview=False, fix_chat_starter=fix_chat_starter)
    client = unbuilt.test_client()
    page = client.get("/")
    assert page.status_code == 200
    assert b"has not been built yet" in page.data
    assert client.get("/api/health").get_json() == {"status": "ok", "is_frontend_built": False}


def test_a_schedule_body_the_model_refuses_is_a_bad_request(
    client: FlaskClient, service: RoutinesService, sync_routine: RoutineId
) -> None:
    for body in ({"every": "15x", "at": None, "days": [], "day_of_month": None, "once": None}, ["not", "a", "body"]):
        refused = client.put(f"/api/routines/{sync_routine}/schedule", json=body)
        assert refused.status_code == 400, body
        assert refused.get_json()["detail"]
    assert service.detail(sync_routine).row.schedule_text == "Every hour"
