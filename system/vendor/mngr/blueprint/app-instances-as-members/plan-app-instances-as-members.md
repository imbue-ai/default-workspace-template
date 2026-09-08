# App instances as first-class members

All work happens in the default-workspace-template checkout (`system/apps/system_interface/`, `system/apps/files/`, `system/scripts/`), on top of the `mngr/rail-shortcuts-and-app-lifecycle` branch.

## Refined prompt

App instances as first-class members in the default-workspace-template system_interface: numbered per-app instances minted by the backend, refs `service:<name>?instance=N`, listed in Everything and project tab lists like chats/terminals/browsers, shareable across projects, uniform Delete verb, location-beacon resume for the vendored dufs frontend. Replaces the anonymous UUID instance nonce added earlier on this branch; migrates main-era layouts only.

* Conceptual model: every tab-list object is an app instance; terminals/browsers/chats derive instances from backing state (tmux, fleet daemon, mngr), plain apps from what references them — an instance exists while any project's member list or any view's saved layout holds it, and ceases to exist when nothing does.
* Unification is minimal today: new code is ref-first and kind-agnostic ("instance source" seam with only the layouts-derived source implemented); the existing per-kind switches move into one dispatch table with their bodies unchanged; full unification of the three legacy kinds is deferred follow-up work (tracked in a GitHub issue).
* No new instance registry file: minting scans members + saved layouts plus an in-flight reservation set (terminal-allocator style) for the lowest free number machine-wide.
* Ref/name spelling: `service:files?instance=files-2` (full canonical instance name in the query, mirroring `service:browser?session=browser-2`; eases later renaming); display name "<app display name> 2" ("File Viewer 2", "Docs 2" for a renamed app).
* Pinning/shortcuts stay service-level and untouched; instances are what the tab lists hold.
* Instance menus carry the instance verbs (Refresh, Add to project..., Remove from project, Delete) plus a divider and the service group (Share, Stop/Start), so service verbs stay reachable from any instance, including under Everything.
* Everything: the tab list shows instances only (a zero-instance app has no tab-list row anywhere); its rail shortcut list shows every openable app by default — built-ins first in rail order, then apps alphabetically, rows fixed (no unpin/hide, since Everything has no registry entry) — so a newly added app appears there automatically; the All apps popover under Everything then shows its "already pinned here" empty state.
* Open surfaces: launcher "Open new" app tiles always mint a new instance; the launcher's "In this project"/"On this machine" tables list instances with per-instance recency; All apps popover click and a pinned app's `service:<name>` ref resolve MRU-instance-or-create; shortcut focus/new modes map to MRU-or-create / always-mint.
* Agents get symmetric verbs: `layout.py open <app> --new` mints an instance, and the existing close/remove/delete ops accept instance refs; documented in the manage-projects skill.
* Delete is uniform everywhere, confirmation included; instances of an app the machine no longer offers stay listed with Delete as the remaining verb; Delete clears the instance's title, recency, and stored location.
* Location beacon: the vendored dufs frontend posts its current path one hop up to the dockview shell on each page load; the shell validates the origin, resolves the pane by `event.source`, and stores the path machine-wide per instance ref; panes for an instance open at the service origin plus the stored path; the `build-app` scaffold adds the beacon one-liner so future apps opt in; non-beaconing apps always open at the origin.
* Per-instance rename and beacon adoption beyond dufs + the scaffold are explicit follow-ups (rename waits for the display-name addressing work terminals/browsers already defer to).
* Migration: from main-era layouts only — a legacy app pane adopts the app's lowest-free instance on first mount and files through the normal auto-membership path, `CLEANUP`-marked; the unreleased UUID `serviceInstanceId` nonce from this branch is removed, not shimmed.

## Overview

* Plain apps become multi-instance the way terminals and browsers already are: each open pane is a numbered, named, filed object (`files-1`, `files-2`) rather than an anonymous view of one service — restoring "one live page per object" with object = instance, and making the tab list behave identically across all kinds.
* An instance is registry-derived, not registry-stored: it exists while any project's member list or any view's saved layout references it. This matches the unifying principle that every kind derives its instances from *some* source — tmux, the fleet daemon, mngr agents, or (for stateless apps) the layouts themselves.
* The backend mints canonical names (`<app>-<N>`, lowest free number machine-wide) exactly as the terminal allocator does, and the ref grammar rides the browser's existing `service:<name>?<query>` plumbing.
* The vendored dufs frontend gains a location beacon (a one-hop `postMessage` to the dockview shell), so a files instance reopens in the folder it was viewing — the model's proof that instances are real objects, not labels.
* Scope is deliberately minimal on unification: new code is kind-agnostic and the existing per-kind switches consolidate into one dispatch table with their bodies unchanged; migrating chats/terminals/browsers onto the new seam is deferred follow-up.

## Expected behavior

* Clicking the File Viewer shortcut (focus mode) goes to the view's most recently used files instance, or creates `files-1` when the view shows none — exactly like Chat.
* "New File Viewer" (shortcut new mode, menu complementary action, launcher tile, or `layout.py open files --new`) mints the next free instance machine-wide and opens it; the in-flight create guard applies.
* Every instance is a tab-list row with a derived display name ("File Viewer 2"), a kind icon, recency, and presence in Everything; instances can be filed into any number of projects, and two projects showing `files-2` share one live page.
* Instance rows and tabs carry: Refresh, Add to project..., Remove from project (not under Everything), Delete (confirm-gated, trash icon) — plus a divider and the service group (Share, Stop/Start) so service verbs stay reachable from any instance.
* Deleting an instance unfiles it from every project, closes its panes everywhere, and clears its title, recency, and stored location; nothing server-side is touched (the app's service keeps running). Removing an instance's last reference (last project membership and last pane) is equivalent to deleting it.
* Everything's rail shows a shortcut row for every openable app automatically (built-ins first, then apps alphabetically; rows fixed); its tab list shows instances only, so a zero-instance app appears in the rail/popover/launcher but has no tab-list row.
* A files instance reopens at the folder it was last viewing — across view switches, full page reloads, and workspace restarts. Apps without the beacon open at their service origin as today.
* When an app's service is stopped, all its instance rows dim and open instance tabs show the stopped placeholder; Start (from anywhere) restores them. Instances of an app the machine no longer offers stay listed with Delete as the remaining useful verb.
* Main-era workspaces upgrade transparently: an existing app pane becomes that app's instance 1 on first mount, filed into the project showing it; pins and shortcut settings are untouched.
* Agents see instances through the same grammar: `layout.py list/open/focus/close` accept `service:<name>?instance=<app>-<N>` refs; bare `open files` means MRU-or-create; `open files --new` mints.

## Changes

* Backend (`system_interface`): a kind-agnostic instance module — enumerate an app's instances by scanning project member lists plus every view's saved layout content; an allocator endpoint (`POST /api/apps/<name>/instances/allocate`-shaped) minting the lowest free `<app>-<N>` under a lock with an in-flight reservation set; a machine-wide per-ref location store (same shape as member titles/last-used) with a beacon-ingest endpoint and cleanup on delete.
* Ref grammar: extend the shared `service:` ref parsing (frontend `Projects.ts`/`DockviewWorkspace.ts`, backend `projects.py`/`layout_ops`) so `?instance=<name>` refs resolve, dedup, title, and file exactly as `?session=` browser refs do; deterministic panel ids per instance so focus-dedup and delete sweeps work like chats/terminals.
* Frontend model: instances feed the machine inventory (replacing service entries in the tab-list/launcher assembly); MRU-or-create and always-mint open paths replace the service-level `openAppTab` special cases; the anonymous `serviceInstanceId` UUID nonce is removed and the live-key instance id becomes the minted canonical name.
* Dispatch-table consolidation: the existing per-kind branches in tab-row building and the open/create path move into one table (bodies unchanged) with the layouts-derived app source as the first properly-pluggable entry.
* Menus and dialogs: instance rows/tabs get the combined instance + service verb groups; the uniform Delete confirmation gains app-instance wording (leaves every project; the app keeps running).
* Everything's rail: render a fixed shortcut row per openable app (built-ins first, then apps alphabetically); the All apps popover under Everything shows the all-pinned empty state.
* `layout.py`: `open <app> --new`, instance refs accepted by existing ops, manage-projects skill documentation.
* Vendored dufs assets: a "minds patch" location beacon (one-hop `postMessage` per page load); the dockview shell listens, validates origin, resolves the pane via `event.source`, and posts to the location store; `build-app` scaffold and skill document the beacon line.
* Migration (`CLEANUP`-marked): on first mount, a legacy service-keyed app pane is adopted as the app's lowest-free instance and filed through the normal auto-membership path; no compat for the unreleased UUID nonce.
* Docs: system_interface README Projects section, `system/apps/files/README.md`, manage-projects and build-app skills; changelog entries per touched project.
* Deferred (tracked in a GitHub issue): migrating chats/terminals/browsers onto the instance-source seam; per-instance rename; beacon adoption in other existing apps.
