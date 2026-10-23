from pathlib import Path

import pytest
from flask.testing import FlaskClient

from app_schema.testing import assert_api_schema_describes_app
from style_guide_app.server import create_app

MANIFEST_PATH = Path(__file__).resolve().parents[2] / "app.toml"


@pytest.fixture
def static_directory(tmp_path: Path) -> Path:
    directory = tmp_path / "static"
    (directory / "assets").mkdir(parents=True)
    (directory / "index.html").write_text("<!doctype html><title>Style Guide</title><div id=app></div>")
    (directory / "assets" / "index-AbCdEf12.js").write_text("console.log('bundle');")
    (directory / "assets" / "logo.svg").write_text("<svg/>")
    return directory


@pytest.fixture
def client(static_directory: Path) -> FlaskClient:
    return create_app(static_directory).test_client()


def test_the_api_document_describes_every_route(client: FlaskClient) -> None:
    assert_api_schema_describes_app(client, MANIFEST_PATH)


def test_the_page_serves_the_built_document_uncached(client: FlaskClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "<div id=app></div>" in response.get_data(as_text=True)
    assert response.headers["Cache-Control"] == "no-store"
    assert "Content-Security-Policy" in response.headers


def test_a_content_named_asset_is_cached_for_good_and_another_is_revalidated(client: FlaskClient) -> None:
    hashed = client.get("/assets/index-AbCdEf12.js")
    assert hashed.status_code == 200
    assert "max-age=31536000" in hashed.headers["Cache-Control"]
    plain = client.get("/assets/logo.svg")
    assert plain.status_code == 200
    assert plain.headers["Cache-Control"] == "no-cache"
    assert client.get("/assets/missing.js").status_code == 404


def test_an_unbuilt_page_says_how_to_build_it_and_health_reports_it(tmp_path: Path) -> None:
    client = create_app(tmp_path / "never-built").test_client()
    page = client.get("/")
    assert page.status_code == 200
    assert "npm run build" in page.get_data(as_text=True)
    assert client.get("/api/health").get_json() == {"status": "ok", "is_frontend_built": False}


def test_health_reports_a_built_page(client: FlaskClient) -> None:
    assert client.get("/api/health").get_json() == {"status": "ok", "is_frontend_built": True}
