"""Tests for the routes: the page on each of its views with the shell script injected, the stats, history and disk
endpoints, the shell modules, health, and the API document."""

import threading
from pathlib import Path
from typing import Any

import pytest
from app_schema.testing import assert_api_schema_describes_app
from flask.testing import FlaskClient

from system_monitor import actions, disk, runner, stats


@pytest.fixture
def client() -> FlaskClient:
    return runner.app.test_client()


@pytest.mark.parametrize("path", ["/", "/cpu", "/memory", "/disk"])
def test_the_page_is_served_on_every_view_with_the_shell_script_before_the_closing_body(
    client: FlaskClient, path: str
) -> None:
    response = client.get(path)

    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert "<title>System Monitor</title>" in page
    assert page.index(
        'import { connectToShell } from "/_static/app_contract.js"'
    ) < page.index("</body>")


def test_the_stats_endpoint_answers_the_snapshot_uncached(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot: dict[str, Any] = {
        "cpu": {"usage": 0.37, "cores": 4, "processes": 150, "sampled_at": 1000.0},
        "memory": {"total": 1, "available": 0, "used": 1},
        "disk": {"total": 8, "used": 3, "free": 5},
        "usage": [
            {
                "id": "chats",
                "label": "Chats",
                "hint": "",
                "bytes": 1,
                "cpu": 0.24,
                "items": [{"name": "Inbox", "bytes": 1, "cpu": 0.24, "stop": None}],
            },
            {
                "id": "scratch",
                "label": "Temporary files",
                "hint": "",
                "bytes": 0,
                "cpu": None,
                "items": [],
                "limit": 2,
            },
        ],
        "usage_mode": "exact",
        "stops": [
            {
                "at": "2026-10-17T00:00:00+00:00",
                "what": "a chat's agent",
                "command": None,
            }
        ],
        "events": [
            {
                "at": "2026-10-17T00:00:00+00:00",
                "kind": "stop",
                "what": "Stopped a chat's agent to free memory",
                "duration_seconds": None,
                "top": [],
            },
            {
                "at": "2026-10-16T00:00:00+00:00",
                "kind": "busy",
                "what": "CPU above 90% for 8 minutes (mostly a chat)",
                "duration_seconds": 504,
                "top": [{"name": "a chat", "share": 0.7}],
            },
        ],
        "history": [{"t": 2000, "cpu": 0.3, "memory": 0.75}],
    }
    monkeypatch.setattr(runner, "SNAPSHOT", stats.SharedSnapshot(lambda: snapshot))

    response = client.get("/api/stats")

    # Every row carries limit on the wire; only the scratch row has one.
    chats_row, scratch_row = snapshot["usage"]
    assert response.get_json() == {
        **snapshot,
        "usage": [{**chats_row, "limit": None}, scratch_row],
    }
    assert response.headers["Cache-Control"] == "no-store"


def test_the_history_endpoint_answers_a_span_and_refuses_another(
    client: FlaskClient,
) -> None:
    response = client.get("/api/history?span=24h")

    assert response.status_code == 200
    body = response.get_json()
    assert (body["span"], body["step_seconds"]) == ("24h", 300)
    assert isinstance(body["samples"], list)
    assert client.get("/api/history?span=3d").status_code == 400


def test_the_disk_endpoint_answers_the_figures_the_backups_and_an_idle_walk(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(stats, "WORKSPACE_DIR", tmp_path)
    # No state dir means no backup events to read, so the backups are not configured.
    monkeypatch.delenv("MNGR_AGENT_STATE_DIR", raising=False)

    response = client.get("/api/disk")

    assert response.status_code == 200
    body = response.get_json()
    assert set(body["disk"]) == {"total", "used", "free"}
    assert body["disk"]["total"] > 0
    assert body["backups"]["state"] == "not_configured"
    assert body["backups"]["last_success_at"] is None
    assert body["backups"]["readme_path"].endswith("host_backup/README.md")
    # Nothing has asked for a walk of the disk.
    assert body["scan"] == {
        "state": "idle",
        "scanned_at": None,
        "rows": [],
        "missing": [],
    }


def test_starting_a_disk_walk_answers_202_then_200_while_it_runs(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(stats, "WORKSPACE_DIR", tmp_path)
    monkeypatch.delenv("MNGR_AGENT_STATE_DIR", raising=False)
    gate = threading.Event()
    rows = [
        {"id": "other", "label": "Everything else", "hint": "", "bytes": 7, "items": []}
    ]
    scanner = disk.DiskScanner(run=lambda: (gate.wait(10), (rows, ["caches"]))[1])
    monkeypatch.setattr(runner, "DISK_SCANNER", scanner)

    try:
        started = client.post("/api/disk/scan")
        assert started.status_code == 202
        assert started.get_json()["scan"]["state"] == "scanning"
        # A second request while the walk runs starts none.
        repeated = client.post("/api/disk/scan")
        assert repeated.status_code == 200
        assert repeated.get_json()["scan"]["state"] == "scanning"
    finally:
        gate.set()
    scanner.wait(10.0)

    body = client.get("/api/disk").get_json()
    assert body["scan"]["state"] == "done"
    assert body["scan"]["scanned_at"] is not None
    assert body["scan"]["rows"] == rows and body["scan"]["missing"] == ["caches"]


def test_only_the_two_shell_modules_are_served(client: FlaskClient) -> None:
    assert client.get("/_static/runner.py").status_code == 404
    assert client.get("/_static/..%2Frunner.py").status_code == 404


def test_health_answers_ok(client: FlaskClient) -> None:
    assert client.get("/health").get_json() == {"status": "ok"}


def test_the_app_describes_its_api_the_way_its_manifest_says(
    client: FlaskClient,
) -> None:
    assert_api_schema_describes_app(
        client, Path(__file__).resolve().parents[2] / "app.toml"
    )


def test_the_stop_endpoint_runs_the_request_and_relays_a_refusal(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[tuple[str, str]] = []

    def stop(kind: str, target_id: str) -> None:
        asked.append((kind, target_id))
        if kind == "app":
            raise actions.StopRefusedError(
                403, "Only the apps you built can be quit from here"
            )

    monkeypatch.setattr(runner, "STOP", stop)

    stopped = client.post("/api/stop", json={"kind": "chat", "id": "chat-123"})
    refused = client.post("/api/stop", json={"kind": "app", "id": "chat"})
    nonsense = client.post("/api/stop", json={"kind": "service", "id": "sshd"})
    empty = client.post("/api/stop")

    assert (stopped.status_code, stopped.get_json()) == (200, {"status": "ok"})
    assert (refused.status_code, refused.get_json()) == (
        403,
        {"detail": "Only the apps you built can be quit from here"},
    )
    assert nonsense.status_code == 400
    assert "kind" in nonsense.get_json()["detail"]
    assert empty.status_code == 400
    # Only well-formed requests reach the action.
    assert asked == [("chat", "chat-123"), ("app", "chat")]
