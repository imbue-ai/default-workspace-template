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

# The ATIF-shaped records renamed the envelope's emitting source to "emitter".
_ATIF_ENVELOPE = {
    "timestamp": "2026-08-18T12:00:00.000000000Z",
    "event_id": "evt-1",
    "emitter": "claude/common_transcript",
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


def test_strip_keeps_the_atif_header_verbatim() -> None:
    record = {
        "type": "header",
        "event_id": "header",
        "emitter": "claude/common_transcript",
        "schema_version": "ATIF-v1.7",
        "agent_id": "agent-abc",
    }

    stripped = strip_transcript_record(record)

    assert stripped == snapshot(
        {
            "event_id": "header",
            "emitter": "claude/common_transcript",
            "type": "header",
            "schema_version": "ATIF-v1.7",
            "agent_id": "agent-abc",
        }
    )


def test_strip_drops_a_header_missing_its_schema_version() -> None:
    # Headers carry no timestamp, so schema_version is the only field left that
    # distinguishes a real header from an arbitrary object claiming the type.
    assert strip_transcript_record({"type": "header", "event_id": "header", "emitter": "claude"}) is None


def test_strip_drops_arguments_reasoning_and_unlisted_extras_from_atif_steps() -> None:
    record = {
        **_ATIF_ENVELOPE,
        "type": "step",
        "source": "agent",
        "message": "on it",
        "reasoning_content": "chain of thought about /etc/shadow",
        "model_name": "claude-fable-5",
        "reasoning_effort": "high",
        "llm_call_count": 1,
        "is_copied_context": False,
        "metrics": {"prompt_tokens": 10, "completion_tokens": 5, "cached_tokens": 4},
        "tool_calls": [
            {"tool_call_id": "call-1", "function_name": "Bash", "arguments": {"command": "cat /secret"}},
        ],
        "extra": {"finish_reason": "tool_use", "message_id": "msg-1", "raw_prompt": "must not survive"},
    }

    stripped = strip_transcript_record(record)

    assert stripped == snapshot(
        {
            "timestamp": "2026-08-18T12:00:00.000000000Z",
            "event_id": "evt-1",
            "emitter": "claude/common_transcript",
            "type": "step",
            "source": "agent",
            "message": "on it",
            "tool_calls": [{"tool_call_id": "call-1", "function_name": "Bash"}],
            "model_name": "claude-fable-5",
            "reasoning_effort": "high",
            "llm_call_count": 1,
            "is_copied_context": False,
            "metrics": {"prompt_tokens": 10, "completion_tokens": 5, "cached_tokens": 4},
            "extra": {"finish_reason": "tool_use", "message_id": "msg-1"},
        }
    )


def test_strip_reduces_context_management_on_a_system_step_and_counts_its_inline_result() -> None:
    record = {
        **_ATIF_ENVELOPE,
        "type": "step",
        "source": "system",
        "message": "",
        "observation": {
            "results": [{"content": "the whole compaction summary", "extra": {"tool_name": "compact"}}],
        },
        # Only the two known scalars survive: an emitter that annotates the descriptor with
        # anything else must not smuggle it through as an object copied whole.
        "extra": {
            "context_management": {
                "type": "compaction",
                "boundary": "replace",
                "summary": "the whole compaction summary again",
            }
        },
    }

    stripped = strip_transcript_record(record)

    assert stripped == snapshot(
        {
            "timestamp": "2026-08-18T12:00:00.000000000Z",
            "event_id": "evt-1",
            "emitter": "claude/common_transcript",
            "type": "step",
            "source": "system",
            "message": "",
            "observation": {
                "results": [
                    {
                        "source_call_id": "",
                        "content_byte_count": 28,
                        "extra": {"is_error": False, "tool_name": "compact"},
                    }
                ]
            },
            "extra": {"context_management": {"type": "compaction", "boundary": "replace"}},
        }
    )


def test_strip_reduces_atif_observation_results_to_metadata_and_byte_counts() -> None:
    record = {
        **_ATIF_ENVELOPE,
        "type": "observation",
        "results": [
            {
                "source_call_id": "call-1",
                "content": "the entire secret file contents",
                "extra": {"is_error": False, "tool_name": "Read"},
            },
            {"source_call_id": "call-2", "content": "boom", "extra": {"is_error": True, "tool_name": "Bash"}},
        ],
    }

    stripped = strip_transcript_record(record)

    assert stripped == snapshot(
        {
            "timestamp": "2026-08-18T12:00:00.000000000Z",
            "event_id": "evt-1",
            "emitter": "claude/common_transcript",
            "type": "observation",
            "results": [
                {
                    "source_call_id": "call-1",
                    "content_byte_count": 31,
                    "extra": {"is_error": False, "tool_name": "Read"},
                },
                {
                    "source_call_id": "call-2",
                    "content_byte_count": 4,
                    "extra": {"is_error": True, "tool_name": "Bash"},
                },
            ],
        }
    )


def test_strip_reduces_atif_metrics_to_the_allowlisted_numeric_counters() -> None:
    record = {
        **_ATIF_ENVELOPE,
        "type": "step",
        "source": "agent",
        "message": "on it",
        "metrics": {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "cached_tokens": 4,
            "cost_usd": 0.02,
            # ATIF's own metric fields for the tokenized exchange: a detokenizable copy of the
            # very text the strip is dropping. No emitter of ours sets them.
            "prompt_token_ids": [1, 2, 3],
            "completion_token_ids": [4, 5],
            "logprobs": [{"token": "on", "logprob": -0.1}],
            "extra": {"cache_creation_input_tokens": 7, "raw_usage": {"prompt": "the whole prompt"}},
        },
    }

    stripped = strip_transcript_record(record)

    assert stripped is not None
    assert stripped["metrics"] == snapshot(
        {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "cached_tokens": 4,
            "cost_usd": 0.02,
            "extra": {"cache_creation_input_tokens": 7},
        }
    )


def test_strip_drops_metric_counters_that_are_not_numbers() -> None:
    record = {
        **_ATIF_ENVELOPE,
        "type": "step",
        "source": "agent",
        "message": "",
        "metrics": {"prompt_tokens": "10", "completion_tokens": True, "cached_tokens": 4},
    }

    stripped = strip_transcript_record(record)

    assert stripped is not None
    assert stripped["metrics"] == snapshot({"cached_tokens": 4})


def test_strip_reduces_legacy_usage_to_the_allowlisted_numeric_counters() -> None:
    record = {
        **_ENVELOPE,
        "type": "assistant_message",
        "text": "on it",
        "usage": {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_read_tokens": 2,
            "cache_write_tokens": 1,
            "raw_usage": {"prompt": "the whole prompt"},
        },
    }

    stripped = strip_transcript_record(record)

    assert stripped is not None
    assert stripped["usage"] == snapshot(
        {"input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 2, "cache_write_tokens": 1}
    )


def test_strip_keeps_the_sidechain_marker_on_steps_and_observation_results() -> None:
    step = {
        **_ATIF_ENVELOPE,
        "type": "step",
        "source": "agent",
        "message": "on it",
        "extra": {"is_sidechain": True},
    }
    observation = {
        **_ATIF_ENVELOPE,
        "type": "observation",
        "results": [
            {
                "source_call_id": "call-1",
                "content": "out",
                "extra": {"is_error": False, "tool_name": "Bash", "is_sidechain": True},
            }
        ],
    }

    stripped_step = strip_transcript_record(step)
    stripped_observation = strip_transcript_record(observation)

    assert stripped_step is not None and stripped_observation is not None
    assert stripped_step["extra"] == snapshot({"is_sidechain": True})
    assert stripped_observation["results"][0]["extra"] == snapshot(
        {"is_error": False, "tool_name": "Bash", "is_sidechain": True}
    )


def test_strip_omits_tool_calls_from_a_step_that_carries_none() -> None:
    # The source schema has no ``tool_calls`` on user and system steps at all, so the redacted
    # record must not invent an empty one.
    record = {**_ATIF_ENVELOPE, "type": "step", "source": "user", "message": "please fix the bug"}

    stripped = strip_transcript_record(record)

    assert stripped == snapshot(
        {
            "timestamp": "2026-08-18T12:00:00.000000000Z",
            "event_id": "evt-1",
            "emitter": "claude/common_transcript",
            "type": "step",
            "source": "user",
            "message": "please fix the bug",
        }
    )


def test_strip_serializes_multimodal_message_and_result_content() -> None:
    # ATIF v1.6 allows a list of content parts; no emitter of ours writes one, but the fail-closed
    # rule still applies -- the parts are serialized, not repr'd, so they are scrubbed and counted
    # as the content they are.
    step = {
        **_ATIF_ENVELOPE,
        "type": "step",
        "source": "user",
        "message": [
            {"type": "text", "text": "look at this"},
            {"type": "image", "url": "data:image/png;base64,AAAA"},
        ],
    }
    observation = {
        **_ATIF_ENVELOPE,
        "type": "observation",
        "results": [{"source_call_id": "call-1", "content": [{"type": "text", "text": "out"}]}],
    }

    stripped_step = strip_transcript_record(step)
    stripped_observation = strip_transcript_record(observation)

    assert stripped_step is not None and stripped_observation is not None
    assert stripped_step["message"] == snapshot(
        '[{"text": "look at this", "type": "text"}, {"type": "image", "url": "data:image/png;base64,AAAA"}]'
    )
    assert stripped_observation["results"][0]["content_byte_count"] == snapshot(33)


def test_strip_drops_atif_records_with_broken_envelopes() -> None:
    assert strip_transcript_record({**_ATIF_ENVELOPE, "type": "step", "message": "no source"}) is None
    assert strip_transcript_record({"type": "observation", "event_id": "e", "results": []}) is None


def test_redact_transcript_records_scrubs_atif_step_messages() -> None:
    records = [
        {
            **_ATIF_ENVELOPE,
            "type": "step",
            "source": "user",
            "message": "token line\nsafe line with a@b.com",
        }
    ]

    def scan_texts(texts: Sequence[str]) -> list[set[int]]:
        return [{1} for _ in texts]

    def scrub_pii(text: str) -> str:
        return text.replace("a@b.com", "[REDACTED_EMAIL_ADDRESS]")

    batch = redact_transcript_records(records, scan_texts, scrub_pii)

    assert batch.records[0]["message"] == snapshot("[REDACTED_SECRET]\nsafe line with [REDACTED_EMAIL_ADDRESS]")


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
