# Projects UI overhaul — implementation plan

## Overview

- Introduce a "Projects" concept to the `system_interface` default workspace
  template: named groupings of tabs, each with its own independent dockview
  layout.
- Tabs (Chat, File viewer, Browser, Terminal, App) are global, first-class
  entities with content that exists independently of any project.
- A Project is a `(layout, membership filter)` pair over tabs, not a
  container that owns them — deleting a project never deletes the tabs in
  it.
- Regular tabs (Chat/File viewer/Browser/Terminal) belong to exactly one
  project by default. Apps are the sole exception: because an App tab is
  just a dockview panel pointing at a singleton running service, the same
  app can be opened as a second panel in another project via a machine-wide
  "All apps" picker, with no content duplication.
- "Everything" is a real, stored, default project whose membership is
  implicitly "every tab that exists" (auto-populated on tab creation), with
  its own independently arrangeable layout — not a synthesized/read-only
  mirror of other projects.
- Navigation is via a new left icon-rail sidebar (collapsed by default,
  expands on hover) and a top-left project picker dropdown decorated with a
  colorful squiggle, both modeled directly on the reference prototype at
  https://imbue-ai.github.io/mind-sketches/prototypes/minds-dockview/.
- This entirely replaces the existing named-layout mechanism
  (`workspace_layouts.py` / `layouts/<slug>.json`) rather than reusing or
  renaming it — Projects get their own storage and metadata.
- This is a template-only change (`apps/minds/system`, developed via the
  `.external_worktrees/default-workspace-template` worktree). Already
  deployed VMs/workspaces are unaffected; only newly created workspaces pick
  up Projects, so no migration path is required.
- Per-tab controls (all tab types) gain: open in new window, refresh, and a
  Share dropdown, extending the existing `createCustomTab()` action buttons.
  Destroy becomes confirm-gated and explicitly cross-project. There is no
  pre-existing "share-workspace-tab" feature in this repo's history — that
  reference in the original task brief was incorrect; Share today is only
  an informational modal pointing at the separate, workspace-wide
  `share-gateway` service.

## Expected behavior

- On first load, a new workspace starts in the "Everything" project, showing
  every tab that exists across the workspace, arranged in its own layout.
- The left sidebar is a narrow icon rail by default; hovering it expands to
  show project names (each with an icon) and, below a separator, tab-type
  quick-add shortcuts (Chat / File viewer / Browser / Terminal).
- Clicking a project in the sidebar (or via the top project picker) switches
  the dockview surface to that project's own layout — panel positions are
  independent per project, so the same tab can sit in a different spot (or
  be entirely absent) in one project versus another.
- The top-left project picker shows the active project's name with a
  colorful squiggle decoration next to it, and opens a dropdown to switch or
  create projects.
- Creating a new tab (from a project's "+" or the sidebar quick-add) creates
  fresh content and adds it to the current project's layout and to
  Everything's layout. It does not appear in any other project.
- Opening an app in a second project uses a dedicated "All apps" picker
  (machine-wide list filtered to Apps, mirroring the prototype's "ON THIS
  MACHINE" + "Filter by type → Apps" pattern) — selecting an app adds a
  panel onto that same running service in the current project, without
  creating new content.
- Per-tab "Close" (the tab's "x") removes the tab from the current project's
  view only; the tab's content is untouched and remains visible in
  Everything and any other project it belongs to.
- Per-tab "Destroy" (a new item in the tab's action dropdown) opens a
  confirmation dialog stating that this removes the tab from all projects
  and tears down its live session, but that its transcript/content remains
  accessible afterward. Only on explicit confirm does it proceed.
- Per-tab "Refresh" and "Open in new window" work uniformly across all tab
  types (today Refresh only exists for non-terminal iframe tabs; this
  extends it everywhere it makes sense).
- Per-tab "Share" becomes a dropdown; its content/config surface is
  unchanged (still routes to the informational modal / `share-gateway`),
  just relocated behind a dropdown instead of a single button.
- Deleting a project removes its membership entries and layout, but every
  tab that was in it continues to exist and remains visible in Everything
  and any other project it belonged to.

## Implementation plan

### Backend — `system/apps/system_interface/imbue/system_interface/`

- **New `projects.py`** (sibling to, not built on, `workspace_layouts.py`):
  - Storage: `projects/<project-id>.json` — project metadata (name, color
    for the squiggle, created/updated timestamps) plus a membership+layout
    map `{tabRef: dockviewPosition}` for every tab that belongs to this
    project.
  - `projects_meta.json` — registry for project ordering/listing.
  - Reserved id `everything` — a real stored project; whenever any tab is
    created (in any project), a corresponding entry is also written into
    `everything`'s layout map (default position: appended).
  - Reuse the autosave-debounce and advisory-mutex patterns already proven
    in `layout_ops.py` for concurrent-write safety.
  - Error types mirroring `workspace_layouts.py`'s
    `LayoutNameError`/`LayoutConflictError`/`LayoutNotFoundError` shape,
    renamed to `Project*`.
- **`server.py`**: new REST endpoints —
  - `GET /api/projects`, `POST /api/projects` (create)
  - `GET /api/projects/<id>`, `POST /api/projects/<id>` (autosave layout)
  - `POST /api/projects/<id>/delete`
  - `POST /api/projects/<id>/tabs` — add/move/remove a tab within that
    project's layout map (used by tab creation, the All-apps picker, and
    drag/close interactions)
  - `GET /api/apps` (or extend an existing endpoint) — machine-wide list of
    running App-type tabs, for the "All apps" picker
  - Destroy endpoint gains a confirmation-required flag/response shape so
    the frontend can render the confirm dialog before the destructive call.
- **Remove** the named-layout endpoints and `workspace_layouts.py` outright
  from the template once Projects ships (no live migration needed per the
  template-only scoping above).

### Frontend — `system/apps/system_interface/frontend/src/`

- **`models/Projects.ts`** (new): CRUD for projects, tab-membership ops,
  active-project tracking. Mirrors `WorkspaceLayouts.ts` (API client shape)
  and `ClientIdentity.ts` (localStorage-backed per-browser active project,
  WS sync for live cross-client updates).
- **`views/Sidebar.ts`** (new): collapsed icon rail (`w-8`-style,
  `transition-[width]` hover-expand, matching the prototype's
  `.machine-sidebar`), with a projects section (top) and a tab-type
  quick-add section (bottom, Chat/File viewer/Browser/Terminal). Mounted in
  `App.ts` alongside `DockviewWorkspace`.
- **`views/ProjectPicker.ts`** (new): top-bar dropdown showing the active
  project's name plus the squiggle SVG (inline path lifted from the
  prototype: `viewBox="21.97 21.97 304.05 304.05"`, single wavy `<path>`,
  `stroke-width≈25.8`, no fill; stroke color per-project — confirm exact
  color-assignment rule during implementation), with create/switch actions.
- **`views/AllAppsPicker.ts`** (new): machine-wide, Apps-filtered tab
  picker for opening an already-running app into the current project
  (mirrors the prototype's "ON THIS MACHINE" list + "Filter by type"
  control).
- **`views/DockviewWorkspace.ts`** (modify): parameterize by active project
  id; on project switch, render that project's layout map (analogous to
  today's layout-swap-on-slug-change logic, against the new Projects API
  instead of `WorkspaceLayouts`). A tab closed/moved within one project's
  view must only touch that project's layout map — this applies to
  Everything too, since it's now a first-class layout, not a mirror.
- **`views/DockviewWorkspace.ts` `createCustomTab()`** (modify): add "open
  in new window" (`window.open` against the panel's resolved URL/ref);
  extend Share into a dropdown; add a confirm-gated Destroy flow (new
  confirmation dialog component, e.g. `views/DestroyConfirmDialog.ts`
  extension or a new sibling) explaining cross-project removal and
  transcript retention; ensure Refresh/new-window are available uniformly
  across Chat/Terminal/Browser/File viewer/App tab types.
- **`App.ts`** (modify): mount `Sidebar` and `ProjectPicker` alongside the
  existing `DockviewWorkspace`.

## Implementation phases

1. **Backend foundation** — `projects.py`, `projects_meta.json`, REST
   endpoints for plain (non-"everything") project CRUD + tab membership,
   with unit tests. No frontend changes.
2. **Frontend data wiring** — `Projects.ts` model; wire
   `DockviewWorkspace.ts` to project-scoped layout maps (create/switch/
   delete projects, add/remove tab from project). No sidebar/picker UI yet;
   driven by a temporary debug affordance to validate end-to-end.
3. **Sidebar** — icon rail, hover-expand, project list, tab-type quick-add
   shortcuts.
4. **Project picker** — dropdown + squiggle asset, create/switch actions.
5. **Everything project** — auto-membership on tab creation everywhere,
   confirm its layout is independently arrangeable and never a synthesized
   mirror.
6. **All-apps picker** — machine-wide Apps-filtered list; opening an app
   into the current project without duplicating content.
7. **Per-tab control extensions** — new-window, refresh parity across all
   tab types, Share-as-dropdown, confirm-gated cross-project Destroy.
8. **Cleanup** — delete the old named-layout mechanism
   (`workspace_layouts.py`, its endpoints, `LayoutDialog.ts`) from the
   template.

Each phase lands as its own PR with a changelog entry per the repo's
per-project changelog convention.

## Testing strategy

- **Backend unit tests** (`projects_test.py`, mirroring
  `workspace_layouts_test.py`'s structure): project CRUD, tab-membership
  add/move/remove, Everything auto-membership on tab creation, concurrent
  autosave/mutex behavior, error cases (invalid project id, deleting a
  project that doesn't exist, name conflicts).
- **Backend endpoint tests** (extend `server_test.py`): REST surface for
  all new `/api/projects*` and `/api/apps` endpoints, including the
  confirm-required shape on Destroy.
- **Frontend unit tests**: `Projects.ts` model logic (active-project
  tracking, localStorage persistence, WS sync handling).
- **Integration/acceptance tests**: switching between projects preserves
  independent layouts; closing a tab in one project doesn't remove it from
  Everything or other projects; destroying a tab removes it everywhere and
  the confirm dialog blocks accidental destruction; opening an app via
  All-apps in a second project doesn't duplicate its backing service;
  deleting a project doesn't delete its tabs.
- **Manual verification in-browser** (per this repo's manual-verification
  requirement) for each phase's UI: sidebar hover-expand behavior, project
  picker + squiggle rendering, drag/switch interactions, confirm dialog
  copy and behavior — exercised against a running dev instance of
  `system_interface`, not just unit tests.
- Existing `test_ratchets.py`/`test_embed_ratchets.py` in `system_interface`
  must continue to pass — no new raw `postMessage` usage (route any new
  embedder communication through `embed.ts`), no new ratchet violations
  introduced by the new frontend/backend modules.

## Resolved (confirmed by Preston)

- **Project settings**: each project gets a settings button opening a
  settings surface where the user picks the project's **name**, **color**,
  and **squiggle** (chosen from a set of squiggle shapes). Squiggle
  appearance is therefore user-selected per project, not derived from the
  project id — the model must persist all three fields, and a small library
  of squiggle path variants ships with the frontend.
- **All-apps picker scope**: shows **anything on the machine** — every app
  present, with no filtering against what's already in the current project.

## Open questions

- **Confirm-dialog copy**: exact wording for the Destroy confirmation
  (stating cross-project removal + transcript retention) — draft during
  Phase 7, review before shipping.
