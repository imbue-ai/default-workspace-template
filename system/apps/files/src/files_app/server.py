"""The Files server: dufs behind it for the workspace's own files, and three things dufs cannot do in front of it.

- ``/__files/api/connections`` lists what the user has shared from their computers.
- ``/__remote/<device>/<path>`` is a shared item read live from the computer it is on, answered in dufs's own shape
  (a page, ``?json``, the bytes, PUT, DELETE, MKCOL), so the frontend browses it as it browses any folder.
- Every answer's ``Content-Disposition`` is made ASCII-safe. dufs names the file in it as it is, and a name with a
  character outside Latin-1 (the narrow space macOS puts in screenshot names) breaks the forwarder between the
  workspace and the user's browser, which then answers with an error in place of the file.
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
from flask import stream_with_context
from loguru import logger

from files_app.connections import SYNCED_FOLDERS
from files_app.connections import device_permissions
from files_app.connections import devices_with_device_paths
from files_app.connections import list_connections
from files_app.connections import list_on_device
from files_app.gateway import Gateway
from files_app.gateway import device_files_path
from files_app.webdav import DavEntry

API_PREFIX: Final[str] = "/__files/api"
REMOTE_PREFIX: Final[str] = "/__remote"

_HOP_BY_HOP: Final[frozenset[str]] = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "host",
    }
)
_ALL_METHODS: Final[list[str]] = [
    "GET",
    "HEAD",
    "POST",
    "PUT",
    "DELETE",
    "OPTIONS",
    "MKCOL",
    "MOVE",
    "COPY",
    "PROPFIND",
    "PATCH",
    "CHECKAUTH",
    "LOGOUT",
]
_CHUNK: Final[int] = 64 * 1024
# What a file page offers to edit, as dufs does: small, and text.
_EDITABLE_LIMIT: Final[int] = 4 * 1024 * 1024
_TEXT_TYPES: Final[re.Pattern[str]] = re.compile(r"^(text/|application/(json|xml|javascript|x-sh|toml|yaml))")
_ASSETS_PREFIX: Final[re.Pattern[str]] = re.compile(r'src="(/__dufs_v[^"/]+__/)')
_NO_MOVE_TO_COMPUTER: Final[str] = "Moving isn't supported on files shared from your computer."
_NO_COPY_ON_COMPUTER: Final[str] = "Copying isn't supported on files shared from your computer."
_UNREACHABLE: Final[str] = "Your computer isn't answering. It may be asleep, or Imbue Studio may be closed."


def safe_content_disposition(value: str) -> str:
    """The header with its plain ``filename`` reduced to ASCII; the exact name stays in ``filename*``."""
    match = re.search(r'filename="([^"]*)"', value)
    if match is None:
        return value.encode("ascii", "replace").decode("ascii")
    plain = match.group(1).encode("ascii", "replace").decode("ascii").replace("?", "_")
    head = value[: match.start()] + f'filename="{plain}"' + value[match.end() :]
    if "filename*=" not in head:
        head += f"; filename*=UTF-8''{urllib.parse.quote(match.group(1))}"
    return head.encode("ascii", "replace").decode("ascii")


def _outgoing_headers(upstream: httpx.Headers) -> list[tuple[str, str]]:
    headers: list[tuple[str, str]] = []
    for raw_key, raw_value in upstream.raw:
        key = raw_key.decode("latin-1")
        if key.lower() in _HOP_BY_HOP:
            continue
        value = raw_value.decode("utf-8", "replace")
        if key.lower() == "content-disposition":
            value = safe_content_disposition(value)
        headers.append((key, value.encode("latin-1", "replace").decode("latin-1")))
    return headers


def _request_body() -> Iterator[bytes] | None:
    if request.content_length in (None, 0) and "chunked" not in request.headers.get("Transfer-Encoding", ""):
        return None
    stream = request.stream
    return iter(lambda: stream.read(_CHUNK), b"")


def _relayed(upstream: httpx.Response) -> Response:
    response = Response(stream_with_context(upstream.iter_raw(_CHUNK)), status=upstream.status_code)
    response.headers.clear()
    for key, value in _outgoing_headers(upstream.headers):
        response.headers.add(key, value)
    response.call_on_close(upstream.close)
    return response


def _json(content: object, status: int = 200) -> Response:
    return Response(json.dumps(content), status=status, mimetype="application/json")


def _text(message: str, status: int) -> Response:
    return Response(message, status=status, mimetype="text/plain")


def _index_data(href: str, children: list[DavEntry]) -> dict[str, object]:
    """A shared folder's listing in the shape dufs gives a page or a ``?json`` answer."""
    return {
        "href": href,
        "kind": "Index",
        "uri_prefix": "/",
        "allow_upload": True,
        "allow_delete": True,
        "allow_search": False,
        "allow_archive": False,
        "dir_exists": True,
        "paths": [
            {
                "path_type": "Dir" if child.is_folder else "File",
                "name": child.name,
                "mtime": child.mtime,
                "size": child.size,
            }
            for child in children
        ],
    }


def create_app(
    dufs_url: str,
    gateway: Gateway | None,
    static_directory: Path,
    synced_root: Path = SYNCED_FOLDERS,
    client: httpx.Client | None = None,
) -> Flask:
    app = Flask(__name__, static_folder=None)
    dufs = client or httpx.Client(base_url=dufs_url, timeout=httpx.Timeout(30.0, read=None))
    assets_prefix: list[str] = []
    # Whether each device's file server is reached under its id, as the last listing of the shares found.
    device_paths: dict[str, bool] = {}

    def dufs_assets_prefix() -> str:
        """The prefix dufs serves the bundle under, read once off one of its own pages."""
        if not assets_prefix:
            page = dufs.get("/", headers={"User-Agent": "Mozilla/5.0"}).text
            match = _ASSETS_PREFIX.search(page)
            assets_prefix.append(match.group(1) if match else "/")
        return assets_prefix[0]

    def page(data: dict[str, object]) -> Response:
        """The app's page with the data a dufs page would carry, for a path dufs does not serve."""
        encoded = base64.b64encode(json.dumps(data).encode()).decode()
        html = (static_directory / "index.html").read_text()
        html = html.replace("__ASSETS_PREFIX__", dufs_assets_prefix()).replace("__INDEX_DATA__", encoded)
        response = Response(html, mimetype="text/html")
        response.headers["Cache-Control"] = "no-cache"
        return response

    def has_device_paths(gateway: Gateway, device: str) -> bool:
        """Read off the device's own grants when no listing has said yet (a share's page opened straight away)."""
        if device not in device_paths:
            device_paths[device] = device in devices_with_device_paths([device_permissions(gateway, device)])
        return device_paths[device]

    def no_gateway() -> Response:
        return _text("This workspace isn't connected to Imbue Studio, so it can't reach your computers.", 503)

    # Connections

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

    # Shared items, live

    def remote_listing(gateway: Gateway, device: str, by_id: bool, path: str) -> Response:
        entries = list_on_device(gateway, device, by_id, path, depth=1)
        if entries is None:
            return _text(_UNREACHABLE, 502)
        return _json(_index_data(f"{REMOTE_PREFIX}/{device}{path}", entries[1:]))

    @app.route(f"{REMOTE_PREFIX}/<device>/", defaults={"path": ""}, methods=_ALL_METHODS)
    @app.route(f"{REMOTE_PREFIX}/<device>/<path:path>", methods=_ALL_METHODS)
    def remote(device: str, path: str) -> Response:
        if gateway is None:
            return no_gateway()
        raw_path = urllib.parse.unquote(urllib.parse.urlsplit(request.environ.get("REQUEST_URI", request.path)).path)
        device_path = "/" + raw_path[len(f"{REMOTE_PREFIX}/{device}/") :]
        # The gateway client drops dot segments, so `..` would climb out of the file server to the gateway's own API.
        if any(segment in (".", "..") for segment in device_path.split("/")):
            return _text("That path isn't a file or folder on your computer.", 400)
        try:
            by_id = has_device_paths(gateway, device)
        except (httpx.HTTPError, ValueError) as error:
            logger.debug("{} did not say how its files are reached: {}", device, error)
            return _text(_UNREACHABLE, 502)
        query = request.args
        method = request.method
        if method == "GET" and "json" in query:
            if "q" in query:
                return _json({"href": device_path, "kind": "Index", "paths": [], "dir_exists": True})
            return remote_listing(gateway, device, by_id, device_path)
        if method == "GET" and "zip" in query:
            return _text("Downloading a folder from your computer isn't supported yet.", 501)
        if method == "MOVE":
            return _text("Renaming isn't supported on files shared from your computer.", 501)
        if method == "COPY":
            return _text(_NO_COPY_ON_COMPUTER, 501)
        if method in ("GET", "HEAD") and ("view" in query or "edit" in query or device_path.endswith("/")):
            # Depth 1, so a folder's page carries its listing, which the frontend shows without asking again.
            described = list_on_device(gateway, device, by_id, device_path, depth=1)
            if described is None or len(described) == 0:
                return _text(_UNREACHABLE, 502)
            entry = described[0]
            href = f"{REMOTE_PREFIX}/{device}{device_path}"
            if entry.is_folder:
                return page(_index_data(href, described[1:]))
            editable = entry.size <= _EDITABLE_LIMIT and bool(_TEXT_TYPES.match(entry.content_type))
            kind = "Edit" if "edit" in query else "View"
            return page(
                {
                    "href": href,
                    "kind": kind,
                    "uri_prefix": "/",
                    "allow_upload": True,
                    "allow_delete": True,
                    "editable": editable,
                }
            )
        forwarded = {
            key: value
            for key, value in request.headers.items()
            if key.lower() in ("range", "content-type", "content-length", "depth")
        }
        files_path = device_files_path(device_path, device if by_id else None)
        upstream_request = gateway.build(method, files_path, device, forwarded, _request_body())
        try:
            upstream = gateway.send(upstream_request)
        except httpx.HTTPError as error:
            logger.debug("{} did not answer {} {}: {}", device, method, device_path, error)
            return _text(_UNREACHABLE, 502)
        if upstream.status_code == 403:
            upstream.close()
            return _text("Your computer only shares this to read.", 403)
        return _relayed(upstream)

    # Everything else is dufs's

    @app.route("/", defaults={"path": ""}, methods=_ALL_METHODS)
    @app.route("/<path:path>", methods=_ALL_METHODS)
    def proxy(path: str) -> Response:
        # dufs would take a destination under the live-share prefix for a folder on its own disk.
        destination = urllib.parse.urlsplit(request.headers.get("Destination", "")).path
        if request.method in ("MOVE", "COPY") and destination.startswith(f"{REMOTE_PREFIX}/"):
            return _text(_NO_MOVE_TO_COMPUTER, 501)
        target = request.environ.get("REQUEST_URI") or request.full_path
        headers = [(key, value) for key, value in request.headers.items() if key.lower() not in _HOP_BY_HOP]
        upstream_request = dufs.build_request(request.method, target, headers=headers, content=_request_body())
        upstream = dufs.send(upstream_request, stream=True)
        return _relayed(upstream)

    return app
