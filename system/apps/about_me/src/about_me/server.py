"""The "What Studio knows about me" app: a view onto the agent's memory, with the user's controls.

One page at ``/`` lists every memory in ``data/memories/`` grouped by what it is about (the user, how they like to
work, their projects, pointers), each with Edit and Delete, and the web lookup's memory fact by fact with its sources.
The user can add a memory about themselves, forget what the lookup found, or forget everything. The page is plain
HTML on the page kit (the shell-built ``/_static/workspace_theme.css`` and ``.js``), so it wears the workspace's
theme. It reads and writes only the memory directory and the lookup's sources, under the workspace root it runs
from, and never reaches the network.

- Listen address: bind ``HOST`` and ``PORT``, which default to the loopback address and this app's assigned port
  but honor ``ABOUT_ME_HOST`` and ``ABOUT_ME_PORT``, so a preview can boot on a spare port beside the live one.
- ``ABOUT_ME_WORKSPACE_DIR`` (the working directory by default, the repo root supervisord runs it from) is where
  the files are read, so a preview can run against a copy.

This is a synchronous Flask app served by the threaded Werkzeug server. The app owns its own browser origin (the
forwarder routes ``http://about-me.<workspace-host>/`` straight to this port), so root-absolute URLs work unmodified.
"""

import os
from pathlib import Path
from typing import Final

from flask import Flask
from flask import Response
from flask import abort
from flask import send_file
from flask.typing import ResponseReturnValue
from loguru import logger
from werkzeug.serving import run_simple

from about_me.data_types import AddMemoryRequest
from about_me.data_types import DeleteFactRequest
from about_me.data_types import DeleteMemoryRequest
from about_me.data_types import EditFactRequest
from about_me.data_types import EditMemoryRequest
from about_me.data_types import ForgetRequest
from about_me.data_types import HealthResponse
from about_me.data_types import ProfileResponse
from about_me.errors import InvalidFactError
from about_me.errors import StaleFactError
from about_me.errors import StaleMemoryError
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
    "Everything Studio remembers about you and how you like to work, as its own memory holds it, with controls to "
    "add to it, edit or delete any of it, or forget all of it."
)
STATIC_DIR: Final[Path] = Path(__file__).parent / "static"
# The page's own script and stylesheet, by file name under /assets/.
ASSET_FILES: Final[dict[str, str]] = {
    "about_me.js": "text/javascript",
    "about_me.css": "text/css",
}
# The shell-built browser modules every app serves from its own origin (relative to the repo root the app runs
# from): the app contract and the element context menu every page imports, and the page kit a plain-HTML page
# wears the workspace's theme with (docs/system/blueprint/workspace-themes/, section 5.3).
SHELL_STATIC_MODULES_DIR: Final[Path] = SHELL_APP_CONTRACT_PATH.parent
SHELL_STATIC_FILES: Final[dict[str, str]] = {
    "app_contract.js": "text/javascript",
    "context_menu.js": "text/javascript",
    "workspace_theme.js": "text/javascript",
    "workspace_theme.css": "text/css",
}
HEALTH_PATH: Final[str] = "/api/health"
HTTP_NOT_FOUND: Final[int] = 404
HTTP_BAD_REQUEST: Final[int] = 400
HTTP_CONFLICT: Final[int] = 409
_STALE_DETAIL: Final[str] = "What I remember changed since this page loaded; showing it again."

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
    ("POST", "/api/memories/add"): RouteModels(request_model=AddMemoryRequest, response_model=ProfileResponse),
    ("POST", "/api/memories/edit"): RouteModels(request_model=EditMemoryRequest, response_model=ProfileResponse),
    ("POST", "/api/memories/delete"): RouteModels(request_model=DeleteMemoryRequest, response_model=ProfileResponse),
    ("POST", "/api/facts/edit"): RouteModels(request_model=EditFactRequest, response_model=ProfileResponse),
    ("POST", "/api/facts/delete"): RouteModels(request_model=DeleteFactRequest, response_model=ProfileResponse),
    ("POST", "/api/forget"): RouteModels(request_model=ForgetRequest, response_model=ProfileResponse),
    ("GET", HEALTH_PATH): RouteModels(response_model=HealthResponse),
    ("GET", "/_static/{filename}"): SchemalessRoute(reason="one of the browser modules the shell builds"),
    **THEME_STATIC_ROUTE_TABLE,
}


def _send_named_file(directory: Path, filename: str, mimetypes: dict[str, str]) -> Response:
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
        """Everything the agent remembers: each memory, the web lookup's facts with their sources, and the raw files."""
        response = model_response(store.read())
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/api/memories/add")
    def add_memory() -> ResponseReturnValue:
        """Keep something the user typed about themselves as a new memory, with its line in the memory index.

        400 when the text is empty or longer than 500 characters.
        """
        body = parse_request_body(AddMemoryRequest)
        try:
            return model_response(store.add_memory(body.text))
        except InvalidFactError as invalid:
            return error_response(str(invalid), HTTP_BAD_REQUEST)

    @app.post("/api/memories/edit")
    def edit_memory() -> ResponseReturnValue:
        """Give one memory a new summary and text.

        400 when either is empty or too long; 409 when the memory changed since the page read it.
        """
        body = parse_request_body(EditMemoryRequest)
        try:
            return model_response(store.edit_memory(body.file_name, body.revision, body.description, body.body))
        except InvalidFactError as invalid:
            return error_response(str(invalid), HTTP_BAD_REQUEST)
        except StaleMemoryError as stale:
            logger.info("Refused a stale memory edit: {}", stale)
            return error_response(_STALE_DETAIL, HTTP_CONFLICT)

    @app.post("/api/memories/delete")
    def delete_memory() -> ResponseReturnValue:
        """Delete one memory and its line in the memory index; 409 when it changed since the page read it."""
        body = parse_request_body(DeleteMemoryRequest)
        try:
            return model_response(store.delete_memory(body.file_name, body.revision))
        except StaleMemoryError as stale:
            logger.info("Refused a stale memory delete: {}", stale)
            return error_response(_STALE_DETAIL, HTTP_CONFLICT)

    @app.post("/api/facts/edit")
    def edit_fact() -> ResponseReturnValue:
        """Give one fact new words, keeping its source and date.

        400 when the text cannot be one line of the memory; 409 when the memory changed since the page read it.
        """
        body = parse_request_body(EditFactRequest)
        try:
            return model_response(store.edit_fact(body.fact_id, body.text))
        except InvalidFactError as invalid:
            return error_response(str(invalid), HTTP_BAD_REQUEST)
        except StaleFactError as stale:
            logger.info("Refused a stale edit: {}", stale)
            return error_response(_STALE_DETAIL, HTTP_CONFLICT)

    @app.post("/api/facts/delete")
    def delete_fact() -> ResponseReturnValue:
        """Forget one fact: remove its line from the memory, and the sources no remaining fact cites.

        Deleting the last fact forgets the lookup, as "Forget what the lookup found" does. 409 when the memory changed since
        the page read it, so a stale page never deletes another line; the page then reloads it.
        """
        body = parse_request_body(DeleteFactRequest)
        try:
            return model_response(store.delete_fact(body.fact_id))
        except StaleFactError as stale:
            logger.info("Refused a stale delete: {}", stale)
            return error_response(_STALE_DETAIL, HTTP_CONFLICT)

    @app.post("/api/forget")
    def forget() -> ResponseReturnValue:
        """Forget what the web lookup found (its memory, its index line, the sources), or every memory."""
        body = parse_request_body(ForgetRequest)
        return model_response(store.forget_lookup() if body.scope == "lookup" else store.forget_everything())

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
