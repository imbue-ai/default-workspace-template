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
import time
from collections.abc import Callable
from typing import Any

from loguru import logger

# pixelflux's wheel links native libs (libva*, libgbm, libdrm, ...) at IMPORT time,
# even in CPU mode. Those are absent on CI / bare boxes (where the live view never
# runs), AND on a fresh workspace they're apt-installed by the env-converge one-shot
# (env.d unit 1010-browser-display-audio), which may still be running when the browser service first imports this module. So DON'T import
# pixelflux at module load -- import it LAZILY on the first real capture (retried each
# start). The module then always imports (CI stays green), and a capture self-heals the
# moment the libs land, with no service restart and no boot-time race.
_pixelflux: "tuple[Any, Any] | None" = None  # cached (CaptureSettings, ScreenCapture)


def _load_pixelflux() -> "tuple[Any, Any] | None":
    """Import pixelflux on demand (cached once it succeeds). Returns
    ``(CaptureSettings, ScreenCapture)``, or None if its native libs aren't present yet
    (env-converge still running, or CI / a bare box)."""
    global _pixelflux
    if _pixelflux is None:
        try:
            from pixelflux import CaptureSettings, ScreenCapture

            _pixelflux = (CaptureSettings, ScreenCapture)
        except ImportError as e:
            logger.warning("pixelflux import failed ({}); no video until its native libs are present", e)
            return None
    return _pixelflux

# Outbound depth per stream socket. Kept SMALL to bound latency: a backpressured client
# riding at the cap would otherwise buffer (queue / stripes-per-frame / fps) seconds of
# stale video. ~64 stripes ≈ a few frames ≈ ~200 ms; on overflow we flush to the newest
# stripe and request a keyframe (a dropped delta corrupts that row anyway).
_STREAM_QUEUE_MAX = int(os.environ.get("BROWSER_STREAM_QUEUE_MAX", "64"))

# 20fps, not 30: a browser is mostly static reading + occasional scroll, so 20 is
# plenty smooth and cuts the CPU-x264 encode cost by a third -- which matters a lot on a
# small/constrained workspace where a busy encoder starves everything else (and drops
# sockets -> "Reconnecting…"). Damage-driven capture already idles a static page near 0.
_TARGET_FPS = float(os.environ.get("BROWSER_STREAM_FPS", "20"))
_VIDEO_CRF = int(os.environ.get("BROWSER_STREAM_CRF", "25"))
# A keyframe at least this often, so a client whose decoder ever desyncs (a rejected chunk
# or a dropped delta on a then-static page) always recovers within the interval instead of
# waiting for a repaint that may never come.
_KEYFRAME_INTERVAL_S = float(os.environ.get("BROWSER_STREAM_KEYFRAME_INTERVAL", "2"))
# Minimum seconds between keyframe requests triggered by a full-queue drop (so a
# persistently-slow client can't make us re-encode a keyframe on every stripe).
_IDR_ON_DROP_INTERVAL = 0.5

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
        self._cap: Any = None  # pixelflux ScreenCapture while capturing, else None
        self._subscribers: list["queue.Queue[bytes | None]"] = []
        self._lock = threading.Lock()  # guards _subscribers + _cap against the pixelflux thread
        self._mode = _MODE_H264
        self._last_idr_at = 0.0  # throttle for keyframe-on-drop (see _on_stripe)

    def add_subscriber(self, want_h264: bool) -> "queue.Queue[bytes | None] | None":
        """Register a stream socket. Starts the encoder on the first subscriber (in that
        subscriber's codec mode); otherwise forces a keyframe so the newcomer can begin
        decoding at once. Returns its outbound queue, or None if the encoder can't start
        (pixelflux's native libs not present yet) so the caller retries."""
        client_queue: "queue.Queue[bytes | None]" = queue.Queue(maxsize=_STREAM_QUEUE_MAX)
        with self._lock:
            if not self._subscribers:
                # First subscriber: start the encoder. If pixelflux's native libs aren't
                # present yet, DON'T register -- return None so the handler closes the
                # socket and the viewer's backoff retries (self-heals once the libs land).
                self._mode = _MODE_H264 if want_h264 else _MODE_JPEG
                if not self._start_locked():
                    return None
            elif self._cap is not None and want_h264 != (self._mode == _MODE_H264):
                logger.warning(
                    "browser stream {}: subscriber wants {} but encoder is running {} "
                    "(one mode per capture); this viewer may not decode",
                    self._display_name, "h264" if want_h264 else "jpeg",
                    "h264" if self._mode == _MODE_H264 else "jpeg",
                )
            self._subscribers.append(client_queue)
            # Force a keyframe NOW that this subscriber is REGISTERED, so it gets a full
            # frame at once. Critical for a browser sitting on an already-loaded STATIC page
            # (common: the home page finished loading during init, before any viewer): the
            # encoder is damage-driven, so with no repaint it would emit nothing, and the
            # encoder's own start-time initial frame fired into an empty subscriber list
            # (before this append) and was lost -- leaving the viewer BLACK until the next
            # damage (a mouse move, a nav). Requesting an IDR here paints immediately.
            if self._cap is not None:
                self._cap.request_idr_frame()
        return client_queue

    def remove_subscriber(self, client_queue: "queue.Queue[bytes | None]") -> None:
        """Deregister a stream socket; stops the encoder when the last one leaves. The
        actual ``stop_capture`` runs OUTSIDE ``_lock`` -- it joins pixelflux's callback
        thread, which may itself be blocked taking ``_lock`` in ``_on_stripe``; stopping
        under the lock would deadlock the whole (single-threaded) daemon."""
        cap_to_stop = None
        with self._lock:
            if client_queue in self._subscribers:
                self._subscribers.remove(client_queue)
            if not self._subscribers and self._cap is not None:
                cap_to_stop, self._cap = self._cap, None
        if cap_to_stop is not None:
            self._stop_capture(cap_to_stop)

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

    def _start_locked(self) -> bool:
        """Start the pixelflux encoder. Returns True on success, False if pixelflux's
        native libs aren't present (no video, but never a crash)."""
        loaded = _load_pixelflux()
        if loaded is None:
            logger.error(
                "browser stream {}: pixelflux unavailable; no video for this browser. The "
                "workspace's env-converge install provides its native libs (libva*, libgbm) for "
                "the headful path -- confirm it completed.",
                self._display_name,
            )
            return False
        capture_settings_cls, screen_capture_cls = loaded
        x, y, w, h = self._region()
        settings = capture_settings_cls()
        settings.capture_x, settings.capture_y = x, y
        settings.capture_width, settings.capture_height = w, h
        settings.target_fps = _TARGET_FPS
        settings.output_mode = self._mode
        settings.use_cpu = True
        settings.encode_node_index = -1  # force software x264 (no GPU in the sandbox)
        settings.video_crf = _VIDEO_CRF
        # Guaranteed periodic keyframe: recovery for any desynced decoder on a static page.
        settings.keyframe_interval_s = _KEYFRAME_INTERVAL_S
        # Paint-over: after a region goes static, re-encode it at higher quality (sharper
        # text, the common browser content) at ~zero steady cost. Also supplies IDR refresh.
        settings.use_paint_over_quality = True
        # NOTE: there is deliberately no ``video_vbv_multiplier`` here. pixelflux only wires
        # VBV into x264 on its CBR branch (``cbr_mode`` -> ``i_vbv_max_bitrate`` /
        # ``i_vbv_buffer_size``); the CRF branch we use sets ``f_rf_constant`` alone and
        # ignores the multiplier entirely. Setting it read as rate-capping the IDR bursts but
        # did nothing, so IDR bytes reach the socket unshaped. Actually bounding them means
        # opting into ``video_cbr_mode`` + ``video_bitrate_kbps``, which is a real
        # quality-vs-latency tradeoff and needs measuring before it is turned on.
        cap = screen_capture_cls()
        # NOTE: we deliberately do NOT enable pixelflux's server-side cursor compositing.
        # Its cursor monitor spawns a thread that reads $DISPLAY LATER (after we restore
        # it below), so it fails / could attach to another browser's display. The user's
        # own local cursor is already visible over the canvas, so this is no visible loss.
        # pixelflux connects via x11rb using $DISPLAY at start. Point it at this browser's
        # display for the (synchronous, no-await) start, then restore -- so a concurrent
        # browser launch's own DISPLAY is untouched.
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
        return True

    def _stop_capture(self, cap: Any) -> None:
        """Stop a capture in a short-lived daemon thread. ``stop_capture`` JOINS pixelflux's
        callback thread; the caller is the single asyncio loop, and a wedged Xvfb (dead
        display) could make that join hang -- freezing every browser, route, and launch in
        the fleet. The subscriber list + ``_cap`` are already cleared synchronously before
        we get here, so the browser is logically stopped; only the native teardown runs off
        the loop. (Also keeps _lock uncontended: the join would deadlock under it.)"""
        def _stop() -> None:
            try:
                cap.stop_capture()
            except _PIXELFLUX_ERRORS as e:
                logger.debug("capture stop ignored ({})", e)
        threading.Thread(target=_stop, name=f"pixelflux-stop{self._display_name}", daemon=True).start()
        logger.info("browser stream {}: encoder stopping", self._display_name)

    def _on_stripe(self, frame: object) -> None:
        """pixelflux native-thread callback: copy the stripe bytes out (the frame
        buffer is reused) and fan them out to every subscriber, header included."""
        data = bytes(memoryview(frame))  # type: ignore[arg-type]
        with self._lock:
            subscribers = list(self._subscribers)
            cap = self._cap
        dropped = False
        for client_queue in subscribers:
            try:
                client_queue.put_nowait(data)
            except queue.Full:
                # Backpressured: flush this client's whole backlog to the newest stripe
                # (stale video is worthless; a dropped delta corrupts the row regardless),
                # then it recovers on the keyframe we request below. Bounded by the queue
                # size (never unbounded).
                for _ in range(_STREAM_QUEUE_MAX):
                    try:
                        client_queue.get_nowait()
                    except queue.Empty:
                        break
                try:
                    client_queue.put_nowait(data)
                    dropped = True
                except queue.Full:
                    pass
        # A dropped H.264 delta corrupts that stripe until it next repaints (damage-
        # driven), which a static region may never do. Ask for a keyframe so the client
        # recovers -- throttled so a persistently-slow client doesn't spam keyframes.
        if dropped and cap is not None:
            now = time.monotonic()
            if now - self._last_idr_at > _IDR_ON_DROP_INTERVAL:
                self._last_idr_at = now
                try:
                    cap.request_idr_frame()
                except _PIXELFLUX_ERRORS:
                    pass

    def close(self) -> None:
        """Force-stop and drop all subscribers (browser teardown)."""
        cap_to_stop = None
        with self._lock:
            for client_queue in self._subscribers:
                try:
                    client_queue.put_nowait(None)  # shutdown sentinel
                except queue.Full:
                    pass
            self._subscribers.clear()
            cap_to_stop, self._cap = self._cap, None
        if cap_to_stop is not None:
            self._stop_capture(cap_to_stop)  # OUTSIDE the lock (joins the callback thread)
