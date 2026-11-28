"""An app's OpenAPI document, which lives in the app SDK (``imbue.app_sdk.api_schema``).

# CLEANUP: delete this package's modules once no supported workspace holds an app written before the SDK that has
# not been published since: nothing built in imports them, a scaffolded app imports the SDK alone, and publishing
# such an app is what moves it to the SDK's names. The package itself stays, as what brings the workspace's SDK to
# an app built in the workspace.
"""

from imbue.app_sdk.api_schema import ANSWERED_MODEL_FLAG as ANSWERED_MODEL_FLAG
from imbue.app_sdk.api_schema import API_SCHEMA_ENDPOINT as API_SCHEMA_ENDPOINT
from imbue.app_sdk.api_schema import DEFAULT_API_SCHEMA_PATH as DEFAULT_API_SCHEMA_PATH
from imbue.app_sdk.api_schema import EXTENSION_KEY as EXTENSION_KEY
from imbue.app_sdk.api_schema import HTTP_BAD_REQUEST as HTTP_BAD_REQUEST
from imbue.app_sdk.api_schema import NO_SCHEMA_KEY as NO_SCHEMA_KEY
from imbue.app_sdk.api_schema import OPENAPI_VERSION as OPENAPI_VERSION
from imbue.app_sdk.api_schema import OTHER_METHODS_KEY as OTHER_METHODS_KEY
from imbue.app_sdk.api_schema import PARSED_BODY_FLAG as PARSED_BODY_FLAG
from imbue.app_sdk.api_schema import AlternateResponse as AlternateResponse
from imbue.app_sdk.api_schema import ErrorResponse as ErrorResponse
from imbue.app_sdk.api_schema import RouteModels as RouteModels
from imbue.app_sdk.api_schema import RouteTable as RouteTable
from imbue.app_sdk.api_schema import SchemalessRoute as SchemalessRoute
from imbue.app_sdk.api_schema import build_openapi_document as build_openapi_document
from imbue.app_sdk.api_schema import current_route_key as current_route_key
from imbue.app_sdk.api_schema import (
    flask_rule_to_openapi_path as flask_rule_to_openapi_path,
)
from imbue.app_sdk.api_schema import is_static_endpoint as is_static_endpoint
from imbue.app_sdk.api_schema import merge_route_tables as merge_route_tables
from imbue.app_sdk.api_schema import (
    register_api_schema_route as register_api_schema_route,
)
