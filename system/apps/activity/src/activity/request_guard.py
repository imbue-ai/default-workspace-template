"""Refusing writes that did not come from this app's own page, and requests from anyone but the workspace's owner.

The share gateway rejects a foreign Origin, but it admits every origin of the workspace (another app's page among
them), and a page open in the agents' browser runs inside the container and reaches this server directly, past the
gateway. So the server checks for itself. A write must be JSON, which a browser will not send cross-origin without a
CORS preflight this server never approves, and it must come from this app's own origin. The ``Host`` header cannot
say which that is: the desktop's forwarder drops it, so the server sees its loopback address while the page's
``Origin`` is the app's own ``https://<label>.agent-<id>.localhost:<port>``. What the browser says is trusted instead:
``Sec-Fetch-Site: same-origin`` on a request from the app's own page (a browser that predates the header is checked
by the Origin's first label, the app's unguessable origin label). A write with no Origin (curl, an agent's script)
comes from inside the workspace's trust boundary and is let through.

Separately, only the workspace's owner may use the app at all: it shows every chat's name and every process's command
line, and it can stop chats, none of which a visitor the owner shared one app with should reach.
"""

import urllib.parse
from typing import Final

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import ValidationError

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure

SAFE_METHODS: Final[frozenset[str]] = frozenset({"GET", "HEAD", "OPTIONS"})
JSON_CONTENT_TYPE: Final[str] = "application/json"
SAME_ORIGIN_FETCH_SITE: Final[str] = "same-origin"


@pure
def is_write_allowed(
    method: str, origin: str | None, fetch_site: str | None, content_type: str | None, app_origin_label: str | None
) -> bool:
    """Whether a request may go on: any read, or a JSON write from this app's own page or from no page at all."""
    if method.upper() in SAFE_METHODS:
        return True
    if (content_type or "").split(";")[0].strip().lower() != JSON_CONTENT_TYPE:
        return False
    if origin is None:
        return True
    if fetch_site is not None:
        return fetch_site.strip().lower() == SAME_ORIGIN_FETCH_SITE
    origin_host = urllib.parse.urlsplit(origin).hostname or ""
    return app_origin_label is not None and origin_host.split(".")[0] == app_origin_label


# The identity every request into the workspace carries from the proxies (the share identity spec, section 4.2).
IDENTITY_HEADER: Final[str] = "X-Imbue-Identity"


class _RequestIdentity(FrozenModel):
    """The part of the header this app reads, parsed as the shell's ``RequestIdentity`` parses it."""

    model_config = ConfigDict(extra="ignore")

    owner: bool = Field(description="Whether the requester is the workspace's owner")


@pure
def is_owner_request(identity_header: str | None) -> bool:
    """Whether the requester is the workspace's owner. As the shell reads it, a missing or unreadable header is the
    owner: it means the request came through no current proxy, from inside the workspace."""
    if identity_header is None or not identity_header.strip():
        return True
    try:
        return _RequestIdentity.model_validate_json(identity_header).owner
    except ValidationError as e:
        logger.warning("Ignored an unreadable {} header: {}", IDENTITY_HEADER, e.errors()[0]["msg"])
        return True
