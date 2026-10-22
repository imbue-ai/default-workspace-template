"""The Skills app: the page at ``/`` and ``/<name>`` (the skills beside the one being read, or ``/?find=<query>``,
skills found online), and the API it reads.

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
from flask import request
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
from skills_app.data_types import OnlineSkillResults
from skills_app.data_types import RenderedInstructions
from skills_app.errors import SkillIndexUnavailableError
from skills_app.instructions import render_instructions
from skills_app.interfaces import SkillIndexSearcherInterface
from skills_app.online_search import MIN_QUERY_LENGTH
from skills_app.online_search import SkillsShSearcher
from skills_app.online_search import online_skills
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

HTTP_BAD_REQUEST: Final[int] = 400
HTTP_NOT_FOUND: Final[int] = 404
HTTP_INTERNAL_SERVER_ERROR: Final[int] = 500
HTTP_BAD_GATEWAY: Final[int] = 502

# The browser-side modules the workspace shell builds and every app serves from its own origin, by file name.
SHELL_MODULES: Final[dict[str, Path]] = {path.name: path for path in SHELL_MODULE_PATHS}

ROUTE_TABLE: RouteTable = {
    ("GET", "/"): SchemalessRoute(reason="the Skills list page"),
    ("GET", "/{name}"): SchemalessRoute(reason="a skill's page"),
    ("GET", "/_static/{basename}"): SchemalessRoute(reason="a browser-side module the shell builds"),
    ("GET", HEALTH_PATH): RouteModels(response_model=HealthResponse),
    ("GET", "/api/skills"): RouteModels(response_model=SkillListing),
    ("GET", "/api/skills/{name}"): RouteModels(response_model=SkillDetail),
    ("GET", "/api/skills/{name}/instructions"): RouteModels(response_model=RenderedInstructions),
    ("GET", "/api/find"): RouteModels(response_model=OnlineSkillResults),
}


def create_app(
    catalog: SkillsCatalogInterface, static_directory: Path, searcher: SkillIndexSearcherInterface
) -> Flask:
    app = Flask("skills_app", static_folder=None)
    register_api_schema_route(
        app, DEFAULT_API_SCHEMA_PATH, title=APP_DISPLAY_NAME, description=APP_DESCRIPTION, route_table=ROUTE_TABLE
    )
    attach_document_policy(app, None)
    page_document = static_directory.resolve() / PAGE_DOCUMENT_FILENAME

    def page() -> ResponseReturnValue:
        """The one document, which connects itself to the shell framing it."""
        return Response(
            page_document.read_text(encoding="utf-8"), mimetype="text/html", headers={"Cache-Control": "no-store"}
        )

    def unreadable_history(error: GitHistoryError) -> ResponseReturnValue:
        logger.warning("Failed to read the skills' history: {}", error)
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

    def described(name: str) -> SkillDetail | ResponseReturnValue:
        """The skill a request names, or the error response saying why there is none."""
        wanted = parse_skill_name(name)
        if wanted is None:
            return error_response(f"There is no skill called {name!r}.", HTTP_NOT_FOUND)
        try:
            return catalog.describe_skill(wanted)
        except SkillNotFoundError as e:
            return error_response(str(e), HTTP_NOT_FOUND)
        except GitHistoryError as e:
            return unreadable_history(e)

    @app.get("/api/skills/<name>")
    def detail(name: str) -> ResponseReturnValue:
        """One skill with its SKILL.md as written and the files beside it."""
        found = described(name)
        if not isinstance(found, SkillDetail):
            return found
        response = model_response(found)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/skills/<name>/instructions")
    def instructions(name: str) -> ResponseReturnValue:
        """One skill's SKILL.md body, rendered for reading."""
        found = described(name)
        if not isinstance(found, SkillDetail):
            return found
        response = model_response(RenderedInstructions(html=render_instructions(found.content, found.directory)))
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/find")
    def find() -> ResponseReturnValue:
        """The skills the online index matches for ``q``, most installed first, marked against the workspace's own."""
        query = request.args.get("q", "").strip()
        if len(query) < MIN_QUERY_LENGTH:
            return error_response(f"Search for at least {MIN_QUERY_LENGTH} characters.", HTTP_BAD_REQUEST)
        try:
            matches = searcher.search(query)
        except SkillIndexUnavailableError as e:
            logger.warning("Failed to search skills.sh: {}", e)
            return error_response(
                "Couldn't reach skills.sh. Check the workspace's internet connection and try again.", HTTP_BAD_GATEWAY
            )
        try:
            installed = catalog.list_skills().skills
        except GitHistoryError as e:
            return unreadable_history(e)
        response = model_response(OnlineSkillResults(query=query, skills=online_skills(matches, installed)))
        response.headers["Cache-Control"] = "no-store"
        return response

    return app


def main() -> None:
    setup_logging(level=os.environ.get("SKILLS_APP_LOG_LEVEL", "INFO"))
    app = create_app(build_catalog(repo_root_from_environment(os.environ)), STATIC_DIR, SkillsShSearcher())
    logger.info("Serving the Skills app at http://{}:{}/", HOST, PORT)
    run_simple(HOST, PORT, app, threaded=True, use_reloader=False, use_debugger=False)


if __name__ == "__main__":
    main()
