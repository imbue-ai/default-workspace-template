"""Refusing writes that did not come from this app's own page.

The share gateway rejects a foreign Origin, but it admits every origin of the workspace (another app's page among
them), and a page open in the agents' browser runs inside the container and reaches this server directly, past the
gateway. So the server checks for itself: a write must be JSON, which a browser will not send cross-origin without
a CORS preflight this server never approves, and a present Origin must name this server's own host. A request with
no Origin (curl, an agent's script) comes from inside the workspace's trust boundary and is let through.
"""

import urllib.parse
from typing import Final

from imbue.imbue_common.pure import pure

SAFE_METHODS: Final[frozenset[str]] = frozenset({"GET", "HEAD", "OPTIONS"})
JSON_CONTENT_TYPE: Final[str] = "application/json"


@pure
def is_write_allowed(method: str, origin: str | None, host: str, content_type: str | None) -> bool:
    if method.upper() in SAFE_METHODS:
        return True
    if (content_type or "").split(";")[0].strip().lower() != JSON_CONTENT_TYPE:
        return False
    if origin is None:
        return True
    return urllib.parse.urlsplit(origin).netloc.lower() == host.lower()
