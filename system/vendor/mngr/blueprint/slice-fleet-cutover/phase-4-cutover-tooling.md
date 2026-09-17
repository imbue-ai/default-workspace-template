# Phase 4 -- the cutover tooling (`minds-admin cutover`): detailed design

Status: decided 2026-08-28 (Josh + the phase-4 planning session), not yet implemented.
This document refines `### Phase 4` of
[`plan-slice-fleet-cutover.md`](./plan-slice-fleet-cutover.md) and is authoritative
wherever the two differ. Everything below was checked against the code on branch
`new-fleet-phase-3` (mngr-internal PR #725); the "Corrections" section lists where the
plan's original sketch (and `~/handoff/phase-4-new-fleet.md`) were wrong.

Vocabulary: a **machine** is the slice VM (mngr host id, `pool_hosts` row); the
**workspace** is its content (the `<host_hex>` btrfs subvolume + the container). `libs/`
code says host/agent (the vocabulary ratchet in `test_meta_ratchets.py`, pinned at 337);
`apps/minds_admin` and the connector may say workspace.

## 1. Scope and non-goals

Phase 4 delivers, on one PR stacked on `new-fleet-phase-3`:

1. `minds-admin cutover {preflight, drain, repave, restore}` (`apps/minds_admin/imbue/minds_admin/cli/cutover.py`)
   with the stage drivers in `cli/cutover_drivers.py` and the pure renderers / records / DB
   helpers in `slices/cutover_{scripts,types,state,db}.py`, unit-tested, deleted wholesale in phase 6.
2. The connector's `workspace_migrating` 409 guard plus an **admin start endpoint**
   (mirror of `admin_stop_workspace`).
3. The gen-2 disk-accounting fix (root partition shrink, swapfile + image-tar cache moved onto
   the XFS partition, measured `disk_gb`, named reserve constants) -- product code, not
   cutover-only.
4. The fixed-ports variant of `render_gen2_restore_reserve_script` (a keyword parameter; the
   default path stays byte-identical).
5. Gen-2 box prep additions the restore needs (`btrfs-progs`, the `nbd` module).
6. The runbook `apps/minds/docs/deploy/gen2-cutover.md` and the `next_deploy.md` archive-cleanup line.
7. One end-to-end dev rehearsal (the exit criterion).

Non-goals: the rollout itself (phase 5), any gen-1 code deletion (phase 6), the `:22`
lockdown on staging/production (deferred by the plan), DWT changes (none expected), a
gen-1 conversion path inside the product (deleted in phase 2; never comes back).

Josh's rules that shaped this design: one-time cutover code that gets deleted, not dual-mode
product code; downtime is fine; the simplest thing that is *correct*; measure nothing that
does not need measuring (the data disks are small and sparse and boxes are not full).

## 2. Settled decisions (do not re-open)

| # | Decision |
|---|---|
| D1 | Transfer via the tier's S3 bucket as planned (`cutover/<host_id>/`); no throughput measurement, no box-to-box staging. Uploads for a box's workspaces may run concurrently (`--upload-concurrency`, default 4). |
| D2 | `stopped` gen-1 rows: preflight lists them; the runbook starts them before the window (new admin start endpoint); `drain` treats a `stopped` row that still holds its retained VM (box link set) like a leased one minus the halt; **finalized-stopped** rows (NULL placement) and `crashed` rows are refused (named in the preflight report). No offline boot-disk key harvest is built. |
| D3 | Container image: one `default-workspace-template:<tag>` tar per distinct version, built once (on the canary or the first repaved box via the existing seed path), published to `s3://<bucket>/<prefix>cutover/images/<tag>.tar.zst`, `docker load`ed into each guest at restore. The box's bake-seeded tar cache (one tar per tag) is NOT used to stage the restore images. Every workspace is rebuilt at its `git describe` version regardless of the image its container ran (env-converge replays the record on the fresh rootfs). |
| D4 | Container recreation is **harvest-and-replay**: `drain` saves `docker inspect` of the workspace container; `restore` runs `docker create` from it with a fixed set of overrides (section 6.5). The slow-path machinery (`teardown_container_on_existing_vps` / `create_host_on_existing_vps`) is NOT used (it deletes the subvolume, rewrites `host_state.json`, and mints a new container host key). |
| D5 | Dev rehearsal on an existing dev gen-1 box that does not host Josh's live workspace (or a newly ordered one); `e1396039` (15.204.140.221) is never touched without Josh present. A repaved box can be re-registered gen-1 and re-prepped to repeat the rehearsal. |
| D6 | Adoption: no client change. The harvested VM host key rides `build_qemu_slice_user_data(..., ssh_keys)`; the harvested container key is `docker cp`'d back. The client's post-relocation verification reinstalls the reconciler on the gen-2 VM; harmless (stable instance-id, no cloud-init replay). Phase 6's recognizer must tolerate that. |
| D7 | Quota: **transplant the data, not the filesystem** (section 6.3). `restore` creates a fresh gen-2 data disk and `btrfs send \| btrfs receive`s the gen-1 home subvolume into it, then `btrfs qgroup assign`s it to `1/0`. No classic qgroups, no "accept unaccounted extents". |
| D8 | Disk accounting: fix the product math (section 7): root partition ~20 GiB; swapfile and the DWT tar cache move to `/srv/mngr-slices/`; gen-2 `disk_gb` = measured XFS partition GiB recorded at `prep`; reserve = sum of named constants (64 GiB); drop the 10% fraction for gen-2; the ordering guard estimates `usable - root - boot - reserve`. |
| D9 | Connector reconcile noise during the window (parked rows vs. still-present gen-1 VMs) is accepted and documented. |
| D10 | The cutover runs from a long-lived host (tmux on the dev box or a Modal sandbox), every stage re-runnable per workspace from the JSON state dir; mutating stages gated by `--yes-i-mean-<tier>` (copy the `env.py` pattern). |
| D11 | Branching: `new-fleet-phase-4` stacked on `new-fleet-phase-3` (PR #725); the PR body links back through #725 -> #716 -> #686 -> #683 and #725's body gets a "Followed by" line (patched via `gh api`, `gh pr edit` fails). |
| D12 | Handoff Q1: keep the "agent host" `Description=` wording; delete the `new-fleet-phase-3___keep-guest-unit-descriptions-byte-identical` side branch. Handoff Q3: do not probe `e1396039`. |

## 3. Command surface

All four are env-aware exactly like `pool`/`server` (`require_activated_env_name`,
`resolve_pool_database_url`, `resolve_pool_private_key_pem`, `resolve_admin_connector_url`,
`resolve_admin_api_key_value`); a new `resolve_workspace_storage_config()` in
`cli/_tier_secrets.py` reads the tier's `storage` Vault entry (`WORKSPACE_STORAGE_*`, the KEK;
the same keys the connector's `storage.read_storage_config` reads, plus the dev-env
`WORKSPACE_STORAGE_KEY_PREFIX=<env>/` the deploy stamps -- see
`apps/minds_admin/imbue/minds_admin/envs/providers/workspace_storage`).

```
minds-admin cutover preflight  [--server-id ID ...] [--json-out PATH]
minds-admin cutover drain      --yes-i-mean-<tier> [--server-id ID ...] [--upload-concurrency N] [--dry-run]
minds-admin cutover repave     --yes-i-mean-<tier> [--server-id ID ...] [--dry-run]
minds-admin cutover restore    --yes-i-mean-<tier> [--server-id ID ...] [--pool-tag TAG] [--pool-rows-per-box N=2] [--publish-image-tars] [--image-build-server-id ID] [--dry-run]
```

Registered in `cli/root.py` (`cli.add_command(cutover)`). `--server-id` scopes to boxes;
default is every gen-1 box of the env's tier. Every stage writes a JSON report plus a
table to the state dir and exits non-zero when any workspace/box failed.

State dir `~/.minds-<env>/cutover/` (0700):

- `workspaces/<host_db_id>.json` -- a `CutoverWorkspaceState` FrozenModel: `host_db_id`,
  `host_id`, `agent_id`, `host_name`, `leased_to_user`, `origin_server_id`, `box_public_address`,
  `vm_ssh_port`, `container_ssh_port`, `is_host_key_rotated` (section 4 item 2),
  `lima_instance_name`, `lima_disk_name`, `version_tag`,
  `gen1_data_disk_virtual_gib`, `gen1_data_disk_format`, `migrated_data_disk_gib`
  (`= row.disk_gb`, which 039 stamped as `gen1 + 16`), `memory_units` (8), `artifact_key_prefix`,
  `archive_sha256`, `archive_size_bytes`, `stage` (enum: `HARVESTED`, `HALTED`, `UPLOADED`, `PARKED`,
  `RESTORED`, `FAILED`), `last_error`.
- `workspaces/<host_db_id>.inspect.json` -- verbatim `docker inspect` output of the workspace
  container (section 6.5).
- `keys/<host_db_id>/{vm_ssh_host_ed25519_key, vm_ssh_host_ed25519_key.pub, vm_authorized_keys,
  container_ssh_host_ed25519_key, container_ssh_host_ed25519_key.pub, container_authorized_keys}`
  (0600), `shred -u`'d when the workspace reaches `RESTORED`.
- `boxes/<server_id>.json` -- per-box stage (`DRAINED`, `REPAVED`, `RESTORED`) and the
  measured XFS size after repave.
- `reports/<stage>-<timestamp>.json` + `.txt`.

## 4. `cutover preflight` (read-only)

Per gen-1 box of the tier (`bare_metal_servers.box_generation = 1`), over the resolved
management dial (`box_access.resolve_server_management_dial`), and per `pool_hosts` row on it:

1. Row status classification: `leased` -> candidate; `stopped` with `bare_metal_server_id`
   set -> candidate (VM retained); `stopped` with NULL placement, `crashed`, `stopping`,
   `starting`, `removing`, `unreachable`, `baking` -> **refusal**, named with the remedy
   (start it / wait / abandon+release / destroy). `available` rows are listed (drain destroys
   them). The `stopped`-with-NULL-placement rows are on no box, so the whole-tier run lists
   them separately (`fetch_unplaced_gen1_pool_rows`, minus the rows this cutover itself parked);
   a `--server-id` run is box-scoped and leaves them out.
2. VM-root SSH as `root` at `box:ssh_port` with the pool key, pinned to
   `outer_host_public_key`... **except** that an adopted host serves its rotated key, not the
   row's bake key. Probe the served key (`is_server_presenting_host_key`) against the row
   first; on mismatch fall back to TOFU for the *probe only* and record `is_host_key_rotated`.
   The harvested live key is what restore pins, so this is safe; still report it.
3. Inside the container (`docker exec <cid>`, `cid` by label `com.imbue.mngr.host-id=<host_id>`):
   `git -C /home/user/workspace describe --tags --match 'minds-v*' --abbrev=0`. Refuse the
   tier when the tag is below `minds-v0.3.10` or the describe fails (no `minds-v*` tag
   reachable; a workspace whose clone never fetched tags). Cross-check against
   `attributes.repo_branch_or_tag` (the baked version, a lower bound) and report both.
4. Health probe (section 6.6) -- warnings only.
5. `qemu-img info -U --output=json ~/.lima/_disks/<disk>/datadisk` on the box: record `format`
   (expected `qcow2`; see Corrections) and `virtual-size`; assert `virtual-size` equals
   `row.disk_gb - 16` GiB (the 039 backfill) and refuse on mismatch.
6. Per box fit: `sum over candidates of (GEN2_BOOT_DISK_GIB + row.disk_gb) <= gen2 disk budget`
   and `count * compute_machine_memory_footprint_mib(8) <= compute_box_unit_budget_mib(ram_gb)`.
   The disk budget for a not-yet-repaved box is `compute_gen2_storage_partition_estimate_gib(disk_gb)
   - GEN2_STORAGE_RESERVE_GIB` (section 7); refuse with the shortfall.
7. Report: per workspace `account email` (admin accounts API), host name, version, floor
   verdict, health, sizes, key-rotation flag; per box fit; the set of distinct versions (the
   image tars restore needs).

## 5. `cutover drain`

Per box in parallel (bounded), per candidate workspace with `--upload-concurrency` uploads
in flight per box:

1. **Harvest** (skipped when the state file is already `>= HARVESTED`): over VM-root SSH
   `cat /etc/ssh/ssh_host_ed25519_key{,.pub} /root/.ssh/authorized_keys`;
   `docker exec <cid> cat /etc/ssh/ssh_host_ed25519_key{,.pub} /root/.ssh/authorized_keys`;
   `docker inspect <cid>` (whole JSON). Write the state dir files. Abort the workspace if
   the served VM key != harvested public key.
2. **Halt**: `limactl stop <instance>` as `limahost` + `touch ~/.lima/<instance>/mngr-stop-requested`
   (the same two commands as `box_scripts.build_stop_vm_commands`, so the gen-1 box autostart
   never resurrects it). The same two commands run for a `stopped`-with-retained-VM row: a
   no-op when the connector's stop already halted the VM, and the halt the row still needs
   when a failed in-place start left its VM running (the upload requires the VM observed halted).
3. **Upload** (a small gen-1 box script rendered in `slices/cutover_scripts.py`, NOT in
   `gen2_scripts` -- it is gen-1 and dies in phase 6; reuse `script_prelude`,
   `render_transfer_env`, `build_launch_detached_command`, `build_read_status_command`,
   `parse_status_text` from `gen2_scripts.transfer`): `zstd -T0 | age -e -r <recipient> | s5cmd pipe`
   of `~/.lima/_disks/<disk>/datadisk` to `s3://<bucket>/<prefix>cutover/<host_id>/datadisk.zst.age`,
   `tee`ing sha256 + byte count into the status file exactly like `render_upload_script`.
   A fresh age identity per workspace (`age-keygen` on the box, as `_generate_age_keypair_on_box`
   does), KEK-wrapped with the connector's envelope: base64 of
   `imbue_common.secret_wrapping.wrap_dek(kek, identity.encode())`, which is byte-identical to
   `storage.wrap_dek` (12-byte nonce + AES-256-GCM, no AAD; `imbue_common` is already a
   `minds_admin` dependency, the connector module is not importable from it) and written with
   the manifest to
   `.../cutover/<host_id>/manifest.json` (`CutoverArchiveManifest`: sha256, bytes, virtual
   size, format, version tag, ports, origin box, `wrapped_dek`, harvested public keys, the
   `docker inspect` JSON, the `git describe` output).
   Idempotent: an existing manifest whose sha matches the object's `s5cmd ls` size is a skip;
   a partial upload re-runs.
4. **Park** the row (one guarded CAS, `rowcount == 1` or fail the workspace):
   `UPDATE pool_hosts SET status='stopped', vps_address=NULL, ssh_port=NULL, container_ssh_port=NULL,
   bare_metal_server_id=NULL, transition_heartbeat_at=NULL, transition_id=NULL, artifact_manifest=NULL,
   wrapped_dek=NULL, stop_requested_at=NOW(), stopped_at=NOW(), transition_error=NULL
   WHERE id=%s AND status IN ('leased','stopped') AND box_generation=1`.
   Parked rows are invisible to the watchdog (`_IN_FLIGHT_ROW_PREDICATE_SQL` needs a box
   link) and to the retention finalize; the lease-record sweep still sees an ACTIVE record
   holding a lease, which is correct. `POST /workspaces/{id}/start` answers the 409 guard.
5. Unleased `available`/stale rows on the box: `destroy_pool_hosts_in_parallel` (the
   `pool destroy` path), as `server drain` does.
6. Box: `update_server(status='draining')` once every candidate on it is `PARKED`.

`--dry-run` prints what each step would do (rows, object keys, commands) and touches nothing.

## 6. `cutover repave` and `cutover restore`

### 6.1 Repave

Per `draining` box (parallel): refuse unless every row that was on it is `PARKED` (state dir)
or destroyed (DB). Then, in one command: `update_server(box_generation=2, status='delivered',
cpu_overcommit_ratio=DEFAULT_SLICE_CPU_OVERCOMMIT_RATIO)` (`lima_service_user` stays `limahost`:
gen-2 pins it to `GEN2_SLICE_SERVICE_USER` and `setup` re-stamps it); run the
`server setup` body (factor `setup`'s post-argument body into a function `setup_server_to_ready(...)`
that both the click command and the cutover call): OVH reinstall as `GEN2_REINSTALL_OS_TEMPLATE`
with `build_gen2_reinstall_storage()`, host key recorded, SSH wait on the public address,
composed gen-2 prep (which now measures and records `disk_gb`, section 7), status `ready`.
Idempotent: a box already `ready` on gen-2 with the prep converged is skipped; `installing`
resumes at prep (existing `setup` semantics). On dev the tier's `[modal_proxy]` block means
the repave locks `:22` down -- expected; every later step dials via `box_access` (onetun).
Staging/production have no `[modal_proxy]`, so `:22` stays open there as the plan defers.

After repave, `df --output=size -B1 /srv/mngr-slices` is recorded in `boxes/<server_id>.json`
and the restore fit is re-checked against it.

### 6.2 Image tars

Before the first restore on a tier: for each distinct `version_tag` in the preflight report,
ensure `s3://<bucket>/<prefix>cutover/images/<tag>.tar.zst` exists. Build it once with the
existing seed machinery (`SliceVpsDockerProvider._seed_box_image`, reached through the bake's
seed phase of `pool create --from-tag <tag> --count 1` on the canary / first repaved box -- it
produces the `default-workspace-template:<tag>` tar, Playwright layer included, in the box cache
dir; `pool warm-cache` does not apply, it is content-addressed-only and refuses tag-keyed
seeding), then `zstd | s5cmd pipe` it to S3 (plain, no age: it is
public template content). `cutover restore --publish-image-tars` does this step; it is
idempotent per tag. The restore's guest-side load: on the box,
`s5cmd cat ... | zstd -d | ssh -i <transfer key> -p <vm_port> root@127.0.0.1 'docker load'`
(the loopback pattern of `SshBoxImageCache.load_image_into_slice`, with a per-transfer key
authorized on the new VM root as `SliceVpsDockerProvider._transfer_key_authorized` does).

### 6.3 Data disk: fresh gen-2 disk + `btrfs send/receive` (D7)

Runs on the box as `limahost` (the box gains `btrfs-progs` and `modprobe nbd max_part=16` in
the gen-2 prep; the two `qemu-nbd` attaches need `sudo` -- add exact-argument sudoers lines
for `/usr/bin/qemu-nbd`, `/usr/bin/mount`, `/usr/bin/umount`, `/usr/bin/btrfs` on the cutover's
fixed paths, in a cutover-owned `/etc/sudoers.d/mngr-cutover` installed by `repave` and
removed in phase 6; alternatively run this step as the `debian` sudo user over the
management dial -- pick whichever needs fewer new grants, and record it). **Recorded:** the
transplant runs as root over the management dial (`run_root_script_over_ssh`, the box prep's own
path, from `cli/cutover_drivers.py`); no sudoers file is installed, so nothing has to be removed
in phase 6.

1. Download `datadisk.zst.age` to `<transplant_dir>/gen1-datadisk.qcow2` (verify sha), where
   `<transplant_dir>` is `/srv/mngr-slices/cutover/<instance>` on the storage partition: the
   transfer dir under the service user's home is on the 20 GiB root partition and holds only
   the sourced env file.
2. `qemu-img create -f qcow2 <transplant_dir>/datadisk.qcow2 <migrated_data_disk_gib>G`.
3. `qemu-nbd -c /dev/nbd0 gen1-datadisk.qcow2` (read-write: the file is a downloaded copy,
   and the read-only snapshot in step 5 needs a writable mount); `qemu-nbd -c /dev/nbd1 datadisk.qcow2`.
4. `mkfs.btrfs -L mngr-data /dev/nbd1`; mount both (`/dev/nbd0p1` -- gen-1 disks are
   partitioned; probe with `lsblk` and fall back to `/dev/nbd0` if not); on the new fs:
   `btrfs quota enable --simple`, `btrfs qgroup create 1/0`.
5. `btrfs subvolume snapshot -r <old>/<host_hex> <old>/<host_hex>-cutover-ro` (`btrfs send`
   only accepts a read-only subvolume), then
   `btrfs send <old>/<host_hex>-cutover-ro | btrfs receive <new>/`; rename to `<host_hex>`;
   `btrfs property set <new>/<host_hex> ro false`; `btrfs qgroup assign 0/<subvolid> 1/0 <new>`
   (received extents are created by the receiving subvolume, so simple quotas account them
   fully). `mkdir <new>/snapshots` (`host_backup` recreates its snapshots; nothing else
   under the old root is carried: gen-1 `snapshots/` subvolumes are dropped).
6. `umount`, `qemu-nbd -d` both, delete `gen1-datadisk.qcow2`. The prepared
   `datadisk.qcow2` is moved into the slice dir by the reserve step.

This removes the plan's `qemu-img convert`, `qemu-img resize`, offline relabel and the
`growpart` path for migrated disks: the result is byte-for-byte a gen-2 carve layout (whole-disk
btrfs, label `mngr-data`, quotas from mkfs, home subvolume in `1/0`). The guest first boot then
finds the labeled disk, creates the containerd subvolumes in `1/0`, and the grow oneshot sets
the `1/0` limit.

### 6.4 Reserve at fixed ports

`render_gen2_restore_reserve_script` gains `fixed_ports: tuple[int, int] | None = None`
(keyword-only; when set, the port pick is replaced by an in-use check of exactly those two
ports against `ss -Htln` and the recorded env files, refusing with `RESTORE_NO_PORTS_MARKER`
if either is taken). Prove the default path unchanged with the importlib byte-identity test
(load `HEAD:` module, render identical inputs, `assert old == new`;
`gen2_scripts/transfer_test.py` also pins it). The cutover also passes a `user_data_b64` (the reserve
script today copies the artifact meta tar's `user-data`; add `user_data_b64: str | None` --
when given, write it instead of fetching the meta object). The user-data is
`build_qemu_slice_user_data(host_dir="/mngr-btrfs", root_authorized_public_keys=<harvested VM
authorized_keys lines>, host_private_key_pem=<harvested>, host_public_key_openssh=<harvested>)`.
Sizing inputs: `units=8`, `data_disk_gib=row.disk_gb`, budgets from the repaved box row
(`compute_box_total_units`, `compute_box_unit_budget_mib`, the new gen-2 disk budget),
`vcpus=compute_machine_vcpus(cpu_threads, row.cpu_overcommit_ratio, 8, total_units)` (4.0 after
6.1; a carve reads the row, not the constant), `uplink_mbps` from the row.
The env template is `build_qemu_slice_env_file(..., ordinal=None, placeholders)`; meta-data
`render_gen2_restore_meta_data(instance_name, artifact_generation=row.artifact_generation + 1, ordinal=None)`.
After the marker line: `cp --reflink=auto` the base image into `<slice_dir>/disk.qcow2` +
`qemu-img resize 10G` (the carve reserve's step 5, which the connector's restore never does
because it downloads a boot disk) and `mv <transplant_dir>/datadisk.qcow2 <slice_dir>/` (a
rename: same filesystem). Then
`sudo systemctl enable` + `start mngr-slice@<ordinal>`, wait for the VM banner
(`build_gen2_wait_banner_command`), then `cloud-init status --wait` over VM-root SSH
(`wait_for_guest_cloud_init_to_finish` shape).

### 6.5 Container: harvest-and-replay (D4)

From the saved `docker inspect` JSON build one `docker create` line:

- keep: `Name` (strip the leading `/`; it is `<bake MNGR_PREFIX><host_name>` with the bake's
  `host_name` = `slice-<hex>`, and
  `host_state.json` -> `config.container_name` addresses the container by it), all
  `Config.Labels` (`com.imbue.mngr.{host-id,host-name,provider,tags}`), `Config.Env`,
  `HostConfig.PortBindings` (`0.0.0.0:2222->22/tcp`), the three mounts as recorded
  (`mngr-host-vol-<hex>` -> `/mngr-vol`, `mngr-snapshot-trigger-<hex>` -> `/mngr-snapshot`,
  `/mngr-btrfs/snapshots:/mngr-snapshots:ro`), `HostConfig.RestartPolicy` (`unless-stopped`).
- override: image `default-workspace-template:<version_tag>`; `--runtime runsc`;
  `--tmpfs /run --tmpfs /tmp` (`GEN2_CONTAINER_TMPFS_START_ARGS`); `--workdir=/`;
  `--security-opt=no-new-privileges`; `--memory`/`--memory-swap` =
  `build_slice_container_memory_start_args(compute_machine_guest_memory_mib(8))` (6656m);
  entrypoint `sh -c <CONTAINER_ENTRYPOINT_CMD>` (the current constant, not the recorded one).
- before create: `create_bind_volume_on_outer` for the host volume (`device=/mngr-btrfs/<hex>`),
  then `provision_snapshot_helper_on_outer` (`btrfs_mount_path=/mngr-btrfs`,
  `subvolume_path=/mngr-btrfs/<hex>`, `trigger_volume_name=mngr-snapshot-trigger-<hex>`), which
  creates the trigger volume (`device=/var/lib/mngr-snapshot`) itself; both via an
  `OuterHost` over VM-root SSH (pool key, pinned to the harvested VM key) with
  `mngr_ctx` -- `minds_admin` already depends on `mngr`/`mngr_vps`.
- after create, before start: `docker cp` the harvested container host key pair to
  `/etc/ssh/`, `authorized_keys` to `/root/.ssh/`, and an empty
  `SSHD_PROVISIONED_MARKER_PATH` (`/etc/ssh/mngr_host_provisioned`, which mngr's own
  container setup writes and the image's self-healing entrypoint gates sshd on -- without it
  the container comes back from a VM reboot or a connector start with no sshd) (a `docker cp`
  into a created-but-not-started container works; `chmod 600/644` via a one-shot
  `docker run --rm --volumes-from` is unnecessary -- `docker cp` preserves modes from a tar
  stream built with the right modes).
- `docker start`; recreate the `/home/user -> /mngr-vol/home` symlink mngr's container setup makes at bake
  (the image ships a plain empty `/home/user`): `build_home_volume_symlink_command` from `mngr_vps`, with the
  slice provider's `volume_home_path` read from the tag's `settings.toml`; `start_container_sshd(outer, name)`.
- install the `minds-autostart` outer units: the script text is the DWT tag's
  `.mngr/settings.toml` -> `create_templates.pool_host.post_host_create_outer_command__extend`
  (parse with `tomllib` from a clone of the tag; the cutover already needs the tag checkout
  for the image build). Run each entry as root over VM SSH. It `touch`es
  `/mngr-btrfs/.minds-volume-ready` behind a mountpoint check and starts the path unit, which
  runs `minds_start_services_agent.sh` (`mngr start system-services`) inside the container.
- `host_state.json` on the volume is left alone. (Its `container_id` goes stale; no reader
  found -- `PlacementHandle` resolves by `container_name`. Re-check before shipping; if a
  reader exists, patch the one field.)

### 6.6 Health probe

On the box: both banners (`build_gen2_wait_banner_command` for `vm_ssh_port` and
`container_ssh_port`). Over VM SSH: `docker inspect -f '{{.State.Running}}'`; inside the
container (`docker exec`): `supervisorctl status` every program `RUNNING` or `EXITED` (the one-shots exit by design; poll up to 10 min -- the
autostart's `mngr start` and env-converge's slow phase take time), and
`curl -fsS http://127.0.0.1:8000/` (the `system_interface`) answering. Failure marks the
workspace `FAILED` with the detail, leaves the row parked, keeps the slice dir for a re-run
(the fixed-ports reserve reclaims a leftover dir for the same instance).

### 6.7 Final CAS and box finish

`UPDATE pool_hosts SET status='leased', vps_address=%s, ssh_port=%s, container_ssh_port=%s,
bare_metal_server_id=%s, box_generation=2, memory_units=8, transition_error=NULL,
transition_failure_count=0, stop_requested_at=NULL, stopped_at=NULL WHERE id=%s AND
status='stopped' AND bare_metal_server_id IS NULL AND disk_gb=%s` (asserting the 039 size;
`rowcount == 1`). `attributes`, `agent_id`, `host_id`, `host_name`, `lima_instance_name`,
`lima_disk_name`, `outer_host_public_key`, `container_host_public_key` untouched (the
harvested keys equal the served ones; for adopted hosts the row's bake key stays stale as
today). Then shred `keys/<host_db_id>/`, mark `RESTORED`.

Box finish: every candidate `RESTORED` -> `update_server(status='ready')` (setup already
set it; assert) -> bake `--pool-rows-per-box` default rows from `--pool-tag` via
`allocate_slices` (region label from the box's `region` through `US_REGION_BY_OVH_DATACENTER_CODE`).

### 6.8 Idempotency matrix

| state file stage | drain does | restore does |
|---|---|---|
| absent | harvest, halt, upload, park | refuse (not drained) |
| HARVESTED | halt, upload, park | refuse |
| HALTED/UPLOADED | (re)upload if incomplete, park | refuse |
| PARKED | skip | full restore |
| FAILED (restore) | skip | reclaim dir via reserve, retry |
| RESTORED | skip | skip |

A row already `leased` on `box_generation=2` is `RESTORED` regardless of the state file.

## 7. Gen-2 disk accounting (D8, product code)

- `apps/minds_admin/imbue/minds_admin/slices/ordering.py`: `_GEN2_ROOT_PARTITION_MIB` 102400 -> 20480
  (OS + journald + apt + prep artifacts only). `_GEN2_BOOT_PARTITION_MIB` unchanged.
- `slices/bare_metal_prep.py` gen-2 prep: swapfile at `/srv/mngr-slices/swapfile`
  (`_SWAPFILE_PATH` becomes per-generation), the DWT tar cache dir under `/srv/mngr-slices/`
  for gen-2 (`box_default_workspace_template_cache_dir` grows a generation parameter or a
  gen-2 sibling in `gen2_scripts.layout`; `SshBoxImageCache.cache_dir` callers follow),
  `btrfs-progs` added to `_GEN2_BOX_APT_PACKAGES`, `nbd` in `/etc/modules-load.d/`. The prep
  echoes `MNGR_STORAGE_PARTITION_GIB <n>` (from `df --output=size -B1 /srv/mngr-slices`,
  floored to GiB) and `prep_box`/`setup` record it as `disk_gb` for gen-2 rows (a marker-line
  parse like `_record_box_wireguard_public_key`).
- `libs/mngr_imbue_cloud/.../gen2_scripts/sizing.py`: new constants
  `GEN2_SWAPFILE_GIB = 32`, `GEN2_IMAGE_TAR_CACHE_GIB = 16`, `GEN2_BASE_IMAGE_GIB = 4`,
  `GEN2_STAGING_MARGIN_GIB = 12`, `GEN2_STORAGE_RESERVE_GIB = 64` (their sum, asserted by a
  test), `GEN2_ROOT_PARTITION_GIB = 20`, `GEN2_BOOT_PARTITION_GIB = 1`;
  `compute_gen2_disk_budget_gib(storage_partition_gib) = storage_partition_gib - GEN2_STORAGE_RESERVE_GIB`
  (raises `InvalidMachineSizeError` at <= 0);
  `compute_gen2_storage_partition_estimate_gib(usable_disk_gb) = usable - root - boot` for
  the pre-delivery ordering guard. `compute_box_disk_budget_gib` (the 10%/20 GiB rule) is
  deleted: gen-1's slot math never calls it (`bare_metal.compute_slice_disk_budget_gib` applies
  `DISK_RESERVE_GB` / `DISK_RESERVE_FRACTION` itself, and those constants stay), and every
  existing caller is gen-2 and moves to the new budget: `stop_start._gen2_box_budgets` and
  `stop_start._plan_eviction_for_box`, `server.py` `_format_gen2_capacity` /
  `compute_server_slice_sizing` (which feeds the `slice_provider` / `qemu_slice_client` carve
  budgets), `bare_metal._gen2_default_machine_disk_budgets` behind
  `assert_gen2_box_disk_fits_default_machines` (uses the estimate; `pricing.py`'s units-valid
  column follows through it). `sizing_test.py` pins the arithmetic.
- The connector's `BoxRow.disk_gb` semantics for gen-2 rows are now "storage partition GiB";
  the two-budget guard sums against the new budget. No migration needed (`disk_gb` stays an
  integer column); dev-josh-2's row is refreshed by the next `prep` (do that as part of the
  rehearsal, not by hand).
- Docs: `host-pool-setup.md` "Disk layout inside a gen-2 slice" and the two-budget paragraph,
  `specs/slice-fleet/spec.md` sizing constants (a "Superseded" note), the plan's Phase 1
  `SLICE_BOOT_DISK_GIB` bullet is unaffected.

## 8. Connector changes (small; `# CLEANUP: remove in phase 6`)

- `workspaces.py` `start_workspace`: after the ownership read, `if box_generation == 1 and
  vps_address is None and status == 'stopped' and artifact_manifest is None`:
  `raise HTTPException(409, detail={"code": "workspace_migrating", "message": "this workspace is
  being migrated to new infrastructure and will come back on its own"})` before the quota lock.
  The `artifact_manifest` clause is what tells a parked row from a finalized-stopped one: only
  the park CAS (section 5 step 4) clears the manifest, while the connector's own stop records
  it in the same UPDATE that makes the row `stopped` and the retention finalize never touches
  it -- so the finalized-stopped gen-1 rows that exist between the release deploy and the
  tier's window keep starting normally. Unit test: gen-1 + NULL placement + NULL manifest ->
  409; gen-2 rows, placed rows, and a finalized-stopped gen-1 row (NULL placement, manifest
  present) unaffected.
- `POST /admin/workspaces/{host_db_id}/start` (admin key): the owner start's CAS without the
  ownership/quota checks (quota re-check kept? No -- the operator is restarting a workspace
  the user already had running; skip quota, keep the `stopped`-only precondition). Exposed
  as `minds-admin workspaces start <host_db_id>` and used by the runbook's pre-window step.
  `ImbueCloudConnectorClient.admin_start_workspace` alongside `admin_stop_workspace`.

## 9. Rehearsal plan (the exit criterion)

1. Pick a dev gen-1 box that does not host Josh's workspace (or order one: `server order
   --box-generation 1` via `just order-server`, `await-delivery`, `setup` -- `setup` has no
   generation flag, it dispatches on the recorded one), prep gen-1, `pool create --count 3`
   on `dev-josh-2`'s env (or a dedicated dev env), lease all three with the canary test account
   (`canary-sizing-4b0f3e59@imbue.com`), open one in the desktop, write data into `/home/user`,
   `apt install` something (env-converge record), stop-and-start one of them to have a
   `stopped`-with-retained-VM row, run `update-self` on one to a newer tag.
2. `cutover preflight` (expect: 3 candidates, versions, fit).
3. `cutover drain --yes-i-mean-dev` (expect: 3 parked rows, 3 manifests, box `draining`;
   `POST /workspaces/{id}/start` -> 409; desktop shows stopped).
4. `cutover repave --yes-i-mean-dev` (expect: box gen-2 `ready`, `disk_gb` measured, `:22`
   locked down, `df` matches).
5. `cutover restore --yes-i-mean-dev --pool-tag <tag>` (expect: 3 `leased` rows at the same
   address/ports, desktop opens each with no key prompt, `/home/user` intact, apt package
   present after env-converge, `supervisorctl status` RUNNING, quota `btrfs qgroup show`
   accounts the home subvolume, resize (`machines resize --units 16` + restart) works, 2 pool
   rows baked).
6. Repeat once from scratch (re-register the box gen-1, re-prep) to prove idempotency of a
   partially failed run: kill `restore` mid-workspace and re-run.

Status: **passed -- the exit criterion is met.** Run 1 (2026-08-29/30, at `minds-v0.4.3`)
restored all three workspaces and exercised two resume paths, but step 5's apt check failed
on the DWT env-converge fresh-rootfs record clobber (DWT #523) and the step-6 from-scratch
repeat was deferred. Run 2 (2026-09-01, from scratch at `minds-v0.4.4`, which carries the
DWT fix): the box was re-registered gen-1 and re-prepped (~13 min), three fresh workspaces
baked with `--from-tag` (~11 min), seeded and one stop/admin-started (all three fresh
slices first needed the services agent started by re-running the outer autostart -- later
proven to be NOT a tmux race but the bake's designed parked state: raw `POST /hosts/lease`
does not start the services agent (only claim/adopt does), and the bake's chat-agent
teardown targeted the bake host name instead of `Chat-1`, leaking the bootstrap chat; both
fixed on main post-rehearsal -- see mngr-internal#775 -- and neither was cutover code);
preflight CLEAN first try;
drain 3m46s; repave ~13 min; restore with three deliberate driver kills (mid-`btrfs
send`, at the VM-banner wait after a transplant, and between materialize and unit start)
each resumed by a plain re-run. Every step-5 check passed, including `dpkg -s cowsay` on
the restored workspace (env-converge replayed the record) and the resize 8 -> 16 via the
user API. Timings (14-slot box, three small workspaces): gen-1 re-register+setup 13 min,
bake 11 min, drain 3m46s, repave 13 min, image seed bake + publish ~7 min, transplant
~1 min per workspace, restore ~3 min per workspace plus probe, pool bake ~6 min.

## 10. Testing strategy

Unit (inline snapshots; `assert_valid_bash` on every rendered script): the gen-1 upload
script; the fixed-ports reserve (+ byte-identity of the default path); the send/receive
disk script; the `docker create` line built from a fixture `docker inspect` JSON (name,
labels, mounts kept; overrides applied); the autostart extraction from a fixture
`settings.toml`; the manifest model round-trip and DEK wrap/unwrap compatibility with the
connector's `storage.unwrap_dek` (same bytes); preflight classification (every row status),
version floor parse, fit math; report rendering; the sizing constants and both new budget
functions; `pool_hosts` CAS SQL shapes against the connector's fake store where the tests
already exist (`stop_start_test.py` style). Connector: the 409 guard, the admin start
endpoint. Ratchets to keep green: connector `check_yaml_usage` 17, plugin 48,
`test_meta_ratchets.py` (workspace vocabulary 337 -- new "workspace" words only in
`apps/`, migration numbers) and each touched project's `test_ratchets.py` (short uuids,
if/elif-without-else). Targeted
`just test-quick` only; CI is the full run.

## 11. Runbook (`apps/minds/docs/deploy/gen2-cutover.md`)

Per tier: prerequisites (release deployed; alerting armed; dev boxes consolidated to one env);
the announcement text (floor `minds-v0.3.10`, the gVisor note, the window); the pre-window
step (preflight, start every stopped workspace via `minds-admin workspaces start`, re-run
preflight until clean); publish image tars; the command sequence with `--dry-run` first;
verification checklist (section 9 step 5); what to do on a `FAILED` workspace (re-run
`restore` for the box; if the probe keeps failing, the row stays parked and the user is
told); the accepted reconcile noise; the `next_deploy.md` line
`CLEANUP: delete s3://<bucket>/<prefix>cutover/ (archive of every migrated data disk) after <date well past production>`.

## 12. Corrections to the plan and `~/handoff/phase-4-new-fleet.md`

- Handoff §5 item 6: the `minds-autostart` installer is NOT in `bare_metal_prep.py` (a comment
  only); it is the DWT `pool_host` template's `post_host_create_outer_command__extend`.
- Plan "Gen-1 data disk format ... assumed raw": `limactl disk create` defaults to qcow2
  (`mngr_lima/limactl.py`), and the disk carries a partition table (`guest.py` grow script,
  PR #574). Moot under D7, but preflight records the format anyway.
- Plan's restore steps `qemu-img convert`, `qemu-img resize`, offline relabel: replaced by D7.
- Plan's "preflight refuses ... not running": replaced by D2.
- Plan's "reserve at the row's previous two ports ... lowest free ordinal": kept, but the
  reserve must also take the user-data (D6) and the pre-built data disk (D7) instead of
  fetching a meta tar / downloading a data disk.
- `server setup` does re-record the box host key (plan open question) but only runs from
  `delivered`/`installing`; repave flips a `draining` box to `delivered` first.
- Gen-2 disk budget overstated the XFS partition by ~101 GiB minus the reserve (D8).
- `stopped` production rows exist (17 on 2026-08-19) and their keys live only in the gen-1
  boot-disk artifact (D2).

- Module placement: the stage drivers live in `cli/cutover_drivers.py`, not `slices/cutover.py`,
  because the `minds_admin` layers contract puts `cli` above `bake` and `slices` and the drivers
  reuse `cli/server.py` (`allocate_slices`, `setup_server_to_ready`, `run_root_script_over_ssh`)
  and `cli/_tier_secrets.py`; the pure renderers/records stay in `slices/cutover_*.py`.
- The gen-2 restore's autostart installer is read from the tag's default-workspace-template
  checkout (`resolved_bake_source(from_tag=...)`), so the restore clones each distinct version once.

## 13. Deferred / out of scope here

The phase-6 recognizer for a client-reinstalled reconciler on gen-2; `lint-imports` not run
in CI; the connector/plugin duplicate 2 GiB df-guard margins; a generation-neutral home for
`assert_valid_bash`; CI boxes (phase 5); the staging soak realism question; the production
distinct-version count (the first production preflight answers it).
