"""Request-level tests for the provider routes.

Seven routes composed hand-written dicts, against a hand-written TypeScript interface, with
no codegen and no schema between them: renaming a key passes the type checker, the linter and
both test suites while every row in the chooser renders `undefined`. These assertions are
about the WIRE -- the key sets and the status codes -- which is the part nothing else pins.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from flask.testing import FlaskClient

from imbue.system_interface.accounts import accounts_root
from imbue.system_interface.accounts import commit_account
from imbue.system_interface.accounts import mint_account_dir
from imbue.system_interface.accounts import read_index
from imbue.system_interface.harnesses.auth_flows import AuthFlowService
from imbue.system_interface.harnesses.signed_in import SignedIn
from imbue.system_interface.server import create_application
from imbue.system_interface.testing import build_test_state


@contextmanager
def _client(auth_flows: AuthFlowService | None = None) -> Iterator[FlaskClient]:
    yield create_application(build_test_state(auth_flows=auth_flows)).test_client()


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """A home whose accounts root is where the isolation fixture already points."""
    return accounts_root().parent.parent


def _signed_in_service(home: Path) -> AuthFlowService:
    return AuthFlowService.create(home=home, work_dir=home / "work", probe=lambda *_a: SignedIn.YES)


# ----- the shapes the client reads ---------------------------------------------------------


def test_lanes_carry_every_key_the_chooser_reads(home: Path) -> None:
    with _client() as client:
        response = client.get("/api/lanes")
    assert response.status_code == 200
    lanes = response.get_json()["lanes"]
    assert lanes, "the chooser renders nothing without lanes"
    for lane in lanes:
        assert set(lane) == {"id", "provider_name", "subtitle", "harness", "methods", "key_providers"}
        for method in lane["methods"]:
            assert set(method) == {"id", "label", "description", "signup_url", "shape", "is_primary"}
        for key_provider in lane["key_providers"]:
            assert set(key_provider) == {"provider_id", "display", "env_var", "hint"}


def test_exactly_one_method_per_lane_is_primary(home: Path) -> None:
    """The chooser opens the primary directly and files the rest under "other ways"."""
    with _client() as client:
        lanes = client.get("/api/lanes").get_json()["lanes"]
    for lane in lanes:
        assert [m["is_primary"] for m in lane["methods"]].count(True) == 1


def test_accounts_carry_every_key_the_picker_reads(home: Path) -> None:
    account_id, _ = mint_account_dir(home)
    commit_account(account_id, "anthropic", "Anthropic", home)
    with _client() as client:
        response = client.get("/api/accounts")
    assert response.status_code == 200
    payload = response.get_json()
    assert set(payload) == {"accounts", "mru"}
    (row,) = payload["accounts"]
    assert set(row) == {"id", "lane", "harness", "label"}
    assert row["label"] == "Anthropic (Claude Code)"
    assert payload["mru"] == account_id


def test_accounts_are_numbered_by_what_the_label_says(home: Path) -> None:
    """Two lanes run on pi and can both mint an OpenRouter account; numbering by lane gives
    two rows reading "OpenRouter (Pi)" with nothing between them."""
    for lane in ("openrouter", "api-key"):
        account_id, _ = mint_account_dir(home)
        commit_account(account_id, lane, "OpenRouter", home)
    with _client() as client:
        labels = [row["label"] for row in client.get("/api/accounts").get_json()["accounts"]]
    assert labels == ["OpenRouter (Pi)", "OpenRouter (Pi) 2"]


# ----- the status codes the client branches on ---------------------------------------------


def test_an_unknown_lane_is_a_404_with_a_clean_message(home: Path) -> None:
    with _client() as client:
        response = client.post("/api/accounts", json={"lane_id": "bogus", "method_id": "x"})
    assert response.status_code == 404
    detail = response.get_json()["detail"]
    assert "bogus" in detail
    # `LaneNotFoundError` subclasses KeyError, whose str() adds its own quotes.
    assert not detail.startswith("'"), f"the message is double-quoted: {detail}"


def test_re_authenticating_an_unknown_account_is_a_404_not_a_500(home: Path) -> None:
    with _client(_signed_in_service(home)) as client:
        response = client.post(
            "/api/accounts", json={"lane_id": "opencode-go", "method_id": "api_key", "account_id": "nope"}
        )
    assert response.status_code == 404


def test_an_account_id_that_is_a_path_is_refused(home: Path) -> None:
    """The id is joined onto a path and the resolved directory is what failure paths remove."""
    with _client(_signed_in_service(home)) as client:
        for hostile in ("../..", "/home/user/workspace"):
            response = client.post(
                "/api/accounts",
                json={"lane_id": "opencode-go", "method_id": "api_key", "account_id": hostile},
            )
            assert response.status_code == 404, hostile


def test_deleting_an_unknown_account_is_a_404(home: Path) -> None:
    with _client() as client:
        assert client.delete("/api/accounts/nope").status_code == 404


# ----- one full paste round trip through the routes ----------------------------------------


def test_a_key_paste_mints_an_account_and_lists_it(home: Path) -> None:
    with _client(_signed_in_service(home)) as client:
        started = client.post("/api/accounts", json={"lane_id": "opencode-go", "method_id": "api_key"})
        assert started.status_code == 200
        assert started.get_json()["shape"] == "paste"
        flow_id = started.get_json()["flow_id"]

        submitted = client.post(f"/api/accounts/flow/{flow_id}", json={"api_key": "sk-test"})
        assert submitted.status_code == 200
        assert submitted.get_json()["state"] == "ok"

        (row,) = client.get("/api/accounts").get_json()["accounts"]

    assert row["label"] == "Opencode Go (Pi)"
    assert row["harness"] == "pi-coding"
    assert len(read_index(home).accounts) == 1


def test_aborting_a_flow_leaves_no_account_behind(home: Path) -> None:
    with _client(_signed_in_service(home)) as client:
        flow_id = client.post(
            "/api/accounts", json={"lane_id": "opencode-go", "method_id": "api_key"}
        ).get_json()["flow_id"]

        assert client.delete(f"/api/accounts/flow/{flow_id}").status_code == 200
        assert client.get("/api/accounts").get_json()["accounts"] == []
