"""Request-level tests for the provider routes.

Seven routes composed hand-written dicts, against a hand-written TypeScript interface, with
no codegen and no schema between them: renaming a key passes the type checker, the linter and
both test suites while every row in the chooser renders `undefined`. These assertions are
about the WIRE -- the key sets and the status codes -- which is the part nothing else pins.
"""

from __future__ import annotations

import json
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
from flask.testing import FlaskClient

from imbue.chat.accounts import commit_account
from imbue.chat.accounts import mint_account_dir
from imbue.chat.accounts import read_index
from imbue.chat.harnesses.auth_flows import AuthFlowService
from imbue.chat.harnesses.codex.model import codex_model_options_path
from imbue.chat.harnesses.codex.model import write_codex_model_options
from imbue.chat.harnesses.pi_coding.local_model import LocalModelProfile
from imbue.chat.harnesses.signed_in import SignedIn
from imbue.chat.server import create_application
from imbue.chat.state import ChatAppState
from imbue.chat.testing import FakePexpectProcess
from imbue.chat.testing import build_test_state
from imbue.mngr_codex.app_server_client import CodexModel
from imbue.mngr_codex.app_server_client import ReasoningEffortOption


@contextmanager
def _client(auth_flows: AuthFlowService | None = None) -> Generator[FlaskClient, None, None]:
    yield create_application(build_test_state(auth_flows=auth_flows)).test_client()


@contextmanager
def _client_for(state: ChatAppState) -> Generator[FlaskClient, None, None]:
    yield create_application(state).test_client()


def _signed_in_service(tmp_path: Path) -> AuthFlowService:
    """A service that writes where the ROUTES read.

    Both sides must resolve the same store. The routes pass no home, so they resolve
    `MINDS_ACCOUNTS_ROOT` -- which conftest points at a per-test directory -- and a service
    built with an explicit home would quietly write somewhere else, leaving every listing
    empty for reasons that look like a serialization bug.
    """
    return AuthFlowService.create(home=None, work_dir=tmp_path / "work", probe=lambda *_a: SignedIn.YES)


# the shapes the client reads


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
            assert set(method) == {"id", "label", "description", "signup_url", "note", "shape", "is_primary"}
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
    assert set(payload) == {"accounts", "mru", "default"}
    (row,) = payload["accounts"]
    # `label` is the composed string for anything showing one; `provider` / `harness_label` /
    # `seq` are its parts, which the combo card renders at different sizes on one row.
    assert set(row) == {
        "id",
        "lane",
        "harness",
        "provider",
        "harness_label",
        "seq",
        "name",
        "label",
        "reauth_method",
        "signed_in_at",
    }
    assert row["provider"] == "Anthropic"
    assert row["harness_label"] == "Claude Code"
    assert row["label"] == "Anthropic (Claude Code)"
    assert payload["mru"] == account_id
    assert payload["default"] is None


def test_pinning_and_unpinning_the_default_through_the_row_route() -> None:
    first_id, _ = mint_account_dir()
    commit_account(first_id, "anthropic", "Anthropic")
    second_id, _ = mint_account_dir()
    commit_account(second_id, "google", "Google")
    with _client() as client:
        assert client.patch(f"/api/accounts/{first_id}", json={"is_default": True}).status_code == 200
        assert client.get("/api/accounts").get_json()["default"] == first_id
        # The mru is the newest sign-in; the pin stands over it.
        assert client.get("/api/accounts").get_json()["mru"] == second_id
        assert client.patch(f"/api/accounts/{first_id}", json={"is_default": False}).status_code == 200
        assert client.get("/api/accounts").get_json()["default"] is None


def test_a_non_boolean_is_default_is_refused() -> None:
    account_id, _ = mint_account_dir()
    commit_account(account_id, "anthropic", "Anthropic")
    with _client() as client:
        response = client.patch(f"/api/accounts/{account_id}", json={"is_default": "yes"})
    assert response.status_code == 400
    assert read_index().default_account is None


def test_a_patch_that_says_nothing_is_refused() -> None:
    account_id, _ = mint_account_dir()
    commit_account(account_id, "anthropic", "Anthropic")
    with _client() as client:
        response = client.patch(f"/api/accounts/{account_id}", json={})
    assert response.status_code == 400


def test_pinning_an_unknown_account_is_a_404() -> None:
    with _client() as client:
        response = client.patch("/api/accounts/nope", json={"is_default": True})
    assert response.status_code == 404


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


# the status codes the client branches on


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


# one full paste round trip through the routes


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
        flow_id = client.post("/api/accounts", json={"lane_id": "opencode-go", "method_id": "api_key"}).get_json()[
            "flow_id"
        ]

        assert client.delete(f"/api/accounts/flow/{flow_id}").status_code == 200
        assert client.get("/api/accounts").get_json()["accounts"] == []


def test_a_non_string_key_provider_is_refused_not_a_500(tmp_path: Path) -> None:
    """It becomes a KEY in pi's auth.json, so an unhashable value crashes rather than 400s."""
    with _client(_signed_in_service(tmp_path)) as client:
        started = client.post("/api/accounts", json={"lane_id": "opencode-go", "method_id": "api_key"})
        flow_id = started.get_json()["flow_id"]
        response = client.post(
            f"/api/accounts/flow/{flow_id}",
            json={"api_key": "sk-test", "key_provider": ["not", "a", "string"]},
        )
    assert response.status_code == 400
    assert "key_provider" in response.get_json()["detail"]


def test_a_key_provider_the_lane_does_not_have_is_refused(tmp_path: Path) -> None:
    """An unrecognised id was written into auth.json as a provider the lane does not have."""
    with _client(_signed_in_service(tmp_path)) as client:
        started = client.post("/api/accounts", json={"lane_id": "opencode-go", "method_id": "api_key"})
        flow_id = started.get_json()["flow_id"]
        response = client.post(
            f"/api/accounts/flow/{flow_id}",
            json={"api_key": "sk-test", "key_provider": "not-a-real-provider"},
        )
    assert response.status_code == 400


def test_a_null_name_clears_rather_than_naming_the_account_None() -> None:
    """`str(None)` is "None" -- a four-character name the user never typed, under the cap."""
    account_id, _unused = mint_account_dir()
    commit_account(account_id, "anthropic", "Anthropic")
    with _client() as client:
        client.patch(f"/api/accounts/{account_id}", json={"name": "Work"})
        assert client.patch(f"/api/accounts/{account_id}", json={"name": None}).status_code == 200
        (row,) = client.get("/api/accounts").get_json()["accounts"]
    assert row["provider"] == "Anthropic"


def test_a_non_string_name_is_refused() -> None:
    account_id, _unused = mint_account_dir()
    commit_account(account_id, "anthropic", "Anthropic")
    with _client() as client:
        response = client.patch(f"/api/accounts/{account_id}", json={"name": ["Work"]})
    assert response.status_code == 400


# what a new agent on an account could run on


def test_account_model_options_offers_the_catalog_for_a_static_harness_and_the_account_itself_for_codex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The switch dialog's picker for an account the chat is not on yet: a claude account offers its whole
    catalog (``models`` and ``options`` both null, so the page renders the catalog); a codex account is asked
    for its own, and answers from the sidecar a probe left when codex cannot be reached at all."""
    claude_id, _ = mint_account_dir()
    commit_account(claude_id, "anthropic", "Anthropic")
    codex_id, codex_dir = mint_account_dir()
    commit_account(codex_id, "openai", "OpenAI")
    # No codex on PATH, so the account probe fails to spawn and the sidecar is the answer. Without
    # this the test would shell out to whatever codex the image happens to carry, and ask the
    # network.
    monkeypatch.setenv("PATH", str(tmp_path))
    with _client_for(build_test_state()) as client:
        static = client.get(f"/api/accounts/{claude_id}/model-options")
        assert static.status_code == 200
        assert static.get_json() == {"models": None, "options": None}

        # An account nothing has ever asked offers only the default, rather than another account's models.
        empty = client.get(f"/api/accounts/{codex_id}/model-options")
        assert empty.status_code == 200
        assert empty.get_json() == {"models": None, "options": []}

        write_codex_model_options(
            codex_model_options_path(codex_dir),
            (
                CodexModel(
                    id="gpt-6-astra",
                    model="gpt-6-astra",
                    displayName="GPT-6 Astra",
                    supported_reasoning_efforts=(
                        ReasoningEffortOption(reasoningEffort="low"),
                        ReasoningEffortOption(reasoningEffort="high"),
                    ),
                ),
            ),
        )
        offered = client.get(f"/api/accounts/{codex_id}/model-options")
        assert offered.status_code == 200
        assert offered.get_json()["models"] is None
        assert [option["id"] for option in offered.get_json()["options"]] == ["gpt-6-astra"]

        assert client.get("/api/accounts/acct-nope/model-options").status_code == 404


# the relayed browser callback


_VISITOR = '{"owner": false, "user_id": "u-1", "email": "visitor@example.com"}'


def test_a_visitor_cannot_relay_a_sign_in_callback(tmp_path: Path) -> None:
    with _client(_signed_in_service(tmp_path)) as client:
        response = client.post(
            "/api/accounts/flow/any/callback",
            json={"path_and_query": "/callback?state=s"},
            headers={"X-Imbue-Identity": _VISITOR},
        )

    assert response.status_code == 403
    assert response.get_json() == {"detail": "Only the owner of this workspace can connect an AI account."}


def test_a_callback_for_a_flow_that_is_not_running_is_a_conflict(tmp_path: Path) -> None:
    with _client(_signed_in_service(tmp_path)) as client:
        response = client.post(
            "/api/accounts/flow/gone/callback",
            json={"path_and_query": "/callback?state=s"},
            headers={"X-Imbue-Identity": '{"owner": true}'},
        )

    assert response.status_code == 409


def test_a_callback_needs_its_path_and_query(tmp_path: Path) -> None:
    with _client(_signed_in_service(tmp_path)) as client:
        response = client.post("/api/accounts/flow/any/callback", json={"path_and_query": 3})

    assert response.status_code == 400


def test_a_relayed_callback_answers_with_how_the_sign_in_ended(tmp_path: Path) -> None:
    relay_url = (
        "https://claude.ai/oauth/authorize?client_id=c"
        "&redirect_uri=http%3A%2F%2Flocalhost%3A54871%2Fcallback&state=relay-state"
    )
    manual_url = "https://claude.ai/oauth/authorize?client_id=c&state=manual"

    process = FakePexpectProcess([(0, manual_url)])

    def spawner(*_args: object, env: dict[str, str], **_kwargs: object) -> FakePexpectProcess:
        Path(env["MINDS_SIGNIN_URL_FILE"]).write_text(relay_url + "\n")
        return process

    def deny(_port: int, _path: str) -> None:
        process.print_output("Login failed: No authorization code received\r\n")
        process.exit(1)

    service = AuthFlowService.create(
        home=None,
        work_dir=tmp_path / "work",
        spawner=spawner,
        probe=lambda *_a: SignedIn.YES,
        fetch_callback=deny,
    )
    with _client(service) as client:
        started = client.post("/api/accounts", json={"lane_id": "anthropic", "method_id": "subscription"}).get_json()
        response = client.post(
            f"/api/accounts/flow/{started['flow_id']}/callback",
            json={"path_and_query": "/callback?error=access_denied&state=relay-state"},
        )

    assert started["relay_url"] == relay_url
    assert response.status_code == 200
    assert response.get_json() == {
        "state": "failed",
        "detail": "Access wasn't approved on Claude's page.",
        "account_id": None,
        "provider_name": "Anthropic",
    }


def test_an_api_key_account_signs_in_again_with_a_key() -> None:
    subscription_id, _ = mint_account_dir()
    commit_account(subscription_id, "anthropic", "Anthropic")
    key_id, key_dir = mint_account_dir()
    commit_account(key_id, "anthropic", "Anthropic")
    (key_dir / "settings.json").write_text('{"env": {"ANTHROPIC_API_KEY": "sk-ant-api03-key-5c1e"}}')
    codex_key_id, codex_key_dir = mint_account_dir()
    commit_account(codex_key_id, "openai", "OpenAI")
    (codex_key_dir / "auth.json").write_text('{"auth_mode": "apikey", "OPENAI_API_KEY": "sk-proj-7d2a"}')

    with _client() as client:
        rows = client.get("/api/accounts").get_json()["accounts"]

    assert {row["id"]: row["reauth_method"] for row in rows} == {
        subscription_id: "subscription",
        key_id: "api_key",
        codex_key_id: "api_key",
    }


# a local model server


_MODEL_SERVER_ADDRESS = "box.lan:9000"


def _network_with_one_model_server() -> httpx.Client:
    """A network on which one model server answers, at ``_MODEL_SERVER_ADDRESS``, listing one model the way
    an OpenAI-compatible server does; every other address refuses the connection."""

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.netloc.decode() != _MODEL_SERVER_ADDRESS:
            raise httpx.ConnectError("connection refused", request=request)
        if request.url.path != "/v1/models":
            return httpx.Response(404, json={})
        return httpx.Response(200, json={"object": "list", "data": [{"id": "qwen2.5:7b"}]})

    return httpx.Client(transport=httpx.MockTransport(answer))


def test_a_named_model_server_answers_with_every_key_the_form_reads() -> None:
    with _client_for(build_test_state(http_client=_network_with_one_model_server())) as client:
        response = client.post("/api/local-model-servers", json={"base_url": _MODEL_SERVER_ADDRESS})

    assert response.status_code == 200
    body = response.get_json()
    assert set(body) == {"servers", "studio_link", "default_context_window", "min_context_window"}
    assert body["servers"] == [
        {
            "base_url": f"http://{_MODEL_SERVER_ADDRESS}/v1",
            "name": _MODEL_SERVER_ADDRESS,
            "models": ["qwen2.5:7b"],
            "studio_port": None,
        }
    ]
    assert body["studio_link"] == "UNAVAILABLE"
    assert body["min_context_window"] <= body["default_context_window"]


def test_a_model_server_that_does_not_answer_is_a_400_with_the_reason() -> None:
    with _client_for(build_test_state(http_client=_network_with_one_model_server())) as client:
        unanswered = client.post("/api/local-model-servers", json={"base_url": "elsewhere.lan:9000"})
        malformed = client.post("/api/local-model-servers", json={"base_url": "ftp://nope"})

    assert unanswered.status_code == 400
    assert "elsewhere.lan:9000" in unanswered.get_json()["detail"]
    assert malformed.status_code == 400


def test_a_visitor_cannot_make_the_workspace_look_for_model_servers() -> None:
    """The route fetches an address the caller chose, from inside the workspace."""
    with _client() as client:
        response = client.post(
            "/api/local-model-servers", json={"base_url": "localhost:11434"}, headers={"X-Imbue-Identity": _VISITOR}
        )

    assert response.status_code == 403


def _accept_local_model(base_url: str, api_key: str, model_id: str, context_window: int) -> LocalModelProfile:
    return LocalModelProfile(context_window=context_window, is_reasoning=False)


def test_a_local_model_sign_in_mints_an_account_and_lists_it(tmp_path: Path) -> None:
    service = AuthFlowService.create(
        home=None,
        work_dir=tmp_path / "work",
        probe=lambda *_a: SignedIn.YES,
        local_model_verifier=_accept_local_model,
    )
    with _client(service) as client:
        started = client.post("/api/accounts", json={"lane_id": "local-model", "method_id": "endpoint"})
        assert started.status_code == 200
        assert started.get_json()["shape"] == "endpoint"
        flow_id = started.get_json()["flow_id"]
        submitted = client.post(
            f"/api/accounts/flow/{flow_id}",
            json={"endpoint": {"base_url": "localhost:11434", "model_id": "qwen2.5:7b", "context_window": 32768}},
        )
        listed = client.get("/api/accounts").get_json()["accounts"]

    assert submitted.status_code == 200
    assert submitted.get_json()["state"] == "ok"
    (account,) = listed
    assert (account["lane"], account["label"], account["reauth_method"]) == (
        "local-model",
        "Local model (Pi)",
        "endpoint",
    )


# a model server on the user's computer, reached through the Imbue Studio app

_STUDIO_AGENT_ID = "agent-0123456789abcdef0123456789abcdef"
_STUDIO_ROUTES = f"/minds-api-proxy/api/v1/agents/{_STUDIO_AGENT_ID}/local-model"


class _StudioStandIn:
    """The network as a workspace away from the user's computer sees it: no model server answers
    directly, and the latchkey gateway answers for the Imbue Studio app as ``answers`` says."""

    def __init__(self, answers: dict[tuple[str, str], httpx.Response]) -> None:
        self.answers = answers
        self.requests: list[tuple[str, str, object]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host != "gateway.test":
            raise httpx.ConnectError("connection refused", request=request)
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.path, body))
        return self.answers[(request.method, request.url.path)]

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))


def _in_a_workspace_with_a_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LATCHKEY_GATEWAY", "http://gateway.test")
    monkeypatch.setenv("LATCHKEY_GATEWAY_PASSWORD", "pw")
    monkeypatch.setenv("MNGR_AGENT_ID", _STUDIO_AGENT_ID)


def test_a_workspace_away_from_the_users_computer_asks_for_the_permission_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _in_a_workspace_with_a_gateway(monkeypatch)
    studio = _StudioStandIn(
        {
            ("GET", f"{_STUDIO_ROUTES}/servers"): httpx.Response(403, json={}),
            ("POST", "/permission-requests"): httpx.Response(201, json={}),
        }
    )
    with _client_for(build_test_state(http_client=studio.client())) as client:
        first = client.post("/api/local-model-servers", json={})
        check = client.post("/api/local-model-servers", json={"is_requesting_access": False})

    assert first.get_json()["studio_link"] == "AWAITING_APPROVAL"
    assert first.get_json()["servers"] == []
    assert check.get_json()["studio_link"] == "AWAITING_APPROVAL"
    assert [(method, path) for method, path, _ in studio.requests] == [
        ("GET", f"{_STUDIO_ROUTES}/servers"),
        ("POST", "/permission-requests"),
        ("GET", f"{_STUDIO_ROUTES}/servers"),
    ]


def test_the_servers_on_the_users_computer_are_offered_once_the_user_has_allowed_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _in_a_workspace_with_a_gateway(monkeypatch)
    studio = _StudioStandIn(
        {
            ("GET", f"{_STUDIO_ROUTES}/servers"): httpx.Response(
                200, json={"servers": [{"port": 11434, "name": "Ollama", "models": ["qwen3:8b"]}]}
            )
        }
    )
    with _client_for(build_test_state(http_client=studio.client())) as client:
        body = client.post("/api/local-model-servers", json={}).get_json()

    assert body["studio_link"] == "CONNECTED"
    assert body["servers"] == [
        {"base_url": "http://127.0.0.1:11434/v1", "name": "Ollama", "models": ["qwen3:8b"], "studio_port": 11434}
    ]


def test_a_local_model_sign_in_through_the_imbue_studio_app_runs_on_the_tunnels_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _in_a_workspace_with_a_gateway(monkeypatch)
    studio = _StudioStandIn(
        {("POST", f"{_STUDIO_ROUTES}/tunnel"): httpx.Response(200, json={"workspace_port": 11435})}
    )
    verified: list[str] = []

    def verify(base_url: str, api_key: str, model_id: str, context_window: int) -> LocalModelProfile:
        verified.append(base_url)
        return LocalModelProfile(context_window=context_window, is_reasoning=False)

    service = AuthFlowService.create(
        home=None, work_dir=tmp_path / "work", probe=lambda *_a: SignedIn.YES, local_model_verifier=verify
    )
    with _client_for(build_test_state(auth_flows=service, http_client=studio.client())) as client:
        flow_id = client.post("/api/accounts", json={"lane_id": "local-model", "method_id": "endpoint"}).get_json()[
            "flow_id"
        ]
        submitted = client.post(
            f"/api/accounts/flow/{flow_id}",
            json={
                "endpoint": {
                    "base_url": "http://127.0.0.1:11434/v1",
                    "model_id": "qwen3:8b",
                    "context_window": 32768,
                    "studio_port": 11434,
                }
            },
        )

    assert submitted.status_code == 200
    assert submitted.get_json()["state"] == "ok"
    assert studio.requests == [("POST", f"{_STUDIO_ROUTES}/tunnel", {"port": 11434})]
    assert verified == ["http://127.0.0.1:11435/v1"]


def test_a_tunnel_the_imbue_studio_app_will_not_open_is_a_400_with_the_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _in_a_workspace_with_a_gateway(monkeypatch)
    studio = _StudioStandIn({("POST", f"{_STUDIO_ROUTES}/tunnel"): httpx.Response(403, json={})})
    with _client_for(build_test_state(auth_flows=_signed_in_service(tmp_path), http_client=studio.client())) as client:
        flow_id = client.post("/api/accounts", json={"lane_id": "local-model", "method_id": "endpoint"}).get_json()[
            "flow_id"
        ]
        response = client.post(
            f"/api/accounts/flow/{flow_id}",
            json={"endpoint": {"base_url": "", "model_id": "qwen3:8b", "context_window": 32768, "studio_port": 11434}},
        )

    assert response.status_code == 400
    assert "no longer allowed" in response.get_json()["detail"]


def test_an_endpoint_with_no_model_is_refused_not_a_500(tmp_path: Path) -> None:
    with _client(_signed_in_service(tmp_path)) as client:
        flow_id = client.post("/api/accounts", json={"lane_id": "local-model", "method_id": "endpoint"}).get_json()[
            "flow_id"
        ]
        response = client.post(f"/api/accounts/flow/{flow_id}", json={"endpoint": {"base_url": "localhost:11434"}})

    assert response.status_code == 400
