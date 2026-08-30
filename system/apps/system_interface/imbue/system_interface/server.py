import html
import json
import os
import queue
import shlex
import socket
import threading
import time
import traceback
from collections.abc import Callable
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from typing import Final
from uuid import uuid4

import httpx
from flask import Flask
from flask import Response
from flask import request
from flask import send_file
from flask import send_from_directory
from flask_sock import Sock
from loguru import logger as _loguru_logger
from pydantic import Field
from simple_websocket import ConnectionClosed
from werkzeug.exceptions import HTTPException
from werkzeug.exceptions import NotFound

from imbue.concurrency_group.subprocess_utils import run_local_command_modern_version
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.errors import MngrError
from imbue.mngr.primitives import AgentId
from imbue.system_interface import app_instances
from imbue.system_interface import client_activity
from imbue.system_interface import latchkey_endpoints
from imbue.system_interface import member_last_used
from imbue.system_interface import member_locations
from imbue.system_interface import member_titles
from imbue.system_interface import projects
from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.agent_discovery import SendFailedError
from imbue.system_interface.agent_discovery import discover_agents
from imbue.system_interface.agent_discovery import get_host_dir
from imbue.system_interface.agent_discovery import start_agent
from imbue.system_interface.agent_manager import AgentManager
from imbue.system_interface.app_context import SystemInterfaceState
from imbue.system_interface.app_context import attach_state
from imbue.system_interface.app_context import get_state
from imbue.system_interface.attachments import delete_upload
from imbue.system_interface.attachments import get_uploads_directory
from imbue.system_interface.attachments import resolve_upload_path
from imbue.system_interface.attachments import store_uploaded_file
from imbue.system_interface.config import Config
from imbue.system_interface.event_queues import AgentEventQueues
from imbue.system_interface.file_serving import try_serve_file
from imbue.system_interface import accounts_endpoints
from imbue.system_interface.harnesses.claude import auth_endpoints
from imbue.system_interface.harnesses.interrupt import restart_drain
from imbue.system_interface.harnesses.model import ModelIdentity
from imbue.system_interface.harnesses.model import ModelOption
from imbue.system_interface.harnesses.registry import HARNESS_SPECS
from imbue.system_interface.harnesses.registry import build_resolver
from imbue.system_interface.harnesses.registry import get_catalog
from imbue.system_interface.harnesses.registry import get_harness_spec
from imbue.system_interface.harnesses.session import SendOutcome
from imbue.system_interface.harnesses.session_watcher import AgentSessionWatcher
from imbue.system_interface.layout_ops import LayoutMutex
from imbue.system_interface.layout_ops import allocate_next_terminal_name
from imbue.system_interface.layout_ops import allocate_terminal_panel_id
from imbue.system_interface.layout_ops import filter_user_terminal_sessions
from imbue.system_interface.layout_ops import is_broadcasting_op
from imbue.system_interface.layout_ops import is_destroyable_terminal_session
from imbue.system_interface.layout_ops import is_known_op
from imbue.system_interface.layout_ops import is_mutating_op
from imbue.system_interface.layout_ops import is_sessionless_browser_ref
from imbue.system_interface.layout_ops import layout_inspect
from imbue.system_interface.layout_ops import layout_list
from imbue.system_interface.layout_ops import parse_tmux_sessions_output
from imbue.system_interface.layout_ops import terminal_origin_label
from imbue.system_interface.liveness import SupervisorProgramActionError
from imbue.system_interface.liveness import start_supervisor_program
from imbue.system_interface.liveness import stop_supervisor_program
from imbue.system_interface.liveness import supervisor_socket_path
from imbue.system_interface.models import ActivityRequest
from imbue.system_interface.models import ActivityResponse
from imbue.system_interface.models import AgentCreationError
from imbue.system_interface.models import AgentListItem
from imbue.system_interface.models import AgentListResponse
from imbue.system_interface.models import AgentNameConflictError
from imbue.system_interface.models import AgentRenameError
from imbue.system_interface.models import AgentRestartError
from imbue.system_interface.models import AppEntry
from imbue.system_interface.models import AttachmentError
from imbue.system_interface.models import AttachmentUploadResponse
from imbue.system_interface.models import CreateAgentResponse
from imbue.system_interface.models import CreateChatRequest
from imbue.system_interface.models import DestroyAgentResponse
from imbue.system_interface.models import DrainToComposerResponse
from imbue.system_interface.models import ErrorResponse
from imbue.system_interface.models import FastModePromptAnsweredResponse
from imbue.system_interface.models import InterruptAgentResponse
from imbue.system_interface.models import ModelOptionsResponse
from imbue.system_interface.models import PoweredByResponse
from imbue.system_interface.models import SendMessageRequest
from imbue.system_interface.models import SendMessageResponse
from imbue.system_interface.models import SetModelChoiceRequest
from imbue.system_interface.models import ShoulderTapAtomicResponse
from imbue.system_interface.models import StartAgentResponse
from imbue.system_interface.models import StopAgentResponse
from imbue.system_interface.models import TerminalSessionInfo
from imbue.system_interface.plugins import get_plugin_manager
from imbue.system_interface.update_staleness import UPDATE_STALENESS_META_TAG
from imbue.system_interface.update_staleness import WORKSPACE_ROOT_DIRECTORY
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster

_LOOPBACK_CLIENT_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

logger = _loguru_logger


# Stamped on every app-shell response so a caller can tell the real app from
# the "not built" placeholder, which is otherwise an identical HTTP 200 HTML
# response. The reveal flow's frontend health check reads it.
FRONTEND_BUILT_HEADER = "X-Frontend-Built"

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
  // prefixed onto the workspace host COORDINATE -- the host-<hex> label and
  // everything after it -- and never onto this page's host verbatim, which
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
      if (/^(?:host|agent)-[a-f0-9]+$/i.test(labels[index].split(":")[0])) {
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

# Default number of events for tail-first loading
_DEFAULT_TAIL_COUNT = 50

# Name under which the browser daemon registers itself (via forward_port.py)
# in data/.state/apps.toml. The /api/browsers passthrough resolves the daemon's
# local backend URL from this registry entry.
_BROWSER_SERVICE_NAME = "browser"

# The name this shell registers itself under. It is an app like any other in
# the registry, so the deregister endpoint has to refuse it explicitly -- pulling
# its own row would leave the workspace with no origin to serve the UI from.
_SHELL_SERVICE_NAME = "system_interface"

# The services the stop/start endpoints refuse outright: the shell that serves
# the UI, and the terminal service whose ttyd carries every terminal tab.
# Neither registers a ``program`` (see system/supervisord.conf), so this is
# defense in depth for hand-edited registries. Everything else with a
# ``program`` -- the browser fleet daemon included -- is stoppable.
_ESSENTIAL_SERVICE_NAMES = frozenset({_SHELL_SERVICE_NAME, "terminal"})

_SERVICE_REF_PREFIX = "service:"

# ``system/scripts/forward_port.py`` owns the app registry at
# ``data/.state/apps.toml`` -- its lock file and its atomic replace -- so
# deregistering an app shells out to the script rather than growing a second
# writer of the same file here. The script imports tomlkit, a workspace-venv
# dependency that this app does not declare, so it runs under ``uv run`` from
# the workspace root, exactly as ``.agents/shared/scripts/serve_isolated_instance.py``
# invokes it. The root is this package's own location walked back out of
# ``system/apps/system_interface/imbue/system_interface``.
_FORWARD_PORT_SCRIPT = WORKSPACE_ROOT_DIRECTORY / "system" / "scripts" / "forward_port.py"

# Generous: the registration script runs under ``uv run``, which may have to
# resolve the workspace environment before the (near-instant) TOML rewrite.
_FORWARD_PORT_TIMEOUT_SECONDS = 60.0

# How often flask-sock sends a keepalive ping on each WebSocket connection.
# Pings detect (and tear down) half-dead peers without any asyncio machinery --
# each connection owns its own thread, so a wedged send only stalls that thread.
_WS_PING_INTERVAL_SECONDS = 25

# Cap on the `mngr destroy` subprocess. A destroy measured ~16s idle on this
# class of host (mngr CLI startup + discovery + teardown + inline worktree gc)
# and degrades under load, so the old 30s cap SIGTERMed real destroys mid-
# teardown (a partial destroy the user saw as a 500). Every internal mngr
# cleanup step is itself bounded, so destroy cannot hang indefinitely: a
# generous cap only converts spurious kills into patience.
_DESTROY_TIMEOUT_SECONDS = 120.0
# `mngr label` is a metadata write (data.json merge), fast even on a busy host.
_LABEL_TIMEOUT_SECONDS = 30.0

# The member-ref prefix that marks an object as an mngr agent. The rest of a
# chat ref is the agent's id (``chat:<agent-id>``, as every UI surface files it).
_CHAT_MEMBER_REF_PREFIX = "chat:"


class _ReflectClientSubprotocols:
    """A WebSocket subprotocols allow-list that accepts whatever the client offers.

    ``flask_sock`` builds one ``simple_websocket.Server`` per connection from
    ``SOCK_SERVER_OPTIONS`` and completes the WebSocket handshake (selecting and
    echoing the subprotocol) *before* our route handler runs, so a handler cannot
    choose the subprotocol per-connection. ``simple_websocket``'s default
    ``choose_subprotocol`` echoes the first client-offered subprotocol that is
    ``in`` this allow-list; making ``__contains__`` always true turns that into a
    transparent passthrough -- the server echoes back whatever subprotocol the
    client requested.

    Chrome aborts a WebSocket handshake (close 1006) if the client offered a
    subprotocol and the 101 response echoes none, so any future WS route that
    negotiates a subprotocol works without touching this list. Today's own
    endpoints (the ``/api/ws`` broadcaster and the proto-agent-logs stream)
    offer no subprotocol, so the negotiation loop never runs and no
    subprotocol is echoed -- the passthrough is inert for them but keeps the
    server permissive for subprotocol-bearing clients.
    """

    def __contains__(self, _subprotocol: object) -> bool:
        return True


def _json_response(content: Any, status_code: int = 200) -> Response:
    """Build a compact JSON response, matching the wire format the frontend expects."""
    body = json.dumps(content, separators=(",", ":"), ensure_ascii=False)
    return Response(body, status=status_code, mimetype="application/json")


def _html_response(html_content: str, status_code: int = 200) -> Response:
    """Build an uncacheable HTML response for the app shell.

    The shell is assembled per request (base path, hostname, agent id, and the
    configured plugin script tags are injected into it), so it is never a
    cacheable artifact to begin with. It is also the *only* thing standing
    between a reload and a stale UI: the built assets it links are
    content-hashed, so a freshly-fetched shell always names the current bundle,
    and a cached one always names the old one.

    That matters because a page cannot drop its own HTTP cache -- the
    ``location.reload(true)`` form is a Firefox-only extension -- so
    ``reloadInterface`` (see ``frontend/src/reload.ts``) can only reload and
    trust the response to be fresh. ``no-store`` is what makes that trust
    well-founded, including for viewers reaching the workspace through a
    shared Cloudflare tunnel, where an intermediary is free to cache anything
    we do not mark otherwise.
    """
    response = Response(html_content, status=status_code, mimetype="text/html")
    response.headers["Cache-Control"] = "no-store"
    return response


def _shell_response(html_content: str, *, is_frontend_built: bool) -> Response:
    """Return an app-shell response, stamped with whether it is the real app.

    Both the app and the not-built placeholder are HTTP 200 HTML, so nothing
    downstream can tell them apart from the status line alone. The header says
    which one this is, so a health check does not have to pattern-match markup
    that is free to change.
    """
    response = _html_response(html_content)
    response.headers[FRONTEND_BUILT_HEADER] = "true" if is_frontend_built else "false"
    return response


def _inject_base_path_meta_tag(html_content: str, root_path: str) -> str:
    meta_tag = f'<meta name="system-interface-base-path" content="{root_path}">'
    return html_content.replace("</head>", f"{meta_tag}\n</head>")


def _read_host_name() -> str:
    """Read the host name from $MNGR_HOST_DIR/data.json, falling back to socket.gethostname()."""
    host_dir = os.environ.get("MNGR_HOST_DIR", "")
    if host_dir:
        data_path = Path(host_dir) / "data.json"
        if data_path.exists():
            try:
                data = json.loads(data_path.read_text())
                name = data.get("host_name")
                if name:
                    return str(name)
            except (json.JSONDecodeError, OSError):
                pass
    return socket.gethostname()


def _inject_hostname_meta_tag(html_content: str) -> str:
    hostname = _read_host_name()
    meta_tag = f'<meta name="system-interface-hostname" content="{hostname}">'
    return html_content.replace("</head>", f"{meta_tag}\n</head>")


def _inject_plugin_script_tags(html_content: str, plugin_basenames: list[str], root_path: str) -> str:
    script_tags = "\n".join(f'<script src="{root_path}/plugins/{basename}"></script>' for basename in plugin_basenames)
    return html_content.replace("</body>", f"{script_tags}\n</body>")


def _inject_agent_id_meta_tag(html_content: str) -> str:
    """Inject the primary agent ID as a meta tag for the frontend."""
    agent_id = os.environ.get("MNGR_AGENT_ID", "")
    meta_tag = f'<meta name="system-interface-agent-id" content="{agent_id}">'
    return html_content.replace("</head>", f"{meta_tag}\n</head>")


def _inject_update_staleness_meta_tag(html_content: str, staleness: str | None) -> str:
    """Inject the update-staleness variant so the frontend can render its banner.

    Injected only when stale: the banner keys off the tag's presence, and a
    consistent workspace's shell carries no tag at all.
    """
    if staleness is None:
        return html_content
    meta_tag = f'<meta name="{UPDATE_STALENESS_META_TAG}" content="{html.escape(staleness, quote=True)}">'
    return html_content.replace("</head>", f"{meta_tag}\n</head>")


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
        html_content = _inject_base_path_meta_tag(html_content, root_path)
        html_content = _inject_hostname_meta_tag(html_content)
        html_content = _inject_agent_id_meta_tag(html_content)
        html_content = _inject_update_staleness_meta_tag(html_content, staleness)
        if config.javascript_plugin_basenames:
            html_content = _inject_plugin_script_tags(html_content, config.javascript_plugin_basenames, root_path)
        return _shell_response(html_content, is_frontend_built=True)
    return _frontend_not_built_response()


def render_frontend_not_built_page(terminal_label: str | None) -> str:
    """Fill the not-built placeholder in, given the terminal's origin label.

    Takes the label rather than reading the registry itself so the page can be
    rendered for a workspace with no terminal (pass ``None``) without touching
    the filesystem, which is the case worth testing and the one hardest to
    arrange for real.

    The label is substituted as a JSON literal, so the script receives a string
    however it is spelled, and ``""`` -- which the script treats as "no
    terminal" -- when there is none. ``layout_ops.terminal_origin_label``
    has already rejected anything that is not a single DNS label, so no value
    that reaches here can close the script element.

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
        return _shell_response("", is_frontend_built=False)
    # Logged with the resolved directory because the usual cause is that the
    # served tree was replaced under a running service, which is otherwise
    # invisible from the supervisor logs.
    _loguru_logger.warning(
        "Served the not-built placeholder: no frontend bundle at {}", get_state().static_directory / "index.html"
    )
    return _shell_response(render_frontend_not_built_page(terminal_origin_label()), is_frontend_built=False)


def _index_catch_all(path: str) -> Response:
    # An agent-authored file is addressed by its absolute on-disk path, which
    # lands here as a catch-all path; serve it (image inline, any other existing
    # file as a download) before falling through to the single-page-app shell.
    # Paths that match no file are client-side routes and render the app as before.
    file_response = try_serve_file(path)
    if file_response is not None:
        return file_response
    return _index()


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
    # What keeps an asset request off the SPA catch-all is the route itself,
    # registered unconditionally in ``create_application``; nothing here
    # decides that.
    try:
        return send_from_directory(assets_directory, filename)
    except NotFound:
        return Response(status=404)


def _discover_with_filters() -> list[AgentInfo]:
    """Discover agents using the app-level filter configuration."""
    state = get_state()
    return discover_agents(
        provider_names=state.provider_names,
        include_filters=state.include_filters,
        exclude_filters=state.exclude_filters,
    )


def _list_agents_endpoint() -> Response:
    """List all mngr-managed agents."""
    agents = _discover_with_filters()
    items = [AgentListItem(id=agent.id, name=agent.name, state=agent.state) for agent in agents]
    return _json_response(AgentListResponse(agents=items).model_dump())


def _find_agent(agent_id: str) -> AgentInfo | None:
    """Find a specific agent by ID, from the AgentManager's already-loaded state."""
    agent_manager: AgentManager = get_state().agent_manager
    return agent_manager.get_agent_info_by_id(agent_id)


def _agent_not_found_response(agent_id: str) -> Response:
    error = ErrorResponse(detail=f"Agent '{agent_id}' not found")
    return _json_response(error.model_dump(), status_code=404)


def _get_events(agent_id: str) -> Response:
    """Get events for an agent. Supports tail-first loading and backfill."""
    agent_info = _find_agent(agent_id)
    if agent_info is None:
        return _agent_not_found_response(agent_id)

    before_event_id = request.args.get("before")
    after_event_id = request.args.get("after")
    offset_str = request.args.get("offset")
    limit_str = request.args.get("limit", str(_DEFAULT_TAIL_COUNT))
    try:
        limit = int(limit_str)
    except ValueError:
        limit = _DEFAULT_TAIL_COUNT
    # A non-positive limit would defeat the window cap and break slicing, so fall
    # back to the default.
    if limit <= 0:
        limit = _DEFAULT_TAIL_COUNT

    watcher = get_state().get_or_create_watcher(agent_info)
    if before_event_id:
        # Page older: the `limit` events immediately before the cursor.
        events = watcher.get_backfill_events(before_event_id, limit=limit)
    elif after_event_id:
        # Page newer: the `limit` events immediately after the cursor (used when
        # the loaded window has been moved off the live tail by a jump).
        events = watcher.get_forward_events(after_event_id, limit=limit)
    elif offset_str is not None:
        # Jump: a `limit`-event window starting at an arbitrary global index, so
        # the client can land at a far scroll position in one bounded read.
        try:
            offset = int(offset_str)
        except ValueError:
            offset = 0
        events = watcher.get_events_at_offset(offset, limit)
    else:
        # Initial load: the newest `limit` events (the live tail). Bounded read
        # from the end; the client pages/jumps from here.
        events = watcher.get_tail_events(limit)

    # `total` is the full transcript length and `offset` is the global index of the
    # first returned event. Together they place the loaded window in the whole
    # conversation, so the client sizes the scrollbar for the full length and
    # derives whether more history exists above (offset > 0) and below
    # (offset + len < total) -- no separate has_more flag needed.
    total = watcher.get_total_event_count()
    offset = watcher.get_event_offset(events[0]["event_id"]) if events else total
    return _json_response({"events": events, "offset": offset, "total": total})


def _stream_filtered_events(
    agent_id: str,
    event_queues: AgentEventQueues,
    event_queue: "queue.Queue[dict[str, Any] | None]",
    should_forward: Callable[[dict[str, Any]], bool],
) -> Iterator[str]:
    """Yield SSE frames for queued events that pass ``should_forward``.

    Shared by the main agent stream and the per-subagent stream, which differ
    only in which events they keep: the main stream drops subagent-session
    events (they belong to the per-subagent stream, and would otherwise render
    the subagent's own prompt and tool calls inline in the parent thread),
    while the subagent stream keeps only its own session. Filtered-out events
    do not reset the keepalive counter. A ``None`` from the queue (shutdown
    sentinel) ends the stream.
    """
    keepalive_counter = 0
    _loguru_logger.info("SSE stream opened for agent {} (conn {})", agent_id, id(event_queue))
    close_reason = "event-queues shutdown"
    try:
        while not event_queues.is_shutdown:
            try:
                event = event_queue.get(timeout=1)
                if event is None:
                    close_reason = "queue shutdown sentinel"
                    break
                if not should_forward(event):
                    continue
                keepalive_counter = 0
                yield f"data: {json.dumps(event)}\n\n"
            except queue.Empty:
                keepalive_counter += 1
                if keepalive_counter >= 8:
                    keepalive_counter = 0
                    yield ": keepalive\n\n"
    except GeneratorExit:
        close_reason = "client disconnected"
    finally:
        _loguru_logger.info(
            "SSE stream closed for agent {} (conn {}, reason: {})", agent_id, id(event_queue), close_reason
        )
        event_queues.unregister(agent_id, event_queue)


def _sse_response(generator: Iterator[str]) -> Response:
    return Response(
        generator,
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _stream_events(agent_id: str) -> Response:
    """SSE stream for an agent's new events."""
    agent_info = _find_agent(agent_id)
    if agent_info is None:
        return _agent_not_found_response(agent_id)

    state = get_state()
    watcher = state.get_or_create_watcher(agent_info)

    event_queues = state.event_queues
    event_queue = event_queues.register(agent_id)

    return _sse_response(_stream_filtered_events(agent_id, event_queues, event_queue, watcher.is_main_session_event))


def _send_message_endpoint(agent_id: str) -> Response:
    """Send a message to an agent."""
    agent_info = _find_agent(agent_id)
    if agent_info is None:
        return _agent_not_found_response(agent_id)

    send_message_request = SendMessageRequest.model_validate(request.get_json())
    state = get_state()
    agent_manager: AgentManager = state.agent_manager
    message_id = send_message_request.message_id or uuid4().hex

    # Ensure the watcher exists BEFORE the send, as the tap and stop endpoints already do. For
    # a harness that holds its own queue (antigravity), the watcher owns the only thread that
    # can ever deliver it -- so a send arriving here first (a headless client, or the first
    # request after a restart) would otherwise enqueue a message with nothing running to drain
    # it, and decide "is a turn open?" from an unpublished reading.
    state.get_or_create_watcher(agent_info)

    # The agent's session owns the whole send lifecycle (contract A1/A2): the file session
    # records the message as *Sending* around mngr's blocking delivery (greying the tap button
    # for the duration); the codex session hands it to its live ledger, passing ``message_id``
    # only as the correlation token the committed item echoes back (Fix 2).
    session = agent_manager.get_or_create_session(agent_info)
    try:
        outcome = session.send(send_message_request.message, message_id)
    except SendFailedError as send_failure:
        # The harness said why it refused, in words written for the person who has to fix it
        # ("the agent is in shell mode with an unsubmitted command"). Pass that through rather
        # than the generic failure below -- it is the only thing here the user can act on.
        # The kind travels beside the detail so the chat can decide what to offer: trying again
        # can clear a blocked input and cannot help when there is nothing left to talk to.
        return _json_response({"detail": send_failure.detail, "kind": send_failure.kind}, status_code=500)
    if outcome is SendOutcome.NOT_READY:
        failure = ErrorResponse(
            detail=f"Agent '{agent_info.name}' is not ready to receive messages yet (its daemon is starting)."
        )
        return _json_response(failure.model_dump(), status_code=503)
    if outcome is SendOutcome.FAILED:
        failure = ErrorResponse(detail=f"Failed to send message to agent '{agent_info.name}' (0 successful agents)")
        return _json_response(failure.model_dump(), status_code=500)

    _record_client_message_activity(agent_info, send_message_request)
    return _json_response(SendMessageResponse(status="ok").model_dump())


def _record_client_message_activity(agent_info: AgentInfo, send_message_request: SendMessageRequest) -> None:
    """Record which client (and layout) a message came from, so agents can attribute requests to a
    client via ``layout.py context``. Legacy callers without client metadata are not recorded."""
    events_path = _client_activity_events_path()
    if events_path is not None and send_message_request.client_id:
        client_activity.append_message_event(
            events_path,
            client_id=send_message_request.client_id,
            device_kind=send_message_request.device_kind,
            layout_slug=send_message_request.active_layout,
            agent_id=agent_info.id,
            agent_name=agent_info.name,
            message_text=send_message_request.message,
        )


def _get_harnesses_endpoint() -> Response:
    """The static per-harness model catalogs -- the model bar's compile-time half.

    One response covers every harness (each catalog dumped verbatim: options,
    switch mode, picker mode, powered-by label, shoulder-tap capability); the
    frontend keys in by an agent's harness.

    Every harness is always included, deliberately: what the user has signed in to
    decides what they can LAUNCH, not what the app can render. A codex or pi agent that
    exists some other way (``mngr create``, or one left behind after its account was
    removed) still needs its catalog for the model bar to resolve, so narrowing this to
    the signed-in harnesses would strand that agent's chip on an unrecognized model.
    """
    catalogs: dict[str, Any] = {}
    for harness in HARNESS_SPECS:
        # A parsed catalog (pi) reads data files; a bad/absent one must be
        # skipped, not 500 the endpoint for every other harness.
        try:
            catalog = get_catalog(harness).model_dump()
        except (OSError, ValueError) as e:
            logger.warning("Skipping model catalog for harness {}: {}", harness.value, e)
            continue
        # The catalog model is the wire shape for the model bar; the popup declarations
        # live on the HarnessSpec and are merged in here so one response carries
        # everything the frontend keys by harness.
        spec = get_harness_spec(harness)
        catalog["popups"] = [popup.model_dump() for popup in spec.popups]
        catalogs[harness.value] = catalog
    return _json_response(catalogs)


def _agent_switch_options(agent_manager: "AgentManager", agent_info: AgentInfo) -> tuple[ModelOption, ...]:
    """The option set the switch endpoint validates against: per-agent for codex, static otherwise.

    Codex has no static catalog, so its valid model/effort/fast set is per-agent -- the ONE reconciled
    set (:meth:`AgentManager.get_codex_model_options`) that the picker offered and the chip matches
    against, seeded on connect and refreshed by each picker-open (D2), falling back to the persisted
    sidecar while that in-memory set is empty (post-restart). Empty (no set and no sidecar) only until
    first populated -- a switch then fails validation, which is correct: nothing to switch to until a
    connect, a picker-open, or a persisted sidecar supplies the account's ``model/list``. Every other
    harness validates against its static catalog options.
    """
    return agent_manager.get_or_create_session(agent_info).switch_options()


def _set_model_choice_endpoint(agent_id: str) -> Response:
    """Apply a model/effort/fast selection by asking the agent's resolver to switch.

    Harness-blind: it validates the request against the agent's option set (the static catalog for
    claude/pi, the per-agent ``model/list`` set for codex), then hands a concrete identity to the
    resolver's ``switch`` (which decides how to apply it). Returns 400 for an invalid selection, 404
    for an unknown agent, 500 when the switch fails. On success it forces one authoritative
    model-choice broadcast so the frontend reconciles.
    """
    agent_info = _find_agent(agent_id)
    if agent_info is None:
        return _agent_not_found_response(agent_id)

    req = SetModelChoiceRequest.model_validate(request.get_json())
    agent_manager: AgentManager = get_state().agent_manager
    options = _agent_switch_options(agent_manager, agent_info)
    # The picker only ever sends a valid option id, so validation is an exact id lookup.
    option = next((opt for opt in options if opt.id == req.model_id), None)
    if option is None:
        return _json_response(ErrorResponse(detail=f"Unknown model '{req.model_id}'").model_dump(), status_code=400)

    # Flat guards (rather than a branch per axis-presence) so effort is validated
    # against the model's declared set: required + in-set when the model has efforts,
    # and absent when it does not.
    declared_efforts = {choice.level for choice in option.efforts}
    has_effort_axis = len(option.efforts) > 0
    if has_effort_axis and req.effort is None:
        return _json_response(ErrorResponse(detail="This model requires an effort level").model_dump(), 400)
    if has_effort_axis and req.effort is not None and req.effort not in declared_efforts:
        return _json_response(
            ErrorResponse(detail=f"'{req.effort}' is not a valid effort for '{req.model_id}'").model_dump(), 400
        )
    if not has_effort_axis and req.effort is not None:
        return _json_response(ErrorResponse(detail=f"'{req.model_id}' has no effort axis").model_dump(), 400)
    if req.fast and not option.supports_fast:
        return _json_response(ErrorResponse(detail=f"'{req.model_id}' does not support fast mode").model_dump(), 400)

    # The live read is harness-neutral (shared reader), so the resolver -- which now owns
    # only the switch/offer side -- is built inline from agent_info rather than cached.
    resolver = build_resolver(agent_info)

    identity = ModelIdentity(model_id=req.model_id, effort=req.effort, fast=req.fast)
    result = resolver.switch(
        identity,
        frozenset(req.axes),
        lambda line: agent_manager.send_message_to_agent(AgentId(agent_info.id), line) is None,
    )
    if not result.ok:
        detail = result.detail or f"Failed to switch model for agent '{agent_info.name}'"
        return _json_response(ErrorResponse(detail=detail).model_dump(), status_code=500)

    # Force one authoritative broadcast so the optimistic pick reconciles even when
    # the resolved value is unchanged (see H1 in the model-bar plan).
    agent_manager.refresh_model_choice(agent_info.id)
    return _json_response(SendMessageResponse(status="ok").model_dump())


def _get_model_options_endpoint(agent_id: str) -> Response:
    """The models this agent should OFFER in the picker right now.

    Recomputed per request (the frontend calls it each time the picker opens). Two shapes:

    * a DYNAMIC harness (codex) has no static catalog, so it returns the FULL per-agent
      :class:`ModelOption`s (``options``) -- id, label, per-model efforts, fast support -- fetched
      fresh from ``model/list`` on this open (D2), so a subscription-tier change shows up live.
    * a static/catalog-backed harness (claude, pi) returns ``models`` -- the ids to offer, matched
      back to the static catalog for labels/efforts (``null`` = offer the whole catalog). This
      reflects an account-gated set (pi's authenticated models) on a fresh login without a refetch.
    """
    agent_info = _find_agent(agent_id)
    if agent_info is None:
        return _agent_not_found_response(agent_id)
    resolver = build_resolver(agent_info)
    dynamic_options = resolver.list_offered_options()
    if dynamic_options is not None:
        # Reconcile (D2): this fresh per-open fetch becomes the ONE per-agent set the chip-match and
        # the switch-validation also read, so immediately after this open all three agree. A failed
        # fetch (empty) is NOT stored -- it must not clobber the last-known set (seeded on connect or
        # from an earlier open) that the chip is still matching against. The RAW list behind these
        # mapped options is also written through to the codex sidecar inside the resolver's
        # ``list_offered_options`` (where the raw ``model/list`` is still in hand), so the chip
        # resolves offline after a restart.
        if dynamic_options:
            get_state().agent_manager.get_or_create_session(agent_info).note_offered_options(dynamic_options)
        return _json_response(ModelOptionsResponse(options=dynamic_options).model_dump())
    return _json_response(ModelOptionsResponse(models=resolver.list_offered_models()).model_dump())


def _get_powered_by_endpoint(agent_id: str) -> Response:
    """The agent's credit text -- a per-agent path decoupled from the model bar.

    The text is a pure function of the agent's harness, so it must never blink with the live
    model choice or wait on the catalog fetch. This resolves the harness backend-side and
    returns the harness's verbatim credit string, so the frontend can render it from ``agentId``
    alone, independent of ``model_choice`` and of ``GET /api/harnesses``. A harness that shows
    no credit (claude) declares "", which the frontend renders as nothing. 404 for an unknown
    agent (e.g. a proto-agent), which the frontend also treats as "no credit".
    """
    agent_info = _find_agent(agent_id)
    if agent_info is None:
        return _agent_not_found_response(agent_id)
    return _json_response(PoweredByResponse(label=get_catalog(agent_info.harness).powered_by_text).model_dump())


def _build_fast_mode_answered_label_command(agent_name: str) -> list[str]:
    """Build the ``mngr label`` argv that latches the fast-mode prompt as answered.

    Pure: argv assembly only, so the repo<->mngr CLI contract is testable
    against the live CLI without a subprocess (see ``server_test.py``).
    """
    return ["mngr", "label", agent_name, "-l", "fast_mode_prompt_answered=true"]


def _mark_fast_mode_prompt_answered(agent_id: str) -> Response:
    """Latch the fast-mode prompt as answered for one agent, via an agent label.

    The prompt asks once per agent, ever: any exit from the modal routes here, so
    the label is the durable record that the question was put to the user. The
    label reaches the frontend with the next observe relist; the frontend keeps
    its own in-session mark so the prompt cannot re-fire in the meantime.
    """
    agent_manager: AgentManager = get_state().agent_manager
    agent_state = agent_manager.get_agent_by_id(agent_id)
    if agent_state is None:
        error = ErrorResponse(detail=f"Agent '{agent_id}' not found")
        return _json_response(error.model_dump(), status_code=404)

    result = run_local_command_modern_version(
        command=_build_fast_mode_answered_label_command(agent_state.name),
        cwd=None,
        is_checked=False,
        timeout=_LABEL_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        detail = f"Failed to record the fast-mode answer for '{agent_state.name}': {result.stderr.strip()}"
        return _json_response(ErrorResponse(detail=detail).model_dump(), status_code=500)

    return _json_response(FastModePromptAnsweredResponse(status="ok").model_dump())


def _activity_endpoint() -> Response:
    """Report the workspace UI's current agent-tab activity for OOM prioritization.

    The frontend posts a snapshot ({open, visible, messaged}) whenever a tab
    opens/closes, the visible tab changes, or a message is sent. The agent manager
    hands it to the chat OOM prioritizer, which re-tags each chat agent's
    ``oom_score_adj`` so more-engaged chats are more protected from a memory shed
    (workers and the primary agent are excluded and never re-tagged). Best-effort
    and idempotent: the endpoint just records the snapshot and returns ok.
    """
    activity_request = ActivityRequest.model_validate(request.get_json())
    agent_manager: AgentManager = get_state().agent_manager
    agent_manager.record_activity(
        open_ids=activity_request.open,
        visible_ids=activity_request.visible,
        messaged_id=activity_request.messaged,
    )
    return _json_response(ActivityResponse(status="ok").model_dump())


def _upload_attachment() -> Response:
    """Store a file the user attached to a chat message under data/uploads/.

    The frontend uploads each attachment here as soon as the user drops, pastes,
    or picks it, then appends the returned absolute path to the message text it
    sends to the agent. Returns the stored path and size so the composer can show
    a preview and reference the file.
    """
    file_storage = request.files.get("file")
    if file_storage is None or not file_storage.filename:
        error = ErrorResponse(detail="No file provided in the 'file' field")
        return _json_response(error.model_dump(), status_code=400)

    uploads_directory = get_uploads_directory()
    try:
        stored_path = store_uploaded_file(uploads_directory, file_storage.filename, file_storage)
    except AttachmentError as e:
        error = ErrorResponse(detail=str(e))
        return _json_response(error.model_dump(), status_code=500)

    size_bytes = stored_path.stat().st_size
    response = AttachmentUploadResponse(path=str(stored_path), size=size_bytes)
    return _json_response(response.model_dump(), status_code=201)


def _serve_attachment(relative_path: str) -> Response:
    """Serve a stored attachment for inline preview, confined to data/uploads/."""
    resolved_path = resolve_upload_path(get_uploads_directory(), relative_path)
    if resolved_path is None:
        error = ErrorResponse(detail=f"Attachment '{relative_path}' not found")
        return _json_response(error.model_dump(), status_code=404)
    return send_file(resolved_path)


def _delete_attachment(relative_path: str) -> Response:
    """Delete a stored attachment when the user removes it before sending.

    Idempotent: a path that is missing or escapes the uploads directory is a
    no-op, so a double-remove or a stale id still reports success.
    """
    delete_upload(get_uploads_directory(), relative_path)
    return _json_response({"status": "ok"})


def _interrupt_agent_endpoint(agent_id: str) -> Response:
    """Interrupt an agent's current turn by restarting it.

    Runs ``mngr start <agent> --restart --no-resume``, which stops the agent
    (ending any in-progress turn) and starts it fresh without sending a resume
    message. Returns 404 if the agent is unknown, 400 if the agent carries the
    ``is_primary=true`` label, 500 if the restart command fails, 200 otherwise.

    Refuses to interrupt agents carrying the ``is_primary=true`` label: that's
    the services agent for the workspace, and restarting it would stop the
    bootstrap, web, share-gateway, and other supervised services. The
    frontend already hides ``is_primary=true`` agents from the visible agent
    list; this is defense-in-depth for callers that hit the endpoint directly
    (curl, scripted use, etc.).
    """
    agent_info = _find_agent(agent_id)
    if agent_info is None:
        return _agent_not_found_response(agent_id)

    if agent_info.labels.get("is_primary") == "true":
        error = ErrorResponse(
            detail=(
                f"Refusing to interrupt agent '{agent_info.name}': it carries "
                "the is_primary=true label (services agent for this workspace)"
            )
        )
        return _json_response(error.model_dump(), status_code=400)

    agent_name = agent_info.name

    is_restarted, output = _restart_agent_process(agent_name)
    if not is_restarted:
        error = ErrorResponse(detail=f"Failed to interrupt agent '{agent_name}': {output}")
        return _json_response(error.model_dump(), status_code=500)

    # The restart abandons the session transcript mid-turn, so the
    # transcript-derived activity state would stay pinned at THINKING /
    # TOOL_RUNNING until the user sends another message. Reset it to IDLE
    # now so the activity indicator clears immediately after the stop.
    get_state().agent_manager.reset_activity_state(agent_id)

    return _json_response(InterruptAgentResponse(status="ok").model_dump())


def _restart_agent_process(agent_name: str) -> tuple[bool, str]:
    """Run ``mngr start <agent> --restart --no-resume``; return ``(is_restarted, output)``.

    Stops the agent (ending any in-progress turn) and relaunches it fresh without
    a resume prompt: conversation history is preserved (each harness resumes its
    own on-disk session) and the in-harness queue is dropped by the SIGKILL.
    ``output`` is stdout on success, stderr on failure (for the caller's message).
    Refused by mngr for an ``is_primary=true`` agent; callers guard that with a
    clearer 400 before calling.
    """
    result = run_local_command_modern_version(
        command=["mngr", "start", agent_name, "--restart", "--no-resume"],
        cwd=None,
        is_checked=False,
        timeout=60.0,
    )
    is_restarted = result.returncode == 0
    return is_restarted, (result.stdout.strip() if is_restarted else result.stderr.strip())


def _refuse_queue_action_on_primary(agent_info: AgentInfo, action: str) -> Response | None:
    """A 400 refusing a restart-based queue action on the primary services agent, or None.

    Both queue actions restart the agent; restarting the ``is_primary=true``
    services agent would tear down the workspace's supervised services. The
    frontend hides primary agents, so this is defense-in-depth for direct callers.
    """
    if agent_info.labels.get("is_primary") == "true":
        error = ErrorResponse(
            detail=(
                f"Refusing to {action} agent '{agent_info.name}': it carries the "
                "is_primary=true label (services agent for this workspace)"
            )
        )
        return _json_response(error.model_dump(), status_code=400)
    return None


def _interrupt_capabilities(
    agent_info: AgentInfo,
) -> tuple[AgentSessionWatcher, Callable[[], tuple[bool, str]], Callable[[], None]]:
    """The harness-neutral capabilities a queue action binds for one agent: the queue mirror,
    a process restart (``mngr start --restart --no-resume``), and an activity-settle.

    Shared by the restart-drain flush and the (per-harness) stop button, mirroring how the
    switch endpoint binds its ``send`` callback.
    """
    state = get_state()
    watcher = state.get_or_create_watcher(agent_info)
    return (
        watcher,
        lambda: _restart_agent_process(agent_info.name),
        lambda: state.agent_manager.reset_activity_state(agent_info.id),
    )


def _flush_queue_endpoint(agent_id: str) -> Response:
    """Shoulder tap: restart the agent and resend the whole queue as one turn.

    Combining is required: after the restart the agent is idle, so sending the
    messages one at a time would let the first open a turn and the rest re-queue.
    Returns 404 for an unknown agent, 400 for the primary services agent, 500 if
    the restart or the resend fails, 200 otherwise.
    """
    agent_info = _find_agent(agent_id)
    if agent_info is None:
        return _agent_not_found_response(agent_id)
    refusal = _refuse_queue_action_on_primary(agent_info, "flush the queue of")
    if refusal is not None:
        return refusal

    watcher, restart_process, settle_activity = _interrupt_capabilities(agent_info)
    # Empty-queue short-circuit lives HERE (not in the shared restart-drain): a flush with
    # nothing queued would resend nothing, so it is a clean no-op. The stop button, by contrast,
    # still interrupts an empty-queue turn -- so the restart-drain no longer short-circuits.
    if not watcher.get_queued_block():
        return _json_response(SendMessageResponse(status="ok").model_dump())

    try:
        block = restart_drain(agent_info, watcher, restart_process, settle_activity)
    except AgentRestartError as e:
        return _json_response(ErrorResponse(detail=str(e)).model_dump(), status_code=500)

    if block:
        agent_manager: AgentManager = get_state().agent_manager
        resend_failure = agent_manager.send_message_to_agent(AgentId(agent_info.id), block)
        if resend_failure is not None:
            # The harness said why; passing that on rather than a generic sentence is the whole
            # point of carrying it this far.
            return _json_response({"detail": resend_failure.reason, "kind": resend_failure.kind}, status_code=500)

    return _json_response(SendMessageResponse(status="ok").model_dump())


def _shoulder_tap_atomic_endpoint(agent_id: str) -> Response:
    """Atomic shoulder tap: merge the queue into the live turn without restarting the agent.

    The gentle counterpart to :func:`_flush_queue_endpoint`: rather than SIGKILL-restart the
    agent and resend the queue, the agent's session delivers the harness's native tap and the
    agent stays alive. HOW each harness taps lives with its implementation -- claude's cancel
    chord in ``harnesses/claude/tap.py`` (``ClaudeAtomicShoulderTap``), pi's locked
    ``pi_inbox`` flush sentinel in ``harnesses/pi_coding/model.py`` (``PiAtomicShoulderTap``),
    codex's live-ledger interrupt+resend in ``harnesses/codex/session.py`` -- not here.

    Returns 404 for an unknown agent, 400 for a harness whose catalog declares no atomic tap
    or for the primary services agent, an error status when the tap failed (e.g. a claude
    dialog block maps to 409), and 200 otherwise with the harness's own verdict (``tapped``,
    ``no_open_turn``, or the benign ``send_in_flight`` no-op a raced send produces).
    """
    agent_info = _find_agent(agent_id)
    if agent_info is None:
        return _agent_not_found_response(agent_id)
    if not get_catalog(agent_info.harness).native_atomic_shoulder_tap_possible:
        error = ErrorResponse(
            detail=(
                f"Agent '{agent_info.name}' runs the {agent_info.harness.value} harness, which does not "
                "support an atomic shoulder tap"
            )
        )
        return _json_response(error.model_dump(), status_code=400)
    refusal = _refuse_queue_action_on_primary(agent_info, "shoulder-tap the queue of")
    if refusal is not None:
        return refusal

    # The session dispatches to the harness's native tap (claude's chord executor, pi's locked
    # inbox sentinel, codex's live-ledger interrupt+resend). A retryable refusal racing an
    # in-flight send is a benign 200 no-op status, never an error dialog -- the pushed
    # ``shoulder_tap_available`` flag already greys the button while anything is Sending.
    state = get_state()
    watcher = state.get_or_create_watcher(agent_info)
    agent_manager = state.agent_manager
    outcome = agent_manager.get_or_create_session(agent_info).shoulder_tap(
        agent_info,
        watcher,
        press_chord=lambda: agent_manager.press_key_chord_on_agent(
            AgentId(agent_info.id), get_harness_spec(agent_info.harness).cancel_chord
        ),
        send_recovery=lambda text: agent_manager.send_message_to_agent(AgentId(agent_info.id), text) is None,
    )
    if outcome.error_detail is not None:
        error = ErrorResponse(detail=outcome.error_detail)
        return _json_response(error.model_dump(), status_code=outcome.error_status_code)
    return _json_response(ShoulderTapAtomicResponse(status=outcome.status, block=outcome.block).model_dump())


def _drain_to_composer_endpoint(agent_id: str) -> Response:
    """Interrupt to composer: interrupt the running turn and hand the queued block back, unsent.

    Dispatches through the harness's registered interrupt-to-composer implementation (the base
    restart-drain by default; native overrides for pi, codex, and claude's empty-queue chord),
    which returns the concatenated block the frontend drops into the composer for the user to
    edit and send, rather than resent. Unlike the flush there is NO empty-queue short-circuit: a
    stop mid-turn with nothing queued still interrupts (block comes back empty). The endpoint
    binds the harness-neutral capabilities -- watcher, restart, activity-settle, and the native
    cancel keypress (routed through mngr's locked message API, like the tap) -- and the
    implementation uses whichever it needs. Returns 404 for an unknown agent, 400 for the primary
    services agent, 500 if the interrupt fails, 200 with ``{block}`` otherwise.
    """
    agent_info = _find_agent(agent_id)
    if agent_info is None:
        return _agent_not_found_response(agent_id)
    refusal = _refuse_queue_action_on_primary(agent_info, "interrupt the queue of")
    if refusal is not None:
        return refusal

    agent_manager: AgentManager = get_state().agent_manager

    watcher, restart_process, settle_activity = _interrupt_capabilities(agent_info)
    try:
        block = agent_manager.get_or_create_session(agent_info).interrupt_to_composer(
            agent_info,
            watcher,
            restart_process,
            settle_activity,
            lambda: agent_manager.press_key_chord_on_agent(
                AgentId(agent_info.id), get_harness_spec(agent_info.harness).cancel_chord
            ),
        )
    except AgentRestartError as e:
        return _json_response(ErrorResponse(detail=str(e)).model_dump(), status_code=500)
    except OSError as e:
        logger.opt(exception=e).error("Failed to record the interrupt for agent {}", agent_info.name)
        error = ErrorResponse(detail=f"Failed to record the interrupt for agent '{agent_info.name}'")
        return _json_response(error.model_dump(), status_code=500)

    return _json_response(DrainToComposerResponse(block=block).model_dump())


def _get_subagent_events(agent_id: str, subagent_session_id: str) -> Response:
    """Get events for a specific subagent session."""
    agent_info = _find_agent(agent_id)
    if agent_info is None:
        return _agent_not_found_response(agent_id)

    watcher = get_state().get_or_create_watcher(agent_info)
    events = watcher.get_all_events(session_id=subagent_session_id)

    # Include metadata in the response
    metadata = watcher.get_subagent_metadata(subagent_session_id)

    return _json_response({"events": events, "metadata": metadata})


def _stream_subagent_events(agent_id: str, subagent_session_id: str) -> Response:
    """SSE stream for a subagent's new events, filtered by session_id."""
    agent_info = _find_agent(agent_id)
    if agent_info is None:
        return _agent_not_found_response(agent_id)

    state = get_state()
    state.get_or_create_watcher(agent_info)

    event_queues = state.event_queues
    event_queue = event_queues.register(agent_id)

    return _sse_response(
        _stream_filtered_events(
            agent_id,
            event_queues,
            event_queue,
            lambda event: event.get("session_id") == subagent_session_id,
        )
    )


# Stores the user's "never show again" choice for the terminal lifecycle banner,
# alongside the named layouts in the primary agent's workspace_layout dir.
_TERMINAL_BANNER_FILENAME = "terminal_banner.json"

# Serializes terminal-name allocation and tracks names handed out but not yet
# materialized as live tmux sessions (session creation is lazy, on ttyd connect).
_terminal_allocate_lock = threading.Lock()
_recently_allocated_terminal_names: set[str] = set()


def _primary_agent_layout_dir() -> Path | None:
    """Return the workspace layout directory for this workspace's primary agent.

    The system_interface always serves a single workspace (its own primary
    agent); the layout lives at $MNGR_HOST_DIR/agents/<MNGR_AGENT_ID>/workspace_layout/.
    Returns None if either env var is missing, which should only happen in
    dev/test setups that don't care about persistence.
    """
    agent_id = os.environ.get("MNGR_AGENT_ID", "")
    if not agent_id:
        return None
    return projects.primary_agent_layout_dir(get_host_dir(), agent_id)


def _client_activity_events_path() -> Path | None:
    """Where the workspace-level client-activity event log lives, or None."""
    layout_dir = _primary_agent_layout_dir()
    if layout_dir is None:
        return None
    return client_activity.get_events_path(layout_dir)


def _parse_json_object_body() -> dict[str, Any] | Response:
    """Parse the request body as a JSON object, or return a 400 error response."""
    try:
        body = json.loads(request.get_data())
    except (json.JSONDecodeError, ValueError) as e:
        _loguru_logger.opt(exception=e).warning("Request to {} carried invalid JSON", request.path)
        error = ErrorResponse(detail="Invalid JSON in request body")
        return _json_response(error.model_dump(), status_code=400)
    if not isinstance(body, dict):
        error = ErrorResponse(detail="Request body must be a JSON object")
        return _json_response(error.model_dump(), status_code=400)
    return body


def _default_project_infos() -> list[dict[str, Any]]:
    """The starter project, for dev/test setups with no layout dir.

    Mirrors the entry ``projects.py`` seeds into a real registry; nothing is
    persisted in that case, so the display metadata is inlined here rather
    than read back from a file that will never exist.
    """
    return [
        projects.ProjectInfo(
            project_id=projects.DEFAULT_PROJECT_ID,
            name=projects.DEFAULT_PROJECT_NAME,
            color=projects.DEFAULT_PROJECT_COLOR,
            glyph=projects.DEFAULT_PROJECT_GLYPH,
            has_content=False,
            members=(),
        ).model_dump()
    ]


def _parse_project_metadata_body() -> tuple[str, str, int] | Response:
    """Parse the ``{name, color, glyph}`` body shared by project create and settings.

    Only shape is checked here; the value rules (usable name, ``#RRGGBB``
    color, in-range glyph) belong to ``projects.py`` and surface as its own
    errors, so the two callers map them to HTTP identically.
    """
    body = _parse_json_object_body()
    if isinstance(body, Response):
        return body
    name = body.get("name")
    color = body.get("color")
    glyph = body.get("glyph")
    if not isinstance(name, str) or not name.strip():
        error = ErrorResponse(detail="'name' must be a non-empty string")
        return _json_response(error.model_dump(), status_code=400)
    if not isinstance(color, str):
        error = ErrorResponse(detail="'color' must be a '#RRGGBB' string")
        return _json_response(error.model_dump(), status_code=400)
    # ``bool`` is an ``int`` subclass, so reject it explicitly rather than
    # letting ``true`` address the second glyph.
    if not isinstance(glyph, int) or isinstance(glyph, bool):
        error = ErrorResponse(detail="'glyph' must be an integer index into the glyph table")
        return _json_response(error.model_dump(), status_code=400)
    return name.strip(), color, glyph


def _project_metadata_error_response(e: ValueError) -> Response:
    """Map a rejected name / color / glyph onto a 400 with the module's own message."""
    return _json_response(ErrorResponse(detail=str(e)).model_dump(), status_code=400)


def _project_not_found_response(project_id: str) -> Response:
    error = ErrorResponse(detail=f"Project '{project_id}' not found")
    return _json_response(error.model_dump(), status_code=404)


def _list_projects_endpoint() -> Response:
    """List every project -- members included -- plus the last-active project id."""
    layout_dir = _primary_agent_layout_dir()
    if layout_dir is None:
        # No primary agent configured (dev/test): expose the starter project so
        # the frontend can still pick an active one; nothing persists.
        return _json_response({"projects": _default_project_infos(), "last_active_id": projects.DEFAULT_PROJECT_ID})
    infos = projects.list_projects(layout_dir)
    return _json_response(
        {
            "projects": [info.model_dump() for info in infos],
            "last_active_id": projects.get_last_active_id(layout_dir),
        }
    )


def _create_project_endpoint() -> Response:
    """Register a new empty project from the posted display metadata.

    The server owns slugification, so two names that shorten to the same id
    are rejected rather than silently sharing one content file.
    """
    layout_dir = _primary_agent_layout_dir()
    if layout_dir is None:
        error = ErrorResponse(detail="No primary agent configured for this workspace")
        return _json_response(error.model_dump(), status_code=500)
    parsed = _parse_project_metadata_body()
    if isinstance(parsed, Response):
        return parsed
    name, color, glyph = parsed
    try:
        info = projects.create_project(layout_dir, name, color, glyph)
    except (projects.ProjectNameError, projects.ProjectColorError, projects.ProjectGlyphError) as e:
        return _project_metadata_error_response(e)
    except projects.ProjectConflictError as e:
        return _json_response(ErrorResponse(detail=str(e)).model_dump(), status_code=409)
    get_state().broadcaster.broadcast({"type": "project_updated", **info.model_dump()})
    return _json_response(info.model_dump())


def _get_project_endpoint(project_id: str) -> Response:
    """Get one project's saved content (null when the project is still empty).

    ``?device=desktop|mobile`` selects which device's arrangement to read
    (default desktop); each client passes its own UA-derived kind.
    """
    layout_dir = _primary_agent_layout_dir()
    if layout_dir is None:
        return _json_response({"layout": None})
    device = request.args.get("device", projects.DEFAULT_DEVICE)
    try:
        content = projects.read_project_content(layout_dir, project_id, device)
    except projects.ProjectDeviceError as e:
        return _json_response(ErrorResponse(detail=str(e)).model_dump(), status_code=400)
    except projects.ProjectNotFoundError:
        return _project_not_found_response(project_id)
    return _json_response({"layout": content})


def _autosave_project_endpoint(project_id: str) -> Response:
    """Persist the posted content to an existing project (the autosave path)."""
    layout_dir = _primary_agent_layout_dir()
    if layout_dir is None:
        error = ErrorResponse(detail="No primary agent configured for this workspace")
        return _json_response(error.model_dump(), status_code=500)
    body = _parse_json_object_body()
    if isinstance(body, Response):
        return body
    layout_content = body.get("layout")
    client_id = str(body.get("client_id") or "")
    device = str(body.get("device") or projects.DEFAULT_DEVICE)
    if not isinstance(layout_content, dict):
        error = ErrorResponse(detail="'layout' must be a JSON object")
        return _json_response(error.model_dump(), status_code=400)
    try:
        projects.write_project_content(layout_dir, project_id, layout_content, device)
    except projects.ProjectDeviceError as e:
        return _json_response(ErrorResponse(detail=str(e)).model_dump(), status_code=400)
    except projects.ProjectNotFoundError:
        # The project was deleted while this client's autosave was in flight;
        # the client hears about the deletion over the WebSocket.
        return _project_not_found_response(project_id)
    get_state().broadcaster.broadcast(
        {"type": "project_saved", "project_id": project_id, "saved_by_client_id": client_id, "device": device}
    )
    return _json_response({"status": "ok"})


def _update_project_settings_endpoint(project_id: str) -> Response:
    """Replace one project's display metadata, keeping its id, content and members.

    A rename never re-slugifies the id: the id keys both the content file and
    the registry entry that owns the members, so a rename is purely cosmetic.
    """
    layout_dir = _primary_agent_layout_dir()
    if layout_dir is None:
        error = ErrorResponse(detail="No primary agent configured for this workspace")
        return _json_response(error.model_dump(), status_code=500)
    parsed = _parse_project_metadata_body()
    if isinstance(parsed, Response):
        return parsed
    name, color, glyph = parsed
    try:
        info = projects.update_project(layout_dir, project_id, name, color, glyph)
    except (projects.ProjectNameError, projects.ProjectColorError, projects.ProjectGlyphError) as e:
        return _project_metadata_error_response(e)
    except projects.ProjectNotFoundError:
        return _project_not_found_response(project_id)
    get_state().broadcaster.broadcast({"type": "project_updated", **info.model_dump()})
    return _json_response(info.model_dump())


def _parse_member_ref_body() -> str | Response:
    """Parse the ``{ref}`` body shared by the member add and remove endpoints.

    Only shape is checked here; what a ref may look like belongs to the
    frontend and ``layout_ops``, which own the grammar (``service:<name>``,
    ``chat:<agent-id>``, ``terminal:<name>``, ``url:<hash>``).
    """
    body = _parse_json_object_body()
    if isinstance(body, Response):
        return body
    ref = body.get("ref")
    if not isinstance(ref, str) or not ref.strip():
        error = ErrorResponse(detail="'ref' must be a non-empty string")
        return _json_response(error.model_dump(), status_code=400)
    return ref.strip()


def _broadcast_members_changed(project_ids: list[str]) -> None:
    """Tell every client that these projects' member lists moved.

    Membership is durable and independent of the layout, so a client that does
    not have the affected project mounted still has to refresh its sidebar --
    hence a plain broadcast rather than a layout-targeted one.
    """
    get_state().broadcaster.broadcast({"type": "project_members_changed", "project_ids": project_ids})


def _set_project_shortcut_endpoint(project_id: str) -> Response:
    """Record one shortcut's pin or mode override on this project.

    Which starting points a project keeps to hand -- and what clicking each
    does (focus the most recent member of its kind, or always create) -- are
    properties of that project, so both are stored per project rather than per
    user. The body is ``{shortcut, is_pinned?, mode?}`` with at least one of
    the optional fields present; ``shortcut`` accepts the built-in names and
    ``app:<service-name>`` (whose pinning stays membership, so only ``mode``
    applies there). The response carries the project's full effective override
    map, so clients settle on one authoritative answer.

    Not a member call: none of the built-ins is an object with a ref. "chat"
    is a create, and the terminal and browser services are fleets reached by
    making a session rather than by opening the service, so there is no
    membership here to add or drop and this rides its own field instead.

    No primary agent configured (dev/test) means nothing persists, so after
    validating the body this answers the same soft no-op the add-member
    endpoint does, rather than 500ing on every pin click.
    """
    body = _parse_json_object_body()
    if isinstance(body, Response):
        return body
    shortcut = body.get("shortcut")
    is_pinned = body.get("is_pinned")
    mode = body.get("mode")
    if not isinstance(shortcut, str) or not shortcut.strip():
        return _json_response(ErrorResponse(detail="'shortcut' must be a non-empty string").model_dump(), 400)
    if is_pinned is not None and not isinstance(is_pinned, bool):
        return _json_response(ErrorResponse(detail="'is_pinned' must be a boolean").model_dump(), 400)
    if mode is not None and not isinstance(mode, str):
        return _json_response(ErrorResponse(detail="'mode' must be a string").model_dump(), 400)
    if is_pinned is None and mode is None:
        return _json_response(
            ErrorResponse(detail="At least one of 'is_pinned' and 'mode' must be present").model_dump(), 400
        )
    layout_dir = _primary_agent_layout_dir()
    if layout_dir is None:
        return _json_response({"project_id": project_id, "shortcut_overrides": {}})
    try:
        overrides = projects.set_shortcut_override(layout_dir, project_id, shortcut.strip(), is_pinned, mode)
    except projects.ProjectNotFoundError:
        return _project_not_found_response(project_id)
    except projects.ProjectShortcutError as e:
        return _json_response(ErrorResponse(detail=str(e)).model_dump(), status_code=400)
    # The same broadcast a membership change rides: both move what a project's
    # rail shows, and a client with the project unmounted still has to catch up.
    _broadcast_members_changed([project_id])
    return _json_response({"project_id": project_id, "shortcut_overrides": overrides})


def _add_project_member_endpoint(project_id: str) -> Response:
    """Add one ref to this project's member list.

    Idempotent, and deliberately indifferent to what else shows the ref: a
    project is a view, so the same object appearing in several at once is
    ordinary rather than a conflict.

    No primary agent configured (dev/test) means nothing persists, so this
    answers like the read side of the same resource does (``GET
    /api/projects/members``, which reports an empty map rather than raising)
    instead of 500ing on every add.
    """
    layout_dir = _primary_agent_layout_dir()
    if layout_dir is None:
        return _json_response({"project_id": project_id, "members": []})
    parsed = _parse_member_ref_body()
    if isinstance(parsed, Response):
        return parsed
    try:
        projects.add_member(layout_dir, project_id, parsed)
    except projects.ProjectNotFoundError:
        return _project_not_found_response(project_id)
    _broadcast_members_changed([project_id])
    return _json_response({"project_id": project_id, "members": projects.list_members(layout_dir, project_id)})


def _remove_project_member_endpoint(project_id: str) -> Response:
    """Drop one ref from this project's member list.

    "Remove from project" hides the object in this one view and nothing more:
    it keeps running, it stays in every other project showing it, and it stays
    in Everything, which is the home for everything on the machine.
    """
    layout_dir = _primary_agent_layout_dir()
    if layout_dir is None:
        error = ErrorResponse(detail="No primary agent configured for this workspace")
        return _json_response(error.model_dump(), status_code=500)
    parsed = _parse_member_ref_body()
    if isinstance(parsed, Response):
        return parsed
    try:
        projects.remove_member(layout_dir, project_id, parsed)
    except projects.ProjectNotFoundError:
        return _project_not_found_response(project_id)
    _broadcast_members_changed([project_id])
    return _json_response({"project_id": project_id, "members": projects.list_members(layout_dir, project_id)})


def _share_project_member_endpoint() -> Response:
    """Add one ref to another project without taking it out of any other.

    Opening an object from the launcher's "on this machine" table files it in
    the project you are looking at. Nothing is reassigned: a project is a view,
    so the object keeps showing wherever it already showed. Only the
    destination is therefore broadcast as changed.
    """
    layout_dir = _primary_agent_layout_dir()
    if layout_dir is None:
        error = ErrorResponse(detail="No primary agent configured for this workspace")
        return _json_response(error.model_dump(), status_code=500)
    body = _parse_json_object_body()
    if isinstance(body, Response):
        return body
    ref = body.get("ref")
    to_project_id = body.get("to_project_id")
    if not isinstance(ref, str) or not ref.strip():
        error = ErrorResponse(detail="'ref' must be a non-empty string")
        return _json_response(error.model_dump(), status_code=400)
    if not isinstance(to_project_id, str) or not to_project_id:
        error = ErrorResponse(detail="'to_project_id' must be a non-empty string")
        return _json_response(error.model_dump(), status_code=400)
    try:
        projects.add_member(layout_dir, to_project_id, ref.strip())
    except projects.ProjectNotFoundError:
        return _project_not_found_response(to_project_id)
    _broadcast_members_changed([to_project_id])
    return _json_response(
        {
            "ref": ref.strip(),
            "to_project_id": to_project_id,
            "projects": projects.projects_showing(layout_dir, ref.strip()),
        }
    )


def _list_project_members_endpoint() -> Response:
    """Every filed ref on the machine mapped to the projects showing it.

    Membership is many-to-many, so this is a map to *lists*: a ref shows up
    under every project whose filter includes it, and a ref no project holds is
    simply absent. It decorates rows rather than resolving them -- nothing has
    to be looked up here before an object can be opened, because every view
    opens into its own dock.
    """
    layout_dir = _primary_agent_layout_dir()
    if layout_dir is None:
        return _json_response({"members": {}})
    return _json_response({"members": projects.all_members(layout_dir)})


def _broadcast_member_title_changed(ref: str, title: str | None) -> None:
    """Tell every client what this object is called now; None means unnamed again.

    A title belongs to the object rather than to a panel, so a client showing
    it in a project this one never opened -- or listing it backgrounded, with no
    panel at all -- still has to repaint. Hence a plain broadcast, as membership
    changes get.
    """
    get_state().broadcaster.broadcast({"type": "member_title_changed", "ref": ref, "title": title})


def _list_member_titles_endpoint() -> Response:
    """Every name the user has given an object, keyed by its ref.

    One flat map for the whole machine: a rename names the object, so the same
    name is what every view showing it draws, and a ref that is absent is simply
    unnamed -- the caller falls back to whatever the object calls itself.
    """
    layout_dir = _primary_agent_layout_dir()
    if layout_dir is None:
        return _json_response({"titles": {}})
    return _json_response({"titles": member_titles.read_titles(layout_dir)})


def _rename_chat_agent_for_ref(layout_dir: Path, ref: str, title: str) -> Response:
    """Rename the mngr agent behind a ``chat:`` ref, keeping its name pair matched.

    A chat is an mngr agent, and the agent itself holds its name pair -- the
    canonical true name plus the typed form as its ``display_name`` label (the
    same pairing minds establishes for hosts). So a chat's rename goes to mngr
    rather than to the workspace's title store: renaming only the store is what
    used to leave ``mngr list`` showing a different name than the tab.

    Any *stored* title the ref still carries (a legacy entry, from before chat
    names lived on the agent) is cleared once mngr accepted the rename, so a
    stale store entry can never shadow the agent's own name again. The typed
    name still comes back in the response and the broadcast -- it always equals
    what the agent's label now derives to, so every surface settles immediately.

    A refused rename leaves everything as it was and surfaces the error: a name
    conflict answers 409 (retry with another name), an unusable name 400, and an
    mngr failure 500.
    """
    chosen_title = member_titles.validated_title(title)
    if chosen_title is None:
        # Clearing is store-only: mngr has no empty name to be given, so the
        # chat keeps its name and only a legacy stored shadow is dropped.
        member_titles.clear_title(layout_dir, ref)
        _broadcast_member_title_changed(ref, None)
        return _json_response({"ref": ref, "title": None})
    agent_manager: AgentManager = get_state().agent_manager
    try:
        agent_manager.rename_chat_agent(ref[len(_CHAT_MEMBER_REF_PREFIX) :], chosen_title)
    except AgentNameConflictError as e:
        return _json_response(ErrorResponse(detail=str(e)).model_dump(), status_code=409)
    except AgentRenameError as e:
        return _json_response(ErrorResponse(detail=str(e)).model_dump(), status_code=500)
    member_titles.clear_title(layout_dir, ref)
    _broadcast_member_title_changed(ref, chosen_title)
    return _json_response({"ref": ref, "title": chosen_title})


def _set_member_title_endpoint() -> Response:
    """Name one object, machine-wide, or clear its name with a blank one.

    The ref is not required to be filed anywhere: an object in no project at all
    still shows in Everything and can still be renamed there, and a backgrounded
    member can be renamed with no panel to hang the name on -- which is the
    point of keying this by ref. The stored name comes back in the response and
    in the broadcast, ``null`` when the entry was cleared.

    A ``chat:`` ref is an mngr agent, whose name lives on the agent itself
    rather than in the store -- see ``_rename_chat_agent_for_ref``.
    """
    layout_dir = _primary_agent_layout_dir()
    if layout_dir is None:
        error = ErrorResponse(detail="No primary agent configured for this workspace")
        return _json_response(error.model_dump(), status_code=500)
    body = _parse_json_object_body()
    if isinstance(body, Response):
        return body
    ref = body.get("ref")
    title = body.get("title")
    if not isinstance(ref, str) or not ref.strip():
        error = ErrorResponse(detail="'ref' must be a non-empty string")
        return _json_response(error.model_dump(), status_code=400)
    if not isinstance(title, str):
        error = ErrorResponse(detail="'title' must be a string (an empty one clears the name)")
        return _json_response(error.model_dump(), status_code=400)
    try:
        if ref.strip().startswith(_CHAT_MEMBER_REF_PREFIX):
            return _rename_chat_agent_for_ref(layout_dir, ref.strip(), title)
        stored_title = member_titles.set_title(layout_dir, ref, title)
    except member_titles.MemberTitleLengthError as e:
        return _json_response(ErrorResponse(detail=str(e)).model_dump(), status_code=400)
    _broadcast_member_title_changed(ref.strip(), stored_title)
    return _json_response({"ref": ref.strip(), "title": stored_title})


def _broadcast_member_last_used_changed(ref: str, at_ms: int | None) -> None:
    """Tell every client when this object was last used; None means never again.

    Recency belongs to the object rather than to a panel, so a client offering
    it in a launcher this one never opened still has to re-order. Hence a plain
    broadcast, as renames get.
    """
    get_state().broadcaster.broadcast({"type": "member_last_used_changed", "ref": ref, "at_ms": at_ms})


def _list_member_last_used_endpoint() -> Response:
    """When each object was last in front of the user, keyed by its ref.

    One flat map for the whole machine: recency is a fact about the object, so
    the same ordering is what every client's launcher draws, and a ref that is
    absent has simply never been used -- the caller renders it with no recency
    rather than inventing one.
    """
    layout_dir = _primary_agent_layout_dir()
    if layout_dir is None:
        return _json_response({"last_used": {}})
    return _json_response({"last_used": member_last_used.read_last_used(layout_dir)})


def _touch_member_last_used_endpoint() -> Response:
    """Record that one object is in front of the user, machine-wide, right now.

    The client sends only the ref; the moment is this server's own clock, which
    kills the clock-skew question -- every entry in the store is stamped by the
    one clock that also serves the map back. The ref is not required to be
    filed anywhere, for the same reason a name is not: an object in no project
    at all still shows in Everything, and a backgrounded member can be used
    again the moment it is opened.
    """
    layout_dir = _primary_agent_layout_dir()
    if layout_dir is None:
        error = ErrorResponse(detail="No primary agent configured for this workspace")
        return _json_response(error.model_dump(), status_code=500)
    body = _parse_json_object_body()
    if isinstance(body, Response):
        return body
    ref = body.get("ref")
    if not isinstance(ref, str) or not ref.strip():
        error = ErrorResponse(detail="'ref' must be a non-empty string")
        return _json_response(error.model_dump(), status_code=400)
    stored_ms = member_last_used.touch_last_used(layout_dir, ref, int(time.time() * 1000))
    _broadcast_member_last_used_changed(ref.strip(), stored_ms)
    return _json_response({"ref": ref.strip(), "at_ms": stored_ms})


def _broadcast_member_location_changed(ref: str, path: str | None) -> None:
    """Tell every client where this object is looking now; None means nowhere again.

    A location belongs to the object rather than to a panel, so a client that
    could open it from a launcher this one never touched still has to know
    where it would open. Hence a plain broadcast, as renames get.
    """
    get_state().broadcaster.broadcast({"type": "member_location_changed", "ref": ref, "path": path})


def _list_member_locations_endpoint() -> Response:
    """Where each beaconing object was last looking, keyed by its ref.

    One flat map for the whole machine: a location belongs to the object, so
    the same opening path is what every view uses, and a ref that is absent
    has simply never beaconed -- the caller opens at the service origin.
    """
    layout_dir = _primary_agent_layout_dir()
    if layout_dir is None:
        return _json_response({"locations": {}})
    return _json_response({"locations": member_locations.read_locations(layout_dir)})


def _set_member_location_endpoint() -> Response:
    """Record where one object is looking, machine-wide, or clear it with a blank.

    The shell is the writer: it has already validated the beacon's origin and
    resolved the posting pane to its ref, so this end only checks shape (a
    rooted path within the cap). The stored path comes back in the response
    and the broadcast, ``null`` when the entry was cleared.
    """
    layout_dir = _primary_agent_layout_dir()
    if layout_dir is None:
        error = ErrorResponse(detail="No primary agent configured for this workspace")
        return _json_response(error.model_dump(), status_code=500)
    body = _parse_json_object_body()
    if isinstance(body, Response):
        return body
    ref = body.get("ref")
    path = body.get("path")
    if not isinstance(ref, str) or not ref.strip():
        error = ErrorResponse(detail="'ref' must be a non-empty string")
        return _json_response(error.model_dump(), status_code=400)
    if not isinstance(path, str):
        error = ErrorResponse(detail="'path' must be a string (an empty one clears the location)")
        return _json_response(error.model_dump(), status_code=400)
    try:
        stored_path = member_locations.set_location(layout_dir, ref, path)
    except member_locations.MemberLocationError as e:
        return _json_response(ErrorResponse(detail=str(e)).model_dump(), status_code=400)
    _broadcast_member_location_changed(ref.strip(), stored_path)
    return _json_response({"ref": ref.strip(), "path": stored_path})


def _list_app_instances_endpoint() -> Response:
    """Every app instance the machine holds, by service name.

    An instance exists while any project's member list or any view's saved
    layout references it (see ``app_instances``), so this is the machine
    inventory's app half: the tab lists and launchers list instances, never
    bare services.
    """
    layout_dir = _primary_agent_layout_dir()
    if layout_dir is None:
        return _json_response({"instances": {}})
    return _json_response({"instances": app_instances.list_app_instances(layout_dir)})


def _allocate_app_instance_endpoint(name: str) -> Response:
    """Mint the next free ``<name>-<N>`` instance of one registered app.

    The instance does not exist yet when this answers -- existence is derived
    from references, and the caller's open is what files the first one -- so
    the allocator's in-flight reservation set is what keeps two rapid mints
    apart. 404 for a name no registered app answers to: minting is an open
    surface's act, and every open surface starts from a registered app.
    """
    layout_dir = _primary_agent_layout_dir()
    if layout_dir is None:
        error = ErrorResponse(detail="No primary agent configured for this workspace")
        return _json_response(error.model_dump(), status_code=500)
    if get_state().agent_manager.get_app_by_name(name) is None:
        error = ErrorResponse(detail=f"No registered app named {name!r}")
        return _json_response(error.model_dump(), status_code=404)
    instance_name = app_instances.allocate_app_instance(layout_dir, name)
    return _json_response(
        {"name": name, "instance": instance_name, "ref": app_instances.instance_ref(name, instance_name)}
    )


def _delete_project_endpoint(project_id: str) -> Response:
    """Delete a project: a pure view operation, nothing more.

    Only the project's registry entry, member list and saved content go, which
    is exactly what ``projects.delete_project`` does -- every object it showed
    keeps running untouched, stays in Everything, and stays in any other
    project already showing it. A machine may end up with zero projects;
    Everything is always there to fall back to, and the frontend's confirmation
    says as much before this is ever called.
    """
    layout_dir = _primary_agent_layout_dir()
    if layout_dir is None:
        error = ErrorResponse(detail="No primary agent configured for this workspace")
        return _json_response(error.model_dump(), status_code=500)
    try:
        fallback_id = projects.delete_project(layout_dir, project_id)
    except projects.ProjectNotFoundError:
        return _project_not_found_response(project_id)
    logger.info("Deleted project {} (fallback {})", project_id, fallback_id)
    get_state().broadcaster.broadcast(
        {"type": "project_deleted", "project_id": project_id, "fallback_id": fallback_id}
    )
    return _json_response({"fallback_id": fallback_id})


def _delete_project_panel_endpoint(panel_id: str) -> Response:
    """Drop a destroyed object from every project that holds it.

    Destroying a tab tears down the agent, terminal, or browser behind it, so
    it has to leave the projects that are not currently mounted too -- as a
    panel, which would otherwise restore a tab whose identity can no longer be
    resolved, and as a member, which would otherwise keep listing it as
    backgrounded forever. The optional ``ref`` in the body is the member that
    panel stood for; a caller that knows only the panel omits it and drops the
    panel alone. Clients that have an affected project open re-apply it from
    the broadcast.

    The name the user gave the object goes with it, since refs are handed out
    again -- the terminal allocator reuses the lowest free ``terminal-<N>`` --
    and a name left behind would land on whatever answers to that ref next. It
    is dropped here rather than inside ``projects``, which knows about member
    lists and layouts and deliberately not about a machine-wide store.
    """
    layout_dir = _primary_agent_layout_dir()
    if layout_dir is None:
        error = ErrorResponse(detail="No primary agent configured for this workspace")
        return _json_response(error.model_dump(), status_code=500)
    try:
        body = json.loads(request.get_data() or b"{}")
    except (json.JSONDecodeError, ValueError) as e:
        _loguru_logger.opt(exception=e).warning("Panel delete for {} carried invalid JSON", panel_id)
        error = ErrorResponse(detail="Invalid JSON in request body")
        return _json_response(error.model_dump(), status_code=400)
    raw_ref = body.get("ref") if isinstance(body, dict) else None
    ref = raw_ref.strip() if isinstance(raw_ref, str) and raw_ref.strip() else None
    changed_project_ids = projects.remove_panel_from_all_projects(layout_dir, panel_id, ref)
    if changed_project_ids:
        # The ref rides along so a client showing the object under a different
        # panel id (browser and app pane ids are minted per open) can still
        # drop it from its live dock.
        get_state().broadcaster.broadcast(
            {"type": "project_panel_removed", "panel_id": panel_id, "ref": ref, "project_ids": changed_project_ids}
        )
        # The sweep reaches Everything's saved arrangement too, but Everything
        # has no member list, so it never belongs in a members-changed event.
        member_project_ids = [
            project_id for project_id in changed_project_ids if project_id != projects.EVERYTHING_VIEW_ID
        ]
        if ref is not None and member_project_ids:
            _broadcast_members_changed(member_project_ids)
    if ref is not None and member_titles.clear_title(layout_dir, ref):
        _broadcast_member_title_changed(ref, None)
    # The recency goes for the same reason as the name: a reused ref must not
    # surface at the top of the launcher on the strength of a dead object.
    if ref is not None and member_last_used.clear_last_used(layout_dir, ref):
        _broadcast_member_last_used_changed(ref, None)
    # And the stored location: instance names are reused too, and a location
    # left behind would aim the next holder of this ref at a folder it never
    # visited.
    if ref is not None and member_locations.clear_location(layout_dir, ref):
        _broadcast_member_location_changed(ref, None)
    # A deleted app instance's allocator reservation goes with it, so its
    # number frees up immediately (mirroring the terminal allocator's discard
    # on destroy).
    parsed_instance = app_instances.parse_instance_ref(ref) if ref is not None else None
    if parsed_instance is not None:
        app_instances.release_app_instance(layout_dir, parsed_instance[1])
    return _json_response({"project_ids": changed_project_ids})


def _tmux_prefix() -> str:
    """The mngr session-name prefix; agent sessions carry it, terminals do not."""
    return os.environ.get("MNGR_PREFIX", "mngr-")


def _list_tmux_sessions() -> tuple[TerminalSessionInfo, ...]:
    """Enumerate every tmux session on the default socket, or () when none.

    A missing tmux server (no sessions yet) returns a non-zero exit code, which
    we treat as an empty list rather than an error.
    """
    result = run_local_command_modern_version(
        command=["tmux", "list-sessions", "-F", "#{session_name}\t#{session_id}\t#{session_path}"],
        cwd=None,
        is_checked=False,
        timeout=5.0,
    )
    if result.returncode != 0:
        return ()
    return parse_tmux_sessions_output(result.stdout)


def _list_terminals() -> Response:
    """List the live user-terminal tmux sessions (excludes mngr agent sessions)."""
    prefix = _tmux_prefix()
    sessions = filter_user_terminal_sessions(_list_tmux_sessions(), prefix)
    return _json_response(
        {
            "terminals": [session.model_dump() for session in sessions],
            "prefix": prefix,
        }
    )


def _allocate_terminal() -> Response:
    """Reserve the next free ``terminal-<N>`` name for a new terminal tab.

    The lock plus the in-memory ``_recently_allocated_terminal_names`` set make
    consecutive allocations return distinct names even before the ttyd
    connection has actually created the tmux session (creation is lazy, so two
    rapid clicks would otherwise both see the same live-session set and collide).
    """
    prefix = _tmux_prefix()
    with _terminal_allocate_lock:
        live_names = {session.session_name for session in _list_tmux_sessions()}
        # Drop reservations that have since become real sessions so the set
        # cannot grow without bound.
        _recently_allocated_terminal_names.difference_update(live_names)
        taken = live_names | _recently_allocated_terminal_names
        name = allocate_next_terminal_name(taken, prefix)
        _recently_allocated_terminal_names.add(name)
    return _json_response({"session_name": name})


def _kill_terminal_session(session_name: str) -> str | None:
    """Kill one tmux session, returning tmux's complaint when it survived.

    tmux returns non-zero both for a genuine failure and for an already-absent
    session (nothing to kill). The two are told apart by re-listing: if the
    session is gone, the kill succeeded (or was an idempotent repeat); if it is
    still present, the kill really failed and the caller must surface that
    rather than reporting a terminal as gone while it keeps running.
    """
    # ``=`` forces an exact session-name match so tmux's prefix fallback can't
    # target a different session.
    result = run_local_command_modern_version(
        command=["tmux", "kill-session", "-t", f"={session_name}"],
        cwd=None,
        is_checked=False,
        timeout=5.0,
    )
    if result.returncode != 0:
        still_live = any(session.session_name == session_name for session in _list_tmux_sessions())
        if still_live:
            _loguru_logger.warning("Failed to kill terminal session {}: {}", session_name, result.stderr.strip())
            return result.stderr.strip()
    with _terminal_allocate_lock:
        _recently_allocated_terminal_names.discard(session_name)
    return None


def _destroy_terminal(session_name: str) -> Response:
    """Kill a user-terminal tmux session. Refuses to touch mngr agent sessions."""
    prefix = _tmux_prefix()
    if not is_destroyable_terminal_session(session_name, prefix):
        error = ErrorResponse(detail=f"Refusing to destroy non-terminal session: {session_name!r}")
        return _json_response(error.model_dump(), status_code=400)
    failure = _kill_terminal_session(session_name)
    if failure is not None:
        error = ErrorResponse(detail=f"Failed to destroy terminal {session_name!r}: {failure}")
        return _json_response(error.model_dump(), status_code=500)
    return _json_response({"status": "ok"})


def _get_terminal_banner_dismissed() -> Response:
    """Whether the user has permanently dismissed the terminal lifecycle banner."""
    layout_dir = _primary_agent_layout_dir()
    if layout_dir is None:
        return _json_response({"dismissed": False})
    banner_file = layout_dir / _TERMINAL_BANNER_FILENAME
    if not banner_file.exists():
        return _json_response({"dismissed": False})
    try:
        data = json.loads(banner_file.read_text())
    except (json.JSONDecodeError, OSError) as e:
        _loguru_logger.opt(exception=e).warning(
            "Failed to read terminal banner state at {}; treating as not dismissed", banner_file
        )
        return _json_response({"dismissed": False})
    dismissed = bool(data.get("dismissed", False)) if isinstance(data, dict) else False
    return _json_response({"dismissed": dismissed})


def _set_terminal_banner_dismissed() -> Response:
    """Persist the user's "never show again" choice for the terminal banner."""
    layout_dir = _primary_agent_layout_dir()
    if layout_dir is None:
        error = ErrorResponse(detail="No primary agent configured for this workspace")
        return _json_response(error.model_dump(), status_code=500)
    try:
        body = json.loads(request.get_data() or b"{}")
    except (json.JSONDecodeError, ValueError) as e:
        _loguru_logger.opt(exception=e).warning("terminal banner-dismissed received invalid JSON body")
        error = ErrorResponse(detail="Invalid JSON in request body")
        return _json_response(error.model_dump(), status_code=400)
    dismissed = bool(body.get("dismissed", False)) if isinstance(body, dict) else False
    layout_dir.mkdir(parents=True, exist_ok=True)
    (layout_dir / _TERMINAL_BANNER_FILENAME).write_text(json.dumps({"dismissed": dismissed}))
    return _json_response({"dismissed": dismissed})


def _resolve_terminal_id_for_tty(client_tty: str) -> str | None:
    """Reverse-look-up the dockview terminal id bound to a tmux client tty.

    ``system/apps/terminal/run_ttyd.sh`` records ``terminal_id -> $(tty)`` files under
    ``$MNGR_AGENT_STATE_DIR/commands/ttyd/clients/`` when a tab attaches; this
    finds the id whose recorded tty matches ``client_tty``. Returns None when
    the mapping directory or a matching entry is absent.
    """
    if not client_tty:
        return None
    state_dir = os.environ.get("MNGR_AGENT_STATE_DIR", "")
    if not state_dir:
        return None
    clients_dir = Path(state_dir) / "commands" / "ttyd" / "clients"
    if not clients_dir.is_dir():
        return None
    for entry in clients_dir.iterdir():
        if not entry.is_file():
            continue
        try:
            recorded_tty = entry.read_text().strip()
        except OSError:
            continue
        if recorded_tty == client_tty:
            return entry.name
    return None


def _terminal_notify_endpoint() -> Response:
    """Loopback endpoint the tmux hooks call when a terminal's session changes.

    Body: ``{kind, client_tty, session_name, session_id}``. For a session
    switch we resolve the affected dockview tab from ``client_tty``; for a
    rename the frontend matches by ``session_id`` so ``terminal_id`` stays None.
    Either way we re-broadcast as a ``terminal_session`` WS event.
    """
    client_host = request.remote_addr or ""
    if client_host not in _LOOPBACK_CLIENT_HOSTS:
        error = ErrorResponse(detail="terminal notify is only callable from loopback")
        return _json_response(error.model_dump(), status_code=403)
    try:
        body = json.loads(request.get_data())
    except (json.JSONDecodeError, ValueError) as e:
        _loguru_logger.opt(exception=e).warning("terminal notify received invalid JSON body")
        error = ErrorResponse(detail="Invalid JSON in request body")
        return _json_response(error.model_dump(), status_code=400)
    if not isinstance(body, dict):
        error = ErrorResponse(detail="Request body must be a JSON object")
        return _json_response(error.model_dump(), status_code=400)
    kind = body.get("kind")
    session_name = str(body.get("session_name") or "")
    session_id = str(body.get("session_id") or "")
    client_tty = str(body.get("client_tty") or "")
    broadcaster: WebSocketBroadcaster = get_state().broadcaster
    if kind == "session-changed":
        # Resolve which dockview tab this tmux client belongs to. An
        # unresolved tty (e.g. an mngr agent-session client, which never
        # writes the ttyd clients map) means there is no terminal tab to
        # update, so skip the broadcast entirely.
        terminal_id = _resolve_terminal_id_for_tty(client_tty)
        if terminal_id is None:
            return _json_response({"ok": True, "broadcast": False})
        broadcaster.broadcast_terminal_session(terminal_id, session_id, session_name)
        return _json_response({"ok": True, "broadcast": True})
    if kind == "session-renamed":
        # A rename has no client context; the frontend matches the affected
        # tab by ``session_id``.
        broadcaster.broadcast_terminal_session(None, session_id, session_name)
        return _json_response({"ok": True, "broadcast": True})
    error = ErrorResponse(detail=f"Unknown terminal notify kind: {kind!r}")
    return _json_response(error.model_dump(), status_code=400)


def _browser_backend_url(path: str) -> str | None:
    """The registered browser daemon's URL for ``path``, or None when unregistered.

    One resolver for every fleet passthrough, so they agree on which service
    entry the browser fleet lives behind.
    """
    base_url = get_state().agent_manager.get_service_url(_BROWSER_SERVICE_NAME)
    if base_url is None:
        return None
    return f"{base_url.rstrip('/')}/{path}"


def _browsers_passthrough() -> Response:
    """Same-origin passthrough for the browser daemon's fleet API.

    Browser panels live on their own service origin now, so the shell frontend
    can no longer ``fetch()`` the daemon's ``/browsers`` endpoint directly
    (sibling service origins are same-site but not same-origin, and the daemon
    sends no CORS headers). This server-side hop forwards ``GET`` /
    ``POST /api/browsers`` to the registered ``browser`` service's local
    backend and relays the backend's body and status verbatim (GET returns
    ``{"browsers": [...]}``; POST relays the daemon's create result, including
    its 400/409/503 rejections). Returns a 503 JSON error when the service is
    not registered or unreachable.
    """
    state = get_state()
    backend_url = _browser_backend_url("browsers")
    if backend_url is None:
        error = ErrorResponse(detail="Browser service is not registered")
        return _json_response(error.model_dump(), status_code=503)
    try:
        if request.method == "POST":
            backend_response = state.http_client.post(
                backend_url,
                content=request.get_data(),
                headers={"Content-Type": request.headers.get("Content-Type") or "application/json"},
            )
        else:
            backend_response = state.http_client.get(backend_url)
    except httpx.HTTPError as e:
        _loguru_logger.warning("Browser service request to {} failed: {}", backend_url, e)
        error = ErrorResponse(detail="Browser service is unreachable")
        return _json_response(error.model_dump(), status_code=503)
    return Response(
        backend_response.content,
        status=backend_response.status_code,
        content_type=backend_response.headers.get("content-type", "application/json"),
    )


def _destroy_browser_passthrough(name: str) -> Response:
    """Same-origin passthrough for retiring one browser in the fleet.

    Companion to :func:`_browsers_passthrough` for the destroy control on a
    browser pane's tab. The panels live on their own service origin, so the
    shell can't ``DELETE`` the daemon's ``/browsers/<name>`` directly (no CORS);
    this hop forwards ``DELETE /api/browsers/<name>`` to the registered
    ``browser`` service and relays the daemon's body and status verbatim
    (including its 404/409/503 rejections). Returns a 503 JSON error when the
    service is not registered or unreachable.
    """
    state = get_state()
    backend_url = _browser_backend_url(f"browsers/{name}")
    if backend_url is None:
        error = ErrorResponse(detail="Browser service is not registered")
        return _json_response(error.model_dump(), status_code=503)
    try:
        backend_response = state.http_client.delete(backend_url)
    except httpx.HTTPError as e:
        _loguru_logger.warning("Browser service DELETE to {} failed: {}", backend_url, e)
        error = ErrorResponse(detail="Browser service is unreachable")
        return _json_response(error.model_dump(), status_code=503)
    return Response(
        backend_response.content,
        status=backend_response.status_code,
        content_type=backend_response.headers.get("content-type", "application/json"),
    )


def _run_forward_port_removal(name: str) -> str | None:
    """Drop one app's row from the port registry, returning the failure text.

    Thin wrapper over ``forward_port.py --remove``: the script holds the
    registry's lock and does the atomic replace, so this only has to invoke it
    and report what it said when it refused.
    """
    result = run_local_command_modern_version(
        command=["uv", "run", "python3", str(_FORWARD_PORT_SCRIPT), "--remove", "--name", name],
        cwd=WORKSPACE_ROOT_DIRECTORY,
        is_checked=False,
        timeout=_FORWARD_PORT_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        _loguru_logger.warning("Failed to deregister app {}: {}", name, result.stderr.strip())
        return result.stderr.strip() or f"forward_port.py exited {result.returncode}"
    return None


def _deregister_app_endpoint(name: str) -> Response:
    """Take one app out of the port registry and out of every project.

    Deregistering is the whole of what this workspace can do to an app, and the
    response says so rather than dressing it up as a destroy: nothing here
    supervises the program behind a registered port. Its row leaves
    ``data/.state/apps.toml`` (so it stops being an addressable service and
    stops appearing in the app list), its ``service:<name>`` member leaves every
    project's list, and whatever is listening on that port keeps listening until
    whoever started it stops it -- which ``is_process_stopped`` reports as the
    constant false it always is. An app whose program is supervised simply
    re-registers itself the next time that program starts.
    """
    layout_dir = _primary_agent_layout_dir()
    if layout_dir is None:
        error = ErrorResponse(detail="No primary agent configured for this workspace")
        return _json_response(error.model_dump(), status_code=500)
    if name == _SHELL_SERVICE_NAME:
        error = ErrorResponse(detail=f"Refusing to deregister the workspace shell itself: {name!r}")
        return _json_response(error.model_dump(), status_code=400)
    if get_state().agent_manager.get_service_url(name) is None:
        error = ErrorResponse(detail=f"No registered app named {name!r}")
        return _json_response(error.model_dump(), status_code=404)
    failure = _run_forward_port_removal(name)
    if failure is not None:
        error = ErrorResponse(detail=f"Failed to deregister app {name!r}: {failure}")
        return _json_response(error.model_dump(), status_code=500)
    ref = f"{_SERVICE_REF_PREFIX}{name}"
    project_ids = projects.projects_showing(layout_dir, ref)
    for project_id in project_ids:
        projects.remove_member(layout_dir, project_id, ref)
    if project_ids:
        _broadcast_members_changed(project_ids)
    logger.info("Deregistered app {} (left {} project(s); its process was not stopped)", name, len(project_ids))
    return _json_response({"name": name, "project_ids": project_ids, "is_process_stopped": False})


def _resolve_stoppable_app(name: str) -> "AppEntry | Response":
    """The registered app behind a stop/start request, or the refusal response.

    Shared by the two endpoints so what may be stopped and what may be started
    stay the same set: 404 for an unknown name, 400 for the essential services
    (defense in depth against a hand-edited registry granting them a
    ``program``), 400 for a row without a ``program`` (nothing here supervises
    the process behind it).
    """
    if name in _ESSENTIAL_SERVICE_NAMES:
        error = ErrorResponse(detail=f"{name!r} is an essential service and cannot be stopped or started here")
        return _json_response(error.model_dump(), status_code=400)
    app_entry = get_state().agent_manager.get_app_by_name(name)
    if app_entry is None:
        error = ErrorResponse(detail=f"No registered app named {name!r}")
        return _json_response(error.model_dump(), status_code=404)
    if not app_entry.program:
        error = ErrorResponse(
            detail=(
                f"App {name!r} has no supervised program registered, so it cannot be "
                "stopped or started from the workspace (it is managed outside it)"
            )
        )
        return _json_response(error.model_dump(), status_code=400)
    return app_entry


def _finish_app_lifecycle_action(name: str) -> Response:
    """Land a stop/start's outcome: re-probe liveness now and answer the new state.

    The refresh broadcasts ``apps_updated`` itself when anything changed, so
    every client's dimming and placeholders flip in the same beat as the
    response.
    """
    agent_manager: AgentManager = get_state().agent_manager
    agent_manager.refresh_app_liveness()
    refreshed = agent_manager.get_app_by_name(name)
    return _json_response({"name": name, "is_running": refreshed.is_running if refreshed is not None else False})


def _stop_app_endpoint(name: str) -> Response:
    """Stop one app's supervisord program. Idempotent; the registry row stays.

    The service level of the two-level lifecycle: the app stays listed, stays
    filed in its projects, and keeps its origin -- only the program behind it
    stops. Reversible in one click, so no confirmation gate anywhere on the
    callers.
    """
    resolved = _resolve_stoppable_app(name)
    if isinstance(resolved, Response):
        return resolved
    try:
        stop_supervisor_program(resolved.program, supervisor_socket_path())
    except SupervisorProgramActionError as e:
        return _json_response(ErrorResponse(detail=str(e)).model_dump(), status_code=502)
    logger.info("Stopped app {} (program {})", name, resolved.program)
    return _finish_app_lifecycle_action(name)


def _start_app_endpoint(name: str) -> Response:
    """Start one app's supervisord program. Idempotent."""
    resolved = _resolve_stoppable_app(name)
    if isinstance(resolved, Response):
        return resolved
    try:
        start_supervisor_program(resolved.program, supervisor_socket_path())
    except SupervisorProgramActionError as e:
        return _json_response(ErrorResponse(detail=str(e)).model_dump(), status_code=502)
    logger.info("Started app {} (program {})", name, resolved.program)
    return _finish_app_lifecycle_action(name)


def _get_screen_capture(agent_id: str) -> Response:
    """Capture the tmux pane content for an agent.

    Returns the visible screen content (and optionally scrollback) as plain
    text. Useful for seeing what's on an agent's terminal when it has no
    Claude session data (e.g., the agent crashed on startup).
    """
    agent_info = _find_agent(agent_id)
    if agent_info is None:
        return _agent_not_found_response(agent_id)

    prefix = os.environ.get("MNGR_PREFIX", "mngr-")
    session_name = f"{prefix}{agent_info.name}"
    include_scrollback = request.args.get("scrollback", "false").lower() == "true"
    scrollback_flag = ["-S", "-"] if include_scrollback else []
    command = ["tmux", "capture-pane", "-t", session_name, *scrollback_flag, "-p"]

    result = run_local_command_modern_version(
        command=command,
        cwd=None,
        is_checked=False,
        timeout=5.0,
    )
    success = result.returncode == 0
    if not success:
        return _json_response(
            {"screen": None, "error": f"tmux session not found: {session_name}"},
            status_code=200,
        )
    return _json_response({"screen": result.stdout})


def _serve_static_file(basename: str) -> Response:
    config: Config = get_state().config
    file_path_string = config.static_file_basename_to_path.get(basename)
    if file_path_string is None:
        error = ErrorResponse(detail=f"Static file '{basename}' not found")
        return _json_response(error.model_dump(), status_code=404)
    file_path = Path(file_path_string)
    if not file_path.is_file():
        error = ErrorResponse(detail=f"Static file not found on disk: {file_path}")
        return _json_response(error.model_dump(), status_code=404)
    return send_file(file_path)


def _create_chat_agent() -> Response:
    """Create a new chat agent in the primary agent's work directory.

    One endpoint for every harness: the ``chat`` role is the same, and the request's
    ``harness`` field (validated against :class:`HarnessType`, claude by default) picks
    which harness template the server stacks under it.

    The chat's display name is minted here (server-side) when the request names
    none: the first free "<word> N" for the harness, counted against every name
    on the machine -- agents, in-flight creates, and the member-title store's
    chosen names -- so simultaneous creates cannot both mint "Chat 1". An
    explicitly requested name that collides answers 409 so the caller can retry
    with another. The response carries the resulting name pair (canonical
    ``name`` + human-readable ``display_name``) beside the agent id.

    A chat created inside a project carries that project's id in the agent's
    ``project`` label, which is where chat membership lives (mngr already
    propagates the label to the agent's own children). ``project_id`` rides
    beside the request model rather than inside it for that reason: it is a
    label on the created agent, not part of the chat's identity. A create with
    no ``project_id`` leaves the chat filed in no project, which is ordinary:
    Everything enumerates the machine, so it surfaces there anyway.
    """
    agent_manager: AgentManager = get_state().agent_manager
    body = _parse_json_object_body()
    if isinstance(body, Response):
        return body
    project_id = str(body.get("project_id") or "")
    request_fields = {key: value for key, value in body.items() if key != "project_id"}

    # Chosen member titles count as taken so an auto-minted "Chat 2" can never
    # collide with, say, a terminal someone renamed to "Chat 2".
    layout_dir = _primary_agent_layout_dir()
    titled_names = tuple(member_titles.read_titles(layout_dir).values()) if layout_dir is not None else ()

    try:
        create_request = CreateChatRequest.model_validate(request_fields)
        created = agent_manager.create_chat_agent(
            create_request.name,
            # The `first` create template belongs to the workspace's own first run, not to
            # anything a client asks for -- bootstrap stacks it on its own `mngr create`.
            extra_role_templates=(),
            project_id=project_id,
            extra_taken_names=titled_names,
            account_id=create_request.account_id,
        )
        response = CreateAgentResponse(agent_id=created.agent_id, name=created.name, display_name=created.display_name)
        return _json_response(response.model_dump(), status_code=201)
    except AgentNameConflictError as e:
        return _json_response(ErrorResponse(detail=str(e)).model_dump(), status_code=409)
    except (AgentCreationError, OSError, ValueError) as e:
        error = ErrorResponse(detail=str(e))
        return _json_response(error.model_dump(), status_code=400)


def _ws_endpoint(websocket: Any) -> None:
    """Unified WebSocket for agent state and app updates."""
    state = get_state()
    # Resolve the primary agent's layout dir once, at connect, and bind it to
    # this connection for the lifetime of the loop. The resolver reads
    # process-global env (MNGR_HOST_DIR / MNGR_AGENT_ID); capturing it here
    # keeps every write this connection makes pointed at *this* server's
    # workspace even if that env is later mutated (which only happens in tests,
    # where several servers share one process -- a stray late write from a
    # lingering connection would otherwise land in another server's log).
    _run_ws_broadcast_loop(
        websocket=websocket,
        agent_manager=state.agent_manager,
        ws_broadcaster=state.broadcaster,
        layout_dir=_primary_agent_layout_dir(),
    )


def _handle_client_state_message(
    raw_message: str,
    client_queue: "queue.Queue[str | None]",
    ws_broadcaster: WebSocketBroadcaster,
    layout_dir: Path | None,
    is_first_report: bool,
) -> bool:
    """Process one incoming WebSocket message; returns True for a ``client_state``.

    ``client_state`` is the only message type clients send: it registers the
    browser's client id, active layout, and device kind (on connect and on
    every layout switch). Registration feeds the broadcaster's client
    registry (used to target layout-mutating ops), the last-active-layout
    record, and the client-activity event log (a ``layout_switch`` event when
    the report names a different previous layout, else a ``client_connected``
    event for the connection's first report).
    """
    try:
        parsed = json.loads(raw_message)
    except json.JSONDecodeError as e:
        _loguru_logger.opt(exception=e).warning("Ignored unparsable WebSocket message from client")
        return False
    if not isinstance(parsed, dict) or parsed.get("type") != "client_state":
        _loguru_logger.warning("Ignored unexpected WebSocket message type from client: {!r}", parsed)
        return False
    client_id = str(parsed.get("client_id") or "")
    active_layout = str(parsed.get("active_layout") or "")
    device_kind = str(parsed.get("device_kind") or "")
    previous_layout = str(parsed.get("previous_layout") or "")
    if not client_id or not active_layout:
        return False
    ws_broadcaster.set_client_info(client_queue, client_id, active_layout, device_kind)
    if is_first_report:
        _loguru_logger.info(
            "WS client registered: client_id={} layout={} device={} (conn {})",
            client_id,
            active_layout,
            device_kind,
            id(client_queue),
        )
    elif previous_layout and previous_layout != active_layout:
        _loguru_logger.info(
            "WS client {} switched layout {} -> {} (conn {})",
            client_id,
            previous_layout,
            active_layout,
            id(client_queue),
        )
    else:
        # A re-report on an already-registered connection with an unchanged
        # layout; not worth a log line.
        pass
    if layout_dir is not None:
        # A client reports its VIEW id (a project id, or "everything"), so the
        # projects registry is where last-active belongs. Writing it into the
        # old named-layout store instead -- which rejects unknown slugs -- left
        # that store pinned at "desktop" forever while warning on every switch,
        # and layout ops with no explicit target then resolved to a view no
        # client was ever on (a guaranteed 412).
        projects.set_last_active_id(layout_dir, active_layout)
        events_path = client_activity.get_events_path(layout_dir)
        if previous_layout and previous_layout != active_layout:
            client_activity.append_layout_switch_event(
                events_path, client_id, device_kind, previous_layout, active_layout
            )
        elif is_first_report:
            client_activity.append_client_connected_event(events_path, client_id, device_kind, active_layout)
        else:
            # A re-report on an already-registered connection with an
            # unchanged layout; the registry update above is all it needs.
            pass
    return True


def _run_ws_broadcast_loop(
    websocket: Any,
    agent_manager: AgentManager,
    ws_broadcaster: WebSocketBroadcaster,
    layout_dir: Path | None,
) -> None:
    """Stream broadcaster messages to ``websocket`` until the client disconnects.

    Each WebSocket connection owns its own thread (flask-sock + the threaded
    WSGI server), so this loop simply blocks on the per-client queue and
    forwards messages. flask-sock's ``ping_interval`` keepalive closes a
    half-dead peer, surfacing as ``ConnectionClosed`` from ``send``; the
    broadcaster can also evict a hopelessly-behind client by pushing the
    shutdown sentinel (``None``) into the queue.

    Incoming ``client_state`` registrations are drained non-blockingly on
    each loop iteration (simple_websocket buffers frames on its own reader
    thread, so ``receive(timeout=0)`` never blocks); worst-case processing
    latency is one queue-poll interval (~1 s), well under any agent-driven
    op that depends on the registration.
    """
    client_queue = ws_broadcaster.register()
    _loguru_logger.info("WS /api/ws connection opened (conn {})", id(client_queue))
    # Overwritten by the paths below; every exit from the loop goes through one
    # of them, so this default should never surface in a log line.
    disconnect_reason = "handler exited"
    try:
        websocket.send(
            json.dumps(
                {
                    "type": "agents_updated",
                    "agents": agent_manager.get_agents_serialized(),
                }
            )
        )
        websocket.send(
            json.dumps(
                {
                    "type": "apps_updated",
                    "apps": agent_manager.get_apps_serialized(),
                }
            )
        )

        for proto in agent_manager.get_proto_agents():
            websocket.send(json.dumps({"type": "proto_agent_created", **proto}))

        is_client_registered = False
        shutdown = False
        while not shutdown:
            incoming = websocket.receive(timeout=0)
            while incoming is not None:
                if _handle_client_state_message(
                    str(incoming),
                    client_queue,
                    ws_broadcaster,
                    layout_dir=layout_dir,
                    is_first_report=not is_client_registered,
                ):
                    if not is_client_registered:
                        # Now that a client can apply layout ops, hand it the
                        # chats that appeared while nobody could.
                        agent_manager.flush_pending_auto_opens()
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


def _proto_agent_logs_endpoint(websocket: Any, agent_id: str) -> None:
    """WebSocket for streaming proto-agent creation logs."""
    agent_manager: AgentManager = get_state().agent_manager
    log_queue = agent_manager.get_log_queue(agent_id)
    _run_proto_agent_logs_loop(websocket=websocket, log_queue=log_queue)


def _run_proto_agent_logs_loop(
    websocket: Any,
    log_queue: "queue.Queue[str | None] | None",
) -> None:
    """Stream ``log_queue`` messages to ``websocket`` until the proto-agent finishes.

    If ``log_queue`` is ``None`` the proto-agent does not exist; send a
    structured not-found error and close the socket.
    """
    if log_queue is None:
        try:
            websocket.send(json.dumps({"done": True, "success": False, "error": "Proto-agent not found"}))
        except ConnectionClosed:
            pass
        return

    try:
        finished = False
        while not finished:
            try:
                message = log_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if message is None:
                finished = True
            else:
                websocket.send(message)
    except ConnectionClosed:
        pass


def _build_destroy_command(agent_name: str) -> list[str]:
    """Build the ``mngr destroy --force`` argv for one agent.

    Pure: argv assembly only, so the repo<->mngr CLI contract is testable
    against the live CLI without a subprocess (see ``server_test.py``).
    """
    return ["mngr", "destroy", agent_name, "--force"]


def _destroy_agent(agent_id: str) -> Response:
    """Destroy an agent by running mngr destroy --force.

    Refuses to destroy agents carrying the ``is_primary=true`` label: that's
    the services agent for the workspace, and destroying it would tear down
    the bootstrap, web, share-gateway, and other supervised services
    along with it. The frontend already hides ``is_primary=true`` agents
    from the visible agent list; this is defense-in-depth for callers that
    hit the endpoint directly (curl, scripted use, etc.).
    """
    agent_manager: AgentManager = get_state().agent_manager
    agent_state = agent_manager.get_agent_by_id(agent_id)
    if agent_state is None:
        error = ErrorResponse(detail=f"Agent '{agent_id}' not found")
        return _json_response(error.model_dump(), status_code=404)

    if agent_state.labels.get("is_primary") == "true":
        error = ErrorResponse(
            detail=(
                f"Refusing to destroy agent '{agent_state.name}': it carries "
                "the is_primary=true label (services agent for this workspace)"
            )
        )
        return _json_response(error.model_dump(), status_code=400)

    agent_name = agent_state.name

    result = run_local_command_modern_version(
        command=_build_destroy_command(agent_name),
        cwd=None,
        is_checked=False,
        timeout=_DESTROY_TIMEOUT_SECONDS,
    )
    success = result.returncode == 0
    output = result.stdout.strip() if success else result.stderr.strip()
    if not success:
        error = ErrorResponse(detail=f"Failed to destroy agent '{agent_name}': {output}")
        return _json_response(error.model_dump(), status_code=500)

    # Remove the agent from the system_interface's tracked state immediately
    # so the frontend reflects the destruction without waiting for mngr observe.
    agent_manager.remove_agent(agent_id)

    return _json_response(DestroyAgentResponse(status="ok").model_dump())


def _build_stop_command(agent_name: str) -> list[str]:
    """Build the ``mngr stop`` argv for one agent.

    Pure: argv assembly only, so the repo<->mngr CLI contract is testable
    against the live CLI without a subprocess (see ``server_test.py``).
    """
    return ["mngr", "stop", agent_name]


def _stop_agent(agent_id: str) -> Response:
    """Stop an agent's process by running ``mngr stop`` -- the reversible
    counterpart to ``_destroy_agent``.

    The agent keeps its transcript, name, and project memberships; messaging
    it (or the start endpoint) brings it back. Refuses the ``is_primary=true``
    services agent for the same reason destroy does: stopping it would take
    down every supervised service in the workspace. The agent stays in the
    manager's tracked state -- the observe stream reports the STOPPED state on
    its own.
    """
    agent_manager: AgentManager = get_state().agent_manager
    agent_state = agent_manager.get_agent_by_id(agent_id)
    if agent_state is None:
        error = ErrorResponse(detail=f"Agent '{agent_id}' not found")
        return _json_response(error.model_dump(), status_code=404)

    if agent_state.labels.get("is_primary") == "true":
        error = ErrorResponse(
            detail=(
                f"Refusing to stop agent '{agent_state.name}': it carries "
                "the is_primary=true label (services agent for this workspace)"
            )
        )
        return _json_response(error.model_dump(), status_code=400)

    # Stopping is lighter work than a destroy (no resource teardown), but it
    # rides the same mngr CLI startup and host-lock path, so it shares the
    # destroy's generous bound.
    result = run_local_command_modern_version(
        command=_build_stop_command(agent_state.name),
        cwd=None,
        is_checked=False,
        timeout=_DESTROY_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        error = ErrorResponse(detail=f"Failed to stop agent '{agent_state.name}': {result.stderr.strip()}")
        return _json_response(error.model_dump(), status_code=500)

    return _json_response(StopAgentResponse(status="ok").model_dump())


def _start_agent(agent_id: str) -> Response:
    """Ensure an agent is running so its terminal session is attachable.

    Opening an agent's terminal attaches to that agent's tmux session; while
    the agent is STOPPED that session does not exist, so the attach fails
    immediately. The frontend calls this endpoint before opening a terminal
    tab -- both for the chat-page "Open agent terminal" link and for terminal
    tabs restored from a saved dockview layout.

    This goes through the exact same in-process mngr start path that sending a
    message to the agent uses (see ``agent_discovery.start_agent``), so opening
    a terminal and messaging the agent succeed or fail together rather than
    diverging. mngr's own lifecycle check makes the start a no-op for an
    already-running agent, so this is cheap in the common case.
    """
    agent_info = _find_agent(agent_id)
    if agent_info is None:
        return _agent_not_found_response(agent_id)

    try:
        start_agent(agent_info.name)
    except MngrError as e:
        error = ErrorResponse(detail=f"Failed to start agent '{agent_info.name}': {e}")
        return _json_response(error.model_dump(), status_code=500)

    return _json_response(StartAgentResponse(status="ok").model_dump())


def _resolve_project_id_for_layout_arg(layout_dir: Path, requested: str) -> str | None:
    """Resolve a layout op's target name against the *views* a client can be in.

    A connected client reports its active *view* as its active layout -- that
    view is the arrangement the client autosaves into -- so a layout op naming
    something that is not a registered named layout still addresses a real
    target when it names a project. "Everything" resolves too: it has no
    registry entry, but it is the home a client is most likely to be sitting in
    and it keeps a content file like any other view. Returns None when the name
    matches nothing, leaving the caller to report the miss.
    """
    try:
        project_id = projects.slugify_project_name(requested)
    except projects.ProjectNameError:
        return None
    if project_id == projects.EVERYTHING_VIEW_ID:
        return project_id
    if any(info.project_id == project_id for info in projects.list_projects(layout_dir)):
        return project_id
    return None


class LayoutViewEntry(FrozenModel):
    """One view as the ``views`` op reports it."""

    id: str = Field(description="View id: a project id, or the Everything view id")
    name: str = Field(description="Display name")
    is_everything: bool = Field(description="Whether this is the unfiltered Everything view")
    members: tuple[str, ...] = Field(description="Member refs the view shows (empty for Everything)")
    has_desktop_content: bool = Field(description="Whether a desktop arrangement file exists yet")
    has_mobile_content: bool = Field(description="Whether a mobile arrangement file exists yet")
    clients_on: tuple[str, ...] = Field(description="Ids of connected clients with this view in front")


def _layout_views_entry(
    layout_dir: Path,
    clients_by_view: dict[str, list[str]],
    view_id: str,
    name: str,
    members: list[str],
    is_everything: bool,
) -> LayoutViewEntry:
    """One view as the ``views`` op reports it: identity, members, per-device
    content presence, and which connected clients have it in front."""
    return LayoutViewEntry(
        id=view_id,
        name=name,
        is_everything=is_everything,
        members=tuple(members),
        has_desktop_content=projects.project_content_path(layout_dir, view_id).exists(),
        has_mobile_content=projects.project_content_path(layout_dir, view_id, "mobile").exists(),
        clients_on=tuple(clients_by_view.get(view_id, [])),
    )


def _layout_op_display_name(layout_dir: Path, slug: str) -> str:
    """The human-readable name of the view ``slug`` resolved to.

    A project's name comes from the registry; the unfiltered view is named
    here because it has no registry entry to be named from.
    """
    if slug == projects.EVERYTHING_VIEW_ID:
        return projects.EVERYTHING_VIEW_NAME
    for info in projects.list_projects(layout_dir):
        if info.project_id == slug:
            return info.name
    return slug


def _default_view_id(layout_dir: Path | None) -> str | None:
    """The view an op with no explicit ``--layout`` targets.

    The view the connected clients are actually on, when they agree -- with one
    client (the ordinary workspace) this is simply "the view the user is looking
    at", which is what an agent means when it names no target. When zero or
    several distinct views are connected, the projects registry's last-active id
    breaks the tie: it tracks the most recent view any client reported, so it is
    the best single answer there is. None only without a registry to fall back
    on (dev/test with no layout dir and no agreeing client).
    """
    distinct_views = {info["active_layout_slug"] for info in get_state().broadcaster.get_connected_client_infos()}
    if len(distinct_views) == 1:
        return next(iter(distinct_views))
    if layout_dir is None:
        return None
    return projects.get_last_active_id(layout_dir)


def _resolve_requested_layout_slug(
    args_raw: dict[str, Any],
    layout_dir: Path | None,
) -> tuple[str | None, Response | None]:
    """Resolve a layout op's ``args.layout`` (or the current-view default) to a view id.

    Returns ``(slug, None)`` on success and ``(None, error_response)`` when an
    explicitly-named view is unusable or unknown. With no layout dir
    configured (dev/test), an explicit name is slugified without registry
    validation and the default is None.
    """
    requested = args_raw.get("layout")
    if isinstance(requested, str) and requested:
        if layout_dir is None:
            try:
                return projects.slugify_project_name(requested), None
            except projects.ProjectNameError as e:
                return None, _json_response(ErrorResponse(detail=str(e)).model_dump(), status_code=400)
        project_id = _resolve_project_id_for_layout_arg(layout_dir, requested)
        if project_id is not None:
            return project_id, None
        known_views = ", ".join(
            [info.name for info in projects.list_projects(layout_dir)] + [projects.EVERYTHING_VIEW_NAME]
        )
        error = ErrorResponse(detail=f"View {requested!r} not found (known views: {known_views})")
        return None, _json_response(error.model_dump(), status_code=404)
    return _default_view_id(layout_dir), None


def _layout_broadcast_endpoint() -> Response:
    """Unified loopback endpoint for the agent-facing ``system/scripts/layout.py`` helper.

    Body: ``{op, args, agent_id}``.

    Dispatch:

    - ``list`` / ``inspect``: pure server-side queries that read the
      ``agent_manager``'s in-memory service/agent registry plus the
      persisted ``layout.json`` (for ``is_open`` flags / tree layout)
      and return a structured payload. Bypass the mutex.
    - ``refresh`` / ``reload_system_interface``: state-preserving
      broadcasts that don't mutate serialized layout. Bypass the mutex.
      ``reload_system_interface`` tells connected browsers to reload the
      whole top-level page. Broadcast by
      ``system/scripts/refresh_workspace_view.py`` for any interface
      change, backend-only ones included, from whichever flow made it
      (``update-system-interface``, ``update-app``, ``update-self``).
    - All other ops (``open``, ``focus``, ``split``, ``close``, ``move``,
      ``rename``, ``maximize``, ``restore``, ``replace-url``): acquire
      the advisory mutex first; on contention return HTTP 409 with the
      holder's metadata so the caller can decide whether to retry. On
      success, broadcast the ``layout_op`` WS message and return.

    The endpoint is locked to loopback clients (no authentication exists
    between callers and the system interface inside the container).
    """
    client_host = request.remote_addr or ""
    if client_host not in _LOOPBACK_CLIENT_HOSTS:
        error = ErrorResponse(detail="layout broadcast is only callable from loopback")
        return _json_response(error.model_dump(), status_code=403)

    raw_body = request.get_data()
    try:
        body = json.loads(raw_body)
    except (json.JSONDecodeError, ValueError) as e:
        _loguru_logger.opt(exception=e).warning("layout broadcast received invalid JSON body")
        error = ErrorResponse(detail="Invalid JSON in request body")
        return _json_response(error.model_dump(), status_code=400)
    if not isinstance(body, dict):
        error = ErrorResponse(detail="Request body must be a JSON object")
        return _json_response(error.model_dump(), status_code=400)

    op = body.get("op")
    args_raw = body.get("args", {})
    agent_id = body.get("agent_id") or request.headers.get("X-Mngr-Agent-Id") or ""
    if not isinstance(op, str) or not is_known_op(op):
        error = ErrorResponse(detail=f"Unknown layout op: {op!r}")
        return _json_response(error.model_dump(), status_code=400)
    if not isinstance(args_raw, dict):
        error = ErrorResponse(detail="``args`` must be a JSON object")
        return _json_response(error.model_dump(), status_code=400)

    agent_manager: AgentManager = get_state().agent_manager
    agent_name_by_id = {a["id"]: a["name"] for a in agent_manager.get_agents_serialized()}
    layout_dir = _primary_agent_layout_dir()

    if op in {"list", "inspect"}:
        slug, error_response = _resolve_requested_layout_slug(args_raw, layout_dir)
        if error_response is not None:
            return error_response
        device = str(args_raw.get("device") or projects.DEFAULT_DEVICE)
        try:
            projects.validate_device(device)
        except projects.ProjectDeviceError as e:
            return _json_response(ErrorResponse(detail=str(e)).model_dump(), status_code=400)
        layout_path = (
            projects.project_content_path(layout_dir, slug, device)
            if layout_dir is not None and slug is not None
            else None
        )
        if op == "list":
            entries = layout_list(
                agent_manager.list_service_names(),
                agent_manager.get_agents_serialized(),
                layout_path,
                agent_name_by_id,
            )
            # Log the caller for telemetry; v1 has no enforcement.
            logger.info("layout op={} agent_id={} layout={} entries={}", op, agent_id, slug, len(entries))
            return _json_response({"ok": True, "layout_slug": slug, "entries": entries})
        summary = layout_inspect(layout_path, agent_name_by_id)
        logger.info("layout op={} agent_id={} layout={} panels={}", op, agent_id, slug, len(summary.get("panels", [])))
        return _json_response({"ok": True, "layout_slug": slug, "layout": summary})

    if op == "views":
        # Enumerate the views themselves: every registered project plus the
        # unfiltered Everything view, with each one's member list, per-device
        # content presence, and which connected clients have it in front.
        # ``context`` answers "who asked"; this answers "what views exist".
        if layout_dir is None:
            return _json_response({"ok": True, "views": [], "last_active_id": None})
        clients_by_view: dict[str, list[str]] = {}
        for client_info in get_state().broadcaster.get_connected_client_infos():
            clients_by_view.setdefault(client_info["active_layout_slug"], []).append(client_info["client_id"])
        views = [
            _layout_views_entry(layout_dir, clients_by_view, info.project_id, info.name, list(info.members), False)
            for info in projects.list_projects(layout_dir)
        ]
        # Everything has no member list: it shows whatever exists.
        views.append(
            _layout_views_entry(
                layout_dir, clients_by_view, projects.EVERYTHING_VIEW_ID, projects.EVERYTHING_VIEW_NAME, [], True
            )
        )
        last_active_id = projects.get_last_active_id(layout_dir)
        logger.info("layout op={} agent_id={} views={}", op, agent_id, len(views))
        return _json_response(
            {"ok": True, "views": [view.model_dump() for view in views], "last_active_id": last_active_id}
        )

    if op == "context":
        # Per-client activity summary: who is connected, on which layout,
        # and what they recently asked for. The live registry overrides the
        # event-log-derived current layout for connected clients (fresher,
        # and correct even if an event write was skipped).
        events_path = _client_activity_events_path()
        events = client_activity.read_client_activity_events(events_path) if events_path is not None else []
        connected_infos = get_state().broadcaster.get_connected_client_infos()
        live_layout_by_client_id = {info["client_id"]: info["active_layout_slug"] for info in connected_infos}
        clients = client_activity.summarize_client_activity(events, set(live_layout_by_client_id))
        for client_summary in clients:
            live_layout = live_layout_by_client_id.get(client_summary["client_id"])
            if live_layout:
                client_summary["current_layout"] = live_layout
        logger.info("layout op={} agent_id={} clients={}", op, agent_id, len(clients))
        return _json_response({"ok": True, "clients": clients})

    if op == "load":
        requested = args_raw.get("layout")
        if not isinstance(requested, str) or not requested:
            error = ErrorResponse(detail="'load' requires a layout name in args.layout")
            return _json_response(error.model_dump(), status_code=400)
        if layout_dir is None:
            error = ErrorResponse(detail="No primary agent configured for this workspace")
            return _json_response(error.model_dump(), status_code=500)
        slug, error_response = _resolve_requested_layout_slug(args_raw, layout_dir)
        if error_response is not None:
            return error_response
        if slug is None:
            # Unreachable: an explicit layout name (validated above) always
            # resolves to a slug or an error response.
            error = ErrorResponse(detail="Failed to resolve the requested layout")
            return _json_response(error.model_dump(), status_code=500)
        display_name = _layout_op_display_name(layout_dir, slug)
        # Target the explicitly-named client, else the client that most
        # recently messaged the requesting agent, else every client.
        explicit_client = args_raw.get("client")
        if isinstance(explicit_client, str) and explicit_client:
            target_client_id: str | None = explicit_client
        else:
            events_path = _client_activity_events_path()
            events = client_activity.read_client_activity_events(events_path) if events_path is not None else []
            target_client_id = client_activity.find_client_id_for_agent(events, agent_id)
        get_state().broadcaster.broadcast_load_layout(slug, display_name, target_client_id)
        logger.info("layout op={} agent_id={} layout={} target_client={}", op, agent_id, slug, target_client_id)
        return _json_response({"ok": True, "layout": slug, "target_client_id": target_client_id})

    if not is_broadcasting_op(op):
        # Defensive: every non-list/inspect op should broadcast. Catch
        # drift in the op-set definitions.
        error = ErrorResponse(detail=f"Op {op!r} has no broadcast handler")
        return _json_response(error.model_dump(), status_code=500)

    # Terminal creation is the one path where the script returns a ref
    # synchronously: the frontend's "New terminal" button gives each
    # terminal a freshly-minted iframe panel id, so the server pre-mints
    # one here, injects it into the broadcast args (the frontend uses it
    # verbatim), and reports the resulting ``terminal:<hash>`` ref back
    # in the HTTP response. Every other ref kind either dedups against
    # the existing panel set or is discoverable via a subsequent
    # ``inspect``.
    # A browser pane must name a specific fleet browser. Reject a session-less
    # ``service:browser`` open/split before it broadcasts, so an agent can't spawn
    # the orphan "Open a browser from the + menu" placeholder pane. Guides the caller
    # to the right form rather than erroring opaquely. The fleet's own pane-pull
    # always carries ``?session=<name>``, so it is unaffected.
    if op in {"open", "split"} and is_sessionless_browser_ref(args_raw.get("ref")):
        error = ErrorResponse(
            detail=(
                "A browser pane needs a specific browser name: use "
                "'service:browser?session=<name>', or the agentic-browser-fleet 'new'/'task' "
                "commands, which open the pane for you. The bare 'service:browser' opens a "
                "viewer bound to no browser."
            )
        )
        return _json_response(error.model_dump(), status_code=400)

    allocated_ref: str | None = None
    if op in {"open", "split"} and args_raw.get("ref") == "service:terminal":
        panel_id, allocated_ref = allocate_terminal_panel_id()
        args_raw = {**args_raw, "panel_id": panel_id}

    layout_mutex: LayoutMutex = get_state().layout_mutex
    broadcaster: WebSocketBroadcaster = get_state().broadcaster
    if is_mutating_op(op):
        # Mutating ops are view-targeted: they are delivered only to connected
        # clients that have the target view active (those clients apply the
        # mutation and autosave it into the view's file). Naming no view means
        # "the view the user is looking at" -- the resolve below defaults to
        # the connected client's own view (see ``_default_view_id``) -- and
        # with no client on the resolved view the op cannot take effect
        # anywhere, so it fails loudly rather than broadcasting into the void.
        target_layout_slug, layout_error_response = _resolve_requested_layout_slug(args_raw, layout_dir)
        if layout_error_response is not None:
            return layout_error_response
        if target_layout_slug is None or not broadcaster.has_client_on_layout(target_layout_slug):
            # The name for the miss: what the caller asked for, or what the
            # default resolved to when they asked for nothing.
            requested_layout = args_raw.get("layout") or target_layout_slug or "<no view>"
            connected_clients = broadcaster.get_connected_client_infos()
            _loguru_logger.warning(
                "Layout op {!r} rejected (412): no connected client on layout {!r}; connected clients: {}",
                op,
                requested_layout,
                connected_clients,
            )
            client_summary = (
                ", ".join(
                    f"{info['client_id']} (layout={info['active_layout_slug']}, device={info['device_kind']})"
                    for info in connected_clients
                )
                or "none"
            )
            error = ErrorResponse(
                detail=(
                    f"No connected client has layout '{requested_layout}' active. Ask the user to switch "
                    f"to it, or run `layout.py load {requested_layout!r}` first. "
                    f"Connected clients: {client_summary}."
                )
            )
            return _json_response(error.model_dump(), status_code=412)
        broadcast_args = {key: value for key, value in args_raw.items() if key != "layout"}
        holder = layout_mutex.try_acquire(agent_id, op, args_raw)
        if holder is not None:
            error_body = {
                "detail": (
                    f"Another layout op is in flight: agent_id={holder['agent_id']} "
                    f"op={holder['operation']}. Retry after the mutex TTL elapses."
                ),
                "retry_after_ms": layout_mutex.retry_after_ms(),
                "in_flight": holder,
            }
            return _json_response(error_body, status_code=409)
        try:
            broadcaster.broadcast_layout_op(
                op, broadcast_args, requester_agent_id=agent_id, target_layout_slug=target_layout_slug
            )
        finally:
            layout_mutex.release(agent_id, op)
    else:
        broadcaster.broadcast_layout_op(op, args_raw, requester_agent_id=agent_id)

    logger.info("layout op={} agent_id={} args={}", op, agent_id, args_raw)
    response_body: dict[str, Any] = {"ok": True}
    if allocated_ref is not None:
        response_body["ref"] = allocated_ref
    return _json_response(response_body)


def _handle_unhandled_exception(exc: Exception) -> Response | HTTPException:
    # Let werkzeug's own HTTP errors (404 routing, 405, etc.) render normally;
    # only genuine unhandled exceptions become a 500 JSON body. Returning the
    # exception (not re-raising it) is how Flask keeps the real status code --
    # a raise from inside the handler re-enters handle_exception and comes out
    # as a 500.
    if isinstance(exc, HTTPException):
        return exc
    tb = traceback.format_exception(type(exc), exc, exc.__traceback__)
    logger.error("Unhandled exception on {} {}: {}\n{}", request.method, request.path, exc, "".join(tb))
    return _json_response({"detail": f"Internal server error: {exc}"}, status_code=500)


def create_application(state: SystemInterfaceState) -> Flask:
    """Assemble the Flask app around an already-built ``SystemInterfaceState``.

    Pure assembler: it wires routes, plugins, and error handling onto the app
    and attaches the injected ``state``. It constructs no collaborators and
    starts nothing. The composition root (``main.build_production_state`` plus
    ``main.main``) builds the real object graph and starts the agent manager;
    tests build a ``SystemInterfaceState`` with fakes via
    ``testing.build_test_state`` and pass it here.
    """
    # static_folder=None disables Flask's default /static route; the system
    # interface serves its own static assets explicitly below.
    application = Flask(__name__, static_folder=None)
    application.config["SOCK_SERVER_OPTIONS"] = {
        "ping_interval": _WS_PING_INTERVAL_SECONDS,
        # Echo back whatever subprotocol a client offers so subprotocol-bearing
        # WS clients can connect; see ``_ReflectClientSubprotocols``.
        "subprotocols": _ReflectClientSubprotocols(),
    }
    attach_state(application, state)

    plugin_manager = get_plugin_manager()
    plugin_manager.hook.register_event_broadcaster(broadcaster=state.event_queues.broadcast)

    application.register_error_handler(Exception, _handle_unhandled_exception)

    plugin_manager.hook.endpoint(app=application)

    sock = Sock(application)

    application.add_url_rule("/", view_func=_index, methods=["GET"])
    application.add_url_rule("/favicon.ico", view_func=_favicon, methods=["GET"])
    application.add_url_rule("/api/agents", view_func=_list_agents_endpoint, methods=["GET"])
    application.add_url_rule("/api/agents/create-chat", view_func=_create_chat_agent, methods=["POST"])
    application.add_url_rule("/api/agents/<agent_id>/events", view_func=_get_events, methods=["GET"])
    application.add_url_rule("/api/agents/<agent_id>/stream", view_func=_stream_events, methods=["GET"])
    application.add_url_rule("/api/agents/<agent_id>/message", view_func=_send_message_endpoint, methods=["POST"])
    application.add_url_rule("/api/harnesses", view_func=_get_harnesses_endpoint, methods=["GET"])
    application.add_url_rule("/api/agents/<agent_id>/model", view_func=_set_model_choice_endpoint, methods=["POST"])
    application.add_url_rule(
        "/api/agents/<agent_id>/model-options", view_func=_get_model_options_endpoint, methods=["GET"]
    )
    application.add_url_rule("/api/agents/<agent_id>/powered-by", view_func=_get_powered_by_endpoint, methods=["GET"])
    application.add_url_rule(
        "/api/agents/<agent_id>/fast-mode-answered",
        view_func=_mark_fast_mode_prompt_answered,
        methods=["POST"],
    )
    application.add_url_rule("/api/activity", view_func=_activity_endpoint, methods=["POST"])
    application.add_url_rule("/api/uploads", view_func=_upload_attachment, methods=["POST"])
    application.add_url_rule("/api/uploads/<path:relative_path>", view_func=_serve_attachment, methods=["GET"])
    application.add_url_rule(
        "/api/uploads/<path:relative_path>",
        view_func=_delete_attachment,
        methods=["DELETE"],
        endpoint="_delete_attachment",
    )
    application.add_url_rule("/api/agents/<agent_id>/interrupt", view_func=_interrupt_agent_endpoint, methods=["POST"])
    application.add_url_rule("/api/agents/<agent_id>/flush-queue", view_func=_flush_queue_endpoint, methods=["POST"])
    application.add_url_rule(
        "/api/agents/<agent_id>/shoulder-tap-atomic",
        view_func=_shoulder_tap_atomic_endpoint,
        methods=["POST"],
    )
    application.add_url_rule(
        "/api/agents/<agent_id>/drain-to-composer", view_func=_drain_to_composer_endpoint, methods=["POST"]
    )
    application.add_url_rule("/api/projects", view_func=_list_projects_endpoint, methods=["GET"])
    application.add_url_rule(
        "/api/projects", view_func=_create_project_endpoint, methods=["POST"], endpoint="_create_project"
    )
    # The static member routes are registered before ``/api/projects/<project_id>``
    # for readability only: werkzeug matches a literal segment ahead of a
    # converter regardless of registration order, so ``/api/projects/members``
    # never resolves to a project called "members".
    application.add_url_rule("/api/projects/members", view_func=_list_project_members_endpoint, methods=["GET"])
    application.add_url_rule("/api/projects/members/share", view_func=_share_project_member_endpoint, methods=["POST"])
    application.add_url_rule("/api/projects/<project_id>", view_func=_get_project_endpoint, methods=["GET"])
    application.add_url_rule(
        "/api/projects/<project_id>",
        view_func=_autosave_project_endpoint,
        methods=["POST"],
        endpoint="_autosave_project",
    )
    application.add_url_rule(
        "/api/projects/<project_id>/settings", view_func=_update_project_settings_endpoint, methods=["POST"]
    )
    application.add_url_rule(
        "/api/projects/<project_id>/members", view_func=_add_project_member_endpoint, methods=["POST"]
    )
    application.add_url_rule(
        "/api/projects/<project_id>/members/remove", view_func=_remove_project_member_endpoint, methods=["POST"]
    )
    application.add_url_rule(
        "/api/projects/<project_id>/shortcuts", view_func=_set_project_shortcut_endpoint, methods=["POST"]
    )
    application.add_url_rule("/api/projects/<project_id>/delete", view_func=_delete_project_endpoint, methods=["POST"])
    application.add_url_rule(
        "/api/projects/panels/<panel_id>/delete", view_func=_delete_project_panel_endpoint, methods=["POST"]
    )
    # Titles are keyed by ref and belong to the machine rather than to any one
    # project, so they hang off their own route rather than under /api/projects.
    application.add_url_rule("/api/member-titles", view_func=_list_member_titles_endpoint, methods=["GET"])
    application.add_url_rule(
        "/api/member-titles", view_func=_set_member_title_endpoint, methods=["POST"], endpoint="_set_member_title"
    )
    # Last-used is keyed by ref and machine-wide for the same reason titles are.
    application.add_url_rule("/api/member-last-used", view_func=_list_member_last_used_endpoint, methods=["GET"])
    application.add_url_rule(
        "/api/member-last-used",
        view_func=_touch_member_last_used_endpoint,
        methods=["POST"],
        endpoint="_touch_member_last_used",
    )
    application.add_url_rule("/api/agents/<agent_id>/screen", view_func=_get_screen_capture, methods=["GET"])
    application.add_url_rule("/api/agents/<agent_id>/destroy", view_func=_destroy_agent, methods=["POST"])
    application.add_url_rule("/api/agents/<agent_id>/start", view_func=_start_agent, methods=["POST"])
    application.add_url_rule("/api/agents/<agent_id>/stop", view_func=_stop_agent, methods=["POST"])
    application.add_url_rule("/api/terminals", view_func=_list_terminals, methods=["GET"])
    application.add_url_rule("/api/terminals/allocate", view_func=_allocate_terminal, methods=["POST"])
    application.add_url_rule(
        "/api/terminals/banner-dismissed",
        view_func=_get_terminal_banner_dismissed,
        methods=["GET"],
    )
    application.add_url_rule(
        "/api/terminals/banner-dismissed",
        view_func=_set_terminal_banner_dismissed,
        methods=["POST"],
        endpoint="_set_terminal_banner_dismissed",
    )
    application.add_url_rule(
        "/api/terminals/<session_name>/destroy",
        view_func=_destroy_terminal,
        methods=["POST"],
    )
    application.add_url_rule("/api/terminals/notify", view_func=_terminal_notify_endpoint, methods=["POST"])
    application.add_url_rule("/api/browsers", view_func=_browsers_passthrough, methods=["GET", "POST"])
    application.add_url_rule("/api/browsers/<string:name>", view_func=_destroy_browser_passthrough, methods=["DELETE"])
    application.add_url_rule(
        "/api/apps/<string:name>/deregister", view_func=_deregister_app_endpoint, methods=["POST"]
    )
    application.add_url_rule("/api/apps/<string:name>/stop", view_func=_stop_app_endpoint, methods=["POST"])
    application.add_url_rule("/api/apps/<string:name>/start", view_func=_start_app_endpoint, methods=["POST"])
    application.add_url_rule("/api/apps/instances", view_func=_list_app_instances_endpoint, methods=["GET"])
    application.add_url_rule(
        "/api/apps/<string:name>/instances/allocate",
        view_func=_allocate_app_instance_endpoint,
        methods=["POST"],
    )
    application.add_url_rule("/api/member-locations", view_func=_list_member_locations_endpoint, methods=["GET"])
    application.add_url_rule(
        "/api/member-locations",
        view_func=_set_member_location_endpoint,
        methods=["POST"],
        endpoint="_set_member_location_endpoint",
    )
    auth_endpoints.register_routes(application)
    accounts_endpoints.register_routes(application)
    latchkey_endpoints.register_routes(application)
    application.add_url_rule("/api/layout/broadcast", view_func=_layout_broadcast_endpoint, methods=["POST"])
    application.add_url_rule(
        "/api/agents/<agent_id>/subagents/<subagent_session_id>/events",
        view_func=_get_subagent_events,
        methods=["GET"],
    )
    application.add_url_rule(
        "/api/agents/<agent_id>/subagents/<subagent_session_id>/stream",
        view_func=_stream_subagent_events,
        methods=["GET"],
    )
    sock.route("/api/ws")(_ws_endpoint)
    sock.route("/api/proto-agents/<agent_id>/logs")(_proto_agent_logs_endpoint)
    application.add_url_rule("/plugins/<basename>", view_func=_serve_static_file, methods=["GET"])

    # Registered unconditionally, even when the bundle is absent at startup: the
    # directory can appear later (a rebuild), and a route decided at construction
    # time can never notice. Without the route, asset requests fall through to
    # the catch-all below and come back as index.html with a text/html type,
    # which the browser refuses as a module script -- a blank screen instead of
    # the recoverable placeholder. A file that really is missing gets the plain
    # 404 ``_serve_asset`` answers with itself.
    application.add_url_rule("/assets/<path:filename>", view_func=_serve_asset, methods=["GET"])

    application.add_url_rule("/<path:path>", view_func=_index_catch_all, methods=["GET"])

    return application
