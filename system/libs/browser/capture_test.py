"""Unit tests for the stripe fan-out in browser.capture (no real X server or encoder
-- the fan-out path is exercised directly with fake stripe buffers)."""

import queue
import struct
import time

import pytest
from browser import capture as capture_mod
from browser.capture import Capture, StripeMailbox, _DamageRateBooster


def _capture() -> Capture:
    return Capture(":0", lambda: (0, 0, 100, 100))


def _h264(y: int, idr: bool = False, tag: bytes = b"") -> bytes:
    """A minimal valid H.264 stripe: [0]=0x04, [1]=frametype, [2:4]=frame_id, [4:6]=y."""
    return bytes([0x04, 0x01 if idr else 0x00, 0, 0]) + struct.pack(">H", y) + tag


def _jpeg(y: int, tag: bytes = b"") -> bytes:
    """A minimal valid JPEG stripe: [0]=0x03, [1]=pad, [2:4]=frame_id, [4:6]=y."""
    return bytes([0x03, 0, 0, 0]) + struct.pack(">H", y) + tag


class _FakeCap:
    """Stand-in for a pixelflux ScreenCapture: records stop + IDR requests, so we can
    assert teardown and the keyframe-on-drop policy."""

    def __init__(self) -> None:
        self.stopped = False
        self.idr_requests = 0

    def stop_capture(self) -> None:
        self.stopped = True

    def request_idr_frame(self) -> None:
        self.idr_requests += 1


class _FakeBooster:
    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


# --- StripeMailbox semantics -------------------------------------------------


def test_mailbox_delivers_stripes_in_row_arrival_order() -> None:
    box = StripeMailbox()
    box.put(_h264(0, idr=True, tag=b"a"))
    box.put(_h264(400, idr=True, tag=b"b"))
    assert box.get_nowait() == _h264(0, idr=True, tag=b"a")
    assert box.get_nowait() == _h264(400, idr=True, tag=b"b")
    with pytest.raises(queue.Empty):
        box.get_nowait()


def test_mailbox_keeps_only_the_newest_stripe_per_row() -> None:
    # An unsent stripe for a row is strictly obsolete once a newer one exists: the
    # client draws each row from whatever arrives last, so delivering the stale one
    # would spend socket time showing something already false.
    box = StripeMailbox()
    box.put(_h264(0, idr=True, tag=b"old"))
    box.put(_h264(0, idr=True, tag=b"new"))
    assert box.get_nowait() == _h264(0, idr=True, tag=b"new")
    with pytest.raises(queue.Empty):
        box.get_nowait()


def test_mailbox_replacement_keeps_the_rows_queue_position() -> None:
    # Replacing row 0's pending stripe must not push it behind row 400 -- pop order
    # stays oldest-pending-row first so no row is starved by a chattier one.
    box = StripeMailbox()
    box.put(_h264(0, idr=True, tag=b"first"))
    box.put(_h264(400, idr=True))
    box.put(_h264(0, idr=True, tag=b"second"))
    assert box.get_nowait() == _h264(0, idr=True, tag=b"second")
    assert box.get_nowait() == _h264(400, idr=True)


def test_mailbox_reports_a_broken_chain_when_a_delta_replaces_an_unsent_h264_stripe() -> None:
    # Dropping an unsent H.264 stripe leaves the client's decoder without the state the
    # replacement DELTA assumes -> the row corrupts until a keyframe. put() must say so.
    box = StripeMailbox()
    assert box.put(_h264(0)) is False  # nothing replaced
    assert box.put(_h264(0)) is True  # delta replaced an unsent delta -> chain broken
    assert box.put(_h264(0, idr=True)) is False  # a keyframe replacement resets the chain
    assert box.put(_h264(0)) is True  # ...but a delta replacing the unsent KEYFRAME breaks it


def test_mailbox_jpeg_replacement_never_breaks_a_chain() -> None:
    # JPEG stripes are self-contained; dropping one loses nothing but staleness.
    box = StripeMailbox()
    assert box.put(_jpeg(0)) is False
    assert box.put(_jpeg(0)) is False


def test_mailbox_unrecognized_payloads_pass_through_uncoalesced() -> None:
    # Anything without a parseable stripe header must never be merged away.
    box = StripeMailbox()
    assert box.put(b"??") is False
    assert box.put(b"??") is False
    assert box.get_nowait() == b"??"
    assert box.get_nowait() == b"??"


def test_mailbox_close_drains_then_yields_the_shutdown_sentinel() -> None:
    box = StripeMailbox()
    box.put(_h264(0, idr=True))
    box.close()
    assert box.get_nowait() == _h264(0, idr=True)  # pending stripes still delivered
    assert box.get_nowait() is None  # then the sentinel, forever
    assert box.get_nowait() is None
    assert box.put(_h264(0)) is False  # puts after close are ignored


def test_mailbox_get_times_out_like_a_queue() -> None:
    box = StripeMailbox()
    started = time.monotonic()
    with pytest.raises(queue.Empty):
        box.get(timeout=0.05)
    assert time.monotonic() - started < 5  # sanity: it timed out rather than hanging


# --- Capture fan-out ---------------------------------------------------------


def test_removing_the_last_subscriber_nulls_and_stops_the_encoder_and_booster() -> None:
    # The stop must run with _cap already nulled (and, in production, OUTSIDE the lock --
    # the deadlock fix): here we just assert the last-out path stops + clears everything.
    cap = _capture()
    fake = _FakeCap()
    booster = _FakeBooster()
    cap._cap = fake  # type: ignore[assignment]
    cap._booster = booster  # type: ignore[assignment]
    box = StripeMailbox()
    cap._subscribers = [box]
    cap.remove_subscriber(box)
    assert fake.stopped
    assert booster.stopped
    assert cap._cap is None
    assert cap._booster is None
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
    box1 = StripeMailbox()
    box2 = StripeMailbox()
    cap._subscribers = [box1, box2]
    cap._on_stripe(bytearray(_h264(0, idr=True, tag=b"stripe-bytes")))  # buffer-protocol frame
    assert box1.get_nowait() == _h264(0, idr=True, tag=b"stripe-bytes")
    assert box2.get_nowait() == _h264(0, idr=True, tag=b"stripe-bytes")


def test_on_stripe_requests_a_keyframe_when_a_replacement_breaks_a_chain() -> None:
    # A slow client whose unsent delta gets replaced needs an IDR to resync; the request
    # is throttled so a persistently-slow client can't force one per stripe.
    cap = _capture()
    fake = _FakeCap()
    cap._cap = fake  # type: ignore[assignment]
    box = StripeMailbox()
    cap._subscribers = [box]
    cap._on_stripe(bytearray(_h264(0)))
    assert fake.idr_requests == 0  # nothing replaced yet
    cap._on_stripe(bytearray(_h264(0)))
    assert fake.idr_requests == 1  # chain broken -> keyframe requested
    cap._on_stripe(bytearray(_h264(0)))
    assert fake.idr_requests == 1  # within the throttle window -> no second request


def test_on_stripe_close_unblocks_the_stream_socket() -> None:
    cap = _capture()
    box = StripeMailbox()
    cap._subscribers = [box]
    cap.close()
    assert box.get_nowait() is None  # sentinel: runner's send loop exits
    assert not cap._subscribers


def test_has_subscribers_tracks_the_list() -> None:
    cap = _capture()
    assert not cap.has_subscribers()
    cap._subscribers.append(StripeMailbox())
    assert cap.has_subscribers()


def test_jpeg_stripe_fans_out_too() -> None:
    cap = _capture()
    box = StripeMailbox()
    cap._subscribers = [box]
    cap._on_stripe(bytearray(_jpeg(0, tag=b"jpeg")))
    assert box.get_nowait() == _jpeg(0, tag=b"jpeg")


# --- _DamageRateBooster lifecycle -------------------------------------------


def test_booster_survives_an_unreachable_display_and_stop_is_safe() -> None:
    # The watcher is strictly an optimization: a display it cannot connect to must end
    # the thread quietly (capture continues at the base rate), and stop() afterwards
    # must not raise even though the thread never reached its select loop.
    booster = _DamageRateBooster("bogus:display:name", _FakeCap())
    booster._thread.join(timeout=10)
    assert not booster._thread.is_alive()
    booster.stop()  # closes the pipe fds; must be safe after the thread already exited
