import json
from collections.abc import Sequence
from pathlib import Path

from imbue.analytics.injected.collect import run_collection
from imbue.analytics.injected.workspace_redaction import RedactionError
from imbue.analytics.protocol import parse_collection_output


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records))


def _event(event_id: str, event_type: str, source: str, **extra: object) -> dict:
    return {
        "timestamp": "2026-08-18T12:00:00.000000000Z",
        "event_id": event_id,
        "type": event_type,
        "source": source,
        **extra,
    }


def _build_fixture_workspace(tmp_path: Path) -> tuple[Path, Path]:
    workspace_root = tmp_path / "workspace"
    host_dir = tmp_path / ".mngr"
    workspace_root.mkdir()
    _write_jsonl(
        host_dir / "agents/agent-abc/events/claude/common_transcript/events.jsonl",
        [_event("evt-t1", "user_message", "claude", content="hello there")],
    )
    _write_jsonl(
        host_dir / "agents/agent-abc/workspace_layout/events/client_activity/events.jsonl",
        [_event("evt-c1", "message", "client_activity", client_id="c1", message_text="hi")],
    )
    _write_jsonl(
        host_dir / "agents/agent-abc/events/services/events.jsonl",
        [_event("evt-s1", "service_registered", "services", name="terminal")],
    )
    return workspace_root, host_dir


def _no_findings(texts: Sequence[str]) -> list[set[int]]:
    return [set() for _ in texts]


def _keep_text(text: str) -> str:
    return text


def test_run_collection_emits_a_stream_the_runner_parser_accepts(tmp_path: Path) -> None:
    """The emitter/parser conformance test: collect's output validates cleanly."""
    workspace_root, host_dir = _build_fixture_workspace(tmp_path)
    emitted_lines: list[str] = []

    state = run_collection(
        workspace_root=workspace_root,
        host_dir=host_dir,
        run_id="run-1",
        script_version="abc123",
        cursor_by_source={},
        budget_bytes=10_000_000,
        scan_texts=_no_findings,
        scrub_pii=_keep_text,
        emit_line=lambda payload: emitted_lines.append(json.dumps(payload, sort_keys=True)),
    )

    parsed = parse_collection_output("\n".join(emitted_lines))
    assert parsed.dropped_line_count == 0
    assert [record.event_id for record in parsed.transcript_records] == ["evt-t1"]
    assert {record.event_id for record in parsed.metrics_records} >= {"evt-c1", "evt-s1"}
    assert any(record.feed_source == "workspace_state" for record in parsed.metrics_records)
    assert parsed.run_summary is not None
    assert "workspace_layout" not in parsed.run_summary.error_by_source
    assert parsed.run_summary.script_version == "abc123"
    assert parsed.run_summary.is_budget_exhausted is False
    assert parsed.run_summary.record_count_by_source["transcripts"] == 1
    assert state.read_bytes > 0
    # Cursors round-trip as JSON strings the runner persists verbatim.
    transcripts_cursor = json.loads(parsed.run_summary.cursor_by_source["transcripts"])
    (transcript_offset,) = transcripts_cursor.values()
    assert transcript_offset > 0


def test_run_collection_fails_the_transcript_feed_closed_when_scanning_breaks(tmp_path: Path) -> None:
    workspace_root, host_dir = _build_fixture_workspace(tmp_path)
    emitted_lines: list[str] = []

    def broken_scanner(texts: Sequence[str]) -> list[set[int]]:
        raise RedactionError("kingfisher exited 3")

    run_collection(
        workspace_root=workspace_root,
        host_dir=host_dir,
        run_id="run-1",
        script_version="abc123",
        cursor_by_source={},
        budget_bytes=10_000_000,
        scan_texts=broken_scanner,
        scrub_pii=_keep_text,
        emit_line=lambda payload: emitted_lines.append(json.dumps(payload, sort_keys=True)),
    )

    parsed = parse_collection_output("\n".join(emitted_lines))
    # Nothing transcript-shaped leaves the workspace, and the cursor is not
    # advanced (absent from the summary), so the next run retries the feed.
    assert parsed.transcript_records == ()
    assert parsed.run_summary is not None
    assert "transcripts" not in parsed.run_summary.cursor_by_source
    # The other feeds still ran.
    assert any(record.event_id == "evt-c1" for record in parsed.metrics_records)
    summary_line = json.loads(emitted_lines[-1])
    assert "kingfisher exited 3" in summary_line["error_by_source"]["transcripts"]


def test_run_collection_reports_a_wholesale_missing_workspace_layout(tmp_path: Path) -> None:
    """Old workspace generations keep data at other paths: the run must say so.

    The hollow run's summary carries a workspace_layout error entry, which the
    runner folds into the server-side audit row's detail -- so "connected fine
    but read an empty world" stops looking identical to a healthy collection.
    """
    workspace_root = tmp_path / "workspace"
    host_dir = tmp_path / ".mngr"
    emitted_lines: list[str] = []

    run_collection(
        workspace_root=workspace_root,
        host_dir=host_dir,
        run_id="run-1",
        script_version="abc123",
        cursor_by_source={},
        budget_bytes=10_000_000,
        scan_texts=_no_findings,
        scrub_pii=_keep_text,
        emit_line=lambda payload: emitted_lines.append(json.dumps(payload, sort_keys=True)),
    )

    parsed = parse_collection_output("\n".join(emitted_lines))
    assert parsed.run_summary is not None
    layout_error = parsed.run_summary.error_by_source["workspace_layout"]
    assert "expected workspace layout entirely missing" in layout_error
    # The snapshot still emits, carrying the queryable layout markers.
    (state_record,) = [record for record in parsed.metrics_records if record.feed_source == "workspace_state"]
    state_payload = json.loads(state_record.payload)
    assert state_payload["is_workspace_repo_present"] is False
    assert state_payload["is_host_agents_dir_present"] is False


def test_run_collection_reports_budget_exhaustion(tmp_path: Path) -> None:
    workspace_root, host_dir = _build_fixture_workspace(tmp_path)
    emitted_lines: list[str] = []

    run_collection(
        workspace_root=workspace_root,
        host_dir=host_dir,
        run_id="run-1",
        script_version="abc123",
        cursor_by_source={},
        budget_bytes=0,
        scan_texts=_no_findings,
        scrub_pii=_keep_text,
        emit_line=lambda payload: emitted_lines.append(json.dumps(payload, sort_keys=True)),
    )

    parsed = parse_collection_output("\n".join(emitted_lines))
    assert parsed.run_summary is not None
    assert parsed.run_summary.is_budget_exhausted is True
    # Feeds emitted nothing, and their cursors stayed at the start so nothing
    # is skipped once budget is available again.
    assert parsed.transcript_records == ()
