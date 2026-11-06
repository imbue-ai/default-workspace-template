"""Updating an installed app: the new version replaces the old one with the app's data and port kept, and when the
new one fails to install or start -- or a restart interrupts it -- the old version is put back and runs again."""

from pathlib import Path

import pytest
from flask import Flask
from flask.testing import FlaskClient

from app_store.added_apps import AddAppRequest
from app_store.catalog import CatalogApp
from app_store.catalog import PythonRecipe
from app_store.engine import InstallEngine
from app_store.engine import UPDATE_INTERRUPTED_REASON
from app_store.errors import InstallRefusedError
from app_store.layout import NO_BACKUP_MARKER
from app_store.layout import render_manifest
from app_store.pages import build_pages_blueprint
from app_store.records import InstallStatus
from app_store.records import JobKind
from app_store.room import WorkspaceRoom
from app_store.template_sizes import TemplateSizeStore
from app_store.testing import FakePackageInstaller
from app_store.testing import FakePortProbe
from app_store.testing import FakeRepositorySizeFetcher
from app_store.testing import FakeResourceReader
from app_store.testing import FakeSupervisor
from app_store.testing import build_test_adder
from app_store.testing import build_test_engine
from app_store.testing import build_test_finder
from getting_started.template_catalog import TemplateCatalogStore
from getting_started.testing import FakeTemplateCatalogFetcher


def _installed(tmp_path: Path, slug: str = "jupyterlab") -> InstallEngine:
    engine = build_test_engine(tmp_path)
    engine.request_install(slug)
    engine.run_next_job()
    record = engine.records.read().find(slug)
    assert record is not None and record.status is InstallStatus.INSTALLED
    return engine


def _newer(engine: InstallEngine, slug: str = "jupyterlab", version: str = "9.9.9") -> CatalogApp:
    app = engine.catalog.find(slug)
    assert app is not None
    return app.model_copy(
        update={"version": version, "recipe": PythonRecipe(kind="python", requirement=f"{slug}=={version}")}
    )


def test_an_update_installs_the_new_version_and_keeps_the_data_and_port(tmp_path: Path) -> None:
    engine = _installed(tmp_path)
    before = engine.records.read().find("jupyterlab")
    assert before is not None
    data = engine.layout.app_data_dir("jupyterlab")
    (data / "notebook.ipynb").write_text("mine")

    queued = engine.request_update("jupyterlab", _newer(engine))
    assert (queued.status, queued.job) == (InstallStatus.QUEUED, JobKind.UPDATE)
    engine.run_next_job()

    after = engine.records.read().find("jupyterlab")
    assert after is not None
    assert (after.status, after.version, after.port, after.failure_reason) == (
        InstallStatus.INSTALLED,
        "9.9.9",
        before.port,
        "",
    )
    assert after.update_to is None and after.job is None
    assert (data / "notebook.ipynb").read_text() == "mine"
    assert not engine.layout.previous_env_dir("jupyterlab").exists()
    assert (engine.layout.env_dir("jupyterlab") / NO_BACKUP_MARKER).exists()
    assert isinstance(engine.installer, FakePackageInstaller)
    assert engine.installer.installed[-1] == "jupyterlab==9.9.9"
    assert isinstance(engine.supervisor, FakeSupervisor)
    # Stopped before the new version went in, then started again on it.
    assert engine.supervisor.calls[-3:] == ["stop jupyterlab", "apply jupyterlab", "start jupyterlab"]
    assert "updated to 9.9.9" in engine.layout.log_path("jupyterlab").read_text()


def test_an_update_that_fails_to_install_puts_the_old_version_back(tmp_path: Path) -> None:
    engine = _installed(tmp_path)
    old_env = engine.layout.env_dir("jupyterlab")
    (old_env / "old-version.txt").write_text("4.6.5")
    dropin = engine.layout.dropin_path("jupyterlab").read_text()
    assert isinstance(engine.installer, FakePackageInstaller)
    engine.request_update("jupyterlab", _newer(engine))
    engine.installer.is_failing = True
    engine.run_next_job()

    record = engine.records.read().find("jupyterlab")
    assert record is not None
    assert record.status is InstallStatus.INSTALLED
    assert record.version != "9.9.9"
    assert f"so {record.version} was put back" in record.failure_reason
    assert (old_env / "old-version.txt").read_text() == "4.6.5"
    assert not engine.layout.previous_env_dir("jupyterlab").exists()
    assert engine.layout.dropin_path("jupyterlab").read_text() == dropin
    assert isinstance(engine.supervisor, FakeSupervisor)
    assert engine.supervisor.calls[-1] == "start jupyterlab"
    assert "was put back" in engine.layout.log_path("jupyterlab").read_text()


def test_an_update_that_never_answers_puts_the_old_version_and_its_files_back(tmp_path: Path) -> None:
    engine = _installed(tmp_path)
    layout = engine.layout
    (layout.env_dir("jupyterlab") / "old-version.txt").write_text("old")
    manifest, icon = layout.manifest_path("jupyterlab").read_text(), layout.icon_path("jupyterlab").read_bytes()
    # The new version brings its own icon, written before it is started.
    newer = _newer(engine).model_copy(update={"icon": "marimo.svg"})
    engine.request_update("jupyterlab", newer)
    assert isinstance(engine.probe, FakePortProbe)
    engine.probe.is_answering = False
    engine.run_next_job()
    record = engine.records.read().find("jupyterlab")
    assert record is not None and record.status is InstallStatus.INSTALLED
    assert "JupyterLab 9.9.9 didn't start answering on its port" in record.failure_reason
    assert (layout.env_dir("jupyterlab") / "old-version.txt").read_text() == "old"
    assert layout.manifest_path("jupyterlab").read_text() == manifest
    assert layout.icon_path("jupyterlab").read_bytes() == icon


def test_an_update_is_refused_when_it_cannot_run(tmp_path: Path) -> None:
    engine = build_test_engine(tmp_path)
    with pytest.raises(InstallRefusedError, match="isn't installed"):
        engine.request_update("jupyterlab", _newer(engine))
    engine = _installed(tmp_path / "second")
    current = engine.catalog.find("jupyterlab")
    assert current is not None
    with pytest.raises(InstallRefusedError, match="already installed"):
        engine.request_update("jupyterlab", current)
    with pytest.raises(InstallRefusedError, match="isn't for"):
        engine.request_update("jupyterlab", _newer(engine, "marimo"))
    assert isinstance(engine.resources, FakeResourceReader)
    engine.resources.room = WorkspaceRoom(disk_free_mb=30_000, memory_available_mb=10)
    with pytest.raises(InstallRefusedError):
        engine.request_update("jupyterlab", _newer(engine))
    engine.resources.room = WorkspaceRoom(disk_free_mb=30_000, memory_available_mb=8_000)
    engine.request_update("jupyterlab", _newer(engine))
    with pytest.raises(InstallRefusedError, match="busy"):
        engine.request_update("jupyterlab", _newer(engine, version="9.9.10"))


def test_a_restart_during_an_update_puts_the_old_version_back(tmp_path: Path) -> None:
    engine = _installed(tmp_path)
    env, previous = engine.layout.env_dir("jupyterlab"), engine.layout.previous_env_dir("jupyterlab")
    (env / "old-version.txt").write_text("old")
    current = engine.catalog.find("jupyterlab")
    assert current is not None
    newer = _newer(engine)
    engine.request_update("jupyterlab", newer)
    # As if the store stopped mid-configure: the old environment set aside, half a new one in its place, and the new
    # version's manifest written but not yet its drop-in.
    env.rename(previous)
    env.mkdir()
    engine.layout.manifest_path("jupyterlab").write_text(render_manifest(newer))
    engine.records.update("jupyterlab", status=InstallStatus.CONFIGURING)

    engine.reconcile()
    record = engine.records.read().find("jupyterlab")
    assert record is not None
    assert (record.status, record.job, record.failure_reason) == (
        InstallStatus.INSTALLED,
        None,
        UPDATE_INTERRUPTED_REASON,
    )
    assert (env / "old-version.txt").read_text() == "old"
    assert not previous.exists()
    assert engine.records.read().queue == ()
    assert engine.layout.manifest_path("jupyterlab").read_text() == render_manifest(current)


_DEMO_COMMAND = "demo --port {port} --host 127.0.0.1"


def _client(tmp_path: Path, engine: InstallEngine) -> FlaskClient:
    app = Flask("app-store-under-test", static_folder=None)
    app.register_blueprint(
        build_pages_blueprint(
            tmp_path / "static",
            engine,
            build_test_adder(engine, tmp_path),
            build_test_finder(engine, tmp_path),
            TemplateCatalogStore(
                catalog_url="", cache_path=tmp_path / "catalog.json", fetcher=FakeTemplateCatalogFetcher()
            ),
            TemplateSizeStore(cache_path=tmp_path / "sizes.json", fetcher=FakeRepositorySizeFetcher()),
            tmp_path / "app_contract.js",
        )
    )
    return app.test_client()


def _store_entry(client: FlaskClient, slug: str) -> dict[str, object]:
    return next(app for app in client.get("/api/store").get_json()["apps"] if app["slug"] == slug)


def test_a_shipped_app_offers_the_version_the_workspace_brought_and_updates_to_it(tmp_path: Path) -> None:
    engine = _installed(tmp_path)
    client = _client(tmp_path, engine)
    shipped = engine.catalog.find("jupyterlab")
    assert shipped is not None
    entry = _store_entry(client, "jupyterlab")
    assert (entry["installed_version"], entry["update_kind"], entry["update_version"]) == (
        shipped.version,
        "CATALOG",
        None,
    )
    # As if the workspace's own update brought a newer jupyterlab into the catalog.
    engine.records.update("jupyterlab", version="1.0.0")
    entry = _store_entry(client, "jupyterlab")
    assert (entry["installed_version"], entry["update_version"]) == ("1.0.0", shipped.version)
    assert client.get("/api/apps/jupyterlab/newest-version").get_json() == {
        "installed_version": "1.0.0",
        "newest_version": shipped.version,
        "is_newer": True,
    }
    assert client.post("/api/apps/jupyterlab/update", json={}).status_code == 202
    engine.run_next_job()
    assert _store_entry(client, "jupyterlab")["installed_version"] == shipped.version
    assert _store_entry(client, "jupyterlab")["update_version"] is None


def test_an_added_package_finds_its_newest_version_and_updates_to_it(tmp_path: Path) -> None:
    engine = build_test_engine(tmp_path)
    adder = build_test_adder(engine, tmp_path)
    adder.add(
        AddAppRequest.model_validate(
            {"source": "pypi", "package": "demo", "version": "1.0", "display_name": "Demo", "command": _DEMO_COMMAND}
        )
    )
    engine.run_next_job()
    client = _client(tmp_path, engine)
    assert _store_entry(client, "demo")["update_kind"] == "REGISTRY"
    assert client.get("/api/apps/demo/newest-version").get_json() == {
        "installed_version": "1.0",
        "newest_version": "1.1",
        "is_newer": True,
    }
    assert client.post("/api/apps/demo/update", json={}).status_code == 202
    engine.run_next_job()
    added = engine.added.find("demo")
    assert added is not None
    assert (added.app.version, added.command) == ("1.1", ("demo", "--port", "{port}", "--host", "127.0.0.1"))
    record = engine.records.read().find("demo")
    assert record is not None and record.version == "1.1"
    assert client.get("/api/apps/demo/newest-version").get_json()["is_newer"] is False
    refused = client.post("/api/apps/demo/update", json={"version": "1.1"})
    assert refused.status_code == 409
    assert "already installed" in refused.get_json()["detail"]


def test_an_app_added_from_github_is_updated_through_a_chat(tmp_path: Path) -> None:
    engine = build_test_engine(tmp_path)
    client = _client(tmp_path, engine)
    submitted = client.post("/api/repo-adds", json={"install_file": _github_file("Repo App")}).get_json()
    client.post(f"/api/repo-adds/{submitted['pending']['id']}/confirm")
    engine.run_next_job()
    assert _store_entry(client, "repoapp")["update_kind"] == "GITHUB"
    assert client.get("/api/apps/repoapp/newest-version").status_code == 422
    assert client.post("/api/apps/repoapp/update", json={}).status_code == 422
    assert client.get("/api/apps/nothing/newest-version").status_code == 404


def _github_file(title: str) -> dict[str, object]:
    repository, commit = "https://github.com/someone/repo-app", "a" * 40
    return {
        "format": 1,
        "title": title,
        "description": "An app.",
        "repository": repository,
        "commit": commit,
        "recipe": {"kind": "python_git", "repository": repository, "commit": commit},
        "launch": {"argv": ["{env_dir}/bin/repo-app", "--host", "127.0.0.1", "--port", "{port}"]},
        "resources": {"disk_mb": 10},
    }


def _github_file_at(
    commit: str, title: str = "Repo App", repository: str = "https://github.com/someone/repo-app"
) -> dict[str, object]:
    document = _github_file(title)
    document.update(
        {
            "repository": repository,
            "commit": commit,
            "recipe": {"kind": "python_git", "repository": repository, "commit": commit},
        }
    )
    return document


def test_a_chat_hands_over_a_newer_github_version_and_the_person_confirms_the_update(tmp_path: Path) -> None:
    engine = build_test_engine(tmp_path)
    client = _client(tmp_path, engine)
    added = client.post("/api/repo-adds", json={"install_file": _github_file_at("a" * 40)}).get_json()
    client.post(f"/api/repo-adds/{added['pending']['id']}/confirm")
    engine.run_next_job()
    current = client.get("/api/added-apps/repoapp/install-file").get_json()
    assert current["installed_version"] == "a" * 12
    assert current["install_file"]["commit"] == "a" * 40

    newer = _github_file_at("b" * 40)
    answer = client.post("/api/repo-adds", json={"install_file": newer, "replaces": "repoapp", "chat_id": "chat-2"})
    assert answer.status_code == 201
    pending = answer.get_json()["pending"]
    assert (pending["slug"], pending["replaces"], pending["installed_version"], pending["version"]) == (
        "repoapp",
        "repoapp",
        "a" * 12,
        "b" * 12,
    )
    assert client.post(f"/api/repo-adds/{pending['id']}/confirm").status_code == 202
    record = engine.records.read().find("repoapp")
    assert record is not None and record.job is JobKind.UPDATE
    engine.run_next_job()

    entry = engine.added.find("repoapp")
    assert entry is not None and entry.repo_file is not None
    assert (entry.repo_file.commit, entry.app.version, entry.chat_id) == ("b" * 40, "b" * 12, "chat-2")
    assert entry.registry_url.endswith("b" * 40)
    record = engine.records.read().find("repoapp")
    assert record is not None and (record.status, record.version) == (InstallStatus.INSTALLED, "b" * 12)


def test_a_github_update_that_cannot_apply_is_refused(tmp_path: Path) -> None:
    engine = build_test_engine(tmp_path)
    client = _client(tmp_path, engine)
    added = client.post("/api/repo-adds", json={"install_file": _github_file_at("a" * 40)}).get_json()
    client.post(f"/api/repo-adds/{added['pending']['id']}/confirm")

    def refusal(document: dict[str, object], replaces: str) -> str:
        answer = client.post("/api/repo-adds", json={"install_file": document, "replaces": replaces})
        assert answer.status_code == 422
        return str(answer.get_json()["detail"])

    assert "isn't installed" in refusal(_github_file_at("b" * 40), "repoapp")
    engine.run_next_job()
    assert "already at that version" in refusal(_github_file_at("a" * 40), "repoapp")
    other = _github_file_at("b" * 40, repository="https://github.com/someone/other")
    assert "comes from" in refusal(other, "repoapp")
    assert "isn't an app you added from GitHub" in refusal(_github_file_at("b" * 40), "jupyterlab")
    assert client.get("/api/added-apps/jupyterlab/install-file").status_code == 404
