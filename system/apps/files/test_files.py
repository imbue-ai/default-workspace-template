import base64
import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from app_manifest.manifest import load_manifest
from playwright.sync_api import Page
from playwright.sync_api import Route
from playwright.sync_api import expect


_FILES_DIR = Path(__file__).parent


def _serve_files_fixture(route: Route) -> None:
    path = urlsplit(route.request.url).path
    if path.startswith("/_assets/"):
        asset = _FILES_DIR / "assets" / path.removeprefix("/_assets/")
        route.fulfill(path=asset)
        return
    fixture_name = "workspace" if path == "/" else "home"
    data = json.loads((_FILES_DIR / "fixtures" / f"{fixture_name}.json").read_text())
    data["href"] = path.rstrip("/") or "/"
    encoded = base64.b64encode(json.dumps(data).encode()).decode()
    document = (_FILES_DIR / "assets" / "index.html").read_text()
    route.fulfill(
        body=document.replace("__INDEX_DATA__", encoded).replace("__ASSETS_PREFIX__", "/_assets/"),
        content_type="text/html",
    )


@pytest.mark.timeout(120, func_only=False)
def test_default_launch_home_and_workspace_show_the_intended_folders(page: Page) -> None:
    manifest = load_manifest(_FILES_DIR / "app.toml")
    assert manifest.launch_paths[0].path == "/data/"
    page.route("http://files.test/**", _serve_files_fixture)
    page.goto(f"http://files.test{manifest.launch_paths[0].path}")
    expect(page.locator(".paths-table tbody .cell-name a")).to_have_text(["notes"])
    page.get_by_role("link", name="Workspace", exact=True).click()
    expect(page).to_have_url("http://files.test/")
    expect(page.locator(".paths-table tbody .cell-name a")).to_have_text(["data"])
    page.get_by_role("link", name="Home", exact=True).click()
    expect(page).to_have_url("http://files.test/data/")
    expect(page.locator(".paths-table tbody .cell-name a")).to_have_text(["notes"])


@pytest.mark.timeout(120, func_only=False)
def test_folder_launch_parameter_and_home_use_workspace_relative_paths(page: Page) -> None:
    page.route("http://files.test/**", _serve_files_fixture)
    page.goto("http://files.test/data/?path=/data/notes/")
    expect(page).to_have_url("http://files.test/data/notes/")
    expect(page.locator(".breadcrumb b")).to_have_text("notes")
    page.get_by_role("link", name="Home", exact=True).click()
    expect(page).to_have_url("http://files.test/data/")
