"""Project storage for the system interface.

A *project* is a **view** over the machine's objects: a filter saying which of
them it shows, plus its own saved dockview arrangement. A member is a panel ref
(``service:<name>``, ``service:browser?session=<name>``, ``chat:<agent-id>``,
``terminal:<name>``, ``url:<hash>``) the project shows whether or not a tab for
it is open, so a member with no panel is simply *backgrounded*: still running,
just not docked. Closing a tab therefore never changes the member list; only
``add_member`` / ``remove_member`` do.

Nothing owns anything. The same object may appear in any number of projects at
once -- the one app a machine runs can sit in every project that cares about
it -- and removing it from one project hides it there and nowhere else. There
is consequently no "move": adding it somewhere new does not take it away from
where it already is.

"Everything" is the view with no filter, and it is the *home*: every object on
the machine shows up in it, including objects filed in no project at all. It
keeps its own layout like any other view, so it has a content file here, but it
never gets a member list -- its membership is "whatever exists".

Storage mirrors named layouts (see ``workspace_layouts.py``): each project's
content lives in its own file under ``<workspace_layout_dir>/projects/<id>.json``
with a small registry file (``projects_meta.json``) holding the per-project
metadata, its member list, and the last-active id. A project with no content
file yet is simply "empty", which the frontend renders as the New Tab launcher.

A workspace that predates projects keeps its whole arrangement in the
named-layout store; on first read that is folded into one starter project (see
``_migrate_named_layouts_unlocked``).
"""

import hashlib
import json
import re
import threading
import urllib.parse
from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger as _loguru_logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.system_interface.workspace_layouts import DESKTOP_LAYOUT_SLUG
from imbue.system_interface.workspace_layouts import read_layout_content

# The project every workspace starts on: the one a fresh machine seeds, and the
# one a pre-projects machine's arrangement migrates into. Its name follows the
# "Project N" series the switcher's "New project" continues, so the next
# project created is "Project 2".
DEFAULT_PROJECT_NAME: Final[str] = "Project 1"
DEFAULT_PROJECT_ID: Final[str] = "project-1"
DEFAULT_PROJECT_COLOR: Final[str] = "#F0603A"
DEFAULT_PROJECT_GLYPH: Final[int] = 0

# The unfiltered view. It is not a project: it has no registry entry, no member
# list and cannot be renamed or deleted. It does keep its own arrangement like
# any other view, which is the one thing here that treats it specially -- its
# content file is readable and writable even though it is registered nowhere.
EVERYTHING_VIEW_ID: Final[str] = "everything"
EVERYTHING_VIEW_NAME: Final[str] = "Everything"

_PROJECTS_SUBDIR: Final[str] = "projects"
_META_FILENAME: Final[str] = "projects_meta.json"

# ``glyph`` indexes the frontend's squiggle table, which has exactly ten
# entries.
_GLYPH_COUNT: Final[int] = 10

_COLOR_PATTERN: Final[re.Pattern[str]] = re.compile(r"#[0-9a-fA-F]{6}")

# Query parameter that distinguishes individual browsers in the per-workspace
# browser fleet. Each fleet browser is separately addressable, so the suffix
# rides along in its ref -- mirroring ``layout_ops``, which resolves live
# panels to the same refs members are filed under.
_BROWSER_SESSION_QUERY_KEY: Final[str] = "session"

# Serializes every read-modify-write of the meta file + content files across
# the threaded WSGI server, exactly as ``workspace_layouts._layouts_lock`` does
# for named layouts. The migration below reaches into that module's lock while
# holding this one; nothing there ever reaches back, so the nesting is one-way.
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


class LastProjectDeletionError(ValueError):
    """Raised when deleting a project would leave the workspace with none."""

    ...


class ProjectColorError(ValueError):
    """Raised when a project color is not a ``#RRGGBB`` hex string."""

    ...


class ProjectGlyphError(ValueError):
    """Raised when a glyph index falls outside the frontend's glyph table."""

    ...


class ProjectMemberRefError(ValueError):
    """Raised when a member ref is empty once trimmed."""

    ...


class ProjectInfo(FrozenModel):
    """One project as listed to clients."""

    project_id: str = Field(description="Slugified filename-safe identifier")
    name: str = Field(description="Free-form name shown in the UI")
    color: str = Field(description="Accent color as a '#RRGGBB' hex string")
    glyph: int = Field(description="Index into the frontend's squiggle glyph table")
    has_content: bool = Field(description="Whether a saved content file exists yet")
    members: tuple[str, ...] = Field(description="Panel refs this project shows, open or backgrounded")


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
def validated_member_ref(ref: str) -> str:
    """Whitespace-trim a member ref, rejecting one that is empty.

    Refs are opaque here beyond being non-blank: the grammar belongs to the
    frontend and ``layout_ops``, and a store that second-guessed it would
    reject perfectly good members every time a new panel kind appears. Public
    because a ref is the machine's own name for an object rather than a
    projects detail -- ``member_titles`` files names under the same keys and
    borrows this rather than restating what a ref may be.
    """
    trimmed = ref.strip()
    if not trimmed:
        raise ProjectMemberRefError("Member ref is empty")
    return trimmed


@pure
def _project_entry(name: str, color: str, glyph: int, members: list[str]) -> dict[str, Any]:
    """Build one registry entry from validated display metadata plus its members."""
    return {
        "name": _validated_name(name),
        "color": _validated_color(color),
        "glyph": _validated_glyph(glyph),
        "members": list(members),
    }


@pure
def _entry_members(entry: dict[str, Any]) -> list[str]:
    """The member refs of one registry entry, tolerating a hand-edit that lost them.

    Returns a fresh list, so callers mutate membership by assigning back to
    ``entry["members"]`` rather than by editing what they were handed.
    """
    members = entry.get("members")
    if not isinstance(members, list):
        return []
    return [member for member in members if isinstance(member, str) and member]


def _meta_path(layout_dir: Path) -> Path:
    return layout_dir / _META_FILENAME


def _projects_dir(layout_dir: Path) -> Path:
    return layout_dir / _PROJECTS_SUBDIR


def project_content_path(layout_dir: Path, project_id: str) -> Path:
    """On-disk path of one project's content file."""
    return _projects_dir(layout_dir) / f"{project_id}.json"


@pure
def _browser_session_suffix(url: Any) -> str:
    """Return ``?session=<id>`` for a browser-fleet iframe URL, else ``""``.

    Mirrors the identical parse in ``layout_ops``: the browser viewer is served
    at the browser service's origin with a ``?session=<id>`` query and each id
    is a separately-addressable pane, so two fleet browsers must not collapse
    into one ``service:browser`` member. Tolerates a non-string or unparsable
    ``url`` by returning ``""``.
    """
    if not isinstance(url, str):
        return ""
    session_values = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get(_BROWSER_SESSION_QUERY_KEY, [])
    if not session_values:
        return ""
    return f"?{_BROWSER_SESSION_QUERY_KEY}={session_values[0]}"


@pure
def _panel_member_ref(panel_id: str, params: dict[str, Any]) -> str:
    """The member ref one saved panel's params address it by.

    Follows the grammar ``layout_ops`` resolves live panels to, with one
    deliberate difference: a chat is filed under its stable agent id rather
    than the agent's renameable display name, so membership survives a rename.
    A panel whose params name nothing recognizable falls back to the
    ``url:<hash>`` form ``layout_ops`` gives ad-hoc URL panels.
    """
    chat_agent_id = params.get("chatAgentId")
    terminal_session_name = params.get("terminalSessionName")
    service_name = params.get("serviceName")
    if params.get("panelType") == "chat" and isinstance(chat_agent_id, str) and chat_agent_id:
        return f"chat:{chat_agent_id}"
    if isinstance(terminal_session_name, str) and terminal_session_name:
        return f"terminal:{terminal_session_name}"
    if isinstance(service_name, str) and service_name:
        session_suffix = _browser_session_suffix(params.get("url"))
        return f"service:{service_name}{session_suffix}"
    return f"url:{hashlib.sha256(panel_id.encode('utf-8')).hexdigest()[:8]}"


@pure
def member_refs_from_content(content: dict[str, Any]) -> list[str]:
    """Every member ref the panels of one saved arrangement resolve to.

    Panel order is preserved and duplicates collapse, which is what lets an
    arrangement built before projects existed be filed as a member list
    wholesale.
    """
    dockview = content.get("dockview")
    panels = dockview.get("panels") if isinstance(dockview, dict) else None
    if not isinstance(panels, dict):
        return []
    panel_params = content.get("panelParams")
    params_by_panel_id = panel_params if isinstance(panel_params, dict) else {}
    refs: list[str] = []
    for panel_id in panels:
        params = params_by_panel_id.get(panel_id)
        ref = _panel_member_ref(panel_id, params if isinstance(params, dict) else {})
        if ref not in refs:
            refs.append(ref)
    return refs


def _default_meta() -> dict[str, Any]:
    return {
        "project_by_id": {
            DEFAULT_PROJECT_ID: _project_entry(
                DEFAULT_PROJECT_NAME,
                DEFAULT_PROJECT_COLOR,
                DEFAULT_PROJECT_GLYPH,
                [],
            )
        },
        "last_active_id": DEFAULT_PROJECT_ID,
    }


def _write_meta_unlocked(layout_dir: Path, meta: dict[str, Any]) -> None:
    layout_dir.mkdir(parents=True, exist_ok=True)
    _meta_path(layout_dir).write_text(json.dumps(meta, indent=2))


def _migrate_named_layouts_unlocked(layout_dir: Path) -> dict[str, Any] | None:
    """Fold a pre-projects named-layout store into one starter project.

    A workspace that predates projects keeps its whole arrangement in the
    named-layout store's ``desktop`` layout. Rather than upgrading onto an
    empty desktop, that arrangement becomes the starter project's content and
    each of its panels is filed as a member, so the machine looks the same
    afterwards and everything on it stays reachable from the sidebar. Reading
    through ``workspace_layouts`` rather than straight off disk also catches a
    workspace old enough to still keep one implicit ``layout.json``: that
    module's own legacy migration runs first and hands the result back here.
    Returns the seeded registry, or None when there is nothing to migrate.
    One-shot in the same way as
    ``workspace_layouts._migrate_legacy_layout_unlocked``: it runs only while
    the projects registry is still absent, and steps aside from a starter
    project that already has content of its own.
    """
    starter_path = project_content_path(layout_dir, DEFAULT_PROJECT_ID)
    if starter_path.exists():
        return None
    content = read_layout_content(layout_dir, DESKTOP_LAYOUT_SLUG)
    if content is None:
        return None
    members = member_refs_from_content(content)
    starter_path.parent.mkdir(parents=True, exist_ok=True)
    starter_path.write_text(json.dumps(content, separators=(",", ":")))
    _loguru_logger.info(
        "Migrated the '{}' layout into the '{}' project with {} member(s)",
        DESKTOP_LAYOUT_SLUG,
        DEFAULT_PROJECT_ID,
        len(members),
    )
    return {
        "project_by_id": {
            DEFAULT_PROJECT_ID: _project_entry(
                DEFAULT_PROJECT_NAME,
                DEFAULT_PROJECT_COLOR,
                DEFAULT_PROJECT_GLYPH,
                members,
            )
        },
        "last_active_id": DEFAULT_PROJECT_ID,
    }


def _read_meta_unlocked(layout_dir: Path) -> dict[str, Any]:
    """Read the registry, seeding the starter project on first use.

    A corrupt meta file -- or one a hand-edit left holding no projects at all
    -- is treated as first use (logged at warning) rather than crashing every
    project endpoint: the registry is derivable state and the content files
    themselves are untouched. Reseeding is also what guarantees the rest of
    this module always has at least one project to fall back to.
    """
    meta_path = _meta_path(layout_dir)
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            _loguru_logger.opt(exception=e).warning("Failed to read {}; reinitializing defaults", meta_path)
            meta = None
        if isinstance(meta, dict) and isinstance(meta.get("project_by_id"), dict) and meta["project_by_id"]:
            return meta
    migrated_meta = _migrate_named_layouts_unlocked(layout_dir)
    meta = migrated_meta if migrated_meta is not None else _default_meta()
    _write_meta_unlocked(layout_dir, meta)
    return meta


def _project_info(layout_dir: Path, project_id: str, entry: dict[str, Any]) -> ProjectInfo:
    """Present one registry entry, tolerating fields a hand-edit left off."""
    glyph = entry.get("glyph")
    return ProjectInfo(
        project_id=project_id,
        name=str(entry.get("name", project_id)),
        color=str(entry.get("color", DEFAULT_PROJECT_COLOR)),
        glyph=glyph if isinstance(glyph, int) else DEFAULT_PROJECT_GLYPH,
        has_content=project_content_path(layout_dir, project_id).exists(),
        members=tuple(_entry_members(entry)),
    )


def list_projects(layout_dir: Path) -> list[ProjectInfo]:
    """Every registered project, in registry order, with members and content flags."""
    with _projects_lock:
        meta = _read_meta_unlocked(layout_dir)
        return [_project_info(layout_dir, project_id, entry) for project_id, entry in meta["project_by_id"].items()]


def get_last_active_id(layout_dir: Path) -> str:
    with _projects_lock:
        meta = _read_meta_unlocked(layout_dir)
        last_active = meta.get("last_active_id")
        if isinstance(last_active, str) and (
            last_active == EVERYTHING_VIEW_ID or last_active in meta["project_by_id"]
        ):
            return last_active
        return next(iter(meta["project_by_id"]))


def set_last_active_id(layout_dir: Path, project_id: str) -> None:
    """Record ``project_id`` as the most recently used project; unknown ids are ignored."""
    with _projects_lock:
        meta = _read_meta_unlocked(layout_dir)
        if project_id != EVERYTHING_VIEW_ID and project_id not in meta["project_by_id"]:
            _loguru_logger.warning("Ignored last-active update for unknown project id {!r}", project_id)
            return
        if meta.get("last_active_id") != project_id:
            meta["last_active_id"] = project_id
            _write_meta_unlocked(layout_dir, meta)


def read_project_content(layout_dir: Path, project_id: str) -> dict[str, Any] | None:
    """The saved content of one project, or None when the project is still empty.

    Raises ProjectNotFoundError for an id that is neither registered nor the
    reserved Everything view, whose arrangement is stored even though it has no
    registry entry. A corrupt content file is reported as empty (logged) so the frontend can
    fall back to the fresh-workspace state instead of erroring forever.
    """
    with _projects_lock:
        meta = _read_meta_unlocked(layout_dir)
        if project_id != EVERYTHING_VIEW_ID and project_id not in meta["project_by_id"]:
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

    Raises ProjectNotFoundError when the id is neither registered nor the
    reserved Everything view -- autosaves against a just-deleted project must
    fail rather than resurrect it.

    Writing deliberately leaves both the last-active pointer and the member
    list alone. An autosave records where tabs sit, and opening or closing a
    tab is not a membership change: members move only through the explicit
    calls below.
    """
    with _projects_lock:
        meta = _read_meta_unlocked(layout_dir)
        if project_id != EVERYTHING_VIEW_ID and project_id not in meta["project_by_id"]:
            raise ProjectNotFoundError(project_id)
        content_path = project_content_path(layout_dir, project_id)
        content_path.parent.mkdir(parents=True, exist_ok=True)
        content_path.write_text(json.dumps(content, separators=(",", ":")))


def list_members(layout_dir: Path, project_id: str) -> list[str]:
    """The refs one project owns, in the order they were added.

    Raises ProjectNotFoundError for an unknown id.
    """
    with _projects_lock:
        meta = _read_meta_unlocked(layout_dir)
        entry = meta["project_by_id"].get(project_id)
        if entry is None:
            raise ProjectNotFoundError(project_id)
        return _entry_members(entry)


def all_members(layout_dir: Path) -> dict[str, list[str]]:
    """Every filed ref on the machine mapped to the projects showing it.

    A ref appears under every project whose filter includes it, so the values
    are lists rather than a single owner. Refs in no project at all are absent
    here entirely -- they still exist on the machine, and Everything shows them
    by enumerating the machine rather than this registry.
    """
    with _projects_lock:
        meta = _read_meta_unlocked(layout_dir)
        projects_by_ref: dict[str, list[str]] = {}
        for project_id, entry in meta["project_by_id"].items():
            for ref in _entry_members(entry):
                projects_by_ref.setdefault(ref, []).append(project_id)
        return projects_by_ref


def projects_showing(layout_dir: Path, ref: str) -> list[str]:
    """Every project whose member list includes ``ref``, in registry order.

    An empty list is a real answer, not a miss: an object filed in no project
    still exists and still shows up in Everything.
    """
    member_ref = validated_member_ref(ref)
    with _projects_lock:
        meta = _read_meta_unlocked(layout_dir)
        return [
            project_id for project_id, entry in meta["project_by_id"].items() if member_ref in _entry_members(entry)
        ]


def add_member(layout_dir: Path, project_id: str, ref: str) -> None:
    """Add ``ref`` to ``project_id``'s member list.

    Idempotent, and deliberately unconcerned with what else holds the ref: a
    project is a *view* over the machine's objects, so the same object showing
    in several projects at once is ordinary rather than a conflict. The one app
    a machine runs can sit in every project that cares about it.
    Raises ProjectNotFoundError for an unknown id.
    """
    member_ref = validated_member_ref(ref)
    with _projects_lock:
        meta = _read_meta_unlocked(layout_dir)
        entry = meta["project_by_id"].get(project_id)
        if entry is None:
            raise ProjectNotFoundError(project_id)
        members = _entry_members(entry)
        if member_ref in members:
            return
        entry["members"] = [*members, member_ref]
        _write_meta_unlocked(layout_dir, meta)


def remove_member(layout_dir: Path, project_id: str, ref: str) -> None:
    """Drop ``ref`` from ``project_id``'s member list.

    This hides the object in that one view and nothing more. It keeps running,
    it stays in every other project holding it, and it stays in Everything --
    so this is "remove from project", never "stop" and never "delete". A ref
    the project does not hold is a no-op; an unknown project id raises
    ProjectNotFoundError.
    """
    member_ref = validated_member_ref(ref)
    with _projects_lock:
        meta = _read_meta_unlocked(layout_dir)
        entry = meta["project_by_id"].get(project_id)
        if entry is None:
            raise ProjectNotFoundError(project_id)
        members = _entry_members(entry)
        if member_ref not in members:
            return
        entry["members"] = [member for member in members if member != member_ref]
        _write_meta_unlocked(layout_dir, meta)


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
    empty grid. Membership is a separate list and is dropped alongside this by
    ``remove_panel_from_all_projects``.
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


def _strip_panel_file_unlocked(layout_dir: Path, project_id: str, panel_id: str) -> bool:
    """Rewrite one project's content without ``panel_id``, reporting whether it held it.

    A project reduced to no panels at all has its content file removed so it
    reopens on the launcher rather than restoring an empty grid.
    """
    content_path = project_content_path(layout_dir, project_id)
    if not content_path.exists():
        return False
    try:
        content = json.loads(content_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        _loguru_logger.opt(exception=e).warning("Skipped unreadable project content at {}", content_path)
        return False
    if not isinstance(content, dict) or not content_contains_panel(content, panel_id):
        return False
    stripped = strip_panel_from_content(content, panel_id)
    if stripped is None:
        content_path.unlink(missing_ok=True)
    else:
        content_path.write_text(json.dumps(stripped, separators=(",", ":")))
    return True


def remove_panel_from_all_projects(layout_dir: Path, panel_id: str, ref: str | None = None) -> list[str]:
    """Drop a destroyed object from every project, returning the ids that changed.

    Destroy is the one cross-project operation: the underlying agent, terminal,
    or browser is gone for good, so it has to leave the projects that are not
    currently mounted as well -- as a panel in their saved content, which would
    otherwise restore a tab whose identity can no longer be resolved, and as a
    member, which would otherwise keep listing it as backgrounded forever.
    ``ref`` is the member the panel stood for; a caller that knows only the
    panel passes None and drops the panel alone. Projects holding neither are
    left untouched.
    """
    changed_project_ids: list[str] = []
    with _projects_lock:
        meta = _read_meta_unlocked(layout_dir)
        is_meta_dirty = False
        for project_id, entry in meta["project_by_id"].items():
            members = _entry_members(entry)
            is_member_dropped = ref is not None and ref in members
            if is_member_dropped:
                entry["members"] = [member for member in members if member != ref]
                is_meta_dirty = True
            is_panel_dropped = _strip_panel_file_unlocked(layout_dir, project_id, panel_id)
            if is_member_dropped or is_panel_dropped:
                changed_project_ids.append(project_id)
        if is_meta_dirty:
            _write_meta_unlocked(layout_dir, meta)
    return changed_project_ids


def create_project(layout_dir: Path, name: str, color: str, glyph: int) -> ProjectInfo:
    """Register a new empty project and make it the last-active one.

    The id is the slugified name, so two projects whose names shorten to the
    same slug cannot silently share one content file: the second raises
    ProjectConflictError. A create is always followed by a switch in the UI,
    hence moving the last-active pointer here.
    """
    project_id = slugify_project_name(name)
    entry = _project_entry(name, color, glyph, [])
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
    """Replace one project's display metadata, keeping its id, content and members.

    Renaming never re-slugifies the id: the id keys the content file and the
    registry entry that owns the members, so a rename is purely cosmetic.
    Raises ProjectNotFoundError for an unknown id.
    """
    with _projects_lock:
        meta = _read_meta_unlocked(layout_dir)
        existing_entry = meta["project_by_id"].get(project_id)
        if existing_entry is None:
            raise ProjectNotFoundError(project_id)
        entry = _project_entry(name, color, glyph, _entry_members(existing_entry))
        meta["project_by_id"][project_id] = entry
        _write_meta_unlocked(layout_dir, meta)
        return _project_info(layout_dir, project_id, entry)


def delete_project(layout_dir: Path, project_id: str) -> str:
    """Delete a project and return the fallback id clients should switch to.

    The fallback is the first remaining project in registry order. The member
    list goes with the project, which changes nothing about the objects it
    showed: they keep running, and they stay in every other project showing them
    and in Everything. Stopping any of them is the caller's job, and is what the
    delete confirmation enumerates. Raises ProjectNotFoundError for an unknown id
    and LastProjectDeletionError when this is the only project left, since the
    fallback is always another project.
    """
    with _projects_lock:
        meta = _read_meta_unlocked(layout_dir)
        if project_id not in meta["project_by_id"]:
            raise ProjectNotFoundError(project_id)
        if len(meta["project_by_id"]) <= 1:
            raise LastProjectDeletionError("Cannot delete the last remaining project")
        del meta["project_by_id"][project_id]
        fallback_id = next(iter(meta["project_by_id"]))
        if meta.get("last_active_id") == project_id:
            meta["last_active_id"] = fallback_id
        _write_meta_unlocked(layout_dir, meta)
        project_content_path(layout_dir, project_id).unlink(missing_ok=True)
        return fallback_id
