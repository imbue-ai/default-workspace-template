from pathlib import Path

import pytest

from oom_priority import app_registry


def test_a_missing_registry_reads_as_empty(tmp_path: Path) -> None:
    assert app_registry.read_priority_by_program(tmp_path / "apps.toml") == {}


def test_rows_with_a_program_map_to_their_priority_and_default_to_user(tmp_path: Path) -> None:
    registry = tmp_path / "apps.toml"
    registry.write_text(
        '[[apps]]\nname = "files"\nurl = "http://localhost:8300"\nprogram = "files"\npriority = "files"\n'
        '[[apps]]\nname = "news"\nurl = "http://localhost:8090"\nprogram = "news"\n'
        '[[apps]]\nname = "owner-exec"\nurl = "http://localhost:8793"\ninternal = true\n'
        '[[apps]]\nname = "odd"\nurl = "http://localhost:8091"\nprogram = "odd"\npriority = 7\n'
    )

    assert app_registry.read_priority_by_program(registry) == {"files": "files", "news": "user", "odd": "user"}


def test_an_unparseable_registry_is_reported_and_reads_as_empty(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    registry = tmp_path / "apps.toml"
    registry.write_text("[[apps]\nname = \n")

    assert app_registry.read_priority_by_program(registry) == {}
    assert any("cannot be read" in record.getMessage() for record in caplog.records)


def test_registry_path_honours_the_environment_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(app_registry.ENV_APPS_FILE, raising=False)
    assert app_registry.registry_path() == Path(app_registry.DEFAULT_APPS_FILE)

    monkeypatch.setenv(app_registry.ENV_APPS_FILE, "/elsewhere/apps.toml")
    assert app_registry.registry_path() == Path("/elsewhere/apps.toml")
