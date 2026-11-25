import pytest
from app_schema.testing import CROSS_SITE_FETCH_HEADERS
from flask.testing import FlaskClient
from workspace_layout.identity import FETCH_MODE_HEADER
from workspace_layout.identity import FETCH_SITE_HEADER
from workspace_layout.identity import OTHER_ORIGIN_REFUSAL
from workspace_layout.identity import RequestIdentity

from imbue.system_interface.shell.testing import identity_headers
from imbue.system_interface.workspace_routes import STATUS_ROUTE
from imbue.system_interface.workspace_routes import WORKSPACE_ROUTE_TABLE

# A workspace's share address, which is where the web client's page reaches the shell.
_SHARE_BASE_URL = "https://shell-5f3c.ws.example"

_OWNER_FROM_WEB_CLIENT = RequestIdentity(
    owner=True, user_id="user-owner-9c21", email="owner@example.com", is_from_web_client=True
)
_OWNER_FROM_ELSEWHERE = RequestIdentity(owner=True, user_id="user-owner-9c21", email="owner@example.com")
_GRANTEE_FROM_WEB_CLIENT = RequestIdentity(
    owner=False, user_id="user-bob-4471", email="bob@example.com", is_from_web_client=True
)


@pytest.mark.parametrize(("method", "path"), sorted(WORKSPACE_ROUTE_TABLE))
def test_every_workspace_route_admits_the_web_clients_page_on_the_gateways_word(
    client: FlaskClient, method: str, path: str
) -> None:
    response = client.open(
        path,
        method=method,
        headers={**CROSS_SITE_FETCH_HEADERS, **identity_headers(_OWNER_FROM_WEB_CLIENT)},
        base_url=_SHARE_BASE_URL,
        json={},
    )

    # The route itself answers: a body it refuses or a state it reports, never the guard's refusal.
    assert response.status_code != 403, response.get_json()


@pytest.mark.parametrize(("method", "path"), sorted(WORKSPACE_ROUTE_TABLE))
def test_every_workspace_route_refuses_another_sites_page_that_the_gateway_did_not_vouch_for(
    client: FlaskClient, method: str, path: str
) -> None:
    response = client.open(
        path,
        method=method,
        headers={**CROSS_SITE_FETCH_HEADERS, **identity_headers(_OWNER_FROM_ELSEWHERE)},
        base_url=_SHARE_BASE_URL,
        json={},
    )

    assert response.status_code == 403
    assert response.get_json() == {"detail": OTHER_ORIGIN_REFUSAL}


def test_the_web_clients_page_reads_the_status_whether_the_browser_calls_it_same_site_or_cross_site(
    client: FlaskClient,
) -> None:
    for fetch_site in ("cross-site", "same-site"):
        response = client.get(
            STATUS_ROUTE,
            headers={
                FETCH_SITE_HEADER: fetch_site,
                FETCH_MODE_HEADER: "cors",
                **identity_headers(_OWNER_FROM_WEB_CLIENT),
            },
            base_url=_SHARE_BASE_URL,
        )

        assert response.status_code == 200
        assert response.get_json()["operations"]


def test_a_grantee_of_the_whole_workspace_is_admitted_from_the_web_client_as_the_owner_is(
    client: FlaskClient,
) -> None:
    response = client.get(
        STATUS_ROUTE,
        headers={**CROSS_SITE_FETCH_HEADERS, **identity_headers(_GRANTEE_FROM_WEB_CLIENT)},
        base_url=_SHARE_BASE_URL,
    )

    assert response.status_code == 200


@pytest.mark.parametrize(("method", "path"), [("GET", "/api/apps"), ("POST", "/api/layout"), ("GET", "/api/schema")])
def test_the_web_clients_page_is_refused_on_every_route_of_the_shell_but_the_workspace_routes(
    client: FlaskClient, method: str, path: str
) -> None:
    response = client.open(
        path,
        method=method,
        headers={**CROSS_SITE_FETCH_HEADERS, **identity_headers(_OWNER_FROM_WEB_CLIENT)},
        base_url=_SHARE_BASE_URL,
        json={},
    )

    assert response.status_code == 403
    assert response.get_json() == {"detail": OTHER_ORIGIN_REFUSAL}
