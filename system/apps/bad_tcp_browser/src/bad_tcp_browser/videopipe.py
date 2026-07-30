"""NAIVE pixelflux H.264 stripe pipe -- the deliberately-bad "TCP streaming" foil.

This is the counterpart to the optimized pipe in the browser app, stripped of
EVERY latency defense so the two can be demoed side by side:

  * NO credit-ack window    -- bytes in flight are unbounded; the sender never
                               waits for the viewer to catch up.
  * NO capture-rate control -- the encoder runs flat out at a fixed fps even when
                               the link plainly cannot carry it.
  * NO RTT-adaptive sizing, NO delay-gated quality servo.

It captures striped H.264 and shoves every stripe straight at the socket with a
blocking send. On a healthy LAN this looks fine. On a congested / high-latency
link there is nothing bounding the backlog, so the kernel + tunnel send buffers
fill with reliably-delivered-but-ever-later frames and interaction latency climbs
without limit -- exactly what per-stripe credit-ack (see the browser app's
videopipe) prevents. This module exists ONLY to demonstrate that contrast.
DO NOT add flow control here -- that is the whole point of it.

What is kept is only what makes the picture CORRECT (not fast): a per-row
newest-wins mailbox (a server-side OOM guard so a wedged viewer can't grow our
memory without bound -- it caps the *server's* backlog, never the wire's), and
sticky-IDR / request-a-keyframe-on-a-dropped-delta so a broken decode chain
resyncs instead of painting garbage. Neither bounds bytes in flight; the wire
backlog -- the latency you feel -- grows freely.

Wire format is pixelflux's own (unchanged)::

    byte 0     0x04 (H.264 magic)
    byte 1     frame type: 0x01 IDR, 0x02 non-IDR intra, 0x00 delta
    bytes 2-3  frame counter, u16 big-endian (wraps)
    bytes 4-5  stripe y-start, u16 big-endian
    bytes 6-9  stripe width / height, u16 big-endian each
    bytes 10+  Annex B payload
"""

import base64
import contextlib
import importlib
import os
import shutil
import subprocess
import threading
import time
from typing import Any

from loguru import logger

# pixelflux's native module dlopens system libraries (libva, pixman) that
# env-converge may still be installing at first boot, so the import lives in a
# retryable holder rather than crashing the service (see the browser app).
_pixelflux: dict[str, object] = {"module": None, "error": "not yet imported"}


def _attempt_pixelflux_import() -> None:
    if _pixelflux["module"] is not None:
        return
    try:
        _pixelflux["module"] = importlib.import_module("pixelflux")
    except ImportError as error:
        _pixelflux["error"] = str(error)
        return
    if _pixelflux["error"] != "not yet imported":
        logger.info("pixelflux import succeeded on retry (deferred native libraries arrived)")
    _pixelflux["error"] = None


_attempt_pixelflux_import()

WIRE_HEADER_LEN = 10
_WIRE_MAGIC_H264 = 0x04
FRAME_TYPE_IDR = 0x01

# Server-side floor between IDR requests (global: every row's encoder refreshes),
# so a resync storm can't make the encoders spend all their time on full frames.
# This is correctness pacing, not flow control -- keyframes only fire on a broken
# decode chain, never to slow delivery down.
_IDR_REQUEST_MIN_INTERVAL = 0.4

# Fixed capture rate -- flat out, never adapts (that is the point). 60fps: the
# capture loop is a fixed tick, so every change waits half a tick on average.
_CAPTURE_FPS = float(os.environ.get("BAD_TCP_VIDEO_FPS", "60"))
# Fixed quality: no delay-gated softening. Motion CRF + a crisp paint-over pass on
# settle, same as the base encoder -- what is missing is the *adaptation*.
_VIDEO_CRF = int(os.environ.get("BAD_TCP_VIDEO_CRF", "28"))
_PAINTOVER_CRF = int(os.environ.get("BAD_TCP_VIDEO_PAINTOVER_CRF", "18"))
_PAINTOVER_TRIGGER_FRAMES = 30

# Initial capture size (matches the session's cold-start window); the viewer sends
# its real pane size on connect, so the capture re-targets within ~150ms.
_INIT_CAPTURE_W = int(os.environ.get("BAD_TCP_BROWSER_WIDTH", "1280"))
_INIT_CAPTURE_H = int(os.environ.get("BAD_TCP_BROWSER_HEIGHT", "800"))


class VideoPipeError(RuntimeError):
    pass


def is_available() -> bool:
    """Pixelflux's native module loaded (the capture display arrives per-pipe)."""
    return _pixelflux["module"] is not None


def display_geometry(display: str) -> tuple[int, int]:
    """The X display's root geometry, from xdpyinfo (present wherever Xvfb is)."""
    if shutil.which("xdpyinfo") is None:
        raise VideoPipeError("xdpyinfo is not installed; cannot size the capture")
    result = subprocess.run(
        ["xdpyinfo", "-display", display], capture_output=True, text=True, timeout=10
    )
    if result.returncode != 0:
        raise VideoPipeError(f"xdpyinfo failed for {display}: {result.stderr.strip()[:200]}")
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("dimensions:"):
            width, height = line.split()[1].split("x")
            return int(width), int(height)
    raise VideoPipeError(f"xdpyinfo output had no dimensions line for {display}")


def parse_wire_header(packet: bytes) -> tuple[int, int, int, bool]:
    """(frame_id, y_start, frame_type, is_idr) from a pixelflux wire packet.

    Raises on anything that is not an H.264 packet -- a format drift between
    pixelflux versions should explode here, not paint garbage.
    """
    if len(packet) < WIRE_HEADER_LEN:
        raise VideoPipeError(f"video packet shorter than wire header: {len(packet)} bytes")
    if packet[0] != _WIRE_MAGIC_H264:
        raise VideoPipeError(f"unexpected video packet magic 0x{packet[0]:02x}")
    frame_type = packet[1]
    frame_id = (packet[2] << 8) | packet[3]
    y_start = (packet[4] << 8) | packet[5]
    return frame_id, y_start, frame_type, frame_type == FRAME_TYPE_IDR


class _StripeRow:
    """One row's mailbox (newest unsent stripe wins). ``needs_keyframe`` is set when
    a delta is dropped (the decode chain is broken) so the row waits for an IDR --
    a CORRECTNESS latch, not a credit window. There is no in-flight accounting."""

    def __init__(self) -> None:
        self.mailbox: bytes | None = None
        self.mailbox_is_idr = False
        self.needs_keyframe = False


class PixelfluxVideoPipe:
    """One viewer's capture->encode->send pipeline on the session's X display.

    Encoder callbacks land stripes in per-row mailboxes; the sender drains ANY
    mailboxed stripe the instant it exists -- there is no credit gate, no rate
    control, no ack. That is deliberate: this is the naive foil.
    """

    def __init__(self, browser_id: str, display: str) -> None:
        self.browser_id = browser_id
        self.display = display
        self._capture = None
        self._settings = None
        self._condition = threading.Condition()
        self._rows: dict[int, _StripeRow] = {}
        self._cursor_message: str | None = None
        self._control_message: str | None = None
        self._cap_w = 0
        self._cap_h = 0
        self._expected_w = _INIT_CAPTURE_W
        self._closed = False
        self._last_idr_request = 0.0
        self.frames_captured = 0
        self.frames_dropped = 0

    def start(self) -> None:
        _attempt_pixelflux_import()
        if _pixelflux["module"] is None:
            raise VideoPipeError(
                f"pixelflux failed to import (missing system libraries? see setup_system.sh): {_pixelflux['error']}"
            )
        pixelflux_module: Any = _pixelflux["module"]
        # pixelflux targets whatever $DISPLAY names (CaptureSettings has no display
        # field); this service owns one session, so pointing the process env here is
        # safe.
        os.environ["DISPLAY"] = self.display
        self._cap_w, self._cap_h = display_geometry(self.display)
        width = min(_INIT_CAPTURE_W, self._cap_w)
        height = min(_INIT_CAPTURE_H, self._cap_h)
        self._expected_w = width
        settings = pixelflux_module.CaptureSettings()
        settings.capture_width = width
        settings.capture_height = height
        settings.target_fps = _CAPTURE_FPS  # fixed; never retuned
        settings.output_mode = 1  # H.264
        settings.use_cpu = True
        settings.video_crf = _VIDEO_CRF
        settings.use_paint_over_quality = True
        settings.video_paintover_crf = _PAINTOVER_CRF
        settings.paint_over_trigger_frames = _PAINTOVER_TRIGGER_FRAMES
        capture = pixelflux_module.ScreenCapture()
        capture.start_capture(self._on_frame, settings)
        self._settings = settings
        capture.set_cursor_callback(self._on_cursor)
        self._capture = capture
        logger.info(
            "bad-tcp video pipe started for {} on {} ({}x{} @ {}fps, NO flow control)",
            self.browser_id, self.display, width, height, _CAPTURE_FPS,
        )

    def _on_frame(self, frame) -> None:  # noqa: ANN001  (pixelflux native frame object)
        # Encoder thread: copy out (the native buffer is reused) and mailbox it.
        packet = bytes(frame)
        try:
            _, y_start, _, is_idr = parse_wire_header(packet)
        except VideoPipeError as error:  # noqa: F841  (handled just below)
            logger.warning("bad-tcp video pipe {} dropped malformed packet ({})", self.browser_id, error)
            return
        with self._condition:
            self.frames_captured += 1
            stripe_w = (packet[6] << 8) | packet[7]
            if stripe_w != self._expected_w:
                return  # a stripe encoded at the pre-resize width; the client resets on `res,`
            row = self._rows.setdefault(y_start, _StripeRow())
            if row.mailbox is not None and row.mailbox_is_idr and not is_idr:
                # Sticky IDR: never let a delta overwrite an unsent keyframe (the row's
                # recovery). Correctness, not flow control.
                self.frames_dropped += 1
                return
            if row.mailbox is not None:
                # Overwriting an unsent stripe: if either side was a delta the decode
                # chain is broken until a keyframe.
                self.frames_dropped += 1
                if not row.mailbox_is_idr or not is_idr:
                    row.needs_keyframe = True
            row.mailbox = packet
            row.mailbox_is_idr = is_idr
            self._condition.notify()

    def _on_cursor(self, _msg_type, png_bytes, hot_x, hot_y) -> None:  # noqa: ANN001  (pixelflux callback shape)
        encoded = base64.b64encode(bytes(png_bytes)).decode("ascii")
        with self._condition:
            self._cursor_message = f"cursor,{int(hot_x)},{int(hot_y)},{encoded}"
            self._condition.notify()

    def take_cursor_message(self) -> str | None:
        """The latest undelivered cursor text frame, if any (newest wins)."""
        with self._condition:
            message, self._cursor_message = self._cursor_message, None
            return message

    @property
    def condition(self) -> threading.Condition:
        """The sender's wakeup condition; the audio pipe notifies it too."""
        return self._condition

    def take_control_message(self) -> str | None:
        """The latest undelivered control frame (e.g. `res,w,h`), if any."""
        with self._condition:
            message, self._control_message = self._control_message, None
            return message

    def set_capture_region(self, width: int, height: int) -> tuple[int, int]:
        """Re-target the capture to width x height, clamped to the framebuffer and to
        even dimensions. Resets the row map and forces a keyframe; tells the viewer
        the realized size via a `res,` control frame."""
        width = max(2, min(width - (width % 2), self._cap_w))
        height = max(2, min(height - (height % 2), self._cap_h))
        with self._condition:
            if self._capture is None:
                return width, height
            self._capture.update_capture_region(0, 0, width, height)
            self._capture.request_idr_frame()
            self._expected_w = width
            self._rows.clear()
            self._control_message = f"res,{width},{height}"
            self._condition.notify()
        return width, height

    def next_packet(self, timeout: float, has_extra=None) -> bytes | None:  # noqa: ANN001
        """Return the next mailboxed stripe the INSTANT one exists -- no credit gate,
        no rate control. The ONLY skip is a broken-chain delta (waits for a keyframe),
        which is correctness, not backpressure. Blocks up to ``timeout`` when idle."""
        deadline = time.monotonic() + timeout
        with self._condition:
            while not self._closed:
                if (
                    self._cursor_message is not None
                    or self._control_message is not None
                    or (has_extra is not None and has_extra())
                ):
                    return None  # let the sender drain a pending cursor/control/audio frame now
                for y_start in sorted(self._rows):
                    row = self._rows[y_start]
                    if row.mailbox is None:
                        continue
                    if row.needs_keyframe and not row.mailbox_is_idr:
                        # Decode chain is broken; drop the delta and ask for a keyframe.
                        self.frames_dropped += 1
                        row.mailbox = None
                        self._request_idr_locked()
                        continue
                    packet, row.mailbox = row.mailbox, None
                    if row.mailbox_is_idr:
                        row.needs_keyframe = False
                    return packet  # ship it -- no ack, no window, no rate check
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(timeout=remaining)
            return None

    def _request_idr_locked(self) -> None:
        now = time.monotonic()
        if now - self._last_idr_request < _IDR_REQUEST_MIN_INTERVAL:
            return
        self._last_idr_request = now
        if self._capture is not None:
            self._capture.request_idr_frame()

    def stop(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        capture, self._capture = self._capture, None
        if capture is None:
            return
        # stop_capture joins native encoder threads; guard against it wedging the
        # service if a callback is stuck.
        stopper = threading.Thread(target=lambda: self._stop_capture(capture), daemon=True)
        stopper.start()
        stopper.join(timeout=5)
        if stopper.is_alive():
            logger.warning("bad-tcp video pipe {} capture did not stop within 5s; abandoning it", self.browser_id)
        else:
            logger.info(
                "bad-tcp video pipe stopped for {} (captured {}, dropped {})",
                self.browser_id, self.frames_captured, self.frames_dropped,
            )

    @staticmethod
    def _stop_capture(capture) -> None:  # noqa: ANN001
        with contextlib.suppress(Exception):
            capture.stop_capture()
