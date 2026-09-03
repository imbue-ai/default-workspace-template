"""Fleet media socket: pixelflux H.264 video to one viewer of one browser.

The pixel path here is the fleet's streaming stack -- the newest-wins credit-ack pipe,
the outbound send loop (control message, out-of-band cursor, stripe packets),
TCP_NODELAY, the permessage-deflate strip, and the 250ms heartbeat the client keys on.
The caller resolves the browser by NAME and hands us its private display (:N), so we
open a capture pipe on THAT display.

The clipboard bridge (XFixes copy-out monitor + xclip paste-in via
:mod:`browser.xclipboard`) is keyed per browser (each has its own display and possibly
several viewers), and paste-IN is GATED on ``session.input_allowed`` so only the
controlling human can write into the browser -- copy-OUT (what's already on the screen
the human is watching) is ungated.

Audio: pcmflux Opus chunks (:mod:`browser.audiopipe`) ride
the SAME ``/stream`` socket as video, interleaved by a leading magic byte and
drained on the single sender thread. The fleet adaptation is that each browser
captures ITS OWN PulseAudio sink monitor (``session.audio_capture_device``), so
split-view browsers don't mix sound. Audio is strictly additive -- if pcmflux or
the sink is unavailable, video streams unchanged.
"""

import base64
import os
import socket as socket_module
import threading
import time
from collections import deque
from typing import Any
from typing import Callable

from flask import Response
from flask import jsonify
from flask import request
from loguru import logger
from simple_websocket import ConnectionClosed

from browser import telemetry
from browser import window_guardian
from browser.audiopipe import AudioPipe
from browser.audiopipe import AudioPipeError
from browser.audiopipe import is_available as is_audio_available
from browser.stream_conductor import StreamConnection
from browser.stream_conductor import conductor
from browser.videopipe import PixelfluxVideoPipe
from browser.videopipe import VideoPipeError
from browser.window_guardian import WindowGuardian
from browser.xclipboard import ClipboardError
from browser.xclipboard import ClipboardMonitor
from browser.xclipboard import read_clipboard
from browser.xclipboard import set_clipboard
from browser.xinput import InputRouter

_RECEIVE_POLL_SECONDS = 0.05
# While a viewer is connected the server never goes silent longer than this: the
# client's freeze attributor needs "socket silent" to unambiguously mean transport
# (a starved server that encodes nothing would otherwise be misfiled as a stall).
_HEARTBEAT_SECONDS = 0.25

# Paste-in bodies (images) can be large; the runner caps the request body so a giant
# paste is rejected before it's read. 10 MiB matches xclipboard's read cap and the
# client-side pre-check.
_CLIPBOARD_MAX_BYTES = 10 * 1024 * 1024
# Text small enough to inline over the stream control channel; larger text and all
# images route through GET /clipboard/out so no >1 MiB WS frame tears down the video
# socket.
_CLIP_INLINE_TEXT_MAX = 200 * 1024


class _BrowserClipboard:
    """Per-browser clipboard state: the copy-out stash (one slot, newest wins), the
    set of connected viewers' outbound send queues, and the XFixes monitor watching
    this browser's display. The monitor is started with the first viewer and closed
    when the last one leaves (ref-counted on ``sinks``)."""

    def __init__(self, display: str) -> None:
        self.display = display
        self.out_data: bytes = b""
        self.out_mime: str = "text/plain"
        self.sinks: set[Callable[[str], None]] = set()
        self.monitor: ClipboardMonitor | None = None


# Per-browser clipboard state, keyed by browser id, guarded by one lock. The HTTP
# paste/out routes and the /stream send loop / monitor thread all reach it here.
_clip_lock = threading.Lock()
_clips: dict[str, _BrowserClipboard] = {}

class _BrowserSlotCap:
    """Per-browser connection cap: a keyed counter under one lock. reserve() claims a slot for
    a browser id (False at cap); release() frees one, dropping the key at zero so the map holds
    only browsers with a live connection. Backstops the per-connection footprint (threads, FDs,
    X connections) that the conductor's one-live-encoder cap doesn't bound -- a client can't
    exhaust resources by opening connections in a loop."""

    def __init__(self, cap: int) -> None:
        self._cap = cap
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}

    def reserve(self, browser_id: str) -> bool:
        with self._lock:
            count = self._counts.get(browser_id, 0)
            if count >= self._cap:
                return False
            self._counts[browser_id] = count + 1
            return True

    def release(self, browser_id: str) -> None:
        with self._lock:
            count = self._counts.get(browser_id, 0)
            if count <= 1:
                self._counts.pop(browser_id, None)
            else:
                self._counts[browser_id] = count - 1


# One cap per connection kind: /stream (a thread + X connection + pipe each), /cast (two
# threads + a broadcast queue the loop iterates per control event), and the read-only
# /telemetry firehose (a reader thread each). Same generous per-browser limit for all three.
stream_slots = _BrowserSlotCap(8)
cast_slots = _BrowserSlotCap(8)
telemetry_slots = _BrowserSlotCap(8)

# Reject an oversized inbound /stream control frame before parsing. Control frames (ack,
# resize, i/h, held-key list) are tiny; a multi-MB frame is hostile, not real traffic.
_MAX_INBOUND_FRAME_BYTES = 64 * 1024


# Warn the viewer when the box is under memory pressure. gVisor makes per-process memory
# accounting unreliable (RSS double-counts shared pages across Chromium's ~10 processes, so
# an idle browser already reads ~4GB), so we can't threshold a single browser. Instead we
# trigger box-wide off MemAvailable (reliable under gVisor): warn once the box is more than
# this fraction committed. Default 0.75 -- half was too alarmist.
_MEM_WARN_USED_FRACTION = float(os.environ.get("BROWSER_MEM_WARN_USED_FRACTION", "0.75"))
_MEM_CHECK_INTERVAL = 5.0


def _memory_pressure_high() -> bool:
    """True when the box is more than _MEM_WARN_USED_FRACTION committed (allocatable memory
    below the remaining fraction of total RAM)."""
    total = available = None
    try:
        with open("/proc/meminfo") as meminfo:
            for line in meminfo:
                if line.startswith("MemTotal:"):
                    total = int(line.split()[1])
                elif line.startswith("MemAvailable:"):
                    available = int(line.split()[1])
                if total is not None and available is not None:
                    break
    except OSError:
        return False
    if not total or available is None:
        return False
    return available < (1.0 - _MEM_WARN_USED_FRACTION) * total


def _on_remote_clipboard(browser_id: str, data: bytes, mime: str) -> None:
    """A copy happened in the remote browser (XFixes fired): stash it for the GET and
    signal every connected viewer of this browser. Small text inlines over the control
    channel; images and large text ride the GET so no >1 MiB WS frame can tear down the
    video socket."""
    with _clip_lock:
        clip = _clips.get(browser_id)
        if clip is None:
            return
        clip.out_data = data
        clip.out_mime = mime
        if mime.startswith("text/") and len(data) <= _CLIP_INLINE_TEXT_MAX:
            message = "clip,txt," + base64.b64encode(data).decode("ascii")
        else:
            message = f"clip,out,{mime},{len(data)}"
        for send in clip.sinks:
            send(message)


def _register_clip_sink(browser_id: str, display: str, send: Callable[[str], None]) -> None:
    """Add a viewer's outbound queue for this browser and ensure its XFixes copy-out
    monitor is running (started on the first viewer)."""
    stale_monitor: ClipboardMonitor | None = None
    with _clip_lock:
        clip = _clips.get(browser_id)
        if clip is None or clip.display != display:
            # First viewer (or the browser relaunched on a new display): fresh state.
            # Defer closing the old monitor until AFTER the lock is released -- close()
            # joins the monitor thread, which may be blocked in _on_remote_clipboard
            # waiting on _clip_lock; closing it here would stall for the full join timeout
            # (mirrors _unregister_clip_sink, which closes off the lock for the same reason).
            if clip is not None:
                stale_monitor = clip.monitor
            clip = _BrowserClipboard(display)
            _clips[browser_id] = clip
        clip.sinks.add(send)
        if clip.monitor is None:
            monitor = ClipboardMonitor(display, lambda d, m: _on_remote_clipboard(browser_id, d, m))
            clip.monitor = monitor
            monitor.start()
    if stale_monitor is not None:
        stale_monitor.close()


def _unregister_clip_sink(browser_id: str, send: Callable[[str], None]) -> None:
    """Drop a viewer's queue; close the monitor and forget the browser once the last
    viewer of it disconnects."""
    monitor_to_close: ClipboardMonitor | None = None
    with _clip_lock:
        clip = _clips.get(browser_id)
        if clip is None:
            return
        clip.sinks.discard(send)
        if not clip.sinks:
            monitor_to_close = clip.monitor
            _clips.pop(browser_id, None)
    if monitor_to_close is not None:
        monitor_to_close.close()  # off the lock: close() joins the monitor thread


def _await_clipboard_owned(display: str, attempts: int = 20, interval: float = 0.05) -> bool:
    """Poll the X CLIPBOARD until xclip's forked owner is serving it (up to ~1s).

    Runs off the event loop (Flask request thread), same category of display settle as the
    Xvfb/PulseAudio/XTEST waits elsewhere -- a real ownership handoff, not an event-loop sleep."""
    for _ in range(attempts):
        if read_clipboard(display) is not None:
            return True
        time.sleep(interval)
    return False


def clipboard_paste(browser_id: str, session: Any, data: bytes, mime: str) -> Response:
    """Paste-in: set the browser's X CLIPBOARD from the POST body, then inject Ctrl+V.

    GATED on ``session.input_allowed`` -- only the controlling human may write into the
    browser (an agent mid-task must not have a stray paste land). The selection is set
    BEFORE the paste keystroke, and the write is recorded so the copy-out monitor doesn't
    echo it back (plus the fleet's control gate)."""
    if not session.input_allowed:
        return jsonify({"error": "not controlling"}), 409
    with _clip_lock:
        clip = _clips.get(browser_id)
        display = clip.display if clip is not None else None
        monitor = clip.monitor if clip is not None else None
    if display is None:
        return jsonify({"error": "no active viewer"}), 409
    if not data:
        return jsonify({"error": "empty clipboard"}), 400
    if len(data) > _CLIPBOARD_MAX_BYTES:  # enforce the declared cap (was previously unused)
        return jsonify({"error": "clipboard too large"}), 413
    router = InputRouter(display)
    try:
        set_clipboard(display, data, mime)
        if monitor is not None:
            monitor.note_written(data)
        # xclip -i forks a BACKGROUND selection owner; injecting Ctrl+V before it has
        # actually claimed the X CLIPBOARD makes the paste read a stale/empty selection --
        # the intermittent "nothing pasted" while the client still reported success. Confirm
        # the selection is owned+readable before pasting (off-loop, in the Flask request
        # thread), and fail honestly if it never takes, so an ok response actually means the
        # clipboard was set and Ctrl+V was injected.
        if not _await_clipboard_owned(display):
            logger.warning("clipboard paste for {} never took ownership", browser_id)
            return jsonify({"error": "clipboard not set"}), 500
        router.paste()
    except ClipboardError as error:
        logger.warning("clipboard paste failed for {} ({})", browser_id, error)
        return jsonify({"error": "paste failed"}), 500
    finally:
        router.close()
    return jsonify({"ok": True})


def clipboard_out(browser_id: str) -> Response:
    """Copy-out: the bytes of the last remote copy on this browser, in their native mime."""
    with _clip_lock:
        clip = _clips.get(browser_id)
        data = clip.out_data if clip is not None else b""
        mime = clip.out_mime if clip is not None else "text/plain"
    return Response(data, mimetype=mime)


def strip_websocket_compression() -> None:
    """Drop the client's permessage-deflate offer before the handshake (verbatim).

    simple_websocket accepts the extension unconditionally and flask-sock exposes
    no off switch, so without this every already-compressed H.264 stripe would be
    zlib-deflated here and inflated by the viewer -- pure latency and CPU waste on
    incompressible data. flask_sock builds its handshaking Server from
    ``request.environ`` inside the route, so a before_request hook is early enough.
    """
    request.environ.pop("HTTP_SEC_WEBSOCKET_EXTENSIONS", None)


def _set_nodelay(ws: Any) -> None:
    """Interactive stream: never let Nagle hold a stripe or input tail (verbatim)."""
    try:
        ws.sock.setsockopt(socket_module.IPPROTO_TCP, socket_module.TCP_NODELAY, 1)
    except OSError as error:
        logger.debug("could not set TCP_NODELAY on stream socket ({})", error)


def _receive_pump(
    ws: Any, pipe: PixelfluxVideoPipe, router: InputRouter, session: Any,
    conn: StreamConnection, stop_event: threading.Event,
) -> None:
    """Read credit acks, resize, active/hidden, and Selkies input on a dedicated thread
    until the socket closes.

    ``i``/``h`` report this viewer's attention to the conductor (``i`` = the user is
    attending to this pane -> make it the sole active stream; ``h`` = it went off-screen
    -> pause). Live input (``router.handle`` -- Selkies kd/ku/kr/kh/m -> XTEST) is GATED on
    ``session.input_allowed`` (who holds control); a real key/mouse also claims the active
    stream, since interacting with a pane means you're attending to it. Resize of the
    SHARED Chromium window is likewise gated on control (only the controller reshapes the
    window); the per-connection capture region is honored regardless.
    """
    try:
        while not stop_event.is_set():
            data = ws.receive(timeout=_RECEIVE_POLL_SECONDS)
            if data is None or isinstance(data, bytes):
                continue
            if len(data) > _MAX_INBOUND_FRAME_BYTES:
                # Control frames are tiny; a giant one is hostile (e.g. a multi-MB `kh`
                # held-key list building a huge int set). Drop it before parsing.
                continue
            if data.startswith("ack,"):
                try:
                    frame_id, y_start = data[4:].split(",")
                    pipe.ack(int(frame_id), int(y_start))
                except ValueError:
                    logger.warning("dropped malformed ack {!r}", data[:32])
            elif data == "i":
                conductor.interact(conn)  # user is attending to this pane -> sole active stream
            elif data == "h":
                conductor.hidden(conn)    # pane off-screen -> pause (0 CPU, 0 bandwidth)
            elif data.startswith("r,"):
                try:
                    width_s, height_s = data[2:].split(",")
                    # Floor to a sane minimum; the pipe clamps to the framebuffer
                    # cap and to even dimensions and returns what it applied.
                    requested_w = max(320, int(width_s))
                    requested_h = max(240, int(height_s))
                except ValueError:
                    logger.warning("dropped malformed resize {!r}", data[:32])
                else:
                    applied_w, applied_h = pipe.set_capture_region(requested_w, requested_h)
                    if session.input_allowed:  # only the controller reshapes the shared window
                        router.resize_window(applied_w, applied_h)
            elif data.startswith("kr") or data.startswith("kh"):
                router.handle(data)  # release-all / held-key heartbeat: always allowed
            elif session.input_allowed:
                conductor.interact(conn)  # driving a pane means you're attending to it
                router.handle(data)       # kd/ku/m: only while the human holds control
    except ConnectionClosed:
        pass
    finally:
        stop_event.set()


def serve_stream(ws: Any, browser_id: str, display: str, session: Any) -> None:
    """Serve one viewer of one already-running browser on its private ``display``.

    The outbound send loop drives one viewer's media plane. ``session``
    is the LiveBrowser, read for ``input_allowed`` (the control gate for injected input) and
    ``audio_capture_device`` (this browser's PulseAudio monitor, or None if no audio).
    """
    _set_nodelay(ws)
    if not stream_slots.reserve(browser_id):
        logger.warning("stream connection cap reached for {}; rejecting new viewer", browser_id)
        ws.close(1013)  # retryable: an existing viewer may free a slot
        return
    pipe = PixelfluxVideoPipe(browser_id, display)
    try:
        pipe.start()
    except VideoPipeError as error:
        logger.warning("video pipe failed to start for {} ({})", browser_id, error)
        stream_slots.release(browser_id)
        ws.close(1011)
        return
    # Passive telemetry: begin recording for this browser (the hot paths emit into a
    # lock-free ring drained by the read-only /telemetry firehose; see browser.telemetry).
    # Everything from here on is inside the try so a prologue failure (e.g. InputRouter
    # can't open the X display) still unwinds the telemetry ref AND the started pipe --
    # otherwise the ref leaks (pinning the resource sampler on) and the capture is orphaned.
    telemetry.hub.open(browser_id)
    router: "InputRouter | None" = None
    clip_sink: "Callable[[str], None] | None" = None
    audio_pipe: "AudioPipe | None" = None
    receiver: "threading.Thread | None" = None
    conn: "StreamConnection | None" = None
    guardian: "WindowGuardian | None" = None
    stop_event = threading.Event()
    try:
        telemetry.hub.emit(browser_id, {"type": "conn", "event": "open"})
        router = InputRouter(display)
        # Cold-start size: the viewer passes its real pane size as ?w=&h= on the connect
        # URL, so the first emitted frame is already pane-sized -- no 1280x800 frame is
        # ever shown and no resize round-trip is needed.
        initial_w = request.args.get("w", type=int)
        initial_h = request.args.get("h", type=int)
        if initial_w is not None and initial_h is not None:
            applied_w, applied_h = pipe.set_capture_region(max(320, initial_w), max(240, initial_h))
            router.resize_window(applied_w, applied_h)
        # Clipboard copy-out signals for THIS viewer: the XFixes monitor thread appends
        # small control strings here and the send loop below drains them. Register the queue
        # as a sink so a remote copy reaches this viewer.
        clip_queue: "deque[str]" = deque(maxlen=32)
        clip_sink = clip_queue.append  # one identity for register/unregister set membership
        _register_clip_sink(browser_id, display, clip_sink)
        # Keep the browser to one window pinned to the pane: re-pin against drags, close any
        # Ctrl+N / torn-out window. Its own X connection + thread, stopped via stop_event.
        guardian = WindowGuardian(browser_id, display, stop_event)
        guardian.start()
        # Audio is strictly additive (pcmflux importable AND this browser has a sink). It's
        # started lazily on the first RESUME (not here) so a connection that opens paused
        # doesn't spin up a capture just to stop it; the sender loop owns its lifecycle.
        audio_device = session.audio_capture_device if is_audio_available() else None

        # The conductor decides which single viewer streams; this connection starts PAUSED
        # and its sender loop reconciles the conductor's ``active`` flag against the pipe.
        def _wake() -> None:
            with pipe.condition:
                pipe.condition.notify_all()

        conn = StreamConnection(browser_id, _wake)
        receiver = threading.Thread(
            target=_receive_pump,
            kwargs={"ws": ws, "pipe": pipe, "router": router, "session": session,
                    "conn": conn, "stop_event": stop_event},
            name=f"browser-stream-recv-{browser_id}",
            daemon=True,
        )
        receiver.start()
        last_send = time.monotonic()
        last_tcpinfo = last_send
        last_memcheck = 0.0
        mem_high = False  # last high-memory state sent to this viewer (edge-triggered)
        while not stop_event.is_set():
            # Reconcile the conductor's desired state with the live pipe. Exactly one
            # viewer (across all browsers and devices) is active; the rest fully STOP the
            # capture -- ~0 CPU, 0 bandwidth -- and resume in ~40ms with fresh keyframes.
            want_active = conn.active.is_set()
            if want_active and pipe.is_paused:
                pipe.resume()
                ws.send("active")  # viewer clears its paused overlay
                last_send = time.monotonic()
            elif not want_active and not pipe.is_paused:
                pipe.pause()
                ws.send("paused")  # viewer shows its paused overlay over the frozen frame
                last_send = time.monotonic()
            # Audio follows the active state DIRECTLY, not the video pause->resume
            # transition above: the video pipe starts unpaused (videopipe._paused=False), so
            # a viewer that claims active on connect (the normal single-viewer case) makes
            # ``want_active`` true without the pipe ever having been paused -- the resume
            # branch never fires. Gating audio on that transition left such a viewer silent.
            # Reconcile it against ``want_active`` on its own so it starts whenever active.
            if want_active and audio_device and audio_pipe is None:
                candidate = AudioPipe(audio_device, pipe.condition)
                try:
                    candidate.start()
                    audio_pipe = candidate
                except AudioPipeError as error:
                    logger.warning("audio pipe unavailable for {} ({})", browser_id, error)
            elif not want_active and audio_pipe is not None:
                audio_pipe.stop()
                audio_pipe = None
            if pipe.is_paused:
                # Nothing to encode; block for a conductor wakeup and send NO heartbeat
                # (0 bandwidth -- the WS ping keeps the socket alive).
                pipe.next_packet(timeout=_HEARTBEAT_SECONDS)
                continue
            # Local-hop TCP health, ~2Hz (see browser.telemetry).
            if time.monotonic() - last_tcpinfo >= 0.5:
                last_tcpinfo = time.monotonic()
                info = telemetry.read_tcp_info(ws.sock)
                if info is not None:
                    telemetry.hub.emit(browser_id, {"type": "tcpinfo", **info})
            # Box-wide memory pressure -> nudge the viewer to close tabs. Edge-triggered so we
            # send only on a change; only reaches this active viewer (a paused one continues above).
            if time.monotonic() - last_memcheck >= _MEM_CHECK_INTERVAL:
                last_memcheck = time.monotonic()
                high = _memory_pressure_high()
                if high != mem_high:
                    mem_high = high
                    ws.send("mem,high" if high else "mem,ok")
                    last_send = time.monotonic()
            # A guardian (this browser's, any viewer) closed a Ctrl+N / torn-out window -> tell
            # the ACTIVE viewer why it vanished. Browser-wide signal, drained only here (active).
            if window_guardian.take_extra_closed(browser_id):
                ws.send("multiwin")
                last_send = time.monotonic()
            control_message = pipe.take_control_message()
            if control_message is not None:
                ws.send(control_message)  # ahead of any new-size stripe (single sender => ordered)
                last_send = time.monotonic()
            cursor_message = pipe.take_cursor_message()
            if cursor_message is not None:
                ws.send(cursor_message)
                last_send = time.monotonic()
            while clip_queue:
                ws.send(clip_queue.popleft())
                last_send = time.monotonic()
            audio_pending = None
            if audio_pipe is not None:
                audio_pending = audio_pipe.has_pending
                for chunk in audio_pipe.drain():
                    ws.send(chunk)
                    last_send = time.monotonic()
            packet = pipe.next_packet(timeout=_HEARTBEAT_SECONDS, has_extra=audio_pending)
            if packet is not None:
                ws.send(packet)
                last_send = time.monotonic()
            elif time.monotonic() - last_send >= _HEARTBEAT_SECONDS:
                ws.send("hb")
                last_send = time.monotonic()
    except ConnectionClosed:
        pass
    finally:
        stop_event.set()
        if conn is not None:
            conductor.leave(conn)
        telemetry.hub.emit(browser_id, {"type": "conn", "event": "close"})
        telemetry.hub.close(browser_id)
        if clip_sink is not None:
            _unregister_clip_sink(browser_id, clip_sink)
        if audio_pipe is not None:
            audio_pipe.stop()
        pipe.stop()
        if router is not None:
            router.close()
        if receiver is not None:
            receiver.join(timeout=5)
        if guardian is not None:
            guardian.join(timeout=5)  # stop_event already set above; it exits its next tick
        stream_slots.release(browser_id)
