"""Unit tests for the pixelflux video pipe: wire header parsing and credit flow."""

import pytest

from streamed_browser.videopipe import (
    FRAME_TYPE_IDR,
    WIRE_HEADER_LEN,
    CreditWindow,
    VideoPipeError,
    parse_wire_header,
)


def _packet(frame_id: int, frame_type: int, y_start: int = 0, payload: bytes = b"\x00" * 4) -> bytes:
    header = bytes(
        [0x04, frame_type, frame_id >> 8, frame_id & 0xFF, y_start >> 8, y_start & 0xFF, 5, 0, 3, 32]
    )
    return header + payload


def test_parse_wire_header_reads_id_row_type_and_idr_flag() -> None:
    frame_id, y_start, frame_type, is_idr = parse_wire_header(_packet(517, FRAME_TYPE_IDR, y_start=400))
    assert (frame_id, y_start, frame_type, is_idr) == (517, 400, FRAME_TYPE_IDR, True)
    frame_id, y_start, frame_type, is_idr = parse_wire_header(_packet(0, 0x00))
    assert (frame_id, y_start, frame_type, is_idr) == (0, 0, 0x00, False)


def test_parse_wire_header_rejects_short_and_foreign_packets() -> None:
    with pytest.raises(VideoPipeError):
        parse_wire_header(b"\x04\x01\x00")
    with pytest.raises(VideoPipeError):
        parse_wire_header(b"\x03" + b"\x00" * (WIRE_HEADER_LEN + 4))


def test_credit_window_blocks_at_limit_and_reopens_on_ack() -> None:
    window = CreditWindow(limit=2)
    assert window.admits(is_keyframe=True)
    window.note_sent_keyframe(1)
    window.note_sent(2)
    assert not window.admits(is_keyframe=False)
    assert not window.admits(is_keyframe=True)  # credit, not keyframes, is the gate
    window.ack(1)
    assert window.admits(is_keyframe=False)


def test_credit_window_ack_is_cumulative() -> None:
    window = CreditWindow(limit=2)
    window.note_sent(10)
    window.note_sent(11)
    assert not window.admits(is_keyframe=False)
    window.ack(11)  # cumulative: acknowledges 10 as well
    window.note_sent(12)
    window.note_sent(13)
    assert not window.admits(is_keyframe=False), "cumulative ack must have cleared exactly two slots"


def test_dropped_delta_requires_keyframe_to_resume() -> None:
    window = CreditWindow(limit=2)
    window.note_sent(1)
    window.note_dropped_delta()
    assert not window.admits(is_keyframe=False)
    assert window.admits(is_keyframe=True)
    window.note_sent_keyframe(2)
    window.ack(2)
    assert window.admits(is_keyframe=False)


def test_ack_of_unknown_frame_is_harmless() -> None:
    window = CreditWindow(limit=1)
    window.note_sent(5)
    window.ack(9999)
    assert not window.admits(is_keyframe=False)
    window.ack(5)
    assert window.admits(is_keyframe=False)


def test_capture_rate_tracks_delivery_and_snaps_to_max_when_idle() -> None:
    from streamed_browser.videopipe import target_capture_fps

    assert target_capture_fps(0, 2) == 60.0  # idle -> ceiling
    # 40 stripes over the 2s window on 2 rows = 10 fps delivered -> 1.5x + 2.
    assert target_capture_fps(40, 2) == 17.0
    assert target_capture_fps(4, 2) == 15.0  # floor
    assert target_capture_fps(400, 2) == 60.0  # cap
