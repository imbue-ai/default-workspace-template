"""The latchkey gateway, as Files reaches it: the user's devices, the file-sharing permissions this workspace holds on
each, and the shared files themselves. Every supervised program inherits the gateway's address
and credentials from its environment, as the chat app's latchkey endpoints do."""

import os
import urllib.parse
from collections.abc import Iterable
from collections.abc import Mapping
from typing import Final

import httpx

_ENV_GATEWAY: Final[str] = "LATCHKEY_GATEWAY"
_ENV_GATEWAY_PASSWORD: Final[str] = "LATCHKEY_GATEWAY_PASSWORD"
# Only present on desktop-hosted gateways, which deny a request without it.
_ENV_GATEWAY_PERMISSIONS_OVERRIDE: Final[str] = "LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE"
_HEADER_PASSWORD: Final[str] = "X-Latchkey-Gateway-Password"
_HEADER_PERMISSIONS_OVERRIDE: Final[str] = "X-Latchkey-Gateway-Permissions-Override"
_HEADER_DEVICE: Final[str] = "X-Latchkey-Device"

# A device answers through the user's computer, which may be asleep: a listing should give up rather than hang.
DEFAULT_TIMEOUT_SECONDS: Final[float] = 8.0


class Gateway:
    """One client for every call Files makes through the gateway."""

    def __init__(self, base_url: str, password: str, permissions_override: str | None, client: httpx.Client) -> None:
        self._base_url = base_url.rstrip("/")
        self._password = password
        self._permissions_override = permissions_override
        self._client = client

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] = os.environ) -> "Gateway | None":
        """The gateway this process was given, or None when it was given none (a test, a machine with no app)."""
        base_url = environ.get(_ENV_GATEWAY, "")
        if base_url == "":
            return None
        client = httpx.Client(timeout=httpx.Timeout(DEFAULT_TIMEOUT_SECONDS, read=None))
        return cls(
            base_url, environ.get(_ENV_GATEWAY_PASSWORD, ""), environ.get(_ENV_GATEWAY_PERMISSIONS_OVERRIDE), client
        )

    def _headers(self, device: str | None, extra: Mapping[str, str] | None) -> dict[str, str]:
        headers = {_HEADER_PASSWORD: self._password}
        if self._permissions_override:
            headers[_HEADER_PERMISSIONS_OVERRIDE] = self._permissions_override
        if device is not None:
            headers[_HEADER_DEVICE] = device
        headers.update(extra or {})
        return headers

    def request(
        self,
        method: str,
        path: str,
        device: str | None = None,
        headers: Mapping[str, str] | None = None,
        content: str | bytes | Iterable[bytes] | None = None,
        timeout: float | None = DEFAULT_TIMEOUT_SECONDS,
    ) -> httpx.Response:
        """A whole response from ``path`` on the gateway (``devices``, ``permissions/self``, ...)."""
        return self._client.request(
            method,
            f"{self._base_url}/{path.lstrip('/')}",
            headers=self._headers(device, headers),
            content=content,
            timeout=timeout,
        )

    def build(
        self, method: str, path: str, device: str, headers: Mapping[str, str], content: Iterable[bytes] | None
    ) -> httpx.Request:
        """A request to relay, its body (when it has one) streamed from the caller's."""
        return self._client.build_request(
            method,
            f"{self._base_url}/{path.lstrip('/')}",
            headers=self._headers(device, headers),
            content=content,
        )

    def send(self, request: httpx.Request) -> httpx.Response:
        """Send a built request; its body is read as it arrives, and the caller closes it."""
        return self._client.send(request, stream=True)


def device_files_path(path: str, device: str | None) -> str:
    """The gateway path of a file or folder on a device's file server. A device that grants file sharing per device
    serves it under its own id (``minds-file-server-read-<device id>:<path>`` matches ``files/<device id><path>``);
    one that predates that serves it with no id (``minds-file-server-read-<path>`` matches ``files<path>``). Either
    way the device also goes in a header, which routes the request to it."""
    quoted = urllib.parse.quote(path.lstrip("/"), safe="/")
    return f"minds-api-proxy/api/v1/files/{device}/{quoted}" if device else f"minds-api-proxy/api/v1/files/{quoted}"
