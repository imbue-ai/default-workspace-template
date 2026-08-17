# Projects UI overhaul — implementation plan

Status: DRAFT — awaiting confirmation before implementation.

## Summary

Introduce a "Projects" concept to system_interface: named groupings of tabs,
each with its own tab layout, navigated via a new left icon-rail sidebar and
a top-left project picker. Replaces the existing named-layout mechanism
entirely (layouts are not reused/renamed — Projects are a new, separate
concept with their own storage and metadata).

Reference prototype: https://imbue-ai.github.io/mind-sketches/prototypes/minds-dockview/
Prior related work (superseded by this plan, not reused): `blueprint/dockview-named-layouts/plan-dockview-named-layouts.md`

## Confirmed requirements (from Preston + prototype study)

1. **Tabs are global, first-class entities with content** (a chat, a file
   viewer, a browser, a terminal, an app). A tab exists independently of any
   project.
2. **Projects are a (layout, membership filter) pair over tabs**, not
   containers that own tabs. Deleting a project must not delete the tabs in
   it — it only removes that project's membership + layout entries; other
   projects/Everything still see the tab.
2a. **A regular tab (Chat, File viewer, Browser, Terminal) belongs to exactly
   one project by default** — no arbitrary multi-project membership for these.
   **Apps are the one exception**: confirmed in the prototype's "New Tab"
   screen — the "ON THIS MACHINE" list has a "Filter by type" control with an
   `Apps` checkbox (alongside `Chats`/`Browsers`); this filtered, machine-wide
   list is the "All apps" affordance. Because an App tab is just a dockview
   panel pointing at a singleton running service (same `serviceName` backing
   today's `iframe` panels), opening that same app from the All-apps list into
   a second project simply creates a second panel onto the same service — no
   content duplication, and this is the *only* built-in way a tab-like thing
   ends up visibly "in" more than one project. Chat/Terminal/Browser tabs have
   no equivalent multi-project affordance in the prototype.
3. Tab types: Chat, File viewer, Browser, Terminal, or an agent-built App.
4. Left sidebar: icon-only rail, collapsed by default, expands into a full
   menu on hover (matches prototype `.machine-sidebar`, `w-8` collapsed
   width, `transition-[width]` on hover). Top section lists projects; bottom
   section lists tab-type quick-add shortcuts (Chat/Files/Browser/Terminal).
5. Project picker: dropdown at the very top, decorated with a colorful
   squiggle next to the project name. Confirmed live in the prototype: it's
   an inline SVG (no CSS class, `viewBox="21.97 21.97 304.05 304.05"`,
   single wavy `<path>`, `stroke="#F0603A"` in the "aurora" example,
   `stroke-width≈25.8`, no fill) rendered in a `<span class="flex w-5
   shrink-0 items-center justify-center">` next to the project name once a
   workspace is open, visible when the layout is in "project view" mode.
   Stroke color likely varies per project/workspace (deterministic from an
   id, matching the colored-dot avatars seen in the machine switcher) —
   confirm the exact color-assignment rule before implementing, but the path
   geometry itself can be lifted as-is.
6. Default project: **"Everything"** — the layout+filter pair where the
   filter is empty (no restriction), so it shows every tab that exists,
   still with its own independent layout/arrangement (Everything's layout is
   not derived from other projects' layouts — a tab can sit in a different
   position in Everything than it does in "Coding", just like any other
   project pair).
7. Per-tab controls (all tab types): open in new window, refresh, share (via
   dropdown). Existing per-tab Refresh/Share/Destroy/Close buttons in
   `createCustomTab()` are the closest current analog — Share today only
   opens an informational modal (real sharing is workspace-wide via a
   separate `share-gateway` service, not per-tab). No prior
   "share-workspace-tab" feature exists in this repo's history; that
   reference in the original task description was incorrect.

## Current architecture (for context — see full research notes for file:line detail)

- Frontend: Mithril (not React), module-level mutable state + `m.redraw()` +
  pub-sub listeners, no Redux/Zustand.
- Dockview: single `DockviewComponent` (dockview-core) in
  `system/apps/system_interface/frontend/src/views/DockviewWorkspace.ts`.
  Panel types: `chat | iframe | subagent`. Browser/Terminal/App are all
  `iframe` panels distinguished by `serviceName`/URL shape. Panels are
  addressed by stable, type-prefixed refs (`service:<name>`,
  `chat:<agent-name>`, `subagent:<session-id>`, `terminal:<hash>`,
  `url:<hash>`) — this ref scheme is exactly what a global Tab identity can
  be built on.
- Layout persistence today: named layouts, `layouts/<slug>.json` +
  `layouts_meta.json`, backend in
  `system/apps/system_interface/imbue/system_interface/workspace_layouts.py`,
  REST endpoints in `server.py`, UI via `LayoutDialog.ts`. **This plan
  replaces this mechanism** — a "layout" today is a full dockview grid for
  the whole workspace; Projects need per-tab membership on top of that,
  which this mechanism has no concept of.
- No sidebar exists today; `App.ts` renders only the dockview surface.
- Per-tab action buttons already exist in `createCustomTab()`
  (Refresh/Share/Destroy/Close) — extend rather than rebuild.
- This is a template change: `apps/minds/system` (the default workspace
  template, developed via the `.external_worktrees/default-workspace-template`
  worktree) is what's being modified. Existing deployed VMs/workspaces are
  unaffected — only newly created workspaces pick up Projects. No migration
  path is needed for already-running workspaces.

## Proposed data model

### Backend (new)
- **Tabs are the source of truth for content**, keyed by the existing
  panel-ref scheme (`service:*`, `chat:*`, `subagent:*`, `terminal:*`,
  `url:*`) — a tab's identity is its ref; no new content storage needed
  beyond what already backs each panel type today.
- New `projects.py` (sibling to `workspace_layouts.py`, not built on top of
  it): owns `projects/<project-id>.json` = project metadata (name, color for
  the squiggle, created/updated) + a **membership+layout map**: `{tabRef:
  dockviewPosition}` for every tab that's part of this project. Adding a tab
  to a project writes one entry; removing it deletes the entry (tab content
  itself is untouched). `projects_meta.json` registry for ordering/listing.
- **`everything` is a real, stored project** (not synthesized), except its
  membership filter is implicitly "all tabs that exist" rather than an
  explicit set — practically: whenever a tab is created anywhere, it also
  gets an entry auto-added to `everything`'s layout map (default
  position e.g. new-tab-at-end), same as it's explicitly added to whichever
  project the user created it from. This avoids needing dynamic
  aggregation/dedup logic at read time and keeps Everything's layout
  independently arrangeable, per Preston's confirmation that Everything
  "still [has] a layout."
- New REST endpoints mirroring the existing layout endpoints' shape:
  `GET/POST /api/projects`, `GET/POST /api/projects/<id>`,
  `POST /api/projects/<id>/delete`, plus tab-membership ops
  (`POST /api/projects/<id>/tabs` to add/move/remove a tab within that
  project's layout). Reuse the autosave-debounce and advisory-mutex patterns
  already proven in `layout_ops.py`.
- Old named-layout endpoints/`workspace_layouts.py` are deleted outright in
  the template (not deprecated-in-place) once Projects ships, since this is
  a template-only change with no live migration burden.

### Frontend (new)
- `frontend/src/models/Projects.ts` — CRUD for projects + tab-membership ops
  + active-project tracking (mirrors `WorkspaceLayouts.ts`/`ClientIdentity.ts`
  patterns: localStorage for per-browser active project, WS sync for live
  updates across clients).
- `frontend/src/views/Sidebar.ts` — new Mithril component: collapsed icon
  rail (hover-to-expand), projects section + tab-type quick-add section.
  Mounted in `App.ts` alongside `DockviewWorkspace`.
- `frontend/src/views/ProjectPicker.ts` — top-bar dropdown + squiggle
  decoration, using the confirmed SVG path/geometry above.
- `DockviewWorkspace.ts` changes: parameterize by active project id, render
  that project's layout map on switch (analogous to today's
  layout-swap-on-slug-change logic, against the new Projects API). A tab
  closed/moved within one project's view only touches that project's layout
  map, never other projects' — this must hold for Everything too now that
  it's a first-class layout and not a mirror.
- Tab creation flow: creating a new tab from any project adds it to that
  project's layout map **and** to Everything's.
- **Only Apps get a "add to another project" UI affordance** — a new
  "All apps" entry point (mirroring the prototype's "ON THIS MACHINE" list
  filtered to `Apps`) lets the user pick an already-running app and open it
  as a new panel in the current project, which just adds a second membership
  entry pointing at the same `service:*` ref. Chat/File viewer/Browser/
  Terminal tabs get no such affordance — they're created fresh per project
  and stay single-project (storage-wise the membership map *could* hold the
  same non-app ref in two projects, but the UI never offers to do that, so
  in practice it won't happen).
- `createCustomTab()` extension: add "open in new window" (likely
  `window.open` against the panel's resolved URL/ref) and extend the
  existing Share button to a dropdown (share modal stays the entry point
  for actual sharing config, which remains workspace-level in
  `share-gateway`).

## Confirmed: close vs. destroy semantics

- **Close** (existing "x" on the tab): removes the tab from the current
  project's view/layout only. The tab's content is untouched and it remains
  visible in Everything and any other project it belongs to.
- **Destroy** (new item in the per-tab dropdown, alongside Share/Refresh/etc):
  opens a confirmation dialog before acting — the dialog must state that this
  removes the tab from **all** projects and that its transcript/content
  remains accessible afterward (i.e. destroy tears down the live
  session/panel and all project memberships, but does not delete history).
  Only on confirm does it proceed; cancel leaves everything untouched. This
  replaces today's bare "Destroy" tab-action button (which already exists
  for chat/terminal/browser tabs in `createCustomTab()`) with a
  confirm-gated version, and extends it to all tab types uniformly since
  destroy is now a cross-project operation.

## Open design questions to resolve before/at build start

- **Squiggle color rule**: confirm whether stroke color is per-project
  (like the machine-switcher's colored dots) or fixed/theme-driven.

## Suggested build order

1. Backend: `projects.py` + tab-membership REST endpoints, with tests. No
   frontend changes yet.
2. Frontend: `Projects.ts` model + wire `DockviewWorkspace.ts` to
   project-scoped layout maps (create/switch/delete projects, tab
   add/remove-from-project), no sidebar UI yet — driven via a temporary
   debug affordance.
3. Sidebar component (icon rail + hover expand + project list + tab-type
   shortcuts).
4. Project picker (dropdown + squiggle, using the confirmed SVG asset).
5. Everything project wiring (auto-membership on tab creation, its own
   independent layout).
6. "All apps" interaction: machine-wide, `Apps`-filtered tab-picker (mirrors
   the prototype's "ON THIS MACHINE" list + "Filter by type" control) for
   opening an already-running app into the current project.
7. Per-tab control extensions: new-window, refresh (already exists, verify
   parity across all tab types incl. Chat/Terminal which don't have it
   today), share-as-dropdown, and the close-vs-destroy semantics split.
8. Delete old named-layout mechanism from the template.

Each phase should land as its own PR with changelog entries per the repo's
per-project changelog convention.
