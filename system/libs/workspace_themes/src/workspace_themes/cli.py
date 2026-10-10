import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

import click
from app_manifest.errors import ManifestLoadError
from app_manifest.manifest import MANIFEST_FILENAME, ThemingMode, load_manifest
from imbue.imbue_common.pure import pure
from PIL import Image

from workspace_themes.catalog import (
    APPS_DIRECTORY,
    CachingThemeCatalogLoader,
    read_app_manifests,
)
from workspace_themes.contract import (
    CONTRACT_VERSION,
    FALLBACK_ICON_STEM,
    STANDARD_THEME_ID,
    THEME_MANIFEST_FILENAME,
    WORKSPACE_THEMES_DIRECTORY,
)
from workspace_themes.data_types import ThemeCatalog, ThemeEntry, ThemeIcons
from workspace_themes.errors import (
    IconImageError,
    InvalidThemeValueError,
    ThemeFolderExistsError,
    ThemeNotFoundError,
)
from workspace_themes.icons import (
    check_icon_image,
    fit_icon_image,
    icon_install_path,
    install_icon_file,
    load_icon_image,
    pixel_scale_hint,
    preview_icon_image,
    render_pixel_grid,
)
from workspace_themes.lint import lint_app_frontend
from workspace_themes.primitives import (
    HexColor,
    IconFormat,
    ThemeDescription,
    ThemeId,
    ThemeName,
    ThemeSource,
)
from workspace_themes.serving import build_bundle_css
from workspace_themes.validation import check_svg_icon

_STARTER_TOKENS: Final[
    str
] = """/* {name}: the design tokens this theme sets (docs/system/blueprint/workspace-themes/, section 4.1).
   Every token left out keeps its base theme's value. */
:root {{
}}
"""
_STARTER_PARTS: Final[
    str
] = """/* {name}: how the interface's parts look (docs/system/blueprint/workspace-themes/, section 4.2).
   Select parts as [data-part="..."]; never a class or an id. */
"""


def _load_catalog(repo_root: str) -> ThemeCatalog:
    return CachingThemeCatalogLoader(repo_root=Path(repo_root).resolve()).load()


def _require_theme(catalog: ThemeCatalog, theme_id: str) -> ThemeEntry:
    entry = catalog.find_available(theme_id)
    if entry is None:
        found = catalog.find(theme_id)
        if found is not None:
            raise click.ClickException(
                f"theme {theme_id!r} is unavailable: " + "; ".join(found.problems)
            )
        raise click.ClickException(str(ThemeNotFoundError(theme_id)))
    return entry


def _require_icons(entry: ThemeEntry) -> ThemeIcons:
    if entry.icons is None:
        raise click.ClickException(f"theme {str(entry.id)!r} has no icon spec")
    return entry.icons


@click.group()
def main() -> None:
    """Workspace themes: list, check, scaffold, and draw icons for the theme folders."""


@main.command("list")
@click.option(
    "--repo-root",
    default=".",
    show_default=True,
    type=click.Path(file_okay=False),
    help="The workspace root",
)
@click.option(
    "--json", "is_json", is_flag=True, default=False, help="Print the catalog as JSON"
)
def list_themes(repo_root: str, is_json: bool) -> None:
    """Every theme the workspace has, and whether it is available."""
    catalog = _load_catalog(repo_root)
    if is_json:
        click.echo(
            json.dumps(
                [
                    {
                        "id": entry.id,
                        "name": entry.manifest.name if entry.manifest else None,
                        "source": entry.source,
                        "available": entry.is_available,
                        "folder": str(entry.folder),
                        "chain": list(entry.chain),
                        "problems": list(entry.problems),
                    }
                    for entry in catalog.entries
                ],
                indent=2,
            )
        )
        return
    for entry in catalog.entries:
        name = entry.manifest.name if entry.manifest else "?"
        state = (
            "available"
            if entry.is_available
            else f"unavailable ({len(entry.problems)} problems)"
        )
        click.echo(f"{entry.id}\t{name}\t{entry.source}\t{state}\t{entry.folder}")


@main.command("validate")
@click.argument("theme_ids", nargs=-1)
@click.option(
    "--repo-root",
    default=".",
    show_default=True,
    type=click.Path(file_okay=False),
    help="The workspace root",
)
def validate_themes(theme_ids: Sequence[str], repo_root: str) -> None:
    """Check themes against the contract (every theme when none is named); exits 1 when any has a problem."""
    catalog = _load_catalog(repo_root)
    is_any_problem = False
    entries: list[ThemeEntry] = []
    for theme_id in theme_ids:
        found = catalog.find_all(theme_id)
        if not found:
            click.echo(f"{theme_id}: no theme folder has this id", err=True)
            is_any_problem = True
        entries.extend(found)
    for entry in entries if theme_ids else catalog.entries:
        label = _validation_label(catalog, entry)
        if entry.is_available:
            click.echo(f"{label}: ok")
            continue
        is_any_problem = True
        click.echo(f"{label}: {len(entry.problems)} problems")
        for problem in entry.problems:
            click.echo(f"  {problem}")
    if is_any_problem:
        sys.exit(1)


@pure
def _validation_label(catalog: ThemeCatalog, entry: ThemeEntry) -> str:
    """The theme's id, and its folder too when another folder has the same id."""
    if len(catalog.find_all(entry.id)) > 1:
        return f"{entry.id} ({entry.folder})"
    return str(entry.id)


@pure
def _starter_manifest(
    theme_id: ThemeId, name: ThemeName, description: ThemeDescription, base: ThemeId
) -> str:
    return (
        f"contract = {CONTRACT_VERSION}\n"
        f'id = "{theme_id}"\n'
        f"name = {json.dumps(str(name))}\n"
        f"description = {json.dumps(str(description))}\n"
        f'base = "{base}"\n'
        "\n[styles]\n"
        'files = ["tokens.css", "parts.css"]\n'
        "\n# [chrome] and [icons] are taken from the base until this theme sets its own.\n"
    )


@main.command("new")
@click.argument("theme_id")
@click.option("--name", required=True, help="What the theme picker shows")
@click.option("--description", required=True, help="One sentence about the theme")
@click.option(
    "--base",
    default=str(STANDARD_THEME_ID),
    show_default=True,
    help="The theme to start from",
)
@click.option(
    "--repo-root",
    default=".",
    show_default=True,
    type=click.Path(file_okay=False),
    help="The workspace root",
)
def new_theme(
    theme_id: str, name: str, description: str, base: str, repo_root: str
) -> None:
    """Lay out a new theme folder under themes/, starting from a base theme."""
    try:
        parsed_id = ThemeId(theme_id)
        parsed_name = ThemeName(name)
        parsed_description = ThemeDescription(description)
        parsed_base = ThemeId(base)
    except InvalidThemeValueError as error:
        raise click.ClickException(str(error)) from None
    catalog = _load_catalog(repo_root)
    _require_theme(catalog, parsed_base)
    if catalog.find(parsed_id) is not None:
        raise click.ClickException(f"a theme {theme_id!r} already exists")
    folder = Path(repo_root).resolve() / WORKSPACE_THEMES_DIRECTORY / parsed_id
    try:
        folder.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise click.ClickException(
            str(ThemeFolderExistsError(f"{folder} already exists"))
        ) from None
    (folder / THEME_MANIFEST_FILENAME).write_text(
        _starter_manifest(parsed_id, parsed_name, parsed_description, parsed_base),
        encoding="utf-8",
    )
    (folder / "tokens.css").write_text(
        _STARTER_TOKENS.format(name=parsed_name), encoding="utf-8"
    )
    (folder / "parts.css").write_text(
        _STARTER_PARTS.format(name=parsed_name), encoding="utf-8"
    )
    click.echo(str(folder))


@main.command("bundle")
@click.argument("theme_id")
@click.option(
    "--repo-root",
    default=".",
    show_default=True,
    type=click.Path(file_okay=False),
    help="The workspace root",
)
def print_bundle(theme_id: str, repo_root: str) -> None:
    """Print the bundle a page loads for a theme."""
    click.echo(
        build_bundle_css(_require_theme(_load_catalog(repo_root), theme_id)), nl=False
    )


@main.group("icon")
def icon_group() -> None:
    """Make and check a theme's icons, with whatever image model is at hand."""


def _app_names(repo_root: Path) -> list[str]:
    return [
        str(manifest.name)
        for manifest in read_app_manifests(repo_root)
        if not manifest.internal
    ]


def _apps_without_icon(
    catalog: ThemeCatalog, entry: ThemeEntry, app_names: Sequence[str]
) -> list[str]:
    """The apps a theme draws no icon for. None for a theme whose icons are the standard ones (each app's own
    icon.svg): the standard theme, and a theme that takes its icons from it."""
    icons = _require_icons(entry)
    standard = catalog.find(STANDARD_THEME_ID)
    if standard is not None and icons.spec_folder == standard.folder:
        return []
    covered = {*icons.curated_by_app, *icons.generated_by_app}
    return [name for name in app_names if name not in covered]


@icon_group.command("spec")
@click.argument("theme_id")
@click.option(
    "--repo-root",
    default=".",
    show_default=True,
    type=click.Path(file_okay=False),
    help="The workspace root",
)
def icon_spec(theme_id: str, repo_root: str) -> None:
    """Print what a model needs to draw this theme's icons, and which apps still lack one."""
    root = Path(repo_root).resolve()
    catalog = _load_catalog(repo_root)
    entry = _require_theme(catalog, theme_id)
    icons = _require_icons(entry)
    apps_without_icon = _apps_without_icon(catalog, entry, _app_names(root))
    report = {
        "theme": entry.id,
        "spec": icons.spec.model_dump(mode="json"),
        "guide": str(icons.spec_folder / icons.spec.guide),
        "references": [
            str(icons.spec_folder / reference) for reference in icons.spec.references
        ],
        "fallback": str(icons.fallback) if icons.fallback else None,
        "draw_at_scale": pixel_scale_hint(icons.spec),
        "apps_without_icon": apps_without_icon,
    }
    click.echo(json.dumps(report, indent=2))


@icon_group.command("missing")
@click.argument("theme_ids", nargs=-1)
@click.option(
    "--repo-root",
    default=".",
    show_default=True,
    type=click.Path(file_okay=False),
    help="The workspace root",
)
def icon_missing(theme_ids: tuple[str, ...], repo_root: str) -> None:
    """List, for every available theme (or those named), the apps it draws no icon for; exits 1 when any."""
    root = Path(repo_root).resolve()
    catalog = _load_catalog(repo_root)
    entries = (
        [_require_theme(catalog, theme_id) for theme_id in theme_ids]
        if theme_ids
        else [entry for entry in catalog.entries if entry.is_available]
    )
    app_names = _app_names(root)
    missing_by_theme = {
        str(entry.id): _apps_without_icon(catalog, entry, app_names)
        for entry in entries
    }
    lines = [
        f"{theme_id}: {', '.join(apps)}"
        for theme_id, apps in missing_by_theme.items()
        if apps
    ]
    if not lines:
        click.echo("every theme has an icon for every app")
        return
    click.echo("\n".join(lines))
    sys.exit(1)


@icon_group.command("fit")
@click.argument("candidate", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--theme", "theme_id", required=True, help="The theme whose limits to fit"
)
@click.option(
    "--out",
    "output_path",
    required=True,
    type=click.Path(dir_okay=False),
    help="Where to write the png",
)
@click.option(
    "--preview",
    "preview_path",
    type=click.Path(dir_okay=False),
    help="Also write an enlarged copy to look at",
)
@click.option(
    "--repo-root",
    default=".",
    show_default=True,
    type=click.Path(file_okay=False),
    help="The workspace root",
)
def icon_fit(
    candidate: str,
    theme_id: str,
    output_path: str,
    preview_path: str | None,
    repo_root: str,
) -> None:
    """Force a drawn candidate onto the theme's icon limits, then report what still misses them."""
    icons = _require_icons(_require_theme(_load_catalog(repo_root), theme_id))
    if icons.spec.format != IconFormat.PNG:
        raise click.ClickException(
            f"theme {theme_id!r} draws svg icons; write the svg yourself, following its guide, and check it with "
            "`icon check`"
        )
    try:
        fitted = fit_icon_image(load_icon_image(Path(candidate)), icons.spec)
    except IconImageError as error:
        raise click.ClickException(str(error)) from None
    _save_fitted(fitted, icons, output_path, preview_path)
    _report_icon_problems(check_icon_image(fitted, icons.spec), output_path)


@pure
def _parse_legend(legend: str) -> dict[str, HexColor]:
    color_by_character: dict[str, HexColor] = {}
    for pair in legend.split(","):
        character, separator, color = pair.strip().partition("=")
        if not separator or len(character) != 1:
            raise InvalidThemeValueError(
                f"legend entry {pair!r} must be <one character>=#rrggbb"
            )
        color_by_character[character] = HexColor(color.strip())
    return color_by_character


@icon_group.command("grid")
@click.argument("grid_file", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--theme", "theme_id", required=True, help="The theme whose limits to fit"
)
@click.option(
    "--legend",
    required=True,
    help="Colors by character, as k=#000000,w=#ffffff ('.' is clear)",
)
@click.option(
    "--out",
    "output_path",
    required=True,
    type=click.Path(dir_okay=False),
    help="Where to write the png",
)
@click.option(
    "--preview",
    "preview_path",
    type=click.Path(dir_okay=False),
    help="Also write an enlarged copy to look at",
)
@click.option(
    "--repo-root",
    default=".",
    show_default=True,
    type=click.Path(file_okay=False),
    help="The workspace root",
)
def icon_grid(
    grid_file: str,
    theme_id: str,
    legend: str,
    output_path: str,
    preview_path: str | None,
    repo_root: str,
) -> None:
    """Draw a pixel grid written as text (one character per pixel), fitted to the theme's limits."""
    icons = _require_icons(_require_theme(_load_catalog(repo_root), theme_id))
    try:
        drawn = render_pixel_grid(
            Path(grid_file).read_text(encoding="utf-8"), _parse_legend(legend)
        )
        fitted = fit_icon_image(drawn, icons.spec)
    except (IconImageError, InvalidThemeValueError) as error:
        raise click.ClickException(str(error)) from None
    _save_fitted(fitted, icons, output_path, preview_path)
    _report_icon_problems(check_icon_image(fitted, icons.spec), output_path)


def _save_fitted(
    fitted: Image.Image,
    icons: ThemeIcons,
    output_path: str,
    preview_path: str | None,
) -> None:
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    fitted.save(output_path, format="PNG")
    if preview_path is not None:
        Path(preview_path).parent.mkdir(parents=True, exist_ok=True)
        preview_icon_image(fitted, icons.spec).save(preview_path, format="PNG")


def _report_icon_problems(problems: Sequence[str], label: str) -> None:
    if not problems:
        click.echo(f"{label}: ok")
        return
    click.echo(f"{label}: {len(problems)} problems")
    for problem in problems:
        click.echo(f"  {problem}")
    sys.exit(1)


def _icon_problems(path: Path, icons: ThemeIcons) -> list[str]:
    if icons.spec.format == IconFormat.SVG:
        return check_svg_icon(path)
    try:
        return check_icon_image(load_icon_image(path), icons.spec)
    except IconImageError as error:
        return [str(error)]


@icon_group.command("check")
@click.argument("icon_path", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--theme", "theme_id", required=True, help="The theme whose limits to check against"
)
@click.option(
    "--repo-root",
    default=".",
    show_default=True,
    type=click.Path(file_okay=False),
    help="The workspace root",
)
def icon_check(icon_path: str, theme_id: str, repo_root: str) -> None:
    """Check an icon against the theme's limits; exits 1 when it misses any."""
    icons = _require_icons(_require_theme(_load_catalog(repo_root), theme_id))
    _report_icon_problems(_icon_problems(Path(icon_path), icons), icon_path)


@icon_group.command("install")
@click.argument("icon_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--theme", "theme_id", required=True, help="The theme the icon is for")
@click.option(
    "--app",
    "app_name",
    required=True,
    help="The app the icon is for, by its registered name",
)
@click.option(
    "--repo-root",
    default=".",
    show_default=True,
    type=click.Path(file_okay=False),
    help="The workspace root",
)
def icon_install(icon_path: str, theme_id: str, app_name: str, repo_root: str) -> None:
    """Put a checked icon where the shell finds it: a workspace theme's folder, or data/ for a built-in theme."""
    root = Path(repo_root).resolve()
    entry = _require_theme(_load_catalog(repo_root), theme_id)
    icons = _require_icons(entry)
    if app_name not in _app_names(root) and app_name != FALLBACK_ICON_STEM:
        raise click.ClickException(f"there is no app {app_name!r} here")
    # An icon installed for a built-in theme only fills a gap: the shell draws the theme's shipped icon first, and
    # takes the generic program icon from the theme folder alone.
    if entry.source == ThemeSource.BUILTIN and (
        app_name == FALLBACK_ICON_STEM or app_name in icons.curated_by_app
    ):
        raise click.ClickException(
            f"theme {theme_id!r} is built in and ships its own {app_name!r} icon, which the shell draws instead of "
            f"an installed one; to change it, make a workspace theme based on it (workspace-themes new <id> "
            f"--base {theme_id}) and install the icon there"
        )
    problems = _icon_problems(Path(icon_path), icons)
    if problems:
        _report_icon_problems(problems, icon_path)
    destination = icon_install_path(entry, root, app_name, icons.spec.format)
    install_icon_file(Path(icon_path), destination)
    click.echo(str(destination))


@main.command("lint-app")
@click.argument("app_package")
@click.option(
    "--repo-root",
    default=".",
    show_default=True,
    type=click.Path(file_okay=False),
    help="The workspace root",
)
def lint_app(app_package: str, repo_root: str) -> None:
    """Find literal colors, fonts, and shadows in a tokens-mode app's frontend; exits 1 when there are any."""
    app_directory = Path(repo_root).resolve().joinpath(*APPS_DIRECTORY, app_package)
    manifest_path = app_directory / MANIFEST_FILENAME
    try:
        manifest = load_manifest(manifest_path)
    except ManifestLoadError as error:
        raise click.ClickException(str(error)) from None
    if manifest.theming.mode != ThemingMode.TOKENS:
        click.echo(
            f"{app_package}: theming mode is {manifest.theming.mode}; only tokens-mode apps are linted"
        )
        return
    findings = lint_app_frontend(app_directory)
    for finding in findings:
        click.echo(
            f"{finding.path.relative_to(app_directory)}:{finding.line_number}: {finding.message}"
        )
        click.echo(f"    {finding.line}")
    if findings:
        sys.exit(1)
    click.echo(f"{app_package}: ok")
