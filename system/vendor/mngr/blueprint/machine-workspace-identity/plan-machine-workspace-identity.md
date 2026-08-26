# Machine/workspace identity

## Overview

- Fix the divergences documented in `specs/machine-workspace-naming/audit.md`: a **workspace**
  is the logical unit identified by its `system-services` agent id; a **machine** is the host
  it runs on, identified by host id. Today most of the stack below the UI (connector records,
  shares, backup buckets, workspace LLM keys, content URLs) keys workspaces by machine
  identifiers.
- Terminology rule (from `specs/allow-duplicate-agent-ids.md`) becomes enforced: mngr-level
  code says host/agent; minds-level code says machine/workspace. Ratchets keep it that way,
  with explicit carve-outs for wire models that mirror the connector's vocabulary and for the
  bake tooling deferred to issue #461.
- Identity decisions: `WorkspaceId` is a typed wrapper over the services agent id (no new id
  space), living in `mngr_imbue_cloud` so both the plugin and minds import it. The machine is
  the slice VM: `host_id` stays stable across imbue_cloud stop/start. Restore keeps the
  workspace id; clone mints new ids; both are enabled by a new low-level internal mngr
  `mutate_id` primitive.
- The connector's `workspace_records` become workspace-keyed via a one-time PK migration
  (`(user_id, agent_id)`, `host_id` demoted to a mutable column), with host-keyed routes kept
  as lookup shims. Builds on the `mngr/audit-connector-forward-compat` branch (hard
  prerequisite, merging separately): WireModel tolerance, preserve-on-absent merge,
  `record_format` write-locks, and the golden compat test.
- Foundations only: no user-facing move/restore/clone flows ship in this program.
  `SUPPORTED_RECORD_FORMAT` stays 1; format 2 (mutable-host semantics) is defined by spec and
  only ever written by future move flows.
- Public share domains stop publicizing internal ids: new shares get a random 32-hex workspace
  segment (minted once at first share, persisted) and a truncated-SHA256 user segment (pure
  function, no storage). Old shares grandfather their existing domains.
- Content URLs re-key from `host-<hex>` to the workspace's agent id (mngr_forward's internals
  are already agent-keyed; only the URL label changes), with a legacy shim that 301s HTML
  navigations from old origins.

## Expected behavior

### Identity and vocabulary

- A workspace's stable identity everywhere in minds and the connector is its `WorkspaceId`
  (the `agent-<32hex>` id of its `system-services` agent). The machine (`host-<32hex>`) is a
  swappable attribute.
- mngr-level code, docs, and log lines speak host/agent; minds-level ones speak
  machine/workspace. New violations fail ratchet tests. The minds glossary defines workspace
  correctly (a logical unit addressed by its services agent id, running on a machine) instead
  of "a persistent mngr host".
- User-visible copy is consistent: the create-form name is the *workspace* name (error copy no
  longer says "Machine name"); account-plan rows keep their machine labels for machine-counted
  quotas.

### Connector records (sync)

- One record per workspace per account, keyed by workspace id. `host_id` is the workspace's
  current machine, mutable. Wire behavior for existing clients is unchanged: host-keyed routes
  still resolve, list responses keep every existing field.
- New workspace-keyed routes exist alongside the host-keyed ones; the host-keyed routes are
  CLEANUP-tagged shims that look the row up by its `host_id` column.
- The `record_format` rules from the forward-compat branch govern future semantics: format 2
  (a row whose machine has changed) is spec-defined here but never written by this program.
- New record columns (`backup_bucket`, `share_label`) are written immediately but served on
  the wire only after the pre-tolerant strict snapshot is pruned (same column-first/wire-later
  pattern the branch uses for `record_format`), so a strict client can never see — and drop —
  a row carrying an unknown field.

### Shares

- Share records are keyed by workspace id; the share follows the workspace, not the machine.
- A workspace's first share mints a random 32-lowercase-hex workspace label, persisted on the
  row; unshare keeps the row inactive and re-share resurrects the same URL (today's behavior).
- New share domains are `<label32hex>.<sha256(user_id)[:32]>.<region>.<domain>` — nothing in a
  CT-logged certificate reveals an mngr host id, agent id, or SuperTokens user id. Existing
  shares keep their old `host-<hex>.<user-id-hex>` domains until deleted.
- Disabling/enabling sharing, grants editing, and relay auth behave exactly as today.

### Backups and workspace LLM keys

- New backup buckets are named by workspace id (`<prefix>--agent-<hex>`; the `agent-` short
  name prefix is reserved alongside `host-`). The record's `backup_bucket` column is the
  source of truth; reapers and guards fall back to name-derivation only for legacy rows.
- Existing host-named buckets are grandfathered forever; nothing migrates bucket contents.
- New workspace LLM key mints use alias `workspace-<workspace_id>` and workspace-id metadata;
  rotate-on-exists still works per workspace. The mint endpoints dual-accept host-or-workspace
  ids during the transition. The desktop's key-mint page accepts `?workspace=` with either id
  shape.

### Content URLs

- A workspace's origin family becomes `[<service>.]agent-<hex>.localhost:<port>`, and the auth
  bridge becomes `/goto/<agent-id>/`. URLs now survive a future machine change.
- Legacy `host-<hex>` origins: HTML navigations 301 to the canonical agent origin (browser
  state accumulates in one place); non-HTML requests from stale pages fail and heal on reload.
  Electron's persisted window URLs are *not* migrated — the redirect shim heals them.
- In generic (non-minds) mngr_forward use, every agent owns its own origin; the old
  host-label guess (`resolve_agent_for_host`'s smallest-instance-key tie-break) survives only
  inside the legacy shim.

### dwt-facing entry points

- minds dual-accepts host-or-workspace ids at the deep-link entry points dwt uses (the claude
  sign-in modal's key-mint link). dwt switches to sending the workspace id at its next
  release; the dual-accept is CLEANUP-tagged on "no supported workspace predates that
  release".

### mutate_id

- mngr gains an internal API function (no CLI) that rewrites identity in a host's on-disk
  state: an agent's id (state dir name, `data.json`) or the host's own id. Stopped-state only.
  No production caller ships in this program; it exists, fully tested, for the future
  restore/clone/adopt flows.

## Implementation plan

### P1 — taxonomy, types, ratchets, mechanical renames

- `apps/minds/docs/workspace/glossary.md`: rewrite the **workspace** entry (logical unit =
  services agent id; runs on a **machine** = mngr host); add a **machine** entry; fix the
  README/design.md "machine-level sharing (a per-workspace share)" phrasing.
- `specs/machine-workspace-naming/`: add the terminology/decisions doc (the model, the
  carve-outs, restore-vs-clone identity semantics, format-2 definition) referenced by
  ratchets and later phases; link from `specs/allow-duplicate-agent-ids.md`.
- `libs/mngr_imbue_cloud/imbue/mngr_imbue_cloud/primitives.py`: `WorkspaceId` — typed wrapper
  validating the `agent-<32hex>` shape (duplicated-by-convention regex; no mngr import needed
  by the connector, which keeps its own regex).
- `apps/minds/imbue/minds/primitives.py`: `DeviceId` (accepts the legacy `host-<hex>`-shaped
  values); `device_identity.py` and `workspace_record_store.py` stop typing the install id as
  mngr `HostId`.
- `apps/minds/imbue/minds/config/data_types.py`: rename `WorkspacePaths` -> `InstallationPaths`
  (mechanical, ~341 references across 54 files; its own PR).
- `apps/minds/imbue/minds/desktop_client/pending_create_attempts.py` + `labeled_hosts.py` +
  `agent_creator.py`: host label `workspace-id` -> `create-attempt-id`; stamp the new label,
  read both, CLEANUP the dual-read.
- `apps/minds/imbue/minds/desktop_client/host_names.py`: error copy says workspace name.
- `apps/minds/imbue/minds/desktop_client/account_plan_view.py` + connector `entitlements.py`
  docstrings: machine-counting quotas described as machines (labels mostly done; finish
  docstrings). No wire renames.
- mngr-level comment sweep (vocabulary only, no behavior): `libs/mngr` (`primitives.py`,
  `interfaces/provider_instance.py`, `providers/ssh_utils.py`, `providers/host_key_store.py`,
  `providers/host_dir_layouts.py`, `api/preservation.py`, `api/discovery_events.py`),
  `mngr_claude/plugin.py` (minds references), `mngr_forward` (reword the dwt-commit CLEANUP
  condition to a version condition), `mngr_vps`, `mngr_lima`.
- `test_meta_ratchets.py`: repo-wide terminology ratchets — (a) mngr-level code
  (`libs/mngr` + mngr-level plugins) bans `workspace`/`machine`/`minds`/
  `default-workspace-template` in non-test code, allowlisting `mngr_imbue_cloud`'s
  `wire.py`/`wire_types.py` (mirrors server vocabulary), its bake/slice modules (issue #461),
  and external senses (uv workspaces, Claude's "workspace trust dialog"); (b) minds-level
  user-facing strings prefer machine/workspace. Seed counts at current violations via
  inline-snapshot; tighten as the sweep lands.

### P2 — mutate_id

- `libs/mngr/imbue/mngr/hosts/mutate_id.py` (new): internal functions
  `mutate_agent_id(host, state_dir, old_id, new_id)` and `mutate_host_id(host, new_id)` —
  rename the agent state dir, rewrite the agent/host `data.json` id fields, refuse when the
  agent/host is running or the target id already exists on the host. Pure state-layout
  mechanics; no workspace semantics, no CLI, no plugin hook (no provider stores agent ids
  outside host_dir state).
- Unit + integration tests against the local provider's host-dir layout (shared fixtures).

### P3 — connector record re-key + minds record-store switch

- `apps/remote_service_connector/migrations/031_workspace_keyed_records.sql`: PK becomes
  `(user_id, agent_id)`; `host_id` gains a plain (non-PK, non-unique) index for the shims --
  non-unique because machine-reuse flows can leave a tombstone and an active row sharing a
  `(user_id, host_id)` pair; add nullable `backup_bucket` and `share_label` columns. The
  active-only `(user_id, agent_id)` partial unique index collapses into the PK.
- `apps/remote_service_connector/imbue/remote_service_connector/sync.py`: store keys rows by
  agent_id; `PUT/DELETE /sync/records/by-workspace/{workspace_id}` (new routes, registered in
  the compat test's route classification); existing `/sync/records/{host_id}` routes become
  CLEANUP-tagged shims (the DELETE resolves via the host_id column; the PUT checks the path
  against the body's host_id and addresses the row by agent_id); CAS/preserve-on-absent semantics
  unchanged; serve `backup_bucket`/`share_label` only when set *and* the strict snapshot is
  pruned (column-first/wire-later, mirroring `record_format`).
- `apps/remote_service_connector/imbue/remote_service_connector/retention.py` + `r2/naming.py`:
  reapers prefer `backup_bucket` from the record; name-derivation (`host-<hex>` short names)
  stays as the legacy fallback; reserve the `agent-` short-name prefix alongside `host-` in
  bucket-create validation.
- `libs/mngr_imbue_cloud`: `wire_types.py` record model gains the new optional fields; the
  sync CLI (`cli/sync.py`) gains the by-workspace addressing; `connector/client.py` calls the
  new routes when talking to a new server, falling back to host-keyed routes (version-probe by
  404) with a CLEANUP.
- `apps/minds/imbue/minds/desktop_client/workspace_record_store.py`: replica keyed by
  workspace id (`ReplicaRecord` identity = agent_id; on-disk replica files re-keyed on load);
  absence-tombstoning, associate/disassociate, reaper candidates, and eviction all key on
  workspace id; `restored_from_host_id` left untouched (machine lineage, future flows).
- `apps/minds/imbue/minds/desktop_client/backup_reaper.py` / `backup_provisioning.py` /
  `backup_export.py`: consume `backup_bucket` when present; legacy fallback to host-named
  buckets; new provisioning names buckets `agent-<hex>` and records `backup_bucket`.
- `SUPPORTED_RECORD_FORMAT` stays 1 everywhere; the decisions doc defines format 2 =
  "this row's machine has changed at least once" for future flows.

### P4 — shares + workspace LLM keys

- `apps/remote_service_connector/imbue/remote_service_connector/shares.py` (+ migration):
  share rows keyed by workspace id (host_id kept as the machine attribute + legacy lookup);
  `POST /shares` accepts optional `workspace_id` in the body (additive request field — safe);
  first share mints the random 32-hex `share_label`, persisted; `ShareCoordinate` for
  label-bearing rows becomes `<share_label>.<sha256(user_id)[:32]>.<region>.<domain>`; rows
  without a label (legacy) keep the old host/user-id coordinate. `DELETE /shares/{host_id}`
  and `GET /shares/{host_id}/status` stay host-keyed (no by-workspace variants); the
  workspace-to-host resolution lands client-side in minds, whose
  `/api/v1/workspace-sharing/{workspace_id}` routes resolve the workspace to its machine
  before calling the connector.
- `share_certs.py`, `share_broker.py`, `relays.py`, `hosts.py` share bring-up: coordinate
  construction goes through the row's stored label scheme; no other behavior change.
- `apps/remote_service_connector/imbue/remote_service_connector/llm_keys.py`: workspace-mint
  accepts `workspace_id` (host_id fallback shim); ownership check consults the re-keyed
  records; new alias `workspace-<workspace_id>`; rotate-on-exists deletes keys under either
  alias shape for the workspace (old alias derived via the record's host_id).
- `apps/minds/imbue/minds/desktop_client/sharing_handler.py` + `ai_keys.py` +
  `share_materials_injection.py`: drive sharing and mints by workspace id; `?workspace=` param
  dual-accepts both id shapes (resolve host->workspace via the record store / discovery).
- `libs/mngr_imbue_cloud/cli/shares.py` + `keys.py`: accept workspace addressing; keep host
  addressing as deprecated aliases.

### P5 — agent-keyed origins

- `libs/mngr_forward`: origin label and `/goto/` path become the agent id
  (`server.py` route + `_GOTO` shape regex, `cookie.py` Domain scoping, `resolver.py` — the
  agent-keyed internals are already there); legacy `host-<hex>` labels: HTML navigations 301
  to the canonical agent origin (via `resolve_agent_for_host`, which becomes the shim),
  non-HTML 404; CLEANUP-tagged.
- `apps/minds/imbue/minds/desktop_client`: URL construction (`/goto/`, `host-<hex>.localhost`
  origin family in `app.py`, `backend_resolver.py`, forward spawn, README) uses the workspace
  id the UI already keys on; `/api/v1/machines/<host_id>/sharing` routes renamed to
  workspace-keyed (`api_v1.py` + `frontend/src/models/workspaceOptions.ts` — SPA and server
  ship together, and the latchkey gateway deny-lists these routes, so no external consumer
  needs a shim).
- `apps/minds/frontend/src/models/workspaces.ts`: alias maps stay (the shim still surfaces
  host-shaped inputs); docstrings updated.
- e2e/snapshot-tier tests that navigate `host-<hex>` URLs updated to the agent-keyed form
  (plus one legacy-redirect assertion).

### P6 — dwt-side switch + CLEANUP tags

- default-workspace-template (separate PR via `just dwt-worktree`, same branch name):
  `system_interface` claude auth modal sends the workspace id (its own services-agent id from
  `MNGR_AGENT_ID`/agent state) instead of reading `host_id` from host `data.json`; field
  renamed accordingly in `models.py`/`harnesses/claude/auth.py`.
- mngr repo: CLEANUP entries verified across P3-P5 shims with their checkable conditions
  (strict-snapshot pruned; no supported workspace predates the dwt release; no in-window
  client uses host-keyed routes).
- `apps/minds/docs/`: `workspace-stop-start.md` corrected (the pool row is the lease/machine
  record; the workspace's identity is its workspace id), `backup-retention.md`,
  desktop-client README, `mngr_forward` README.

## Implementation phases

1. **P1 — taxonomy, types, ratchets, renames** (2-3 PRs: docs+types+ratchets; the
   `InstallationPaths` rename; the comment sweep). No behavior change; no wire dependency.
2. **P2 — `mutate_id`** (1 PR). Independent of the connector; can land in parallel with P1.
3. **P3 — connector re-key + record store** (2 PRs: connector; then plugin+minds). *Blocked on
   `mngr/audit-connector-forward-compat` merging*; the new-field wire serving additionally
   waits on the strict-snapshot prune (columns land immediately).
4. **P4 — shares + keys + buckets** (1-2 PRs). Depends on P3's re-keyed records.
5. **P5 — origins** (1-2 PRs: mngr_forward; minds). Depends on P1 types only; sequenced after
   P3/P4 so the dual-accept resolution has workspace-keyed data behind it.
6. **P6 — dwt switch + doc corrections + CLEANUP audit** (1 mngr PR + 1 dwt PR). Last, so the
   dual-accepts it retires are already deployed.

Each phase leaves every wire surface backward compatible (compat test enforced) and every
suite green.

## Testing strategy

- **Unit**: `WorkspaceId`/`DeviceId` validation; share label/hash derivation; alias and
  bucket-name derivation (both shapes); `mutate_id` refusal cases (running agent, id
  collision); host-label-vs-agent-label parsing in mngr_forward.
- **Integration**: connector store tests for the migrated keying (host-shim and by-workspace
  routes hitting the same row; CAS and preserve-on-absent unchanged; `record_format_too_new`
  guard on the re-keyed table); record-store replica re-key on load; sharing bring-up with a
  label-bearing row and with a legacy row; key mint rotate-on-exists across old/new aliases;
  forward-plugin redirect shim (HTML 301, non-HTML 404, canonical byte-forwarding).
- **Wire compat**: every new route classified in `wire_compat_test.py`; existing snapshots
  must keep parsing every response (this is the regression net for the whole program); the
  column-first/wire-later fields get an explicit gated-serving test.
- **Ratchets**: terminology ratchets seeded via inline-snapshot, tightened per sweep PR
  (`--inline-snapshot=trim`).
- **e2e**: existing snapshot-tier desktop tests updated for agent-keyed URLs plus one
  legacy-origin redirect assertion; deployment tests updated only where they already touch
  records (standard rigor — no new real-tier suites).
- **Edge cases**: a record whose host_id no longer matches any live host (shim lookups);
  two records sharing a host_id historically (migration collision handling: impossible for
  ACTIVE rows, verified for tombstones before the PK flip); re-share of a legacy share (keeps
  old domain); strict-window client listing rows with unset new columns (fields omitted).

## Open questions

- Migration collision audit: whether any production `workspace_records` tombstones share an
  `(user_id, agent_id)` pair (would block the PK flip; resolved by keeping the newest and
  deleting older tombstones — needs a pre-migration query against each tier).
- The exact CLEANUP dates depend on when the forward-compat branch's first tolerant release
  ships and when its strict snapshot ages out; the plan's conditions are checkable but not yet
  dated.
- Whether the share content domains should eventually get a Public Suffix List entry so the
  per-user registrable site is a true browser site boundary — orthogonal to this program,
  noted while redesigning the label scheme.

Explicitly deferred (not open): the 426 forced-update enforcement spec; the bake-tooling
relocation (issue #461); the entitlement wire-name rename (ride-along only, post-prune);
user-facing move/restore/clone flows (and with them the first format-2 writer and the
`SUPPORTED_RECORD_FORMAT` bump); the `host_backup` service rename (last, with a template
wave).
