"""Transcript redaction for the injected collection script (runs INSIDE the workspace).

Implements specs/minds-analytics/redaction-contract.md exactly:

1. Structural strip: tool inputs and outputs are dropped entirely; roles,
   message text, tool names/ids, counts, timings, and usage metadata survive.
   Emitter-specific extra fields are dropped unless allowlisted.
2. Text scrubbing (message text only): the workspace's pinned secret scanners
   (betterleaks + kingfisher) run over the surviving text and any finding's
   line is replaced with ``[REDACTED_SECRET]``; then a PII scrubber (Presidio,
   wired in by ``collect.py``) replaces detected entities.

Fail-closed everywhere: a record that does not match a known shape is dropped,
a scanner that is missing or errors raises (failing the transcript feed for
this run rather than shipping unscanned text), and a scanner finding without a
usable line redacts the whole text.

Injected next to ``collect.py``; importable with only the stdlib plus
pydantic. Never imports anything else from the monorepo.
"""

import json
import logging
import subprocess
import tempfile
from collections.abc import Callable
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from typing import Final

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

logger = logging.getLogger("analytics_collect.redaction")

REDACTED_SECRET_MARKER: Final[str] = "[REDACTED_SECRET]"

# Whole-text sentinel in a finding line set: a finding without a usable line
# number redacts the entire text.
WHOLE_TEXT_LINE: Final[int] = 0

# Emitter-specific extra fields that survive the structural strip (the
# contract allowlists session/conversation ids; ``agent_id`` is
# collection-added metadata naming the agent state dir the record came from).
_ALLOWLISTED_EXTRA_FIELDS: Final[frozenset[str]] = frozenset(
    {"session_id", "conversation_id", "message_id", "agent_id"}
)

_ENVELOPE_FIELDS: Final[tuple[str, ...]] = ("timestamp", "event_id", "source", "type")

_SCANNER_TIMEOUT_SECONDS: Final[float] = 300.0

# Exit codes the scanners document for "findings" (anything else non-zero is
# a scanner error and fails the feed).
_BETTERLEAKS_FINDINGS_EXIT_CODE: Final[int] = 99
_KINGFISHER_FINDINGS_EXIT_CODES: Final[frozenset[int]] = frozenset({200, 205})


class RedactionError(Exception):
    """Raised when redaction cannot be guaranteed (missing/broken scanner, bad report)."""


class RedactedTranscriptBatch(BaseModel):
    """The outcome of redacting one batch of raw transcript records."""

    model_config = ConfigDict(frozen=True)

    records: tuple[dict[str, Any], ...] = Field(description="Fully redacted records, in input order")
    dropped_record_count: int = Field(description="Records dropped for unknown or invalid shape")


def _allowlisted_extras(record: dict[str, Any]) -> dict[str, Any]:
    return {key: record[key] for key in _ALLOWLISTED_EXTRA_FIELDS if key in record}


def _envelope_or_none(record: dict[str, Any]) -> dict[str, Any] | None:
    envelope: dict[str, Any] = {}
    for field in _ENVELOPE_FIELDS:
        value = record.get(field)
        if not isinstance(value, str) or not value:
            return None
        envelope[field] = value
    return envelope


def _strip_assistant_parts(raw_parts: Any) -> list[dict[str, Any]]:
    """Apply the per-part dispositions; unknown part types are dropped (fail closed)."""
    stripped_parts: list[dict[str, Any]] = []
    if not isinstance(raw_parts, list):
        return stripped_parts
    for part in raw_parts:
        if not isinstance(part, dict):
            continue
        part_type = part.get("type")
        if part_type == "text":
            stripped_parts.append({"type": "text", "content": str(part.get("content", ""))})
        elif part_type == "tool_call":
            stripped_parts.append(
                {
                    "type": "tool_call",
                    "tool_call_id": str(part.get("tool_call_id", "")),
                    "tool_name": str(part.get("tool_name", "")),
                }
            )
        elif part_type == "tool_call_response":
            stripped_parts.append(
                {
                    "type": "tool_call_response",
                    "tool_call_id": str(part.get("tool_call_id", "")),
                    "is_error": bool(part.get("is_error", False)),
                }
            )
        else:
            # reasoning parts and anything unknown are dropped entirely.
            pass
    return stripped_parts


def strip_transcript_record(record: dict[str, Any]) -> dict[str, Any] | None:
    """The structural strip: returns the surviving fields, or None to drop the record."""
    envelope = _envelope_or_none(record)
    if envelope is None:
        return None
    record_type = envelope["type"]
    if record_type == "user_message":
        return {
            **envelope,
            **_allowlisted_extras(record),
            "role": "user",
            "content": str(record.get("content", "")),
        }
    if record_type == "assistant_message":
        tool_calls = [
            {"tool_call_id": str(call.get("tool_call_id", "")), "tool_name": str(call.get("tool_name", ""))}
            for call in record.get("tool_calls", [])
            if isinstance(call, dict)
        ]
        stripped: dict[str, Any] = {
            **envelope,
            **_allowlisted_extras(record),
            "role": "assistant",
            "text": str(record.get("text", "")),
            "tool_calls": tool_calls,
            "parts": _strip_assistant_parts(record.get("parts")),
            "parts_ordered": bool(record.get("parts_ordered", True)),
        }
        for optional_field in ("model", "finish_reason"):
            if isinstance(record.get(optional_field), str):
                stripped[optional_field] = record[optional_field]
        if isinstance(record.get("usage"), dict):
            stripped["usage"] = record["usage"]
        return stripped
    if record_type == "tool_result":
        output = record.get("output", "")
        return {
            **envelope,
            **_allowlisted_extras(record),
            "tool_call_id": str(record.get("tool_call_id", "")),
            "tool_name": str(record.get("tool_name", "")),
            "is_error": bool(record.get("is_error", False)),
            "output_byte_count": len(str(output).encode("utf-8", errors="replace")),
        }
    return None


def _scrubbable_text_slots(record: dict[str, Any]) -> list[tuple[str, int | None]]:
    """The (field, part index) slots of a stripped record that hold message text."""
    slots: list[tuple[str, int | None]] = []
    if "content" in record:
        slots.append(("content", None))
    if "text" in record:
        slots.append(("text", None))
    for part_idx, part in enumerate(record.get("parts", [])):
        if part.get("type") == "text":
            slots.append(("parts", part_idx))
    return slots


def redact_secret_lines(text: str, finding_lines: set[int]) -> str:
    """Replace each finding's line (1-based) with the marker; line 0 redacts the whole text."""
    if not finding_lines:
        return text
    if WHOLE_TEXT_LINE in finding_lines:
        return REDACTED_SECRET_MARKER
    lines = text.split("\n")
    replaced = [
        REDACTED_SECRET_MARKER if (line_idx + 1) in finding_lines else line for line_idx, line in enumerate(lines)
    ]
    return "\n".join(replaced)


def replace_pii_spans(text: str, spans: Sequence[tuple[int, int, str]]) -> str:
    """Replace each (start, end, entity_type) span with ``[REDACTED_<ENTITY_TYPE>]``.

    Overlapping spans are applied right-to-left so earlier replacements never
    shift later offsets.
    """
    result = text
    for start, end, entity_type in sorted(spans, key=lambda span: span[0], reverse=True):
        if 0 <= start < end <= len(result):
            result = result[:start] + f"[REDACTED_{entity_type.upper()}]" + result[end:]
    return result


def _scanner_file_index(path_text: str) -> int | None:
    stem = Path(path_text).stem
    return int(stem) if stem.isdigit() else None


def _run_betterleaks(scan_dir: Path, finding_lines_by_text_idx: list[set[int]]) -> None:
    report_path = scan_dir / "betterleaks_report.json"
    command = [
        "betterleaks",
        "dir",
        str(scan_dir / "texts"),
        "--redact",
        "--no-banner",
        "--exit-code",
        str(_BETTERLEAKS_FINDINGS_EXIT_CODE),
        "--report-format",
        "json",
        "--report-path",
        str(report_path),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=_SCANNER_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RedactionError(f"betterleaks failed to run: {e}") from e
    if result.returncode == 0:
        return
    if result.returncode != _BETTERLEAKS_FINDINGS_EXIT_CODE:
        raise RedactionError(f"betterleaks exited {result.returncode}: {result.stderr.strip()[:500]}")
    try:
        findings = json.loads(report_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise RedactionError("betterleaks reported findings but its report is unreadable") from e
    for finding in findings or []:
        text_idx = _scanner_file_index(str(finding.get("File", "")))
        if text_idx is None or text_idx >= len(finding_lines_by_text_idx):
            continue
        start_line = finding.get("StartLine")
        end_line = finding.get("EndLine", start_line)
        if isinstance(start_line, int) and start_line > 0 and isinstance(end_line, int) and end_line >= start_line:
            finding_lines_by_text_idx[text_idx].update(range(start_line, end_line + 1))
        else:
            finding_lines_by_text_idx[text_idx].add(WHOLE_TEXT_LINE)


def _run_kingfisher(scan_dir: Path, finding_lines_by_text_idx: list[set[int]]) -> None:
    command = [
        "kingfisher",
        "scan",
        str(scan_dir / "texts"),
        "--no-validate",
        "--no-update-check",
        "--redact",
        "--format",
        "jsonl",
        "--quiet",
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=_SCANNER_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RedactionError(f"kingfisher failed to run: {e}") from e
    if result.returncode != 0 and result.returncode not in _KINGFISHER_FINDINGS_EXIT_CODES:
        raise RedactionError(f"kingfisher exited {result.returncode}: {result.stderr.strip()[:500]}")
    for raw_line in result.stdout.splitlines():
        if not raw_line.strip():
            continue
        try:
            entry = json.loads(raw_line)
        except json.JSONDecodeError:
            logger.warning("Skipped one unparsable kingfisher report line")
            continue
        if not isinstance(entry, dict) or "finding" not in entry:
            # The trailing scan-summary line carries no finding.
            continue
        finding = entry.get("finding") or {}
        text_idx = _scanner_file_index(str(finding.get("path", "")))
        if text_idx is None or text_idx >= len(finding_lines_by_text_idx):
            continue
        line = finding.get("line")
        if isinstance(line, int) and line > 0:
            finding_lines_by_text_idx[text_idx].add(line)
        else:
            finding_lines_by_text_idx[text_idx].add(WHOLE_TEXT_LINE)


def scan_texts_for_secret_lines(texts: Sequence[str]) -> list[set[int]]:
    """Run both pinned secret scanners over the texts; returns finding line sets per text.

    Raises RedactionError when either scanner is missing or errors -- a broken
    scanner must fail the feed, never silently pass text through.
    """
    if not texts:
        return []
    finding_lines_by_text_idx: list[set[int]] = [set() for _ in texts]
    with tempfile.TemporaryDirectory(prefix="analytics-redaction-") as temp_dir:
        scan_dir = Path(temp_dir)
        texts_dir = scan_dir / "texts"
        texts_dir.mkdir()
        for text_idx, text in enumerate(texts):
            (texts_dir / f"{text_idx:06d}.txt").write_text(text, encoding="utf-8", errors="replace")
        _run_betterleaks(scan_dir, finding_lines_by_text_idx)
        _run_kingfisher(scan_dir, finding_lines_by_text_idx)
    return finding_lines_by_text_idx


def redact_transcript_records(
    raw_records: Sequence[dict[str, Any]],
    # Batch secret-line scanner: texts -> per-text finding line sets (1-based;
    # 0 redacts the whole text). Production: scan_texts_for_secret_lines.
    scan_texts: Callable[[Sequence[str]], list[set[int]]],
    # Per-text PII scrubber. Production: the Presidio scrubber collect.py builds.
    scrub_pii: Callable[[str], str],
) -> RedactedTranscriptBatch:
    """The full redaction pipeline: structural strip, then secret lines, then PII."""
    stripped_records: list[dict[str, Any]] = []
    dropped_record_count = 0
    for raw_record in raw_records:
        stripped = strip_transcript_record(raw_record)
        if stripped is None:
            dropped_record_count += 1
        else:
            stripped_records.append(stripped)

    # Gather every surviving text slot so both scanners run once per batch.
    slot_owners: list[tuple[dict[str, Any], str, int | None]] = []
    texts: list[str] = []
    for record in stripped_records:
        for field, part_idx in _scrubbable_text_slots(record):
            slot_owners.append((record, field, part_idx))
            texts.append(record[field][part_idx]["content"] if part_idx is not None else record[field])

    finding_lines_by_text_idx = scan_texts(texts) if texts else []
    for slot_idx, (record, field, part_idx) in enumerate(slot_owners):
        secret_redacted = redact_secret_lines(texts[slot_idx], finding_lines_by_text_idx[slot_idx])
        scrubbed = scrub_pii(secret_redacted)
        if part_idx is not None:
            record[field][part_idx]["content"] = scrubbed
        else:
            record[field] = scrubbed
    return RedactedTranscriptBatch(records=tuple(stripped_records), dropped_record_count=dropped_record_count)
