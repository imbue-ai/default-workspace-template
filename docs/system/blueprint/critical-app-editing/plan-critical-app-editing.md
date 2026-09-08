# Editing the workspace's critical apps

Paired branches: `gabriel/critical-app-editing` in this repo and in the mngr repo. The mngr side is specified in the mngr repo at `blueprint/critical-app-editing/plan-critical-app-editing.md` and lands first; this repo vendors it.

This plan replaces an unmerged pair of branches and their spec: `submit/system-interface-live-editing-plan` in this repo (spec `docs/system/blueprint/system-interface-live-editing/`) and `gabriel/denim-pigeon` in the mngr repo. Those branches were written against a system interface that still contained the chat; the workspace app model (`../workspace-app-model/`) has since split the chat into its own app, which changes what "editing the workspace UI safely" means. The parts of that branch that still apply are carried here and named in the Implementation plan.

## Refined prompt

Fresh paired branches in mngr and default-workspace-template, replacing the system-interface-live-editing branch, with a new spec for editing the workspace's critical apps.

* Manifest-keyed routing is the core: update-app reads `app.toml`, and `critical = true` selects the careful flow, described in a reference under update-app that replaces update-system-interface. The shell, chat, and terminal are critical; a change to the shared workspace_ui library routes the same way because it rebuilds both bundles. Heals keep their own path, with no safety net.
* `app.toml` gains an optional `[preview]` table whose defaults are the scaffold convention; one generic script boots any app's preview from it, and build-app's scaffold emits it.
* The careful flow is the lead-driven live loop: isolated worktree, build, in-place preview refresh, harden worker created at approval on the lead's branch, go-live through the atomic apply only after the harden pass (relaxing this for contained changes is noted as future work). Every critical app previews, the terminal included; a preview and its worktree persist across turns until explicit abandonment. A new pass on an app with an open notice proceeds and replaces the rollback point, saying so.
* The observer becomes `agent-observer`, a manifest-less supervised service in the chat's memory band; every chat instance follows it, using the follower landed in the mngr branch first. `--stream-events` stays in mngr. While the observer is down the chat serves its last known list and reports degraded.
* One chat preview mechanism: a `--secondary` chat built from the worktree (no account reconcile, no OOM writes, no nudges, no registration, scratch data dir, live accounts read), tracking everything the live chat tracks, opened on the user's current conversation, with real sends allowed. When both bundles changed, the preview shell's copied registry points at that secondary chat.
* A preview shell gets a seeded copy of the live state dir plus a copied registry, refuses mutating relay verbs, and hides them.
* After an apply from the careful flow, the shell shows a notice per critical app whose program or bundle changed, or a top banner for the shell itself: recently updated, roll back or everything seems good. Only a person closes it. The rollback point and its snapshots are kept whole until it is closed, a rollback runs, or the next apply replaces it. Rollback restores the kept point and restarts only the touched programs through supervisorctl; a rollback that needs the services agent restarted restores the files and names the command for an agent. The outcome goes to the apply's run record, not to the driving agent. update-self keeps its own run record and raises no notice.
* Carry forward all of the old branch's fixes that still apply: serve_isolated_instance refresh, verified teardown, OOM band and boot logs; create_worker `--branch` and reading the worker's branch from mngr; the settled post-restart verdict; the unknown-`/api` 404; the three doc defects; the prototype taxonomy; the "layout.py open shows the user" rule; the update-app verify timing. Only the FOLLOW-mode health gate and the pre-flight FOLLOW environment are dropped.
* mngr side: the observe read side including the follower, the `initial_branch` widening, and notify's probe, merged from the old mngr branch.
* Done means the automated suites plus a scripted manual scenario in a real workspace, with findings recorded in this folder.
* The deferred items live in this plan's Open questions only.
* The template branch re-applies the surviving diffs from `submit/system-interface-live-editing-plan` by hand as new commits grouped by topic; the mngr branch merges or cherry-picks `gabriel/denim-pigeon`.
* Lands as one unit; phasing is for build order.

## Overview

- **Why now.** The old careful flow existed because the system interface was one process: any change could take away the user's only way to fix things, so every change waited out a full harden pass before the user saw anything. The app model split that process into a shell, a chat app, and a terminal app, each critical but with a different blast radius and recovery path. The old branch's live-editing loop is still the right UX; its safety machinery (a second observer, a strict health gate) solved a problem the split removed and missed the ones the split created.
- **Principles.** Caution scales with the recovery path, not the surface. The manifest chooses the flow, not the app's name. A preview confirms intent and is not the safety gate. A preview reads live state but owns nothing it writes. Go-live is one atomic motion for every critical app. The shell is the recovery surface for a broken app; the terminal handover is the floor for a broken shell, never something the flow leans on.
- **Routing by manifest.** `update-app` stays the front door for every app edit. It reads the app's `app.toml`; `critical = true` (the shell, the chat, the terminal, and any user app that declares it) routes to the careful flow, a reference under update-app. `update-system-interface` is removed. A change under `system/libs/workspace_ui/` routes the same way because it rebuilds both critical bundles.
- **One preview mechanism for every app.** `app.toml` gains an optional `[preview]` table describing how to boot a throwaway instance (ports, environment, arguments, which directories to copy, the health path, the path to open). Its defaults are the scaffold's existing convention, so every app previews by construction; the three built-ins declare what they need beyond it. One generic script boots, refreshes, and tears down a preview from the table, on top of the shared `serve_isolated_instance.py`.
- **The observer is its own program.** `mngr observe` runs as `agent-observer`, a supervised service. The chat app no longer spawns an observer; it follows the event file the program writes, through the follower mngr now exports. There is no second code path: the live chat and a preview chat are the same reader. A dead observer is restarted by supervisord and the chat resumes on the next snapshot, where today the live chat's agent view freezes for good.
- **Chat previews are a secondary chat.** `chat-app --secondary` boots the worktree's chat against the live agents: it follows the observer, reads the live accounts, tracks every agent the live chat tracks, and withholds the writes a second instance must not make (account reconcile, OOM scores, shell nudges, registration; its stores live in a scratch copy). The user opens it on the conversation they are in. Sends are real, since the change under review may be about what happens when a message is sent.
- **A preview shell is the real workspace with one app swapped, read-only.** It boots against a seeded copy of the live shell's state directory and a copied registry whose rows for previewed sibling apps point at their previews, refuses the relay verbs that would act on live instances, and hides them.
- **The apply keeps its rollback point, and the shell offers it back.** An apply from the careful flow leaves its snapshots and a record of what it touched in place. The shell shows a notice on every tab of a touched critical app (a banner for the shell itself): recently updated, roll back or everything seems good. Only a person closes it. Rollback is the apply's own forward revert plus snapshot restore, restarting only the touched programs through supervisorctl, so the shell needs no mngr.
- **Carried forward from the old branch.** The isolated-instance script's refresh, verified teardown, memory band and boot logs; `create_worker.py --branch` and reading the worker's branch back from mngr; the settled post-restart health verdict; the unknown-`/api` 404 on the shell; the prototype taxonomy in interactive-delivery; the "opening a tab shows the user" rule; update-app's verify timing; the three doc defects.

## Expected behavior

### Routing and the careful flow

- An agent asked to change any app loads `update-app`, locates the code, reads the owning app's `app.toml`, and branches on `critical`. A non-critical app follows the live loop as today. A critical app follows `references/critical-app.md`: nothing is edited in the served tree.
- A change under `system/libs/workspace_ui/` routes to the careful flow with both the shell and the chat as its apps.
- The careful flow: take the editing lease (one lease, `editing critical apps`, held from entry through go-live or abandonment); provision an isolated worktree on the pass branch in the background while exploring; edit, build, boot or refresh the preview; open it for the user with `layout.py open`, which is the act of showing them; iterate. On approval, create the harden worker on the pass branch; on `done`, check freshness, apply, tear the preview down, release the lease.
- The harden pass always runs before go-live on a critical app. Contained changes wait for it too (see Open questions for the relaxation).
- Every critical app previews. The lead's own check ends at "it came up"; the thorough pass is the worker's, against its own instance.
- A preview and its worktree persist across turns until the user approves or abandons the pass; abandonment tears down the preview, its tab, the worktree, and the worker if one exists. A stale lease is broken only on the user's call and tears the orphaned pass down the same way.
- Starting a pass on an app whose notice is still open proceeds; the lead tells the user the previous update is unconfirmed and that the new apply will replace its rollback point.
- Heals keep their own path: heal-creation is unchanged and the reference says a heal may edit a critical app live at the agent's own risk.

### Previews

- `python3 .agents/skills/update-app/scripts/preview_app.py up --app <name> --worktree <dir> [--with <name>]... [--instance-key <key>]` boots the app from the worktree per its `[preview]` table on free ports, surfaces it as the labeled `<name>-preview` tab, and prints the instance name. `refresh` re-boots the inner process in place after a rebuild, leaving the port, wrapper, registrations, and tab untouched. `down` tears it down and verifies the processes died.
- `--with` boots sibling previews from the same worktree and, for a shell preview, rewrites those apps' rows in the copied registry to point at them. A workspace_ui change previews as the shell with the chat.
- A shell preview shows the user's real projects and tabs (a seeded copy of the live state directory), with every live app's instances, and the previewed sibling apps in place of the live ones. Rename, delete, create, stop, start, and app stop and start are refused with a 403 naming the preview and hidden from the menus; project and layout edits land in the copy.
- A chat preview lists every chat, renders the live transcripts, and opens on the conversation the user is in (the lead passes its own agent id as the instance key). Sending a message sends it to the real agent.
- A terminal preview serves the ttyd page and its instances API on two free ports, over a copy of the terminal's store and a scratch state directory; the sessions it creates are real tmux sessions.
- A preview that cannot boot does not come up: the script quotes the tail of the boot log, keeps a copy of it, and tears the partial instance down. A `down` that cannot kill a process says so, keeps the state, and exits non-zero.
- Preview processes are tagged into the `user` memory band, so they outlive every agent, the lead included.

### The observer and the chat

- `agent-observer` runs `mngr observe --quiet` from the primary agent's work dir under supervisord, in the chat's memory band, restarted on exit like every service.
- The chat app follows the observer's event file. At boot with no observer up yet, the chat waits for one: its instances API answers 503 until the first full snapshot is folded, as it does today until the first agent list.
- When the observer dies mid-run, the chat keeps serving its last known list, its health reports degraded with the reason, and the shell's existing failure handling shows the instances with status `error`. When the observer comes back, its opening snapshot replaces the folded view and the chat reports healthy again.
- `mngr notify` and every other reader are unaffected: `--stream-events` stays, and the lock probe still answers whether an observer runs.
- The update apply's chat pre-flight (`--preflight`) is unchanged and starts no follower. After a restart the apply's instances probe waits on the chat exactly as today.
- A rollback into a tree from before this release restores a chat that runs its own observer while the program from the newer supervisord table is gone with the restart; nothing special is needed.

### The notice and rollback

- After an apply run by the careful flow, every tab of a critical app whose program or bundle the apply changed carries a band above the page: "This app was updated a moment ago. If something is not working, you can go back to the previous version." with "Roll back" and "Everything seems good". A shell update shows the same as a top banner.
- "Everything seems good" discards the kept snapshots and the record; the band goes away on every window.
- "Roll back" asks for confirmation naming the update and the apps it touched, then runs the rollback: the merge is reverted forward (work committed since is kept), the snapshots are restored, the touched programs are restarted through supervisorctl, health is probed, the ledger records the rollback, and the record is cleared. The band shows progress and then the outcome.
- A rollback whose diff reaches the bootstrap or the services agent's own setup restores the files, skips the restart, and the band says an agent must restart the workspace and names the command.
- If the rollback's own probes fail, the apply's existing emergency path applies and the staleness banner takes over.
- The next apply, from any flow, replaces the kept record and snapshots. update-self's applies keep no record and raise no notice.
- The outcome of a rollback is written to the apply's run record; the driving agent is not messaged.
- Only a person closes the notice. The careful flow's reference tells agents never to confirm or roll back on the user's behalf and to tell the user the notice is there.

### Carried-forward behaviour

- `create_worker.py launch --branch <spec>` passes the branch through to `mngr create`; `launch-sync` reads the worker's branch back from `mngr ls --format json` (`initial_branch`) rather than assuming `mngr/<name>`, fails fast and destroys the worker when it cannot, and reads the branch from the payload rather than the exit code.
- The apply's post-restart verdict is settled state: several consecutive healthy answers on unchanging supervisord pids for the shell and every critical instance app's program, within the same budget.
- The shell answers an unknown `/api/...` path with a JSON 404 instead of the app shell.
- `interactive-delivery.md` phase 5 names the two demonstrative-artifact types and the wiring-cost versus restart-cost choice; `update-app`'s verify step says to verify before the user can see it; `manage-layout` and `CLAUDE.md` say `layout.py open` shows the user something live.
- `update-creation` and `heal-creation` spell `create_worker.py await` with its required `--name`; the worker brief says never to use Playwright's `networkidle` against a shell or chat instance; the "no `## Change origin` marker" exception is stated where the task-file format is defined.

## Implementation plan

### mngr repo (paired branch, lands first)

Specified in the mngr repo's `blueprint/critical-app-editing/plan-critical-app-editing.md`. In short: merge the old `gabriel/denim-pigeon` branch (the observe read side with `ObserveEventFollower`, `is_observe_writer_running`, `find_last_full_state_offset`; the `initial_branch` widening; notify's probe), then let a follower start while no observer holds the lock (`require_writer=False`, reporting the outage through `failure_detail()` until a writer appears) so a chat booting beside a not-yet-started observer program does not have to retry. This repo's `system/vendor/mngr/` is regenerated from that branch.

### `system/libs/app_manifest`

- `manifest.py`: `PreviewSpec` (`FrozenModel`) with `command: tuple[str, ...]` (default: the program's entry point), `ports: tuple[str, ...]` (named free ports; `main` always present, e.g. `("main", "sidecar")`), `env: dict[str, str]`, `args: tuple[str, ...]`, `copies: dict[str, str]` (a key to a repo-relative directory copied into the instance's scratch space), `health_path: str` (default `/health`), `open_path: str` (default `/`), `open_path_takes_key: bool`. Placeholders in `env` and `args`: `{port:main}`, `{port:<name>}`, `{host}`, `{copy:<key>}`, `{scratch}`, `{registry}` (a copied registry with previewed siblings rewritten), `{shell_url}` (the preview shell's URL when one is up, else empty). `AppManifest.preview: PreviewSpec` with the scaffold defaults derived from `name`: `env = {<PKG>_PORT: {port:main}, <PKG>_HOST: {host}, <PKG>_DATA_DIR: {copy:data}}`, `copies = {data: data/.apps/<name>}`.
- `manifest_test.py`: defaults, placeholder validation (an unknown placeholder or an undeclared port name fails validation), the three built-ins' tables.
- `README.md` and `changelog/gabriel-critical-app-editing.md`.

### Built-in manifests

- `system/apps/system_interface/app.toml`: `[preview]` with `command = ["system-interface", "--preview", "--state-dir", "{copy:state}"]`, `env = {SYSTEM_INTERFACE_PORT = "{port:main}", SYSTEM_INTERFACE_HOST = "{host}", MINDS_APPS_FILE = "{registry}"}`, `copies = {state = "data/.state/system_interface"}`, `health_path = "/api/health"`.
- `system/apps/chat/app.toml`: `command = ["chat-app", "--secondary", "--nudge-shell-url", "{shell_url}"]`, `env = {CHAT_PORT, CHAT_HOST, CHAT_DATA_DIR = "{copy:data}"}`, `copies = {data = "data/.apps/chat"}`, `health_path = "/api/health"`, `open_path = "/{key}"`, `open_path_takes_key = true`.
- `system/apps/terminal/app.toml`: `ports = ["main", "sidecar"]`, `command = ["terminal-app", "--no-register", "--app-url", "http://127.0.0.1:{port:main}", "--instances-url", "http://127.0.0.1:{port:sidecar}", "--store", "{copy:store}/instances.json", "--state-dir", "{scratch}/state"]`, `copies = {store = "data/.apps/terminal"}`, `health_path = "/_instances"`.
- `system/test_app_manifests.py`: every critical built-in's preview table validates and names real entry points.

### `.agents/shared/scripts/serve_isolated_instance.py` (carried from the old branch, plus what the generic script needs)

- `refresh --name`: re-boot the inner process on its existing port, waiting for the old process to release the port; record the new pid before the health wait.
- `down`: SIGTERM, wait, SIGKILL, wait; remove the state directory only once every recorded process group is gone; a survivor keeps the state, names its pid, exits non-zero. `up` refuses to boot over a stale instance it could not clear.
- Boot log: a boundary marker per spawn; the failure message quotes the tail since the last marker and the health probe's response body; a failed `up` copies the log to `data/.state/isolated-instances/<name>-failed.log`.
- Both the inner server and the wrapper are launched through `oom_tag_service.py user`.
- New: `--port-env` may repeat with a name (`--port-env NAME=ENV`), `--copy KEY=SOURCE` copies a directory into the instance's scratch space before boot and exposes it to placeholder substitution, `--arg-template` substitution for the launch argv, and `--inner-path` handed to the wrapper. The state file records ports, copies, and the wrapper's inner path so `refresh` and `down` need nothing but the name.
- `serve_isolated_instance_test.py`: the carried tests plus copies, named ports, and inner path.

### `.agents/shared/scripts/preview_wrapper_server.py`

- `--inner-path`: the path the framed iframe opens on the inner service.

### `.agents/skills/update-app/scripts/preview_app.py` (new)

- `up`, `refresh`, `down`, driven by `--app <name>` and `--worktree <dir>`: reads the worktree's manifest, resolves the `[preview]` table's placeholders, builds the registry copy (when `{registry}` is used) with every `--with` sibling's row pointing at that sibling's preview, and calls `serve_isolated_instance.py`. Registers `<name>-preview-app` and `<name>-preview` (the wrapper), titled from `--title`. One preview per app at a time; a different pass's live preview refuses with the same guard the old reveal script had.
- `preview_app_test.py`: placeholder resolution, registry rewriting, refusal of a second pass, `--with` ordering (siblings boot first so their URLs exist when the shell's registry is copied).
- `reveal_system_interface.py` and its test are deleted with the skill.

### `.agents/skills/update-app`

- `SKILL.md`: a first step under "Match the flow to the scope of the change": find the app, read its `app.toml`, and if `critical = true` (or the change is under `system/libs/workspace_ui/`) follow `references/critical-app.md` and stop reading here. The verify step says when to verify. The isolated-instance section points at `preview_app.py` for any app with a manifest and keeps the raw `serve_isolated_instance.py` invocation for manifest-less services. The turn-end section drops the system-interface pointer.
- `references/critical-app.md` (new): the careful flow, adapted from the old branch's rewritten `update-system-interface` skill: the three adjustments (code isolation, the preview as the user's view, the atomic apply as go-live); the lease; worktree provisioning in the background; the live loop with `preview_app.py up` / `refresh` and `layout.py open <name>-preview`; what to look at per app (the motivating conversation for the chat, the real tabs for the shell); the handoff shapes to the worker (implement-then-harden, harden-only); the freshness check over both frontends, the library, and the lockfile; the apply command with both `--worker-bundle` arguments and `--keep-rollback-point`; the exit codes; teardown; the notice and who may close it; the screenshot fallback when no client can show the tab; the "recently updated, unconfirmed" message when a notice is open.
- `references/verify.md` in build-app is unchanged; `build-app/SKILL.md` and `scaffold_flask_lib.py` emit a `[preview]` table using the scaffold's env names, and the port list names the observer's absence of a port and the preview ports as dynamic.

### Removed and repointed skills and references

- Delete `.agents/skills/update-system-interface/`.
- `update-creation/SKILL.md`: `type` is `skill`, `app`, or `service`; the system-interface paragraphs go; the go-live step says a critical app's merge and go-live belong to `update-app/references/critical-app.md`. `await` is spelled with `--name`.
- `heal-creation/SKILL.md`: `await` with `--name`; one line saying a heal of a critical app is a live edit at the agent's risk and points at the careful flow for the safe path.
- `.agents/shared/worker/references/type-app.md`: a "critical app" section (build both bundles at the npm root, report both bundle paths, never `networkidle`, the two handoff shapes); `type-system-interface.md` is deleted; `op-update.md`'s system-interface exception is retargeted at the critical-app handoff.
- `.agents/shared/references/interactive-delivery.md`: phase 5's two artifact types and the wiring-cost versus restart-cost choice; "no commits on the served branch" wording.
- `.agents/shared/references/service-processes.md`: the `agent-observer` program.
- `manage-layout/SKILL.md` (description and body) and `CLAUDE.md`: `layout.py open` shows the user something, live, the moment it returns.
- `update-self/SKILL.md`: the editing-lease rule names the critical apps; the notice is described; the apply's `--keep-rollback-point` is not used by update-self.
- `AGENTS.md`, `README.md`, `system/apps/README.md`, `docs/system/workspace-internals.md`, the shell's and the chat's READMEs: the observer program, the preview table, the careful flow.

### `.agents/skills/launch-task/scripts/create_worker.py` (carried)

- `--branch` passthrough on `launch` and `launch-sync`; `read_worker_branch` from `mngr ls --format json` after launch, raising and destroying the worker when it cannot answer; the worker name validated before interpolation into the CEL filter; the listing's `errors` quoted when empty. `create_worker_test.py` carried.

### `.agents/skills/update-self/scripts`

- `update_apply_contract.py`: `LastGoodRecord` (`merge_sha`, `rollback_to`, `applied_at`, `driven_by`, `snapshots: list[SnapshotRecord]`, `programs: list[str]` (the touched critical apps' programs, the shell included), `apps: list[str]`, `needs_services_restart: bool`, `outcome: str | None`) at `data/.state/update-apply/last-good.json`; `read_last_good`, `write_last_good`, `clear_last_good`.
- `update_apply.py`: `apply --keep-rollback-point` writes the record instead of discarding snapshots on success, computing `programs` from the plan (`app_tools` touched, bundles touched, the shell when its package or bundle changed) and `needs_services_restart` from the plan's provisioner and dockerfile classes and `system/scripts/bootstrap*`; any apply, with or without the flag, first discards a previous record's snapshots. New `rollback_last` and `confirm_last` functions.
- `update_self.py`: `rollback-last` and `confirm-last` subcommands. `rollback-last`: refuse without a record; forward-revert the merge (`git revert -m 1 --no-edit`, the rollback commit subject the apply already uses); restore the snapshots; `supervisorctl reread && supervisorctl update` when the supervisord table changed; `supervisorctl restart <program>...` unless `needs_services_restart`; wait for settled health on the shell and the instances API of each restarted critical app; append a ledger line; write the outcome into the record's `outcome` and the run record; clear the record on success; on a failed probe fall into the existing emergency reporting.
- `update_probes.py`: `wait_settled` (carried) generalised to a set of programs: consecutive healthy answers from the shell's health and each critical instance app's instances API, with `supervisorctl status` pids unchanged for the shell and those apps' programs; `parse_supervisor_pid`. The apply's post-restart and recovery probes use it.
- `update_self_test.py`: the record's lifecycle, rollback with program restarts, the services-restart case, the settled verdict, the carried tests.

### `system/supervisord.conf` and `system/services/oom_priority`

- `[program:agent-observer]`: `command=python3 system/services/oom_priority/bin/oom_tag_service.py agent-observer bash -c "cd \"$MNGR_AGENT_WORK_DIR\" && exec mngr observe --quiet"`, `priority` before the chat, the standard log settings; the earlyoom note lists it in the shed order.
- `bands.py`: `SERVICE_BANDS["agent-observer"] = 25` (the chat's band) with a comment: shedding it freezes every chat's agent view until supervisord brings it back, so it is worth exactly what the chat is. `bands_test.py`.
- `system/services/README.md`: the program.

### `system/apps/chat`

- `main.py`: `--secondary` (no account reconcile, no registration, no nudger unless `--nudge-shell-url` is given, OOM writes withheld) alongside `--preflight`; `build_production_state` takes the data directory from `Config`.
- `config.py`: `chat_data_dir: Path = Path("data/.apps/chat")` (`CHAT_DATA_DIR`); the message stamps path and the instances store path derive from it.
- `agent_manager.py`: delete `_start_observe`, `_watch_observe_process`, `_build_observe_command_argv`, `_resolve_observe_cwd`, and the `mngr_binary` use for observe; `start` builds an `ObserveEventFollower(events_base_dir=get_host_dir(), on_line=..., require_writer=False)` and keeps `_handle_observe_output_line` as the fold; `get_agent_events_status()` (`AgentEventsStatus`: `is_stream_healthy`, `detail`) from the old branch, FOLLOW-only; `is_agent_list_known` is set by the initial discovery or the first folded snapshot as today; `build` takes `is_secondary` and hands the OOM prioritizer a refusing `set_adj` when set (the old branch's `_refuse_to_set_oom_score_adj`).
- `server.py`: `/api/health` gains `agent_events` (`{"is_stream_healthy", "detail"}`) when a manager exists; status stays `ok` (the pre-flight boot keeps polling it) and the instances API is what says the app is usable.
- `instances.py`: unchanged rule (503 until the agent list is known); the record status for a degraded stream is left to the shell's fetch-failure path.
- `agent_manager_test.py`, `main_test.py`, `server_test.py`, `conftest.py`, `testing.py`: follower tests write events through mngr's own writer (`append_observe_event`) into a temp host dir; the observe-subprocess tests go; `--secondary` covered; `running_workspace` optionally runs a real `mngr observe` for the system tests.
- `README.md` and the changelog entry.

### `system/apps/system_interface`

- `main.py`: `--preview`; `SystemInterfaceState.is_preview`.
- `shell/routes.py`: the relay verbs (`relay_create_route`, `relay_delete_route`, `relay_rename_route`, `relay_location_route`, `relay_stop_route`, `relay_start_route`) and `stop_app` / `start_app` answer `403 {"detail": "This is a preview of a proposed change; it cannot change the live workspace."}` when `is_preview`. New routes: `GET /api/updates/pending` (the notice record, or `null`), `POST /api/updates/pending/confirm`, `POST /api/updates/pending/rollback` (runs `python3 .agents/skills/update-self/scripts/update_self.py rollback-last` detached, answers 202; refused in a preview shell).
- `shell/update_notice.py` (new): reads and watches `data/.state/update-apply/last-good.json` and the run record for the rollback's progress; broadcasts `update_notice_changed`.
- `shell/inventory.py` / the inventory document: `is_preview: bool`.
- `server.py`: the unknown-`/api` 404 (carried).
- `ws_broadcaster.py`: `update_notice_changed`.
- `update_staleness.py`: unchanged; the preview shell's staleness banner is suppressed (`is_preview`).
- Frontend: `models/UpdateNotice.ts` (fetch, socket, confirm, rollback with its confirmation dialog, progress and outcome states); `views/UpdateNoticeBand.ts` rendered inside every `IframePanel` whose app is named in the record, and `views/UpdateNoticeBanner.ts` for the shell itself beside `UpdateStalenessBanner`; `tabMenu.ts`, `Sidebar.ts`, `AllAppsPicker.ts`, and `NewTabLauncher.ts` hide the mutating verbs when `is_preview`; tests beside each.
- `shell/routes_test.py`, `test_e2e.py` (the notice band from a seeded record over stub apps; the preview refusals), `test_project_ratchets.py` (no new mngr imports, no app names), `test_embed_ratchets.py`.
- `README.md` and the changelog entry.

### `system/apps/terminal`

- `main.py`: `--no-register`; the ttyd port already follows `--app-url`; the state directory and store options exist. `README.md` and the changelog entry.

### Contracts amendments (`../workspace-app-model/contracts.md`, in place)

- Section 2: the `[preview]` table and its defaults; the built-in manifests table gains a `preview` column.
- Section 4.3: the chat's `/api/health` `agent_events` field.
- Section 5: `GET /api/updates/pending`, `POST /api/updates/pending/confirm`, `POST /api/updates/pending/rollback`; a preview shell's 403 on the relay verbs.
- Section 8: `update_notice_changed`.
- Section 9: `is_preview` on the inventory document.
- Section 15: `agent-observer` in the bands.
- Section 17: `data/.state/update-apply/last-good.json`; `data/.state/isolated-instances/<name>/copies/`.

### Changelog entries

`.agents/changelog/gabriel-critical-app-editing.md`, `system/changelog/…`, `system/apps/chat/changelog/…`, `system/apps/system_interface/changelog/…`, `system/apps/terminal/changelog/…`, `system/libs/app_manifest/changelog/…`, `system/services/oom_priority/changelog/…`; in the mngr repo, `dev/changelog/…` for the spec plus the entries the old branch already carries.

## Implementation phases

1. **mngr: the read side lands.** Merge the old mngr branch into `gabriel/critical-app-editing`; add `require_writer=False` and its tests; open the mngr PR; regenerate `system/vendor/mngr/` here from it. Working system: nothing in the template changes yet.
2. **The observer program and the chat as a follower.** `agent-observer` in supervisord and the bands; the chat's agent manager follows the file; `--secondary`, `CHAT_DATA_DIR`, and the health field; the chat's tests. Working system: the workspace runs as before, and a killed observer no longer freezes the chat.
3. **Previews for every app.** `PreviewSpec` in app_manifest; the three built-ins' tables; the scaffold's table; the carried isolated-instance work plus copies, named ports and inner path; `preview_app.py`; the shell's `--preview` refusals and hidden verbs; the terminal's `--no-register`. Working system: `preview_app.py up --app chat --worktree <dir>` shows a secondary chat in a labeled tab; the same for the shell and the terminal.
4. **Routing and the careful flow.** update-app's manifest step and `references/critical-app.md`; `create_worker.py --branch`; the update-creation, heal-creation, worker-reference, interactive-delivery, manage-layout, CLAUDE.md, and build-app edits; delete update-system-interface and its script. Working system: a critical app change runs end to end through preview, harden, and the existing apply.
5. **The kept rollback point and the notice.** `LastGoodRecord`, `--keep-rollback-point`, `rollback-last`, `confirm-last`, the settled verdict; the shell's routes, watcher, socket message, band and banner; the reference's notice section. Working system: an apply from the careful flow raises the notice, and both buttons work, including a rollback that restarts only the chat.
6. **Docs, contracts, and validation.** The contracts amendments, READMEs, changelogs; the manual scenario run in a real workspace with its findings recorded in this folder; fixes from it.

## Testing strategy

### Unit tests

- `app_manifest`: `PreviewSpec` defaults per app name, placeholder validation, undeclared port names, the built-ins' tables loading.
- `serve_isolated_instance_test.py`: refresh keeps the port and wrapper, `down` escalates and refuses to drop state over a survivor, the boot-log excerpt is scoped to the last marker, copies land in the scratch space, named ports resolve.
- `preview_app_test.py`: placeholder resolution against a fake manifest, the registry copy with rewritten sibling rows, `--with` boot order, the one-pass guard.
- `create_worker_test.py`: the carried `--branch` and `read_worker_branch` cases.
- `update_self_test.py`: the record written only with `--keep-rollback-point`; a plain apply discards a previous record; `rollback-last` restores snapshots and restarts exactly the recorded programs; the `needs_services_restart` case skips the restart and records the command; `confirm-last` discards; `wait_settled` resets on a pid change and passes on a stable streak across several programs.
- Chat: the follower fold over events written by mngr's writer (a full snapshot, a state change, a removal, a truncated file); boot with no writer holding the lock reports degraded and folds once a snapshot arrives; the observer dying mid-run keeps the last list and reports degraded; `--secondary` reconciles nothing, registers nothing, nudges only the given URL, and refuses OOM writes; `CHAT_DATA_DIR` moves the stamps and the instances store.
- Shell: the preview refusals on every relay verb and app verb; the notice routes over a seeded record; the rollback route spawns the script and answers 202; the inventory's `is_preview`; frontend vitest for the band, the banner, the confirmation dialog, and the hidden verbs.
- Bands: `agent-observer` resolves to the chat's band.

### Integration and system tests

- `test_chat_system.py`: the two-server fixture with a real `mngr observe` writing the events file; the chat's instances API lists the agents the observer reports; restarting the observer under the chat leaves the list intact and the health field flips degraded and back.
- Shell `test_e2e.py`: over stub apps, a seeded record renders the band on the right tabs and the banner for the shell; "Everything seems good" clears it everywhere; a preview shell hides the verbs and refuses them.
- Terminal: `test_terminal_app.py` boots with `--no-register` and two custom ports.
- The apply's tests over a fixture tree: an apply with `--keep-rollback-point` leaves snapshots and the record; the rollback path restores them.

### Manual scenario (run once before merge, findings recorded here)

- A chat change (a composer behaviour) through the careful flow: preview opens on the lead's own conversation, a message sent from the preview reaches the agent, harden, apply, the band appears on every chat tab, "Roll back" restores the previous chat and only the chat program restarts, then the same change re-applied and confirmed.
- A shell change (the New Tab page) through preview, harden, apply, the banner, and confirm.
- A workspace_ui change previewed as the shell with the chat.
- A terminal change through preview and apply.
- `supervisorctl restart agent-observer` under a live chat; `supervisorctl stop agent-observer` for a minute.
- A second pass started while a notice is open.

### Edge cases

- The observer not yet up when the chat boots (supervisord starts them together).
- A snapshot larger than the atomic append size mid-write.
- A preview started while another pass's preview is up.
- A rollback that touches the shell: the shell answers, then restarts itself; the browser reconnects and the band shows the outcome.
- A rollback into a tree without `agent-observer`.
- The kept snapshots' disk use if a notice is never closed.
- `MINDS_APPS_FILE` respected by every registry reader the preview shell uses (the inventory, `layout.py`, `forward_port.py`).

## Open questions

- **Relaxing the harden gate for contained changes.** With the notice and the kept rollback point as a net, a copy tweak on a critical app could apply right after approval and harden afterwards, like non-critical apps. Not in this plan; revisit once the notice has been used in anger.
- **A lighter chat preview.** If a secondary chat proves too heavy to boot per iteration (it starts every agent's harness session), a frontend-only preview serving the new bundle over the live chat's API through a vite preview proxy is the fallback. Not planned.
- **A notice for update-self applies.** A template update touches every critical app at once; today it keeps its own run record and the minds app's modal. Whether it should raise the same notice is open.
- **A safety net for live heals.** A heal that edits a critical app live has no rollback point and no notice. A cheap middle ground (commit first, raise the notice with that commit as the point, restart the program on rollback) was considered and left out.
- **The observer's band.** `25` puts it beside the chat. Whether it should sit at the shell's band (`20`), since the chat is useless without it, is a judgment call for the drill.
- **How long to keep snapshots.** They are kept until the notice is closed. A notice nobody closes keeps node_modules and tool-environment copies on disk indefinitely; a nag or an expiry after days may be wanted.
- **Where the agent learns the outcome.** A rollback's outcome goes to the run record only. If leads keep missing it, messaging the driving agent is the next step.
- **The chat's health contract.** `agent_events` on `/api/health` is additive and only present with a manager; whether the instances record should carry a degraded status directly rather than through the shell's fetch-failure path is open.
- **Preview sends reaching the minds inbox.** A permission card raised by a message sent from a preview chat reaches the live chrome through the preview shell's relay. Probably right; confirm in the manual scenario.
