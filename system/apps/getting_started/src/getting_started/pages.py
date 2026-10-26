"""The app's routes on its own origin: the page, the health probe, the template catalog, and the contract module.

The page is the frontend's built document (``static/index.html``); its bundle is served under ``/assets``. The
catalog route answers the freshest copy the store holds, with each drawing resolved to a URL (``catalog`` is null
when no catalog URL is configured, and a 503 says nothing could be loaded). The contract module the page speaks to
the shell with is the shell's build output, served from this origin (desktop-interface contracts.md section 7).
"""

from pathlib import Path
from typing import Final
from typing import assert_never

from flask import Blueprint
from flask import Response
from flask import send_file
from flask import send_from_directory
from flask.typing import ResponseReturnValue
from pydantic import Field
from werkzeug.exceptions import NotFound
from workspace_themes.flask_routes import THEME_STATIC_ROUTE_TABLE
from workspace_themes.flask_routes import register_workspace_theme_route

from app_manifest.content_security_policy import attach_document_policy
from app_manifest.content_security_policy import url_origin
from app_manifest.registry import APP_CONTRACT_ROUTE
from app_schema.api_schema import DEFAULT_API_SCHEMA_PATH
from app_schema.api_schema import RouteModels
from app_schema.api_schema import RouteTable
from app_schema.api_schema import SchemalessRoute
from app_schema.api_schema import register_api_schema_route
from app_schema.routes import error_response
from app_schema.routes import model_response
from getting_started.template_catalog import CatalogDocument
from getting_started.template_catalog import TemplateCatalogAvailability
from getting_started.template_catalog import TemplateCatalogStore
from getting_started.template_catalog import catalog_wire
from imbue.imbue_common.frozen_model import FrozenModel

BLUEPRINT_NAME: Final[str] = "getting_started_pages"
HEALTH_PATH: Final[str] = "/api/health"
APP_DISPLAY_NAME: Final[str] = "Getting Started"
APP_DESCRIPTION: Final[str] = "The ways into the workspace: things to start, and the published templates to adapt."
TEMPLATES_CATALOG_PATH: Final[str] = "/api/templates-catalog"
PAGE_DOCUMENT_FILENAME: Final[str] = "index.html"

HTTP_NOT_FOUND: Final[int] = 404
HTTP_SERVICE_UNAVAILABLE: Final[int] = 503

_TEMPLATES_UNAVAILABLE_DETAIL: Final[str] = "failed to load templates"

# Served in place of the page while the frontend has not been built (``static/`` is build output).
_NOT_BUILT_PAGE: Final[str] = (
    '<!doctype html><html><head><meta charset="utf-8"><title>Getting Started</title></head>'
    "<body><p>The Getting Started page has not been built yet (run <code>npm run build</code> in <code>system/</code>).</p>"
    "</body></html>"
)


class HealthResponse(FrozenModel):
    """What the health probe answers."""

    status: str = Field(description="Always 'ok'")
    is_frontend_built: bool = Field(description="Whether the page's bundle has been built, so the page can be served")


class TemplatesCatalogResponse(FrozenModel):
    """What the catalog route answers: the catalog as last fetched, and how current it is."""

    catalog: CatalogDocument | None = Field(description="The catalog; null when no catalog URL is configured")
    is_stale: bool = Field(description="Whether the catalog is the last good copy rather than a fresh fetch")


# What every route exchanges, which the app's API document publishes and every parse and answer is checked against.
ROUTE_TABLE: RouteTable = {
    ("GET", "/"): SchemalessRoute(reason="the Getting Started page"),
    ("GET", "/assets/{filename}"): SchemalessRoute(reason="one file of the page's built bundle"),
    ("GET", HEALTH_PATH): RouteModels(response_model=HealthResponse),
    ("GET", TEMPLATES_CATALOG_PATH): RouteModels(response_model=TemplatesCatalogResponse),
    ("GET", APP_CONTRACT_ROUTE): SchemalessRoute(reason="the app contract module, a JavaScript file"),
    **THEME_STATIC_ROUTE_TABLE,
}


def build_pages_blueprint(static_directory: Path, catalog: TemplateCatalogStore, contract_path: Path) -> Blueprint:
    blueprint = Blueprint(BLUEPRINT_NAME, __name__)
    register_api_schema_route(
        blueprint, DEFAULT_API_SCHEMA_PATH, title=APP_DISPLAY_NAME, description=APP_DESCRIPTION, route_table=ROUTE_TABLE
    )
    register_workspace_theme_route(blueprint)
    # The template drawings live beside the catalog (catalog/README.md), so the page admits images from the
    # catalog's origin besides its own; with no catalog there is no templates section and nothing to admit.
    image_sources = "'self' data: blob:"
    if catalog.catalog_url != "":
        image_sources = f"{image_sources} {url_origin(catalog.catalog_url)}"
    attach_document_policy(blueprint, {"img-src": image_sources})

    @blueprint.get("/")
    def page() -> ResponseReturnValue:
        """The Getting Started page: things to start, and the published templates to adapt."""
        document = static_directory / PAGE_DOCUMENT_FILENAME
        if not document.is_file():
            return Response(_NOT_BUILT_PAGE, mimetype="text/html", headers={"Cache-Control": "no-store"})
        # Read rather than streamed, so the document policy can admit the page's inline theme boot by its hash.
        return Response(document.read_text(), mimetype="text/html", headers={"Cache-Control": "no-store"})

    @blueprint.get("/assets/<path:filename>")
    def asset(filename: str) -> ResponseReturnValue:
        """One file of the page's built bundle, by name."""
        # Existence and safety are both left to ``send_from_directory``: the filename arrives with any ``..``
        # segments intact, and a missing asset is a plain 404.
        try:
            return send_from_directory((static_directory / "assets").absolute(), filename)
        except NotFound:
            return Response(status=HTTP_NOT_FOUND)

    @blueprint.get(HEALTH_PATH)
    def health() -> ResponseReturnValue:
        """Whether the app is up, and whether its page has been built."""
        return model_response(
            HealthResponse(status="ok", is_frontend_built=(static_directory / PAGE_DOCUMENT_FILENAME).is_file())
        )

    @blueprint.get(TEMPLATES_CATALOG_PATH)
    def templates_catalog() -> ResponseReturnValue:
        """The published template catalog as last fetched (null when none is configured; 503 when none could be loaded)."""
        reading = catalog.read()
        match reading.availability:
            case TemplateCatalogAvailability.DISABLED:
                return model_response(TemplatesCatalogResponse(catalog=None, is_stale=False))
            case TemplateCatalogAvailability.UNAVAILABLE:
                return error_response(_TEMPLATES_UNAVAILABLE_DETAIL, HTTP_SERVICE_UNAVAILABLE)
            case TemplateCatalogAvailability.FRESH | TemplateCatalogAvailability.STALE:
                assert reading.catalog is not None, "a fresh or stale reading carries its catalog"
                return model_response(
                    TemplatesCatalogResponse(
                        catalog=catalog_wire(reading.catalog, catalog.catalog_url),
                        is_stale=reading.availability is TemplateCatalogAvailability.STALE,
                    )
                )
            case _ as unreachable:
                assert_never(unreachable)

    @blueprint.get(APP_CONTRACT_ROUTE)
    def app_contract() -> ResponseReturnValue:
        """The app contract module: how the page talks to the shell framing it."""
        if not contract_path.is_file():
            return error_response(f"the workspace shell's frontend is not built: {contract_path} is missing", HTTP_NOT_FOUND)
        # Flask resolves a relative path against the app's own directory, not the cwd the path names.
        return send_file(contract_path.absolute(), mimetype="text/javascript")

    return blueprint
