"""The errors an app's route helpers raise, which live in the app SDK (``imbue.app_sdk.errors``); see
``app_schema.api_schema``."""

from imbue.app_sdk.errors import AppSdkError
from imbue.app_sdk.errors import InvalidRequestBodyError as InvalidRequestBodyError
from imbue.app_sdk.errors import RouteModelsMismatchError as RouteModelsMismatchError
from imbue.app_sdk.errors import RouteTableConflictError as RouteTableConflictError

# The base every error here extends, under the name it had in this package.
AppSchemaError = AppSdkError
