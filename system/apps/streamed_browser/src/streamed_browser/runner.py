"""Streamed-browser web service: a full Chromium streamed as H.264 pixels.

Reached through the system_interface proxy at ``/service/streamed-browser/``.
Serves one viewer page (assets/index.html) and one WebSocket, ``/stream``,
carrying everything:

    out (binary)  pixelflux video frames (10-byte stripe header + Annex B)
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

import os
import threading
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify
from flask_sock import Sock
from loguru import logger
from simple_websocket import ConnectionClosed
from werkzeug.serving import make_server

from streamed_browser.session import SessionStartupError, StreamedBrowserSession, is_chromium_installed
from streamed_browser.videopipe import PixelfluxVideoPipe, VideoPipeError
from streamed_browser.videopipe import is_available as is_pipe_available
from streamed_browser.xinput import InputRouter

_INDEX_HTML = Path(__file__).parent / "assets" / "index.html"
_PORT = int(os.environ.get("STREAMED_BROWSER_PORT", "8091"))
_SEND_POLL_SECONDS = 1.0
_RECEIVE_POLL_SECONDS = 0.05

application = Flask(__name__, static_folder=None)
application.config["SOCK_SERVER_OPTIONS"] = {"ping_interval": 25}
sock = Sock(application)

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
                    pipe.ack(int(data[4:]))
                except ValueError:
                    logger.warning("dropped malformed ack {!r}", data[:32])
            else:
                router.handle(data)
    except ConnectionClosed:
        pass
    finally:
        stop_event.set()


def stream_socket(ws: Any) -> None:
    """One viewer: bring the session up, then pump frames out and input in."""
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
    stop_event = threading.Event()
    receiver = threading.Thread(
        target=_receive_pump,
        kwargs={"ws": ws, "pipe": pipe, "router": router, "stop_event": stop_event},
        name="streamed-browser-receive",
        daemon=True,
    )
    receiver.start()
    try:
        while not stop_event.is_set():
            packet = pipe.next_packet(timeout=_SEND_POLL_SECONDS)
            if packet is not None:
                ws.send(packet)
    except ConnectionClosed:
        pass
    finally:
        stop_event.set()
        pipe.stop()
        router.close()
        receiver.join(timeout=5)


application.add_url_rule("/", view_func=index, methods=["GET"])
application.add_url_rule("/health", view_func=health, methods=["GET"])
sock.route("/stream")(stream_socket)


def main() -> None:
    server = make_server("127.0.0.1", _PORT, application, threaded=True)
    logger.info("streamed-browser service listening on {}", _PORT)
    server.serve_forever()
