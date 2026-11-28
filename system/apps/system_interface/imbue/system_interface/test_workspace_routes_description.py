import copy
import json
from typing import Any

import pytest
from flask import Flask

from imbue.imbue_common.pytest_utils import inline_snapshot_is_updating
from imbue.system_interface.workspace_api_models import WORKSPACE_OPERATIONS
from imbue.system_interface.workspace_routes import SHARING_GRANTS_ROUTE
from imbue.system_interface.workspace_routes import STATUS_ROUTE
from imbue.system_interface.workspace_routes import TIMEZONE_ROUTE
from imbue.system_interface.workspace_routes import WORKSPACE_OPERATION_BY_ROUTE
from imbue.system_interface.workspace_routes import WORKSPACE_ROUTE_TABLE
from imbue.system_interface.workspace_routes_description import FROZEN_DESCRIPTIONS_DIRECTORY
from imbue.system_interface.workspace_routes_description import OPERATION_KEY
from imbue.system_interface.workspace_routes_description import WORKSPACE_ROUTES_DESCRIPTION_PATH
from imbue.system_interface.workspace_routes_description import build_workspace_routes_description
from imbue.system_interface.workspace_routes_description import description_compatibility_breaks


def _rendered(document: dict[str, Any]) -> str:
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def test_the_checked_in_description_is_what_the_code_produces(app: Flask, request: pytest.FixtureRequest) -> None:
    """Run this file with ``--inline-snapshot=fix`` to rewrite the description after changing a route."""
    produced = _rendered(build_workspace_routes_description(app))
    if inline_snapshot_is_updating(request.config):
        WORKSPACE_ROUTES_DESCRIPTION_PATH.write_text(produced, encoding="utf-8")

    assert WORKSPACE_ROUTES_DESCRIPTION_PATH.read_text(encoding="utf-8") == produced


def test_the_description_holds_every_workspace_route_and_no_other(app: Flask) -> None:
    description = build_workspace_routes_description(app)

    described = {(method.upper(), path) for path, path_item in description["paths"].items() for method in path_item}
    assert described == set(WORKSPACE_ROUTE_TABLE)
    status_answer = description["components"]["schemas"]["WorkspaceStatusResponse"]
    assert set(status_answer["required"]) == {"schema", "template_ref", "operations", "compatibility", "initial_data"}
    assert len(WORKSPACE_OPERATIONS) == 20


def test_every_route_but_the_status_names_its_own_operation_of_those_the_status_lists(app: Flask) -> None:
    description = build_workspace_routes_description(app)

    operation_by_route = {
        (method.upper(), path): entry.get(OPERATION_KEY)
        for path, path_item in description["paths"].items()
        for method, entry in path_item.items()
    }

    assert operation_by_route == {**WORKSPACE_OPERATION_BY_ROUTE, ("GET", STATUS_ROUTE): None}
    assert set(WORKSPACE_OPERATION_BY_ROUTE) == set(WORKSPACE_ROUTE_TABLE) - {("GET", STATUS_ROUTE)}
    assert sorted(WORKSPACE_OPERATION_BY_ROUTE.values()) == sorted(WORKSPACE_OPERATIONS)


def test_the_description_breaks_no_release_still_frozen(app: Flask) -> None:
    current = build_workspace_routes_description(app)

    breaks_by_release = {
        frozen_path.name: description_compatibility_breaks(current, json.loads(frozen_path.read_text()))
        for frozen_path in sorted(FROZEN_DESCRIPTIONS_DIRECTORY.glob("*.json"))
    }

    assert {name: breaks for name, breaks in breaks_by_release.items() if breaks} == {}


def test_a_description_is_compatible_with_itself_and_with_one_that_only_adds(app: Flask) -> None:
    frozen = build_workspace_routes_description(app)
    current = copy.deepcopy(frozen)
    current["paths"]["/api/workspace/something-new"] = {"get": {"responses": {"200": {"description": "Success"}}}}
    current["components"]["schemas"]["TimezoneResponse"]["properties"]["applied_at"] = {"type": "string"}
    current["components"]["schemas"]["TimezoneRequest"]["properties"]["reason"] = {"type": "string"}

    assert description_compatibility_breaks(frozen, frozen) == []
    assert description_compatibility_breaks(current, frozen) == []


def test_a_field_no_longer_described_by_a_request_that_takes_any_field_is_not_a_break(app: Flask) -> None:
    current = build_workspace_routes_description(app)
    frozen = copy.deepcopy(current)
    frozen["components"]["schemas"]["ProfileSnapshotRequest"]["properties"]["account_id"] = {"type": "string"}

    assert description_compatibility_breaks(current, frozen) == []


def test_a_request_field_no_longer_taken_or_newly_required_inside_an_object_is_a_break(app: Flask) -> None:
    frozen = build_workspace_routes_description(app)
    current = copy.deepcopy(frozen)
    del current["components"]["schemas"]["TimezoneRequest"]["properties"]["is_replacing"]
    del current["components"]["schemas"]["GrantListModel"]["properties"]["email_domains"]
    current["components"]["schemas"]["GrantedIdentityModel"]["required"] = ["email"]

    breaks = description_compatibility_breaks(current, frozen)

    assert f"PUT {TIMEZONE_ROUTE} no longer takes the request field 'is_replacing'" in breaks
    assert f"PUT {SHARING_GRANTS_ROUTE} no longer takes the request field 'grants.workspace.email_domains'" in breaks
    assert f"PUT {SHARING_GRANTS_ROUTE} no longer takes the request field 'grants.services.email_domains'" in breaks
    assert f"PUT {SHARING_GRANTS_ROUTE} now requires the request field 'identities.email'" in breaks


def test_a_removed_route_a_removed_answer_field_and_a_newly_required_request_field_are_breaks(app: Flask) -> None:
    frozen = build_workspace_routes_description(app)
    current = copy.deepcopy(frozen)
    del current["paths"][STATUS_ROUTE]
    del current["paths"][TIMEZONE_ROUTE]["put"]
    del current["components"]["schemas"]["SharingStateResponse"]["properties"]["is_shared"]
    del current["components"]["schemas"]["GrantListModel"]["properties"]["email_domains"]
    del current["components"]["schemas"]["WorkspaceApp"]["properties"]["icon"]
    current["components"]["schemas"]["UpdateStartRequest"]["required"] = ["ceiling"]
    current["paths"]["/api/workspace/update/skip"]["post"][OPERATION_KEY] = "update.skip.version"

    breaks = description_compatibility_breaks(current, frozen)

    assert f"{STATUS_ROUTE} is no longer served" in breaks
    assert f"PUT {TIMEZONE_ROUTE} is no longer served" in breaks
    assert "GET /api/workspace/sharing (200) no longer answers the field 'is_shared'" in breaks
    assert "GET /api/workspace/sharing (200).grants.workspace no longer answers the field 'email_domains'" in breaks
    assert "GET /api/workspace/sharing (200).grants.services no longer answers the field 'email_domains'" in breaks
    assert "GET /api/workspace/apps (200).apps no longer answers the field 'icon'" in breaks
    assert "POST /api/workspace/update/start now requires the request field 'ceiling'" in breaks
    assert "POST /api/workspace/update/skip no longer serves the operation 'update.skip'" in breaks
