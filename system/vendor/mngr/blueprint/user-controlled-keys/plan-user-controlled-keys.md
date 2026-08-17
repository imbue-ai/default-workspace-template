# Plan: user-controlled SSH keys (structured key store, imbue_cloud adoption, sync fixes)

## Overview

- Host-key and client-key state is today scattered across append-only files with many writers (bake tooling, connector, lease, rebuild, minds sync materializer); every consumer guesses precedence, and wrong guesses are permanent. This produced the observed production wedges: stale synced pins permanently shadowing live keys, and the slice-restart authorized_keys wipe (PR 382's incident).
- Move to a single authority: the user. Keys become structured, per-host state owned by mngr; `known_hosts` files become derived artifacts rendered from a store; the DEK-encrypted workspace record (already CAS-revisioned) is the sole post-adoption channel between the user's devices.
- imbue_cloud hosts get **adoption**: at lease (and on first connect for existing leases), the client rotates both endpoints' sshd host keys, takes ownership of `authorized_keys` via an in-VM reconciler unit, and records user-origin pins. After adoption the connector's bake-time keys are irrelevant; trust flows only through the user's synced record.
- The connector is trusted exactly once, at lease handoff (bootstrap pins). Pool-key **stripping** is deferred: adoption preserves the pool management key in `authorized_keys` until a later strip phase, which becomes a desired-state content change (the reconciler already owns the file).
- PR 382 is closed rather than merged: its mngr_lima root-cause fix, latchkey warn, and investigation doc are re-landed here; its connector repair cron + `leased_ssh_public_key` column are replaced by a one-shot operator fleet sweep plus client-side ensure-adopted healing.
- Per-host keys become universal for new hosts: host keys per-host in docker/modal (the last shared ones), client keys per-host in every provider. Legacy hosts keep working via read fallbacks; nothing provider-wide ever enters a synced record.
- The minds sync payload wire shape is unchanged (old versions keep parsing it); only the semantics change: producers render `ssh_known_hosts` from the store, importers replace-by-endpoint+keytype gated on record revision.
- Adoption covers slices only (no legacy OVH-VPS pool hosts exist). No kill switch and no encoded staging criterion: the tier pipeline, two-phase per-host rotation, ensure-adopted healing, and the operator sweep are the safety mechanisms; a hotfix release is the brake.
- PRs stack: the first phase stacks on `mngr/remote-machine-stopping`, each later phase on the previous.

## Expected behavior

- Syncing a workspace to a new machine just works: the new device materializes the synced client key and pins, connects with strict host-key checking, and the workspace loads. Stale pins in old records can no longer permanently wedge a host (imports replace same-endpoint pins instead of appending behind them).
- After `mngr stop` / `mngr start` relocates a slice (remote-machine-stopping), every device pins the host's unchanged user-origin keys at the new endpoint automatically; adopted hosts never regress to bake-time connector keys.
- A slow-path rebuild re-runs adoption; other devices pick up the rebuilt keys on their next sync pull (record revision advances).
- A VM restart can no longer strand the owner: the cidata generator no longer truncates `authorized_keys` (new carves), the fleet sweep patches existing slices' `lima.yaml`, and on adopted hosts the in-VM reconciler re-asserts the desired `authorized_keys` on every boot regardless of what cloud-init replays.
- `mngr imbue_cloud hosts rotate <host>` rotates everything by default (client key + both endpoints' host keys), pushes the updated record, and other devices converge on next pull. Lost-device revocation is a documented runbook: sign out all devices, change master password (DEK rewrap), rotate each cloud workspace.
- Sharing UX is unchanged, but every desktop-driven share (local and cloud rows alike) now injects materials client-side over the user's own SSH; the connector's server-side primitive serves only web-created workspaces. An old minds version sharing an adopted host fails with a clear error (documented caveat; upgrade path).
- Wedged production hosts are repaired by an operator-run sweep that patches `lima.yaml` and restores VM-root `authorized_keys` from the workspace's own container copy; its single-host mode plus backup restore is the break-glass story (no standing owner-initiated repair endpoint).
- Old minds versions interoperate: the record wire and payload shapes are unchanged; old devices keep today's behavior on cleaner data, new devices get full replace semantics. Mixed adopted/unadopted fleets are fully supported (adoption is per-host and incremental).
- Operators can always access slice hosts for repair (pool key retained pre-strip; box-level `limactl` regardless), but the service can never re-establish itself as pinning authority — only the user's devices update the record; an operator re-key is correctly refused by devices until the user re-adopts.

## Implementation plan

### mngr core (`libs/mngr`)

- NEW `imbue/mngr/providers/host_key_store.py`:
  - `HostKeyPin` (FrozenModel): address, port, keytype, public_key, origin (`BOOTSTRAP`/`USER` enum), updated_at.
  - `HostKeyRecord` (FrozenModel): host_id + tuple of pins (+ client-key path reference).
  - Store functions: load/save per-host `host_keys.json` in the per-host state dir; `pin_host_key(...)` with the precedence rule (user-origin replaced only by newer user material; bootstrap-origin replaceable by anything); replace-by-(endpoint, keytype) always.
  - Renderer: `render_known_hosts_file(...)` — atomic write of a per-host or aggregated provider-wide `known_hosts`; emits only pins belonging to live host records (dead-endpoint GC).
- `imbue/mngr/providers/ssh_utils.py`: `add_host_to_known_hosts` / `clear_host_from_known_hosts` become store-backed shims (write through the store, then render); keep signatures so call sites migrate incrementally.
- `imbue/mngr/providers/docker/instance.py`: `_get_host_keypair` moves to a per-host dir (vps `per_host_key_dir` pattern + legacy read fallback); per-host client key for new containers; pinning through the store.
- `imbue/mngr_forward/ssh_tunnel.py` (Phase 0): remove the `AutoAddPolicy` fallback — a missing known_hosts file is an error, not trust-on-first-use.

### provider plugins

- `libs/mngr_modal/imbue/mngr_modal/instance.py`: per-host host keypair keyed by host_id (injection-at-boot already handles restore); same key re-injected on `start_host`, fresh host + client keypair minted on clone (`create_host --snapshot` to a new host_id); no legacy fallback needed (sandboxes cycle within ~a day; old shared key file left until then, `CLEANUP:`-marked).
- `libs/mngr_lima/imbue/mngr_lima/`:
  - `lima_yaml.py`: cidata `_build_root_authorized_keys_block` append-if-absent instead of truncating (salvaged from PR 382, with its executed-bash tests).
  - `instance.py`: per-host client key for new VMs (replacing shared `root_ssh_key`, with read fallback); per-host pinning through the store.
- `libs/mngr_vps/imbue/mngr_vps/` (covers vultr/ovh/aws/gcp/azure): per-host `vps_ssh_key` + `container_ssh_key` for new hosts (generation at create; threading through `cloud_init.py`, `container_setup.py`, `bare_realizer.py`, `docker_realizer.py`; resolution falls back to the provider-wide pair for legacy hosts); provider-wide `vps_known_hosts` rendered from the store.
- `libs/mngr_latchkey/imbue/mngr_latchkey/discovery.py`: warn once per host on `UNAUTHENTICATED` instead of silent skip (salvaged from PR 382).

### mngr_imbue_cloud (`libs/mngr_imbue_cloud`)

- NEW `imbue/mngr_imbue_cloud/providers/adoption.py`:
  - `ensure_adopted(...)`: idempotent, marker-driven; cheap marker check on connect, full re-verification at most once per process lifetime per host plus after start/restart/rebuild; verifies host keys, `authorized_keys` desired state, and the reconciler unit; heals drift.
  - Two-phase host-key rotation for both endpoints: install new key alongside old (sshd serves multiple host keys), pin user-origin, verify a fresh strict connection, then remove old keys and other keytypes. Crash mid-rotation never strands the host.
  - Reconciler installation: writes `/etc/systemd/system/` unit (ordered after cloud-init, reboot-resilience-units precedent) + root-owned desired-state `authorized_keys` file (user keys ∪ pool key, pre-strip); the unit re-asserts the file on every boot, defeating cidata replay.
  - Adoption marker in the per-host state dir; desired-state content rendered from the store.
- `imbue/mngr_imbue_cloud/providers/instance.py`:
  - `_ensure_host_key_pinned` becomes store-backed: writes bootstrap-origin pins only, never displacing user-origin ones; keytype-aware (Phase 0 gets the minimal keytype fix ahead of the store).
  - Adoption hooks: after lease in `create_host` (fast and slow paths — the slow-path rebuild runs full adoption as part of setup), in the connect path (ensure_adopted), and in `_start_workspace_and_wait` (re-pin the new endpoint from the store, not from connector keys).
- NEW CLI `hosts rotate` (in `cli/`): rotates client key + both host keys by default, updates desired state via the reconciler file, pushes pins into the local store; minds' next reconcile pushes the record.
- NEW CLI `admin repair-keys` (operator sweep): fleet mode patches each slice's `lima.yaml` provision block on its box and repairs wiped VM roots by copying the container's own `authorized_keys` upward; single-host mode is the break-glass tool. Uses box access + pool key; no DB column needed.

### minds (`apps/minds`)

- `desktop_client/workspace_record_store.py`:
  - Phase 0: `merge_known_hosts_text` → replace-by-(endpoint, keytype) (record-wins on non-leasing devices; leasing devices keep the lease.json skip); `WorkspaceSecretsPayload` gains `extra="ignore"`.
  - Phase 4: `collect_ssh_key_material` reads the store (renders clean current pins; never slurps files; drops the broken lima fallback; provider-wide keys never enter a record); import parses `ssh_known_hosts` text into pins and applies them through the store, gated on record revision via a new local-only `last_applied_secrets_revision` field (sibling of `secrets_content_hash`).
- `desktop_client/sharing_handler.py` (Phase 1): delete the `is_cloud_row` server-side branch — all desktop-driven shares use the client-side path (`shares create` + `share_materials_injection`); parity checklist vs the server primitive (share record, relay token, grants, owner_email, entry label, readiness probe).
- Docs: `libs/mngr_imbue_cloud/README.md` (adoption + rotate), minds glossary ("adoption" entry), `apps/minds/docs/security-boundaries-audit.md` (trust-model change; stopped-artifact operator-decryptability note), NEW lost-device runbook doc; re-land PR 382's investigation doc.

### remote_service_connector (`apps/remote_service_connector`)

- No new columns or endpoints. `enable-sharing` remains for web-created workspaces only (desktop callers migrate off it in Phase 1); its retirement for desktop flows documented with the mixed-version caveat.

## Implementation phases

Each phase is a stacked PR: Phase 0 stacks on `mngr/remote-machine-stopping`, each later phase on the previous.

1. **Phase 0 — correctness fixes (small, ships first).** Replace-don't-append in the minds materializer with record-wins; keytype-aware `_ensure_host_key_pinned`; remove the `ssh_tunnel.py` AutoAddPolicy fallback; `extra="ignore"` on the payload model. Heals currently-wedged sync imports on their next pass.
2. **Phase 1 — sharing unification.** All desktop-driven shares go client-side (cloud rows included, independent of adoption); server primitive becomes web-only. Standalone and adoption-prerequisite.
3. **Phase 2 — structured key store + per-host conversions.** The store + renderer in mngr core; all providers route pinning through it (including remote-machine-stopping's start-path re-pin); per-host host keys for docker/modal; per-host client keys for new hosts in every provider, with legacy fallbacks; dead-endpoint GC.
4. **Phase 3 — imbue_cloud adoption.** `adoption.py` (rotation, reconciler, ensure-adopted, marker); adoption hooks at create/connect/rebuild/start; salvaged 382 fixes (cidata generator, latchkey warn, doc); `admin repair-keys` sweep (+ fleet run on each tier); `hosts rotate`. Slices only.
5. **Phase 4 — minds sync semantics.** Render-from-store on push; revision-gated replace on import; store-backed materialization; docs (README, glossary, security boundaries, lost-device runbook).
6. **Phase 5 — strip readiness (design only, no implementation).** Checklist versioned with the plan (see [strip-readiness-checklist.md](./strip-readiness-checklist.md)): pool/bake-key removal is a reconciler desired-state content change; audit the stop/start supervisor's in-VM operations; retire the web-only sharing primitive's container access for adopted rows; confirm no remaining operator SSH path assumes bake keys.

## Testing strategy

- Unit tests (mngr core): store precedence (user vs bootstrap, replace-by-endpoint+keytype, newer-user-wins), renderer output + dead-endpoint GC, known_hosts parsing round-trip, shim behavior of `add_host_to_known_hosts`.
- Unit tests (mngr_imbue_cloud): rotation state machine against a mock host interface (two-phase ordering, crash-at-each-step leaves a connectable host); ensure-adopted verification/heal decisions; desired-state rendering (pool key preserved pre-strip); sweep command rendering.
- Unit tests (minds): payload compat (legacy blob parse, `extra="ignore"` with unknown fields, old-shape round-trip), revision-gated import (stale revision ignored, newer replaces, dirty rows preserved), record-wins vs lease.json skip, no provider-wide key ever collected.
- Integration tests: adoption end-to-end against a local docker host standing in for a slice (rotate, reboot-simulated reconciler re-assert, ensure-adopted heals a manually-clobbered `authorized_keys`); sharing parity (client-side path produces the same in-workspace materials the server primitive did); per-host client/host key creation + legacy fallback per provider.
- minds `deployment_tests/` (real dev env, real boxes): full adoption lifecycle — create (adopt) → stop/start relocation (pins re-derived at new endpoint) → rotate → second-device sync (records pulled, materialized, connect succeeds) → sweep repair of a deliberately-wiped VM root → destroy. No new acceptance tests; existing suites must stay green.
- Edge cases: adoption crash between install-alongside and commit (retry completes); import racing a local lease (lease.json wins); record with rotated keys read by a pre-Phase-0 device (append behavior on clean data — degraded but not wedged); endpoint recycling across slices (per-host files keep pins separate); modal clone mints fresh keys while restore reuses them; old client calling server-side share on an adopted host (clear failure).
- Manual verification before completion (per repo rule): exercise the two-device sync + adoption story by hand on a dev env exactly as a user would.

## Open questions

- Per-account SSH host CA (certificates instead of raw pins) as the eventual end state — blocked on paramiko's lack of host-certificate verification; revisit if strict-SSH paths move to the OpenSSH CLI.
- Strip-phase timing: what fleet/telemetry condition triggers actually removing pool/bake keys (and closing the boot-window gap by ordering sshd after the reconciler)?
- Per-device client keys (instead of one synced per-workspace key) as a later refinement — better lost-device story, needs an authorized_keys reconciliation and new-device bootstrap design.
- Support posture for the mixed-version sharing caveat: silently tolerate old-client failures against adopted hosts until auto-update catches up, or add a server-side error message pointing at the upgrade?
- Reconciler unit versioning: how does a later mngr version upgrade an installed unit/desired-state format (likely ensure-adopted rewrites on drift, but state the compatibility rule)?
