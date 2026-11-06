"""A computer's shared files, read live through the latchkey gateway from the computer's file server (WebDAV). It
lists, reads, writes, makes folders, and deletes; it has no MOVE, and a folder is not offered as a zip."""

import re
import urllib.parse
from collections.abc import Iterator
from collections.abc import Mapping
from typing import Final

import httpx
from flask import Response
from flask import request
from flask import stream_with_context
from loguru import logger
from werkzeug.datastructures import Headers

from files_app.connections import device_permissions
from files_app.connections import devices_with_device_paths
from files_app.connections import list_on_device
from files_app.gateway import Gateway
from files_app.gateway import device_files_path
from files_app.local_files import EDITABLE_LIMIT
from files_app.sources import EntryRecord
from files_app.sources import FileData
from files_app.sources import FilesError
from files_app.sources import FolderData
from files_app.sources import PageData
from files_app.webdav import DavEntry

UNREACHABLE: Final[str] = "Your computer isn't answering. It may be asleep, or Imbue Studio may be closed."
_TEXT_TYPES: Final[re.Pattern[str]] = re.compile(r"^(text/|application/(json|xml|javascript|x-sh|toml|yaml))")
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
_CHUNK: Final[int] = 64 * 1024


def safe_content_disposition(value: str) -> str:
    """The header with its plain ``filename`` reduced to ASCII; the exact name stays in ``filename*``. A name with a
    character outside Latin-1 (the narrow space macOS puts in screenshot names) breaks the forwarder between the
    workspace and the user's browser, which then answers with an error in place of the file."""
    match = re.search(r'filename="([^"]*)"', value)
    if match is None:
        return value.encode("ascii", "replace").decode("ascii")
    plain = match.group(1).encode("ascii", "replace").decode("ascii").replace("?", "_")
    head = value[: match.start()] + f'filename="{plain}"' + value[match.end() :]
    if "filename*=" not in head:
        head += f"; filename*=UTF-8''{urllib.parse.quote(match.group(1))}"
    return head.encode("ascii", "replace").decode("ascii")


def _record(entry: DavEntry) -> EntryRecord:
    return EntryRecord(
        name=entry.name, is_folder=entry.is_folder, is_symlink=False, size=entry.size, mtime=entry.mtime
    )


def _relayed(upstream: httpx.Response) -> Response:
    response = Response(stream_with_context(upstream.iter_raw(_CHUNK)), status=upstream.status_code)
    response.headers.clear()
    for raw_key, raw_value in upstream.headers.raw:
        key = raw_key.decode("latin-1")
        if key.lower() in _HOP_BY_HOP:
            continue
        value = raw_value.decode("utf-8", "replace")
        if key.lower() == "content-disposition":
            value = safe_content_disposition(value)
        response.headers.add(key, value.encode("latin-1", "replace").decode("latin-1"))
    response.call_on_close(upstream.close)
    return response


class DeviceFiles:
    """One device's shared files. ``has_device_paths`` is whether the device's file server is reached under its id,
    read off the device's grants when no listing of the shares has said yet."""

    def __init__(self, gateway: Gateway, device: str, has_device_paths: bool | None) -> None:
        self._gateway = gateway
        self._device = device
        self._has_device_paths = has_device_paths

    @property
    def known_device_paths(self) -> bool | None:
        """``has_device_paths`` if it is known yet, without asking the device."""
        return self._has_device_paths

    @property
    def has_device_paths(self) -> bool:
        if self._has_device_paths is None:
            try:
                granted = device_permissions(self._gateway, self._device)
            except (httpx.HTTPError, ValueError) as error:
                logger.debug("{} did not say how its files are reached: {}", self._device, error)
                raise FilesError(502, UNREACHABLE) from error
            self._has_device_paths = self._device in devices_with_device_paths([granted])
        return self._has_device_paths

    def describe(self, path: str) -> PageData:
        # Depth 1, so a folder's page carries its listing, which the frontend shows without asking again.
        described = list_on_device(self._gateway, self._device, self.has_device_paths, path, depth=1)
        if described is None or len(described) == 0:
            raise FilesError(502, UNREACHABLE)
        entry = described[0]
        if entry.is_folder:
            return FolderData(entries=tuple(_record(child) for child in described[1:]))
        return FileData(is_text=entry.size <= EDITABLE_LIMIT and bool(_TEXT_TYPES.match(entry.content_type)))

    def _send(
        self, method: str, path: str, headers: Mapping[str, str], body: Iterator[bytes] | None
    ) -> httpx.Response:
        files_path = device_files_path(path, self._device if self.has_device_paths else None)
        outgoing = self._gateway.build(method, files_path, self._device, headers, body)
        try:
            upstream = self._gateway.send(outgoing)
        except httpx.HTTPError as error:
            logger.debug("{} did not answer {} {}: {}", self._device, method, path, error)
            raise FilesError(502, UNREACHABLE) from error
        if upstream.status_code == 403:
            upstream.close()
            raise FilesError(403, "Your computer only shares this to read.")
        return upstream

    def _change(self, method: str, path: str, headers: Mapping[str, str], body: Iterator[bytes] | None) -> None:
        upstream = self._send(method, path, headers, body)
        try:
            if upstream.status_code >= 400:
                detail = upstream.read().decode("utf-8", "replace").strip()
                raise FilesError(upstream.status_code, detail or f"Your computer answered {upstream.status_code}.")
        finally:
            upstream.close()

    def read(self, path: str, headers: Headers) -> Response | None:
        # A HEAD stays one, so asking whether a file is there doesn't fetch it from the computer.
        ranged = {"Range": headers["Range"]} if "Range" in headers else {}
        return _relayed(self._send(request.method, path, ranged, None))

    def write(self, path: str, body: Iterator[bytes], length: int | None) -> None:
        headers = {} if length is None else {"Content-Length": str(length)}
        self._change("PUT", path, headers, body)

    def make_folder(self, path: str) -> None:
        self._change("MKCOL", path, {}, None)

    def delete(self, path: str) -> None:
        self._change("DELETE", path, {}, None)

    def move(self, path: str, destination: str) -> None:
        raise FilesError(501, "Renaming isn't supported on files shared from your computer.")

    def zip(self, path: str) -> Response:
        raise FilesError(501, "Downloading a folder from your computer isn't supported yet.")
