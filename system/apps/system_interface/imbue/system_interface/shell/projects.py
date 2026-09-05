"""Projects: the shared views of contracts.md section 6, stored in ``projects.json`` (section 7)."""

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from typing import Final

from app_manifest.manifest import DefaultShortcut
from app_manifest.primitives import AppName
from app_manifest.registry import RegistryRow
from loguru import logger
from pydantic import Field
from pydantic import ValidationError

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.system_interface.shell.data_types import Project
from imbue.system_interface.shell.data_types import Shortcut
from imbue.system_interface.shell.data_types import effective_actions
from imbue.system_interface.shell.errors import ProjectConflictError
from imbue.system_interface.shell.errors import ProjectNotFoundError
from imbue.system_interface.shell.errors import ProjectValueError
from imbue.system_interface.shell.primitives import Address
from imbue.system_interface.shell.primitives import EVERYTHING_VIEW_ID
from imbue.system_interface.shell.primitives import ProjectId
from imbue.system_interface.shell.primitives import ViewId
from imbue.system_interface.shell.state_files import STATE_FILES_LOCK
from imbue.system_interface.shell.state_files import read_json_object
from imbue.system_interface.shell.state_files import write_json_atomic

PROJECTS_FILENAME: Final[str] = "projects.json"
PROJECTS_FILE_VERSION: Final[int] = 1

# ``glyph`` indexes the frontend's squiggle table, which has exactly ten entries.
GLYPH_COUNT: Final[int] = 10
_COLOR_PATTERN: Final[re.Pattern[str]] = re.compile(r"#[0-9a-fA-F]{6}")
_SLUG_STRIP_PATTERN: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9]+")


class ProjectsDocument(FrozenModel):
    """The whole of ``projects.json``."""

    version: int = Field(description="The file format version")
    projects: tuple[Project, ...] = Field(description="Every project, in creation order")


@pure
def slugify_project_name(name: str) -> ProjectId:
    """The id a project name shortens to; raises ProjectValueError when nothing usable remains."""
    slug = _SLUG_STRIP_PATTERN.sub("-", name.strip().lower()).strip("-")
    if not slug:
        raise ProjectValueError(f"Project name {name!r} contains no usable characters")
    try:
        return ProjectId(slug)
    except ValueError as e:
        raise ProjectValueError(f"Project name {name!r} cannot be a project id: {e}") from e


@pure
def validated_project_name(name: str) -> str:
    trimmed = name.strip()
    if not trimmed:
        raise ProjectValueError("Project name is empty")
    return trimmed


@pure
def validated_project_color(color: str) -> str:
    trimmed = color.strip()
    if _COLOR_PATTERN.fullmatch(trimmed) is None:
        raise ProjectValueError(f"Project color {color!r} is not a '#RRGGBB' hex string")
    return trimmed


@pure
def validated_project_glyph(glyph: int) -> int:
    if not 0 <= glyph < GLYPH_COUNT:
        raise ProjectValueError(f"Project glyph {glyph} is outside the range 0..{GLYPH_COUNT - 1}")
    return glyph


@pure
def seed_shortcuts(rows: Sequence[RegistryRow]) -> tuple[Shortcut, ...]:
    """A new project's shortcuts: every registered app's ``default_shortcut``, in registry order."""
    shortcuts: list[Shortcut] = []
    for row in rows:
        if row.internal or row.default_shortcut is None:
            continue
        shortcuts.append(shortcut_from_default(row.name, row.default_shortcut))
    return tuple(shortcuts)


@pure
def shortcut_from_default(app: AppName, default_shortcut: DefaultShortcut) -> Shortcut:
    return Shortcut(app=app, action=default_shortcut.action, mode=default_shortcut.mode)


@pure
def validated_shortcut(shortcut: Shortcut, row: RegistryRow | None) -> Shortcut:
    """A shortcut whose action the app declares (or the synthesized ``open``); raises ProjectValueError otherwise."""
    if row is None:
        raise ProjectValueError(f"No registered app named {shortcut.app!r}")
    if shortcut.action not in {action.id for action in effective_actions(row)}:
        raise ProjectValueError(f"App {shortcut.app!r} declares no action {shortcut.action!r}")
    return shortcut


@pure
def project_wire_json(project: Project) -> dict[str, Any]:
    """The ``project`` object of contracts.md section 6."""
    return {
        "id": str(project.id),
        "name": project.name,
        "color": project.color,
        "glyph": project.glyph,
        "tabs": [str(address) for address in project.tabs],
        "shortcuts": [
            {"app": str(shortcut.app), "action": str(shortcut.action), "mode": shortcut.mode.value}
            for shortcut in project.shortcuts
        ],
    }


@pure
def _with_shortcut(project: Project, shortcut: Shortcut) -> Project:
    is_present = any(
        existing.app == shortcut.app and existing.action == shortcut.action for existing in project.shortcuts
    )
    replaced = tuple(
        shortcut if existing.app == shortcut.app and existing.action == shortcut.action else existing
        for existing in project.shortcuts
    )
    shortcuts = replaced if is_present else (*project.shortcuts, shortcut)
    return project.model_copy_update(to_update(project.field_ref().shortcuts, shortcuts))


class ProjectStore(MutableModel):
    """Reads and writes ``projects.json`` under the shell's state lock."""

    state_directory: Path = Field(frozen=True, description="The shell's state directory")

    def _path(self) -> Path:
        return self.state_directory / PROJECTS_FILENAME

    def _read_unlocked(self) -> ProjectsDocument:
        """The stored document; an absent or unreadable file is an empty registry (logged), never a crash."""
        raw = read_json_object(self._path())
        if raw is None:
            return ProjectsDocument(version=PROJECTS_FILE_VERSION, projects=())
        try:
            return ProjectsDocument.model_validate(raw)
        except ValidationError as e:
            logger.warning("Ignored an unreadable projects file at {}: {}", self._path(), e.errors()[0]["msg"])
            return ProjectsDocument(version=PROJECTS_FILE_VERSION, projects=())

    def _write_unlocked(self, document: ProjectsDocument) -> None:
        write_json_atomic(self._path(), document.model_dump(mode="json"))

    def list_projects(self) -> list[Project]:
        with STATE_FILES_LOCK:
            return list(self._read_unlocked().projects)

    def get_project(self, project_id: str) -> Project:
        for project in self.list_projects():
            if project.id == project_id:
                return project
        raise ProjectNotFoundError(project_id)

    def is_view_known(self, view_id: str) -> bool:
        return view_id == EVERYTHING_VIEW_ID or any(project.id == view_id for project in self.list_projects())

    def create_project(self, name: str, color: str, glyph: int, shortcuts: Sequence[Shortcut]) -> Project:
        """Register a new empty project; two names that shorten to one id are a conflict."""
        project = Project(
            id=slugify_project_name(name),
            name=validated_project_name(name),
            color=validated_project_color(color),
            glyph=validated_project_glyph(glyph),
            tabs=(),
            shortcuts=tuple(shortcuts),
        )
        with STATE_FILES_LOCK:
            document = self._read_unlocked()
            existing = next((candidate for candidate in document.projects if candidate.id == project.id), None)
            if existing is not None:
                raise ProjectConflictError(
                    f"Project name {name!r} conflicts with existing project {existing.name!r} (both shorten to '{project.id}')"
                )
            self._write_unlocked(
                document.model_copy_update(to_update(document.field_ref().projects, (*document.projects, project)))
            )
        return project

    def update_project_settings(self, project_id: str, name: str, color: str, glyph: int) -> Project:
        """Replace one project's display metadata; the id, tabs, and shortcuts stay."""
        return self._replace(
            project_id,
            lambda project: project.model_copy_update(
                to_update(project.field_ref().name, validated_project_name(name)),
                to_update(project.field_ref().color, validated_project_color(color)),
                to_update(project.field_ref().glyph, validated_project_glyph(glyph)),
            ),
        )

    def delete_project(self, project_id: str) -> ViewId:
        """Delete a project and return the view clients on it should fall back to."""
        with STATE_FILES_LOCK:
            document = self._read_unlocked()
            if not any(project.id == project_id for project in document.projects):
                raise ProjectNotFoundError(project_id)
            remaining = tuple(project for project in document.projects if project.id != project_id)
            self._write_unlocked(document.model_copy_update(to_update(document.field_ref().projects, remaining)))
        return ViewId(remaining[0].id) if remaining else ViewId(EVERYTHING_VIEW_ID)

    def add_tab(self, project_id: str, address: Address) -> Project:
        """Add an address to the project's tab set; idempotent."""
        return self._replace(
            project_id,
            lambda project: (
                project
                if address in project.tabs
                else project.model_copy_update(to_update(project.field_ref().tabs, (*project.tabs, address)))
            ),
        )

    def remove_tab(self, project_id: str, address: Address) -> Project:
        return self._replace(
            project_id,
            lambda project: project.model_copy_update(
                to_update(project.field_ref().tabs, tuple(tab for tab in project.tabs if tab != address))
            ),
        )

    def set_shortcut(self, project_id: str, shortcut: Shortcut) -> Project:
        """Add the shortcut at the end of the rail, or replace the entry for the same (app, action) in place."""
        return self._replace(project_id, lambda project: _with_shortcut(project, shortcut))

    def remove_shortcut(self, project_id: str, app: str, action: str) -> Project:
        return self._replace(
            project_id,
            lambda project: project.model_copy_update(
                to_update(
                    project.field_ref().shortcuts,
                    tuple(
                        existing
                        for existing in project.shortcuts
                        if not (existing.app == app and existing.action == action)
                    ),
                )
            ),
        )

    def remove_addresses_everywhere(self, addresses: Sequence[Address]) -> list[ProjectId]:
        """Drop addresses no app lists any more from every tab set; returns the projects that changed."""
        doomed = set(addresses)
        changed: list[ProjectId] = []
        with STATE_FILES_LOCK:
            document = self._read_unlocked()
            updated: list[Project] = []
            for project in document.projects:
                kept = tuple(tab for tab in project.tabs if tab not in doomed)
                if len(kept) != len(project.tabs):
                    changed.append(project.id)
                    updated.append(project.model_copy_update(to_update(project.field_ref().tabs, kept)))
                else:
                    updated.append(project)
            if changed:
                self._write_unlocked(
                    document.model_copy_update(to_update(document.field_ref().projects, tuple(updated)))
                )
        return changed

    def referenced_addresses(self) -> set[Address]:
        return {address for project in self.list_projects() for address in project.tabs}

    def _replace(self, project_id: str, transform: Any) -> Project:
        with STATE_FILES_LOCK:
            document = self._read_unlocked()
            updated: list[Project] = []
            replaced: Project | None = None
            for project in document.projects:
                if project.id == project_id:
                    replaced = transform(project)
                    updated.append(replaced)
                else:
                    updated.append(project)
            if replaced is None:
                raise ProjectNotFoundError(project_id)
            self._write_unlocked(document.model_copy_update(to_update(document.field_ref().projects, tuple(updated))))
        return replaced
