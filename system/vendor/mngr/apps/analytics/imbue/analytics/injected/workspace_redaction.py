"""Transcript redaction for the injected collection script (runs INSIDE the workspace).

Implements specs/minds-analytics/redaction-contract.md exactly:

1. Structural strip: tool inputs and outputs are dropped entirely; roles,
   message text, tool names/ids, counts, timings, and usage metadata survive.
   Emitter-specific extra fields and token counters are dropped unless
   allowlisted -- nothing emitter-controlled passes through. Both stream
   vintages are handled: the ATIF-shaped ``header``/``step``/``observation``
   records and the legacy ``user_message``/``assistant_message``/``tool_result``
   ones that pre-cutover agents keep emitting.
2. Text scrubbing (message text only): the workspace's pinned secret scanners
   (betterleaks + kingfisher) run over the surviving text and any finding's
   line is replaced with ``[REDACTED_SECRET]``; then a PII scrubber (Presidio,
   wired in by ``collect.py``) replaces detected entities; then random-looking
   identifier tokens are replaced with ``[REDACTED_TOKEN]`` (workspace-local
   paths are kept).

Fail-closed everywhere: a record that does not match a known shape is dropped,
a scanner that is missing or errors raises (failing the transcript feed for
this run rather than shipping unscanned text), and a scanner finding without a
usable line redacts the whole text.

Injected next to ``collect.py``; importable with only the stdlib plus
pydantic. Never imports anything else from the monorepo.
"""

import json
import logging
import math
import re
import subprocess
import tempfile
from collections import Counter
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
REDACTED_TOKEN_MARKER: Final[str] = "[REDACTED_TOKEN]"

# Chunks starting with these prefixes are workspace-local paths readers rely
# on; they are kept whole, random-looking segments included.
_KEPT_CHUNK_PREFIXES: Final[tuple[str, ...]] = ("/home/user", "~/")

_UUID_SEGMENT_RE: Final[re.Pattern[str]] = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_HEX_SEGMENT_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-fA-F]{16,}$")
_DIGIT_RUN_RE: Final[re.Pattern[str]] = re.compile(r"\d{7,}")
_TOKEN_CHARSET_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9+_=-]{20,}$")
_ALNUM_SEGMENT_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9]{12,}$")
_SURROUNDING_PUNCTUATION: Final[str] = ".,;:!?)('\"`<>[]{}"

# Whole-text sentinel in a finding line set: a finding without a usable line
# number redacts the entire text.
WHOLE_TEXT_LINE: Final[int] = 0

# Emitter-specific extra fields that survive the structural strip (the
# contract allowlists session/conversation ids; ``agent_id`` is
# collection-added metadata naming the agent state dir the record came from).
_ALLOWLISTED_EXTRA_FIELDS: Final[frozenset[str]] = frozenset(
    {"session_id", "conversation_id", "message_id", "agent_id"}
)

# Keys of an ATIF step's ``extra`` object that survive as-is: structural
# annotations only, never free text. ``is_sidechain`` and ``context_management``
# survive too, but coerced rather than copied (see ``_strip_atif_step``).
_ALLOWLISTED_STEP_EXTRA_FIELDS: Final[frozenset[str]] = _ALLOWLISTED_EXTRA_FIELDS | frozenset({"finish_reason"})

# The counters that survive from an ATIF step's ``metrics``. Allowlisting rather
# than passing the object through is what structurally excludes ATIF's
# ``prompt_token_ids`` / ``completion_token_ids`` / ``logprobs`` fields: none of
# our emitters set them, and one that did would ship a detokenizable copy of the
# transcript through a stage that only claims to carry counts.
_ALLOWLISTED_METRIC_FIELDS: Final[frozenset[str]] = frozenset(
    {"prompt_tokens", "completion_tokens", "cached_tokens", "cost_usd"}
)
_ALLOWLISTED_METRIC_EXTRA_FIELDS: Final[frozenset[str]] = frozenset({"cache_creation_input_tokens"})

# The legacy ``usage`` block's counters, under the pre-cutover converter's names.
_ALLOWLISTED_LEGACY_USAGE_FIELDS: Final[frozenset[str]] = frozenset(
    {"input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"}
)

_LEGACY_ENVELOPE_FIELDS: Final[tuple[str, ...]] = ("timestamp", "event_id", "source", "type")

# The ATIF-shaped records name the emitting source ``emitter`` (ATIF claims
# ``source`` for the step originator). Header records carry no timestamp: they
# describe the stream, not an event in it.
_ATIF_HEADER_ENVELOPE_FIELDS: Final[tuple[str, ...]] = ("event_id", "emitter", "type", "schema_version")
_ATIF_EVENT_ENVELOPE_FIELDS: Final[tuple[str, ...]] = ("timestamp", "event_id", "emitter", "type")
_ATIF_RECORD_TYPES: Final[frozenset[str]] = frozenset({"header", "step", "observation"})

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


def _is_number(value: Any) -> bool:
    """Whether the value is a JSON number: bools are Python ints, but they are not counters."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _allowlisted_counters(raw_counters: Any, allowlisted_fields: frozenset[str]) -> dict[str, Any]:
    if not isinstance(raw_counters, dict):
        return {}
    return {key: raw_counters[key] for key in allowlisted_fields if _is_number(raw_counters.get(key))}


def _strip_metrics(raw_metrics: dict[str, Any]) -> dict[str, Any]:
    """An ATIF step's ``metrics`` reduced to the allowlisted numeric counters."""
    stripped = _allowlisted_counters(raw_metrics, _ALLOWLISTED_METRIC_FIELDS)
    extra = _allowlisted_counters(raw_metrics.get("extra"), _ALLOWLISTED_METRIC_EXTRA_FIELDS)
    if extra:
        stripped["extra"] = extra
    return stripped


def _strip_context_management(raw_context_management: Any) -> dict[str, Any] | None:
    """The compaction descriptor reduced to its two known scalars."""
    if not isinstance(raw_context_management, dict):
        return None
    return {
        "type": str(raw_context_management.get("type", "")),
        "boundary": str(raw_context_management.get("boundary", "")),
    }


def _envelope_or_none(record: dict[str, Any], envelope_fields: tuple[str, ...]) -> dict[str, Any] | None:
    envelope: dict[str, Any] = {}
    for field in envelope_fields:
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


def _content_text(value: Any) -> str:
    """One message/result content field as text.

    ATIF v1.6 allows a list of content parts where our emitters write a plain string. Serializing
    such a list is what keeps a multimodal record's actual content -- rather than a Python repr of
    it -- the thing that gets scrubbed and counted.
    """
    return json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, list) else str(value)


def _byte_count(value: Any) -> int:
    return len(_content_text(value).encode("utf-8", errors="replace"))


def _strip_observation_results(raw_results: Any) -> list[dict[str, Any]]:
    """Apply the ATIF observation-result dispositions: the output text becomes its byte count.

    ``is_error`` and ``tool_name`` stay nested under ``extra``, where the
    unredacted record carries them, so a reader of the redacted stream can use
    the same field paths.
    """
    stripped_results: list[dict[str, Any]] = []
    if not isinstance(raw_results, list):
        return stripped_results
    for result in raw_results:
        if not isinstance(result, dict):
            continue
        raw_extra = result.get("extra")
        extra = raw_extra if isinstance(raw_extra, dict) else {}
        stripped_extra: dict[str, Any] = {
            "is_error": bool(extra.get("is_error", False)),
            "tool_name": str(extra.get("tool_name", "")),
        }
        if "is_sidechain" in extra:
            # Emitters mark only the sidechain lane, so the key stays absent on the main one.
            stripped_extra["is_sidechain"] = bool(extra["is_sidechain"])
        stripped_results.append(
            {
                "source_call_id": str(result.get("source_call_id", "")),
                "content_byte_count": _byte_count(result.get("content", "")),
                "extra": stripped_extra,
            }
        )
    return stripped_results


def _strip_atif_step(record: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any] | None:
    """The ATIF step dispositions: message text survives (scrubbed), everything free-form does not.

    ``reasoning_content`` is dropped wholesale, mirroring the legacy rule for
    reasoning parts, and tool-call ``arguments`` are dropped the way the legacy
    ``input_preview`` was.
    """
    source = record.get("source")
    if not isinstance(source, str) or not source:
        return None
    stripped: dict[str, Any] = {
        **envelope,
        **_allowlisted_extras(record),
        "source": source,
        "message": _content_text(record.get("message", "")),
    }
    tool_calls = [
        {"tool_call_id": str(call.get("tool_call_id", "")), "function_name": str(call.get("function_name", ""))}
        for call in record.get("tool_calls") or []
        if isinstance(call, dict)
    ]
    if tool_calls:
        # The source schema has no ``tool_calls`` on user and system steps at all.
        stripped["tool_calls"] = tool_calls
    model_name = record.get("model_name")
    if isinstance(model_name, str):
        stripped["model_name"] = model_name
    reasoning_effort = record.get("reasoning_effort")
    if isinstance(reasoning_effort, str) or _is_number(reasoning_effort):
        stripped["reasoning_effort"] = reasoning_effort
    llm_call_count = record.get("llm_call_count")
    if isinstance(llm_call_count, int) and not isinstance(llm_call_count, bool):
        stripped["llm_call_count"] = llm_call_count
    is_copied_context = record.get("is_copied_context")
    if isinstance(is_copied_context, bool):
        stripped["is_copied_context"] = is_copied_context
    metrics = record.get("metrics")
    if isinstance(metrics, dict):
        stripped["metrics"] = _strip_metrics(metrics)
    observation = record.get("observation")
    if isinstance(observation, dict):
        # System steps carry their result inline; it is stripped exactly like a
        # streamed observation record's.
        stripped["observation"] = {"results": _strip_observation_results(observation.get("results"))}
    extra = record.get("extra")
    if isinstance(extra, dict):
        allowlisted = {key: extra[key] for key in _ALLOWLISTED_STEP_EXTRA_FIELDS if key in extra}
        if "is_sidechain" in extra:
            allowlisted["is_sidechain"] = bool(extra["is_sidechain"])
        context_management = _strip_context_management(extra.get("context_management"))
        if context_management is not None:
            allowlisted["context_management"] = context_management
        if allowlisted:
            stripped["extra"] = allowlisted
    return stripped


def _strip_atif_record(record: dict[str, Any], record_type: str) -> dict[str, Any] | None:
    if record_type == "header":
        header_envelope = _envelope_or_none(record, _ATIF_HEADER_ENVELOPE_FIELDS)
        if header_envelope is None:
            return None
        return {**header_envelope, **_allowlisted_extras(record)}
    envelope = _envelope_or_none(record, _ATIF_EVENT_ENVELOPE_FIELDS)
    if envelope is None:
        return None
    if record_type == "step":
        return _strip_atif_step(record, envelope)
    return {
        **envelope,
        **_allowlisted_extras(record),
        "results": _strip_observation_results(record.get("results")),
    }


def strip_transcript_record(record: dict[str, Any]) -> dict[str, Any] | None:
    """The structural strip: returns the surviving fields, or None to drop the record.

    Handles both stream vintages: the ATIF-shaped records (``header`` / ``step``
    / ``observation``) and, for agents provisioned before the cutover, the
    legacy ``user_message`` / ``assistant_message`` / ``tool_result`` records.
    """
    if record.get("type") in _ATIF_RECORD_TYPES:
        return _strip_atif_record(record, str(record["type"]))
    envelope = _envelope_or_none(record, _LEGACY_ENVELOPE_FIELDS)
    if envelope is None:
        return None
    record_type = envelope["type"]
    # CLEANUP: the legacy branches below serve agents provisioned before the ATIF
    # cutover, which keep their old emitter for life. Remove them once no
    # pre-cutover agent is still collected from.
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
        usage = record.get("usage")
        if isinstance(usage, dict):
            stripped["usage"] = _allowlisted_counters(usage, _ALLOWLISTED_LEGACY_USAGE_FIELDS)
        return stripped
    if record_type == "tool_result":
        output = record.get("output", "")
        return {
            **envelope,
            **_allowlisted_extras(record),
            "tool_call_id": str(record.get("tool_call_id", "")),
            "tool_name": str(record.get("tool_name", "")),
            "is_error": bool(record.get("is_error", False)),
            "output_byte_count": _byte_count(output),
        }
    return None


def _scrubbable_text_slots(record: dict[str, Any]) -> list[tuple[str, int | None]]:
    """The (field, part index) slots of a stripped record that hold message text."""
    slots: list[tuple[str, int | None]] = []
    # "message" is the ATIF step's text; "content"/"text" are the legacy records'.
    for text_field in ("message", "content", "text"):
        if text_field in record:
            slots.append((text_field, None))
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


def _shannon_entropy(segment: str) -> float:
    counts = Counter(segment)
    total = len(segment)
    return -sum((count / total) * math.log2(count / total) for count in counts.values())


def _character_class(character: str) -> str:
    if character.isdigit():
        return "digit"
    if character.islower():
        return "lower"
    if character.isupper():
        return "upper"
    return "other"


def _character_class_alternations(segment: str) -> int:
    """How often consecutive characters switch between digit/lower/upper/other."""
    alternation_count = 0
    previous_class = None
    for character in segment:
        current_class = _character_class(character)
        if previous_class is not None and current_class != previous_class:
            alternation_count += 1
        previous_class = current_class
    return alternation_count


def _is_random_looking_segment(segment: str) -> bool:
    if _UUID_SEGMENT_RE.match(segment) is not None:
        return True
    if _HEX_SEGMENT_RE.match(segment) is not None:
        return True
    if _DIGIT_RUN_RE.search(segment) is not None:
        return True
    digit_count = sum(1 for character in segment if character.isdigit())
    # High-entropy token shapes (base64-ish, mixed alphanumerics). The digit
    # and hyphen requirements keep hyphenated English and camelCase words out.
    if (
        _TOKEN_CHARSET_RE.match(segment) is not None
        and digit_count >= 2
        and segment.count("-") <= 1
        and _character_class_alternations(segment) >= 6
        and _shannon_entropy(segment) >= 3.5
    ):
        return True
    if (
        _ALNUM_SEGMENT_RE.match(segment) is not None
        and digit_count >= 2
        and _character_class_alternations(segment) >= 5
        and _shannon_entropy(segment) >= 3.3
    ):
        return True
    return False


def _scrub_chunk_segments(chunk: str) -> str:
    """Scrub one whitespace-delimited chunk per '/'-segment, keeping the readable parts."""
    scrubbed_segments = []
    for segment in chunk.split("/"):
        core = segment.strip(_SURROUNDING_PUNCTUATION)
        if core and _is_random_looking_segment(core):
            scrubbed_segments.append(segment.replace(core, REDACTED_TOKEN_MARKER, 1))
        else:
            scrubbed_segments.append(segment)
    return "/".join(scrubbed_segments)


def scrub_random_tokens(text: str) -> str:
    """Replace random-looking identifier tokens with ``[REDACTED_TOKEN]``.

    Deliberately aggressive: collected transcripts exist for reading the
    words, so identifier-shaped noise (uuids, long hex, long digit runs,
    high-entropy tokens) is dropped. Workspace-local paths (``/home/user...``,
    ``~/...``) are kept whole; other path-ish chunks are scrubbed per
    '/'-segment so the readable part of a path survives.
    """
    output_pieces = []
    for chunk in re.split(r"(\s+)", text):
        if not chunk or chunk.isspace():
            output_pieces.append(chunk)
        elif chunk.strip(_SURROUNDING_PUNCTUATION).startswith(_KEPT_CHUNK_PREFIXES):
            output_pieces.append(chunk)
        else:
            output_pieces.append(_scrub_chunk_segments(chunk))
    return "".join(output_pieces)


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
    """The full redaction pipeline: structural strip, secret lines, PII, then random tokens."""
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
        scrubbed = scrub_random_tokens(scrub_pii(secret_redacted))
        if part_idx is not None:
            record[field][part_idx]["content"] = scrubbed
        else:
            record[field] = scrubbed
    return RedactedTranscriptBatch(records=tuple(stripped_records), dropped_record_count=dropped_record_count)
