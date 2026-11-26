from pathlib import Path

import pytest
from flask.testing import FlaskClient

from imbue.app_sdk.app_testing import assert_api_schema_describes_app
from imbue.app_sdk.app_testing import assert_request_guard_refuses_unknown_provenance
from onboarding_activity.markers import Marker
from onboarding_activity.markers import mark
from onboarding_checklist.items import read_checklist_items
from onboarding_checklist.server import create_app
from workspace_themes.testing import write_standard_theme

MANIFEST_PATH = Path(__file__).resolve().parents[2] / "app.toml"


@pytest.fixture
def static_directory(tmp_path: Path) -> Path:
    directory = tmp_path / "static"
    (directory / "assets").mkdir(parents=True)
    (directory / "index.html").write_text("<!doctype html><title>Onboarding checklist</title><div id=app></div>")
    (directory / "assets" / "index-AbCdEf12.js").write_text("console.log('bundle');")
    return directory


@pytest.fixture
def marker_directory(tmp_path: Path) -> Path:
    return tmp_path / "markers"


def _client(
    static_directory: Path, marker_directory: Path, has_user_built_app: bool = False, contract_path: Path | None = None
) -> FlaskClient:
    return create_app(
        static_directory,
        lambda: read_checklist_items(lambda: has_user_built_app, marker_directory),
        contract_path if contract_path is not None else static_directory / "no-contract.js",
    ).test_client()


@pytest.fixture
def client(static_directory: Path, marker_directory: Path) -> FlaskClient:
    return _client(static_directory, marker_directory)


def test_the_api_document_describes_every_route(client: FlaskClient) -> None:
    assert_api_schema_describes_app(client, MANIFEST_PATH)


def test_the_page_serves_the_built_document_uncached(client: FlaskClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "<div id=app></div>" in response.get_data(as_text=True)
    assert response.headers["Cache-Control"] == "no-store"
    assert "Content-Security-Policy" in response.headers


def test_a_content_named_asset_is_cached_for_good_and_a_missing_one_is_a_404(client: FlaskClient) -> None:
    hashed = client.get("/assets/index-AbCdEf12.js")
    assert hashed.status_code == 200
    assert "max-age=31536000" in hashed.headers["Cache-Control"]
    assert client.get("/assets/missing.js").status_code == 404


def test_an_unbuilt_page_says_how_to_build_it_and_health_reports_it(tmp_path: Path, marker_directory: Path) -> None:
    client = _client(tmp_path / "never-built", marker_directory)
    page = client.get("/")
    assert page.status_code == 200
    assert "npm run build" in page.get_data(as_text=True)
    assert client.get("/api/health").get_json() == {"status": "ok", "is_frontend_built": False}


def test_health_reports_a_built_page(client: FlaskClient) -> None:
    assert client.get("/api/health").get_json() == {"status": "ok", "is_frontend_built": True}


def test_the_items_route_answers_the_four_items_in_order_none_done_on_a_new_workspace(client: FlaskClient) -> None:
    response = client.get("/api/items")

    assert response.status_code == 200
    assert response.get_json() == {
        "items": [
            {"id": "new_chat", "label": "Work in parallel by starting a new chat", "is_done": False},
            {"id": "phone_access", "label": "Access Studio from your phone", "is_done": False},
            {"id": "build_app", "label": "Build an app", "is_done": False},
            {"id": "connect_data", "label": "Connect some data", "is_done": False},
        ]
    }


def test_the_items_route_reads_what_is_done_again_on_every_request(
    static_directory: Path, marker_directory: Path
) -> None:
    client = _client(static_directory, marker_directory, has_user_built_app=True)

    def done() -> list[str]:
        return [item["id"] for item in client.get("/api/items").get_json()["items"] if item["is_done"]]

    assert done() == ["build_app"]
    mark(Marker.SECOND_CHAT_STARTED, marker_directory)
    assert done() == ["new_chat", "build_app"]
    mark(Marker.PHONE_LAYOUT_USED, marker_directory)
    mark(Marker.PERMISSION_REQUEST_FILED, marker_directory)
    assert done() == ["new_chat", "phone_access", "build_app", "connect_data"]


def test_the_items_route_answers_an_item_whose_marker_cannot_be_read_as_not_done(
    static_directory: Path, tmp_path: Path
) -> None:
    not_a_directory = tmp_path / "markers"
    not_a_directory.write_text("")

    response = _client(static_directory, not_a_directory).get("/api/items")

    assert response.status_code == 200
    assert [item["is_done"] for item in response.get_json()["items"]] == [False, False, False, False]


def test_the_items_route_answers_a_visitor_as_it_answers_the_owner(client: FlaskClient) -> None:
    visitor = '{"user_id": "user-2", "email": "visitor@example.com", "owner": false}'

    response = client.get("/api/items", headers={"X-Imbue-Identity": visitor})

    assert response.status_code == 200
    assert len(response.get_json()["items"]) == 4


def test_the_contract_module_is_served_from_this_origin_once_the_shell_has_built_it(
    static_directory: Path, marker_directory: Path, tmp_path: Path
) -> None:
    contract_path = tmp_path / "app_contract.js"
    client = _client(static_directory, marker_directory, contract_path=contract_path)

    assert client.get("/_static/app_contract.js").status_code == 404
    contract_path.write_text("export function connectToShell() {}\n")
    response = client.get("/_static/app_contract.js")
    assert response.status_code == 200 and response.mimetype == "text/javascript"


def test_the_workspace_theme_is_served_from_the_pages_own_origin(
    static_directory: Path, marker_directory: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_standard_theme(tmp_path)
    monkeypatch.chdir(tmp_path)
    client = _client(static_directory, marker_directory)

    assert client.get("/_static/themes/standard/theme.css").status_code == 200
    assert client.get("/_static/themes/no-such-theme/theme.css").status_code == 404


def test_a_page_of_another_origin_and_a_header_less_request_off_loopback_are_refused(client: FlaskClient) -> None:
    assert_request_guard_refuses_unknown_provenance(client, "GET", "/api/items")
