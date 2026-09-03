import pytest
from browser import videopipe
from browser.videopipe import target_capture_fps


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
