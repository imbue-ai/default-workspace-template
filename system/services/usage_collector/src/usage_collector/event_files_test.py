import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from usage_collector.event_files import event_files, follow_event_files
from usage_collector.feed import UsageFeed
from usage_collector.ingest import Ingestor
from usage_collector.store import UsageStore
from usage_collector.testing import NOW, model_call_payload


def _event_file(host_dir: Path, root: str = "agents", agent: str = "agent-1") -> Path:
    path = host_dir / root / agent / "events" / "claude" / "usage" / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _calls(store: UsageStore) -> int:
    rows = store.totals([], NOW - timedelta(days=1))
    return rows[0].calls if rows else 0


def test_model_call_lines_are_ingested_and_other_lines_skipped(
    tmp_path: Path, store: UsageStore, ingestor: Ingestor
) -> None:
    path = _event_file(tmp_path)
    path.write_text(
        json.dumps(
            {"timestamp": "2026-10-22T00:00:00Z", "session_id": "legacy-snapshot"}
        )
        + "\n"
        + json.dumps(model_call_payload())
        + "\n"
        + '{"schema_version": 1, "half a line'
    )

    assert (
        follow_event_files(
            event_files(tmp_path, tmp_path / "workspace-events"),
            store,
            ingestor.ingest,
            "ws-host",
        ).events_read
        == 1
    )
    assert _calls(store) == 1


def test_reading_resumes_where_it_left_off_including_a_finished_partial_line(
    tmp_path: Path, store: UsageStore, ingestor: Ingestor
) -> None:
    path = _event_file(tmp_path)
    second = json.dumps(model_call_payload())
    path.write_text(json.dumps(model_call_payload()) + "\n" + second[:20])
    follow_event_files(
        event_files(tmp_path, tmp_path / "workspace-events"),
        store,
        ingestor.ingest,
        "ws-host",
    )

    with path.open("a") as handle:
        handle.write(second[20:] + "\n")

    assert (
        follow_event_files(
            event_files(tmp_path, tmp_path / "workspace-events"),
            store,
            ingestor.ingest,
            "ws-host",
        ).events_read
        == 1
    )
    assert (
        follow_event_files(
            event_files(tmp_path, tmp_path / "workspace-events"),
            store,
            ingestor.ingest,
            "ws-host",
        ).events_read
        == 0
    )
    assert _calls(store) == 2


def test_a_truncated_file_is_read_again_from_the_start_without_double_counting(
    tmp_path: Path, store: UsageStore, ingestor: Ingestor
) -> None:
    path = _event_file(tmp_path)
    first = json.dumps(model_call_payload())
    path.write_text(first + "\n" + json.dumps(model_call_payload()) + "\n")
    follow_event_files(
        event_files(tmp_path, tmp_path / "workspace-events"),
        store,
        ingestor.ingest,
        "ws-host",
    )

    path.write_text(first + "\n")

    follow_event_files(
        event_files(tmp_path, tmp_path / "workspace-events"),
        store,
        ingestor.ingest,
        "ws-host",
    )
    assert _calls(store) == 2


def test_preserved_agents_files_are_read_too(
    tmp_path: Path, store: UsageStore, ingestor: Ingestor
) -> None:
    _event_file(tmp_path, root="preserved", agent="old--agent-0").write_text(
        json.dumps(model_call_payload()) + "\n"
    )

    assert (
        follow_event_files(
            event_files(tmp_path, tmp_path / "workspace-events"),
            store,
            ingestor.ingest,
            "ws-host",
        ).events_read
        == 1
    )


def test_agent_events_are_attributed_to_this_workspace(
    tmp_path: Path, store: UsageStore, ingestor: Ingestor
) -> None:
    _event_file(tmp_path).write_text(
        json.dumps(
            model_call_payload(attribution={"workspace_id": "MacBook-of-the-agent"})
        )
        + "\n"
    )

    follow_event_files(
        event_files(tmp_path, tmp_path / "workspace-events"),
        store,
        ingestor.ingest,
        "ws-host",
    )

    [row] = store.totals(["origin_kind"], NOW - timedelta(days=1))
    assert row.calls == 1
    [raw] = store.totals(["harness", "origin_id"], NOW - timedelta(days=1))
    assert raw.calls == 1
    assert _workspace_ids(store) == ["ws-host"]


def _workspace_ids(store: UsageStore) -> list[str]:
    return [
        row.group["workspace_id"] or ""
        for row in store.totals(["workspace_id"], NOW - timedelta(days=1))
    ]


def test_workspace_usage_files_are_followed_beside_the_agents(
    tmp_path: Path, store: UsageStore, ingestor: Ingestor
) -> None:
    workspace_events = tmp_path / "usage" / "events"
    workspace_events.mkdir(parents=True)
    (workspace_events / "app-files.jsonl").write_text(
        json.dumps(model_call_payload()) + "\n"
    )
    _event_file(tmp_path).write_text(json.dumps(model_call_payload()) + "\n")

    paths = list(event_files(tmp_path, workspace_events))

    assert [path.name for path in paths] == ["events.jsonl", "app-files.jsonl"]
    assert follow_event_files(paths, store, ingestor.ingest, "ws-host").events_read == 2


def test_a_lost_store_is_rebuilt_from_the_files(
    tmp_path: Path, store: UsageStore, ingestor: Ingestor
) -> None:
    workspace_events = tmp_path / "events"
    workspace_events.mkdir()
    (workspace_events / "app-files.jsonl").write_text(
        json.dumps(model_call_payload()) + "\n"
    )
    follow_event_files(
        event_files(None, workspace_events), store, ingestor.ingest, "ws-host"
    )

    rebuilt_store = UsageStore(tmp_path / "rebuilt.db")
    rebuilt = Ingestor(rebuilt_store, UsageFeed())
    follow_event_files(
        event_files(None, workspace_events), rebuilt_store, rebuilt.ingest, "ws-host"
    )

    assert _calls(rebuilt_store) == 1
    rebuilt_store.close()


def test_an_agents_snapshot_events_give_its_subscription_limits(
    tmp_path: Path, store: UsageStore, ingestor: Ingestor
) -> None:
    _event_file(tmp_path).write_text(
        json.dumps(
            {
                "source": "claude/usage",
                "timestamp": "2026-10-22T12:00:00Z",
                "session_id": "s-1",
                "rate_limits": {"seven_day": {"used_percentage": 7}},
            }
        )
        + "\n"
    )

    follow_event_files(
        event_files(tmp_path, tmp_path / "workspace-events"),
        store,
        ingestor.ingest,
        "ws-host",
        ingestor.observe_limits,
    )

    [reading] = store.limits(datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert (reading.source, reading.window, reading.used_percentage) == (
        "claude",
        "seven_day",
        7,
    )


def test_a_preserved_copy_read_after_its_events_were_pruned_counts_nothing_twice(
    tmp_path: Path, store: UsageStore, ingestor: Ingestor
) -> None:
    long_ago = NOW - timedelta(days=120)
    live = _event_file(tmp_path)
    live.write_text(
        json.dumps(
            model_call_payload(
                started_at=(long_ago - timedelta(seconds=2)).isoformat(),
                ended_at=long_ago.isoformat(),
            )
        )
        + "\n"
    )
    follow_event_files([live], store, ingestor.ingest, "ws-host")
    store.prune(NOW)
    preserved = _event_file(tmp_path, root="preserved")
    preserved.write_bytes(live.read_bytes())
    live.unlink()

    follow_event_files([preserved], store, ingestor.ingest, "ws-host")

    [row] = store.totals([], long_ago - timedelta(days=1))
    assert row.calls == 1
