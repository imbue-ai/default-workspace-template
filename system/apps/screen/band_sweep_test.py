"""The raise-only band sweep over a recorded process tree (spec section 9.4)."""

from mock_process_tree_test import FakeProcessTree
from oom_priority import bands
from screen.band_sweep import BandSweeper
from screen.band_sweep import sweep_once
from screen.band_sweep import target_band
from testing import ManualClock
from testing import wait_until

_SESSION_BAND = bands.SCREEN_STACK


def _tree() -> FakeProcessTree:
    # The session (pid 100) runs its own programs, a terminal the user opened from the panel (its shell and a
    # build in it), and a Chromium the user started from that terminal; the service's Chromium (pid 500) hangs
    # elsewhere in the tree with its gpu process and two renderers.
    return FakeProcessTree(
        parents={
            101: 100,
            102: 100,
            103: 100,
            104: 100,
            110: 100,
            111: 110,
            112: 111,
            120: 110,
            121: 120,
            501: 500,
            502: 500,
            503: 500,
        },
        names={
            100: "xfce4-session",
            101: "xfwm4",
            102: "xfce4-panel",
            103: "xfsettingsd",
            104: "dbus-daemon",
            110: "xfce4-terminal",
            111: "bash",
            112: "cargo",
            120: "chrome",
            121: "chrome",
            500: "chrome",
            501: "chrome",
            502: "chrome",
            503: "chrome",
        },
        adjustments={
            100: _SESSION_BAND,
            101: _SESSION_BAND,
            102: _SESSION_BAND,
            103: _SESSION_BAND,
            104: _SESSION_BAND,
            110: _SESSION_BAND,
            111: _SESSION_BAND,
            112: _SESSION_BAND,
            120: 0,
            121: 300,
            500: 0,
            501: 200,
            502: 300,
            503: 300,
        },
    )


def test_programs_on_the_screen_are_raised_and_the_sessions_own_are_not() -> None:
    tree = _tree()
    writes = sweep_once(100, [500], tree)
    raised = {write.pid: write.new for write in writes}
    assert raised[110] == bands.AGENT_SUBPROCESS
    assert raised[111] == bands.AGENT_SUBPROCESS
    assert raised[112] == bands.AGENT_SUBPROCESS
    for own in (100, 101, 102, 103, 104):
        assert own not in raised
        assert tree.adjustments[own] == _SESSION_BAND


def test_chromiums_processes_land_in_the_shared_browser_band_preserving_chromes_order() -> None:
    tree = _tree()
    sweep_once(100, [500], tree)
    assert tree.adjustments[500] == bands.SHARED_BROWSER_FLOOR
    assert (
        tree.adjustments[500]
        < tree.adjustments[501]
        < tree.adjustments[502]
        == tree.adjustments[503]
        == bands.SHARED_BROWSER
    )
    # A Chromium the user started from a terminal on the Screen is treated the same.
    assert tree.adjustments[120] == bands.SHARED_BROWSER_FLOOR
    assert tree.adjustments[121] == bands.SHARED_BROWSER
    # Chromium's renderers sit above every program on the Screen, which sit above the stack.
    assert bands.AGENT_SUBPROCESS < bands.SHARED_BROWSER_FLOOR
    assert _SESSION_BAND < bands.AGENT_SUBPROCESS


def test_the_sweep_only_raises_and_repeating_it_writes_nothing_more() -> None:
    tree = _tree()
    tree.adjustments[112] = 950
    first = sweep_once(100, [500], tree)
    assert all(write.new > write.old for write in first)
    assert 112 not in {write.pid for write in first}
    assert sweep_once(100, [500], tree) == []


def test_a_process_that_exited_and_a_missing_session_are_skipped() -> None:
    tree = _tree()
    del tree.names[112]
    writes = sweep_once(100, [], tree)
    assert 112 not in {write.pid for write in writes}
    assert sweep_once(None, [], tree) == []


def test_target_band_rules() -> None:
    assert target_band("xfwm4", _SESSION_BAND) is None
    assert target_band("xfce4-terminal", _SESSION_BAND) == bands.AGENT_SUBPROCESS
    assert target_band("xfce4-terminal", bands.AGENT_SUBPROCESS) is None
    assert target_band("chrome", 300) == bands.SHARED_BROWSER
    assert target_band("chrome", bands.SHARED_BROWSER_FLOOR) is None
    assert target_band("tilion", 0) == bands.SHARED_BROWSER_FLOOR


def test_the_sweeper_sweeps_on_its_interval_and_in_a_burst_after_a_kick() -> None:
    tree = _tree()
    clock = ManualClock()
    sweeper = BandSweeper(
        session_pid=lambda: 100,
        chromium_pids=lambda: [500],
        tree=tree,
        interval_seconds=30.0,
        burst_interval_seconds=0.01,
        burst_duration_seconds=5.0,
        clock=clock,
    )
    sweeper.start()
    try:
        assert wait_until(lambda: sweeper.sweep_count == 1)
        assert tree.adjustments[112] == bands.AGENT_SUBPROCESS
        # Idle on the long interval: no further sweep arrives on its own.
        assert not wait_until(lambda: sweeper.sweep_count >= 2, timeout_seconds=0.3)
        sweeper.kick()
        assert wait_until(lambda: sweeper.sweep_count >= 4)
    finally:
        sweeper.stop()
