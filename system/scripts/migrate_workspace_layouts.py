#!/usr/bin/env python3
"""Carry a pre-arc workspace's projects, arrangements, titles, recency, and file-viewer
locations into the workspace app model's state files, once.

Before the workspace app model (docs/system/blueprint/workspace-app-model/), the system
interface kept its projects and layouts under the state directory of the agent it ran as
(``$MNGR_HOST_DIR/agents/<agent-id>/workspace_layout/``; that agent is the services agent,
which the bootstrap runs as too) and named things by per-kind refs (``chat:<agent-id>``,
``terminal:<name>``, ``service:files?instance=files-2``, ...).
The shell now reads ``data/.state/system_interface/`` and names everything by address
(``app:<name>``, ``app:<name>?instance=<key>``). This script maps the one onto the other:

- ``projects_meta.json`` becomes ``projects.json`` (tab sets and rail shortcuts);
- each ``projects/<id>.json`` and ``<id>.mobile.json`` becomes the view's
  ``layouts/<id>/seed.desktop.json`` and ``seed.mobile.json``, the arrangement a client that
  has never visited the view starts from, with every panel renamed to a fresh tab id and every
  panel that maps to nothing (a launcher, a subagent view, an ad-hoc URL page) pruned;
- every file viewer found gets a record in the files app's store, at the folder it was showing;
- every terminal found gets a record in the terminal app's store, with the title it was given,
  so the terminal app lists it (and the shell keeps its tabs) before its tmux session exists
  again;
- ``migrated.json`` marks the run so it never runs twice.

Subcommands: ``run`` (the default; writes) and ``plan`` (prints what a run would write,
``--json`` for the machine-readable form). Standard-library only, like the other scripts
here, and never destructive: the old directory is left untouched, an output that already
exists is skipped (a projects file that holds projects or cannot be read, any seed;
``--force`` overwrites the projects file and the seeds), the two app stores only ever gain
records, and one unreadable view costs that view and nothing else. A workspace
with no old directory is marked migrated at once, so a fresh workspace is never "unmigrated".

The old directory is the one under the agent the environment names when that agent has one
(the boot-time run, as the services agent); otherwise it is whichever agent of the host has
one, since the update apply runs the script as a chat agent whose own state directory never
held a store. Several such agents are ambiguous: the run says so and writes nothing, not
even the marker, so a later run can be pointed at the right one with ``--source``.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import secrets
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, NamedTuple, Sequence

# Where the shell and the apps keep their state, relative to the repo root every supervised
# program and the bootstrap run from (contracts.md sections 7 and 17).
DEFAULT_STATE_DIR = Path("data/.state/system_interface")
DEFAULT_APPS_DATA_DIR = Path("data/.apps")
DEFAULT_REGISTRY_PATH = Path("data/.state/apps.toml")
ENV_APPS_FILE = "MINDS_APPS_FILE"
ENV_HOST_DIR = "MNGR_HOST_DIR"
ENV_AGENT_ID = "MNGR_AGENT_ID"

MARKER_FILENAME = "migrated.json"
MARKER_VERSION = 1
PROJECTS_FILENAME = "projects.json"
PROJECTS_FILE_VERSION = 1
LAYOUTS_DIRNAME = "layouts"
STORE_FILENAME = "instances.json"
STORE_VERSION = 1

# The old store's directory under an agent's state directory, and its files, as the retired
# ``projects``, ``member_titles``, ``member_last_used``, and ``member_locations`` modules wrote
# them.
AGENTS_DIRNAME = "agents"
LEGACY_LAYOUT_DIRNAME = "workspace_layout"
LEGACY_META_FILENAME = "projects_meta.json"
LEGACY_PROJECTS_SUBDIR = "projects"
LEGACY_TITLES_FILENAME = "member_titles.json"
LEGACY_LAST_USED_FILENAME = "member_last_used.json"
LEGACY_LOCATIONS_FILENAME = "member_locations.json"

EVERYTHING_VIEW_ID = "everything"
DESKTOP = "desktop"
MOBILE = "mobile"
LEGACY_CONTENT_SUFFIX_BY_DEVICE = {DESKTOP: ".json", MOBILE: ".mobile.json"}

# A project's display defaults, as the old registry filled them in for a hand-edited entry.
DEFAULT_PROJECT_COLOR = "#F0603A"
DEFAULT_PROJECT_GLYPH = 0
GLYPH_COUNT = 10

# The rail rows every project had before shortcuts were data (contracts.md section 2's
# ``default_shortcut`` column), in rail order, keyed as the old overrides map keyed them.
BUILT_IN_SHORTCUTS = (
    ("chat", "new", "new"),
    ("terminal", "new", "focus"),
    ("files", "new", "focus"),
    ("browser", "new", "focus"),
)
LEGACY_APP_SHORTCUT_PREFIX = "app:"
SHORTCUT_MODES = ("focus", "new")
OPEN_ACTION_ID = "open"

# The address grammar (contracts.md section 1) and the rules the stores hold their values to.
APP_NAME_PATTERN = re.compile(r"^[a-z0-9_]+(?:-[a-z0-9_]+)*$")
MAX_APP_NAME_LENGTH = 32
INSTANCE_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
TMUX_SESSION_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
VIEW_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
COLOR_PATTERN = re.compile(r"^#[0-9a-fA-F]{6}$")
MAX_INSTANCE_TITLE_LENGTH = 256
MAX_LOCATION_LENGTH = 2048
# The instances library lets a URL carry the tab placeholder at most once.
TAB_PLACEHOLDER = "{tab}"
FILES_KEY_NUMBER_PATTERN = re.compile(r"^files-([1-9][0-9]*)$")

# The frontend's panel components, as its ``createComponent`` names them (the shell's
# ``dockview_document`` module writes the same entry).
INSTANCE_COMPONENT = "instance"
CUSTOM_TAB_COMPONENT = "custom"
MINTED_ID_BYTES = 8
UNMIGRATED_DOCKVIEW_KEYS = frozenset({"floatingGroups", "popoutGroups"})

ADDRESS_SCHEME = "app:"

# The built-in apps whose every tab is an instance: a bare ``service:<name>`` of one of these
# named the old sessionless viewer (or a pin), never something a tab can show now.
INSTANCE_ONLY_APPS = frozenset({"chat", "terminal", "files", "browser"})
# The one app whose old panels named their instance by a ``?session=`` parameter of the URL.
BROWSER_APP = "browser"


class LegacyProject(NamedTuple):
    """One entry of the old ``projects_meta.json`` registry, tolerating hand-edits."""

    project_id: str
    name: str
    color: str
    glyph: int
    members: tuple[str, ...]
    # The old overrides map: a built-in name or ``app:<name>`` to its deviations from the
    # defaults (``is_pinned`` False, or a ``mode``); the legacy ``unpinned_shortcuts`` list is
    # folded in as ``is_pinned`` False.
    overrides: dict[str, dict[str, Any]]


class MigratedLayout(NamedTuple):
    """One old content file as a layout record, with what was kept and what was pruned."""

    # None when no panel maps to an instance.
    record: dict[str, Any] | None
    addresses: tuple[str, ...]
    dropped_panel_ids: tuple[str, ...]


class SeedPlan(NamedTuple):
    """One seed layout a run writes, or skips."""

    view_id: str
    device: str
    path: Path
    layout: dict[str, Any] | None
    addresses: tuple[str, ...]
    dropped_panel_ids: tuple[str, ...]
    is_skipped: bool
    note: str


class ProjectPlan(NamedTuple):
    """One project as the run files it."""

    document: dict[str, Any]
    dropped_members: tuple[str, ...]


class MigrationPlan(NamedTuple):
    """Everything a run would write, computed before anything is written."""

    source_dir: Path
    is_source_present: bool
    is_already_migrated: bool
    projects: tuple[ProjectPlan, ...]
    is_projects_skipped: bool
    # Why the projects file is kept as it is, when it is.
    projects_note: str
    seeds: tuple[SeedPlan, ...]
    files_records: tuple[dict[str, Any], ...]
    terminal_records: tuple[dict[str, Any], ...]
    notes: tuple[str, ...]


def _log(message: str) -> None:
    sys.stderr.write(f"migrate_workspace_layouts: {message}\n")


def mint_tab_id() -> str:
    return f"tab-{secrets.token_hex(MINTED_ID_BYTES)}"


def legacy_layout_dir_from_env(environ: dict[str, str]) -> Path | None:
    """The old store this workspace's shell wrote, from the mngr environment, or None (logged)
    when the environment names nothing or the host holds more than one.

    The store of the environment's own agent when it has one (the boot-time run is the
    services agent, which the old shell ran as); else the one store any agent of the host has
    (the update apply runs as a chat agent, whose own state directory never held one); else
    the environment's own, absent, so a fresh workspace still gets its marker.
    """
    host_dir = environ.get(ENV_HOST_DIR, "")
    agent_id = environ.get(ENV_AGENT_ID, "")
    if not host_dir or not agent_id:
        _log(
            f"neither --source nor ${ENV_HOST_DIR} and ${ENV_AGENT_ID} name the old layout store; nothing to do"
        )
        return None
    agents_dir = Path(host_dir) / AGENTS_DIRNAME
    own_layout_dir = agents_dir / agent_id / LEGACY_LAYOUT_DIRNAME
    if (own_layout_dir / LEGACY_META_FILENAME).is_file():
        return own_layout_dir
    found = sorted(
        meta_path.parent
        for meta_path in agents_dir.glob(
            f"*/{LEGACY_LAYOUT_DIRNAME}/{LEGACY_META_FILENAME}"
        )
    )
    if len(found) > 1:
        _log(
            "several agents hold an old layout store ("
            + ", ".join(str(path) for path in found)
            + "); pass --source to choose one; nothing to do"
        )
        return None
    return found[0] if found else own_layout_dir


def registry_path_from_env(environ: dict[str, str]) -> Path:
    return Path(environ.get(ENV_APPS_FILE, str(DEFAULT_REGISTRY_PATH)))


# --- The address mapping ------------------------------------------------------------------


def _is_app_name(name: str) -> bool:
    return (
        0 < len(name) <= MAX_APP_NAME_LENGTH
        and APP_NAME_PATTERN.fullmatch(name) is not None
    )


def _is_instance_key(key: str) -> bool:
    return INSTANCE_KEY_PATTERN.fullmatch(key) is not None


def _address(app: str, key: str | None) -> str | None:
    if not _is_app_name(app):
        return None
    if key is None:
        return f"{ADDRESS_SCHEME}{app}"
    if not _is_instance_key(key):
        return None
    if app == "terminal" and TMUX_SESSION_NAME_PATTERN.fullmatch(key) is None:
        return None
    return f"{ADDRESS_SCHEME}{app}?instance={key}"


def address_for_ref(ref: str) -> str | None:
    """The address a member ref names, or None for a ref nothing can show any more.

    The table of the phase 9 spec: chats by agent id, terminals by session name, browsers by
    session, app instances by key, a bare ``service:<name>`` as the single-instance app's one
    address; ``url:`` and ``subagent:`` refs (and anything unparseable) map to nothing.
    """
    scheme, separator, body = ref.partition(":")
    if not separator or not body:
        return None
    if scheme == "chat":
        return _address("chat", body)
    if scheme == "terminal":
        return _address("terminal", body)
    if scheme != "service":
        return None
    name, query_separator, query = body.partition("?")
    if not query_separator:
        return None if name in INSTANCE_ONLY_APPS else _address(name, None)
    parameter, value_separator, value = query.partition("=")
    if not value_separator or not value:
        return None
    if parameter == "instance":
        return _address(name, value)
    if parameter == "session" and name == BROWSER_APP:
        return _address(name, value)
    return None


def is_app_pin_ref(ref: str) -> bool:
    """Whether a member ref is an app's pin (a bare ``service:<name>``), which becomes a shortcut.

    A bare ref of one of the built-in apps is not a pin (their rail rows were never members;
    such a ref named the old sessionless viewer) and maps to nothing, like a bare ref whose
    name is not an app name at all.
    """
    scheme, separator, name = ref.partition(":")
    return (
        scheme == "service"
        and bool(separator)
        and "?" not in name
        and _is_app_name(name)
        and name not in INSTANCE_ONLY_APPS
    )


def _browser_session_from_url(url: Any) -> str | None:
    if not isinstance(url, str):
        return None
    query = url.partition("?")[2].partition("#")[0]
    for pair in query.split("&"):
        parameter, separator, value = pair.partition("=")
        if separator and parameter == "session" and value:
            return value
    return None


def ref_for_panel(params: dict[str, Any]) -> str | None:
    """The member ref a saved panel's params filed it under (the old ``projects`` grammar), or None."""
    # A chat panel from before ``chatAgentId`` existed names its agent by ``agentId`` alone,
    # the fallback the old frontend read too.
    chat_agent_id = params.get("chatAgentId") or params.get("agentId")
    terminal_session_name = params.get("terminalSessionName")
    service_name = params.get("serviceName")
    service_instance_id = params.get("serviceInstanceId")
    if (
        params.get("panelType") == "chat"
        and isinstance(chat_agent_id, str)
        and chat_agent_id
    ):
        return f"chat:{chat_agent_id}"
    if isinstance(terminal_session_name, str) and terminal_session_name:
        return f"terminal:{terminal_session_name}"
    if isinstance(service_name, str) and service_name:
        if isinstance(service_instance_id, str) and service_instance_id:
            return f"service:{service_name}?instance={service_instance_id}"
        session = (
            _browser_session_from_url(params.get("url"))
            if service_name == BROWSER_APP
            else None
        )
        if session is not None:
            return f"service:{service_name}?session={session}"
        return f"service:{service_name}"
    return None


# --- Reading the old store ------------------------------------------------------------------


def _read_json_object(path: Path, notes: list[str]) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        notes.append(f"skipped unreadable {path}: {e}")
        return None
    if not isinstance(parsed, dict):
        notes.append(f"skipped {path}: expected a JSON object")
        return None
    return parsed


def _read_ref_map(
    path: Path, key: str, value_type: type, notes: list[str]
) -> dict[str, Any]:
    stored = _read_json_object(path, notes)
    raw = stored.get(key) if stored is not None else None
    if not isinstance(raw, dict):
        return {}
    return {
        ref: value
        for ref, value in raw.items()
        if isinstance(ref, str)
        and isinstance(value, value_type)
        and not isinstance(value, bool)
    }


def _legacy_overrides(entry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw_overrides = entry.get("shortcut_overrides")
    if isinstance(raw_overrides, dict):
        return {
            shortcut_id: dict(override)
            for shortcut_id, override in raw_overrides.items()
            if isinstance(shortcut_id, str) and isinstance(override, dict)
        }
    legacy_unpinned = entry.get("unpinned_shortcuts")
    if not isinstance(legacy_unpinned, list):
        return {}
    return {
        name: {"is_pinned": False} for name in legacy_unpinned if isinstance(name, str)
    }


def read_legacy_projects(meta: dict[str, Any], notes: list[str]) -> list[LegacyProject]:
    """Every project of the old registry, in registry order, tolerating fields a hand-edit lost."""
    project_by_id = meta.get("project_by_id")
    if not isinstance(project_by_id, dict):
        notes.append(
            f"{LEGACY_META_FILENAME} holds no project_by_id map; no projects migrated"
        )
        return []
    projects: list[LegacyProject] = []
    for project_id, entry in project_by_id.items():
        if not isinstance(project_id, str) or not isinstance(entry, dict):
            notes.append(f"skipped a malformed project entry {project_id!r}")
            continue
        if (
            VIEW_ID_PATTERN.fullmatch(project_id) is None
            or project_id == EVERYTHING_VIEW_ID
        ):
            notes.append(f"skipped project {project_id!r}: not a usable project id")
            continue
        color = entry.get("color")
        trimmed_color = color.strip() if isinstance(color, str) else ""
        glyph = entry.get("glyph")
        members = entry.get("members")
        projects.append(
            LegacyProject(
                project_id=project_id,
                name=str(entry.get("name") or project_id).strip() or project_id,
                color=trimmed_color
                if COLOR_PATTERN.fullmatch(trimmed_color)
                else DEFAULT_PROJECT_COLOR,
                glyph=glyph
                if isinstance(glyph, int)
                and not isinstance(glyph, bool)
                and 0 <= glyph < GLYPH_COUNT
                else DEFAULT_PROJECT_GLYPH,
                members=tuple(
                    member for member in members if isinstance(member, str) and member
                )
                if isinstance(members, list)
                else (),
                overrides=_legacy_overrides(entry),
            )
        )
    return projects


def read_registry_rows(path: Path, notes: list[str]) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        parsed = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        notes.append(f"ignored unreadable registry {path}: {e}")
        return []
    rows = parsed.get("apps")
    return (
        [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    )


# --- Seed layouts ---------------------------------------------------------------------------


def _pruned_grid_node(node: dict[str, Any], panel_id: str) -> dict[str, Any] | None:
    """Drop ``panel_id`` from one grid node, or None when the node empties out (the shell's rule)."""
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
            pruned
            for pruned in (_pruned_grid_node(child, panel_id) for child in children)
            if pruned is not None
        ]
        if not pruned_children:
            return None
        return {**node, "data": pruned_children}
    return node


def strip_panel_from_dockview(
    dockview: dict[str, Any], panel_id: str
) -> dict[str, Any] | None:
    """Remove one panel from a serialized dockview grid, or None when nothing is left.

    The same shape as the shell's ``layouts.strip_panel_from_dockview``: the panel leaves
    ``panels`` and whichever group holds it, a group that empties collapses away, and a grid
    that empties answers None.
    """
    panels = dockview.get("panels")
    pruned_panels = (
        {key: value for key, value in panels.items() if key != panel_id}
        if isinstance(panels, dict)
        else panels
    )
    if isinstance(pruned_panels, dict) and not pruned_panels:
        return None
    pruned: dict[str, Any] = {**dockview, "panels": pruned_panels}
    grid = dockview.get("grid")
    if isinstance(grid, dict):
        root = grid.get("root")
        pruned_root = (
            _pruned_grid_node(root, panel_id) if isinstance(root, dict) else root
        )
        if pruned_root is None:
            return None
        pruned["grid"] = {**grid, "root": pruned_root}
    return pruned


def _rename_panel_in_grid_node(node: Any, old_id: str, new_id: str) -> None:
    if not isinstance(node, dict):
        return
    data = node.get("data")
    if node.get("type") == "leaf" and isinstance(data, dict):
        views = data.get("views")
        if isinstance(views, list):
            data["views"] = [new_id if view == old_id else view for view in views]
        if data.get("activeView") == old_id:
            data["activeView"] = new_id
        return
    if isinstance(data, list):
        for child in data:
            _rename_panel_in_grid_node(child, old_id, new_id)


def _panel_title(entry: dict[str, Any], params: dict[str, Any], address: str) -> str:
    for candidate in (
        params.get("customTitle"),
        entry.get("title"),
        params.get("title"),
    ):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return address


def _panel_entry(tab_id: str, address: str, title: str) -> dict[str, Any]:
    return {
        "id": tab_id,
        "contentComponent": INSTANCE_COMPONENT,
        "tabComponent": CUSTOM_TAB_COMPONENT,
        "title": title,
        "params": {"kind": INSTANCE_COMPONENT, "address": address, "tabId": tab_id},
    }


def migrate_layout_content(
    content: dict[str, Any],
    device: str,
    last_used_ms_by_ref: dict[str, int],
    now_iso: str,
    mint: Callable[[], str],
) -> MigratedLayout:
    """One old content file as a layout record (the ``layout`` of contracts.md section 6).

    The grid is kept as dockview saved it; every panel that maps to an address is renamed to a
    fresh tab id and its entry rebuilt in the frontend's current shape; every other panel (a
    launcher, a subagent view, an ad-hoc URL page, a second panel of an address already kept)
    is pruned.
    """
    dockview = content.get("dockview")
    if not isinstance(dockview, dict) or not isinstance(dockview.get("panels"), dict):
        return MigratedLayout(record=None, addresses=(), dropped_panel_ids=())
    panel_params = content.get("panelParams")
    params_by_panel_id = panel_params if isinstance(panel_params, dict) else {}
    # dockview's floating and popout groups also name panel ids; nothing in the old shell made
    # any, so they are dropped rather than renamed.
    document: dict[str, Any] | None = {
        key: value
        for key, value in copy.deepcopy(dockview).items()
        if key not in UNMIGRATED_DOCKVIEW_KEYS
    }
    tabs: dict[str, dict[str, Any]] = {}
    kept_addresses: list[str] = []
    dropped: list[str] = []
    for panel_id, entry in list(dockview["panels"].items()):
        if document is None:
            break
        params = params_by_panel_id.get(panel_id)
        params = params if isinstance(params, dict) else {}
        ref = ref_for_panel(params)
        address = None if ref is None else address_for_ref(ref)
        if address is None or address in kept_addresses:
            document = strip_panel_from_dockview(document, panel_id)
            dropped.append(panel_id)
            continue
        tab_id = mint()
        grid = document.get("grid")
        _rename_panel_in_grid_node(
            grid.get("root") if isinstance(grid, dict) else None, panel_id, tab_id
        )
        panels = document["panels"]
        del panels[panel_id]
        panels[tab_id] = _panel_entry(
            tab_id,
            address,
            _panel_title(entry if isinstance(entry, dict) else {}, params, address),
        )
        tabs[tab_id] = {
            "address": address,
            "tab_id": tab_id,
            "last_focused_ms": last_used_ms_by_ref.get(ref, 0),
        }
        kept_addresses.append(address)
    if document is None or not tabs:
        return MigratedLayout(
            record=None, addresses=(), dropped_panel_ids=tuple(dropped)
        )
    record = {
        "dockview": document,
        "tabs": tabs,
        "device_kind": device,
        "updated_at": now_iso,
    }
    return MigratedLayout(
        record=record, addresses=tuple(kept_addresses), dropped_panel_ids=tuple(dropped)
    )


# --- Projects -------------------------------------------------------------------------------


def _pin_shortcut(
    app: str, registry_rows: Sequence[dict[str, Any]]
) -> tuple[str, str] | None:
    """The ``(action, mode)`` an app's pin becomes: its default action when the registry says it has
    instances, else the synthesized ``open`` of a single-instance app."""
    for row in registry_rows:
        if row.get("name") != app:
            continue
        if row.get("instances") is not True:
            return OPEN_ACTION_ID, "focus"
        default_shortcut = row.get("default_shortcut")
        if isinstance(default_shortcut, dict) and isinstance(
            default_shortcut.get("action"), str
        ):
            mode = default_shortcut.get("mode")
            return default_shortcut[
                "action"
            ], mode if mode in SHORTCUT_MODES else "focus"
        actions = row.get("actions")
        if isinstance(actions, list):
            for action in actions:
                if isinstance(action, dict) and isinstance(action.get("id"), str):
                    return action["id"], "focus"
        return None
    return OPEN_ACTION_ID, "focus"


def _shortcut(
    app: str, action: str, mode_override: Any, default_mode: str
) -> dict[str, str]:
    """One rail row, in its stored mode override when that names a mode, else the default."""
    return {
        "app": app,
        "action": action,
        "mode": mode_override if mode_override in SHORTCUT_MODES else default_mode,
    }


def derive_shortcuts(
    project: LegacyProject, registry_rows: Sequence[dict[str, Any]]
) -> list[dict[str, str]]:
    """A project's rail as data: the built-in rows minus the unpinned ones, each in its effective mode,
    then one row per pinned app in member order."""
    shortcuts: list[dict[str, str]] = []
    for app, action, default_mode in BUILT_IN_SHORTCUTS:
        override = project.overrides.get(app, {})
        if override.get("is_pinned") is False:
            continue
        shortcuts.append(_shortcut(app, action, override.get("mode"), default_mode))
    for member in project.members:
        if not is_app_pin_ref(member):
            continue
        app = member[len("service:") :]
        if any(shortcut["app"] == app for shortcut in shortcuts):
            continue
        pin = _pin_shortcut(app, registry_rows)
        if pin is None:
            continue
        action, default_mode = pin
        override = project.overrides.get(f"{LEGACY_APP_SHORTCUT_PREFIX}{app}", {})
        shortcuts.append(_shortcut(app, action, override.get("mode"), default_mode))
    return shortcuts


def build_project_plan(
    project: LegacyProject,
    seed_addresses: Sequence[str],
    registry_rows: Sequence[dict[str, Any]],
) -> ProjectPlan:
    """The project as the shell files it: its members mapped to addresses (pins become shortcuts, dead
    refs are dropped), then any address its seeds dock that the member list did not name."""
    tabs: list[str] = []
    dropped: list[str] = []
    for member in project.members:
        if is_app_pin_ref(member):
            continue
        address = address_for_ref(member)
        if address is None:
            dropped.append(member)
        elif address not in tabs:
            tabs.append(address)
    for address in seed_addresses:
        if address not in tabs:
            tabs.append(address)
    document = {
        "id": project.project_id,
        "name": project.name,
        "color": project.color,
        "glyph": project.glyph,
        "tabs": tabs,
        "shortcuts": derive_shortcuts(project, registry_rows),
    }
    return ProjectPlan(document=document, dropped_members=tuple(dropped))


# --- The app stores -------------------------------------------------------------------------


def _ms_to_iso(at_ms: int) -> str:
    return datetime.fromtimestamp(at_ms / 1000, tz=timezone.utc).isoformat()


def _iso_to_ms(at_iso: str) -> int:
    return int(datetime.fromisoformat(at_iso).timestamp() * 1000)


def _is_location(path: str) -> bool:
    """Whether the instances library accepts ``path`` as an instance URL (its rules, restated)."""
    return (
        path.startswith("/")
        and not path.startswith("//")
        and len(path) <= MAX_LOCATION_LENGTH
        and not any(character < " " or character == "\x7f" for character in path)
        and path.count(TAB_PLACEHOLDER) <= 1
    )


def files_record(
    key: str,
    location_by_ref: dict[str, str],
    last_used_ms_by_ref: dict[str, int],
    now_iso: str,
) -> dict[str, Any]:
    """One file viewer as the files app's store lists it (an ``InstanceRecord`` of the instances library)."""
    ref = f"service:files?instance={key}"
    location = location_by_ref.get(ref, "")
    match = FILES_KEY_NUMBER_PATTERN.fullmatch(key)
    last_used_ms = last_used_ms_by_ref.get(ref)
    # The old store's rule, which also keeps a hand-edited value convertible: a stamp ahead of
    # the clock reads as now.
    last_active = now_iso
    if isinstance(last_used_ms, int) and 0 < last_used_ms < _iso_to_ms(now_iso):
        last_active = _ms_to_iso(last_used_ms)
    return {
        "key": key,
        "url": location.strip() if _is_location(location.strip()) else "/",
        "title": f"File Viewer {match.group(1)}" if match else key,
        "status": "idle",
        "lifetime": "referenced",
        "last_active": last_active,
        "renameable": False,
    }


def terminal_record(name: str, title_by_ref: dict[str, str]) -> dict[str, Any]:
    """One terminal as the terminal app's store remembers it: its session name and the title it was given."""
    title = title_by_ref.get(f"terminal:{name}", "").strip()
    return {
        "name": name,
        "title": title if 0 < len(title) <= MAX_INSTANCE_TITLE_LENGTH else None,
        "workdir": None,
    }


def _instance_keys_of(app: str, addresses: Sequence[str]) -> list[str]:
    prefix = f"{ADDRESS_SCHEME}{app}?instance="
    keys: list[str] = []
    for address in addresses:
        if address.startswith(prefix) and address[len(prefix) :] not in keys:
            keys.append(address[len(prefix) :])
    return keys


class MergedStore(NamedTuple):
    """An app store with the migration's records folded in, and how many were new."""

    document: dict[str, Any]
    added_count: int


def merged_store_document(
    existing: dict[str, Any] | None,
    records_key: str,
    identity_key: str,
    records: Sequence[dict[str, Any]],
) -> MergedStore:
    """The store with every record whose identity it lacks appended; the existing records are never changed."""
    existing_records = existing.get(records_key) if existing is not None else None
    kept = (
        [record for record in existing_records if isinstance(record, dict)]
        if isinstance(existing_records, list)
        else []
    )
    known = {record.get(identity_key) for record in kept}
    added = [record for record in records if record[identity_key] not in known]
    return MergedStore(
        document={"version": STORE_VERSION, records_key: [*kept, *added]},
        added_count=len(added),
    )


# --- Planning and writing -------------------------------------------------------------------


def _write_json_atomic(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.tmp-{secrets.token_hex(8)}")
    try:
        temp_path.write_text(json.dumps(document, indent=2), encoding="utf-8")
        os.replace(temp_path, path)
    except OSError:
        temp_path.unlink(missing_ok=True)
        raise


def _has_projects(document: dict[str, Any] | None) -> bool:
    projects = document.get("projects") if document is not None else None
    return isinstance(projects, list) and len(projects) > 0


def _skipped_seed(
    view_id: str,
    device: str,
    path: Path,
    dropped_panel_ids: tuple[str, ...],
    note: str,
) -> SeedPlan:
    return SeedPlan(
        view_id=view_id,
        device=device,
        path=path,
        layout=None,
        addresses=(),
        dropped_panel_ids=dropped_panel_ids,
        is_skipped=True,
        note=note,
    )


def _plan_seeds(
    source_dir: Path,
    state_dir: Path,
    view_ids: Sequence[str],
    last_used_ms_by_ref: dict[str, int],
    now_iso: str,
    is_forced: bool,
    mint: Callable[[], str],
    notes: list[str],
) -> list[SeedPlan]:
    """Each view's per-device seeds, from the content files it has; what cannot be read or shows
    nothing is a skipped seed, and an existing seed is skipped unless forced."""
    seeds: list[SeedPlan] = []
    for view_id in view_ids:
        for device, suffix in LEGACY_CONTENT_SUFFIX_BY_DEVICE.items():
            content_path = source_dir / LEGACY_PROJECTS_SUBDIR / f"{view_id}{suffix}"
            seed_path = state_dir / LAYOUTS_DIRNAME / view_id / f"seed.{device}.json"
            if not content_path.exists():
                continue
            content = _read_json_object(content_path, notes)
            if content is None:
                seeds.append(
                    _skipped_seed(
                        view_id, device, seed_path, (), f"unreadable {content_path}"
                    )
                )
                continue
            migrated = migrate_layout_content(
                content, device, last_used_ms_by_ref, now_iso, mint
            )
            if migrated.record is None:
                seeds.append(
                    _skipped_seed(
                        view_id,
                        device,
                        seed_path,
                        migrated.dropped_panel_ids,
                        "no panel maps to an instance",
                    )
                )
                continue
            is_existing = seed_path.exists() and not is_forced
            seeds.append(
                SeedPlan(
                    view_id=view_id,
                    device=device,
                    path=seed_path,
                    layout=migrated.record,
                    addresses=migrated.addresses,
                    dropped_panel_ids=migrated.dropped_panel_ids,
                    is_skipped=is_existing,
                    note="seed already exists" if is_existing else "",
                )
            )
    return seeds


def _projects_note(state_dir: Path, is_forced: bool, notes: list[str]) -> str:
    """Why the projects file is kept as it is, or "" when the run writes it."""
    projects_notes: list[str] = []
    existing_projects = _read_json_object(state_dir / PROJECTS_FILENAME, projects_notes)
    notes.extend(projects_notes)
    if is_forced:
        return ""
    if projects_notes:
        return "it cannot be read; pass --force to overwrite it"
    if _has_projects(existing_projects):
        return "it already holds projects"
    return ""


def _referenced_addresses(
    projects: Sequence[ProjectPlan], seeds: Sequence[SeedPlan]
) -> list[str]:
    """Every address a project's tab set or a seed docks, with repeats."""
    referenced: list[str] = []
    for project_plan in projects:
        referenced.extend(project_plan.document["tabs"])
    for seed in seeds:
        referenced.extend(seed.addresses)
    return referenced


def plan_migration(
    source_dir: Path,
    state_dir: Path,
    registry_path: Path,
    now_iso: str,
    is_forced: bool,
    mint: Callable[[], str],
) -> MigrationPlan:
    """Read the old store and compute every output, without writing anything."""
    notes: list[str] = []
    marker_path = state_dir / MARKER_FILENAME
    is_already_migrated = marker_path.exists() and not is_forced
    meta = _read_json_object(source_dir / LEGACY_META_FILENAME, notes)
    is_source_present = source_dir.is_dir() and meta is not None
    if not is_source_present:
        return MigrationPlan(
            source_dir=source_dir,
            is_source_present=False,
            is_already_migrated=is_already_migrated,
            projects=(),
            is_projects_skipped=False,
            projects_note="",
            seeds=(),
            files_records=(),
            terminal_records=(),
            notes=tuple(notes),
        )
    title_by_ref = _read_ref_map(
        source_dir / LEGACY_TITLES_FILENAME, "title_by_ref", str, notes
    )
    last_used_ms_by_ref = _read_ref_map(
        source_dir / LEGACY_LAST_USED_FILENAME, "last_used_ms_by_ref", int, notes
    )
    location_by_ref = _read_ref_map(
        source_dir / LEGACY_LOCATIONS_FILENAME, "location_by_ref", str, notes
    )
    registry_rows = read_registry_rows(registry_path, notes)
    legacy_projects = read_legacy_projects(meta, notes)

    # Every view's seeds: each project's, then Everything's.
    seeds = _plan_seeds(
        source_dir,
        state_dir,
        [project.project_id for project in legacy_projects] + [EVERYTHING_VIEW_ID],
        last_used_ms_by_ref,
        now_iso,
        is_forced,
        mint,
        notes,
    )

    # The projects, with the addresses their seeds dock folded into their tab sets.
    projects: list[ProjectPlan] = []
    for project in legacy_projects:
        seed_addresses = [
            address
            for seed in seeds
            if seed.view_id == project.project_id and seed.layout is not None
            for address in seed.addresses
        ]
        projects.append(build_project_plan(project, seed_addresses, registry_rows))
    projects_note = _projects_note(state_dir, is_forced, notes)

    # The app stores: one record per instance any project or seed references.
    referenced = _referenced_addresses(projects, seeds)
    files_records = tuple(
        files_record(key, location_by_ref, last_used_ms_by_ref, now_iso)
        for key in _instance_keys_of("files", referenced)
    )
    terminal_records = tuple(
        terminal_record(name, title_by_ref)
        for name in _instance_keys_of("terminal", referenced)
    )
    return MigrationPlan(
        source_dir=source_dir,
        is_source_present=True,
        is_already_migrated=is_already_migrated,
        projects=tuple(projects),
        is_projects_skipped=projects_note != "",
        projects_note=projects_note,
        seeds=tuple(seeds),
        files_records=files_records,
        terminal_records=terminal_records,
        notes=tuple(notes),
    )


def plan_as_json(plan: MigrationPlan) -> dict[str, Any]:
    return {
        "source": str(plan.source_dir),
        "is_source_present": plan.is_source_present,
        "is_already_migrated": plan.is_already_migrated,
        "is_projects_skipped": plan.is_projects_skipped,
        "projects_note": plan.projects_note,
        "projects": [
            {**project.document, "dropped_members": list(project.dropped_members)}
            for project in plan.projects
        ],
        "seeds": [
            {
                "view_id": seed.view_id,
                "device": seed.device,
                "path": str(seed.path),
                "tabs": list(seed.addresses),
                "dropped_panels": list(seed.dropped_panel_ids),
                "is_skipped": seed.is_skipped,
                "note": seed.note,
            }
            for seed in plan.seeds
        ],
        "files": [
            {"key": record["key"], "url": record["url"]}
            for record in plan.files_records
        ],
        "terminals": [
            {"name": record["name"], "title": record["title"]}
            for record in plan.terminal_records
        ],
        "notes": list(plan.notes),
    }


def apply_plan(
    plan: MigrationPlan, state_dir: Path, apps_data_dir: Path, now_iso: str
) -> None:
    """Write every output the plan holds, the two app stores by merging, then the marker."""
    for note in plan.notes:
        _log(note)
    if plan.is_source_present:
        if plan.is_projects_skipped:
            _log(
                f"kept the existing {state_dir / PROJECTS_FILENAME}: {plan.projects_note}"
            )
        else:
            _write_json_atomic(
                state_dir / PROJECTS_FILENAME,
                {
                    "version": PROJECTS_FILE_VERSION,
                    "projects": [project.document for project in plan.projects],
                },
            )
            _log(
                f"wrote {len(plan.projects)} project(s) to {state_dir / PROJECTS_FILENAME}"
            )
        for seed in plan.seeds:
            if seed.is_skipped or seed.layout is None:
                _log(f"skipped the {seed.device} seed of {seed.view_id!r}: {seed.note}")
                continue
            _write_json_atomic(seed.path, seed.layout)
            _log(
                f"wrote the {seed.device} seed of {seed.view_id!r} with {len(seed.addresses)} tab(s)"
            )
        for app, records_key, identity_key, records in (
            ("files", "instances", "key", plan.files_records),
            ("terminal", "sessions", "name", plan.terminal_records),
        ):
            store_path = apps_data_dir / app / STORE_FILENAME
            store_notes: list[str] = []
            existing = _read_json_object(store_path, store_notes)
            if store_notes:
                _log(f"left the {app} store alone: {store_notes[0]}")
                continue
            if existing is not None and existing.get("version") != STORE_VERSION:
                _log(
                    f"left the {app} store alone: {store_path} is version "
                    f"{existing.get('version')!r}; this script writes version {STORE_VERSION}"
                )
                continue
            merged = merged_store_document(existing, records_key, identity_key, records)
            if merged.added_count > 0:
                _write_json_atomic(store_path, merged.document)
                _log(f"added {merged.added_count} record(s) to {store_path}")
    elif plan.source_dir.is_dir():
        _log(
            f"no readable {LEGACY_META_FILENAME} in {plan.source_dir}; nothing to migrate"
        )
    else:
        _log(f"no old layout store at {plan.source_dir}; nothing to migrate")
    _write_json_atomic(
        state_dir / MARKER_FILENAME,
        {
            "version": MARKER_VERSION,
            "migrated_at": now_iso,
            "source": str(plan.source_dir),
        },
    )


def _parse_now(value: str | None) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat()
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=None,
        help=(
            f"the old {LEGACY_LAYOUT_DIRNAME} directory (default: the one under "
            f"${ENV_HOST_DIR}/{AGENTS_DIRNAME}/${ENV_AGENT_ID}, else the one any agent of "
            f"${ENV_HOST_DIR} has)"
        ),
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=DEFAULT_STATE_DIR,
        help="the shell's state directory",
    )
    parser.add_argument(
        "--apps-data-dir",
        type=Path,
        default=DEFAULT_APPS_DATA_DIR,
        help="where the apps keep their stores",
    )
    parser.add_argument(
        "--registry",
        type=Path,
        default=None,
        help=f"the app registry (default: ${ENV_APPS_FILE} or {DEFAULT_REGISTRY_PATH})",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="run again: overwrite the projects file and the seeds, and rewrite the marker",
    )
    parser.add_argument("--now", default=None, help=argparse.SUPPRESS)
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("run", help="write the state files (the default)")
    plan_parser = subparsers.add_parser(
        "plan", help="print what a run would write, without writing"
    )
    plan_parser.add_argument(
        "--json", action="store_true", help="machine-readable output"
    )
    return parser


def main(
    argv: Sequence[str] | None = None, environ: dict[str, str] | None = None
) -> int:
    args = build_parser().parse_args(argv)
    environment = dict(os.environ) if environ is None else environ
    source_dir = (
        args.source
        if args.source is not None
        else legacy_layout_dir_from_env(environment)
    )
    if source_dir is None:
        return 0
    registry_path = (
        args.registry
        if args.registry is not None
        else registry_path_from_env(environment)
    )
    now_iso = _parse_now(args.now)
    plan = plan_migration(
        source_dir, args.state_dir, registry_path, now_iso, args.force, mint_tab_id
    )
    if args.command == "plan":
        if args.json:
            sys.stdout.write(json.dumps(plan_as_json(plan), indent=2) + "\n")
        else:
            _print_plan(plan)
        return 0
    if plan.is_already_migrated:
        _log(
            f"already migrated ({args.state_dir / MARKER_FILENAME} exists); pass --force to run again"
        )
        return 0
    apply_plan(plan, args.state_dir, args.apps_data_dir, now_iso)
    return 0


def _print_plan(plan: MigrationPlan) -> None:
    lines = [
        f"source: {plan.source_dir} ({'present' if plan.is_source_present else 'absent'})"
    ]
    if plan.is_already_migrated:
        lines.append("already migrated (pass --force to run again)")
    for project in plan.projects:
        document = project.document
        lines.append(
            f"project {document['id']}: {len(document['tabs'])} tab(s), {len(document['shortcuts'])} shortcut(s)"
        )
        for member in project.dropped_members:
            lines.append(f"  dropped member {member}")
    for seed in plan.seeds:
        state = (
            f"skipped ({seed.note})"
            if seed.is_skipped
            else f"{len(seed.addresses)} tab(s)"
        )
        lines.append(
            f"seed {seed.view_id} {seed.device}: {state}; {len(seed.dropped_panel_ids)} panel(s) dropped"
        )
    lines.append(
        f"files: {len(plan.files_records)} record(s); terminals: {len(plan.terminal_records)} record(s)"
    )
    lines.extend(f"note: {note}" for note in plan.notes)
    sys.stdout.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    sys.exit(main())
