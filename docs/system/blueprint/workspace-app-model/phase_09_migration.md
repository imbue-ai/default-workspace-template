# Phase 9: migration

Contracts: [contracts.md](contracts.md) sections 1, 7, and 16.

## Goal

Carry a pre-arc workspace's projects, arrangements, titles, recency, and file-browser locations into the new state files, once, deterministically; and rewrite every pre-manifest user app to the manifest form so that, from here on, every Python app runs from its own tool and apps leave the root uv workspace.

## Files

Created:

- `system/scripts/migrate_workspace_layouts.py`: stdlib-only, like the other scripts; it writes the files store's JSON shape directly, and a test in `app_instances` pins that shape against the store's reader; subcommands `run` (default) and `plan --json` (prints what it would write without writing).
- `system/scripts/migrate_workspace_layouts_test.py`: over a fixture directory built in the test from today's writers' shapes (the registry, two projects, desktop and mobile arrangements with chat, terminal, browser, files, url, and subagent panels, titles, recency, locations).
- `system/scripts/migrate_workspace_apps.py` and its test: for every `system/apps/<package>/` with a `pyproject.toml` and no `app.toml` (a pre-manifest app, contracts section 14), writes the `app.toml` the build-app scaffold would write today (`name` and `program` from the `[program:*]` block that runs `uv run <name>`, `display_name` from the pyproject description or the name, `icon = "icon.svg"` when the file exists, `instances = false`, `priority = "user"`), rewrites that program's command to `forward_port.py --manifest ... --url ... && <name>`, drops the app's entries from the root `pyproject.toml`'s `[project.dependencies]` and `[tool.uv.sources]`, and installs the tool (`uv tool install -e system/apps/<package>`). Idempotent (an app with a manifest is skipped) and committed as one change on the workspace branch, since the apply runs it inside its rollback scope.

Modified:

- `system/libs/bootstrap/src/bootstrap/manager.py`: calls the script (best-effort, logged) after `_recover_interrupted_update` and before `_exec_supervisord`.
- `.agents/skills/update-self/scripts/update_apply.py`: runs the script after the merge lands and before the services restart, inside the rollback scope.
- `docs/system/README.md` and the shell README: a section on the marker and how to re-run.
- `pyproject.toml` (root): `system/apps/*` leaves the workspace member glob and the terminal and files `exclude` entries go with it, since after this phase every app is a tool and no pre-manifest app remains; each app's own `pyproject.toml` gains a `[tool.uv.sources]` table naming its path dependencies as editable so it resolves standalone (verify first that `uv tool install -e system/apps/<package>` resolves a project outside the enclosing workspace). `uv.lock` is re-locked and `uv sync --all-packages` no longer installs any app into the root venv.

## Inputs

`$MNGR_HOST_DIR/agents/<primary>/workspace_layout/`: `projects_meta.json`, `projects/<id>.json` and `<id>.mobile.json`, `member_titles.json`, `member_last_used.json`, `member_locations.json`, `auto_opened_chats.json` (ignored), `events/client_activity/events.jsonl` (ignored).
The primary agent id comes from `MNGR_AGENT_ID` as today.

## Outputs

- `data/.state/system_interface/projects.json`: one project per registry entry, in order, with `tabs` from the member list mapped by the table below, `shortcuts` from `shortcut_overrides` and legacy `unpinned_shortcuts` (a pinned built-in becomes `(app, new, mode)` with the effective mode; an unpinned one is omitted; a `service:<name>` pin becomes `(name, new, focus)`), and `last_active_view` from `last_active_id`.
- `data/.state/system_interface/layouts/<view>/seed.desktop.json` and `seed.mobile.json`: the dockview JSON kept verbatim with each panel renamed to a fresh tab id, the `tabs` map from `panelParams` mapped by the table, and `last_focused_ms` from `member_last_used.json`; panels that map to nothing are pruned from the grid with the phase 7 helper.
- `data/.apps/files/instances.json`: one record per `service:files?instance=<key>` found anywhere, `url` from `member_locations.json` or `/`.
- `data/.state/system_interface/migrated.json`: the marker.

## Mapping

| Old panel or member | New address |
|---|---|
| `chat:<agent-id>`, `panelType: chat` with `chatAgentId` | `app:chat?instance=<agent-id>` |
| `terminal:<name>`, `terminalSessionName` | `app:terminal?instance=<name>` |
| `service:browser?session=<name>`, browser URL with `?session=` | `app:browser?instance=<name>` |
| `service:files?instance=<key>`, `serviceInstanceId` | `app:files?instance=<key>` |
| `service:<name>` member (a pin) | a `(name, new, focus)` shortcut, no tab |
| `service:<name>` panel of a single-instance app | `app:<name>` |
| `url:<hash>`, an external URL panel, a launcher panel, `subagent:` panels | dropped |
| `member_titles.json` terminal entries | applied with `tmux rename-session` when the session exists at migration time; otherwise dropped |
| `member_titles.json` other entries | dropped |

## Behaviour

- Idempotent: the marker short-circuits; `--force` re-runs after removing the outputs it wrote.
- Never destructive: the old directory is untouched; a later release deletes it.
- A missing old directory writes the marker and nothing else, so a fresh workspace is not "unmigrated" forever.
- Errors in one view are logged and skip that view; the marker is still written, and `plan` shows what was skipped.
- The app migration runs before the environment refresh in the apply (its rewritten root pyproject is what `uv sync --all-packages --frozen` then resolves) and is all-or-nothing per app: a tool install that fails leaves that app's files as they were and reports it, so the app keeps running from the root venv.

## Tests

- Every mapping row, the pruning of dropped panels including a group that empties, the shortcut derivation for each override shape, the files store output, idempotency, `--force`, a missing directory, a corrupt file in one view.
- The app migration: a scaffolded pre-manifest app gains the expected manifest, program line, and root-pyproject edits; an app with a manifest is untouched; a failed tool install leaves the app as it was.
- Bootstrap and apply wiring tests assert the call site and its error handling.

## Manual verification

Before deployment (recorded in phase 11's checklist): update a real pre-arc workspace through update-self and confirm projects, tabs, folder paths, and terminal titles survive.

## Changelog entries

`system/changelog/mngr-better-chat-app-arc.md`, `system/libs/bootstrap/changelog/mngr-better-chat-app-arc.md`, `.agents/changelog/mngr-better-chat-app-arc.md`.

## Exit criteria

The script's tests pass and a dev workspace carried from before phase 7 shows its projects and tabs after boot.
