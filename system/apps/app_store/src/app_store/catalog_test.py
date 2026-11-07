"""The shipped catalog validates, its drop-ins are gitignored, and every rule a catalog entry is checked against
refuses what it should."""

import subprocess
from pathlib import Path
from typing import Any

import pytest

from app_store.catalog import CATALOG_PATH
from app_store.catalog import ICONS_DIRECTORY
from app_store.catalog import LaunchCommand
from app_store.catalog import NodeRecipe
from app_store.catalog import PythonRecipe
from app_store.catalog import load_app_catalog
from app_store.catalog import parse_app_catalog
from app_store.catalog import render_argv
from app_store.errors import CatalogError

_REPO_ROOT = Path(__file__).resolve().parents[5]

_CATEGORIES = """
[[categories]]
key = "data"
title = "Data and notebooks"
"""

_GOOD_ENTRY = """
[[apps]]
slug = "demo"
tags = ["data"]
title = "Demo"
description = "A demo."
about = "A demo app."
author = "Someone"
license = "MIT"
homepage_url = "https://example.com"
source_url = "https://github.com/someone/demo"
version = "1.2.3"
icon = "datasette.svg"
recipe = { kind = "python", requirement = "demo==1.2.3" }
resources = { disk_mb = 10, memory_idle_mb = 20, memory_in_use_mb = 30 }
[apps.launch]
argv = ["{env_dir}/bin/demo", "--host", "127.0.0.1", "--port", "{port}", "--data", "{data_dir}"]
"""


def _parse(text: str) -> Any:
    return parse_app_catalog(_CATEGORIES + text, ICONS_DIRECTORY, "test")


def test_the_shipped_catalog_validates_with_an_icon_for_every_app() -> None:
    catalog = load_app_catalog()
    assert [app.slug for app in catalog.apps] == [
        "jupyterlab",
        "marimo",
        "datasette",
        "nodered",
        "tiddlywiki",
        "radicale",
        "excalidraw",
    ]
    for app in catalog.apps:
        assert (ICONS_DIRECTORY / app.icon).is_file()
        assert app.version == app.recipe.pinned_version


def test_every_shipped_apps_generated_dropin_is_gitignored() -> None:
    # The store writes system/supervisord.conf.d/<slug>.conf, which must never be committed.
    paths = [f"system/supervisord.conf.d/{app.slug}.conf" for app in load_app_catalog().apps]
    completed = subprocess.run(
        ["git", "check-ignore", "--no-index", *paths], cwd=_REPO_ROOT, capture_output=True, text=True, check=False
    )
    assert sorted(completed.stdout.split()) == sorted(paths), f"add these to .gitignore: {paths}"


def test_a_good_entry_parses_and_its_argv_renders() -> None:
    catalog = _parse(_GOOD_ENTRY)
    app = catalog.find("demo")
    assert app is not None
    assert render_argv(app.launch.argv, 8090, Path("/d"), Path("/e")) == [
        "/e/bin/demo",
        "--host",
        "127.0.0.1",
        "--port",
        "8090",
        "--data",
        "/d",
    ]
    assert catalog.find("missing") is None


def test_jupyterlab_trusts_writes_from_its_own_address_only() -> None:
    # The desktop frames JupyterLab from another site, where its _xsrf cookie never arrives, so the cookie check is
    # off and the origin check, keyed to the app's own origin label, does that job.
    jupyter = load_app_catalog().find("jupyterlab")
    assert jupyter is not None
    rendered = render_argv(jupyter.launch.argv, 8090, Path("/d"), Path("/e"), "jupyterlab-f8n3n82u")
    assert "--ServerApp.disable_check_xsrf=True" in rendered
    assert "--ServerApp.allow_origin_pat=^https?://jupyterlab-f8n3n82u\\.[^/]+$" in rendered
    assert not any(argument.startswith("--ServerApp.allow_origin=") for argument in rendered)
    # Doubled braces render as literal ones: the tornado settings are JSON.
    assert '--ServerApp.tornado_settings={"headers":{"Content-Security-Policy":"frame-ancestors *"}}' in rendered


def test_an_init_argv_cannot_name_the_origin_label() -> None:
    with pytest.raises(ValueError, match="unknown placeholders"):
        LaunchCommand(argv=("{env_dir}/bin/a", "127.0.0.1", "{port}"), init_argv=("{env_dir}/bin/a", "{origin_label}"))
    with pytest.raises(ValueError, match="launch env uses unknown placeholders"):
        LaunchCommand(argv=("{env_dir}/bin/a", "127.0.0.1", "{port}"), env={"X": "{secret}"})
    LaunchCommand(argv=("{env_dir}/bin/a", "127.0.0.1", "{port}", "{origin_label}"), env={"X": "{origin_label}"})


@pytest.mark.parametrize(
    ("old", "new", "complaint"),
    [
        ('slug = "demo"', 'slug = "my-demo"', "lowercase letters and digits"),
        ('requirement = "demo==1.2.3"', 'requirement = "demo>=1.2"', "pin one exact version"),
        ('version = "1.2.3"', 'version = "1.2.4"', "differs from the recipe"),
        ('"127.0.0.1"', '"0.0.0.0"', "must bind 127.0.0.1"),
        ('"{port}"', '"8080"', "must pass the app its {port}"),
        ('"{env_dir}/bin/demo"', '"/usr/bin/demo"', "from the app's environment"),
        ('"{data_dir}"', '"{home}"', "unknown placeholders"),
        ('homepage_url = "https://example.com"', 'homepage_url = "http://example.com"', "https URL"),
        ('kind = "python", requirement', 'kind = "shell", requirement', "kind"),
        ("memory_in_use_mb = 30", "memory_in_use_mb = 10", "below memory_idle_mb"),
        ('tags = ["data"]', 'tags = ["games"]', "which no category declares"),
        ('tags = ["data"]', "tags = []", "categories with no apps"),
    ],
)
def test_an_entry_breaking_a_rule_is_refused(old: str, new: str, complaint: str) -> None:
    with pytest.raises(CatalogError, match="does not validate") as raised:
        _parse(_GOOD_ENTRY.replace(old, new, 1))
    assert complaint in str(raised.value)


def test_the_shipped_categories_each_hold_an_app() -> None:
    catalog = load_app_catalog()
    assert [(category.key, category.title) for category in catalog.categories] == [
        ("data", "Data and notebooks"),
        ("automation", "Automation"),
        ("notes", "Notes and knowledge"),
        ("organize", "Organize your life"),
    ]
    jupyter = catalog.find("jupyterlab")
    assert jupyter is not None and jupyter.tags == ("data",)


def test_categories_must_be_distinct_used_and_well_named() -> None:
    with pytest.raises(CatalogError, match="categories with no apps"):
        _parse('[[categories]]\nkey = "empty"\ntitle = "Empty"\n' + _GOOD_ENTRY)
    with pytest.raises(CatalogError, match="duplicate category keys"):
        _parse('[[categories]]\nkey = "data"\ntitle = "Again"\n' + _GOOD_ENTRY)
    with pytest.raises(CatalogError, match="category key"):
        _parse('[[categories]]\nkey = "Bad Key"\ntitle = "Bad"\n' + _GOOD_ENTRY)


def test_a_missing_icon_and_duplicate_slugs_are_refused() -> None:
    with pytest.raises(CatalogError, match="names icon"):
        _parse(_GOOD_ENTRY.replace('icon = "datasette.svg"', 'icon = "nope.svg"'))
    with pytest.raises(CatalogError, match="names icon"):
        _parse(_GOOD_ENTRY.replace('icon = "datasette.svg"', 'icon = "../icons/datasette.svg"'))
    with pytest.raises(CatalogError, match="duplicate slugs"):
        _parse(_GOOD_ENTRY + _GOOD_ENTRY)


def test_text_that_is_not_toml_and_a_missing_file_are_refused(tmp_path: Path) -> None:
    with pytest.raises(CatalogError, match="not valid TOML"):
        _parse("[[apps]\n")
    with pytest.raises(CatalogError, match="cannot read"):
        load_app_catalog(tmp_path / "apps.toml")


def test_recipes_describe_what_they_install() -> None:
    python = PythonRecipe(kind="python", requirement="jupyterlab[full]==4.6.5")
    node = NodeRecipe(kind="node", package="@scope/tool@1.0.0")
    assert (python.pinned_version, python.source_label, python.package_spec) == (
        "4.6.5",
        "Python package",
        "jupyterlab[full]==4.6.5",
    )
    assert (node.pinned_version, node.source_label, node.package_spec) == ("1.0.0", "npm package", "@scope/tool@1.0.0")
    with pytest.raises(ValueError, match="3.x"):
        PythonRecipe(kind="python", requirement="a==1", python="2.7")
    with pytest.raises(ValueError, match="pin one exact version"):
        NodeRecipe(kind="node", package="node-red@latest")


def test_an_init_argv_and_env_follow_the_same_rules() -> None:
    with pytest.raises(ValueError, match="init argv"):
        LaunchCommand(argv=("{env_dir}/bin/a", "127.0.0.1", "{port}"), init_argv=("/bin/sh", "-c", "x"))
    # Binding loopback through an environment variable counts.
    LaunchCommand(argv=("{env_dir}/bin/a", "{port}"), env={"HOST": "127.0.0.1"})
    with pytest.raises(ValueError, match="malformed placeholder"):
        LaunchCommand(argv=("{env_dir}/bin/a", "127.0.0.1", "{port", "{port}"))
    with pytest.raises(ValueError, match="empty"):
        LaunchCommand(argv=())


def test_the_catalog_file_lives_in_the_package() -> None:
    assert CATALOG_PATH.parent.parent.name == "app_store"
