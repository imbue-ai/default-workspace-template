"""Streamed-browser web service: a full Chromium streamed as H.264 pixels.

Reached through the system_interface proxy at ``/service/streamed-browser/``.
Serves one viewer page (assets/index.html) and one WebSocket, ``/stream``,
carrying everything:

    out (binary)  pixelflux video frames (10-byte stripe header + Annex B),
                  and mono Opus audio chunks interleaved by a leading 0x01
                  magic byte (video packets lead with 0x04 -- see audiopipe)
    in  (text)    Selkies-grammar input (kd/ku/kr/m -- see xinput) plus our
                  credit acks (``ack,<frame_id>`` -- see videopipe)

The service owns ONE session (Xvfb + Chromium, see session.py), created lazily
on the first stream connect and kept alive across reconnects so page state and
the profile persist. Each connect gets a fresh capture pipe (the stream always
opens with SPS/PPS + IDR) and a fresh input router (whose close releases any
held keys/buttons, so a dropped viewer can't wedge the browser).

Flask + flask-sock, thread-per-connection -- the same shape as the other
workspace apps; there is no async world here at all.
"""

import base64
import os
from collections import deque
import socket as socket_module
import threading
import time
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, request
from flask_sock import Sock
from loguru import logger
from simple_websocket import ConnectionClosed
from werkzeug.serving import make_server

from streamed_browser.audiopipe import AudioPipe, AudioPipeError
from streamed_browser.audiopipe import is_available as is_audio_available
from streamed_browser.session import AUDIO_SOURCE_DEVICE, SessionStartupError, StreamedBrowserSession, is_chromium_installed
from streamed_browser.videopipe import PixelfluxVideoPipe, VideoPipeError
from streamed_browser.videopipe import is_available as is_pipe_available
from streamed_browser.xclipboard import ClipboardError, ClipboardMonitor, set_clipboard
from streamed_browser.xinput import InputRouter

_INDEX_HTML = Path(__file__).parent / "assets" / "index.html"
_PORT = int(os.environ.get("STREAMED_BROWSER_PORT", "8091"))
_SEND_POLL_SECONDS = 1.0
_RECEIVE_POLL_SECONDS = 0.05
# While a viewer is connected the server never goes silent longer than this:
# the client's freeze attributor needs "socket silent" to unambiguously mean
# transport (a starved server that encodes nothing would otherwise be
# misfiled as a network stall).
_HEARTBEAT_SECONDS = 0.25

application = Flask(__name__, static_folder=None)
application.config["SOCK_SERVER_OPTIONS"] = {"ping_interval": 25}
# Paste-in bodies (images) can be large; cap so a giant paste returns 413
# before the body is read into memory rather than wedging Chromium. 10 MiB
# matches xclipboard's read cap and the client pre-check.
_CLIPBOARD_MAX_BYTES = 10 * 1024 * 1024
application.config["MAX_CONTENT_LENGTH"] = _CLIPBOARD_MAX_BYTES
# Copied-out payload stashed for GET /clipboard/out (one slot, newest wins),
# and the monitor that fills it; both keyed to the live session.
_clip_lock = threading.Lock()
_clip_out: dict[str, object] = {"data": b"", "mime": "text/plain"}
_clip_monitor: ClipboardMonitor | None = None
# The current viewer's outbound control queue (small text signals to the
# client); set on connect, cleared on disconnect.
_clip_sink: dict[str, object] = {"send": None}
# Text small enough to inline over the WS control channel; larger text and all
# images route through GET /clipboard/out to stay well under the 1 MiB frame
# cap that would otherwise tear down the video socket.
_CLIP_INLINE_TEXT_MAX = 200 * 1024
sock = Sock(application)


def _strip_websocket_compression() -> None:
    """Drop the client's permessage-deflate offer before the handshake.

    simple_websocket accepts the extension unconditionally and flask-sock
    exposes no off switch, so without this every already-compressed H.264
    stripe would be zlib-deflated here and inflated by the viewer -- pure
    latency and CPU waste on incompressible data. flask_sock builds its
    handshaking Server from ``request.environ`` inside the route, so a
    before_request hook is early enough.
    """
    request.environ.pop("HTTP_SEC_WEBSOCKET_EXTENSIONS", None)


application.before_request(_strip_websocket_compression)

session = StreamedBrowserSession()


def index() -> Response:
    return Response(_INDEX_HTML.read_text(), mimetype="text/html")


def health() -> Response:
    return jsonify(
        {
            "status": "ok",
            "session_healthy": session.is_healthy,
            "chromium_installed": is_chromium_installed(),
            "pipe_available": is_pipe_available() or session.is_healthy,
        }
    )


def _receive_pump(ws: Any, pipe: PixelfluxVideoPipe, router: InputRouter, stop_event: threading.Event) -> None:
    """Read acks and input on a dedicated thread until the socket closes."""
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
            else:
                router.handle(data)
    except ConnectionClosed:
        pass
    finally:
        stop_event.set()


def _set_nodelay(ws: Any) -> None:
    """Interactive stream: never let Nagle hold a stripe or input tail."""
    try:
        ws.sock.setsockopt(socket_module.IPPROTO_TCP, socket_module.TCP_NODELAY, 1)
    except OSError as error:
        logger.debug("could not set TCP_NODELAY on stream socket ({})", error)


def _on_remote_clipboard(data: bytes, mime: str) -> None:
    """A copy happened in the remote browser (XFixes fired): stash it for the
    GET, and signal the connected viewer. Small text inlines over the control
    channel; images and large text ride the GET so no >1 MiB WS frame can tear
    down the video socket."""
    with _clip_lock:
        _clip_out["data"] = data
        _clip_out["mime"] = mime
        send = _clip_sink["send"]
        if send is None:
            return
        if mime.startswith("text/") and len(data) <= _CLIP_INLINE_TEXT_MAX:
            send("clip,txt," + base64.b64encode(data).decode("ascii"))
        else:
            send(f"clip,out,{mime},{len(data)}")


def _ensure_clipboard_monitor(display: str) -> None:
    """Start (once per session display) the XFixes copy-out monitor."""
    global _clip_monitor
    with _clip_lock:
        if _clip_monitor is not None and _clip_monitor.display == display:
            return
        if _clip_monitor is not None:
            _clip_monitor.close()
        _clip_monitor = ClipboardMonitor(display, _on_remote_clipboard)
    _clip_monitor.start()


def clipboard_paste() -> Response:
    """Paste-in: set the remote CLIPBOARD from the POST body, then inject Ctrl+V.

    Flask already rejected bodies over MAX_CONTENT_LENGTH with 413 before this
    runs, so the payload is bounded. The X selection is set BEFORE the paste
    keystroke, and clipboard sets are recorded so the copy-out monitor doesn't
    echo them back."""
    with _clip_lock:
        display = _clip_sink["display"]
        router = _clip_sink["router"]
    if display is None or router is None:
        return jsonify({"error": "no active session"}), 409
    data = request.get_data()
    if not data:
        return jsonify({"error": "empty clipboard"}), 400
    mime = (request.headers.get("Content-Type") or "text/plain").split(";")[0].strip()
    try:
        set_clipboard(display, data, mime)
        if _clip_monitor is not None:
            _clip_monitor.note_written(data)
        router.paste()
    except ClipboardError as error:
        logger.warning("clipboard paste failed ({})", error)
        return jsonify({"error": "paste failed"}), 500
    return jsonify({"ok": True})


def clipboard_out() -> Response:
    """Copy-out: the bytes of the last remote copy, in their native mime."""
    with _clip_lock:
        data = _clip_out["data"]
        mime = _clip_out["mime"]
    return Response(data, mimetype=mime)


def stream_socket(ws: Any) -> None:
    """One viewer: bring the session up, then pump frames out and input in."""
    _set_nodelay(ws)
    try:
        display = session.ensure_started()
    except SessionStartupError as error:
        logger.warning("streamed browser session failed to start ({})", error)
        ws.close(1013)  # retryable: installs may still be converging
        return
    pipe = PixelfluxVideoPipe("streamed-browser", display)
    try:
        pipe.start()
    except VideoPipeError as error:
        logger.warning("video pipe failed to start ({})", error)
        ws.close(1011)
        return
    router = InputRouter(display)
    # Cold-start size: the viewer passes its real pane size as ?w=&h= on the
    # connect URL, so the first emitted frame is already pane-sized -- no
    # 1280x800 frame is ever shown and no resize round-trip is needed. Carrying
    # it IN the connect (rather than a post-connect `r,` message) is what makes
    # it race-free: the size arrives with the socket, not on a timer that can
    # fire before the socket is open. Clamp like the live resize path does.
    initial_w = request.args.get("w", type=int)
    initial_h = request.args.get("h", type=int)
    if initial_w is not None and initial_h is not None:
        applied_w, applied_h = pipe.set_capture_region(max(320, initial_w), max(240, initial_h))
        router.resize_window(applied_w, applied_h)
    _ensure_clipboard_monitor(display)
    clip_queue: deque[str] = deque(maxlen=32)
    with _clip_lock:
        _clip_sink["send"] = clip_queue.append
        _clip_sink["display"] = display
        _clip_sink["router"] = router
    # Audio is strictly additive: gate on pcmflux being importable AND the
    # session's null sink existing. A start failure leaves video untouched.
    # It shares the video pipe's Condition so a fresh Opus chunk wakes the same
    # single sender thread (simple_websocket sends are not cross-thread safe).
    audio_pipe: AudioPipe | None = None
    if is_audio_available() and session.audio_available:
        candidate = AudioPipe(AUDIO_SOURCE_DEVICE, pipe.condition)
        try:
            candidate.start()
            audio_pipe = candidate
        except AudioPipeError as error:
            logger.warning("audio pipe unavailable ({}); streaming video only", error)
    stop_event = threading.Event()
    receiver = threading.Thread(
        target=_receive_pump,
        kwargs={"ws": ws, "pipe": pipe, "router": router, "stop_event": stop_event},
        name="streamed-browser-receive",
        daemon=True,
    )
    receiver.start()
    last_send = time.monotonic()
    audio_pending = audio_pipe.has_pending if audio_pipe is not None else None
    try:
        while not stop_event.is_set():
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
        with _clip_lock:
            if _clip_sink["send"] == clip_queue.append:
                _clip_sink["send"] = None
                _clip_sink["display"] = None
                _clip_sink["router"] = None
        if audio_pipe is not None:
            audio_pipe.stop()
        pipe.stop()
        router.close()
        receiver.join(timeout=5)


application.add_url_rule("/", view_func=index, methods=["GET"])
application.add_url_rule("/health", view_func=health, methods=["GET"])
application.add_url_rule("/clipboard/paste", view_func=clipboard_paste, methods=["POST"])
application.add_url_rule("/clipboard/out", view_func=clipboard_out, methods=["GET"])
sock.route("/stream")(stream_socket)


def main() -> None:
    server = make_server("127.0.0.1", _PORT, application, threaded=True)
    logger.info("streamed-browser service listening on {}", _PORT)
    server.serve_forever()
