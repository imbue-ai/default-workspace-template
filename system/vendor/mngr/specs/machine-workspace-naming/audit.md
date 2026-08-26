# Audit: machine/workspace meanings and identifiers

## The intended model

- A **workspace** is the logical unit a user works out of: permissions (what the agent can
  access, which outside users can access it), apps, data. Its backups are
  substrate-independent (restorable onto another host). At the mngr level a workspace is
  identified by the **agent id of its `system-services` agent** (with `host_id` as a
  qualifier when needed, since migration allows the same agent id on two hosts at once).
- A **machine** is the place a workspace runs: CPUs, RAM, disk, an IP address. A machine
  backup includes everything (full boot disk). A machine cannot meaningfully be moved or
  copied. At the mngr level a machine is identified by **host id**.
- Terminology rule (already codified in `specs/allow-duplicate-agent-ids.md`):
  mngr-level code says host/agent; minds-level code says machine/workspace. A machine is
  the host a workspace runs on; a workspace is the `system-services` agent with a given id.

This audit collects every place found where the codebase diverges from that model. No
changes have been made; this is the collection pass.

## A. Workspace-level concepts identified by machine identifiers

### A1. Synced workspace records are keyed by `host_id`

The connector's `workspace_records` table -- the durable, cross-device record of a
workspace -- has primary key `(user_id, host_id)`; `agent_id` is a secondary column with
only a one-ACTIVE-row-per-agent partial index. All CRUD, CAS, tombstoning, and reaping key
on `host_id`.

- `apps/remote_service_connector/imbue/remote_service_connector/sync.py:63-99`
  (`WorkspaceRecordModel`: `host_id` "Host the workspace is on"; `agent_id` "Logical
  workspace id"), `sync.py:544` (`PUT /sync/records/{host_id}`), `sync.py:596`
  (`DELETE /sync/records/{host_id}`), `sync.py:421-434` (reaper candidates keyed by
  host_id), `sync.py:436-449` (`any_record_references_backup_bucket(..., host_id)`).
- Wire mirror: `libs/mngr_imbue_cloud/imbue/mngr_imbue_cloud/data_types.py:595-627`
  (`SyncWorkspaceRecord`: "host_id ... (PK with the account)"; "agent_id: Logical
  workspace id (one ACTIVE record per agent_id)").
- Client replica: `apps/minds/imbue/minds/desktop_client/workspace_record_store.py:137-189`
  (`ReplicaRecord`, same shape; module docstring: "The connector holds one record per
  (account, host)").
- Restore lineage is machine-to-machine: `restored_from_host_id`
  (`sync.py:83-85`, `workspace_record_store.py:155`) -- workspace lineage expressed as a
  chain of host ids rather than a stable workspace (agent) id.

Under the intended model the workspace record's identity should be the workspace id
(system-services agent id), with the host id describing where it currently runs.

### A2. The connector's "workspace" lifecycle identity is a lease row id

`GET/POST /workspaces/*` declares the pool_hosts row id (`host_db_id`) the workspace's
durable identity, and the docs pin `host_id` as never-changing across the whole lifecycle.

- `apps/remote_service_connector/imbue/remote_service_connector/workspaces.py:61-64`
  (`WorkspaceInfo.host_db_id: "Durable workspace identity (the pool_hosts row id)"`),
  plus the stop/start/abandon routes keyed by `host_db_id`.
- `libs/mngr_imbue_cloud/imbue/mngr_imbue_cloud/data_types.py:334-359` (`WorkspaceInfo`,
  same wording).
- `apps/minds/docs/workspace-stop-start.md`: "The pool_hosts row is the workspace's stable
  identity across the whole lifecycle (`host_db_id` and `host_id` never change)".

### A3. Workspace backups (substrate-independent restic repos) are named by host id

The backup that is supposed to be the workspace's substrate-independent safety net is
keyed by the machine id at every remote layer:

- Bucket naming: `apps/remote_service_connector/imbue/remote_service_connector/r2/naming.py:50-59`
  (`WORKSPACE_BACKUP_SHORT_NAME_RE = host-<hex>`, `RESERVED_BUCKET_SHORT_NAME_PREFIX =
  "host-"`, `parse_workspace_backup_bucket_name` returns `(user_id_prefix, host_id)`).
- Reaper identifies workspace backups by host-id-shaped bucket names:
  `apps/remote_service_connector/imbue/remote_service_connector/retention.py` (whole module).
- minds: `apps/minds/imbue/minds/primitives.py:228-244` (`BackupProvider.IMBUE_CLOUD`:
  "create a per-workspace R2 bucket (named after the host id)");
  `apps/minds/imbue/minds/desktop_client/backup_reaper.py:89` ("Workspace host id (names
  the backup bucket)"); `backup_provisioning.py` (provisioning keyed by host_id);
  `backup_export.py:48-50` (export zip named by host_id).
- Docs: `apps/minds/docs/backup-retention.md` ("named `<account-prefix>--<host-id>`");
  `libs/mngr_imbue_cloud/README.md` ("buckets whose short name is their workspace's host
  id, `host-<hex>`").
- Internal inconsistency: the *local* canonical restic env for the same backup is keyed by
  agent id (`apps/minds/imbue/minds/desktop_client/backup_env_store.py:36-43`), so one
  backup's local key is the workspace id while its remote key is the machine id. The UI
  layer says so directly: `apps/minds/imbue/minds/desktop_client/data_types.py:12-15`
  (`RemoteWorkspaceTile`: "agent_id ... (drives backup status)"; "host_id ... (drives
  remove-from-list)").

### A4. Shares (a workspace permission) are keyed by host id, and modeled as machine-level

Which outside users may access a workspace is part of the workspace, but the entire
sharing stack keys on the machine:

- Connector share coordinate/domain is `host-<hex>.<user-label>.<region>.<domain>`:
  `apps/remote_service_connector/imbue/remote_service_connector/shares.py:5-14,88-111,127-144`;
  `POST /shares` body `{host_id}`, `DELETE /shares/{host_id}`,
  `GET /shares/{host_id}/status` (README "Shares" section); share records and relay
  tokens keyed by host_id.
- Plugin wire type: `libs/mngr_imbue_cloud/imbue/mngr_imbue_cloud/data_types.py:447-465`
  (`ShareInfo.host_id: "The workspace's host coordinate"`).
- minds API surface places sharing under machines:
  `apps/minds/imbue/minds/desktop_client/api_v1.py:3274-3293`
  (`/api/v1/machines/<host_id>/sharing`), consumed by
  `apps/minds/frontend/src/models/workspaceOptions.ts:411`.
- Product docs declare it machine-level while calling it a per-workspace share in the
  same breath: `apps/minds/README.md:10` ("Optional machine-level sharing"),
  `apps/minds/README.md:33` ("machine-level sharing (a per-workspace share ...)"),
  `apps/minds/docs/design.md` ("Sharing is machine-level and user-initiated").
- Consequence: the shared URL is bound to the machine. If a workspace moves to a new
  machine (restore, migration), its shared domain, grants record, relay token, and
  certificates do not follow it.

### A5. Workspace-scoped LiteLLM keys are keyed by host id

- `apps/remote_service_connector/imbue/remote_service_connector/llm_keys.py:199-281`
  (`WorkspaceMintRequest.host_id: "The workspace's mngr host id"`; key alias
  `workspace-<host_id>`; metadata `workspace_host_id`; ownership check = active workspace
  record for this host id).
- `apps/minds/imbue/minds/desktop_client/ai_keys.py` (mint page keyed by
  `?workspace=<host_id>`; module docstring says "The page is keyed by the workspace's
  mngr **host id**"). Line 117 logs "Minted LiteLLM key for machine {}" -- the same file
  calls the id a workspace key and a machine key.
- The in-workspace sign-in modal deep-links via host id:
  `default-workspace-template/system/apps/system_interface/imbue/system_interface/harnesses/claude/auth.py:299,450-469`
  and `.../system_interface/models.py:334` (`workspace_host_id` field).

A key that follows the workspace should be keyed by the workspace id; keyed by host id it
is orphaned or mis-attributed when the workspace changes machines.

### A6. Workspace content URLs and entry points are keyed by host id

- `libs/mngr_forward`: the whole origin family is
  `[<service>.]host-<hex>.localhost:<port>` and the auth bridge is `/goto/{host_id}`
  (`libs/mngr_forward/imbue/mngr_forward/server.py:1512-1515`, `cookie.py:66`, README).
- `apps/minds/imbue/minds/desktop_client/README.md:26,95-99` ("Each workspace owns a
  family of origins keyed by its host id"; workspace entry via `/goto/<host-id>/`).
- Electron persists window URLs keyed by host id; the restore filter needs both
  coordinates: `apps/minds/imbue/minds/desktop_client/backend_resolver.py:138-155`
  (docstring explicitly acknowledges the agent-keyed/host-keyed split).
- `apps/minds/imbue/minds/desktop_client/ui_api_lifecycle.py:82,224-256` routes accept a
  "workspace coordinate" that "may be host-keyed" and resolve it back to an agent id --
  compensating code for the dual keying.

Consequence: a workspace's URLs (including any user bookmarks and persisted windows)
change identity when the workspace moves machines, even though the workspace itself is
supposed to be the stable thing.

## B. Machine-level things called "workspace"

### B1. imbue_cloud stop/start artifacts (full VM disks) are "workspace" artifacts

The stop/start artifact is the slice VM's qcow2 disks plus lima metadata -- by
definition a machine backup -- yet the entire vocabulary is "workspace storage":

- `apps/remote_service_connector/imbue/remote_service_connector/storage.py` (module
  docstring "workspace stop/start artifacts"; `workspace_key_prefix(config,
  mngr_host_id)`; `WORKSPACE_STORAGE_*` env vars/secret).
- `apps/remote_service_connector/imbue/remote_service_connector/stop_start.py`,
  `workspaces.py` docstrings; `apps/minds/imbue/minds/envs/providers/workspace_storage.py`.
- `apps/minds/docs/workspace-stop-start.md` ("Artifact: the slice's self-contained qcow2
  `disk` + `datadisk` ... keyed under `[<env>/]<host-id>/gen-<n>/`").

Additionally, the restore path moves this machine image onto a different box while
preserving `host_id` (`libs/mngr/imbue/mngr/providers/host_key_store.py:305-321`
`move_host_endpoint_pins`: "a stopped imbue_cloud workspace restored onto a different
box"). That makes host_id a durable identity that survives substrate moves -- host_id is
being used as the workspace's identity, which is exactly the role agent_id should play,
while the mechanism (full-disk move) is machine-level.

### B2. "Remote workspaces" quotas count pool-host leases

- Entitlements `max_remote_workspaces` / `max_total_workspaces` /
  `max_active_synced_workspaces` count pool_hosts rows and host-keyed sync records
  (`apps/remote_service_connector/imbue/remote_service_connector/hosts.py:588-655,1050-1060`,
  `entitlements.py`).
- The minds account UI already relabels these as machines:
  `apps/minds/imbue/minds/desktop_client/account_plan_view.py:46,70` ("Remote machines",
  "Synced machines", "Stopped remote machines still count until destroyed."). The wire
  names say workspaces; the UI says machines; the counted rows are leases/machines.

### B3. Glossary and design docs define workspace as a host

- `apps/minds/docs/workspace/glossary.md:5`: "**workspace**: a persistent mngr *host*
  ... It is addressed by its primary agent's id" -- the definition itself conflates the
  workspace with the machine, then assigns the agent-id address.
- `apps/minds/docs/design.md` "Agent creation" and the desktop-client README describe the
  workspace's identity/URL family in host terms throughout (see A6).

### B4. The workspace backup service is named `host_backup`

- `default-workspace-template/system/services/host_backup/` (supervisord program
  `host-backup`): continuously backs up the container's `host_dir` to the restic repo --
  this is the substrate-independent *workspace* backup of the intended model, named after
  the host. Its bucket is host-id-named (A3). The retention/entitlement stack calls the
  same thing "workspace backups". One concept, two vocabularies, machine-keyed.

## C. Misapplied or cross-typed identifiers

### C1. The `workspace-id` host label holds a create-attempt id

- `apps/minds/imbue/minds/desktop_client/pending_create_attempts.py:96,124`
  (`WORKSPACE_ID_HOST_LABEL = "workspace-id"`; value is the opaque pending-create-attempt
  id) and `labeled_hosts.py` (module docstring: "workspace-id label (the opaque
  pending-create-attempt id)"). A label named "workspace-id", stamped on a *host*, whose
  value is neither the workspace id (agent id) nor a host id.

### C2. Auto host names use the base "workspace"; error messages say "Machine name"

- `apps/minds/imbue/minds/desktop_client/host_names.py:15-18`
  (`_DEFAULT_HOST_NAME_BASE = "workspace"`, giving hosts names `workspace-1`, ...) while
  line 37 raises "Machine name must include at least one letter or number." The user's
  workspace display name is slugified into the *host* name (glossary: "the normalized
  slug is the host's name"), so the machine is named after the workspace and keeps that
  name if the workspace is renamed or moved.

### C3. The desktop install's device id is typed `HostId`

- `apps/minds/imbue/minds/desktop_client/device_identity.py` (`get_or_create_device_id ->
  HostId`; "It is HostId-shaped (a user's machine is a host)") and
  `workspace_record_store.py:370` (`device_id: HostId`). Deliberate, but it mints HostId
  values that are not mngr hosts, and `hosting_device_id` on workspace records carries
  them as plain strings -- the type system cannot tell a machine id from an install id.

### C4. `WorkspacePaths` names the minds install's data directories

- `apps/minds/imbue/minds/config/data_types.py:30` -- the desktop client installation's
  paths object is called `WorkspacePaths` though it describes the *installation* (the
  glossary's own term), not a workspace.

### C5. Workspace-list identity is a union of agent id and create-attempt id

- `apps/minds/imbue/minds/desktop_client/ui_models.py:60` (`UiWorkspaceEntry.id`:
  "Workspace agent id (stable identity), or the create-attempt id for create rows").
  Documented and transitional, but it means "the workspace id" in the UI channel is not
  always an agent id.

### C6. No `WorkspaceId` (or typed id) on any wire model

- There is no `WorkspaceId` type anywhere in the repo. Every wire/DB model passes
  `host_id` / `agent_id` as bare `str` (`SyncWorkspaceRecord`, `ReplicaRecord`,
  `WorkspaceInfo`, `LeaseResult`, `SliceBakeOutcome`, `ShareInfo`,
  `WorkspaceMintRequest`, ...), so an id-swap (agent id where a host id belongs, or vice
  versa) cannot be caught by the type system -- contrary to the style guide's typed-id
  rule and the reason these two ids keep blurring.

## D. mngr-core vocabulary drift

Per the terminology rule, mngr-level code should say host/agent, but "workspace" appears
in core comments meaning host, agents-on-a-host, or work_dir contents:

- `libs/mngr/imbue/mngr/primitives.py:264-266` (HostState comments: "observation of the
  workspace is impossible"; "the generic provider SSH into the workspace host").
- `libs/mngr/imbue/mngr/interfaces/provider_instance.py:337,719,732` ("its workspaces
  stay visible" -- meaning the host's agents).
- `libs/mngr/imbue/mngr/providers/ssh_utils.py:66,171` ("the minds workspaces'
  owner-exec"; "keep synced workspace ...").
- `libs/mngr/imbue/mngr/providers/host_key_store.py:316` ("stopped imbue_cloud workspace
  restored onto a different box").
- `libs/mngr/imbue/mngr/providers/host_dir_layouts.py:6` ("applies from the workspace
  clone").
- `libs/mngr/imbue/mngr/api/preservation.py:198,225` ("--from is fundamentally a
  workspace clone"; "copies the source *workspace*" -- here meaning the work_dir).
- `libs/mngr/imbue/mngr/api/discovery_events.py:460` ("workspace IDs" as an example of
  plugin config fields).
- Plugins at the mngr level: `libs/mngr_vps/imbue/mngr_vps/instance.py:916` ("Remove
  every workspace container identified by its host-id label"),
  `libs/mngr_vps/imbue/mngr_vps/instance.py:492`, `bare_realizer.py:177`,
  `libs/mngr_lima/imbue/mngr_lima/instance.py:817`, `host_store.py:61`,
  `libs/mngr_claude/imbue/mngr_claude/plugin.py:3291,3322,3679` ("workspace clone",
  "workspace .env"; note plugin.py:1629's "workspace trust dialog" is Claude Code's own
  vocabulary and arguably exempt).

There is also a third overloaded sense throughout: "workspace" as the repo checkout /
work_dir (`/home/user/workspace` in the default-workspace-template, "workspace clone" in
preservation.py, uv "workspace" in conftest/build tooling). The uv/python-workspace and
Claude-trust-dialog senses are external vocabulary and out of scope; the work_dir sense
inside our own prose is part of the drift.

## E. Already aligned with the intended model (reference points)

- `specs/allow-duplicate-agent-ids.md` -- the canonical identity model and terminology
  rule; `AgentInstanceKey` (`libs/mngr/imbue/mngr/primitives.py:354-389`) is the
  workspace-must-sometimes-be-identified-by-both mechanism.
- `apps/minds/imbue/minds/desktop_client/ui_models.py:46-58` (`UiWorkspaceEntry`
  docstring) and `apps/minds/frontend/src/models/workspaces.ts:1-12` -- state the model
  exactly (agent_id = stable identity; host_id = "the logical machine currently running
  it", swappable transport attribute).
- `/api/v1/workspaces/<agent_id>` and the association store are agent-keyed
  (`apps/minds/imbue/minds/desktop_client/api_v1.py:295-420`, `session_store.py`).
- Workspace discovery = agents with the `is_primary` label
  (`backend_resolver.py:101-136,1160-1200`) -- the system-services agent id is the
  workspace id.
- latchkey's `target_workspace_id` parses as an `AgentId`
  (`apps/minds/imbue/minds/desktop_client/latchkey/permission_overview.py:714-724`).
- `backend_resolver.py:584-596` warns when an agent id spans machines ("Workspace agent
  id {} resolved to {} machines"), using the right vocabulary.
- mngr `docs/concepts/hosts.md` describes hosts in machine terms (including "outer"
  machines) without workspace vocabulary.

## Summary of the divergence pattern

The UI/desktop layer has largely adopted the intended model (workspace = agent id,
machine = host id). Everything below it -- the connector's workspace records, shares,
workspace-scoped LLM keys, backup buckets, stop/start artifacts, quotas, and the
content-URL/`/goto/` scheme -- still uses `host_id` (or a lease row id) as the
workspace's identity, and the docs/glossary still define a workspace as a host. The
compensating shims (`ui_api_lifecycle`'s host-or-agent coordinate resolution,
`workspaces.ts`'s agentId<->hostId alias maps, `backup_env_store`'s agent-keyed local
copy of a host-named remote bucket) are where the two models currently meet.
