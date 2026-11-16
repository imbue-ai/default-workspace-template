"""The Markdown Editor: the page at ``/`` that opens a markdown file by ``?path=``, and the API it reads and saves
the file through.

The page is a build of ``frontend/`` (one of the npm workspace's frontends): Tiptap over ProseMirror, with tracked
suggestions and comments written into the file as CriticMarkup, so the agent that reads the file next sees them in
place. The server opens files under the workspace user's home (``files.WorkspaceFiles``), writes only markdown and
text files, refuses a save based on a version the file has since left, and keeps each version it saved over under
``data/.apps/markdown-editor/history/``.

- Listen address: bind ``HOST`` and ``PORT``, which default to the loopback address and this app's assigned port
  but honor ``MARKDOWN_EDITOR_APP_HOST`` and ``MARKDOWN_EDITOR_APP_PORT``.
- ``MARKDOWN_EDITOR_APP_ROOT`` (default ``/home/user``) is the folder files may be opened under,
  ``MARKDOWN_EDITOR_APP_SEARCH_ROOT`` (default ``/home/user/workspace``) where the start page looks for markdown
  files, and ``MARKDOWN_EDITOR_APP_DATA_DIR`` (default ``data/.apps/markdown-editor``, under the repo root the
  supervised app runs from) where the history is kept.

This is a synchronous Flask app served by the threaded Werkzeug server. The app owns its own browser origin (the
forwarder routes ``http://markdown-editor.<workspace-host>/`` straight to this port), so root-absolute URLs work
unmodified.
"""

import os
import re
from pathlib import Path
from typing import Final

from flask import Flask
from flask import Response
from flask import request
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
from app_schema.request_guard import install_request_guard
from app_schema.routes import error_response
from app_schema.routes import model_response
from app_schema.routes import parse_request_body
from imbue.imbue_common.logging import setup_logging
from markdown_editor_app.data_types import FileRecord
from markdown_editor_app.data_types import FileVersion
from markdown_editor_app.data_types import FoundFiles
from markdown_editor_app.data_types import HealthResponse
from markdown_editor_app.data_types import SaveRequest
from markdown_editor_app.errors import FileRefusedError
from markdown_editor_app.files import MarkdownFinder
from markdown_editor_app.files import WorkspaceFiles

HOST: Final[str] = os.environ.get("MARKDOWN_EDITOR_APP_HOST", "127.0.0.1")
PORT: Final[int] = int(os.environ.get("MARKDOWN_EDITOR_APP_PORT", "8070"))
ROOT: Final[Path] = Path(os.environ.get("MARKDOWN_EDITOR_APP_ROOT", "/home/user"))
SEARCH_ROOT: Final[Path] = Path(os.environ.get("MARKDOWN_EDITOR_APP_SEARCH_ROOT", "/home/user/workspace"))
DATA_DIR: Final[Path] = Path(os.environ.get("MARKDOWN_EDITOR_APP_DATA_DIR", "data/.apps/markdown-editor"))

APP_DISPLAY_NAME: Final[str] = "Markdown Editor"
APP_DESCRIPTION: Final[str] = (
    "Read, comment on and suggest edits to markdown files (agent plans, notes); suggestions and comments are "
    "saved inside the file as CriticMarkup."
)
STATIC_DIR: Final[Path] = Path(__file__).parent / "static"
ICON_PATH: Final[Path] = Path(__file__).resolve().parents[2] / "icon.svg"
# A bundle file Vite named from its content, which may be cached for good; the rest is checked on every load.
_HASHED_ASSET: Final[re.Pattern[str]] = re.compile(r"-[A-Za-z0-9_-]{8}\.(js|css)$")
_A_YEAR_IN_SECONDS: Final[int] = 365 * 24 * 60 * 60
PAGE_DOCUMENT_FILENAME: Final[str] = "index.html"
HEALTH_PATH: Final[str] = "/api/health"

HTTP_NOT_FOUND: Final[int] = 404

# A markdown file may show images from anywhere on the web; everything else stays on the app's own origin.
DOCUMENT_POLICY_OVERRIDES: Final[dict[str, str]] = {"img-src": "'self' data: blob: https:", "frame-src": "'none'"}

_NOT_BUILT_PAGE: Final[str] = (
    '<!doctype html><html><head><meta charset="utf-8"><title>Markdown Editor</title></head>'
    "<body><p>The Markdown Editor has not been built yet (run <code>npm run build</code> in <code>system/</code>)."
    "</p></body></html>"
)

ROUTE_TABLE: RouteTable = {
    ("GET", "/"): SchemalessRoute(reason="the editor page, which opens the file its ?path= names"),
    ("GET", "/assets/{filename}"): SchemalessRoute(reason="one file of the page's built bundle"),
    ("GET", "/icon.svg"): SchemalessRoute(reason="the app's icon, as the page's favicon"),
    ("GET", HEALTH_PATH): RouteModels(response_model=HealthResponse),
    ("GET", "/api/file"): RouteModels(response_model=FileRecord),
    ("PUT", "/api/file"): RouteModels(request_model=SaveRequest, response_model=FileVersion),
    ("GET", "/api/file/version"): RouteModels(response_model=FileVersion),
    ("GET", "/api/file/raw"): SchemalessRoute(reason="the file exactly as saved, as text"),
    ("GET", "/api/files"): RouteModels(response_model=FoundFiles),
}


def create_app(files: WorkspaceFiles, finder: MarkdownFinder, static_directory: Path) -> Flask:
    app = Flask("markdown_editor_app", static_folder=None)
    install_request_guard(app, exempt_rules=frozenset())
    register_api_schema_route(
        app, DEFAULT_API_SCHEMA_PATH, title=APP_DISPLAY_NAME, description=APP_DESCRIPTION, route_table=ROUTE_TABLE
    )
    attach_document_policy(app, DOCUMENT_POLICY_OVERRIDES)
    static_directory = static_directory.resolve()

    @app.after_request
    def no_sniffing(response: Response) -> Response:
        # A raw file is served as text, and must never be taken for a page however it reads.
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.errorhandler(FileRefusedError)
    def file_refused(error: FileRefusedError) -> ResponseReturnValue:
        return error_response(error.detail, error.status)

    @app.get("/")
    def page() -> ResponseReturnValue:
        """The editor page; ``?path=`` names the file to open, ``?mode=`` (suggest, edit, read) its mode."""
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

    @app.get("/icon.svg")
    def icon() -> ResponseReturnValue:
        """The app's icon, which the page shows as its favicon."""
        if not ICON_PATH.is_file():
            return error_response("The icon is missing.", HTTP_NOT_FOUND)
        return send_file(ICON_PATH, mimetype="image/svg+xml", max_age=0)

    @app.get(HEALTH_PATH)
    def health() -> ResponseReturnValue:
        """Whether the app is up, and whether its page has been built."""
        return model_response(
            HealthResponse(status="ok", is_frontend_built=(static_directory / PAGE_DOCUMENT_FILENAME).is_file())
        )

    @app.get("/api/file")
    def get_file() -> ResponseReturnValue:
        """One file's text and version (``?path=``), and whether the editor may save it."""
        return model_response(files.read(request.args.get("path", "")))

    @app.put("/api/file")
    def put_file() -> ResponseReturnValue:
        """Save a file; refused with 409 when it changed since the version the edit started from.

        A ``base_sha256`` of null saves regardless (the page's "keep mine" after a conflict).
        """
        save = parse_request_body(SaveRequest)
        return model_response(files.write(save.path, save.text, save.base_sha256))

    @app.get("/api/file/version")
    def get_version() -> ResponseReturnValue:
        """The file's current version (``?path=``), which the page polls to notice an outside change."""
        return model_response(files.version(request.args.get("path", "")))

    @app.get("/api/file/raw")
    def get_raw() -> ResponseReturnValue:
        """The file (``?path=``) exactly as saved; ``download=1`` saves it under its own name."""
        path = files.raw_path(request.args.get("path", ""))
        return send_file(
            path,
            mimetype="text/plain; charset=utf-8",
            as_attachment=request.args.get("download") == "1",
            download_name=path.name,
            max_age=0,
        )

    @app.get("/api/files")
    def list_files() -> ResponseReturnValue:
        """The markdown files in the workspace, most recently changed first."""
        return model_response(FoundFiles(root=files.shown(files.search_root), files=tuple(finder.find())))

    return app


def main() -> None:
    setup_logging(level=os.environ.get("MARKDOWN_EDITOR_APP_LOG_LEVEL", "INFO"))
    files = WorkspaceFiles(root=ROOT, search_root=SEARCH_ROOT, history_dir=DATA_DIR / "history")
    finder = MarkdownFinder(files)
    # Warm the start page's file list while the first window is still loading.
    finder.warm()
    app = create_app(files, finder, STATIC_DIR)
    logger.info("Serving the Markdown Editor at http://{}:{}/", HOST, PORT)
    run_simple(HOST, PORT, app, threaded=True, use_reloader=False, use_debugger=False)


if __name__ == "__main__":
    main()
