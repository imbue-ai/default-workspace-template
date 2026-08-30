# Open threads on the workspace-update work

The one tracker for what is still open across the combined update branch
(`gabriel/tactful-swift`: mngr-internal PR #639 + the paired
default-workspace-template PR #501). It covers both halves: the Minds-app side
(detection, badges, modal, dispatch, scheduling, the apply window) and the
template side (the atomic `apply`/`recover`, the marker, the staleness banner,
the unattended pass). The template repo's `docs/system/blueprint/safe-update-apply/`
holds only that side's spec and points here for anything unresolved.

Resolved items are dropped rather than struck through; the decisions worth
remembering are in the last two sections. Claims about the code were checked
against both branches on 2026-08-28.

Some items carry a transcript pointer for the conversation that raised them,
relative to two Claude Code project directories on the author's machine:

```
$APPUX  = ~/.claude/projects/-Users-gabeguralnick--sculptor-workspaces-c33085dee20840d98c1ca23160e2d918-code
$SAFETY = ~/.claude/projects/-Users-gabeguralnick--sculptor-workspaces-b6927d89d7524b0a9f580c610653299d-code
```

---

## 1. Follow-ups that are decided but not built

### 1a. Reconcile the host env file from an update (needs mngr + minds work)

From the geebspace regression (Sentry `a48711fe73aa415a81256cb337def87d`,
minds 0.4.2): `minds-v0.3.12` retired the secondary latchkey gateway, so the
desktop app stopped creating the port-1990 tunnel, but the workspace's host env
file, written once at `mngr create`, still named it. A user app read
`LATCHKEY_GATEWAY_SECONDARY` with no fallback and served stale data for eight
days. Every gate the branch adds would still pass on that update.

Why it is the update flow's problem: `_write_host_env_vars`
(`libs/mngr/imbue/mngr/api/create.py`) is called only from host creation; the
values are `--host-env` flags the desktop app builds from
`prepare_agent_latchkey`'s `latchkey_env` (`agent_creator.py`), so nothing in
the template's tree names them and update-self cannot see them. It also pins
`_materialize_legacy_override_targets` in `mngr_latchkey/remote_gateway.py`
("delete once no workspace predates the one-gateway rollout") forever.
`Host.set_env_vars` exists on both ends; what does not exist is a caller.

Decided shape: the updating agent asks the app to reconcile it, over the
latchkey gateway's `minds-api-proxy` that `update-self` already uses for
`GET /api/v1/app/version` (whose baseline grant exists for exactly the
unattended-worker case). Pieces:

1. A mngr CLI surface for host env on an existing host (a thin wrapper over
   `Host.set_env_vars`; `--host-env` exists only on `mngr create` today).
2. `GET /api/v1/workspaces/<agent_id>/host-env/check` first: returns the
   added / removed / changed key names without applying, so the worker can grep
   the workspace for each (this alone would have caught the regression).
   Degrade, don't fail, on an old app (record the gap in the report and
   continue; `/app/version` treats 403/404 as "predates the route"). Return
   names only: `LATCHKEY_GATEWAY_PASSWORD` and the override JWT are credentials.
3. `POST .../host-env/reconcile`, driven via `_run_mngr_blocking`, returning the
   diff applied. Defensible ungated because it is self-targeting and
   payload-free: the only reachable outcome is "match what a fresh create would
   produce today". It belongs before the apply's services-agent restart, since
   the env file is read only at process launch.
4. One permission entry (`workspace_permissions.json`, or a baseline grant on
   the `app/version` model) and the call from the worker with `$MNGR_AGENT_ID`.

Constraints: `prepare_agent_latchkey` is not idempotent (it mints a fresh
permissions handle and, for desktop-gateway hosts, a new override JWT; only
`LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE` mints, the rest is deterministic), so
factor the pure part out. Drift is bidirectional: geebspace's env was also
*missing* `MINDS_VIA_DESKTOP_URL_PREFIX`, so removal-only is not enough.

Open decision: which keys mngr owns. `agent_setup.py` names only the current
constants, so "keys I no longer emit" cannot be computed from current code.
Prefix ownership (mngr owns `LATCHKEY_*` in the host env file and replaces that
subset each provision; verify first that nothing else writes a `LATCHKEY_*`
host-env key) looks right, with an explicit retired-keys list carrying a
`CLEANUP:` marker per entry as the fallback.

### 1b. A post-apply pass over the live workspace (template)

Every gate runs pre-apply and off-live: §4b validates in the worker's worktree,
and the apply's own gates are `_preflight` (the merged backend on a throwaway
port) and the frontend probe. Nothing probes the user's own apps against the
live workspace after the apply, which is where the geebspace regression lived
(a live host env var, not a file). A verdict of `UPDATED` is about the merge; a
user app the update quietly broke does not enter it, and the refresh pipeline
keeping its last good copy on failure makes a dead integration look like
"nothing changed".

Decided: the pass must not add to the user's wait, so it is a background step
after the verdict is recorded that upgrades an `UPDATED` verdict (or appends to
its detail) when it finds soft failures. The cheap version is the widened
`check-app-errors` skill (already on the branch: every log including stdout,
lowercase soft-failure prose, recently-changed logs, scheduled jobs reconciled
against their logs). Build it once that widened check has been exercised on a
real update.

### 1c. Dedupe the staleness detector's copies (template, SI side)

The decision against splitting `update_self.py` into a `system/libs` package
stands (see §3). What survives of that finding is SI-side only:
`update_staleness.py` carries a narrower copy of `classify_path` because it
cannot import from the skill, and the marker path is a literal in
`update_staleness.py`, `bootstrap/manager.py`, and
`update_apply_contract.py`. Dedupe in the SI's direction, without the skill
importing anything.

### 1d. `strict=False` covers only the in-process read (needs a mngr PR)

Only `agent_discovery.py`'s `load_config` call is relaxed. `mngr
start/stop/create/destroy` subprocesses still parse settings strictly, so the
update-time lockout persists for those. No such change is on this branch.

> `$SAFETY/e2c6f899-5a71-402f-9556-872ac5f1158d.jsonl`, agent
> `tsk_01m0tcbfryf599fcwp70e30tdm`, `2026-08-24T19:28:44Z`.

---

## 2. Product and verification threads

- **Two adjacent settings sections named "Updates" and "Machine updates"**
  (`SettingsSections.ts`: the app's own auto-updater vs. the workspace night
  window). A rename was suggested as a product call.
  > `$APPUX/a61b083d-39d9-48b7-b734-a569133ca865.jsonl`, agent
  > `tsk_01m0tcb2gwexrs46dzjkj2fwdx`, `2026-08-24T17:29:51Z`, last paragraph.
- **A "Cancel update" for a run in flight**, deferred 2026-08-26. The modal
  cancels a *schedule*; nothing cancels a run. Wanted: a press mid-run that
  destroys the run's chat agent and puts the row back to IDLE explicitly, so
  the next poll does not file STALLED and show "Update failed". Open points:
  STARTING has no agent yet, so a cancel there is a flag the dispatch checks
  after its spawn returns; APPLYING should refuse; destroy takes the chat's
  transcript with it.
- **Repeated silent skips of "Update tonight".** Modeled on iOS: an
  unreachable machine or one with agents working is skipped and re-armed, and
  the modal shows the last skip reason. Undecided whether repeated skips should
  eventually escalate beyond that line.
  > `$APPUX/07744cac-c50b-40a9-8692-4e6cb0c85c67.jsonl`, agent
  > `tsk_01kzyqrz1bev88st4cbf97sjqz`, `2026-08-14T17:30:45Z`, item 3.
- **The modal's version list with dates.** Shipped link-only; a release CI
  step that synthesizes a changelog was floated and never specced.
- **A badge-opened update modal is not mutually exclusive with the recovery
  card.** An auto-raised recovery card is; a modal the user opened by hand is
  not.
  > `$APPUX/54894249-3893-456b-8cd1-137a1364c5a0.jsonl`, agent
  > `tsk_01m0ttrt4mf51rzy0wy9vq8zd0`, `2026-08-24T21:59:56Z`.
- **The auto-open tab race is narrowed, not closed.** For a stopped machine the
  backend (probe + create) and the frontend (poll + page load + WS connect) are
  both waiting on the same host; nothing guarantees the frontend wins, and
  neither side has been measured. The deterministic alternative (an explicit
  `layout.py open` from the app) was ruled out as unacceptable coupling.
  > `$APPUX/dcfb5a48-8b7a-4fc4-8abe-c9126268c6e0.jsonl`, agent
  > `tsk_01m0wxtc0jez1rwwphb92mz4eb`, `2026-08-25T18:51:12Z`; the ruling is in
  > `$APPUX/f862ce3b-fb02-4347-bb43-c65161dced86.jsonl` at `19:05:14Z`.
- **No manual Electron verification of the newest UI work**: the liveness-poll
  badges ("Preparing update…", "Updating…", "Waiting for you") and the
  version-override settings group.
- **The customization-survival hold** (intact / intact-but-changed /
  cannot-be-kept; only the third holds) has not been through real updates. If
  holds prove too frequent or too rare, the lever is the worker guide's
  adapt-first wording.
- From the safe-update-apply spec: whether the boot log is enough of a fallback
  when the DRI wake fails post-recovery (the snapshots should have restored
  `mngr`); and whether the staleness banner needs any affordance beyond text
  without becoming an action surface.

---

## 3. Decisions worth remembering

- **One channel, `run.json`.** The app reads a single file
  (`data/.state/update-apply/run.json`, written by `update_self.py run-status`)
  in the same exec that lists the run's chat agent. The event stream, the
  `update=true` label, the `update` source in mngr_forward, and the consent
  tiers (`UpdateConsentKind`, the 428 handshake) were all deleted. The apply
  mirrors its marker phase and restamp into the record, so the app never reads
  the marker; the apply window is sized off that restamp plus the template's
  recovery grace, with the fixed 360s only as a fallback for an unreadable
  restamp.
- **No `system/libs/update_apply` split.** The skill's first step stages the
  *target* version's copy of itself and runs from it, so the apply must stay
  self-contained: an import would resolve against the old tree, or no tree
  during recovery. The test reaches the module by path ~217 times, and a split
  adds an import-resolution failure surface to the one program that must work
  when the tree is broken.
- **Whole-merge revert on any apply failure is retained** despite Incident A's
  blast radius; the half-applied alternative is worse. The one exception: a
  failed provisioner run alone does not roll back (the tree and services stay
  consistent, the re-run is cheap); the apply continues to the restart and
  probes and, if they pass, lands with `provision-incomplete.json` and a loud
  stderr line, exit code still 0.
- **A stale or unstamped `--worker-bundle` falls back to a live build** rather
  than failing the apply: failing would turn a passable apply into a
  whole-release rollback whose retry needs a fresh worker pass, and
  update-system-interface's ordinary merge makes the worker's bundle
  legitimately stale. A *live* build that does not match the merged tree does
  fail before restart.
- **Every apply restarts the services agent**; the per-path restart rule was a
  list nobody could keep complete.
- **A machine below `minds-v0.3.10` is badged "Recreate to update" up front**;
  the app carries no migration machinery, and every short-ending verdict is one
  "Update failed" outcome pointing at the agent's chat.
- **Scheduling an UNKNOWN machine for tonight** means an unattended run may
  land a merge from an upstream Minds cannot name; full parity was chosen over
  attended-only. The one place the design widens what runs unwatched.
- **Prerelease tags** are ordered by the parser but none exist (the
  release-channel manifest rejects them); revisit when the canary channel's tag
  shape is decided.
- **The bug-report collector** now attaches every agent's transcript written
  inside the recency window, workers included, so a successful pass `mngr
  stop`s its worker rather than destroying it.

---

## 4. Incident background

The template side was shaped by three real updates; the flow-level fixes are
all on the branch and described in the `.agents` changelog entry.

- **Incident A** (a minds workspace updating to minds-v0.4.1 under the old
  flow): the reveal silently failed, its `--rollback-to` reverted the entire
  2,527-file update, a retry reported "nothing to reveal" over the reverted
  tree, and the user had a broken chat interface for ~55 minutes while the
  agent claimed success twice. Source of: the atomic apply, the exit-code
  contract, `_has_rollback_since`, the ledger written post-success only, the
  `strict=False` read, the staleness banner, per-phase timings and budgets.
- **Incident B** (Sentry `4cf0919b9dc74b8f98ef9bc049e9bb66`, geebspace): a
  live re-provision hit the Claude installer following `HOME=/home/user` while
  the check read `/root/.local/bin/claude`, and `bunzip2 -c >
  /usr/local/bin/restic` truncating a binary `host_backup` was executing
  (ETXTBSY). Source of: `HOME=/root` in `setup_system.sh`, the
  decompress-then-rename install, the provisioner's canonical env, the
  provisioner-failure-does-not-roll-back rule, the live re-provision test in
  `apps/minds/test_snapshot_resume.py`, `norecursedirs = ["data"]`, and the
  `classify-merge` degenerate-base refusal.
- **The geebspace regression** (§1a above): a retired env var nobody grepped
  for. Source of: `with_agent_env.sh` exporting the full system PATH (its
  `/root/.local/bin:$PATH` had hidden `/usr/local/bin/latchkey` from every
  cron job: 256,654 failures and zero successful runs since 2026-08-03), the
  worker's impact analysis enumerating `system/vendor/**` and the vendored
  changelogs, and the widened `check-app-errors`.
