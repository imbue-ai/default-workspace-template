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
    assert "What I remember about you, from what you've told me and the web lookup you asked for." in page
    assert "I don't remember anything about you yet." in page
    assert '<form id="add-fact"' in page
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


def test_adding_a_fact_answers_the_profile_with_it(client: FlaskClient) -> None:
    response = client.post("/api/facts/add", json={"text": "Is learning to sail"})

    assert response.status_code == 200
    added = response.get_json()["facts"][-1]
    assert (added["text"], added["is_told_by_user"], added["source_url"]) == ("Is learning to sail", True, None)


def test_an_empty_or_missing_fact_is_a_400_and_changes_nothing(client: FlaskClient, workspace: Path) -> None:
    empty = client.post("/api/facts/add", json={"text": "   "})
    missing = client.post("/api/facts/add", json={})

    assert (empty.status_code, missing.status_code) == (400, 400)
    assert empty.get_json()["detail"] == "Type something to add."
    assert (workspace / PROFILE_PATH).read_bytes() == SAMPLE_PROFILE


def test_editing_a_fact_answers_the_profile_with_its_new_words(client: FlaskClient) -> None:
    facts = client.get("/api/profile").get_json()["facts"]
    target = next(fact for fact in facts if fact["text"] == "Is learning Japanese")

    response = client.post("/api/facts/edit", json={"fact_id": target["id"], "text": "Is learning Korean"})

    assert response.status_code == 200
    edited = [fact for fact in response.get_json()["facts"] if fact["text"] == "Is learning Korean"]
    assert len(edited) == 1 and edited[0]["is_told_by_user"]


def test_a_stale_edit_is_a_409_and_an_empty_one_a_400(client: FlaskClient, workspace: Path) -> None:
    facts = client.get("/api/profile").get_json()["facts"]

    stale = client.post("/api/facts/edit", json={"fact_id": "0-0000000000000000", "text": "Anything"})
    empty = client.post("/api/facts/edit", json={"fact_id": facts[0]["id"], "text": " "})

    assert (stale.status_code, empty.status_code) == (409, 400)
    assert (workspace / PROFILE_PATH).read_bytes() == SAMPLE_PROFILE


def test_health(client: FlaskClient) -> None:
    assert client.get("/api/health").get_json() == {"status": "ok"}
