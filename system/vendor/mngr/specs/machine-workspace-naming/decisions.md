# Machine/workspace naming: the model and decisions

Companion to [audit.md](./audit.md) (the divergence collection) and the implementation plan
(`blueprint/machine-workspace-identity/`). This is the reference the terminology ratchets and
later phases point at.

## The model

- A **workspace** is the logical unit a user works out of: permissions (what the agent can
  access, which outside users can access it), apps, data, customizations. Its backups are
  substrate-independent (restorable onto another machine).
- A **machine** is the place a workspace runs: CPUs, RAM, disk, an IP address. A machine
  cannot be *copied* -- there are never two live instances of one machine -- though an
  imbue_cloud machine (a slice VM) can be suspended and resumed on different bare metal,
  keeping its identity. The bare-metal box underneath is substrate, not the machine
  (mngr's "outer host").

## Identifiers

- The **workspace id** is the id of the workspace's `system-services` agent. There is no
  separate id space: the typed wrapper `WorkspaceId`
  (`imbue.mngr_imbue_cloud.primitives`) validates the same `agent-<32hex>` shape. A
  workspace's id never changes for the life of the workspace.
- The **machine id** is the mngr host id (`host-<32hex>`). It is stable across imbue_cloud
  stop/start (the VM is the machine), and a workspace's current machine is a mutable
  attribute of its record, never its identity.
- Because migration allows the same agent id on two hosts during a move window, code that
  addresses one concrete instance uses the `(host_id, agent_id)` pair
  (`AgentInstanceKey`); see `specs/allow-duplicate-agent-ids.md`.
- A minds installation is identified by a `DeviceId` (`imbue.minds.primitives`) -- values
  are host-id-shaped for historical reasons but a device id is not an mngr host id.
- The opaque id joining a host to its pending-create-attempt record is the
  `create-attempt-id` host label (legacy spelling `workspace-id` is read as a fallback).

## Terminology rule

- mngr-level code (`libs/mngr` and mngr-level plugins) speaks **host/agent** and never
  references minds, default-workspace-template, or workspace-level concepts.
- minds-level code (`apps/minds`, the connector, default-workspace-template) speaks
  **machine/workspace**.
- Enforced by repo-wide ratchets in `test_meta_ratchets.py`, with these carve-outs:
  - `mngr_imbue_cloud`'s `wire.py` / `wire_types.py` / `primitives.py` mirror the
    connector's wire vocabulary (which says "workspace"), and `WorkspaceId` lives in the
    plugin so both it and minds can import it. The plugin's `cli/` and `connector/`
    modules are exempt for the same reason: CLI flags/help and the connector client's
    request/response fields speak that wire vocabulary directly.
  - `mngr_imbue_cloud`'s bake/slice operator tooling is minds-level infrastructure living
    in the plugin until it moves (https://github.com/imbue-ai/mngr-internal/issues/461).
  - External senses stay: uv/python "workspace" in build tooling, Claude Code's own
    "workspace trust dialog", and quotes of external APIs.
- The container path `/home/user/workspace` is correctly named (it holds the workspace's
  content) and is a minds-level concern; mngr never references it.

## Operation semantics

- **restore** (future flow): the same workspace on a new machine. The workspace id is kept;
  the new machine's host id is re-stamped into the restored state (via mngr's internal
  `mutate_id` state-layout primitive). Anything derived from the workspace id (backup
  bucket, share label, LLM key alias) carries over.
- **clone** (future flow): a new workspace (and machine) created from a backup or live
  workspace. Both ids are minted fresh via `mutate_id`, and nothing keyed to the source
  workspace (bucket, share label, key alias) is copied.
- **move/migrate** (future flow): same workspace id, new machine; the record's current
  machine flips atomically at cutover, and the old machine is retired. In-flight state is
  owned by the orchestrating client, not the synced record.

## record_format 2

- `record_format` (see the connector's forward-compat machinery) bumps to 2 on the first
  semantically breaking change to a record's meaning: **the row's machine has changed at
  least once** (its `host_id` no longer names the machine the workspace was created on).
- Purely additive fields (e.g. `backup_bucket`, `share_label`) ride the preserve-on-absent
  merge at format 1 without a bump.
- `SUPPORTED_RECORD_FORMAT` stays 1 until the first move-capable release ships; format 2 is
  defined here so that release has a fixed contract to implement.

## Public naming

- New share domains are `<share-label>.<user-hash>.<region>.<content-domain>`: the share
  label is 32 random lowercase hex minted once at the workspace's first share and persisted
  on the share row; the user segment is the first 32 hex of SHA-256 of the SuperTokens user
  id. Certificates land in public CT logs, so no internal id (host id, agent id, SuperTokens
  user id) may appear in a share domain. Existing shares keep their old domains.
- Account-private names may use the workspace id verbatim: new backup buckets'
  short name is the workspace id (`agent-<hex>`; the `agent-` short-name prefix is reserved
  alongside the legacy `host-`), and new workspace LLM key aliases are
  `workspace-<workspace_id>` (the `workspace-` prefix is the rotate-on-exists namespace).
