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
import importlib
import os
import shutil
import subprocess
import threading
import time
from typing import Any

from loguru import logger

# pixelflux is a hard dependency of this package, but its native module dlopens
# system libraries (libva, pixman) that env-converge may still be installing
# when the service first boots. An unguarded module-level import would take
# down the whole service (crash-looping every route) on a host where they are
# missing -- which is exactly what happened on the first workspace deploy --
# and caching that failure forever stranded a fresh workspace's pane until a
# manual restart. So the import lives in a retryable holder: attempted at
# module load, re-attempted on every pipe start while it remains broken.
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

# Per-row unacknowledged-stripe window. The FLOOR (2) keeps a stripe in flight
# while the previous one's ack returns; the live limit adapts upward with the
# measured ack round trip (DCV ships frames-in-transit 2..8 for the same
# reason): a high-RTT viewer needs more in flight to sustain frame rate, and
# sizing by measured RTT adds only what the pipe itself occupies -- never a
# standing queue.
_CREDIT_LIMIT = 2
_CREDIT_LIMIT_MAX = 8

# Delay-gated quality servo (SQP-shaped): the smoothed ack RTT is compared to
# the observed minimum; inflation beyond the budget means our own bytes are
# queueing somewhere, so motion CRF steps softer (cheaper, smaller) until the
# delay drains, then recovers. Paint-over crispness on settle is untouched.
_RTT_EWMA_ALPHA = 0.2
_QUALITY_DELAY_BUDGET_S = 0.10
_QUALITY_RECOVER_DELAY_S = 0.04
_CRF_SOFT_STEP = 4
_CRF_RECOVER_STEP = 2
_CRF_MAX = 38

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
# Trigger counts DAMAGED FRAMES at the capture tick, not wall time: 5 frames
# at a 60fps tick is 83ms -- a mid-scroll micro-pause -- and each firing costs
# a ~200KB crisp-IDR burst that blocks live frames behind ~1s of wire
# (measured; this was a dominant freeze mechanism). 30 frames ~= 0.5s of real
# stillness at full tick.
_PAINTOVER_TRIGGER_FRAMES = 30


class VideoPipeError(RuntimeError):
    pass


# Closed-loop capture rate (the Salsify idea: the encoder should not outrun the
# transport). The credit window caps DELIVERY at the path's real rate, but the
# encoder ticks open-loop -- measured on a live workspace it encoded 60/s while
# ~10/s were deliverable, burning a full core on stripes the mailbox then
# discarded. The controller keys on the WASTE signal: mailbox drops mean the
# encoder outran delivery, so the rate steps down toward the interval's
# MEASURED delivered rate (converging in one step); drop-free intervals climb
# gently toward the ceiling. Gating the decrease on drops -- never on a low
# delivery rate alone -- avoids the ratchet-down trap where bursty damage
# under-fills a rate window and locks the tick at the floor.
_RATE_MIN_FPS = 8.0
_RATE_MAX_FPS = float(os.environ.get("BROWSER_VIDEO_FPS", "60"))
_RATE_DECREASE_FACTOR = 0.6
_RATE_INCREASE_FPS = 3.0


def target_capture_fps(current_fps: float, dropped_in_interval: int, delivered_fps: float) -> float:
    """Next capture rate from this interval's drop count and delivered rate.

    On drops, step toward what the path demonstrably delivered (plus slack)
    rather than a blind multiplicative cut -- converges in one step instead of
    sawtoothing; the multiplicative cut remains as the fallback when nothing
    was delivered at all. Drop-free intervals climb gently (a +10 ramp against
    a ~20-stripe/s ceiling guaranteed an overshoot drop-burst every ~2s,
    log-verified). Pure, for tests.
    """
    if dropped_in_interval > 0:
        if delivered_fps > 0:
            return max(_RATE_MIN_FPS, min(current_fps, delivered_fps * 1.2))
        return max(_RATE_MIN_FPS, current_fps * _RATE_DECREASE_FACTOR)
    return min(_RATE_MAX_FPS, current_fps + _RATE_INCREASE_FPS)


def is_available() -> bool:
    """Pixelflux's native module loaded (the capture display arrives per-pipe)."""
    return _pixelflux["module"] is not None


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
        self.limit = limit
        self._unacked: list[int] = []
        self._blocking_idr: int | None = None
        self.needs_keyframe = False

    @property
    def has_credit(self) -> bool:
        # An unacked IDR consumes the WHOLE window: stripes vary ~25x in size
        # (4KB delta vs 110KB IDR), so counting them equally let ~220KB into
        # flight at recovery time -- exactly when the path is weakest. No new
        # stripe ships until the IDR lands.
        if self._blocking_idr is not None:
            return False
        return len(self._unacked) < self.limit

    def note_sent(self, frame_id: int) -> None:
        self._unacked.append(frame_id)

    def note_dropped_delta(self) -> None:
        self.needs_keyframe = True

    def note_sent_keyframe(self, frame_id: int) -> None:
        self.needs_keyframe = False
        self._blocking_idr = frame_id
        self._unacked.append(frame_id)

    def ack(self, frame_id: int) -> None:
        """Acknowledge frame_id and everything sent before it (cumulative, so a
        lost ack message is harmless; ids wrap at u16, so membership decides)."""
        if frame_id in self._unacked:
            cutoff = self._unacked.index(frame_id)
            acked = self._unacked[: cutoff + 1]
            del self._unacked[: cutoff + 1]
            if self._blocking_idr in acked:
                self._blocking_idr = None

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
        self.sent_at: dict[int, float] = {}  # frame_id -> send time, for ack RTT


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
        self._settings = None
        self._condition = threading.Condition()
        self._rows: dict[int, _StripeRow] = {}
        self._cursor_message: str | None = None
        self._closed = False
        self._last_idr_request = 0.0
        self._current_fps = _RATE_MAX_FPS
        self._last_retune = 0.0
        self._dropped_at_last_retune = 0
        self._acks_since_retune = 0
        self._rtt_ewma: float | None = None
        self._rtt_min: float | None = None
        self._current_crf = _VIDEO_CRF
        self.frames_captured = 0
        self.frames_dropped = 0

    def start(self) -> None:
        _attempt_pixelflux_import()
        if _pixelflux["module"] is None:
            raise VideoPipeError(
                f"pixelflux failed to import (missing system libraries? see setup_system.sh): {_pixelflux['error']}"
            )
        pixelflux_module: Any = _pixelflux["module"]
        # pixelflux targets whatever $DISPLAY names -- CaptureSettings has no
        # display field -- so point the process at this pipe's display. Safe
        # process-globally: the service owns one session, and every pipe
        # captures that session's display.
        os.environ["DISPLAY"] = self.display
        width, height = display_geometry(self.display)
        settings = pixelflux_module.CaptureSettings()
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
        capture = pixelflux_module.ScreenCapture()
        capture.start_capture(self._on_frame, settings)
        self._settings = settings
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
            if row.mailbox is not None and row.mailbox_is_idr and not is_idr:
                # STICKY IDR: an unsent keyframe is the row's recovery -- letting
                # a delta overwrite it re-broke the chain and forced another
                # >=0.4s IDR request cycle, looping into multi-second freezes
                # (probe-verified as the dominant stall mechanism). Drop the
                # delta instead; the pending IDR supersedes it visually anyway.
                self.frames_dropped += 1
                return
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
                if self._cursor_message is not None:
                    # A pending cursor shape must not wait out the poll timeout
                    # (up to 1s of hover-feedback lag on a static screen):
                    # return so the sender's cursor check runs now.
                    return None
                for y_start in sorted(self._rows):
                    row = self._rows[y_start]
                    if row.mailbox is None:
                        continue
                    if row.window.admits(row.mailbox_is_idr):
                        packet = row.mailbox
                        row.mailbox = None
                        frame_id, _, _, is_idr = parse_wire_header(packet)
                        row.sent_at[frame_id] = time.monotonic()
                        if len(row.sent_at) > 32:
                            row.sent_at.pop(next(iter(row.sent_at)))
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
            sent = row.sent_at.pop(frame_id, None)
            if sent is not None:
                rtt = time.monotonic() - sent
                self._rtt_ewma = rtt if self._rtt_ewma is None else (1 - _RTT_EWMA_ALPHA) * self._rtt_ewma + _RTT_EWMA_ALPHA * rtt
                self._rtt_min = rtt if self._rtt_min is None else min(self._rtt_min, rtt)
            row.window.ack(frame_id)
            if row.window.needs_keyframe:
                self._request_idr_locked()
            self._retune_capture_rate_locked()
            self._condition.notify()

    def _retune_capture_rate_locked(self) -> None:
        """AIMD on the interval's mailbox drops, at most once a second."""
        self._acks_since_retune += 1
        now = time.monotonic()
        interval = now - self._last_retune
        if interval < 1.0:
            return
        self._last_retune = now
        dropped = self.frames_dropped - self._dropped_at_last_retune
        self._dropped_at_last_retune = self.frames_dropped
        delivered_fps = self._acks_since_retune / interval / max(1, len(self._rows))
        self._acks_since_retune = 0
        wanted = target_capture_fps(self._current_fps, dropped, delivered_fps)
        if wanted != self._current_fps and self._capture is not None:
            self._current_fps = wanted
            self._capture.update_framerate(wanted)
            logger.debug("video pipe {} capture rate retuned to {:.0f}fps ({} drops)", self.browser_id, wanted, dropped)
        self._adapt_window_and_quality_locked()

    def _adapt_window_and_quality_locked(self) -> None:
        """RTT-driven adaptation, piggybacked on the once-a-second retune.

        Window: enough stripes in flight to cover the measured ack RTT at the
        current frame rate (the pipe's own occupancy), clamped 2..8 -- DCV's
        frames-in-transit range. Quality: SQP-shaped delay gate -- smoothed RTT
        inflated past the observed minimum plus budget means our bytes are the
        queue, so motion CRF softens until delay drains, then recovers.
        """
        if self._rtt_ewma is None or self._rtt_min is None:
            return
        # Size the window against a FIXED reference rate, not the live AIMD
        # rate: a high-RTT viewer lowers delivered fps, and a window sized
        # from that shrinks, lowering fps further -- a measured death spiral
        # to the floor. 30fps is the experience the window should be able to
        # carry when the encoder has content for it.
        wanted_limit = max(_CREDIT_LIMIT, min(_CREDIT_LIMIT_MAX, int(30.0 * self._rtt_ewma) + 1))
        for row in self._rows.values():
            row.window.limit = wanted_limit
        inflation = self._rtt_ewma - self._rtt_min
        wanted_crf = self._current_crf
        if inflation > _QUALITY_DELAY_BUDGET_S:
            wanted_crf = min(_CRF_MAX, self._current_crf + _CRF_SOFT_STEP)
        elif inflation < _QUALITY_RECOVER_DELAY_S and self._current_crf > _VIDEO_CRF:
            wanted_crf = max(_VIDEO_CRF, self._current_crf - _CRF_RECOVER_STEP)
        if wanted_crf != self._current_crf and self._capture is not None and self._settings is not None:
            self._current_crf = wanted_crf
            self._settings.video_crf = wanted_crf
            self._capture.update_tunables(self._settings)
            logger.debug(
                "video pipe {} crf retuned to {} (rtt ewma {:.0f}ms, min {:.0f}ms, window {})",
                self.browser_id, wanted_crf, self._rtt_ewma * 1000, self._rtt_min * 1000, wanted_limit,
            )

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
