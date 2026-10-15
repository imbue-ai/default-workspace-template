"""Tests for the routes: the page (built or not), the assets, the health probe, the catalog, and the contract module."""

from pathlib import Path

import pytest
from flask import Flask
from flask.testing import FlaskClient

from getting_started.pages import build_pages_blueprint
from getting_started.template_catalog import TemplateCatalogStore
from getting_started.testing import FakeTemplateCatalogFetcher
from getting_started.testing import catalog_document
from getting_started.testing import catalog_template_document

_CATALOG_URL = "https://example.test/catalog/new-tab-templates.json"


def _client(
    tmp_path: Path, catalog_url: str = _CATALOG_URL, fetcher: FakeTemplateCatalogFetcher | None = None
) -> FlaskClient:
    static_directory = tmp_path / "static"
    contract_path = tmp_path / "app_contract.js"
    store = TemplateCatalogStore(
        catalog_url=catalog_url,
        cache_path=tmp_path / "template_catalog.json",
        fetcher=fetcher if fetcher is not None else FakeTemplateCatalogFetcher(),
    )
    app = Flask("getting-started-under-test", static_folder=None)
    app.register_blueprint(build_pages_blueprint(static_directory, store, contract_path))
    return app.test_client()


def test_the_page_says_it_is_not_built_until_the_bundle_exists_and_then_serves_it(tmp_path: Path) -> None:
    client = _client(tmp_path)
    response = client.get("/")
    assert response.status_code == 200
    page = response.text
    assert "The Getting Started page isn't available right now. It comes back on its own once it is rebuilt." in page
    assert 'id="reload">Reload</button>' in page and "Ask an agent to\nrebuild it" in page
    # The build command is behind "See details", and the page asks its health probe whether it has been built since.
    assert page.index("<summary>See details</summary>") < page.index("<code>npm run build</code>")
    assert 'fetch("api/health"' in page and 'import("/_static/app_contract.js")' in page
    assert client.get("/api/health").get_json() == {"status": "ok", "is_frontend_built": False}

    (tmp_path / "static" / "assets").mkdir(parents=True)
    (tmp_path / "static" / "index.html").write_text("<!doctype html><title>Getting Started</title>")
    (tmp_path / "static" / "assets" / "index.js").write_text("console.log(1)")
    built = client.get("/")
    assert built.data == b"<!doctype html><title>Getting Started</title>"
    assert built.headers["Cache-Control"] == "no-store"
    assert client.get("/api/health").get_json() == {"status": "ok", "is_frontend_built": True}
    assert client.get("/assets/index.js").data == b"console.log(1)"
    assert client.get("/assets/missing.js").status_code == 404
    assert client.get("/assets/../index.html").status_code == 404


def test_the_catalog_route_answers_the_catalog_with_resolved_thumbnails(tmp_path: Path) -> None:
    fetcher = FakeTemplateCatalogFetcher(
        body_by_url={
            _CATALOG_URL: catalog_document(
                catalog_template_document("inbox"),
                shelves=[{"key": "popular", "title": "Most popular", "slugs": ["inbox"]}],
            )
        }
    )
    response = _client(tmp_path, fetcher=fetcher).get("/api/templates-catalog")

    assert response.status_code == 200
    body = response.get_json()
    assert body["is_stale"] is False
    assert body["catalog"]["shelves"][0]["slugs"] == ["inbox"]
    (template,) = body["catalog"]["templates"]
    assert template["thumbnail_url"] == "https://example.test/catalog/thumbnails/someone--inbox.svg"
    assert "thumbnail" not in template


def test_the_catalog_route_says_when_nothing_could_be_loaded_and_answers_null_when_disabled(tmp_path: Path) -> None:
    unavailable = _client(tmp_path).get("/api/templates-catalog")
    assert unavailable.status_code == 503
    assert unavailable.get_json() == {"detail": "failed to load templates"}

    disabled = _client(tmp_path / "disabled", catalog_url="").get("/api/templates-catalog")
    assert disabled.status_code == 200
    assert disabled.get_json() == {"catalog": None, "is_stale": False}


def test_a_try_again_fetches_the_catalog_past_the_hold_after_a_failed_fetch(tmp_path: Path) -> None:
    fetcher = FakeTemplateCatalogFetcher()
    client = _client(tmp_path, fetcher=fetcher)
    assert client.get("/api/templates-catalog").status_code == 503
    fetcher.body_by_url[_CATALOG_URL] = catalog_document(catalog_template_document("inbox"))

    # An ordinary read waits out the hold that follows a failure; a person's Try again does not.
    assert client.get("/api/templates-catalog").status_code == 503
    retried = client.get("/api/templates-catalog?retry=1")

    assert retried.status_code == 200 and retried.get_json()["catalog"]["templates"][0]["slug"] == "inbox"
    assert fetcher.fetched_urls == [_CATALOG_URL, _CATALOG_URL]


@pytest.mark.parametrize("is_built", [False, True])
def test_the_contract_module_is_served_from_this_origin_once_the_shell_has_built_it(
    tmp_path: Path, is_built: bool
) -> None:
    client = _client(tmp_path)
    if is_built:
        (tmp_path / "app_contract.js").write_text("export function connectToShell() {}\n")
    response = client.get("/_static/app_contract.js")
    if is_built:
        assert response.status_code == 200
        assert response.mimetype == "text/javascript"
        assert b"connectToShell" in response.data
    else:
        assert response.status_code == 404
