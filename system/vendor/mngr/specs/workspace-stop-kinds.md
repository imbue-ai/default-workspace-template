# Workspace stop kinds: who stopped a machine, and who may start it again

Status: decided 2026-09-13 (Josh + the staging migration re-test); implemented on `mngr/gen2-ci-rollout-docs` (PR imbue-ai/mngr-internal#967); the staging connector carries migration 042 since deploy `20260914T135700Z`; a first live drill passed on staging on 2026-09-14 with a desktop that lacked the stop-kind desktop code but whose `mngr start` subprocess ran the new plugin (the watchdog dispatched, the plugin refused at once, the row never started; see `apps/minds/docs/deploy/history/minds-v0.6.0.md`); step 2 passed with the real 0.5.2 client on 2026-09-14 (its plugin waited out `stopping`, the connector's 409 refused both the watchdog's start and a Restart click, the migration completed) and step 1 passed the same day with a client built from this branch (the recovery gate declined on its live read, nothing was dispatched, the machine came back on gen-2 with its latchkey state); steps 3 and 4 of section 6 are pending.
Refines the workspace lifecycle in [`apps/minds/docs/deploy/reference/workspace-stop-start.md`](../apps/minds/docs/deploy/reference/workspace-stop-start.md) and the migration guard described in [`blueprint/slice-fleet-cutover/phase-5.5-incremental-rollout.md`](../blueprint/slice-fleet-cutover/phase-5.5-incremental-rollout.md).
Runbooks affected: [`apps/minds/docs/deploy/gen2-cutover.md`](../apps/minds/docs/deploy/gen2-cutover.md).
Audience: the agent implementing this across `apps/remote_service_connector`, `libs/mngr_imbue_cloud`, `apps/minds`, and `apps/minds_admin`.

## 1. Purpose and scope

An imbue_cloud machine can be stopped by four different actors with four different intents: the owner from one of their devices, an operator taking the machine away for maintenance (today: the gen-1 to gen-2 migration), an operator freeing capacity (today: `server drain`; later: idle shutdown), and the account suspension fan-out.
Today the connector runs one stop transition for all four and records nothing about which one it was.
Every client therefore renders every stopped machine the same way, offers Start for all of them, and its unattended recovery treats any of them as a wedge to be started again.

This spec adds a **stop kind** to the workspace lifecycle so that:

- the connector refuses owner starts of a machine an operator is holding, from the moment the stop is requested;
- the desktop shows *why* a machine is stopped, hides Start when the user cannot start it, and never auto-starts a machine whose stop was requested;
- operators pick the kind when they stop a machine, and can change it afterwards (an unsuspended account's machines become user-startable again).

In scope: the connector's lifecycle routes and schema, the `mngr_imbue_cloud` wire model and start path, the minds desktop (backend and frontend), and the `minds-admin` commands that stop machines.
Out of scope: the local (docker/lima) and bring-your-own-key cloud providers, whose hosts stop on their own and keep today's behavior; the mngr core (`DiscoveredHost` is deliberately untouched, see S8).

## 2. Background

### 2.1 The lifecycle today

A workspace is a `pool_hosts` row in one of `leased` (wire: `running`), `stopping`, `stopped`, `starting`, `crashed` (`apps/remote_service_connector/imbue/remote_service_connector/workspaces.py`).
The owner stop route, the operator stop route (`POST /admin/workspaces/{id}/stop`) and the suspension fan-out all run the same `_STOP_LEASED_WORKSPACE_SQL` CAS; the only trace a stop leaves is `stop_requested_at`.
The owner start route starts any `stopped` row the caller owns, except a gen-1 row in the cutover's *parked* shape (no placement, no manifest), which answers 409 `workspace_migrating`.
The desktop derives a machine's lifecycle badge from mngr's `HostState`, which the imbue_cloud provider maps from the wire status; it ignores `stop_requested_at` and `transition_error`.
The desktop's unattended recovery starts any shutdown-capable machine that stops answering unless the stop was made from inside that same app process (`SystemInterfaceHealthTracker.suppress_unattended_recovery`).

### 2.2 The incident this fixes

On 2026-09-13 the staging migration drill of `workspace-3` failed with the desktop open on that workspace.
The migrate's admin stop halted the VM; the desktop's open stream broke; the health tracker went STUCK after 8 seconds and dispatched an unattended `mngr start`; that start waited out `stopping` and landed the instant the row read `stopped`, before the migrate had copied the stop artifact and parked the row.
The restart in place deleted the artifact, the migrate's copy failed with NoSuchKey, and the workspace came back on gen-1 as if nothing had happened.

The parked-shape guard is structurally late: it engages after the migrate's S3 copy, and every actor that can start a `stopped` row (the owner on any device, the watchdog on any client version, a second device) has the whole stop-to-park window to do so.
Before this change `gen2-cutover.md` claimed "while a workspace is mid-migration its owner sees it stopped and start answers 409"; that held only from the park onwards (the runbook now describes the hold from the stop request).

### 2.3 Why a stop kind rather than a new status

A new wire status (`migrating`) would ripple through every supervisor, watchdog and sweep predicate that matches `status = 'stopped'`, and shipped clients would render it as "Status unknown" with an "update the app" remedy that is wrong for a migration.
A kind beside the status leaves the state machine alone: the row is still `stopped`, and the kind says what that means for the user.

## 3. Settled decisions (do not re-open)

| # | Decision |
|---|---|
| S1 | The status machine is unchanged. A nullable `stop_kind` column is added to `pool_hosts`, stamped by every stop and cleared by every start. |
| S2 | Kinds: `owner`, `maintenance`, `idle`, `suspension`. NULL (a row stopped before this change) means `owner`. |
| S3 | `maintenance` covers the migration and any other operator hold. The owner cannot start it; only an operator can. |
| S4 | `idle` is an operator stop to free capacity (`server drain`, future idle shutdown). The owner can start it by an explicit click. |
| S5 | `suspension` is stamped by the suspend fan-out. Unsuspending an account rewrites its `suspension` rows to `idle`. |
| S6 | The hold engages at the stop request: the operator stop route takes the kind, and the owner start route refuses `maintenance` and `suspension` rows from `stopping` onwards. The migrate's park-first reordering is dropped. |
| S7 | The desktop never auto-starts an imbue_cloud machine whose connector lifecycle is `stopping`, `stopped` or `starting`. A user can always click Start when the kind allows it. |
| S8 | The desktop learns the kind by reading the connector directly (`mngr imbue_cloud machines show`), not through a new field on mngr's `DiscoveredHost`. Discovery keeps owning the state enum. |
| S9 | A `maintenance` machine renders with the badge "Maintenance", no Start control, and the notice band "This machine is undergoing maintenance and will be back shortly." |
| S10 | Old (pre-fix) clients get the 409 with that same sentence when they try to start a held machine; one RECOVERY_FAILED card and one error report per outage on those clients is accepted. |
| S11 | Operators can change a stopped row's kind (`minds-admin workspaces set-stop-kind`), which is also how a held migration that has not yet parked its row is handed back without a rollback (a parked row stays refused by the parked-shape guard, whatever its kind, and has no artifact to restore from; `cutover rollback` is its way back). |
| S12 | The migrate refuses to run against a connector that predates stop kinds. |

## 4. Design

### 4.1 Data model

Migration `042_workspace_stop_kind.sql`:

```sql
ALTER TABLE pool_hosts ADD COLUMN stop_kind TEXT
    CHECK (stop_kind IN ('owner', 'maintenance', 'idle', 'suspension'));
```

No backfill: NULL reads as `owner` everywhere (S2).
The column is set by the stop CAS and by the kind route (4.2.3), and cleared (set NULL) by the start CAS.

| Kind | Stamped by | Owner start | Operator start | Desktop badge | Desktop Start control | Notice band |
|---|---|---|---|---|---|---|
| `owner` / NULL | `POST /workspaces/{id}/stop` | allowed | allowed | "Stopped" | shown | none |
| `idle` | admin stop with `kind=idle` (drain, idle shutdown); unsuspend | allowed | allowed | "Stopped" | shown | none |
| `maintenance` | admin stop with `kind=maintenance` (the migrate) | refused (409) | allowed | "Maintenance" | hidden | maintenance sentence |
| `suspension` | the suspend fan-out | refused (409; the account gates refuse earlier anyway) | allowed | "Stopped" | hidden | none |
| unrecognized (newer server) | -- | client refuses before calling | -- | "Stopped" | hidden | none |

The kind describes the *current* stop only.
It says nothing once the row is `leased`, so every start clears it and a later stop stamps it afresh.

### 4.2 Connector (`apps/remote_service_connector`)

#### 4.2.1 Stopping

`_STOP_LEASED_WORKSPACE_SQL` gains `stop_kind = %s`.
The owner route passes `owner`.
`begin_stopping_all_leased_workspaces` passes `suspension`.
`POST /admin/workspaces/{id}/stop` accepts an optional JSON body `{"kind": "maintenance" | "idle" | "suspension"}`; an absent body means `idle`, so an operator checkout from before this change keeps today's user-restartable semantics.

The operator route is idempotent on the transition but **always stamps the kind**: a row that is already `stopping` or `stopped` (for example one the owner stopped an hour ago, which the migrate now wants) has its `stop_kind` set to the requested kind in the same call, without a new transition.
Without this an owner-stopped row would carry no hold through its migration.
The owner route never changes the kind of an already-stopped row.

The stop routes' responses gain `stop_kind`, and `WorkspaceInfo` (server side) gains `stop_kind: str | None`; `_WORKSPACE_SELECT_COLUMNS` and `_workspace_info_from_row` read the column.

#### 4.2.2 Starting

A new `_raise_if_workspace_held(current_db_status, stop_kind)` runs in the owner start route **before** `_raise_if_start_precondition_unmet`, so a held row answers the hold from `stopping` onwards rather than "wait and retry":

```python
HTTPException(
    status_code=409,
    detail={
        "code": "workspace_under_maintenance",
        "message": "This machine is undergoing maintenance and will be back shortly.",
    },
)
```

for `maintenance` and `suspension`.
The existing parked-shape guard `_raise_if_workspace_is_migrating` keeps its predicate but answers with this same detail; `workspace_migrating` as a code disappears (no shipped client parses it).
Its CLEANUP note stands: the parked-shape check goes with the cutover in phase 6, the kind check stays.

Both start CASes (`stopped -> starting`, owner and admin) add `stop_kind = NULL`.
The owner's CAS also carries the hold predicate (`stop_kind IS NULL OR stop_kind IN ('idle', 'owner')`), so a re-stamp that lands between the route's read and its CAS is refused by the CAS (the route then reports the row's current state) rather than cleared.
The admin start route does not call `_raise_if_workspace_held`: it is how the migrate's rollback and the unsuspend-then-start flows bring a held row back.

#### 4.2.3 Changing the kind

New route `POST /admin/workspaces/{id}/stop-kind`, body `{"kind": ...}`, admin-key authenticated.
It rewrites `stop_kind` on a row whose status is `stopping` or `stopped` and answers 409 otherwise (a running row has no stop to describe).
That 409 doubles as the migrate's feature probe (4.4): an old connector answers 404 for the unknown route.
The unsuspend fan-out gains a `workspaces` step that runs the same update for every `suspension` row of the account, setting `idle` (S5); the step reports the count and is re-runnable.
The route is exempt from the wire-compat strict-parse coverage as `_OPERATOR`, like the other admin routes.

#### 4.2.4 The stop/start supervisor

No change.
Its finish-start SQL sets `status = 'leased'` and may leave `stop_kind` alone: the start CAS already cleared it.
A failed start lands back on `stopped` with `stop_kind` NULL, which is deliberate: the operator (or the product restore) has already decided to bring the machine back, so the user may retry.

#### 4.2.5 Wire compatibility

`stop_kind` is an additive optional field on `GET /workspaces` and `GET /workspaces/{id}`; the 0.4.0 compat snapshot ignores unknown fields, and the golden test's response validation covers it.
The new admin route and the admin stop body are exempt routes.

### 4.3 Plugin (`libs/mngr_imbue_cloud`)

- `wire_types.py`: `WorkspaceStopKind(WireEnum)` with `OWNER`, `MAINTENANCE`, `IDLE`, `SUSPENSION`, `UNKNOWN`; `WorkspaceInfo.stop_kind: WorkspaceStopKind | None = None` (None when the server predates the field).
- `errors.py`: `ImbueCloudWorkspaceHeldError(ImbueCloudError)` carrying the server's message, raised by the connector client when a start answers 409 with code `workspace_under_maintenance`, following the `_raise_if_quota_exceeded` pattern.
  Its message starts with an exported constant `WORKSPACE_HELD_MESSAGE`, so the desktop (which runs `mngr start` as a subprocess) can recognize it in stderr the way it matches mngr's `HOST_SHUTDOWN_NOT_SUPPORTED_MESSAGE` today.
- `providers/instance.py`, `_advance_workspace_start`: when the observed status is `STOPPING` or `STOPPED` and `stop_kind` is `MAINTENANCE` or `SUSPENSION`, return `ImbueCloudWorkspaceHeldError` at once instead of waiting or requesting; when `stop_kind` is `UNKNOWN`, return the existing unrecognized-state error (the "update the app" remedy of the remote-compatibility corpus).
- `cli/machines.py`, `_machine_display_payload`: add `"stop_kind"` (lowercase wire value, or null).
- `connector/client.py`: `admin_stop_workspace(admin_api_key, host_db_id, kind)` sends the body; new `admin_set_workspace_stop_kind(admin_api_key, host_db_id, kind)`.

A pure `is_owner_startable(stop_kind: WorkspaceStopKind | None) -> bool` in the plugin is the one place the "who may start" table is encoded for Python clients (the start path above uses it); the frontend mirrors the same table as `isOwnerStartableStopKind` in `landing-controls.ts`, and the Python desktop makes no such decision of its own (it recognizes the connector's refusal sentence instead).

### 4.4 Operator CLI (`apps/minds_admin`)

- `workspaces stop` gains a required `--kind {maintenance,idle,suspension}`.
- New `workspaces set-stop-kind HOST_DB_ID KIND`.
- `server drain` stops with `idle` (its docstring already promises the user's next start restores the workspace).
- `cutover migrate`: `_stop_workspace_via_product` stops with `maintenance`.
  The invocation probes the connector with the kind route (asking it to hold the probed row as `maintenance`): a 409 ("no stop to describe", the row is running) or a 2xx (the row was stopped meanwhile and now carries the hold the migrate wants on it) proves the connector carries stop kinds, a 404 (unknown route) means it predates them, and the invocation aborts with a message naming the connector deploy as the remedy (S12).
  The probe runs twice: once up front against the first `leased` candidate when there is one (so an old connector is refused before any row is started or harvested), and again immediately before every product stop, where the row is running by construction -- this second probe is the guarantee, since a selection of only `stopped` candidates has no running row to probe up front.
  Nothing is stopped before the probe passes.
- `_FINISH_RESTORE_POOL_HOST_SQL` (the migrate's final CAS, which lands the row on `leased` without passing through `starting`) sets `stop_kind = NULL`, like every start CAS.
- `cutover rollback`: `_ROLLBACK_PARK_POOL_HOST_SQL` sets `stop_kind = 'maintenance'` (the row is parked back onto gen-1 and must stay held until the admin start clears it); `_PARK_POOL_HOST_SQL` and `_ROLLBACK_RESTORE_ARTIFACT_SQL` leave the column alone (the row already carries `maintenance` from the stop).
- `_ROW_POLL_SECONDS` drops from 15 to 5 seconds so the migrate notices `stopped` promptly; correctness no longer depends on it.
- `is_artifact_resave_due` and `rollback_would_clobber_newer_artifact` stay (they cover a connector without the hold) under a `CLEANUP:` note tied to every tier running migration 042.
- The park-first reordering discussed before this spec is **not** implemented (S6).

### 4.5 Desktop (`apps/minds`)

#### 4.5.1 Reading the kind

Discovery gives the desktop the lifecycle state but not the kind (S8).
A new backend component, `MachineStopKindTracker` (`desktop_client/machine_stop_kinds.py`), keeps `stop_kind` by host id for imbue_cloud machines:

- It runs `mngr imbue_cloud machines show --account <email>` (the list form, one round trip per signed-in account) through the existing `ImbueCloudCli` wrapper, which gains `list_machines(account)` returning `MachineSizeCliInfo` entries extended with `stop_kind`.
- It polls only while discovery reports at least one imbue_cloud machine in `STOPPING`, `STOPPED` or `STARTING`, at the provider's discovery cadence (30 seconds by default), and once immediately on every `RUNNING` to non-running edge; with every cloud machine running it holds no data and makes no calls.
- A read failure keeps the previous value and logs at debug; the badge falls back to the plain lifecycle label.

`UiWorkspaceEntry` gains `stop_kind: str` (the lowercase wire value, `"unknown"` for an unrecognized one, `""` when running or not known).

#### 4.5.2 Rendering

Frontend (`apps/minds/frontend`):

- `landing-controls.ts`: `isStartShown = isShutdownSupported && liveness === "STOPPED" && isOwnerStartableStopKind(stopKind)`, where `isOwnerStartableStopKind` is true for `""`, `"owner"`, `"idle"` and false for everything else (including `"unknown"`).
- `MIND_LIVENESS_LABELS` stays; the badge text for a `STOPPED` or `STOPPING` machine whose kind is `maintenance` is "Maintenance", otherwise the lifecycle label.
- `notice-band.ts`: `noticeBandFor` gains the displayed machine's `liveness` and `stopKind` (the shell already holds the entry).
  A new payload key `workspace-maintenance` (variant `info`, no action) carries "This machine is undergoing maintenance and will be back shortly." for a machine whose kind is `maintenance` and whose liveness is `STOPPING` or `STOPPED`.
  For any machine whose liveness is `STOPPING`, `STOPPED` or `STARTING` the recovery notices (`workspace-recovering`, `workspace-restart-failed`) are withheld: the machine is expectedly unreachable, and the landing page already suppresses the health badge on the same rule (`landing-controls.ts`).
  The device and discovery notices are unaffected.
- `RecoveryPage.ts`: `?intent=start` on a machine that is not owner-startable renders the same sentence and dispatches nothing.
- The machines-list row (`LandingPage.ts`, `CreateTemplatePage.ts`): `rowClickActionFor` answers `blocked` for a `STOPPED` or `STOPPING` machine that is not owner-startable, and the row renders non-interactive (no cursor, no click), so a click cannot lead to a start the connector would refuse; the badge or the key chip says why.

#### 4.5.3 Unattended recovery

`UnattendedRecoveryDispatcher` gains an optional injected `read_cloud_lifecycle: Callable[[AgentId], WorkspaceStatus | None]` (wired in `app.py` from the `ImbueCloudCli` wrapper and the session store's account lookup, alongside the existing `should_decline_dispatch` veto).
`_dispatch_from_edge` and `_release_owed_start` call it for network-dependent imbue_cloud workspaces before dispatching: a **live** connector read (`machines show`, one round trip; dispatches are rare) rather than discovery, whose 30-second cadence can lag the 8-second STUCK edge.
When the status is `stopping`, `stopped` or `starting`, the dispatch is declined with an info log ("Not auto-starting {}: its stop was requested; the connector reports it {}") and the machine is marked through the existing `suppress_unattended_recovery`, whose wording widens from "stopped from inside the app" to "stopped on purpose"; as today, a later successful probe clears the mark.
The tracker's health may read STUCK meanwhile; both surfaces already withhold health rendering for a non-running liveness (4.5.2), so the user sees the lifecycle badge, not a stuck machine.
When the read fails, today's behavior applies (dispatch; the server-side hold refuses a held machine).

`perform_mind_host_action` (the v1 lifecycle route's Start) and `run_host_recovery_sequence` (the machines list's Start and the unattended dispatch) recognize `WORKSPACE_HELD_MESSAGE` in `mngr start`'s stderr (like the existing shutdown-not-supported match), log it as information rather than a failure, and mark the machine as stopped on purpose.
The v1 route answers the refusal with the connector's sentence alone as its reason (the host was not started, so the outcome is not a success); the recovery sequence ends the episode as a plain outcome: the card shows the server's sentence, the operation is declined (neither completed nor failed, with the sentence as its warning), the tracker returns the agent to STUCK (a probe target, so the operator's start is noticed) and nothing is reported as an error.

### 4.6 Compatibility

| Party | Behavior |
|---|---|
| Client before this change (0.5.2, 0.6.0) against the new connector | Renders "Stopped" with Start for every kind. A Start click or a watchdog dispatch on a held machine gets the 409, whose detail text (the maintenance sentence) reaches the failure card; the watchdog paints one RECOVERY_FAILED card and one error report per outage (S10). Never starts a held machine. |
| New client against an old connector | `stop_kind` is absent: every stopped machine reads as `owner`, exactly today's behavior. |
| Old `minds-admin` against the new connector | Its admin stop sends no body and stamps `idle`. Its migrate would stamp `idle`, not `maintenance`; that checkout must not run migrations (the drill runs from this branch). |
| New `minds-admin` against an old connector | The migrate refuses per S12; `workspaces stop --kind` and `set-stop-kind` fail with the connector's 404/422, which is acceptable for operator tooling. |
| Rows stopped before migration 042 | NULL, read as `owner`. A currently suspended account's stopped rows stay user-startable after unsuspend, as today. |

## 5. Failure modes and edge cases

| Situation | Behavior |
|---|---|
| Owner (or any device) requests a start of a `maintenance` row | 409 with the maintenance sentence; nothing changes. |
| Watchdog fires on a new client during the stop | Live read says `stopping`; dispatch declined; badge shows Maintenance within one poll. |
| Watchdog fires and the live read fails | Dispatch proceeds; the server refuses; the card shows the sentence and no error is reported. |
| Migrate crashes after the stop, before the park | Row is `stopped`, `maintenance`, held, with its artifact intact. Operator re-runs the migrate, rolls back, or `set-stop-kind idle` to hand it back. |
| Migrate crashes after the park, before the restore | Row is `stopped`, `maintenance`, parked (no placement, no manifest). `set-stop-kind idle` does not hand it back: the parked-shape guard refuses the owner's start regardless of kind and there is no artifact for the product restore. Operator re-runs the migrate or rolls back. |
| Operator wants to migrate a row the owner already stopped | The admin stop on the `stopped` row stamps `maintenance` without a transition; the hold is in force before the harvest. |
| Admin start of a held row fails (restore error) | Row lands on `stopped` with `stop_kind` NULL; the user may retry; the operator may re-hold with `set-stop-kind maintenance`. |
| Unsuspend with rows still `stopping` | The kind route accepts `stopping` rows, so they become `idle` too. |
| Connector predates 042 when the migrate runs | The kind-route probe answers 404; the invocation aborts before any row is stopped (before any row is touched when a candidate is running), naming the connector deploy. |
| Watchdog fires on a machine stopped from the user's other device (`owner`) | Live read says `stopped`; dispatch declined; badge "Stopped" with Start. Today's client would have started it. |
| Server sends a kind this client does not know | Client treats it as not owner-startable; Start hidden; `mngr start` refuses with the update remedy; badge "Stopped". |
| Desktop cannot read the kind (CLI error) | Badge falls back to the lifecycle label; Start shown for `STOPPED`; a click on a held machine gets the 409 sentence. |

## 6. Testing

Unit tests beside the code, real fakes only:

- Connector (`workspaces_test.py`, `suspension_admin_test.py`, `wire_compat_test.py`): each stop route stamps its kind; the admin stop re-stamps an already-stopped row; the owner start refuses `maintenance` and `suspension` from `stopping` and `stopped` with the structured 409 and starts `owner`, `idle` and NULL; the admin start ignores the hold and clears the kind; the kind route's status guard; the unsuspend step rewrites `suspension` to `idle`; the parked-shape guard answers the new detail; `WorkspaceInfo` carries the field under every compat snapshot.
- Plugin (`wire_test.py`, `workspace_lifecycle_test.py`, `providers/instance_test.py`, `connector/client_test.py`, `cli/machines_test.py`): enum coercion; `_advance_workspace_start` returns the held error without requesting a start; the 409 maps to the typed error; the show payload carries the kind; `is_owner_startable`.
- Operator CLI (`cutover_drivers_test.py`, `workspaces_admin` tests, `server` drain tests): the migrate stops with `maintenance`, aborts on a 404 probe before any stop, and its finish CAS clears the kind; the rollback park stamps `maintenance`; drain stops with `idle`; the CLI requires `--kind`.
- Desktop (`machine_stop_kinds_test.py`, `workspace_recovery_test.py`, the frontend's `landing-controls.test.ts`, `notice-band.test.ts`, `RecoveryPage.test.ts`): the tracker polls only with a non-running cloud machine; the entry field; the dispatcher declines on a live non-running read and marks the machine, and dispatches when the read fails; the held-message stderr match ends a recovery as a plain outcome; Start hidden for `maintenance`, `suspension`, unknown; the maintenance band payload and the withheld recovery notices for a non-running liveness; the recovery page's held branch.

Behaviors (`apps/minds/behaviors/`): a new `machine-lifecycle/` folder with a README defining *held machine*, *owner-startable* and *stopped on purpose* (*stop kind* itself is defined in the workspace glossary, per the corpus convention), and `operator-stops.feature` covering: a held machine shows Maintenance and no Start; an idle-stopped machine can be started by the user; the app never auto-starts a cloud machine whose stop was requested; an unrecognized stop kind is not actionable (also cross-referenced from `remote-compatibility/invariants.feature`).

Live drill (staging, after the connector deploy and a client built from this branch):

1. Migrate `workspace-3` with the app open on it. Expected: the badge flips to Maintenance and the band appears within one poll; the log shows the declined dispatch and no `mngr start`; the machine comes back on gen-2 with its latchkey state (the re-test this work unblocked).
2. Repeat from the 0.5.2 client with its tab open. Expected: one RECOVERY_FAILED card carrying the maintenance sentence, no start, the migration completes.
3. `server drain` a box holding one test workspace. Expected: badge "Stopped", Start shown, the click restores it.
4. Suspend and unsuspend a test account with a stopped workspace. Expected: `suspension` then `idle` on the row; Start works after the unsuspend.

## 7. Rollout order

1. Land this on `mngr/gen2-ci-rollout-docs`; CI green.
2. `minds-admin env deploy --yes-i-mean-staging` (migration 042 applies; the deploy is a ROLLOVER with one additive migration).
3. Restart the branch desktop client so it carries the new plugin and desktop code, then run the live drill above.
4. Production gets 042 with its first phase-5.5 connector deploy, before any production migration.
5. The desktop half ships in the 0.6.1 cut together with imbue-ai/mngr-internal#970.

## 8. Documentation and changelogs

- `apps/remote_service_connector/README.md`: the stop-kind vocabulary, the admin stop body, the kind route, the 409 detail.
- `apps/minds_admin/README.md`: `workspaces stop --kind`, `workspaces set-stop-kind`.
- `apps/minds/docs/deploy/reference/workspace-stop-start.md`: the kind column in the lifecycle description.
- `apps/minds/docs/deploy/gen2-cutover.md`: the hold is in force from the stop request; what owners see; `set-stop-kind idle` as the hand-back before the park, `cutover rollback` after it.
- `blueprint/slice-fleet-cutover/phase-5.5-incremental-rollout.md` "Expected behavior": the maintenance badge replaces the "sees it stopped" sentence.
- `apps/minds/docs/workspace/glossary.md`: *stop kind*.
- Changelog entries for `apps/remote_service_connector`, `libs/mngr_imbue_cloud`, `apps/minds`, `apps/minds_admin` and `dev` (this spec), at `<project>/changelog/mngr-gen2-ci-rollout-docs.md`.

## 9. Out of scope and follow-ups

- Idle shutdown itself (the policy that decides to stop a machine for capacity) is not designed here; it stamps `idle` when it exists.
- A distinct badge for `idle` ("Stopped to free capacity") was considered and dropped: the user's action is the same as for their own stop.
- Local and bring-your-own-key providers keep today's unattended recovery; a stopped local host may well need the start.
- Surfacing `transition_error` in the desktop remains undone.
- The watchdog undoing a stop made from the user's other device was a pre-existing quirk; S7 closes it for imbue_cloud as a side effect.
