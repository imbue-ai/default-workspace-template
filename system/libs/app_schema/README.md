# app_schema

The names of the app SDK ([`imbue-app-sdk`](https://github.com/imbue-ai/mngr/tree/main/libs/app_sdk), imported as `imbue.app_sdk`) under the package they had before the SDK existed.

An app's request guard, its route helpers, the OpenAPI document built from its routes, and the assertions its tests make about them now live in the SDK, so that an app can lock and run outside a workspace.
Every built-in app imports the SDK directly.
This package stays for apps a workspace's agent built before the SDK: each module here re-exports the SDK's own objects, so such an app keeps working unchanged until it is published, which is when its imports move to the SDK's names.
A newly scaffolded app depends on it as well, though it imports the SDK alone: this package is how the SDK reaches the environment of an app built in the workspace, at the commit the workspace pins, so such an app moves to a new SDK with the workspace and needs no release of the SDK to do it.
Publishing an app replaces the dependency with one on the released `imbue-app-sdk`, because a path into a workspace resolves nowhere else.

| Old import | Now |
| --- | --- |
| `app_schema.api_schema` | `imbue.app_sdk.api_schema` |
| `app_schema.routes` | `imbue.app_sdk.routes` |
| `app_schema.request_guard` | `imbue.app_sdk.request_guard` |
| `app_schema.errors` | `imbue.app_sdk.errors` (`AppSchemaError` is `AppSdkError`) |
| `app_schema.testing` | `imbue.app_sdk.app_testing` |

Write new code against `imbue.app_sdk`.
The SDK's README describes the route table, the helpers, and the guard.
