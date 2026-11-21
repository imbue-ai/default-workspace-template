"""Tests for the catalog builder: an app's entry read from its ``app.toml``, the apps and shelves of a
rebuilt catalog, the command itself, and the promises the shipped catalog keeps to older readers."""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import build_catalog_from_export as builder
import pytest

_BUILDER_PATH = Path(builder.__file__)
_SHIPPED_CATALOG_PATH = _BUILDER_PATH.parent / "new-tab-templates.json"
_GENERATED_AT = "2026-11-21T00:00:00Z"
_WEATHER_URL = "https://github.com/someone/weather-desk"

_WEATHER_MANIFEST = """\
name = "weather-desk"
display_name = "Weather Desk"
description = "Forecasts for the places you care about."
icon = "icon.svg"
version = "1.2.0"
verified_template = "minds-v0.11.0"
a_key_from_a_newer_template = true

[run]
command = "weather-desk"

[[secrets]]
file = "weather-desk"
variables = ["OPENWEATHER_API_KEY", "WEATHER_HOME"]
note = "An API key from the account's settings page"

[[secrets]]
file = "weather-desk-alerts"
variables = ["ALERTS_TOKEN"]

[[permissions]]
scope = "slack-api"
permission = "slack-read-all"
note = "Reads the channel the forecast is posted to"

[[permissions]]
scope = "slack-api"
permission = "slack-write"

[llm]
method = "keyed"
note = ""

[environment]
apt = ["poppler-utils", "ffmpeg"]
npm_global = { prettier = "3.8.1" }
"""

_ICON = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 216 216"><rect width="216" height="216"/></svg>\n'


def _write_app_checkout(
    directory: Path, manifest: str = _WEATHER_MANIFEST, icon: str | None = _ICON
) -> Path:
    """A checkout of a published app's repository: its ``app.toml`` and, unless None, its icon."""
    directory.mkdir(parents=True)
    (directory / "app.toml").write_text(manifest)
    if icon is not None:
        (directory / "icon.svg").write_text(icon)
    return directory


def _template(slug: str) -> dict[str, Any]:
    return {
        "slug": slug,
        "title": slug.title(),
        "description": f"What {slug} does.",
        "what_it_is": "",
        "author": "someone",
        "repository_url": f"https://github.com/someone/{slug}",
        "thumbnail": f"thumbnails/someone--{slug}.svg",
        "version": "v1",
        "updated_at": "2026-07-16T20:48:42Z",
        "required_accounts": [],
        "required_secrets": [],
        "needs_ai": False,
        "apt_packages": [],
        "choices": [],
    }


def _app(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "title": name.title(),
        "description": "",
        "author": "someone",
        "repository_url": f"https://github.com/someone/{name}",
        "icon": f"app-icons/someone--{name}.svg",
        "version": "",
        "required_accounts": [],
        "required_secrets": [],
        "needs_ai": False,
        "apt_packages": [],
    }


def _catalog(
    templates: list[dict[str, Any]],
    shelves: list[dict[str, Any]],
    apps: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """A catalog as the builder wrote it; ``apps`` None is one written before apps could be published."""
    catalog: dict[str, Any] = {
        "format": 1,
        "generated_at": "2026-09-07T00:00:00Z",
        "templates": templates,
    }
    if apps is not None:
        catalog["apps"] = apps
    catalog["shelves"] = shelves
    return catalog


def _shelf(key: str, *entries: str) -> dict[str, Any]:
    return {"key": key, "title": key.title(), "slugs": list(entries)}


def _rebuilt(
    existing: dict[str, Any],
    read_apps: list[dict[str, Any]] | None = None,
    dropped_app_names: list[str] | None = None,
    popular_entries: list[str] | None = None,
) -> dict[str, Any]:
    """The catalog rebuilt with no export: its templates and shelves kept."""
    return builder.build_catalog(
        existing,
        None,
        None,
        popular_entries,
        read_apps or [],
        dropped_app_names or [],
        _GENERATED_AT,
    )


# an app's entry
def test_an_apps_entry_is_read_from_its_manifest(tmp_path: Path) -> None:
    checkout = _write_app_checkout(tmp_path / "weather-desk")
    entry, icon_source = builder.read_catalog_app(_WEATHER_URL, checkout)
    assert entry == {
        "name": "weather-desk",
        "title": "Weather Desk",
        "description": "Forecasts for the places you care about.",
        "author": "someone",
        "repository_url": _WEATHER_URL,
        "icon": "app-icons/someone--weather-desk.svg",
        "version": "1.2.0",
        "required_accounts": [
            {"scope": "slack-api", "permission": "slack-read-all"},
            {"scope": "slack-api", "permission": "slack-write"},
        ],
        "required_secrets": ["OPENWEATHER_API_KEY", "WEATHER_HOME", "ALERTS_TOKEN"],
        "needs_ai": True,
        "apt_packages": ["poppler-utils", "ffmpeg"],
    }
    assert icon_source == checkout / "icon.svg"


def test_an_app_that_declares_nothing_beyond_its_name_asks_for_nothing(
    tmp_path: Path,
) -> None:
    checkout = _write_app_checkout(
        tmp_path / "plain",
        manifest='name = "plain"\ndisplay_name = "Plain"\n',
        icon=None,
    )
    entry, icon_source = builder.read_catalog_app(
        "https://github.com/someone/plain", checkout
    )
    assert entry == {
        "name": "plain",
        "title": "Plain",
        "description": "",
        "author": "someone",
        "repository_url": "https://github.com/someone/plain",
        "icon": "",
        "version": "",
        "required_accounts": [],
        "required_secrets": [],
        "needs_ai": False,
        "apt_packages": [],
    }
    assert icon_source is None


def test_an_apps_icon_is_the_file_its_manifest_names(tmp_path: Path) -> None:
    checkout = _write_app_checkout(
        tmp_path / "drawn",
        manifest='name = "drawn"\ndisplay_name = "Drawn"\nicon = "art/tile.svg"\n',
        icon=None,
    )
    (checkout / "art").mkdir()
    (checkout / "art" / "tile.svg").write_text(_ICON)
    entry, icon_source = builder.read_catalog_app(
        "https://github.com/someone/drawn", checkout
    )
    assert entry["icon"] == "app-icons/someone--drawn.svg"
    assert icon_source == checkout / "art" / "tile.svg"


@pytest.mark.parametrize(
    ("repository_url", "manifest", "message"),
    [
        (
            "git@github.com:someone/weather-desk.git",
            _WEATHER_MANIFEST,
            "repository URL",
        ),
        ("https://github.com/someone", _WEATHER_MANIFEST, "repository URL"),
        (_WEATHER_URL, 'display_name = "Nameless"\n', "names no name"),
        (_WEATHER_URL, 'name = "untitled"\n', "names no display_name"),
        (_WEATHER_URL, 'name = "app:odd"\ndisplay_name = "Odd"\n', "not an app name"),
        (_WEATHER_URL, "name = ", "cannot read"),
        (
            _WEATHER_URL,
            'name = "odd"\ndisplay_name = "Odd"\nicon = "/etc/hostname"\n',
            "not a relative .svg path",
        ),
        (
            _WEATHER_URL,
            'name = "odd"\ndisplay_name = "Odd"\nicon = "notes.txt"\n',
            "not a relative .svg path",
        ),
        (
            _WEATHER_URL,
            'name = "odd"\ndisplay_name = "Odd"\nicon = 5\n',
            "an icon that is no path",
        ),
        (
            _WEATHER_URL,
            'name = "odd"\ndisplay_name = "Odd"\n[[permissions]]\nscope = "slack-api"\n',
            "cannot read",
        ),
    ],
)
def test_an_app_the_builder_cannot_list_is_refused_with_the_reason(
    tmp_path: Path, repository_url: str, manifest: str, message: str
) -> None:
    checkout = _write_app_checkout(tmp_path / "app", manifest=manifest)
    with pytest.raises(builder.CatalogBuildError, match=message):
        builder.read_catalog_app(repository_url, checkout)


def test_an_icon_that_leads_out_of_the_checkout_is_refused(tmp_path: Path) -> None:
    # A file of whoever runs the builder, which a checkout of someone else's app must not reach.
    (tmp_path / "private.svg").write_text("not the app's to publish\n")
    linked = _write_app_checkout(
        tmp_path / "linked",
        manifest='name = "linked"\ndisplay_name = "Linked"\n',
        icon=None,
    )
    (linked / "icon.svg").symlink_to(tmp_path / "private.svg")
    climbing = _write_app_checkout(
        tmp_path / "climbing",
        manifest='name = "climbing"\ndisplay_name = "Climbing"\nicon = "../private.svg"\n',
        icon=None,
    )
    for checkout in (linked, climbing):
        with pytest.raises(
            builder.CatalogBuildError, match="leads outside the app's checkout"
        ):
            builder.read_catalog_app(
                f"https://github.com/someone/{checkout.name}", checkout
            )


def test_a_checkout_with_no_manifest_is_refused(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    with pytest.raises(builder.CatalogBuildError, match="cannot read"):
        builder.read_catalog_app(_WEATHER_URL, tmp_path / "empty")


# the rebuilt catalog
def test_a_rebuild_without_an_export_keeps_the_templates_and_shelves_and_lists_the_app() -> (
    None
):
    existing = _catalog(
        [_template("inbox"), _template("orchard")],
        [_shelf("popular", "inbox"), _shelf("work", "orchard")],
    )
    catalog = _rebuilt(existing, read_apps=[_app("weather-desk")])
    assert list(catalog) == ["format", "generated_at", "templates", "apps", "shelves"]
    assert catalog["generated_at"] == _GENERATED_AT
    assert catalog["templates"] == existing["templates"]
    assert catalog["apps"] == [_app("weather-desk")]
    assert catalog["shelves"] == existing["shelves"]


def test_a_catalog_written_before_apps_gains_an_empty_apps_list() -> None:
    existing = _catalog([_template("inbox")], [_shelf("popular", "inbox")])
    assert "apps" not in existing
    assert _rebuilt(existing)["apps"] == []


def test_an_app_given_again_replaces_its_entry_in_place_and_a_new_one_goes_last() -> (
    None
):
    existing = _catalog(
        [_template("inbox")], [], apps=[_app("weather-desk"), _app("ledger")]
    )
    refreshed = {**_app("weather-desk"), "version": "2.0.0"}
    catalog = _rebuilt(existing, read_apps=[_app("notes"), refreshed])
    assert [(app["name"], app["version"]) for app in catalog["apps"]] == [
        ("weather-desk", "2.0.0"),
        ("ledger", ""),
        ("notes", ""),
    ]


def test_the_popular_shelf_names_apps_among_templates_and_drops_what_the_catalog_lacks() -> (
    None
):
    existing = _catalog(
        [_template("inbox"), _template("orchard")],
        [_shelf("popular", "orchard"), _shelf("work", "orchard")],
    )
    catalog = _rebuilt(
        existing,
        read_apps=[_app("weather-desk")],
        popular_entries=["inbox", "app:weather-desk", "app:not-listed", "gone"],
    )
    assert catalog["shelves"] == [
        {
            "key": "popular",
            "title": "Most popular",
            "slugs": ["inbox", "app:weather-desk"],
        },
        _shelf("work", "orchard"),
    ]


def test_dropping_an_app_takes_it_out_of_the_apps_and_every_shelf() -> None:
    existing = _catalog(
        [_template("inbox")],
        [
            _shelf("popular", "app:weather-desk", "inbox", "app:ledger"),
            _shelf("weather", "app:weather-desk"),
        ],
        apps=[_app("weather-desk"), _app("ledger")],
    )
    catalog = _rebuilt(existing, dropped_app_names=["weather-desk"])
    assert [app["name"] for app in catalog["apps"]] == ["ledger"]
    # The shelf that held only the dropped app is gone with it.
    assert catalog["shelves"] == [_shelf("popular", "inbox", "app:ledger")]


@pytest.mark.parametrize(
    ("read_apps", "dropped_app_names", "message"),
    [
        ([], ["never-listed"], "lists no app named"),
        ([_app("ledger")], ["ledger"], "both given and dropped"),
        ([_app("notes"), _app("notes")], [], "two apps given"),
    ],
)
def test_a_contradictory_request_is_refused(
    read_apps: list[dict[str, Any]], dropped_app_names: list[str], message: str
) -> None:
    existing = _catalog([_template("inbox")], [], apps=[_app("ledger")])
    with pytest.raises(builder.CatalogBuildError, match=message):
        _rebuilt(existing, read_apps=read_apps, dropped_app_names=dropped_app_names)


def test_a_first_build_needs_an_export() -> None:
    with pytest.raises(builder.CatalogBuildError, match="pass --export"):
        builder.build_catalog(
            None, None, None, None, [_app("weather-desk")], [], _GENERATED_AT
        )


def test_a_rebuild_from_an_export_keeps_the_apps_and_the_app_entries_of_its_shelves(
    tmp_path: Path,
) -> None:
    existing = _catalog(
        [_template("retired")],
        [
            _shelf("popular", "retired", "app:weather-desk"),
            _shelf("work", "retired", "app:weather-desk"),
            _shelf("gone-from-the-export", "app:weather-desk"),
        ],
        apps=[_app("weather-desk")],
    )
    export = {
        "inspirations": [
            {
                **_template("inbox"),
                "repository_full_name": "someone/inbox",
                "required_accounts": [
                    {"service": "slack-api", "permission": "slack-read-all"}
                ],
            },
            {**_template("no-repository"), "repository_full_name": ""},
        ],
        "shelves": [
            _shelf("work", "inbox", "no-repository"),
            _shelf("home", "inbox"),
        ],
    }
    catalog = builder.build_catalog(
        existing, export, tmp_path, ["inbox"], [], [], _GENERATED_AT
    )
    assert [template["slug"] for template in catalog["templates"]] == ["inbox"]
    assert catalog["templates"][0]["required_accounts"] == [
        {"scope": "slack-api", "permission": "slack-read-all"}
    ]
    assert catalog["apps"] == [_app("weather-desk")]
    assert catalog["shelves"] == [
        {"key": "popular", "title": "Most popular", "slugs": ["inbox"]},
        _shelf("work", "inbox", "app:weather-desk"),
        _shelf("home", "inbox"),
    ]


# the command
def _run_builder(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_BUILDER_PATH), *arguments],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_the_command_lists_an_app_and_copies_its_icon_beside_the_catalog(
    tmp_path: Path,
) -> None:
    catalog_path = tmp_path / "published" / "new-tab-templates.json"
    catalog_path.parent.mkdir()
    existing = _catalog([_template("inbox")], [_shelf("popular", "inbox")])
    catalog_path.write_text(json.dumps(existing))
    checkout = _write_app_checkout(tmp_path / "checkouts" / "weather-desk")

    result = _run_builder(
        "--catalog",
        str(catalog_path),
        "--app",
        f"{_WEATHER_URL}={checkout}",
        "--popular",
        "app:weather-desk,inbox",
    )

    assert result.returncode == 0, result.stderr
    assert "1 templates and 1 apps in 1 shelves" in result.stdout
    catalog = json.loads(catalog_path.read_text())
    assert catalog["templates"] == existing["templates"]
    (app,) = catalog["apps"]
    assert app["name"] == "weather-desk"
    assert app["repository_url"] == _WEATHER_URL
    assert app["icon"] == "app-icons/someone--weather-desk.svg"
    assert (catalog_path.parent / app["icon"]).read_text() == _ICON
    assert catalog["shelves"] == [
        {
            "key": "popular",
            "title": "Most popular",
            "slugs": ["app:weather-desk", "inbox"],
        }
    ]

    # Dropping it takes the entry, its shelf entry, and its icon away again.
    dropped = _run_builder("--catalog", str(catalog_path), "--drop-app", "weather-desk")
    assert dropped.returncode == 0, dropped.stderr
    after = json.loads(catalog_path.read_text())
    assert after["apps"] == []
    assert after["shelves"] == [
        {"key": "popular", "title": "Most popular", "slugs": ["inbox"]}
    ]
    assert not (catalog_path.parent / app["icon"]).exists()


def test_the_command_refuses_a_bad_request_and_leaves_the_catalog_alone(
    tmp_path: Path,
) -> None:
    catalog_path = tmp_path / "new-tab-templates.json"
    original = json.dumps(_catalog([_template("inbox")], []))
    catalog_path.write_text(original)

    result = _run_builder("--catalog", str(catalog_path), "--app", "no-separator")

    assert result.returncode == 2
    assert "--app takes" in result.stderr
    assert catalog_path.read_text() == original
    assert not (tmp_path / "app-icons").exists()


# the shipped catalog
def test_the_shipped_catalog_is_what_the_builder_writes_and_names_only_what_it_lists() -> (
    None
):
    """An older workspace reads this file too: it never reads ``apps``, and drops a shelf entry that is no
    template's slug, so an app must be named only behind ``app:`` and a slug must never hold a colon."""
    raw = _SHIPPED_CATALOG_PATH.read_text(encoding="utf-8")
    shipped = json.loads(raw)
    assert list(shipped) == ["format", "generated_at", "templates", "apps", "shelves"]
    assert raw == json.dumps(shipped, indent=2, ensure_ascii=False) + "\n"
    # Rebuilt from itself with nothing asked, the builder changes nothing: no entry is dangling.
    assert (
        builder.build_catalog(
            shipped, None, None, None, [], [], shipped["generated_at"]
        )
        == shipped
    )
    slugs = [template["slug"] for template in shipped["templates"]]
    assert all(":" not in slug for slug in slugs)
    assert len(set(slugs)) == len(slugs)
    app_names = [app["name"] for app in shipped["apps"]]
    assert len(set(app_names)) == len(app_names)
    for app in shipped["apps"]:
        assert not app["icon"] or (_SHIPPED_CATALOG_PATH.parent / app["icon"]).is_file()
