import html
import json
import queue
import re
import shlex
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Final

from flask import Flask
from flask import Response
from flask import request
from flask import send_file
from flask import send_from_directory
from loguru import logger as _loguru_logger
from pydantic import ValidationError
from simple_websocket import ConnectionClosed
from werkzeug.exceptions import NotFound

from imbue.system_interface.agent_manager import AgentManager
from imbue.system_interface.app_context import SystemInterfaceState
from imbue.system_interface.app_context import attach_state
from imbue.system_interface.app_context import get_state
from imbue.system_interface.chat_document import create_chat_application
from imbue.system_interface.config import Config
from imbue.system_interface.documents import FRONTEND_BUILT_HEADER
from imbue.system_interface.documents import document_response
from imbue.system_interface.documents import inject_base_path_meta_tag
from imbue.system_interface.documents import inject_meta_tag
from imbue.system_interface.documents import inject_plugin_script_tags
from imbue.system_interface.file_serving import try_serve_file
from imbue.system_interface.request_helpers import handle_unhandled_exception
from imbue.system_interface.request_helpers import json_response
from imbue.system_interface.shell.data_types import ClientStateReport
from imbue.system_interface.shell.projects import project_wire_json
from imbue.system_interface.shell.routes import register_shell_routes
from imbue.system_interface.shell.state import ShellState
from imbue.system_interface.update_staleness import UPDATE_STALENESS_META_TAG
from imbue.system_interface.wsgi import build_sock
from imbue.system_interface.wsgi_dispatch import PathDispatchingFlask

# The browser-side contract module (contracts.md section 10): built as its own library
# entry into ``static/_static/`` and served with a permissive CORS header, since every
# app page that speaks the contract loads it from the shell's origin.
APP_CONTRACT_FILENAME: Final[str] = "app_contract.js"
APP_CONTRACT_PATH: Final[str] = f"/_static/{APP_CONTRACT_FILENAME}"

# The terminal app's registered name: the not-built placeholder embeds it as the way out.
_TERMINAL_APP_NAME: Final[str] = "terminal"

# An app's origin label as ``forward_port.py`` mints it: one DNS label. Read back rather than
# assumed, because the registry is a file on disk that other things write; a row that could
# not be a hostname could not name an origin either.
_ORIGIN_LABEL_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.IGNORECASE)

logger = _loguru_logger


def _shell() -> ShellState:
    return get_state().shell


def _terminal_origin_label() -> str | None:
    """The terminal app's unguessable origin label, or None when there is no terminal to offer."""
    entry = _shell().inventory.entry(_TERMINAL_APP_NAME)
    if entry is None or not _ORIGIN_LABEL_PATTERN.match(entry.row.label):
        return None
    return entry.row.label


# How often the placeholder asks whether the bundle is back. Also the interval
# of the scriptless fallback below, so both routes behave the same.
_NOT_BUILT_POLL_SECONDS = 10

# The ``mngr`` invocation the placeholder offers for standing up an agent to
# repair the workspace. It mirrors what ``agent_manager._build_chat_create_command``
# runs for a chat -- ``--template chat`` for the shared work directory, the output
# style, and running in the workspace tree rather than a worktree of it, plus the
# ``user_created`` label that puts the agent in the dynamic chat memory band.
# Where it differs from that builder:
#
# No ``--type``, which is the one thing that builder cannot leave out: it is
# serving a menu entry, so the harness is a choice the user already made. This
# page has no such choice to carry, so leaving the flag off lets ``mngr`` resolve
# the harness from ``[commands.create] type`` -- whatever this workspace is
# configured to open chats as, rather than whatever it was when this string was
# written. ``--template chat`` supplies the rest either way: it is harness-
# agnostic (``output_style`` is honored by the claude, codex and pi plugins
# alike), so it does not pick one and must not be relied on to.
#
# No ``--transfer none`` either, for a different reason: the ``chat`` template
# already sets it, and the builder spells it out only because it is assembling
# an argv rather than a line for a reader. It is not optional the way the harness
# is -- an agent in a worktree would repair a copy of the workspace instead of
# the workspace -- so ``server_test.py`` reads the template and fails if that
# setting ever leaves it, rather than trusting this comment.
#
# ``--connect`` instead of its ``--no-connect``, which exists to keep a headless
# caller from attaching. Someone typing this wants the opposite, and connecting
# is what turns the create into a conversation. Not merely explicit for the
# reader's sake: the workspace's own ``[commands.create] connect = false`` is
# the default this overrides.
#
# ``--message`` so the agent opens already knowing what the reader is looking at.
# It quotes the page's own heading, which is the one detail a reader on this page
# can be certain of and the one the agent can act on without being told anything
# else.
#
# No name, so mngr mints one. A reload, a second tab, or a first attempt that did
# not finish therefore starts a fresh conversation rather than rejoining an
# earlier one.
#
# Written as the shell line a reader sees and copies, because that string is the
# artifact; the argv is derived from it by the same parse a shell performs, so
# what is offered cannot drift from what runs. Quoted with ``"`` rather than as
# ``shlex.join`` would (``'i'"'"'m ...``): the message carries apostrophes, and
# this is the one line on the page a reader has to be able to read in full.
#
# Kept in sync with that builder by ``server_test.py``, which also validates it
# against the live CLI. It is a suggestion, not a dispatch: the server never
# runs it, so an agent is created only if the reader decides to.
_NOT_BUILT_REPAIR_MNGR_COMMAND: Final[str] = (
    "mngr create --connect --template chat --label user_created=true "
    '--message "i\'m seeing \\"this workspace\'s interface needs to be rebuilt, can you fix it?\\""'
)

_NOT_BUILT_REPAIR_ARGV: Final[tuple[str, ...]] = tuple(shlex.split(_NOT_BUILT_REPAIR_MNGR_COMMAND))

# ``mngr connect`` refuses to attach from inside tmux unless ``is_nested_tmux_allowed``
# is set (see ``mngr.api.connect``, which gates purely on ``$TMUX``), and it is the
# connect half of the create above that would hit it. The workspace's own terminal
# tabs are tmux sessions, so a reader who runs this in one gets a created agent and
# an error instead of a conversation. Dropping ``TMUX`` for this one command is
# exactly what mngr does for itself once the check passes, and scoping it with
# ``env -u`` leaves the reader's own shell alone.
_NOT_BUILT_REPAIR_COMMAND: Final[str] = "env -u TMUX " + _NOT_BUILT_REPAIR_MNGR_COMMAND

# Served in place of the app whenever the compiled bundle is missing. The bundle
# is gitignored build output, so a code refresh that replaces the tree can leave
# the workspace here. The page offers a way out and returns to the interface on
# its own once there is one, so a bundle restored by anything else -- most often
# the reveal flow's rollback -- brings the reader back without them having to
# know to retry.
#
# The way out is a terminal, not a "rebuild" button. A button has to be right
# about what went wrong; the states that strand a workspace here are dominated
# by ones where a build dispatched from the server would fail too (no registry,
# no memory, a lockfile that does not resolve), and it would fail with nowhere
# to say so, on a page with no application to render the failure. It would also
# inherit the server's memory band, which puts it ahead of the user's chats and
# agents in a shed. A shell is the general case of every repair rather than one
# of them, and ttyd is already running and already *more* protected than this
# server, so the page is pointing at something that outlives it rather than
# starting anything.
#
# Nothing here is part of the compiled bundle -- this is a string in the
# backend -- so the script below ships whether or not the frontend has ever
# been built. That is what lets the page be more than static text in exactly
# the state where the frontend is missing.
_FRONTEND_NOT_BUILT_TEMPLATE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Interface not built</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 0; display: flex;
         align-items: center; justify-content: center; min-height: 100vh;
         background: #14161a; color: #e6e8eb; }
  main { max-width: 48rem; padding: 2rem; width: 100%; box-sizing: border-box; }
  h1 { font-size: 1.25rem; font-weight: 600; margin: 0 0 0.75rem; }
  p { line-height: 1.5; margin: 0 0 1rem; color: #b6bcc4; max-width: 34rem; }
  code { background: #1d2026; border-radius: 4px; padding: 0.1rem 0.3rem;
         font-size: 0.9em; }
  /* The command wraps rather than scrolling: a horizontal scrollbar hides
     most of it behind a control nobody looks for, and this is the one line on
     the page a reader has to be able to read in full. */
  #repair { position: relative; }
  pre { background: #1d2026; border-radius: 6px; padding: 0.75rem 1rem;
        margin: 0 0 1rem; font-size: 0.85em; color: #e6e8eb;
        white-space: pre-wrap; overflow-wrap: anywhere; }
  /* Room for the copy button, so the last line never runs underneath it. */
  #repair-command { padding-right: 5.5rem; }
  #copy-repair { position: absolute; top: 0.5rem; right: 0.5rem;
                 font: inherit; font-size: 0.8em; padding: 0.25rem 0.6rem;
                 color: #e6e8eb; background: #2a2f37; border: 1px solid #3a414b;
                 border-radius: 4px; cursor: pointer; }
  #copy-repair:hover { background: #333a44; }
  #copy-repair[hidden] { display: none; }
  #terminal-slot[hidden] { display: none; }
  #terminal { width: 100%; height: 24rem; border: 1px solid #2a2f37;
              border-radius: 6px; background: #000; }
</style>
<!-- The scriptless fallback for the poll at the end of the body. Whole-page
     reloads and a live terminal cannot coexist -- a refresh every ten seconds
     would destroy the session the reader is typing into -- so this runs only
     where there is no script to poll with, and therefore no terminal either. -->
<noscript><meta http-equiv="refresh" content="__POLL_SECONDS__"></noscript>
</head>
<body>
<main>
  <h1>This workspace's interface needs to be rebuilt</h1>
  <p>The compiled interface is missing, so there is nothing to show yet. Your
     work and your agents are untouched -- only the interface itself is gone,
     and this page returns to the interface on its own once it is back.</p>
  <p>If you would rather not work the repair out yourself, this creates an agent
     and tells it what you are looking at:</p>
  <div id="repair">
    <pre id="repair-command">__REPAIR_COMMAND__</pre>
    <!-- Hidden until the script confirms it can actually copy, so the page
         never shows a button that does nothing. -->
    <button id="copy-repair" type="button" hidden>Copy</button>
  </div>
  <!-- The lead-in belongs to the terminal, not to the command: the command
       stands on its own wherever the reader finds a shell, so it keeps its own
       introduction above and is never left orphaned when there is no terminal
       to offer. -->
  <p id="terminal-intro" hidden>You can run that -- or do the repair yourself --
     in this workspace's terminal, below.</p>
  <div id="terminal-slot" hidden>
    <iframe id="terminal" title="Workspace terminal"></iframe>
  </div>
</main>
<script>
(function () {
  // The terminal's hostname label, minted per workspace, or "" when there is
  // no terminal registered to offer.
  var terminalLabel = __TERMINAL_LABEL__;

  // Mirrors deriveServiceOrigin/workspaceHostCoordinate in
  // frontend/src/origin.ts, which is canonical: a service origin is its label
  // prefixed onto the workspace COORDINATE -- the first host-<hex> (or, on a
  // workspace-keyed share domain, bare 32-hex share) label and everything
  // after it -- and never onto this page's host verbatim, which
  // would nest the service under the shell's own label and route back here.
  //
  // It differs from origin.ts in one way, deliberately: no coordinate label
  // means no origin, rather than falling back to the host unchanged. The shell
  // can assume it is running inside a workspace; this page cannot (a direct
  // hit on the loopback port has no coordinate), and a made-up origin would
  // show the reader a broken frame instead of the prose that still helps them.
  function serviceOrigin(label) {
    var labels = window.location.host.split(".");
    for (var index = 0; index < labels.length; index++) {
      // The port rides on whichever label is last, so it is stripped before
      // matching -- otherwise a coordinate that happens to BE the last label
      // reads as an ordinary one and the terminal is silently not offered.
      // The slice below keeps it, which is what the origin needs.
      if (/^(?:(?:host|agent)-[a-f0-9]+|[a-f0-9]{32})$/i.test(labels[index].split(":")[0])) {
        return window.location.protocol + "//" + label + "." +
               labels.slice(index).join(".") + "/";
      }
    }
    return null;
  }

  // The command is long enough to be worth not retyping, and the reader may be
  // copying it into a terminal on the far side of a share. Shown only when the
  // clipboard is actually reachable: it needs a secure context, which every
  // workspace origin is (``*.localhost`` locally, https on a share) but which a
  // direct hit on the loopback port is not.
  var copyButton = document.getElementById("copy-repair");
  if (navigator.clipboard && window.isSecureContext) {
    copyButton.hidden = false;
    copyButton.addEventListener("click", function () {
      navigator.clipboard
        .writeText(document.getElementById("repair-command").textContent)
        .then(function () {
          copyButton.textContent = "Copied";
          setTimeout(function () {
            copyButton.textContent = "Copy";
          }, 2000);
        })
        // A denied permission is the realistic failure. Selecting the text puts
        // the reader one keystroke from the same result rather than leaving a
        // button that silently did nothing.
        .catch(function () {
          window.getSelection().selectAllChildren(document.getElementById("repair-command"));
          copyButton.textContent = "Press to copy";
        });
    });
  }

  var origin = terminalLabel ? serviceOrigin(terminalLabel) : null;
  if (origin) {
    document.getElementById("terminal").src = origin;
    document.getElementById("terminal-slot").hidden = false;
    document.getElementById("terminal-intro").hidden = false;
  }

  // Ask whether the bundle is back rather than reloading blind. The reply
  // carries the same header the reveal flow's own health check reads, so a
  // rebuild by ANY route -- a rollback, a build run in the terminal above --
  // brings the interface back with no further action. Asking (rather than
  // refreshing) is what lets the terminal stay alive between checks.
  setInterval(function () {
    fetch(window.location.pathname, { method: "HEAD", cache: "no-store" })
      .then(function (response) {
        if (response.headers.get("__BUILT_HEADER__") === "true") {
          window.location.reload();
        }
      })
      // The backend restarting is the expected way for this to fail, and it is
      // also a moment when the bundle may be arriving. Say nothing and ask again.
      .catch(function () {});
  }, __POLL_SECONDS__ * 1000);
})();
</script>
</body>
</html>
"""


def _inject_update_staleness_meta_tag(html_content: str, staleness: str | None) -> str:
    """Inject the update-staleness variant so the frontend can render its banner.

    Injected only when stale: the banner keys off the tag's presence, and a
    consistent workspace's shell carries no tag at all.
    """
    if staleness is None:
        return html_content
    return inject_meta_tag(html_content, UPDATE_STALENESS_META_TAG, staleness)


def _shell_update_staleness() -> str | None:
    """The staleness variant to inject into this app shell, if any.

    Asked per built-shell request, so a tree that moved -- or an apply marker
    that appeared -- after this process started is still seen. Skipped for
    ``HEAD``: that is the not-built placeholder's own poll, once every ten
    seconds per open tab for the length of an outage, and the placeholder
    itself never asks (it carries no banner). Reading staleness forks git, and
    an outage is precisely when the tree has moved and both of its reads run.
    """
    if request.method == "HEAD":
        return None
    return get_state().update_staleness.staleness()


def _index() -> Response:
    index_path = get_state().static_directory / "index.html"
    if index_path.exists():
        staleness = _shell_update_staleness()
        config: Config = get_state().config
        root_path = (request.script_root or "").rstrip("/")
        html_content = index_path.read_text()
        html_content = inject_base_path_meta_tag(html_content, root_path)
        html_content = _inject_update_staleness_meta_tag(html_content, staleness)
        if config.javascript_plugin_basenames:
            html_content = inject_plugin_script_tags(html_content, config.javascript_plugin_basenames, root_path)
        return document_response(html_content, is_frontend_built=True)
    return _frontend_not_built_response()


def render_frontend_not_built_page(terminal_label: str | None) -> str:
    """Fill the not-built placeholder in, given the terminal's origin label.

    Takes the label rather than reading the registry itself so the page can be
    rendered for a workspace with no terminal (pass ``None``) without touching
    the filesystem, which is the case worth testing and the one hardest to
    arrange for real.

    The label is substituted as a JSON literal, so the script receives a string
    however it is spelled, and ``""`` -- which the script treats as "no
    terminal" -- when there is none. ``_terminal_origin_label`` has already
    rejected anything that is not a single DNS label, so no value that reaches
    here can close the script element.

    The repair command goes in as HTML text rather than markup. It is a shell
    line written for a reader, so ``&`` and ``<`` are ordinary characters in it
    that the browser would otherwise take as an entity reference or a tag --
    which would show something other than the command, and hand the copy button
    (which reads ``textContent``) something other than what the tests validated.
    Quotes are left alone: this is text content, and the line is full of them.
    """
    return (
        _FRONTEND_NOT_BUILT_TEMPLATE.replace("__TERMINAL_LABEL__", json.dumps(terminal_label or ""))
        .replace("__REPAIR_COMMAND__", html.escape(_NOT_BUILT_REPAIR_COMMAND, quote=False))
        .replace("__BUILT_HEADER__", FRONTEND_BUILT_HEADER)
        .replace("__POLL_SECONDS__", str(_NOT_BUILT_POLL_SECONDS))
    )


def _frontend_not_built_response() -> Response:
    """Render the placeholder shown when there is no compiled bundle to serve.

    A ``HEAD`` gets the header and nothing else. That is the placeholder's own
    poll asking whether the bundle is back yet -- every ten seconds, for every
    open tab, for as long as the outage lasts -- and answering it in full would
    re-read the app registry and re-render the page each time, and bury the one
    diagnostic below in six repetitions a minute of itself. Werkzeug drops the
    body of a HEAD response anyway, so the caller sees no difference.
    """
    if request.method == "HEAD":
        return document_response("", is_frontend_built=False)
    # Logged with the resolved directory because the usual cause is that the
    # served tree was replaced under a running service, which is otherwise
    # invisible from the supervisor logs.
    _loguru_logger.warning(
        "Served the not-built placeholder: no frontend bundle at {}", get_state().static_directory / "index.html"
    )
    return document_response(render_frontend_not_built_page(_terminal_origin_label()), is_frontend_built=False)


def _index_catch_all(path: str) -> Response:
    # An agent-authored file is addressed by its absolute on-disk path, which
    # lands here as a catch-all path; serve it (image inline, any other existing
    # file as a download) before falling through to the single-page-app shell.
    # Paths that match no file are client-side routes and render the app as before.
    file_response = try_serve_file(path)
    if file_response is not None:
        return file_response
    return _index()


def _health_endpoint() -> Response:
    """The probe route of contracts.md section 5: alive, and whether the built frontend is being served."""
    is_frontend_built = (get_state().static_directory / "index.html").exists()
    return json_response({"status": "ok", "is_frontend_built": is_frontend_built})


def _serve_app_contract() -> Response:
    """Serve the browser-side contract module (contracts.md section 10) for any origin's app page."""
    contract_path = get_state().static_directory / "_static" / APP_CONTRACT_FILENAME
    if not contract_path.is_file():
        return Response(status=404)
    response = send_file(contract_path, mimetype="text/javascript")
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response


def _favicon() -> Response:
    favicon_path = get_state().static_directory / "favicon.ico"
    if favicon_path.exists():
        return send_file(favicon_path, mimetype="image/x-icon")
    return Response(status=404)


def _serve_asset(filename: str) -> Response:
    assets_directory = get_state().static_directory / "assets"
    # A missing asset is a plain 404, as for the favicon above, rather than the
    # HTML error page ``send_from_directory`` would raise. Existence and safety
    # are both left to ``send_from_directory``: ``filename`` arrives with any
    # ``..`` segments intact, so joining it onto the directory ourselves would
    # stat paths outside it -- an existence oracle for the whole filesystem.
    try:
        return send_from_directory(assets_directory, filename)
    except NotFound:
        return Response(status=404)


def _serve_static_file(basename: str) -> Response:
    config: Config = get_state().config
    file_path_string = config.static_file_basename_to_path.get(basename)
    if file_path_string is None:
        return json_response({"detail": f"Static file '{basename}' not found"}, status_code=404)
    file_path = Path(file_path_string)
    if not file_path.is_file():
        return json_response({"detail": f"Static file not found on disk: {file_path}"}, status_code=404)
    return send_file(file_path)


def _ws_endpoint(websocket: Any) -> None:
    """The one WebSocket per window (contracts.md section 8)."""
    state = get_state()
    _run_ws_broadcast_loop(websocket=websocket, shell=state.shell, agent_manager=state.agent_manager)


def _handle_client_state_message(
    raw_message: str,
    client_queue: "queue.Queue[str | None]",
    shell: ShellState,
    is_first_report: bool,
) -> bool:
    """Process one incoming WebSocket message; returns True for a well-formed ``client_state``.

    ``client_state`` is the only message type clients send: it registers the browser's client
    id, device kind, and active view (on connect and on every view switch). Registration feeds
    the broadcaster's client registry (which targets layout ops), the client record, and the
    client-activity log (a ``view_switch`` when the report names a different previous view).
    """
    try:
        parsed = json.loads(raw_message)
    except json.JSONDecodeError as e:
        _loguru_logger.opt(exception=e).warning("Ignored unparsable WebSocket message from client")
        return False
    if not isinstance(parsed, dict) or parsed.get("type") != "client_state":
        _loguru_logger.warning("Ignored unexpected WebSocket message type from client: {!r}", parsed)
        return False
    try:
        report = ClientStateReport.model_validate({key: value for key, value in parsed.items() if key != "type"})
    except ValidationError as e:
        _loguru_logger.warning("Ignored a malformed client_state report: {}", e.errors()[0]["msg"])
        return False
    shell.broadcaster.set_client_info(
        client_queue, str(report.client_id), str(report.active_view), report.device_kind.value
    )
    shell.clients.record_report(report, datetime.now(timezone.utc))
    if is_first_report:
        _loguru_logger.info(
            "WS client registered: client_id={} view={} device={} (conn {})",
            report.client_id,
            report.active_view,
            report.device_kind.value,
            id(client_queue),
        )
    elif report.previous_view and report.previous_view != report.active_view:
        _loguru_logger.info(
            "WS client {} switched view {} -> {} (conn {})",
            report.client_id,
            report.previous_view,
            report.active_view,
            id(client_queue),
        )
        shell.activity.append_view_switch(
            str(report.client_id), report.device_kind.value, report.previous_view, str(report.active_view)
        )
    else:
        # A re-report on an already-registered connection with an unchanged view.
        pass
    return True


def _run_ws_broadcast_loop(websocket: Any, shell: ShellState, agent_manager: AgentManager) -> None:
    """Stream the shell broadcaster's messages to ``websocket`` until the client disconnects.

    Each WebSocket connection owns its own thread (flask-sock + the threaded WSGI server), so
    this loop blocks on the per-client queue and forwards messages. flask-sock's keepalive
    closes a half-dead peer, surfacing as ``ConnectionClosed`` from ``send``; the broadcaster
    can also evict a hopelessly-behind client by pushing the shutdown sentinel (``None``).

    Incoming ``client_state`` registrations are drained non-blockingly on each loop iteration.
    """
    ws_broadcaster = shell.broadcaster
    client_queue = ws_broadcaster.register()
    _loguru_logger.info("WS /api/ws connection opened (conn {})", id(client_queue))
    disconnect_reason = "handler exited"
    try:
        websocket.send(json.dumps({"type": "apps_updated", "apps": shell.inventory.serialized()}))
        websocket.send(
            json.dumps(
                {
                    "type": "projects_updated",
                    "projects": [project_wire_json(project) for project in shell.projects.list_projects()],
                }
            )
        )
        # The chat pages read their live state (activity, model choice, queued messages,
        # creation logs) from this same socket while the chat app shares the shell's process.
        # CLEANUP: drop these two sends and the agent manager parameter in phase 10 of the
        # workspace app model, when the chat app's own process serves its own socket.
        websocket.send(json.dumps({"type": "agents_updated", "agents": agent_manager.get_agents_serialized()}))
        for proto in agent_manager.get_proto_agents():
            websocket.send(json.dumps({"type": "proto_agent_created", **proto}))

        is_client_registered = False
        shutdown = False
        while not shutdown:
            incoming = websocket.receive(timeout=0)
            while incoming is not None:
                if _handle_client_state_message(
                    str(incoming), client_queue, shell, is_first_report=not is_client_registered
                ):
                    is_client_registered = True
                incoming = websocket.receive(timeout=0)
            try:
                message = client_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if message is None:
                shutdown = True
                disconnect_reason = "shutdown sentinel (evicted by broadcaster or server shutdown)"
            else:
                websocket.send(message)
    except ConnectionClosed:
        disconnect_reason = "connection closed"
    finally:
        client_info = ws_broadcaster.get_client_info(client_queue)
        _loguru_logger.info(
            "WS /api/ws connection closed (conn {}, client_id={}, reason: {})",
            id(client_queue),
            client_info["client_id"] if client_info is not None else "<unregistered>",
            disconnect_reason,
        )
        ws_broadcaster.unregister(client_queue)


def create_application(state: SystemInterfaceState) -> Flask:
    """Assemble the Flask app around an already-built ``SystemInterfaceState``.

    Pure assembler: it wires routes and error handling onto the app and attaches the injected
    ``state``. It constructs no collaborators and starts nothing. The composition root
    (``main.build_production_state`` plus ``main.main``) builds the real object graph and
    starts the agent manager and the shell; tests build a state with fakes via
    ``testing.build_test_state`` and pass it here.
    """
    # static_folder=None disables Flask's default /static route; the shell serves its own
    # static assets explicitly below.
    application = PathDispatchingFlask(__name__, static_folder=None)
    attach_state(application, state)
    application.register_error_handler(Exception, handle_unhandled_exception)
    sock = build_sock(application)

    application.add_url_rule("/", view_func=_index, methods=["GET"])
    application.add_url_rule("/favicon.ico", view_func=_favicon, methods=["GET"])
    application.add_url_rule("/api/health", view_func=_health_endpoint, methods=["GET"])
    application.add_url_rule(APP_CONTRACT_PATH, view_func=_serve_app_contract, methods=["GET"])
    register_shell_routes(application)
    sock.route("/api/ws")(_ws_endpoint)
    application.add_url_rule("/plugins/<basename>", view_func=_serve_static_file, methods=["GET"])

    # Registered unconditionally, even when the bundle is absent at startup: the directory can
    # appear later (a rebuild), and a route decided at construction time can never notice.
    application.add_url_rule("/assets/<path:filename>", view_func=_serve_asset, methods=["GET"])
    application.add_url_rule("/<path:path>", view_func=_index_catch_all, methods=["GET"])

    # The chat document is a second Flask app over the same state, picked per request by
    # the path it alone serves (see wsgi_dispatch); the shell app stays the object callers hold.
    # CLEANUP: drop the dispatch once phase 10 of the workspace app model moves the chat app
    # into its own process.
    application.chat_application = create_chat_application(state)

    return application
