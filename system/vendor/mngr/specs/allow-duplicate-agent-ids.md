# Allow duplicate agent ids across hosts

## Overview

An agent id is the stable *logical* identity of an agent.
Until now, most of the codebase silently assumed that an agent id is globally unique: aggregation maps, event replay, tunnel tags, and per-agent caches were all keyed by the bare agent id.

This spec changes the identity model so that the same agent id may exist on multiple (different) hosts at the same time.
The motivating use case is migrating an agent from one host to another: the agent id stays the same while the host changes, which requires a window where the agent exists on both hosts.
In minds terminology (see below), this is migrating a *workspace* from one *machine* to another.

**Audience:** developers working on mngr, its plugins, or minds.

**Related specs:** [creation-rename.md](creation-rename.md), [cleanup-error-aggregation.md](cleanup-error-aggregation.md), [provider-shape.md](provider-shape.md).

### Terminology

- "host" and "agent" are mngr-level terms.
- "machine" and "workspace" are minds-level terms.
  A machine is the host a workspace runs on; a workspace is the `system-services` agent with a given id.
- New code must use the terms of its own layer: mngr-level additions use host/agent, minds-level concepts use machine/workspace (even when a field's *value* is a mngr `HostId`).

## Identity model

1. An agent id is **unique per host**, never globally.
   Two agents on the same host can never share an id (the state dir path, tmux/env `MNGR_AGENT_ID` matching, and per-host `data.json` layout all depend on this).
   This is now enforced explicitly: `mngr create --id <existing-id>` targeting a host that already has that agent raises `DuplicateAgentIdOnHostError` (except in update mode and the imbue_cloud pre-baked-adopt path).
2. The identity of one concrete agent *instance* is the pair `(host_id, agent_id)`.
   The canonical string form is `AgentInstanceKey`: `"<agent_id>@<host_id>"` (defined in `imbue/mngr/primitives.py`).
   This string doubles as a CLI address (`mngr event <agent_id>@<host_id>` resolves through the normal address grammar), so an instance key is always directly actionable.
3. Anything that aggregates agents **across hosts** must key on the instance, never on the bare agent id.
   There is no tie-breaking or "preferred instance" logic anywhere in mngr or its plugins: consumers report and act on every instance.
4. Only two places may legitimately narrow a bare agent id to a single instance, and both are explicit rather than heuristic:
   - User-facing single-target address resolution (`mngr connect`, `mngr transcript`, `mngr event`, ...), which raises an error listing every instance and telling the user to disambiguate with `ID@HOST`.
   - minds, which will (in follow-up work) resolve "the workspace's current machine" from its ACTIVE workspace record.

### Plural-command semantics

Commands that operate on *all* matches of a filter (`mngr destroy`, `mngr stop`, `mngr start`, `mngr message`, `mngr exec`, `mngr label`, ...) keep their existing semantics: a bare identifier that matches multiple agents operates on all of them.
This is the documented filter model, applied uniformly to names and ids.
As a safety valve, `mngr destroy --force` with a bare *id* that matches multiple instances first prints the full instance list, since the id used to identify exactly one agent.

**Warning:** any tooling that migrates an agent must therefore address the source instance as `ID@SOURCE-HOST` when destroying it; a bare-id destroy would destroy the freshly-copied target too.

## Component changes

### mngr core

- `primitives.py`: new `AgentInstanceKey` primitive plus `DiscoveredAgent.instance_key`.
- `errors.py`: new `DuplicateAgentIdOnHostError`.
- `api/create.py`: the locked pre-create check now also rejects a duplicate agent id on the *same* host (skipping update mode and the pre-baked-adopt agent).
- `api/discovery_aggregator.py`: `DiscoveryStateAggregator` keys agents by instance; `AggregatorDelta` carries `added_agent_instances` / `removed_agent_instances`; agent-destroyed handling is host-scoped, so destroying `(host A, id X)` never evicts `(host B, id X)`.
- `api/discovery_events.py`: the resolution replay maps are instance-keyed; destroyed-agent tracking is host-scoped; `resolve_provider_names_for_identifiers` returns the union of providers for an id; `resolve_hosts_for_identifiers` raises the multi-host disambiguation error for ids just as it does for names.
- `api/observe.py`: all observer tracking (state history, last-known details, PID watchers) is instance-keyed; `AgentRemovedEvent` gains an additive `host_id` field (readers tolerate its absence in old lines).
- `api/find.py`: the multi-match error distinguishes ids from names and suggests `ID@HOST` disambiguation.
- `cli/complete_names.py`: membership replay is instance-keyed so a host-scoped destroy does not hide the surviving instance's name.
- Docs (`docs/concepts/agents.md`, `docs/conventions.md`, `future_specs/agent.md`): agent ids are documented as unique per host, with the migration overlap called out.

### Plugins

- `mngr_forward`: the resolver, stream manager, per-agent event streams, service-map cache, and (until it was removed) the `resolver_snapshot` envelope are instance-keyed; per-agent `mngr event` subprocesses address `ID@HOST`.
  Known limitation: the reverse-tunnel handler (`reverse_handler.py`) still tracks tunnels by bare agent id (its discovered/destroyed callbacks receive the bare id), so during a migration window a destroy on one host can tear down the tracked tunnels for a same-id agent on another host; re-key it when reverse tunnels need to survive that window.
- `mngr_latchkey`: pending-setup tracking and reverse-tunnel tags use instance keys; the destroyed callback carries the host id.
- `mngr_notifications`: the RUNNING-before-UNKNOWN bit is instance-keyed (falling back to the bare id for old event lines without host details).
- `mngr_kanpan`: board entries carry `host_id`; dired-style marks and batch command execution are instance-scoped, and mark-driven commands address `ID@HOST` so a marked row never acts on an unmarked same-id row.

### minds

Minds only adapts to the changed mngr surfaces in this change; its workspace-level policy ("the ACTIVE record's machine is the workspace's current machine") is deliberately deferred to the migration work.

- `forward_cli.py`: consumes the instance-keyed aggregator/delta API. It also tolerated both bare-id and instance-keyed `resolver_snapshot` payload keys, normalizing to its own bare-id view; that envelope has since been removed, along with the normalization.
- `backend_resolver.py`: logs a warning when a workspace agent id resolves to multiple machines, so an unexpected duplicate is visible instead of silently first-matched.

### Wire-format compatibility rules

- All cross-process JSON surfaces evolve additively: new fields (e.g. `AgentRemovedEvent.host_id`) may be added, and readers must tolerate their absence.
- Replay semantics changed without a schema change: discovery events already carried `host_id` everywhere it is now needed.
- The forward plugin's `resolver_snapshot` envelope (since removed) and on-disk service-map cache switch to instance keys.
  Old cache entries simply fail to seed (a benign startup slowdown that self-corrects); old/new minds and mngr ship together in the desktop app, and minds parses both key forms.

## Edge cases and failure modes

- **Host-scoped destroy vs. replay:** an `AGENT_DESTROYED` event only forgets the instance on its own host.
  A snapshot from another provider/host that still contains the same agent id is unaffected.
- **Same id, same provider:** two hosts within one provider may carry the same agent id; provider-snapshot reconciliation diffs instances, so neither clobbers the other.
- **Unknown-agent retention:** `ProviderDiscoverySnapshotEvent.unknown_agent_ids` remains id-scoped on the wire; when an id is marked unknown, every prior instance of that id in the snapshot's provider is conservatively retained as unknown.
- **Old on-disk events:** lines written before this change carry all required host ids for discovery events; observe-history lines missing host details fall back to bare-id keying for that line only.

## Testing

- Unit: aggregator and resolution-map re-keying, including the keystone regression (destroying `(host A, id X)` must not evict `(host B, id X)`) and two providers reporting the same id.
- Unit/integration: the same-host duplicate-id guard; single-target commands erroring with the instance list; `ID@HOST` resolution.
- Acceptance (docker): two hosts sharing an agent id end to end -- a bare-id single-target command refuses with the `ID@HOST` disambiguation, a bare-id plural command (exec) operates on every instance, `ID@HOST` addresses each instance independently, and destroying one instance leaves the other fully functional (with the bare id resolving again once only one instance remains).

## Follow-up work (deliberately NOT in this change)

These are the known pieces required to actually ship workspace migration on top of this change:

1. **`mngr migrate` preserves the agent id by default** (with a `--new-id` opt-out), passing `--id <source-id>` through to create.
   Its destroy step MUST address the source as `ID@SOURCE-HOST`; a bare reference would destroy both instances under the all-matches semantics.
   A create-without-start option (`mngr create --no-start`) is needed so the target instance can exist without running until cutover.
2. **Connector cutover for synced workspace records**: keep the `workspace_records_one_active_per_agent_idx` unique index; cutover is tombstone-old-row-then-insert-new-row (the index forces this safe ordering).
   For web-only workspaces (no device has them in local discovery), the cutover must be a single connector-side transaction (a "replace-active" operation, like `claim_host` is server-orchestrated today) because no client-side reconcile can repair a half-done cutover.
   The new row should carry a machine-lineage link (reuse `restored_from_host_id` or add a dedicated migrated-from field).
3. **minds current-machine policy**: when a workspace's agent id spans machines, minds resolves the current machine from the ACTIVE workspace record; private (recordless) workspaces prefer the running instance.
   Until then minds only warns on duplicates (this change).
4. **imbue_cloud adopt-under-id**: claiming/leasing a pool machine as a migration *target* requires adopting the pre-baked `system-services` agent under a caller-supplied id (move the state dir, rewrite `data.json` and the env file).
   Today the adopt path fixes the id to the bake's (`FixedAgentIdError`) and the adopt logic exists in two places (the plugin's lease path and the connector's `claim_host` port), which should be consolidated when this lands.
5. **Migration copies the agent state dir** (transcripts, activity, usage history are part of the agent's identity).
   With that, `mngr_usage`'s preserved-usage dedup ("skip preserved usage when a live agent holds the id") avoids double-counting; this needs a test when migrate lands.
   Preserved-session dirs (`preserved/<name>--<id>`) intentionally overwrite newest-wins when both instances are eventually destroyed.
6. **kanpan data-source field maps** (`KanpanDataSource.fetch` and the cached-fields store) remain keyed by bare agent id; during a migration window a data-source column may mix the two instances' values.
   Display-only; re-key when kanpan grows real multi-instance usage.
7. **Forward `resolver_snapshot` schema**: settled by removal. The envelope had no consumer left once minds' recovery diagnostics probe was deleted, so it and minds' bare-id normalization of it are both gone; the resolver's own service-map cache is untouched and still instance-keyed.
