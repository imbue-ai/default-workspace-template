"""Fleet media socket: pixelflux H.264 video to one viewer of one browser.

The pixel path here is copied VERBATIM from the streamed-browser prototype's ``/stream``
handler -- the newest-wins credit-ack pipe, the outbound send loop
(control message, out-of-band cursor, stripe packets), TCP_NODELAY (#21), the
permessage-deflate strip (#22), and the 250ms heartbeat the client's freeze
attributor keys on (#25). The ONLY adaptation is the fleet's multi-browser
reality: the caller resolves the browser by NAME and hands us its private display
(:N), so we open a capture pipe on THAT display instead of a single global
session.

The clipboard bridge is likewise ported verbatim from the streamed-browser prototype (XFixes
copy-out monitor + xclip paste-in via :mod:`browser.xclipboard`), adapted two
ways for the fleet: the state is keyed per browser (each has its own display and
possibly several viewers), and paste-IN is GATED on ``session.input_allowed`` so
only the controlling human can write into the browser -- copy-OUT (what's already
on the screen the human is watching) is ungated.

Audio is ported verbatim too: pcmflux Opus chunks (:mod:`browser.audiopipe`) ride
the SAME ``/stream`` socket as video, interleaved by a leading magic byte and
drained on the single sender thread. The fleet adaptation is that each browser
captures ITS OWN PulseAudio sink monitor (``session.audio_capture_device``), so
split-view browsers don't mix sound. Audio is strictly additive -- if pcmflux or
the sink is unavailable, video streams unchanged.
"""

import base64
import socket as socket_module
import threading
import time
from collections import deque
from typing import Any, Callable

from flask import Response, jsonify, request
from loguru import logger
from simple_websocket import ConnectionClosed

from browser import telemetry
from browser.audiopipe import AudioPipe, AudioPipeError
from browser.audiopipe import is_available as is_audio_available
from browser.videopipe import PixelfluxVideoPipe, VideoPipeError
from browser.xclipboard import ClipboardError, ClipboardMonitor, read_clipboard, set_clipboard
from browser.xinput import InputRouter

_RECEIVE_POLL_SECONDS = 0.05
# While a viewer is connected the server never goes silent longer than this: the
# client's freeze attributor needs "socket silent" to unambiguously mean transport
# (a starved server that encodes nothing would otherwise be misfiled as a stall).
_HEARTBEAT_SECONDS = 0.25

# Paste-in bodies (images) can be large; the runner caps the request body so a giant
# paste is rejected before it's read. 10 MiB matches xclipboard's read cap and the
# client-side pre-check (verbatim from the streamed-browser prototype).
_CLIPBOARD_MAX_BYTES = 10 * 1024 * 1024
# Text small enough to inline over the stream control channel; larger text and all
# images route through GET /clipboard/out so no >1 MiB WS frame tears down the video
# socket (verbatim threshold from the streamed-browser prototype).
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


def _on_remote_clipboard(browser_id: str, data: bytes, mime: str) -> None:
    """A copy happened in the remote browser (XFixes fired): stash it for the GET and
    signal every connected viewer of this browser. Small text inlines over the control
    channel; images and large text ride the GET so no >1 MiB WS frame can tear down the
    video socket (verbatim policy from the streamed-browser prototype)."""
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
    echo it back (verbatim from the streamed-browser prototype, plus the fleet's control gate)."""
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
    ws: Any, pipe: PixelfluxVideoPipe, router: InputRouter, session: Any, stop_event: threading.Event
) -> None:
    """Read credit acks, resize, and Selkies input on a dedicated thread until the
    socket closes.

    Ack (flow control) and resize handling are verbatim from the streamed-browser prototype. Live
    input (``router.handle`` -- Selkies kd/ku/kr/kh/m -> XTEST) is GATED on the browser's
    ``input_allowed`` (the thread-safe mirror of who holds control): a human's mouse/key
    is injected only while the human owns the browser, so a stale event can't land after
    an agent takes over. Release/heartbeat (``kr``/``kh``) always pass so held keys are
    freed on a flip even after the gate closes.
    """
    try:
        while not stop_event.is_set():
            data = ws.receive(timeout=_RECEIVE_POLL_SECONDS)
            if data is None or isinstance(data, bytes):
                continue
            if data.startswith("ack,"):
                try:
                    frame_id, y_start = data[4:].split(",")
                    pipe.ack(int(frame_id), int(y_start))
                except ValueError:
                    logger.warning("dropped malformed ack {!r}", data[:32])
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
                    router.resize_window(applied_w, applied_h)
            elif data.startswith("f,"):
                # Viewer visibility throttle: a hidden pane asks for ~1fps so the
                # encoder stops burning CPU while nobody watches; a shown pane asks for
                # the full rate. Ungated (it's the viewer's own render concern, not an
                # input action). The pipe clamps to [1, _CAPTURE_FPS].
                try:
                    pipe.set_target_fps(float(data[2:]))
                except ValueError:
                    logger.warning("dropped malformed fps {!r}", data[:32])
            elif data.startswith("kr") or data.startswith("kh"):
                router.handle(data)  # release-all / held-key heartbeat: always allowed
            elif session.input_allowed:
                # A real key/mouse event means a human is actively driving a VISIBLE pane,
                # so lift any visibility throttle to full rate -- a bulletproof un-stick that
                # doesn't depend on the client's IntersectionObserver firing (idempotent, so
                # it's a cheap no-op once already full).
                pipe.set_target_fps(float("inf"))
                router.handle(data)  # kd/ku/m: only while the human holds control
    except ConnectionClosed:
        pass
    finally:
        stop_event.set()


def serve_stream(ws: Any, browser_id: str, display: str, session: Any) -> None:
    """Serve one viewer of one already-running browser on its private ``display``.

    The outbound send loop is verbatim from the streamed-browser prototype's stream handler. ``session``
    is the LiveBrowser, read for ``input_allowed`` (the control gate for injected input) and
    ``audio_capture_device`` (this browser's PulseAudio monitor, or None if no audio).
    """
    _set_nodelay(ws)
    pipe = PixelfluxVideoPipe(browser_id, display)
    try:
        pipe.start()
    except VideoPipeError as error:
        logger.warning("video pipe failed to start for {} ({})", browser_id, error)
        ws.close(1011)
        return
    # Passive telemetry: begin recording for this browser (the hot paths emit into a
    # lock-free ring drained by the read-only /telemetry firehose; see browser.telemetry).
    telemetry.hub.open(browser_id)
    telemetry.hub.emit(browser_id, {"type": "conn", "event": "open"})
    router = InputRouter(display)
    # Cold-start size: the viewer passes its real pane size as ?w=&h= on the connect
    # URL, so the first emitted frame is already pane-sized -- no 1280x800 frame is
    # ever shown and no resize round-trip is needed (verbatim from the streamed-browser prototype).
    initial_w = request.args.get("w", type=int)
    initial_h = request.args.get("h", type=int)
    if initial_w is not None and initial_h is not None:
        applied_w, applied_h = pipe.set_capture_region(max(320, initial_w), max(240, initial_h))
        router.resize_window(applied_w, applied_h)
    # Clipboard copy-out signals for THIS viewer: the XFixes monitor thread appends
    # small control strings here and the send loop below drains them (deque append /
    # popleft are each thread-safe; a single sender keeps WS sends serialized). Register
    # the queue as a sink so a remote copy reaches this viewer (verbatim from the streamed-browser prototype).
    clip_queue: "deque[str]" = deque(maxlen=32)
    clip_sink = clip_queue.append  # one identity for register/unregister set membership
    _register_clip_sink(browser_id, display, clip_sink)
    # Audio is strictly additive: gate on pcmflux being importable AND this browser having
    # its own sink. It shares the video pipe's Condition so a fresh Opus chunk wakes the
    # same single sender thread (simple_websocket sends are not cross-thread safe). A start
    # failure leaves video untouched. Verbatim from the streamed-browser prototype, keyed to this browser.
    audio_pipe: "AudioPipe | None" = None
    audio_device = session.audio_capture_device
    if is_audio_available() and audio_device:
        candidate = AudioPipe(audio_device, pipe.condition)
        try:
            candidate.start()
            audio_pipe = candidate
        except AudioPipeError as error:
            logger.warning("audio pipe unavailable for {} ({}); streaming video only", browser_id, error)
    stop_event = threading.Event()
    receiver = threading.Thread(
        target=_receive_pump,
        kwargs={"ws": ws, "pipe": pipe, "router": router, "session": session, "stop_event": stop_event},
        name=f"browser-stream-recv-{browser_id}",
        daemon=True,
    )
    receiver.start()
    last_send = time.monotonic()
    last_tcpinfo = last_send
    audio_pending = audio_pipe.has_pending if audio_pipe is not None else None
    try:
        while not stop_event.is_set():
            # Local-hop TCP health, ~2Hz (rules out local buffering; the socket peers
            # with a local forwarder so it does NOT see WAN loss -- see browser.telemetry).
            if time.monotonic() - last_tcpinfo >= 0.5:
                last_tcpinfo = time.monotonic()
                info = telemetry.read_tcp_info(ws.sock)
                if info is not None:
                    telemetry.hub.emit(browser_id, {"type": "tcpinfo", **info})
            control_message = pipe.take_control_message()
            if control_message is not None:
                # Ahead of any new-size stripe (single sender thread => ordered).
                ws.send(control_message)
                last_send = time.monotonic()
            cursor_message = pipe.take_cursor_message()
            if cursor_message is not None:
                ws.send(cursor_message)
                last_send = time.monotonic()
            while clip_queue:
                ws.send(clip_queue.popleft())
                last_send = time.monotonic()
            if audio_pipe is not None:
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
        telemetry.hub.emit(browser_id, {"type": "conn", "event": "close"})
        telemetry.hub.close(browser_id)
        _unregister_clip_sink(browser_id, clip_sink)
        if audio_pipe is not None:
            audio_pipe.stop()
        pipe.stop()
        router.close()
        receiver.join(timeout=5)
