import configparser
from pathlib import Path

from app_manifest.manifest import load_manifest
from app_store.catalog import load_app_catalog
from app_store.layout import GENERATED_MARKER
from app_store.layout import StoreLayout
from app_store.layout import is_store_dropin
from app_store.layout import render_dropin
from app_store.layout import render_manifest


def test_every_catalog_apps_generated_manifest_validates(tmp_path: Path) -> None:
    for app in load_app_catalog().apps:
        folder = tmp_path / app.slug
        folder.mkdir()
        (folder / "icon.svg").write_text("<svg/>")
        (folder / "app.toml").write_text(render_manifest(app))
        manifest = load_manifest(folder / "app.toml")
        assert (str(manifest.name), str(manifest.display_name), str(manifest.program)) == (
            app.slug,
            app.title,
            app.slug,
        )
        assert str(manifest.priority) == "user"
        assert manifest.stop_when_no_windows is True


def test_the_generated_dropin_runs_the_launcher_in_the_user_band() -> None:
    app = load_app_catalog().find("datasette")
    assert app is not None
    text = render_dropin(app, Path("/home/user/workspace"))
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(text)
    program = parser["program:datasette"]
    assert program["command"] == (
        "python3 system/services/oom_priority/bin/oom_tag_service.py user .venv/bin/app-store-launch datasette"
    )
    assert program["directory"] == "/home/user/workspace"
    assert program["stderr_logfile"] == "/var/log/supervisor/datasette-stderr.log"
    assert text.startswith(GENERATED_MARKER)


def test_only_a_dropin_the_store_wrote_counts_as_the_stores(tmp_path: Path) -> None:
    ours = tmp_path / "a.conf"
    ours.write_text(GENERATED_MARKER + " for A\n")
    theirs = tmp_path / "b.conf"
    theirs.write_text("[program:b]\n")
    assert is_store_dropin(ours)
    assert not is_store_dropin(theirs)
    assert not is_store_dropin(tmp_path / "missing.conf")


def test_an_apps_files_sit_in_one_folder_and_its_dropin_beside_the_others(tmp_path: Path) -> None:
    layout = StoreLayout(data_dir=tmp_path / "data", repo_root=tmp_path)
    assert layout.env_dir("x") == tmp_path / "data" / "x" / "env"
    assert layout.app_data_dir("x") == tmp_path / "data" / "x" / "data"
    assert layout.log_path("x") == tmp_path / "data" / "x" / "install.log"
    assert layout.dropin_path("x") == tmp_path / "system" / "supervisord.conf.d" / "x.conf"
    assert layout.records_path == tmp_path / "data" / "installs.json"
    assert layout.registry_path == tmp_path / "data" / ".state" / "apps.toml"
