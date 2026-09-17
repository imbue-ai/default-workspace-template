# `cutover migrate` carries the workspace's latchkey state onto the gen-2 VM

Status: decided 2026-09-13 (Josh + the staging migration drill); implemented on `mngr/gen2-ci-rollout-docs` (2026-09-13); the live drill (L8) passed on staging on 2026-09-14, twice: once with a desktop whose supervisor re-provisioned the new VM, and once (`workspace-migrate-in-old`, a 0.5.2 desktop whose supervisor never reached the new VM) with the replayed gateway alone answering a GitHub call from inside the container -- the desktop-independent proof L8 asked for (see `apps/minds/docs/deploy/history/minds-v0.6.0.md`).
Refines [`phase-5.5-incremental-rollout.md`](./phase-5.5-incremental-rollout.md) and the harvest-and-replay design in [`phase-4-cutover-tooling.md`](./phase-4-cutover-tooling.md) section 6.5.
Runbook: [`apps/minds/docs/deploy/gen2-cutover.md`](../../apps/minds/docs/deploy/gen2-cutover.md).
Related but out of scope: imbue-ai/mngr-internal#970 (the desktop's latchkey supervisor never follows a workspace restored onto new coordinates); that is fixed separately.

## 1. Purpose and scope

A migrated workspace must come back with its service sign-ins intact and a working latchkey gateway, without waiting for the user's desktop.
Today `cutover migrate` transplants only the workspace's data disk into a fresh gen-2 VM, so everything latchkey keeps on the VM is lost: the credential store, the gateway's config and policy, its supervisor programs, and its tmpfs secrets.
The user's agents then see every service as signed out and ask for each again ("your GitHub sign-in didn't survive the restart"), which is what the 2026-09-13 staging drill observed on both migrated workspaces.

This spec covers only `minds-admin cutover migrate` (and the shared code it reuses from `mngr_latchkey`).
It does not change the desktop client, the connector, ordinary stop/start, or rollback.

Audience: the agent implementing the change in `apps/minds_admin` and `libs/mngr_latchkey`.

## 2. Background: where latchkey state lives on a slice VM

Latchkey's design is that the machine owns its credentials (see the module docstrings of `libs/mngr_latchkey/imbue/mngr_latchkey/remote/_mirror.py` and `remote/credentials.py`).
The desktop keeps only a mirror under `<latchkey_directory>/mngr_latchkey/hosts/<host_id>/`, which it refreshes from the machine and never trusts between times.
On the slice VM, the state is spread over three places:

| Where | Files | Written by | Survives an ordinary gen-2 stop/start? |
|---|---|---|---|
| `/root/.latchkey/` (boot disk) | `credentials.json.enc`, `config.json`, `permissions.json`, `data-format-version`, `extensions/desktop_gateway_proxy.mjs`, `container_tunnel_key`, `container_tunnel_key.pub`, `gateway_run.sh`, `gateway.log`, `tunnel.log` | the desktop's provisioning pass (`remote/provisioning.py`) and the gateway itself | yes: the stop artifact carries the boot disk |
| `/etc/supervisor/conf.d/` (boot disk) | `latchkey-gateway.conf`, `latchkey-tunnel.conf` | provisioning | yes |
| software (boot disk) | apt `supervisor`, Node, the npm-global `latchkey` CLI, `/usr/local/bin/latchkey-curl-dispatch` and `latchkey-curl-impersonate` | provisioning (`ensure_latchkey_installed`) | yes |
| `/run/mngr-latchkey/` (tmpfs, 0700) | `gateway_encryption_key`, `gateway_listen_password` (the machine's own pair), `desktop_gateway_password`, `desktop_permissions_override` (the connected desktop's pair) | provisioning | no, by design; the desktop rewrites them on its next pass |

Two properties the design depends on:

- `credentials.json.enc` is encrypted under the machine's own key, which exists only in `/run/mngr-latchkey/gateway_encryption_key` and in the desktop's durable mirror (`machine_encryption_key`).
  The key is deliberately never written to the VM's disk (`remote/_machine.py`, `TMPFS_SECRETS_DIR`).
- `gateway_listen_password` is what the workspaces on the machine present as `LATCHKEY_GATEWAY_PASSWORD`; it is baked into each workspace's container env at `mngr create` and can never change (`_resolve_machine_gateway_password`).

`gateway_run.sh` refuses to start the gateway when either of the machine's two tmpfs files is missing, so a VM with the disk files but no tmpfs pair has no gateway until a desktop provisioning pass.

## 3. What the migrate does today, and what it loses

`_harvest_workspace` in `apps/minds_admin/imbue/minds_admin/cli/cutover_drivers.py` reads, from the live gen-1 VM over VM-root SSH: the VM's sshd host key pair and root `authorized_keys`, the container's sshd host key pair and root `authorized_keys`, the container's `docker inspect`, and its `git describe`.
The keys are persisted 0600 under `~/.minds-<env>/cutover/keys/<host_db_id>/` (`slices/cutover_state.py`) and shredded after the restore.
`_boot_and_replay_workspace` then materializes the transplanted data disk on a fresh gen-2 VM booted from the pinned guest image, loads the version's image tar, recreates the container from the harvested inspect, `docker cp`s the harvested container files in, runs the template's autostart installer, and probes health.

Nothing in that list touches `/root/.latchkey`, the supervisor confs, the software, or the tmpfs secrets.
The fresh gen-2 VM has none of them.
The container's root `authorized_keys` (replayed) still contains the public half of the origin's `container_tunnel_key`, but the private half is gone.

When the desktop later provisions the migrated VM it reinstalls the software, restores `permissions.json` from its canonical copy, and rewrites the tmpfs pair from its mirror of the machine's key and password.
It never pushes the credential store (the machine's store is authoritative), so the agents' sign-ins are gone for good, and any OAuth refresh tokens the machine had rotated since the desktop's last mirror are unrecoverable.

## 4. Settled decisions (do not re-open)

| # | Decision |
|---|---|
| L1 | The migrate moves the machine-owned latchkey state: the files provisioning and the gateway keep under `/root/.latchkey/` (`MACHINE_LATCHKEY_DISK_FILENAMES` plus the bundled `extensions/`), both supervisor confs, and all four tmpfs files. Left behind on purpose: the two logs, the files the desktop mirrors onto the machine (`browser_state.json.enc`, `last-daily-count`; the desktop's next pass re-supplies them) and a pre-supervisord build's `gateway.pid` / `tunnel.pid` (provisioning's legacy teardown kills whatever PID such a file names, so a stale one must not reach the new VM). |
| L2 | The migrated VM comes back with a working gateway on its own: the migrate installs the software, replays the files, and starts the two supervisor programs before the health probe. It does not wait for the desktop. |
| L3 | The install and supervisor steps reuse `mngr_latchkey.remote.provisioning` (same version pins as the desktop) through an `OuterHostInterface`; no second installer is written in `minds_admin`. Private helpers that are needed are made public (or wrapped in one public entry point in `mngr_latchkey.remote`) rather than imported by underscored name. |
| L4 | Secrets transit the operator's cutover state dir like the harvested SSH keys today: written 0600, shredded after the restore. The tmpfs pair is written on the target only into `/run/mngr-latchkey`, after the same RAM-backed filesystem check the desktop performs. |
| L5 | The latchkey state is harvested at the existing harvest point, from the live origin VM, before the product stop. A token the gateway refreshes between harvest and stop is lost; accepted. |
| L6 | An origin with no `/root/.latchkey/` skips the whole latchkey replay (logged, not an error). An origin whose disk files exist but whose tmpfs pair is absent replays the disk files and confs, installs the software, and skips the start; the desktop's next pass supplies the pair. |
| L7 | Rollback needs no change: the gen-1 stop artifact's boot disk still holds the store. |
| L8 | Verification is unit tests for the new harvest and replay pieces plus a live staging drill on a fresh 0.5.2 workspace holding a granted credential, with the desktop closed. |

## 5. Design

The change adds a latchkey leg to each of the migrate's existing stages.
Names below are suggestions; keep them consistent with the surrounding code.

### 5.1 Harvest (extend `_harvest_workspace`)

Over the same VM-root `OuterHost` the key harvest already opens:

1. Run one harvest command (`cutover_scripts.build_vm_latchkey_harvest_command()`) that prints, each behind a marker line naming the path and its octal mode (`stat -c %a`), the **base64** of:
   - `/root/.latchkey/credentials.json.enc`, `config.json`, `permissions.json`, `data-format-version`, `container_tunnel_key`, `container_tunnel_key.pub`, `gateway_run.sh`
   - every regular file under `/root/.latchkey/extensions/` (the command lists the directory itself; the current content is one file, `desktop_gateway_proxy.mjs`)
   - `/etc/supervisor/conf.d/latchkey-gateway.conf`, `/etc/supervisor/conf.d/latchkey-tunnel.conf`
   - `/run/mngr-latchkey/gateway_encryption_key`, `gateway_listen_password`, `desktop_gateway_password`, `desktop_permissions_override`

   **Do not reuse `_build_marked_cat_command` / `parse_marked_files` as they are.**
   That builder deliberately fails the whole chain on a missing file (a missing sshd key must abort the harvest), and its parser normalizes every file to end in a newline, which is wrong for byte-exact secrets such as the 43-byte `gateway_encryption_key` (the gateway reads it with `$(cat ...)`, so a trailing newline would happen to work, but the store and keys must round-trip byte for byte).
   Write a sibling builder and parser: one guarded block per path (`[ -f p ] && { echo MARKER p $(stat -c %a p); base64 -w0 p; echo; }`), so an absent file simply emits no marker, and decode base64 on the operator side.
2. Classify the result into a new frozen `HarvestedLatchkeyState` (in `slices/cutover_types.py`):
   - `disk_files: tuple[HarvestedFile, ...]` where `HarvestedFile` carries `path`, `content_base64` (`SecretStr` for `credentials.json.enc`, `container_tunnel_key`, and every tmpfs file; plain `str` otherwise -- as implemented, `SecretStr` for every file, so no harvested content can reach a repr or log whichever file it belongs to) and `mode` (whatever the origin reports; today `0600` for everything except `gateway_run.sh` at `0700` and `container_tunnel_key.pub` and `data-format-version` at `0644`).
   - `supervisor_confs: tuple[HarvestedFile, ...]`
   - `tmpfs_files: tuple[HarvestedFile, ...]`
   - `is_present: bool` (false when `/root/.latchkey/` does not exist on the origin)
3. Record in the workspace state file (`CutoverWorkspaceState`) a `latchkey_replay_plan: LatchkeyReplayPlan` enum: `ABSENT`, `DISK_ONLY`, `FULL`, so a resumed run knows what it will replay without re-reading the origin (which is gone after the stop). As implemented the field is Optional, `None` on a record written before this leg existed (nothing was harvested, so nothing replays); a `CLEANUP:` note in `cutover_types.py` says when it can become required.

### 5.2 Persist (extend `CutoverStateStore`)

Write the harvested files under `keys/<host_db_id>/latchkey/` mirroring their VM paths (`root/.latchkey/...`, `etc/supervisor/conf.d/...`, `run/mngr-latchkey/...`), via the existing `_write_private_file` (0600, atomic).
Extend `read_keys` / a new `read_latchkey_state` to load them back, and `shred_keys` to shred them.
The tmpfs pair and `credentials.json.enc` are the sensitive items; treat the whole directory as such.

### 5.3 Install (new step in `_boot_and_replay_workspace`, before the container is recreated)

Once `wait_for_guest_cloud_init_to_finish(outer)` returns and only when the plan is `DISK_ONLY` or `FULL`: call `ensure_latchkey_installed(host)` from `mngr_latchkey.remote.provisioning` (which also installs the curl binaries) against the VM-root `OuterHost`.
This installs apt `supervisor`, Node, the pinned `latchkey` CLI and the impersonate binaries at the versions the desktop pins.
Budget: minutes, network-bound; use the provisioning module's own timeouts.

**Note:** `_build_ensure_installed_script` installs only software (curl, Node, supervisor, latchkey, the curl pair); the supervisor confs and `gateway_run.sh` are written by `_ensure_latchkey_gateway_running`, which this design does not call.
So the install never overwrites the replayed confs, and it may run before or after 5.4; run it before so a failed install leaves no half-replayed state.

### 5.4 Replay disk files and confs (same stage, before the container start)

Write every harvested disk file and supervisor conf to its original path on the new VM with its origin mode, creating `/root/.latchkey/` and `/root/.latchkey/extensions/` (0700).
As implemented, each group travels as one tar built in memory (rooted at `/`, the two directories as 0700 members), uploaded 0600 with a single `outer.write_file` and extracted with `tar -xpf ... -C /`: one round trip per group rather than one per file (the repo's per-file-upload ratchet), and no file content on a command line.
`$HOME` is `/root` on both generations' guests, so paths are replayed verbatim; assert that `resolve_remote_latchkey_directory(outer)` returns `/root/.latchkey` and fail the workspace otherwise.

**Warning:** the tmpfs pair is not written here.
`/run/mngr-latchkey` is written in 5.5, after the RAM-backed check.

### 5.5 Replay tmpfs and start the gateway (after the container is created and started, before the health probe)

Only when the plan is `FULL`:

1. Run `ensure_ram_backed_secrets_dir` from `mngr_latchkey.remote.provisioning`; it creates `/run/mngr-latchkey` 0700 and refuses if the filesystem is not tmpfs or ramfs.
2. Write the four tmpfs files 0600 (as implemented: one tar uploaded into `/run/mngr-latchkey` itself and extracted there, so the key never transits the disk).
3. Reload and start the two supervisor programs with the provisioning module's `reload_supervisor_programs(host, host_name, program_name, restart=True)`, for `latchkey-gateway` then `latchkey-tunnel`.

The tunnel conf embeds the container's sshd port as published on the VM's loopback.
The container is recreated from the harvested `docker inspect`, which preserves its port bindings, so the origin's conf stays valid on the target.
As implemented, the harvest checks this rather than trusting it: `latchkey_tunnel_port_error_or_none` compares the `-p` port in the harvested `latchkey-tunnel.conf` with the inspect's `22/tcp` host binding, and a mismatch fails the workspace before the product stop, so the origin keeps running.

Ordering rationale: the tunnel program dials the container's sshd, so it must start after the container; the gateway only needs its files, but starting both together keeps one reload.

### 5.6 Health probe (extend `_wait_for_workspace_health`)

When the plan is `FULL`, add to the probe: `supervisorctl status latchkey-gateway latchkey-tunnel` on the VM reports both `RUNNING`, and a TCP connect to `127.0.0.1:1989` on the VM succeeds (the gateway's HTTP routes all need the listen password, so do not probe an HTTP path; the supervisor state plus the bound port is the check).
Report a failure the same way other probe warnings are reported; the row stays parked and the operator decides between re-run and rollback.

When the plan is `DISK_ONLY`, do not require the programs to be running; `gateway_run.sh` exits non-zero by design until the desktop writes the tmpfs pair.

### 5.7 Finish and cleanup

Unchanged: `_finish_restore` re-leases the row; the state store shreds `keys/<host_db_id>/` (now including the latchkey files) after a successful migration.
Re-runs resume from the state file as today; the replay steps are idempotent (rewriting the same files, restarting the same programs).
As implemented the harvest runs once, when the workspace has no state record yet. A re-run whose origin is still running on gen-1 (an earlier run aborted between the harvest and the stop, or an operator started the origin again) re-runs the stop but replays that first harvest, so latchkey changes made on the origin in between (a refreshed token, a new grant) are lost; to carry them, roll the workspace back and migrate it again, which harvests afresh.

## 6. Failure modes

| Situation | Behavior |
|---|---|
| `/root/.latchkey` absent on the origin | plan `ABSENT`; migrate proceeds without any latchkey work; report notes it |
| disk files present, tmpfs pair absent (origin gateway was down) | plan `DISK_ONLY`; software installed, files replayed, no start; report notes it |
| install fails (apt or npm unreachable) | workspace `FAILED` after the transplant, row stays parked; re-run resumes at the install (the state file records the plan) |
| `/run` not RAM-backed on the target | refuse to write the tmpfs pair (never to disk); workspace `FAILED`, report names the filesystem type |
| gateway or tunnel not `RUNNING` at the probe | probe warning, row stays parked; inspect `/root/.latchkey/gateway.log` and `tunnel.log` on the target |
| `$HOME` on the target is not `/root` | workspace `FAILED` before any file is written |
| the origin's tunnel drop-in dials a port the container does not publish | workspace `FAILED` at the harvest, before the stop; the origin keeps running |
| a re-run finds the origin running on gen-1 after an earlier run's harvest | the stop re-runs and the first harvest is replayed; latchkey changes made since are lost unless the workspace is rolled back and migrated again |

## 7. Testing

Unit (`apps/minds_admin`, `_test.py` beside the code, no network, no mocks of our own interfaces beyond the existing fakes):

- `build_vm_latchkey_harvest_command` renders guarded cats for every path and `parse_latchkey_harvest_output` round-trips a fixture with some files absent.
- Classification into `HarvestedLatchkeyState` yields `ABSENT`, `DISK_ONLY`, `FULL` for the three fixture shapes, with the right modes and `SecretStr` on the sensitive files.
- `CutoverStateStore` writes the latchkey files 0600 under the mirrored layout, reads them back, and `shred_keys` removes them.
- The replay driver writes files in the specified order and calls the tmpfs check before any `/run` write (use the existing fake `OuterHostInterface` in the cutover tests).

Unit (`libs/mngr_latchkey`): whatever helpers are made public keep their existing tests; add one for the new entry point if one is introduced.

Live (staging, per L8), driven from this branch with the 0.5.2 client:

1. Create a fresh workspace from the 0.5.2 desktop (a `minds-v0.5.2` row on a gen-1 box; bake one if none is available), grant it one latchkey service (GitHub), and confirm an agent can use it.
2. Quit the desktop (leave the supervisor; it is stopped by the relaunch).
3. `cutover migrate --yes-i-mean-staging --target-server-id <gen2 box> --workspace <row>`.
4. With the desktop still closed: on the target VM `supervisorctl status` shows both programs `RUNNING`, `/root/.latchkey/credentials.json.enc` is present, and from inside the container a latchkey call for the granted service succeeds.
5. Relaunch the desktop; confirm it adopts the running key and password (`Adopted the encryption key ...` is acceptable, `Abandoning the credential store` is a failure) and the service still shows as connected in the Permissions tab.

Record the drill in `apps/minds/docs/deploy/history/minds-v0.6.0.md` and update the runbook's "Verify" list in `gen2-cutover.md` to include the latchkey check.

## 8. Out of scope and follow-ups

- The desktop supervisor's stale route cache and per-session provisioning gate (imbue-ai/mngr-internal#970) are fixed separately, in `libs/mngr_latchkey/imbue/mngr_latchkey/discovery.py` on this branch after the 0.6.0 tag; a <= 0.6.0 desktop's forwarded latchkey routes still need an app restart after a migration even though the machine's own gateway is up.
- Moving `/root/.latchkey` onto the data disk (for example `/mnt/mngr-data/latchkey` with a symlink) would make the store durable by construction and let a future migration carry it with the data disk. That is a `mngr_latchkey` provisioning change and a separate spec.
- Phase 6 deletes the `cutover` group and, with it, this code.
