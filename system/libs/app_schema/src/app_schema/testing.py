"""What an app's own tests assert about the document it serves (``app_schema.api_schema``)."""

from pathlib import Path

from app_manifest.manifest import load_manifest
from flask.testing import FlaskClient
from openapi_spec_validator import validate

from app_schema.api_schema import (
    API_SCHEMA_ENDPOINT,
    OTHER_METHODS_KEY,
    flask_rule_to_openapi_path,
    is_static_endpoint,
)


def assert_api_schema_describes_app(client: FlaskClient, manifest_path: Path) -> None:
    """The app answers its manifest's ``api_schema_path`` with a valid document that describes every route.

    The document's title and description are the manifest's ``display_name``
    and ``description``, every rule of the app's url map except Flask's static
    routes and the schema route itself appears among its paths, and every
    operation carries a summary, which is the first paragraph of its view's
    docstring: a route without one is listed but says nothing about itself.
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
        and rule.endpoint.rsplit(".", 1)[-1] != API_SCHEMA_ENDPOINT
    }
    assert set(document["paths"]) == expected_paths
    undescribed = [
        f"{method.upper()} {path}"
        for path, path_item in document["paths"].items()
        for method, operation in path_item.items()
        if method != OTHER_METHODS_KEY and "summary" not in operation
    ]
    assert not undescribed, (
        "these routes have no docstring, so the document says nothing about them: "
        + ", ".join(undescribed)
    )
