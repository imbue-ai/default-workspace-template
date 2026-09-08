"""The template catalog behind the New Tab page's "Start from a template" section.

The catalog is a JSON document at a fixed URL (``catalog/new-tab-templates.json`` in the
template repository, by default), fetched on demand and reused for a few hours. The last
copy that parsed is written under the shell's state directory, so a machine that cannot
reach the URL keeps showing what it last saw; with no copy at all the page says the
templates failed to load. The document is cross-version data -- an older workspace reads a
newer catalog -- so every model ignores unknown fields, and only the format number and the
four fields a card cannot do without are required.
"""

import json
import threading
import time
from abc import ABC
from abc import abstractmethod
from enum import auto
from pathlib import Path
from typing import Any
from typing import Final
from typing import TypeVar
from urllib.parse import urljoin

import httpx
from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import ValidationError

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.system_interface.shell.state_files import read_json_object
from imbue.system_interface.shell.state_files import write_json_atomic

# The one document format this reader understands; a catalog naming another is refused.
CATALOG_FORMAT: Final[int] = 1

# Where the shipped catalog lives: the raw file on the template repository. The branch moves
# to ``main`` once this work merges there.
DEFAULT_TEMPLATE_CATALOG_URL: Final[str] = (
    "https://raw.githubusercontent.com/imbue-ai/default-workspace-template/josh/new-tab-base/catalog/new-tab-templates.json"
)

# The last good copy, under the shell's state directory.
CATALOG_CACHE_FILENAME: Final[str] = "template_catalog.json"

# A fetched copy is reused this long before the next request refetches it.
FRESH_FOR_SECONDS: Final[float] = 6 * 60 * 60.0
# After a failed fetch, requests keep answering what is held for this long before trying again.
RETRY_AFTER_FAILURE_SECONDS: Final[float] = 60.0
FETCH_TIMEOUT_SECONDS: Final[float] = 10.0
FETCH_SLOW_SECONDS: Final[float] = 3.0


EntryT = TypeVar("EntryT", bound=FrozenModel)


class TemplateCatalogError(Exception):
    """Base error for the template catalog."""


class TemplateCatalogFormatError(TemplateCatalogError, ValueError):
    """The document is not a catalog this reader understands."""


class RequiredAccount(FrozenModel):
    """A service and the permission on it a template needs connected before it runs."""

    model_config = ConfigDict(extra="ignore")

    service: str = Field(description="The latchkey service, e.g. slack-api")
    permission: str = Field(description="The permission on that service, e.g. slack-read-all")


class TemplateChoice(FrozenModel):
    """A decision the template's author left to whoever adopts it."""

    model_config = ConfigDict(extra="ignore")

    summary: str = Field(description="What is unresolved in the published version")
    resolution: str = Field(description="What to do about it")


class CatalogTemplate(FrozenModel):
    """One published template as the catalog lists it."""

    model_config = ConfigDict(extra="ignore")

    slug: str = Field(min_length=1, description="Unique within the catalog; what a shelf refers to")
    title: str = Field(min_length=1, description="The name a card shows")
    description: str = Field(description="One or two sentences")
    repository_url: str = Field(min_length=1, description="The repository a mind adopts it from")
    what_it_is: str = Field(default="", description="Several paragraphs, blank-line separated")
    author: str = Field(default="", description="Who published it; empty when unknown")
    thumbnail: str = Field(
        default="", description="The drawing, relative to the catalog file or absolute; empty for none"
    )
    version: str = Field(default="", description="The published version, e.g. v1; empty when never tagged")
    updated_at: str = Field(default="", description="When the repository last changed, ISO 8601")
    required_accounts: tuple[RequiredAccount, ...] = Field(default=(), description="Accounts to connect")
    required_secrets: tuple[str, ...] = Field(default=(), description="Keys the adopter supplies")
    needs_ai: bool = Field(default=False, description="Whether its code calls a model")
    apt_packages: tuple[str, ...] = Field(default=(), description="System packages it installs")
    choices: tuple[TemplateChoice, ...] = Field(default=(), description="Decisions left to the adopter")


class CatalogShelf(FrozenModel):
    """A browsing row: a title and the slugs it holds, in display order."""

    model_config = ConfigDict(extra="ignore")

    key: str = Field(min_length=1, description="Stable identifier of the row")
    title: str = Field(min_length=1, description="The row's heading")
    slugs: tuple[str, ...] = Field(description="The templates in the row, in order")


class TemplateCatalog(FrozenModel):
    """The whole document, validated."""

    model_config = ConfigDict(extra="ignore")

    format: int = Field(description="The document format; only CATALOG_FORMAT is read")
    generated_at: str = Field(default="", description="When the catalog was produced, ISO 8601")
    templates: tuple[CatalogTemplate, ...] = Field(description="Every template, in catalog order")
    shelves: tuple[CatalogShelf, ...] = Field(default=(), description="The browsing rows, in order")


class TemplateCatalogAvailability(UpperCaseStrEnum):
    """What a read of the store can answer with."""

    # No catalog URL is configured: the page shows no templates section at all.
    DISABLED = auto()
    # A copy fetched within the freshness window.
    FRESH = auto()
    # The last good copy (from disk, or a fetch that could not be refreshed).
    STALE = auto()
    # Nothing was ever fetched and nothing is on disk.
    UNAVAILABLE = auto()


class TemplateCatalogReading(FrozenModel):
    """One answer of the store: the catalog when there is one, and how current it is."""

    availability: TemplateCatalogAvailability = Field(description="How the catalog below should be read")
    catalog: TemplateCatalog | None = Field(description="The catalog, None when disabled or unavailable")


def parse_template_catalog(body: bytes, source: str) -> TemplateCatalog:
    """The catalog in ``body``. A template or shelf that does not validate is skipped with a warning;
    a body that is not a format-1 document raises ``TemplateCatalogFormatError``."""
    try:
        parsed = json.loads(body)
    except ValueError as e:
        raise TemplateCatalogFormatError(f"the catalog at {source} is not JSON: {e}") from e
    if not isinstance(parsed, dict):
        raise TemplateCatalogFormatError(f"the catalog at {source} is not a JSON object")
    if parsed.get("format") != CATALOG_FORMAT:
        raise TemplateCatalogFormatError(
            f"the catalog at {source} has format {parsed.get('format')!r}; this workspace reads format {CATALOG_FORMAT}"
        )
    templates = _validated_entries(parsed.get("templates"), CatalogTemplate, "template", source)
    shelves = _validated_entries(parsed.get("shelves", []), CatalogShelf, "shelf", source)
    try:
        return TemplateCatalog(
            format=CATALOG_FORMAT,
            generated_at=str(parsed.get("generated_at", "")),
            templates=tuple(templates),
            shelves=tuple(shelves),
        )
    except ValidationError as e:
        raise TemplateCatalogFormatError(f"the catalog at {source} does not validate: {e.errors()[0]['msg']}") from e


def _validated_entries(raw_entries: Any, model: type[EntryT], noun: str, source: str) -> list[EntryT]:
    if not isinstance(raw_entries, list):
        raise TemplateCatalogFormatError(f"the catalog at {source} carries no {noun} list")
    entries: list[EntryT] = []
    for raw_entry in raw_entries:
        try:
            entries.append(model.model_validate(raw_entry))
        except ValidationError as e:
            logger.warning("Skipped a {} in the catalog at {}: {}", noun, source, e.errors()[0]["msg"])
    return entries


@pure
def resolve_thumbnail_url(catalog_url: str, thumbnail: str) -> str:
    """Where a template's drawing is: its ``thumbnail`` resolved against the catalog's own URL (an
    absolute URL stays as it is; an empty one stays empty)."""
    if thumbnail == "":
        return ""
    return urljoin(catalog_url, thumbnail)


@pure
def template_wire_json(template: CatalogTemplate, catalog_url: str) -> dict[str, Any]:
    document = template.model_dump(mode="json")
    del document["thumbnail"]
    document["thumbnail_url"] = resolve_thumbnail_url(catalog_url, template.thumbnail)
    return document


@pure
def catalog_wire_json(catalog: TemplateCatalog, catalog_url: str) -> dict[str, Any]:
    """The ``catalog`` object the page reads: the templates with their drawings resolved to URLs."""
    return {
        "generated_at": catalog.generated_at,
        "templates": [template_wire_json(template, catalog_url) for template in catalog.templates],
        "shelves": [shelf.model_dump(mode="json") for shelf in catalog.shelves],
    }


class TemplateCatalogFetcherInterface(MutableModel, ABC):
    """Fetches the catalog document's bytes from its URL."""

    @abstractmethod
    def fetch(self, url: str) -> bytes | None:
        """The document at ``url``, or None when it could not be fetched (already logged)."""


class HttpTemplateCatalogFetcher(TemplateCatalogFetcherInterface):
    """The production fetcher: one GET per call, bounded by a hard timeout and a slow-fetch warning."""

    def fetch(self, url: str) -> bytes | None:
        started_at = time.monotonic()
        try:
            response = httpx.get(url, timeout=FETCH_TIMEOUT_SECONDS, follow_redirects=True)
        except httpx.HTTPError as e:
            logger.warning("Failed to fetch the template catalog from {}: {}", url, e)
            return None
        elapsed = time.monotonic() - started_at
        if elapsed > FETCH_SLOW_SECONDS:
            logger.warning("Fetched the template catalog from {} slowly, in {:.1f}s", url, elapsed)
        if response.is_error:
            logger.warning("Fetching the template catalog from {} answered {}", url, response.status_code)
            return None
        return response.content


class TemplateCatalogStore(MutableModel):
    """The catalog as this process holds it: a fetched copy reused for a while, backed by the last good
    copy on disk. ``read`` never raises: it answers the freshest thing it has, or that it has nothing."""

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    catalog_url: str = Field(frozen=True, description="Where the document is fetched from; empty disables the catalog")
    cache_path: Path = Field(frozen=True, description="Where the last good copy is written")
    fetcher: TemplateCatalogFetcherInterface = Field(frozen=True, description="How the document is fetched")
    fresh_for_seconds: float = Field(
        default=FRESH_FOR_SECONDS, frozen=True, description="How long a fetched copy is reused"
    )
    retry_after_failure_seconds: float = Field(
        default=RETRY_AFTER_FAILURE_SECONDS, frozen=True, description="How long a failed fetch is not retried"
    )

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _catalog: TemplateCatalog | None = PrivateAttr(default=None)
    _is_cache_loaded: bool = PrivateAttr(default=False)
    _is_stale: bool = PrivateAttr(default=True)
    _fetched_at: float | None = PrivateAttr(default=None)
    _failed_at: float | None = PrivateAttr(default=None)

    def read(self) -> TemplateCatalogReading:
        if self.catalog_url == "":
            return TemplateCatalogReading(availability=TemplateCatalogAvailability.DISABLED, catalog=None)
        with self._lock:
            if not self._is_cache_loaded:
                self._catalog = self._read_cache_file()
                self._is_cache_loaded = True
            now = time.monotonic()
            if self._should_fetch(now):
                self._refresh_locked(now)
            if self._catalog is None:
                return TemplateCatalogReading(availability=TemplateCatalogAvailability.UNAVAILABLE, catalog=None)
            availability = TemplateCatalogAvailability.STALE if self._is_stale else TemplateCatalogAvailability.FRESH
            return TemplateCatalogReading(availability=availability, catalog=self._catalog)

    def _should_fetch(self, now: float) -> bool:
        if self._failed_at is not None and now - self._failed_at < self.retry_after_failure_seconds:
            return False
        return self._fetched_at is None or now - self._fetched_at >= self.fresh_for_seconds

    def _refresh_locked(self, now: float) -> None:
        """Fetch and parse the document; on success it replaces what is held (in memory and on disk),
        on failure what is held is kept and marked stale."""
        body = self.fetcher.fetch(self.catalog_url)
        if body is None:
            self._note_failure(now)
            return
        try:
            catalog = parse_template_catalog(body, self.catalog_url)
        except TemplateCatalogFormatError as e:
            logger.warning("Refused the fetched template catalog: {}", e)
            self._note_failure(now)
            return
        self._catalog = catalog
        self._is_stale = False
        self._fetched_at = now
        self._failed_at = None
        self._write_cache_file(catalog)

    def _note_failure(self, now: float) -> None:
        self._failed_at = now
        self._is_stale = True

    def _read_cache_file(self) -> TemplateCatalog | None:
        document = read_json_object(self.cache_path)
        if document is None:
            return None
        try:
            return parse_template_catalog(json.dumps(document).encode("utf-8"), str(self.cache_path))
        except TemplateCatalogFormatError as e:
            logger.warning("Ignored the cached template catalog: {}", e)
            return None

    def _write_cache_file(self, catalog: TemplateCatalog) -> None:
        # A copy that cannot be written costs the next process its fallback, not this one its answer.
        try:
            write_json_atomic(self.cache_path, catalog.model_dump(mode="json"))
        except OSError as e:
            logger.warning("Could not cache the template catalog at {}: {}", self.cache_path, e)


def build_template_catalog_store(
    catalog_url: str, state_directory: Path, fetcher: TemplateCatalogFetcherInterface | None = None
) -> TemplateCatalogStore:
    """The store over the shell's state directory, fetching over HTTP unless a fetcher is injected."""
    return TemplateCatalogStore(
        catalog_url=catalog_url,
        cache_path=state_directory / CATALOG_CACHE_FILENAME,
        fetcher=fetcher if fetcher is not None else HttpTemplateCatalogFetcher(),
    )
