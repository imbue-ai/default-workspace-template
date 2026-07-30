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
    assert window.admits(is_keyframe=False) or window.admits(is_keyframe=True)
    window.note_sent(1)
    window.note_sent(2)
    assert not window.admits(is_keyframe=False)
    assert not window.admits(is_keyframe=True)  # credit, not keyframes, is the gate
    window.ack(1)
    assert window.admits(is_keyframe=False)


def test_unacked_keyframe_consumes_the_whole_window() -> None:
    # Stripes vary ~25x in bytes; an IDR must monopolize the window until it
    # lands, or "bounded bytes in flight" fails exactly at recovery time.
    window = CreditWindow(limit=2)
    window.note_sent_keyframe(7)
    assert not window.admits(is_keyframe=False)
    assert not window.admits(is_keyframe=True)
    window.ack(7)
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


def test_sticky_idr_survives_later_deltas_in_the_mailbox() -> None:
    # A delta overwriting an unsent IDR re-broke the row's chain and looped
    # into >=0.4s recovery cycles (probe-verified dominant freeze mechanism).
    from streamed_browser.videopipe import PixelfluxVideoPipe

    pipe = PixelfluxVideoPipe("test", ":0")
    idr = _packet(1, FRAME_TYPE_IDR)
    delta = _packet(2, 0x00)
    pipe._on_frame(idr)
    pipe._on_frame(delta)  # must NOT displace the pending keyframe
    delivered = pipe.next_packet(timeout=0.1)
    assert delivered == idr
    assert pipe.frames_dropped == 1


def test_ack_of_unknown_frame_is_harmless() -> None:
    window = CreditWindow(limit=1)
    window.note_sent(5)
    window.ack(9999)
    assert not window.admits(is_keyframe=False)
    window.ack(5)
    assert window.admits(is_keyframe=False)


def test_capture_rate_steps_to_delivered_on_drops_and_climbs_gently() -> None:
    from streamed_browser.videopipe import target_capture_fps

    # Drops with a measured delivered rate: converge to delivered * 1.2 in one step.
    assert target_capture_fps(60.0, dropped_in_interval=5, delivered_fps=10.0) == 12.0
    # Drops with nothing delivered: multiplicative fallback.
    assert target_capture_fps(60.0, dropped_in_interval=5, delivered_fps=0.0) == 36.0
    # Floor.
    assert target_capture_fps(10.0, dropped_in_interval=1, delivered_fps=1.0) == 8.0
    # Drop-free: gentle additive climb, capped.
    assert target_capture_fps(36.0, dropped_in_interval=0, delivered_fps=20.0) == 39.0
    assert target_capture_fps(60.0, dropped_in_interval=0, delivered_fps=30.0) == 60.0
