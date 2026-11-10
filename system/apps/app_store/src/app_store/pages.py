"""The app's routes on its own origin: the page, the store's state, install, uninstall and update, each app's icon and
raw install log, adding apps from PyPI or npm (checked, added, removed, and the job that works out a start command),
apps from GitHub waiting for confirmation, the template catalog and a template's size, the health probe, and the
contract module.

The page is the frontend's built document (``static/index.html``); its bundle is served under ``/assets``. Opening
an installed app and adopting a template both happen in the page, through the shell connection.
"""

from pathlib import Path
from typing import Final
from typing import assert_never

from flask import Blueprint
from flask import Response
from flask import send_file
from flask import send_from_directory
from flask.typing import ResponseReturnValue
from pydantic import Field
from werkzeug.exceptions import NotFound
from workspace_themes.flask_routes import THEME_STATIC_ROUTE_TABLE
from workspace_themes.flask_routes import register_workspace_theme_route

from app_manifest.content_security_policy import attach_document_policy
from app_manifest.content_security_policy import url_origin
from app_manifest.registry import APP_CONTRACT_ROUTE
from app_schema.api_schema import DEFAULT_API_SCHEMA_PATH
from app_schema.api_schema import RouteModels
from app_schema.api_schema import RouteTable
from app_schema.api_schema import SchemalessRoute
from app_schema.api_schema import register_api_schema_route
from app_schema.routes import error_response
from app_schema.routes import model_response
from app_schema.routes import parse_request_body
from app_store.added_apps import AddAppRequest
from app_store.added_apps import AddedApp
from app_store.adding import AppAdder
from app_store.adding import CheckResult
from app_store.command_finder import CommandFinder
from app_store.command_finder import FinderJob
from app_store.engine import InstallEngine
from app_store.errors import AddAppRefusedError
from app_store.errors import InstallRefusedError
from app_store.records import InstallRecord
from app_store.records import InstallStatus
from app_store.repo_adds import PendingRepoAddStore
from app_store.repo_adds import PendingRepoAddView
from app_store.repo_adds import RepoAddSubmission
from app_store.repo_adds import RepoAdds
from app_store.repo_install_file import RepoInstallFile
from app_store.template_sizes import TemplateSizeStore
from app_store.views import StoreSnapshot
from app_store.views import busy_slug
from app_store.views import store_app_entry
from app_store.views import workspace_app_entries
from getting_started.pages import TemplatesCatalogResponse
from getting_started.template_catalog import TemplateCatalogAvailability
from getting_started.template_catalog import TemplateCatalogStore
from getting_started.template_catalog import catalog_wire
from imbue.imbue_common.frozen_model import FrozenModel

BLUEPRINT_NAME: Final[str] = "app_store_pages"
HEALTH_PATH: Final[str] = "/api/health"
APP_DISPLAY_NAME: Final[str] = "App Store"
APP_DESCRIPTION: Final[str] = "Install open-source apps in one click, and browse published templates to adopt."
STORE_PATH: Final[str] = "/api/store"
ADDED_APPS_PATH: Final[str] = "/api/added-apps"
ADDED_APPS_CHECK_PATH: Final[str] = "/api/added-apps/check"
FIND_COMMAND_PATH: Final[str] = "/api/added-apps/find-command"
TEMPLATES_CATALOG_PATH: Final[str] = "/api/templates-catalog"
REPO_ADDS_PATH: Final[str] = "/api/repo-adds"
PAGE_DOCUMENT_FILENAME: Final[str] = "index.html"

HTTP_CREATED: Final[int] = 201
HTTP_ACCEPTED: Final[int] = 202
HTTP_NOT_FOUND: Final[int] = 404
HTTP_CONFLICT: Final[int] = 409
HTTP_UNPROCESSABLE: Final[int] = 422
HTTP_SERVICE_UNAVAILABLE: Final[int] = 503

# Raw logs and screenshots hold what an app printed or drew, so the browser must take them as what they say they are.
_RAW_FILE_HEADERS: Final[dict[str, str]] = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}

_NOT_BUILT_PAGE: Final[str] = (
    '<!doctype html><html><head><meta charset="utf-8"><title>App Store</title></head>'
    "<body><p>The App Store page has not been built yet (run <code>npm run build</code> in <code>system/</code>).</p>"
    "</body></html>"
)


class HealthResponse(FrozenModel):
    """What the health probe answers."""

    status: str = Field(description="Always 'ok'")
    is_frontend_built: bool = Field(description="Whether the page's bundle has been built, so the page can be served")


class UninstallRequest(FrozenModel):
    """How to uninstall an app."""

    is_deleting_data: bool = Field(description="Whether the app's own data (notebooks, databases, settings) goes too")


class InstallRecordResponse(FrozenModel):
    """The app's install record after the request was queued."""

    record: InstallRecord = Field(description="The record, now queued")


class RemovedResponse(FrozenModel):
    """An added app was taken out of the store."""

    slug: str = Field(description="The app removed")


class FinderJobResponse(FrozenModel):
    """A job working out an app's start command."""

    job: FinderJob = Field(description="Where the job stands")


class UpdateRequest(FrozenModel):
    """Which version to update an installed app to."""

    version: str = Field(
        default="", description="For an app added from PyPI or npm: the exact version; empty for the newest"
    )


class NewestVersionResponse(FrozenModel):
    """The newest version of an installed app, against the one installed."""

    installed_version: str = Field(description="The version installed now")
    newest_version: str = Field(description="The newest version there is")
    is_newer: bool = Field(description="Whether the newest version differs from the installed one")


class InstallFileResponse(FrozenModel):
    """The install file an app added from GitHub was added (or last updated) with."""

    install_file: RepoInstallFile = Field(description="The file, as the store keeps it")
    installed_version: str = Field(description="The version installed now")


class PendingRepoAddResponse(FrozenModel):
    """An app from GitHub waiting for the person to confirm it."""

    pending: PendingRepoAddView = Field(description="What it is, what will run, and whether it fits")


class DiscardedResponse(FrozenModel):
    """A pending add was dropped."""

    id: str = Field(description="The pending add dropped")


class TemplateSizeResponse(FrozenModel):
    """A template's size."""

    size_kb: int | None = Field(description="The template repository's size in KB; null when not estimated")


ROUTE_TABLE: RouteTable = {
    ("GET", "/"): SchemalessRoute(reason="the App Store page"),
    ("GET", "/assets/{filename}"): SchemalessRoute(reason="one file of the page's built bundle"),
    ("GET", HEALTH_PATH): RouteModels(response_model=HealthResponse),
    ("GET", STORE_PATH): RouteModels(response_model=StoreSnapshot),
    ("POST", "/api/apps/{slug}/install"): RouteModels(
        response_model=InstallRecordResponse, success_status=HTTP_ACCEPTED
    ),
    ("POST", "/api/apps/{slug}/uninstall"): RouteModels(
        request_model=UninstallRequest, response_model=InstallRecordResponse, success_status=HTTP_ACCEPTED
    ),
    ("POST", ADDED_APPS_CHECK_PATH): RouteModels(request_model=AddAppRequest, response_model=CheckResult),
    ("POST", ADDED_APPS_PATH): RouteModels(
        request_model=AddAppRequest, response_model=InstallRecordResponse, success_status=HTTP_ACCEPTED
    ),
    ("DELETE", "/api/added-apps/{slug}"): RouteModels(response_model=RemovedResponse),
    ("POST", FIND_COMMAND_PATH): RouteModels(
        request_model=AddAppRequest, response_model=FinderJobResponse, success_status=HTTP_ACCEPTED
    ),
    ("GET", FIND_COMMAND_PATH + "/{job_id}"): RouteModels(response_model=FinderJobResponse),
    ("POST", FIND_COMMAND_PATH + "/{job_id}/cancel"): RouteModels(response_model=FinderJobResponse),
    ("GET", FIND_COMMAND_PATH + "/{job_id}/screenshot.png"): SchemalessRoute(
        reason="a screenshot of the app while it was tried out, a PNG image"
    ),
    ("GET", FIND_COMMAND_PATH + "/{job_id}/log"): SchemalessRoute(
        reason="the raw log of working out the command, plain text"
    ),
    ("GET", "/api/apps/{slug}/newest-version"): RouteModels(response_model=NewestVersionResponse),
    ("GET", "/api/added-apps/{slug}/install-file"): RouteModels(response_model=InstallFileResponse),
    ("POST", "/api/apps/{slug}/update"): RouteModels(
        request_model=UpdateRequest, response_model=InstallRecordResponse, success_status=HTTP_ACCEPTED
    ),
    ("POST", REPO_ADDS_PATH): RouteModels(
        request_model=RepoAddSubmission, response_model=PendingRepoAddResponse, success_status=HTTP_CREATED
    ),
    ("GET", REPO_ADDS_PATH + "/{pending_id}"): RouteModels(response_model=PendingRepoAddResponse),
    ("POST", REPO_ADDS_PATH + "/{pending_id}/confirm"): RouteModels(
        response_model=InstallRecordResponse, success_status=HTTP_ACCEPTED
    ),
    ("DELETE", REPO_ADDS_PATH + "/{pending_id}"): RouteModels(response_model=DiscardedResponse),
    ("GET", REPO_ADDS_PATH + "/{pending_id}/screenshot.png"): SchemalessRoute(
        reason="the trial's screenshot of an app waiting for confirmation, a PNG image"
    ),
    ("GET", "/api/apps/{slug}/icon.svg"): SchemalessRoute(reason="the app's icon, an SVG file"),
    ("GET", "/api/apps/{slug}/log"): SchemalessRoute(reason="the app's raw install log, plain text"),
    ("GET", TEMPLATES_CATALOG_PATH): RouteModels(response_model=TemplatesCatalogResponse),
    ("GET", "/api/templates/{slug}/size"): RouteModels(response_model=TemplateSizeResponse),
    ("GET", APP_CONTRACT_ROUTE): SchemalessRoute(reason="the app contract module, a JavaScript file"),
    **THEME_STATIC_ROUTE_TABLE,
}


def build_pages_blueprint(
    static_directory: Path,
    engine: InstallEngine,
    adder: AppAdder,
    finder: CommandFinder,
    template_catalog: TemplateCatalogStore,
    template_sizes: TemplateSizeStore,
    contract_path: Path,
) -> Blueprint:
    blueprint = Blueprint(BLUEPRINT_NAME, __name__)
    repo_adds = RepoAdds(adder=adder, pending=PendingRepoAddStore(path=engine.layout.pending_repo_adds_path))
    register_api_schema_route(
        blueprint,
        DEFAULT_API_SCHEMA_PATH,
        title=APP_DISPLAY_NAME,
        description=APP_DESCRIPTION,
        route_table=ROUTE_TABLE,
    )
    register_workspace_theme_route(blueprint)
    # Installed apps' icons are drawn from their registered SVG as data: URLs; template drawings come from the
    # catalog's origin.
    image_sources = "'self' data: blob:"
    if template_catalog.catalog_url != "":
        image_sources = f"{image_sources} {url_origin(template_catalog.catalog_url)}"
    attach_document_policy(blueprint, {"img-src": image_sources})

    @blueprint.get("/")
    def page() -> ResponseReturnValue:
        """The App Store page: open-source apps to install, the apps this workspace has, and published templates."""
        document = static_directory / PAGE_DOCUMENT_FILENAME
        if not document.is_file():
            return Response(_NOT_BUILT_PAGE, mimetype="text/html", headers={"Cache-Control": "no-store"})
        return Response(
            document.read_text(encoding="utf-8"), mimetype="text/html", headers={"Cache-Control": "no-store"}
        )

    @blueprint.get("/assets/<path:filename>")
    def asset(filename: str) -> ResponseReturnValue:
        """One file of the page's built bundle, by name."""
        try:
            return send_from_directory((static_directory / "assets").absolute(), filename)
        except NotFound:
            return error_response(f"no asset {filename!r}", HTTP_NOT_FOUND)

    @blueprint.get(HEALTH_PATH)
    def health() -> ResponseReturnValue:
        """Whether the app is up, and whether its page has been built."""
        return model_response(
            HealthResponse(status="ok", is_frontend_built=(static_directory / PAGE_DOCUMENT_FILENAME).is_file())
        )

    @blueprint.get(STORE_PATH)
    def store() -> ResponseReturnValue:
        """The room the workspace has, every app in the store with where its install stands, and this workspace's apps."""
        room = engine.resources.read_room()
        records = engine.records.read()
        catalog = engine.catalog
        # The entries the catalog took (offered_catalog): a shipped app is never shown as one the user added.
        curated_slugs = {app.slug for app in engine.curated.apps}
        added_by_slug: dict[str, AddedApp] = {}
        for entry in engine.added.read():
            if entry.app.slug not in curated_slugs:
                added_by_slug.setdefault(entry.app.slug, entry)
        apps = tuple(
            store_app_entry(
                app,
                record,
                room,
                engine.layout.log_path(app.slug).is_file(),
                engine.memory_reader.program_memory_mb(app.slug)
                if record is not None and record.status is InstallStatus.INSTALLED
                else None,
                added_by_slug.get(app.slug),
            )
            for app, record in ((app, records.find(app.slug)) for app in catalog.apps)
        )
        store_slugs = {record.slug for record in records.records}
        return model_response(
            StoreSnapshot(
                room=room,
                categories=catalog.categories,
                apps=apps,
                workspace_apps=workspace_app_entries(engine.read_registry_rows(), store_slugs),
                busy_with=busy_slug(records),
                pending_repo_adds=tuple(repo_adds.view(pending) for pending in repo_adds.pending.read()),
            )
        )

    @blueprint.post("/api/apps/<slug>/install")
    def install(slug: str) -> ResponseReturnValue:
        """Queue an install of a store app (409 with the reason when it cannot be installed, e.g. it won't fit)."""
        try:
            record = engine.request_install(slug)
        except InstallRefusedError as e:
            return error_response(str(e), _refusal_status(engine, slug))
        return model_response(InstallRecordResponse(record=record), HTTP_ACCEPTED)

    @blueprint.post("/api/apps/<slug>/uninstall")
    def uninstall(slug: str) -> ResponseReturnValue:
        """Queue an uninstall of a store app, keeping or deleting its own data."""
        request_body = parse_request_body(UninstallRequest)
        try:
            record = engine.request_uninstall(slug, request_body.is_deleting_data)
        except InstallRefusedError as e:
            return error_response(str(e), _refusal_status(engine, slug))
        return model_response(InstallRecordResponse(record=record), HTTP_ACCEPTED)

    @blueprint.get("/api/apps/<slug>/newest-version")
    def newest_version(slug: str) -> ResponseReturnValue:
        """The newest version of an installed app: a shipped app's comes with the workspace; an app added from PyPI
        or npm is looked up on its registry (422 for an app added from GitHub, which a chat checks)."""
        record = engine.records.read().find(slug)
        app = engine.catalog.find(slug)
        if app is None or record is None or record.status is not InstallStatus.INSTALLED:
            return error_response(f"{slug} isn't installed.", HTTP_NOT_FOUND)
        added = engine.added.find(slug)
        if added is not None and added.source == "github":
            return error_response("A chat checks GitHub apps for a newer version.", HTTP_UNPROCESSABLE)
        try:
            newest = app.version if added is None else adder.newest_version(slug)
        except AddAppRefusedError as e:
            return error_response(str(e), HTTP_UNPROCESSABLE)
        return model_response(
            NewestVersionResponse(
                installed_version=record.version, newest_version=newest, is_newer=newest != record.version
            )
        )

    @blueprint.get("/api/added-apps/<slug>/install-file")
    def added_install_file(slug: str) -> ResponseReturnValue:
        """The install file an app added from GitHub runs, which a chat starts from to look for a newer version."""
        added = engine.added.find(slug)
        record = engine.records.read().find(slug)
        if added is None or added.repo_file is None:
            return error_response(f"{slug} isn't an app you added from GitHub.", HTTP_NOT_FOUND)
        return model_response(
            InstallFileResponse(
                install_file=added.repo_file, installed_version=record.version if record is not None else ""
            )
        )

    @blueprint.post("/api/apps/<slug>/update")
    def update(slug: str) -> ResponseReturnValue:
        """Queue an update of an installed app, keeping its data; the version it had is put back if the new one
        fails. A shipped app updates to the catalog's version; an app added from PyPI or npm to ``version`` (empty
        for the newest). 409 when it cannot be updated now; 422 for an app added from GitHub, which a chat updates."""
        request_body = parse_request_body(UpdateRequest)
        added = engine.added.find(slug)
        if added is not None and added.source == "github":
            return error_response("A chat updates GitHub apps: check for a newer version.", HTTP_UNPROCESSABLE)
        try:
            if added is None:
                target = engine.catalog.find(slug)
                if target is None:
                    return error_response(f"There is no app called {slug!r} in the store.", HTTP_NOT_FOUND)
                record = engine.request_update(slug, target)
            else:
                entry = adder.build_update(slug, request_body.version.strip())
                record = engine.request_update(slug, entry.app, entry)
        except AddAppRefusedError as e:
            return error_response(str(e), HTTP_UNPROCESSABLE)
        except InstallRefusedError as e:
            return error_response(str(e), HTTP_CONFLICT)
        return model_response(InstallRecordResponse(record=record), HTTP_ACCEPTED)

    @blueprint.post(ADDED_APPS_CHECK_PATH)
    def check_added_app() -> ResponseReturnValue:
        """Check an app to add from PyPI or npm without installing it: the version it pins, how many packages it
        pulls in, its estimated disk, and whether it fits (422 with the reason when a rule refuses it)."""
        request_body = parse_request_body(AddAppRequest)
        try:
            return model_response(adder.check(request_body))
        except AddAppRefusedError as e:
            return error_response(str(e), HTTP_UNPROCESSABLE)

    @blueprint.post(ADDED_APPS_PATH)
    def add_app() -> ResponseReturnValue:
        """Add an app from PyPI or npm to the store and queue its install (422 when a rule refuses it, 409 when it
        cannot be installed now)."""
        request_body = parse_request_body(AddAppRequest)
        try:
            record = adder.add(request_body)
        except AddAppRefusedError as e:
            return error_response(str(e), HTTP_UNPROCESSABLE)
        except InstallRefusedError as e:
            return error_response(str(e), HTTP_CONFLICT)
        return model_response(InstallRecordResponse(record=record), HTTP_ACCEPTED)

    @blueprint.delete("/api/added-apps/<slug>")
    def remove_added_app(slug: str) -> ResponseReturnValue:
        """Take an added app out of the store, with whatever it left behind; only once it is uninstalled."""
        try:
            adder.remove(slug)
        except AddAppRefusedError as e:
            return error_response(str(e), HTTP_UNPROCESSABLE)
        return model_response(RemovedResponse(slug=slug))

    @blueprint.post(FIND_COMMAND_PATH)
    def find_command() -> ResponseReturnValue:
        """Start working out an added app's start command: install the package on the side, have AI write the
        command, and test-launch it (422 when the package request breaks a rule). Poll the job for the outcome."""
        request_body = parse_request_body(AddAppRequest)
        try:
            job = finder.start(request_body)
        except AddAppRefusedError as e:
            return error_response(str(e), HTTP_UNPROCESSABLE)
        return model_response(FinderJobResponse(job=job), HTTP_ACCEPTED)

    @blueprint.get(FIND_COMMAND_PATH + "/<job_id>")
    def find_command_job(job_id: str) -> ResponseReturnValue:
        """Where a job working out a start command stands: what it is doing, or what it found and why."""
        job = finder.get(job_id)
        if job is None:
            return error_response(f"no job {job_id!r}", HTTP_NOT_FOUND)
        return model_response(FinderJobResponse(job=job))

    @blueprint.post(FIND_COMMAND_PATH + "/<job_id>/cancel")
    def cancel_find_command(job_id: str) -> ResponseReturnValue:
        """Stop a job working out a start command; it cleans up what it downloaded."""
        job = finder.cancel(job_id)
        if job is None:
            return error_response(f"no job {job_id!r}", HTTP_NOT_FOUND)
        return model_response(FinderJobResponse(job=job))

    @blueprint.get(FIND_COMMAND_PATH + "/<job_id>/screenshot.png")
    def find_command_screenshot(job_id: str) -> ResponseReturnValue:
        """A screenshot of the app while it was being tried out."""
        image = finder.screenshot(job_id)
        if image is None:
            return error_response(f"no screenshot for {job_id!r}", HTTP_NOT_FOUND)
        return _png(image)

    @blueprint.get(FIND_COMMAND_PATH + "/<job_id>/log")
    def find_command_log(job_id: str) -> ResponseReturnValue:
        """The raw log of working out a start command: every command run, its output, and the AI's answers."""
        log_path = finder.log_path(job_id)
        if finder.get(job_id) is None or not log_path.is_file():
            return error_response(f"no log for {job_id!r}", HTTP_NOT_FOUND)
        return _plain_text(log_path)

    @blueprint.post(REPO_ADDS_PATH)
    def submit_repo_add() -> ResponseReturnValue:
        """Hand the store an install file for one commit of a GitHub repository, as a chat does once it has worked
        out how to install and start it. The store checks it by the catalog's rules and holds it for the person to
        confirm in the store; nothing is installed yet (422 with the reason when a rule refuses it)."""
        request_body = parse_request_body(RepoAddSubmission)
        try:
            pending = repo_adds.submit(request_body)
        except AddAppRefusedError as e:
            return error_response(str(e), HTTP_UNPROCESSABLE)
        return model_response(PendingRepoAddResponse(pending=repo_adds.view(pending)), HTTP_CREATED)

    @blueprint.get(REPO_ADDS_PATH + "/<pending_id>")
    def pending_repo_add(pending_id: str) -> ResponseReturnValue:
        """An app from GitHub waiting for confirmation: what it is, the exact commands it runs, and whether it fits."""
        pending = repo_adds.pending.find(pending_id)
        if pending is None:
            return error_response("That app is no longer waiting to be added.", HTTP_NOT_FOUND)
        return model_response(PendingRepoAddResponse(pending=repo_adds.view(pending)))

    @blueprint.post(REPO_ADDS_PATH + "/<pending_id>/confirm")
    def confirm_repo_add(pending_id: str) -> ResponseReturnValue:
        """Add an app from GitHub the person confirmed, and queue its install (422 when it can no longer be added,
        409 when it cannot be installed now)."""
        try:
            record = repo_adds.confirm(pending_id)
        except AddAppRefusedError as e:
            return error_response(str(e), HTTP_UNPROCESSABLE)
        except InstallRefusedError as e:
            return error_response(str(e), HTTP_CONFLICT)
        return model_response(InstallRecordResponse(record=record), HTTP_ACCEPTED)

    @blueprint.get(REPO_ADDS_PATH + "/<pending_id>/screenshot.png")
    def pending_repo_add_screenshot(pending_id: str) -> ResponseReturnValue:
        """The screenshot the chat's trial took of an app waiting for confirmation, inside a frame as its window
        will show it."""
        image = repo_adds.pending.screenshot(pending_id)
        if image is None:
            return error_response(f"no screenshot for {pending_id!r}", HTTP_NOT_FOUND)
        return _png(image)

    @blueprint.delete(REPO_ADDS_PATH + "/<pending_id>")
    def discard_repo_add(pending_id: str) -> ResponseReturnValue:
        """Drop an app from GitHub that was waiting for confirmation, without adding it."""
        try:
            repo_adds.discard(pending_id)
        except AddAppRefusedError as e:
            return error_response(str(e), HTTP_NOT_FOUND)
        return model_response(DiscardedResponse(id=pending_id))

    @blueprint.get("/api/apps/<slug>/icon.svg")
    def icon(slug: str) -> ResponseReturnValue:
        """A store app's icon."""
        app = engine.catalog.find(slug)
        if app is None:
            return error_response(f"no app {slug!r} in the store", HTTP_NOT_FOUND)
        return send_file((engine.icons_directory / app.icon).absolute(), mimetype="image/svg+xml", max_age=3600)

    @blueprint.get("/api/apps/<slug>/log")
    def install_log(slug: str) -> ResponseReturnValue:
        """A store app's raw install log, as written."""
        if engine.catalog.find(slug) is None:
            return error_response(f"no app {slug!r} in the store", HTTP_NOT_FOUND)
        log_path = engine.layout.log_path(slug)
        if not log_path.is_file():
            return error_response(f"no install log for {slug}", HTTP_NOT_FOUND)
        return _plain_text(log_path)

    @blueprint.get(TEMPLATES_CATALOG_PATH)
    def templates_catalog() -> ResponseReturnValue:
        """The published template catalog as last fetched (null when none is configured; 503 when none could be loaded)."""
        reading = template_catalog.read()
        match reading.availability:
            case TemplateCatalogAvailability.DISABLED:
                return model_response(TemplatesCatalogResponse(catalog=None, is_stale=False))
            case TemplateCatalogAvailability.UNAVAILABLE:
                return error_response("failed to load templates", HTTP_SERVICE_UNAVAILABLE)
            case TemplateCatalogAvailability.FRESH | TemplateCatalogAvailability.STALE:
                assert reading.catalog is not None, "a fresh or stale reading carries its catalog"
                return model_response(
                    TemplatesCatalogResponse(
                        catalog=catalog_wire(reading.catalog, template_catalog.catalog_url),
                        is_stale=reading.availability is TemplateCatalogAvailability.STALE,
                    )
                )
            case _ as unreachable:
                assert_never(unreachable)

    @blueprint.get("/api/templates/<slug>/size")
    def template_size(slug: str) -> ResponseReturnValue:
        """A template's size: its repository's, looked up once and cached (null when not estimated)."""
        reading = template_catalog.read()
        template = None
        if reading.catalog is not None:
            template = next((entry for entry in reading.catalog.templates if entry.slug == slug), None)
        if template is None:
            return error_response(f"no template {slug!r} in the catalog", HTTP_NOT_FOUND)
        return model_response(TemplateSizeResponse(size_kb=template_sizes.size_kb(template.repository_url)))

    @blueprint.get(APP_CONTRACT_ROUTE)
    def app_contract() -> ResponseReturnValue:
        """The app contract module: how the page talks to the shell framing it."""
        if not contract_path.is_file():
            return error_response(
                f"the workspace shell's frontend is not built: {contract_path} is missing", HTTP_NOT_FOUND
            )
        return send_file(contract_path.absolute(), mimetype="text/javascript")

    return blueprint


def _refusal_status(engine: InstallEngine, slug: str) -> int:
    """404 for an app the store does not have; 409 for one it has that cannot take the job now."""
    return HTTP_NOT_FOUND if engine.catalog.find(slug) is None else HTTP_CONFLICT


def _plain_text(path: Path) -> Response:
    """A raw log, as written."""
    return Response(
        path.read_text(encoding="utf-8", errors="replace"), mimetype="text/plain", headers=_RAW_FILE_HEADERS
    )


def _png(image: bytes) -> Response:
    """A screenshot, as taken."""
    return Response(image, mimetype="image/png", headers=_RAW_FILE_HEADERS)
