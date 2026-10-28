"""The Routines app: the pages at ``/``, ``/<id>``, ``/<id>/runs/<run id>`` and ``/<id>/runs/<run id>/log``, and
the API they read.

Services run from /home/user/workspace (the repo root). The app stores nothing: everything it shows is the
routines library's state under ``data/.state/routines``, read through the library's service, and every change
goes through that same service, which is what the ``routines`` command runs, so the app and an agent never
disagree.

- Listen address: bind ``HOST`` and ``PORT``, which default to the loopback address and this app's assigned
  port but honor ``ROUTINES_APP_HOST`` and ``ROUTINES_APP_PORT``, so a preview can boot on a spare port
  beside the live one. ``ROUTINES_APP_PREVIEW`` makes that boot read-only: a preview may not start runs or
  rewrite definitions, since those reach the live workspace, and answers every change with 403 as the shell's
  preview does.

This is a synchronous Flask app served by the threaded Werkzeug server. The app owns its own browser origin
(the forwarder routes ``http://routines.<workspace-host>/`` straight to this port), so root-absolute URLs
work unmodified.
"""

import os
import re
from pathlib import Path
from typing import Final

from flask import Flask
from flask import Response
from flask import send_file
from flask import send_from_directory
from flask.typing import ResponseReturnValue
from loguru import logger
from pydantic import ValidationError
from werkzeug.serving import run_simple

from app_manifest.content_security_policy import attach_document_policy
from app_manifest.primitives import AppName
from app_manifest.registry import read_origin_label
from app_schema.api_schema import DEFAULT_API_SCHEMA_PATH
from app_schema.api_schema import RouteModels
from app_schema.api_schema import RouteTable
from app_schema.api_schema import SchemalessRoute
from app_schema.api_schema import register_api_schema_route
from app_schema.routes import error_response
from app_schema.routes import model_response
from app_schema.routes import parse_request_body
from imbue.imbue_common.ids import InvalidRandomIdError
from imbue.imbue_common.logging import setup_logging
from routines.data_types import RoutineDetail
from routines.data_types import RoutineListing
from routines.data_types import RoutineRow
from routines.data_types import RunDetail
from routines.data_types import RunStartReceipt
from routines.data_types import ScheduleChange
from routines.definitions import describe_validation_error
from routines.errors import DefinitionCommitError
from routines.errors import DefinitionValidationError
from routines.errors import InvalidRoutineIdError
from routines.errors import RoutineNotFoundError
from routines.errors import RunInFlightError
from routines.errors import RunNotFoundError
from routines.errors import StateRemovalError
from routines.fix_brief import build_fix_brief
from routines.fix_brief import fix_chat_name
from routines.paths import RoutinesPaths
from routines.paths import paths_from_environment
from routines.primitives import RoutineId
from routines.primitives import RunId
from routines.registry import CHAT_APP_NAME
from routines.service import RoutinesService
from routines.service import build_service
from routines_app.data_types import EnabledRequest
from routines_app.data_types import FixChatResponse
from routines_app.data_types import HealthResponse
from routines_app.documents import inject_chat_label_meta_tag
from routines_app.errors import FixChatError
from routines_app.fix_chat import FixChatStarterInterface
from routines_app.fix_chat import SubprocessFixChatStarter

HOST: Final[str] = os.environ.get("ROUTINES_APP_HOST", "127.0.0.1")
PORT: Final[int] = int(os.environ.get("ROUTINES_APP_PORT", "8050"))
IS_PREVIEW: Final[bool] = os.environ.get("ROUTINES_APP_PREVIEW", "") not in ("", "0", "false")

APP_DISPLAY_NAME: Final[str] = "Routines"
APP_DESCRIPTION: Final[str] = (
    "Everything that runs on a schedule: each routine's health, next run and history, with run-now, on/off, "
    "reschedule and fix controls."
)
STATIC_DIR: Final[Path] = Path(__file__).parent / "static"
# A bundle file Vite named from its content, which may be cached for good; the rest is checked on every load.
_HASHED_ASSET: Final[re.Pattern[str]] = re.compile(r"-[A-Za-z0-9_-]{8}\.(js|css)$")
_A_YEAR_IN_SECONDS: Final[int] = 365 * 24 * 60 * 60
PAGE_DOCUMENT_FILENAME: Final[str] = "index.html"
HEALTH_PATH: Final[str] = "/api/health"

HTTP_BAD_REQUEST: Final[int] = 400
HTTP_NOT_FOUND: Final[int] = 404
HTTP_CONFLICT: Final[int] = 409
HTTP_FORBIDDEN: Final[int] = 403
HTTP_BAD_GATEWAY: Final[int] = 502
HTTP_NO_CONTENT: Final[int] = 204
HTTP_INTERNAL_SERVER_ERROR: Final[int] = 500

PREVIEW_REFUSAL: Final[str] = "This is a preview of the Routines app; changes are made from the live one."

_NOT_BUILT_PAGE: Final[str] = (
    '<!doctype html><html><head><meta charset="utf-8"><title>Routines</title></head>'
    "<body><p>The Routines app has not been built yet (run <code>npm run build</code> in <code>system/</code>).</p>"
    "</body></html>"
)

ROUTE_TABLE: RouteTable = {
    ("GET", "/"): SchemalessRoute(reason="the Routines list page"),
    ("GET", "/{routine_id}"): SchemalessRoute(reason="a routine's page"),
    ("GET", "/{routine_id}/runs/{run_id}"): SchemalessRoute(reason="a run's page"),
    ("GET", "/{routine_id}/runs/{run_id}/log"): SchemalessRoute(reason="a run's whole-log page"),
    ("GET", "/assets/{filename}"): SchemalessRoute(reason="one file of the pages' built bundle"),
    ("GET", HEALTH_PATH): RouteModels(response_model=HealthResponse),
    ("GET", "/api/routines"): RouteModels(response_model=RoutineListing),
    ("GET", "/api/routines/{routine_id}"): RouteModels(response_model=RoutineDetail),
    ("GET", "/api/routines/{routine_id}/runs/{run_id}"): RouteModels(response_model=RunDetail),
    ("GET", "/api/routines/{routine_id}/runs/{run_id}/log"): SchemalessRoute(reason="the run's log, as text"),
    ("POST", "/api/routines/{routine_id}/run"): RouteModels(response_model=RunStartReceipt),
    ("PUT", "/api/routines/{routine_id}/enabled"): RouteModels(
        request_model=EnabledRequest, response_model=RoutineRow
    ),
    ("PUT", "/api/routines/{routine_id}/schedule"): RouteModels(
        request_model=ScheduleChange, response_model=RoutineRow
    ),
    ("POST", "/api/routines/{routine_id}/fix"): RouteModels(response_model=FixChatResponse),
    ("DELETE", "/api/routines/{routine_id}"): RouteModels(response_model=None, success_status=HTTP_NO_CONTENT),
}


def _routine_id(raw: str) -> RoutineId | None:
    try:
        return RoutineId(raw)
    except InvalidRoutineIdError:
        return None


def _run_id(raw: str) -> RunId | None:
    try:
        return RunId(raw)
    except InvalidRandomIdError:
        return None


def create_app(
    service: RoutinesService, static_directory: Path, is_preview: bool, fix_chat_starter: FixChatStarterInterface
) -> Flask:
    app = Flask("routines_app", static_folder=None)
    register_api_schema_route(
        app, DEFAULT_API_SCHEMA_PATH, title=APP_DISPLAY_NAME, description=APP_DESCRIPTION, route_table=ROUTE_TABLE
    )
    attach_document_policy(app, None)
    static_directory = static_directory.resolve()

    def page() -> ResponseReturnValue:
        """The one document, carrying the chat app's origin label (read per load: the registry is one small file)."""
        document = static_directory / PAGE_DOCUMENT_FILENAME
        if not document.is_file():
            return Response(_NOT_BUILT_PAGE, mimetype="text/html", headers={"Cache-Control": "no-store"})
        chat_label = read_origin_label(service.registry_path, AppName(CHAT_APP_NAME))
        html_content = inject_chat_label_meta_tag(document.read_text(encoding="utf-8"), chat_label)
        return Response(html_content, mimetype="text/html", headers={"Cache-Control": "no-store"})

    def not_found(routine_id: str) -> ResponseReturnValue:
        return error_response(f"There is no routine called {routine_id!r}.", HTTP_NOT_FOUND)

    @app.get("/")
    def list_page() -> ResponseReturnValue:
        """The Routines list page."""
        return page()

    @app.get("/<routine_id>")
    def routine_page(routine_id: str) -> ResponseReturnValue:
        """A routine's page: the same document, which reads its path."""
        return page()

    @app.get("/<routine_id>/runs/<run_id>")
    def run_page(routine_id: str, run_id: str) -> ResponseReturnValue:
        """A run's page: the same document, which reads its path."""
        return page()

    @app.get("/<routine_id>/runs/<run_id>/log")
    def log_page(routine_id: str, run_id: str) -> ResponseReturnValue:
        """A run's whole-log page: the same document, which reads its path."""
        return page()

    @app.get("/assets/<path:filename>")
    def asset(filename: str) -> ResponseReturnValue:
        """One file of the pages' built bundle, by name; one named from its content is cached for good."""
        is_hashed = _HASHED_ASSET.search(filename) is not None
        response = send_from_directory(
            (static_directory / "assets").absolute(), filename, max_age=_A_YEAR_IN_SECONDS if is_hashed else None
        )
        if not is_hashed:
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get(HEALTH_PATH)
    def health() -> ResponseReturnValue:
        """Whether the app is up, and whether its pages have been built."""
        return model_response(
            HealthResponse(status="ok", is_frontend_built=(static_directory / PAGE_DOCUMENT_FILENAME).is_file())
        )

    @app.get("/api/routines")
    def listing() -> ResponseReturnValue:
        """Every routine with its health, schedule, next run and last run, problems first."""
        response = model_response(service.listing())
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/routines/<routine_id>")
    def detail(routine_id: str) -> ResponseReturnValue:
        """One routine with its definition and recent runs."""
        wanted = _routine_id(routine_id)
        if wanted is None:
            return not_found(routine_id)
        try:
            response = model_response(service.detail(wanted))
        except RoutineNotFoundError:
            return not_found(routine_id)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/routines/<routine_id>/runs/<run_id>")
    def run_detail(routine_id: str, run_id: str) -> ResponseReturnValue:
        """One run with the tail of its log and the links it left."""
        wanted = _routine_id(routine_id)
        wanted_run = _run_id(run_id)
        if wanted is None:
            return not_found(routine_id)
        if wanted_run is None:
            return error_response(f"There is no run called {run_id!r}.", HTTP_NOT_FOUND)
        try:
            return model_response(service.run_detail(wanted, wanted_run))
        except RoutineNotFoundError:
            return not_found(routine_id)
        except RunNotFoundError as e:
            return error_response(str(e), HTTP_NOT_FOUND)

    @app.get("/api/routines/<routine_id>/runs/<run_id>/log")
    def run_log(routine_id: str, run_id: str) -> ResponseReturnValue:
        """The run's whole log, unprocessed, as text; 404 for a run the ledger does not know."""
        wanted = _routine_id(routine_id)
        wanted_run = _run_id(run_id)
        if wanted is None or wanted_run is None:
            return Response("No such run.\n", status=HTTP_NOT_FOUND, mimetype="text/plain")
        try:
            is_known = service.run_exists(wanted, wanted_run)
        except RoutineNotFoundError:
            is_known = False
        if not is_known:
            return Response("No such run.\n", status=HTTP_NOT_FOUND, mimetype="text/plain")
        path = service.paths.run_log_path(wanted, wanted_run)
        if not path.is_file():
            return Response("No log yet.\n", mimetype="text/plain")
        return send_file(path.absolute(), mimetype="text/plain")

    @app.post("/api/routines/<routine_id>/run")
    def run_now(routine_id: str) -> ResponseReturnValue:
        """Start a manual run in a process of its own; 409 while one is already going, 400 for a broken definition."""
        wanted = _routine_id(routine_id)
        if wanted is None:
            return not_found(routine_id)
        if is_preview:
            return error_response(PREVIEW_REFUSAL, HTTP_FORBIDDEN)
        try:
            return model_response(service.start_run_detached(wanted))
        except RoutineNotFoundError:
            return not_found(routine_id)
        except RunInFlightError as e:
            return error_response(str(e), HTTP_CONFLICT)
        except DefinitionValidationError as e:
            return error_response(str(e), HTTP_BAD_REQUEST)

    @app.put("/api/routines/<routine_id>/enabled")
    def set_enabled(routine_id: str) -> ResponseReturnValue:
        """Turn the routine on or off; answers the routine as the list shows it."""
        wanted = _routine_id(routine_id)
        if wanted is None:
            return not_found(routine_id)
        if is_preview:
            return error_response(PREVIEW_REFUSAL, HTTP_FORBIDDEN)
        body = parse_request_body(EnabledRequest)
        try:
            service.set_enabled(wanted, body.is_enabled)
            return model_response(service.row(wanted))
        except RoutineNotFoundError:
            return not_found(routine_id)

    @app.put("/api/routines/<routine_id>/schedule")
    def set_schedule(routine_id: str) -> ResponseReturnValue:
        """Change when the routine runs; 400 with the reason when the schedule does not validate."""
        wanted = _routine_id(routine_id)
        if wanted is None:
            return not_found(routine_id)
        if is_preview:
            return error_response(PREVIEW_REFUSAL, HTTP_FORBIDDEN)
        change = parse_request_body(ScheduleChange)
        try:
            service.change_schedule(wanted, change)
            return model_response(service.row(wanted))
        except RoutineNotFoundError:
            return not_found(routine_id)
        except ValidationError as e:
            return error_response(describe_validation_error(e), HTTP_BAD_REQUEST)
        except DefinitionValidationError as e:
            return error_response(str(e), HTTP_BAD_REQUEST)
        except DefinitionCommitError as e:
            return error_response(str(e), HTTP_CONFLICT)

    @app.post("/api/routines/<routine_id>/fix")
    def fix(routine_id: str) -> ResponseReturnValue:
        """Open a chat briefed on the routine's failure; answers the chat's id."""
        wanted = _routine_id(routine_id)
        if wanted is None:
            return not_found(routine_id)
        if is_preview:
            return error_response(PREVIEW_REFUSAL, HTTP_FORBIDDEN)
        try:
            routine_detail = service.detail(wanted)
        except RoutineNotFoundError:
            return not_found(routine_id)
        last_finished = next((run for run in routine_detail.runs if run.outcome is not None), None)
        last_run_log = service.log_tail(wanted, last_finished.run_id)[0] if last_finished is not None else ""
        brief = build_fix_brief(routine_detail, f"/{wanted}", last_run_log)
        try:
            chat_id = fix_chat_starter.start(brief, fix_chat_name(routine_detail.row.title))
        except FixChatError as e:
            return error_response(str(e), HTTP_BAD_GATEWAY)
        return model_response(FixChatResponse(chat_id=chat_id))

    @app.delete("/api/routines/<routine_id>")
    def remove(routine_id: str) -> ResponseReturnValue:
        """Remove the routine: its definition and state go, its runs stay in the ledger; 409 while one is going."""
        wanted = _routine_id(routine_id)
        if wanted is None:
            return not_found(routine_id)
        if is_preview:
            return error_response(PREVIEW_REFUSAL, HTTP_FORBIDDEN)
        try:
            service.remove(wanted)
        except RoutineNotFoundError:
            return not_found(routine_id)
        except RunInFlightError as e:
            return error_response(str(e), HTTP_CONFLICT)
        except StateRemovalError as e:
            return error_response(str(e), HTTP_INTERNAL_SERVER_ERROR)
        except DefinitionCommitError as e:
            # The routine is gone by the time a commit is refused; the next definition write commits the deletion.
            logger.warning("Removed {} but could not commit the removal: {}", wanted, e)
        return Response(status=HTTP_NO_CONTENT)

    return app


def main() -> None:
    setup_logging(level=os.environ.get("ROUTINES_APP_LOG_LEVEL", "INFO"))
    paths: RoutinesPaths = paths_from_environment()
    service = build_service(paths, dict(os.environ))
    app = create_app(service, STATIC_DIR, IS_PREVIEW, SubprocessFixChatStarter(repo_root=paths.repo_root))
    logger.info("Serving the Routines app at http://{}:{}/", HOST, PORT)
    run_simple(HOST, PORT, app, threaded=True, use_reloader=False, use_debugger=False)


if __name__ == "__main__":
    main()
