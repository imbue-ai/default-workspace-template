"""User profiles: the display name and avatar imbue_cloud holds for an account, fetched by user id and cached on disk.

The identity header a proxy stamps names the requester (``owner``, ``user_id``, ``email``) and
nothing more; what to call them and what they look like is the account's profile, which the
connector serves publicly at ``GET {broker_url}/users/{user_id}/profile``. The broker's URL is
``SHARE_BROKER_URL`` in ``data/.secrets/share.env``, the file the minds desktop writes while the
workspace is shared (read fresh on every miss; absent means no profiles are available). Each
answer -- and each failure -- is cached for five minutes under ``profiles/<user_id>.json``, so a
connector outage costs one failed fetch per user per five minutes and never a hung request.
"""

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

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.system_interface.shell.errors import ShellStateError
from imbue.system_interface.shell.primitives import UserId
from imbue.system_interface.shell.state_files import read_json_object
from imbue.system_interface.shell.state_files import write_json_atomic

# The share materials the minds desktop writes while the workspace is shared, relative to the
# workspace root the supervised process runs from; the gateway's ``materials.py`` parses the same file.
DEFAULT_SHARE_ENV_PATH: Final[Path] = Path("data/.secrets/share.env")
SHARE_BROKER_URL_KEY: Final[str] = "SHARE_BROKER_URL"
# The cache lives beside the presence files: ``<presence dir>/profiles/<user_id>.json``.
PROFILES_DIRECTORY_NAME: Final[str] = "profiles"

PROFILE_CACHE_TTL: Final[timedelta] = timedelta(minutes=5)
PROFILE_REQUEST_TIMEOUT_SECONDS: Final[float] = 2.0
# A fetch slower than this is logged: the connector is degrading before it is down.
_SLOW_PROFILE_FETCH_SECONDS: Final[float] = 1.0

# One ``KEY=value`` line of share.env, with or without a leading ``export`` and with optional quotes.
_ENV_LINE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"""^(?:export\s+)?([A-Z0-9_]+)=["']?([^"'\n]*)["']?\s*$""", re.MULTILINE
)


class UserProfile(FrozenModel):
    """What imbue_cloud shows for an account: a self-chosen name and avatar, never an identity."""

    user_id: str = Field(description="The account the profile belongs to")
    display_name: str | None = Field(description="The name the account chose, when it has one")
    avatar_url: str | None = Field(description="The avatar image URL, when the account has one")


class _ProfileWire(FrozenModel):
    """The connector's profile answer: cross-version wire data, so unknown fields are ignored."""

    model_config = ConfigDict(extra="ignore")

    user_id: str = Field(description="The account the profile belongs to")
    display_name: str | None = Field(default=None, description="The chosen name, when there is one")
    avatar_url: str | None = Field(default=None, description="The avatar URL, when there is one")


class _CachedProfile(FrozenModel):
    """One ``profiles/<user_id>.json``: the last answer, or the last failure, and when it was fetched."""

    model_config = ConfigDict(extra="ignore")

    user_id: str = Field(description="The account the entry is for")
    display_name: str | None = Field(default=None, description="The name as last fetched")
    avatar_url: str | None = Field(default=None, description="The avatar URL as last fetched")
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
    return UserProfile(user_id=cached.user_id, display_name=cached.display_name, avatar_url=cached.avatar_url)


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
                avatar_url=fetched.avatar_url if fetched is not None else None,
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

    def _cache_path(self, user_id: UserId) -> Path:
        return self.cache_directory / f"{user_id}.json"

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
        return UserProfile(user_id=wire.user_id, display_name=wire.display_name, avatar_url=wire.avatar_url)
