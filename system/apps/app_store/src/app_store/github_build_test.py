"""Building a reviewed app from a pinned GitHub commit: the recipe's rules, the install steps, keeping only the built
site, the build's needs in the fit check, and a full install through the engine."""

from pathlib import Path

import pytest

from app_store.catalog import AppCatalog
from app_store.catalog import CatalogApp
from app_store.catalog import GithubBuildRecipe
from app_store.catalog import ResourceEstimate
from app_store.catalog import load_app_catalog
from app_store.errors import InstallError
from app_store.errors import InstallRefusedError
from app_store.records import InstallStatus
from app_store.room import RoomVerdict
from app_store.room import WorkspaceRoom
from app_store.room import check_app_room
from app_store.system import CommandPackageInstaller
from app_store.system import install_steps
from app_store.system import keep_built_site
from app_store.testing import FakeCommandRunner
from app_store.testing import FakeResourceReader
from app_store.testing import build_test_engine

_COMMIT = "a2ec2889babf7d2295469c6d90ebe77fae57df84"


def _recipe(**overrides: object) -> GithubBuildRecipe:
    fields: dict[str, object] = {
        "kind": "github_build",
        "repository": "https://github.com/excalidraw/excalidraw",
        "ref": "v0.18.1",
        "commit": _COMMIT,
        "node_tools": ["yarn@1.22.22", "http-server@14.1.1"],
        "build": [["{env_dir}/node_modules/.bin/yarn", "install"], ["{env_dir}/node_modules/.bin/yarn", "build"]],
        "build_env": {"YARN_IGNORE_ENGINES": "true", "TOOLS": "{env_dir}/node_modules/.bin"},
        "output": "excalidraw-app/build",
    }
    fields.update(overrides)
    return GithubBuildRecipe.model_validate(fields)


def test_a_recipe_names_its_release_and_commit() -> None:
    recipe = _recipe()
    assert recipe.pinned_version == "0.18.1"
    assert recipe.source_label == "Built from GitHub"
    assert recipe.package_spec == "excalidraw/excalidraw v0.18.1 (a2ec2889babf)"


@pytest.mark.parametrize(
    ("overrides", "complaint"),
    [
        ({"repository": "https://gitlab.com/x/y"}, "https://github.com/<owner>/<name>"),
        ({"repository": "https://github.com/x/y/tree/main"}, "https://github.com/<owner>/<name>"),
        ({"commit": "a2ec2889"}, "full 40-character hash"),
        ({"ref": "main"}, "release tag"),
        ({"node_tools": ["yarn"]}, "pin one exact version"),
        ({"node_tools": ["yarn@^1.22.0"]}, "pin one exact version"),
        ({"output": "../../etc"}, "plain path inside the checkout"),
        ({"output": "/abs/build"}, "plain path inside the checkout"),
        ({"build": [["yarn", "build"]]}, "from the app's environment"),
        ({"build": [["{env_dir}/node_modules/.bin/yarn", "--port", "{port}"]]}, "unknown placeholders"),
        ({"build": []}, "at least 1"),
        ({"build_env": {"X": "{data_dir}"}}, "unknown placeholders"),
    ],
)
def test_a_recipe_breaking_a_rule_is_refused(overrides: dict[str, object], complaint: str) -> None:
    with pytest.raises(ValueError, match=complaint):
        _recipe(**overrides)


def test_the_install_installs_tools_fetches_the_commit_by_hash_and_builds_in_the_checkout(tmp_path: Path) -> None:
    env = tmp_path / "env"
    steps = install_steps(_recipe(), env)
    assert [step.argv for step in steps] == [
        ("npm", "install", "--prefix", str(env), "--no-audit", "--no-fund", "yarn@1.22.22", "http-server@14.1.1"),
        ("git", "init", "--quiet", str(env / "source")),
        (
            "git",
            "-C",
            str(env / "source"),
            "fetch",
            "--quiet",
            "--depth",
            "1",
            "https://github.com/excalidraw/excalidraw.git",
            _COMMIT,
        ),
        (
            "git",
            "-C",
            str(env / "source"),
            "-c",
            "advice.detachedHead=false",
            "checkout",
            "--quiet",
            "--detach",
            _COMMIT,
        ),
        (f"{env}/node_modules/.bin/yarn", "install"),
        (f"{env}/node_modules/.bin/yarn", "build"),
    ]
    assert [step.cwd for step in steps[4:]] == [env / "source", env / "source"]
    assert steps[4].env == {"YARN_IGNORE_ENGINES": "true", "TOOLS": f"{env}/node_modules/.bin"}
    assert all(step.cwd is None for step in steps[:4])


def test_only_the_built_site_is_kept(tmp_path: Path) -> None:
    env = tmp_path / "env"
    built = env / "source" / "excalidraw-app" / "build"
    built.mkdir(parents=True)
    (built / "index.html").write_text("<!doctype html>")
    (env / "source" / "node_modules").mkdir()
    keep_built_site(_recipe(), env)
    assert (env / "site" / "index.html").read_text() == "<!doctype html>"
    assert not (env / "source").exists()


def test_a_build_that_made_no_site_fails(tmp_path: Path) -> None:
    (tmp_path / "env" / "source").mkdir(parents=True)
    with pytest.raises(InstallError, match="made no site at excalidraw-app/build"):
        keep_built_site(_recipe(), tmp_path / "env")


def test_the_installer_runs_every_step_then_keeps_the_site_and_stops_at_a_failure(tmp_path: Path) -> None:
    env = tmp_path / "env"
    built = env / "source" / "excalidraw-app" / "build"
    built.mkdir(parents=True)
    (built / "index.html").write_text("<!doctype html>")
    runner = FakeCommandRunner()
    CommandPackageInstaller(runner=runner).install(_recipe(), env, tmp_path / "install.log")
    assert len(runner.commands) == 6
    assert (env / "site" / "index.html").is_file()

    failing = FakeCommandRunner(exit_status=1)
    with pytest.raises(InstallError, match="excalidraw/excalidraw v0.18.1"):
        CommandPackageInstaller(runner=failing).install(_recipe(), tmp_path / "other", tmp_path / "install.log")
    assert len(failing.commands) == 1


def _built_excalidraw() -> CatalogApp:
    """The shipped Excalidraw entry as it was when it was built from source: the build recipe and its needs."""
    shipped = load_app_catalog().find("excalidraw")
    assert shipped is not None
    return shipped.model_copy(
        update={
            "version": "0.18.1",
            "recipe": _recipe(),
            "resources": ResourceEstimate(
                disk_mb=55, memory_idle_mb=160, memory_in_use_mb=170, build_disk_mb=1100, build_memory_mb=3100
            ),
        }
    )


def _catalog_with_built_excalidraw() -> AppCatalog:
    shipped = load_app_catalog()
    apps = tuple(_built_excalidraw() if app.slug == "excalidraw" else app for app in shipped.apps)
    return shipped.model_copy(update={"apps": apps})


def test_the_fit_counts_what_the_build_needs_and_says_so() -> None:
    excalidraw = _built_excalidraw()
    assert (excalidraw.resources.install_disk_mb, excalidraw.resources.install_memory_mb) == (1100, 3100)
    check = check_app_room(excalidraw, WorkspaceRoom(disk_free_mb=30_000, memory_available_mb=2_000))
    assert check.verdict is RoomVerdict.WONT_FIT
    assert "about 3.0 GB of memory while it builds, and 2.0 GB is available" in check.blocked_reason
    fits = check_app_room(excalidraw, WorkspaceRoom(disk_free_mb=30_000, memory_available_mb=5_000))
    assert fits.verdict is RoomVerdict.FITS
    jupyter = load_app_catalog().find("jupyterlab")
    assert jupyter is not None
    tight = check_app_room(jupyter, WorkspaceRoom(disk_free_mb=30_000, memory_available_mb=100))
    assert "while in use" in tight.blocked_reason


def test_install_figures_take_the_larger_of_building_and_running() -> None:
    assert ResourceEstimate(disk_mb=50, build_disk_mb=1000).install_disk_mb == 1000
    assert ResourceEstimate(disk_mb=50).install_disk_mb == 50
    assert ResourceEstimate(disk_mb=None).install_disk_mb is None
    assert ResourceEstimate(disk_mb=1, memory_in_use_mb=200, build_memory_mb=100).install_memory_mb == 200
    assert ResourceEstimate(disk_mb=1).install_memory_mb is None


def test_a_github_app_installs_through_the_engine_like_any_other(tmp_path: Path) -> None:
    engine = build_test_engine(tmp_path, curated=_catalog_with_built_excalidraw())
    engine.request_install("excalidraw")
    engine.run_next_job()
    record = engine.records.read().find("excalidraw")
    assert record is not None and record.status is InstallStatus.INSTALLED
    assert (engine.layout.env_dir("excalidraw") / "site" / "index.html").is_file()
    assert "app-store-launch excalidraw" in engine.layout.dropin_path("excalidraw").read_text()


def test_a_github_app_is_refused_when_the_build_would_not_fit(tmp_path: Path) -> None:
    engine = build_test_engine(tmp_path, curated=_catalog_with_built_excalidraw())
    assert isinstance(engine.resources, FakeResourceReader)
    engine.resources.room = WorkspaceRoom(disk_free_mb=30_000, memory_available_mb=1_000)
    with pytest.raises(InstallRefusedError, match="while it builds"):
        engine.request_install("excalidraw")
