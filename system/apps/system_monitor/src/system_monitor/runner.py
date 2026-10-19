"""The Memory & disk app: how much memory and disk this workspace is using.

Services run from /home/user/workspace (the repo root). The app writes
nothing to disk. It keeps the memory graph's last 10 minutes in memory, and
every open window shares one snapshot of the rest, taken at most every few
seconds (see ``stats``).

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
from pathlib import Path

from app_manifest.registry import SHELL_MODULE_PATHS
from app_schema.api_schema import (
    DEFAULT_API_SCHEMA_PATH,
    RouteModels,
    RouteTable,
    SchemalessRoute,
    register_api_schema_route,
)
from app_schema.routes import model_response
from flask import Flask, Response, abort, send_file
from werkzeug.serving import run_simple

from system_monitor import memory, stats
from system_monitor.data_types import HealthResponse, StatsResponse

# Listen address. Defaults to this app's assigned port (below 8080, the first
# port the build-app scaffold hands a user app) but is overridable via the env
# vars so an editing agent can boot a throwaway instance on a spare port next to
# the live one (see the update-app skill).
HOST = os.environ.get("SYSTEM_MONITOR_HOST", "127.0.0.1")
PORT = int(os.environ.get("SYSTEM_MONITOR_PORT", "8040"))

# What the manifest says the app is, repeated here for the API document (the test checks they agree).
APP_DISPLAY_NAME = "Memory & disk"
APP_DESCRIPTION = (
    "How much memory and disk the workspace is using, what is holding the memory, and what the memory guard stopped."
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

# Every open window polls /api/stats; they all share one snapshot, taken at most once per few
# seconds, so a second or third window adds almost no work.
SNAPSHOT = stats.SharedSnapshot(stats.snapshot)

# The memory graph's last 10 minutes, recorded by the app (see main) so the line stays whole
# while a window is hidden or reloaded. Sent fresh with every response, outside the snapshot.
HISTORY = memory.MemoryHistory()

# What every route exchanges, which the app's API document publishes and every answer is checked against.
ROUTE_TABLE: RouteTable = {
    ("GET", "/"): SchemalessRoute(reason="the Memory & disk page"),
    ("GET", "/api/stats"): RouteModels(response_model=StatsResponse),
    ("GET", "/_static/{basename}"): SchemalessRoute(reason="a browser-side module the shell builds"),
    ("GET", "/health"): RouteModels(response_model=HealthResponse),
}

app = Flask("system_monitor", static_folder=None)
# The app describes its own API: an OpenAPI document of every route below, at the manifest's api_schema_path.
register_api_schema_route(
    app, DEFAULT_API_SCHEMA_PATH, title=APP_DISPLAY_NAME, description=APP_DESCRIPTION, route_table=ROUTE_TABLE
)


@app.route("/")
def index() -> Response:
    """The Memory & disk page."""
    page = (STATIC_DIR / "index.html").read_text()
    return Response(
        page.replace("</body>", SHELL_PAGE_SCRIPT + "\n</body>"), mimetype="text/html"
    )


@app.route("/api/stats")
def get_stats() -> Response:
    """The latest reading: memory and disk in use, what is using the memory, the memory graph's last 10 minutes, and
    what the memory guard stopped in the last 24 hours."""
    response = model_response(StatsResponse.model_validate({**SNAPSHOT.get(), "history": HISTORY.samples()}))
    response.headers["Cache-Control"] = "no-store"
    return response


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
    memory.start_sampler(HISTORY)
    run_simple(HOST, PORT, app, threaded=True, use_reloader=False, use_debugger=False)


if __name__ == "__main__":
    main()
