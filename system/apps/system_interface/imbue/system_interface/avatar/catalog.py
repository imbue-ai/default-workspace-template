"""The avatar catalog (pinned-taskbar-entries plan section 5.1): the designs registered from inside the workspace,
kept with their originals under the app data directory because the originals are the user's, beside the bundled
designs the shell ships."""

from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import ValidationError
from pydantic import field_validator

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.system_interface.avatar.designs import BUNDLED_DESIGNS
from imbue.system_interface.avatar.designs import MAX_SVG_BYTES
from imbue.system_interface.avatar.designs import bundled_design
from imbue.system_interface.avatar.designs import bundled_design_source
from imbue.system_interface.avatar.designs import parse_design_svg
from imbue.system_interface.avatar.primitives import DesignId
from imbue.system_interface.shell.errors import InvalidShellValueError
from imbue.system_interface.shell.state_files import STATE_FILES_LOCK
from imbue.system_interface.shell.state_files import read_json_object
from imbue.system_interface.shell.state_files import write_json_atomic

# Where registered designs live: under the app data directory (the originals are the user's), relative to the
# workspace root the supervised process runs from.
DEFAULT_AVATAR_CATALOG_DIRECTORY: Final[Path] = Path("data/.apps/system_interface/avatars")
CATALOG_FILENAME: Final[str] = "catalog.json"
CATALOG_FILE_VERSION: Final[int] = 1
MAX_REGISTERED_DESIGNS: Final[int] = 32
MAX_LABEL_LENGTH: Final[int] = 64
MAX_SOURCE_PATH_LENGTH: Final[int] = 4096
# Past this the catalog file is not one this shell wrote: the designs are bounded in count and size.
_MAX_CATALOG_BYTES: Final[int] = MAX_REGISTERED_DESIGNS * MAX_SVG_BYTES * 6 + 1024 * 1024


class DesignRegistration(FrozenModel):
    """A trusted local author's original drawing and where it came from (the body of ``POST /api/avatars``)."""

    id: DesignId = Field(description="The stable catalog id")
    label: str = Field(min_length=1, max_length=MAX_LABEL_LENGTH, description="What the chooser calls it")
    svg: str = Field(min_length=1, max_length=MAX_SVG_BYTES, description="The original source, kept verbatim")
    source_path: str = Field(
        min_length=1, max_length=MAX_SOURCE_PATH_LENGTH, description="The original's local path or provenance"
    )

    @field_validator("svg")
    @classmethod
    def _validate_svg(cls, value: str) -> str:
        parse_design_svg(value)
        return value


class AvatarCatalog(FrozenModel):
    """The whole of ``catalog.json``: a bounded list of registered designs."""

    version: int = Field(default=CATALOG_FILE_VERSION, ge=1, le=1, description="The file format version")
    designs: tuple[DesignRegistration, ...] = Field(
        default=(), max_length=MAX_REGISTERED_DESIGNS, description="The registered designs, in registration order"
    )


class DesignListing(FrozenModel):
    """One design as ``GET /api/avatars`` lists it."""

    id: DesignId = Field(description="The catalog id")
    label: str = Field(description="What the chooser calls it")
    source_path: str | None = Field(description="Where a registered design came from; None for a bundled one")


@pure
def design_listing_wire_json(listing: DesignListing) -> dict[str, Any]:
    return {"id": str(listing.id), "label": listing.label, "source_path": listing.source_path}


class AvatarCatalogStore(MutableModel):
    """Reads and writes the registered designs' ``catalog.json`` under the shell's state lock."""

    directory: Path = Field(frozen=True, description="The catalog directory under the app data directory")

    def _path(self) -> Path:
        return self.directory / CATALOG_FILENAME

    def read(self) -> AvatarCatalog:
        """The registered designs; an absent, oversized, or unreadable catalog reads as empty (logged)."""
        with STATE_FILES_LOCK:
            return self._read_unlocked(is_writing=False)

    def _read_unlocked(self, is_writing: bool) -> AvatarCatalog:
        path = self._path()
        if not path.exists():
            return AvatarCatalog()
        if path.stat().st_size > _MAX_CATALOG_BYTES:
            raise InvalidShellValueError(f"the avatar catalog at {path} exceeds its storage bound")
        raw = read_json_object(path)
        if raw is None:
            if is_writing:
                raise InvalidShellValueError(f"the avatar catalog at {path} is unreadable; refusing to overwrite it")
            return AvatarCatalog()
        try:
            return AvatarCatalog.model_validate(raw)
        except ValidationError as e:
            if is_writing:
                raise InvalidShellValueError(
                    f"the avatar catalog at {path} is invalid; refusing to overwrite it"
                ) from e
            logger.warning("Ignored an invalid avatar catalog at {}: {}", path, e.errors()[0]["msg"])
            return AvatarCatalog()

    def entries(self) -> list[DesignListing]:
        """Every design on offer: the bundled ones, then the registered ones."""
        listings = [DesignListing(id=design.id, label=design.label, source_path=None) for design in BUNDLED_DESIGNS]
        for registered in self.read().designs:
            listings.append(
                DesignListing(id=registered.id, label=registered.label, source_path=registered.source_path)
            )
        return listings

    def source(self, design_id: str) -> str | None:
        """A design's original markup, or None for an id nothing holds."""
        bundled = bundled_design(design_id)
        if bundled is not None:
            return bundled_design_source(bundled)
        return next((design.svg for design in self.read().designs if design.id == design_id), None)

    def register(self, registration: DesignRegistration) -> None:
        """Add a design, replacing a registered one of the same id (its source and provenance together); a bundled
        id is never replaced and the catalog never grows past its bound."""
        if bundled_design(registration.id) is not None:
            raise InvalidShellValueError(f"design id {str(registration.id)!r} is bundled and cannot be replaced")
        with STATE_FILES_LOCK:
            existing = self._read_unlocked(is_writing=True)
            kept = tuple(design for design in existing.designs if design.id != registration.id)
            if len(kept) >= MAX_REGISTERED_DESIGNS:
                raise InvalidShellValueError(
                    f"the catalog holds {MAX_REGISTERED_DESIGNS} registered designs; replace one by its id instead"
                )
            catalog = AvatarCatalog(designs=(*kept, registration))
            write_json_atomic(self._path(), catalog.model_dump(mode="json"))
        logger.info("Registered avatar design {} from {}", registration.id, registration.source_path)
