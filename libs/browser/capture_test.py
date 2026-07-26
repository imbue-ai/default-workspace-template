"""Unit tests for the stripe fan-out in browser.capture (no real X server or encoder
-- the fan-out path is exercised directly with fake stripe buffers)."""

import queue

import pytest
from browser import capture as capture_mod
from browser.capture import Capture


def _capture() -> Capture:
    return Capture(":0", lambda: (0, 0, 100, 100))


class _FakeCap:
    """Stand-in for a pixelflux ScreenCapture: records stop, so we can assert the encoder
    is torn down when the last subscriber leaves."""

    def __init__(self) -> None:
        self.stopped = False

    def stop_capture(self) -> None:
        self.stopped = True

    def request_idr_frame(self) -> None:
        pass


def test_removing_the_last_subscriber_nulls_and_stops_the_encoder() -> None:
    # The stop must run with _cap already nulled (and, in production, OUTSIDE the lock --
    # the deadlock fix): here we just assert the last-out path stops + clears the capture.
    cap = _capture()
    fake = _FakeCap()
    cap._cap = fake  # type: ignore[assignment]
    q: "queue.Queue[bytes | None]" = queue.Queue()
    cap._subscribers = [q]
    cap.remove_subscriber(q)
    assert fake.stopped
    assert cap._cap is None
    assert not cap._subscribers


def test_add_subscriber_returns_none_and_does_not_register_when_pixelflux_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Native libs missing (CI / deferred-install still running): the first subscriber must
    # NOT be registered (so the handler closes the socket and the viewer retries) instead
    # of holding a videoless slot forever.
    monkeypatch.setattr(capture_mod, "_load_pixelflux", lambda: None)
    cap = _capture()
    assert cap.add_subscriber(True) is None
    assert not cap._subscribers


def test_on_stripe_fans_out_verbatim_to_every_subscriber() -> None:
    cap = _capture()
    q1: "queue.Queue[bytes | None]" = queue.Queue(maxsize=8)
    q2: "queue.Queue[bytes | None]" = queue.Queue(maxsize=8)
    cap._subscribers = [q1, q2]
    cap._on_stripe(bytearray(b"\x04\x01stripe-bytes"))  # buffer-protocol frame
    assert q1.get_nowait() == b"\x04\x01stripe-bytes"
    assert q2.get_nowait() == b"\x04\x01stripe-bytes"


def test_on_stripe_drops_oldest_when_a_subscriber_is_full() -> None:
    cap = _capture()
    q: "queue.Queue[bytes | None]" = queue.Queue(maxsize=2)
    cap._subscribers = [q]
    for i in range(4):
        cap._on_stripe(bytearray([0x04, i]))
    # Only the two most recent survive; the encoder never blocks on a slow client.
    assert [q.get_nowait(), q.get_nowait()] == [bytes([0x04, 2]), bytes([0x04, 3])]
    assert q.empty()


def test_has_subscribers_tracks_the_list() -> None:
    cap = _capture()
    assert not cap.has_subscribers()
    cap._subscribers.append(queue.Queue())
    assert cap.has_subscribers()


def test_jpeg_stripe_fans_out_too() -> None:
    cap = _capture()
    q: "queue.Queue[bytes | None]" = queue.Queue(maxsize=1)
    cap._subscribers = [q]
    cap._on_stripe(bytearray(b"\x03jpeg"))  # 0x03 = JPEG header byte
    assert q.get_nowait() == b"\x03jpeg"
