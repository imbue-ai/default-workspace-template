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
from flask import jsonify
from flask import send_file
from flask import send_from_directory
from flask.typing import ResponseReturnValue
from werkzeug.exceptions import NotFound

from app_manifest.registry import APP_CONTRACT_ROUTE
from getting_started.template_catalog import TemplateCatalogAvailability
from getting_started.template_catalog import TemplateCatalogStore
from getting_started.template_catalog import catalog_wire_json

BLUEPRINT_NAME: Final[str] = "getting_started_pages"
HEALTH_PATH: Final[str] = "/api/health"
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


def build_pages_blueprint(static_directory: Path, catalog: TemplateCatalogStore, contract_path: Path) -> Blueprint:
    blueprint = Blueprint(BLUEPRINT_NAME, __name__)

    @blueprint.get("/")
    def page() -> ResponseReturnValue:
        document = static_directory / PAGE_DOCUMENT_FILENAME
        if not document.is_file():
            return Response(_NOT_BUILT_PAGE, mimetype="text/html", headers={"Cache-Control": "no-store"})
        response = send_file(document.absolute(), mimetype="text/html")
        response.headers["Cache-Control"] = "no-store"
        return response

    @blueprint.get("/assets/<path:filename>")
    def asset(filename: str) -> ResponseReturnValue:
        # Existence and safety are both left to ``send_from_directory``: the filename arrives with any ``..``
        # segments intact, and a missing asset is a plain 404.
        try:
            return send_from_directory((static_directory / "assets").absolute(), filename)
        except NotFound:
            return Response(status=HTTP_NOT_FOUND)

    @blueprint.get(HEALTH_PATH)
    def health() -> ResponseReturnValue:
        return jsonify({"status": "ok", "is_frontend_built": (static_directory / PAGE_DOCUMENT_FILENAME).is_file()})

    @blueprint.get(TEMPLATES_CATALOG_PATH)
    def templates_catalog() -> ResponseReturnValue:
        reading = catalog.read()
        match reading.availability:
            case TemplateCatalogAvailability.DISABLED:
                return jsonify({"catalog": None, "is_stale": False})
            case TemplateCatalogAvailability.UNAVAILABLE:
                return jsonify({"detail": _TEMPLATES_UNAVAILABLE_DETAIL}), HTTP_SERVICE_UNAVAILABLE
            case TemplateCatalogAvailability.FRESH | TemplateCatalogAvailability.STALE:
                assert reading.catalog is not None, "a fresh or stale reading carries its catalog"
                return jsonify(
                    {
                        "catalog": catalog_wire_json(reading.catalog, catalog.catalog_url),
                        "is_stale": reading.availability is TemplateCatalogAvailability.STALE,
                    }
                )
            case _ as unreachable:
                assert_never(unreachable)

    @blueprint.get(APP_CONTRACT_ROUTE)
    def app_contract() -> ResponseReturnValue:
        if not contract_path.is_file():
            return (
                jsonify({"detail": f"the workspace shell's frontend is not built: {contract_path} is missing"}),
                HTTP_NOT_FOUND,
            )
        # Flask resolves a relative path against the app's own directory, not the cwd the path names.
        return send_file(contract_path.absolute(), mimetype="text/javascript")

    return blueprint
