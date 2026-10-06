import json
import os
from pathlib import Path
from typing import Any

import pytest

from imbue.chat.activity_state import ActivityState
from imbue.chat.activity_state import CompactionCause
from imbue.chat.activity_state import parse_iso_timestamp_to_epoch
from imbue.chat.compaction_status import COMPACTION_REQUEST_FILENAME
from imbue.chat.compaction_status import CompactionRequest
from imbue.chat.compaction_status import CompactionTrigger
from imbue.chat.compaction_status import cause_of_compacting_marker
from imbue.chat.compaction_status import cause_of_last_compaction
from imbue.chat.compaction_status import is_context_compacted_event
from imbue.chat.compaction_status import read_compaction_request
from imbue.chat.compaction_status import read_compaction_signal
from imbue.chat.compaction_status import shown_compaction_cause
from imbue.chat.compaction_status import write_compaction_request
from imbue.chat.harnesses.events import DisplayKind

_STARTED_AT = "2026-10-06T21:14:07.000000000Z"
_MTIME = 1_791_000_123.0
# Later than every time the files record, so none is clamped unless a test means it to be.
_NOW = 1_800_000_000.0


def _write_with_mtime(path: Path, content: str) -> Path:
    path.write_text(content)
    os.utime(path, (_MTIME, _MTIME))
    return path


def test_read_compaction_signal_reads_the_hooks_marker(tmp_path: Path) -> None:
    marker = _write_with_mtime(tmp_path / "compacting", json.dumps({"trigger": "auto", "started_at": _STARTED_AT}))

    signal = read_compaction_signal(marker, _NOW)

    assert signal is not None
    assert signal.trigger == CompactionTrigger.AUTO
    assert signal.written_at == parse_iso_timestamp_to_epoch(_STARTED_AT)


def test_read_compaction_signal_reads_the_last_compaction_record(tmp_path: Path) -> None:
    record = _write_with_mtime(
        tmp_path / "last_compaction.json", json.dumps({"trigger": "manual", "ended_at": _STARTED_AT})
    )

    signal = read_compaction_signal(record, _NOW)

    assert signal is not None
    assert signal.trigger == CompactionTrigger.MANUAL
    assert signal.written_at == parse_iso_timestamp_to_epoch(_STARTED_AT)


def test_read_compaction_signal_is_none_for_a_missing_file(tmp_path: Path) -> None:
    assert read_compaction_signal(tmp_path / "compacting", _NOW) is None


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(json.dumps({"trigger": "manual"}), id="no_time"),
        pytest.param(json.dumps({"trigger": "manual", "started_at": "yesterday"}), id="unparseable_time"),
    ],
)
def test_read_compaction_signal_falls_back_to_the_mtime_without_a_readable_time(tmp_path: Path, content: str) -> None:
    signal = read_compaction_signal(_write_with_mtime(tmp_path / "compacting", content), _NOW)

    assert signal is not None
    assert signal.trigger == CompactionTrigger.MANUAL
    assert signal.written_at == _MTIME


def test_read_compaction_signal_warns_and_uses_the_mtime_for_a_corrupt_file(
    tmp_path: Path, loguru_records: list[str]
) -> None:
    signal = read_compaction_signal(_write_with_mtime(tmp_path / "compacting", '{"trigger": "man'), _NOW)

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
    signal = read_compaction_signal(_write_with_mtime(tmp_path / "compacting", content), _NOW)

    assert signal is not None
    assert signal.trigger == CompactionTrigger.UNKNOWN


def test_read_compaction_signal_falls_back_to_the_mtime_for_a_recorded_time_in_the_future(tmp_path: Path) -> None:
    marker = _write_with_mtime(tmp_path / "compacting", json.dumps({"trigger": "auto", "started_at": _STARTED_AT}))
    started_at = parse_iso_timestamp_to_epoch(_STARTED_AT)
    assert started_at is not None and started_at - 3600.0 > _MTIME

    signal = read_compaction_signal(marker, started_at - 3600.0)

    assert signal is not None
    assert signal.trigger == CompactionTrigger.AUTO
    assert signal.written_at == _MTIME


def test_read_compaction_signal_reads_an_mtime_in_the_future_as_now(tmp_path: Path) -> None:
    marker = _write_with_mtime(tmp_path / "compacting", json.dumps({"trigger": "auto"}))

    signal = read_compaction_signal(marker, _MTIME - 60.0)

    assert signal is not None
    assert signal.written_at == _MTIME - 60.0


def test_read_compaction_signal_warns_and_uses_the_mtime_for_an_undecodable_file(
    tmp_path: Path, loguru_records: list[str]
) -> None:
    marker = tmp_path / "compacting"
    marker.write_bytes(b"\xff\xfe\x00not utf-8")
    os.utime(marker, (_MTIME, _MTIME))

    signal = read_compaction_signal(marker, _NOW)

    assert signal is not None
    assert signal.trigger == CompactionTrigger.UNKNOWN
    assert signal.written_at == _MTIME
    assert any(record.startswith("WARNING Failed to decode the compaction record") for record in loguru_records)


@pytest.mark.parametrize("cause", [CompactionCause.IDLE, CompactionCause.MANUAL])
def test_a_written_compaction_request_reads_back(tmp_path: Path, cause: CompactionCause) -> None:
    path = tmp_path / COMPACTION_REQUEST_FILENAME

    write_compaction_request(path, cause, _MTIME)

    assert read_compaction_request(path, _NOW) == CompactionRequest(cause=cause, requested_at=_MTIME)
    assert json.loads(path.read_text())["cause"] == cause.value
    assert [entry.name for entry in tmp_path.iterdir()] == [COMPACTION_REQUEST_FILENAME]


def test_a_compaction_request_is_not_written_into_a_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        write_compaction_request(tmp_path / "gone" / COMPACTION_REQUEST_FILENAME, CompactionCause.IDLE, _MTIME)

    assert not (tmp_path / "gone").exists()


def test_a_compaction_request_time_in_the_future_reads_as_now(tmp_path: Path) -> None:
    path = tmp_path / COMPACTION_REQUEST_FILENAME
    write_compaction_request(path, CompactionCause.IDLE, _NOW + 3600.0)

    request = read_compaction_request(path, _NOW)

    assert request is not None
    assert request.requested_at == _NOW


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(b'{"cause": "idle"', id="truncated"),
        pytest.param(json.dumps({"cause": "sideways", "requested_at": _STARTED_AT}).encode(), id="unknown_cause"),
        pytest.param(json.dumps({"cause": "idle", "requested_at": "yesterday"}).encode(), id="unparseable_time"),
        pytest.param(b"\xff\xfe\x00not utf-8", id="undecodable"),
    ],
)
def test_an_unreadable_compaction_request_reads_as_none_with_a_warning(
    tmp_path: Path, loguru_records: list[str], content: bytes
) -> None:
    path = tmp_path / COMPACTION_REQUEST_FILENAME
    path.write_bytes(content)

    assert read_compaction_request(path, _NOW) is None
    assert any(record.startswith("WARNING Failed to") for record in loguru_records)


def test_a_missing_compaction_request_reads_as_none(tmp_path: Path) -> None:
    assert read_compaction_request(tmp_path / COMPACTION_REQUEST_FILENAME, _NOW) is None


@pytest.mark.parametrize(
    "marker_cause, pending_cause, derived_state, expected",
    [
        pytest.param(CompactionCause.NATIVE, None, ActivityState.TOOL_RUNNING, CompactionCause.NATIVE, id="marker"),
        pytest.param(
            CompactionCause.IDLE,
            CompactionCause.IDLE,
            ActivityState.THINKING,
            CompactionCause.IDLE,
            id="marker_mid_turn",
        ),
        pytest.param(None, CompactionCause.MANUAL, ActivityState.IDLE, CompactionCause.MANUAL, id="request_when_idle"),
        pytest.param(None, CompactionCause.MANUAL, ActivityState.THINKING, None, id="request_behind_a_turn"),
        pytest.param(None, CompactionCause.IDLE, ActivityState.TOOL_RUNNING, None, id="request_behind_a_tool"),
        pytest.param(None, None, ActivityState.IDLE, None, id="nothing"),
    ],
)
def test_shown_compaction_cause(
    marker_cause: CompactionCause | None,
    pending_cause: CompactionCause | None,
    derived_state: ActivityState,
    expected: CompactionCause | None,
) -> None:
    assert shown_compaction_cause(marker_cause, pending_cause, derived_state) == expected


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
