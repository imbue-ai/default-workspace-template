"""The description of the workspace routes that is checked in beside the app (``workspace_routes_api.json``), and
the promise a release makes about it.

The shell's ``/api/schema`` is built when the shell runs, so a client's code cannot be held to it before a
workspace exists. The workspace routes alone are therefore also described in a file in the tree, which a test keeps
equal to what the code produces, and which the Imbue Studio clients are held to and generated from. Each route's
entry names the operation it serves (``x-workspace-operation``), the name a client looks for in the status route's
answer.

For as long as a client that calls a route is supported, the route keeps its path and its operation's name, no
field of an answer is removed, and no field of a request becomes required. Each release freezes a copy of the description
(``workspace_routes_api_frozen/``), and ``description_compatibility_breaks`` holds the current description to every
copy still there.
"""

from collections.abc import Mapping
from pathlib import Path
from typing import Any
from typing import Final

from app_schema.api_schema import build_openapi_document
from flask import Flask

from imbue.system_interface.workspace_routes import WORKSPACE_OPERATION_BY_ROUTE
from imbue.system_interface.workspace_routes import WORKSPACE_ROUTE_PREFIX
from imbue.system_interface.workspace_routes import WORKSPACE_ROUTE_TABLE

_APP_DIRECTORY: Final[Path] = Path(__file__).resolve().parents[2]
WORKSPACE_ROUTES_DESCRIPTION_PATH: Final[Path] = _APP_DIRECTORY / "workspace_routes_api.json"
FROZEN_DESCRIPTIONS_DIRECTORY: Final[Path] = _APP_DIRECTORY / "workspace_routes_api_frozen"

_DESCRIPTION_TITLE: Final[str] = "Workspace routes"
_DESCRIPTION_TEXT: Final[str] = (
    "The state a workspace owns, read and changed over HTTP by a client outside it: its sharing grants, its profile "
    "snapshot and timezone, its backup configuration, its updates, its suggestion switches, its apps and its "
    "wallpaper."
)
_DESCRIPTION_VERSION: Final[str] = "1"
# The key under which a route's entry names the operation it serves, which a client looks for in the status
# route's answer before it calls the route.
OPERATION_KEY: Final[str] = "x-workspace-operation"
_SCHEMA_REFERENCE_PREFIX: Final[str] = "#/components/schemas/"
_OPENAPI_METHODS: Final[frozenset[str]] = frozenset({"get", "put", "post", "delete", "patch"})


def build_workspace_routes_description(app: Flask) -> dict[str, Any]:
    """The OpenAPI document of the workspace routes of ``app`` alone."""
    document = build_openapi_document(
        app,
        title=_DESCRIPTION_TITLE,
        description=_DESCRIPTION_TEXT,
        version=_DESCRIPTION_VERSION,
        route_table=WORKSPACE_ROUTE_TABLE,
        excluded_endpoints=frozenset(),
    )
    document["paths"] = {
        path: path_item
        for path, path_item in document["paths"].items()
        if path.startswith(WORKSPACE_ROUTE_PREFIX + "/")
    }
    for (method, path), operation in WORKSPACE_OPERATION_BY_ROUTE.items():
        document["paths"][path][method.lower()][OPERATION_KEY] = operation
    return document


def _resolved(schema: object, document: Mapping[str, Any]) -> Mapping[str, Any]:
    """The object schema ``schema`` stands for: a reference to a component followed, a value that may be null read
    as the value, and a list or a map of objects read as the object; an empty schema for anything else."""
    if not isinstance(schema, Mapping):
        return {}
    reference = schema.get("$ref")
    if isinstance(reference, str) and reference.startswith(_SCHEMA_REFERENCE_PREFIX):
        return _resolved(document["components"]["schemas"].get(reference[len(_SCHEMA_REFERENCE_PREFIX) :]), document)
    alternatives = schema.get("anyOf")
    if isinstance(alternatives, list):
        objects = [resolved for resolved in (_resolved(member, document) for member in alternatives) if resolved]
        return objects[0] if objects else {}
    for container_key in ("items", "additionalProperties"):
        if isinstance(schema.get(container_key), Mapping):
            return _resolved(schema[container_key], document)
    return schema if "properties" in schema or "oneOf" in schema else {}


def _json_schema(holder: object, document: Mapping[str, Any]) -> Mapping[str, Any]:
    """The JSON schema of a request body or a response; empty when it carries none."""
    if not isinstance(holder, Mapping):
        return {}
    content = holder.get("content")
    media = content.get("application/json") if isinstance(content, Mapping) else None
    return _resolved(media.get("schema"), document) if isinstance(media, Mapping) else {}


def _answer_schemas(schema: Mapping[str, Any], document: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The object schemas an answer may be: itself, or each of a choice between several."""
    choices = schema.get("oneOf")
    if isinstance(choices, list):
        return [_resolved(choice, document) for choice in choices]
    return [schema]


def _removed_fields(
    frozen: Mapping[str, Any],
    frozen_document: Mapping[str, Any],
    current: Mapping[str, Any],
    current_document: Mapping[str, Any],
    where: str,
) -> list[str]:
    """The fields ``frozen`` answers that ``current`` no longer does, through nested objects."""
    breaks: list[str] = []
    frozen_properties = frozen.get("properties")
    current_properties = current.get("properties")
    if not isinstance(frozen_properties, Mapping):
        return breaks
    for name, frozen_property in frozen_properties.items():
        if not isinstance(current_properties, Mapping) or name not in current_properties:
            breaks.append(f"{where} no longer answers the field {name!r}")
            continue
        breaks.extend(
            _removed_fields(
                _resolved(frozen_property, frozen_document),
                frozen_document,
                _resolved(current_properties[name], current_document),
                current_document,
                f"{where}.{name}",
            )
        )
    return breaks


def description_compatibility_breaks(current: Mapping[str, Any], frozen: Mapping[str, Any]) -> list[str]:
    """What ``current`` breaks for a client written against ``frozen``: a route or a method gone or serving an
    operation of another name, a field of an answer removed, or a field of a request become required. Empty when a
    client of ``frozen`` still works."""
    breaks: list[str] = []
    for path, frozen_item in frozen.get("paths", {}).items():
        current_item = current.get("paths", {}).get(path)
        if not isinstance(current_item, Mapping):
            breaks.append(f"{path} is no longer served")
            continue
        for method, frozen_operation in frozen_item.items():
            if method not in _OPENAPI_METHODS:
                continue
            where = f"{method.upper()} {path}"
            current_operation = current_item.get(method)
            if not isinstance(current_operation, Mapping):
                breaks.append(f"{where} is no longer served")
                continue
            frozen_name = frozen_operation.get(OPERATION_KEY)
            if frozen_name is not None and current_operation.get(OPERATION_KEY) != frozen_name:
                breaks.append(f"{where} no longer serves the operation {frozen_name!r}")
            frozen_request = _json_schema(frozen_operation.get("requestBody"), frozen)
            current_request = _json_schema(current_operation.get("requestBody"), current)
            newly_required = sorted(set(current_request.get("required", ())) - set(frozen_request.get("required", ())))
            breaks.extend(f"{where} now requires the request field {name!r}" for name in newly_required)
            for status, frozen_response in frozen_operation.get("responses", {}).items():
                if status == "default":
                    continue
                current_response = current_operation.get("responses", {}).get(status)
                if current_response is None:
                    breaks.append(f"{where} no longer answers {status}")
                    continue
                frozen_answers = _answer_schemas(_json_schema(frozen_response, frozen), frozen)
                current_answers = _answer_schemas(_json_schema(current_response, current), current)
                for frozen_answer in frozen_answers:
                    # Held to the current answer that keeps the most of it: a status answered by a choice of
                    # models lists them in no promised order.
                    candidates = [
                        _removed_fields(frozen_answer, frozen, current_answer, current, f"{where} ({status})")
                        for current_answer in current_answers
                    ]
                    breaks.extend(min(candidates, key=len) if candidates else [])
    return breaks
