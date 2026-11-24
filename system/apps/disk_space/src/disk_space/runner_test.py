"""The app's routes: what they answer, what they refuse, and who they refuse.

The browser's own requests carry no identity header and come from loopback, so the test client's plain requests are
the owner's, exactly as the workspace's own browser's are. A share adds a header: those tests send one.
"""

import json
import time
from pathlib import Path

import pytest
from app_schema.testing import assert_request_guard_refuses_unknown_provenance
from flask.testing import FlaskClient

from disk_space import runner, scan, store
from disk_space.testing import (
    GIB,
    FakeWorkspaceCommands,
    a_disk,
    a_node,
    a_saved_item,
    install_disk,
    save_measurement,
)

A_PERSON = {"owner": False, "user_id": "user-1", "email": "someone@example.com"}
A_VISITING_AGENT = {
    "owner": False,
    "agent_id": "agent-9",
    "agent_name": "Helper",
    "granter": "user-1",
}
THE_OWNER = {"owner": True, "user_id": "owner-1", "email": "owner@example.com"}


@pytest.fixture
def client() -> FlaskClient:
    return runner.app.test_client()


@pytest.fixture
def measured() -> dict:
    return save_measurement(
        [
            a_node("root", None, "Whole disk", size=93 * GIB),
            a_node(
                "n1",
                "root",
                "AI model downloads",
                size=7 * GIB,
                path="/var/cache/user/huggingface",
                verdict=scan.SAFE,
                why="Downloaded again the next time something needs them.",
                item=True,
                method="remove",
            ),
            a_node(
                "n2",
                "root",
                "Your folder: notes",
                size=3 * GIB,
                path="/home/user/workspace/data/notes",
                verdict=scan.ASK,
                why="A folder of yours.",
                item=True,
                method="remove",
            ),
            a_node(
                "n3",
                "root",
                "Workspace code",
                size=80 * GIB,
                path="/home/user/workspace/system",
                verdict=scan.KEEP,
                why="The apps and services themselves.",
                item=True,
            ),
        ]
    )


def _identity(header: dict) -> dict[str, str]:
    return {"X-Imbue-Identity": json.dumps(header)}


# What the page reads.


def test_the_page_is_served_with_its_script_and_stylesheet(client: FlaskClient) -> None:
    page = client.get("/")
    assert page.status_code == 200
    assert page.mimetype == "text/html"
    assert b"Disk Space" in page.data
    assert (
        b'<meta name="viewport" content="width=device-width, initial-scale=1">'
        in page.data
    )
    assert b"/static/app.js" in page.data
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/app.css").status_code == 200
    assert client.get("/static/index.html").status_code == 404
    assert client.get("/static/..%2Frunner.py").status_code == 404


def test_the_page_may_only_run_its_own_scripts(client: FlaskClient) -> None:
    policy = client.get("/").headers["Content-Security-Policy"]
    assert "default-src 'self'" in policy
    assert "object-src 'none'" in policy
    assert "frame-src http: https:" in policy
    assert "report-uri /_csp-report" in policy
    # The shell's modules are served from this app's own origin, so they need no exception.
    assert "script-src 'self'" in policy


def test_the_shell_modules_are_served_by_name_and_nothing_else(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    modules = tmp_path / "_static"
    modules.mkdir()
    (modules / "app_contract.js").write_text(
        "export const connectToShell = () => {};\n"
    )
    monkeypatch.setattr(runner, "SHELL_STATIC_MODULES_DIR", modules)
    served = client.get("/_static/app_contract.js")
    assert served.status_code == 200
    assert served.mimetype == "text/javascript"
    assert client.get("/_static/context_menu.js").status_code == 404
    assert client.get("/_static/other.js").status_code == 404
    assert client.get("/_static/..%2Fapp.py").status_code == 404


def test_the_page_shows_the_measurement_the_disk_and_what_the_check_saw(
    client: FlaskClient, measured: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.update_watch(helper_chat_id="chat-abc", last_check_at=1234.0)
    store.write_settings(92)
    install_disk(monkeypatch, a_disk(percent=93))
    answer = client.get("/api/state").get_json()
    assert answer["disk"]["percent"] == 93
    assert answer["threshold"] == 92
    assert answer["scanning"] is False
    assert answer["scan_started_at"] is None
    assert answer["snapshot"]["id"] == measured["id"]
    assert [node["name"] for node in answer["snapshot"]["nodes"]][0] == "Whole disk"
    assert answer["helper_chat_id"] == "chat-abc"
    assert answer["last_check_at"] == 1234.0
    assert answer["history"] == []
    assert answer["is_owner"] is True


def test_the_page_says_when_a_measurement_is_running(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.update_watch(scan_started_at=1234.0)
    install_disk(monkeypatch, a_disk())
    with store.held_lock("scan", wait=False) as held:
        assert held is True
        answer = client.get("/api/state").get_json()
    assert answer["scanning"] is True
    assert answer["scan_started_at"] == 1234.0


def test_the_page_with_no_measurement_yet_answers_empty_rather_than_failing(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_disk(monkeypatch, a_disk())
    answer = client.get("/api/state").get_json()
    assert answer["snapshot"] is None
    assert answer["history"] == []
    assert answer["helper_chat_id"] is None


def test_the_status_route_answers_what_the_caretaker_asks(
    client: FlaskClient, measured: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_disk(monkeypatch, a_disk(used=93 * GIB, free=7 * GIB, percent=93))
    answer = client.get("/api/status").get_json()
    assert answer["percent_used"] == 93
    assert answer["is_over_threshold"] is True
    assert answer["safe_to_delete_bytes"] == 7 * GIB
    assert answer["safe_to_delete"][0]["path"] == "/var/cache/user/huggingface"
    assert [item["path"] for item in answer["ask_first"]] == [
        "/home/user/workspace/data/notes"
    ]


def test_measuring_again_is_asked_for_and_answered(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scan, "scan_in_background", lambda: True)
    assert client.post("/api/scan").get_json() == {"started": True}
    monkeypatch.setattr(scan, "scan_in_background", lambda: False)
    assert client.post("/api/scan").get_json() == {"started": False}


def test_the_history_and_the_measurement_are_readable_exactly_as_stored(
    client: FlaskClient, measured: dict
) -> None:
    entry = store.append_history(
        "delete", "You deleted 1 item.", deleted=[{"path": "/x"}], freed_bytes=10
    )
    raw_entry = client.get(f"/raw/history/{entry['id']}")
    assert raw_entry.status_code == 200
    assert json.loads(raw_entry.data)["deleted"] == [{"path": "/x"}]
    assert client.get("/raw/history/no-such-entry").status_code == 404
    raw_snapshot = client.get(f"/raw/snapshot/{measured['id']}")
    assert json.loads(raw_snapshot.data)["nodes"][0]["name"] == "Whole disk"
    assert client.get("/raw/snapshot/no-such-measurement").status_code == 404


def test_the_raw_du_output_a_measurement_was_built_from_is_served(
    client: FlaskClient, measured: dict
) -> None:
    (store.snapshots_dir() / measured["id"] / "du-workspace.txt").write_text(
        "/home/user/workspace\t10\n"
    )
    answer = client.get(f"/raw/snapshot/{measured['id']}/workspace")
    assert answer.status_code == 200
    assert answer.data == b"/home/user/workspace\t10\n"
    assert client.get(f"/raw/snapshot/{measured['id']}/nowhere").status_code == 404
    assert client.get(
        f"/raw/snapshot/{measured['id']}/..%2F..%2Fetc%2Fpasswd"
    ).status_code in (404, 405)
    assert client.get("/raw/snapshot/.hidden/workspace").status_code == 404


def test_a_content_security_violation_is_taken_and_not_answered_with_an_error(
    client: FlaskClient,
) -> None:
    assert (
        client.post(
            "/_csp-report",
            json={"csp-report": {"blocked-uri": "https://example.com/x.js"}},
        ).status_code
        == 204
    )
    assert (
        client.post(
            "/_csp-report", data="not json", content_type="application/json"
        ).status_code
        == 204
    )
    assert client.post("/_csp-report", json={"something": "else"}).status_code == 204


def test_the_app_describes_its_own_api_and_says_which_routes_are_the_owners(
    client: FlaskClient,
) -> None:
    document = client.get("/api/schema").get_json()
    assert {"GET /api/state", "GET /api/status", "POST /api/delete"} <= {
        f"{method.upper()} {path}"
        for path, methods in document["paths"].items()
        for method in methods
    }
    models = document["components"]["schemas"]
    for model in ("DeleteRequest", "HelperRequest", "SettingsRequest"):
        assert "Owner only" in models[model]["description"], model
    assert "Owner only" not in models["StateResponse"]["description"]
    assert "Owner only" not in models["ScanResponse"]["description"]


# Who may change anything.


def test_a_page_of_another_origin_cannot_call_the_app(client: FlaskClient) -> None:
    assert_request_guard_refuses_unknown_provenance(client, "GET", "/api/state")


def test_a_visitor_cannot_delete_files(
    client: FlaskClient, measured: dict, tmp_path: Path
) -> None:
    path, folder = a_saved_item(tmp_path)
    save_measurement(
        [
            a_node("root", None, "Whole disk"),
            a_node(
                "n1",
                "root",
                "Cache",
                size=GIB,
                path=path,
                verdict=scan.SAFE,
                item=True,
                method="remove",
            ),
        ]
    )
    for visitor in (A_PERSON, A_VISITING_AGENT):
        refused = client.post(
            "/api/delete", json={"paths": [path]}, headers=_identity(visitor)
        )
        assert refused.status_code == 403
        assert "Only the owner" in refused.get_json()["detail"]
        assert folder.exists()


def test_a_visitor_cannot_start_a_chat_or_move_the_alert_line(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands = FakeWorkspaceCommands()
    monkeypatch.setattr(runner.helper, "_run", commands.run)
    for visitor in (A_PERSON, A_VISITING_AGENT):
        assert (
            client.post("/api/helper", json={}, headers=_identity(visitor)).status_code
            == 403
        )
        assert (
            client.post(
                "/api/settings", json={"threshold": 70}, headers=_identity(visitor)
            ).status_code
            == 403
        )
    assert commands.calls == []
    assert store.read_settings()["threshold"] == 90


def test_a_visitor_may_read_what_the_app_shows(
    client: FlaskClient, measured: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_disk(monkeypatch, a_disk())
    as_a_person = client.get("/api/state", headers=_identity(A_PERSON))
    assert as_a_person.status_code == 200
    # The page greys out what a visitor may not do, and says why.
    assert as_a_person.get_json()["is_owner"] is False
    assert (
        client.get("/api/status", headers=_identity(A_VISITING_AGENT)).status_code
        == 200
    )
    assert client.get("/", headers=_identity(A_PERSON)).status_code == 200


def test_the_state_says_whether_the_reader_is_the_owner(
    client: FlaskClient, measured: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_disk(monkeypatch, a_disk())
    assert client.get("/api/state").get_json()["is_owner"] is True
    assert (
        client.get("/api/state", headers=_identity(THE_OWNER)).get_json()["is_owner"]
        is True
    )
    assert (
        client.get("/api/state", headers=_identity(A_VISITING_AGENT)).get_json()[
            "is_owner"
        ]
        is False
    )


def test_the_owner_may_delete_files_and_the_record_says_who_asked(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, folder = a_saved_item(tmp_path)
    save_measurement(
        [
            a_node("root", None, "Whole disk"),
            a_node(
                "n1",
                "root",
                "Cache",
                size=GIB,
                path=path,
                verdict=scan.SAFE,
                item=True,
                method="remove",
            ),
        ]
    )
    install_disk(monkeypatch, a_disk(free=7 * GIB), a_disk(free=8 * GIB))
    answer = client.post(
        "/api/delete", json={"paths": [path]}, headers=_identity(THE_OWNER)
    )
    assert answer.status_code == 200
    assert answer.get_json()["deleted"][0]["path"] == path
    assert store.read_history()[0]["requested_by"] == "user:owner-1"
    assert not folder.exists()


def test_a_request_with_no_identity_header_is_the_owners_and_is_noted_as_such(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, folder = a_saved_item(tmp_path)
    save_measurement(
        [
            a_node("root", None, "Whole disk"),
            a_node(
                "n1",
                "root",
                "Cache",
                size=GIB,
                path=path,
                verdict=scan.SAFE,
                item=True,
                method="remove",
            ),
        ]
    )
    install_disk(monkeypatch, a_disk())
    assert client.post("/api/delete", json={"paths": [path]}).status_code == 200
    assert store.read_history()[0]["requested_by"] == "owner"
    assert not folder.exists()


def test_a_route_that_reads_the_identity_answers_a_header_that_does_not_parse(
    client: FlaskClient,
) -> None:
    answered = client.post(
        "/api/delete", json={"paths": ["/x"]}, headers={"X-Imbue-Identity": "not json"}
    )
    assert answered.status_code == 400
    # A read that never looks at the identity does not see the difference.
    assert (
        client.get("/api/status", headers={"X-Imbue-Identity": "not json"}).status_code
        == 200
    )


# What the routes accept and refuse.


def test_deleting_asks_for_a_path(client: FlaskClient) -> None:
    answered = client.post("/api/delete", json={"paths": []})
    assert answered.status_code == 400
    assert answered.get_json()["detail"] == "Name at least one item to delete."


def test_deleting_says_what_was_refused(
    client: FlaskClient, measured: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_disk(monkeypatch, a_disk())
    asked = client.post(
        "/api/delete", json={"paths": ["/home/user/workspace/data/notes"]}
    )
    assert asked.status_code == 200
    assert asked.get_json()["deleted"] == []
    assert (
        asked.get_json()["refused"][0]["reason"]
        == "It is in Ask first: only you can approve deleting it."
    )


def test_the_alert_line_is_saved_and_clamped(client: FlaskClient) -> None:
    assert client.post("/api/settings", json={"threshold": 94}).get_json() == {
        "threshold": 94,
        "renotify_hours": 6.0,
    }
    assert (
        client.post("/api/settings", json={"threshold": 10}).get_json()["threshold"]
        == 50
    )
    assert store.read_settings()["threshold"] == 50


def test_the_helper_chat_is_started_from_the_page(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands = FakeWorkspaceCommands()
    monkeypatch.setattr(runner.helper, "_run", commands.run)
    answer = client.post("/api/helper", json={"item": "/var/tmp/leftover"})
    assert answer.get_json() == {"chat_id": "chat-abc", "created": True, "detail": ""}
    assert (
        "Start with this item the user asked about: /var/tmp/leftover"
        in commands.creates()[0][-1]
    )
    assert (
        "The user asked for help freeing disk space from the Disk Space page."
        in commands.creates()[0][-1]
    )


def test_the_helper_reuses_the_chat_it_already_made(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands = FakeWorkspaceCommands()
    monkeypatch.setattr(runner.helper, "_run", commands.run)
    first = client.post("/api/helper", json={}).get_json()
    second = client.post("/api/helper", json={}).get_json()
    assert first["created"] is True
    assert second == {"chat_id": "chat-abc", "created": False, "detail": ""}
    assert len(commands.creates()) == 1


def test_a_helper_that_could_not_start_answers_with_why(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands = FakeWorkspaceCommands()
    commands.queue(1, stderr="the chat app is not running\n")
    monkeypatch.setattr(runner.helper, "_run", commands.run)
    answer = client.post("/api/helper", json={}).get_json()
    assert answer == {
        "chat_id": "",
        "created": False,
        "detail": "the chat app is not running",
    }


def test_a_route_that_does_not_exist_answers_404(client: FlaskClient) -> None:
    assert client.get("/api/nothing").status_code == 404
    assert client.post("/api/state").status_code == 405


def test_the_health_probe_answers(client: FlaskClient) -> None:
    assert client.get("/health").get_json() == {"status": "ok"}


def test_a_measurement_is_only_ever_reported_as_it_was_saved(
    client: FlaskClient, measured: dict
) -> None:
    answer = client.get("/api/state").get_json()
    assert answer["snapshot"]["taken_at"] == measured["taken_at"]
    assert answer["snapshot"]["disk"] == measured["disk"]
    assert time.time() - answer["snapshot"]["taken_at"] < 60
