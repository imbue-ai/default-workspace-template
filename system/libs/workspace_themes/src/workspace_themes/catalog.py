import hashlib
import os
import threading
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from pathlib import Path
from typing import Final

from app_manifest.errors import InvalidManifestValueError, ManifestLoadError
from app_manifest.manifest import (
    MANIFEST_FILENAME,
    AppManifest,
    ThemingMode,
    load_manifest,
)
from app_manifest.primitives import AppName
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.pure import pure
from loguru import logger
from pydantic import Field, PrivateAttr

from workspace_themes.contract import (
    APP_STYLES_DIRECTORY,
    BUILTIN_THEMES_DIRECTORY,
    FALLBACK_ICON_STEM,
    GENERATED_ICONS_DIRECTORY,
    ICONS_DIRECTORY,
    MAX_BASE_CHAIN_DEPTH,
    STANDARD_LEADING_SLOTS,
    STANDARD_THEME_ID,
    STANDARD_TITLE_ALIGN,
    STANDARD_TRAILING_SLOTS,
    THEME_MANIFEST_FILENAME,
    WORKSPACE_THEMES_DIRECTORY,
)
from workspace_themes.data_types import (
    ResolvedChrome,
    ThemeCatalog,
    ThemeEntry,
    ThemeIcons,
    ThemeManifest,
    ThemeStyleFile,
)
from workspace_themes.interfaces import ThemeCatalogLoaderInterface
from workspace_themes.primitives import (
    THEME_ID_PATTERN,
    ThemeId,
    ThemeRelativePath,
    ThemeRevision,
    ThemeSource,
)
from workspace_themes.validation import (
    check_attribution_files,
    check_folder_files,
    check_icon_files,
    check_manifest_rules,
    check_resolved_chrome,
    check_style_files,
    read_theme_manifest,
)

_REVISION_LENGTH: Final[int] = 16
# Where the workspace's apps are, relative to the repo root.
APPS_DIRECTORY: Final[tuple[str, str]] = ("system", "apps")


class ThemeRoot(FrozenModel):
    """A directory whose subfolders are themes."""

    directory: Path = Field(description="The directory scanned for theme folders")
    source: ThemeSource = Field(description="What a theme found there is")


class _FoundTheme(FrozenModel):
    """A theme folder before its chain is resolved."""

    id: ThemeId = Field(description="The folder's name")
    folder: Path = Field(description="The folder")
    source: ThemeSource = Field(description="Which root it is in")
    manifest: ThemeManifest | None = Field(
        description="The parsed manifest, when it could be read"
    )
    problems: tuple[str, ...] = Field(
        description="What is wrong with the folder itself"
    )


def default_theme_roots(repo_root: Path) -> tuple[ThemeRoot, ...]:
    return (
        ThemeRoot(
            directory=repo_root / BUILTIN_THEMES_DIRECTORY, source=ThemeSource.BUILTIN
        ),
        ThemeRoot(
            directory=repo_root / WORKSPACE_THEMES_DIRECTORY,
            source=ThemeSource.WORKSPACE,
        ),
    )


def app_manifest_paths(repo_root: Path) -> list[Path]:
    return sorted(repo_root.joinpath(*APPS_DIRECTORY).glob(f"*/{MANIFEST_FILENAME}"))


def read_app_manifests(repo_root: Path) -> list[AppManifest]:
    """Every app manifest here that loads; one that does not is logged and left out."""
    manifests: list[AppManifest] = []
    for manifest_path in app_manifest_paths(repo_root):
        try:
            manifests.append(load_manifest(manifest_path, repo_root=repo_root))
        except (ManifestLoadError, InvalidManifestValueError) as error:
            logger.debug("Skipping an app manifest that does not load: {}", error)
    return manifests


def read_app_parts_by_app(repo_root: Path) -> dict[AppName, frozenset[str]]:
    """Every app here, by name, with the theming parts it declares (none unless its mode is parts)."""
    parts_by_app: dict[AppName, frozenset[str]] = {}
    for manifest in read_app_manifests(repo_root):
        is_parts_mode = manifest.theming.mode == ThemingMode.PARTS
        parts_by_app[manifest.name] = (
            frozenset(str(part.name) for part in manifest.theming.parts)
            if is_parts_mode
            else frozenset()
        )
    return parts_by_app


def _find_theme_folders(roots: Sequence[ThemeRoot]) -> list[_FoundTheme]:
    found: list[_FoundTheme] = []
    taken_by_id: dict[str, Path] = {}
    for root in roots:
        if not root.directory.is_dir():
            continue
        for folder in sorted(
            path for path in root.directory.iterdir() if path.is_dir()
        ):
            if not (folder / THEME_MANIFEST_FILENAME).is_file():
                continue
            if THEME_ID_PATTERN.fullmatch(folder.name) is None:
                logger.warning(
                    "Skipping theme folder {}: its name is not a theme id", folder
                )
                continue
            theme_id = ThemeId(folder.name)
            # A linked folder's files are outside the roots, and its edits are not seen; it is listed, with why it
            # is unavailable, but never read.
            if folder.is_symlink():
                manifest, problems = None, ["a theme folder may not be a link"]
            else:
                manifest, problems = read_theme_manifest(folder)
            if manifest is not None:
                problems = [*problems, *check_manifest_rules(manifest, folder.name)]
            taken = taken_by_id.get(theme_id)
            if taken is not None:
                problems = [
                    *problems,
                    f"the id {str(theme_id)!r} is already taken by {taken}",
                ]
            else:
                taken_by_id[theme_id] = folder
            found.append(
                _FoundTheme(
                    id=theme_id,
                    folder=folder,
                    source=root.source,
                    manifest=manifest,
                    problems=tuple(problems),
                )
            )
    return found


@pure
def _base_of(theme: _FoundTheme) -> ThemeId | None:
    if theme.id == STANDARD_THEME_ID or theme.manifest is None:
        return None
    return theme.manifest.base if theme.manifest.base is not None else STANDARD_THEME_ID


def _resolve_chain(
    theme: _FoundTheme, found_by_id: Mapping[str, _FoundTheme]
) -> tuple[tuple[ThemeId, ...], list[str]]:
    """The theme's base chain, standard first and ending with the theme, and what breaks it."""
    chain: list[ThemeId] = [theme.id]
    current = theme
    # Each pass adds one theme to the chain, which a cycle or the depth limit ends, so the loop ends.
    while (base_id := _base_of(current)) is not None:
        if base_id in chain:
            return tuple(reversed(chain)), [
                f"base: the chain {' -> '.join([*chain, base_id])} goes round in a circle"
            ]
        if len(chain) == MAX_BASE_CHAIN_DEPTH:
            return tuple(reversed(chain)), [
                f"base: the chain is more than {MAX_BASE_CHAIN_DEPTH} themes deep, counting standard and this theme"
            ]
        base = found_by_id.get(base_id)
        if base is None:
            return tuple(reversed(chain)), [f"base: there is no theme {str(base_id)!r}"]
        if base.problems or base.manifest is None:
            return tuple(reversed(chain)), [f"base: {str(base_id)!r} is unavailable"]
        chain.append(base_id)
        current = base
    ordered = tuple(reversed(chain))
    if ordered[0] != STANDARD_THEME_ID:
        return ordered, ["base: the chain does not end at the standard theme"]
    return ordered, []


def _resolve_chrome(manifests: Sequence[ThemeManifest]) -> ResolvedChrome:
    title_align = STANDARD_TITLE_ALIGN
    leading = STANDARD_LEADING_SLOTS
    trailing = STANDARD_TRAILING_SLOTS
    for manifest in manifests:
        chrome = manifest.chrome
        title_align = (
            chrome.title_align if chrome.title_align is not None else title_align
        )
        leading = chrome.leading if chrome.leading is not None else leading
        trailing = chrome.trailing if chrome.trailing is not None else trailing
    return ResolvedChrome(title_align=title_align, leading=leading, trailing=trailing)


def _icons_by_app_name(
    directory: Path, suffix: str, container: Path
) -> dict[AppName, Path]:
    """The icon files in the directory, by app name. A link could point anywhere, so a linked icon is left out, and
    so is every icon when a link takes the directory outside ``container``."""
    icons: dict[AppName, Path] = {}
    if not directory.is_dir():
        return icons
    if not directory.resolve().is_relative_to(container.resolve()):
        logger.warning(
            "Skipping the icons in {}: a link takes the folder outside {}",
            directory,
            container,
        )
        return icons
    for path in sorted(directory.glob(f"*{suffix}")):
        if path.stem == FALLBACK_ICON_STEM:
            continue
        if path.is_symlink():
            logger.warning("Skipping icon {}: it is a link", path)
            continue
        try:
            icons[AppName(path.stem)] = path
        except InvalidManifestValueError as error:
            logger.debug(
                "Skipping icon {}: its name is not an app name: {}", path, error
            )
            continue
    return icons


def _resolve_icons(
    chain_themes: Sequence[_FoundTheme], generated_icons_root: Path
) -> ThemeIcons | None:
    """The icons of the nearest theme that declares ``[icons]``; a theme after it in the chain draws to that spec,
    so its own icon files, curated and generated, are read too, a nearer theme's file taking precedence."""
    declared = [
        (index, theme.manifest.icons)
        for index, theme in enumerate(chain_themes)
        if theme.manifest is not None and theme.manifest.icons is not None
    ]
    if not declared:
        return None
    owner_index, spec = declared[-1]
    owner = chain_themes[owner_index]
    suffix = f".{spec.format}"
    curated_by_app: dict[AppName, Path] = {}
    generated_by_app: dict[AppName, Path] = {}
    fallback: Path | None = None
    for theme in chain_themes[owner_index:]:
        curated_by_app.update(
            _icons_by_app_name(theme.folder / ICONS_DIRECTORY, suffix, theme.folder)
        )
        generated_by_app.update(
            _icons_by_app_name(
                generated_icons_root / theme.id / ICONS_DIRECTORY,
                suffix,
                generated_icons_root,
            )
        )
        candidate = theme.folder / ICONS_DIRECTORY / f"{FALLBACK_ICON_STEM}{suffix}"
        if candidate.is_file() and not candidate.is_symlink():
            fallback = candidate
    return ThemeIcons(
        spec=spec,
        spec_folder=owner.folder,
        curated_by_app=curated_by_app,
        generated_by_app=generated_by_app,
        fallback=fallback,
    )


def _style_files_of(
    chain_themes: Sequence[_FoundTheme], present_app_names: AbstractSet[str]
) -> tuple[ThemeStyleFile, ...]:
    """The bundle's files, base first; an overlay for an app this workspace does not have is left out."""
    files: list[ThemeStyleFile] = []
    for theme in chain_themes:
        if theme.manifest is None:
            continue
        files.extend(
            ThemeStyleFile(theme_id=theme.id, path=path)
            for path in theme.manifest.styles.files
        )
        files.extend(
            ThemeStyleFile(
                theme_id=theme.id,
                path=ThemeRelativePath(f"{APP_STYLES_DIRECTORY}/{app_name}.css"),
            )
            for app_name in theme.manifest.styles.apps
            if app_name in present_app_names
        )
    return tuple(files)


def _file_signature_lines(directory: Path) -> list[str]:
    if not directory.is_dir():
        return []
    lines: list[str] = []
    for current, directory_names, file_names in sorted(os.walk(directory)):
        # os.walk does not go into a linked folder, so the link itself is signed: adding or removing one is seen.
        lines.extend(
            f"{(Path(current) / name).relative_to(directory).as_posix()}\tlink"
            for name in sorted(directory_names)
            if (Path(current) / name).is_symlink()
        )
        for file_name in sorted(file_names):
            path = Path(current) / file_name
            try:
                status = path.stat()
            except OSError as error:
                logger.trace("Skipping {} in the catalog signature: {}", path, error)
                continue
            lines.append(
                f"{path.relative_to(directory).as_posix()}\t{status.st_size}\t{status.st_mtime_ns}"
            )
    return lines


def _revision_of(folders: Sequence[Path]) -> ThemeRevision:
    digest = hashlib.sha256()
    for folder in folders:
        digest.update(str(folder).encode())
        # A linked folder is never read, so what it points at does not count.
        if folder.is_symlink():
            continue
        for line in _file_signature_lines(folder):
            digest.update(line.encode())
    return ThemeRevision(digest.hexdigest()[:_REVISION_LENGTH])


def _entry_for(
    theme: _FoundTheme,
    found_by_id: Mapping[str, _FoundTheme],
    app_parts_by_app: Mapping[AppName, frozenset[str]],
    generated_icons_root: Path,
) -> ThemeEntry:
    problems = list(theme.problems)
    chain, chain_problems = (
        _resolve_chain(theme, found_by_id) if theme.manifest else ((theme.id,), [])
    )
    problems.extend(chain_problems)
    # The theme is the last of its chain; found_by_id holds the first folder with each id, which for a theme that
    # repeats another's id is that other theme.
    chain_themes = [
        *(found_by_id[theme_id] for theme_id in chain[:-1] if theme_id in found_by_id),
        theme,
    ]
    folders = [chain_theme.folder for chain_theme in chain_themes]
    generated_folders = [generated_icons_root / theme_id for theme_id in chain]
    revision = _revision_of([*folders, *generated_folders])
    if theme.manifest is None or chain_problems:
        return ThemeEntry(
            id=theme.id,
            folder=theme.folder,
            source=theme.source,
            manifest=theme.manifest,
            chain=chain,
            style_files=(),
            chrome=None,
            icons=None,
            revision=revision,
            problems=tuple(problems),
        )
    manifests = [
        chain_theme.manifest
        for chain_theme in chain_themes
        if chain_theme.manifest is not None
    ]
    chrome = _resolve_chrome(manifests)
    icons = _resolve_icons(chain_themes, generated_icons_root)
    problems.extend(check_folder_files(theme.folder))
    problems.extend(check_style_files(theme.folder, theme.manifest, app_parts_by_app))
    problems.extend(check_resolved_chrome(chrome))
    problems.extend(check_attribution_files(theme.folder, theme.manifest))
    if theme.manifest.icons is not None:
        problems.extend(check_icon_files(theme.folder, theme.id, theme.manifest.icons))
    if icons is None:
        problems.append("icons: neither the theme nor its bases declare [icons]")
    return ThemeEntry(
        id=theme.id,
        folder=theme.folder,
        source=theme.source,
        manifest=theme.manifest,
        chain=chain,
        style_files=_style_files_of(chain_themes, set(app_parts_by_app)),
        chrome=chrome,
        icons=icons,
        revision=revision,
        problems=tuple(problems),
    )


@pure
def _listing_order(entry: ThemeEntry) -> tuple[int, int, str]:
    is_not_standard = 0 if entry.id == STANDARD_THEME_ID else 1
    source_rank = 0 if entry.source == ThemeSource.BUILTIN else 1
    name = (
        str(entry.manifest.name).casefold()
        if entry.manifest is not None
        else str(entry.id)
    )
    return (is_not_standard, source_rank, name)


def read_theme_catalog(
    roots: Sequence[ThemeRoot],
    app_parts_by_app: Mapping[AppName, frozenset[str]],
    generated_icons_root: Path,
) -> ThemeCatalog:
    """Every theme folder under the roots, resolved through its bases and checked against the contract."""
    found = _find_theme_folders(roots)
    found_by_id: dict[str, _FoundTheme] = {}
    for theme in found:
        found_by_id.setdefault(theme.id, theme)
    entries = [
        _entry_for(theme, found_by_id, app_parts_by_app, generated_icons_root)
        for theme in found
    ]
    return ThemeCatalog(
        entries=tuple(sorted(_with_unavailable_bases(entries), key=_listing_order))
    )


def _with_unavailable_bases(entries: Sequence[ThemeEntry]) -> list[ThemeEntry]:
    """The entries with a theme marked unavailable when a base it starts from is, bases settled first."""
    settled: list[ThemeEntry] = []
    available_ids: set[str] = set()
    for entry in sorted(entries, key=lambda candidate: len(candidate.chain)):
        unavailable_base = next(
            (base for base in entry.chain[:-1] if base not in available_ids), None
        )
        if unavailable_base is not None and entry.is_available:
            problem = f"base: {unavailable_base!r} is unavailable"
            entry = entry.model_copy_update(
                to_update(entry.field_ref().problems, (*entry.problems, problem))
            )
        if entry.is_available:
            available_ids.add(entry.id)
        settled.append(entry)
    return settled


def _catalog_signature(repo_root: Path, roots: Sequence[ThemeRoot]) -> str:
    """What the catalog is read from, as a digest: every theme file, every generated icon, and every app manifest."""
    digest = hashlib.sha256()
    for root in roots:
        for line in _file_signature_lines(root.directory):
            digest.update(f"{root.directory}\t{line}".encode())
    for line in _file_signature_lines(repo_root / GENERATED_ICONS_DIRECTORY):
        digest.update(line.encode())
    for manifest_path in app_manifest_paths(repo_root):
        try:
            status = manifest_path.stat()
        except OSError as error:
            logger.trace(
                "Skipping {} in the catalog signature: {}", manifest_path, error
            )
            continue
        digest.update(
            f"{manifest_path}\t{status.st_size}\t{status.st_mtime_ns}".encode()
        )
    return digest.hexdigest()


class CachingThemeCatalogLoader(ThemeCatalogLoaderInterface):
    """Reads the workspace's themes from its repo root, again only when one of the files they come from changed."""

    repo_root: Path = Field(frozen=True, description="The workspace's repo root")
    cached_signature: str | None = Field(
        default=None, description="The signature the cached catalog was read at"
    )
    cached_catalog: ThemeCatalog | None = Field(
        default=None, description="The catalog last read"
    )
    # Request threads and the shell's theme watch load at once; the signature and the catalog are read and kept as
    # one pair, or an older catalog could be kept under a newer signature.
    _load_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def load(self) -> ThemeCatalog:
        with self._load_lock:
            roots = default_theme_roots(self.repo_root)
            signature = _catalog_signature(self.repo_root, roots)
            if self.cached_catalog is not None and signature == self.cached_signature:
                return self.cached_catalog
            catalog = read_theme_catalog(
                roots,
                read_app_parts_by_app(self.repo_root),
                self.repo_root / GENERATED_ICONS_DIRECTORY,
            )
            self.cached_signature = signature
            self.cached_catalog = catalog
            return catalog
