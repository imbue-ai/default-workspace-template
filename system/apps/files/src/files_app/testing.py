"""A gateway that answers in memory, for the Files server's tests."""

import json
from typing import Any
from typing import Final

import httpx
from werkzeug.test import TestResponse

from files_app.gateway import Gateway

DEVICE: Final[str] = "host-mac"


def propfind_body(folder: str, children: list[tuple[str, bool, int]]) -> bytes:
    """A multistatus the way the computer's file server writes one: hrefs under ``/users/``, the folder first."""

    def response(href: str, is_folder: bool, size: int) -> str:
        kind = "<ns0:resourcetype><ns0:collection /></ns0:resourcetype>" if is_folder else "<ns0:resourcetype />"
        length = "" if is_folder else f"<ns0:getcontentlength>{size}</ns0:getcontentlength>"
        content_type = "" if is_folder else "<ns0:getcontenttype>text/plain</ns0:getcontenttype>"
        return (
            f"<ns0:response><ns0:href>{href}</ns0:href><ns0:propstat><ns0:prop>{kind}{length}{content_type}"
            "<ns0:getlastmodified>Thu, 23 Jan 2025 00:40:34 GMT</ns0:getlastmodified></ns0:prop>"
            "<ns0:status>HTTP/1.1 200 OK</ns0:status></ns0:propstat></ns0:response>"
        )

    base = "/users" + folder[len("/Users") :] if folder.startswith("/Users") else folder
    quoted = base.replace(" ", "%20")
    parts = [response(quoted, True, 0)] + [
        response(f"{quoted.rstrip('/')}/{name.replace(' ', '%20')}{'/' if is_folder else ''}", is_folder, size)
        for name, is_folder, size in children
    ]
    return (
        '<?xml version="1.0" encoding="utf-8" ?><ns0:multistatus xmlns:ns0="DAV:">'
        + "".join(parts)
        + "</ns0:multistatus>"
    ).encode()


class FakeComputer:
    """The user's computer as the gateway reaches it: the permissions it granted, the folders it holds, and every
    request it was sent. It grants per device when a permission names it, and then serves its files under its id
    alone; otherwise with no id alone, as the gateway refuses a path no grant matches."""

    def __init__(
        self,
        permissions: list[str],
        folders: dict[str, list[tuple[str, bool, int]]],
        is_awake: bool = True,
        is_writable: bool = True,
    ):
        self.permissions = permissions
        self.folders = folders
        self.files: dict[str, bytes] = {}
        self.is_awake = is_awake
        self.is_writable = is_writable
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/devices":
            return httpx.Response(200, json={"devices": [{"device_id": DEVICE, "hostname": "Kims-Mac.local"}]})
        if path == "/permissions/self":
            return httpx.Response(200, json={"rules": [{"latchkey-self": list(self.permissions)}]})
        prefix = "/minds-api-proxy/api/v1/files"
        if not path.startswith(prefix):
            return httpx.Response(404)
        if not self.is_awake:
            return httpx.Response(502, text="device unreachable")
        rest = path[len(prefix) :]
        is_by_id = rest.startswith(f"/{DEVICE}/")
        if is_by_id != any(f"-{DEVICE}:" in permission for permission in self.permissions):
            return httpx.Response(403, text="request not permitted by the user")
        on_computer = httpx.URL(rest[len(f"/{DEVICE}") :] if is_by_id else rest).path
        folder = on_computer.rstrip("/") or "/"
        if request.method == "PROPFIND":
            if folder in self.folders:
                children = self.folders[folder] if request.headers.get("Depth") == "1" else []
                return httpx.Response(207, content=propfind_body(folder + "/", children))
            if on_computer in self.files:
                return httpx.Response(
                    207,
                    content=propfind_body(on_computer, []).replace(
                        b"<ns0:resourcetype><ns0:collection /></ns0:resourcetype>", b"<ns0:resourcetype />"
                    ),
                )
            return httpx.Response(404)
        if request.method == "GET" and on_computer in self.files:
            name = on_computer.rsplit("/", 1)[-1]
            return httpx.Response(
                200,
                content=self.files[on_computer],
                headers=[
                    (b"Content-Type", b"image/png"),
                    (b"Content-Disposition", f'inline; filename="{name}"'.encode()),
                ],
            )
        if request.method == "PUT" and not self.is_writable:
            return httpx.Response(403, text="forbidden")
        if request.method == "PUT":
            self.files[on_computer] = request.read()
            return httpx.Response(201)
        return httpx.Response(404)

    def streamed(self, request: httpx.Request) -> httpx.Response:
        """The answer as the network gives it: a body still to be read, which the server relays as it arrives."""
        answer = self.handle(request)
        body = answer.read()
        return httpx.Response(answer.status_code, headers=answer.headers, stream=httpx.ByteStream(body))

    def gateway(self) -> Gateway:
        return Gateway(
            "http://gateway.test", "secret", None, httpx.Client(transport=httpx.MockTransport(self.streamed))
        )


def json_of(response: TestResponse) -> dict[str, Any]:
    return json.loads(response.get_data())
