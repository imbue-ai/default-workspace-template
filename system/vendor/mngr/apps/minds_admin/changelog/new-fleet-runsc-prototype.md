Phase 1 of the slice-fleet cutover plan (`blueprint/slice-fleet-cutover/`): hardened, runsc-native gen-2 slices. Stacked on `new-fleet-base`; dev canary only.

- The gen-2 `server prep` bakes the pinned gVisor release into the trixie guest image (same download + sha512 verification as the VPS host setup) and registers `runsc` with `--overlay2=none` directly in the image's `/etc/docker/daemon.json`; it also pins `kvm_intel`/`kvm_amd nested=0` in `/etc/modprobe.d/` (applies at the box's next reboot). Re-staging rule unchanged: delete the box's `debian-13-base.qcow2` and re-run prep. Re-prep also converges the hardened unit and helper.

- Gen-2 bakes create the workspace container under runsc with `/run` and `/tmp` on tmpfs (per-box `-S providers.imbue_cloud_slice.docker_runtime` / `default_start_args` overrides); gen-1 bakes get neither. `pool create --docker-runtime {runc,runsc}` overrides the runtime on a gen-2 box (the plain-runc comparison bake for the checkpoint) and is refused for gen-1 boxes. The dry-run report shows the resolved runtime.

- The box telemetry collector scans `systemd`'s journal lines for OOM kills of `mngr-slice@*` units and emits the new `SLICE_UNIT_OOM_KILLED` signal.

- New operator script `scripts/slice_runtime_benchmark.py`: runs the runc-vs-runsc workload set (cold `uv sync`, `npm ci`, a git clone, Fortress launch, Claude Code startup, container SSH connect latency, earlyoom under pressure, and the prototype micro-benchmarks) against leased slices over VM-root SSH and writes the markdown report under `blueprint/slice-fleet-cutover/`. Deleted with the cutover tooling in phase 6.

- The benchmark harness runs each timed command in a brace group so its `/dev/null` stdin covers a whole pipeline (a piped workload previously recorded a bogus instant success).

- `pool create` inserts each slice's `pool_hosts` row (status `baking`) before carving its VM and flips it to `available` when the bake finishes (deleted on failure), so the post-bake orphan reap sees in-flight slices as tracked; the reap additionally spares any rowless VM that is running or younger than 2 h and never deletes the data disk of a running or spared VM (previously a concurrent bake's data disk could be unlinked under its live VM). New `pool reap-orphans --server-id <id> [--dry-run]` runs the same reap on demand and reports reaped/spared resources; `pool destroy` claims a `baking` row only once it is older than 2 h.

- The gen-2 guest image's docker daemon.json sets `data-root=/mnt/mngr-data/docker`, json-file log rotation and a 1 GB build-cache GC bound, containerd's root moves to `/mnt/mngr-data/containerd`, both engine units get a `RequiresMountsFor=/mnt/mngr-data` drop-in (and no boot enablement; first boot starts them after the mount), and journald is capped at 512 MiB (`server prep` re-stages it).
