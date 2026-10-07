from pathlib import Path

from app_manifest.primitives import AppName
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.primitives import NonEmptyStr
from pydantic import Field

from workspace_themes.primitives import (
    ChromeSlot,
    HexColor,
    IconBackground,
    IconDerivation,
    IconFormat,
    IconRendering,
    ThemeAuthor,
    ThemeDescription,
    ThemeId,
    ThemeName,
    ThemeRelativePath,
    ThemeRevision,
    ThemeSource,
    TitleAlign,
)


class ThemeStyles(FrozenModel):
    """The manifest's ``[styles]`` table: the theme's style files, in the order they apply."""

    files: tuple[ThemeRelativePath, ...] = Field(
        default=(), description="Style files, in the order they apply"
    )
    apps: tuple[AppName, ...] = Field(
        default=(),
        description="Apps the theme styles beyond tokens, each with apps/<name>.css",
    )


class ThemeChrome(FrozenModel):
    """The manifest's ``[chrome]`` table; a key left out takes the base theme's value."""

    title_align: TitleAlign | None = Field(
        default=None, description="Where the title sits"
    )
    leading: tuple[ChromeSlot, ...] | None = Field(
        default=None, description="The slots from the left edge"
    )
    trailing: tuple[ChromeSlot, ...] | None = Field(
        default=None, description="The slots ending at the right edge"
    )


class ResolvedChrome(FrozenModel):
    """A theme's title bar with every key settled through its bases."""

    title_align: TitleAlign = Field(description="Where the title sits")
    leading: tuple[ChromeSlot, ...] = Field(description="The slots from the left edge")
    trailing: tuple[ChromeSlot, ...] = Field(
        description="The slots ending at the right edge"
    )


class ThemeIconSpec(FrozenModel):
    """The manifest's ``[icons]`` table: how the theme's icons are drawn, as limits a script can check."""

    guide: ThemeRelativePath = Field(description="The Markdown icon guide")
    format: IconFormat = Field(description="svg or png")
    size: int = Field(
        ge=16, le=512, description="The square canvas in pixels; a png's exact size"
    )
    rendering: IconRendering = Field(description="How the shell scales the icon")
    background: IconBackground = Field(description="Whether the corners must be clear")
    palette: tuple[HexColor, ...] = Field(
        default=(), description="When not empty, the only opaque colors allowed"
    )
    max_colors: int | None = Field(
        default=None,
        ge=2,
        le=256,
        description="At most this many distinct opaque colors",
    )
    derive: IconDerivation = Field(
        description="How the shell makes an icon from an app's standard one"
    )
    references: tuple[ThemeRelativePath, ...] = Field(
        default=(),
        description="Icons of this theme that show the style best, for a model to follow",
    )


class ThemeAttribution(FrozenModel):
    """A third-party work in the theme folder."""

    name: NonEmptyStr = Field(description="The work's name")
    url: NonEmptyStr = Field(description="Where it comes from")
    license: NonEmptyStr = Field(description="An SPDX id, or the font license's name")
    files: tuple[ThemeRelativePath, ...] = Field(
        description="The paths or folders it covers"
    )


class ThemeManifest(FrozenModel):
    """A theme's ``theme.toml`` (docs/system/blueprint/workspace-themes/, section 3.1)."""

    contract: int = Field(description="The contract version the theme is written to")
    id: ThemeId = Field(description="Equals the folder's name")
    name: ThemeName = Field(description="What the theme picker shows")
    description: ThemeDescription = Field(description="One sentence")
    base: ThemeId | None = Field(
        default=None, description="The theme this one starts from; standard when absent"
    )
    author: ThemeAuthor | None = Field(default=None, description="Who made the theme")
    styles: ThemeStyles = Field(
        default_factory=ThemeStyles, description="The style files"
    )
    chrome: ThemeChrome = Field(
        default_factory=ThemeChrome, description="The title bar's arrangement"
    )
    icons: ThemeIconSpec | None = Field(
        default=None, description="How icons are drawn; absent takes the base's"
    )
    attribution: tuple[ThemeAttribution, ...] = Field(
        default=(), description="Third-party works in the folder"
    )


class ThemeStyleFile(FrozenModel):
    """One style file of a theme's bundle: the theme whose folder holds it, and its path there."""

    theme_id: ThemeId = Field(description="The theme whose folder holds the file")
    path: ThemeRelativePath = Field(description="The file's path in that folder")


class ThemeIcons(FrozenModel):
    """A theme's resolved icon spec and where its icon files are."""

    spec: ThemeIconSpec = Field(description="The limits the icons follow")
    # The folder the spec came from: the theme's own, or the base's it inherits the icons of.
    spec_folder: Path = Field(
        description="The theme folder the icon spec and its curated icons are in"
    )
    curated_by_app: dict[AppName, Path] = Field(
        description="Curated icon files, by app name"
    )
    generated_by_app: dict[AppName, Path] = Field(
        description="Icons generated in this workspace, by app name"
    )
    fallback: Path | None = Field(
        description="The generic program icon; none for the standard theme"
    )


class ThemeEntry(FrozenModel):
    """A theme as the catalog found and resolved it."""

    id: ThemeId = Field(description="The theme's id")
    folder: Path = Field(description="The theme's folder")
    source: ThemeSource = Field(description="Built in or made in this workspace")
    manifest: ThemeManifest | None = Field(
        description="The parsed manifest; none when it could not be read"
    )
    chain: tuple[ThemeId, ...] = Field(
        description="The base chain, standard first, ending with this theme"
    )
    style_files: tuple[ThemeStyleFile, ...] = Field(
        description="The bundle's style files, in the order they apply"
    )
    chrome: ResolvedChrome | None = Field(
        description="The resolved title bar; none when unavailable"
    )
    icons: ThemeIcons | None = Field(
        description="The resolved icons; none when unavailable"
    )
    revision: ThemeRevision = Field(
        description="Changes whenever a file the theme is drawn from changes"
    )
    problems: tuple[str, ...] = Field(
        description="Why the theme is unavailable; empty when it is available"
    )

    @property
    def is_available(self) -> bool:
        return not self.problems and self.manifest is not None


class ThemeCatalog(FrozenModel):
    """Every theme the workspace has, available or not, in the order they are listed."""

    entries: tuple[ThemeEntry, ...] = Field(
        description="Every theme found, standard first"
    )

    def find(self, theme_id: str) -> ThemeEntry | None:
        return next((entry for entry in self.entries if entry.id == theme_id), None)

    def find_all(self, theme_id: str) -> tuple[ThemeEntry, ...]:
        """Every entry with the id: more than one when a workspace theme repeats a built-in theme's id."""
        return tuple(entry for entry in self.entries if entry.id == theme_id)

    def find_available(self, theme_id: str) -> ThemeEntry | None:
        entry = self.find(theme_id)
        return entry if entry is not None and entry.is_available else None
