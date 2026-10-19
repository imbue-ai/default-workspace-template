from pathlib import Path

from flask import Blueprint, Flask, Response
from openapi_spec_validator import validate
from pydantic import BaseModel

from app_schema.api_schema import (
    OTHER_METHODS_KEY,
    RouteModels,
    build_openapi_document,
    flask_rule_to_openapi_path,
    register_api_schema_route,
)
from app_schema.testing import assert_api_schema_describes_app


class NoteBody(BaseModel):
    title: str
    text: str


class NoteRecord(BaseModel):
    id: str
    body: NoteBody


def _notes_app() -> Flask:
    app = Flask("notes", static_folder=None)

    @app.get("/")
    def index() -> Response:
        """The notes page."""
        return Response("notes", mimetype="text/html")

    @app.post("/api/notes")
    def create_note() -> Response:
        """Create a note.

        The body carries a title and the text; the answer is the stored record.
        """
        return Response("{}", mimetype="application/json")

    @app.route("/api/notes/<note_id>", methods=["GET", "DELETE"])
    def one_note(note_id: str) -> Response:
        """One note by id."""
        return Response("{}", mimetype="application/json")

    @app.route("/dav/<path:name>", methods=["PROPFIND", "MKCOL", "GET"])
    def dav(name: str) -> Response:
        """The notes over WebDAV."""
        return Response("")

    @app.route("/tree/", defaults={"subpath": ""})
    @app.route("/tree/<path:subpath>")
    def tree(subpath: str) -> Response:
        """The folder tree, from the root or a subpath."""
        return Response(subpath)

    return app


def test_flask_rules_become_openapi_paths() -> None:
    assert flask_rule_to_openapi_path("/api/notes/<note_id>") == "/api/notes/{note_id}"
    assert (
        flask_rule_to_openapi_path("/files/<path:name>/<int:n>") == "/files/{name}/{n}"
    )


def test_the_document_covers_every_route_with_docstrings_parameters_and_models() -> (
    None
):
    app = _notes_app()
    document = build_openapi_document(
        app,
        title="Notes",
        description="Keeps notes.",
        version="1.0",
        route_models={
            ("POST", "/api/notes"): RouteModels(
                request_model=NoteBody, response_model=NoteRecord
            )
        },
        excluded_endpoints=frozenset(),
    )
    validate(document)
    assert document["info"] == {
        "title": "Notes",
        "version": "1.0",
        "description": "Keeps notes.",
    }
    assert set(document["paths"]) == {
        "/",
        "/api/notes",
        "/api/notes/{note_id}",
        "/dav/{name}",
        "/tree/",
        "/tree/{subpath}",
    }
    # A rule that fills an argument from ``defaults`` has no such parameter in its path.
    assert "parameters" not in document["paths"]["/tree/"]["get"]
    assert [
        parameter["name"]
        for parameter in document["paths"]["/tree/{subpath}"]["get"]["parameters"]
    ] == ["subpath"]
    # Methods OpenAPI has no operation for are listed, not dropped and not made into invalid operations.
    assert document["paths"]["/dav/{name}"][OTHER_METHODS_KEY] == ["MKCOL", "PROPFIND"]
    assert set(document["paths"]["/dav/{name}"]) == {"get", OTHER_METHODS_KEY}
    create = document["paths"]["/api/notes"]["post"]
    assert create["summary"] == "Create a note."
    assert create["description"].endswith("the answer is the stored record.")
    assert create["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/NoteBody"
    }
    assert create["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/NoteRecord"
    }
    assert set(document["components"]["schemas"]) == {"NoteBody", "NoteRecord"}
    one = document["paths"]["/api/notes/{note_id}"]
    assert set(one) == {"get", "delete"}
    assert one["get"]["parameters"] == [
        {
            "name": "note_id",
            "in": "path",
            "required": True,
            "schema": {"type": "string"},
        }
    ]
    assert one["get"]["operationId"] == "get_api_notes_note_id"
    assert "components" not in build_openapi_document(
        app,
        title="Notes",
        description="x",
        version="1.0",
        route_models={},
        excluded_endpoints=frozenset(),
    )


def test_a_blank_docstring_describes_nothing() -> None:
    app = Flask("blank", static_folder=None)

    @app.get("/")
    def index() -> Response:
        """ """
        return Response("")

    document = build_openapi_document(
        app,
        title="Blank",
        description="x",
        version="1.0",
        route_models={},
        excluded_endpoints=frozenset(),
    )
    operation = document["paths"]["/"]["get"]
    assert "summary" not in operation
    assert "description" not in operation


def test_the_summary_is_the_first_paragraph_as_one_line() -> None:
    app = Flask("wrapped", static_folder=None)

    @app.get("/fleet")
    def fleet() -> Response:
        """Register a new browser and answer its name at once (the launch
        runs in the background).

        The fleet starts empty.
        """
        return Response("")

    @app.get("/health")
    def health() -> Response:
        """Whether the app is up.

        Always ok.
        """
        return Response("")

    document = build_openapi_document(
        app,
        title="Wrapped",
        description="x",
        version="1.0",
        route_models={},
        excluded_endpoints=frozenset(),
    )
    fleet_operation = document["paths"]["/fleet"]["get"]
    assert fleet_operation["summary"] == (
        "Register a new browser and answer its name at once (the launch runs in the background)."
    )
    assert fleet_operation["description"].endswith("The fleet starts empty.")
    assert document["paths"]["/health"]["get"]["summary"] == "Whether the app is up."


def test_the_route_serves_the_document_without_describing_itself(
    notes_manifest_path: Path,
) -> None:
    app = _notes_app()
    register_api_schema_route(
        app,
        "/api/schema",
        title="Notes",
        description="Keeps the person's notes and finds them again.",
    )
    client = app.test_client()

    assert_api_schema_describes_app(client, notes_manifest_path)
    assert "/api/schema" not in client.get("/api/schema").get_json()["paths"]


def test_the_route_works_on_a_blueprint_and_sees_routes_registered_later(
    notes_manifest_path: Path,
) -> None:
    """A blueprint's own static route (``pages.static``) is Flask's, not the app's, so it is left out too."""
    blueprint = Blueprint("pages", __name__, static_folder="static")

    @blueprint.get("/api/health")
    def health() -> Response:
        """Whether the app is up."""
        return Response("{}", mimetype="application/json")

    register_api_schema_route(
        blueprint,
        "/api/schema",
        title="Notes",
        description="Keeps the person's notes and finds them again.",
    )
    app = Flask("notes", static_folder=None)
    app.register_blueprint(blueprint)

    @app.get("/late")
    def late() -> Response:
        """A route registered after the document's."""
        return Response("late")

    assert_api_schema_describes_app(app.test_client(), notes_manifest_path)
    assert set(app.test_client().get("/api/schema").get_json()["paths"]) == {
        "/api/health",
        "/late",
    }
