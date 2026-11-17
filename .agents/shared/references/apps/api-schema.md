# The app describes its own API

Read this before adding or changing a route of an app whose manifest has an
`api_schema_path`.

Every app with an HTTP API serves an OpenAPI document of its routes at
`/api/schema` (the manifest's `api_schema_path`). The document is how a
visiting agent, and any other agent, learns what the app can do without reading
its code. It is built by
`app_schema.api_schema.register_api_schema_route(app, DEFAULT_API_SCHEMA_PATH, title=..., description=..., route_table=ROUTE_TABLE)`,
which the scaffold already calls in `runner.py` with the manifest's
`display_name` and `description` and a starting table. It is built from the
live url map on each request, so every route you add is listed automatically.

## What each route owes the document

- **A docstring on the view.** Its first paragraph, as one line, is the
  operation's summary; the whole of it is the operation's description. Write
  both in the user's terms.
- **An entry in `ROUTE_TABLE`**, keyed by `(method, path)` with path parameters
  in `{name}` form:
  - A route that reads or answers JSON gets
    `RouteModels(request_model=..., response_model=..., success_status=...)`,
    plus `alternate_responses` for a second success under another status, or a
    failure whose answer is a model richer than the error shape. The view reads
    its body only through `app_schema.routes.parse_request_body(Model)` and
    answers only through `app_schema.routes.model_response(model, status)`. A
    refusal or a failure is `app_schema.routes.error_response(detail, status)`.
  - A page, a file, a stream, a socket or a redirect gets
    `SchemalessRoute(reason=...)`.
  - Define the models as `FrozenModel`s with a `Field(description=...)` per
    attribute. The descriptions are what the document shows.
- **A current table and description.** Update the table when a route's models
  or status change, and the `description` in `app.toml` when a change moves
  what the app is for.

## What enforces it

Never read the body with `request.get_json` or answer JSON with `jsonify` of a
dict. The helpers check every parse and answer against the table at request
time, and the app checks every answer. So a route that disagrees with the
document, answers JSON by hand, or succeeds without reading its body through
the helper fails its first request instead of shipping a document that lies. A
failure richer than `{"detail"}` (a model extending `ErrorResponse`) is listed
among the route's answers like any other.

The scaffolded test (`assert_api_schema_describes_app`, in `runner_test.py`)
keeps the rest honest, so run it with every change to a route. It fails when:

- the manifest's `description` and the document disagree,
- a route is missing from the document,
- a view has no docstring,
- a route is not in the table, or the table names a route the app lacks,
- a modelled view reads its body around the helper.
