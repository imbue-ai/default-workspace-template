"""Wallpapers (desktop-interface contracts.md section 4.4): bundled with the shell's static assets, or image files
a user or an agent dropped under ``data/.apps/system_interface/wallpapers/``."""

from pathlib import Path
from typing import Any
from typing import Final
from typing import assert_never

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.system_interface.shell.data_types import Wallpaper
from imbue.system_interface.shell.errors import InvalidShellValueError
from imbue.system_interface.shell.primitives import WallpaperKind
from imbue.system_interface.shell.primitives import WallpaperName

# Under ``data/.apps/system_interface/``, relative to the workspace root the supervised process runs from.
DEFAULT_WALLPAPER_FILES_DIRECTORY: Final[Path] = Path("data/.apps/system_interface/wallpapers")
# Under the shell's static directory, beside the frontend bundle.
BUNDLED_WALLPAPERS_DIRNAME: Final[str] = "wallpapers"

ACCEPTED_WALLPAPER_SUFFIXES: Final[tuple[str, ...]] = (".png", ".jpg", ".jpeg", ".webp")

WALLPAPER_ROUTE_PREFIX: Final[str] = "/wallpapers"


class WallpaperDirectories(FrozenModel):
    """Where each kind of wallpaper is read from."""

    bundled: Path = Field(description="The shell's bundled wallpapers")
    files: Path = Field(description="The workspace's own wallpaper files")


class WallpaperListing(FrozenModel):
    """One wallpaper as ``GET /api/wallpapers`` lists it."""

    kind: WallpaperKind = Field(description="Bundled or file")
    name: WallpaperName = Field(description="The file name without its extension")
    url: str = Field(description="Where the image is served")


@pure
def wallpaper_url(wallpaper: Wallpaper) -> str:
    return f"{WALLPAPER_ROUTE_PREFIX}/{wallpaper.kind.value}/{wallpaper.name}"


@pure
def _directory_for(kind: WallpaperKind, directories: WallpaperDirectories) -> Path:
    match kind:
        case WallpaperKind.BUNDLED:
            return directories.bundled
        case WallpaperKind.FILE:
            return directories.files
        case _ as unreachable:
            assert_never(unreachable)


def _list_kind(kind: WallpaperKind, directories: WallpaperDirectories) -> list[WallpaperListing]:
    directory = _directory_for(kind, directories)
    if not directory.is_dir():
        return []
    listings: list[WallpaperListing] = []
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.suffix.lower() not in ACCEPTED_WALLPAPER_SUFFIXES:
            continue
        try:
            name = WallpaperName(path.stem)
        except InvalidShellValueError:
            continue
        wallpaper = Wallpaper(kind=kind, name=name)
        listings.append(WallpaperListing(kind=kind, name=name, url=wallpaper_url(wallpaper)))
    return listings


def list_wallpapers(directories: WallpaperDirectories) -> list[WallpaperListing]:
    """Every wallpaper on offer, bundled first, each kind by name."""
    return [*_list_kind(WallpaperKind.BUNDLED, directories), *_list_kind(WallpaperKind.FILE, directories)]


def resolve_wallpaper_file(wallpaper: Wallpaper, directories: WallpaperDirectories) -> Path | None:
    """The image file a wallpaper reference names, or None when no accepted file has that name."""
    directory = _directory_for(wallpaper.kind, directories)
    for suffix in ACCEPTED_WALLPAPER_SUFFIXES:
        candidate = directory / f"{wallpaper.name}{suffix}"
        if candidate.is_file():
            return candidate
    return None


@pure
def wallpaper_listing_wire_json(listing: WallpaperListing) -> dict[str, Any]:
    return {"kind": listing.kind.value, "name": str(listing.name), "url": listing.url}
