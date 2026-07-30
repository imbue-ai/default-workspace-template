"""Unit tests for the naive video pipe: wire-header parsing + the (absent) flow control.

These assert the pipe ships stripes with NO credit gate -- the whole point of the
bad-tcp foil -- while still keeping the decode chain correct (sticky IDR, resync on
a broken chain).
"""

import pytest

from bad_tcp_browser.videopipe import (
    FRAME_TYPE_IDR,
    WIRE_HEADER_LEN,
    PixelfluxVideoPipe,
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


def test_stripe_ships_immediately_with_no_credit_gate() -> None:
    # The naive pipe has no credit window: a mailboxed stripe is returned at once,
    # and there is no per-row send limit to exhaust (the whole latency defense the
    # optimized pipe has is absent here, on purpose).
    pipe = PixelfluxVideoPipe("test", ":0")
    first = _packet(1, FRAME_TYPE_IDR)
    pipe._on_frame(first)
    assert pipe.next_packet(timeout=0.1) == first
    # A second frame on the same row (no ack ever sent) still ships -- nothing gated.
    second = _packet(2, 0x00)
    pipe._on_frame(second)
    assert pipe.next_packet(timeout=0.1) == second


def test_sticky_idr_survives_later_deltas_in_the_mailbox() -> None:
    # A delta must NOT displace an unsent IDR (that would re-break the row's chain).
    pipe = PixelfluxVideoPipe("test", ":0")
    idr = _packet(1, FRAME_TYPE_IDR)
    delta = _packet(2, 0x00)
    pipe._on_frame(idr)
    pipe._on_frame(delta)
    assert pipe.next_packet(timeout=0.1) == idr
    assert pipe.frames_dropped == 1


def test_broken_chain_delta_waits_for_a_keyframe() -> None:
    # Overwriting an unsent delta breaks the decode chain: the row drops deltas until
    # a keyframe arrives (correctness, not flow control).
    pipe = PixelfluxVideoPipe("test", ":0")
    pipe._on_frame(_packet(1, 0x00))
    pipe._on_frame(_packet(2, 0x00))  # overwrite -> chain broken, needs_keyframe
    assert pipe.next_packet(timeout=0.05) is None  # the broken delta is dropped, not shipped
    idr = _packet(3, FRAME_TYPE_IDR)
    pipe._on_frame(idr)
    assert pipe.next_packet(timeout=0.1) == idr  # the keyframe resyncs the row
