"""User profiles: the display name and profile picture imbue_cloud holds for an account, fetched by user id and cached on disk.

The identity header a proxy stamps names the requester (``owner``, ``user_id``, ``email``) and
nothing more; what to call them and what they look like is the account's profile, which the
connector serves publicly at ``GET {broker_url}/users/{user_id}/profile``. The broker's URL is
``SHARE_BROKER_URL`` in ``data/.secrets/share.env``, the file the Imbue Studio desktop writes while the
workspace is shared (read fresh on every miss; absent means no profiles are available). Each
answer -- and each failure -- is cached for five minutes under ``profiles/<user_id>.json``, so a
connector outage costs one failed fetch per user per five minutes and never a hung request.

The picture itself is served from the shell's own origin (the multiplayer spec, section 4.2): the pages'
content security policy admits images from the workspace alone, so the roster points at the shell's
picture route and the shell fetches the bytes from the profile's URL once, keeping them beside the
profile under ``profiles/<user_id>.picture.json`` until the profile names another URL.
"""

import base64
import hashlib
import re
import time
from collections.abc import Sequence
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import Final

import httpx
from loguru import logger
from pydantic import AwareDatetime
from pydantic import ConfigDict
from pydantic import Field
from pydantic import ValidationError
from workspace_layout.primitives import UserId

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.system_interface.shell.errors import ShellStateError
from imbue.system_interface.shell.state_files import read_json_object
from imbue.system_interface.shell.state_files import write_json_atomic

# The share materials the Imbue Studio desktop writes while the workspace is shared, relative to the
# workspace root the supervised process runs from; the gateway's ``materials.py`` parses the same file.
DEFAULT_SHARE_ENV_PATH: Final[Path] = Path("data/.secrets/share.env")
SHARE_BROKER_URL_KEY: Final[str] = "SHARE_BROKER_URL"
# The cache lives beside the presence files: ``<presence dir>/profiles/<user_id>.json``.
PROFILES_DIRECTORY_NAME: Final[str] = "profiles"

PROFILE_CACHE_TTL: Final[timedelta] = timedelta(minutes=5)
PROFILE_REQUEST_TIMEOUT_SECONDS: Final[float] = 2.0
PICTURE_REQUEST_TIMEOUT_SECONDS: Final[float] = 5.0
# A picture larger than this is refused rather than cached: the connector validates uploads well under it.
MAX_PROFILE_PICTURE_BYTES: Final[int] = 4 * 1024 * 1024
_PICTURE_CACHE_SUFFIX: Final[str] = ".picture.json"
# How many hex digits of the picture URL's digest name its version in the shell's route: enough that two
# pictures of one person never share one, short enough for a query value.
_PICTURE_VERSION_LENGTH: Final[int] = 16
# A fetch slower than this is logged: the connector is degrading before it is down.
_SLOW_PROFILE_FETCH_SECONDS: Final[float] = 1.0
_SLOW_PICTURE_FETCH_SECONDS: Final[float] = 2.0

# One ``KEY=value`` line of share.env, with or without a leading ``export`` and with optional quotes.
_ENV_LINE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"""^(?:export\s+)?([A-Z0-9_]+)=["']?([^"'\n]*)["']?\s*$""", re.MULTILINE
)


class UserProfile(FrozenModel):
    """What imbue_cloud shows for an account: a self-chosen name and profile picture, never an identity."""

    user_id: str = Field(description="The account the profile belongs to")
    display_name: str | None = Field(description="The name the account chose, when it has one")
    profile_picture_url: str | None = Field(description="The profile picture URL, when the account has one")


class ProfilePicture(FrozenModel):
    """A profile picture's bytes and media type, as the shell serves them from its own origin."""

    media_type: str = Field(description="The image's media type, as the connector served it")
    content: bytes = Field(description="The image bytes")


class _CachedPicture(FrozenModel):
    """The on-disk form of a fetched picture: the URL it came from, so a new picture is fetched afresh."""

    url: str = Field(description="The profile picture URL the bytes were fetched from")
    media_type: str = Field(description="The image's media type")
    content_base64: str = Field(description="The image bytes, base64")


@pure
def picture_version_of(profile_picture_url: str) -> str:
    """The version a picture URL carries in the shell's own picture route: a digest of the connector's URL, which
    changes with the picture's content hash, so a page's cached copy is never shown for a newer picture."""
    return hashlib.sha256(profile_picture_url.encode()).hexdigest()[:_PICTURE_VERSION_LENGTH]


class _ProfileWire(FrozenModel):
    """The connector's profile answer: cross-version wire data, so unknown fields are ignored."""

    model_config = ConfigDict(extra="ignore")

    user_id: str = Field(description="The account the profile belongs to")
    display_name: str | None = Field(default=None, description="The chosen name, when there is one")
    profile_picture_url: str | None = Field(default=None, description="The profile picture URL, when there is one")


class _CachedProfile(FrozenModel):
    """One ``profiles/<user_id>.json``: the last answer, or the last failure, and when it was fetched."""

    model_config = ConfigDict(extra="ignore")

    user_id: str = Field(description="The account the entry is for")
    display_name: str | None = Field(default=None, description="The name as last fetched")
    profile_picture_url: str | None = Field(default=None, description="The profile picture URL as last fetched")
    fetched_at: AwareDatetime = Field(description="When the connector was last asked")
    is_fetch_failed: bool = Field(description="Whether that ask failed (the entry then holds no profile)")


@pure
def parse_share_broker_url(share_env_text: str) -> str | None:
    """The broker URL share.env names, without a trailing slash; None when the file does not name one."""
    value_by_key = {match.group(1): match.group(2) for match in _ENV_LINE_PATTERN.finditer(share_env_text)}
    broker_url = value_by_key.get(SHARE_BROKER_URL_KEY, "").strip().rstrip("/")
    return broker_url if broker_url else None


def read_share_broker_url(share_env_path: Path) -> str | None:
    """The broker URL of the current share; None while the workspace is not shared (no file) or the file is unusable."""
    if not share_env_path.exists():
        return None
    try:
        text = share_env_path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("Could not read the share materials at {}: {}", share_env_path, e)
        return None
    return parse_share_broker_url(text)


@pure
def _profile_of(cached: _CachedProfile) -> UserProfile | None:
    if cached.is_fetch_failed:
        return None
    return UserProfile(
        user_id=cached.user_id, display_name=cached.display_name, profile_picture_url=cached.profile_picture_url
    )


class ProfileResolver(MutableModel):
    """Answers a user's profile from the on-disk cache, asking the connector on a miss or after the TTL."""

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    cache_directory: Path = Field(frozen=True, description="Where the per-user cache files live")
    share_env_path: Path = Field(frozen=True, description="The share materials file naming the broker")
    cache_ttl: timedelta = Field(
        default=PROFILE_CACHE_TTL, frozen=True, description="How long an answer, or a failure, is trusted"
    )
    request_timeout_seconds: float = Field(
        default=PROFILE_REQUEST_TIMEOUT_SECONDS, frozen=True, description="The hard bound on one fetch"
    )
    transport: httpx.BaseTransport | None = Field(
        default=None, frozen=True, description="A test's stand-in for the network; the real one when None"
    )

    def resolve(self, user_id: UserId, now: datetime) -> UserProfile | None:
        """The user's profile, or None when the connector has none, cannot be reached, or the workspace is not
        shared. Never raises for a connector failure: that is cached as a miss until the TTL passes."""
        cached = self._read_cached(user_id)
        if cached is not None and now - cached.fetched_at < self.cache_ttl:
            return _profile_of(cached)
        broker_url = read_share_broker_url(self.share_env_path)
        if broker_url is None:
            return None
        fetched = self._fetch(broker_url, user_id)
        self._write_cached(
            _CachedProfile(
                user_id=str(user_id),
                display_name=fetched.display_name if fetched is not None else None,
                profile_picture_url=fetched.profile_picture_url if fetched is not None else None,
                fetched_at=now,
                is_fetch_failed=fetched is None,
            )
        )
        return fetched

    def resolve_many(self, user_ids: Sequence[UserId], now: datetime) -> dict[str, UserProfile]:
        """The profile of each user that has one, by user id."""
        profile_by_user_id: dict[str, UserProfile] = {}
        for user_id in user_ids:
            profile = self.resolve(user_id, now)
            if profile is not None:
                profile_by_user_id[str(user_id)] = profile
        return profile_by_user_id

    def resolve_picture(self, user_id: UserId, now: datetime) -> ProfilePicture | None:
        """The user's profile picture, or None when their profile names none (or cannot be resolved): the cached
        bytes while the profile still names the URL they came from, else fetched from that URL and cached. A fetch
        that fails is not cached: the page falls back to a monogram and the next load tries again."""
        profile = self.resolve(user_id, now)
        if profile is None or profile.profile_picture_url is None:
            return None
        cached = self._read_cached_picture(user_id)
        if cached is not None and cached.url == profile.profile_picture_url:
            return ProfilePicture(media_type=cached.media_type, content=base64.b64decode(cached.content_base64))
        fetched = self._fetch_picture(profile.profile_picture_url, user_id)
        if fetched is None:
            return None
        self._write_cached_picture(
            user_id,
            _CachedPicture(
                url=profile.profile_picture_url,
                media_type=fetched.media_type,
                content_base64=base64.b64encode(fetched.content).decode(),
            ),
        )
        return fetched

    def _cache_path(self, user_id: UserId) -> Path:
        return self.cache_directory / f"{user_id}.json"

    def _picture_cache_path(self, user_id: UserId) -> Path:
        return self.cache_directory / f"{user_id}{_PICTURE_CACHE_SUFFIX}"

    def _read_cached_picture(self, user_id: UserId) -> _CachedPicture | None:
        document = read_json_object(self._picture_cache_path(user_id))
        if document is None:
            return None
        try:
            return _CachedPicture.model_validate(document)
        except ValidationError as e:
            logger.warning("Skipped an unreadable picture cache entry for {}: {}", user_id, e.errors()[0]["msg"])
            return None

    def _write_cached_picture(self, user_id: UserId, cached: _CachedPicture) -> None:
        # A cache that cannot be written costs a fetch per request, not the request itself.
        try:
            write_json_atomic(self._picture_cache_path(user_id), cached.model_dump(mode="json"))
        except OSError as e:
            logger.warning("Could not cache the profile picture of {}: {}", user_id, e)

    def _fetch_picture(self, url: str, user_id: UserId) -> ProfilePicture | None:
        started_at = time.monotonic()
        client = httpx.Client(transport=self.transport, timeout=PICTURE_REQUEST_TIMEOUT_SECONDS)
        with client:
            try:
                response = client.get(url)
            except httpx.HTTPError as e:
                logger.warning("Failed to fetch the profile picture of {} from {}: {}", user_id, url, e)
                return None
        elapsed_seconds = time.monotonic() - started_at
        if elapsed_seconds > _SLOW_PICTURE_FETCH_SECONDS:
            logger.warning("Fetched the profile picture of {} slowly ({:.1f}s)", user_id, elapsed_seconds)
        if response.status_code != httpx.codes.OK:
            logger.warning("Fetching the profile picture of {} answered HTTP {}", user_id, response.status_code)
            return None
        if len(response.content) > MAX_PROFILE_PICTURE_BYTES:
            logger.warning("Refused the profile picture of {}: {} bytes", user_id, len(response.content))
            return None
        media_type = response.headers.get("Content-Type", "").split(";")[0].strip()
        if not media_type.startswith("image/"):
            logger.warning("Refused the profile picture of {}: its type is {!r}", user_id, media_type)
            return None
        return ProfilePicture(media_type=media_type, content=response.content)

    def _read_cached(self, user_id: UserId) -> _CachedProfile | None:
        document = read_json_object(self._cache_path(user_id))
        if document is None:
            return None
        try:
            return _CachedProfile.model_validate(document)
        except ValidationError as e:
            logger.warning("Skipped an unreadable profile cache entry for {}: {}", user_id, e.errors()[0]["msg"])
            return None

    def _write_cached(self, cached: _CachedProfile) -> None:
        # A cache that cannot be written costs a fetch per request, not the request itself.
        try:
            write_json_atomic(self._cache_path(UserId(cached.user_id)), cached.model_dump(mode="json"))
        except ShellStateError as e:
            logger.warning("Could not cache the profile of {}: {}", cached.user_id, e)

    def _fetch(self, broker_url: str, user_id: UserId) -> UserProfile | None:
        url = f"{broker_url}/users/{user_id}/profile"
        started_at = time.monotonic()
        client = httpx.Client(transport=self.transport, timeout=self.request_timeout_seconds)
        with client:
            try:
                response = client.get(url)
            except httpx.HTTPError as e:
                logger.warning("Failed to fetch the profile of {} from {}: {}", user_id, url, e)
                return None
        elapsed_seconds = time.monotonic() - started_at
        if elapsed_seconds > _SLOW_PROFILE_FETCH_SECONDS:
            logger.warning("Fetched the profile of {} slowly ({:.1f}s)", user_id, elapsed_seconds)
        if response.status_code != httpx.codes.OK:
            logger.warning("Fetching the profile of {} answered HTTP {}", user_id, response.status_code)
            return None
        try:
            wire = _ProfileWire.model_validate_json(response.content)
        except ValidationError as e:
            logger.warning("Ignored an unreadable profile answer for {}: {}", user_id, e.errors()[0]["msg"])
            return None
        # An empty URL is no picture: the pages draw a monogram for None, and an <img src=""> renders broken.
        return UserProfile(
            user_id=wire.user_id, display_name=wire.display_name, profile_picture_url=wire.profile_picture_url or None
        )
