import threading
import time
from collections.abc import Callable

import pytest
from browser import videopipe
from browser.videopipe import PixelfluxVideoPipe, target_capture_fps
from mock_capture_backend_test import FakeCaptureBackend


def test_drop_free_interval_climbs_by_increment() -> None:
    # A drop-free interval ramps the capture rate gently toward the ceiling. Start from the
    # floor (always below the cap, whatever BROWSER_VIDEO_FPS_CAP is) so this tests the climb
    # step, not the ceiling clamp (that's the next test).
    start = videopipe._RATE_MIN_FPS
    assert target_capture_fps(start, dropped_in_interval=0, delivered_fps=start, consecutive_drop_intervals=0) == pytest.approx(
        start + videopipe._RATE_INCREASE_FPS
    )


def test_drop_free_climb_is_capped_at_max() -> None:
    # The climb never exceeds the configured max fps.
    assert target_capture_fps(videopipe._RATE_MAX_FPS, 0, videopipe._RATE_MAX_FPS, 0) == videopipe._RATE_MAX_FPS


def test_single_drop_interval_backs_off_gently_not_collapse() -> None:
    # The core regression fix: one drop interval is treated as ordinary probing
    # overshoot -- shave a little and hold near the ceiling -- even when the drops
    # themselves cratered the delivered estimate. Before the fix this collapsed
    # toward delivered*1.2 (~12fps here), sawtoothing the rate to the floor.
    result = target_capture_fps(60.0, dropped_in_interval=20, delivered_fps=10.0, consecutive_drop_intervals=1)
    assert result == pytest.approx(60.0 * videopipe._RATE_GENTLE_BACKOFF)
    assert result > 40.0  # decisively NOT collapsed toward the depressed delivered estimate


def test_sustained_drops_converge_toward_delivered() -> None:
    # Once drops persist, the path genuinely cannot hold the rate: converge onto
    # what it actually delivered (clamped to the current rate).
    result = target_capture_fps(60.0, dropped_in_interval=5, delivered_fps=25.0, consecutive_drop_intervals=videopipe._SUSTAINED_DROP_INTERVALS)
    assert result == pytest.approx(25.0 * 1.1)


def test_sustained_drops_with_no_delivery_use_multiplicative_fallback() -> None:
    # Sustained drops with nothing delivered at all fall back to a multiplicative cut.
    result = target_capture_fps(50.0, dropped_in_interval=5, delivered_fps=0.0, consecutive_drop_intervals=videopipe._SUSTAINED_DROP_INTERVALS)
    assert result == pytest.approx(50.0 * videopipe._RATE_DECREASE_FACTOR)


def test_rate_never_falls_below_floor() -> None:
    # Every drop branch is floored at _RATE_MIN_FPS.
    gentle = target_capture_fps(videopipe._RATE_MIN_FPS, 5, 1.0, 1)
    sustained = target_capture_fps(videopipe._RATE_MIN_FPS, 5, 0.0, videopipe._SUSTAINED_DROP_INTERVALS)
    assert gentle >= videopipe._RATE_MIN_FPS
    assert sustained >= videopipe._RATE_MIN_FPS


def test_window_reference_exceeds_max_rate() -> None:
    # The credit window must be sized to carry the encoder's top rate, or it throttles
    # delivery below the rate and the controller collapses (the pinned-at-floor bug).
    assert videopipe._WINDOW_REFERENCE_FPS > videopipe._RATE_MAX_FPS


# --- pause, resume, and the paused wait (over a fake capture) -------------------------

_WAIT_SECONDS = 0.2
# A wait that returns at once is far below this.
_MIN_BLOCKED_SECONDS = 0.15


def _started_pipe(backend: FakeCaptureBackend) -> PixelfluxVideoPipe:
    pipe = PixelfluxVideoPipe("browser-1", ":97", backend=backend)
    pipe.start()
    return pipe


def _seconds_spent(action: Callable[[], None]) -> float:
    started = time.monotonic()
    action()
    return time.monotonic() - started


def test_a_viewer_that_connects_hidden_waits_while_paused_instead_of_spinning() -> None:
    # The connect path: the viewer's pane size is applied (leaving a ``res,`` pending),
    # then the conductor pauses the connection before anything drained it.
    backend = FakeCaptureBackend()
    pipe = _started_pipe(backend)
    pipe.set_capture_region(1000, 640)
    pipe.pause()

    assert _seconds_spent(lambda: pipe.wait_while_paused(_WAIT_SECONDS)) >= _MIN_BLOCKED_SECONDS
    assert pipe.take_control_message() is None
    assert backend.captures[0].is_stopped
    pipe.stop()


def test_a_cursor_change_landing_while_paused_does_not_wake_the_paused_wait() -> None:
    backend = FakeCaptureBackend()
    pipe = _started_pipe(backend)
    pipe.pause()
    cursor_callback = backend.captures[0].cursor_callback
    assert cursor_callback is not None
    cursor_callback(0, b"png", 3, 4)

    assert _seconds_spent(lambda: pipe.wait_while_paused(_WAIT_SECONDS)) >= _MIN_BLOCKED_SECONDS
    pipe.stop()


def test_resume_restarts_the_capture_at_the_viewers_size_and_announces_it() -> None:
    backend = FakeCaptureBackend()
    pipe = _started_pipe(backend)
    pipe.set_capture_region(1000, 640)
    pipe.pause()

    pipe.resume()

    assert not pipe.is_paused
    assert len(backend.captures) == 2
    resumed = backend.captures[1]
    assert resumed.settings is not None
    assert (resumed.settings.capture_width, resumed.settings.capture_height) == (1000, 640)
    assert resumed.idr_requests == 1
    assert pipe.take_control_message() == "res,1000,640"
    pipe.stop()


def test_the_conductors_wake_ends_the_paused_wait_at_once() -> None:
    backend = FakeCaptureBackend()
    pipe = _started_pipe(backend)
    pipe.pause()
    waiter = threading.Thread(target=lambda: pipe.wait_while_paused(5.0), daemon=True)

    def wake_until_the_wait_ends() -> None:
        # The conductor's wake, repeated until the waiter has returned, so the test never
        # depends on the waiter being inside the wait before the one notify lands.
        waiter.start()
        while waiter.is_alive():
            with pipe.condition:
                pipe.condition.notify_all()
            waiter.join(timeout=0.01)

    assert _seconds_spent(wake_until_the_wait_ends) < 1.0
    pipe.stop()


def test_the_paused_wait_returns_at_once_for_a_pipe_that_is_not_paused() -> None:
    backend = FakeCaptureBackend()
    pipe = _started_pipe(backend)

    assert _seconds_spent(lambda: pipe.wait_while_paused(_WAIT_SECONDS)) < _MIN_BLOCKED_SECONDS
    pipe.stop()
