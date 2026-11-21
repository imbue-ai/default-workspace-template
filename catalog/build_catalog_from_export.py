"""Build ``new-tab-templates.json`` from a published-templates export and published apps.

The export is the ``inspirations.json`` document the mind-sketches prototype ingested: a
list of published templates (``inspirations``), the playable samples, and the shelves that
group them for browsing. This script turns it into the catalog document the workspace's
New Tab page reads (format 1, see ``catalog/README.md``), and copies each template's
drawing beside it:

    python3 catalog/build_catalog_from_export.py \\
        --export path/to/inspirations.json \\
        --thumbnails path/to/thumbnails \\
        --popular inbox-digest-review,plain-text-gtd,...

Only templates with a repository are kept (a template is adopted from its repository, so
one without cannot be), one entry per slug (the more recently updated of two repositories
publishing the same slug), and only the shelves that still hold something. A "Most
popular" shelf built from ``--popular`` leads the shelves.

A published app is listed from a checkout of its published repository, read from the
``app.toml`` at its root, with its icon copied beside the catalog:

    python3 catalog/build_catalog_from_export.py \\
        --app https://github.com/someone/weather-desk=path/to/weather-desk \\
        --popular inbox-digest-review,app:weather-desk,...

Without ``--export`` the templates and shelves already in the catalog are kept, so an app
is added, refreshed, or dropped (``--drop-app``) without the export in hand. The apps
already listed are kept either way; ``--app`` adds one or replaces the one of its name. A
shelf names an app as ``app:<name>`` among its template slugs: ``--popular`` takes such
entries, and a shelf rebuilt from an export keeps the app entries it held.

Stdlib only, so it runs from any checkout without a sync.
"""

import argparse
import json
import shutil
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

_CATALOG_DIRECTORY = Path(__file__).resolve().parent
_CATALOG_PATH = _CATALOG_DIRECTORY / "new-tab-templates.json"
_THUMBNAILS_DIRECTORY_NAME = "thumbnails"
_APP_ICONS_DIRECTORY_NAME = "app-icons"

_CATALOG_FORMAT = 1
_POPULAR_SHELF_KEY = "popular"
_POPULAR_SHELF_TITLE = "Most popular"

# What a shelf entry naming an app starts with; the app's name follows. A template's slug
# never holds a colon, so a reader that knows no apps finds no template by that entry and
# drops it from the row.
_APP_SHELF_ENTRY_PREFIX = "app:"

_APP_MANIFEST_FILENAME = "app.toml"
# The icon of an app whose manifest names none.
_DEFAULT_APP_ICON = "icon.svg"
_APP_ICON_SUFFIX = ".svg"

# Templates left out of the shipped catalog whatever the export says: a title that does not
# belong on a first-run surface.
_EXCLUDED_SLUGS = frozenset({"fuck-slack"})


class CatalogBuildError(Exception):
    """The catalog cannot be built from what the builder was given."""


def _owner_of(repository_full_name: str) -> str:
    return repository_full_name.split("/")[0]


def _thumbnail_stem(entry: dict[str, Any]) -> str:
    return f"{_owner_of(entry['repository_full_name'])}--{entry['slug']}"


def _newest_per_slug(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per slug, keeping the most recently updated repository; export order otherwise."""
    newest_by_slug: dict[str, dict[str, Any]] = {}
    for entry in entries:
        current = newest_by_slug.get(entry["slug"])
        if current is None or entry["updated_at"] > current["updated_at"]:
            newest_by_slug[entry["slug"]] = entry
    return [entry for entry in entries if newest_by_slug[entry["slug"]] is entry]


def _catalog_template(entry: dict[str, Any], thumbnails_source: Path) -> dict[str, Any]:
    thumbnail_name = f"{_thumbnail_stem(entry)}.svg"
    has_thumbnail = (thumbnails_source / thumbnail_name).is_file()
    return {
        "slug": entry["slug"],
        "title": entry["title"],
        "description": entry["description"],
        "what_it_is": entry["what_it_is"],
        "author": _owner_of(entry["repository_full_name"]),
        "repository_url": entry["repository_url"],
        "thumbnail": f"{_THUMBNAILS_DIRECTORY_NAME}/{thumbnail_name}"
        if has_thumbnail
        else "",
        "version": entry["version"],
        "updated_at": entry["updated_at"],
        "required_accounts": [
            {"scope": account["service"], "permission": account["permission"]}
            for account in entry["required_accounts"]
        ],
        "required_secrets": entry["required_secrets"],
        "needs_ai": entry["needs_ai"],
        "apt_packages": entry["apt_packages"],
        "choices": entry["choices"],
    }


def _repository_owner(repository_url: str) -> str:
    """Who publishes the repository at ``repository_url``: the first segment of its path."""
    parts = urlsplit(repository_url)
    segments = [segment for segment in parts.path.split("/") if segment]
    if parts.scheme != "https" or not parts.netloc or len(segments) != 2:
        raise CatalogBuildError(
            f"{repository_url!r} is not a repository URL of the form https://<host>/<owner>/<name>"
        )
    return segments[0]


def app_shelf_entry(app_name: str) -> str:
    """How a shelf names the app ``app_name``."""
    return f"{_APP_SHELF_ENTRY_PREFIX}{app_name}"


def _app_icon_source(
    app_directory: Path, manifest_path: Path, icon: object
) -> Path | None:
    """The file the manifest's ``icon`` names, or None when the checkout holds none.

    The checkout is someone else's repository and the file is copied into the catalog, so
    the icon is held to the manifest's own rule (a relative ``.svg`` path) and has to be a
    file of the checkout itself: a path or a link that leads out of it is refused.
    """
    if not isinstance(icon, str) or not icon:
        raise CatalogBuildError(
            f"{manifest_path} names an icon that is no path: {icon!r}"
        )
    if Path(icon).is_absolute() or Path(icon).suffix.lower() != _APP_ICON_SUFFIX:
        raise CatalogBuildError(
            f"{manifest_path} names the icon {icon!r}, which is not a relative {_APP_ICON_SUFFIX} path"
        )
    icon_source = app_directory / icon
    if not icon_source.is_file():
        return None
    if not icon_source.resolve().is_relative_to(app_directory.resolve()):
        raise CatalogBuildError(
            f"the icon {icon!r} that {manifest_path} names leads outside the app's checkout"
        )
    return icon_source


def read_catalog_app(
    repository_url: str, app_directory: Path
) -> tuple[dict[str, Any], Path | None]:
    """The catalog's entry for the app checked out at ``app_directory`` and published at
    ``repository_url``, read from its ``app.toml``, and the icon file the entry's ``icon``
    is a copy of (None for an app with no icon). Keys the manifest carries beyond the ones
    read here are passed over, as every reader of another workspace's manifest does."""
    owner = _repository_owner(repository_url)
    manifest_path = app_directory / _APP_MANIFEST_FILENAME
    try:
        manifest = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise CatalogBuildError(f"cannot read {manifest_path}: {e}") from e
    for required_key in ("name", "display_name"):
        value = manifest.get(required_key)
        if not isinstance(value, str) or not value:
            raise CatalogBuildError(f"{manifest_path} names no {required_key}")
    name = manifest["name"]
    if ":" in name or "/" in name:
        raise CatalogBuildError(
            f"{manifest_path} names the app {name!r}, which is not an app name"
        )
    icon_source = _app_icon_source(
        app_directory, manifest_path, manifest.get("icon", _DEFAULT_APP_ICON)
    )
    try:
        entry = {
            "name": name,
            "title": manifest["display_name"],
            "description": manifest.get("description", ""),
            "author": owner,
            "repository_url": repository_url,
            "icon": f"{_APP_ICONS_DIRECTORY_NAME}/{owner}--{name}{icon_source.suffix}"
            if icon_source is not None
            else "",
            "version": manifest.get("version", ""),
            "required_accounts": [
                {"scope": permission["scope"], "permission": permission["permission"]}
                for permission in manifest.get("permissions", [])
            ],
            "required_secrets": [
                variable
                for secret in manifest.get("secrets", [])
                for variable in secret["variables"]
            ],
            "needs_ai": "llm" in manifest,
            "apt_packages": list(manifest.get("environment", {}).get("apt", [])),
        }
    except (KeyError, TypeError, AttributeError) as e:
        raise CatalogBuildError(
            f"{manifest_path} declares what the app needs in a shape this builder cannot read: {e!r}"
        ) from e
    return entry, icon_source


def _merged_apps(
    existing_apps: list[dict[str, Any]],
    read_apps: list[dict[str, Any]],
    dropped_app_names: list[str],
) -> list[dict[str, Any]]:
    """The apps already listed with each read app added, or put in place of the one of its
    name, and the dropped ones removed; catalog order, new apps last."""
    read_by_name: dict[str, dict[str, Any]] = {}
    for app in read_apps:
        if app["name"] in read_by_name:
            raise CatalogBuildError(f"two apps given are named {app['name']!r}")
        read_by_name[app["name"]] = app
    existing_names = {app["name"] for app in existing_apps}
    for name in dropped_app_names:
        if name not in existing_names:
            raise CatalogBuildError(f"the catalog lists no app named {name!r} to drop")
        if name in read_by_name:
            raise CatalogBuildError(f"the app {name!r} is both given and dropped")
    merged = [read_by_name.get(app["name"], app) for app in existing_apps]
    merged += [app for app in read_apps if app["name"] not in existing_names]
    return [app for app in merged if app["name"] not in dropped_app_names]


def _is_app_entry(shelf_entry: str) -> bool:
    return shelf_entry.startswith(_APP_SHELF_ENTRY_PREFIX)


def _shelves_from_export(
    export: dict[str, Any], existing_shelves: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The export's shelves. An export knows no apps, so each shelf keeps the app entries
    the catalog's shelf of the same key held, after its templates."""
    app_entries_by_key = {
        shelf["key"]: [entry for entry in shelf["slugs"] if _is_app_entry(entry)]
        for shelf in existing_shelves
    }
    return [
        {
            "key": shelf["key"],
            "title": shelf["title"],
            "slugs": [*shelf["slugs"], *app_entries_by_key.get(shelf["key"], [])],
        }
        for shelf in export["shelves"]
    ]


def build_catalog(
    existing: dict[str, Any] | None,
    export: dict[str, Any] | None,
    thumbnails_source: Path | None,
    popular_entries: list[str] | None,
    read_apps: list[dict[str, Any]],
    dropped_app_names: list[str],
    generated_at: str,
) -> dict[str, Any]:
    """The catalog document. ``existing`` is the catalog being replaced (None for a first
    build); ``export`` the templates export, or None to keep the existing templates and
    shelves; ``popular_entries`` the leading shelf's entries, or None to leave that shelf as
    the rest of the inputs make it; ``read_apps`` the entries of the apps given."""
    existing_shelves: list[dict[str, Any]] = (
        existing.get("shelves", []) if existing is not None else []
    )
    if export is not None:
        if thumbnails_source is None:
            raise CatalogBuildError("an export needs the directory of its drawings")
        adoptable = [
            entry
            for entry in export["inspirations"]
            if entry["repository_full_name"] and entry["slug"] not in _EXCLUDED_SLUGS
        ]
        templates = [
            _catalog_template(entry, thumbnails_source)
            for entry in _newest_per_slug(adoptable)
        ]
        # A build from an export without --popular has no curated shelf, as it never had.
        leading_entries = popular_entries if popular_entries is not None else []
        shelves = _shelves_from_export(export, existing_shelves)
    elif existing is not None:
        templates = existing["templates"]
        leading_entries = popular_entries
        shelves = existing_shelves
    else:
        raise CatalogBuildError(
            "there is no catalog to keep templates from; pass --export"
        )
    apps = _merged_apps(
        existing.get("apps", []) if existing is not None else [],
        read_apps,
        dropped_app_names,
    )

    # The curated shelf leads; the other shelves follow with the entries the catalog no
    # longer carries dropped, and a shelf that ends up empty dropped with them.
    if leading_entries is not None:
        shelves = [
            {
                "key": _POPULAR_SHELF_KEY,
                "title": _POPULAR_SHELF_TITLE,
                "slugs": leading_entries,
            },
            *(shelf for shelf in shelves if shelf["key"] != _POPULAR_SHELF_KEY),
        ]
    known_entries = {template["slug"] for template in templates} | {
        app_shelf_entry(app["name"]) for app in apps
    }
    kept_shelves: list[dict[str, Any]] = []
    for shelf in shelves:
        entries = [entry for entry in shelf["slugs"] if entry in known_entries]
        if entries:
            kept_shelves.append(
                {"key": shelf["key"], "title": shelf["title"], "slugs": entries}
            )

    return {
        "format": _CATALOG_FORMAT,
        "generated_at": generated_at,
        "templates": templates,
        "apps": apps,
        "shelves": kept_shelves,
    }


def copy_thumbnails(
    catalog: dict[str, Any], thumbnails_source: Path, catalog_directory: Path
) -> int:
    (catalog_directory / _THUMBNAILS_DIRECTORY_NAME).mkdir(parents=True, exist_ok=True)
    copied = 0
    for template in catalog["templates"]:
        if not template["thumbnail"]:
            continue
        shutil.copyfile(
            thumbnails_source / Path(template["thumbnail"]).name,
            catalog_directory / template["thumbnail"],
        )
        copied += 1
    return copied


def copy_app_icons(
    read_apps: list[tuple[dict[str, Any], Path | None]], catalog_directory: Path
) -> int:
    """Copy each read app's icon to where its catalog entry says it is."""
    copied = 0
    for app, icon_source in read_apps:
        if icon_source is None:
            continue
        destination = catalog_directory / app["icon"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(icon_source, destination)
        copied += 1
    return copied


def remove_dropped_app_icons(
    existing: dict[str, Any] | None, catalog: dict[str, Any], catalog_directory: Path
) -> None:
    """Delete the icon of every app the new catalog no longer points at."""
    kept_icons = {app["icon"] for app in catalog["apps"]}
    for app in existing.get("apps", []) if existing is not None else []:
        if app.get("icon") and app["icon"] not in kept_icons:
            (catalog_directory / app["icon"]).unlink(missing_ok=True)


def _parse_app_argument(argument: str) -> tuple[str, Path]:
    repository_url, separator, directory = argument.partition("=")
    if not separator or not repository_url or not directory:
        raise CatalogBuildError(
            f"--app takes <repository url>=<path to a checkout of the app>, not {argument!r}"
        )
    return repository_url, Path(directory)


def _now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def run(args: argparse.Namespace) -> None:
    catalog_path: Path = args.catalog
    catalog_directory = catalog_path.parent
    existing = (
        json.loads(catalog_path.read_text(encoding="utf-8"))
        if catalog_path.is_file()
        else None
    )
    export = (
        json.loads(args.export.read_text(encoding="utf-8"))
        if args.export is not None
        else None
    )
    popular_entries = (
        [entry for entry in args.popular.split(",") if entry]
        if args.popular is not None
        else None
    )
    read_apps = [
        read_catalog_app(*_parse_app_argument(argument)) for argument in args.app
    ]
    catalog = build_catalog(
        existing,
        export,
        args.thumbnails,
        popular_entries,
        [app for app, _ in read_apps],
        args.drop_app,
        _now(),
    )

    copied_thumbnails = (
        copy_thumbnails(catalog, args.thumbnails, catalog_directory)
        if export is not None
        else 0
    )
    copied_icons = copy_app_icons(read_apps, catalog_directory)
    remove_dropped_app_icons(existing, catalog, catalog_directory)
    catalog_path.write_text(
        json.dumps(catalog, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(
        f"wrote {len(catalog['templates'])} templates and {len(catalog['apps'])} apps "
        f"in {len(catalog['shelves'])} shelves to {catalog_path}"
    )
    print(
        f"copied {copied_thumbnails} thumbnails and {copied_icons} app icons "
        f"to {catalog_directory}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--export",
        type=Path,
        default=None,
        help="The inspirations.json export to convert; without it the catalog's templates "
        "and shelves are kept",
    )
    parser.add_argument(
        "--thumbnails",
        type=Path,
        default=None,
        help="Directory of <owner>--<slug>.svg drawings to copy; required with --export",
    )
    parser.add_argument(
        "--popular",
        default=None,
        help="Comma-separated entries for the leading 'Most popular' shelf, in order: "
        "template slugs, and apps as app:<name>",
    )
    parser.add_argument(
        "--app",
        action="append",
        default=[],
        metavar="REPOSITORY_URL=CHECKOUT",
        help="A published app to list: its published repository's URL and a checkout of "
        "it, whose app.toml and icon are read; repeatable",
    )
    parser.add_argument(
        "--drop-app",
        action="append",
        default=[],
        metavar="NAME",
        help="An app to take out of the catalog, by name; repeatable",
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        default=_CATALOG_PATH,
        help="The catalog file to write; drawings and icons go beside it",
    )
    args = parser.parse_args()
    if args.export is not None and args.thumbnails is None:
        parser.error("--export needs --thumbnails")
    try:
        run(args)
    except CatalogBuildError as e:
        parser.error(str(e))


if __name__ == "__main__":
    main()
