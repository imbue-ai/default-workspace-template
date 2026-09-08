# Plan: inner-workspace-updates — outer-app UX for updating out-of-date workspaces

## Overview

- The minds app detects workspaces whose template version is behind the app's pinned template ref (`FALLBACK_BRANCH`) and offers to update them from the landing page and workspace view, replacing the "ask your agent to update itself" folk remedy.
- Existing plumbing is reused: the `original_minds_version` create-time label and `GET /api/v1/workspaces/<id>/version` for detection, the app-version ceiling route the update-self skill already reads, and the assist-style dispatch (`mngr exec` wrapping an inner `mngr create --message "/update-self"`).
- The one new cross-boundary contract is a run-status file: update-self records each pass's start (chat agent name, unattended flag) and its one terminal verdict at `data/.state/update-apply/run.json` (`update_self.py run-status`), and the app polls it over `mngr exec` in the same probe that lists the run's chat agent. One pull channel, no event stream; a hand-launched run is discovered from the same file. There is no mechanical "recreation required" predicate; non-applicable updates remain agent judgment and the app consumes the verdict.
- The update's reveal step takes the workspace's system services down on purpose (`mngr start --restart system-services` bounces tmux, bootstrap, and supervisord; `reveal_system_interface.py` bounces the interface). Health/recovery stays normal through the long prepare phase; only a deadline-bounded apply window, armed on any sighting of the apply in the run record, suppresses STUCK accounting and recovery dispatch (the misdiagnosis class of mngr-internal#427).
- Unattended runs ("update tonight", bulk) rely on safe-update-apply's atomic apply and the skill's fully unattended design; its one mid-flight hold is the customization-survival gate, which applies to attended runs identically. The update chat agent is the record and offers post-update rollback; the app has no rollback UI. On a machine without backups, Update now / Update tonight asks in place at the button.
- The dwt-side work stacks on the safe-update-apply branch (itself on dwt PR 409) and may modify that spec; this spec owns the outer-app side plus the run-status and unattended-mode contracts.

## Expected behavior

### Detection

- Each workspace resolves to a tri-state: up to date, out of date, or unknown.
- "Out of date" requires a positive read: a `minds-v*` version (`current_minds_version` from git when running, else the `original_minds_version` label) semver-below the app's `workspace_template_ref` ceiling.
- A non-`minds-v*` value on either side yields no update UI (dev builds report a branch and impose no ceiling).
- Everything else is "unknown" (stopped legacy workspaces without the label, custom-template workspaces, tagless clones). Unknown rows get a passive badge whose explainer modal says why and suggests starting the workspace or asking an agent inside; it still offers to send the workspace's own update agent in to look, which is a check, never an assumed update. Unknown rows are excluded from bulk actions.
- Reverse divergence (workspace newer than the ceiling) shows a quiet "this app is older than this workspace; update the app" notice; nothing to run.
- Detection is a cached background pass; version reads are never issued per-row at render time.

### Landing page

- Out-of-date rows show a warning badge plus a button opening the update modal.
- When more than one workspace is confirmed out of date, "Update all now" and "Update all tonight" appear at the top of the page.
- An in-flight row shows the run's phase ("Preparing update…" while it builds in its own branch and worktree, "Updating…" once the apply lands) and disables Stop/Restart/Destroy. Badge and lock are probe-based, not timer-based; a probe-confirmed stall unlocks the row and surfaces the failure UX.
- A liveness poll visits each in-flight run (one `mngr exec` per run, every ~20s, reading the run-status file and the chat agent's lifecycle state together; STARTING is never probed). The record's verdict ends the run, its apply fields arm the apply window, its hold (with reason) moves the row to waiting at once, and the agent's state settles the rest. An agent gone with no verdict is a stall immediately. An agent idle across two consecutive polls flips the badge to a warn-toned "Waiting for you" that opens the same modal and keeps the row locked; the agent moving again restores the phase label with no debounce.
- A row with a pending tonight-run shows a small "update scheduled" indicator.
- After a successful unattended run, a dismissible per-workspace note ("updated to minds-vX.Y.Z last night") points at the chat summary.
- Mixed bulk outcomes surface per-row only; no aggregate banner.
- Remote rows (synced from another device) show no update UI; detection, actions, and scheduling state are local to the hosting device.

### Update modal

- Minimal v1: current vs. supported version, the version list with dates, a link to the template repo's changelog on GitHub, and a brief credits-cost note. A synthesized changelog is a follow-on.
- Buttons: "Update now" and "Update tonight". A scheduled workspace also shows the last skip reason, the next attempt window, and a cancel action.
- The modal only offers the newest supported version. A quiet "Update to a different version…" link closes it and lands in the machine's Settings → Updates group with the specific-version field open.

### Choosing a version (explicit target)

- Settings → Updates is the full update control: both versions, "Update now", and a collapsed "Update to a specific version" disclosure accepting a tag, `main`, or a git ref. It is available on every machine, up-to-date ones included, since an explicit target needs no reading from the app.
- The override rides the attended seed prompt as prose, like the unattended pre-authorization: it names the target and states that the user chose and already confirmed it, and names no flag or step of the skill (the skill re-points itself at the target version's copy of its flow). The version field's copy warns that such a target may not be a tested release and may break the machine; the press is the confirmation. The run still judges whether the ref exists.
- Attended-only: scheduled runs and bulk actions never carry an override.
- On a dev build (branch ceiling) the field is prefilled with the build's own template ref as the workspace's fetch resolves it (`upstream/<branch>`; `main` bare).
- The dispatch route accepts the ref in its body, refuses one that could read as a flag or shell text, and skips the availability gate exactly when a ref is named.

### In-workspace surfaces

- Out-of-date state appears in the shell's notice band (ranked below discovery and health conditions) and on the hub page via the same decision function.
- The band also reports a run: preparing, applying, waiting-for-you, and how the last run ended. The apply leg ranks above the workspace's own health (the app took those services down itself); every other leg ranks below health and above the standing out-of-date line.
- Nothing raises the update modal on its own; it is reached from the band's "See update".

### Update now (attended)

- Dispatch is assist-shaped: probe that the workspace has the update-self skill, then `mngr exec` an inner `mngr create` chat with the `/update-self` message. The app navigates into the workspace and the chat tab auto-opens.
- The spawned chat carries `assist=true` (old system interfaces auto-open it); the new system interface generalizes auto-open behind a purpose-neutral label. Nothing else identifies the run: the skill records its own chat name in the run-status file, so a hand-launched run is identified identically.
- No gate on concurrently running chats.
- A stopped workspace is auto-started first.

### Update tonight / unattended runs

- "Update tonight" records app-side intent; a background loop fires within a night window (default 2:00–5:00 a.m. local, configurable app-global in settings).
- Runs are opportunistic, iOS-style: skipped silently and re-armed when conditions are unmet (app not running, workspace unreachable, chats actively running). Skips are visible in the modal.
- The scheduling flow notes that stopped workspaces will be started; any provider may be auto-started, and a tonight-run restores the prior run state afterward.
- No consent tiers: the unattended seed message says only what the skill cannot know itself (pre-authorized, user away). On a machine without backups, the press that starts or schedules the run gets an in-place confirmation (each row's payload carries `is_backup_configured`).
- Unattended runs still create the visible auto-opening chat tab; the agent's completion message summarizes what changed and offers rollback.
- A real failure (stuck, rolled back) cancels the schedule, with no automatic retry, and surfaces through the failure UX.
- "Update all now" runs unattended and immediately for all confirmed rows, with the same no-backup confirmation when any covered machine lacks backups.

### Status, verdicts, and failure

- update-self records itself in `data/.state/update-apply/run.json` (`run-status`): the pass's start (chat agent name, unattended flag, started-at), then exactly one terminal verdict: `UPDATED`, `UPDATED_WITH_REBUILD_ITEMS`, `ALREADY_CURRENT`, `NEEDS_RECREATION`, `STUCK`, `REFUSED` (with the newest in-place-compatible ref when one exists), plus a plain-language detail line and the resulting ref. A new run's start supersedes the previous record. No progress reporting; live progress belongs to the chat tab.
- The liveness poll reads the record for in-flight rows and lands the verdict (updating badges, re-reading the version); the detection sweep reads it for idle rows, which is how a hand-launched run or a verdict written while the app was closed reaches the row.
- Verdict-level failures surface in the app only; Sentry reporting of verdicts is not shipped.
- A failed or needs-recreation outcome shows on the landing badge and on the notice band; the modal carries the verdict and opens from either.
- Every short-ending verdict (`STUCK`, `REFUSED`, `NEEDS_RECREATION`) and a stall are one "Update failed" outcome whose copy says to check in with the update agent in its chat. The app carries no migration machinery: a machine that cannot be updated in place is caught before any run, by a hardcoded cutoff (`minds-v0.3.10`, `OLDEST_IN_PLACE_UPDATABLE_VERSION`) in detection. A workspace whose read version sorts below it gets the `NEEDS_RECREATION` availability regardless of the app's ceiling: a "Recreate to update" badge, a standing band line, and a modal/settings explainer with the two steps (create a new machine; ask its agent to run `/migrate-workspace from <old>`), and it is never dispatchable, scheduled, or covered by bulk actions.

### Health probes and recovery during an update

- During the prepare phase the live workspace is untouched and fully probed; health/recovery behaves exactly as today, recovery dispatch included. The app steps back only for the apply step.
- The apply's reveal fails the system-interface probes for longer than the stuck threshold; without handling, the tracker goes STUCK, `UnattendedRecoveryDispatcher` fires a start-only restart, and the user sees "Restarting" (eventually `recovery_failed`) on a healthy mid-apply workspace, as in mngr-internal#427.
- The apply window opens on any sighting of the apply in the run record and is deadline-bounded, sized off the apply's own restamp time (`apply_updated_at`, mirrored from the template's marker on every phase) plus the template's recovery grace, so a slow-but-alive apply that keeps restamping keeps its window. Inside it: a probe grace (the create-attempt-grace pattern; failures do not accumulate toward STUCK), no recovery dispatch, and the UI reads as applying. It closes on the terminal verdict, a probe success, or the deadline.
- The liveness poll's sighting is the normal path. For the race where the reveal's outage lands between polls, the dispatcher gets a guard: on a stuck edge for a workspace whose update state is in-flight, it does a one-shot read of the run record over exec (which works while the interface is down). Apply under way: arm the window and decline. No apply: dispatch normally. The record is the app's one authority; the template's own apply marker is never read by the app, and the apply's phase order guarantees the record is stamped before anything can disturb the interface.
- On deadline expiry everything reverts: failure accounting restarts from scratch, so a genuinely wedged reveal reaches STUCK and dispatches recovery. Because the stuck edge fires once per outage episode, the race path (edge fired, dispatch declined) would never see a second edge, so when the window expires with the workspace still STUCK the update machinery invokes `dispatch_host_restart` directly.

## Implementation plan

### minds desktop client (backend, `apps/minds/imbue/minds/desktop_client/`)

- New `workspace_update_state.py`: detection pass and cache.
  - `MindsVersion` parsing/comparison over `minds-v*` tags (semver ordering; mirrors `update_self.py`'s tag regex and `_compute_code_state` in `backup_workspace_scripts.py`).
  - The store keeps detection and run facts as separate slices (the run slice holds the run's own `run.json` record whole) and composes them, with the schedule store's armed record, into the wire model `UiWorkspaceUpdate` on read; there is no intermediate state model and no publish step for schedules.
  - Background pass on its own loop (pattern: daemon loops registered in `app.py`), reading `original_minds_version` via `BackendResolverInterface.get_agent_label` and, for running workspaces, `workspace_version.read_workspace_git_version`; caches results, refreshes on landed verdicts and lifecycle changes; the same sweep reads the run-status record for idle reachable rows.
- New `skill_chat.py`, shared by the get-help and update chats: the skill-presence probe (`.agents/skills/<skill>/SKILL.md`, sentinel-based, `--no-start`) and the inner `mngr create` run through `mngr exec`, with labels `assist=true` + `auto_open=true`. `update_chat.py` and `assist_chat.py` only build their seed messages: `/update-self` plus, when they apply, the version-override note and the no-backups go-ahead; `/assist <description>`. `update_chat.py` also auto-starts a stopped workspace (`mngr start`) before dispatch.
- New `update_status.py`: the run-status contract (wire enums plus the lenient `run.json` parser). `update_apply_window.py` owns the combined probe (run record + agent listing in one exec) and the apply window (armed on apply sightings, closed on the verdict or deadline; on expiry with the workspace still STUCK, invoke `dispatch_host_restart`). The liveness poll in `update_service.py` drives verdicts and stalled-vs-updating from the same probe.
- `system_interface_health.py`: generalize the create-attempt grace into a purpose-labeled deadline-bounded probe grace for the apply window.
- `workspace_recovery.py`: `UnattendedRecoveryDispatcher` gains the race guard as an injected check from the update machinery.
- New `update_schedule_store.py`: persisted tonight-intent records (agent id, created-at, last skip reason/time), atomic JSON file in the app data dir (pattern: `pending_create_attempts.py`).
- New `update_scheduler.py`: night-window loop (default 2:00–5:00 local, configurable), per-workspace skip conditions (unreachable, active chats via the running-chats gate probe from `backup_update.py`), prior-run-state capture/restore around auto-start, unattended dispatch, schedule cancellation on real failure.
- Routes (`api_v1.py` or `app.py`, wherever the assist route lives): dispatch update-now, schedule/cancel tonight, bulk now/tonight, and read endpoints for update state if it does not ride the channel.
- `ui_models.py` / `ui_publisher.py`: per-workspace update state to the frontend (badge tri-state, updating, scheduled, verdict, last-night note).
- Sentry verdict reporting: not shipped.
- Migrate handoff: `mngr message <update-agent>` via `MngrCaller`.
- Settings: night-window value in the app's settings storage, exposed in the settings overlay.

### minds frontend (`apps/minds/frontend/src/`)

- `models/updates.ts`: store mirroring per-workspace update state with optimistic pending state reconciled against pushed state (pattern: `MindLivenessTracker` in `models/create.ts`); `updateRunPhase`/`updateRunOutcome` as the one producer the row badge and notice band both read.
- `views/pages/LandingPage.ts`: out-of-date/unknown/updating/scheduled badges in `liveRow` (via `StatusBadge`), row-action disabling while updating, the dismissible updated-last-night note, bulk actions at the top.
- New `views/components/UpdateModal.ts`: version comparison, version list, changelog link, cost note, now/tonight buttons, schedule state, verdict display, needs-recreation actions.
- New unknown-version explainer modal; quiet app-older-than-workspace notice.
- `views/shell/notice-band.ts`: new `workspace-out-of-date` key + `update-workspace` action, ranked below discovery and health, plus a key per run phase and one for a run's outcome.
- `views/shell/shell-state.ts`: inside the apply window, health-driven recovery surfaces (STUCK redirect, restarting banner) defer to the applying presentation; outside it they behave as today.

### default-workspace-template (stacked on the safe-update-apply branch; may amend that spec)

- `.agents/skills/update-self/SKILL.md` + `scripts/update_self.py`: the `run-status` subcommand writing `data/.state/update-apply/run.json` (start before anything else in the pass; the verdict from whichever section ends it); the marker-before-any-disturbance phase-order invariant stated at the phase constants; the completion message's rollback offer.
- `system/apps/system_interface/.../agent_manager.py`: generalize chat-tab auto-open behind `auto_open=true` while still honoring `assist=true`.

## Implementation phases

1. **Contract + detection (backend only).** Run-status schema; dwt-side recording in update-self; `workspace_update_state.py` detection and version comparison; state exposed to the frontend.
2. **Passive surfacing.** Landing badges, minimal update modal, unknown explainer, app-older notice, notice-band key. No dispatch.
3. **Attended update now.** `update_chat.py` dispatch + auto-start, probe-based in-flight tracking and verdict reads, row locking, verdict surfacing. Completes the v1 cut.
4. **Too-old detection.** The in-place cutoff in detection, the `NEEDS_RECREATION` availability, and the badge/band/modal explainer that replaces any app-side migration handoff.
5. **Tonight + bulk.** Schedule store, night-window loop, no-backup confirmation, settings entry, skip surfacing, bulk actions, morning-after note.

## Testing strategy

- Unit (minds, `_test.py`): version parsing/comparison and tri-state derivation (label vs. git precedence, non-`minds-v` refs, null label, newer-than-ceiling); run-record parsing and state transitions (hand-launched runs, dedup by run start, dismissal survival); dispatch arg builders incl. the unattended variant (pattern: `assist_chat`'s tests with `RecordingMngrCaller`); night-window and skip logic with injected clock/state; the stalled-vs-updating decision function.
- Unit (minds, health/recovery): the probe grace suppresses failure accounting until its deadline and restores it after; the dispatcher's race guard; the poll's apply sighting opens the window (sized by the restamp) and the verdict closes it; expiry-with-STUCK invokes the restart dispatch; a prepare-phase outage dispatches recovery normally.
- Unit (frontend): notice-band precedence with the new keys; update store transitions and optimistic reconciliation.
- Unit (dwt): `update_self_test.py` additions for the `run-status` start/verdict round trip and overwrite/lenience; `agent_manager` auto-open honoring both labels.
- Integration: the update-state field riding the channel end-to-end against a fixture workspace; dispatch route behavior for unsupported/unreachable workspaces (409/502-style, as assist does).
- Behaviors: `.feature` coverage in `apps/minds/behaviors/` for the detection tri-state, attended flow, and unattended runs including the no-backup confirmation.
- Manual: a real docker workspace on an old template tag updated end-to-end via the UI (badge, modal, dispatch, chat, verdict, badge clears), confirming no "Restarting" banner, recovery redirect, recovery card, or unattended-recovery dispatch during the reveal; tmux/screenshot checks for badges and modals; a needs-recreation dry run. Interactive checks stay manual.

## Open questions

Unresolved questions are tracked in `open-threads.md` beside this plan. Settled at implementation:

- Update state rides the channel as a field on `UiWorkspaceEntry`.
- The contract is the run-status file; `ALREADY_CURRENT` is its own verdict and not a failure.
- The apply window is sized off the apply's restamp time plus the template's recovery grace; the fixed constant survives only as the fallback for an unreadable restamp, and each poll's sighting re-arms it.
- The unattended pre-authorization is a message preamble only; the run-status file carries the unattended fact back.
- Prerelease tags are included in the semver ordering, mirroring `update_self.py`, but none exist (the release-channel manifest rejects them); revisit when the canary channel's tag shape is decided.
