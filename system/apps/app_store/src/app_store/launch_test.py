from pathlib import Path

import pytest
from click.testing import CliRunner

from app_store.errors import AppStoreError
from app_store.launch import LAUNCH_FAILED_EXIT_CODE
from app_store.launch import launch_environment
from app_store.launch import main
from app_store.launch import plan_launch
from app_store.launch import registered_origin_label
from app_store.testing import build_test_engine


def test_the_plan_registers_the_generated_manifest_and_fills_in_the_argv(tmp_path: Path) -> None:
    engine = build_test_engine(tmp_path)
    engine.request_install("jupyterlab")
    engine.run_next_job()

    plan = plan_launch("jupyterlab", engine.layout)
    argv, extra_env = plan.render("jupyterlab-f8n3n82u")

    data_dir = engine.layout.app_data_dir("jupyterlab")
    assert plan.manifest_path == engine.layout.manifest_path("jupyterlab")
    assert plan.app_url == "http://localhost:8080"
    assert plan.data_dir == data_dir
    assert argv[0] == f"{engine.layout.env_dir('jupyterlab')}/bin/jupyter-lab"
    assert "--port=8080" in argv
    assert f"--ServerApp.root_dir={data_dir}" in argv
    assert "--ServerApp.allow_origin_pat=^https?://jupyterlab-f8n3n82u\\.[^/]+$" in argv
    assert extra_env["JUPYTER_CONFIG_DIR"] == f"{data_dir}/.jupyter"


def test_an_app_the_store_has_not_installed_cannot_be_planned(tmp_path: Path) -> None:
    engine = build_test_engine(tmp_path)
    with pytest.raises(AppStoreError, match="not an app the App Store has installed"):
        plan_launch("datasette", engine.layout)
    with pytest.raises(AppStoreError):
        plan_launch("nope", engine.layout)


def test_the_origin_label_is_read_from_the_registry_once_registered(tmp_path: Path) -> None:
    registry = tmp_path / "apps.toml"
    registry.write_text(
        '[[apps]]\nname = "jupyterlab"\nurl = "http://localhost:8082"\nlabel = "jupyterlab-f8n3n82u"\n'
    )
    assert registered_origin_label("jupyterlab", registry) == "jupyterlab-f8n3n82u"
    with pytest.raises(AppStoreError, match="no origin label for marimo"):
        registered_origin_label("marimo", registry)


def test_the_apps_environment_adds_to_the_programs_own() -> None:
    assert launch_environment({"PATH": "/bin", "A": "1"}, {"A": "2"}) == {"PATH": "/bin", "A": "2"}


def test_launching_an_app_the_store_has_not_installed_exits_without_retrying(tmp_path: Path) -> None:
    result = CliRunner().invoke(main, ["datasette"], env={"APP_STORE_DATA_DIR": str(tmp_path / "data")})
    assert result.exit_code == LAUNCH_FAILED_EXIT_CODE
    assert not (tmp_path / "data" / "datasette").exists()
