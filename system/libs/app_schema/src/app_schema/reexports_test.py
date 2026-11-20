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

# Each old module, the SDK module it re-exports, and the public names the old module defined before the SDK
# existed: what an app written against it may import.
_REEXPORTING_MODULES = [
    (
        app_schema.api_schema,
        imbue.app_sdk.api_schema,
        frozenset(
            {
                "ANSWERED_MODEL_FLAG",
                "API_SCHEMA_ENDPOINT",
                "AlternateResponse",
                "DEFAULT_API_SCHEMA_PATH",
                "EXTENSION_KEY",
                "ErrorResponse",
                "HTTP_BAD_REQUEST",
                "NO_SCHEMA_KEY",
                "OPENAPI_VERSION",
                "OTHER_METHODS_KEY",
                "PARSED_BODY_FLAG",
                "RouteModels",
                "RouteTable",
                "SchemalessRoute",
                "build_openapi_document",
                "current_route_key",
                "flask_rule_to_openapi_path",
                "is_static_endpoint",
                "merge_route_tables",
                "register_api_schema_route",
            }
        ),
    ),
    (
        app_schema.routes,
        imbue.app_sdk.routes,
        frozenset(
            {
                "error_response",
                "model_response",
                "parse_request_body",
                "registered_route_table",
            }
        ),
    ),
    (
        app_schema.request_guard,
        imbue.app_sdk.request_guard,
        frozenset({"HTTP_FORBIDDEN", "install_request_guard", "request_provenance"}),
    ),
    (
        app_schema.testing,
        imbue.app_sdk.app_testing,
        frozenset(
            {
                "CROSS_SITE_FETCH_HEADERS",
                "FORWARDED_BASE_URL",
                "OWNER_IDENTITY_HEADERS",
                "REBOUND_BASE_URL",
                "assert_api_schema_describes_app",
                "assert_request_guard_refuses_unknown_provenance",
            }
        ),
    ),
    (
        workspace_layout.identity,
        imbue.app_sdk.identity,
        frozenset(
            {
                "ANONYMOUS_OWNER",
                "FETCH_MODE_HEADER",
                "FETCH_SITE_HEADER",
                "IDENTITY_HEADER",
                "LOOPBACK_ADDRESSES",
                "LOOPBACK_HOSTNAMES",
                "OTHER_ORIGIN_REFUSAL",
                "RequestIdentity",
                "RequestProvenance",
                "UNTRUSTED_LOOPBACK_REFUSAL",
                "describe_provenance_refusal",
                "is_from_another_origin",
                "is_loopback_address",
                "is_loopback_host",
                "is_owner_participant",
                "parse_identity_header",
                "participant_key_of",
                "visiting_user_id",
            }
        ),
    ),
]


def _public_names(module: object) -> set[str]:
    return {name for name in vars(module) if not name.startswith("_")}


@pytest.mark.parametrize(
    ("old_module", "sdk_module", "names_before_the_sdk"), _REEXPORTING_MODULES
)
def test_the_old_module_still_offers_every_name_it_had_and_each_is_the_sdks_own_object(
    old_module: object, sdk_module: object, names_before_the_sdk: frozenset[str]
) -> None:
    offered = _public_names(old_module)
    assert not names_before_the_sdk - offered, sorted(names_before_the_sdk - offered)
    for name in offered:
        assert vars(old_module)[name] is vars(sdk_module)[name], name


def test_the_old_error_base_is_the_sdks_so_an_app_catching_it_still_catches_the_helpers_errors() -> (
    None
):
    assert app_schema.errors.AppSchemaError is imbue.app_sdk.errors.AppSdkError
    assert issubclass(
        app_schema.errors.InvalidRequestBodyError, app_schema.errors.AppSchemaError
    )
    assert (
        app_schema.errors.RouteModelsMismatchError
        is imbue.app_sdk.errors.RouteModelsMismatchError
    )
    assert (
        app_schema.errors.RouteTableConflictError
        is imbue.app_sdk.errors.RouteTableConflictError
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
