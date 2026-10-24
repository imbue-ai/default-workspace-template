import json
from collections.abc import Iterator
from datetime import timedelta

import pytest
from flask.testing import FlaskClient

from usage_collector.feed import UsageFeed
from usage_collector.ingest import Ingestor
from usage_collector.server import create_app
from usage_collector.status import IngestStatus
from usage_collector.store import UsageStore
from usage_collector.testing import NOW, model_call_payload

_VISITOR = json.dumps({"owner": False, "user_id": "u-2", "email": "v@example.com"})


@pytest.fixture
def client(
    store: UsageStore, feed: UsageFeed, ingestor: Ingestor, status: IngestStatus
) -> Iterator[FlaskClient]:
    yield create_app(store, feed, status).test_client()


def test_a_visitor_admitted_by_the_gateway_reads_usage_like_the_owner(
    client: FlaskClient,
) -> None:
    assert (
        client.get("/v1/totals", headers={"X-Imbue-Identity": _VISITOR}).status_code
        == 200
    )


def test_ingested_events_show_up_in_totals(
    client: FlaskClient, ingestor: Ingestor
) -> None:
    ingestor.ingest(model_call_payload(attribution={"origin_id": "files"}))
    ingestor.ingest(model_call_payload(attribution={"origin_id": "routines"}))

    since = (NOW - timedelta(hours=1)).isoformat()
    totals = client.get(
        "/v1/totals", query_string={"group_by": "origin_id", "since": since}
    ).get_json()
    assert [(row["group"]["origin_id"], row["calls"]) for row in totals["rows"]] == [
        ("files", 1),
        ("routines", 1),
    ]


def test_events_cannot_be_posted(client: FlaskClient) -> None:
    assert client.post("/v1/events", json=model_call_payload()).status_code == 404


def test_the_stream_sends_each_new_event(
    client: FlaskClient, feed: UsageFeed, ingestor: Ingestor
) -> None:
    response = client.get("/v1/stream", buffered=False)
    chunks = iter(response.response)
    assert next(chunks) == b": connected\n\n"

    payload = model_call_payload()
    ingestor.ingest(payload)

    chunk = next(chunks)
    message = chunk.decode() if isinstance(chunk, bytes) else chunk
    assert message.startswith("event: model_call\ndata: ")
    assert json.loads(message.split("data: ", 1)[1])["event_id"] == payload["event_id"]
    response.close()
    assert feed.subscriber_count == 0


def test_latency_and_calls_answer_over_http(
    client: FlaskClient, ingestor: Ingestor
) -> None:
    payload = model_call_payload()
    ingestor.ingest(payload)
    since = (NOW - timedelta(hours=1)).isoformat()

    latency = client.get(
        "/v1/latency", query_string={"group_by": "model", "since": since}
    ).get_json()
    calls = client.get("/v1/calls", query_string={"origin_id": "files"}).get_json()

    assert latency["rows"][0]["p50_ms"] == 2000
    assert [call["event"]["event_id"] for call in calls["calls"]] == [
        payload["event_id"]
    ]
    assert calls["total"] == 1


def test_bad_calls_parameters_are_refused(client: FlaskClient) -> None:
    assert client.get("/v1/calls", query_string={"limit": "many"}).status_code == 400
    assert client.get("/v1/calls", query_string={"limit": "0"}).status_code == 400
    assert client.get("/v1/calls", query_string={"before": "nope"}).status_code == 400
    assert client.get("/v1/calls", query_string={"offset": "-1"}).status_code == 400


def test_health_reports_catching_up_until_a_complete_pass(
    client: FlaskClient, status: IngestStatus
) -> None:
    assert client.get("/health").get_json() == {"status": "ok", "catching_up": True}

    status.record_pass(is_complete=True)

    assert client.get("/health").get_json() == {"status": "ok", "catching_up": False}


def test_limits_answer_the_freshest_reading_per_window(
    client: FlaskClient, ingestor: Ingestor
) -> None:
    ingestor.observe_limits(
        {
            "source": "claude/usage",
            "timestamp": NOW.isoformat(),
            "rate_limits": {
                "five_hour": {"used_percentage": 27, "window_seconds": 18000}
            },
        }
    )

    answer = client.get(
        "/v1/limits", query_string={"since": (NOW - timedelta(days=1)).isoformat()}
    ).get_json()

    assert [
        (r["source"], r["window"], r["used_percentage"]) for r in answer["limits"]
    ] == [("claude", "five_hour", 27)]


def test_latency_takes_filters_over_http(
    client: FlaskClient, ingestor: Ingestor
) -> None:
    ingestor.ingest(model_call_payload(billing_mode="API_KEY"))
    ingestor.ingest(model_call_payload(billing_mode="SUBSCRIPTION"))
    since = (NOW - timedelta(hours=1)).isoformat()

    rows = client.get(
        "/v1/latency",
        query_string={
            "since": since,
            "billing_mode": "API_KEY,UNKNOWN",
            "provider": "anthropic",
        },
    ).get_json()["rows"]

    assert [row["timed_calls"] for row in rows] == [1]


def test_one_turns_calls_and_totals_by_turn_answer_over_http(
    client: FlaskClient, ingestor: Ingestor
) -> None:
    in_turn = model_call_payload(attribution={"turn_id": "request-1"})
    ingestor.ingest(in_turn)
    ingestor.ingest(model_call_payload())
    since = (NOW - timedelta(hours=1)).isoformat()

    calls = client.get("/v1/calls", query_string={"turn_id": "request-1"}).get_json()
    totals = client.get(
        "/v1/totals", query_string={"group_by": "turn_id", "since": since}
    ).get_json()

    assert [call["event"]["event_id"] for call in calls["calls"]] == [
        in_turn["event_id"]
    ]
    assert calls["total"] == 1
    assert sorted(
        (row["group"]["turn_id"] or "", row["calls"]) for row in totals["rows"]
    ) == [("", 1), ("request-1", 1)]


def test_turns_answer_over_http_with_their_total(
    client: FlaskClient, ingestor: Ingestor
) -> None:
    ingestor.ingest(model_call_payload(attribution={"turn_id": "t-1"}))
    ingestor.ingest(model_call_payload(attribution={"turn_id": "t-1"}))

    answer = client.get("/v1/turns", query_string={"origin_id": "files"}).get_json()

    assert [(turn["turn_id"], turn["calls"]) for turn in answer["turns"]] == [
        ("t-1", 2)
    ]
    assert answer["total"] == 1
    assert client.get("/v1/turns", query_string={"limit": "0"}).status_code == 400


def test_turns_over_http_count_only_the_window_since_asks_for(
    client: FlaskClient, ingestor: Ingestor
) -> None:
    ingestor.ingest(model_call_payload(attribution={"turn_id": "t-1"}))
    earlier = (NOW - timedelta(hours=1)).isoformat()
    later = (NOW + timedelta(hours=1)).isoformat()

    recent = client.get("/v1/turns", query_string={"since": earlier}).get_json()
    none_yet = client.get("/v1/turns", query_string={"since": later}).get_json()

    assert recent["total"] == 1
    assert none_yet == {"turns": [], "total": 0}
    assert client.get("/v1/turns", query_string={"since": "soon"}).status_code == 400
