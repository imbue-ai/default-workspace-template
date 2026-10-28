"""What an app's own tests assert about the document it serves (``app_schema.api_schema``)."""

import inspect
from pathlib import Path

from app_manifest.manifest import load_manifest
from flask.testing import FlaskClient
from openapi_spec_validator import validate

from app_schema.api_schema import (
    API_SCHEMA_ENDPOINT,
    EXTENSION_KEY,
    OTHER_METHODS_KEY,
    RouteModels,
    RouteTable,
    flask_rule_to_openapi_path,
    is_static_endpoint,
)

# A view that reads its body through Flask directly bypasses ``parse_request_body`` and so the route table.
_RAW_BODY_READS = ("request.get_json(", "request.json", ".get_json(")


def _is_schema_endpoint(endpoint: str) -> bool:
    return endpoint.rsplit(".", 1)[-1] == API_SCHEMA_ENDPOINT


def _views_reading_their_body_raw(
    client: FlaskClient, route_table: RouteTable
) -> list[str]:
    """The routes of the table's ``RouteModels`` entries whose view reads the request body without the helper."""
    offenders: list[str] = []
    application = client.application
    for rule in application.url_map.iter_rules():
        if is_static_endpoint(rule.endpoint) or _is_schema_endpoint(rule.endpoint):
            continue
        view = application.view_functions.get(rule.endpoint)
        if view is None:
            continue
        openapi_path = flask_rule_to_openapi_path(rule.rule)
        entries = [
            route_table.get((method, openapi_path))
            for method in rule.methods or frozenset()
        ]
        is_modelled = any(
            isinstance(entry, RouteModels) and not entry.is_body_parsed_by_view
            for entry in entries
        )
        if not is_modelled:
            continue
        try:
            source = inspect.getsource(inspect.unwrap(view))
        except (OSError, TypeError):
            continue
        if any(marker in source for marker in _RAW_BODY_READS):
            offenders.append(f"{rule.endpoint} ({openapi_path})")
    return sorted(set(offenders))


def assert_api_schema_describes_app(client: FlaskClient, manifest_path: Path) -> None:
    """The app answers its manifest's ``api_schema_path`` with a valid document that describes every route.

    The document's title and description are the manifest's ``display_name``
    and ``description``; every rule of the app's url map except Flask's static
    routes and the schema route itself appears among its paths; every operation
    carries a summary (the first paragraph of its view's docstring); every
    operation is in the app's route table, with its models or the reason it
    exchanges no JSON, and the table names no route the app lacks; and no
    modelled route reads its body around ``parse_request_body`` unless its entry
    says the view parses the body itself.
    """
    manifest = load_manifest(manifest_path)
    assert manifest.api_schema_path is not None, (
        f"{manifest_path} declares no api_schema_path"
    )
    assert manifest.description is not None, f"{manifest_path} declares no description"
    response = client.get(str(manifest.api_schema_path))
    assert response.status_code == 200, response.data
    document = response.get_json()
    assert isinstance(document, dict)
    validate(document)
    assert document["info"]["title"] == manifest.display_name
    assert document["info"]["description"] == manifest.description
    expected_paths = {
        flask_rule_to_openapi_path(rule.rule)
        for rule in client.application.url_map.iter_rules()
        if not is_static_endpoint(rule.endpoint)
        and not _is_schema_endpoint(rule.endpoint)
    }
    assert set(document["paths"]) == expected_paths
    operations = {
        (method.upper(), path): operation
        for path, path_item in document["paths"].items()
        for method, operation in path_item.items()
        if method != OTHER_METHODS_KEY
    }
    undescribed = [
        f"{method} {path}"
        for (method, path), operation in operations.items()
        if "summary" not in operation
    ]
    assert not undescribed, (
        "these routes have no docstring, so the document says nothing about them: "
        + ", ".join(undescribed)
    )
    route_table: RouteTable | None = client.application.extensions.get(EXTENSION_KEY)
    assert route_table is not None, (
        "the app registered no route table: pass route_table to register_api_schema_route"
    )
    untabled = [
        f"{method} {path}"
        for (method, path), operation in operations.items()
        if (method, path) not in route_table
    ]
    assert not untabled, (
        "these routes are not in the route table (name their models, or declare them schemaless): "
        + ", ".join(untabled)
    )
    stale = [
        f"{method} {path}"
        for (method, path) in route_table
        if (method, path) not in operations
    ]
    assert not stale, (
        "the route table names routes the app does not have: " + ", ".join(stale)
    )
    raw_readers = _views_reading_their_body_raw(client, route_table)
    assert not raw_readers, (
        "these views read the request body without parse_request_body, so the table cannot vouch for them: "
        + ", ".join(raw_readers)
    )
