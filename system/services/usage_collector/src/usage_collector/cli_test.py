import threading
from pathlib import Path

from usage_collector.cli import run_maintenance
from usage_collector.feed import UsageFeed
from usage_collector.ingest import Ingestor
from usage_collector.status import IngestStatus
from usage_collector.store import UsageStore


def test_a_failing_pass_marks_the_store_behind_instead_of_ending_maintenance(
    tmp_path: Path, status: IngestStatus
) -> None:
    closed_store = UsageStore(tmp_path / "usage.db")
    closed_store.close()
    status.record_pass(True)
    stop = threading.Event()
    stop.set()

    run_maintenance(
        Ingestor(closed_store, UsageFeed()),
        closed_store,
        {"IMBUE_USAGE_DATA_DIR": str(tmp_path / "usage")},
        status,
        stop,
    )

    assert status.is_catching_up
