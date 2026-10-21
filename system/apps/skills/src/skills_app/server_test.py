from pathlib import Path

from flask.testing import FlaskClient

from app_schema.testing import assert_api_schema_describes_app
from skills_app.server import STATIC_DIR
from skills_app.server import create_app
from skills_catalog.catalog import build_catalog
from skills_catalog.testing import commit_all
from skills_catalog.testing import write_skill

MANIFEST_PATH = Path(__file__).resolve().parents[2] / "app.toml"


def test_the_api_document_describes_every_route(client: FlaskClient) -> None:
    assert_api_schema_describes_app(client, MANIFEST_PATH)


def test_every_page_path_serves_the_one_document_with_the_shell_script(client: FlaskClient) -> None:
    for path in ("/", "/assist", "/no-such-skill"):
        response = client.get(path)
        page = response.get_data(as_text=True)
        assert response.status_code == 200, path
        assert "<title>Skills</title>" in page
        assert page.index('import { connectToShell } from "/_static/app_contract.js"') < page.index("</body>")
        assert response.headers["Cache-Control"] == "no-store"
        # The page's inline scripts are admitted by hash, so a script injected later cannot run.
        assert "script-src 'self' 'sha256-" in response.headers["Content-Security-Policy"]


def test_the_listing_answers_every_skill_with_its_origin(workspace_root: Path, client: FlaskClient) -> None:
    write_skill(workspace_root, "news-digest", "Summarize the news.")
    commit_all(workspace_root, "Crystallize news-digest")

    response = client.get("/api/skills")

    assert response.headers["Cache-Control"] == "no-store"
    assert [(skill["name"], skill["origin"]) for skill in response.get_json()["skills"]] == [
        ("assist", "BUILT_IN"),
        ("news-digest", "MADE_HERE"),
        ("notify-user", "BUILT_IN"),
    ]


def test_a_skill_written_after_the_app_started_is_listed_on_the_next_request(
    workspace_root: Path, client: FlaskClient
) -> None:
    assert len(client.get("/api/skills").get_json()["skills"]) == 2

    write_skill(workspace_root, "draft-skill", "Not saved yet.")

    by_name = {skill["name"]: skill for skill in client.get("/api/skills").get_json()["skills"]}
    assert by_name["draft-skill"]["origin"] == "NOT_SAVED"
    assert by_name["draft-skill"]["has_unsaved_changes"] is True


def test_a_skill_page_answers_its_file_as_written(workspace_root: Path, client: FlaskClient) -> None:
    response = client.get("/api/skills/assist")

    detail = response.get_json()
    assert response.status_code == 200
    assert detail["summary"]["description"] == "The template's assist."
    assert detail["skill_file_path"] == ".agents/skills/assist/SKILL.md"
    assert detail["content"] == (workspace_root / ".agents/skills/assist/SKILL.md").read_text()
    assert [file["path"] for file in detail["files"]] == ["SKILL.md"]


def test_a_missing_or_unnameable_skill_is_not_found(client: FlaskClient) -> None:
    missing = client.get("/api/skills/no-such-skill")
    hidden = client.get("/api/skills/.hidden")

    assert missing.status_code == 404
    assert "no-such-skill" in missing.get_json()["detail"]
    assert hidden.status_code == 404


def test_a_checkout_whose_history_cannot_be_read_answers_the_reason(tmp_path: Path) -> None:
    write_skill(tmp_path, "assist", "Not in a git checkout.")
    client = create_app(build_catalog(tmp_path), STATIC_DIR).test_client()

    response = client.get("/api/skills")

    assert response.status_code == 500
    assert "Couldn't read where the skills came from" in response.get_json()["detail"]


def test_only_the_two_shell_modules_are_served(client: FlaskClient) -> None:
    assert client.get("/_static/server.py").status_code == 404
    assert client.get("/_static/..%2Fserver.py").status_code == 404


def test_health_answers_ok(client: FlaskClient) -> None:
    assert client.get("/api/health").get_json() == {"status": "ok"}
