from pathlib import Path

import pytest
from loguru import logger

from app_manifest.errors import RegistryReadError
from app_manifest.registry import DEFAULT_APPS_FILE
from app_manifest.registry import ENV_APPS_FILE
from app_manifest.registry import read_registry
from app_manifest.registry import registry_path

_ICON = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="M2 2h20v20H2z"/></svg>'


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
    assert row.instances is False
    assert row.instances_url is None
    assert row.critical is False
    assert row.priority == "user"
    assert row.default_shortcut is None
    assert row.actions == ()


def test_a_manifest_row_reads_every_copied_field(tmp_path: Path) -> None:
    registry = tmp_path / "apps.toml"
    registry.write_text(
        "[[apps]]\n"
        'name = "files"\n'
        'url = "http://localhost:8300"\n'
        'label = "files-abcd1234"\n'
        f'icon = "{_ICON.replace(chr(34), chr(92) + chr(34))}"\n'
        'program = "files"\n'
        'display_name = "File Viewer"\n'
        "instances = true\n"
        'instances_url = "http://127.0.0.1:8301"\n'
        "critical = false\n"
        'priority = "files"\n'
        'default_shortcut = {action = "new", mode = "focus"}\n'
        'actions = [{id = "new", label = "New File Viewer"}]\n'
    )

    rows = read_registry(registry)

    assert len(rows) == 1
    row = rows[0]
    assert row.icon == _ICON
    assert row.display_name == "File Viewer"
    assert row.instances is True
    assert row.instances_url == "http://127.0.0.1:8301"
    assert row.priority == "files"
    assert row.default_shortcut is not None
    assert row.default_shortcut.action == "new"
    assert [(action.id, action.label) for action in row.actions] == [("new", "New File Viewer")]


def test_a_row_that_fails_validation_is_skipped_and_logged(tmp_path: Path) -> None:
    registry = tmp_path / "apps.toml"
    registry.write_text(
        '[[apps]]\nname = "good"\nurl = "http://localhost:5000"\n'
        '[[apps]]\nname = "bad"\nurl = "http://localhost:5001"\ninstances = "maybe"\n'
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
    assert "bad" in captured[0] and "instances" in captured[0]
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
