import gzip
import json
from pathlib import Path

from share_gateway.request_counts import ARCHIVE_THRESHOLD_BYTES
from share_gateway.request_counts import DENIED_NO_GRANT
from share_gateway.request_counts import EVENT_SOURCE
from share_gateway.request_counts import EVENT_TYPE
from share_gateway.request_counts import MAX_KEYS_PER_WINDOW
from share_gateway.request_counts import METHOD_CLASS_READ
from share_gateway.request_counts import METHOD_CLASS_WEBSOCKET
from share_gateway.request_counts import METHOD_CLASS_WRITE
from share_gateway.request_counts import OUTCOME_ALLOWED
from share_gateway.request_counts import OVERFLOW_REQUESTER_KIND
from share_gateway.request_counts import REQUESTER_KIND_OWNER
from share_gateway.request_counts import REQUESTER_KIND_USER
from share_gateway.request_counts import RequestCountFlusher
from share_gateway.request_counts import RequestCounter
from share_gateway.request_counts import WINDOW_SECONDS
from share_gateway.request_counts import classify_method
from share_gateway.request_counts import events_path_from_environ
from share_gateway.request_counts import window_start_of

_MINUTE_START = 1_800_000_000  # a multiple of 60


class _Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _records(events_path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in events_path.read_text().splitlines() if line.strip()]


def _counter(tmp_path: Path, clock: _Clock) -> RequestCounter:
    return RequestCounter(tmp_path / "events" / EVENT_SOURCE / "events.jsonl", clock=clock)


def test_classify_method_splits_reads_writes_and_websockets() -> None:
    assert classify_method("get", False) == METHOD_CLASS_READ
    assert classify_method("HEAD", False) == METHOD_CLASS_READ
    assert classify_method("OPTIONS", False) == METHOD_CLASS_READ
    assert classify_method("POST", False) == METHOD_CLASS_WRITE
    assert classify_method("DELETE", False) == METHOD_CLASS_WRITE
    assert classify_method("GET", True) == METHOD_CLASS_WEBSOCKET


def test_window_start_floors_to_the_minute() -> None:
    assert window_start_of(_MINUTE_START + 59.9) == _MINUTE_START
    assert window_start_of(_MINUTE_START + 60.0) == _MINUTE_START + WINDOW_SECONDS


def test_events_path_follows_the_services_agents_state_dir() -> None:
    assert events_path_from_environ({}) is None
    assert events_path_from_environ({"MNGR_AGENT_STATE_DIR": "/state"}) == Path(
        f"/state/events/{EVENT_SOURCE}/events.jsonl"
    )


def test_a_finished_window_is_written_as_one_record_per_key_under_the_envelope(tmp_path: Path) -> None:
    clock = _Clock(_MINUTE_START + 5)
    counter = _counter(tmp_path, clock)
    counter.record("web", REQUESTER_KIND_USER, "user-bob", METHOD_CLASS_READ, OUTCOME_ALLOWED)
    counter.record("web", REQUESTER_KIND_USER, "user-bob", METHOD_CLASS_READ, OUTCOME_ALLOWED)
    counter.record("web", REQUESTER_KIND_OWNER, "", METHOD_CLASS_WRITE, OUTCOME_ALLOWED)
    counter.record("files", REQUESTER_KIND_USER, "user-eve", METHOD_CLASS_READ, DENIED_NO_GRANT)

    # The window is still open: nothing is written yet.
    counter.flush(is_final=False)
    assert counter.events_path is not None and not counter.events_path.exists()

    clock.now = _MINUTE_START + WINDOW_SECONDS + 1
    counter.flush(is_final=False)

    records = _records(counter.events_path)
    assert [
        (r["service"], r["requester_kind"], r["requester_id"], r["method_class"], r["outcome"], r["request_count"])
        for r in records
    ] == [
        ("files", REQUESTER_KIND_USER, "user-eve", METHOD_CLASS_READ, DENIED_NO_GRANT, 1),
        ("web", REQUESTER_KIND_OWNER, "", METHOD_CLASS_WRITE, OUTCOME_ALLOWED, 1),
        ("web", REQUESTER_KIND_USER, "user-bob", METHOD_CLASS_READ, OUTCOME_ALLOWED, 2),
    ]
    first = records[0]
    assert first["type"] == EVENT_TYPE and first["source"] == EVENT_SOURCE
    assert str(first["event_id"]).startswith("evt-") and len({r["event_id"] for r in records}) == 3
    assert first["window_start"] == "2027-01-15T08:00:00+00:00"
    assert first["window_seconds"] == WINDOW_SECONDS
    assert first["timestamp"] == "2027-01-15T08:01:00.000000000Z"
    assert set(first) == {
        "timestamp",
        "type",
        "event_id",
        "source",
        "window_start",
        "window_seconds",
        "service",
        "requester_kind",
        "requester_id",
        "method_class",
        "outcome",
        "request_count",
    }

    # A drained window is gone: a second flush writes nothing more.
    counter.flush(is_final=False)
    assert len(_records(counter.events_path)) == 3


def test_a_final_flush_writes_the_open_window_too(tmp_path: Path) -> None:
    clock = _Clock(_MINUTE_START + 5)
    counter = _counter(tmp_path, clock)
    counter.record("web", REQUESTER_KIND_OWNER, "", METHOD_CLASS_READ, OUTCOME_ALLOWED)

    counter.flush(is_final=True)

    assert counter.events_path is not None
    assert [r["request_count"] for r in _records(counter.events_path)] == [1]


def test_keys_past_the_cap_fold_into_one_overflow_record(tmp_path: Path) -> None:
    clock = _Clock(_MINUTE_START + 5)
    counter = _counter(tmp_path, clock)
    for index in range(MAX_KEYS_PER_WINDOW + 10):
        counter.record("web", REQUESTER_KIND_USER, f"user-{index}", METHOD_CLASS_READ, OUTCOME_ALLOWED)
    # A key the window already holds is still counted under itself past the cap.
    counter.record("web", REQUESTER_KIND_USER, "user-0", METHOD_CLASS_READ, OUTCOME_ALLOWED)

    counter.flush(is_final=True)

    assert counter.events_path is not None
    records = _records(counter.events_path)
    overflow = [r for r in records if r["requester_kind"] == OVERFLOW_REQUESTER_KIND]
    assert len(records) == MAX_KEYS_PER_WINDOW + 1
    assert [
        (r["service"], r["requester_id"], r["method_class"], r["outcome"], r["request_count"]) for r in overflow
    ] == [("", "", "", OUTCOME_ALLOWED, 10)]
    assert [r["request_count"] for r in records if r["requester_id"] == "user-0"] == [2]


def test_each_window_keeps_its_own_counts(tmp_path: Path) -> None:
    clock = _Clock(_MINUTE_START + 5)
    counter = _counter(tmp_path, clock)
    counter.record("web", REQUESTER_KIND_OWNER, "", METHOD_CLASS_READ, OUTCOME_ALLOWED)
    clock.now = _MINUTE_START + WINDOW_SECONDS + 5
    counter.record("web", REQUESTER_KIND_OWNER, "", METHOD_CLASS_READ, OUTCOME_ALLOWED)

    counter.flush(is_final=True)

    assert counter.events_path is not None
    assert [(r["window_start"], r["request_count"]) for r in _records(counter.events_path)] == [
        ("2027-01-15T08:00:00+00:00", 1),
        ("2027-01-15T08:01:00+00:00", 1),
    ]


def test_an_unset_state_dir_counts_nothing(tmp_path: Path) -> None:
    counter = RequestCounter(None, clock=_Clock(_MINUTE_START))
    counter.record("web", REQUESTER_KIND_OWNER, "", METHOD_CLASS_READ, OUTCOME_ALLOWED)
    counter.flush(is_final=True)
    assert counter.drain(is_final=True) == []
    assert sorted(tmp_path.iterdir()) == []


def test_an_oversized_stream_is_archived_before_the_next_write(tmp_path: Path) -> None:
    clock = _Clock(_MINUTE_START + 5)
    counter = _counter(tmp_path, clock)
    events_path = counter.events_path
    assert events_path is not None
    events_path.parent.mkdir(parents=True)
    oversized = b'{"type": "old"}\n' * (ARCHIVE_THRESHOLD_BYTES // 16 + 1)
    events_path.write_bytes(oversized)
    counter.record("web", REQUESTER_KIND_OWNER, "", METHOD_CLASS_READ, OUTCOME_ALLOWED)

    counter.flush(is_final=True)

    assert [r["request_count"] for r in _records(events_path)] == [1]
    archives = sorted(events_path.parent.glob("events.jsonl.*"))
    assert [path.suffix for path in archives] == [".gz"]
    assert gzip.decompress(archives[0].read_bytes()) == oversized


def test_a_stream_staged_by_a_gateway_that_died_is_archived_by_the_next_flush(tmp_path: Path) -> None:
    clock = _Clock(_MINUTE_START + 5)
    counter = _counter(tmp_path, clock)
    events_path = counter.events_path
    assert events_path is not None
    events_path.parent.mkdir(parents=True)
    # Left behind by a gateway stopped after staging a stream but before compressing it; the new stream is small.
    staged_path = events_path.with_name("events.jsonl.20260501000000000000.archiving")
    staged_path.write_bytes(b'{"type": "old"}\n')
    events_path.write_bytes(b'{"type": "current"}\n')
    counter.record("web", REQUESTER_KIND_OWNER, "", METHOD_CLASS_READ, OUTCOME_ALLOWED)

    counter.flush(is_final=True)

    assert not staged_path.exists()
    archive_path = events_path.with_name("events.jsonl.20260501000000000000.gz")
    assert gzip.decompress(archive_path.read_bytes()) == b'{"type": "old"}\n'
    assert [r.get("type") for r in _records(events_path)] == ["current", EVENT_TYPE]


def test_the_flusher_writes_everything_when_stopped(tmp_path: Path) -> None:
    clock = _Clock(_MINUTE_START + 5)
    counter = _counter(tmp_path, clock)
    flusher = RequestCountFlusher(counter, interval_seconds=3600.0)
    flusher.start()
    counter.record("web", REQUESTER_KIND_OWNER, "", METHOD_CLASS_READ, OUTCOME_ALLOWED)

    flusher.stop()

    assert counter.events_path is not None
    assert [r["request_count"] for r in _records(counter.events_path)] == [1]
