"""The Skills app: the pages at ``/`` and ``/<name>``, and the API they read.

Services run from /home/user/workspace (the repo root). The app stores nothing and changes nothing: everything it
shows is read from ``.agents/skills`` through the skills catalog (``system/libs/skills_catalog``) on every request,
so a skill an agent made a moment ago is listed on the next load.

- Listen address: bind ``HOST`` and ``PORT``, which default to the loopback address and this app's assigned port
  but honor ``SKILLS_APP_HOST`` and ``SKILLS_APP_PORT``, so a preview can boot on a spare port beside the live one.

This is a synchronous Flask app served by the threaded Werkzeug server. The app owns its own browser origin (the
forwarder routes ``http://skills.<workspace-host>/`` straight to this port), so root-absolute URLs work unmodified.
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

from app_manifest.content_security_policy import attach_document_policy
from app_manifest.registry import SHELL_MODULE_PATHS
from app_schema.api_schema import DEFAULT_API_SCHEMA_PATH
from app_schema.api_schema import RouteModels
from app_schema.api_schema import RouteTable
from app_schema.api_schema import SchemalessRoute
from app_schema.api_schema import register_api_schema_route
from app_schema.routes import error_response
from app_schema.routes import model_response
from imbue.imbue_common.logging import setup_logging
from skills_app.data_types import HealthResponse
from skills_catalog.catalog import build_catalog
from skills_catalog.catalog import parse_skill_name
from skills_catalog.catalog import repo_root_from_environment
from skills_catalog.data_types import SkillDetail
from skills_catalog.data_types import SkillListing
from skills_catalog.errors import GitHistoryError
from skills_catalog.errors import SkillNotFoundError
from skills_catalog.interfaces import SkillsCatalogInterface

HOST: Final[str] = os.environ.get("SKILLS_APP_HOST", "127.0.0.1")
PORT: Final[int] = int(os.environ.get("SKILLS_APP_PORT", "8060"))

# What the manifest says the app is, repeated here for the API document (the test checks they agree).
APP_DISPLAY_NAME: Final[str] = "Skills"
APP_DESCRIPTION: Final[str] = (
    "Every skill this workspace's agents have, where each came from, and its instructions as written."
)
STATIC_DIR: Final[Path] = Path(__file__).parent / "static"
PAGE_DOCUMENT_FILENAME: Final[str] = "index.html"
HEALTH_PATH: Final[str] = "/api/health"

HTTP_NOT_FOUND: Final[int] = 404
HTTP_INTERNAL_SERVER_ERROR: Final[int] = 500

# The browser-side modules the workspace shell builds and every app serves from its own origin, by file name.
SHELL_MODULES: Final[dict[str, Path]] = {path.name: path for path in SHELL_MODULE_PATHS}

# The script every page serves: it connects the page to the shell framing it, reports where the page is so the
# shell reopens this app's window at the same place, and installs the element context menu.
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
    ("GET", "/"): SchemalessRoute(reason="the Skills list page"),
    ("GET", "/{name}"): SchemalessRoute(reason="a skill's page"),
    ("GET", "/_static/{basename}"): SchemalessRoute(reason="a browser-side module the shell builds"),
    ("GET", HEALTH_PATH): RouteModels(response_model=HealthResponse),
    ("GET", "/api/skills"): RouteModels(response_model=SkillListing),
    ("GET", "/api/skills/{name}"): RouteModels(response_model=SkillDetail),
}


def create_app(catalog: SkillsCatalogInterface, static_directory: Path) -> Flask:
    app = Flask("skills_app", static_folder=None)
    register_api_schema_route(
        app, DEFAULT_API_SCHEMA_PATH, title=APP_DISPLAY_NAME, description=APP_DESCRIPTION, route_table=ROUTE_TABLE
    )
    attach_document_policy(app, None)
    page_document = static_directory.resolve() / PAGE_DOCUMENT_FILENAME

    def page() -> ResponseReturnValue:
        """The one document, with the shell's page script before its closing body tag."""
        html_content = page_document.read_text(encoding="utf-8")
        return Response(
            html_content.replace("</body>", SHELL_PAGE_SCRIPT + "\n</body>"),
            mimetype="text/html",
            headers={"Cache-Control": "no-store"},
        )

    def unreadable_history(error: GitHistoryError) -> ResponseReturnValue:
        logger.warning("Could not read the skills' history: {}", error)
        return error_response(f"Couldn't read where the skills came from: {error}", HTTP_INTERNAL_SERVER_ERROR)

    @app.get("/")
    def list_page() -> ResponseReturnValue:
        """The Skills list page."""
        return page()

    @app.get("/<name>")
    def skill_page(name: str) -> ResponseReturnValue:
        """A skill's page: the same document, which reads its path."""
        return page()

    @app.get("/_static/<basename>")
    def shell_module(basename: str) -> ResponseReturnValue:
        """One of the two browser-side modules the workspace shell builds and every app serves from its own origin."""
        module_path = SHELL_MODULES.get(basename)
        if module_path is None or not module_path.is_file():
            abort(HTTP_NOT_FOUND)
        return send_file(module_path.absolute(), mimetype="text/javascript")

    @app.get(HEALTH_PATH)
    def health() -> ResponseReturnValue:
        """Whether the app is up."""
        return model_response(HealthResponse(status="ok"))

    @app.get("/api/skills")
    def listing() -> ResponseReturnValue:
        """Every skill under .agents/skills with where it came from, by name."""
        try:
            response = model_response(catalog.list_skills())
        except GitHistoryError as e:
            return unreadable_history(e)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/skills/<name>")
    def detail(name: str) -> ResponseReturnValue:
        """One skill with its SKILL.md as written and the files beside it."""
        wanted = parse_skill_name(name)
        if wanted is None:
            return error_response(f"There is no skill called {name!r}.", HTTP_NOT_FOUND)
        try:
            response = model_response(catalog.describe_skill(wanted))
        except SkillNotFoundError as e:
            return error_response(str(e), HTTP_NOT_FOUND)
        except GitHistoryError as e:
            return unreadable_history(e)
        response.headers["Cache-Control"] = "no-store"
        return response

    return app


def main() -> None:
    setup_logging(level=os.environ.get("SKILLS_APP_LOG_LEVEL", "INFO"))
    app = create_app(build_catalog(repo_root_from_environment(os.environ)), STATIC_DIR)
    logger.info("Serving the Skills app at http://{}:{}/", HOST, PORT)
    run_simple(HOST, PORT, app, threaded=True, use_reloader=False, use_debugger=False)


if __name__ == "__main__":
    main()
