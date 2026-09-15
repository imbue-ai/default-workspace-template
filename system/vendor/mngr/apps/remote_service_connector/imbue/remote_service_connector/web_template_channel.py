"""The template tag a web create pins to, read from the release feed's ``<channel>-web.json``.

Browser creates (``POST /hosts/claim``) lease a pool host baked at exactly one
default-workspace-template tag. The pin is a release-channel pointer rather
than a deploy-time constant, so a connector deploy never moves it: the
promotion workflow publishes ``[web_channels.<channel>]`` from
``apps/minds/release-channels.toml`` as ``<channel>-web.json`` on the tier's
update feed (``scripts/release_channel/web_channels.py``), and this module
reads it on every web create, cached briefly per channel.

The channel is the web user's own choice (the chrome's Settings page, sent
on the claim body; default stable). A tier with no update feed, a feed that
cannot be read, or a channel file that is not published all resolve to None,
and the caller falls back to the deploy-time pin -- which is what dev envs
and staging (no feed) run on, and what production serves during a feed outage.
"""

import http.client
import json
import logging
import os
import re
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Final

from cachetools import TTLCache
from cachetools import cached
from tenacity import retry
from tenacity import retry_if_exception_type
from tenacity import stop_after_attempt
from tenacity import wait_fixed

from imbue.remote_service_connector.errors import WebChannelManifestError

logger = logging.getLogger(__name__)

# The tier's update-feed base URL (``update_feed_base_url`` in its committed
# client.toml), pushed into the connector's Modal Secret by ``minds-admin env
# deploy``. Empty or unset means the tier publishes no channel manifests.
UPDATE_FEED_BASE_URL_ENV_VAR: Final[str] = "MINDS_UPDATE_FEED_BASE_URL"

# The channels the web chrome can opt into; the same names the desktop
# channels use, since the same promotion file declares both.
WEB_CHANNELS: Final[tuple[str, ...]] = ("stable", "beta", "alpha")
DEFAULT_WEB_CHANNEL: Final[str] = "stable"

# The manifest key carrying the tag. Repeats the literal in
# ``scripts/release_channel/web_channels.py`` (``TEMPLATE_REF_KEY``): scripts
# do not ship into this container, so the two cannot share a constant.
TEMPLATE_REF_KEY: Final[str] = "templateRef"

# The pin is always a release tag: web claims lease an exact match on it and
# derive the slice-fleet generation cap from its version.
_TEMPLATE_TAG_RE: Final[re.Pattern[str]] = re.compile(r"^minds-v\d+\.\d+\.\d+$")

# Same cache and fetch shape as the stable download link reader in
# ``accounts_web``: a promotion reaches every container within the TTL, and
# a read that fails is cached too, so an outage costs one create the fetch.
_WEB_CHANNEL_CACHE_SECONDS: Final[float] = 60.0
_WEB_CHANNEL_FETCH_TIMEOUT_SECONDS: Final[float] = 2.0
_WEB_CHANNEL_FETCH_ATTEMPTS: Final[int] = 2
_WEB_CHANNEL_RETRY_SECONDS: Final[float] = 0.25
_FETCH_FAILURES: Final[tuple[type[Exception], ...]] = (OSError, http.client.HTTPException, UnicodeDecodeError)

# (feed_base_url, channel_file) -> the file's text.
FetchChannelFile = Callable[[str, str], str]


def web_channel_filename(channel: str) -> str:
    return f"{channel}-web.json"


def normalize_web_channel(channel: str | None) -> str:
    """The channel a claim asked for, or stable when it named none or something unknown.

    Unknown values (a newer chrome's vocabulary, a hand-edited cookie) fall
    to stable rather than failing the create: the pin is a preference, not
    an authorization.
    """
    if channel is None:
        return DEFAULT_WEB_CHANNEL
    candidate = channel.strip().lower()
    return candidate if candidate in WEB_CHANNELS else DEFAULT_WEB_CHANNEL


def parse_web_channel_manifest(manifest_text: str) -> str:
    """The template tag a ``<channel>-web.json`` names; raises ``WebChannelManifestError`` on anything else."""
    try:
        document = json.loads(manifest_text)
    except json.JSONDecodeError as exc:
        raise WebChannelManifestError("The web channel manifest is not valid JSON") from exc
    if not isinstance(document, dict):
        raise WebChannelManifestError("The web channel manifest is not a JSON object")
    template_ref = document.get(TEMPLATE_REF_KEY)
    if not isinstance(template_ref, str) or _TEMPLATE_TAG_RE.match(template_ref.strip()) is None:
        raise WebChannelManifestError(
            f"The web channel manifest's {TEMPLATE_REF_KEY} is not a minds-vX.Y.Z release tag: {template_ref!r}"
        )
    return template_ref.strip()


@retry(
    retry=retry_if_exception_type(_FETCH_FAILURES),
    stop=stop_after_attempt(_WEB_CHANNEL_FETCH_ATTEMPTS),
    wait=wait_fixed(_WEB_CHANNEL_RETRY_SECONDS),
    reraise=True,
)
def _fetch_channel_file(feed_base_url: str, channel_file: str) -> str:
    """Read one channel file off the feed, retrying a failed read.

    Capped attempts because the route is sync: each holds a worker thread the
    rest of the connector shares. The feed's CDN answers 403 to
    ``Python-urllib/<version>`` by name, hence the explicit User-Agent.
    """
    request = urllib.request.Request(
        f"{feed_base_url.rstrip('/')}/{channel_file}", headers={"User-Agent": "minds-connector"}
    )
    with urllib.request.urlopen(request, timeout=_WEB_CHANNEL_FETCH_TIMEOUT_SECONDS) as response:
        return response.read().decode()


def read_web_template_ref(channel: str, feed_base_url: str, fetch: FetchChannelFile) -> str | None:
    """The tag ``channel``'s web manifest names, or None when it cannot be read.

    An unpublished channel file (404) is expected on a tier whose promotion
    has never listed the channel and is logged at info; anything else is a
    read the feed was expected to serve and is logged at warning. Either way
    the caller falls back to the deploy-time pin.
    """
    channel_file = web_channel_filename(channel)
    try:
        return parse_web_channel_manifest(fetch(feed_base_url, channel_file))
    except urllib.error.HTTPError as exc:
        if exc.code == http.HTTPStatus.NOT_FOUND:
            logger.info("No web pin published for channel %s (%s is not on the feed)", channel, channel_file)
        else:
            logger.warning(
                "Could not read the web pin for channel %s: %s returned %s", channel, channel_file, exc.code
            )
        return None
    except (*_FETCH_FAILURES, WebChannelManifestError) as exc:
        logger.warning("Could not read the web pin for channel %s from %s: %s", channel, channel_file, exc)
        return None


# The condition serialises concurrent misses, so a cold container hit by
# several creates at once reads the feed once per channel.
@cached(cache=TTLCache(maxsize=len(WEB_CHANNELS), ttl=_WEB_CHANNEL_CACHE_SECONDS), condition=threading.Condition())
def cached_web_template_ref(channel: str, feed_base_url: str) -> str | None:
    return read_web_template_ref(channel, feed_base_url, _fetch_channel_file)


def channel_web_template_ref(channel: str | None) -> str | None:
    """The template tag a web create on ``channel`` pins to, or None when no feed can say.

    None on a tier with no update feed configured, and on any read the feed
    cannot satisfy; the caller then uses the deploy-time pin.
    """
    feed_base_url = os.environ.get(UPDATE_FEED_BASE_URL_ENV_VAR, "").strip()
    if not feed_base_url:
        return None
    return cached_web_template_ref(normalize_web_channel(channel), feed_base_url)
