# App-launched chats resolve like the workspace's own

Paired change across this repo (`apps/minds`) and the default-workspace-template (dwt). This plan is the single design doc for both; the dwt PR references it.

## Overview

- The Minds app starts two chats inside a workspace from outside: the `/update-self` chat behind "Update now" and the `/assist` chat behind "Ask an agent". Both run `mngr create --template chat` inside the container via `mngr exec`.
- Four ways that create goes wrong today, on real user machines: it binds no provider account (every turn "Not logged in" on any template since the account store landed in minds-v0.5.0); it picks no harness, so the template's `type = "claude"` default wins even when only codex is signed in; the chat's tab never appears on minds-v0.5.1 and later shells, whose rewrite dropped the label-driven auto-open; and a claude pin mismatch (a stale image env re-installing the old version, see Cathy's machine) refuses every claude create, including the update that would fix it.
- PR #885 hotfixed the binding by having the app shell into the template's `default_account_args.py` and splice its answer into the create. It works, but it makes the app parse a template script's output and refuse on its shape: coupling we do not want. This plan reverts that in favor of a mechanism mngr already has.
- Key decision: the workspace writes `.mngr/settings.local.toml` (mngr's gitignored local config layer, above the project file and below `-S`) with `[commands.create]` defaults naming the machine's default account: its harness as `type`, its binding (`env` for claude, an `extra_provision_command` symlink for codex, agy and pi), and the `account=<id>` label. Every `mngr create` in the workspace that does not say otherwise resolves to that account: the app's bare create, workers, automations, the caretaker. The app needs to know nothing about accounts.
- The chat app owns the file. The accounts module regenerates it on every index write (server or script), and the chat app reconciles at boot. The file names the pinned default account, else the most recently used one, exactly as the chat picker resolves a launch. Pin and MRU stay in `index.json`; the file is derived output.
- `type = "claude"` leaves the committed `settings.toml`. With no account, a create fails instead of launching an unauthenticated claude, and a committed self-gating pre-command script turns mngr's "No agent type provided" into "sign in to a provider first" for every creator on every harness.
- Auto-open comes back where it belongs: the chat app reacts to the `assist` / `auto_open` labels on a newly observed agent and docks its tab through the shell's open op, held until a client is there and delivered once. The app sends labels and runs no layout op itself.
- The stuck-machine lever stays app-side: `-S agent_types.claude.check_installation=false` on the app's create, since it is the only thing that reaches a workspace whose in-container mngr refuses the update. A `minds_app` create template for app-launched settings is a follow-up; mngr reinstalling on a pin mismatch and the chat app tolerating one are explicitly not wanted.
- Backcompat: workspaces on minds-v0.5.0 through v0.5.2 keep accounts but have no file writer. For their one update the app keeps #885's resolver probe as a fallback used only when the local file is absent, marked `CLEANUP` for removal after the release that ships the writer has been the minimum updatable template for one release cycle. Pre-account-store workspaces need nothing: their shared login makes an unbound claude create correct.
- Landing order: dwt PR first (ships in the first template release after minds-v0.5.2), app PR second. Each half is compatible with the other's current state.

## Expected behavior

From the user's side:

- Pressing "Update now" or "Ask an agent" on any workspace on the new template starts a chat on the same account and harness a New Tab chat would use. A codex-only user gets a codex update chat.
- The chat's tab appears in every window that has the workspace open, focused, where the user is looking. A window that opens later still gets it once, as long as the chat app has not delivered it and the chat is under 12 hours old at chat-app startup.
- With no provider signed in, the app shows the workspace's own refusal, which reads "sign in to a provider first" rather than mngr's config-set hint. The update run slot is released so a retry after signing in works.
- On a workspace whose claude binary no longer matches the template's pin, both app-launched chats still start. The workspace's own New Tab chat still fails there; that is accepted.
- A worker launched from a chat, a caretaker run, and any automation run on the machine's default account and harness without their creators naming one. A caller that wants a specific harness can still say so.
- Re-authenticating an account now restarts every agent bound to it, workers and app-launched chats included, not only chats the chat app created.
- On a workspace from minds-v0.5.0 through v0.5.2 (accounts, no file writer), an app-launched chat is bound through the old resolver, as #885 does today, for as long as that path exists. If the resolver names no claude account, the chat is created unbound and its first turn shows the provider's error in the tab; there is no early refusal any more.
- On a workspace older than v0.5.0 nothing changes: an unbound claude create on the shared login, tab surfaced by the `assist` label.
- The automation runner works again on the current template: it no longer stacks a `claude` template that stopped existing when harnesses moved to `--type`.
- Nobody is expected to read or edit the local file. A hand edit is left alone until the next account event rewrites the managed block; keys the chat app does not manage survive every rewrite.

From the system's side:

- `.mngr/settings.local.toml` exists only once an account exists. Its managed block is `[commands.create]` with `type`, `label`, and either `env` (claude: `CLAUDE_CONFIG_DIR=<account dir>`) or `extra_provision_command` (codex, agy, pi: `mkdir -p` plus `ln -sfn <account credential> "$MNGR_AGENT_STATE_DIR/<harness credential path>"`, evaluated with the agent env sourced so the state dir resolves without knowing the agent id).
- CLI list flags append after config, so the chat app's own create keeps passing the chosen account's binding explicitly and wins over the file's default for the same variable or path.
- The committed `settings.toml` gains `[pre_command_scripts] create = ["python3 system/scripts/require_create_account.py"]`; the script exits 0 when the local file names `commands.create.type` and otherwise fails with the sign-in message. mngr aborts the create quoting it.
- `[commands.create]` in the committed file keeps everything except `type`.
- The app's create is `mngr create <name> --template chat --transfer none --no-connect --label assist=true --label auto_open=true -S agent_types.claude.check_installation=false --message <text>`, prefixed with `MNGR_ALLOW_UNKNOWN_CONFIG=1` as today, plus `--label user_created=true` so the agent sits in the chat memory band.
- The reactor posts `{op: "open", args: {address: "app:chat?instance=<id>", client: <id>}}` to the shell's `/api/layout/broadcast` once per connected client, with the shell's default placement, which also files the chat into whatever project view each client is on.

## Implementation plan

### default-workspace-template

`system/apps/chat/imbue/chat/create_defaults.py` (new)

- `MANAGED_KEYS`: `type`, `env`, `extra_provision_command`, `label` under `commands.create`.
- `default_create_settings(index, home) -> CreateDefaults | None`: resolves the account the way `binding.resolve_binding("")` does (pinned, else MRU, else oldest, skipping lanes this build lacks and folders that are gone) and returns its harness, binding form, and label; None when no usable account exists.
- `binding_defaults(harness, account_dir) -> list[str]` next to `binding.create_args`: same forms, but the symlink destination is the shell expression over `$MNGR_AGENT_STATE_DIR` rather than a concrete path. `agent_credential_path` gains a sibling that yields the harness credential path relative to the state dir, so the two stay one table.
- `write_create_defaults(repo_root, defaults)`: loads the file with tomlkit (or starts empty), replaces only the managed keys, writes atomically. With `defaults is None` and a managed block present, removes the managed keys (the file is left in place if it holds anything else, deleted otherwise).
- `read_create_defaults(repo_root) -> str | None`: the `commands.create.type` the file names, for the pre-command script and tests.

`system/apps/chat/imbue/chat/accounts.py`

- `_write_index` calls `write_create_defaults` after the index lands, under the same lock. Every index mutation (commit, delete, rename, set_mru, set_default_account, reconcile's prune) therefore regenerates the file, from scripts and the server alike.

`system/apps/chat/imbue/chat/main.py`

- Boot reconcile: after `_reconcile_account_store`, regenerate the file from the index once (covers a workspace updated onto this template with accounts already present, and a file deleted by hand). Skipped under `--preflight`.

`system/apps/chat/imbue/chat/auto_open.py` (new, the reactor)

- `AUTO_OPEN_LABELS = ("auto_open", "assist")` and `is_auto_open_labeled(labels)`.
- `AutoOpenLedger(path)`: delivered agent ids, JSON under `data/.apps/chat/auto_opened_chats.json`; `is_delivered`, `mark_delivered`, `forget`. A file of the wrong shape is logged and treated as empty.
- `ShellLayoutClient`: `connected_client_ids()` from `GET /api/clients` (the `connected` flag), `open_chat(agent_id, client_id) -> bool` posting the open op; base URL from `MINDS_WORKSPACE_SERVER_URL` like `app_instances.nudge`. Unlike `post_to_shell`, it returns whether the shell accepted, since the reactor holds on refusal.
- `AutoOpenReactor(ledger, shell, clock)`: `note_appeared(agent)` on a labeled agent not yet delivered; `seed_at_startup(agents)` keeps only labeled, undelivered agents whose `create_time` is under 12 hours old; `flush()` tries every pending id against every connected client, marks delivered on the first accepted open, and leaves the rest pending; `forget(agent_id)` on removal.

`system/apps/chat/imbue/chat/agent_manager.py`

- `_handle_observe_event`: feeds `added_agent_ids` to the reactor (startup seed on the first full state, `note_appeared` afterwards), `removed_agent_ids` to `forget`, then `flush`.
- A flush also runs when the shell nudges the chat app about a new client (`/api/apps/chat/changed` is the existing nudge path; the reactor hooks the client-activity report the chat pages already send, so an arriving window triggers delivery without polling).
- `restart_agents_on_account`: unchanged code, wider effect noted in its docstring (workers and app-launched chats now carry the label).

`system/scripts/require_create_account.py` (new)

- Reads `.mngr/settings.local.toml` relative to the cwd (mngr runs pre-command scripts at the project root). Exits 0 when `commands.create.type` is present; otherwise prints "No provider account is signed in on this machine. Sign in from a chat tab, then try again." to stderr and exits 1. Stdlib `tomllib` only, so it runs before any venv exists.

`.mngr/settings.toml`

- Drop `type = "claude"` from `[commands.create]`; add the `[pre_command_scripts]` table.
- Comment on `[commands.create]` pointing at the local file as the source of the default type and binding.

`system/scripts/default_account_args.py` and `system/libs/automations/run_automation.sh`

- Delete the script. The runner drops `HARNESS`, the resolver call, and `--template "$HARNESS"`; gains `--type <harness>` as an optional override passed through to `mngr create`. `system/libs/automations/README.md` and the manage-scheduled-tasks skill text follow.

Docs

- `system/apps/chat/README.md` (provider accounts section: the file, what it carries, who writes it), `system/libs/automations/README.md`, and the `.agents/shared/references/service-processes.md` note about the unset config-dir variable.
- Changelog entries per touched dwt project.

### apps/minds

`apps/minds/imbue/minds/desktop_client/skill_chat.py`

- `build_skill_chat_mngr_args`: drop the `account_args` parameter; add `--label user_created=true` and `-S agent_types.claude.check_installation=false`. Keep both auto-open labels.
- Keep `resolve_account_binding`, `AccountBinding`, and the probe builder, but as the backcompat path only: `build_skill_support_probe_args` also echoes a `MNGR_LOCAL_SETTINGS_PRESENT` / `ABSENT` sentinel for `.mngr/settings.local.toml`, so one exec answers both questions. `check_skill_support` returns a small model carrying `SkillSupport` and `is_local_settings_present`.
- `spawn_skill_chat(..., account_args)` keeps the parameter; callers pass the resolver's args only when the file is absent and the resolver answered `BOUND`. `UNAVAILABLE` and `UNREACHABLE` no longer refuse: the create runs unbound and the workspace's verdict is what the user sees.
- `CLEANUP` comments on every backcompat piece, all naming the same criterion.

`apps/minds/imbue/minds/desktop_client/update_service.py` and `app.py`

- Dispatch: probe once; if the file is present, spawn bare; else resolve the binding and spawn with whatever it returned. Drop the `UNAVAILABLE` refusal branch (the 409 and its run-slot release) and the "no signed-in Anthropic account" copy in `ui_api_updates.py`.

`apps/minds/behaviors/workspace-updates/update-run.feature`

- Replace the `@no-account-to-run-on` scenario with one where the workspace's own refusal is shown and the run is startable again.

Docs and changelog

- `apps/minds/docs/design.md`, `docs/workspace/glossary.md`, and the `skill_chat.py` module docstring: the app no longer resolves accounts; the workspace's local config does.
- `apps/minds/changelog/gabriel-polar-toad.md` and `dev/changelog/gabriel-polar-toad.md` (this plan lives under `blueprint/`).

## Implementation phases

1. **dwt: the local file.** `create_defaults.py`, the accounts-module hook, the boot reconcile, `settings.toml` without `type` and with the pre-command, `require_create_account.py`. After this phase a workspace's own chats, workers and automations all resolve the default account; the app's current create (post-#885) still works because its explicit `--env` appends after the file's.
2. **dwt: automations and the resolver.** Delete `default_account_args.py`; the runner runs on the file's default with an optional `--type`. Fixes the automation runner's dead `claude` template reference.
3. **dwt: the reactor.** `auto_open.py`, the agent-manager hooks, the client-arrival flush. App-launched chats now surface on the new shell.
4. **dwt: docs, changelogs, release.** Ships as the first template release after minds-v0.5.2.
5. **app: the create.** Bare create with the two new flags; probe extended with the file sentinel; #885's binding demoted to the file-absent fallback; the `UNAVAILABLE` refusal and its copy removed; behaviors and docs updated.
6. **app: tests and changelog.** Unit coverage per below, the acceptance test, changelog entries, `CLEANUP` markers verified to name the criterion.

## Testing strategy

Unit, dwt (chat app):

- `create_defaults_test.py`: pinned default beats MRU beats oldest; a pinned account on a lane this build lacks is skipped; a claude account yields `type = "claude"` plus the env binding; codex, agy and pi yield the symlink command over `$MNGR_AGENT_STATE_DIR`; the account label rides every form; no usable account removes the managed keys; keys outside the managed block round-trip through a rewrite; the written file loads under the vendored mngr's strict parser with no narrowing violation against the committed `settings.toml`.
- `accounts_test.py`: every index mutation regenerates the file (commit, delete, rename, set_mru, set_default_account, reconcile's prune of a folder that is gone).
- `auto_open_test.py`: a labeled agent with one connected client is opened once and recorded; no client holds it and a later client arrival delivers it; two clients each get an open; a delivered id survives a ledger reload; a startup seed skips delivered ids and agents older than 12 hours; an unlabeled agent is ignored; a removed agent is forgotten; a wrong-shaped ledger starts empty with a log line.
- `agent_manager_test.py`: the observe handler feeds the reactor for added and removed agents; the CLI-contract test for the chat create no longer relies on a config default type.

Unit, dwt (scripts):

- `require_create_account_test.py`: passes with a type, fails with the message without one or without the file; runs under the vendored mngr as a real `pre_command_scripts.create` entry against a temp project and the create is refused quoting the message.
- `run_automation` shell test: the create argv carries `--template automation`, no harness template, and `--type` only when given.

End-to-end, dwt:

- Chat e2e (`test_e2e.py` family): seed a codex account folder and index; create a chat through the instances API and a worker through `mngr create -t worker` with a fake `mngr` on PATH; assert both argv carry `--type codex` and the symlink binding from the file, and that the file names that account. Sign in a second account and pin it; assert the file follows the pin.
- Shell pipeline e2e (`test_layout_pipeline.py` harness): a chat app with a labeled agent seeded and one connected client ends with the chat docked in that client's layout file; with no client, nothing is docked until a client registers.

Unit, app:

- `skill_chat_test.py`: the create argv (labels, `user_created`, the `-S`, no account args) and the extended probe's two sentinels; the fallback is consulted only when the file is absent; `UNAVAILABLE` and `UNREACHABLE` no longer refuse.
- `update_service_test.py`, `test_desktop_client.py`, `ui_api_updates_test.py`: dispatch on a file-present workspace makes one probe and one bare create; on a file-absent workspace with a bound resolver the create carries its args; the pre-command refusal reaches the modal as the verdict block and the run slot is released.

Acceptance, app (snapshot stage, skipped without `ANTHROPIC_API_KEY`):

- Launch an assist chat through the app into the baked workspace; assert the created agent's env or credential link points at the workspace's default account and it carries the `account` label; assert the shell docked it in the connected client's layout.

Manual verification before declaring done:

- A fresh docker workspace on the new template: sign in to codex only, press "Ask an agent", confirm a codex chat answers and its tab appears; sign out, press again, confirm the sign-in refusal; press "Update now" and confirm the run starts.
- A workspace with claude pinned to a version the binary does not match: confirm the assist chat starts and the workspace's own New Tab chat still refuses.
- A minds-v0.5.0 workspace (no writer): confirm the app binds through the fallback and the update completes, after which the updated chat app writes the file.

## Open questions

- The shell nudge path for client arrival: the reactor needs a signal when a window connects. The chat pages report presence to the chat app already; if that report is not reliably the first thing a new window does, the reactor falls back to retrying on a short timer while anything is pending.
- `$MNGR_AGENT_STATE_DIR` in an `extra_provision_command` default: confirmed available through the sourced agent env at implementation time on the vendored mngr, but not yet exercised through a `commands.create` default specifically.
- Whether `mngr create -t worker` from inside a bound claude chat should follow the file or its parent: today the claude plugin copies the spawning shell's `CLAUDE_CONFIG_DIR`, so the parent wins for claude and the file wins for every other harness. Left as is; the per-role preference follow-up decides it properly.
- The `CLEANUP` criterion phrasing: "the release that ships the writer has been the minimum updatable template for one release cycle", or a fleet check that no older machine exists. Either is acceptable; the marker names the first, and a fleet check can justify pulling it earlier.
- Follow-ups, not in scope: per-role account preferences and a chat settings surface for them (also where the automation runner's `--type` override belongs); a `minds_app` create template carrying the app-launched settings, stacked when the workspace defines it.
