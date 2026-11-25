"""The share gateway and the shell agree on what the hosted web client's page may call.

The gateway vouches for a request by the path it is for, the shell's request guard admits it by the route it
matches, and caddy answers the browser's permission check for the methods it names. Each lives in its own package
and imports nothing of the other's, so these hold them to one another.
"""

from imbue.system_interface.workspace_routes import (
    WORKSPACE_ROUTE_PREFIX,
    WORKSPACE_ROUTE_TABLE,
)
from share_gateway.identity import RequesterIdentity, render_identity_header
from share_gateway.web_client import (
    WEB_CLIENT_ALLOWED_METHODS,
    WEB_CLIENT_MARK_FIELD,
    WEB_CLIENT_ROUTE_PATH_PREFIX,
)
from workspace_layout.identity import RequestIdentity, is_vouched_web_client_request


def test_the_gateway_vouches_for_exactly_the_paths_the_shell_names() -> None:
    assert WEB_CLIENT_ROUTE_PATH_PREFIX == f"{WORKSPACE_ROUTE_PREFIX}/"
    assert all(
        path.startswith(WEB_CLIENT_ROUTE_PATH_PREFIX)
        for _method, path in WORKSPACE_ROUTE_TABLE
    )


def test_the_permission_check_allows_every_method_a_workspace_route_uses() -> None:
    allowed = set(WEB_CLIENT_ALLOWED_METHODS.split(", "))

    assert {method for method, _path in WORKSPACE_ROUTE_TABLE} <= allowed


def test_the_mark_the_gateway_stamps_is_the_one_the_request_guard_reads() -> None:
    requester = RequesterIdentity(
        user_id="user-owner-9c21", email="owner@example.com", is_owner=True
    )

    assert WEB_CLIENT_MARK_FIELD in RequestIdentity.model_fields
    assert (
        is_vouched_web_client_request(
            render_identity_header(requester, is_from_web_client=True)
        )
        is True
    )
    assert (
        is_vouched_web_client_request(
            render_identity_header(requester, is_from_web_client=False)
        )
        is False
    )
