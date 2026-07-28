"""Striped H.264 / JPEG capture of a browser's X display (the video transport).

Wraps ``pixelflux`` (Selkies' capture+encode library: a prebuilt CPU wheel with
libx264 bundled, GStreamer-free). One :class:`Capture` per browser, started ON
DEMAND -- the first ``/stream`` subscriber starts the encoder, the last one to
leave stops it, so an unwatched browser costs zero CPU. pixelflux emits the final
wire bytes (a 10-byte header for H.264, 6-byte for JPEG, then the payload -- see
``docs/live-view-v2.md``); we fan those bytes out to every subscriber's
:class:`StripeMailbox` VERBATIM, never parsing beyond the tiny fixed header.

Each subscriber's mailbox keeps only the NEWEST unsent stripe per stripe row.
Stripes are drawn independently at ``(0, y_start)``, so an older unsent stripe for
a row is strictly obsolete the moment a newer one exists -- delivering it would
spend socket time showing something already false. This bounds a slow client's
staleness at ~one frame regardless of how many stripes the encoder produces
(the old fixed-depth FIFO held 64 stripes, which on a 2-core host -- 2 stripes per
frame -- was 32 frames = 1.6 s of stale video at 20 fps before anything dropped).

Threading: ``pixelflux`` invokes the stripe callback on its own native thread; the
fan-out into the mailboxes is lock-protected and thread-safe. Encoder start/stop
run on the daemon's loop thread (the callers reach them via the loop bridge), which
is also where the per-browser ``DISPLAY`` is mutated -- so the brief env change here
can't race a concurrent browser launch (it saves and restores the prior value, and
the loop is single-threaded). Each running capture also owns a small
:class:`_DamageRateBooster` watcher thread on its own X connection (see below).
"""

import os
import queue
import select
import struct
import threading
import time
from collections.abc import Callable
from typing import Any

from loguru import logger
from Xlib import X
from Xlib import display as xdisplay
from Xlib import error as xerror
from Xlib.ext import damage as xdamage

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


# Base capture rate. pixelflux's capture loop is a FIXED tick -- it sleeps to a
# wall-clock deadline and nothing wakes it early, so a screen change waits half a
# frame interval ON AVERAGE before the encoder even looks at it: ~25 ms at the old
# 20 fps, ~12.5 ms at 40. That tick wait, not encode CPU, is the larger latency
# cost -- measured on the smallest cloud workspace (2 vCPU), the whole encoder ran
# at ~27% of one core during sustained scrolling, so the CPU argument for 20 no
# longer held. Idle cost stays near zero either way: the encoder is damage-driven
# past capture, so a static page pays only the per-tick grab+hash.
_TARGET_FPS = float(os.environ.get("BROWSER_STREAM_FPS", "40"))
# While the display is actually changing, the _DamageRateBooster raises the live
# rate to this (and decays back after _BOOST_DECAY_S of quiet). <= base disables.
_BOOST_FPS = float(os.environ.get("BROWSER_STREAM_BOOST_FPS", "60"))
_BOOST_DECAY_S = float(os.environ.get("BROWSER_STREAM_BOOST_DECAY_S", "2"))
_VIDEO_CRF = int(os.environ.get("BROWSER_STREAM_CRF", "25"))
# A keyframe at least this often, so a client whose decoder ever desyncs (a rejected chunk
# or a dropped delta on a then-static page) always recovers within the interval instead of
# waiting for a repaint that may never come.
_KEYFRAME_INTERVAL_S = float(os.environ.get("BROWSER_STREAM_KEYFRAME_INTERVAL", "2"))
# Minimum seconds between keyframe requests triggered by dropping an unsent stripe (so a
# persistently-slow client can't make us re-encode a keyframe on every replacement).
_IDR_ON_DROP_INTERVAL = 0.5

_MODE_H264 = 1
_MODE_JPEG = 0

# Wire-format facts the mailbox relies on (see docs/live-view-v2.md): both stripe
# kinds carry their row's y_start as a big-endian u16 at bytes [4:6], and an H.264
# stripe's byte [1] is its frametype (0x01 = IDR keyframe, 0x00 = delta).
_STRIPE_TYPE_JPEG = 0x03
_STRIPE_TYPE_H264 = 0x04
_H264_FRAMETYPE_IDR = 0x01
_STRIPE_MIN_LEN = 6

# Errors a pixelflux call can raise (it's a PyO3/Rust extension; failures surface as
# these built-ins). The optional/best-effort calls -- cursor rendering, region update,
# rate changes, stop -- swallow these so a display hiccup never takes the daemon down.
_PIXELFLUX_ERRORS = (RuntimeError, OSError, ValueError, AttributeError)

# Errors the XDamage watcher can hit talking to a display that is resizing, wedged, or
# torn down mid-session (ConnectionClosedError is NOT under DisplayError in python-xlib).
# The watcher is an optimization; any of these just ends it.
_XLIB_ERRORS = (
    xerror.DisplayError,
    xerror.ConnectionClosedError,
    xerror.XError,
    OSError,
    RuntimeError,
    ValueError,
    AttributeError,
)


class StripeMailbox:
    """Outbound stripe buffer for one ``/stream`` subscriber: newest-per-row.

    Holds at most ONE pending stripe per stripe row (keyed by the wire header's
    y_start); a newer stripe for a row replaces the unsent older one in place, so
    the buffer's depth -- and therefore the client's worst-case staleness -- is
    bounded at one frame regardless of stripe count or frame rate. Replacement is
    where H.264's delta chain can break: dropping an unsent delta (or worse, an
    unsent keyframe) leaves the client decoding against state it never saw, so
    :meth:`put` reports it and the Capture requests a fresh IDR (throttled).

    API-compatible with the ``queue.Queue`` it replaced where the stream socket
    handler is concerned: ``get(timeout=...)`` raises ``queue.Empty`` on timeout and
    returns ``None`` (the shutdown sentinel) once closed and drained, so
    ``runner.stream_socket`` needed no changes.
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        # row key -> newest unsent stripe. Insertion-ordered; in-place replacement
        # keeps a row's queue position, so pop order stays oldest-pending-row first.
        self._pending: "dict[tuple[int, int], bytes]" = {}
        self._unknown_seq = 0  # distinct keys for unrecognized payloads: never coalesced
        self._closed = False

    def _row_key(self, data: bytes) -> "tuple[int, int]":
        if len(data) >= _STRIPE_MIN_LEN and data[0] in (_STRIPE_TYPE_H264, _STRIPE_TYPE_JPEG):
            return (data[0], struct.unpack_from(">H", data, 4)[0])
        # Unknown/short payload: give it a unique key so it passes through un-coalesced
        # (type byte -1 can never collide with a real stripe's key).
        self._unknown_seq += 1
        return (-1, self._unknown_seq)

    def put(self, data: bytes) -> bool:
        """Store a stripe, replacing any unsent one for the same row.

        Returns True iff the replacement broke that row's H.264 decode chain: an
        unsent H.264 stripe was dropped and the replacement is a delta (a keyframe
        replacement resets the chain by itself; JPEG stripes are self-contained)."""
        key = self._row_key(data)
        with self._cond:
            if self._closed:
                return False
            replaced = self._pending.get(key)
            self._pending[key] = data
            self._cond.notify()
        return (
            replaced is not None
            and key[0] == _STRIPE_TYPE_H264
            and data[1] != _H264_FRAMETYPE_IDR
        )

    def get(self, timeout: "float | None" = None) -> "bytes | None":
        """Pop the oldest-pending row's stripe. Raises ``queue.Empty`` on timeout;
        returns ``None`` once the mailbox is closed and drained (shutdown sentinel)."""
        with self._cond:
            if not self._cond.wait_for(lambda: self._pending or self._closed, timeout):
                raise queue.Empty
            if self._pending:
                key = next(iter(self._pending))
                return self._pending.pop(key)
            return None

    def get_nowait(self) -> "bytes | None":
        """Queue-compatible non-blocking pop (raises ``queue.Empty`` when nothing pends)."""
        return self.get(timeout=0)

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()


class _DamageRateBooster:
    """Raises the live capture rate while the display is actually changing.

    TRUE damage-driven capture -- waking the encoder the moment a pixel changes --
    needs an upstream pixelflux change: its capture loop sleeps to a fixed wall-clock
    deadline that nothing can interrupt, so the best any caller can do is choose how
    fast that clock ticks. pixelflux DOES honor rate changes live (``fps`` is re-read
    every iteration), so this watcher holds a second X connection to the browser's
    display, subscribes to XDamage on the root window (which reports rendering into
    any descendant, i.e. the whole screen), and:

    - on the first damage event, raises the rate to ``_BOOST_FPS``;
    - after ``_BOOST_DECAY_S`` with no damage, decays back to ``_TARGET_FPS``.

    So sustained motion (scroll, video) is captured at the boost rate while a static
    page ticks -- and pays its per-tick grab+hash -- only at the base rate. The
    watcher is strictly an optimization: any X error (display torn down mid-session,
    extension missing) just ends the thread and capture continues at the base rate.
    Idle, the thread blocks in ``select`` on the X socket and costs nothing.
    """

    def __init__(self, display_name: str, cap: Any) -> None:
        self._display_name = display_name
        self._cap = cap  # pixelflux ScreenCapture; update_framerate() is thread-safe (atomic store)
        # Self-pipe so stop() can interrupt a select() blocked on the X socket.
        self._stop_r, self._stop_w = os.pipe()
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"damage-boost{display_name}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Non-blocking: wake the watcher thread so it exits, then release the wake pipe.

        Called exactly once, by whoever harvested the booster out of the Capture (the
        harvest nulls ``_booster`` under the Capture lock, so there is a single caller).
        The pipe fds are closed HERE, never by the thread -- so a stop can never write
        into a recycled fd number, and a thread that died early (X error) at worst
        parks two fds until this teardown."""
        self._stopped.set()
        try:
            os.write(self._stop_w, b"x")
        except OSError:
            pass
        # If the thread is blocked in select(), either the write above or the close
        # below (EBADF) wakes it; it exits on the event either way.
        for pipe_fd in (self._stop_r, self._stop_w):
            try:
                os.close(pipe_fd)
            except OSError:
                pass

    def _set_fps(self, fps: float) -> None:
        try:
            self._cap.update_framerate(fps)
        except _PIXELFLUX_ERRORS as e:
            logger.debug("damage boost {}: rate change ignored ({})", self._display_name, e)

    def _run(self) -> None:
        disp = None
        try:
            disp = xdisplay.Display(self._display_name)
            # DAMAGE requires a version handshake before any other request.
            disp.damage_query_version()
            damage_id = disp.screen().root.damage_create(xdamage.DamageReportNonEmpty)
            disp.flush()
            fd = disp.fileno()
            boosted = False
            last_damage = 0.0
            while not self._stopped.is_set():
                # Idle (not boosted): block until damage or stop. Boosted: wake at
                # decay granularity to check for quiet.
                readable, _, _ = select.select(
                    [fd, self._stop_r], [], [], _BOOST_DECAY_S if boosted else None
                )
                if self._stop_r in readable:
                    break
                now = time.monotonic()
                if fd in readable:
                    while disp.pending_events():
                        disp.next_event()  # contents irrelevant; the wakeup IS the signal
                    # NonEmpty reporting arms once per accumulation: subtract to re-arm.
                    disp.damage_subtract(damage_id, X.NONE, X.NONE)
                    disp.flush()
                    last_damage = now
                    if not boosted:
                        boosted = True
                        self._set_fps(_BOOST_FPS)
                if boosted and now - last_damage >= _BOOST_DECAY_S:
                    boosted = False
                    self._set_fps(_TARGET_FPS)
        except _XLIB_ERRORS as e:
            logger.debug("damage boost {}: watcher ended ({}); capture stays at base rate",
                         self._display_name, e)
        finally:
            # The pipe fds are stop()'s to close, never this thread's (fd-recycling safety).
            if disp is not None:
                try:
                    disp.close()
                except _XLIB_ERRORS:
                    pass


class Capture:
    """On-demand striped encoder for one browser's display region."""

    def __init__(self, display_name: str, region: Callable[[], tuple[int, int, int, int]]) -> None:
        self._display_name = display_name  # ":N"
        self._region = region  # () -> (x, y, w, h) current capture rect (chrome cropped)
        self._cap: Any = None  # pixelflux ScreenCapture while capturing, else None
        self._booster: "_DamageRateBooster | None" = None
        self._subscribers: "list[StripeMailbox]" = []
        self._lock = threading.Lock()  # guards _subscribers + _cap against the pixelflux thread
        self._mode = _MODE_H264
        self._last_idr_at = 0.0  # throttle for keyframe-on-drop (see _on_stripe)

    def add_subscriber(self, want_h264: bool) -> "StripeMailbox | None":
        """Register a stream socket. Starts the encoder on the first subscriber (in that
        subscriber's codec mode); otherwise forces a keyframe so the newcomer can begin
        decoding at once. Returns its outbound mailbox, or None if the encoder can't start
        (pixelflux's native libs not present yet) so the caller retries."""
        mailbox = StripeMailbox()
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
            self._subscribers.append(mailbox)
            # Force a keyframe NOW that this subscriber is REGISTERED, so it gets a full
            # frame at once. Critical for a browser sitting on an already-loaded STATIC page
            # (common: the home page finished loading during init, before any viewer): the
            # encoder is damage-driven, so with no repaint it would emit nothing, and the
            # encoder's own start-time initial frame fired into an empty subscriber list
            # (before this append) and was lost -- leaving the viewer BLACK until the next
            # damage (a mouse move, a nav). Requesting an IDR here paints immediately.
            if self._cap is not None:
                self._cap.request_idr_frame()
        return mailbox

    def remove_subscriber(self, mailbox: StripeMailbox) -> None:
        """Deregister a stream socket; stops the encoder when the last one leaves. The
        actual ``stop_capture`` runs OUTSIDE ``_lock`` -- it joins pixelflux's callback
        thread, which may itself be blocked taking ``_lock`` in ``_on_stripe``; stopping
        under the lock would deadlock the whole (single-threaded) daemon."""
        cap_to_stop = None
        booster_to_stop = None
        with self._lock:
            if mailbox in self._subscribers:
                self._subscribers.remove(mailbox)
            if not self._subscribers and self._cap is not None:
                cap_to_stop, self._cap = self._cap, None
                booster_to_stop, self._booster = self._booster, None
        if booster_to_stop is not None:
            booster_to_stop.stop()
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
        if _BOOST_FPS > _TARGET_FPS:
            self._booster = _DamageRateBooster(self._display_name, cap)
        logger.info("browser stream {}: encoder started ({}, {}x{}@{:.0f}, boost {:.0f})",
                    self._display_name, "h264" if self._mode == _MODE_H264 else "jpeg", w, h,
                    _TARGET_FPS, _BOOST_FPS if _BOOST_FPS > _TARGET_FPS else _TARGET_FPS)
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
        chain_broken = False
        for mailbox in subscribers:
            if mailbox.put(data):
                chain_broken = True
        # Replacing an unsent H.264 stripe with a delta leaves that row's decoder without
        # the state the delta assumes, corrupting it until the next keyframe -- which on a
        # static region may never come on its own. Ask for one, throttled so a persistently
        # slow client doesn't turn every replacement into a full-region IDR.
        if chain_broken and cap is not None:
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
        booster_to_stop = None
        with self._lock:
            for mailbox in self._subscribers:
                mailbox.close()  # unblocks the stream socket with the None sentinel
            self._subscribers.clear()
            cap_to_stop, self._cap = self._cap, None
            booster_to_stop, self._booster = self._booster, None
        if booster_to_stop is not None:
            booster_to_stop.stop()
        if cap_to_stop is not None:
            self._stop_capture(cap_to_stop)  # OUTSIDE the lock (joins the callback thread)
