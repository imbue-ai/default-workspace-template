"""The backup store's usage probe: how much of its owner's storage this repository's account uses.

Imbue Studio's backup store (a restic REST server) answers `GET <repository>_usage`
with the account's usage, its soft quota, and the hard limit it refuses data
uploads past. The runner asks before every tick so a workspace over the soft
quota frees space instead of having its uploads refused; any other REST server,
and any failure to ask, means "no quota information" and the tick proceeds.
"""

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from typing import Final

from imbue.imbue_common.frozen_model import FrozenModel
from loguru import logger
from pydantic import Field

REST_REPOSITORY_PREFIX: Final[str] = "rest:"
USAGE_PROBE_SUFFIX: Final[str] = "_usage"
# The probe is one small JSON answer; a store that takes longer than this is
# treated as unreachable, and the tick goes on without quota information.
USAGE_PROBE_TIMEOUT_SECONDS: Final[float] = 15.0

OpenUrlFn = Callable[[urllib.request.Request, float], bytes]


class StoreUsage(FrozenModel):
    """What the backup store reports for the account owning this repository."""

    used_bytes: int = Field(description="Bytes stored in this repository")
    account_used_bytes: int = Field(
        description="Bytes stored across the account's repositories"
    )
    quota_bytes: int = Field(description="The soft limit the account's clients stop at")
    hard_limit_bytes: int = Field(
        description="The limit past which the store refuses data uploads"
    )
    mode: str = Field(description="active | readonly | disabled")

    @property
    def is_over_soft_limit(self) -> bool:
        return self.account_used_bytes > self.quota_bytes

    @property
    def headroom_bytes(self) -> int:
        """The room between the soft and hard limits, in which a prune may repack."""
        return max(0, self.hard_limit_bytes - self.quota_bytes)


def usage_probe_url(repository: str) -> str | None:
    """The probe URL for a `rest:` repository, or None for any other backend.

    restic's `rest:` form carries a plain `http(s)://` URL after the prefix, ending in
    the repository path. The probe lives at `_usage` directly under that path.
    """
    if not repository.startswith(REST_REPOSITORY_PREFIX):
        return None
    base = repository[len(REST_REPOSITORY_PREFIX) :]
    if not base.startswith(("http://", "https://")):
        return None
    if not base.endswith("/"):
        base += "/"
    return base + USAGE_PROBE_SUFFIX


def _basic_auth_header(env: Mapping[str, str], probe_url: str) -> str | None:
    """The `Authorization` value for the probe: the `RESTIC_REST_*` credentials, else the URL's own userinfo."""
    username = env.get("RESTIC_REST_USERNAME", "")
    password = env.get("RESTIC_REST_PASSWORD", "")
    if not username and not password:
        parts = urllib.parse.urlsplit(probe_url)
        username = urllib.parse.unquote(parts.username or "")
        password = urllib.parse.unquote(parts.password or "")
    if not username and not password:
        return None
    token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    return f"Basic {token}"


def _strip_userinfo(url: str) -> str:
    """The URL without its `user:password@`, the host kept exactly as written (brackets, case, port)."""
    parts = urllib.parse.urlsplit(url)
    if "@" not in parts.netloc:
        return url
    return urllib.parse.urlunsplit(parts._replace(netloc=parts.netloc.rsplit("@", 1)[-1]))


def _open_url(request: urllib.request.Request, timeout_seconds: float) -> bytes:
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        return response.read()


def read_store_usage(
    env: Mapping[str, str],
    *,
    open_url: OpenUrlFn = _open_url,
) -> StoreUsage | None:
    """Ask the backup store for the account's usage; None when there is none to ask or it does not answer.

    Only a `rest:` repository is asked. A 404 is another REST server that knows no
    quota (a user's own rest-server), and any other failure -- a malformed address,
    an unreachable store, a timeout, a malformed answer -- is logged and treated the
    same way, so the tick never stalls or aborts on the probe.
    """
    probe_url = usage_probe_url(env.get("RESTIC_REPOSITORY", ""))
    if probe_url is None:
        return None
    try:
        # Parsing the URL (urlsplit) is what rejects a malformed address, so it
        # sits inside the try with the request it builds.
        request = urllib.request.Request(_strip_userinfo(probe_url), method="GET")
        authorization = _basic_auth_header(env, probe_url)
        if authorization is not None:
            request.add_header("Authorization", authorization)
        request.add_header("Accept", "application/json")
        body = open_url(request, USAGE_PROBE_TIMEOUT_SECONDS)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            logger.debug(
                "The repository's server has no usage probe; proceeding without quota information"
            )
        else:
            logger.warning(
                "The backup store refused the usage probe (HTTP {}); proceeding without quota information",
                e.code,
            )
        return None
    except (urllib.error.URLError, OSError, ValueError) as e:
        logger.warning(
            "Could not read the backup store's usage: {}; proceeding without quota information",
            e,
        )
        return None
    try:
        return StoreUsage.model_validate(json.loads(body))
    except ValueError as e:
        logger.warning(
            "The backup store's usage answer was unreadable: {}; proceeding without quota information",
            e,
        )
        return None
