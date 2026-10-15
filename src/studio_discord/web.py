"""A small JSON-over-HTTP helper (standard library only)."""

import json
import urllib.error
import urllib.request
from typing import Any

TIMEOUT_SECONDS = 10.0

# Discord's edge refuses the standard library's default agent string (Cloudflare error 1010).
USER_AGENT = "studio-discord/0.1 (+https://github.com/boweiliu/studio-discord)"


class HttpError(Exception):
    """A request that reached the server and got a non-2xx answer."""

    def __init__(self, status: int, body: str, url: str) -> None:
        super().__init__(f"{status} from {url}: {body[:200]}")
        self.status = status
        self.body = body

    def json_code(self) -> int | None:
        """Discord's numeric error code, when the body carries one."""
        try:
            code = json.loads(self.body).get("code")
        except (ValueError, AttributeError):
            return None
        return code if isinstance(code, int) else None


class UnreachableError(Exception):
    """The server could not be reached at all."""


def request_json(url: str, *, method: str = "GET", headers: dict[str, str] | None = None, body: object = None) -> Any:
    data = None if body is None else json.dumps(body).encode()
    all_headers = {"Accept": "application/json", "User-Agent": USER_AGENT, **(headers or {})}
    if data is not None:
        all_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=all_headers)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            raw = response.read()
    except urllib.error.HTTPError as error:
        raise HttpError(error.code, error.read().decode(errors="replace"), url) from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise UnreachableError(f"{method} {url}: {error}") from error
    return json.loads(raw) if raw else None
