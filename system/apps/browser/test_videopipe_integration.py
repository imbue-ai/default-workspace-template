"""Integration test: the pixelflux pipe against a real Xvfb display.

Exercises the actual engine (capture, damage-driven encode, wire format, IDR
request) and the credit window end to end, without Chromium: the display
content is driven with xsetroot color changes. Skipped where Xvfb or pixelflux
is unavailable (e.g. a bare CI runner without the browser display stack).
"""

import shutil
import subprocess
import threading
import time

import pytest

from browser.videopipe import PixelfluxVideoPipe, is_available, parse_wire_header

_DISPLAY = ":93"


def _deps_present() -> bool:
    return shutil.which("Xvfb") is not None and shutil.which("xsetroot") is not None


@pytest.fixture()
def xvfb_display(monkeypatch):
    if not _deps_present():
        pytest.skip("Xvfb/xsetroot/pixelflux unavailable in this environment")
    server = subprocess.Popen(
        ["Xvfb", _DISPLAY, "-screen", "0", "640x400x24"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    monkeypatch.setenv("DISPLAY", _DISPLAY)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        probe = subprocess.run(
            ["xdpyinfo", "-display", _DISPLAY], capture_output=True, timeout=5
        )
        if probe.returncode == 0:
            break
        threading.Event().wait(0.1)
    else:
        server.terminate()
        pytest.fail("Xvfb did not become ready")
    yield _DISPLAY
    server.terminate()
    server.wait(timeout=5)


def _repaint(display: str, color: str) -> None:
    subprocess.run(["xsetroot", "-display", display, "-solid", color], check=True, timeout=5)


@pytest.mark.timeout(90)
def test_pipe_streams_idr_first_and_respects_credit(xvfb_display) -> None:
    assert is_available()
    pipe = PixelfluxVideoPipe("itest", xvfb_display)
    pipe.start()
    try:
        # Provoke frames and pull the first packet: it must be a keyframe (a
        # fresh encoder opens with SPS/PPS + IDR), and it must parse.
        _repaint(xvfb_display, "red")
        first = None
        deadline = time.monotonic() + 10
        while first is None and time.monotonic() < deadline:
            first = pipe.next_packet(timeout=1.0)
        assert first is not None, "no frame arrived from pixelflux within 10s"
        first_id, _, first_is_idr = parse_wire_header(first)
        assert first_is_idr

        # Second frame sends on remaining credit; then the window is exhausted
        # (limit 2, no acks yet) and the pipe must withhold delivery.
        _repaint(xvfb_display, "blue")
        second = pipe.next_packet(timeout=5.0)
        assert second is not None
        second_id, _, _ = parse_wire_header(second)
        _repaint(xvfb_display, "green")
        assert pipe.next_packet(timeout=1.5) is None, "pipe sent a frame with no credit"

        # Acks open the window; the withheld (or a fresher) frame flows, and
        # because deltas may have been dropped meanwhile the pipe must recover
        # on its own via the IDR request path -- eventually delivering a frame.
        pipe.ack(first_id)
        pipe.ack(second_id)
        _repaint(xvfb_display, "orange")
        resumed = None
        deadline = time.monotonic() + 10
        while resumed is None and time.monotonic() < deadline:
            resumed = pipe.next_packet(timeout=1.0)
        assert resumed is not None, "stream did not resume after acks"
        assert pipe.frames_captured >= 3
    finally:
        pipe.stop()
