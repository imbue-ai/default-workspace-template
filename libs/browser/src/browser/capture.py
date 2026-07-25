"""Striped H.264 / JPEG capture of a browser's X display (the video transport).

Wraps ``pixelflux`` (Selkies' capture+encode library: a prebuilt CPU wheel with
libx264 bundled, GStreamer-free). One :class:`Capture` per browser, started ON
DEMAND -- the first ``/stream`` subscriber starts the encoder, the last one to
leave stops it, so an unwatched browser costs zero CPU. pixelflux emits the final
wire bytes (a 10-byte header for H.264, 6-byte for JPEG, then the payload -- see
``docs/live-view-v2.md``); we fan those bytes out to every subscriber's queue
VERBATIM, never parsing them.

Threading: ``pixelflux`` invokes the stripe callback on its own native thread; the
fan-out onto per-subscriber ``queue.Queue``s is thread-safe. Encoder start/stop run
on the daemon's loop thread (the callers reach them via the loop bridge), which is
also where the per-browser ``DISPLAY`` is mutated -- so the brief env change here
can't race a concurrent browser launch (it saves and restores the prior value, and
the loop is single-threaded).
"""

import os
import queue
import threading
from collections.abc import Callable

from loguru import logger
from pixelflux import CaptureSettings, ScreenCapture

# Outbound depth per stream socket. Stripes are small (damage-driven, ~hundreds of
# bytes) so this is generous slack; a client that still overruns drops its oldest
# buffered stripes (it recovers on pixelflux's periodic paint-over full frames).
_STREAM_QUEUE_MAX = int(os.environ.get("BROWSER_STREAM_QUEUE_MAX", "512"))

_TARGET_FPS = float(os.environ.get("BROWSER_STREAM_FPS", "30"))
_VIDEO_CRF = int(os.environ.get("BROWSER_STREAM_CRF", "25"))

_MODE_H264 = 1
_MODE_JPEG = 0

# Errors a pixelflux call can raise (it's a PyO3/Rust extension; failures surface as
# these built-ins). The optional/best-effort calls -- cursor rendering, region update,
# stop -- swallow these so a display hiccup never takes the daemon down.
_PIXELFLUX_ERRORS = (RuntimeError, OSError, ValueError, AttributeError)


class Capture:
    """On-demand striped encoder for one browser's display region."""

    def __init__(self, display_name: str, region: Callable[[], tuple[int, int, int, int]]) -> None:
        self._display_name = display_name  # ":N"
        self._region = region  # () -> (x, y, w, h) current capture rect (chrome cropped)
        self._cap: ScreenCapture | None = None
        self._subscribers: list["queue.Queue[bytes | None]"] = []
        self._lock = threading.Lock()  # guards _subscribers + _cap against the pixelflux thread
        self._mode = _MODE_H264

    def add_subscriber(self, want_h264: bool) -> "queue.Queue[bytes | None]":
        """Register a stream socket. Starts the encoder on the first subscriber (in
        that subscriber's codec mode); otherwise forces a keyframe so the newcomer can
        begin decoding at once. Returns its outbound queue."""
        client_queue: "queue.Queue[bytes | None]" = queue.Queue(maxsize=_STREAM_QUEUE_MAX)
        with self._lock:
            first = not self._subscribers
            self._subscribers.append(client_queue)
            if first:
                self._mode = _MODE_H264 if want_h264 else _MODE_JPEG
                self._start_locked()
            elif self._cap is not None:
                if want_h264 != (self._mode == _MODE_H264):
                    logger.warning(
                        "browser stream {}: subscriber wants {} but encoder is running {} "
                        "(one mode per capture); this viewer may not decode",
                        self._display_name, "h264" if want_h264 else "jpeg",
                        "h264" if self._mode == _MODE_H264 else "jpeg",
                    )
                self._cap.request_idr_frame()  # keyframe so the newcomer starts clean
        return client_queue

    def remove_subscriber(self, client_queue: "queue.Queue[bytes | None]") -> None:
        """Deregister a stream socket; stops the encoder when the last one leaves."""
        with self._lock:
            if client_queue in self._subscribers:
                self._subscribers.remove(client_queue)
            if not self._subscribers:
                self._stop_locked()

    def has_subscribers(self) -> bool:
        with self._lock:
            return bool(self._subscribers)

    def request_keyframe(self) -> None:
        with self._lock:
            if self._cap is not None:
                self._cap.request_idr_frame()

    def update_region(self) -> None:
        """Re-point the capture at the current region (after a window resize / crop
        measurement). No-op when not capturing."""
        with self._lock:
            if self._cap is None:
                return
            x, y, w, h = self._region()
            try:
                self._cap.update_capture_region(x, y, w, h)
                self._cap.request_idr_frame()
            except _PIXELFLUX_ERRORS as e:
                logger.debug("capture region update ignored ({})", e)

    def _start_locked(self) -> None:
        x, y, w, h = self._region()
        settings = CaptureSettings()
        settings.capture_x, settings.capture_y = x, y
        settings.capture_width, settings.capture_height = w, h
        settings.target_fps = _TARGET_FPS
        settings.output_mode = self._mode
        settings.use_cpu = True
        settings.encode_node_index = -1  # force software x264 (no GPU in the sandbox)
        settings.video_crf = _VIDEO_CRF
        cap = ScreenCapture()
        try:
            cap.set_cursor_rendering(True)  # composite the pointer into the video (R1)
        except _PIXELFLUX_ERRORS as e:  # optional; older builds may lack the method
            logger.debug("cursor rendering unavailable ({})", e)
        # pixelflux connects via x11rb using $DISPLAY at start. Point it at this
        # browser's display for the (synchronous, no-await) start, then restore --
        # so a concurrent browser launch's own DISPLAY is untouched.
        prior = os.environ.get("DISPLAY")
        os.environ["DISPLAY"] = self._display_name
        try:
            cap.start_capture(self._on_stripe, settings)
        finally:
            if prior is None:
                os.environ.pop("DISPLAY", None)
            else:
                os.environ["DISPLAY"] = prior
        self._cap = cap
        logger.info("browser stream {}: encoder started ({}, {}x{}@{:.0f})",
                    self._display_name, "h264" if self._mode == _MODE_H264 else "jpeg", w, h, _TARGET_FPS)

    def _stop_locked(self) -> None:
        if self._cap is None:
            return
        try:
            self._cap.stop_capture()
        except _PIXELFLUX_ERRORS as e:
            logger.debug("capture stop ignored ({})", e)
        self._cap = None
        logger.info("browser stream {}: encoder stopped (no subscribers)", self._display_name)

    def _on_stripe(self, frame: object) -> None:
        """pixelflux native-thread callback: copy the stripe bytes out (the frame
        buffer is reused) and fan them out to every subscriber, header included."""
        data = bytes(memoryview(frame))  # type: ignore[arg-type]
        with self._lock:
            subscribers = list(self._subscribers)
        for client_queue in subscribers:
            try:
                client_queue.put_nowait(data)
            except queue.Full:
                try:
                    client_queue.get_nowait()  # drop oldest; paint-over recovers it
                    client_queue.put_nowait(data)
                except (queue.Empty, queue.Full):
                    pass

    def close(self) -> None:
        """Force-stop and drop all subscribers (browser teardown)."""
        with self._lock:
            for client_queue in self._subscribers:
                try:
                    client_queue.put_nowait(None)  # shutdown sentinel
                except queue.Full:
                    pass
            self._subscribers.clear()
            self._stop_locked()
