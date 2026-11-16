import hashlib
from pathlib import Path

from flask.testing import FlaskClient

from app_schema.testing import assert_api_schema_describes_app
from app_schema.testing import assert_request_guard_refuses_unknown_provenance
from markdown_editor_app.files import MarkdownFinder
from markdown_editor_app.files import WorkspaceFiles
from markdown_editor_app.server import create_app

MANIFEST_PATH = Path(__file__).resolve().parents[2] / "app.toml"


def test_the_api_document_describes_every_route(client: FlaskClient) -> None:
    assert_api_schema_describes_app(client, MANIFEST_PATH)


def test_the_page_serves_the_built_document_uncached_under_the_policy(client: FlaskClient) -> None:
    response = client.get("/?path=/x.md")
    assert response.status_code == 200
    assert '<div id="editor"></div>' in response.get_data(as_text=True)
    assert response.headers["Cache-Control"] == "no-store"
    policy = response.headers["Content-Security-Policy"]
    assert "script-src 'self'" in policy
    assert "img-src 'self' data: blob: https:" in policy
    assert "frame-src 'none'" in policy


def test_a_content_named_asset_is_cached_for_good_and_another_is_revalidated(client: FlaskClient) -> None:
    hashed = client.get("/assets/index-AbCdEf12.js")
    assert hashed.status_code == 200
    assert "max-age=31536000" in hashed.headers["Cache-Control"]
    plain = client.get("/assets/logo.svg")
    assert plain.status_code == 200
    assert plain.headers["Cache-Control"] == "no-cache"
    assert client.get("/assets/missing.js").status_code == 404


def test_the_icon_is_served_as_the_favicon(client: FlaskClient) -> None:
    response = client.get("/icon.svg")
    assert response.status_code == 200
    assert response.mimetype == "image/svg+xml"


def test_an_unbuilt_page_says_how_to_build_it_and_health_reports_it(files: WorkspaceFiles, tmp_path: Path) -> None:
    client = create_app(files, MarkdownFinder(files), tmp_path / "never-built").test_client()
    page = client.get("/")
    assert page.status_code == 200
    assert "npm run build" in page.get_data(as_text=True)
    assert client.get("/api/health").get_json() == {"status": "ok", "is_frontend_built": False}


def test_health_reports_a_built_page(client: FlaskClient) -> None:
    assert client.get("/api/health").get_json() == {"status": "ok", "is_frontend_built": True}


def test_a_file_opens_saves_and_a_stale_save_is_refused(client: FlaskClient, workspace: Path) -> None:
    plan = workspace / "plan.md"
    plan.write_text("# Plan\n")
    opened = client.get("/api/file", query_string={"path": str(plan)}).get_json()
    assert opened["text"] == "# Plan\n"
    assert opened["is_writable"] is True

    saved = client.put(
        "/api/file", json={"path": str(plan), "text": "# Plan{++ v2++}\n", "base_sha256": opened["sha256"]}
    )
    assert saved.status_code == 200
    assert plan.read_text() == "# Plan{++ v2++}\n"
    assert saved.get_json()["sha256"] == hashlib.sha256(plan.read_bytes()).hexdigest()

    version = client.get("/api/file/version", query_string={"path": str(plan)}).get_json()
    assert version["sha256"] == saved.get_json()["sha256"]

    stale = client.put("/api/file", json={"path": str(plan), "text": "lost\n", "base_sha256": opened["sha256"]})
    assert stale.status_code == 409
    assert stale.get_json() == {"detail": "The file was changed outside the editor."}
    assert plan.read_text() == "# Plan{++ v2++}\n"


def test_a_bad_request_is_explained(client: FlaskClient, workspace: Path) -> None:
    assert client.put("/api/file", json={"path": 3}).status_code == 400
    missing = client.get("/api/file", query_string={"path": str(workspace / "nope.md")})
    assert missing.status_code == 404
    assert "There is no file" in missing.get_json()["detail"]
    assert client.get("/api/file", query_string={"path": "/etc/passwd"}).status_code == 403
    assert client.get("/api/file/version", query_string={"path": str(workspace / "nope.md")}).status_code == 404


def test_the_raw_file_is_served_as_text_and_downloads_under_its_own_name(client: FlaskClient, workspace: Path) -> None:
    plan = workspace / "plan.md"
    plan.write_text("raw <script>x</script> {>>note<<}\n")
    shown = client.get("/api/file/raw", query_string={"path": str(plan)})
    assert shown.data == b"raw <script>x</script> {>>note<<}\n"
    assert shown.mimetype == "text/plain"
    assert shown.headers["X-Content-Type-Options"] == "nosniff"
    downloaded = client.get("/api/file/raw", query_string={"path": str(plan), "download": "1"})
    assert downloaded.headers["Content-Disposition"] == "attachment; filename=plan.md"
    assert client.get("/api/file/raw", query_string={"path": str(workspace / "nope.md")}).status_code == 404


def test_the_workspaces_markdown_files_are_listed(client: FlaskClient, workspace: Path) -> None:
    (workspace / "a.md").write_text("a\n")
    listed = client.get("/api/files").get_json()
    assert listed["root"] == str(workspace.resolve())
    assert [item["name"] for item in listed["files"]] == ["a.md"]


def test_a_page_of_another_origin_and_a_header_less_request_off_loopback_are_refused(client: FlaskClient) -> None:
    """The workspace's own browser is on loopback too, so the app reads a header-less request as the owner's only
    from loopback under a loopback Host, and never one its browser marks as another origin's (the request guard,
    app_schema.request_guard)."""
    assert_request_guard_refuses_unknown_provenance(client, "GET", "/api/health")
    assert_request_guard_refuses_unknown_provenance(client, "GET", "/api/files")
