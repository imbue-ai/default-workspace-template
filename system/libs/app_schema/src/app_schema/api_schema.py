"""An app's self-description: the OpenAPI 3.1 document for its HTTP API, built from its Flask routes.

A workspace app that has an HTTP API serves this document at the path its
manifest names in ``api_schema_path`` (``/api/schema`` by convention), so an
agent reaching the app, from inside the workspace or over the share, learns
what the API offers without reading the app's code. The document is built from
the Flask url map on every request: each rule except Flask's static routes (the
app's and a blueprint's) and the schema route itself is a path, each method an
operation whose summary is the first paragraph of the view's docstring (as one
line) and whose description is the whole of it, and each path parameter a
required string parameter. An app
that validates bodies with pydantic can register those models per route, and
they are published as request and response schemas, as the Imbue Studio API
does for its own.
"""

import inspect
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Final

from flask import Blueprint, Flask, Response, current_app, jsonify, request
from imbue.imbue_common.frozen_model import FrozenModel
from pydantic import BaseModel, Field

OPENAPI_VERSION: Final[str] = "3.1.0"
DEFAULT_API_SCHEMA_PATH: Final[str] = "/api/schema"
API_SCHEMA_ENDPOINT: Final[str] = "api_schema"
_IMPLICIT_METHODS: Final[frozenset[str]] = frozenset({"HEAD", "OPTIONS"})
# The methods an OpenAPI path item may carry as operations; any other (WebDAV's PROPFIND, MKCOL, MOVE, COPY)
# is listed under the path item's ``x-other-methods`` extension instead.
_OPENAPI_METHODS: Final[frozenset[str]] = frozenset(
    {"GET", "PUT", "POST", "DELETE", "PATCH", "TRACE"}
)
OTHER_METHODS_KEY: Final[str] = "x-other-methods"
_FLASK_PARAM_PATTERN: Final[re.Pattern[str]] = re.compile(r"<(?:[^:<>]+:)?([^<>]+)>")
_PARAGRAPH_BREAK_PATTERN: Final[re.Pattern[str]] = re.compile(r"\n[ \t]*\n")


class RouteModels(FrozenModel):
    """The pydantic models one route validates with, published as its request and response schemas."""

    request_model: type[BaseModel] | None = Field(
        default=None, description="The JSON request body's model, if any"
    )
    response_model: type[BaseModel] | None = Field(
        default=None, description="The primary success response's model, if any"
    )
    success_status: int = Field(
        default=200, description="The HTTP status of the primary success response"
    )


# Keyed by (HTTP method, OpenAPI path), the path in ``{param}`` form.
RouteModelsByRoute = Mapping[tuple[str, str], RouteModels]


def flask_rule_to_openapi_path(flask_rule: str) -> str:
    """``/x/<conv:name>`` as the OpenAPI path ``/x/{name}``."""
    return _FLASK_PARAM_PATTERN.sub(r"{\1}", flask_rule)


def is_static_endpoint(endpoint: str) -> bool:
    """Whether ``endpoint`` is Flask's own static-file route: ``static`` on the app, ``<blueprint>.static`` on a blueprint."""
    return endpoint.rsplit(".", 1)[-1] == "static"


def _summary_and_description(
    view_func: Callable[..., object] | None,
) -> tuple[str | None, str | None]:
    if view_func is None or not view_func.__doc__:
        return None, None
    cleaned = inspect.cleandoc(view_func.__doc__)
    if not cleaned:
        return None, None
    # The summary is the first paragraph as one line, so a first sentence that wraps is not cut short.
    first_paragraph = _PARAGRAPH_BREAK_PATTERN.split(cleaned, maxsplit=1)[0]
    summary = " ".join(line.strip() for line in first_paragraph.splitlines())
    return summary, cleaned


def _operation_id(method: str, openapi_path: str) -> str:
    path_slug = (
        openapi_path.strip("/").replace("/", "_").replace("{", "").replace("}", "")
    )
    return f"{method.lower()}_{path_slug}" if path_slug else method.lower()


def _schema_reference(model: type[BaseModel]) -> dict[str, object]:
    return {"$ref": f"#/components/schemas/{model.__name__}"}


def _build_operation(
    method: str,
    openapi_path: str,
    path_parameter_names: Sequence[str],
    view_func: Callable[..., object] | None,
    models: RouteModels | None,
) -> dict[str, object]:
    summary, description = _summary_and_description(view_func)
    success_status = str(models.success_status) if models is not None else "200"
    success: dict[str, object] = {"description": "Success"}
    if models is not None and models.response_model is not None:
        success["content"] = {
            "application/json": {"schema": _schema_reference(models.response_model)}
        }
    operation: dict[str, object] = {
        "operationId": _operation_id(method, openapi_path),
        "responses": {success_status: success, "default": {"description": "Error"}},
    }
    if summary is not None:
        operation["summary"] = summary
    if description is not None:
        operation["description"] = description
    if path_parameter_names:
        operation["parameters"] = [
            {"name": name, "in": "path", "required": True, "schema": {"type": "string"}}
            for name in path_parameter_names
        ]
    if models is not None and models.request_model is not None:
        operation["requestBody"] = {
            "required": True,
            "content": {
                "application/json": {"schema": _schema_reference(models.request_model)}
            },
        }
    return operation


def _path_parameter_names(arguments: set[str], openapi_path: str) -> tuple[str, ...]:
    """The rule's arguments that appear in the path; one a rule fills from ``defaults`` is not a path parameter."""
    return tuple(name for name in sorted(arguments) if f"{{{name}}}" in openapi_path)


def _component_schemas(models: Sequence[type[BaseModel]]) -> dict[str, object]:
    """The ``components/schemas`` map for ``models``, with each model's nested ``$defs`` hoisted beside it."""
    components: dict[str, object] = {}
    for model in models:
        schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
        for nested_name, nested_schema in schema.pop("$defs", {}).items():
            components[nested_name] = nested_schema
        components[model.__name__] = schema
    return components


def build_openapi_document(
    app: Flask,
    title: str,
    description: str,
    version: str,
    route_models: RouteModelsByRoute,
    excluded_endpoints: frozenset[str],
    # An OpenAPI document is deeply nested, loosely typed JSON, so the value type is Any rather than a TypedDict.
) -> dict[str, Any]:
    """The OpenAPI document for every route of ``app`` except Flask's static routes and ``excluded_endpoints``."""
    paths: dict[str, dict[str, object]] = {}
    referenced_models: list[type[BaseModel]] = []
    for rule in sorted(
        app.url_map.iter_rules(), key=lambda entry: (entry.rule, entry.endpoint)
    ):
        if is_static_endpoint(rule.endpoint) or rule.endpoint in excluded_endpoints:
            continue
        openapi_path = flask_rule_to_openapi_path(rule.rule)
        view_func = app.view_functions.get(rule.endpoint)
        path_item = paths.setdefault(openapi_path, {})
        for method in sorted((rule.methods or frozenset()) - _IMPLICIT_METHODS):
            if method not in _OPENAPI_METHODS:
                other_methods = path_item.setdefault(OTHER_METHODS_KEY, [])
                assert isinstance(other_methods, list)
                other_methods.append(method)
                continue
            models = route_models.get((method, openapi_path))
            path_item[method.lower()] = _build_operation(
                method,
                openapi_path,
                _path_parameter_names(rule.arguments, openapi_path),
                view_func,
                models,
            )
            if models is not None:
                referenced_models.extend(
                    model
                    for model in (models.request_model, models.response_model)
                    if model is not None
                )
    document: dict[str, Any] = {
        "openapi": OPENAPI_VERSION,
        "info": {"title": title, "version": version, "description": description},
        "paths": paths,
    }
    if referenced_models:
        document["components"] = {"schemas": _component_schemas(referenced_models)}
    return document


def register_api_schema_route(
    target: Flask | Blueprint,
    path: str,
    title: str,
    description: str,
    version: str = "1.0",
    route_models: RouteModelsByRoute | None = None,
) -> None:
    """Serve the app's OpenAPI document at ``path`` on ``target`` (the app, or a blueprint it registers).

    ``title`` and ``description`` are the manifest's ``display_name`` and
    ``description``, so the document says the same thing the launcher does.
    The document is built from the running app's url map on each request, so a
    route registered after this call is still described.
    """
    models_by_route: RouteModelsByRoute = dict(route_models or {})

    def api_schema() -> Response:
        """The OpenAPI 3.1 document describing this app's HTTP API."""
        # The route's own endpoint, whatever name the blueprint carrying it was registered under.
        assert request.endpoint is not None
        document = build_openapi_document(
            current_app,
            title=title,
            description=description,
            version=version,
            route_models=models_by_route,
            excluded_endpoints=frozenset({request.endpoint}),
        )
        return jsonify(document)

    target.add_url_rule(
        path, endpoint=API_SCHEMA_ENDPOINT, view_func=api_schema, methods=["GET"]
    )
