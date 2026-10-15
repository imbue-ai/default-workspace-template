"""The app's routes on its own origin: the page, the health probe, the template catalog, and the contract module.

The page is the frontend's built document (``static/index.html``); its bundle is served under ``/assets``. The
catalog route answers the freshest copy the store holds, with each drawing resolved to a URL (``catalog`` is null
when no catalog URL is configured, and a 503 says nothing could be loaded). The contract module the page speaks to
the shell with is the shell's build output, served from this origin (desktop-interface contracts.md section 7).
"""

import json
from pathlib import Path
from typing import Final
from typing import assert_never

from flask import Blueprint
from flask import Response
from flask import jsonify
from flask import request
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
RETRY_QUERY_PARAMETER: Final[str] = "retry"
PAGE_DOCUMENT_FILENAME: Final[str] = "index.html"

HTTP_NOT_FOUND: Final[int] = 404
HTTP_SERVICE_UNAVAILABLE: Final[int] = 503

_TEMPLATES_UNAVAILABLE_DETAIL: Final[str] = "failed to load templates"

# How often the not-built page asks whether the page has been built since, so a rebuild by any route brings it back.
NOT_BUILT_POLL_SECONDS: Final[int] = 10

# What an agent asked to rebuild the page is told.
_REBUILD_REQUEST: Final[str] = (
    "The Getting Started page isn't available: its built files are missing. Please rebuild it (npm run build in "
    "system/) so it comes back."
)

# Served in place of the page while the frontend has not been built (``static/`` is build output): it checks again
# on its own, offers a Reload, and, once a shell has greeted it, an agent to rebuild it; the build command is behind
# "See details". The contract module it asks the shell with is the shell's, served from this origin; when it cannot
# be loaded (the shell's frontend is not built either), or no shell greets the page, the agent button stays hidden.
_NOT_BUILT_PAGE: Final[str] = (
    """<!doctype html>
<html><head><meta charset="utf-8"><title>Getting Started</title>
<style>body{font-family:system-ui,sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;
margin:0;color:#334155}main{max-width:32rem;padding:2rem}p{line-height:1.5}button{font:inherit;padding:0.4rem 1rem;
border:1px solid #cbd5e1;border-radius:6px;background:#fff;color:inherit;cursor:pointer;margin-right:0.5rem}
button:hover{background:#f1f5f9}button[hidden]{display:none}details{margin-top:1.5rem;font-size:0.85em;color:#64748b}
summary{cursor:pointer}</style></head>
<body><main>
<p>The Getting Started page isn't available right now. It comes back on its own once it is rebuilt.</p>
<p><button type="button" id="reload">Reload</button><button type="button" id="ask-agent" hidden>Ask an agent to
rebuild it</button></p>
<details><summary>See details</summary><p>Its built files are missing. They are rebuilt with
<code>npm run build</code> in <code>system/</code>.</p></details>
</main>
<script type="module">
  document.getElementById("reload").addEventListener("click", () => window.location.reload());
  setInterval(() => {
    fetch("__HEALTH_PATH__", { cache: "no-store" })
      .then((response) => response.json())
      .then((health) => {
        if (health.is_frontend_built === true) window.location.reload();
      })
      // The app restarting is the expected way for this to fail, and a moment the page may be arriving: ask again.
      .catch(() => {});
  }, __POLL_SECONDS__ * 1000);
  if (window.parent !== window) {
    import("__CONTRACT_PATH__")
      .then(({ connectToShell }) => {
        const ask = document.getElementById("ask-agent");
        // Only a shell can start a chat, so the button waits for one to greet the page.
        const shell = connectToShell({ onHandshake: () => (ask.hidden = false) });
        ask.addEventListener("click", () => shell.startWithText(__REBUILD_REQUEST__));
      })
      .catch((error) => console.warn("[getting-started] could not load the shell contract", error));
  }
</script>
</body></html>
""".replace("__HEALTH_PATH__", HEALTH_PATH.lstrip("/"))
    .replace("__POLL_SECONDS__", str(NOT_BUILT_POLL_SECONDS))
    .replace("__CONTRACT_PATH__", APP_CONTRACT_ROUTE)
    .replace("__REBUILD_REQUEST__", json.dumps(_REBUILD_REQUEST))
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
        # A person's Try again asks for a fresh fetch, past the hold the store keeps after a failed one.
        reading = catalog.read(is_retry=request.args.get(RETRY_QUERY_PARAMETER) == "1")
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
