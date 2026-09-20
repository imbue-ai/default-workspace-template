from pathlib import Path
from uuid import uuid4

import pytest
from loguru import logger

from app_manifest.errors import AppRegistrationError
from app_manifest.errors import RegistryReadError
from app_manifest.primitives import AppName
from app_manifest.primitives import AppUrl
from app_manifest.registry import DEFAULT_APPS_FILE
from app_manifest.registry import ENV_APPS_FILE
from app_manifest.registry import read_origin_label
from app_manifest.registry import read_registry
from app_manifest.registry import register_app
from app_manifest.registry import registry_path
from app_manifest.testing import APP_ICON_MARKUP

def test_a_missing_registry_is_empty(tmp_path: Path) -> None:
    assert read_registry(tmp_path / "apps.toml") == []


def test_a_manifest_less_row_reads_with_the_documented_defaults(tmp_path: Path) -> None:
    registry = tmp_path / "apps.toml"
    registry.write_text('[[apps]]\nname = "web"\nurl = "http://localhost:5000"\nlabel = "web-abcd1234"\n')

    rows = read_registry(registry)

    assert len(rows) == 1
    row = rows[0]
    assert row.name == "web"
    assert row.url == "http://localhost:5000"
    assert row.label == "web-abcd1234"
    assert row.icon is None
    assert row.internal is False
    assert row.program is None
    assert row.display_name is None
    assert row.critical is False
    assert row.priority == "user"
    assert row.default_shortcut is None
    assert row.launch_paths == ()
    assert row.launcher_rank is None


def test_a_manifest_row_reads_every_copied_field(tmp_path: Path) -> None:
    registry = tmp_path / "apps.toml"
    registry.write_text(
        "[[apps]]\n"
        'name = "files"\n'
        'url = "http://localhost:8300"\n'
        'label = "files-abcd1234"\n'
        f'icon = "{APP_ICON_MARKUP.replace(chr(34), chr(92) + chr(34))}"\n'
        'program = "files"\n'
        'display_name = "File Viewer"\n'
        "critical = false\n"
        'priority = "files"\n'
        'default_shortcut = {launch = "new", mode = "focus"}\n'
        'launch_paths = [{id = "new", label = "New File Viewer", path = "/", params = ["path"]}, {id = "recent", label = "Recent", path = "/recent"}]\n'
        "launcher_rank = 20\n"
    )

    rows = read_registry(registry)

    assert len(rows) == 1
    row = rows[0]
    assert row.icon == APP_ICON_MARKUP
    assert row.display_name == "File Viewer"
    assert row.priority == "files"
    assert row.default_shortcut is not None
    assert row.default_shortcut.launch == "new"
    assert [
        (launch_path.id, launch_path.label, launch_path.path, launch_path.params)
        for launch_path in row.launch_paths
    ] == [
        ("new", "New File Viewer", "/", ("path",)),
        ("recent", "Recent", "/recent", ()),
    ]
    assert row.launcher_rank == 20


def test_a_row_that_fails_validation_is_skipped_and_logged(tmp_path: Path) -> None:
    registry = tmp_path / "apps.toml"
    registry.write_text(
        '[[apps]]\nname = "good"\nurl = "http://localhost:5000"\n'
        '[[apps]]\nname = "bad"\nurl = "http://localhost:5001"\ncritical = "maybe"\n'
        '[[apps]]\nname = "Bad Name"\nurl = "http://localhost:5002"\n'
    )
    captured: list[str] = []
    sink_id = logger.add(lambda message: captured.append(str(message)), level="WARNING")
    try:
        rows = read_registry(registry)
    finally:
        logger.remove(sink_id)

    assert [row.name for row in rows] == ["good"]
    assert len(captured) == 2
    assert "bad" in captured[0] and "critical" in captured[0]
    assert "name" in captured[1]


def test_unknown_keys_on_a_row_are_ignored(tmp_path: Path) -> None:
    registry = tmp_path / "apps.toml"
    registry.write_text('[[apps]]\nname = "web"\nurl = "http://localhost:5000"\nfuture_key = "x"\n')

    assert [row.name for row in read_registry(registry)] == ["web"]


def test_an_unparseable_registry_raises(tmp_path: Path) -> None:
    registry = tmp_path / "apps.toml"
    registry.write_text("[[apps]\nname = \n")

    with pytest.raises(RegistryReadError, match="not valid TOML"):
        read_registry(registry)


def test_an_apps_key_that_is_not_an_array_raises(tmp_path: Path) -> None:
    registry = tmp_path / "apps.toml"
    registry.write_text('apps = "nope"\n')

    with pytest.raises(RegistryReadError, match="array of tables"):
        read_registry(registry)


def test_registry_path_honours_the_environment_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_APPS_FILE, raising=False)
    assert registry_path() == Path(DEFAULT_APPS_FILE)

    monkeypatch.setenv(ENV_APPS_FILE, "/elsewhere/apps.toml")
    assert registry_path() == Path("/elsewhere/apps.toml")


def test_read_origin_label_answers_the_registered_apps_label_and_nothing_for_an_unregistered_one(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "apps.toml"
    registry.write_text(
        '[[apps]]\nname = "web"\nurl = "http://localhost:5000"\nlabel = "web-a1b2c3d4"\n'
        '[[apps]]\nname = "system_interface"\nurl = "http://localhost:8000"\nlabel = "system_interface-e5f6"\n'
    )

    assert read_origin_label(registry, AppName("system_interface")) == "system_interface-e5f6"
    assert read_origin_label(registry, AppName("web")) == "web-a1b2c3d4"
    assert read_origin_label(registry, AppName("absent")) == ""


def test_read_origin_label_of_an_unreadable_registry_is_empty_and_warns(tmp_path: Path) -> None:
    registry = tmp_path / "apps.toml"
    registry.write_text("[[apps]\nname = \n")
    captured: list[str] = []
    sink_id = logger.add(lambda message: captured.append(str(message)), level="WARNING")
    try:
        label = read_origin_label(registry, AppName("web"))
    finally:
        logger.remove(sink_id)

    assert label == ""
    assert len(captured) == 1
    assert "web" in captured[0] and "not valid TOML" in captured[0]


def test_register_app_writes_the_manifests_row(tmp_path: Path, registration_registry: Path) -> None:
    app_name = f"registered-{uuid4().hex[:8]}"
    (tmp_path / "icon.svg").write_text(APP_ICON_MARKUP)
    manifest_path = tmp_path / "app.toml"
    manifest_path.write_text(
        f'name = "{app_name}"\ndisplay_name = "Registered"\nicon = "icon.svg"\n'
        '[[launch_paths]]\nid = "new"\nlabel = "New"\npath = "/new"\n'
    )

    register_app(manifest_path, AppUrl("http://localhost:8300"))

    rows = read_registry(registration_registry)
    assert [row.name for row in rows] == [app_name]
    assert rows[0].url == "http://localhost:8300"
    assert rows[0].display_name == "Registered"
    assert [launch_path.id for launch_path in rows[0].launch_paths] == ["new"]


def test_register_app_reports_the_scripts_error_for_a_bad_manifest(tmp_path: Path, registration_registry: Path) -> None:
    manifest_path = tmp_path / "app.toml"
    manifest_path.write_text('name = "Not A Name"\n')

    with pytest.raises(AppRegistrationError, match="invalid app name"):
        register_app(manifest_path, AppUrl("http://localhost:8300"))


def test_register_app_needs_the_registration_script_under_the_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(AppRegistrationError, match="not found"):
        register_app(tmp_path / "app.toml", AppUrl("http://localhost:8300"))
