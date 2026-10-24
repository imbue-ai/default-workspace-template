import json
from datetime import timedelta
from pathlib import Path

import pytest
from flask.testing import FlaskClient

from usage_collector.ingest import Ingestor
from usage_collector.testing import NOW
from usage_collector.testing import model_call_payload
from usage_dashboard.settings import UsageSettingsStore
from usage_dashboard.settings import WidgetVariant

_VISITOR = json.dumps({"owner": False, "user_id": "u-2"})
# What GET /api/settings answers while nothing usable is stored.
_DEFAULT_SETTINGS = {
    "widget_variant": "rings",
    "widget_size": "medium",
    "widget_detail": "standard",
    "widget_tray": "rings",
    "widget_place": "desktop",
    "widget_toolbar": "chip",
}


def test_totals_pass_through_with_their_query(client: FlaskClient, ingestor: Ingestor) -> None:
    ingestor.ingest(model_call_payload(attribution={"origin_id": "files"}))
    ingestor.ingest(model_call_payload(attribution={"origin_id": "routines"}))

    response = client.get(
        "/api/usage/totals",
        query_string={"group_by": "origin_id", "since": (NOW - timedelta(hours=1)).isoformat()},
    )

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert [(row["group"]["origin_id"], row["calls"]) for row in response.get_json()["rows"]] == [
        ("files", 1),
        ("routines", 1),
    ]


def test_turns_pass_through(client: FlaskClient, ingestor: Ingestor) -> None:
    ingestor.ingest(model_call_payload(attribution={"turn_id": "t-1"}))
    ingestor.ingest(model_call_payload(attribution={"turn_id": "t-1"}))

    answer = client.get("/api/usage/turns", query_string={"limit": "5"}).get_json()

    assert [(turn["turn_id"], turn["calls"]) for turn in answer["turns"]] == [("t-1", 2)]
    assert answer["total"] == 1


def test_latency_and_calls_pass_through(client: FlaskClient, ingestor: Ingestor) -> None:
    payload = model_call_payload()
    ingestor.ingest(payload)

    latency = client.get(
        "/api/usage/latency", query_string={"since": (NOW - timedelta(hours=1)).isoformat()}
    ).get_json()
    calls = client.get("/api/usage/calls", query_string={"model": "anthropic/claude-opus-5-5"}).get_json()

    assert latency["rows"][0]["p50_ms"] == 2000
    assert [call["event"]["event_id"] for call in calls["calls"]] == [payload["event_id"]]


def test_a_collector_error_keeps_its_status_and_detail(client: FlaskClient) -> None:
    response = client.get("/api/usage/totals", query_string={"group_by": "nonsense"})

    assert response.status_code == 400
    assert "Cannot group by" in response.get_json()["detail"]


def test_a_visitor_admitted_by_the_gateway_reads_usage_like_the_owner(client: FlaskClient, ingestor: Ingestor) -> None:
    ingestor.ingest(model_call_payload())

    for path in ("/api/usage/totals", "/api/usage/calls", "/api/usage/health", "/api/apps"):
        response = client.get(path, headers={"X-Imbue-Identity": _VISITOR})
        assert response.status_code == 200, path


def test_an_unknown_read_is_not_found(client: FlaskClient) -> None:
    assert client.get("/api/usage/events").status_code == 404


def test_an_unreachable_collector_is_503(unreachable_client: FlaskClient) -> None:
    for path in ("/api/usage/totals", "/api/usage/health", "/api/usage/stream"):
        response = unreachable_client.get(path)
        assert response.status_code == 503, path
        assert response.get_json()["detail"] == "The usage collector isn't responding."


def test_collector_health_reports_catching_up(client: FlaskClient) -> None:
    assert client.get("/api/usage/health").get_json() == {"status": "ok", "catching_up": True}


def test_app_names_come_from_the_registry(client: FlaskClient) -> None:
    assert client.get("/api/apps").get_json() == {"display_names": {"files": "Files", "chat": "Chat"}}


def test_source_names_are_the_chat_apps_titles_and_the_routines_titles(client: FlaskClient) -> None:
    assert client.get("/api/source-names").get_json() == {
        "chats": {"agent-1": "Seattle weather", "agent-2": "News"},
        "routines": {"us-news": "US news"},
    }


def test_source_names_are_empty_when_the_chat_app_cannot_be_asked(unreachable_client: FlaskClient) -> None:
    assert unreachable_client.get("/api/source-names").get_json() == {"chats": {}, "routines": {}}


def test_the_page_and_its_assets_are_served(client: FlaskClient) -> None:
    page = client.get("/")
    asset = client.get("/assets/app-Ab12cd34.js")

    assert page.status_code == 200 and b"<title>Usage</title>" in page.data
    assert asset.status_code == 200 and "max-age" in asset.headers["Cache-Control"]
    assert client.get("/api/health").get_json() == {"status": "ok", "is_frontend_built": True}


def test_the_widget_page_is_served_beside_the_page(client: FlaskClient, static_directory: Path) -> None:
    widget = client.get("/widget")

    assert widget.status_code == 200 and b"<title>Usage widget</title>" in widget.data
    assert widget.headers["Cache-Control"] == "no-store"
    (static_directory / "widget.html").unlink()
    assert b"has not been built yet" in client.get("/widget").data


def test_limits_pass_through(client: FlaskClient, ingestor: Ingestor) -> None:
    ingestor.observe_limits(
        {
            "source": "codex/usage",
            "timestamp": NOW.isoformat(),
            "rate_limits": {"primary": {"used_percentage": 40, "window_seconds": 18000}},
        }
    )

    answer = client.get("/api/usage/limits", query_string={"since": (NOW - timedelta(days=1)).isoformat()}).get_json()

    assert [(row["source"], row["used_percentage"]) for row in answer["limits"]] == [("codex", 40)]


def test_the_detail_pages_serve_the_same_document(client: FlaskClient) -> None:
    for path in ("/all", "/p/subscription:anthropic"):
        response = client.get(path)
        assert response.status_code == 200 and b"<title>Usage</title>" in response.data, path


def test_the_widget_look_defaults_to_medium_standard_rings_and_is_kept_once_set(
    client: FlaskClient, settings_store: UsageSettingsStore
) -> None:
    assert client.get("/api/settings").get_json() == _DEFAULT_SETTINGS

    stored = {
        "widget_variant": "bars",
        "widget_size": "large",
        "widget_detail": "more",
        "widget_tray": "stacked",
        "widget_place": "toolbar",
        "widget_toolbar": "meters",
    }
    written = client.post("/api/settings", json=stored)

    assert written.status_code == 200
    assert written.get_json() == stored
    assert client.get("/api/settings").get_json() == stored
    assert settings_store.read().widget_variant is WidgetVariant.BARS
    assert client.post("/api/settings", json={"widget_variant": "minimal"}).get_json()["widget_variant"] == "minimal"
    assert client.post("/api/settings", json={"widget_variant": "dials"}).status_code == 400
    assert client.post("/api/settings", json={"widget_size": "huge"}).status_code == 400
    assert client.post("/api/settings", json={"widget_tray": "sparkle"}).status_code == 400
    assert client.post("/api/settings", json={"widget_place": "dock"}).status_code == 400
    assert client.post("/api/settings", json={"widget_toolbar": "sparkle"}).status_code == 400


@pytest.mark.parametrize("content", ["{not json", '{"widget_place": "dock"}'], ids=["not-json", "unknown-choice"])
def test_an_unreadable_or_unfitting_settings_file_reads_as_the_defaults(
    client: FlaskClient, settings_store: UsageSettingsStore, content: str
) -> None:
    settings_store.path.parent.mkdir(parents=True)
    settings_store.path.write_text(content)

    assert client.get("/api/settings").get_json() == _DEFAULT_SETTINGS
