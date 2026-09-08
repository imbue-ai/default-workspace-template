import json

from inline_snapshot import snapshot

from imbue.analytics.protocol import MAX_LINE_BYTES
from imbue.analytics.protocol import parse_collection_output
from imbue.analytics.protocol import parse_event_timestamp


def _record_line(source: str, event_id: str, record_type: str = "message") -> str:
    return json.dumps(
        {
            "source": source,
            "record": {
                "timestamp": "2026-08-18T12:00:00.123456789Z",
                "event_id": event_id,
                "type": record_type,
                "source": source,
                "extra_field": "kept in payload",
            },
        }
    )


def _summary_line() -> str:
    return json.dumps(
        {
            "source": "run_summary",
            "record_count_by_source": {"client_activity": 1, "transcripts": 1},
            "cursor_by_source": {"client_activity": '{"offset": 10}'},
            "read_bytes": 2048,
            "is_budget_exhausted": False,
            "script_version": "abc123",
        }
    )


def test_parse_routes_transcripts_and_metrics_records_and_reads_the_summary() -> None:
    stdout_text = "\n".join(
        [
            _record_line("client_activity", "evt-1"),
            _record_line("transcripts", "evt-2", record_type="user_message"),
            _summary_line(),
        ]
    )

    parsed = parse_collection_output(stdout_text)

    assert [record.event_id for record in parsed.metrics_records] == ["evt-1"]
    assert [record.event_id for record in parsed.transcript_records] == ["evt-2"]
    assert parsed.dropped_line_count == 0
    assert parsed.run_summary is not None
    assert parsed.run_summary.script_version == "abc123"
    assert parsed.run_summary.cursor_by_source == {"client_activity": '{"offset": 10}'}
    # The payload is the full original record, so downstream schema drift in
    # workspaces never loses fields.
    assert json.loads(parsed.metrics_records[0].payload)["extra_field"] == "kept in payload"


def test_parse_drops_and_counts_malformed_oversize_and_unknown_lines() -> None:
    valid = _record_line("services", "evt-ok")
    oversize = json.dumps({"source": "services", "record": {"blob": "x" * (MAX_LINE_BYTES + 1)}})
    stdout_text = "\n".join(
        [
            "not json at all",
            '["a", "list", "line"]',
            json.dumps({"source": "unknown_feed", "record": {}}),
            json.dumps(
                {"source": "services", "record": {"timestamp": "nope", "event_id": "e", "type": "t", "source": "s"}}
            ),
            json.dumps({"source": "services", "record": "not-a-dict"}),
            oversize,
            valid,
            "",
        ]
    )

    parsed = parse_collection_output(stdout_text)

    assert [record.event_id for record in parsed.metrics_records] == ["evt-ok"]
    assert parsed.dropped_line_count == snapshot(6)
    assert parsed.run_summary is None


def test_parse_rejects_records_with_missing_or_oversized_envelope_fields() -> None:
    missing_event_id = json.dumps(
        {"source": "services", "record": {"timestamp": "2026-08-18T12:00:00Z", "type": "t", "source": "s"}}
    )
    oversized_type = json.dumps(
        {
            "source": "services",
            "record": {
                "timestamp": "2026-08-18T12:00:00Z",
                "event_id": "evt",
                "type": "t" * 300,
                "source": "s",
            },
        }
    )

    parsed = parse_collection_output("\n".join([missing_event_id, oversized_type]))

    assert parsed.metrics_records == ()
    assert parsed.dropped_line_count == 2


def test_parse_takes_the_emitter_as_the_source_of_an_atif_transcript_record() -> None:
    """ATIF records renamed the envelope's emitting source to ``emitter``; ``source`` is now the
    step originator, so only ``emitter`` belongs in the record_source column."""
    observation = json.dumps(
        {
            "source": "transcripts",
            "record": {
                "type": "observation",
                "event_id": "evt-obs",
                "emitter": "claude/common_transcript",
                "timestamp": "2026-08-18T12:00:00.000000000Z",
                "results": [{"source_call_id": "call-1", "content_byte_count": 3}],
            },
        }
    )
    step = json.dumps(
        {
            "source": "transcripts",
            "record": {
                "type": "step",
                "event_id": "evt-step",
                "emitter": "claude/common_transcript",
                "timestamp": "2026-08-18T12:00:01.000000000Z",
                "source": "agent",
                "message": "hi",
            },
        }
    )

    parsed = parse_collection_output("\n".join([observation, step]))

    assert [record.event_id for record in parsed.transcript_records] == ["evt-obs", "evt-step"]
    assert {record.record_source for record in parsed.transcript_records} == {"claude/common_transcript"}
    assert parsed.dropped_line_count == 0


def test_parse_skips_the_atif_stream_header_without_counting_it_as_dropped() -> None:
    """The header describes the stream, not an event in it: no timestamp, and the same event id on
    every agent's stream, so it must not become a row -- but it is framing rather than corruption,
    so it must not inflate the dropped-line count either."""
    header = json.dumps(
        {
            "source": "transcripts",
            "record": {
                "type": "header",
                "event_id": "header",
                "emitter": "claude/common_transcript",
                "schema_version": "ATIF-v1.7",
            },
        }
    )

    parsed = parse_collection_output(header)

    assert parsed.transcript_records == ()
    assert parsed.dropped_line_count == 0


def test_summary_drops_cursor_values_that_are_not_json_object_strings() -> None:
    """The runner persists cursor values verbatim, so only known-source JSON-object strings may survive."""
    summary = json.dumps(
        {
            "source": "run_summary",
            "cursor_by_source": {
                "client_activity": '{"offset": 10}',
                "transcripts": "not json at all",
                "services": '["a", "json", "array"]',
                "servers": {"a": "dict, not a string"},
                "totally_made_up_feed_71203": '{"offset": 10}',
            },
            "read_bytes": 0,
            "is_budget_exhausted": False,
            "script_version": "abc123",
        }
    )

    parsed = parse_collection_output(summary)

    assert parsed.run_summary is not None
    assert parsed.run_summary.cursor_by_source == {"client_activity": '{"offset": 10}'}


def test_the_last_run_summary_wins_over_a_forged_early_one() -> None:
    forged = json.dumps({"source": "run_summary", "script_version": "forged", "read_bytes": 0})
    stdout_text = "\n".join([forged, _summary_line()])

    parsed = parse_collection_output(stdout_text)

    assert parsed.run_summary is not None
    assert parsed.run_summary.script_version == "abc123"


def test_parse_event_timestamp_accepts_nanoseconds_and_rejects_naive_or_garbage() -> None:
    nanosecond = parse_event_timestamp("2026-02-28T12:00:00.123456789Z")
    assert nanosecond is not None
    assert nanosecond.microsecond == 123456

    plain_offset = parse_event_timestamp("2026-02-28T12:00:00+00:00")
    assert plain_offset is not None

    assert parse_event_timestamp("2026-02-28T12:00:00") is None
    assert parse_event_timestamp("yesterday") is None
