"""Build ``new-tab-templates.json`` from a published-templates export.

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
popular" shelf built from ``--popular`` leads the shelves. Stdlib only, so it runs from any
checkout without a sync.
"""

import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_CATALOG_DIRECTORY = Path(__file__).resolve().parent
_CATALOG_PATH = _CATALOG_DIRECTORY / "new-tab-templates.json"
_THUMBNAILS_DIRECTORY = _CATALOG_DIRECTORY / "thumbnails"

_CATALOG_FORMAT = 1
_POPULAR_SHELF_KEY = "popular"
_POPULAR_SHELF_TITLE = "Most popular"

# Templates left out of the shipped catalog whatever the export says: a title that does not
# belong on a first-run surface.
_EXCLUDED_SLUGS = frozenset({"fuck-slack"})


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
        "thumbnail": f"{_THUMBNAILS_DIRECTORY.name}/{thumbnail_name}"
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


def build_catalog(
    export: dict[str, Any], thumbnails_source: Path, popular_slugs: list[str]
) -> dict[str, Any]:
    adoptable = [
        entry
        for entry in export["inspirations"]
        if entry["repository_full_name"] and entry["slug"] not in _EXCLUDED_SLUGS
    ]
    templates = [
        _catalog_template(entry, thumbnails_source)
        for entry in _newest_per_slug(adoptable)
    ]
    known_slugs = {template["slug"] for template in templates}

    # The curated shelf leads; the export's shelves follow with the slugs the catalog no longer
    # carries dropped, and a shelf that ends up empty dropped with them.
    shelves: list[dict[str, Any]] = []
    popular = [slug for slug in popular_slugs if slug in known_slugs]
    if popular:
        shelves.append(
            {"key": _POPULAR_SHELF_KEY, "title": _POPULAR_SHELF_TITLE, "slugs": popular}
        )
    for shelf in export["shelves"]:
        slugs = [slug for slug in shelf["slugs"] if slug in known_slugs]
        if slugs:
            shelves.append(
                {"key": shelf["key"], "title": shelf["title"], "slugs": slugs}
            )

    return {
        "format": _CATALOG_FORMAT,
        "generated_at": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "templates": templates,
        "shelves": shelves,
    }


def copy_thumbnails(catalog: dict[str, Any], thumbnails_source: Path) -> int:
    _THUMBNAILS_DIRECTORY.mkdir(parents=True, exist_ok=True)
    copied = 0
    for template in catalog["templates"]:
        if not template["thumbnail"]:
            continue
        shutil.copyfile(
            thumbnails_source / Path(template["thumbnail"]).name,
            _CATALOG_DIRECTORY / template["thumbnail"],
        )
        copied += 1
    return copied


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--export",
        type=Path,
        required=True,
        help="The inspirations.json export to convert",
    )
    parser.add_argument(
        "--thumbnails",
        type=Path,
        required=True,
        help="Directory of <owner>--<slug>.svg drawings to copy",
    )
    parser.add_argument(
        "--popular",
        default="",
        help="Comma-separated slugs for the leading 'Most popular' shelf, in order",
    )
    args = parser.parse_args()

    export = json.loads(args.export.read_text(encoding="utf-8"))
    popular_slugs = [slug for slug in args.popular.split(",") if slug]
    catalog = build_catalog(export, args.thumbnails, popular_slugs)
    copied = copy_thumbnails(catalog, args.thumbnails)
    _CATALOG_PATH.write_text(
        json.dumps(catalog, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(
        f"wrote {len(catalog['templates'])} templates in {len(catalog['shelves'])} shelves to {_CATALOG_PATH}"
    )
    print(f"copied {copied} thumbnails to {_THUMBNAILS_DIRECTORY}")


if __name__ == "__main__":
    main()
