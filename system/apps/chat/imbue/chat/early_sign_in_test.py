"""The early sign-in's page and routes: a sign-in made there lands in the live account store."""

from pathlib import Path

from app_schema.testing import assert_request_guard_refuses_unknown_provenance
from flask.testing import FlaskClient

from imbue.chat.accounts import account_dir
from imbue.chat.accounts import accounts_root
from imbue.chat.accounts import read_index
from imbue.chat.early_sign_in import EarlySignIn
from imbue.chat.early_sign_in import build_early_sign_in_application
from imbue.chat.harnesses.auth_flows import AuthFlowService
from imbue.chat.harnesses.signed_in import SignedIn

_DOCUMENT = "<!doctype html><html><head><title>Pick your AI provider</title></head><body></body></html>"


def _client(tmp_path: Path) -> tuple[FlaskClient, Path]:
    """The early sign-in's app over a static directory of its own; the live store is the test's own."""
    staging = tmp_path / "staging"
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "sign_in.html").write_text(_DOCUMENT)
    (static / "assets" / "sign_in-3f2a.js").write_text("console.log('chooser');")
    early_sign_in = EarlySignIn(staging_home=staging, live_home=None)
    auth_flows = AuthFlowService.create(
        home=staging,
        work_dir=tmp_path / "work",
        probe=lambda *_args: SignedIn.YES,
        commit_account=early_sign_in.commit_account,
    )
    return build_early_sign_in_application(early_sign_in, auth_flows, static).test_client(), staging


def test_the_request_guard_is_ahead_of_the_routes(tmp_path: Path) -> None:
    client, _staging = _client(tmp_path)

    assert_request_guard_refuses_unknown_provenance(client, "GET", "/api/early-sign-in")


def test_a_key_signed_in_on_the_page_is_moved_into_the_live_store(tmp_path: Path) -> None:
    client, staging = _client(tmp_path)

    started = client.post("/api/accounts", json={"lane_id": "opencode-go", "method_id": "api_key"}).get_json()
    submitted = client.post(f"/api/accounts/flow/{started['flow_id']}", json={"api_key": "sk-test-5521"})
    listed = client.get("/api/accounts").get_json()["accounts"]
    status = client.get("/api/early-sign-in").get_json()

    assert submitted.get_json()["state"] == "ok"
    (account,) = read_index().accounts
    assert account.lane == "opencode-go"
    assert account_dir(account.id).is_dir()
    assert not account_dir(account.id, staging).exists()
    assert [row["id"] for row in listed] == [account.id]
    assert status == {"is_flow_pending": False, "is_done": False, "account_ids": [account.id]}


def test_the_status_reports_a_sign_in_under_way_and_the_page_saying_it_is_done(tmp_path: Path) -> None:
    client, _staging = _client(tmp_path)

    client.post("/api/accounts", json={"lane_id": "opencode-go", "method_id": "api_key"})
    while_signing_in = client.get("/api/early-sign-in").get_json()
    client.post("/api/early-sign-in/done")
    after_done = client.get("/api/early-sign-in").get_json()

    assert while_signing_in == {"is_flow_pending": True, "is_done": False, "account_ids": []}
    assert after_done == {"is_flow_pending": True, "is_done": True, "account_ids": []}


def test_an_abandoned_sign_in_leaves_nothing_in_either_store(tmp_path: Path) -> None:
    client, staging = _client(tmp_path)

    flow_id = client.post("/api/accounts", json={"lane_id": "opencode-go", "method_id": "api_key"}).get_json()[
        "flow_id"
    ]
    client.delete(f"/api/accounts/flow/{flow_id}")

    assert read_index().accounts == ()
    assert list(accounts_root(staging).iterdir()) == [accounts_root(staging) / "index.lock"]


def test_the_root_serves_the_chooser_document_under_the_document_policy(tmp_path: Path) -> None:
    client, _staging = _client(tmp_path)

    document = client.get("/")
    asset = client.get("/assets/sign_in-3f2a.js")
    missing = client.get("/assets/nothing-here.js")

    assert document.status_code == 200
    assert '<meta name="system-interface-base-path" content="">' in document.get_data(as_text=True)
    assert "script-src 'self'" in document.headers["Content-Security-Policy"]
    assert asset.get_data(as_text=True) == "console.log('chooser');"
    assert missing.status_code == 404
