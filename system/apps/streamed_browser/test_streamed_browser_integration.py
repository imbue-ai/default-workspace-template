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

from streamed_browser.videopipe import PIXELFLUX_IMPORT_ERROR, PixelfluxVideoPipe, is_available, parse_wire_header

_DISPLAY = ":93"


def _deps_present() -> bool:
    return (
        PIXELFLUX_IMPORT_ERROR is None
        and shutil.which("Xvfb") is not None
        and shutil.which("xsetroot") is not None
    )


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
        # Provoke frames and drain the initial burst: every row opens with a
        # keyframe (a fresh encoder emits SPS/PPS + IDR per stripe), each row
        # can send up to the credit limit, then the pipe must go quiet.
        _repaint(xvfb_display, "red")
        _repaint(xvfb_display, "blue")
        sent: list[bytes] = []
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            # Mimic the sender loop: cursor messages must be drained or the
            # pipe (correctly) yields to them instead of delivering stripes.
            pipe.take_cursor_message()
            packet = pipe.next_packet(timeout=1.5)
            if packet is None:
                break
            sent.append(packet)
        assert sent, "no stripes arrived from pixelflux within 15s"
        first_by_row: dict[int, bool] = {}
        for packet in sent:
            _, y_start, _, is_idr = parse_wire_header(packet)
            first_by_row.setdefault(y_start, is_idr)
        assert all(first_by_row.values()), f"a row opened without a keyframe: {first_by_row}"
        # Credit exhausted (no acks yet): further damage must not be delivered.
        _repaint(xvfb_display, "green")
        pipe.take_cursor_message()
        assert pipe.next_packet(timeout=1.5) is None, "pipe sent a stripe with no credit"

        # Acks open every row's window; the stream must resume on its own
        # (recovering via the IDR request path where deltas were dropped).
        for packet in sent:
            frame_id, y_start, _, _ = parse_wire_header(packet)
            pipe.ack(frame_id, y_start)
        _repaint(xvfb_display, "orange")
        resumed = None
        deadline = time.monotonic() + 10
        while resumed is None and time.monotonic() < deadline:
            pipe.take_cursor_message()
            resumed = pipe.next_packet(timeout=1.0)
        assert resumed is not None, "stream did not resume after acks"
        assert pipe.frames_captured >= len(first_by_row)
    finally:
        pipe.stop()
