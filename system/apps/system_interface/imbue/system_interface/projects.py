"""Project storage for the system interface.

A *project* is a **view** over the machine's objects: a filter saying which of
them it shows, plus its own saved dockview arrangement. A member is a panel ref
(``service:<name>``, ``service:<name>?instance=<name>-<N>``,
``service:browser?session=<name>``, ``chat:<agent-id>``, ``terminal:<name>``,
``url:<hash>``) the project shows whether or not a tab for it is open, so a
member with no panel is simply *backgrounded*: still running, just not docked.
Closing a tab therefore never changes the member list; only ``add_member`` /
``remove_member`` do.

Nothing owns anything. The same object may appear in any number of projects at
once -- the one app a machine runs can sit in every project that cares about
it -- and removing it from one project hides it there and nowhere else. There
is consequently no "move": adding it somewhere new does not take it away from
where it already is.

"Everything" is the view with no filter, and it is the *home*: every object on
the machine shows up in it, including objects filed in no project at all. It
keeps its own layout like any other view, so it has a content file here, but it
never gets a member list -- its membership is "whatever exists".

Each project's content lives in its own file under
``<workspace_layout_dir>/projects/<id>.json`` (``<id>.mobile.json`` for the
mobile arrangement) with a small registry file (``projects_meta.json``) holding
the per-project metadata, its member list, and the last-active id. A project
with no content file yet is simply "empty", which the frontend renders as the
New Tab launcher.

A workspace that predates projects kept its arrangement in the retired
named-layout store (or, older still, one implicit ``layout.json``); on first
read that is folded into one starter project (see
``_migrate_named_layouts_unlocked``).
"""

import hashlib
import json
import os
import re
import threading
import urllib.parse
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from typing import Final
from typing import TypedDict
from uuid import uuid4

from loguru import logger as _loguru_logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.system_interface.agent_discovery import get_host_dir

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

# Each view keeps one arrangement per device kind: ``projects/<id>.json`` for
# desktop and ``projects/<id>.mobile.json`` for mobile. The kinds mirror the
# frontend's UA-derived ``DeviceKind`` (ClientIdentity.ts), which each client
# reports and routes its own loads and autosaves by. Membership is shared --
# the devices differ only in where tabs sit, never in what the view holds.
DEVICE_KINDS: Final[tuple[str, ...]] = ("desktop", "mobile")
DEFAULT_DEVICE: Final[str] = "desktop"

# ``glyph`` indexes the frontend's squiggle table, which has exactly ten
# entries.
_GLYPH_COUNT: Final[int] = 10

_COLOR_PATTERN: Final[re.Pattern[str]] = re.compile(r"#[0-9a-fA-F]{6}")

# Query parameter that distinguishes individual browsers in the per-workspace
# browser fleet. Each fleet browser is separately addressable, so the suffix
# rides along in its ref -- mirroring ``layout_ops``, which resolves live
# panels to the same refs members are filed under.
_BROWSER_SESSION_QUERY_KEY: Final[str] = "session"

# Query parameter that distinguishes a plain app's instances, riding the same
# ``service:<name>?<query>`` grammar the browser fleet uses. The value is the
# instance's full canonical name (``files-2``), carried on the pane's params
# (``serviceInstanceId``) rather than in its URL -- the URL is the service
# origin plus wherever the instance is looking. See ``app_instances``.
_SERVICE_INSTANCE_QUERY_KEY: Final[str] = "instance"

# Serializes every read-modify-write of the meta file + content files across
# the threaded WSGI server. Mirrors the module-level ``_terminal_allocate_lock``
# convention in ``server.py``.
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


class ProjectColorError(ValueError):
    """Raised when a project color is not a ``#RRGGBB`` hex string."""

    ...


class ProjectGlyphError(ValueError):
    """Raised when a glyph index falls outside the frontend's glyph table."""

    ...


class ProjectMemberRefError(ValueError):
    """Raised when a member ref is empty once trimmed."""

    ...


class ProjectShortcutError(ValueError):
    """Raised when a shortcut name is not one of the rail's built-in rows."""

    ...


class ProjectDeviceError(ValueError):
    """Raised when a device kind is not one of DEVICE_KINDS."""

    ...


def validate_device(device: str) -> str:
    """Return ``device`` unchanged, raising ProjectDeviceError if unknown."""
    if device not in DEVICE_KINDS:
        known = ", ".join(DEVICE_KINDS)
        raise ProjectDeviceError(f"Unknown device kind {device!r} (known kinds: {known})")
    return device


# The rail's four built-in shortcut rows. Unlike an app, none of these is an
# object with a member ref -- "chat" is a create, and the terminal and browser
# services are fleets reached by making a session rather than by opening the
# service -- so which of them a project shows cannot be membership and is
# recorded here instead.
SHORTCUT_NAMES: Final[frozenset[str]] = frozenset({"chat", "files", "browser", "terminal"})

# A pinned app's shortcut is keyed ``app:<service-name>`` in the overrides map.
# App pinning itself stays membership (the ``service:<name>`` member ref IS the
# pin), so an ``app:`` override carries only ``mode``.
APP_SHORTCUT_PREFIX: Final[str] = "app:"

# A shortcut's two modes: focus goes to the most recently used member of the
# kind in the active view (creating only when the view shows none), new always
# creates. Stored lowercase because these are wire/registry values.
SHORTCUT_MODES: Final[tuple[str, ...]] = ("focus", "new")

# Per-shortcut mode defaults, code-side so changing one applies to every
# project that never stored an override. Chat defaults to new ("New Chat") --
# multi-chat discoverability is the point -- and everything else to focus.
_DEFAULT_NEW_MODE_SHORTCUTS: Final[frozenset[str]] = frozenset({"chat"})


@pure
def default_shortcut_mode(shortcut_id: str) -> str:
    return "new" if shortcut_id in _DEFAULT_NEW_MODE_SHORTCUTS else "focus"


@pure
def validated_shortcut_id(shortcut_id: str) -> str:
    """A built-in shortcut name or ``app:<service-name>``, else ProjectShortcutError."""
    if shortcut_id in SHORTCUT_NAMES:
        return shortcut_id
    if shortcut_id.startswith(APP_SHORTCUT_PREFIX) and len(shortcut_id) > len(APP_SHORTCUT_PREFIX):
        return shortcut_id
    known = ", ".join(sorted(SHORTCUT_NAMES))
    raise ProjectShortcutError(f"Unknown shortcut {shortcut_id!r} (known shortcuts: {known}, or 'app:<service-name>')")


class ShortcutOverride(FrozenModel):
    """One shortcut's stored deviations from the code-side defaults."""

    is_pinned: bool | None = Field(
        default=None,
        description=(
            "False when a built-in row is unpinned into the All apps menu; None means "
            "the default (pinned). Never stored for an app: key -- app pinning IS membership."
        ),
    )
    mode: str | None = Field(
        default=None,
        description="'focus' or 'new' when the project has flipped this shortcut's mode; None means the default",
    )


class ProjectInfo(FrozenModel):
    """One project as listed to clients."""

    project_id: str = Field(description="Slugified filename-safe identifier")
    name: str = Field(description="Free-form name shown in the UI")
    color: str = Field(description="Accent color as a '#RRGGBB' hex string")
    glyph: int = Field(description="Index into the frontend's squiggle glyph table")
    has_content: bool = Field(description="Whether a saved content file exists yet")
    members: tuple[str, ...] = Field(description="Panel refs this project shows, open or backgrounded")
    # Sparse on purpose: an absent key means all defaults, so no migration is
    # ever needed and the registry stays hand-edit tolerant. Keys are a
    # built-in name or ``app:<service-name>`` (see ``validated_shortcut_id``).
    shortcut_overrides: dict[str, ShortcutOverride] = Field(
        default_factory=dict,
        description="Per-shortcut deviations from the defaults (pinning for built-ins, mode for any)",
    )


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
class _ProjectEntry(TypedDict):
    """One registry entry as this module writes it.

    Typing covers only the write side: entries read back from disk stay
    tolerant ``dict[str, Any]``, since the registry is hand-editable and every
    reader defends against missing or mistyped fields.
    """

    name: str
    color: str
    glyph: int
    members: list[str]
    shortcut_overrides: dict[str, dict[str, Any]]


def _project_entry(
    name: str,
    color: str,
    glyph: int,
    members: list[str],
    shortcut_overrides: Mapping[str, Mapping[str, Any]] | None = None,
) -> _ProjectEntry:
    """Build one registry entry from validated display metadata plus its members.

    ``shortcut_overrides`` defaults to none -- the same state an absent key
    means on read -- so only ``update_project``, which must carry an existing
    project's shortcut state through a rebuild, ever passes it.
    """
    return {
        "name": _validated_name(name),
        "color": _validated_color(color),
        "glyph": _validated_glyph(glyph),
        "members": list(members),
        "shortcut_overrides": {key: dict(value) for key, value in (shortcut_overrides or {}).items()},
    }


@pure
def _sanitized_override(shortcut_id: str, raw_override: Mapping[str, Any]) -> dict[str, Any]:
    """One override's usable fields, tolerating a hand-edit.

    Only the fields that deviate from the defaults survive: ``is_pinned`` when
    False (True is the default, and app pinning is membership so an ``app:``
    key never carries it -- the spec says such a field is ignored), ``mode``
    when it is a known mode other than the shortcut's default. Anything else
    is dropped rather than trusted, so junk cannot ride every list response.
    """
    sanitized: dict[str, Any] = {}
    is_pinned = raw_override.get("is_pinned")
    if is_pinned is False and shortcut_id in SHORTCUT_NAMES:
        sanitized["is_pinned"] = False
    mode = raw_override.get("mode")
    if isinstance(mode, str) and mode in SHORTCUT_MODES and mode != default_shortcut_mode(shortcut_id):
        sanitized["mode"] = mode
    return sanitized


@pure
def _entry_shortcut_overrides(entry: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """One entry's shortcut overrides, tolerating hand-edits and legacy shapes.

    Keys that are not shortcut ids, values that are not objects, and fields
    that just restate a default are all dropped on read. When the entry has no
    ``shortcut_overrides`` at all, the legacy ``unpinned_shortcuts`` list is
    read as ``{<name>: {"is_pinned": False}}`` -- the shape the projects
    follow-up stored pin state in -- so nothing a user put away pops back.

    CLEANUP: drop the legacy ``unpinned_shortcuts`` branch once no supported
    workspace's registry predates the shortcut_overrides map (the first write
    of any override rewrites the entry to the new shape).
    """
    raw_overrides = entry.get("shortcut_overrides")
    if isinstance(raw_overrides, dict):
        sanitized_overrides: dict[str, dict[str, Any]] = {}
        for shortcut_id, raw_override in raw_overrides.items():
            if not isinstance(shortcut_id, str) or not isinstance(raw_override, dict):
                continue
            try:
                validated_shortcut_id(shortcut_id)
            except ProjectShortcutError:
                continue
            sanitized = _sanitized_override(shortcut_id, raw_override)
            if sanitized:
                sanitized_overrides[shortcut_id] = sanitized
        return sanitized_overrides
    legacy_unpinned = entry.get("unpinned_shortcuts")
    if not isinstance(legacy_unpinned, list):
        return {}
    return {name: {"is_pinned": False} for name in legacy_unpinned if isinstance(name, str) and name in SHORTCUT_NAMES}


@pure
def _entry_members(entry: Mapping[str, Any]) -> list[str]:
    """The member refs of one registry entry, tolerating a hand-edit that lost them.

    Returns a fresh list, so callers mutate membership by assigning back to
    ``entry["members"]`` rather than by editing what they were handed.
    """
    members = entry.get("members")
    if not isinstance(members, list):
        return []
    return [member for member in members if isinstance(member, str) and member]


def primary_agent_layout_dir(host_dir: Path, agent_id: str) -> Path:
    """The workspace layout directory belonging to the workspace's primary agent.

    The system_interface always serves a single workspace (its own primary
    agent). Shared so every consumer of the layout dir -- and of the event logs
    kept beside it -- resolves the same path.
    """
    return host_dir / "agents" / agent_id / "workspace_layout"


def primary_agent_layout_dir_from_env() -> Path | None:
    """The layout directory of the primary agent this process serves, from ``MNGR_AGENT_ID``.

    Returns None when the env var is missing, which should only happen in dev/test
    setups that don't care about persistence.
    """
    agent_id = os.environ.get("MNGR_AGENT_ID", "")
    if not agent_id:
        return None
    return primary_agent_layout_dir(get_host_dir(), agent_id)


def _meta_path(layout_dir: Path) -> Path:
    return layout_dir / _META_FILENAME


def _projects_dir(layout_dir: Path) -> Path:
    return layout_dir / _PROJECTS_SUBDIR


def project_content_path(layout_dir: Path, project_id: str, device: str = DEFAULT_DEVICE) -> Path:
    """On-disk path of one project's content file for one device kind."""
    validate_device(device)
    suffix = ".json" if device == DEFAULT_DEVICE else f".{device}.json"
    return _projects_dir(layout_dir) / f"{project_id}{suffix}"


@pure
def _browser_session_suffix(url: object) -> str:
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
    service_instance_id = params.get("serviceInstanceId")
    if params.get("panelType") == "chat" and isinstance(chat_agent_id, str) and chat_agent_id:
        return f"chat:{chat_agent_id}"
    if isinstance(terminal_session_name, str) and terminal_session_name:
        return f"terminal:{terminal_session_name}"
    if isinstance(service_name, str) and service_name:
        if isinstance(service_instance_id, str) and service_instance_id:
            return f"service:{service_name}?{_SERVICE_INSTANCE_QUERY_KEY}={service_instance_id}"
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


def _write_json_atomic(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` via a same-directory temp file and rename.

    A plain ``write_text`` truncates before it writes, so a concurrent reader
    (another process inspecting the file, an agent, a test poll) can observe
    an empty or partial file. ``os.replace`` makes the swap atomic on POSIX,
    so readers only ever see the old or the new content in full.
    """
    temp_path = path.with_name(f"{path.name}.tmp-{uuid4().hex}")
    temp_path.write_text(text)
    os.replace(temp_path, path)


def _write_meta_unlocked(layout_dir: Path, meta: dict[str, Any]) -> None:
    layout_dir.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(_meta_path(layout_dir), json.dumps(meta, indent=2))


def _read_retired_store_content(layout_dir: Path, *candidate_paths: Path) -> dict[str, Any] | None:
    """The first readable arrangement among ``candidate_paths``, or None.

    Reads straight off disk: the named-layout store these files belonged to is
    retired, so there is no registry left to consult. Unreadable or non-object
    content is skipped (logged) rather than failing the upgrade.
    """
    for candidate_path in candidate_paths:
        if not candidate_path.exists():
            continue
        try:
            content = json.loads(candidate_path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            _loguru_logger.opt(exception=e).warning("Skipped unreadable legacy layout at {}", candidate_path)
            continue
        if isinstance(content, dict):
            return content
    return None


def _migrate_named_layouts_unlocked(layout_dir: Path) -> dict[str, Any] | None:
    """Fold a pre-projects layout store into one starter project.

    A workspace that predates projects kept its whole arrangement in the
    retired named-layout store's ``layouts/desktop.json`` (or, older still, one
    implicit ``layout.json``). Rather than upgrading onto an empty workspace,
    that arrangement becomes the starter project's desktop content and each of
    its panels is filed as a member, so the machine looks the same afterwards
    and everything on it stays reachable from the sidebar. The old store's
    ``mobile`` layout, when present, folds into the starter project's mobile
    arrangement the same way (members are shared, so it adds none of its own).
    Returns the seeded registry, or None when there is nothing to migrate.
    One-shot: it runs only while the projects registry is still absent, and
    steps aside from a starter project that already has content of its own.
    The legacy files are left in place -- nothing reads them anymore, and the
    registry write is what marks the migration done.
    """
    starter_path = project_content_path(layout_dir, DEFAULT_PROJECT_ID)
    if starter_path.exists():
        return None
    content = _read_retired_store_content(
        layout_dir, layout_dir / "layouts" / "desktop.json", layout_dir / "layout.json"
    )
    if content is None:
        return None
    members = member_refs_from_content(content)
    starter_path.parent.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(starter_path, json.dumps(content, separators=(",", ":")))
    mobile_content = _read_retired_store_content(layout_dir, layout_dir / "layouts" / "mobile.json")
    if mobile_content is not None:
        _write_json_atomic(
            project_content_path(layout_dir, DEFAULT_PROJECT_ID, "mobile"),
            json.dumps(mobile_content, separators=(",", ":")),
        )
    _loguru_logger.info(
        "Migrated the pre-projects layout store into the '{}' project with {} member(s)",
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


# Member refs that name nothing a project can show any more, purged on read:
#
# - ``url:`` members, left behind by the old ad-hoc-page filer. A ``url:<hash>``
#   ref named the panel that once showed the page rather than the page itself,
#   so nothing can open or act on one. (CLEANUP: remove -- with the frontend's
#   remaining "url" member handling in models/Projects.ts -- once no supported
#   workspace's registry predates the projects follow-up that stopped filing
#   ad-hoc pages.)
# - bare ``service:files`` members, left behind by builds whose app opens filed
#   the bare service ref. The file viewer's pin is its built-in rail row, never
#   membership, so such an entry only ever rendered as a phantom "files" tab
#   row. Opens file ``?instance=`` refs now. (CLEANUP: remove once no supported
#   workspace's registry predates app instances.)
_PURGED_MEMBER_REFS: Final[tuple[str, ...]] = ("service:files",)
_PURGED_MEMBER_REF_PREFIXES: Final[tuple[str, ...]] = ("url:",)


def _purge_legacy_members_unlocked(layout_dir: Path, meta: dict[str, Any]) -> None:
    """Drop the member refs nothing can show any more (see the table above).

    Pages and panes still persist in each view's saved arrangement; only the
    dead member entries go. Rewrites the registry only when something was
    actually dropped.
    """
    dropped_count = 0
    for entry in meta["project_by_id"].values():
        if not isinstance(entry, dict):
            continue
        members = _entry_members(entry)
        kept_members = [
            ref
            for ref in members
            if ref not in _PURGED_MEMBER_REFS and not ref.startswith(_PURGED_MEMBER_REF_PREFIXES)
        ]
        if len(kept_members) != len(members):
            dropped_count += len(members) - len(kept_members)
            entry["members"] = kept_members
    if dropped_count:
        _loguru_logger.info("Dropped {} legacy member(s) from the project registry", dropped_count)
        _write_meta_unlocked(layout_dir, meta)


def _read_meta_unlocked(layout_dir: Path) -> dict[str, Any]:
    """Read the registry, seeding the starter project on first use.

    A corrupt meta file is treated as first use (logged at warning) rather than
    crashing every project endpoint: the registry is derivable state and the
    content files themselves are untouched. An empty ``project_by_id`` is *not*
    treated as corrupt -- deleting is a pure view operation with no undeletable
    project any more, so a machine legitimately reaches zero of them, and
    Everything is always there to fall back to.
    """
    meta_path = _meta_path(layout_dir)
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            _loguru_logger.opt(exception=e).warning("Failed to read {}; reinitializing defaults", meta_path)
            meta = None
        if isinstance(meta, dict) and isinstance(meta.get("project_by_id"), dict):
            _purge_legacy_members_unlocked(layout_dir, meta)
            return meta
    migrated_meta = _migrate_named_layouts_unlocked(layout_dir)
    meta = migrated_meta if migrated_meta is not None else _default_meta()
    _write_meta_unlocked(layout_dir, meta)
    return meta


def _project_info(layout_dir: Path, project_id: str, entry: Mapping[str, Any]) -> ProjectInfo:
    """Present one registry entry, tolerating fields a hand-edit left off."""
    glyph = entry.get("glyph")
    return ProjectInfo(
        project_id=project_id,
        name=str(entry.get("name", project_id)),
        color=str(entry.get("color", DEFAULT_PROJECT_COLOR)),
        glyph=glyph if isinstance(glyph, int) else DEFAULT_PROJECT_GLYPH,
        has_content=any(project_content_path(layout_dir, project_id, device).exists() for device in DEVICE_KINDS),
        members=tuple(_entry_members(entry)),
        shortcut_overrides={
            shortcut_id: ShortcutOverride.model_validate(override)
            for shortcut_id, override in _entry_shortcut_overrides(entry).items()
        },
    )


def list_projects(layout_dir: Path) -> list[ProjectInfo]:
    """Every registered project, in registry order, with members and content flags."""
    with _projects_lock:
        meta = _read_meta_unlocked(layout_dir)
        return [_project_info(layout_dir, project_id, entry) for project_id, entry in meta["project_by_id"].items()]


def get_last_active_id(layout_dir: Path) -> str:
    """The view a client should land on, falling back to Everything with zero projects."""
    with _projects_lock:
        meta = _read_meta_unlocked(layout_dir)
        last_active = meta.get("last_active_id")
        if isinstance(last_active, str) and (
            last_active == EVERYTHING_VIEW_ID or last_active in meta["project_by_id"]
        ):
            return last_active
        return next(iter(meta["project_by_id"]), EVERYTHING_VIEW_ID)


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


def read_project_content(layout_dir: Path, project_id: str, device: str = DEFAULT_DEVICE) -> dict[str, Any] | None:
    """The saved content of one project on one device, or None when still empty.

    Raises ProjectNotFoundError for an id that is neither registered nor the
    reserved Everything view, whose arrangement is stored even though it has no
    registry entry. Each device kind reads only its own file -- a view arranged
    on desktop is still "empty" on mobile until a mobile client saves there.
    A corrupt content file is reported as empty (logged) so the frontend can
    fall back to the fresh-workspace state instead of erroring forever.
    """
    with _projects_lock:
        meta = _read_meta_unlocked(layout_dir)
        if project_id != EVERYTHING_VIEW_ID and project_id not in meta["project_by_id"]:
            raise ProjectNotFoundError(project_id)
        content_path = project_content_path(layout_dir, project_id, device)
        if not content_path.exists():
            return None
        try:
            content = json.loads(content_path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            _loguru_logger.opt(exception=e).warning("Failed to read project content at {}", content_path)
            return None
        return content if isinstance(content, dict) else None


def write_project_content(
    layout_dir: Path, project_id: str, content: dict[str, Any], device: str = DEFAULT_DEVICE
) -> None:
    """Persist ``content`` for an already-registered project on one device.

    Raises ProjectNotFoundError when the id is neither registered nor the
    reserved Everything view -- autosaves against a just-deleted project must
    fail rather than resurrect it. A write touches only its own device's file;
    it never seeds the other device, which builds its own arrangement the
    first time a client of that kind saves there.

    Writing deliberately leaves both the last-active pointer and the member
    list alone. An autosave records where tabs sit, and opening or closing a
    tab is not a membership change: members move only through the explicit
    calls below.
    """
    with _projects_lock:
        meta = _read_meta_unlocked(layout_dir)
        if project_id != EVERYTHING_VIEW_ID and project_id not in meta["project_by_id"]:
            raise ProjectNotFoundError(project_id)
        content_path = project_content_path(layout_dir, project_id, device)
        content_path.parent.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(content_path, json.dumps(content, separators=(",", ":")))


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


def set_shortcut_override(
    layout_dir: Path,
    project_id: str,
    shortcut_id: str,
    # None leaves the field as it is; a value sets it (and a value equal to the
    # default clears it back out, keeping the stored map sparse).
    is_pinned: bool | None,
    mode: str | None,
) -> dict[str, dict[str, Any]]:
    """Record one shortcut's deviation from the defaults on ``project_id``.

    Pinning moves where a built-in row is offered (the rail when pinned, the
    All apps menu when not) and mode decides what clicking it does (focus the
    most recent member of the kind, or always create). Both are project-scoped
    on purpose: which starting points a project keeps to hand, and how they
    behave, are properties of that project.

    Idempotent; returns the resulting sparse override map. The first write also
    migrates a legacy ``unpinned_shortcuts`` entry to the new shape (the
    legacy key is folded in by the read and dropped from the entry here). An
    unknown project id raises ProjectNotFoundError; an unknown shortcut id, a
    bad mode, or a pin on an ``app:`` key (whose pinning IS membership) raises
    ProjectShortcutError.
    """
    validated_shortcut_id(shortcut_id)
    if mode is not None and mode not in SHORTCUT_MODES:
        known_modes = ", ".join(SHORTCUT_MODES)
        raise ProjectShortcutError(f"Unknown shortcut mode {mode!r} (known modes: {known_modes})")
    if is_pinned is not None and shortcut_id not in SHORTCUT_NAMES:
        raise ProjectShortcutError(
            f"Pinning is not stored for {shortcut_id!r}: an app's pin IS its project membership"
        )
    with _projects_lock:
        meta = _read_meta_unlocked(layout_dir)
        entry = meta["project_by_id"].get(project_id)
        if entry is None:
            raise ProjectNotFoundError(project_id)
        overrides = _entry_shortcut_overrides(entry)
        override = dict(overrides.get(shortcut_id, {}))
        if is_pinned is not None:
            if is_pinned:
                override.pop("is_pinned", None)
            else:
                override["is_pinned"] = False
        if mode is not None:
            if mode == default_shortcut_mode(shortcut_id):
                override.pop("mode", None)
            else:
                override["mode"] = mode
        if override:
            overrides[shortcut_id] = override
        else:
            overrides.pop(shortcut_id, None)
        if entry.get("shortcut_overrides") == overrides and "unpinned_shortcuts" not in entry:
            return overrides
        entry["shortcut_overrides"] = overrides
        # The first write of any override migrates the entry to the new shape:
        # the legacy list was already folded into ``overrides`` by the read.
        # CLEANUP: remove alongside _entry_shortcut_overrides' legacy branch.
        entry.pop("unpinned_shortcuts", None)
        _write_meta_unlocked(layout_dir, meta)
        return overrides


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


@pure
def _panel_ids_resolving_to_ref(content: dict[str, Any], ref: str) -> list[str]:
    """Every saved panel id whose params resolve to ``ref``.

    A panel id is not a stable identity for every kind: a browser or app pane's
    id is minted per open (``iframe-<owner>-<timestamp>``), so the same object
    can sit under a different id in each view's file. The params always resolve
    to the object's one ref, so a destroy that knows the ref finds them all.
    """
    dockview = content.get("dockview")
    panels = dockview.get("panels") if isinstance(dockview, dict) else None
    if not isinstance(panels, dict):
        return []
    panel_params = content.get("panelParams")
    params_by_panel_id = panel_params if isinstance(panel_params, dict) else {}
    matching_ids: list[str] = []
    for panel_id in panels:
        params = params_by_panel_id.get(panel_id)
        if _panel_member_ref(panel_id, params if isinstance(params, dict) else {}) == ref:
            matching_ids.append(panel_id)
    return matching_ids


def _strip_panel_file_unlocked(layout_dir: Path, project_id: str, panel_id: str | None, ref: str | None) -> bool:
    """Rewrite one project's content without the destroyed object's panels.

    Every device's file is swept -- a destroyed object must not restore as a
    dead tab on mobile just because it was destroyed from desktop. Reports
    whether any device's file held a target.
    """
    # A list, not a generator: ``any`` must not short-circuit past a device.
    return any(
        [
            _strip_panel_device_file_unlocked(project_content_path(layout_dir, project_id, device), panel_id, ref)
            for device in DEVICE_KINDS
        ]
    )


def _strip_panel_device_file_unlocked(content_path: Path, panel_id: str | None, ref: str | None) -> bool:
    """Rewrite one device's content file without the destroyed object's panels.

    Panels are matched two ways: by ``panel_id`` (the deterministic id a chat
    or a named terminal is always filed under, or the destroying client's live
    id), and by ``ref`` against each saved panel's params -- which is what
    catches a browser or app pane saved under a per-open minted id this caller
    never saw. Reports whether the file held any of them. A project reduced to
    no panels at all has its content file removed so it reopens on the launcher
    rather than restoring an empty grid.
    """
    if not content_path.exists():
        return False
    try:
        content = json.loads(content_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        _loguru_logger.opt(exception=e).warning("Skipped unreadable project content at {}", content_path)
        return False
    if not isinstance(content, dict):
        return False
    target_panel_ids: list[str] = []
    if panel_id is not None and content_contains_panel(content, panel_id):
        target_panel_ids.append(panel_id)
    if ref is not None:
        for matched_id in _panel_ids_resolving_to_ref(content, ref):
            if matched_id not in target_panel_ids:
                target_panel_ids.append(matched_id)
    if not target_panel_ids:
        return False
    stripped: dict[str, Any] | None = content
    for target_panel_id in target_panel_ids:
        stripped = strip_panel_from_content(stripped, target_panel_id)
        if stripped is None:
            break
    if stripped is None:
        content_path.unlink(missing_ok=True)
    else:
        _write_json_atomic(content_path, json.dumps(stripped, separators=(",", ":")))
    return True


def remove_panel_from_all_projects(layout_dir: Path, panel_id: str | None, ref: str | None = None) -> list[str]:
    """Drop a destroyed object from every project, returning the ids that changed.

    Destroy is the one cross-project operation: the underlying agent, terminal,
    or browser is gone for good, so it has to leave the projects that are not
    currently mounted as well -- as a panel in their saved content, which would
    otherwise restore a tab whose identity can no longer be resolved, and as a
    member, which would otherwise keep listing it as backgrounded forever.
    ``ref`` is the member the panel stood for; a caller that knows only the
    panel passes None and drops the panel alone, while a caller that knows only
    the ref (a browser or app pane's id is minted per open, so there is no
    deterministic id to name) passes None for ``panel_id`` and the sweep finds
    the panels by resolving each saved panel's params to its ref. Projects
    holding neither are left untouched.

    Everything is swept too: it has no registry entry and no member list, but
    it keeps a saved arrangement like any project, and a destroyed object left
    in that file would restore as a dead tab the next time Everything is
    opened. When its content held the panel, ``EVERYTHING_VIEW_ID`` is among
    the returned ids.
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
            is_panel_dropped = _strip_panel_file_unlocked(layout_dir, project_id, panel_id, ref)
            if is_member_dropped or is_panel_dropped:
                changed_project_ids.append(project_id)
        # The registry loop above never reaches Everything -- it is a view, not
        # a project -- so its content file is swept explicitly. Only the panel
        # strip applies: Everything has no member list to drop ``ref`` from.
        if _strip_panel_file_unlocked(layout_dir, EVERYTHING_VIEW_ID, panel_id, ref):
            changed_project_ids.append(EVERYTHING_VIEW_ID)
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
    """Replace one project's display metadata, keeping its id, content, members and shortcut pins.

    Renaming never re-slugifies the id: the id keys the content file and the
    registry entry that owns the members, so a rename is purely cosmetic.
    Raises ProjectNotFoundError for an unknown id.
    """
    with _projects_lock:
        meta = _read_meta_unlocked(layout_dir)
        existing_entry = meta["project_by_id"].get(project_id)
        if existing_entry is None:
            raise ProjectNotFoundError(project_id)
        entry = _project_entry(
            name, color, glyph, _entry_members(existing_entry), _entry_shortcut_overrides(existing_entry)
        )
        meta["project_by_id"][project_id] = entry
        _write_meta_unlocked(layout_dir, meta)
        return _project_info(layout_dir, project_id, entry)


def delete_project(layout_dir: Path, project_id: str) -> str:
    """Delete a project and return the fallback id clients should switch to.

    A pure view operation: only this project's registry entry and its own
    content files (desktop and mobile) go. The member list goes with them, but
    that changes nothing about the objects it showed -- they keep running, and
    they stay in every other project showing them and in Everything, neither of
    which this function ever touches. The fallback is the first remaining
    project in registry order, or Everything once none are left: a machine may
    end up with zero projects and still work, since Everything has no registry
    entry to delete and is always there. Raises ProjectNotFoundError for an
    unknown id.
    """
    with _projects_lock:
        meta = _read_meta_unlocked(layout_dir)
        if project_id not in meta["project_by_id"]:
            raise ProjectNotFoundError(project_id)
        del meta["project_by_id"][project_id]
        fallback_id = next(iter(meta["project_by_id"]), EVERYTHING_VIEW_ID)
        if meta.get("last_active_id") == project_id:
            meta["last_active_id"] = fallback_id
        _write_meta_unlocked(layout_dir, meta)
        for device in DEVICE_KINDS:
            project_content_path(layout_dir, project_id, device).unlink(missing_ok=True)
        return fallback_id
