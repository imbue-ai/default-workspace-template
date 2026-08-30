import os
import stat
from collections.abc import Sequence
from pathlib import Path

import pytest
from inline_snapshot import snapshot

from imbue.analytics.injected.workspace_redaction import RedactionError
from imbue.analytics.injected.workspace_redaction import redact_secret_lines
from imbue.analytics.injected.workspace_redaction import redact_transcript_records
from imbue.analytics.injected.workspace_redaction import replace_pii_spans
from imbue.analytics.injected.workspace_redaction import scan_texts_for_secret_lines
from imbue.analytics.injected.workspace_redaction import scrub_random_tokens
from imbue.analytics.injected.workspace_redaction import strip_transcript_record

_ENVELOPE = {
    "timestamp": "2026-08-18T12:00:00.000000000Z",
    "event_id": "evt-1",
    "source": "claude",
}


def _no_findings(texts: Sequence[str]) -> list[set[int]]:
    return [set() for _ in texts]


def _keep_text(text: str) -> str:
    return text


def test_strip_keeps_user_message_content_and_allowlisted_extras_only() -> None:
    record = {
        **_ENVELOPE,
        "type": "user_message",
        "role": "user",
        "content": "please fix the bug",
        "session_id": "sess-1",
        "internal_scratch": "must not survive",
    }

    stripped = strip_transcript_record(record)

    assert stripped == snapshot(
        {
            "timestamp": "2026-08-18T12:00:00.000000000Z",
            "event_id": "evt-1",
            "source": "claude",
            "type": "user_message",
            "session_id": "sess-1",
            "role": "user",
            "content": "please fix the bug",
        }
    )


def test_strip_drops_tool_inputs_outputs_and_reasoning_from_assistant_messages() -> None:
    record = {
        **_ENVELOPE,
        "type": "assistant_message",
        "role": "assistant",
        "text": "on it",
        "model": "claude-fable-5",
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "finish_reason": "end_turn",
        "tool_calls": [{"tool_call_id": "call-1", "tool_name": "Bash", "input_preview": "rm -rf /secret"}],
        "parts": [
            {"type": "text", "content": "on it"},
            {"type": "tool_call", "tool_call_id": "call-1", "tool_name": "Bash", "input_preview": "rm -rf /secret"},
            {"type": "tool_call_response", "tool_call_id": "call-1", "is_error": True, "output": "root: password"},
            {"type": "reasoning", "content": "chain of thought"},
        ],
        "parts_ordered": True,
    }

    stripped = strip_transcript_record(record)

    assert stripped == snapshot(
        {
            "timestamp": "2026-08-18T12:00:00.000000000Z",
            "event_id": "evt-1",
            "source": "claude",
            "type": "assistant_message",
            "role": "assistant",
            "text": "on it",
            "tool_calls": [{"tool_call_id": "call-1", "tool_name": "Bash"}],
            "parts": [
                {"type": "text", "content": "on it"},
                {"type": "tool_call", "tool_call_id": "call-1", "tool_name": "Bash"},
                {"type": "tool_call_response", "tool_call_id": "call-1", "is_error": True},
            ],
            "parts_ordered": True,
            "model": "claude-fable-5",
            "finish_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
    )


def test_strip_reduces_tool_results_to_metadata_and_byte_counts() -> None:
    record = {
        **_ENVELOPE,
        "type": "tool_result",
        "tool_call_id": "call-1",
        "tool_name": "Read",
        "output": "the entire secret file contents",
        "is_error": False,
    }

    stripped = strip_transcript_record(record)

    assert stripped == snapshot(
        {
            "timestamp": "2026-08-18T12:00:00.000000000Z",
            "event_id": "evt-1",
            "source": "claude",
            "type": "tool_result",
            "tool_call_id": "call-1",
            "tool_name": "Read",
            "is_error": False,
            "output_byte_count": 31,
        }
    )


def test_strip_drops_unknown_record_types_and_broken_envelopes() -> None:
    assert strip_transcript_record({**_ENVELOPE, "type": "totally_new_type", "content": "x"}) is None
    assert strip_transcript_record({"type": "user_message", "content": "no envelope"}) is None


def test_redact_secret_lines_replaces_found_lines_or_the_whole_text() -> None:
    text = "line one\nAKIA-SOMETHING-SECRET\nline three"

    assert redact_secret_lines(text, {2}) == snapshot("line one\n[REDACTED_SECRET]\nline three")
    assert redact_secret_lines(text, {0}) == snapshot("[REDACTED_SECRET]")
    assert redact_secret_lines(text, set()) == text


def test_scrub_random_tokens_redacts_identifier_shapes_and_keeps_words() -> None:
    text = (
        "deploy a0eaa1f2-3b4c-5d6e-7f80-91a2b3c4d5e6 at commit"
        " eb40de1234567890abcdef1234567890abcdef12 for order 10486612345 with"
        " token sk4Xt92bQ7LmPzR0aWq8vN31 after the 12-hour-forecast-check;"
        " call 555-0134 about PostgreSQL15 and CamelCaseWord"
    )

    scrubbed = scrub_random_tokens(text)

    assert scrubbed == snapshot(
        "deploy [REDACTED_TOKEN] at commit [REDACTED_TOKEN] for order [REDACTED_TOKEN] with"
        " token [REDACTED_TOKEN] after the 12-hour-forecast-check;"
        " call 555-0134 about PostgreSQL15 and CamelCaseWord"
    )


def test_scrub_random_tokens_keeps_workspace_paths_and_scrubs_other_path_segments() -> None:
    text = (
        "see /home/user/workspace/data/.tasks/report-1755550000000/out.md"
        " and ~/notes/8f14e45fceea167a5a36dedd4bea2543"
        " but /tmp/8f14e45fceea167a5a36dedd4bea2543/log.txt"
    )

    scrubbed = scrub_random_tokens(text)

    assert scrubbed == snapshot(
        "see /home/user/workspace/data/.tasks/report-1755550000000/out.md"
        " and ~/notes/8f14e45fceea167a5a36dedd4bea2543"
        " but /tmp/[REDACTED_TOKEN]/log.txt"
    )


def test_scrub_random_tokens_leaves_existing_redaction_markers_alone() -> None:
    text = "wrote to [REDACTED_EMAIL_ADDRESS] and [REDACTED_SECRET] stayed"

    assert scrub_random_tokens(text) == text


def test_replace_pii_spans_handles_multiple_spans_without_offset_drift() -> None:
    text = "email me at a@b.com or call 555-0134 ok"

    scrubbed = replace_pii_spans(text, [(12, 19, "EMAIL_ADDRESS"), (28, 36, "PHONE_NUMBER")])

    assert scrubbed == snapshot("email me at [REDACTED_EMAIL_ADDRESS] or call [REDACTED_PHONE_NUMBER] ok")


def test_redact_transcript_records_runs_strip_then_secrets_then_pii() -> None:
    records = [
        {
            **_ENVELOPE,
            "type": "user_message",
            "content": "token line\nsafe line with a@b.com",
        },
        {"type": "not_a_transcript_record"},
    ]

    def scan_texts(texts: Sequence[str]) -> list[set[int]]:
        return [{1} for _ in texts]

    def scrub_pii(text: str) -> str:
        return text.replace("a@b.com", "[REDACTED_EMAIL_ADDRESS]")

    batch = redact_transcript_records(records, scan_texts, scrub_pii)

    assert batch.dropped_record_count == 1
    assert batch.records[0]["content"] == snapshot("[REDACTED_SECRET]\nsafe line with [REDACTED_EMAIL_ADDRESS]")


def test_redact_transcript_records_scrubs_random_tokens_after_pii() -> None:
    records = [
        {
            **_ENVELOPE,
            "type": "user_message",
            "role": "user",
            "content": "check run 3f9d2c81a4b04e12 in /home/user/workspace/data/.tasks/run-1755550000000",
        }
    ]

    batch = redact_transcript_records(records, _no_findings, _keep_text)

    assert batch.records[0]["content"] == snapshot(
        "check run [REDACTED_TOKEN] in /home/user/workspace/data/.tasks/run-1755550000000"
    )


def test_redact_transcript_records_scrubs_text_parts_too() -> None:
    records = [
        {
            **_ENVELOPE,
            "type": "assistant_message",
            "text": "outer a@b.com",
            "parts": [{"type": "text", "content": "inner a@b.com"}],
        }
    ]

    def scrub_pii(text: str) -> str:
        return text.replace("a@b.com", "[REDACTED_EMAIL_ADDRESS]")

    batch = redact_transcript_records(records, _no_findings, scrub_pii)

    assert batch.records[0]["text"] == "outer [REDACTED_EMAIL_ADDRESS]"
    assert batch.records[0]["parts"][0]["content"] == "inner [REDACTED_EMAIL_ADDRESS]"


_FAKE_BETTERLEAKS = """#!/bin/sh
# Fake betterleaks: reports a finding on line 2 of every scanned file.
report_path=""
scan_dir=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --report-path) report_path="$2"; shift 2 ;;
        dir) scan_dir="$2"; shift 2 ;;
        --exit-code|--report-format) shift 2 ;;
        *) shift ;;
    esac
done
printf '[' > "$report_path"
first=1
for f in "$scan_dir"/*.txt; do
    [ "$first" -eq 1 ] || printf ',' >> "$report_path"
    first=0
    printf '{"RuleID": "fake-rule", "File": "%s", "StartLine": 2, "EndLine": 2}' "$f" >> "$report_path"
done
printf ']' >> "$report_path"
exit 99
"""

_FAKE_KINGFISHER = """#!/bin/sh
# Fake kingfisher: reports a finding on line 1 of every scanned file.
scan_dir=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        scan) scan_dir="$2"; shift 2 ;;
        --format) shift 2 ;;
        *) shift ;;
    esac
done
for f in "$scan_dir"/*.txt; do
    printf '{"rule": {"name": "fake"}, "finding": {"path": "%s", "line": 1}}\\n' "$f"
done
printf '{"summary": "done"}\\n'
exit 200
"""


def _install_fake_scanners(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    for name, body in (("betterleaks", _FAKE_BETTERLEAKS), ("kingfisher", _FAKE_KINGFISHER)):
        script = bin_dir / name
        script.write_text(body)
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin")


def test_scan_texts_merges_findings_from_both_scanners(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_scanners(tmp_path, monkeypatch)

    finding_lines = scan_texts_for_secret_lines(["line1\nline2\nline3", "only"])

    assert finding_lines == [{1, 2}, {1, 2}]


def test_scan_texts_fails_closed_when_a_scanner_is_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", f"{empty_bin}{os.pathsep}/usr/bin{os.pathsep}/bin")

    with pytest.raises(RedactionError, match="betterleaks failed to run"):
        scan_texts_for_secret_lines(["some text"])
