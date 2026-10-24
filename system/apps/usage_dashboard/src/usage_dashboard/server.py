"""The Usage app: the dashboard page at ``/`` and the API it reads, which is the usage collector's.

The collector (``system/services/usage_collector``) listens on 127.0.0.1 only, and the page lives on this app's
own origin, so this server forwards the page's reads to it: ``/api/usage/totals``, ``/api/usage/latency``,
``/api/usage/calls``, ``/api/usage/turns`` and ``/api/usage/limits`` answer what the collector's ``/v1/...`` routes answer, ``/api/usage/stream`` relays its
server-sent events, and ``/api/usage/health`` its health. Who may read them is the share gateway's grants on this
app, as for any app: the collector listens on 127.0.0.1 only, so this server is the one way to it from outside
the workspace. A collector that cannot be reached is answered with 503, which the page shows as a banner over the
last numbers it had.

The app stores nothing. Besides the collector it reads the names the page shows a source's usage under: an
app's display name from the app registry, a chat's title from the chat app, and a routine's title from its
definition file (``source_names.py``).

- Listen address: bind ``HOST`` and ``PORT``, which default to the loopback address and this app's assigned port
  but honor ``USAGE_DASHBOARD_HOST`` and ``USAGE_DASHBOARD_PORT``, so a preview can boot on a spare port beside
  the live one. ``USAGE_DASHBOARD_COLLECTOR_URL`` points it at another collector.

This is a synchronous Flask app served by the threaded Werkzeug server; each open page holds one thread for its
stream.
"""

import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import httpx
from flask import Flask
from flask import Response
from flask import request
from flask import send_from_directory
from flask.typing import ResponseReturnValue
from loguru import logger
from werkzeug.serving import run_simple

from app_manifest.content_security_policy import attach_document_policy
from app_manifest.registry import RegistryReadError
from app_manifest.registry import read_registry
from app_manifest.registry_location import registry_path
from app_schema.api_schema import DEFAULT_API_SCHEMA_PATH
from app_schema.api_schema import RouteModels
from app_schema.api_schema import RouteTable
from app_schema.api_schema import SchemalessRoute
from app_schema.api_schema import register_api_schema_route
from app_schema.routes import error_response
from app_schema.routes import model_response
from imbue.imbue_common.logging import setup_logging
from routines.paths import paths_from_environment
from usage_dashboard.data_types import AppNamesResponse
from usage_dashboard.data_types import HealthResponse
from usage_dashboard.data_types import SourceNamesResponse
from usage_dashboard.source_names import chat_titles
from usage_dashboard.source_names import routine_titles

HOST: Final[str] = os.environ.get("USAGE_DASHBOARD_HOST", "127.0.0.1")
PORT: Final[int] = int(os.environ.get("USAGE_DASHBOARD_PORT", "8060"))
COLLECTOR_URL: Final[str] = os.environ.get("USAGE_DASHBOARD_COLLECTOR_URL", "http://127.0.0.1:8795")

APP_DISPLAY_NAME: Final[str] = "Usage"
APP_DESCRIPTION: Final[str] = (
    "The workspace's model usage: spend, tokens, calls and latency by model, source and billing mode."
)
STATIC_DIR: Final[Path] = Path(__file__).parent / "static"
# A bundle file Vite named from its content, which may be cached for good; the rest is checked on every load.
_HASHED_ASSET: Final[re.Pattern[str]] = re.compile(r"-[A-Za-z0-9_-]{8}\.(js|css)$")
_A_YEAR_IN_SECONDS: Final[int] = 365 * 24 * 60 * 60
PAGE_DOCUMENT_FILENAME: Final[str] = "index.html"
HEALTH_PATH: Final[str] = "/api/health"
# The collector routes the page reads, by the name it reads them under.
FORWARDED_READS: Final[dict[str, str]] = {
    "totals": "/v1/totals",
    "latency": "/v1/latency",
    "calls": "/v1/calls",
    "turns": "/v1/turns",
    "limits": "/v1/limits",
    "health": "/health",
}
_READ_TIMEOUT: Final[httpx.Timeout] = httpx.Timeout(10.0)
# The collector sends a keepalive every 15 seconds, so a stream silent for a minute has gone.
_STREAM_TIMEOUT: Final[httpx.Timeout] = httpx.Timeout(10.0, read=60.0)

HTTP_SERVICE_UNAVAILABLE: Final[int] = 503
HTTP_NOT_FOUND: Final[int] = 404
HTTP_OK: Final[int] = 200

_NOT_BUILT_PAGE: Final[str] = (
    '<!doctype html><html><head><meta charset="utf-8"><title>Usage</title></head>'
    "<body><p>The Usage app has not been built yet (run <code>npm run build</code> in <code>system/</code>).</p>"
    "</body></html>"
)

ROUTE_TABLE: RouteTable = {
    ("GET", "/"): SchemalessRoute(reason="the Usage home page"),
    ("GET", "/all"): SchemalessRoute(reason="the detail page for all usage: the same document, which reads its path"),
    ("GET", "/p/{item}"): SchemalessRoute(
        reason="the detail page for one provider's usage: the same document, which reads its path"
    ),
    ("GET", "/assets/{filename}"): SchemalessRoute(reason="one file of the page's built bundle"),
    ("GET", HEALTH_PATH): RouteModels(response_model=HealthResponse),
    ("GET", "/api/apps"): RouteModels(response_model=AppNamesResponse),
    ("GET", "/api/source-names"): RouteModels(response_model=SourceNamesResponse),
    ("GET", "/api/usage/stream"): SchemalessRoute(reason="the usage collector's stream of new calls, relayed"),
    ("GET", "/api/usage/{name}"): SchemalessRoute(
        reason="the usage collector's answer for totals, latency, calls, turns, limits or health, passed through unchanged"
    ),
}


def _collector_unreachable(e: httpx.HTTPError) -> ResponseReturnValue:
    logger.warning("Could not reach the usage collector: {}", e)
    return error_response("The usage collector isn't responding.", HTTP_SERVICE_UNAVAILABLE)


def create_app(
    static_directory: Path, collector: httpx.Client, apps_file: Path, http: httpx.Client, routines_dir: Path
) -> Flask:
    """The app, reading the collector through ``collector`` (its base URL set) and display names from ``apps_file``.

    ``http`` asks the chat app for its chat titles; ``routines_dir`` holds the routine definitions.
    """
    app = Flask("usage_dashboard", static_folder=None)
    register_api_schema_route(
        app, DEFAULT_API_SCHEMA_PATH, title=APP_DISPLAY_NAME, description=APP_DESCRIPTION, route_table=ROUTE_TABLE
    )
    attach_document_policy(app, None)
    static_directory = static_directory.resolve()

    @app.get("/")
    @app.get("/all")
    @app.get("/p/<item>")
    def page(item: str | None = None) -> ResponseReturnValue:
        """The Usage page: the one document, which reads its path (the home page, all usage, or one provider's)."""
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

    @app.get(HEALTH_PATH)
    def health() -> ResponseReturnValue:
        """Whether the app is up, and whether its page has been built."""
        return model_response(
            HealthResponse(status="ok", is_frontend_built=(static_directory / PAGE_DOCUMENT_FILENAME).is_file())
        )

    @app.get("/api/apps")
    def app_names() -> ResponseReturnValue:
        """The display name of every registered app; empty when the registry cannot be read."""
        try:
            rows = read_registry(apps_file)
        except RegistryReadError as e:
            logger.warning("Could not read the app registry for display names: {}", e)
            rows = []
        names = {str(row.name): str(row.display_name) for row in rows if row.display_name is not None}
        return model_response(AppNamesResponse(display_names=names))

    @app.get("/api/source-names")
    def source_names() -> ResponseReturnValue:
        """The titles of the chats and routines the page names usage after."""
        return model_response(
            SourceNamesResponse(chats=chat_titles(apps_file, http), routines=routine_titles(routines_dir))
        )

    @app.get("/api/usage/stream")
    def stream() -> ResponseReturnValue:
        """The collector's server-sent events, relayed as they arrive."""
        upstream_request = collector.build_request("GET", "/v1/stream", timeout=_STREAM_TIMEOUT)
        try:
            upstream = collector.send(upstream_request, stream=True)
        except httpx.HTTPError as e:
            return _collector_unreachable(e)
        if upstream.status_code != HTTP_OK:
            body = upstream.read()
            upstream.close()
            return Response(body, status=upstream.status_code, mimetype="application/json")

        def relay() -> Iterator[bytes]:
            try:
                yield from upstream.iter_raw()
            except httpx.HTTPError as e:
                logger.info("The usage collector's stream ended: {}", e)
            finally:
                upstream.close()

        return Response(
            relay(),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/usage/<name>")
    def forwarded_read(name: str) -> ResponseReturnValue:
        """What the collector answers for ``name`` (totals, latency, calls or health), with the same query."""
        path = FORWARDED_READS.get(name)
        if path is None:
            return error_response(f"There is no usage read called {name!r}.", HTTP_NOT_FOUND)
        try:
            upstream = collector.get(path, params=list(request.args.items(multi=True)), timeout=_READ_TIMEOUT)
        except httpx.HTTPError as e:
            return _collector_unreachable(e)
        return Response(
            upstream.content,
            status=upstream.status_code,
            mimetype="application/json",
            headers={"Cache-Control": "no-store"},
        )

    return app


def main() -> None:
    setup_logging(level=os.environ.get("USAGE_DASHBOARD_LOG_LEVEL", "INFO"))
    collector = httpx.Client(base_url=COLLECTOR_URL)
    app = create_app(STATIC_DIR, collector, registry_path(), httpx.Client(), paths_from_environment().definitions_dir)
    logger.info("Serving the Usage app at http://{}:{}/ over the collector at {}", HOST, PORT, COLLECTOR_URL)
    run_simple(HOST, PORT, app, threaded=True, use_reloader=False, use_debugger=False)


if __name__ == "__main__":
    main()
