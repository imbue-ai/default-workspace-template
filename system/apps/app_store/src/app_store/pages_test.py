"""The routes: the page, the API document, the store's state, install and uninstall, icons and logs, the template
catalog and sizes, and the contract module."""

from pathlib import Path

from flask import Flask
from flask.testing import FlaskClient

from app_manifest.testing import assert_document_policy_admits_page
from app_schema.testing import assert_api_schema_describes_app
from app_store.engine import InstallEngine
from app_store.pages import build_pages_blueprint
from app_store.room import WorkspaceRoom
from app_store.template_sizes import TemplateSizeStore
from app_store.testing import FakeProcessMemoryReader
from app_store.testing import FakeRepositorySizeFetcher
from app_store.testing import FakeResourceReader
from app_store.testing import build_test_adder
from app_store.testing import build_test_engine
from app_store.testing import build_test_finder
from app_store.testing import make_registry_row
from getting_started.template_catalog import TemplateCatalogStore
from getting_started.testing import FakeTemplateCatalogFetcher
from getting_started.testing import catalog_document
from getting_started.testing import catalog_template_document

_CATALOG_URL = "https://example.test/catalog/new-tab-templates.json"
_MANIFEST_PATH = Path(__file__).resolve().parents[2] / "app.toml"


def _client(tmp_path: Path, engine: InstallEngine | None = None, catalog_url: str = _CATALOG_URL) -> FlaskClient:
    engine = engine if engine is not None else build_test_engine(tmp_path)
    catalog = TemplateCatalogStore(
        catalog_url=catalog_url,
        cache_path=tmp_path / "template_catalog.json",
        fetcher=FakeTemplateCatalogFetcher(
            body_by_url={_CATALOG_URL: catalog_document(catalog_template_document("digest"))}
        ),
    )
    sizes = TemplateSizeStore(
        cache_path=tmp_path / "sizes.json",
        fetcher=FakeRepositorySizeFetcher(size_by_url={"https://api.github.com/repos/someone/digest": 2048}),
    )
    contract_path = tmp_path / "app_contract.js"
    app = Flask("app-store-under-test", static_folder=None)
    app.register_blueprint(
        build_pages_blueprint(
            tmp_path / "static",
            engine,
            build_test_adder(engine, tmp_path),
            build_test_finder(engine, tmp_path),
            catalog,
            sizes,
            contract_path,
        )
    )
    return app.test_client()


def test_the_api_document_describes_every_route(tmp_path: Path) -> None:
    assert_api_schema_describes_app(_client(tmp_path), _MANIFEST_PATH)


def test_the_page_says_it_is_not_built_until_the_bundle_exists_and_then_serves_it(tmp_path: Path) -> None:
    client = _client(tmp_path)
    response = client.get("/")
    assert b"not been built" in response.data
    assert_document_policy_admits_page(response)
    assert client.get("/api/health").get_json() == {"status": "ok", "is_frontend_built": False}

    (tmp_path / "static" / "assets").mkdir(parents=True)
    (tmp_path / "static" / "index.html").write_text("<!doctype html><title>App Store</title>")
    (tmp_path / "static" / "assets" / "index.js").write_text("console.log(1)")
    assert client.get("/").get_data(as_text=True) == "<!doctype html><title>App Store</title>"
    assert client.get("/assets/index.js").status_code == 200
    assert client.get("/assets/missing.js").status_code == 404
    assert client.get("/api/health").get_json()["is_frontend_built"] is True
    # Template drawings come from the catalog's origin.
    assert "https://example.test" in response.headers["Content-Security-Policy"]


def test_the_store_answers_the_room_the_catalog_and_the_workspaces_apps(tmp_path: Path) -> None:
    engine = build_test_engine(
        tmp_path,
        registry_rows=[
            make_registry_row("files", 8300, display_name="Files", icon="<svg/>", priority="files", launcher_rank=20),
            make_registry_row("chat", 8010, display_name="Chat", priority="chat", launcher_rank=10),
            make_registry_row("my-tool", 8200, display_name="My tool", priority="user"),
            make_registry_row("system_interface", 8000, internal=True),
            make_registry_row("app-store", 8060, display_name="App Store", priority="app-store"),
        ],
    )
    assert isinstance(engine.resources, FakeResourceReader)
    engine.resources.room = WorkspaceRoom(disk_free_mb=1_000, memory_available_mb=150)

    snapshot = _client(tmp_path, engine).get("/api/store").get_json()

    assert snapshot["room"] == {"disk_free_mb": 1_000, "memory_available_mb": 150}
    assert [category["key"] for category in snapshot["categories"]] == ["data", "automation", "notes", "organize"]
    by_slug = {app["slug"]: app for app in snapshot["apps"]}
    assert len(by_slug) == 7
    assert by_slug["radicale"]["verdict"] == "TIGHT"
    assert by_slug["nodered"]["verdict"] == "WONT_FIT"
    assert "Node-RED needs about 250 MB of memory while in use" in by_slug["nodered"]["blocked_reason"]
    assert (by_slug["jupyterlab"]["memory_idle_mb"], by_slug["jupyterlab"]["memory_in_use_mb"]) == (100, 270)
    assert by_slug["datasette"]["icon_url"] == "/api/apps/datasette/icon.svg"
    assert by_slug["tiddlywiki"]["tags"] == ["notes", "organize"]
    assert by_slug["datasette"]["status"] is None
    assert [(app["name"], app["origin"]) for app in snapshot["workspace_apps"]] == [
        ("chat", "BUILT_IN"),
        ("files", "BUILT_IN"),
        ("my-tool", "MADE_HERE"),
    ]
    assert snapshot["workspace_apps"][1]["icon_svg"] == "<svg/>"
    assert snapshot["busy_with"] is None


def test_install_queues_the_app_and_a_refusal_answers_why(tmp_path: Path) -> None:
    engine = build_test_engine(tmp_path)
    client = _client(tmp_path, engine)

    queued = client.post("/api/apps/datasette/install")
    assert queued.status_code == 202
    assert queued.get_json()["record"]["status"] == "QUEUED"
    assert client.get("/api/store").get_json()["busy_with"] == "datasette"

    again = client.post("/api/apps/datasette/install")
    assert again.status_code == 409
    assert "already installed or being worked on" in again.get_json()["detail"]
    assert client.post("/api/apps/nope/install").status_code == 404

    engine.run_next_job()
    snapshot = client.get("/api/store").get_json()
    datasette = next(app for app in snapshot["apps"] if app["slug"] == "datasette")
    assert (datasette["status"], datasette["app_url"], datasette["has_log"], datasette["memory_now_mb"]) == (
        "INSTALLED",
        "http://localhost:8080",
        True,
        None,
    )
    # Once its program runs, the store reports what it uses right now.
    assert isinstance(engine.memory_reader, FakeProcessMemoryReader)
    engine.memory_reader.memory_by_program["datasette"] = 64
    snapshot = client.get("/api/store").get_json()
    assert next(app for app in snapshot["apps"] if app["slug"] == "datasette")["memory_now_mb"] == 64
    log = client.get("/api/apps/datasette/log")
    assert log.mimetype == "text/plain"
    assert "installed; it takes" in log.get_data(as_text=True)


def test_uninstall_reads_whether_to_delete_the_apps_data(tmp_path: Path) -> None:
    engine = build_test_engine(tmp_path)
    client = _client(tmp_path, engine)
    client.post("/api/apps/datasette/install")
    engine.run_next_job()

    response = client.post("/api/apps/datasette/uninstall", json={"is_deleting_data": True})
    assert response.status_code == 202
    assert response.get_json()["record"]["is_deleting_data"] is True
    assert client.post("/api/apps/datasette/uninstall", json={}).status_code == 400
    assert client.post("/api/apps/marimo/uninstall", json={"is_deleting_data": False}).status_code == 409
    assert client.post("/api/apps/nope/uninstall", json={"is_deleting_data": False}).status_code == 404


def test_icons_and_logs_are_served_per_app(tmp_path: Path) -> None:
    client = _client(tmp_path)
    icon = client.get("/api/apps/marimo/icon.svg")
    assert icon.mimetype == "image/svg+xml"
    assert icon.get_data(as_text=True).startswith("<svg")
    assert client.get("/api/apps/nope/icon.svg").status_code == 404
    assert client.get("/api/apps/marimo/log").status_code == 404
    assert client.get("/api/apps/nope/log").status_code == 404


def test_the_template_catalog_and_a_templates_size(tmp_path: Path) -> None:
    client = _client(tmp_path)
    catalog = client.get("/api/templates-catalog").get_json()
    assert [template["slug"] for template in catalog["catalog"]["templates"]] == ["digest"]
    assert catalog["catalog"]["templates"][0]["thumbnail_url"] == (
        "https://example.test/catalog/thumbnails/someone--digest.svg"
    )
    assert client.get("/api/templates/digest/size").get_json() == {"size_kb": 2048}
    assert client.get("/api/templates/nope/size").status_code == 404


def test_without_a_catalog_url_there_are_no_templates(tmp_path: Path) -> None:
    client = _client(tmp_path, catalog_url="")
    assert client.get("/api/templates-catalog").get_json() == {"catalog": None, "is_stale": False}
    assert client.get("/api/templates/digest/size").status_code == 404


def test_an_unreachable_catalog_answers_503(tmp_path: Path) -> None:
    client = _client(tmp_path, catalog_url="https://elsewhere.test/catalog.json")
    assert client.get("/api/templates-catalog").status_code == 503


def test_the_contract_module_is_served_once_the_shell_is_built(tmp_path: Path) -> None:
    client = _client(tmp_path)
    assert client.get("/_static/app_contract.js").status_code == 404
    (tmp_path / "app_contract.js").write_text("export {}")
    served = client.get("/_static/app_contract.js")
    assert (served.status_code, served.mimetype) == (200, "text/javascript")


_ADD_DEMO = {
    "source": "pypi",
    "package": "demo",
    "display_name": "Demo",
    "command": "demo --port {port} --host 127.0.0.1",
}


def test_an_app_is_checked_added_shown_as_added_and_removed_through_the_routes(tmp_path: Path) -> None:
    engine = build_test_engine(tmp_path)
    client = _client(tmp_path, engine)

    checked = client.post("/api/added-apps/check", json=_ADD_DEMO)
    assert checked.status_code == 200
    assert (checked.get_json()["version"], checked.get_json()["package_count"]) == ("1.1", 2)
    refused = client.post("/api/added-apps/check", json={**_ADD_DEMO, "package": "git+https://github.com/x/y"})
    assert refused.status_code == 422
    assert "Links aren't supported" in refused.get_json()["detail"]
    assert client.post("/api/added-apps/check", json={"source": "pypi"}).status_code == 400

    added = client.post("/api/added-apps", json=_ADD_DEMO)
    assert added.status_code == 202
    assert client.post("/api/added-apps", json=_ADD_DEMO).status_code == 422
    snapshot = client.get("/api/store").get_json()
    demo = next(app for app in snapshot["apps"] if app["slug"] == "demo")
    assert demo["is_added"] is True
    assert demo["added_with"]["command"] == ["demo", "--port", "{port}", "--host", "127.0.0.1"]
    assert demo["added_with"]["registry"] == "PyPI"
    assert (demo["memory_in_use_mb"], demo["peak_memory_mb"]) == (None, None)
    assert "Resolved with uv" in demo["resources_basis"]
    assert next(app for app in snapshot["apps"] if app["slug"] == "datasette")["is_added"] is False

    assert client.delete("/api/added-apps/demo").status_code == 422
    engine.run_next_job()
    engine.request_uninstall("demo", is_deleting_data=True)
    engine.run_next_job()
    assert client.delete("/api/added-apps/demo").get_json() == {"slug": "demo"}
    assert all(app["slug"] != "demo" for app in client.get("/api/store").get_json()["apps"])


def test_a_shipped_app_is_never_shown_as_added_even_when_an_added_entry_has_its_name(tmp_path: Path) -> None:
    engine = build_test_engine(tmp_path)
    client = _client(tmp_path, engine)
    assert client.post("/api/added-apps", json=_ADD_DEMO).status_code == 202
    demo = engine.added.find("demo")
    assert demo is not None
    engine.added.add(demo.model_copy(update={"app": demo.app.model_copy(update={"slug": "datasette"})}))

    apps = client.get("/api/store").get_json()["apps"]
    assert [app["slug"] for app in apps].count("datasette") == 1
    datasette = next(app for app in apps if app["slug"] == "datasette")
    assert (datasette["is_added"], datasette["added_with"], datasette["title"]) == (False, None, "Datasette")
    assert next(app for app in apps if app["slug"] == "demo")["is_added"] is True


def test_adding_an_app_that_cannot_be_installed_now_answers_409(tmp_path: Path) -> None:
    engine = build_test_engine(tmp_path)
    assert isinstance(engine.resources, FakeResourceReader)
    engine.resources.room = WorkspaceRoom(disk_free_mb=1, memory_available_mb=4_000)
    response = _client(tmp_path, engine).post("/api/added-apps", json=_ADD_DEMO)
    assert response.status_code == 409
    assert "needs about" in response.get_json()["detail"]
