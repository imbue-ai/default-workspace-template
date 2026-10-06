import json
import os
from pathlib import Path
from typing import Any

import pytest

from imbue.chat.activity_state import CompactionCause
from imbue.chat.activity_state import parse_iso_timestamp_to_epoch
from imbue.chat.compaction_status import CompactionTrigger
from imbue.chat.compaction_status import cause_of_compacting_marker
from imbue.chat.compaction_status import cause_of_last_compaction
from imbue.chat.compaction_status import is_context_compacted_event
from imbue.chat.compaction_status import read_compaction_signal
from imbue.chat.harnesses.events import DisplayKind

_STARTED_AT = "2026-10-06T21:14:07.000000000Z"
_MTIME = 1_791_000_123.0


def _write_with_mtime(path: Path, content: str) -> Path:
    path.write_text(content)
    os.utime(path, (_MTIME, _MTIME))
    return path


def test_read_compaction_signal_reads_the_hooks_marker(tmp_path: Path) -> None:
    marker = _write_with_mtime(tmp_path / "compacting", json.dumps({"trigger": "auto", "started_at": _STARTED_AT}))

    signal = read_compaction_signal(marker)

    assert signal is not None
    assert signal.trigger == CompactionTrigger.AUTO
    assert signal.written_at == parse_iso_timestamp_to_epoch(_STARTED_AT)


def test_read_compaction_signal_reads_the_last_compaction_record(tmp_path: Path) -> None:
    record = _write_with_mtime(
        tmp_path / "last_compaction.json", json.dumps({"trigger": "manual", "ended_at": _STARTED_AT})
    )

    signal = read_compaction_signal(record)

    assert signal is not None
    assert signal.trigger == CompactionTrigger.MANUAL
    assert signal.written_at == parse_iso_timestamp_to_epoch(_STARTED_AT)


def test_read_compaction_signal_is_none_for_a_missing_file(tmp_path: Path) -> None:
    assert read_compaction_signal(tmp_path / "compacting") is None


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(json.dumps({"trigger": "manual"}), id="no_time"),
        pytest.param(json.dumps({"trigger": "manual", "started_at": "yesterday"}), id="unparseable_time"),
    ],
)
def test_read_compaction_signal_falls_back_to_the_mtime_without_a_readable_time(tmp_path: Path, content: str) -> None:
    signal = read_compaction_signal(_write_with_mtime(tmp_path / "compacting", content))

    assert signal is not None
    assert signal.trigger == CompactionTrigger.MANUAL
    assert signal.written_at == _MTIME


def test_read_compaction_signal_warns_and_uses_the_mtime_for_a_corrupt_file(
    tmp_path: Path, loguru_records: list[str]
) -> None:
    signal = read_compaction_signal(_write_with_mtime(tmp_path / "compacting", '{"trigger": "man'))

    assert signal is not None
    assert signal.trigger == CompactionTrigger.UNKNOWN
    assert signal.written_at == _MTIME
    assert any(record.startswith("WARNING Failed to parse the compaction record") for record in loguru_records)


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(json.dumps({"started_at": _STARTED_AT}), id="missing"),
        pytest.param(json.dumps({"trigger": "unknown", "started_at": _STARTED_AT}), id="unknown"),
        pytest.param(json.dumps({"trigger": "sideways", "started_at": _STARTED_AT}), id="unrecognized"),
    ],
)
def test_read_compaction_signal_reads_an_absent_or_odd_trigger_as_unknown(tmp_path: Path, content: str) -> None:
    signal = read_compaction_signal(_write_with_mtime(tmp_path / "compacting", content))

    assert signal is not None
    assert signal.trigger == CompactionTrigger.UNKNOWN


@pytest.mark.parametrize(
    "trigger, pending_cause, expected",
    [
        pytest.param(CompactionTrigger.AUTO, None, CompactionCause.NATIVE, id="auto"),
        pytest.param(CompactionTrigger.AUTO, CompactionCause.IDLE, CompactionCause.NATIVE, id="auto_beats_request"),
        pytest.param(CompactionTrigger.MANUAL, CompactionCause.IDLE, CompactionCause.IDLE, id="manual_by_sweep"),
        pytest.param(CompactionTrigger.MANUAL, None, CompactionCause.MANUAL, id="manual_typed"),
        pytest.param(CompactionTrigger.UNKNOWN, CompactionCause.IDLE, CompactionCause.IDLE, id="unknown_by_sweep"),
        pytest.param(CompactionTrigger.UNKNOWN, None, CompactionCause.MANUAL, id="unknown_alone"),
    ],
)
def test_cause_of_compacting_marker(
    trigger: CompactionTrigger, pending_cause: CompactionCause | None, expected: CompactionCause
) -> None:
    assert cause_of_compacting_marker(trigger, pending_cause) == expected


@pytest.mark.parametrize(
    "trigger, expected",
    [
        pytest.param(CompactionTrigger.AUTO, CompactionCause.NATIVE, id="auto"),
        pytest.param(CompactionTrigger.MANUAL, CompactionCause.MANUAL, id="manual"),
        pytest.param(CompactionTrigger.UNKNOWN, None, id="unknown"),
    ],
)
def test_cause_of_last_compaction(trigger: CompactionTrigger, expected: CompactionCause | None) -> None:
    assert cause_of_last_compaction(trigger) == expected


@pytest.mark.parametrize(
    "event, expected",
    [
        pytest.param(
            {"type": "user_message", "display": DisplayKind.STATUS, "content": "Context was compacted"},
            True,
            id="compacted",
        ),
        pytest.param(
            {"type": "user_message", "display": DisplayKind.STATUS, "content": "Something else"},
            False,
            id="other_status",
        ),
        pytest.param({"type": "user_message", "content": "Context was compacted"}, False, id="typed_by_user"),
        pytest.param({"type": "assistant_message", "content": "Context was compacted"}, False, id="assistant"),
    ],
)
def test_is_context_compacted_event(event: dict[str, Any], expected: bool) -> None:
    assert is_context_compacted_event(event) is expected
