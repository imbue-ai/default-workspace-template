from pathlib import Path

import pytest
from flask.testing import FlaskClient

from about_me.server import create_app
from about_me.store import MEMORY_INDEX_PATH
from about_me.store import PROFILE_PATH
from about_me.store import SOURCES_PATH
from about_me.testing import SAMPLE_INDEX
from about_me.testing import SAMPLE_PROFILE
from app_schema.testing import assert_api_schema_describes_app
from app_schema.testing import assert_request_guard_refuses_unknown_provenance

MANIFEST_PATH = Path(__file__).resolve().parents[2] / "app.toml"


def test_the_api_document_describes_every_route(client: FlaskClient) -> None:
    assert_api_schema_describes_app(client, MANIFEST_PATH)


def test_the_request_guard_is_ahead_of_the_routes(client: FlaskClient) -> None:
    assert_request_guard_refuses_unknown_provenance(client, "GET", "/api/profile")


def test_the_page_carries_the_header_the_page_kit_and_the_shell_script(client: FlaskClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert "<title>What Studio knows about me</title>" in page
    assert "What I've remembered about you from the web lookup you asked for." in page
    assert "I don't know anything about you from a lookup yet." in page
    assert "View raw file" in page
    assert '<script src="/_static/workspace_theme.js"></script>' in page
    assert page.index('import { connectToShell } from "/_static/app_contract.js"') < page.index("</body>")
    policy = response.headers["Content-Security-Policy"]
    assert "default-src 'self'" in policy
    assert "connect-src" not in policy


@pytest.mark.frontend
def test_the_page_script_talks_only_to_the_apps_own_routes() -> None:
    script = (Path(__file__).parent / "static" / "about_me.js").read_text()

    assert "http" not in script.replace("HTTP_CONFLICT", "")
    assert "innerHTML" not in script


@pytest.mark.parametrize("filename", ["about_me.js", "about_me.css"])
def test_the_pages_own_assets_are_served(client: FlaskClient, filename: str) -> None:
    response = client.get(f"/assets/{filename}")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-cache"


def test_an_unknown_asset_is_a_404(client: FlaskClient) -> None:
    assert client.get("/assets/index.html").status_code == 404
    assert client.get("/_static/server.py").status_code == 404


@pytest.mark.parametrize(
    "filename", ["app_contract.js", "context_menu.js", "workspace_theme.js", "workspace_theme.css"]
)
def test_the_shell_modules_and_page_kit_are_served_from_the_apps_own_origin(
    tmp_path: Path, workspace: Path, filename: str
) -> None:
    shell_static = tmp_path / "shell_static"
    shell_static.mkdir()
    (shell_static / filename).write_text("/* built */")
    client = create_app(workspace, shell_static_directory=shell_static).test_client()

    response = client.get(f"/_static/{filename}")

    assert response.status_code == 200
    assert response.get_data(as_text=True) == "/* built */"


def test_the_profile_lists_each_fact_with_its_source_and_date(client: FlaskClient) -> None:
    response = client.get("/api/profile")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    body = response.get_json()
    first, _, _, told, loose = body["facts"]
    assert first["text"] == "Designs typefaces in Berlin"
    assert first["source_url"] == "https://example.com/portfolio"
    assert first["source_title"] == "Portfolio"
    assert first["found_on"] == "2026-11-01"
    assert told["is_told_by_user"] is True
    assert loose["is_parsed"] is False
    assert body["profile_markdown"] == SAMPLE_PROFILE.decode()


def test_the_profile_of_an_empty_workspace_is_empty(empty_workspace: Path) -> None:
    body = create_app(empty_workspace).test_client().get("/api/profile").get_json()

    assert body == {"facts": [], "profile_markdown": None, "sources_json": None}


def test_deleting_a_fact_answers_the_profile_without_it(client: FlaskClient, workspace: Path) -> None:
    facts = client.get("/api/profile").get_json()["facts"]

    response = client.post("/api/facts/delete", json={"fact_id": facts[1]["id"]})

    assert response.status_code == 200
    assert [fact["text"] for fact in response.get_json()["facts"]] == [
        fact["text"] for index, fact in enumerate(facts) if index != 1
    ]
    assert b"Writes a newsletter" not in (workspace / PROFILE_PATH).read_bytes()


def test_a_stale_delete_is_a_409_and_changes_nothing(client: FlaskClient, workspace: Path) -> None:
    fact_id = client.get("/api/profile").get_json()["facts"][0]["id"]
    edited = SAMPLE_PROFILE.replace(b"Designs typefaces", b"Designs fonts")
    (workspace / PROFILE_PATH).write_bytes(edited)

    response = client.post("/api/facts/delete", json={"fact_id": fact_id})

    assert response.status_code == 409
    assert "changed" in response.get_json()["detail"]
    assert (workspace / PROFILE_PATH).read_bytes() == edited


def test_a_delete_without_a_fact_id_is_a_400(client: FlaskClient) -> None:
    assert client.post("/api/facts/delete", json={}).status_code == 400


def test_forget_all_answers_an_empty_profile_and_removes_the_files(client: FlaskClient, workspace: Path) -> None:
    response = client.post("/api/forget")

    assert response.status_code == 200
    assert response.get_json() == {"facts": [], "profile_markdown": None, "sources_json": None}
    assert not (workspace / PROFILE_PATH).exists()
    assert not (workspace / SOURCES_PATH).exists()
    assert (workspace / MEMORY_INDEX_PATH).read_bytes() == SAMPLE_INDEX.replace(
        b"- [Public profile](user-public-profile.md) -- what the web lookup found\n", b""
    )


def test_health(client: FlaskClient) -> None:
    assert client.get("/api/health").get_json() == {"status": "ok"}
