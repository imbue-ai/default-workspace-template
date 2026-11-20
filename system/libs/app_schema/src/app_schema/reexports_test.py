"""An app written before the SDK imports these names from ``app_schema``; each must still be the SDK's own."""

import imbue.app_sdk.api_schema
import imbue.app_sdk.app_testing
import imbue.app_sdk.errors
import imbue.app_sdk.identity
import imbue.app_sdk.request_guard
import imbue.app_sdk.routes
import pytest
import workspace_layout.identity
from flask import Flask, Response
from pydantic import BaseModel

import app_schema.api_schema
import app_schema.errors
import app_schema.request_guard
import app_schema.routes
import app_schema.testing

_REEXPORTING_MODULES = [
    (app_schema.api_schema, imbue.app_sdk.api_schema),
    (app_schema.routes, imbue.app_sdk.routes),
    (app_schema.request_guard, imbue.app_sdk.request_guard),
    (app_schema.testing, imbue.app_sdk.app_testing),
    (workspace_layout.identity, imbue.app_sdk.identity),
]


def _public_names(module: object) -> set[str]:
    return {name for name in vars(module) if not name.startswith("_")}


@pytest.mark.parametrize(("old_module", "sdk_module"), _REEXPORTING_MODULES)
def test_every_name_the_old_module_offers_is_the_sdks_own_object(
    old_module: object, sdk_module: object
) -> None:
    offered = {
        name: value
        for name, value in vars(old_module).items()
        if not name.startswith("_")
    }
    assert offered, f"{old_module} offers nothing"
    for name, value in offered.items():
        assert value is vars(sdk_module)[name], name


def test_the_old_error_base_is_the_sdks_so_an_app_catching_it_still_catches_the_helpers_errors() -> (
    None
):
    assert app_schema.errors.AppSchemaError is imbue.app_sdk.errors.AppSdkError
    assert issubclass(
        app_schema.errors.InvalidRequestBodyError, app_schema.errors.AppSchemaError
    )


class _Greeted(BaseModel):
    text: str


def test_an_app_written_against_the_old_names_still_serves_its_document_and_its_guard() -> (
    None
):
    app = Flask("greeter", static_folder=None)
    app.config["PROPAGATE_EXCEPTIONS"] = True
    app_schema.request_guard.install_request_guard(app, exempt_rules=frozenset())
    app_schema.api_schema.register_api_schema_route(
        app,
        app_schema.api_schema.DEFAULT_API_SCHEMA_PATH,
        title="Greeter",
        description="Greets.",
        route_table={
            ("GET", "/whoami"): app_schema.api_schema.RouteModels(
                response_model=_Greeted
            )
        },
    )

    @app.get("/whoami")
    def whoami() -> Response:
        """Who greets."""
        return app_schema.routes.model_response(_Greeted(text="the greeter"))

    client = app.test_client()

    assert client.get("/whoami").get_json() == {"text": "the greeter"}
    assert "/whoami" in client.get("/api/schema").get_json()["paths"]
    app_schema.testing.assert_request_guard_refuses_unknown_provenance(
        client, "GET", "/whoami"
    )
