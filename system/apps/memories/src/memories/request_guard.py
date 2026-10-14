"""Refusing writes that did not come from this app's own page.

The share gateway rejects a foreign Origin, but it admits every origin of the workspace (another app's page among
them), and a page open in the agents' browser runs inside the container and reaches this server directly, past the
gateway. So the server checks for itself, from what the browser reports rather than from host names: the forwarding
proxy in front of a local workspace replaces the Host header with the backend's own address, so the page's Origin
never names the Host this server sees.

A write must be JSON, which a browser will not send cross-origin without a CORS preflight this server never approves.
A browser also marks every request with ``Sec-Fetch-Site``, which proxies pass through untouched: only
``same-origin`` is this app's own page, while another app's page in the workspace is ``same-site``. A request without
it (curl, an agent's script) comes from inside the workspace's trust boundary and is let through.
"""

from typing import Final

from imbue.imbue_common.pure import pure

SAFE_METHODS: Final[frozenset[str]] = frozenset({"GET", "HEAD", "OPTIONS"})
JSON_CONTENT_TYPE: Final[str] = "application/json"
SAME_ORIGIN_FETCH_SITE: Final[str] = "same-origin"


@pure
def is_write_allowed(method: str, fetch_site: str | None, content_type: str | None) -> bool:
    if method.upper() in SAFE_METHODS:
        return True
    if (content_type or "").split(";")[0].strip().lower() != JSON_CONTENT_TYPE:
        return False
    return fetch_site is None or fetch_site.strip().lower() == SAME_ORIGIN_FETCH_SITE
