"""The catalog file and the app that reads it, held to each other: what the catalog builder writes for a
published app is what the catalog route answers, and the catalog the repository ships reads whole."""

import json
import subprocess
import sys
from pathlib import Path

from getting_started.template_catalog import catalog_wire
from getting_started.template_catalog import parse_template_catalog
from getting_started.testing import catalog_document
from getting_started.testing import catalog_template_document

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CATALOG_DIRECTORY = _REPO_ROOT / "catalog"
_BUILDER_PATH = _CATALOG_DIRECTORY / "build_catalog_from_export.py"
_SHIPPED_CATALOG_PATH = _CATALOG_DIRECTORY / "new-tab-templates.json"
_CATALOG_URL = "https://example.test/catalog/new-tab-templates.json"

_APP_MANIFEST = """\
name = "weather-desk"
display_name = "Weather Desk"
description = "Forecasts for the places you care about."
icon = "icon.svg"
version = "1.2.0"

[[secrets]]
file = "weather-desk"
variables = ["OPENWEATHER_API_KEY"]

[[permissions]]
scope = "slack-api"
permission = "slack-read-all"
note = "Reads the channel the forecast is posted to"

[llm]
method = "keyed"

[environment]
apt = ["ffmpeg"]
"""


def test_an_app_the_builder_lists_is_answered_by_the_catalog_route_as_its_manifest_says(tmp_path: Path) -> None:
    catalog_path = tmp_path / "catalog" / "new-tab-templates.json"
    catalog_path.parent.mkdir()
    catalog_path.write_bytes(catalog_document(catalog_template_document("inbox")))
    checkout = tmp_path / "weather-desk"
    checkout.mkdir()
    (checkout / "app.toml").write_text(_APP_MANIFEST)
    (checkout / "icon.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>\n')

    built = subprocess.run(
        [
            sys.executable,
            str(_BUILDER_PATH),
            "--catalog",
            str(catalog_path),
            "--app",
            f"https://github.com/someone/weather-desk={checkout}",
            "--popular",
            "app:weather-desk,inbox",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert built.returncode == 0, built.stderr

    catalog = parse_template_catalog(catalog_path.read_bytes(), _CATALOG_URL)
    wire = catalog_wire(catalog, _CATALOG_URL).model_dump(mode="json")
    assert wire["apps"] == [
        {
            "name": "weather-desk",
            "title": "Weather Desk",
            "description": "Forecasts for the places you care about.",
            "repository_url": "https://github.com/someone/weather-desk",
            "author": "someone",
            "icon_url": "https://example.test/catalog/app-icons/someone--weather-desk.svg",
            "version": "1.2.0",
            "required_accounts": [{"scope": "slack-api", "permission": "slack-read-all"}],
            "required_secrets": ["OPENWEATHER_API_KEY"],
            "needs_ai": True,
            "apt_packages": ["ffmpeg"],
        }
    ]
    # The icon the entry points at is the file the builder put beside the catalog.
    assert (catalog_path.parent / "app-icons" / "someone--weather-desk.svg").is_file()
    assert [template["slug"] for template in wire["templates"]] == ["inbox"]
    assert wire["shelves"] == [{"key": "popular", "title": "Most popular", "slugs": ["app:weather-desk", "inbox"]}]


def test_the_shipped_catalog_reads_whole() -> None:
    body = _SHIPPED_CATALOG_PATH.read_bytes()
    shipped = json.loads(body)
    catalog = parse_template_catalog(body, _CATALOG_URL)
    # Nothing the file lists is skipped as invalid.
    assert [template.slug for template in catalog.templates] == [template["slug"] for template in shipped["templates"]]
    assert [app.name for app in catalog.apps] == [app["name"] for app in shipped["apps"]]
    assert [shelf.key for shelf in catalog.shelves] == [shelf["key"] for shelf in shipped["shelves"]]
