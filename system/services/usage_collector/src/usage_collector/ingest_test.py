import json
from datetime import timedelta

from usage_collector.feed import UsageFeed
from usage_collector.ingest import Ingestor, IngestOutcome
from usage_collector.store import UsageStore
from usage_collector.testing import NOW, model_call_payload


def test_ingest_prices_stores_and_publishes(
    ingestor: Ingestor, store: UsageStore, feed: UsageFeed
) -> None:
    subscription = feed.subscribe()

    assert ingestor.ingest(model_call_payload()) == IngestOutcome.STORED

    published = json.loads(subscription.events.get_nowait())
    assert published["cost_provenance"] == "ESTIMATED"
    assert store.totals([], NOW - timedelta(hours=1))[0].calls == 1


def test_a_replayed_event_is_a_duplicate_and_not_republished(
    ingestor: Ingestor, feed: UsageFeed
) -> None:
    payload = model_call_payload()
    ingestor.ingest(payload)
    subscription = feed.subscribe()

    assert ingestor.ingest(payload) == IngestOutcome.DUPLICATE
    assert subscription.events.empty()


def test_an_event_that_does_not_fit_the_schema_is_rejected(
    ingestor: Ingestor, store: UsageStore
) -> None:
    assert (
        ingestor.ingest(model_call_payload(cost_usd="0.10"))
        == IngestOutcome.REJECTED
    )
    assert store.totals([], NOW - timedelta(hours=1))[0].calls == 0


def test_a_newer_writers_extra_field_is_dropped_and_the_call_still_counts(
    ingestor: Ingestor, store: UsageStore, feed: UsageFeed
) -> None:
    subscription = feed.subscribe()

    assert (
        ingestor.ingest(model_call_payload(prompt="never stored"))
        == IngestOutcome.STORED
    )

    assert "never stored" not in subscription.events.get_nowait()
    assert store.totals([], NOW - timedelta(hours=1))[0].calls == 1
