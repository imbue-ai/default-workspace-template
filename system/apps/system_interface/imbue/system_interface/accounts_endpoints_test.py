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


def _signed_in_service(tmp_path: Path) -> AuthFlowService:
    """A service that writes where the ROUTES read.

    Both sides must resolve the same store. The routes pass no home, so they resolve
    `MINDS_ACCOUNTS_ROOT` -- which conftest points at a per-test directory -- and a service
    built with an explicit home would quietly write somewhere else, leaving every listing
    empty for reasons that look like a serialization bug.
    """
    return AuthFlowService.create(home=None, work_dir=tmp_path / "work", probe=lambda *_a: SignedIn.YES)


# ----- the shapes the client reads ---------------------------------------------------------


def test_lanes_carry_every_key_the_chooser_reads() -> None:
    with _client() as client:
        response = client.get("/api/lanes")
    assert response.status_code == 200
    lanes = response.get_json()["lanes"]
    assert lanes, "the chooser renders nothing without lanes"
    for lane in lanes:
        # `harness_label` rides alongside `harness`: the id is what the frontend switches on and
        # the label is what the sign-in header shows ("Runs on Claude Code").
        assert set(lane) == {
            "id",
            "provider_name",
            "subtitle",
            "harness",
            "harness_label",
            "methods",
            "key_providers",
        }
        assert lane["harness_label"], "the sign-in header has nothing to name the harness with"
        for method in lane["methods"]:
            assert set(method) == {"id", "label", "description", "signup_url", "shape", "is_primary"}
        for key_provider in lane["key_providers"]:
            assert set(key_provider) == {"provider_id", "display", "env_var", "hint"}


def test_exactly_one_method_per_lane_is_primary() -> None:
    """The chooser opens the primary directly and files the rest under "other ways"."""
    with _client() as client:
        lanes = client.get("/api/lanes").get_json()["lanes"]
    for lane in lanes:
        assert [m["is_primary"] for m in lane["methods"]].count(True) == 1


def test_accounts_carry_every_key_the_picker_reads() -> None:
    account_id, _ = mint_account_dir()
    commit_account(account_id, "anthropic", "Anthropic")
    with _client() as client:
        response = client.get("/api/accounts")
    assert response.status_code == 200
    payload = response.get_json()
    assert set(payload) == {"accounts", "mru"}
    (row,) = payload["accounts"]
    # `label` is the composed string for anything showing one; `provider` / `harness_label` /
    # `seq` are its parts, which the combo card renders at different sizes on one row.
    assert set(row) == {"id", "lane", "harness", "provider", "harness_label", "seq", "name", "label"}
    assert row["provider"] == "Anthropic"
    assert row["harness_label"] == "Claude Code"
    assert row["label"] == "Anthropic (Claude Code)"
    assert payload["mru"] == account_id


def test_accounts_are_numbered_by_what_the_label_says() -> None:
    """Two lanes run on pi and can both mint an OpenRouter account; numbering by lane gives
    two rows reading "OpenRouter (Pi)" with nothing between them."""
    for lane in ("openrouter", "api-key"):
        account_id, _ = mint_account_dir()
        commit_account(account_id, lane, "OpenRouter")
    with _client() as client:
        labels = [row["label"] for row in client.get("/api/accounts").get_json()["accounts"]]
    assert labels == ["OpenRouter (Pi)", "OpenRouter 2 (Pi)"]


def test_the_number_rides_the_provider_span_the_row_actually_draws() -> None:
    """Every surface renders an account as two spans -- the provider, then the harness. A
    number living only in the composed `label` is a number nothing draws, which is how two
    "Anthropic (Claude Code)" rows ended up indistinguishable on screen."""
    for _ in range(2):
        account_id, _unused = mint_account_dir()
        commit_account(account_id, "anthropic", "Anthropic")
    with _client() as client:
        rows = client.get("/api/accounts").get_json()["accounts"]
    assert [row["provider"] for row in rows] == ["Anthropic", "Anthropic 2"]


def test_a_renamed_account_is_shown_and_numbered_under_its_new_name() -> None:
    first, _unused = mint_account_dir()
    commit_account(first, "anthropic", "Anthropic")
    second, _unused2 = mint_account_dir()
    commit_account(second, "anthropic", "Anthropic")
    with _client() as client:
        assert client.patch(f"/api/accounts/{first}", json={"name": "Work"}).status_code == 200
        rows = client.get("/api/accounts").get_json()["accounts"]
    # The rename takes the first row out of the "Anthropic" run, so the one left is no longer
    # a duplicate and loses the number it only had because of the row now called "Work".
    assert [(row["provider"], row["name"]) for row in rows] == [("Work", "Work"), ("Anthropic", "")]


def test_clearing_a_name_puts_the_provider_back() -> None:
    account_id, _unused = mint_account_dir()
    commit_account(account_id, "anthropic", "Anthropic")
    with _client() as client:
        client.patch(f"/api/accounts/{account_id}", json={"name": "Work"})
        assert client.patch(f"/api/accounts/{account_id}", json={"name": ""}).status_code == 200
        (row,) = client.get("/api/accounts").get_json()["accounts"]
    assert row["provider"] == "Anthropic"


def test_renaming_an_account_that_is_not_there_is_a_404() -> None:
    with _client() as client:
        response = client.patch("/api/accounts/nope", json={"name": "Work"})
    assert response.status_code == 404


def test_a_name_longer_than_a_row_can_show_is_refused() -> None:
    account_id, _unused = mint_account_dir()
    commit_account(account_id, "anthropic", "Anthropic")
    with _client() as client:
        response = client.patch(f"/api/accounts/{account_id}", json={"name": "x" * 200})
    assert response.status_code == 400


# ----- the status codes the client branches on ---------------------------------------------


def test_an_unknown_lane_is_a_404_with_a_clean_message() -> None:
    with _client() as client:
        response = client.post("/api/accounts", json={"lane_id": "bogus", "method_id": "x"})
    assert response.status_code == 404
    detail = response.get_json()["detail"]
    assert "bogus" in detail
    # `AccountError` must not subclass KeyError, whose `__str__` is `repr(args[0])`: a message
    # written for a person would arrive wrapped in its own quotes.
    assert not detail.startswith("'"), f"the message is double-quoted: {detail}"


def test_re_authenticating_an_unknown_account_is_a_404_not_a_500(tmp_path: Path) -> None:
    with _client(_signed_in_service(tmp_path)) as client:
        response = client.post(
            "/api/accounts", json={"lane_id": "opencode-go", "method_id": "api_key", "account_id": "nope"}
        )
    assert response.status_code == 404


def test_an_account_id_that_is_a_path_is_refused(tmp_path: Path) -> None:
    """The id is joined onto a path and the resolved directory is what failure paths remove."""
    with _client(_signed_in_service(tmp_path)) as client:
        for hostile in ("../..", "/home/user/workspace"):
            response = client.post(
                "/api/accounts",
                json={"lane_id": "opencode-go", "method_id": "api_key", "account_id": hostile},
            )
            assert response.status_code == 404, hostile


def test_deleting_an_unknown_account_is_a_404() -> None:
    with _client() as client:
        assert client.delete("/api/accounts/nope").status_code == 404


# ----- one full paste round trip through the routes ----------------------------------------


def test_a_key_paste_mints_an_account_and_lists_it(tmp_path: Path) -> None:
    with _client(_signed_in_service(tmp_path)) as client:
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
    assert len(read_index().accounts) == 1


def test_aborting_a_flow_leaves_no_account_behind(tmp_path: Path) -> None:
    with _client(_signed_in_service(tmp_path)) as client:
        flow_id = client.post(
            "/api/accounts", json={"lane_id": "opencode-go", "method_id": "api_key"}
        ).get_json()["flow_id"]

        assert client.delete(f"/api/accounts/flow/{flow_id}").status_code == 200
        assert client.get("/api/accounts").get_json()["accounts"] == []
