"""Apps added from a GitHub repository: the install file's rules, the installs straight from a commit, holding a file
for confirmation, confirming and discarding it, and the routes a chat and the page use."""

import base64
import json
from pathlib import Path
from typing import Any

import pytest
from flask import Flask
from flask.testing import FlaskClient

from app_store.catalog import GithubBuildRecipe
from app_store.catalog import NodeGitRecipe
from app_store.catalog import PythonGitRecipe
from app_store.engine import InstallEngine
from app_store.errors import AddAppRefusedError
from app_store.errors import InstallRefusedError
from app_store.errors import StoreStateError
from app_store.pages import build_pages_blueprint
from app_store.records import InstallStatus
from app_store.repo_adds import MAX_PENDING
from app_store.repo_adds import PendingRepoAdd
from app_store.repo_adds import PendingRepoAddStore
from app_store.repo_adds import RepoAddSubmission
from app_store.repo_adds import RepoAdds
from app_store.repo_install_file import RepoInstallFile
from app_store.room import RoomVerdict
from app_store.room import WorkspaceRoom
from app_store.system import install_steps
from app_store.template_sizes import TemplateSizeStore
from app_store.testing import FakeRepositorySizeFetcher
from app_store.testing import FakeResourceReader
from app_store.testing import build_test_adder
from app_store.testing import build_test_engine
from app_store.testing import build_test_finder
from getting_started.template_catalog import TemplateCatalogStore
from getting_started.testing import FakeTemplateCatalogFetcher

_REPOSITORY = "https://github.com/simonw/datasette"
_COMMIT = "d1cc495a966cafaed3e50dad73cd88e790031704"


def _file_document(**overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "format": 1,
        "title": "Datasette Latest",
        "description": "Explore and publish data.",
        "license": "Apache-2.0",
        "repository": _REPOSITORY,
        "commit": _COMMIT,
        "recipe": {"kind": "python_git", "repository": _REPOSITORY, "commit": _COMMIT},
        "launch": {"argv": ["{env_dir}/bin/datasette", "serve", "--host", "127.0.0.1", "--port", "{port}"]},
        "resources": {"disk_mb": 30, "memory_idle_mb": 80, "memory_in_use_mb": 90},
        "basis": "Installed from this commit in 5 seconds.",
    }
    document.update(overrides)
    return document


def _install_file(**overrides: Any) -> RepoInstallFile:
    return RepoInstallFile.model_validate(_file_document(**overrides))


def _repo_adds(engine: InstallEngine, tmp_path: Path) -> RepoAdds:
    return RepoAdds(
        adder=build_test_adder(engine, tmp_path),
        pending=PendingRepoAddStore(path=engine.layout.pending_repo_adds_path),
    )


def test_installs_straight_from_a_commit_name_the_commit_and_run_the_package_tools(tmp_path: Path) -> None:
    env = tmp_path / "env"
    python = PythonGitRecipe(kind="python_git", repository=_REPOSITORY, commit=_COMMIT, subdirectory="pkg")
    assert python.pinned_version == "d1cc495a966c"
    assert [step.argv for step in install_steps(python, env)] == [
        ("uv", "venv", "--quiet", "--python", "3.12", str(env)),
        (
            "uv",
            "pip",
            "install",
            "--python",
            str(env / "bin" / "python"),
            f"git+{_REPOSITORY}@{_COMMIT}#subdirectory=pkg",
        ),
    ]
    node = NodeGitRecipe(kind="node_git", repository=_REPOSITORY, commit=_COMMIT)
    assert node.source_label == "npm package from GitHub"
    assert [step.argv for step in install_steps(node, env)] == [
        ("npm", "install", "--prefix", str(env), "--no-audit", "--no-fund", f"git+{_REPOSITORY}.git#{_COMMIT}")
    ]


def test_a_build_of_a_commit_that_is_not_a_release_is_named_by_its_commit() -> None:
    recipe = GithubBuildRecipe(
        kind="github_build",
        repository=_REPOSITORY,
        commit=_COMMIT,
        node_tools=("http-server@14.1.1",),
        build=(("{env_dir}/node_modules/.bin/http-server", "--version"),),
        output="build",
    )
    assert recipe.pinned_version == "d1cc495a966c"
    assert recipe.package_spec == "simonw/datasette (d1cc495a966c)"


@pytest.mark.parametrize(
    ("overrides", "complaint"),
    [
        ({"recipe": {"kind": "python_git", "repository": _REPOSITORY, "commit": "0" * 40}}, "not this file's commit"),
        (
            {"recipe": {"kind": "python_git", "repository": "https://github.com/someone/else", "commit": _COMMIT}},
            "not this file's repository",
        ),
        (
            {"recipe": {"kind": "python_git", "repository": _REPOSITORY, "commit": _COMMIT, "subdirectory": "../x"}},
            "plain path",
        ),
        ({"repository": "https://gitlab.com/simonw/datasette"}, "https://github.com/<owner>/<name>"),
        ({"commit": "main"}, "full 40-character hash"),
        ({"launch": {"argv": ["bash", "-c", "datasette serve --port {port}"]}}, "from the app's environment"),
        ({"launch": {"argv": ["{env_dir}/bin/datasette", "serve", "--host", "127.0.0.1"]}}, "pass the app its"),
        ({"launch": {"argv": ["{env_dir}/bin/datasette", "--host", "0.0.0.0", "--port", "{port}"]}}, "must bind"),
        ({"format": 2}, "format"),
    ],
)
def test_an_install_file_breaking_a_rule_is_refused(overrides: dict[str, Any], complaint: str) -> None:
    with pytest.raises(ValueError, match=complaint):
        _install_file(**overrides)


def test_a_submitted_file_waits_with_the_exact_commands_it_will_run(tmp_path: Path) -> None:
    engine = build_test_engine(tmp_path)
    repo_adds = _repo_adds(engine, tmp_path)
    pending = repo_adds.submit(RepoAddSubmission(install_file=_install_file(), chat_id="chat-1"))
    assert pending.slug == "datasettelatest"
    assert engine.added.read() == ()
    assert engine.records.read().find("datasettelatest") is None

    view = repo_adds.view(pending)
    env = engine.layout.env_dir("datasettelatest")
    assert view.install_commands[-1] == f"uv pip install --python {env}/bin/python git+{_REPOSITORY}@{_COMMIT}"
    assert view.start_command == f"{env}/bin/datasette serve --host 127.0.0.1 --port 8090"
    assert view.commit_url == f"{_REPOSITORY}/commit/{_COMMIT}"
    assert view.repository == "simonw/datasette"
    assert view.basis == "Installed from this commit in 5 seconds."
    assert view.verdict is RoomVerdict.FITS
    assert view.chat_id == "chat-1"


def test_a_name_something_already_has_is_refused(tmp_path: Path) -> None:
    repo_adds = _repo_adds(build_test_engine(tmp_path), tmp_path)
    with pytest.raises(AddAppRefusedError, match="already has that name"):
        repo_adds.submit(RepoAddSubmission(install_file=_install_file(title="Datasette")))
    with pytest.raises(AddAppRefusedError, match="usable name"):
        repo_adds.submit(RepoAddSubmission(install_file=_install_file(title="!")))


def test_confirming_adds_it_as_an_added_app_from_github_and_queues_its_install(tmp_path: Path) -> None:
    engine = build_test_engine(tmp_path)
    repo_adds = _repo_adds(engine, tmp_path)
    pending = repo_adds.submit(RepoAddSubmission(install_file=_install_file(), chat_id="chat-1"))
    record = repo_adds.confirm(pending.id)
    assert record.status is InstallStatus.QUEUED
    assert repo_adds.pending.read() == ()
    added = engine.added.find("datasettelatest")
    assert added is not None
    assert (added.source, added.package, added.chat_id) == ("github", "simonw/datasette", "chat-1")
    assert added.registry_url == f"{_REPOSITORY}/commit/{_COMMIT}"
    assert added.repo_file == _install_file()
    assert added.app.version == "d1cc495a966c"
    assert added.app.source_url == f"{_REPOSITORY}/tree/{_COMMIT}"
    assert "Nobody has reviewed it" in added.app.about

    engine.run_next_job()
    installed = engine.records.read().find("datasettelatest")
    assert installed is not None and installed.status is InstallStatus.INSTALLED
    with pytest.raises(AddAppRefusedError, match="no longer waiting"):
        repo_adds.confirm(pending.id)


def test_an_add_that_will_not_fit_is_refused_and_keeps_waiting(tmp_path: Path) -> None:
    engine = build_test_engine(tmp_path)
    assert isinstance(engine.resources, FakeResourceReader)
    repo_adds = _repo_adds(engine, tmp_path)
    pending = repo_adds.submit(RepoAddSubmission(install_file=_install_file()))
    engine.resources.room = WorkspaceRoom(disk_free_mb=30_000, memory_available_mb=10)
    with pytest.raises(InstallRefusedError):
        repo_adds.confirm(pending.id)
    assert engine.added.read() == ()
    assert repo_adds.pending.find(pending.id) is not None


def test_discarding_drops_it_and_the_oldest_go_first_past_the_limit(tmp_path: Path) -> None:
    engine = build_test_engine(tmp_path)
    repo_adds = _repo_adds(engine, tmp_path)
    first = repo_adds.submit(RepoAddSubmission(install_file=_install_file()))
    repo_adds.discard(first.id)
    assert repo_adds.pending.read() == ()
    with pytest.raises(AddAppRefusedError, match="no longer waiting"):
        repo_adds.discard(first.id)
    ids = [repo_adds.submit(RepoAddSubmission(install_file=_install_file())).id for _ in range(MAX_PENDING + 2)]
    assert [pending.id for pending in repo_adds.pending.read()] == ids[2:]


def test_a_damaged_pending_file_is_read_as_empty(tmp_path: Path) -> None:
    store = PendingRepoAddStore(path=tmp_path / "pending.json")
    store.path.write_text(json.dumps({"format": 9, "pending": []}))
    assert store.read() == ()
    store.path.write_text(json.dumps({"format": 1, "pending": [{"id": "x"}]}))
    assert store.read() == ()


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


def test_a_chat_hands_over_a_file_the_page_shows_it_and_the_person_confirms_it(tmp_path: Path) -> None:
    engine = build_test_engine(tmp_path)
    client = _client(tmp_path, engine)
    refused = client.post("/api/repo-adds", json={"install_file": _file_document(commit="main")})
    assert refused.status_code == 400
    assert "full 40-character hash" in refused.get_json()["detail"]
    taken = client.post("/api/repo-adds", json={"install_file": _file_document(title="Datasette")})
    assert taken.status_code == 422

    answer = client.post("/api/repo-adds", json={"install_file": _file_document(), "chat_id": "chat-1"})
    assert answer.status_code == 201
    pending_id = answer.get_json()["pending"]["id"]
    snapshot = client.get("/api/store").get_json()
    assert [pending["id"] for pending in snapshot["pending_repo_adds"]] == [pending_id]
    assert client.get(f"/api/repo-adds/{pending_id}").get_json()["pending"]["title"] == "Datasette Latest"

    confirmed = client.post(f"/api/repo-adds/{pending_id}/confirm")
    assert confirmed.status_code == 202
    snapshot = client.get("/api/store").get_json()
    assert snapshot["pending_repo_adds"] == []
    added = next(app for app in snapshot["apps"] if app["slug"] == "datasettelatest")
    assert added["is_added"] is True
    assert added["added_with"]["registry"] == "GitHub"
    assert added["added_with"]["chat_id"] == "chat-1"
    assert added["source_label"] == "Python package from GitHub"
    assert client.get(f"/api/repo-adds/{pending_id}").status_code == 404


def test_a_discarded_file_is_gone_from_the_page(tmp_path: Path) -> None:
    engine = build_test_engine(tmp_path)
    client = _client(tmp_path, engine)
    pending_id = client.post("/api/repo-adds", json={"install_file": _file_document()}).get_json()["pending"]["id"]
    assert client.delete(f"/api/repo-adds/{pending_id}").get_json() == {"id": pending_id}
    assert client.get("/api/store").get_json()["pending_repo_adds"] == []
    assert client.delete(f"/api/repo-adds/{pending_id}").status_code == 404
    assert client.post(f"/api/repo-adds/{pending_id}/confirm").status_code == 422


def test_a_python_install_from_a_commit_can_ask_for_extras(tmp_path: Path) -> None:
    recipe = PythonGitRecipe(
        kind="python_git", repository=_REPOSITORY, commit=_COMMIT, package_name="datasette", extras=("web", "s3")
    )
    assert recipe.requirement == f"datasette[web,s3] @ git+{_REPOSITORY}@{_COMMIT}"
    assert install_steps(recipe, tmp_path)[-1].argv[-1] == recipe.requirement
    with pytest.raises(ValueError, match="need the package's own name"):
        PythonGitRecipe(kind="python_git", repository=_REPOSITORY, commit=_COMMIT, extras=("web",))
    with pytest.raises(ValueError, match="plain name"):
        PythonGitRecipe(
            kind="python_git", repository=_REPOSITORY, commit=_COMMIT, package_name="x", extras=("web]; rm",)
        )
    with pytest.raises(ValueError, match="plain package name"):
        PythonGitRecipe(kind="python_git", repository=_REPOSITORY, commit=_COMMIT, package_name="x y")


_PNG = b"\x89PNG\r\n\x1a\n" + b"picture"


def test_the_trials_screenshot_is_kept_shown_and_dropped_with_the_pending_add(tmp_path: Path) -> None:
    engine = build_test_engine(tmp_path)
    client = _client(tmp_path, engine)
    encoded = base64.b64encode(_PNG).decode("ascii")
    pending = client.post(
        "/api/repo-adds", json={"install_file": _file_document(), "screenshot_png_base64": encoded}
    ).get_json()["pending"]
    assert pending["screenshot_url"] == f"/api/repo-adds/{pending['id']}/screenshot.png"
    shot = client.get(pending["screenshot_url"])
    assert (shot.status_code, shot.mimetype, shot.data) == (200, "image/png", _PNG)

    without = client.post("/api/repo-adds", json={"install_file": _file_document(title="Other App")}).get_json()[
        "pending"
    ]
    assert without["screenshot_url"] is None
    assert client.get(f"/api/repo-adds/{without['id']}/screenshot.png").status_code == 404

    client.delete(f"/api/repo-adds/{pending['id']}")
    assert client.get(pending["screenshot_url"]).status_code == 404
    store = PendingRepoAddStore(path=engine.layout.pending_repo_adds_path)
    assert not store.screenshot_path(pending["id"]).exists()


@pytest.mark.parametrize(
    ("encoded", "complaint"),
    [
        ("not base64!", "isn't valid base64"),
        (base64.b64encode(b"GIF89a....").decode("ascii"), "isn't a PNG"),
        (base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * (5 * 1024 * 1024)).decode("ascii"), "too large"),
    ],
)
def test_a_screenshot_that_is_not_a_sane_png_is_refused(tmp_path: Path, encoded: str, complaint: str) -> None:
    client = _client(tmp_path, build_test_engine(tmp_path))
    answer = client.post("/api/repo-adds", json={"install_file": _file_document(), "screenshot_png_base64": encoded})
    assert answer.status_code == 422
    assert complaint in answer.get_json()["detail"]


def test_the_oldest_pending_adds_screenshots_go_with_them(tmp_path: Path) -> None:
    engine = build_test_engine(tmp_path)
    repo_adds = _repo_adds(engine, tmp_path)
    encoded = base64.b64encode(_PNG).decode("ascii")
    first = repo_adds.submit(RepoAddSubmission(install_file=_install_file(), screenshot_png_base64=encoded))
    for _ in range(MAX_PENDING):
        repo_adds.submit(RepoAddSubmission(install_file=_install_file()))
    assert repo_adds.pending.find(first.id) is None
    assert not repo_adds.pending.screenshot_path(first.id).exists()


def test_a_pending_add_that_cannot_be_saved_leaves_no_screenshot_behind(tmp_path: Path) -> None:
    path = tmp_path / "pending-repo-adds.json"
    # A folder where the file should be: the atomic rename onto it fails.
    path.mkdir()
    store = PendingRepoAddStore(path=path)
    pending = PendingRepoAdd(
        id="abc123", slug="datasettelatest", install_file=_install_file(), chat_id="", submitted_at="2026-11-06"
    )
    with pytest.raises(StoreStateError, match="cannot save the pending adds"):
        store.add(pending, _PNG)
    assert not store.screenshot_path("abc123").exists()
    assert store.read() == ()
