"""The layout migration over an old-format fixture: every mapping row, the pruning, the shortcut
derivation, the app stores, the marker, idempotency, --force, and the reader round trips (the
shell's stores, the dockview editor, the instances library's store, the terminal's store)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from app_instances.data_types import InstanceLifetime
from conftest import migrate_workspace_layouts as migrate
from files_app.main import build_files_source
from imbue.system_interface.shell.dockview_document import (
    Direction,
    Placement,
    add_panel,
    panel_id_for_address,
)
from imbue.system_interface.shell.layouts import LayoutStore
from imbue.system_interface.shell.primitives import Address, DeviceKind, mint_tab_id
from imbue.system_interface.shell.projects import ProjectStore
from terminal_app.store import JsonTerminalSessionStore

_NOW = "2026-09-05T12:00:00+00:00"
_TAB_ID = re.compile(r"^tab-[0-9a-f]{16}$")

_CHAT_AAA = "app:chat?instance=agent-aaa"
_TERMINAL_1 = "app:terminal?instance=terminal-1"
_BROWSER_1 = "app:browser?instance=browser-1"
_FILES_2 = "app:files?instance=files-2"
_DOCS = "app:docs"


def _run(
    # None leaves the source to the environment, as the bootstrap and the apply do.
    source: Path | None,
    tmp_path: Path,
    registry: Path,
    *extra: str,
    command: str = "run",
    command_args: tuple[str, ...] = (),
    environ: dict[str, str] | None = None,
) -> tuple[int, Path, Path]:
    state_dir = tmp_path / "state"
    apps_dir = tmp_path / "apps"
    code = migrate.main(
        [
            *(() if source is None else ("--source", str(source))),
            "--state-dir",
            str(state_dir),
            "--apps-data-dir",
            str(apps_dir),
            "--registry",
            str(registry),
            "--now",
            _NOW,
            *extra,
            command,
            *command_args,
        ],
        environ={} if environ is None else environ,
    )
    return code, state_dir, apps_dir


def _plan(legacy_layout_dir: Path, tmp_path: Path, registry: Path) -> Any:
    return migrate.plan_migration(
        legacy_layout_dir,
        tmp_path / "state",
        registry,
        _NOW,
        False,
        migrate.mint_tab_id,
    )


@pytest.mark.parametrize(
    ("ref", "address"),
    [
        ("chat:agent-aaa", _CHAT_AAA),
        ("terminal:terminal-1", _TERMINAL_1),
        ("service:browser?session=browser-1", _BROWSER_1),
        ("service:files?instance=files-2", _FILES_2),
        ("service:notes?instance=notes-3", "app:notes?instance=notes-3"),
        ("service:docs", _DOCS),
        ("service:browser", None),
        ("service:files", None),
        ("url:abcd1234", None),
        ("subagent:s1", None),
        ("chat-terminal:x", None),
        ("terminal:bad.name", None),
        ("chat:has space", None),
        ("service:Not-An-App", None),
        ("service:auth", None),
        ("service:agent-1", None),
        ("service:browser?tab=1", None),
        ("", None),
    ],
)
def test_address_for_ref_maps_every_old_spelling(ref: str, address: str | None) -> None:
    assert migrate.address_for_ref(ref) == address


@pytest.mark.parametrize(
    ("params", "ref"),
    [
        ({"panelType": "chat", "agentId": "agent-aaa"}, "chat:agent-aaa"),
        (
            {"panelType": "chat", "agentId": "agent-aaa", "chatAgentId": "agent-ccc"},
            "chat:agent-ccc",
        ),
        ({"panelType": "chat"}, None),
        ({"panelType": "launcher", "agentId": "agent-aaa"}, None),
        (
            {"panelType": "iframe", "serviceName": "browser", "url": "/?session=b-1"},
            "service:browser?session=b-1",
        ),
        # Only the browser named its instance by a session parameter.
        (
            {"panelType": "iframe", "serviceName": "docs", "url": "/?session=b-1"},
            "service:docs",
        ),
    ],
)
def test_ref_for_panel_reads_the_old_panel_shapes(
    params: dict[str, Any], ref: str | None
) -> None:
    assert migrate.ref_for_panel(params) == ref


def test_plan_files_members_and_docked_panels_and_drops_dead_refs(
    legacy_layout_dir: Path, tmp_path: Path, migration_registry: Path
) -> None:
    plan = _plan(legacy_layout_dir, tmp_path, migration_registry)

    project_1, research = plan.projects
    assert project_1.document["tabs"] == [
        _CHAT_AAA,
        _TERMINAL_1,
        _BROWSER_1,
        _FILES_2,
        _DOCS,
    ]
    assert project_1.dropped_members == (
        "url:abcd1234",
        "subagent:s1",
        "terminal:bad.name",
    )
    assert research.document["tabs"] == ["app:chat?instance=agent-bbb"]
    # The old sessionless files viewer is neither a tab nor a pin.
    assert research.dropped_members == ("service:files",)
    # A hand-edited entry falls back to the display defaults.
    assert research.document["color"] == migrate.DEFAULT_PROJECT_COLOR
    assert research.document["glyph"] == migrate.DEFAULT_PROJECT_GLYPH
    assert project_1.document["glyph"] == 3


def test_read_legacy_projects_stores_a_padded_color_trimmed() -> None:
    meta = {"project_by_id": {"padded": {"name": "Padded", "color": " #abcdef "}}}

    (project,) = migrate.read_legacy_projects(meta, [])

    assert project.color == "#abcdef"


def test_plan_derives_shortcuts_from_overrides_pins_and_the_registry(
    legacy_layout_dir: Path, tmp_path: Path, migration_registry: Path
) -> None:
    plan = _plan(legacy_layout_dir, tmp_path, migration_registry)

    project_1, research = plan.projects
    # The browser row was unpinned, chat's mode was flipped, docs is a single-instance pin whose
    # mode was flipped, notes has instances and pins its first action.
    assert project_1.document["shortcuts"] == [
        {"app": "chat", "action": "new", "mode": "focus"},
        {"app": "terminal", "action": "new", "mode": "focus"},
        {"app": "files", "action": "new", "mode": "focus"},
        {"app": "docs", "action": "open", "mode": "new"},
        {"app": "notes", "action": "new", "mode": "focus"},
    ]
    # The legacy unpinned list still counts, and the bare ``service:files`` member does not
    # pin the row back.
    assert [shortcut["app"] for shortcut in research.document["shortcuts"]] == [
        "chat",
        "terminal",
        "browser",
    ]


_TWO_ACTIONS = [{"id": "new"}, {"id": "note"}]


@pytest.mark.parametrize(
    ("row", "mode_override", "shortcut"),
    [
        # An app the registry does not list counts as single-instance: the synthesized open.
        (None, None, {"app": "notes", "action": "open", "mode": "focus"}),
        (
            {"name": "notes", "instances": False},
            None,
            {"app": "notes", "action": "open", "mode": "focus"},
        ),
        # The member's own mode override wins over the default mode.
        (
            {"name": "notes", "instances": False},
            "new",
            {"app": "notes", "action": "open", "mode": "new"},
        ),
        # An app with instances pins the registry's default_shortcut, mode included, over its
        # first declared action.
        (
            {
                "name": "notes",
                "instances": True,
                "default_shortcut": {"action": "note", "mode": "new"},
                "actions": _TWO_ACTIONS,
            },
            None,
            {"app": "notes", "action": "note", "mode": "new"},
        ),
        (
            {"name": "notes", "instances": True, "actions": _TWO_ACTIONS},
            None,
            {"app": "notes", "action": "new", "mode": "focus"},
        ),
        # A row declaring no action, or none the shell would read back (one bad shortcut
        # would cost the shell the whole projects file), drops the pin.
        ({"name": "notes", "instances": True}, None, None),
        (
            {"name": "notes", "instances": True, "actions": [{"id": "Not Valid"}]},
            None,
            None,
        ),
    ],
)
def test_derive_shortcuts_maps_a_pin_through_its_registry_row(
    row: dict[str, Any] | None,
    mode_override: str | None,
    shortcut: dict[str, str] | None,
) -> None:
    project = migrate.LegacyProject(
        project_id="p",
        name="P",
        color=migrate.DEFAULT_PROJECT_COLOR,
        glyph=migrate.DEFAULT_PROJECT_GLYPH,
        members=("service:notes",),
        override_by_shortcut_id={}
        if mode_override is None
        else {"app:notes": {"mode": mode_override}},
    )

    shortcuts = migrate.derive_shortcuts(project, [] if row is None else [row])

    pinned = [candidate for candidate in shortcuts if candidate["app"] == "notes"]
    assert pinned == ([] if shortcut is None else [shortcut])


def test_plan_prunes_panels_that_map_to_nothing_and_skips_empty_views(
    legacy_layout_dir: Path, tmp_path: Path, migration_registry: Path
) -> None:
    plan = _plan(legacy_layout_dir, tmp_path, migration_registry)

    by_key = {(seed.view_id, seed.device): seed for seed in plan.seeds}
    desktop = by_key[("project-1", "desktop")]
    assert desktop.addresses == (_CHAT_AAA, _TERMINAL_1, _BROWSER_1, _FILES_2, _DOCS)
    assert desktop.dropped_panel_ids == ("iframe-url-1", "subagent-s1", "new-tab-1")
    assert not desktop.is_skipped
    assert by_key[("project-1", "mobile")].addresses == (_CHAT_AAA,)
    # Everything showed only an ad-hoc page, so it has no seed; the corrupt mobile file costs
    # that seed and nothing else.
    everything = by_key[("everything", "desktop")]
    assert everything.is_skipped and everything.layout is None
    research_mobile = by_key[("research", "mobile")]
    assert research_mobile.is_skipped and "unreadable" in research_mobile.note
    assert not by_key[("research", "desktop")].is_skipped
    assert any("research.mobile.json" in note for note in plan.notes)


def test_migrate_layout_content_points_a_pruned_active_group_at_a_surviving_one() -> (
    None
):
    # The focused group held only the launcher, which maps to nothing; the seed must not name
    # a group the grid no longer has.
    launcher_params = {"panelType": "launcher", "agentId": "agent-primary"}
    chat_params = {"panelType": "chat", "chatAgentId": "agent-aaa", "title": "Planning"}
    content = {
        "dockview": {
            "grid": {
                "root": {
                    "type": "branch",
                    "data": [
                        {
                            "type": "leaf",
                            "data": {
                                "views": ["new-tab-1"],
                                "activeView": "new-tab-1",
                                "id": "g-launcher",
                            },
                            "size": 600,
                        },
                        {
                            "type": "leaf",
                            "data": {
                                "views": ["chat-agent-aaa"],
                                "activeView": "chat-agent-aaa",
                                "id": "g-chat",
                            },
                            "size": 600,
                        },
                    ],
                    "size": 800,
                },
                "width": 1200,
                "height": 800,
                "orientation": "HORIZONTAL",
            },
            "panels": {
                "new-tab-1": {"id": "new-tab-1", "params": launcher_params},
                "chat-agent-aaa": {"id": "chat-agent-aaa", "params": chat_params},
            },
            "activeGroup": "g-launcher",
        },
        "panelParams": {"new-tab-1": launcher_params, "chat-agent-aaa": chat_params},
    }

    migrated = migrate.migrate_layout_content(
        content, "desktop", {}, _NOW, migrate.mint_tab_id
    )

    assert migrated.record is not None
    assert migrated.dropped_panel_ids == ("new-tab-1",)
    assert migrated.record["dockview"]["activeGroup"] == "g-chat"
    assert [
        group["data"]["id"]
        for group in migrated.record["dockview"]["grid"]["root"]["data"]
    ] == ["g-chat"]


def test_run_writes_seeds_the_shell_reads_and_the_editor_can_edit(
    legacy_layout_dir: Path, tmp_path: Path, migration_registry: Path
) -> None:
    code, state_dir, _ = _run(legacy_layout_dir, tmp_path, migration_registry)

    assert code == 0
    store = LayoutStore(state_directory=state_dir)
    seed = store.read_layout("project-1", "never-seen-client", DeviceKind.DESKTOP)
    assert seed.dockview is not None
    assert seed.updated_at is not None and seed.updated_at.isoformat() == _NOW
    assert {tab.address for tab in seed.tabs.values()} == {
        _CHAT_AAA,
        _TERMINAL_1,
        _BROWSER_1,
        _FILES_2,
        _DOCS,
    }
    # Panel ids are fresh tab ids, used consistently in the grid, the panel entries, and the
    # tab records; the entries carry the frontend's current shape.
    for panel_id, tab in seed.tabs.items():
        assert _TAB_ID.fullmatch(panel_id) and tab.tab_id == panel_id
        entry = seed.dockview["panels"][panel_id]
        assert entry["contentComponent"] == "instance"
        assert entry["tabComponent"] == "custom"
        assert entry["params"] == {
            "kind": "instance",
            "address": str(tab.address),
            "tabId": panel_id,
        }
    groups = seed.dockview["grid"]["root"]["data"]
    assert [len(group["data"]["views"]) for group in groups] == [2, 3]
    assert all(
        _TAB_ID.fullmatch(view) for group in groups for view in group["data"]["views"]
    )
    assert seed.dockview["activeGroup"] == "g1"
    # Titles and recency carried over: the custom title wins, the last-used stamp lands on the tab.
    files_panel = panel_id_for_address(seed, Address(_FILES_2))
    assert files_panel is not None
    assert seed.dockview["panels"][files_panel]["title"] == "My notes"
    chat_panel = panel_id_for_address(seed, Address(_CHAT_AAA))
    assert (
        chat_panel is not None
        and seed.tabs[chat_panel].last_focused_ms == 1700000000000
    )
    assert seed.tabs[files_panel].last_focused_ms == 1700000001000
    # The seed is a document the shell's editor accepts: a split beside the chat lands in a new group.
    edited = add_panel(
        seed,
        Address("app:notes?instance=notes-1"),
        mint_tab_id(),
        "Notes 1",
        Placement(
            anchor_panel_id=chat_panel,
            direction=Direction.BELOW,
            ratio=0.5,
            is_new_group=True,
            group_id="split-group",
        ),
    )
    assert edited.dockview is not None
    assert (
        panel_id_for_address(edited, Address("app:notes?instance=notes-1")) is not None
    )
    # The mobile seed and the other project's seed exist; the empty and corrupt ones do not.
    assert (state_dir / "layouts" / "project-1" / "seed.mobile.json").exists()
    assert (state_dir / "layouts" / "research" / "seed.desktop.json").exists()
    assert not (state_dir / "layouts" / "research" / "seed.mobile.json").exists()
    assert not (state_dir / "layouts" / "everything").exists()
    # A client of the other device kind starts from its own seed, not the desktop's.
    mobile = store.read_layout("project-1", "never-seen-client", DeviceKind.MOBILE)
    assert [str(tab.address) for tab in mobile.tabs.values()] == [_CHAT_AAA]


def test_run_writes_projects_the_shell_reads(
    legacy_layout_dir: Path, tmp_path: Path, migration_registry: Path
) -> None:
    _, state_dir, _ = _run(legacy_layout_dir, tmp_path, migration_registry)

    projects = ProjectStore(state_directory=state_dir).list_projects()
    assert [project.id for project in projects] == ["project-1", "research"]
    assert [str(address) for address in projects[0].tabs] == [
        _CHAT_AAA,
        _TERMINAL_1,
        _BROWSER_1,
        _FILES_2,
        _DOCS,
    ]
    assert [
        (str(s.app), str(s.action), s.mode.value) for s in projects[0].shortcuts
    ] == [
        ("chat", "new", "focus"),
        ("terminal", "new", "focus"),
        ("files", "new", "focus"),
        ("docs", "open", "new"),
        ("notes", "new", "focus"),
    ]
    assert projects[1].name == "Research"
    # The old last-active pointer is dropped: the document carries only what the shell reads.
    document = json.loads((state_dir / "projects.json").read_text())
    assert set(document) == {"version", "projects"}


def test_run_seeds_the_files_and_terminal_stores_their_apps_read(
    legacy_layout_dir: Path, tmp_path: Path, migration_registry: Path
) -> None:
    _, _, apps_dir = _run(legacy_layout_dir, tmp_path, migration_registry)

    (record,) = build_files_source(
        apps_dir / "files" / "instances.json"
    ).list_instances()
    assert str(record.key) == "files-2"
    assert str(record.url) == "/data/notes?sort=name"
    assert str(record.title) == "File Viewer 2"
    assert record.lifetime is InstanceLifetime.REFERENCED
    assert (
        record.last_active is not None
        and record.last_active.isoformat() == "2023-11-14T22:13:21+00:00"
    )
    (terminal,) = JsonTerminalSessionStore(
        store_path=apps_dir / "terminal" / "instances.json"
    ).list_records()
    assert str(terminal.name) == "terminal-1"
    assert terminal.title is not None and str(terminal.title) == "Build log"
    assert terminal.workdir is None


@pytest.mark.parametrize(
    ("location", "url"),
    [
        ("/data/notes", "/data/notes"),
        (" /data/{tab} ", "/data/{tab}"),
        ("/data/{tab}/{tab}", "/"),
        ("//host/path", "/"),
        ("relative", "/"),
        ("/bad\nline", "/"),
    ],
)
def test_files_record_keeps_only_a_location_the_files_app_reads_back(
    location: str, url: str
) -> None:
    ref = "service:files?instance=files-2"
    record = migrate.files_record("files-2", {ref: location}, {}, _NOW)
    assert record["url"] == url


@pytest.mark.parametrize(
    ("last_used_ms", "last_active"),
    [
        (1700000001000, "2023-11-14T22:13:21+00:00"),
        # A stamp ahead of the migration time reads as the migration time, as the old store
        # read one ahead of its clock; so does one no clock could have produced.
        (1800000000000, _NOW),
        (10**30, _NOW),
        (0, _NOW),
    ],
)
def test_files_record_reads_a_stamp_ahead_of_the_migration_as_the_migration_time(
    last_used_ms: int, last_active: str
) -> None:
    ref = "service:files?instance=files-2"
    record = migrate.files_record("files-2", {}, {ref: last_used_ms}, _NOW)
    assert record["last_active"] == last_active


def test_run_keeps_a_stores_own_record_and_leaves_an_unreadable_store_alone(
    legacy_layout_dir: Path, tmp_path: Path, migration_registry: Path
) -> None:
    terminal_store = tmp_path / "apps" / "terminal" / "instances.json"
    terminal_store.parent.mkdir(parents=True)
    existing = {
        "version": 1,
        "sessions": [{"name": "terminal-1", "title": "Mine", "workdir": "/data"}],
    }
    terminal_store.write_text(json.dumps(existing))
    files_store = tmp_path / "apps" / "files" / "instances.json"
    files_store.parent.mkdir(parents=True)
    files_store.write_text("{corrupt")

    _run(legacy_layout_dir, tmp_path, migration_registry)

    # The record the user already has wins; a store that cannot be read is left alone.
    assert json.loads(terminal_store.read_text()) == existing
    assert files_store.read_text() == "{corrupt"


def test_run_leaves_a_store_of_another_version_alone(
    legacy_layout_dir: Path, tmp_path: Path, migration_registry: Path
) -> None:
    terminal_store = tmp_path / "apps" / "terminal" / "instances.json"
    terminal_store.parent.mkdir(parents=True)
    terminal_store.write_text('{"version": 2, "sessions": []}')
    files_store = tmp_path / "apps" / "files" / "instances.json"
    files_store.parent.mkdir(parents=True)
    files_store.write_text('{"version": 0, "instances": []}')

    _run(legacy_layout_dir, tmp_path, migration_registry)

    # Neither gains the record it would otherwise get, nor is re-stamped.
    assert terminal_store.read_text() == '{"version": 2, "sessions": []}'
    assert files_store.read_text() == '{"version": 0, "instances": []}'


def test_run_writes_the_marker_and_a_second_run_changes_nothing(
    legacy_layout_dir: Path, tmp_path: Path, migration_registry: Path
) -> None:
    _, state_dir, _ = _run(legacy_layout_dir, tmp_path, migration_registry)
    marker = json.loads((state_dir / "migrated.json").read_text())
    assert marker == {
        "version": 1,
        "migrated_at": _NOW,
        "source": str(legacy_layout_dir),
    }
    projects_path = state_dir / "projects.json"
    projects_path.write_text('{"version": 1, "projects": []}')

    code, _, _ = _run(legacy_layout_dir, tmp_path, migration_registry)

    assert code == 0
    assert json.loads(projects_path.read_text()) == {"version": 1, "projects": []}


def test_force_rewrites_the_projects_and_seeds(
    legacy_layout_dir: Path, tmp_path: Path, migration_registry: Path
) -> None:
    _, state_dir, _ = _run(legacy_layout_dir, tmp_path, migration_registry)
    (state_dir / "projects.json").write_text('{"version": 1, "projects": []}')
    seed_path = state_dir / "layouts" / "project-1" / "seed.desktop.json"
    seed_path.write_text(
        '{"dockview": null, "tabs": {}, "device_kind": "desktop", "updated_at": null}'
    )

    _run(legacy_layout_dir, tmp_path, migration_registry, "--force")

    assert len(ProjectStore(state_directory=state_dir).list_projects()) == 2
    assert json.loads(seed_path.read_text())["dockview"] is not None


def test_existing_new_model_state_is_kept_without_force(
    legacy_layout_dir: Path, tmp_path: Path, migration_registry: Path
) -> None:
    state_dir = tmp_path / "state"
    (state_dir / "layouts" / "project-1").mkdir(parents=True)
    kept_projects = {
        "version": 1,
        "projects": [
            {
                "id": "mine",
                "name": "Mine",
                "color": "#112233",
                "glyph": 1,
                "tabs": [],
                "shortcuts": [],
            }
        ],
    }
    (state_dir / "projects.json").write_text(json.dumps(kept_projects))
    kept_seed = (
        '{"dockview": null, "tabs": {}, "device_kind": "desktop", "updated_at": null}'
    )
    (state_dir / "layouts" / "project-1" / "seed.desktop.json").write_text(kept_seed)

    plan = _plan(legacy_layout_dir, tmp_path, migration_registry)
    _run(legacy_layout_dir, tmp_path, migration_registry)

    assert plan.is_projects_skipped
    assert json.loads((state_dir / "projects.json").read_text()) == kept_projects
    assert (
        state_dir / "layouts" / "project-1" / "seed.desktop.json"
    ).read_text() == kept_seed
    # The seeds the workspace did not have are still written, and so is the marker.
    assert (state_dir / "layouts" / "project-1" / "seed.mobile.json").exists()
    assert (state_dir / "migrated.json").exists()


def test_an_unreadable_projects_file_is_kept_and_reported(
    legacy_layout_dir: Path, tmp_path: Path, migration_registry: Path
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "projects.json").write_text("{corrupt")

    plan = _plan(legacy_layout_dir, tmp_path, migration_registry)
    _run(legacy_layout_dir, tmp_path, migration_registry)

    assert plan.is_projects_skipped and "cannot be read" in plan.projects_note
    assert any("projects.json" in note for note in plan.notes)
    assert (state_dir / "projects.json").read_text() == "{corrupt"
    assert (state_dir / "layouts" / "project-1" / "seed.desktop.json").exists()
    assert (state_dir / "migrated.json").exists()


def test_a_missing_old_store_writes_only_the_marker(
    tmp_path: Path, migration_registry: Path
) -> None:
    code, state_dir, apps_dir = _run(tmp_path / "nowhere", tmp_path, migration_registry)

    assert code == 0
    assert sorted(path.name for path in state_dir.iterdir()) == ["migrated.json"]
    assert not apps_dir.exists()


def test_an_unreadable_old_registry_is_reported_and_writes_only_the_marker(
    legacy_layout_dir: Path,
    tmp_path: Path,
    migration_registry: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (legacy_layout_dir / "projects_meta.json").write_text("{corrupt")

    code, state_dir, apps_dir = _run(legacy_layout_dir, tmp_path, migration_registry)

    assert code == 0
    err = capsys.readouterr().err
    assert "skipped unreadable" in err
    assert "no readable projects_meta.json" in err
    assert sorted(path.name for path in state_dir.iterdir()) == ["migrated.json"]
    assert not apps_dir.exists()


def test_plan_json_describes_the_run_without_writing(
    legacy_layout_dir: Path,
    tmp_path: Path,
    migration_registry: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, state_dir, apps_dir = _run(
        legacy_layout_dir,
        tmp_path,
        migration_registry,
        command="plan",
        command_args=("--json",),
    )

    assert code == 0
    assert not state_dir.exists() and not apps_dir.exists()
    printed = json.loads(capsys.readouterr().out)
    assert printed["is_source_present"] and not printed["is_already_migrated"]
    assert [project["id"] for project in printed["projects"]] == [
        "project-1",
        "research",
    ]
    assert printed["projects"][0]["dropped_members"] == [
        "url:abcd1234",
        "subagent:s1",
        "terminal:bad.name",
    ]
    assert printed["files"] == [{"key": "files-2", "url": "/data/notes?sort=name"}]
    assert printed["terminals"] == [{"name": "terminal-1", "title": "Build log"}]
    assert any(
        seed["view_id"] == "everything" and seed["is_skipped"]
        for seed in printed["seeds"]
    )


def test_plan_text_describes_the_run_without_writing(
    legacy_layout_dir: Path,
    tmp_path: Path,
    migration_registry: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, state_dir, apps_dir = _run(
        legacy_layout_dir, tmp_path, migration_registry, command="plan"
    )

    assert code == 0
    assert not state_dir.exists() and not apps_dir.exists()
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == f"source: {legacy_layout_dir} (present)"
    assert "project project-1: 5 tab(s), 5 shortcut(s)" in lines
    assert "  dropped member url:abcd1234" in lines
    assert "seed project-1 desktop: 5 tab(s); 3 panel(s) dropped" in lines
    assert (
        "seed everything desktop: skipped (no panel maps to an instance); 1 panel(s) dropped"
        in lines
    )
    assert "files: 1 record(s); terminals: 1 record(s)" in lines
    assert any(
        line.startswith("note: ") and "research.mobile.json" in line for line in lines
    )


def test_the_source_comes_from_the_mngr_environment(tmp_path: Path) -> None:
    environ = {
        "MNGR_HOST_DIR": str(tmp_path / "host"),
        "MNGR_AGENT_ID": "agent-primary",
    }

    assert migrate.legacy_layout_dir_from_env(environ) == (
        tmp_path / "host" / "agents" / "agent-primary" / "workspace_layout"
    )
    assert migrate.legacy_layout_dir_from_env({"MNGR_HOST_DIR": str(tmp_path)}) is None
    assert (
        migrate.main(["--state-dir", str(tmp_path / "state"), "run"], environ={}) == 0
    )
    assert not (tmp_path / "state").exists()


def test_another_agents_store_is_found_when_the_environments_agent_has_none(
    legacy_layout_dir: Path, tmp_path: Path, migration_registry: Path
) -> None:
    # The update apply runs as a chat agent, whose own state directory never held a store;
    # the services agent's store is the one to migrate.
    environ = {"MNGR_HOST_DIR": str(tmp_path / "host"), "MNGR_AGENT_ID": "agent-chat"}
    assert migrate.legacy_layout_dir_from_env(environ) == legacy_layout_dir

    code, state_dir, _ = _run(None, tmp_path, migration_registry, environ=environ)

    assert code == 0
    assert len(ProjectStore(state_directory=state_dir).list_projects()) == 2
    marker = json.loads((state_dir / "migrated.json").read_text())
    assert marker["source"] == str(legacy_layout_dir)


def test_several_old_stores_are_ambiguous_and_leave_the_workspace_unmarked(
    legacy_layout_dir: Path,
    tmp_path: Path,
    migration_registry: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    other_layout_dir = tmp_path / "host" / "agents" / "agent-other" / "workspace_layout"
    other_layout_dir.mkdir(parents=True)
    (other_layout_dir / "projects_meta.json").write_text("{}")
    environ = {"MNGR_HOST_DIR": str(tmp_path / "host"), "MNGR_AGENT_ID": "agent-chat"}

    assert migrate.legacy_layout_dir_from_env(environ) is None
    code, state_dir, _ = _run(None, tmp_path, migration_registry, environ=environ)

    assert code == 0
    assert "several agents hold an old layout store" in capsys.readouterr().err
    assert not state_dir.exists()
    # The environment's own agent still wins when it holds a store itself.
    own_environ = {**environ, "MNGR_AGENT_ID": "agent-primary"}
    assert migrate.legacy_layout_dir_from_env(own_environ) == legacy_layout_dir
