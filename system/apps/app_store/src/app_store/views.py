"""What the page is sent: the room, the catalog's apps with where each stands, and the apps this workspace has.

"The apps this workspace has" is the app registry (``data/.state/apps.toml``), the same list the desktop's launcher
draws: every app a user can open, with the icon it registered. The store itself and the workspace's internal origins
are left out. Each is labelled by where it came from: built into the workspace (a manifest in a memory band of its
own), made in this workspace (the user band), or installed from the store (an install record names it).
"""

from enum import auto
from typing import Final

from pydantic import Field

from app_manifest.registry import RegistryRow
from app_store.added_apps import AddedApp
from app_store.added_apps import SOURCE_NAMES
from app_store.catalog import AppCategory
from app_store.catalog import CatalogApp
from app_store.catalog import PythonGitRecipe
from app_store.catalog import PythonRecipe
from app_store.records import InstallRecord
from app_store.records import InstallStatus
from app_store.records import JobKind
from app_store.records import StoreRecords
from app_store.repo_adds import PendingRepoAddView
from app_store.room import RoomVerdict
from app_store.room import WorkspaceRoom
from app_store.room import check_app_room
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure

STORE_APP_NAME: Final[str] = "app-store"
USER_PRIORITY: Final[str] = "user"


class UpdateKind(UpperCaseStrEnum):
    """How a newer version of an installed app is found."""

    CATALOG = auto()
    REGISTRY = auto()
    GITHUB = auto()


class AppOrigin(UpperCaseStrEnum):
    """Where an app on this workspace came from."""

    BUILT_IN = auto()
    MADE_HERE = auto()
    STORE = auto()


# How a curated app's figures were worked out (apps.toml says the same at its top).
CURATED_RESOURCES_BASIS: Final[str] = (
    "Measured on a workspace when the app was added to the store: the disk its environment takes after a first run, "
    "and its memory idle and while one person uses it the ordinary way."
)


class AddedWith(FrozenModel):
    """What an app the user added was added with."""

    registry: str = Field(description="PyPI, npm, or GitHub")
    package: str = Field(description="The package's name; for a GitHub repository, its owner/name")
    version: str = Field(description="The pinned version; it never changes on repair or reinstall")
    command: tuple[str, ...] = Field(description="The start command as given: program, then arguments")
    python: str | None = Field(description="The Python version, for a Python package")
    registry_url: str = Field(
        description="The package's page on its registry, at that version; for a repository, the commit's page"
    )
    package_count: int | None = Field(description="How many packages it pulls in; null when unresolved")
    chat_id: str = Field(description="For a repository: the chat that worked out how to install it; empty otherwise")


class StoreAppEntry(FrozenModel):
    """One catalog app as the page shows it."""

    slug: str = Field(description="The catalog slug; also the installed app's name")
    is_added: bool = Field(
        description="Whether the user added it (nobody reviewed it) rather than the store shipping it"
    )
    added_with: AddedWith | None = Field(description="What it was added with; null for a shipped app")
    tags: tuple[str, ...] = Field(description="The keys of the categories it belongs to")
    title: str = Field(description="The name users see")
    description: str = Field(description="One line on what it is for")
    about: str = Field(description="A short paragraph for its detail page")
    author: str = Field(description="Who makes it")
    license: str = Field(description="Its license (SPDX)")
    homepage_url: str = Field(description="The project's home page")
    source_url: str = Field(description="Where its source code lives")
    version: str = Field(description="The pinned version")
    source_label: str = Field(description="What kind of package it is, e.g. 'Python package'")
    package_spec: str = Field(description="Exactly what is installed, e.g. 'datasette==0.65.1'")
    icon_url: str = Field(description="Its icon, served by the store")
    disk_mb: int | None = Field(description="Estimated disk once installed, in MB; null when unknown")
    memory_idle_mb: int | None = Field(description="Estimated memory with nobody using it, in MB; null when unknown")
    memory_in_use_mb: int | None = Field(
        description="Estimated memory while one person uses it, in MB; the fit is checked against it; null when unknown"
    )
    build_disk_mb: int | None = Field(description="Disk it needs while it builds, in MB; null when it is not built")
    build_memory_mb: int | None = Field(
        description="Memory it needs while it builds, in MB; the fit is checked against it too; null when not built"
    )
    resources_basis: str = Field(description="How the figures were worked out, in plain words")
    peak_memory_mb: int | None = Field(
        description="The most its processes held in the first minutes of a run, measured; null until it has run"
    )
    verdict: RoomVerdict = Field(description="Whether it fits in the room the workspace has now")
    blocked_reason: str = Field(description="Why it cannot be installed; empty when it can")
    status: InstallStatus | None = Field(description="Where its install stands; null when never installed")
    job: JobKind | None = Field(description="The job queued or running for it")
    failure_reason: str = Field(description="Why its last job failed; empty otherwise")
    measured_disk_mb: int | None = Field(description="Disk its folder takes, measured after install")
    memory_now_mb: int | None = Field(
        description="What the installed app's processes use right now, in MB; null when it is not installed or not "
        "running (it stops a minute after its last window closes)"
    )
    app_url: str | None = Field(description="Where the installed app answers inside the workspace")
    installed_version: str | None = Field(description="The version installed now; null when it is not installed")
    update_kind: UpdateKind | None = Field(
        description="How a newer version is found for the installed app: CATALOG (it comes with the workspace's "
        "updates), REGISTRY (PyPI or npm, asked on request), or GITHUB (a chat checks); null when not installed"
    )
    update_version: str | None = Field(
        description="A newer version ready to install now (a shipped app's, from the catalog); null when none is known"
    )
    has_log: bool = Field(description="Whether an install log exists to show")


class WorkspaceAppEntry(FrozenModel):
    """One app this workspace has, as the launcher lists it."""

    name: str = Field(description="The registered app name")
    display_name: str = Field(description="What users see")
    description: str = Field(description="One line on what it is for; empty when it declares none")
    icon_svg: str = Field(description="The registered SVG icon, verbatim; empty when it has none")
    url: str = Field(description="Where it answers inside the workspace; the page opens it through the shell")
    origin: AppOrigin = Field(description="Where it came from")


class StoreSnapshot(FrozenModel):
    """Everything the page draws, in one answer it polls."""

    room: WorkspaceRoom = Field(description="What the workspace has free right now")
    categories: tuple[AppCategory, ...] = Field(description="The store's filters, in display order")
    apps: tuple[StoreAppEntry, ...] = Field(description="The catalog's apps, in catalog order")
    workspace_apps: tuple[WorkspaceAppEntry, ...] = Field(description="The apps this workspace has, in launcher order")
    busy_with: str | None = Field(description="The slug whose job is running; null when the store is idle")
    pending_repo_adds: tuple[PendingRepoAddView, ...] = Field(
        default=(), description="Apps a chat worked out how to install from GitHub, waiting for confirmation"
    )


@pure
def added_with(added: AddedApp) -> AddedWith:
    recipe = added.app.recipe
    return AddedWith(
        registry=SOURCE_NAMES[added.source],
        package=added.package,
        version=added.app.version,
        command=added.command,
        python=recipe.python if isinstance(recipe, PythonRecipe | PythonGitRecipe) else None,
        registry_url=added.registry_url,
        package_count=added.package_count,
        chat_id=added.chat_id,
    )


@pure
def store_app_entry(
    app: CatalogApp,
    record: InstallRecord | None,
    room: WorkspaceRoom,
    has_log: bool,
    memory_now_mb: int | None,
    added: AddedApp | None = None,
) -> StoreAppEntry:
    check = check_app_room(app, room)
    is_installed = record is not None and record.status is InstallStatus.INSTALLED
    return StoreAppEntry(
        slug=app.slug,
        is_added=added is not None,
        added_with=added_with(added) if added is not None else None,
        tags=app.tags,
        title=app.title,
        description=app.description,
        about=app.about,
        author=app.author,
        license=app.license,
        homepage_url=app.homepage_url,
        source_url=app.source_url,
        version=app.version,
        source_label=app.recipe.source_label,
        package_spec=app.recipe.package_spec,
        icon_url=f"/api/apps/{app.slug}/icon.svg",
        disk_mb=app.resources.disk_mb,
        memory_idle_mb=app.resources.memory_idle_mb,
        memory_in_use_mb=app.resources.memory_in_use_mb,
        build_disk_mb=app.resources.build_disk_mb,
        build_memory_mb=app.resources.build_memory_mb,
        resources_basis=app.resources.basis or CURATED_RESOURCES_BASIS,
        peak_memory_mb=record.peak_memory_mb if record is not None else None,
        verdict=check.verdict,
        blocked_reason=check.blocked_reason,
        status=record.status if record is not None else None,
        job=record.job if record is not None else None,
        failure_reason=record.failure_reason if record is not None else "",
        measured_disk_mb=record.measured_disk_mb if record is not None else None,
        memory_now_mb=memory_now_mb if is_installed else None,
        app_url=f"http://localhost:{record.port}" if is_installed and record.port is not None else None,
        installed_version=record.version if is_installed and record is not None else None,
        update_kind=update_kind_of(added) if is_installed else None,
        update_version=app.version
        if is_installed and record is not None and added is None and record.version != app.version
        else None,
        has_log=has_log,
    )


@pure
def update_kind_of(added: AddedApp | None) -> UpdateKind:
    if added is None:
        return UpdateKind.CATALOG
    return UpdateKind.GITHUB if added.source == "github" else UpdateKind.REGISTRY


@pure
def workspace_app_entries(rows: list[RegistryRow], store_slugs: set[str]) -> tuple[WorkspaceAppEntry, ...]:
    """The registry's openable apps, the store itself left out, ordered as the launcher orders them: ranked apps
    first by rank, then the rest by name."""
    visible = [row for row in rows if not row.internal and str(row.name) != STORE_APP_NAME]
    visible.sort(
        key=lambda row: (row.launcher_rank is None, row.launcher_rank or 0, str(row.display_name or row.name).lower())
    )
    entries: list[WorkspaceAppEntry] = []
    for row in visible:
        name = str(row.name)
        if name in store_slugs:
            origin = AppOrigin.STORE
        elif str(row.priority) == USER_PRIORITY:
            origin = AppOrigin.MADE_HERE
        else:
            origin = AppOrigin.BUILT_IN
        entries.append(
            WorkspaceAppEntry(
                name=name,
                display_name=str(row.display_name or row.name),
                description=str(row.description or ""),
                icon_svg=row.icon or "",
                url=str(row.url),
                origin=origin,
            )
        )
    return tuple(entries)


@pure
def busy_slug(records: StoreRecords) -> str | None:
    if not records.queue:
        return None
    head = records.find(records.queue[0])
    return head.slug if head is not None else None
