"""The "What Studio knows about me" app: what the opt-in web lookup remembered about the user, and their controls.

One page at ``/`` lists every fact in the lookup's memory with its source and date, a Delete per fact, "Forget all
of this", and the raw files behind a disclosure. The page is plain HTML on the page kit (the shell-built
``/_static/workspace_theme.css`` and ``.js``), so it wears the workspace's theme. It reads and writes only the three
files ``about_me.store`` names, under the workspace root it runs from, and never reaches the network.

- Listen address: bind ``HOST`` and ``PORT``, which default to the loopback address and this app's assigned port
  but honor ``ABOUT_ME_HOST`` and ``ABOUT_ME_PORT``, so a preview can boot on a spare port beside the live one.
- ``ABOUT_ME_WORKSPACE_DIR`` (the working directory by default, the repo root supervisord runs it from) is where
  the files are read, so a preview can run against a copy.

This is a synchronous Flask app served by the threaded Werkzeug server. The app owns its own browser origin (the
forwarder routes ``http://about-me.<workspace-host>/`` straight to this port), so root-absolute URLs work unmodified.
"""

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from flask import Flask
from flask import Response
from flask import abort
from flask import send_file
from flask.typing import ResponseReturnValue
from loguru import logger
from werkzeug.serving import run_simple

from about_me.data_types import DeleteFactRequest
from about_me.data_types import HealthResponse
from about_me.data_types import ProfileResponse
from about_me.errors import StaleFactError
from about_me.store import ProfileStore
from app_manifest.content_security_policy import attach_document_policy
from app_manifest.registry import SHELL_APP_CONTRACT_PATH
from app_schema.api_schema import DEFAULT_API_SCHEMA_PATH
from app_schema.api_schema import RouteModels
from app_schema.api_schema import RouteTable
from app_schema.api_schema import SchemalessRoute
from app_schema.api_schema import register_api_schema_route
from app_schema.request_guard import install_request_guard
from app_schema.routes import error_response
from app_schema.routes import model_response
from app_schema.routes import parse_request_body
from imbue.imbue_common.logging import setup_logging
from workspace_themes.flask_routes import THEME_STATIC_ROUTE_TABLE
from workspace_themes.flask_routes import register_workspace_theme_route

HOST: Final[str] = os.environ.get("ABOUT_ME_HOST", "127.0.0.1")
PORT: Final[int] = int(os.environ.get("ABOUT_ME_PORT", "8070"))
WORKSPACE_DIR: Final[Path] = Path(os.environ.get("ABOUT_ME_WORKSPACE_DIR", "."))

APP_DISPLAY_NAME: Final[str] = "What Studio knows about me"
APP_DESCRIPTION: Final[str] = (
    "What the web lookup you asked for remembered about you, each fact with its source and date, "
    "and controls to delete any of it or forget all of it."
)
STATIC_DIR: Final[Path] = Path(__file__).parent / "static"
# The page's own script and stylesheet, by file name under /assets/.
ASSET_FILES: Final[dict[str, str]] = {
    "about_me.js": "text/javascript",
    "about_me.css": "text/css",
}
# The shell-built browser modules every app serves from its own origin, relative to the repo root the app runs
# from (docs/system/blueprint/workspace-themes/, section 5.3).
SHELL_STATIC_MODULES_DIR: Final[Path] = SHELL_APP_CONTRACT_PATH.parent
SHELL_STATIC_FILES: Final[dict[str, str]] = {
    "app_contract.js": "text/javascript",
    "context_menu.js": "text/javascript",
    "workspace_theme.js": "text/javascript",
    "workspace_theme.css": "text/css",
}
HEALTH_PATH: Final[str] = "/api/health"
HTTP_NOT_FOUND: Final[int] = 404
HTTP_CONFLICT: Final[int] = 409

# Connects the page to the shell framing it (so its links open where the workspace opens them, and the shell
# reopens the window where it was), and installs the element context menu. Harmless outside the shell.
SHELL_PAGE_SCRIPT: Final[str] = """<script type="module">
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

ROUTE_TABLE: RouteTable = {
    ("GET", "/"): SchemalessRoute(reason="the What Studio knows about me page"),
    ("GET", "/assets/{filename}"): SchemalessRoute(reason="the page's own script or stylesheet"),
    ("GET", "/api/profile"): RouteModels(response_model=ProfileResponse),
    ("POST", "/api/facts/delete"): RouteModels(request_model=DeleteFactRequest, response_model=ProfileResponse),
    ("POST", "/api/forget"): RouteModels(response_model=ProfileResponse),
    ("GET", HEALTH_PATH): RouteModels(response_model=HealthResponse),
    ("GET", "/_static/{filename}"): SchemalessRoute(reason="one of the browser modules the shell builds"),
    **THEME_STATIC_ROUTE_TABLE,
}


def _send_named_file(directory: Path, filename: str, mimetypes: Mapping[str, str]) -> Response:
    """One of the files ``mimetypes`` names, from ``directory``, revalidated on every load; any other name is a 404."""
    mimetype = mimetypes.get(filename)
    path = directory / filename
    if mimetype is None or not path.is_file():
        abort(HTTP_NOT_FOUND)
    # Flask resolves a relative path against the app's own directory, not the working directory.
    response = send_file(path.absolute(), mimetype=mimetype)
    response.headers["Cache-Control"] = "no-cache"
    return response


def create_app(
    workspace_root: Path,
    static_directory: Path = STATIC_DIR,
    shell_static_directory: Path = SHELL_STATIC_MODULES_DIR,
) -> Flask:
    app = Flask("about_me", static_folder=None)
    install_request_guard(app, exempt_rules=frozenset())
    register_api_schema_route(
        app, DEFAULT_API_SCHEMA_PATH, title=APP_DISPLAY_NAME, description=APP_DESCRIPTION, route_table=ROUTE_TABLE
    )
    attach_document_policy(app, None)
    register_workspace_theme_route(app)
    store = ProfileStore(workspace_root)

    @app.get("/")
    def page() -> ResponseReturnValue:
        """The What Studio knows about me page."""
        document = (static_directory / "index.html").read_text(encoding="utf-8")
        return Response(
            document.replace("</body>", SHELL_PAGE_SCRIPT + "\n</body>"),
            mimetype="text/html",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/assets/<filename>")
    def asset(filename: str) -> ResponseReturnValue:
        """The page's own script or stylesheet, by file name."""
        return _send_named_file(static_directory, filename, ASSET_FILES)

    @app.get("/api/profile")
    def get_profile() -> ResponseReturnValue:
        """Everything remembered from the lookup: each fact with its source and date, and both raw files."""
        response = model_response(store.read())
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/api/facts/delete")
    def delete_fact() -> ResponseReturnValue:
        """Forget one fact: remove its line from the memory, and the sources no remaining fact cites.

        Deleting the last fact forgets everything, as "Forget all of this" does. 409 when the memory changed since
        the page read it, so a stale page never deletes another line; the page then reloads it.
        """
        body = parse_request_body(DeleteFactRequest)
        try:
            return model_response(store.delete_fact(body.fact_id))
        except StaleFactError as stale:
            logger.info("Refused a stale delete: {}", stale)
            return error_response("What I remember changed since this page loaded; showing it again.", HTTP_CONFLICT)

    @app.post("/api/forget")
    def forget_all() -> ResponseReturnValue:
        """Forget everything from the lookup: delete the memory, its line in the memory index, and the sources."""
        return model_response(store.forget_all())

    @app.get(HEALTH_PATH)
    def health() -> ResponseReturnValue:
        """Whether the app is up."""
        return model_response(HealthResponse(status="ok"))

    @app.get("/_static/<filename>")
    def shell_static_file(filename: str) -> ResponseReturnValue:
        """One of the browser modules the workspace shell builds: the app contract, the context menu, or the page kit."""
        return _send_named_file(shell_static_directory, filename, SHELL_STATIC_FILES)

    return app


def main() -> None:
    setup_logging(level=os.environ.get("ABOUT_ME_LOG_LEVEL", "INFO"))
    app = create_app(WORKSPACE_DIR)
    logger.info("Serving What Studio knows about me at http://{}:{}/", HOST, PORT)
    run_simple(HOST, PORT, app, threaded=True, use_reloader=False, use_debugger=False)


if __name__ == "__main__":
    main()
