"""Wallpapers (desktop-interface contracts.md section 4.4): bundled with the shell's static assets, or image files
a user or an agent dropped under ``data/.apps/system_interface/wallpapers/``."""

from pathlib import Path
from typing import Final
from typing import assert_never

from loguru import logger
from pydantic import Field
from workspace_layout.errors import InvalidLayoutValueError
from workspace_layout.primitives import WallpaperKind
from workspace_layout.primitives import WallpaperName
from workspace_layout.records import Wallpaper
from workspace_themes.workspace_color import Oklch
from workspace_themes.workspace_color import WorkspaceColorRecord
from workspace_themes.workspace_color import fit
from workspace_themes.workspace_color import is_imbue
from workspace_themes.workspace_color import measure

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.system_interface.shell.studio_wordmark import STUDIO_WORDMARK_PATH
from imbue.system_interface.shell.studio_wordmark import STUDIO_WORDMARK_VIEW_BOX

# Under ``data/.apps/system_interface/``, named relative to the workspace root ``build_shell_state`` resolves it
# against.
DEFAULT_WALLPAPER_FILES_DIRECTORY: Final[Path] = Path("data/.apps/system_interface/wallpapers")
# Under the shell's static directory, beside the frontend bundle.
BUNDLED_WALLPAPERS_DIRNAME: Final[str] = "wallpapers"

ACCEPTED_WALLPAPER_SUFFIXES: Final[tuple[str, ...]] = (".png", ".jpg", ".jpeg", ".webp")

WALLPAPER_ROUTE_PREFIX: Final[str] = "/wallpapers"

# The standard look's default wallpaper (docs/system/blueprint/brand-design-system/): drawn here rather than kept as
# a file, in the workspace color's wallpaper ramp, so it follows the color Imbue Studio keeps for the workspace.
STUDIO_WALLPAPER_NAME: Final[WallpaperName] = WallpaperName("studio")
SVG_MIMETYPE: Final[str] = "image/svg+xml"
# Imbue's own wallpaper key, Honey 9; a workspace color of its own is its wallpaper key too.
IMBUE_WALLPAPER_KEY: Final[str] = "#97630c"
# The two wallpaper ramp steps the field and the mark take, on the ramps' shared lightness ladder and chroma curve
# (system/libs/workspace_ui/src/base.css): step 3 for the field, step 2 for the mark.
_FIELD_STEP: Final[tuple[float, float]] = (0.9556, 0.268)
_MARK_STEP: Final[tuple[float, float]] = (0.9798, 0.149)


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
def _wallpaper_url(wallpaper: Wallpaper) -> str:
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
        except InvalidLayoutValueError as e:
            logger.debug("Skipped a wallpaper file with an unusable name at {}: {}", path, e)
            continue
        wallpaper = Wallpaper(kind=kind, name=name)
        listings.append(WallpaperListing(kind=kind, name=name, url=_wallpaper_url(wallpaper)))
    return listings


def list_wallpapers(directories: WallpaperDirectories) -> list[WallpaperListing]:
    """Every wallpaper on offer: the drawn studio wallpaper, then the bundled files, then the workspace's own, each
    kind by name."""
    studio = Wallpaper(kind=WallpaperKind.BUNDLED, name=STUDIO_WALLPAPER_NAME)
    return [
        WallpaperListing(kind=studio.kind, name=studio.name, url=_wallpaper_url(studio)),
        *_list_kind(WallpaperKind.BUNDLED, directories),
        *_list_kind(WallpaperKind.FILE, directories),
    ]


@pure
def is_studio_wallpaper(wallpaper: Wallpaper) -> bool:
    return wallpaper.kind == WallpaperKind.BUNDLED and wallpaper.name == STUDIO_WALLPAPER_NAME


@pure
def _ramp_step(key: str, step: tuple[float, float]) -> str:
    seed = measure(key).oklch
    lightness, chroma_scale = step
    return fit(Oklch(lightness=lightness, chroma=seed.chroma * chroma_scale, hue=seed.hue)).hex


@pure
def studio_wallpaper_svg(workspace_color: WorkspaceColorRecord) -> str:
    """The studio wallpaper: a field of the wallpaper ramp's step 3 with the wordmark in its step 2 along the bottom,
    sized to whatever it covers (an SVG with no size of its own takes the backdrop's)."""
    key = (
        workspace_color.hex
        if workspace_color.hex is not None and not is_imbue(workspace_color.hex)
        else IMBUE_WALLPAPER_KEY
    )
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="100%" height="100%">'
        f'<rect width="100%" height="100%" fill="{_ramp_step(key, _FIELD_STEP)}"/>'
        f'<svg viewBox="{STUDIO_WORDMARK_VIEW_BOX}" width="100%" height="100%" preserveAspectRatio="xMidYMax meet">'
        f'<path d="{STUDIO_WORDMARK_PATH}" fill="{_ramp_step(key, _MARK_STEP)}"/>'
        "</svg></svg>"
    )


def resolve_wallpaper_file(wallpaper: Wallpaper, directories: WallpaperDirectories) -> Path | None:
    """The image file a wallpaper reference names, or None when no accepted file has that name."""
    directory = _directory_for(wallpaper.kind, directories)
    for suffix in ACCEPTED_WALLPAPER_SUFFIXES:
        candidate = directory / f"{wallpaper.name}{suffix}"
        if candidate.is_file():
            return candidate
    return None


class WallpapersListing(FrozenModel):
    """What ``GET /api/wallpapers`` answers: every wallpaper a desktop can wear."""

    wallpapers: tuple[WallpaperListing, ...] = Field(description="The bundled wallpapers, then the files")
