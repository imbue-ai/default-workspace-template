"""The one path every model_call takes into the store: validate, price, insert, publish."""

from collections.abc import Mapping
from enum import auto

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.mngr_usage.model_call import ModelCallEvent, price_model_call
from loguru import logger
from pydantic import ValidationError

from usage_collector.feed import UsageFeed
from usage_collector.limits import limit_readings
from usage_collector.store import UsageStore


class IngestOutcome(UpperCaseStrEnum):
    STORED = auto()
    DUPLICATE = auto()
    REJECTED = auto()


def describe_counts(counts: Mapping[IngestOutcome, int]) -> dict[str, int]:
    return {str(outcome).lower(): count for outcome, count in counts.items()}


class Ingestor:
    """Validates raw events against the model_call schema, prices token-only ones, stores and publishes them."""

    def __init__(self, store: UsageStore, feed: UsageFeed) -> None:
        self._store = store
        self._feed = feed

    def ingest(self, payload: object) -> IngestOutcome:
        """Ingest one raw event read from an event file; a rejected one is logged and skipped."""
        try:
            event = ModelCallEvent.model_validate(payload)
        except ValidationError as e:
            logger.warning(
                "Skipped a model_call event that does not fit the schema: {}", e
            )
            return IngestOutcome.REJECTED
        return self.ingest_event(event)

    def observe_limits(self, record: object) -> None:
        """Keep the subscription limit readings an event carries, model_call or not."""
        self._store.record_limits(limit_readings(record))

    def ingest_event(self, event: ModelCallEvent) -> IngestOutcome:
        priced = price_model_call(event)
        if not self._store.insert(priced):
            return IngestOutcome.DUPLICATE
        self._feed.publish(priced)
        return IngestOutcome.STORED
