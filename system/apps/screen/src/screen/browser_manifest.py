"""The tab manifest, ``data/.apps/browser/instances.json`` (spec sections 10.3 and 16.1): which browsers exist and
the tab URLs each had, so Chromium comes back on them when it is next started. It records no lease or queue state
(process-scoped, gone with the stack) and no profile bytes (Chromium keeps those itself under the profile
directory); it lives under the app's stored data, where the restic host backup covers it.

Writes are atomic (a sibling temporary file then ``os.replace``), so a reader on the next boot sees the old or the
new complete file, never a torn one.
"""

import os
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import ValidationError

from imbue.imbue_common.frozen_model import FrozenModel
from screen.primitives import BrowserName

# Browser ids are names; an older manifest whose ids were integers is treated as missing rather than coerced,
# so numeric profiles never resurrect as named browsers.
MANIFEST_VERSION: Final[int] = 2


class ManifestEntry(FrozenModel):
    """One saved browser: its name, the tab URLs to reopen with the active one marked, and whether the user stopped
    it (a stopped browser comes back stopped, with its tabs, until it is started)."""

    # A manifest written by an older build carries fields this one no longer keeps (``window_seen``).
    model_config = ConfigDict(extra="ignore")

    id: BrowserName = Field(description="The browser's name")
    tabs: tuple[str, ...] = Field(default=(), description="The restorable tab URLs, in order")
    active_tab: int = Field(default=0, description="The index of the tab in front")
    stopped: bool = Field(default=False, description="Whether the user stopped the browser rather than its stack")


class BrowserManifest(FrozenModel):
    """The whole fleet's saved topology."""

    model_config = ConfigDict(extra="ignore")

    version: int = Field(default=MANIFEST_VERSION, description="The manifest's schema version")
    browsers: tuple[ManifestEntry, ...] = Field(default=(), description="Every saved browser, by name")


class ManifestStore(FrozenModel):
    """Reads and atomically writes the manifest at one path."""

    path: Path = Field(description="Where the manifest lives")

    def read(self) -> BrowserManifest | None:
        """The manifest, or None when it is absent, unreadable, or of another version (logged, never raised: the
        fleet then falls back to the on-disk profiles)."""
        if not self.path.exists():
            return None
        try:
            loaded = BrowserManifest.model_validate_json(self.path.read_text())
        except (OSError, ValueError, ValidationError) as e:
            logger.warning("The browser manifest at {} is unreadable ({}); ignoring it", self.path, e)
            return None
        if loaded.version != MANIFEST_VERSION:
            logger.warning(
                "The browser manifest at {} is version {} (expected {}); ignoring it",
                self.path,
                loaded.version,
                MANIFEST_VERSION,
            )
            return None
        return loaded

    def write(self, manifest: BrowserManifest) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(manifest.model_dump_json())
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)
