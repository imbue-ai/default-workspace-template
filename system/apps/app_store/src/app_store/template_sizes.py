"""How big a template is: its GitHub repository's size, from the public API, cached on disk.

The API's ``size`` is the repository's size in KB. One lookup per template, made when its detail page is shown,
kept for a week; a failed lookup (a repository not on GitHub, the unauthenticated rate limit, no network) is
remembered for an hour, and the page says "not estimated" meanwhile.
"""

import re
import threading
import time
from abc import ABC
from abc import abstractmethod
from pathlib import Path
from typing import Final

import httpx
from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import ValidationError

from getting_started.errors import StateFileError
from getting_started.state_files import read_json_object
from getting_started.state_files import write_json_atomic
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure

SIZES_CACHE_FILENAME: Final[str] = "template_sizes.json"
GITHUB_REPOSITORY_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^https://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repository>[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)
GITHUB_API_ROOT: Final[str] = "https://api.github.com/repos"
_KNOWN_FOR_SECONDS: Final[float] = 7 * 24 * 60 * 60.0
_UNKNOWN_FOR_SECONDS: Final[float] = 60 * 60.0
_FETCH_TIMEOUT_SECONDS: Final[float] = 10.0


@pure
def github_api_url(repository_url: str) -> str | None:
    """The API URL for a GitHub repository URL; None for anything else."""
    match = GITHUB_REPOSITORY_PATTERN.match(repository_url)
    if match is None:
        return None
    return f"{GITHUB_API_ROOT}/{match.group('owner')}/{match.group('repository')}"


class RepositorySizeFetcherInterface(MutableModel, ABC):
    """Looks up a repository's size."""

    @abstractmethod
    def fetch_size_kb(self, api_url: str) -> int | None:
        """The repository's size in KB; None when it could not be looked up (already logged)."""


class GithubRepositorySizeFetcher(RepositorySizeFetcherInterface):
    """The production fetcher: one unauthenticated GET of the repository."""

    def fetch_size_kb(self, api_url: str) -> int | None:
        try:
            response = httpx.get(
                api_url, timeout=_FETCH_TIMEOUT_SECONDS, headers={"Accept": "application/vnd.github+json"}
            )
        except httpx.HTTPError as e:
            logger.warning("Failed to look up {}: {}", api_url, e)
            return None
        if response.is_error:
            logger.warning("Looking up {} answered {}", api_url, response.status_code)
            return None
        try:
            size = response.json().get("size")
        except ValueError:
            return None
        return size if isinstance(size, int) and size >= 0 else None


class CachedSize(FrozenModel):
    """One lookup as cached."""

    size_kb: int | None = Field(description="The size in KB; None when the lookup failed")
    looked_up_at: float = Field(description="When, in seconds since the epoch")


class SizeCacheDocument(FrozenModel):
    sizes: dict[str, CachedSize] = Field(default_factory=dict, description="Lookups by repository URL")


class TemplateSizeStore(MutableModel):
    """Template sizes, looked up on demand and cached on disk. Thread-safe; never raises."""

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    cache_path: Path = Field(frozen=True, description="Where the lookups are cached")
    fetcher: RepositorySizeFetcherInterface = Field(frozen=True, description="How a size is looked up")

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _document: SizeCacheDocument | None = PrivateAttr(default=None)

    def size_kb(self, repository_url: str) -> int | None:
        """The repository's size in KB, or None when it is not estimated."""
        api_url = github_api_url(repository_url)
        if api_url is None:
            return None
        with self._lock:
            document = self._loaded()
            cached = document.sizes.get(repository_url)
            now = time.time()
            if cached is not None:
                lifetime = _KNOWN_FOR_SECONDS if cached.size_kb is not None else _UNKNOWN_FOR_SECONDS
                if now - cached.looked_up_at < lifetime:
                    return cached.size_kb
            size = self.fetcher.fetch_size_kb(api_url)
            updated = SizeCacheDocument(
                sizes={**document.sizes, repository_url: CachedSize(size_kb=size, looked_up_at=now)}
            )
            self._document = updated
            try:
                write_json_atomic(self.cache_path, updated.model_dump(mode="json"))
            except StateFileError as e:
                logger.warning("Could not cache template sizes: {}", e)
            return size

    def _loaded(self) -> SizeCacheDocument:
        if self._document is None:
            raw = read_json_object(self.cache_path)
            try:
                self._document = SizeCacheDocument.model_validate(raw) if raw is not None else SizeCacheDocument()
            except ValidationError as e:
                logger.warning("Ignored the template size cache {}: {}", self.cache_path, e)
                self._document = SizeCacheDocument()
        return self._document
