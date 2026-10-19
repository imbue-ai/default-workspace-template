# app_schema

An app's self-description: the OpenAPI 3.1 document for its HTTP API, built
from its Flask routes and served at the path its manifest names in
`api_schema_path` (`/api/schema` by convention). An agent reaching the app,
from inside the workspace or over the share, reads it to learn what the API
offers without reading the app's code; the visiting-agent primer links each
app's document from its apps table.

## API

- `app_schema.api_schema.register_api_schema_route(target, path, title, description, version="1.0", route_models=None)`:
  serve the document at `path` on a `Flask` app or a `Blueprint` it registers.
  `title` and `description` are the manifest's `display_name` and
  `description`, so the document says what the launcher says. The document
  is built from the running app's url map on each request: every rule but
  Flask's static routes (the app's and a blueprint's) and the schema route
  itself is a path, each method an operation whose summary is the first
  paragraph of the view's docstring (as one line) and whose description is the
  whole of it, each path parameter a required string parameter.
  `route_models` maps
  `(method, path)` to `RouteModels(request_model=..., response_model=..., success_status=...)`
  for routes that validate with pydantic; those models are published as
  request and response schemas.
- `app_schema.api_schema.build_openapi_document(app, title, description, version, route_models, excluded_endpoints)`:
  the document itself, for a caller that wants it without the route.
- `app_schema.testing.assert_api_schema_describes_app(client, manifest_path)` (an app's
  tests depend on `app-schema[testing]` for it; the runtime needs `app-schema` alone):
  what every app's own tests assert: the route the manifest names answers a
  valid document whose title and description are the manifest's, whose
  paths are every route of the app, and whose every operation has a summary
  (the first paragraph of the view's docstring, as one line), so a route cannot be added without
  saying what it does.

The scaffolder (`build-app`) registers the route in every new app and writes
the two manifest keys; `docs/system/blueprint/workspace-app-model/contracts.md`
section 2 is the manifest contract.
