# Phase 9: migration

Contracts: [contracts.md](contracts.md) sections 1, 7, 14, and 16.

## Decisions taken before landing it

The phase as first written also rewrote every pre-manifest user app to the manifest form and took `system/apps/*` out of the root uv workspace's member glob.
On 2026-09-05 the user dropped both, for three reasons.
A pre-manifest app already works under the new shell: its program line registers with `--name`, `--icon-file`, and `--program` and runs `uv run <name>` from the root venv, the build script and the apply skip it deliberately, and the registry reader treats a manifest-less row as a single-instance app with the synthesized `open` action, so the rewrite would have edited a user's committed `pyproject.toml` and supervisord config during an unattended update for nothing the user can see.
Every workspace with such an app names it as a `{ workspace = true }` source in its root pyproject, so removing the glob would make every uv command refuse (the update worker's validation included) until that entry was dropped, which is exactly the kind of breakage a migration must not risk.
And user apps belong in the same workspace as everything else: one repo, one lockfile, one set of build and test commands, so the glob stays and a user app's environment isolation comes from its uv tool environment, not from leaving the workspace.

So the two app forms of contracts section 14 are both supported indefinitely: a manifest app runs from its own tool, a pre-manifest app keeps `uv run` from the root venv, and every app stays a workspace member.
Converting an old app to the manifest form is something the update-app skill may offer the next time the user edits that app, with the user present; it is never done by an update.
Phase 11 keeps `forward_port.py`'s `--icon-file` and `--program` for the same reason.

The layout migration below is what the phase is.
It was also adjusted to what phases 3, 7, and 8 landed: the outputs are the shell's current state files, terminals get records in the terminal app's store (the shell prunes any tab whose address no app lists, and at boot no tmux session exists, so without a record every migrated terminal tab would vanish at the first observation and every title with it), an app pin becomes the single-instance app's `open` action, the old last-active project is dropped (the active view lives on the client record now), and an output that already exists is never overwritten.

## Goal

Carry a pre-arc workspace's projects, arrangements, titles, recency, and file-browser locations into the new state files, once, deterministically, without ever destroying anything.

## Files

Created:

- `system/scripts/migrate_workspace_layouts.py`: stdlib-only, like the other scripts; subcommands `run` (the default) and `plan [--json]` (prints what a run would write without writing); options `--source` (default from `MNGR_HOST_DIR` and `MNGR_AGENT_ID`), `--state-dir`, `--apps-data-dir`, `--registry` (default `MINDS_APPS_FILE` or `data/.state/apps.toml`), `--force`.
- `system/scripts/migrate_workspace_layouts_test.py` and the `legacy_layout_dir` and `migration_registry` fixtures in `system/scripts/conftest.py`: a fixture directory built from the retired writers' shapes (two projects, one with every panel kind and the overrides map, one hand-edited with the legacy unpinned list and a corrupt mobile file; an Everything view showing only an ad-hoc page; the three per-ref side stores), with the reader round trips: the shell's `ProjectStore` and `LayoutStore` read the outputs, the dockview editor edits a migrated seed, the instances library's `JsonStoreInstanceSource` and the terminal's `JsonTerminalSessionStore` read the stores.

Modified:

- `system/libs/bootstrap/src/bootstrap/manager.py`: `_migrate_workspace_layouts_best_effort` runs the script after `_recover_interrupted_update` (the restored tree's script is the one to run) and before supervisord starts the shell; a failure is logged and never blocks boot.
- `.agents/skills/update-self/scripts/update_apply.py` and `update_layout.py`: the apply runs the script from the merged tree after the pre-flight and before the restart, so the restarted shell reads migrated state at once; a failure is a warning, never a rollback (the outputs are derivable, the old files are untouched, and the boot-time run retries).
- `docs/system/README.md`, the shell README's state section, `system/apps/README.md`, `docs/system/workspace-internals.md`, the root `pyproject.toml` comment, `system/scripts/build_workspace.sh`, and the update-self scripts' comments: the two app forms are both supported indefinitely; the marker and how to re-run.
- This folder: the plan's sections 3.1, 9, 10, and 11, contracts sections 3, 7, 14, and 16, phase 11 (the flags stay), and the phase 1, 3, and 4 files where they promised the glob's removal.

## Inputs

`$MNGR_HOST_DIR/agents/<primary>/workspace_layout/`: `projects_meta.json` (`project_by_id`, each with `name`, `color`, `glyph`, `members`, and `shortcut_overrides` or the legacy `unpinned_shortcuts`; `last_active_id`), `projects/<id>.json` and `<id>.mobile.json` (`{"dockview", "panelParams"}` as the old frontend saved them, `panelParams` carrying `panelType`, `chatAgentId`, `terminalSessionName`, `serviceName`, `serviceInstanceId`, `url`, `title`, `customTitle`), `member_titles.json` (`title_by_ref`), `member_last_used.json` (`last_used_ms_by_ref`), `member_locations.json` (`location_by_ref`); `auto_opened_chats.json` and `events/` are ignored.
The primary agent id comes from `MNGR_AGENT_ID` and the host directory from `MNGR_HOST_DIR`, as the old shell read them.
The registry (`data/.state/apps.toml`) is read for the pins of apps with instances; a missing registry is fine.

## Outputs

- `data/.state/system_interface/projects.json`: `{"version": 1, "projects": [...]}`, one project per registry entry, in order, with `tabs` from the member list mapped by the table below plus every address the project's seeds dock, `shortcuts` derived as below, `name`, `color`, and `glyph` (a hand-edited entry falls back to the display defaults).
- `data/.state/system_interface/layouts/<view>/seed.desktop.json` and `seed.mobile.json`: a layout record (`dockview`, `tabs`, `device_kind`, `updated_at` = the migration time) whose `dockview` is the old grid kept as dockview saved it, with each panel that maps to an address renamed to a fresh tab id (in the grid, the panel entry, and the tab record), its panel entry rebuilt in the frontend's current shape (`instance` content component, `custom` tab component, params `kind`, `address`, `tabId`, the old title or custom title kept), `last_focused_ms` from `member_last_used.json`, and every other panel pruned with the shell's collapse rule.
  A view whose panels all map to nothing gets no seed.
  Everything gets seeds only.
  No per-client file is written: a first-visiting client materializes its own from the seed.
- `data/.apps/files/instances.json`: the instances library's store shape, one record per `app:files?instance=<key>` any project or seed references (`url` from `member_locations.json` or `/`, `title` `File Viewer <N>`, `referenced`, `last_active` from the recency store or the migration time).
- `data/.apps/terminal/instances.json`: the terminal app's store shape, one record per `app:terminal?instance=<name>` any project or seed references (`name`, `title` from `member_titles.json` or none, no `workdir`), so the terminal app lists the terminal before its tmux session exists again.
- `data/.state/system_interface/migrated.json`: `{"version": 1, "migrated_at", "source"}`.

## Mapping

| Old member or panel | New |
|---|---|
| `chat:<agent-id>`, `panelType: chat` with `chatAgentId` | `app:chat?instance=<agent-id>` |
| `terminal:<name>`, `terminalSessionName` | `app:terminal?instance=<name>` (a name tmux would refuse is dropped) |
| `service:browser?session=<name>`, browser URL with `?session=` | `app:browser?instance=<name>` |
| `service:<name>?instance=<key>`, `serviceInstanceId` | `app:<name>?instance=<key>` |
| `service:<name>` member (a pin) | a shortcut `(name, open, focus)` for a single-instance app, or the registry's default action for an app with instances, in the member's own mode override if any; no tab |
| `service:<name>` panel of a single-instance app | `app:<name>` |
| bare `service:chat`, `service:terminal`, `service:files`, `service:browser` | dropped (every tab of these apps is an instance) |
| `url:<hash>`, an external URL panel, a launcher panel, `subagent:` panels | dropped |
| the built-in rail rows | `(chat, new, new)`, `(terminal, new, focus)`, `(files, new, focus)`, `(browser, new, focus)`, minus the unpinned ones (`is_pinned: false` or the legacy list), each in its mode override if any |
| `member_titles.json` terminal entries | the terminal record's `title` |
| `member_titles.json` other entries | dropped (chats carry theirs on the agent) |
| `member_last_used.json` | `last_focused_ms` on the matching seed tabs; `last_active` of the files records |
| `member_locations.json` | the files records' `url` |
| the registry's `last_active_id` | dropped; a first-visiting client lands on the first project |

## Behaviour

- Idempotent: the marker short-circuits `run`; `--force` runs again, overwriting the projects file and the seeds.
- Never destructive: the old directory is untouched (a later release deletes it); a projects file that already holds projects, and any seed that already exists, are kept and reported unless `--force`; the two app stores only ever gain records (a record the app already holds wins, and a store that cannot be read is left alone); the marker is written in every case.
- A missing old directory writes the marker and nothing else, so a fresh workspace is not "unmigrated" forever.
- An unreadable content file costs that seed and nothing else; `plan` and the log say what was skipped.
- Bootstrap runs it at every boot (best-effort, behind the marker), so it runs after the apply's restart whatever the apply did; the apply runs it too, before the restart, as a warning-only step.

## Tests

- `migrate_workspace_layouts_test.py`: every mapping row, the pruning of dropped panels including a view that empties, the shortcut derivation for the overrides map, the legacy unpinned list, a single-instance pin, and a pin of an app with instances, the files and terminal stores and their merge rule, the marker, idempotency, `--force`, a missing directory, a corrupt file in one view, `plan --json`, the environment-derived source, and the reader round trips above.
- `manager_test.py`: the call runs after the rollback and before supervisord, and a failure never blocks boot.
- `update_self_test.py`: the call runs from the merged tree before the restart, and a failure or an unspawnable script is a warning, not a rollback.

## Manual verification

Before deployment (recorded in phase 11's checklist): update a real pre-arc workspace through update-self and confirm projects, tabs, folder paths, and terminal titles survive.

## Changelog entries

`system/changelog/mngr-better-chat-app-arc.md`, `system/libs/bootstrap/changelog/mngr-better-chat-app-arc.md`, `.agents/changelog/mngr-better-chat-app-arc.md`, `system/apps/system_interface/changelog/mngr-better-chat-app-arc.md`.

## Exit criteria

The script's tests pass, its outputs are read back by the shell's stores and the apps' stores in those tests, and a dev workspace carried from before phase 7 shows its projects and tabs after boot.
