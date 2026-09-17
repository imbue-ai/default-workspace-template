# runsc tuning pass (2026-08-27): volume metadata cache + more vCPUs

Follow-up to `checkpoint-2026-08-27.md` / `benchmark-2026-08-27.md` (the baseline). Generated reports for
this pass, never edited by hand: `benchmark-2026-08-27-exclusive-16vcpu.md` (full run, runc then runsc,
serially), `benchmark-2026-08-27-exclusive-16vcpu-find.md` (the `find_workspace` row re-measured after
the harness fix below), `benchmark-2026-08-27-16vcpu-shared-runsc.md` (attribution run: runsc at 16
vCPUs with the baseline file-access mode).

## Was the baseline runsc slice swapping or otherwise handicapped?

Not swapping. Box-side, both comparison VMs' cgroups (`mngr-slice@1` runc, `@2` runsc) sat exactly at
`memory.max` -- page cache filling to the cap, by design -- with symmetric history: ~300-330 MB swap
peaks during the *bake* (the finding already in the checkpoint), `MemorySwapMax=0` since, and only ~16k
major faults each over the following 9 h (~65 MB faulted back in total). The guests have no swap device
(`pswpin/pswpout` 0). Box steal time 0, box CPU pressure 0.

Two real handicaps were found instead:

1. **No metadata cache on `/home/user`.** The workspace volume was mounted `cache=remote_revalidating`
   -- gVisor's default `--file-access-mounts=shared` for non-root mounts -- so every path lookup
   revalidated against the host, while the rootfs got `cache=fscache`. Direct micro-tests: `stat()` on
   the volume 10.3 us vs 2.6 us on the rootfs (runc: ~2 us either way). The per-mount dentry cache was
   also the default 1000 entries against a 32k-file workspace.
2. **2 vCPUs.** systrap hands every syscall from the app thread to a sentry thread; on 2 vCPUs they
   compete for the same cores together with the sentry's ~30 threads. The sentry also burns ~24% of a
   core at idle: the poll/timer-heavy daemons cost ~3x under gVisor (`mngr observe` 16% vs 5% CPU,
   `system_interface` 9.6% vs 3.3%).

The baseline's headline `find` 35x was also partly a measurement artifact: re-run steady-state under the
baseline config the same `find` was 53 ms vs ~385 ms (7x); the 2.24 s median was taken minutes after
the containers came up, while the workspace stack was still settling on 2 vCPUs.

## What changed for this pass

- runsc runtime args on the runsc slice: `--overlay2=none --file-access-mounts=exclusive --dcache=200000`
  (baseline: `--overlay2=none`). `/mngr-vol` now mounts `cache=fscache, dcache=200000-global`.
- Both slice VMs: `MNGR_SLICE_VCPUS=16` (all 16 box threads; baseline 2). No `cpu.max`, `CPUWeight`
  unchanged, so this only removes the vCPU bound. Containers see `nproc` = 16.
- Both stacks were up and idle before their turn; runc ran first, then runsc, never overlapping.

The runsc slice is a **fresh bake** (see "Incident" below): row `3eaeefcf-1cbf-4b68-bcba-b419d5b042f9`,
mngr host `host-5a146d5fc65d4923a62fff5a4d605762`, ordinal 2, VM port 22004, container port 22005,
leased by the same test account. The runc slice is unchanged apart from the VM restart (its guest picked
up a pending kernel update, 6.12.96 -> 6.12.105). Both containers now report a 6.75 GiB cap (the
baseline printed 7.00 GiB; same `HostConfig.Memory` on both, so the comparison is unaffected).

## Results (medians; "before" = baseline report, "after" = this pass)

| workload | runc before | runc after | runsc before | runsc after | runsc/runc before | runsc/runc after | runsc after/before |
|---|---|---|---|---|---|---|---|
| `python_startup_x20` | 0.22 s | 0.18 s | 0.69 s | 0.30 s | 3.16x | 1.64x | 0.43x |
| `node_startup_x20` | 0.54 s | 0.40 s | 1.58 s | 1.24 s | 2.94x | 3.06x | 0.78x |
| `find_workspace` | 63.6 ms | 63.6 ms | 2.24 s | 0.26 s | 35.16x | 4.10x | 0.12x |
| `tar_workspace` | 0.11 s | 0.11 s | 0.71 s | 0.25 s | 6.26x | 2.22x | 0.35x |
| `git_status` | 41.8 ms | 37.9 ms | 0.28 s | 98.3 ms | 6.58x | 2.59x | 0.35x |
| `compileall_mngr` | 0.82 s | 0.84 s | 1.25 s | 1.84 s | 1.52x | 2.19x | 1.47x |
| `syscall_loop_dd` | 80.6 ms | 82.8 ms | 1.16 s | 0.95 s | 14.45x | 11.42x | 0.82x |
| `dd_write_512m_fsync` | 0.64 s | 0.65 s | 0.69 s | 0.84 s | 1.07x | 1.28x | 1.22x |
| `git_clone_flask` | 1.18 s | 0.97 s | 2.28 s | 1.72 s | 1.93x | 1.78x | 0.75x |
| `uv_sync_cold` | 2.71 s | 1.88 s | 11.40 s | 4.54 s | 4.20x | 2.42x | 0.40x |
| `npm_ci_system_interface` | 2.57 s | 2.13 s | 4.82 s | 3.79 s | 1.87x | 1.78x | 0.79x |
| `fortress_first_page` | 1.04 s | 0.91 s | 2.53 s | 2.18 s | 2.44x | 2.40x | 0.86x |
| `claude_version` | 82.4 ms | 81.5 ms | 0.11 s | 0.21 s | 1.36x | 2.63x | 1.91x |
| `claude_prompt_round_trip` | 30.05 s | 32.60 s | 11.26 s | 13.64 s | 0.37x | 0.42x | 1.21x |
| `container_ssh_connect_latency` | 0.1 ms | 0.1 ms | 0.4 ms | 0.4 ms | 2.40x | 2.57x | 1.00x |
| `earlyoom_pressure` | 1.38 s | 1.66 s | 3.41 s | 3.77 s | 2.47x | 2.27x | 1.11x |

`claude_prompt_round_trip` is one LiteLLM round trip and says nothing about the runtime; `earlyoom_pressure`
is time-to-shed of an allocator (not a speed metric); sshd p95 stayed 4.6 ms (runc) vs 17.6 ms (runsc).
Footprint after the run: runc container 806 MiB, runsc 1.25 GiB (the 200k-dentry cache is in the sentry).

### Attribution (runsc only, all at 16 vCPUs unless noted)

| workload | 2 vCPU + shared (baseline) | 16 vCPU + shared | 16 vCPU + exclusive |
|---|---|---|---|
| `python_startup_x20` | 0.69 s | 0.37 s | 0.30 s |
| `node_startup_x20` | 1.58 s | 0.89 s | 1.24 s |
| `find_workspace` | 2.24 s | 0.78 s | 0.26 s |
| `git_status` | 0.28 s | 0.32 s | 98 ms |
| `compileall_mngr` | 1.25 s | 1.94 s | 1.84 s |
| `syscall_loop_dd` | 1.16 s | 0.93 s | 0.95 s |
| `dd_write_512m_fsync` | 0.69 s | 0.75 s | 0.84 s |
| `uv_sync_cold` | 11.40 s | 7.48 s | 4.54 s |
| `claude_version` | 0.11 s | 0.13 s | 0.21 s |

Reading:

- `exclusive` + big dcache is the win for anything metadata-heavy: `find` 3x, `git status` 3.3x,
  cold `uv sync` 1.6x on top of the vCPU gain. It costs on large-file streaming: `node`/`claude` startup
  (reading ~100 MB binaries) 1.4-1.6x slower and bulk write 1.1x, because reads and writes now go
  through the sentry's page cache instead of straight to the host fd.
- More vCPUs help the interpreter startups, `find`, `uv sync`, `git clone`, `npm ci` (both runtimes gain on
  the real workloads: runc `uv sync` 2.7 -> 1.9 s too), but `compileall` got ~1.5x slower on runsc at 16
  vCPUs in both cache modes -- the sentry's scheduling cost grows with CPUs; a middle value (4-8) is
  worth measuring before choosing a fleet default. 16 vCPUs per slice is not a fleet option anyway
  (14 slices on 16 threads); it was used here only to prove the bound was not artificial.
- Neither change touches the syscall floor: the `dd` loop stays ~11x and fork+exec ~9x (0.44 vs 4.2 ms per
  `/bin/true`, direct measurement). Only the KVM platform (nested virt, which phase 1 deliberately
  disabled) or fewer syscalls can move those.

## Caveat before making `exclusive` the default

`--file-access-mounts=exclusive` lets the sentry cache dirty data for the volume, so anything reading
the volume *from the VM side while the container runs* can see stale bytes until the sentry flushes:
concretely the live btrfs snapshot path (`libs/mngr_vps/imbue/mngr_vps/resources/snapshot_helper.sh`
runs `btrfs subvolume snapshot -r` on the host subvolume; it does not `sync` the container first, and a
guest-kernel `sync` cannot see sentry-held pages). A container stop flushes everything, and a
`docker exec <cid> sync` before a live snapshot would cover the rest (gVisor's `sync(2)` writes all
cached data back). mngr itself never touches the volume's host path while a container runs (only
`docker volume rm` in `wipe.py`). Also note `--dcache` is global and bounds the sentry's open host FDs:
200k is well inside the sandbox's 524k nofile limit.

## Incident: the runsc slice's data disk had already been destroyed at bake time

Restarting `mngr-slice@2` for the vCPU change failed: its `datadisk.qcow2` did not exist. The runc bake's
post-bake orphan reap (`_reap_orphan_slice_resources` in `apps/minds_admin/.../cli/server.py`) had
deleted it at 04:14 (`bake-runc.log`: "Orphan reap: deleting 1 untracked slice disk(s) ...
mngr-slice-dev-josh-2-e2fed6b5b8644f70-data") because the concurrent runsc bake's retry had carved the
instance but not yet inserted its `pool_hosts` row. qemu held the unlinked inode, so the VM ran on a
deleted disk for 9 h and the baseline numbers are valid; the data was released on restart. The row
`67f3ea58-…` was destroyed (`pool destroy --force`) and a fresh runsc slice baked. The reaper's docstring
already states the "no concurrent bake of this env" assumption, but the failure mode is silent data loss
for a soon-to-be-leased workspace; an age threshold (skip resources younger than a bake) or a check
against running units would make it safe. Not changed on this branch.

## Harness fix in this pass

An autofix pass had appended `</dev/null >/dev/null 2>&1` to the timed command string, which for the piped
`find ... | wc -l` workload bound the redirects to `wc` alone: `wc` read `/dev/null` and exited, `find`
died of SIGPIPE, and the run recorded a bogus ~1 ms success. `_timed` now runs the command in a brace
group (regression test added); the `find_workspace` row above comes from the re-run after the fix. The
baseline report predates the bug.

## Decision (2026-08-27, after hands-on use of a 4-vCPU slice)

- Container runtime **runsc** with the default args (`--overlay2=none` only): the `exclusive`
  volume cache is rejected for its live-snapshot consistency cost; it stays documented above as
  a known lever.
- **4 vCPUs per 8-unit machine**: `DEFAULT_SLICE_CPU_OVERCOMMIT_RATIO` 2.0 -> 4.0 (the canary's
  row was updated by hand; the cutover sets it on every gen-2 box before re-carving, see the
  plan's phase 4). The `compileall` regression seen at 16 vCPUs was not re-measured at 4; the
  hands-on feel at 4 was judged good enough.
- **`MemoryMax` shape (decided later the same day)**: squeeze the guest instead of adding headroom.
  The guest boots with `units x 1024 - 512` MiB, `MemoryMax` is exactly the budget share
  `units x 1024 + 512`, `MemorySwapMax=0`, and the qcow2 drives run `cache=none,aio=native` so the
  cap bounds anon memory rather than host page cache; 14 caps sum to 119 GiB on the 125 GiB box.
- **Orphan reaper (decided later the same day)**: row-first bakes (`baking` status) plus running/age
  guards in the reap; see the slice-fleet spec.
- **Disk layout (decided later the same day)**: gen-2 boot disk 10 GiB (OS + capped journal); data disk
  16 GiB + 3.5 GiB/unit (44 GiB for 8 units) holding the container engines' roots, the home subvolume and the
  backup snapshots; one btrfs qgroup over home + containerd's overlayfs snapshots limited to the disk minus a 4 GiB
  system reserve (re-derived by the grow oneshot, so a resize grows it); `max_local_snapshots` 5 -> 1.
- Still open: the idle sentry cost (issue #700).

## Live state left on the canary

- The two comparison slices, the 4-vCPU test slice and the old ordinal-0 row were all destroyed on
  2026-08-27 (`pool destroy`). Its `bare_metal_servers.cpu_overcommit_ratio` is 4.0, its pool DB has
  the baking-rows migration (now `038_pool_hosts_baking_rows.sql`, recorded under its pre-renumber
  filename `037_pool_hosts_baking_rows.sql`, so the next deploy re-applies the idempotent
  `DROP NOT NULL`), and its staged guest image is the final customization (containerd/docker roots on
  the data disk, no boot enablement, journald cap).
- One available slice baked with the final layout (row `16928706-7c15-45e8-ae0b-32de4712d136`,
  `host-caee422d020148c4ad03f873bc83206f`, ordinal 0, ports 22000/22001): 10 GiB boot / 44 GiB data,
  simple quota 40 GiB with ~8 GiB used by the image, guest 7.5 GiB, container cap 6.5 GiB, 4 vCPUs,
  `MemoryMax` 8.5 GiB with zero host page cache. Bake time 308 s (layers now go to the data disk;
  ~10 min before).

## Follow-ups noticed while validating

- `server prep` stages the guest image only when the qcow2 is absent (as documented); a content hash
  of the customization would let prep converge on image changes without the manual delete.
- The bake's first attempt on a freshly prepped box failed twice for setup reasons that are now fixed
  (docker started before the data disk existed; the carve check rejecting the guest holdback); the
  row-first path and the VM rollback were exercised for real each time and left nothing behind.
