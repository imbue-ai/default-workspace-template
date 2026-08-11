"""Project storage for the system interface.

A *project* is a saved dockview arrangement plus the display metadata that
identifies it in the UI: a name, a ``#RRGGBB`` color, and a glyph index into
the frontend's squiggle table. Membership is implicit -- a tab belongs to a
project exactly when a panel for it exists in that project's saved content --
so there is no separate membership map that could drift out of sync.

Storage mirrors named layouts (see ``workspace_layouts.py``): each project's
content lives in its own file under ``<workspace_layout_dir>/projects/<id>.json``
with a small registry file (``projects_meta.json``) holding the per-project
metadata and the last-active id. A project with no content file yet is simply
"empty", which the frontend renders as the fresh welcome-chat state.

The ``everything`` project always exists and can never be deleted. New tabs are
mirrored into its content as well as the active project's, which is what makes
it the unfiltered view while still letting it keep its own arrangement.
"""

import json
import re
import threading
from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger as _loguru_logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure

EVERYTHING_PROJECT_ID: Final[str] = "everything"
EVERYTHING_PROJECT_NAME: Final[str] = "Everything"

_PROJECTS_SUBDIR: Final[str] = "projects"
_META_FILENAME: Final[str] = "projects_meta.json"

# ``glyph`` indexes the frontend's squiggle table, which has exactly ten
# entries. Everything takes the first one and that entry's palette color.
_GLYPH_COUNT: Final[int] = 10
_EVERYTHING_GLYPH: Final[int] = 0
_EVERYTHING_COLOR: Final[str] = "#F0603A"

_COLOR_PATTERN: Final[re.Pattern[str]] = re.compile(r"#[0-9a-fA-F]{6}")

# Serializes every read-modify-write of the meta file + content files across
# the threaded WSGI server, exactly as ``workspace_layouts._layouts_lock`` does
# for named layouts.
_projects_lock = threading.Lock()


class ProjectNameError(ValueError):
    """Raised when a project name is empty or has no usable characters."""

    ...


class ProjectConflictError(ValueError):
    """Raised when a new project's id collides with an existing project."""

    ...


class ProjectNotFoundError(KeyError):
    """Raised when a project id does not exist in the registry."""

    def __init__(self, project_id: str) -> None:
        self.project_id = project_id
        super().__init__(f"Project '{project_id}' not found")


class EverythingProjectDeletionError(ValueError):
    """Raised when deleting the undeletable ``everything`` project is attempted."""

    ...


class ProjectColorError(ValueError):
    """Raised when a project color is not a ``#RRGGBB`` hex string."""

    ...


class ProjectGlyphError(ValueError):
    """Raised when a glyph index falls outside the frontend's glyph table."""

    ...


class ProjectInfo(FrozenModel):
    """One project as listed to clients."""

    project_id: str = Field(description="Slugified filename-safe identifier")
    name: str = Field(description="Free-form name shown in the UI")
    color: str = Field(description="Accent color as a '#RRGGBB' hex string")
    glyph: int = Field(description="Index into the frontend's squiggle glyph table")
    has_content: bool = Field(description="Whether a saved content file exists yet")


@pure
def slugify_project_name(name: str) -> str:
    """Project a free-form project name onto its filename-safe id.

    Raises ProjectNameError when nothing usable remains after slugification.
    """
    lowered = name.strip().lower()
    project_id = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    if not project_id:
        raise ProjectNameError(f"Project name {name!r} contains no usable characters")
    return project_id


@pure
def _validated_name(name: str) -> str:
    """Whitespace-trim a display name, rejecting one that is empty.

    Unlike id derivation this accepts any non-blank text: a rename never moves
    the content file, so the name has no filename to stay compatible with.
    """
    trimmed = name.strip()
    if not trimmed:
        raise ProjectNameError("Project name is empty")
    return trimmed


@pure
def _validated_color(color: str) -> str:
    """Whitespace-trim a ``#RRGGBB`` color, rejecting anything else.

    Case is preserved: the frontend compares swatches against the literal
    palette strings in its glyph table, which are uppercase.
    """
    trimmed = color.strip()
    if _COLOR_PATTERN.fullmatch(trimmed) is None:
        raise ProjectColorError(f"Project color {color!r} is not a '#RRGGBB' hex string")
    return trimmed


@pure
def _validated_glyph(glyph: int) -> int:
    """Check that a glyph index addresses a real entry in the glyph table."""
    if not 0 <= glyph < _GLYPH_COUNT:
        raise ProjectGlyphError(f"Project glyph {glyph} is outside the range 0..{_GLYPH_COUNT - 1}")
    return glyph


@pure
def _project_entry(name: str, color: str, glyph: int) -> dict[str, Any]:
    """Build one fully validated registry entry from client-supplied metadata."""
    return {
        "name": _validated_name(name),
        "color": _validated_color(color),
        "glyph": _validated_glyph(glyph),
    }


def _meta_path(layout_dir: Path) -> Path:
    return layout_dir / _META_FILENAME


def _projects_dir(layout_dir: Path) -> Path:
    return layout_dir / _PROJECTS_SUBDIR


def project_content_path(layout_dir: Path, project_id: str) -> Path:
    """On-disk path of one project's content file."""
    return _projects_dir(layout_dir) / f"{project_id}.json"


def _default_meta() -> dict[str, Any]:
    return {
        "project_by_id": {
            EVERYTHING_PROJECT_ID: _project_entry(
                EVERYTHING_PROJECT_NAME,
                _EVERYTHING_COLOR,
                _EVERYTHING_GLYPH,
            )
        },
        "last_active_id": EVERYTHING_PROJECT_ID,
    }


def _write_meta_unlocked(layout_dir: Path, meta: dict[str, Any]) -> None:
    layout_dir.mkdir(parents=True, exist_ok=True)
    _meta_path(layout_dir).write_text(json.dumps(meta, indent=2))


def _restore_everything_unlocked(layout_dir: Path, meta: dict[str, Any]) -> dict[str, Any]:
    """Put the ``everything`` project back if a hand-edited registry lost it.

    Everything is undeletable through this module, so its absence means the
    file was edited from outside. It is reinserted first so it stays the head
    of the list clients render.
    """
    if EVERYTHING_PROJECT_ID in meta["project_by_id"]:
        return meta
    _loguru_logger.warning("Restoring the missing '{}' project to {}", EVERYTHING_PROJECT_ID, _meta_path(layout_dir))
    everything_entry = _project_entry(EVERYTHING_PROJECT_NAME, _EVERYTHING_COLOR, _EVERYTHING_GLYPH)
    meta["project_by_id"] = {EVERYTHING_PROJECT_ID: everything_entry, **meta["project_by_id"]}
    _write_meta_unlocked(layout_dir, meta)
    return meta


def _read_meta_unlocked(layout_dir: Path) -> dict[str, Any]:
    """Read the registry, seeding the ``everything`` project on first use.

    A corrupt meta file is treated as first use (logged at warning) rather
    than crashing every project endpoint: the registry is derivable state and
    the content files themselves are untouched.
    """
    meta_path = _meta_path(layout_dir)
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            _loguru_logger.opt(exception=e).warning("Failed to read {}; reinitializing defaults", meta_path)
            meta = None
        if isinstance(meta, dict) and isinstance(meta.get("project_by_id"), dict):
            return _restore_everything_unlocked(layout_dir, meta)
    meta = _default_meta()
    _write_meta_unlocked(layout_dir, meta)
    return meta


def _project_info(layout_dir: Path, project_id: str, entry: dict[str, Any]) -> ProjectInfo:
    """Present one registry entry, tolerating fields a hand-edit left off."""
    glyph = entry.get("glyph")
    return ProjectInfo(
        project_id=project_id,
        name=str(entry.get("name", project_id)),
        color=str(entry.get("color", _EVERYTHING_COLOR)),
        glyph=glyph if isinstance(glyph, int) else _EVERYTHING_GLYPH,
        has_content=project_content_path(layout_dir, project_id).exists(),
    )


def list_projects(layout_dir: Path) -> list[ProjectInfo]:
    """Every registered project, in registry order, with content-presence flags."""
    with _projects_lock:
        meta = _read_meta_unlocked(layout_dir)
        return [_project_info(layout_dir, project_id, entry) for project_id, entry in meta["project_by_id"].items()]


def get_last_active_id(layout_dir: Path) -> str:
    with _projects_lock:
        meta = _read_meta_unlocked(layout_dir)
        last_active = meta.get("last_active_id")
        if isinstance(last_active, str) and last_active in meta["project_by_id"]:
            return last_active
        return EVERYTHING_PROJECT_ID


def set_last_active_id(layout_dir: Path, project_id: str) -> None:
    """Record ``project_id`` as the most recently used project; unknown ids are ignored."""
    with _projects_lock:
        meta = _read_meta_unlocked(layout_dir)
        if project_id not in meta["project_by_id"]:
            _loguru_logger.warning("Ignored last-active update for unknown project id {!r}", project_id)
            return
        if meta.get("last_active_id") != project_id:
            meta["last_active_id"] = project_id
            _write_meta_unlocked(layout_dir, meta)


def read_project_content(layout_dir: Path, project_id: str) -> dict[str, Any] | None:
    """The saved content of one project, or None when the project is still empty.

    Raises ProjectNotFoundError for an id that is not registered at all.
    A corrupt content file is reported as empty (logged) so the frontend can
    fall back to the fresh-workspace state instead of erroring forever.
    """
    with _projects_lock:
        meta = _read_meta_unlocked(layout_dir)
        if project_id not in meta["project_by_id"]:
            raise ProjectNotFoundError(project_id)
        content_path = project_content_path(layout_dir, project_id)
        if not content_path.exists():
            return None
        try:
            content = json.loads(content_path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            _loguru_logger.opt(exception=e).warning("Failed to read project content at {}", content_path)
            return None
        return content if isinstance(content, dict) else None


def write_project_content(layout_dir: Path, project_id: str, content: dict[str, Any]) -> None:
    """Persist ``content`` for an already-registered project.

    Raises ProjectNotFoundError when the id is not registered -- autosaves
    against a just-deleted project must fail rather than resurrect it.

    Writing deliberately leaves the last-active pointer alone: a new tab is
    mirrored into Everything's content as well as the active project's, and
    that mirror write must not steal the pointer. Clients move it explicitly
    with ``set_last_active_id``.
    """
    with _projects_lock:
        meta = _read_meta_unlocked(layout_dir)
        if project_id not in meta["project_by_id"]:
            raise ProjectNotFoundError(project_id)
        content_path = project_content_path(layout_dir, project_id)
        content_path.parent.mkdir(parents=True, exist_ok=True)
        content_path.write_text(json.dumps(content, separators=(",", ":")))


@pure
def content_contains_panel(content: dict[str, Any], panel_id: str) -> bool:
    """Whether ``panel_id`` still has a panel entry in this saved content."""
    dockview = content.get("dockview")
    if not isinstance(dockview, dict):
        return False
    panels = dockview.get("panels")
    return isinstance(panels, dict) and panel_id in panels


@pure
def _pruned_grid_node(node: dict[str, Any], panel_id: str) -> dict[str, Any] | None:
    """Drop ``panel_id`` from one grid node, or None when the node empties out.

    A leaf that loses its last view, and a branch that loses all its children,
    both collapse away rather than lingering as an empty split -- dockview
    renders a zero-view group as a blank pane with no tabs.
    """
    node_type = node.get("type")
    if node_type == "leaf":
        data = node.get("data")
        if not isinstance(data, dict):
            return node
        views = [view for view in data.get("views", []) if view != panel_id]
        if not views:
            return None
        pruned_data = {**data, "views": views}
        if pruned_data.get("activeView") == panel_id:
            pruned_data["activeView"] = views[0]
        return {**node, "data": pruned_data}
    if node_type == "branch":
        children = node.get("data")
        if not isinstance(children, list):
            return node
        pruned_children = [
            pruned for pruned in (_pruned_grid_node(child, panel_id) for child in children) if pruned is not None
        ]
        if not pruned_children:
            return None
        return {**node, "data": pruned_children}
    return node


@pure
def strip_panel_from_content(content: dict[str, Any], panel_id: str) -> dict[str, Any] | None:
    """Remove one panel from saved content, or None when nothing is left.

    Destroying a tab has to reach the projects that are not currently mounted,
    so the removal is done against the stored JSON rather than through a live
    dockview: the panel is dropped from ``panels``, from our ``panelParams``
    sidecar, and from whichever group holds it, collapsing groups that empty
    out. Returning None lets the caller delete the content file outright so the
    project falls back to the fresh-workspace state instead of restoring an
    empty grid.
    """
    dockview = content.get("dockview")
    if not isinstance(dockview, dict):
        return content
    panels = dockview.get("panels")
    pruned_panels = (
        {key: value for key, value in panels.items() if key != panel_id} if isinstance(panels, dict) else panels
    )
    if isinstance(pruned_panels, dict) and not pruned_panels:
        return None
    pruned_dockview: dict[str, Any] = {**dockview, "panels": pruned_panels}
    grid = dockview.get("grid")
    if isinstance(grid, dict):
        root = grid.get("root")
        pruned_root = _pruned_grid_node(root, panel_id) if isinstance(root, dict) else root
        if pruned_root is None:
            return None
        pruned_dockview["grid"] = {**grid, "root": pruned_root}
    pruned_content: dict[str, Any] = {**content, "dockview": pruned_dockview}
    panel_params = content.get("panelParams")
    if isinstance(panel_params, dict):
        pruned_content["panelParams"] = {key: value for key, value in panel_params.items() if key != panel_id}
    return pruned_content


def remove_panel_from_all_projects(layout_dir: Path, panel_id: str) -> list[str]:
    """Drop ``panel_id`` from every project that holds it, returning those ids.

    Destroy is a cross-project operation: the underlying agent, terminal, or
    browser is gone, so leaving its panel in some other project's saved content
    would restore a tab whose identity can no longer be resolved. Projects that
    do not hold the panel are left untouched, and a project reduced to nothing
    has its content file removed so it reopens as a fresh workspace.
    """
    changed_project_ids: list[str] = []
    with _projects_lock:
        meta = _read_meta_unlocked(layout_dir)
        for project_id in meta["project_by_id"]:
            content_path = project_content_path(layout_dir, project_id)
            if not content_path.exists():
                continue
            try:
                content = json.loads(content_path.read_text())
            except (json.JSONDecodeError, OSError) as e:
                _loguru_logger.opt(exception=e).warning("Skipped unreadable project content at {}", content_path)
                continue
            if not isinstance(content, dict) or not content_contains_panel(content, panel_id):
                continue
            stripped = strip_panel_from_content(content, panel_id)
            if stripped is None:
                content_path.unlink(missing_ok=True)
            else:
                content_path.write_text(json.dumps(stripped, separators=(",", ":")))
            changed_project_ids.append(project_id)
    return changed_project_ids


def create_project(layout_dir: Path, name: str, color: str, glyph: int) -> ProjectInfo:
    """Register a new empty project and make it the last-active one.

    The id is the slugified name, so two projects whose names shorten to the
    same slug cannot silently share one content file: the second raises
    ProjectConflictError. A create is always followed by a switch in the UI,
    hence moving the last-active pointer here.
    """
    project_id = slugify_project_name(name)
    entry = _project_entry(name, color, glyph)
    with _projects_lock:
        meta = _read_meta_unlocked(layout_dir)
        existing_entry = meta["project_by_id"].get(project_id)
        if existing_entry is not None:
            raise ProjectConflictError(
                f"Project name {name!r} conflicts with existing project "
                f"{existing_entry.get('name')!r} (both shorten to '{project_id}')"
            )
        meta["project_by_id"][project_id] = entry
        meta["last_active_id"] = project_id
        _write_meta_unlocked(layout_dir, meta)
        return _project_info(layout_dir, project_id, entry)


def update_project(layout_dir: Path, project_id: str, name: str, color: str, glyph: int) -> ProjectInfo:
    """Replace one project's display metadata, keeping its id and content.

    Renaming never re-slugifies the id: the id keys the content file, and tabs
    are "in" a project by living in that file. Raises ProjectNotFoundError for
    an unknown id.
    """
    entry = _project_entry(name, color, glyph)
    with _projects_lock:
        meta = _read_meta_unlocked(layout_dir)
        if project_id not in meta["project_by_id"]:
            raise ProjectNotFoundError(project_id)
        meta["project_by_id"][project_id] = entry
        _write_meta_unlocked(layout_dir, meta)
        return _project_info(layout_dir, project_id, entry)


def delete_project(layout_dir: Path, project_id: str) -> str:
    """Delete a project and return the fallback id clients should switch to.

    The fallback is ``everything`` whenever it is present, and otherwise the
    first remaining project in registry order. Raises ProjectNotFoundError for
    an unknown id and EverythingProjectDeletionError for ``everything``, which
    is the unfiltered view and therefore permanent.
    """
    if project_id == EVERYTHING_PROJECT_ID:
        raise EverythingProjectDeletionError(f"The '{EVERYTHING_PROJECT_ID}' project cannot be deleted")
    with _projects_lock:
        meta = _read_meta_unlocked(layout_dir)
        if project_id not in meta["project_by_id"]:
            raise ProjectNotFoundError(project_id)
        del meta["project_by_id"][project_id]
        remaining_ids = meta["project_by_id"]
        fallback_id = EVERYTHING_PROJECT_ID if EVERYTHING_PROJECT_ID in remaining_ids else next(iter(remaining_ids))
        if meta.get("last_active_id") == project_id:
            meta["last_active_id"] = fallback_id
        _write_meta_unlocked(layout_dir, meta)
        project_content_path(layout_dir, project_id).unlink(missing_ok=True)
        return fallback_id
