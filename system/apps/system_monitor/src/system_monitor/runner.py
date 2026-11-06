"""The System Monitor app: how much CPU, memory and disk this workspace is using.

Services run from /home/user/workspace (the repo root). The app writes
nothing to disk: the graphs and the busy stretches come from the files the
resource sampler keeps (``oom_priority.resource_sampler``), and every open
window shares one snapshot of the rest, taken at most every few seconds (see
``stats``). The views are paths of one page (``/``, ``/cpu``, ``/memory``,
``/disk``), so the shell reopens a window on the view it was on.

- Listen address: bind ``HOST`` and ``PORT`` (defined below), which default
  to the loopback address and this app's assigned port but honor the
  ``SYSTEM_MONITOR_HOST`` and ``SYSTEM_MONITOR_PORT`` env vars, so an editing
  agent can boot a throwaway instance on a *spare* port alongside the live
  one (see the update-app skill). Never hardcode them at the ``run_simple``
  call.

This is a synchronous Flask app served by the threaded Werkzeug server.
The app owns its own browser origin (the forwarder routes
``http://system-monitor.<workspace-host>/`` straight to this port), so it
serves at ``/`` and root-absolute URLs work unmodified -- nothing rewrites
anything.
"""

import os
from collections.abc import Callable
from pathlib import Path

from app_manifest.registry import SHELL_MODULE_PATHS
from app_schema.api_schema import (
    DEFAULT_API_SCHEMA_PATH,
    AlternateResponse,
    ErrorResponse,
    RouteModels,
    RouteTable,
    SchemalessRoute,
    register_api_schema_route,
)
from app_schema.routes import model_response, parse_request_body
from flask import Flask, Response, abort, request, send_file
from host_backup.config import get_events_dir
from werkzeug.serving import run_simple

from system_monitor import actions, backups, cpu, disk, memory, stats
from system_monitor.data_types import (
    DiskResponse,
    HealthResponse,
    HistoryResponse,
    ScanResponse,
    StatsResponse,
    StopRequest,
    StopResponse,
)

# Listen address. Defaults to this app's assigned port (below 8080, the first
# port the build-app scaffold hands a user app) but is overridable via the env
# vars so an editing agent can boot a throwaway instance on a spare port next to
# the live one (see the update-app skill).
HOST = os.environ.get("SYSTEM_MONITOR_HOST", "127.0.0.1")
PORT = int(os.environ.get("SYSTEM_MONITOR_PORT", "8040"))

# What the manifest says the app is, repeated here for the API document (the test checks they agree).
APP_DISPLAY_NAME = "System Monitor"
APP_DESCRIPTION = (
    "How much CPU, memory and disk the workspace is using, what is using them, what the memory guard stopped, "
    "and the state of the backups."
)

# The browser-side modules the workspace shell builds and every app serves from
# its own origin, by file name under /_static/: the app contract (how a page
# talks to the shell framing it) and the element context menu (the right-click
# menu whose last rows hand the clicked element to a chat).
SHELL_MODULES = {path.name: path for path in SHELL_MODULE_PATHS}

# The script every page serves (keep it on every page): it connects the page to
# the shell, reports where the page is on the handshake so the shell can reopen
# this app's window at the same place, and installs the element context menu.
# A page visited outside the shell runs it harmlessly: nothing arrives, and the
# menu's Explain and Modify rows grey out.
SHELL_PAGE_SCRIPT = """<script type="module">
  import { connectToShell } from "/_static/app_contract.js";
  import { installElementContextMenu } from "/_static/context_menu.js";
  let handshake = null;
  const connection = connectToShell({
    onHandshake: (received) => {
      handshake = received;
      connection.location(location.pathname + location.search, document.title);
    },
  });
  installElementContextMenu({ connection, handshake: () => handshake });
</script>"""

STATIC_DIR = Path(__file__).parent / "static"
# The views of the one page: the overview at the root and one per resource.
VIEW_PATHS = ("/", "/cpu", "/memory", "/disk")

# Every open window polls /api/stats; they all share one snapshot, taken at most once per few
# seconds, so a second or third window adds almost no work.
SNAPSHOT = stats.SharedSnapshot(stats.snapshot)

# The on-demand walk of the disk: one at a time, kept with its time until the app stops.
DISK_SCANNER = disk.DiskScanner(
    lambda: disk.scan(memory.disk(stats.WORKSPACE_DIR)["used"])
)

# What a Stop button's request runs (kind, id); tests put a stand-in here.
STOP: Callable[[str, str], None] = actions.stop

# What every route exchanges, which the app's API document publishes and every answer is checked against.
ROUTE_TABLE: RouteTable = {
    ("GET", "/"): SchemalessRoute(reason="the System Monitor page, on its overview"),
    ("GET", "/cpu"): SchemalessRoute(reason="the System Monitor page, on its CPU view"),
    ("GET", "/memory"): SchemalessRoute(
        reason="the System Monitor page, on its memory view"
    ),
    ("GET", "/disk"): SchemalessRoute(
        reason="the System Monitor page, on its disk view"
    ),
    ("GET", "/api/stats"): RouteModels(response_model=StatsResponse),
    ("GET", "/api/history"): RouteModels(response_model=HistoryResponse),
    ("GET", "/api/disk"): RouteModels(response_model=DiskResponse),
    ("POST", "/api/disk/scan"): RouteModels(
        response_model=ScanResponse,
        success_status=202,
        alternate_responses=(
            # 200: a walk was already running, so this request started none.
            AlternateResponse(status=200, model=ScanResponse),
        ),
    ),
    ("POST", "/api/stop"): RouteModels(
        request_model=StopRequest, response_model=StopResponse
    ),
    ("GET", "/_static/{basename}"): SchemalessRoute(
        reason="a browser-side module the shell builds"
    ),
    ("GET", "/health"): RouteModels(response_model=HealthResponse),
}

app = Flask("system_monitor", static_folder=None)
# The app describes its own API: an OpenAPI document of every route below, at the manifest's api_schema_path.
register_api_schema_route(
    app,
    DEFAULT_API_SCHEMA_PATH,
    title=APP_DISPLAY_NAME,
    description=APP_DESCRIPTION,
    route_table=ROUTE_TABLE,
)


def _page() -> Response:
    page = (STATIC_DIR / "index.html").read_text()
    return Response(
        page.replace("</body>", SHELL_PAGE_SCRIPT + "\n</body>"), mimetype="text/html"
    )


@app.route("/")
def index() -> Response:
    """The System Monitor page, on its overview."""
    return _page()


@app.route("/cpu")
def cpu_view() -> Response:
    """The System Monitor page, on its CPU view."""
    return _page()


@app.route("/memory")
def memory_view() -> Response:
    """The System Monitor page, on its memory view."""
    return _page()


@app.route("/disk")
def disk_view() -> Response:
    """The System Monitor page, on its disk view."""
    return _page()


@app.route("/api/stats")
def get_stats() -> Response:
    """The latest reading: CPU, memory and disk in use, what is using them, the last 10 minutes of both graphs, and
    the stops and busy stretches of the last 24 hours."""
    response = model_response(StatsResponse.model_validate(SNAPSHOT.get()))
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/history")
def get_history() -> Response:
    """Both graphs over a longer span (``?span=1h`` or ``24h``), from the sampler's minute lines."""
    span = request.args.get("span", "1h")
    if span not in cpu.SPANS:
        abort(400)
    response = model_response(
        HistoryResponse.model_validate(
            {
                "span": span,
                "step_seconds": cpu.SPANS[span][1],
                "samples": cpu.read_history(span),
            }
        )
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/disk")
def get_disk() -> Response:
    """The disk in use, the state of the backups, and what is on the disk when a walk has been asked for."""
    response = model_response(
        DiskResponse.model_validate(
            {
                "disk": memory.disk(stats.WORKSPACE_DIR),
                "backups": backups.summarize(get_events_dir()),
                "scan": DISK_SCANNER.snapshot(),
            }
        )
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/disk/scan", methods=["POST"])
def start_disk_scan() -> Response:
    """Walk the disk to see what is on it; 202 when this request started the walk, 200 when one was already running."""
    started = DISK_SCANNER.start()
    return model_response(
        ScanResponse.model_validate({"scan": DISK_SCANNER.snapshot()}),
        status=202 if started else 200,
    )


@app.route("/api/stop", methods=["POST"])
def stop_item() -> Response:
    """Stop what one of the breakdown's Stop buttons names: a chat's agent, a chat's code checkers, or one of the
    user's apps; 400 for a body that names nothing, else the refusal's own status and reason."""
    body = parse_request_body(StopRequest)
    try:
        STOP(body.kind, body.id)
    except actions.StopRefusedError as refused:
        return model_response(ErrorResponse(detail=refused.detail), refused.status)
    return model_response(StopResponse(status="ok"))


@app.route("/_static/<basename>")
def shell_module(basename: str) -> Response:
    """One of the two browser-side modules the workspace shell builds and every app serves from its own origin."""
    # The two shell-built modules and nothing else: a name that is not one of
    # them is a 404, so this route can never read outside that directory.
    module_path = SHELL_MODULES.get(basename)
    if module_path is None or not module_path.is_file():
        abort(404)
    # Flask resolves a relative path against the app's own directory, not the cwd.
    return send_file(module_path.absolute(), mimetype="text/javascript")


@app.route("/health")
def health() -> Response:
    """Whether the app is up."""
    return model_response(HealthResponse(status="ok"))


def main() -> None:
    run_simple(HOST, PORT, app, threaded=True, use_reloader=False, use_debugger=False)


if __name__ == "__main__":
    main()
