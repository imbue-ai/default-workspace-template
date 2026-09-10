`specs/slice-fleet-gen2/spec.md`: the "Hardening that lima blocked" paragraph records the phase-1 hardening (systemd sandbox, qemu argv, cgroup limits, gVisor-native containers), and the qemu-invocation and template-unit bullets of the implementation section follow the hardened shape; `specs/slice-fleet/spec.md`'s box accounting follows the 20 GiB boot disk. `blueprint/slice-fleet-cutover/` gains the runc-vs-runsc benchmark report for the phase-1 checkpoint.

- `blueprint/slice-fleet-cutover/benchmark-summary-2026-08-27-tuning.md` (plus its three generated reports): the runsc tuning pass -- volume metadata cache (`--file-access-mounts=exclusive --dcache=200000`) and vCPU-bound attribution, the bake-time orphan-reap data-disk incident, and the harness fix.

- The slice-fleet specs and the cutover plan record the 2026-08-27 decisions: runsc with default args (no volume cache), 4 vCPUs per 8 units, and a phase-4 step that sets `cpu_overcommit_ratio` on every gen-2 box row before re-carving.

- The slice-fleet specs, the cutover plan and the tuning summary record the memory decision (guest holdback, exact-budget `MemoryMax`, `cache=none` drives) and the row-first bake / guarded orphan reap.

- The slice-fleet specs and the tuning summary record the 2026-08-27 disk-layout decision (docker data-root on the data disk, one workspace qgroup, 10 GiB boot disk).
