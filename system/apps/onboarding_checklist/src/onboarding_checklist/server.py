"""The Onboarding checklist app: one page at ``/`` listing the four onboarding items, and the route it reads them
from.

The page is a build of ``frontend/`` (one of the npm workspace's frontends). It reads ``/api/items`` for the items
and whether each is done, and a click on an entry shows that item's pointer through the shell that frames the
page. The app stores nothing: what is done is the workspace's onboarding markers and its app registry.

- Listen address: bind ``HOST`` and ``PORT``, which default to the loopback address and this app's assigned port
  but honor ``ONBOARDING_CHECKLIST_HOST`` and ``ONBOARDING_CHECKLIST_PORT``, so a preview can boot on a spare port
  beside the live one.

This is a synchronous Flask app served by the threaded Werkzeug server. The app owns its own browser origin (the
forwarder routes ``http://onboarding-checklist.<workspace-host>/`` straight to this port), so root-absolute URLs
work unmodified.
"""

import os
from collections.abc import Callable
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from flask import Flask
from flask import Response
from flask import send_file
from flask.typing import ResponseReturnValue
from loguru import logger
from werkzeug.serving import run_simple

from app_manifest.built_assets import send_built_asset
from app_manifest.content_security_policy import attach_document_policy
from app_manifest.registry import APP_CONTRACT_ROUTE
from app_manifest.registry import SHELL_APP_CONTRACT_PATH
from app_manifest.registry_location import registry_path
from app_manifest.user_apps import has_user_built_app
from app_schema.api_schema import DEFAULT_API_SCHEMA_PATH
from app_schema.api_schema import RouteModels
from app_schema.api_schema import RouteTable
from app_schema.api_schema import SchemalessRoute
from app_schema.api_schema import register_api_schema_route
from app_schema.request_guard import install_request_guard
from app_schema.routes import error_response
from app_schema.routes import model_response
from imbue.imbue_common.logging import setup_logging
from onboarding_checklist.data_types import ChecklistItem
from onboarding_checklist.data_types import HealthResponse
from onboarding_checklist.data_types import ItemsResponse
from onboarding_checklist.items import read_checklist_items
from workspace_themes.flask_routes import THEME_STATIC_ROUTE_TABLE
from workspace_themes.flask_routes import register_workspace_theme_route

HOST: Final[str] = os.environ.get("ONBOARDING_CHECKLIST_HOST", "127.0.0.1")
PORT: Final[int] = int(os.environ.get("ONBOARDING_CHECKLIST_PORT", "8070"))

APP_DISPLAY_NAME: Final[str] = "Onboarding checklist"
APP_DESCRIPTION: Final[str] = (
    "The four things worth trying in a new workspace, the done ones checked; a click on one shows where it is."
)
STATIC_DIR: Final[Path] = Path(__file__).parent / "static"
PAGE_DOCUMENT_FILENAME: Final[str] = "index.html"
HEALTH_PATH: Final[str] = "/api/health"
ITEMS_PATH: Final[str] = "/api/items"

HTTP_NOT_FOUND: Final[int] = 404

_NOT_BUILT_PAGE: Final[str] = (
    '<!doctype html><html><head><meta charset="utf-8"><title>Onboarding checklist</title></head>'
    "<body><p>The Onboarding checklist has not been built yet (run <code>npm run build</code> in "
    "<code>system/</code>).</p></body></html>"
)

ROUTE_TABLE: RouteTable = {
    ("GET", "/"): SchemalessRoute(reason="the checklist page"),
    ("GET", "/assets/{filename}"): SchemalessRoute(reason="one file of the page's built bundle"),
    ("GET", HEALTH_PATH): RouteModels(response_model=HealthResponse),
    ("GET", ITEMS_PATH): RouteModels(response_model=ItemsResponse),
    ("GET", APP_CONTRACT_ROUTE): SchemalessRoute(reason="the app contract module, a JavaScript file"),
    **THEME_STATIC_ROUTE_TABLE,
}


def create_app(
    static_directory: Path,
    read_items: Callable[[], Sequence[ChecklistItem]],
    contract_path: Path = SHELL_APP_CONTRACT_PATH,
) -> Flask:
    """The app's one Flask application: the request guard every app installs ahead of its routes, then the page,
    its bundle, the health probe and the items. ``read_items`` reads the items as they stand, once per request."""
    app = Flask("onboarding_checklist", static_folder=None)
    install_request_guard(app, exempt_rules=frozenset())
    register_api_schema_route(
        app, DEFAULT_API_SCHEMA_PATH, title=APP_DISPLAY_NAME, description=APP_DESCRIPTION, route_table=ROUTE_TABLE
    )
    attach_document_policy(app, None)
    # The workspace's themes, from this app's own origin, so the page can wear the one the shell names.
    register_workspace_theme_route(app)
    static_directory = static_directory.resolve()

    @app.get("/")
    def page() -> ResponseReturnValue:
        """The checklist page."""
        document = static_directory / PAGE_DOCUMENT_FILENAME
        if not document.is_file():
            return Response(_NOT_BUILT_PAGE, mimetype="text/html", headers={"Cache-Control": "no-store"})
        # Read rather than streamed, so the document policy can admit the page's inline theme boot by its hash.
        return Response(
            document.read_text(encoding="utf-8"), mimetype="text/html", headers={"Cache-Control": "no-store"}
        )

    @app.get("/assets/<path:filename>")
    def asset(filename: str) -> ResponseReturnValue:
        """One file of the page's built bundle, by name; one named from its content is cached for good."""
        return send_built_asset((static_directory / "assets").absolute(), filename)

    @app.get(HEALTH_PATH)
    def health() -> ResponseReturnValue:
        """Whether the app is up, and whether its page has been built."""
        return model_response(
            HealthResponse(status="ok", is_frontend_built=(static_directory / PAGE_DOCUMENT_FILENAME).is_file())
        )

    @app.get(ITEMS_PATH)
    def items() -> ResponseReturnValue:
        """The four onboarding items in order, each with whether the workspace's owner has done it."""
        return model_response(ItemsResponse(items=tuple(read_items())))

    @app.get(APP_CONTRACT_ROUTE)
    def app_contract() -> ResponseReturnValue:
        """The app contract module: how the page talks to the shell framing it."""
        if not contract_path.is_file():
            return error_response(
                f"the workspace shell's frontend is not built: {contract_path} is missing", HTTP_NOT_FOUND
            )
        # Flask resolves a relative path against the app's own directory, not the cwd the path names.
        return send_file(contract_path.absolute(), mimetype="text/javascript")

    return app


def read_live_items() -> tuple[ChecklistItem, ...]:
    """The items as the workspace this app runs in has them: its markers, and the registry beside them, with the
    repo root the cwd every supervised program runs from."""
    return read_checklist_items(lambda: has_user_built_app(Path.cwd(), registry_path()), None)


def main() -> None:
    setup_logging(level=os.environ.get("ONBOARDING_CHECKLIST_LOG_LEVEL", "INFO"))
    app = create_app(STATIC_DIR, read_live_items)
    logger.info("Serving the Onboarding checklist at http://{}:{}/", HOST, PORT)
    run_simple(HOST, PORT, app, threaded=True, use_reloader=False, use_debugger=False)


if __name__ == "__main__":
    main()
