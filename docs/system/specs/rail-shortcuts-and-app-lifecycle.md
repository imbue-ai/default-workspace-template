# Rail shortcuts and app lifecycle

Status: agreed design, pre-implementation.
This spec covers two related pieces of system_interface work: generalizing the rail's top section into configurable **shortcuts** (Part A), and giving apps an honest **lifecycle** -- identity separate from liveness, with Stop/Start replacing the deregister-flavored "Quit" (Part B).
The two parts are independently shippable but share vocabulary and one interaction (what a shortcut does when its backing app is stopped), so they are specified together.

Audience: implementers of `system/apps/system_interface/` (backend and frontend), `system/scripts/forward_port.py`, and the `build-app` / `update-app` skills.

Related reading: the system_interface README's "Projects" section (the verb set and rail as they are today), `frontend/src/views/objectMenu.ts` (the object verb set this spec extends), `.agents/skills/manage-projects/SKILL.md` (the agent-facing project/view layer).

## Background: the current state

Facts this design builds on, as of the projects follow-up (PR #451):

- **Everything is already multi-instance.**
  Terminals are tmux sessions behind one ttyd service, browsers are fleet sessions behind one daemon (addressed as `service:browser?session=<id>`), and chats are mngr agents behind the system interface.
  Plain apps can also host several panes at once; the layout permits two panels with the same ref.
  The only "singleton" behavior anywhere is UI policy: clicking an app row or shortcut focuses an existing pane instead of opening another.
- **`data/.state/apps.toml` is already durable identity, not just a port table.**
  `forward_port.py` upserts `{name, url, label, icon?, internal?}` when a program starts; rows persist when the program stops (only explicit `--remove` deletes), and `label` and `icon` deliberately survive re-registration so origins stay stable.
  What is missing is any notion of whether the app is *running*: nothing probes liveness, and every consumer treats row-presence as the whole story.
- **Registration is fused with program start.**
  Every supervised app's `[program:<name>]` block runs `forward_port.py --name <name> --url ... && <server>`, and `build-app` makes the supervisord program name equal the service name.
  `serve_isolated_instance.py` additionally registers unsupervised, throwaway instances (previews, update-app testing) and deregisters them itself.
- **The rail's top section is four built-in rows plus pinned apps.**
  The built-in rows (Chat, File Viewer, Browser, Terminal) go to what the view already shows and create only when it shows none; each can be unpinned per project into the All apps popover (`unpinned_shortcuts` on the project's registry entry, `POST /api/projects/<id>/shortcuts`).
  Pinning an app IS its project membership; pinned-app rows carry the object verb set from `objectMenu.ts`.
- **"Quit" on an app is deregistration.**
  `POST /api/apps/<name>/deregister` drops the registry row and the app's members; the program keeps running, and a supervised app re-registers on its next restart.
  This is the dishonest middle ground Part B removes.
- The machine-wide **member recency store** (`member_last_used`, surfaced as the launcher's last-active column) records when each member ref was last used.

## Part A: shortcuts

### The shortcut concept

Everything in the rail's top section is a **shortcut**: a per-project entry that starts or reveals something, distinct from the object rows in the tab list below.
Today there are two shortcut kinds: the four built-ins (chat, files, browser, terminal) and pinned apps (one shortcut per pinned app).
The design treats the section as one list of typed shortcut entries so that future kinds (a specific chat, a skill or automation, a URL) can be added without another remodel.

### Modes and labels

Every shortcut has a **mode**, which is per-project state:

- **Focus mode** (label "X", e.g. "Chat"): clicking goes to an existing X in the active view -- the most recently used one -- and creates a new X only when the view shows none.
  The focus-then-create shape is today's behavior; the most-recently-used resolution is new, deliberately replacing today's rule (the chat shortcut prefers the project's own chat -- the primary agent's under Everything -- and the other kinds take the view's first-listed member).
- **New mode** (label "New X", e.g. "New Chat"): clicking always creates a fresh X.

The row's label is derived from the mode; nothing else about the row changes.

"Most recently used" is resolved against the member recency store, restricted to the active view's members of that kind.
Under Everything the restriction is a no-op, so resolution is effectively machine-wide there.
When the view's members of the kind have no recency data, any of them may be focused; when the view shows none at all, focus mode creates a new one -- the same view-scoped rule the shortcuts follow today, unchanged.

Per kind, "create a new X" maps to the existing create paths:

- chat: the launcher's default claude new-chat create (same template stack, auto-minted name; the feature-flagged other-harness tiles are launcher-only and do not change what the shortcut creates).
- terminal: a new tmux session via the terminal allocator.
- browser: a new fleet session.
- app: a new pane on the app's service (see "Uniformity for apps" below).
- files: disabled until an app backs the File Viewer; the row keeps its current disabled treatment, and once a `files` service exists it behaves as an ordinary app-kind shortcut target.

Shortcut-initiated creates take the same in-flight guard the launcher tiles got in the projects follow-up: while a create this shortcut asked for is pending, the row stands down, so a second click cannot start a second object.

The New Tab launcher is unaffected by modes: its "Open new" tiles are always new -- that is what the launcher is for -- and its member/machine tables are always focus-or-file.

### The shortcut menu

Every shortcut row gains a kebab (and right-click) menu.
This is deliberately NOT the object verb set from `objectMenu.ts` -- a shortcut is not an object -- but a small shortcut-menu definition of its own, defined once and shared by all shortcut rows:

1. The **complementary action**: "New X" when the shortcut is in focus mode; "Focus last X" when it is in new mode.
   Both actions are therefore always reachable regardless of mode.
   "Focus last X" is disabled when the active view shows no X (offering a focus that would create is exactly the confusion the two modes exist to avoid); the File Viewer omits the complementary action entirely while no app backs it.
2. The **mode flip**: `Change shortcut to "New X"` (from focus mode) or `Change shortcut to "X"` (from new mode).
   Flipping persists the mode for this project and relabels the row.
3. **Unpin** (built-in rows only; the same act as the row's pin icon, and absent under Everything).
   Pinned-app rows do not need an unpin entry here: their object menu already carries "Remove from project", which for an app IS the unpin.

For pinned apps the row presents one menu: the object verb set as it is today, then a divider, then this shortcut group (complementary action, mode flip).

### Uniformity for apps

App shortcuts get the same mode machinery as the built-ins.
"New Docs" opens a second pane on the same service -- legitimate and occasionally exactly what the user wants (two folders of a file browser side by side).
Mechanically this is an open that skips the focus-existing dedup (`openAppTab` grows an explicit open-new path); the layout already tolerates two panels with one ref.
Known, accepted ambiguity: functions that resolve "the panel for this ref" (`panelIdForMemberRef`) keep returning the first match, so ref-addressed verbs (rail Refresh on a backgrounded row) act on one pane; the tab's own verbs are per-panel and unaffected.

### Defaults

Mode defaults are code-side, per kind:

- chat: **new mode** ("New Chat") -- the point is discoverability of multi-chat.
- files, browser, terminal, apps: focus mode.

Because defaults are code-side and no project has stored a mode yet, changing the chat default applies to every existing project with no migration.
A project that has explicitly flipped a shortcut keeps its choice.

### Storage

Per-project shortcut state generalizes the projects follow-up's `unpinned_shortcuts` list into a sparse overrides map on the project's entry in the `projects_meta.json` registry:

```json
"project_by_id": {
  "<id>": {
    "shortcut_overrides": {
      "chat": {"mode": "focus"},
      "terminal": {"is_pinned": false},
      "app:docs": {"mode": "new"}
    }
  }
}
```

- Keys are shortcut ids: a built-in name (`chat`, `files`, `browser`, `terminal`) or `app:<service-name>`.
- Each override stores only the fields that deviate from the defaults (`is_pinned`, defaulting true; `mode`, defaulting per kind).
  An absent entry means all defaults, so no migration is needed and the registry stays hand-edit tolerant (unknown keys and malformed values are dropped on read, like `_entry_unpinned_shortcuts` does today).
- App pinning itself remains membership (pinning IS the member ref, unchanged); `app:<name>` overrides carry only `mode`.
  An `is_pinned` field on an `app:` key is ignored.
- The legacy `unpinned_shortcuts` list is read as `{<name>: {is_pinned: false}}` when `shortcut_overrides` is absent, and the first write of any override rewrites the entry to the new shape and drops the legacy key.
  Mark the legacy-read branch with a `CLEANUP:` comment (removable once no supported workspace's registry predates this change).

Future shortcut kinds extend the same map with richer entries (a `kind` field plus kind-specific payload); the built-ins do not need one now because the key namespace already distinguishes them.

### Agent-facing surface

Shortcut configuration is not user-only state: agents may read and change it.
`system/scripts/layout.py` grows a `shortcuts` query (the active or named view's effective shortcut list: id, pinned, mode) and a `shortcut set` verb (`--pin/--unpin`, `--mode focus|new`) that calls the same endpoint the UI uses, so both writers share one validation path.
The `manage-projects` skill documents the new verbs alongside its existing membership coverage.
This ships with the storage phase (see Phasing), so the agent surface and the UI surface appear together.

### API

Extend the existing endpoint additively: `POST /api/projects/<id>/shortcuts` accepts `{shortcut, is_pinned?, mode?}` with at least one of the optional fields present.
`shortcut` accepts the built-in names (as today) and `app:<service-name>`.
Responses return the project's full effective override map so clients settle on one authoritative answer.
The endpoint keeps riding the `project_members_changed` broadcast, and (per the review-branch fix pattern) answers a soft no-op when no primary agent is configured.

### Everything

Everything has no project entry, so it shows every shortcut with the code-side defaults, unpinnable and mode-fixed, exactly as it is unpinnable today.
The shortcut menu under Everything offers only the complementary action.

## Part B: app lifecycle

### Identity vs. runtime state

The registry row is the app's identity; whether it is running is derived state, never stored.

`forward_port.py` grows one field:

- `program = "<supervisord program name>"` -- registered via a new `--program` flag.
  Like `internal` (and unlike the icon's tri-state), every registration call is authoritative: passing the flag sets the field, omitting it clears it, so a block that stops passing it cannot leave a stale capability behind.
  Its presence is the capability grant "this app can be stopped and started through supervisord."
  `build-app` passes it on both paths (scaffolded Flask and wrapped server); the `browser` service's own registration passes it too.
  `system_interface` and `terminal` do NOT set it, and the stop endpoint additionally refuses them by name (defense in depth for hand-edited registries).
  Isolated instances and previews never set it (they are unsupervised and own their teardown).

There is no `instances` or singleton field; multi-instance is universal and open behavior is shortcut policy (Part A).

### Essential services

The essential set is exactly `{system_interface, terminal}`: the shell that serves the UI, and the terminal service whose ttyd carries every terminal tab.
Everything else with a `program` -- the browser included -- is stoppable.

### Liveness

`AgentManager` (which already watches `apps.toml`) computes `is_running` per entry:

- Rows with `program`: supervisord's process state, read over its RPC interface on the configured socket (the same channel `supervisorctl` uses).
- Rows without `program`: a cheap TCP connect probe of the row's `url`, cached briefly.

`is_running` is carried on `AppEntry` over the existing WebSocket to the SPA.
UI: a not-running app renders dimmed wherever it appears (rail shortcut, tab list, All apps popover, launcher tables) with "Start" as its primary action; its tooltip distinguishes "stopped" (has `program`) from "not running (managed outside the workspace)" (no `program`).

### Stop and Start

New endpoints:

- `POST /api/apps/<name>/stop` and `POST /api/apps/<name>/start` -- supervisord RPC stop/start for the row's `program`.
  400 for a row without `program`; 400 for the essential set; 404 for an unknown name.
  Both are idempotent: stopping a stopped program and starting a running one answer success (supervisord's ALREADY_STARTED / NOT_RUNNING faults map to the no-op case, not to errors).
  Neither touches the registry row or the app's memberships: a stopped app stays listed, stays filed, stays shareable-later.

Stop is the **service** level, and it coexists with the **instance** level that already exists: Quit on a chat destroys that agent, Quit on a terminal kills that tmux session, and Quit on a browser session retires that one fleet browser.
The browser therefore has both levels -- Quit a single session (existing, unchanged) and Stop the whole fleet daemon (new).
Plain apps have no per-pane process, so their instance level is simply closing the pane (Close tab); Stop is their only process verb.

Verb changes:

- The app's destructive-slot verb in `objectMenu.ts` becomes **"Stop <name>"** (power icon, reversible), wired to the stop endpoint; a stopped app's menus offer **"Start <name>"** instead.
  Stop needs no confirmation dialog: it is reversible in one click, and the current Quit confirmation existed to explain irreversibility that no longer applies.
- **Deregister leaves the UI entirely.**
  Real removal (delete the supervisord block, the package, the registry row) is the mind's job via the `update-app` skill, matching the product stance that the agent maintains `system/`.
  The `/api/apps/<name>/deregister` endpoint remains available for agent tooling, though nothing currently depends on it: its only caller today is the UI verb this change removes, and `serve_isolated_instance.py down` deregisters through `forward_port.py --remove` directly.

Open app tabs of a stopped app show a dead iframe today; render a lightweight "stopped" placeholder with a Start button instead of the raw connection error (implementation may reuse the existing friendly-error page shape).

### Backfill

Existing workspaces have `program`-less rows for apps created before this ships.
No inference by name (that brittleness is what the rejected marker-comment approach warned about): a row gains `program` when its supervisord block's registration line is updated to pass `--program`, which `update-app` does on the app's next touch, and which the app's next restart applies.
Until then such apps simply show no Stop verb, which is today's behavior minus the dishonest Quit.
An optional env-converge step MAY rewrite known build-app blocks in bulk; decide during implementation based on how annoying the gap proves.

## Interactions between the parts

- Clicking a shortcut (either mode) whose backing app is stopped **starts it first**, shows the shortcut's pending state while supervisord brings it up and the port re-registers, then opens the pane.
- "Focus last X" ignores panes of stopped apps only in the sense that the app's rows are dimmed; the recency store itself is unaffected by stop/start.

## Phasing

Each phase is independently shippable, in order:

1. Registry `program` field, `forward_port.py --program`, `build-app` and browser-block updates.
2. Liveness in `AgentManager`, `AppEntry.is_running`, dimmed rendering and the stopped-tab placeholder.
3. Stop/Start endpoints, verb swap, UI deregister removal, README and verb-table updates.
4. Shortcut storage (`shortcut_overrides`, legacy read, API extension) plus the agent-facing surface (`layout.py` verbs, `manage-projects` skill update).
5. Shortcut modes, labels, the shortcut menu, per-kind "new" paths, the create guard, and the chat default flip.

## Out of scope

- Custom shortcut kinds (specific chat, skill, URL): schema accommodates them; nothing is built.
- Uninstall UI: deliberately excluded (see Part B).
- Multi-pane addressing improvements (per-pane member refs for apps): accepted as a known ambiguity.
- minds desktop client changes: none needed; stopped rows keep their labels so local and shared origins stay stable across stop/start.

## Implementation notes (verification owed, not open design)

1. **Browser session resume**: before Phase 3 enables Stop for the browser, verify against the fleet daemon that sessions resume cleanly on Start (Chromium profiles persist on disk) and that open browser tabs get the stopped-tab placeholder rather than a raw error meanwhile.
2. **supervisord RPC access**: confirm the socket path and permissions available to the system_interface process, and whether the RPC responds fast enough to sit on the liveness poll path or needs caching.
