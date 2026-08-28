import json
from collections.abc import Callable

import httpx
import pytest

from imbue.observability.bugsink_api import BugsinkProvisioningError
from imbue.observability.bugsink_api import bugsink_project_names_for_tier
from imbue.observability.bugsink_api import get_or_create_project_dsn
from imbue.observability.bugsink_api import get_or_create_team
from imbue.observability.bugsink_api import parse_manage_auth_token
from imbue.observability.bugsink_api import provision_bugsink_projects
from imbue.observability.primitives import ObservabilityTierName

_TOKEN = "ab" * 20


def test_parse_manage_auth_token_extracts_the_single_token_line() -> None:
    output = f"some ssh noise\ncreated token:\n{_TOKEN}\ntrailing line\n"
    assert parse_manage_auth_token(output) == _TOKEN


def test_parse_manage_auth_token_rejects_output_with_no_token() -> None:
    with pytest.raises(BugsinkProvisioningError, match="found 0"):
        parse_manage_auth_token("no token anywhere\n")


def test_parse_manage_auth_token_rejects_output_with_multiple_tokens() -> None:
    with pytest.raises(BugsinkProvisioningError, match="found 2"):
        parse_manage_auth_token(f"{_TOKEN}\n{'cd' * 20}\n")


def test_bugsink_project_names_include_oauth_redirector_only_on_dev() -> None:
    assert bugsink_project_names_for_tier(ObservabilityTierName("dev")) == ("rsc", "llm", "oauth-redirector")
    assert bugsink_project_names_for_tier(ObservabilityTierName("staging")) == ("rsc", "llm")
    assert bugsink_project_names_for_tier(ObservabilityTierName("production")) == ("rsc", "llm")


def _client_with_handler(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_get_or_create_team_returns_existing_team_without_creating() -> None:
    requests_seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(f"{request.method} {request.url.path}")
        assert request.headers["Authorization"] == f"Bearer {_TOKEN}"
        return httpx.Response(200, json=[{"id": "team-1", "name": "server"}])

    with _client_with_handler(handler) as client:
        team_id = get_or_create_team(client, base_url="https://bugsink.invalid", token=_TOKEN, team_name="server")

    assert team_id == "team-1"
    assert requests_seen == ["GET /api/canonical/0/teams/"]


def test_get_or_create_team_accepts_a_non_string_team_id() -> None:
    # Same tolerance as the project path: any non-None id counts (a skipped
    # name match would silently attempt a duplicate create).
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(200, json=[{"id": 4, "name": "server"}])

    with _client_with_handler(handler) as client:
        team_id = get_or_create_team(client, base_url="https://bugsink.invalid", token=_TOKEN, team_name="server")

    assert team_id == "4"


def test_get_or_create_team_creates_when_absent() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"results": []})
        body = json.loads(request.content)
        assert body == {"name": "server"}
        return httpx.Response(201, json={"id": "team-new", "name": "server"})

    with _client_with_handler(handler) as client:
        team_id = get_or_create_team(client, base_url="https://bugsink.invalid", token=_TOKEN, team_name="server")

    assert team_id == "team-new"


def test_get_or_create_project_dsn_creates_and_reads_detail_dsn() -> None:
    requests_seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(f"{request.method} {request.url.path}")
        if request.method == "GET" and request.url.path.endswith("/projects/"):
            return httpx.Response(200, json=[])
        if request.method == "POST":
            assert json.loads(request.content) == {"name": "rsc", "team": "team-1"}
            return httpx.Response(201, json={"id": 7, "name": "rsc"})
        return httpx.Response(200, json={"id": 7, "dsn": "https://key@bugsink.invalid/7"})

    with _client_with_handler(handler) as client:
        dsn = get_or_create_project_dsn(
            client, base_url="https://bugsink.invalid", token=_TOKEN, team_id="team-1", project_name="rsc"
        )

    assert dsn == "https://key@bugsink.invalid/7"
    assert requests_seen == [
        "GET /api/canonical/0/projects/",
        "POST /api/canonical/0/projects/",
        "GET /api/canonical/0/projects/7/",
    ]


def test_get_or_create_project_dsn_reuses_existing_project() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/projects/"):
            return httpx.Response(200, json=[{"id": 3, "name": "llm"}])
        assert request.method == "GET"
        return httpx.Response(200, json={"id": 3, "dsn": "https://key@bugsink.invalid/3"})

    with _client_with_handler(handler) as client:
        dsn = get_or_create_project_dsn(
            client, base_url="https://bugsink.invalid", token=_TOKEN, team_id="team-1", project_name="llm"
        )

    assert dsn == "https://key@bugsink.invalid/3"


def test_get_or_create_project_dsn_rejects_detail_without_dsn() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/projects/"):
            return httpx.Response(200, json=[{"id": 3, "name": "llm"}])
        return httpx.Response(200, json={"id": 3})

    with _client_with_handler(handler) as client:
        with pytest.raises(BugsinkProvisioningError, match="carries no dsn"):
            get_or_create_project_dsn(
                client, base_url="https://bugsink.invalid", token=_TOKEN, team_id="team-1", project_name="llm"
            )


def test_api_errors_surface_status_and_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="permission denied 6127")

    with _client_with_handler(handler) as client:
        with pytest.raises(BugsinkProvisioningError, match="403.*permission denied 6127"):
            get_or_create_team(client, base_url="https://bugsink.invalid", token=_TOKEN, team_name="server")


def test_provision_bugsink_projects_maps_every_dev_project_to_its_vault_key() -> None:
    # One team lookup, then a get-or-create + detail read per project; the
    # result maps Vault keys (not project names) so the glue script can write
    # the sentry entry directly.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/teams/"):
            return httpx.Response(200, json=[{"id": "team-1", "name": "server"}])
        if request.method == "GET" and request.url.path.endswith("/projects/"):
            return httpx.Response(
                200,
                json=[
                    {"id": 1, "name": "rsc"},
                    {"id": 2, "name": "llm"},
                    {"id": 3, "name": "oauth-redirector"},
                ],
            )
        project_id = request.url.path.rstrip("/").rsplit("/", 1)[-1]
        return httpx.Response(200, json={"id": project_id, "dsn": f"https://key@bugsink.invalid/{project_id}"})

    with _client_with_handler(handler) as client:
        dsn_by_vault_key = provision_bugsink_projects(
            client, base_url="https://bugsink.invalid", token=_TOKEN, tier=ObservabilityTierName("dev")
        )

    assert dsn_by_vault_key == {
        "RSC_SENTRY_DSN": "https://key@bugsink.invalid/1",
        "LITELLM_SENTRY_DSN": "https://key@bugsink.invalid/2",
        "OAUTH_REDIRECTOR_SENTRY_DSN": "https://key@bugsink.invalid/3",
    }
