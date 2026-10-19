"""Tests for the restart helpers of smoketest_app.py.

The script bounces a program through supervisord's RPC without waiting out its
``startsecs`` window and reads readiness from the port; these pin the state
decisions that make that safe: when a stop must precede the start, which
states mean the fresh start already died, and the timeout a restart gets.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).parent / "smoketest_app.py"
_spec = importlib.util.spec_from_file_location("smoketest_app", _SCRIPT)
assert _spec is not None and _spec.loader is not None
smoketest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(smoketest)


def test_stop_precedes_start_only_while_supervisord_counts_the_program_as_running() -> None:
    for state in ("RUNNING", "STARTING", "BACKOFF"):
        assert smoketest.needs_stop(state), state
    for state in ("STOPPED", "EXITED", "FATAL", "UNKNOWN"):
        assert not smoketest.needs_stop(state), state


def test_a_fresh_start_is_dead_in_every_state_but_starting_and_running() -> None:
    for state in ("BACKOFF", "FATAL", "EXITED", "STOPPED", "UNKNOWN"):
        assert smoketest.is_dead_after_start(state), state
    for state in ("STARTING", "RUNNING"):
        assert not smoketest.is_dead_after_start(state), state


def test_restart_raises_a_short_timeout_and_keeps_a_long_one() -> None:
    assert smoketest.effective_timeout(5.0, restart=False) == 5.0
    assert smoketest.effective_timeout(5.0, restart=True) == smoketest.RESTART_MIN_TIMEOUT
    assert smoketest.effective_timeout(60.0, restart=True) == 60.0
