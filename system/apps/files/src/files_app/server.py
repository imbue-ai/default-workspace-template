"""The Files server: the workspace's files and the files shared from the user's computers, answered the same way.

- ``/<path>`` is the workspace's own file or folder (``local_files``), ``/__remote/<device>/<path>`` one read live from
  the computer it is on (``device_files``). Either way a folder or a file's ``?view`` / ``?edit`` page is the
  frontend's ``index.html`` with the path's page data filled in, ``?json`` is that data alone, a file's plain GET is
  its bytes, ``?zip`` a folder as a zip, and PUT, MKCOL, MOVE, and DELETE change it.
- ``/__files/assets/`` is the frontend's build, ``/__files/api/connections`` what the user has shared with the
  workspace from their computers.
"""

import base64
import json
import re
import urllib.parse
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import httpx
from flask import Flask
from flask import Response
from flask import request
from flask import send_from_directory
from loguru import logger

from files_app.connections import SYNCED_FOLDERS
from files_app.connections import list_connections
from files_app.device_files import DeviceFiles
from files_app.gateway import Gateway
from files_app.local_files import LocalFiles
from files_app.sources import FileSource
from files_app.sources import FilesError
from files_app.sources import PageData

API_PREFIX: Final[str] = "/__files/api"
ASSETS_PREFIX: Final[str] = "/__files/assets"
REMOTE_PREFIX: Final[str] = "/__remote"

_METHODS: Final[list[str]] = ["GET", "HEAD", "PUT", "DELETE", "MKCOL", "MOVE"]
_CHUNK: Final[int] = 64 * 1024
# A name vite gives a build file from its content, so the file at it never changes.
_HASHED_ASSET: Final[re.Pattern[str]] = re.compile(r"-[A-Za-z0-9_-]{8}\.(js|css)$")
_A_YEAR_IN_SECONDS: Final[int] = 365 * 24 * 60 * 60
_NO_MOVE_TO_COMPUTER: Final[str] = "Moving isn't supported on files shared from your computer."


def _json(content: object, status: int = 200) -> Response:
    return Response(json.dumps(content), status=status, mimetype="application/json")


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
    root: Path = Path("/"),
    synced_root: Path = SYNCED_FOLDERS,
) -> Flask:
    app = Flask(__name__, static_folder=None)
    # Absolute, as Flask would read a relative one from its own package rather than the working directory.
    static_directory = static_directory.resolve()
    local = LocalFiles(root)
    # Whether each device's file server is reached under its id, as the last listing of the shares found.
    device_paths: dict[str, bool] = {}

    def page(data: PageData) -> Response:
        encoded = base64.b64encode(json.dumps(data.model_dump()).encode()).decode()
        html = (static_directory / "index.html").read_text().replace("__INDEX_DATA__", encoded)
        response = Response(html, mimetype="text/html")
        response.headers["Cache-Control"] = "no-cache"
        return response

    def failure(error: FilesError) -> Response:
        """The error in plain words; a page asked for that isn't there is Files' own "not found" page."""
        is_page_asked = "text/html" in request.headers.get("Accept", "") and "json" not in request.args
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
                    return _json(source.describe(path).model_dump())
                if "view" in query or "edit" in query or path.endswith("/"):
                    return page(source.describe(path))
                read = source.read(path, request.headers)
                return read if read is not None else page(source.describe(path))
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
        is_hashed = _HASHED_ASSET.search(name) is not None
        response = send_from_directory(static_directory, name, max_age=_A_YEAR_IN_SECONDS if is_hashed else None)
        if not is_hashed:
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get(f"{API_PREFIX}/connections")
    def connections() -> Response:
        if gateway is None:
            return _json({"devices": [], "shares": []})
        try:
            listed = list_connections(gateway, synced_root)
        except (httpx.HTTPError, ValueError) as error:
            # ValueError: an answer from the gateway that is not the JSON it should be.
            logger.warning("Couldn't read the shares through the gateway: {}", error)
            return _text(f"Couldn't read your shares: {error}", 502)
        device_paths.update({device.id: device.has_device_paths for device in listed.devices})
        return _json(listed.model_dump())

    @app.route(f"{REMOTE_PREFIX}/<device>/", defaults={"path": ""}, methods=_METHODS)
    @app.route(f"{REMOTE_PREFIX}/<device>/<path:path>", methods=_METHODS)
    def remote(device: str, path: str) -> Response:
        if gateway is None:
            return _text("This workspace isn't connected to Imbue Studio, so it can't reach your computers.", 503)
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
        return answer(local, "", _requested_path())

    return app
