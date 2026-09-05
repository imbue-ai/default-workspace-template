"""Live-browser fleet web service: spawn headful Chromium, stream it, hand agents gated CDP.

Served at its own workspace origin (``browser.host-<hex>.localhost`` locally;
share hostnames follow the same prefix rule). Serves one
self-contained viewer page (assets/index.html) that renders a streamed browser
and an "Agent has control" overlay. The page talks over two WebSockets: the media
plane ``/browsers/{name}/stream`` (pixelflux H.264 pixels + Opus audio out; XTEST
input, resize, and attention (interact/hidden) in) and the control plane ``/browsers/{name}/cast``
(control/ownership state out; take/return-control in). Browsers are addressed by NAME
(a random ~2-word english name like ``alex-smith``), not a sequential int; there is
no default browser.

Agents drive the fleet over HTTP (see the ``agentic-browser-fleet`` CLI):

* ``GET  /browsers``            -- list every browser, its owner, and its tabs.
* ``POST /browsers``            -- start a new browser (body ``{"name": ...}`` optional;
  returns ``{"name": ...}``). 400 invalid name, 409 duplicate name or fleet full.
* ``GET  /browsers/{name}/attach`` -- the gated CDP URL to point `playwright-cli` at.
* ``POST /browsers/{name}/acquire`` -- reserve a browser (and get the exit code an agent
  branches on; see ``fleet._render_action``).
* ``POST /browsers/{name}/release`` -- give a browser back (only its owner can).

The workspace shell reads the same fleet through the instances API of the workspace app
model (``/_instances``; see ``browser.instances``), mounted on this app because the
daemon serves its own origin: one instance per browser, ``working`` while an agent holds
it, ``idle`` otherwise, ``error`` once crashed; ``new`` creates, delete closes, location
navigates the active tab. Every fleet event nudges the shell (``browser.bridged_fleet``).

The service does NOT drive browsers. Agents drive with ``@playwright/cli`` over the
gated CDP endpoint in cdp_proxy.py, which enforces the ownership lease per frame.

ARCHITECTURE: this is a synchronous Flask + flask-sock service (thread-per-
connection, served by a threaded Werkzeug HTTP/1.1 server). The CDP client, the CDP
proxy, and the per-browser ownership state machine in session.py are all async and
run on ONE background asyncio event loop, quarantined behind a
single :class:`~browser.loop_bridge.AsyncLoopBridge`. Every route handler reaches
the async world only through ``bridge.run(coro)`` (blocking) or ``bridge.submit``
(fire-and-forget, returns the in-loop asyncio.Task). This mirrors the proven
Flask+WS pattern in system/apps/system_interface. The service owns its origin, so
the viewer's relative URLs need no prefix or root-path awareness anywhere.
"""

import json
import os
import queue
import signal
import threading
from pathlib import Path
from types import FrameType
from typing import Any

from app_instances.blueprint import build_instances_blueprint
from app_instances.nudge import ShellNudger, ThreadedNudger, shell_base_url
from flask import Flask, Response, jsonify, request
from flask_sock import Sock
from loguru import logger
from simple_websocket import ConnectionClosed

from browser import mediastream, telemetry
from browser.bridged_fleet import BridgedFleet, ManagerNudger
from browser.cdp_proxy import ProxyServer
from browser.errors import UnknownBrowserError
from browser.instances import FleetInstanceSource
from browser.loop_bridge import AsyncLoopBridge
from browser.names import is_valid_browser_name
from browser.oom_retag import start_oom_retagging
from browser.primitives import APP_NAME
from browser.session import (
    BrowserSessionManager,
    BrowserStartupError,
    DuplicateBrowserNameError,
    FleetFullError,
    InvalidBrowserNameError,
    LiveBrowser,
    deferred_install_ready,
    set_proxy_server,
)
from browser.wsgi import make_threaded_server

# The agent-facing CDP proxy port. Fixed by default so an attach URL an agent already
# holds keeps resolving across a service restart; 0 picks an ephemeral port (tests).
_PROXY_PORT = int(os.environ.get("BROWSER_CDP_PROXY_PORT", "8083"))

_INDEX_HTML = Path(__file__).parent / "assets" / "index.html"

# Errors raised when Chromium can't be launched (install not finished, CDP failure).
# CDP failures surface as these built-ins.
_STARTUP_ERRORS = (BrowserStartupError, RuntimeError, OSError, ConnectionError)

# How long a state-changing route's bridge.run waits before giving up and (via the
# bridge) cancelling the orphaned coroutine. The acquire/hold/task streaming paths
# legitimately block until granted/disconnected and pass timeout=None instead.
_ROUTE_TIMEOUT = float(os.environ.get("BROWSER_ROUTE_TIMEOUT", "120"))

# Outbound-drain / inbound-poll cadence for the cast handler and the NDJSON
# generators. The 0.5s NDJSON poll both flushes a heartbeat (so a dead client
# surfaces as a write failure in bounded time) and re-checks the run's state.
_NDJSON_POLL_SECONDS = 0.5
_CAST_OUTBOUND_POLL_SECONDS = 1.0
_CAST_INBOUND_POLL_SECONDS = 0.05

# Application WebSocket close code (private-use 4000-4999 range) for a /cast or /stream
# socket whose browser is gone because it was CLOSED by an agent (or is a stale
# layout-restored tab of one). The viewer renders the terminal "terminated by an agent"
# overlay and stops reconnecting -- distinct from 1008 (failed/invalid) and 1013 (retry).
_WS_CLOSE_TERMINATED = 4001

# The ONE sync<->async boundary: every route reaches the async world through this
# bridge's single background loop (see browser.loop_bridge). The manager and all
# LiveBrowsers are constructed/driven on that loop, so their asyncio locks/events
# keep their cooperative single-threaded meaning.
bridge = AsyncLoopBridge()
manager = BrowserSessionManager()

application = Flask(__name__, static_folder=None)
application.config["SOCK_SERVER_OPTIONS"] = {"ping_interval": 25}
# Clipboard paste-in bodies carry raw image bytes over HTTP (the WS proxy's ~1 MiB cap
# is why clipboard rides HTTP, not the stream socket). Bound it so a giant paste is
# rejected before it's read into memory rather than wedging Chromium.
application.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024
sock = Sock(application)

# Init gate: cleared at import, set when startup restore finishes (always, even on
# failure -- see _startup). State-changing routes return 503 "initializing" until
# then; read-only routes (state/ls/health) stay open so the user can watch the
# fleet come back. A threading.Event (not asyncio.Event) because it is read on
# Flask threads and set from the loop's _startup finally -- both thread-safe.
_init_done = threading.Event()
# The startup status is written once per phase on the loop thread (``_startup``) and
# read on Flask route threads (``init_status``). A small lock guards both sides so a
# reader always sees one fully-consistent dict (never a torn phase/error combination),
# rather than relying on CPython name-rebind atomicity. The single-element holder lets
# the lock-guarded functions swap the value without the ``global`` keyword. See
# _publish_init_status / _read_init_status.
_init_status_lock = threading.Lock()
_init_status_holder: list[dict[str, Any]] = [{"phase": "initializing"}]


def _publish_init_status(status: dict[str, Any]) -> None:
    """Publish the startup status under the lock (called on the loop thread)."""
    with _init_status_lock:
        _init_status_holder[0] = status


def _read_init_status() -> dict[str, Any]:
    """Snapshot the startup status under the lock (called on a Flask reader thread)."""
    with _init_status_lock:
        return _init_status_holder[0]


def _error(payload: dict[str, Any], status: int) -> Response:
    """A JSON error Response with an explicit status code (Flask-idiomatic single value)."""
    response = jsonify(payload)
    response.status_code = status
    return response


def _require_ready() -> Response | None:
    """503 while the fleet is still restoring saved browsers; None once ready."""
    if not _init_done.is_set():
        return _error(
            {
                "error": "Browser fleet is still restoring your saved browsers; try again in a moment.",
                "status": "initializing",
            },
            503,
        )
    return None


async def _startup() -> None:
    """Restore the saved fleet (eager-sequential) behind the init gate. The gate is
    ALWAYS opened in ``finally`` so a restore failure can never wedge the daemon shut.

    Runs as a coroutine on the bridge loop (launched via ``bridge.submit`` at app
    construction), so it does not block read-only routes from serving immediately.
    """
    try:
        ready, reason = deferred_install_ready()
        if not ready:
            # Chromium isn't installed yet; don't block. The fleet starts empty and the
            # first create() (once the install marker appears) brings a browser up.
            _publish_init_status({"phase": "waiting_for_chromium", "reason": reason})
            return
        await manager.restore()
        _publish_init_status({"phase": "ready"})
    except _STARTUP_ERRORS as e:
        logger.error("browser fleet restore failed ({}); serving an empty fleet", e)
        _publish_init_status({"phase": "ready", "error": str(e)})
    finally:
        # Always run the periodic manifest checkpoint -- including the waiting-for-
        # chromium and restore-failed paths, where the fleet comes up lazily later;
        # otherwise tab-URL drift would never be persisted for the daemon's lifetime.
        manager.start_checkpointing()
        # threading.Event.set is thread-safe: this runs on the loop thread, readers
        # are Flask threads.
        _init_done.set()


def _ndjson(event: dict[str, Any]) -> str:
    return json.dumps(event, default=str) + "\n"


def _resolve_sync(browser_id: str) -> "LiveBrowser | Response":
    """Resolve a browser on the loop, turning an unknown name into 404 / startup errors into 503."""
    try:
        return bridge.run(manager.resolve(browser_id), timeout=_ROUTE_TIMEOUT)
    except UnknownBrowserError as e:
        return _error({"error": str(e)}, 404)
    except _STARTUP_ERRORS as e:
        return _error({"error": f"Could not start browser {browser_id}: {e}"}, 503)


def _agent_identity() -> tuple[str | None, str | None]:
    # ADVISORY ONLY: these are client-set headers, so a local caller can present any agent id.
    # They drive ownership/accountability among cooperating in-container agents (all the same
    # user -- one trust domain), NOT authentication. The cross-ORIGIN boundary (a web page riding
    # the user's cookie) is enforced upstream by the system_interface proxy's same-origin check;
    # a non-spoofable per-agent identity would require a token minted by the proxy/manager, which
    # this daemon has no way to verify today.
    return request.headers.get("x-mngr-agent-id"), request.headers.get("x-mngr-agent-name")


def _body() -> dict[str, Any]:
    return request.get_json(silent=True) or {}


# --- read-only routes (no init gate) -----------------------------------------


def index() -> Response:
    return Response(_INDEX_HTML.read_text(), mimetype="text/html")


def health() -> Response:
    return jsonify({"status": "ok", "initializing": not _init_done.is_set()})


def init_status() -> Response:
    """Restore progress: phase is initializing / waiting_for_chromium / ready."""
    return jsonify(_read_init_status())


def list_browsers() -> Response:
    """List the fleet (read-only; works during init). The fleet starts EMPTY -- there is
    no default browser, so nothing is materialized here.

    Also reports whether 'New browser' can run right now (``can_create`` + ``create_reason``
    + count/max) so the UI can gate its button -- mirroring what ``create_browser`` enforces.
    ``can_create`` is NOT gated on ``_init_done``: create works DURING restore (it queues
    behind the serialized relaunches), so the button must stay enabled during init. Only
    a missing Chromium install or the cap disables it."""
    ready, install_reason = deferred_install_ready()
    # capacity() reads the manager's _browsers dict, which is mutated on the loop
    # thread; reading it directly from this Flask worker thread can KeyError mid
    # iteration. Route it through the bridge so the read runs ON the loop thread,
    # like every other manager-state access here.
    count, cap = bridge.run(manager.capacity_async(), timeout=_ROUTE_TIMEOUT)
    if not ready:
        can_create, create_reason = False, install_reason or "installing browser support"
    elif count >= cap:
        can_create, create_reason = False, f"{count}/{cap} browsers open -- close one first"
    else:
        can_create, create_reason = True, ""
    return jsonify(
        {
            "browsers": bridge.run(manager.list_browsers(), timeout=_ROUTE_TIMEOUT),
            "can_create": can_create,
            "create_reason": create_reason,
            "browser_count": count,
            "browser_max": cap,
        }
    )


# --- state-changing routes (init-gated) --------------------------------------


def create_browser() -> Response:
    """Register a new browser and return its name IMMEDIATELY (the Chromium launch runs
    in the background).

    NOT init-gated: create works DURING restore. ``manager.create`` registers the browser
    in ``init`` under ``manager._lock`` (cap check + name resolution + insert -- all fast,
    no Chromium launch) and returns at once, kicking the serialized launch off as a
    background task. So this route does NOT block on (or time out against) the multi-second
    launch -- the optimistic viewer pane finds the registered browser the instant it
    connects and watches it flip from ``init`` to ``running`` over the cast socket. The
    background launch persists the manifest itself once the browser is ``running``. The
    only hard pre-check is that Chromium is installed (else nothing to launch -> 503).

    Body ``{"name": "<name>"}`` is optional; omitted -> the first free ``browser-<N>``
    is minted (the canonical form of the "Browser N" display name the UI derives).
    Response ``{"name": <chosen-name>}``. Errors: 400 invalid name, 409 duplicate name or
    fleet full, 503 Chromium installing. The attach URL is NOT returned here: the launch is
    still in flight, so the CLI polls for it (see ``fleet.cmd_new``)."""
    ready, reason = deferred_install_ready()
    if not ready:
        return _error({"error": reason}, 503)
    name = _body().get("name")
    try:
        # Returns fast: registers init + spawns the serialized launch on the loop.
        session = bridge.run(manager.create(name), timeout=_ROUTE_TIMEOUT)
    except InvalidBrowserNameError as e:
        return _error({"error": str(e)}, 400)
    except (DuplicateBrowserNameError, FleetFullError) as e:
        return _error({"error": str(e)}, 409)
    except _STARTUP_ERRORS as e:
        logger.error("failed to register browser: {}", e)
        return _error({"error": f"Could not start browser: {e}"}, 503)
    return jsonify({"name": session.browser_id})


def close_browser(browser_id: str) -> Response:
    if (gate := _require_ready()) is not None:
        return gate
    # Validate the name before it reaches manager.close / forget_profile_dir, which build a
    # filesystem path from it and rmtree it. The route converter already rejects encoded
    # slashes, but an explicit guard is the real defense against a crafted id escaping the
    # profile directory (defense in depth for the delete path).
    if not is_valid_browser_name(browser_id):
        return jsonify({"error": "invalid browser name"}), 404
    bridge.run(manager.close_and_forget(browser_id), timeout=_ROUTE_TIMEOUT)
    return jsonify({"closed": True})


def release_browser(browser_id: str) -> Response:
    if (gate := _require_ready()) is not None:
        return gate
    agent_id, _ = _agent_identity()
    if not agent_id:
        return _error({"error": "X-Mngr-Agent-Id header required"}, 400)
    resolved = _resolve_sync(browser_id)
    if isinstance(resolved, Response):
        return resolved
    return jsonify({"released": bridge.run(resolved.release(agent_id), timeout=_ROUTE_TIMEOUT)})


def _direct_target(
    browser_id: str, gated: bool = True
) -> "tuple[LiveBrowser, str, str | None] | Response":
    """Resolve (browser, agent_id, agent_name) for an ownership command, or an error Response.

    ``gated`` (default True) blocks the command with 503 "initializing" while the fleet is
    still restoring. Note this is the ONLY place agent identity is read: it comes from the
    ``X-Mngr-Agent-Id`` header the fleet CLI sets. A raw CDP client sends no such header,
    which is exactly why the proxy authenticates with a capability token instead.
    """
    if gated and (gate := _require_ready()) is not None:
        return gate
    agent_id, agent_name = _agent_identity()
    if not agent_id:
        return _error({"error": "X-Mngr-Agent-Id header required"}, 400)
    resolved = _resolve_sync(browser_id)
    if isinstance(resolved, Response):
        return resolved
    return resolved, agent_id, agent_name


def cmd_attach(browser_id: str) -> Response:
    """Issue this agent an attach URL, or say why it can't have one.

    Separate from ``POST /browsers`` because create returns while Chromium is still
    launching -- the token only exists once the process is up, so the CLI polls this.

    Goes through ``_direct_target`` for the agent identity: the token is minted FOR one
    agent, and this route is the last place that identity is visible (the proxy sees a
    generic CDP client with no header). Handing the live token to any caller would make
    agent-vs-agent exclusion unenforceable.
    """
    target = _direct_target(browser_id)
    if isinstance(target, Response):
        return target
    session, agent_id, agent_name = target
    return jsonify(bridge.run(session.attach_for(agent_id, agent_name), timeout=_ROUTE_TIMEOUT))


def cmd_acquire(browser_id: str) -> Response:
    """Explicitly reserve a browser across a run of commands (optional; the first
    command auto-acquires). ``--reclaim`` takes it back from a human who said 'keep going'."""
    target = _direct_target(browser_id)
    if isinstance(target, Response):
        return target
    session, agent_id, agent_name = target
    body = _body()
    # acquire AND read the control-state snapshot in ONE on-loop coroutine: the snapshot
    # reads loop-mutated ownership fields, so it must run on the loop (via the bridge),
    # not directly on this Flask thread (finding [4]).
    result = bridge.run(
        session.acquire_with_state(
            agent_id, agent_name,
            reclaim=bool(body.get("reclaim", False)),
            # `acquire` is the fast reserve-or-queue verb: it never blocks. A busy browser
            # enqueues the agent (woken when it frees) and returns immediately -- matching
            # what the CLI tells the agent ("you're queued ... messaged when it frees").
            # Blocking-wait lives in task/hold, which heartbeat and so detect a dropped
            # client; honoring wait=True on this non-streaming POST would pin a Flask
            # worker thread + a queue slot forever on a caller that walked away.
            wait=False,
            enqueue_on_busy=True,
        ),
        timeout=_ROUTE_TIMEOUT,
    )
    return jsonify(result)


def cmd_handoff(browser_id: str) -> Response:
    """Agent hands this browser to the human (e.g. a CAPTCHA it can't solve). The agent
    must currently hold it; it's put at the FRONT of the resume queue and control goes to
    the human, pinned, until they hand back -- then this agent resumes first."""
    target = _direct_target(browser_id)
    if isinstance(target, Response):
        return target
    session, agent_id, agent_name = target
    body = _body()
    reason = str(body.get("reason", "")).strip() or "human verification needed"
    # handoff AND its control-state snapshot in ONE on-loop coroutine (finding [4]): the
    # snapshot reads loop-mutated ownership fields, so it must not run on the Flask thread.
    result = bridge.run(session.handoff_with_state(agent_id, agent_name, reason), timeout=_ROUTE_TIMEOUT)
    return jsonify(result)


def cmd_clipboard_paste(browser_id: str) -> Response:
    """Human viewer pastes their local clipboard into the browser. Body is the raw
    clipboard bytes; Content-Type is the mime (text/plain or image/*). The paste is
    gated on human control inside the media layer (an agent mid-task can't have a stray
    paste land). Keyed per browser."""
    resolved = _resolve_sync(browser_id)
    if isinstance(resolved, Response):
        return resolved
    data = request.get_data()
    mime = (request.content_type or "text/plain").split(";")[0].strip() or "text/plain"
    return mediastream.clipboard_paste(browser_id, resolved, data, mime)


def cmd_clipboard_out(browser_id: str) -> Response:
    """Copy-out: the bytes of the last remote copy on this browser, native mime. Gated on
    human control (like paste-in) -- a copy-out can carry a secret the human just copied
    (a password, a 2FA code), so only the party currently holding control may read it, not
    an idle agent. GET /clipboard/out, keyed per browser."""
    resolved = _resolve_sync(browser_id)
    if isinstance(resolved, Response):
        return resolved
    if not resolved.input_allowed:
        return jsonify({"error": "clipboard is readable only while you hold control"}), 403
    return mediastream.clipboard_out(browser_id)


# --- control/ownership WebSocket (/cast) -------------------------------------


def _cast_inbound_pump(
    ws: Any, session: LiveBrowser, stop_event: threading.Event
) -> None:
    """Read inbound cast messages on a dedicated thread until the socket closes.

    Inbound (client->loop) and outbound (loop->client) are handled by two threads
    (this one reads; the handler's main thread drains the outbound control queue and
    sends), so a slow inbound poll never stalls the outbound control broadcasts and vice
    versa -- the head-of-line blocking a single interleaved poll would cause. simple-
    websocket supports send and receive from different threads. Each inbound JSON message
    is dispatched to the loop via the bridge; commands are skipped while initializing
    (a human can't grab a half-restored fleet).
    """
    try:
        while not stop_event.is_set():
            data = ws.receive(timeout=_CAST_INBOUND_POLL_SECONDS)
            if data is None:
                continue  # poll timeout; re-check the stop flag and keep reading
            if not _init_done.is_set():
                continue  # the view streams read-only until the gate opens
            try:
                message = json.loads(data)
            except (ValueError, TypeError):
                continue
            # /cast carries ONLY ownership control now (pixels + input ride /stream).
            kind = message.get("type")
            if kind == "take_control":
                bridge.run(session.take_control(), timeout=_ROUTE_TIMEOUT)
            elif kind == "return_to_agents":
                bridge.run(session.return_to_agents(), timeout=_ROUTE_TIMEOUT)
    except ConnectionClosed:
        pass
    finally:
        stop_event.set()


def cast_socket(ws: Any, browser_id: str) -> None:
    """Bridge one cast WebSocket: outbound control/ownership state + inbound take/return.

    Runs in its own Flask thread (thread-per-connection). The browser registers an
    outbound ``queue.Queue`` on the loop; ``LiveBrowser._broadcast`` (on the loop)
    pushes JSON control messages onto it and this handler drains and sends them. A second
    thread reads inbound messages so neither direction blocks the other. Pixels and audio
    ride the separate ``/stream`` socket, not this one.
    """
    resolved = _resolve_sync_for_ws(browser_id)
    if resolved is None:
        _close_unresolved_ws(ws, browser_id)
        return
    session = resolved
    if not mediastream.cast_slots.reserve(browser_id):
        ws.close(1013)  # per-browser cast cap reached; retryable
        return
    # The reserved slot MUST be released on every exit -- including a failure in the
    # register/seed/thread-start setup below. Guard the whole post-reserve body so an
    # exception there can't leak the slot (8 leaks -> the browser can never be cast again
    # until restart); the inner try owns the queue/thread cleanup once they exist.
    try:
        # Register + seed the initial control/tabs sync atomically on the loop, so no
        # live frame can interleave ahead of the state the viewer needs first. The lifecycle
        # is captured in the same on-loop step so the initializing banner below is consistent
        # with the seed.
        client_queue, lifecycle = bridge.run(session.register_cast_queue_with_lifecycle(), timeout=_ROUTE_TIMEOUT)
        if not _init_done.is_set() and lifecycle != "running":
            # The fleet is still restoring AND this browser isn't up yet: tell the viewer, so
            # it shows a banner and clears it on the first live frame/control once this browser
            # is up. A viewer joining an already-running browser is NOT told initializing
            # (finding [3-runner]) -- its seed already carries lifecycle=running and the live
            # page is streaming, so an initializing banner would be a false "still starting".
            # put_nowait is safe: the queue is fresh with at most a few seed messages and its
            # maxsize is far larger (finding [8]).
            client_queue.put_nowait(json.dumps({"type": "initializing"}))
        stop_event = threading.Event()
        inbound = threading.Thread(
            target=_cast_inbound_pump,
            kwargs={"ws": ws, "session": session, "stop_event": stop_event},
            name=f"browser-cast-inbound-{browser_id}",
            daemon=True,
        )
        inbound.start()
        try:
            while not stop_event.is_set():
                try:
                    message = client_queue.get(timeout=_CAST_OUTBOUND_POLL_SECONDS)
                except queue.Empty:
                    continue
                if message is None:
                    break  # shutdown sentinel
                ws.send(message)
        except ConnectionClosed:
            pass
        finally:
            stop_event.set()
            inbound.join(timeout=5)
            bridge.run(session.unregister_cast_queue(client_queue), timeout=_ROUTE_TIMEOUT)
    finally:
        mediastream.cast_slots.release(browser_id)


def _resolve_sync_for_ws(browser_id: str) -> "LiveBrowser | None":
    """Resolve a browser for the cast socket; None for an unknown name or a startup error."""
    try:
        return bridge.run(manager.resolve(browser_id), timeout=_ROUTE_TIMEOUT)
    except (UnknownBrowserError, *_STARTUP_ERRORS):
        return None


def _send_terminal_signal(ws: Any, message: dict[str, Any]) -> None:
    """Deliver a terminal control message as a WS TEXT frame, just BEFORE the socket closes.

    The close CODE alone is NOT a reliable terminal signal on this server: the daemon serves
    flask-sock over werkzeug's dev server, which -- when a handler returns right after an
    explicit ``ws.close(code)`` -- writes a trailing HTTP response onto the already-hijacked
    socket. That corrupts the close handshake, so the browser's WebSocket reports 1006
    "Invalid frame header" and never sees the intended 4001/1008 code. The viewer's onclose
    then falls through to its generic-reconnect branch and loops forever on "Starting
    browser…" instead of showing the terminal overlay.

    A data frame sent BEFORE the close is delivered intact (this is exactly how the live
    ``{"type":"closed"}`` broadcast in ``LiveBrowser.close`` already works), so the viewer
    acts on the message regardless of the lost close code. Best-effort: a socket the client
    already dropped just raises here and there's nothing terminal left to say."""
    try:
        ws.send(json.dumps(message))
    except (ConnectionClosed, OSError):
        pass


def _close_unresolved_ws(ws: Any, browser_id: str) -> None:
    """Close a /cast or /stream WS whose browser didn't resolve, telling the viewer how to
    react (both handlers share this one contract). The terminal reason rides a TEXT frame
    (see :func:`_send_terminal_signal` for why the close code can't be trusted here); the
    matching close code is still sent for spec-compliant clients and for the retryable case:
    - launch FAILED -> ``launch_failed`` + 1008 terminal ("failed to start"); a late
      optimistic viewer that missed the launch_failed broadcast otherwise retries forever.
    - the browser was explicitly CLOSED by an agent -> ``closed`` + 4001 terminal, so the
      viewer shows the "terminated by an agent" overlay rather than the generic "reopen" text.
    - a syntactically valid name not registered YET:
        * while the fleet is still restoring (init not done) -> 1013 retryable, NO terminal
          frame; it may still come up, so the viewer backs off and reconnects.
        * once restore is done -> the name resolves to nothing and never will (e.g. a
          layout-restored tab of a browser closed in a PRIOR daemon life, whose in-memory
          close memory didn't survive the restart) -> ``closed`` + 4001 terminal, so the
          viewer shows the terminated overlay instead of looping forever on "Starting browser…".
    - an invalid name (could never exist) -> ``closed`` + 1008 terminal."""
    if bridge.run(manager.recently_failed_launch_async(browser_id), timeout=_ROUTE_TIMEOUT):
        _send_terminal_signal(ws, {"type": "launch_failed", "browser_id": browser_id})
        ws.close(1008)
    elif bridge.run(manager.recently_closed_async(browser_id), timeout=_ROUTE_TIMEOUT):
        _send_terminal_signal(ws, {"type": "closed", "browser_id": browser_id})
        ws.close(_WS_CLOSE_TERMINATED)
    elif is_valid_browser_name(browser_id):
        if _init_done.is_set():
            _send_terminal_signal(ws, {"type": "closed", "browser_id": browser_id})
            ws.close(_WS_CLOSE_TERMINATED)
        else:
            ws.close(1013)  # still restoring -- retryable, so the viewer reconnects
    else:
        _send_terminal_signal(ws, {"type": "closed", "browser_id": browser_id})
        ws.close(1008)


def stream_socket(ws: Any, browser_id: str) -> None:
    """Pixelflux media socket: one viewer of one browser (pixels + audio + clipboard).

    Control/ownership stays on ``/cast`` (unchanged); this carries the media plane.
    Resolves and gates exactly like ``cast_socket`` (same close-code contract), then
    hands the RUNNING browser's private display to the streamer in ``mediastream.py``.
    """
    resolved = _resolve_sync_for_ws(browser_id)
    if resolved is None:
        _close_unresolved_ws(ws, browser_id)
        return
    session = resolved
    display = getattr(session, "_display", "")
    if not session._is_running or not display:  # _is_running is a property
        ws.close(1013)  # up but not streamable yet -> retryable backoff
        return
    mediastream.serve_stream(ws, browser_id, display, session)


def telemetry_client(browser_id: str) -> Response:
    """Sink for the viewer's own per-stripe decode/paint timings (Rung 2). Watch-only:
    it just forwards each client record into the same hub so the lens can join them to
    the server's sent/ack by (fid, y) and subtract client render from the round trip.
    Never touches the stream; a bad body is dropped, not fatal."""
    # Cap on the bytes actually READ (not the declared content_length, which a chunked
    # request omits): read at most 512KiB+1 and reject anything larger, so a hostile client
    # can't stream an unbounded body into memory through this watch-only sink.
    raw = request.stream.read(512 * 1024 + 1)
    if len(raw) > 512 * 1024:
        return jsonify({"error": "too large"}), 413
    try:
        records = json.loads(raw) if raw else []
    except (ValueError, TypeError):
        return jsonify({"error": "expected JSON"}), 400
    if isinstance(records, dict):
        records = [records]
    if not isinstance(records, list):
        return jsonify({"error": "expected a JSON list"}), 400
    if not is_valid_browser_name(browser_id):
        return jsonify({"error": "invalid browser name"}), 404
    for record in records[:5000]:  # bound the batch: a client reports a handful of stripes per post
        if isinstance(record, dict):
            telemetry.hub.emit(browser_id, _clean_client_record(record))
    return jsonify({"ok": True})


# The only fields the lens joins on / renders for a client record. Coercing to just these
# (numbers only) means a hostile POST can't pin arbitrary-sized values in the by-reference
# telemetry rings -- the records stay a few bytes each, so they can't be inflated into an OOM.
_CLIENT_RECORD_FIELDS = ("fid", "y", "t_arrived", "t_decoded", "t_painted", "dq")


def _clean_client_record(record: dict) -> dict[str, Any]:
    cleaned: dict[str, Any] = {"type": "client"}
    for key in _CLIENT_RECORD_FIELDS:
        value = record.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            cleaned[key] = value
    if record.get("err") is True:
        cleaned["err"] = True
    return cleaned


def telemetry_socket(ws: Any, browser_id: str) -> None:
    """Read-only firehose: replay recent history, then stream new telemetry records
    (batched JSON arrays) to the lens. Subscribing/draining never touches the pipe's
    lock, so a slow or absent lens cannot back-pressure the stream."""
    # Resolve the browser BEFORE subscribing: subscribe() auto-creates hub state for whatever
    # id it's handed, so an unvalidated id would let a caller allocate unbounded per-id state
    # (and keep the resource sampler running) just by opening firehose sockets. Only a real,
    # registered browser may be watched.
    if _resolve_sync_for_ws(browser_id) is None:
        ws.close(1008)
        return
    if not mediastream.telemetry_slots.reserve(browser_id):
        ws.close(1013)  # per-browser firehose cap reached; retryable
        return
    history, records = telemetry.hub.subscribe(browser_id)
    connected = True
    try:
        if history:
            ws.send(json.dumps(history))
        while connected:
            batch = []
            while records:  # drain whatever the fan-out has queued (bounded by contents)
                batch.append(records.popleft())
            if batch:
                ws.send(json.dumps(batch))
            # Pace at ~10Hz and detect a closed socket (receive raises on close); we
            # expect no inbound, so the returned value is ignored.
            try:
                ws.receive(timeout=0.1)
            except ConnectionClosed:
                connected = False
    except ConnectionClosed:
        pass
    finally:
        telemetry.hub.unsubscribe(browser_id, records)
        mediastream.telemetry_slots.release(browser_id)


# --- app construction + lifecycle --------------------------------------------


def _register_routes() -> None:
    application.add_url_rule("/", view_func=index, methods=["GET"])
    application.add_url_rule(
        "/browsers/<string:browser_id>/telemetry/client", view_func=telemetry_client, methods=["POST"]
    )
    application.add_url_rule("/health", view_func=health, methods=["GET"])
    application.add_url_rule("/init-status", view_func=init_status, methods=["GET"])
    application.add_url_rule("/browsers", view_func=list_browsers, methods=["GET"])
    application.add_url_rule("/browsers", view_func=create_browser, methods=["POST"], endpoint="create_browser")
    application.add_url_rule("/browsers/<string:browser_id>", view_func=close_browser, methods=["DELETE"])
    application.add_url_rule("/browsers/<string:browser_id>/release", view_func=release_browser, methods=["POST"])
    application.add_url_rule("/browsers/<string:browser_id>/attach", view_func=cmd_attach, methods=["GET"])
    application.add_url_rule("/browsers/<string:browser_id>/acquire", view_func=cmd_acquire, methods=["POST"])
    application.add_url_rule("/browsers/<string:browser_id>/handoff", view_func=cmd_handoff, methods=["POST"])
    application.add_url_rule(
        "/browsers/<string:browser_id>/clipboard/paste", view_func=cmd_clipboard_paste, methods=["POST"]
    )
    application.add_url_rule(
        "/browsers/<string:browser_id>/clipboard/out", view_func=cmd_clipboard_out, methods=["GET"]
    )
    sock.route("/browsers/<string:browser_id>/cast")(cast_socket)
    # Pixelflux media socket: H.264 stripes out + credit acks/resize in (the pixel plane).
    sock.route("/browsers/<string:browser_id>/stream")(stream_socket)
    # Read-only telemetry firehose feeding the standalone CLI (browser.telemetry_watch).
    sock.route("/browsers/<string:browser_id>/telemetry")(telemetry_socket)
    # Strip permessage-deflate so already-compressed H.264 stripes aren't re-deflated (#22).
    application.before_request(mediastream.strip_websocket_compression)
    # The instances API of the workspace app model (``/_instances``), which the shell reads at
    # the app URL (the manifest names no instances_url): an adapter over the fleet, reaching
    # it through the bridge like every route above. Its nudges and the fleet's own go
    # through whatever nudger the manager has installed (``main`` installs the real one).
    fleet = BridgedFleet(
        bridge=bridge, manager=manager, ready_gate=_init_done, route_timeout_seconds=_ROUTE_TIMEOUT
    )
    application.register_blueprint(
        build_instances_blueprint(FleetInstanceSource(fleet=fleet), ManagerNudger(manager=manager))
    )


_register_routes()


def create_app() -> Flask:
    """Start the bridge loop and launch the (async) startup restore on it, then return
    the app. Idempotent-enough for the daemon entrypoint: ``main`` calls this once.

    Startup is async-on-the-loop (``bridge.submit``), not blocking app construction,
    so read-only routes serve immediately and return 503/initializing until the gate
    opens -- exactly as before.
    """
    bridge.start()
    # The agent's gated CDP endpoint. Its OWN loopback port, deliberately not this Flask
    # app's: that port is registered with forward_port.py and published to the desktop
    # client, and an unauthenticated CDP endpoint must not travel with it.
    bridge.run(_start_proxy(), timeout=_ROUTE_TIMEOUT)
    bridge.submit(_startup())
    return application


async def _start_proxy() -> None:
    """Bring up the fleet-wide CDP proxy and hand it to session.py.

    Falls back to an ephemeral port if the fixed one is taken. A bind failure here would
    otherwise propagate out of ``create_app`` and kill ``main``, which supervisord
    (``autorestart=true``) would then crash-loop -- taking the whole fleet down over a
    port conflict, when a different port works fine (the CLI reads the URL from
    ``/attach``, it never assumes the number).
    """
    server = ProxyServer(port=_PROXY_PORT)
    try:
        await server.start()
    except OSError as e:
        logger.warning("CDP proxy could not bind port {} ({}); using an ephemeral port", _PROXY_PORT, e)
        server = ProxyServer(port=0)
        await server.start()
    set_proxy_server(server)


def _shutdown() -> None:
    """Drain in-flight loop work, close the fleet, and stop the bridge loop.

    Owned exclusively by the signal handler (SIGTERM/SIGINT). ``manager.shutdown``
    cancels the checkpoint loop, writes a final manifest, and closes every browser
    (each browser's close stops its agent + kills its Chromium); then we stop the loop. We
    do NOT also register an atexit handler -- a single owner avoids double-closing
    the fleet or stopping an already-stopped loop.
    """
    logger.info("browser service shutting down; closing sessions")
    try:
        bridge.run(manager.shutdown(), timeout=_ROUTE_TIMEOUT)
    except (TimeoutError, *_STARTUP_ERRORS) as e:
        logger.warning("manager shutdown did not complete cleanly ({})", e)
    bridge.stop()


def _exit_on_signal(_signum: int, _frame: FrameType | None) -> None:
    raise SystemExit(0)


def main() -> None:
    """Build the app, register shutdown, and serve on the threaded HTTP/1.1 server.

    Replaces ``uvicorn.run``. The service is reached at its own workspace origin;
    the viewer uses relative URLs, so no prefix or root-path awareness is needed.
    """
    # Fleet events fire on the loop thread, so the shell is told from a daemon thread; a slow
    # shell never stalls a browser. Installed here, not in create_app, for the same reason
    # as the OOM sweep below: tests that build the app must not post to the workspace shell.
    manager.set_nudger(ThreadedNudger(inner=ShellNudger(app_name=APP_NAME, shell_url=shell_base_url())))
    app = create_app()
    # Chromium overwrites the inherited oom_score_adj with its own gradation;
    # session.py reports every event that can spawn Chromium processes and this
    # worker remaps them back into the browser shedding band (see
    # browser.oom_retag). Started here, not in create_app, so tests that build
    # the app don't sweep the real process tree.
    start_oom_retagging()
    signal.signal(signal.SIGTERM, _exit_on_signal)
    signal.signal(signal.SIGINT, _exit_on_signal)
    server = make_threaded_server("127.0.0.1", 8081, app)
    try:
        server.serve_forever()
    finally:
        _shutdown()


if __name__ == "__main__":
    main()
