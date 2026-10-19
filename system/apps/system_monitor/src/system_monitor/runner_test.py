"""Tests for the routes: the page with the shell script injected, the stats endpoint, the shell modules, health, and
the API document."""

from pathlib import Path
from typing import Any

import pytest
from app_schema.testing import assert_api_schema_describes_app
from flask.testing import FlaskClient

from system_monitor import memory, runner, stats


@pytest.fixture
def client() -> FlaskClient:
    return runner.app.test_client()


def test_the_page_is_served_with_the_shell_script_before_the_closing_body(
    client: FlaskClient,
) -> None:
    response = client.get("/")

    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert "<title>Memory &amp; disk</title>" in page
    assert page.index(
        'import { connectToShell } from "/_static/app_contract.js"'
    ) < page.index("</body>")


def test_the_stats_endpoint_answers_the_snapshot_uncached(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot: dict[str, Any] = {
        "memory": {"total": 1, "available": 0, "used": 1},
        "disk": {"total": 8, "used": 3, "free": 5},
        "usage": [
            {"id": "chats", "label": "Chats", "hint": "", "bytes": 1, "items": [{"name": "Inbox", "bytes": 1}]},
            {"id": "scratch", "label": "Temporary files", "hint": "", "bytes": 0, "items": [], "limit": 2},
        ],
        "usage_mode": "exact",
        "stops": [{"at": "2026-10-17T00:00:00+00:00", "what": "a chat's agent", "command": None}],
    }
    monkeypatch.setattr(runner, "SNAPSHOT", stats.SharedSnapshot(lambda: snapshot))
    history = memory.MemoryHistory(
        read=lambda: {"total": 4, "available": 1, "used": 3}, clock=lambda: 2.0
    )
    history.record()
    monkeypatch.setattr(runner, "HISTORY", history)

    response = client.get("/api/stats")

    # Every row carries limit on the wire; only the scratch row has one.
    chats_row, scratch_row = snapshot["usage"]
    assert response.get_json() == {
        **snapshot,
        "usage": [{**chats_row, "limit": None}, scratch_row],
        "history": [{"t": 2000, "v": 0.75}],
    }
    assert response.headers["Cache-Control"] == "no-store"


def test_only_the_two_shell_modules_are_served(client: FlaskClient) -> None:
    assert client.get("/_static/runner.py").status_code == 404
    assert client.get("/_static/..%2Frunner.py").status_code == 404


def test_health_answers_ok(client: FlaskClient) -> None:
    assert client.get("/health").get_json() == {"status": "ok"}


def test_the_app_describes_its_api_the_way_its_manifest_says(client: FlaskClient) -> None:
    assert_api_schema_describes_app(client, Path(__file__).resolve().parents[2] / "app.toml")
