# app_schema

An app's self-description: the OpenAPI 3.1 document for its HTTP API, built
from its Flask routes and served at the path its manifest names in
`api_schema_path` (`/api/schema` by convention). An agent reaching the app,
from inside the workspace or over the share, reads it to learn what the API
offers without reading the app's code; the visiting-agent primer links each
app's document from its apps table.

The document is only as true as the routes behind it, so the library also
owns how a route reads its JSON body and answers JSON, and checks both
against the app's route table on every request.

## The route table

Every app keeps one table, keyed by `(method, OpenAPI path)`, with an entry
for every operation:

- `RouteModels(request_model=..., response_model=..., success_status=200, alternate_responses=())`
  for a route that exchanges JSON: the pydantic model it validates its body
  with (None for a route that takes no body), the model it answers (None for
  an answer with no body, such as a 204), the status of that answer, and any
  other answer it can give under another status (a second success, or a
  failure whose answer is a model richer than `ErrorResponse`, which the
  document describes as a failure). These are published as the operation's
  request and response schemas. A view that must read its
  body itself (a parser whose refusals are its own contract, or a byte-capped
  read of the stream) adds `is_body_parsed_by_view=True`: the request model
  then documents the body's shape without parsing it, `parse_request_body`
  refuses on that route, and the assertion does not require the helper there.
- `SchemalessRoute(reason=...)` for a route that exchanges no JSON by design:
  a page, a file, a stream, a socket. The reason goes into the
  document as the operation's `x-no-schema`.

Every operation's `default` response is `ErrorResponse` (`{"detail": ...}`),
the one shape every route answers a refusal or a failure with.

## API

- `app_schema.api_schema.register_api_schema_route(target, path, title, description, route_table, version="1.0")`:
  serve the document at `path` on a `Flask` app or a `Blueprint` it registers,
  keep `route_table` on the app, and answer a body that is not its route's
  shape as a 400 `ErrorResponse`. `title` and `description` are the manifest's
  `display_name` and `description`, so the document says what the launcher
  says. The document is built from the running app's url map on each
  request: every rule but Flask's static routes (the app's and a blueprint's)
  and the schema route itself is a path, each method an operation whose
  summary is the first paragraph of the view's docstring (as one line) and
  whose description is the whole of it, each path parameter a required string
  parameter, and each operation's schemas or reason from the table.
- `app_schema.routes.parse_request_body(model)`: the current request's body as
  `model`; a body that is not a JSON object or not the shape is a 400. The
  table must name `model` as the route's request model, or the request fails
  with `RouteModelsMismatchError`.
- `app_schema.routes.model_response(model, status=200)`: `model` as the JSON
  answer; the table must name that model and status for the route (as the
  primary or an alternate), or the request fails the same way. An
  `ErrorResponse` needs no entry.
- `app_schema.routes.error_response(detail, status)`: a refusal or a failure
  as the `ErrorResponse`.
- `app_schema.api_schema.build_openapi_document(app, title, description, version, route_table, excluded_endpoints)`:
  the document itself, for a caller that wants it without the route.
- `app_schema.testing.assert_api_schema_describes_app(client, manifest_path)` (an app's
  tests depend on `app-schema[testing]` for it; the runtime needs `app-schema` alone):
  what every app's own tests assert: the route the manifest names answers a
  valid document whose title and description are the manifest's, whose paths
  are every route of the app, and whose every operation has a summary; every
  operation is in the table and the table names no route the app lacks; and
  no modelled route reads its body around `parse_request_body`.

The app also checks every answer: a modelled route that answered JSON around
`model_response`, or succeeded without reading its body through
`parse_request_body`, fails that request with `RouteModelsMismatchError`.
`merge_route_tables` joins the tables of an app's modules and refuses a route
two of them name.

Between them, a route cannot be added without saying what it does, a modelled
route cannot parse or answer something the document does not show, and a
modelled route cannot quietly read its body or answer JSON by hand; only a
schemaless route's reason rests on the author's word.

The scaffolder (`build-app`) registers the route and a starting table in every
new app and writes the two manifest keys;
`docs/system/blueprint/workspace-app-model/contracts.md` section 2 is the
manifest contract.
