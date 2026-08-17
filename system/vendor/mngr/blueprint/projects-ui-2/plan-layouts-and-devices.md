# Plan: fold layouts into views, add per-device arrangements

> Status: implemented (all five phases) on the default-workspace-template
> branch `preston/project-layout`.

The projects rework left two overlapping systems in `system_interface`. The old
one -- `workspace_layouts.py`, named layouts (`desktop` / `mobile`), five
`/api/layouts` CRUD endpoints -- has no callers left except a one-shot
migration and the agent API's target resolution. The new one -- `projects.py`
-- gives each view (project or Everything) a member list in
`projects_meta.json` and one arrangement in `projects/<id>.json`. Clients
report which view they are in and whether they are mobile or desktop, but
device selects nothing.

The seam between the two is broken in a user-visible way: a client reports its
view id as its "active layout", the old store rejects unknown slugs (logging
"Ignored last-active update" on every switch) and stays pinned at `desktop`
forever, so an agent layout op with no `--layout` resolves to a view no client
is ever on and fails with 412. And `layout.py load` broadcasts to zero
listeners: the CLI reports success while nothing switches.

## The model

One concept: **a view owns one member list and one arrangement per device.**

    workspace_layout/
      projects_meta.json          # name/color/glyph/members[], last_active_id
      projects/<id>.json          # desktop arrangement (existing file, unchanged)
      projects/<id>.mobile.json   # mobile arrangement (created on first mobile autosave)
      projects/everything.json    # Everything participates identically
      projects/everything.mobile.json

Device is chosen by the client, not stored in the registry: a mobile client
(`device_kind` is already derived from the user agent and reported per
connection) loads and autosaves the `.mobile` file, desktop the plain one. A
view with no mobile file yet gets the launcher on mobile -- the existing
fresh-view behavior, no seeding.

Ops broadcast to **clients on a view**; each connected client applies to its
live dock and autosaves into its own device file. An unmounted device
arrangement does not receive ops: arrangements are per-device presentations,
membership is the shared truth, and membership changes already reach every
view through the existing broadcasts.

## Phases

1. **Fix the default and the error.** Point the client-state write at
   `projects.set_last_active_id` (it already accepts Everything) and delete
   the dead `workspace_layouts.set_last_active_slug` call. The no-`--layout`
   default becomes: the single connected client's view when unambiguous, else
   `projects.get_last_active_id`. Kills the 412 and the warning spam.

2. **Wire `load`.** One layout-sync listener in DockviewWorkspace calling the
   already-exported `switchToView(viewId)`, filtered by target client id.
   `layout.py load <view>` then actually switches the client's view. The op's
   display-name lookup moves off the old store.

3. **Per-device arrangements.** `device` parameter on the project-content
   GET/POST (default desktop); the frontend routes load and autosave by its
   own `getDeviceKind()`; the destroy sweep and project delete strip both
   device files; `list` / `inspect` grow `--device` (default desktop).

4. **Retire `workspace_layouts.py`.** Inline the one load-bearing piece -- the
   one-shot migration chain (legacy `layout.json` -> `desktop` -> starter
   project content; old `mobile` layout -> starter project's `.mobile` file)
   -- into `projects.py`, then delete the module, its five `/api/layouts`
   endpoints (no frontend, minds-app, or layout.py callers), and their tests.
   Unify the client's duplicated active-view storage (the legacy layout-slug
   localStorage mirror folds into the project-id one).

5. **Agent surface.** `--layout` becomes `--view` (old spelling kept as a
   compat alias), the module docstring and op help speak projects natively,
   `context` reports each client's view and device, and `.agents/skills/`
   is swept for stale named-layout language.

## Settled decisions

- Named layout *targets* die with phase 4: `desktop` / `mobile` stop resolving
  as views (device is an attribute, not a view). The migration keeps their
  content.
- Ops do not seed the other device's file; membership is what both devices
  share.
- `load` is kept (wired), not removed: with views it is the natural "switch
  what the user is looking at" verb.
