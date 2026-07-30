"""Pixelflux H.264 live video pipe with credit-ack flow control.

An alternative to the CDP JPEG screencast for the live view: pixelflux
(linuxserver's Rust capture/encode engine, the one behind Selkies) watches the
service's shared X display, encodes damage-driven H.264 full frames on the CPU,
and this module streams them to a WebCodecs viewer over the existing
browser-service WebSocket path. Idle screens encode nothing and send nothing.

Flow control is the load-bearing design point, and it is NOT socket
backpressure: the delivery chain to the viewer crosses several hops (gVisor
netstack, sshd -- whose per-channel window alone is 2 MB -- a tunnel, a local
forwarder), each with buffers that swallow writes long after the path is
congested, so "drop when send blocks" fires seconds too late. Instead the
viewer acknowledges every frame it decodes, and the server never has more than
``_CREDIT_LIMIT`` unacknowledged frames outstanding. Bytes in flight across
ALL hops are then bounded by that many frames, no matter where the congestion
lives; when the path degrades the viewer gets fewer, fresher frames instead of
an ever-older backlog. This is the same shape as RDP's per-frame
FRAME_ACKNOWLEDGE and TigerVNC's fence-based congestion window.

Dropping an H.264 delta frame breaks the decode chain, so every drop marks the
stream as needing a keyframe: delta frames are discarded until the (rate
limited) IDR request produces one.

Wire format: pixelflux's own stripe header, verified against
``pixelflux/src/encoders/software.rs`` (encode_with_headers)::

    byte 0     0x04 (H.264 magic)
    byte 1     frame type: 0x01 IDR, 0x02 non-IDR intra, 0x00 delta
    bytes 2-3  frame counter, u16 big-endian (wraps)
    bytes 4-9  stripe y-start / width / height, u16 big-endian each
    bytes 10+  Annex B payload

In full-frame mode there is exactly one stripe per frame (y-start 0). The
header passes through to the viewer untouched; frame ids double as ack tokens.
"""

import contextlib
import os
import shutil
import subprocess
import threading
import time

from loguru import logger
from pixelflux import CaptureSettings, ScreenCapture

WIRE_HEADER_LEN = 10
_WIRE_MAGIC_H264 = 0x04
FRAME_TYPE_IDR = 0x01

# Unacknowledged-frame ceiling. Two frames keeps a frame in flight while the
# previous one's ack returns, so throughput is not halved by the round trip;
# anything higher just rebuilds queueing in the tunnel.
_CREDIT_LIMIT = 2

# Server-side floor between IDR requests, so a struggling viewer (every delta
# dropped -> every frame wants a keyframe) cannot make the encoder spend all
# its time on full refreshes. Keyframes are the most expensive frames to
# encode AND the largest on the wire, which is exactly what a congested path
# cannot afford.
_IDR_REQUEST_MIN_INTERVAL = 0.4

_CAPTURE_FPS = float(os.environ.get("BROWSER_VIDEO_FPS", "30"))
_VIDEO_CRF = int(os.environ.get("BROWSER_VIDEO_CRF", "25"))
# Paint-over: after this many static frames pixelflux re-encodes the settled
# screen at the lower (better) CRF, which is what keeps text readable after a
# scroll without paying 4:4:4 everywhere.
_PAINTOVER_CRF = int(os.environ.get("BROWSER_VIDEO_PAINTOVER_CRF", "18"))


class VideoPipeError(RuntimeError):
    pass


def is_available() -> bool:
    """An X display to capture from (pixelflux itself is a hard dependency)."""
    return bool(os.environ.get("DISPLAY"))


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


def parse_wire_header(packet: bytes) -> tuple[int, int, bool]:
    """(frame_id, frame_type, is_idr) from a pixelflux wire packet.

    Raises on anything that is not an H.264 packet -- a format drift between
    pixelflux versions should explode here, not paint garbage.
    """
    if len(packet) < WIRE_HEADER_LEN:
        raise VideoPipeError(f"video packet shorter than wire header: {len(packet)} bytes")
    if packet[0] != _WIRE_MAGIC_H264:
        raise VideoPipeError(f"unexpected video packet magic 0x{packet[0]:02x}")
    frame_type = packet[1]
    frame_id = (packet[2] << 8) | packet[3]
    return frame_id, frame_type, frame_type == FRAME_TYPE_IDR


class CreditWindow:
    """Pure bookkeeping for ack-credit flow control; thread-safety is the caller's.

    Tracks how many sent frames the viewer has not yet acknowledged and whether
    the decode chain is broken (a delta was dropped) so only a keyframe may
    resume the stream.
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
        """Acknowledge frame_id and everything sent before it.

        Cumulative acks make loss of an individual ack message harmless; frame
        ids wrap at u16 so membership, not ordering, decides the cutoff.
        """
        if frame_id in self._unacked:
            cutoff = self._unacked.index(frame_id)
            del self._unacked[: cutoff + 1]

    def admits(self, is_keyframe: bool) -> bool:
        if not self.has_credit:
            return False
        return is_keyframe or not self.needs_keyframe


class PixelfluxVideoPipe:
    """One viewer's capture->encode->send pipeline on the service's X display.

    Owns a pixelflux ScreenCapture for the lifetime of one WebSocket
    connection. The encoder callback lands frames in a single-slot mailbox
    (newest frame wins -- with full frames, any unsent older frame is stale by
    definition); the connection's sender thread drains the mailbox when the
    credit window admits the frame. Overwriting or skipping a delta breaks the
    decode chain, so both paths funnel through the needs-keyframe state and the
    rate-limited IDR request.
    """

    def __init__(self, browser_id: str, display: str) -> None:
        self.browser_id = browser_id
        self.display = display
        self._capture = None
        self._condition = threading.Condition()
        self._mailbox: bytes | None = None
        self._mailbox_is_idr = False
        self._window = CreditWindow()
        self._closed = False
        self._last_idr_request = 0.0
        self.frames_captured = 0
        self.frames_dropped = 0

    def start(self) -> None:
        if not is_available():
            raise VideoPipeError("no DISPLAY to capture in this workspace")
        width, height = display_geometry(self.display)
        settings = CaptureSettings()
        settings.capture_width = width
        settings.capture_height = height
        settings.target_fps = _CAPTURE_FPS
        settings.output_mode = 1  # H.264
        settings.use_cpu = True  # no GPU in these workspaces; fail loud, not slow
        settings.video_fullframe = True
        settings.video_crf = _VIDEO_CRF
        settings.use_paint_over_quality = True
        settings.video_paintover_crf = _PAINTOVER_CRF
        capture = ScreenCapture()
        capture.start_capture(self._on_frame, settings)
        self._capture = capture
        logger.info(
            "video pipe started for browser {} on {} ({}x{} @ {}fps, crf {})",
            self.browser_id, self.display, width, height, _CAPTURE_FPS, _VIDEO_CRF,
        )

    def _on_frame(self, frame) -> None:  # noqa: ANN001  (pixelflux native frame object)
        # Encoder thread: copy out (the native buffer is reused) and mailbox it.
        packet = bytes(frame)
        try:
            _, _, is_idr = parse_wire_header(packet)
        except VideoPipeError as error:
            logger.warning("video pipe {} dropped malformed packet ({})", self.browser_id, error)
            return
        with self._condition:
            self.frames_captured += 1
            if self._mailbox is not None:
                # Replacing an unsent frame; if it (or the replacement path)
                # was a delta the chain is broken until a keyframe.
                self.frames_dropped += 1
                if not self._mailbox_is_idr or not is_idr:
                    self._window.note_dropped_delta()
            self._mailbox = packet
            self._mailbox_is_idr = is_idr
            self._condition.notify()

    def next_packet(self, timeout: float) -> bytes | None:
        """Block until a frame is admitted by the credit window (or timeout).

        Called by the connection's sender thread. Returns None on timeout or
        after close; the caller just loops. Frames the window refuses stay in
        the mailbox (a newer one may overwrite them meanwhile); deltas refused
        for want of a keyframe trigger the IDR request.
        """
        deadline = time.monotonic() + timeout
        with self._condition:
            while not self._closed:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                if self._mailbox is not None:
                    if self._window.admits(self._mailbox_is_idr):
                        packet = self._mailbox
                        self._mailbox = None
                        frame_id, _, is_idr = parse_wire_header(packet)
                        if is_idr:
                            self._window.note_sent_keyframe(frame_id)
                        else:
                            self._window.note_sent(frame_id)
                        return packet
                    if self._window.needs_keyframe and not self._mailbox_is_idr:
                        self.frames_dropped += 1
                        self._mailbox = None
                        self._request_idr_locked()
                self._condition.wait(timeout=remaining)
            return None

    def ack(self, frame_id: int) -> None:
        """Viewer acknowledged a decoded frame; credit opens, sender wakes."""
        with self._condition:
            self._window.ack(frame_id)
            if self._window.needs_keyframe:
                self._request_idr_locked()
            self._condition.notify()

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
                "video pipe stopped for browser {} (captured {}, dropped {})",
                self.browser_id, self.frames_captured, self.frames_dropped,
            )

    @staticmethod
    def _stop_capture(capture) -> None:  # noqa: ANN001
        with contextlib.suppress(Exception):
            capture.stop_capture()
