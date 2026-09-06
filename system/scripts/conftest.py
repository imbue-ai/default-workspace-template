"""Fixtures for the scripts' tests: a registry file and a fake shell over loopback for
layout.py, and a pre-arc ``workspace_layout`` directory and its registry for
migrate_workspace_layouts.py."""

from __future__ import annotations

import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import tomlkit

_SCRIPT = Path(__file__).parent / "layout.py"
_spec = importlib.util.spec_from_file_location("layout_for_fixtures", _SCRIPT)
assert _spec is not None and _spec.loader is not None
layout = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(layout)


def _write_apps_toml(path: Path, rows: dict[str, tuple[str, ...]]) -> None:
    """A registry with one row per name; the value is the app's declared action ids (none for a
    single-instance app). An app declaring more than one action gets a ``default_shortcut`` on
    its last one, so the primary-action rule has something to prefer over the first."""
    doc = tomlkit.document()
    apps = tomlkit.aot()
    for name, action_ids in rows.items():
        entry = tomlkit.table()
        entry["name"] = name
        entry["url"] = f"http://localhost:9000/{name}"
        entry["instances"] = len(action_ids) > 0
        if action_ids:
            actions = tomlkit.aot()
            for action_id in action_ids:
                action = tomlkit.table()
                action["id"] = action_id
                action["label"] = f"{action_id.capitalize()} {name}"
                actions.append(action)
            entry["actions"] = actions
        if len(action_ids) > 1:
            default_shortcut = tomlkit.inline_table()
            default_shortcut["action"] = action_ids[-1]
            default_shortcut["mode"] = "focus"
            entry["default_shortcut"] = default_shortcut
        apps.append(entry)
    doc["apps"] = apps
    path.write_text(tomlkit.dumps(doc))


@pytest.fixture
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "apps.toml"
    _write_apps_toml(
        path, {"files": (), "terminal": ("new",), "chat": ("subagent", "new")}
    )
    monkeypatch.setenv(layout.ENV_APPS_FILE, str(path))
    return path


class _FakeShellHandler(BaseHTTPRequestHandler):
    """The shell's REST routes the relay verbs and the shortcut commands ride."""

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _respond(self, status: int, body: dict[str, Any]) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        server: Any = self.server
        if self.path == "/api/projects":
            self._respond(200, {"projects": server.projects})
            return
        if self.path == "/api/inventory":
            self._respond(
                200,
                {
                    "projects": server.projects,
                    "everything": {"id": "everything", "tabs": server.everything_tabs},
                    "apps": server.inventory_apps,
                    "clients": server.inventory_clients,
                },
            )
            return
        self._respond(404, {"detail": f"unknown path {self.path}"})

    def do_POST(self) -> None:
        server: Any = self.server
        body_length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(body_length) or b"{}")
        server.posted.append((self.path, body))
        if self.path == "/api/layout/broadcast":
            self._respond(
                200,
                {
                    "ok": True,
                    "clients": server.context_clients,
                    "view_id": "everything",
                    "client_id": "c1",
                    "target_client_id": "c1",
                    "layout": server.op_layout,
                    "created_address": server.created_address,
                },
            )
            return
        if self.path.startswith("/api/projects/") and "/shortcuts" in self.path:
            self._respond(
                200,
                {"id": self.path.split("/")[3], "shortcuts": server.shortcuts_answer},
            )
            return
        if self.path.startswith("/api/apps/"):
            if server.relay_refuses:
                self._respond(404, {"detail": "no such instance"})
            elif self.path.endswith("/delete"):
                self._respond(204, {})
            else:
                self._respond(
                    200,
                    {
                        "instance": {
                            "key": self.path.split("/")[5],
                            "title": body.get("title", ""),
                        }
                    },
                )
            return
        self._respond(404, {"detail": f"unknown path {self.path}"})


@pytest.fixture
def fake_shell(monkeypatch: pytest.MonkeyPatch) -> Any:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeShellHandler)
    server.projects = []
    server.posted = []
    server.shortcuts_answer = []
    server.context_clients = []
    server.relay_refuses = False
    server.everything_tabs = []
    server.inventory_apps = []
    server.inventory_clients = []
    server.op_layout = {"active_panel": None, "panels": [], "tree": None}
    server.created_address = None
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv(
        layout.ENV_WORKSPACE_URL, f"http://127.0.0.1:{server.server_address[1]}"
    )
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


_MIGRATE_SCRIPT = Path(__file__).parent / "migrate_workspace_layouts.py"
_migrate_spec = importlib.util.spec_from_file_location(
    "migrate_workspace_layouts_for_fixtures", _MIGRATE_SCRIPT
)
assert _migrate_spec is not None and _migrate_spec.loader is not None
migrate_workspace_layouts = importlib.util.module_from_spec(_migrate_spec)
_migrate_spec.loader.exec_module(migrate_workspace_layouts)


def _legacy_leaf(group_id: str, views: list[str], size: int) -> dict[str, Any]:
    return {
        "type": "leaf",
        "data": {"views": views, "activeView": views[0], "id": group_id},
        "size": size,
    }


def _legacy_content(
    groups: list[tuple[str, list[str], int]], panels: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """One old ``projects/<id>.json``: dockview's own document (the old component names) plus the
    ``panelParams`` sidecar the old frontend kept beside it."""
    return {
        "dockview": {
            "grid": {
                "root": {
                    "type": "branch",
                    "data": [_legacy_leaf(*group) for group in groups],
                    "size": 800,
                },
                "width": 1200,
                "height": 800,
                "orientation": "HORIZONTAL",
            },
            "panels": {
                panel_id: {
                    "id": panel_id,
                    "contentComponent": params.get("panelType", "iframe"),
                    "title": params.get("title", panel_id),
                    "params": params,
                }
                for panel_id, params in panels.items()
            },
            "activeGroup": groups[0][0],
        },
        "panelParams": panels,
    }


# The old panel params, one per kind the pre-arc frontend saved (``PanelParams`` in its
# ``liveSurfaces.ts``): the address each maps to is what the migration tests assert.
LEGACY_PANELS: dict[str, dict[str, Any]] = {
    "chat-agent-aaa": {
        "panelType": "chat",
        "agentId": "agent-aaa",
        "chatAgentId": "agent-aaa",
        "title": "Planning",
    },
    "iframe-terminal-1": {
        "panelType": "iframe",
        "agentId": "agent-primary",
        "url": "http://terminal-x.host-1.localhost:8421/?arg=_&arg=session&arg=terminal-1",
        "title": "Terminal 1",
        "terminalSessionName": "terminal-1",
        "terminalId": "t-1",
    },
    "iframe-browser-1": {
        "panelType": "iframe",
        "agentId": "agent-primary",
        "serviceName": "browser",
        "url": "http://browser-x.host-1.localhost:8421/?session=browser-1",
        "title": "Browser 1",
    },
    "iframe-files-2": {
        "panelType": "iframe",
        "agentId": "agent-primary",
        "serviceName": "files",
        "serviceInstanceId": "files-2",
        "url": "http://files-x.host-1.localhost:8421/data/notes",
        "customTitle": "My notes",
    },
    "iframe-docs": {
        "panelType": "iframe",
        "agentId": "agent-primary",
        "serviceName": "docs",
        "url": "http://docs-x.host-1.localhost:8421/",
        "title": "docs",
    },
    "iframe-url-1": {
        "panelType": "iframe",
        "agentId": "agent-primary",
        "url": "https://example.com/",
        "title": "Example",
    },
    "subagent-s1": {
        "panelType": "subagent",
        "agentId": "agent-aaa",
        "subagentSessionId": "s1",
        "title": "Subagent",
    },
    "new-tab-1": {"panelType": "launcher", "agentId": "agent-primary"},
}


def write_legacy_layout_dir(layout_dir: Path) -> None:
    """A pre-arc ``workspace_layout`` directory in the shape today's retired writers left: two
    projects (one with every panel kind and the overrides map, one hand-edited with the legacy
    unpinned list, the old sessionless files viewer as a member, and a corrupt mobile file), an
    Everything view showing only an ad-hoc page,
    and the three per-ref side stores."""
    projects_dir = layout_dir / "projects"
    projects_dir.mkdir(parents=True)
    (layout_dir / "projects_meta.json").write_text(
        json.dumps(
            {
                "project_by_id": {
                    "project-1": {
                        "name": "Project 1",
                        "color": "#F0603A",
                        "glyph": 3,
                        "members": [
                            "chat:agent-aaa",
                            "terminal:terminal-1",
                            "service:browser?session=browser-1",
                            "service:files?instance=files-2",
                            "service:docs",
                            "service:notes",
                            "url:abcd1234",
                            "subagent:s1",
                            "terminal:bad.name",
                        ],
                        "shortcut_overrides": {
                            "browser": {"is_pinned": False},
                            "chat": {"mode": "focus"},
                            "app:docs": {"mode": "new"},
                        },
                    },
                    "research": {
                        "name": "Research",
                        "color": "purple",
                        "glyph": 42,
                        "members": ["chat:agent-bbb", "service:files"],
                        "unpinned_shortcuts": ["files"],
                    },
                },
                "last_active_id": "research",
            }
        )
    )
    (projects_dir / "project-1.json").write_text(
        json.dumps(
            _legacy_content(
                [
                    ("g1", ["chat-agent-aaa", "iframe-terminal-1", "new-tab-1"], 600),
                    (
                        "g2",
                        [
                            "iframe-browser-1",
                            "iframe-files-2",
                            "iframe-docs",
                            "iframe-url-1",
                            "subagent-s1",
                        ],
                        600,
                    ),
                ],
                LEGACY_PANELS,
            )
        )
    )
    (projects_dir / "project-1.mobile.json").write_text(
        json.dumps(
            _legacy_content(
                [("m1", ["chat-agent-aaa"], 400)],
                {"chat-agent-aaa": LEGACY_PANELS["chat-agent-aaa"]},
            )
        )
    )
    # The older chat panel shape, from before ``chatAgentId`` existed: the agent is named by
    # ``agentId`` alone.
    research_chat = {"panelType": "chat", "agentId": "agent-bbb", "title": "Reading"}
    (projects_dir / "research.json").write_text(
        json.dumps(
            _legacy_content(
                [("r1", ["chat-agent-bbb", "iframe-url-1"], 1200)],
                {
                    "chat-agent-bbb": research_chat,
                    "iframe-url-1": LEGACY_PANELS["iframe-url-1"],
                },
            )
        )
    )
    (projects_dir / "research.mobile.json").write_text("{not json")
    (projects_dir / "everything.json").write_text(
        json.dumps(
            _legacy_content(
                [("e1", ["iframe-url-1"], 1200)],
                {"iframe-url-1": LEGACY_PANELS["iframe-url-1"]},
            )
        )
    )
    (layout_dir / "member_titles.json").write_text(
        json.dumps(
            {
                "title_by_ref": {
                    "terminal:terminal-1": "Build log",
                    "chat:agent-aaa": "Planning",
                }
            }
        )
    )
    (layout_dir / "member_last_used.json").write_text(
        json.dumps(
            {
                "last_used_ms_by_ref": {
                    "chat:agent-aaa": 1700000000000,
                    "service:files?instance=files-2": 1700000001000,
                    "terminal:terminal-1": "not a number",
                }
            }
        )
    )
    (layout_dir / "member_locations.json").write_text(
        json.dumps(
            {
                "location_by_ref": {
                    "service:files?instance=files-2": "/data/notes?sort=name"
                }
            }
        )
    )


@pytest.fixture
def legacy_layout_dir(tmp_path: Path) -> Path:
    layout_dir = tmp_path / "host" / "agents" / "agent-primary" / "workspace_layout"
    write_legacy_layout_dir(layout_dir)
    return layout_dir


@pytest.fixture
def migration_registry(tmp_path: Path) -> Path:
    """A registry with a single-instance app (``docs``) and an app with instances (``notes``), the two
    shapes an app pin can map onto."""
    path = tmp_path / "migration-apps.toml"
    _write_apps_toml(path, {"docs": (), "notes": ("new",)})
    return path
