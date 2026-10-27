"""The Style Guide app: one page at ``/`` cataloguing the workspace's design system, and the bundle it loads.

The page is a build of ``frontend/`` (one of the npm workspace's frontends), which renders the shared library's
own tokens and components (``system/libs/workspace_ui``) rather than copies of them, so the catalog cannot drift
from what the other apps draw. It wears the workspace's theme like any other page built on the tokens, so under a
theme it shows that theme's values and parts. The app stores nothing.

- Listen address: bind ``HOST`` and ``PORT``, which default to the loopback address and this app's assigned port
  but honor ``STYLE_GUIDE_APP_HOST`` and ``STYLE_GUIDE_APP_PORT``, so a preview can boot on a spare port beside
  the live one.

This is a synchronous Flask app served by the threaded Werkzeug server. The app owns its own browser origin (the
forwarder routes ``http://style-guide.<workspace-host>/`` straight to this port), so root-absolute URLs work
unmodified.
"""

import os
import re
from pathlib import Path
from typing import Final

from flask import Flask
from flask import Response
from flask import abort
from flask import send_file
from flask import send_from_directory
from flask.typing import ResponseReturnValue
from loguru import logger
from werkzeug.serving import run_simple

from app_manifest.content_security_policy import attach_document_policy
from app_schema.api_schema import DEFAULT_API_SCHEMA_PATH
from app_schema.api_schema import RouteModels
from app_schema.api_schema import RouteTable
from app_schema.api_schema import SchemalessRoute
from app_schema.api_schema import register_api_schema_route
from app_schema.routes import model_response
from imbue.imbue_common.logging import setup_logging
from style_guide_app.data_types import HealthResponse
from workspace_themes.flask_routes import THEME_STATIC_ROUTE_TABLE
from workspace_themes.flask_routes import register_workspace_theme_route

HOST: Final[str] = os.environ.get("STYLE_GUIDE_APP_HOST", "127.0.0.1")
PORT: Final[int] = int(os.environ.get("STYLE_GUIDE_APP_PORT", "8060"))

APP_DISPLAY_NAME: Final[str] = "Style Guide"
APP_DESCRIPTION: Final[str] = (
    "Every colour, type role and shared control of the workspace's design system, drawn live from the library "
    "the other apps use."
)
STATIC_DIR: Final[Path] = Path(__file__).parent / "static"
# A bundle file Vite named from its content, which may be cached for good; the rest is checked on every load.
_HASHED_ASSET: Final[re.Pattern[str]] = re.compile(r"-[A-Za-z0-9_-]{8}\.(js|css)$")
_A_YEAR_IN_SECONDS: Final[int] = 365 * 24 * 60 * 60
PAGE_DOCUMENT_FILENAME: Final[str] = "index.html"
HEALTH_PATH: Final[str] = "/api/health"
PAGE_KIT_SAMPLE_PATH: Final[str] = "/page-kit-sample"
# The plain-HTML page the catalog frames to show the page kit, shipped beside this module.
PAGE_KIT_SAMPLE_FILE: Final[Path] = Path(__file__).parent / "page_kit_sample.html"
# The page kit (docs/system/blueprint/workspace-themes/, section 5.3), which the shell's build writes and every app
# serves from its own origin. Relative to the repo root, where the supervised app runs.
SHELL_STATIC_MODULES_DIR: Final[Path] = Path("system/apps/system_interface/imbue/system_interface/static/_static")
PAGE_KIT_FILES: Final[dict[str, str]] = {
    "workspace_theme.js": "text/javascript",
    "workspace_theme.css": "text/css",
}

HTTP_NOT_FOUND: Final[int] = 404

_NOT_BUILT_PAGE: Final[str] = (
    '<!doctype html><html><head><meta charset="utf-8"><title>Style Guide</title></head>'
    "<body><p>The Style Guide has not been built yet (run <code>npm run build</code> in <code>system/</code>).</p>"
    "</body></html>"
)

ROUTE_TABLE: RouteTable = {
    ("GET", "/"): SchemalessRoute(reason="the style guide page"),
    ("GET", "/assets/{filename}"): SchemalessRoute(reason="one file of the page's built bundle"),
    ("GET", HEALTH_PATH): RouteModels(response_model=HealthResponse),
    ("GET", PAGE_KIT_SAMPLE_PATH): SchemalessRoute(reason="a plain-HTML page wearing the page kit, which the catalog frames"),
    ("GET", "/_static/{filename}"): SchemalessRoute(reason="one file of the page kit the shell builds"),
    **THEME_STATIC_ROUTE_TABLE,
}


def create_app(static_directory: Path, shell_static_directory: Path = SHELL_STATIC_MODULES_DIR) -> Flask:
    app = Flask("style_guide_app", static_folder=None)
    register_api_schema_route(
        app, DEFAULT_API_SCHEMA_PATH, title=APP_DISPLAY_NAME, description=APP_DESCRIPTION, route_table=ROUTE_TABLE
    )
    attach_document_policy(app, None)
    # The workspace's themes, from this app's own origin, so the page can wear the one the shell names.
    register_workspace_theme_route(app)
    static_directory = static_directory.resolve()

    @app.get("/")
    def page() -> ResponseReturnValue:
        """The style guide page."""
        document = static_directory / PAGE_DOCUMENT_FILENAME
        if not document.is_file():
            return Response(_NOT_BUILT_PAGE, mimetype="text/html", headers={"Cache-Control": "no-store"})
        return Response(
            document.read_text(encoding="utf-8"), mimetype="text/html", headers={"Cache-Control": "no-store"}
        )

    @app.get("/assets/<path:filename>")
    def asset(filename: str) -> ResponseReturnValue:
        """One file of the page's built bundle, by name; one named from its content is cached for good."""
        is_hashed = _HASHED_ASSET.search(filename) is not None
        response = send_from_directory(
            (static_directory / "assets").absolute(), filename, max_age=_A_YEAR_IN_SECONDS if is_hashed else None
        )
        if not is_hashed:
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get(PAGE_KIT_SAMPLE_PATH)
    def page_kit_sample() -> ResponseReturnValue:
        """A page built as plain HTML on the page kit, which the catalog frames to show what the kit gives one."""
        return Response(
            PAGE_KIT_SAMPLE_FILE.read_text(encoding="utf-8"), mimetype="text/html", headers={"Cache-Control": "no-cache"}
        )

    @app.get("/_static/<filename>")
    def page_kit_file(filename: str) -> ResponseReturnValue:
        """The page kit's script or stylesheet, as the shell's build wrote it."""
        mimetype = PAGE_KIT_FILES.get(filename)
        path = shell_static_directory / filename
        if mimetype is None or not path.is_file():
            abort(HTTP_NOT_FOUND)
        response = send_file(path.absolute(), mimetype=mimetype)
        response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get(HEALTH_PATH)
    def health() -> ResponseReturnValue:
        """Whether the app is up, and whether its page has been built."""
        return model_response(
            HealthResponse(status="ok", is_frontend_built=(static_directory / PAGE_DOCUMENT_FILENAME).is_file())
        )

    return app


def main() -> None:
    setup_logging(level=os.environ.get("STYLE_GUIDE_APP_LOG_LEVEL", "INFO"))
    app = create_app(STATIC_DIR)
    logger.info("Serving the Style Guide at http://{}:{}/", HOST, PORT)
    run_simple(HOST, PORT, app, threaded=True, use_reloader=False, use_debugger=False)


if __name__ == "__main__":
    main()
