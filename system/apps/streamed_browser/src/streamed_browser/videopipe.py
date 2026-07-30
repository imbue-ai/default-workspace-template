"""Pixelflux H.264 stripe pipe with per-row credit-ack flow control.

Pixelflux (linuxserver's Rust capture/encode engine, the one behind Selkies)
watches the session's private X display and encodes damage-driven H.264 on the
CPU -- in STRIPE mode: the frame splits into min(cores, height/64) horizontal
stripes, each with its own change detection and its own encoder, so only
changed rows are encoded (in parallel) and an idle screen costs nothing. Each
stripe row is an independent H.264 stream the viewer decodes with its own
VideoDecoder and composites at its y-offset (the Selkies client's design).

Flow control is per row and it is NOT socket backpressure: the delivery chain
crosses several hops (gVisor netstack, sshd -- whose per-channel window alone
is 2 MB -- a tunnel, a local forwarder) whose buffers swallow writes long
after the path is congested. The viewer acks every stripe as it leaves its
decoder (``ack,<frame_id>,<y_start>``) and each row never has more than
``_CREDIT_LIMIT`` unacknowledged stripes outstanding, so bytes in flight are
bounded everywhere no matter where congestion lives; a degraded path yields
fewer, fresher stripes instead of an ever-older backlog. Undelivered stripes
are replaced newest-wins per row (staleness is bounded at ~1 frame); dropping
a delta breaks that row's decode chain, so the row discards deltas until the
(rate limited, global) IDR request produces a keyframe.

The cursor never touches the framebuffer: pixelflux delivers shape changes
out-of-band (XFixes) as ``(type, png, hot_x, hot_y)`` and the pipe queues them
for the sender as ``cursor,<hot_x>,<hot_y>,<png base64>`` text frames; the
viewer applies them as its CSS cursor at the LOCAL pointer position. Pointer
motion therefore costs zero encode.

Wire format: pixelflux's own stripe header, verified against
``pixelflux/src/encoders/software.rs`` (encode_with_headers)::

    byte 0     0x04 (H.264 magic)
    byte 1     frame type: 0x01 IDR, 0x02 non-IDR intra, 0x00 delta
    bytes 2-3  frame counter, u16 big-endian (wraps)
    bytes 4-5  stripe y-start, u16 big-endian
    bytes 6-9  stripe width / height, u16 big-endian each
    bytes 10+  Annex B payload

Headers pass through to the viewer untouched; (frame id, y-start) is the ack
token.
"""

import base64
import contextlib
import os
import shutil
import subprocess
import threading
import time

from loguru import logger

# pixelflux is a hard dependency of this package, but its native module dlopens
# system libraries (libva and friends) at import. On a host missing them the
# import raises, and an unguarded module-level import would take down the whole
# service (crash-looping every route, not just this pipe) -- which is exactly
# what happened on the first workspace deploy. Guarded so the service always
# boots; start() reports the real reason the pipe is unavailable.
try:
    from pixelflux import CaptureSettings, ScreenCapture
except ImportError as _pixelflux_import_error:
    CaptureSettings = None
    ScreenCapture = None
    PIXELFLUX_IMPORT_ERROR: str | None = str(_pixelflux_import_error)
else:
    PIXELFLUX_IMPORT_ERROR = None

WIRE_HEADER_LEN = 10
_WIRE_MAGIC_H264 = 0x04
FRAME_TYPE_IDR = 0x01

# Per-row unacknowledged-stripe ceiling. Two keeps a stripe in flight while the
# previous one's ack returns, so throughput is not halved by the round trip;
# anything higher just rebuilds queueing in the tunnel.
_CREDIT_LIMIT = 2

# Server-side floor between IDR requests (the request is global: every row's
# encoder refreshes), so a struggling viewer cannot make the encoders spend
# all their time on full refreshes -- keyframes are the most expensive frames
# to encode AND the largest on the wire.
_IDR_REQUEST_MIN_INTERVAL = 0.4

# 60, not 30: the capture loop is a fixed tick nothing wakes early, so every
# screen change waits half a tick on average before being seen -- ~8ms at 60
# vs ~17ms at 30. Encode stays damage-driven, so an idle screen costs the same.
_CAPTURE_FPS = float(os.environ.get("BROWSER_VIDEO_FPS", "60"))
# Deliberately soft during motion (cheap to encode, cheap to ship); the
# paint-over pass re-encodes the settled screen at the crisp CRF, so text is
# sharp whenever the user could actually read it.
_VIDEO_CRF = int(os.environ.get("BROWSER_VIDEO_CRF", "28"))
_PAINTOVER_CRF = int(os.environ.get("BROWSER_VIDEO_PAINTOVER_CRF", "18"))
# Default 15 damaged frames before paint-over -- seconds of soft text after
# every scroll at damage-driven rates. 5 sharpens almost immediately.
_PAINTOVER_TRIGGER_FRAMES = 5


class VideoPipeError(RuntimeError):
    pass


# Closed-loop capture rate (the Salsify idea: the encoder should not outrun the
# transport). The credit window caps DELIVERY at the path's real rate, but the
# encoder ticks open-loop -- measured on a live workspace it encoded 60/s while
# ~10/s were deliverable, burning a full core on stripes the mailbox then
# discarded. The controller is AIMD on the WASTE signal: mailbox drops mean
# the encoder outran delivery (multiplicative decrease), a drop-free interval
# means it kept up (additive increase toward the ceiling). Keying on drops --
# not on a measured delivery rate -- avoids the ratchet-down trap where bursty
# damage under-fills a rate window and locks the tick at the floor.
_RATE_MIN_FPS = 15.0
_RATE_MAX_FPS = float(os.environ.get("BROWSER_VIDEO_FPS", "60"))
_RATE_DECREASE_FACTOR = 0.6
_RATE_INCREASE_FPS = 10.0


def target_capture_fps(current_fps: float, dropped_in_interval: int) -> float:
    """Next capture rate from this interval's mailbox-drop count. Pure, for tests."""
    if dropped_in_interval > 0:
        return max(_RATE_MIN_FPS, current_fps * _RATE_DECREASE_FACTOR)
    return min(_RATE_MAX_FPS, current_fps + _RATE_INCREASE_FPS)


def is_available() -> bool:
    """Pixelflux's native module loaded (the capture display arrives per-pipe)."""
    return PIXELFLUX_IMPORT_ERROR is None


def display_geometry(display: str) -> tuple[int, int]:
    """The X display's root geometry, from xdpyinfo (present wherever Xvfb is)."""
    if shutil.which("xdpyinfo") is None:
        raise VideoPipeError("xdpyinfo is not installed; cannot size the capture")
    result = subprocess.run(
        ["xdpyinfo", "-display", display],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise VideoPipeError(f"xdpyinfo failed for {display}: {result.stderr.strip()[:200]}")
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("dimensions:"):
            size = line.split()[1]
            width, height = size.split("x")
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


class CreditWindow:
    """Pure bookkeeping for one stripe row's ack-credit flow control.

    Tracks how many sent stripes the viewer has not yet acknowledged and
    whether the row's decode chain is broken (a delta was dropped) so only a
    keyframe may resume it. Thread-safety is the caller's.
    """

    def __init__(self, limit: int = _CREDIT_LIMIT) -> None:
        self._limit = limit
        self._unacked: list[int] = []
        self.needs_keyframe = False

    @property
    def has_credit(self) -> bool:
        return len(self._unacked) < self._limit

    def note_sent(self, frame_id: int) -> None:
        self._unacked.append(frame_id)

    def note_dropped_delta(self) -> None:
        self.needs_keyframe = True

    def note_sent_keyframe(self, frame_id: int) -> None:
        self.needs_keyframe = False
        self._unacked.append(frame_id)

    def ack(self, frame_id: int) -> None:
        """Acknowledge frame_id and everything sent before it (cumulative, so a
        lost ack message is harmless; ids wrap at u16, so membership decides)."""
        if frame_id in self._unacked:
            cutoff = self._unacked.index(frame_id)
            del self._unacked[: cutoff + 1]

    def admits(self, is_keyframe: bool) -> bool:
        if not self.has_credit:
            return False
        return is_keyframe or not self.needs_keyframe


class _StripeRow:
    """One row's mailbox (newest unsent stripe wins) plus its credit window."""

    def __init__(self) -> None:
        self.mailbox: bytes | None = None
        self.mailbox_is_idr = False
        self.window = CreditWindow()


class PixelfluxVideoPipe:
    """One viewer's capture->encode->send pipeline on the session's X display.

    Owns a pixelflux ScreenCapture for the lifetime of one WebSocket
    connection. Encoder callbacks land stripes in per-row mailboxes; the
    connection's sender thread drains whichever rows the credit windows admit.
    """

    def __init__(self, browser_id: str, display: str) -> None:
        self.browser_id = browser_id
        self.display = display
        self._capture = None
        self._condition = threading.Condition()
        self._rows: dict[int, _StripeRow] = {}
        self._cursor_message: str | None = None
        self._closed = False
        self._last_idr_request = 0.0
        self._current_fps = _RATE_MAX_FPS
        self._last_retune = 0.0
        self._dropped_at_last_retune = 0
        self.frames_captured = 0
        self.frames_dropped = 0

    def start(self) -> None:
        if PIXELFLUX_IMPORT_ERROR is not None:
            raise VideoPipeError(
                f"pixelflux failed to import (missing system libraries? see setup_system.sh): {PIXELFLUX_IMPORT_ERROR}"
            )
        # pixelflux targets whatever $DISPLAY names -- CaptureSettings has no
        # display field -- so point the process at this pipe's display. Safe
        # process-globally: the service owns one session, and every pipe
        # captures that session's display.
        os.environ["DISPLAY"] = self.display
        width, height = display_geometry(self.display)
        settings = CaptureSettings()
        settings.capture_width = width
        settings.capture_height = height
        settings.target_fps = _CAPTURE_FPS
        settings.output_mode = 1  # H.264
        settings.use_cpu = True  # no GPU in these workspaces; fail loud, not slow
        # STRIPE mode (no video_fullframe): min(cores, height/64) rows, each
        # with its own change detection and encoder -- only changed rows
        # encode, in parallel.
        settings.video_crf = _VIDEO_CRF
        settings.use_paint_over_quality = True
        settings.video_paintover_crf = _PAINTOVER_CRF
        settings.paint_over_trigger_frames = _PAINTOVER_TRIGGER_FRAMES
        capture = ScreenCapture()
        capture.start_capture(self._on_frame, settings)
        # Cursor shape changes arrive out-of-band (XFixes) so pointer motion
        # never dirties the framebuffer; registered after start so a current
        # cursor is replayed to a mid-run registration (pixelflux's REPLAY).
        capture.set_cursor_callback(self._on_cursor)
        self._capture = capture
        logger.info(
            "video pipe started for {} on {} ({}x{} @ {}fps, crf {}/{} paint-over)",
            self.browser_id, self.display, width, height, _CAPTURE_FPS, _VIDEO_CRF, _PAINTOVER_CRF,
        )

    def _on_frame(self, frame) -> None:  # noqa: ANN001  (pixelflux native frame object)
        # Encoder thread: copy out (the native buffer is reused) and mailbox it.
        packet = bytes(frame)
        try:
            _, y_start, _, is_idr = parse_wire_header(packet)
        except VideoPipeError as error:
            logger.warning("video pipe {} dropped malformed packet ({})", self.browser_id, error)
            return
        with self._condition:
            self.frames_captured += 1
            row = self._rows.setdefault(y_start, _StripeRow())
            if row.mailbox is not None:
                # Replacing this row's unsent stripe; if either side of the
                # swap was a delta the row's chain is broken until a keyframe.
                self.frames_dropped += 1
                if not row.mailbox_is_idr or not is_idr:
                    row.window.note_dropped_delta()
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

    def next_packet(self, timeout: float) -> bytes | None:
        """Block until some row's stripe is admitted by its window (or timeout).

        Rows are scanned in y order -- with a handful of rows and per-row
        windows there is no meaningful starvation to arbitrate. Stripes a
        window refuses stay mailboxed (a newer one may overwrite them);
        deltas refused for want of a keyframe are discarded and trigger the
        (global, rate-limited) IDR request.
        """
        deadline = time.monotonic() + timeout
        with self._condition:
            while not self._closed:
                for y_start in sorted(self._rows):
                    row = self._rows[y_start]
                    if row.mailbox is None:
                        continue
                    if row.window.admits(row.mailbox_is_idr):
                        packet = row.mailbox
                        row.mailbox = None
                        frame_id, _, _, is_idr = parse_wire_header(packet)
                        if is_idr:
                            row.window.note_sent_keyframe(frame_id)
                        else:
                            row.window.note_sent(frame_id)
                        return packet
                    if row.window.needs_keyframe and not row.mailbox_is_idr:
                        self.frames_dropped += 1
                        row.mailbox = None
                        self._request_idr_locked()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                # Idle snap-back: retuning otherwise only happens on acks, so a
                # quiet stream would stay at a low tick and the next interaction
                # would pay its latency. Idle capture at the ceiling costs
                # nothing (damage-driven).
                now = time.monotonic()
                if (
                    self._current_fps < _RATE_MAX_FPS
                    and now - self._last_retune > 2.0
                    and self._capture is not None
                ):
                    self._current_fps = _RATE_MAX_FPS
                    self._dropped_at_last_retune = self.frames_dropped
                    self._capture.update_framerate(_RATE_MAX_FPS)
                self._condition.wait(timeout=remaining)
            return None

    def ack(self, frame_id: int, y_start: int) -> None:
        """Viewer acknowledged a decoded stripe; that row's credit opens."""
        with self._condition:
            row = self._rows.get(y_start)
            if row is None:
                return
            row.window.ack(frame_id)
            if row.window.needs_keyframe:
                self._request_idr_locked()
            self._retune_capture_rate_locked()
            self._condition.notify()

    def _retune_capture_rate_locked(self) -> None:
        """AIMD on the interval's mailbox drops, at most once a second."""
        now = time.monotonic()
        if now - self._last_retune < 1.0:
            return
        self._last_retune = now
        dropped = self.frames_dropped - self._dropped_at_last_retune
        self._dropped_at_last_retune = self.frames_dropped
        wanted = target_capture_fps(self._current_fps, dropped)
        if wanted != self._current_fps and self._capture is not None:
            self._current_fps = wanted
            self._capture.update_framerate(wanted)
            logger.debug("video pipe {} capture rate retuned to {:.0f}fps ({} drops)", self.browser_id, wanted, dropped)

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
        # stop_capture joins native encoder threads; guard against it wedging
        # the service if a callback is stuck (observed flakiness in testing).
        stopper = threading.Thread(target=lambda: self._stop_capture(capture), daemon=True)
        stopper.start()
        stopper.join(timeout=5)
        if stopper.is_alive():
            logger.warning("video pipe {} capture did not stop within 5s; abandoning it", self.browser_id)
        else:
            logger.info(
                "video pipe stopped for {} (captured {}, dropped {})",
                self.browser_id, self.frames_captured, self.frames_dropped,
            )

    @staticmethod
    def _stop_capture(capture) -> None:  # noqa: ANN001
        with contextlib.suppress(Exception):
            capture.stop_capture()
