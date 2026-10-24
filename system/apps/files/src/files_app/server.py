"""The Files server: the workspace's files and the files shared from the user's computers, answered the same way.

- ``/<path>`` is the workspace's own file or folder (``local_files``), ``/__remote/<device>/<path>`` one read live from
  the computer it is on (``device_files``). Either way a folder or a file's ``?view`` / ``?edit`` page is the
  frontend's ``index.html`` with the path's page data filled in, ``?json`` is that data alone, a file's plain GET is
  its bytes (``?download`` to save them under the file's name), ``?zip`` a folder as a zip, and PUT, MKCOL, MOVE, and
  DELETE change it.
- ``/__files/assets/`` is the frontend's build, ``/__files/api/connections`` what the user has shared with the
  workspace from their computers (``connections_cache``).
- ``/__files/api/schema`` is the OpenAPI document of these routes, built from the url map (``app_schema``); the
  manifest's ``api_schema_path``, which the visiting-agent primer links.
"""

import base64
import json
import re
import urllib.parse
from collections.abc import Iterator
from pathlib import Path
from pathlib import PurePosixPath
from typing import Final

import httpx
from flask import Flask
from flask import Response
from flask import request
from flask import send_from_directory
from loguru import logger

from app_schema.api_schema import RouteModels
from app_schema.api_schema import RouteTable
from app_schema.api_schema import SchemalessRoute
from app_schema.api_schema import register_api_schema_route
from app_schema.routes import error_response
from app_schema.routes import model_response
from files_app.connections import SYNCED_FOLDERS
from files_app.connections import Connections
from files_app.connections_cache import ConnectionsCache
from files_app.device_files import DeviceFiles
from files_app.gateway import Gateway
from files_app.local_files import LocalFiles
from files_app.local_files import content_disposition
from files_app.sources import FileSource
from files_app.sources import FilesError
from files_app.sources import FolderStamps
from files_app.sources import PageData

API_PREFIX: Final[str] = "/__files/api"
APP_DISPLAY_NAME: Final[str] = "Files"
APP_DESCRIPTION: Final[str] = (
    "The workspace's file browser: the files here and on the person's connected computers, to open, upload and share."
)
ASSETS_PREFIX: Final[str] = "/__files/assets"
REMOTE_PREFIX: Final[str] = "/__remote"

_METHODS: Final[list[str]] = ["GET", "HEAD", "PUT", "DELETE", "MKCOL", "MOVE"]
_CHUNK: Final[int] = 64 * 1024
# A name vite gives a build file from its content, so the file at it never changes.
_HASHED_ASSET: Final[re.Pattern[str]] = re.compile(r"-[A-Za-z0-9_-]{8}\.(js|css)$")
_A_YEAR_IN_SECONDS: Final[int] = 365 * 24 * 60 * 60
_NO_MOVE_TO_COMPUTER: Final[str] = "Moving isn't supported on files shared from your computer."
_NO_GATEWAY: Final[str] = "This workspace isn't connected to Imbue Studio, so it can't reach your computers."
# Queries that ask for data or a download rather than a page, so a failure is answered in plain words.
_NON_PAGE_QUERIES: Final[frozenset[str]] = frozenset({"json", "zip", "download"})

# What every route exchanges, which the API document publishes. The connections listing is the one JSON route; a
# path's routes serve files of any kind (its page, its bytes, a zip, and ``?json`` the page data its view describes),
# so they are declared schemaless with what they answer.
_WORKSPACE_PATHS: Final[tuple[str, ...]] = ("/", "/{path}")
_REMOTE_PATHS: Final[tuple[str, ...]] = (f"{REMOTE_PREFIX}/{{device}}/", f"{REMOTE_PREFIX}/{{device}}/{{path}}")
_FILE_VERBS: Final[dict[str, str]] = {
    "GET": (
        "a folder's page or a file's bytes; ``?json`` the path's page data, ``?download`` a file's bytes as an "
        "attachment, ``?zip`` a folder as a zip"
    ),
    "PUT": "the file's new bytes, as the body; answers no body",
    "DELETE": "removes the path; answers no body",
}
ROUTE_TABLE: RouteTable = {
    ("GET", f"{API_PREFIX}/connections"): RouteModels(response_model=Connections),
    ("GET", f"{API_PREFIX}/stamps"): RouteModels(response_model=FolderStamps),
    ("GET", f"{ASSETS_PREFIX}/{{name}}"): SchemalessRoute(reason="a file of the frontend's build"),
    **{
        (method, path): SchemalessRoute(reason=reason)
        for path in _WORKSPACE_PATHS
        for method, reason in _FILE_VERBS.items()
    },
    **{
        (method, path): SchemalessRoute(reason=f"{reason}, read live from the computer the path is on")
        for path in _REMOTE_PATHS
        for method, reason in _FILE_VERBS.items()
    },
}


def _text(message: str, status: int) -> Response:
    return Response(message, status=status, mimetype="text/plain")


def _request_body() -> Iterator[bytes]:
    if request.content_length in (None, 0) and "chunked" not in request.headers.get("Transfer-Encoding", ""):
        return iter(())
    stream = request.stream
    return iter(lambda: stream.read(_CHUNK), b"")


def _requested_path() -> str:
    """The path as the browser sent it, decoded: the server's own decoding may have merged or dropped segments."""
    return urllib.parse.unquote(urllib.parse.urlsplit(request.environ.get("REQUEST_URI", request.path)).path)


def create_app(
    gateway: Gateway | None,
    static_directory: Path,
    agent_id: str,
    root: Path = Path("/"),
    synced_root: Path = SYNCED_FOLDERS,
) -> Flask:
    app = Flask(__name__, static_folder=None)
    register_api_schema_route(
        app, f"{API_PREFIX}/schema", title=APP_DISPLAY_NAME, description=APP_DESCRIPTION, route_table=ROUTE_TABLE
    )
    # Absolute, as Flask would read a relative one from its own package rather than the working directory.
    static_directory = static_directory.resolve()
    local = LocalFiles(root)
    # Whether each device's file server is reached under its id, as the last listing of the shares found.
    device_paths: dict[str, bool] = {}
    cache = ConnectionsCache(gateway, agent_id, synced_root)

    def page(data: PageData) -> Response:
        encoded = base64.b64encode(json.dumps(data.model_dump()).encode()).decode()
        html = (static_directory / "index.html").read_text().replace("__INDEX_DATA__", encoded)
        response = Response(html, mimetype="text/html")
        response.headers["Cache-Control"] = "no-cache"
        return response

    def failure(error: FilesError) -> Response:
        """The error in plain words; a page asked for that isn't there is Files' own "not found" page."""
        is_page_asked = "text/html" in request.headers.get("Accept", "") and not _NON_PAGE_QUERIES & request.args.keys()
        if error.status == 404 and is_page_asked:
            response = send_from_directory(static_directory, "404.html", max_age=None)
            response.status_code = 404
            return response
        return _text(error.message, error.status)

    def answer(source: FileSource, prefix: str, path: str) -> Response:
        # A gateway client drops dot segments, so `..` would climb out of a computer's file server to the gateway's
        # own API; nothing is reached that way in the workspace either.
        if any(segment in (".", "..") for segment in path.split("/")):
            return _text(f"{path} isn't a path to a file or folder.", 400)
        query = request.args
        try:
            if request.method in ("GET", "HEAD"):
                if "zip" in query:
                    return source.zip(path)
                if "json" in query:
                    return Response(source.describe(path).model_dump_json(), mimetype="application/json")
                if "view" in query or "edit" in query or path.endswith("/"):
                    return page(source.describe(path))
                read = source.read(path, request.headers)
                if read is None:
                    return page(source.describe(path))
                # Only a file's bytes become a download: a computer's refusal stays a page the frontend can read. A
                # 304 carries it too, since the browser takes its headers over onto the copy it saved before.
                if "download" in query and (200 <= read.status_code < 300 or read.status_code == 304):
                    read.headers["Content-Disposition"] = content_disposition("attachment", PurePosixPath(path).name)
                return read
            if request.method == "PUT":
                source.write(path, _request_body(), request.content_length)
                return _text("", 201)
            if request.method == "MKCOL":
                source.make_folder(path)
                return _text("", 201)
            if request.method == "DELETE":
                source.delete(path)
                return _text("", 204)
            destination = urllib.parse.unquote(urllib.parse.urlsplit(request.headers.get("Destination", "")).path)
            if prefix == "" and destination.startswith(f"{REMOTE_PREFIX}/"):
                return _text(_NO_MOVE_TO_COMPUTER, 501)
            source.move(path, destination[len(prefix) :])
            return _text("", 201)
        except FilesError as error:
            return failure(error)
        except PermissionError:
            return _text(f"You don't have permission to change or read {path}.", 403)
        except OSError as error:
            logger.warning("{} {} failed: {}", request.method, path, error)
            return _text(f"Couldn't do that with {path}: {error.strerror or error}", 500)

    @app.get(f"{ASSETS_PREFIX}/<path:name>")
    def asset(name: str) -> Response:
        """A file of the frontend's build; one named from its content is cached for good."""
        is_hashed = _HASHED_ASSET.search(name) is not None
        response = send_from_directory(static_directory, name, max_age=_A_YEAR_IN_SECONDS if is_hashed else None)
        if not is_hashed:
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get(f"{API_PREFIX}/connections")
    def connections() -> Response:
        """The computers connected to this workspace and what each shares with it.

        Each share says how it reaches the workspace (live, or a synced copy and how Imbue Studio says its sync
        stands). Pages ask every few seconds, so a read answers every ask for a couple of seconds after it. Nothing is
        listed while the workspace is not connected to Imbue Studio; a 502 says when Imbue Studio could not be read.
        """
        try:
            listed = cache.current()
        except (httpx.HTTPError, ValueError) as error:
            # ValueError: an answer from the gateway that is not the JSON it should be.
            logger.warning("Couldn't read the shares through the gateway: {}", error)
            return error_response(f"Couldn't read your shares: {error}", 502)
        device_paths.update({device.id: device.has_device_paths for device in listed.devices})
        return model_response(listed)

    @app.get(f"{API_PREFIX}/stamps")
    def stamps() -> Response:
        """Where each workspace folder named by a ``path`` param stands: a stamp that moves when anything in it is
        added, removed or renamed. A page asks every couple of seconds for the folders it shows and lists one again
        only when its stamp moved, so a file that lands in it shows at once; one stat a folder, nothing held."""
        return model_response(local.stamps(request.args.getlist("path")))

    @app.route(f"{REMOTE_PREFIX}/<device>/", defaults={"path": ""}, methods=_METHODS)
    @app.route(f"{REMOTE_PREFIX}/<device>/<path:path>", methods=_METHODS)
    def remote(device: str, path: str) -> Response:
        """A file or folder shared from one of the person's computers, read and changed live on it.

        The same verbs and answers as a workspace path (``?json`` the page data: a folder's entries, each with
        ``name``, ``is_folder``, ``is_symlink``, ``size`` and ``mtime``, or a file's ``is_text``); a 503 while the
        workspace is not connected to Imbue Studio, and moving onto or off a computer is not supported.
        """
        if gateway is None:
            return _text(_NO_GATEWAY, 503)
        prefix = f"{REMOTE_PREFIX}/{device}"
        source = DeviceFiles(gateway, device, device_paths.get(device))
        try:
            return answer(source, prefix, _requested_path()[len(prefix) :])
        finally:
            if source.known_device_paths is not None:
                device_paths[device] = source.known_device_paths

    @app.route("/", defaults={"path": ""}, methods=_METHODS)
    @app.route("/<path:path>", methods=_METHODS)
    def workspace(path: str) -> Response:
        """A file or folder of the workspace.

        GET answers a folder's page (or ``?json`` its page data: ``{"kind": "folder", "entries": [...]}``, each entry
        with ``name``, ``is_folder``, ``is_symlink``, ``size`` and ``mtime``), a file's bytes (``?view`` or ``?edit``
        its page, ``?json`` ``{"kind": "file", "is_text": ...}``), or ``?zip`` a folder as a zip. PUT writes a file
        from the body, MKCOL makes a folder, MOVE (to the ``Destination`` header) renames, DELETE removes. A refusal
        is plain words under its status.
        """
        return answer(local, "", _requested_path())

    return app
