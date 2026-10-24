from collections.abc import Iterator
from pathlib import Path

import pytest

from usage_collector.feed import UsageFeed
from usage_collector.ingest import Ingestor
from usage_collector.status import IngestStatus
from usage_collector.store import UsageStore


@pytest.fixture
def store(tmp_path: Path) -> Iterator[UsageStore]:
    usage_store = UsageStore(tmp_path / "usage.db")
    yield usage_store
    usage_store.close()


@pytest.fixture
def feed() -> UsageFeed:
    return UsageFeed()


@pytest.fixture
def ingestor(store: UsageStore, feed: UsageFeed) -> Ingestor:
    return Ingestor(store, feed)


@pytest.fixture
def status() -> IngestStatus:
    return IngestStatus()
