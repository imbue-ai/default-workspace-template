Slice-fleet-gen2 phase 2: the connector now speaks both slice-fleet generations (specs/slice-fleet-gen2).

- New `box_scripts_gen2.py`: gen-2 (raw-qemu/systemd) variants of the workspace stop/start box scripts -- upload/download/restore-reserve/stop/restart/finalize targeting `/srv/mngr-slices/instances/<name>/`, with the unit `disable` as the boot-autostart stop marker and the unit enabled only after a restore's disks land. Mirrored gen-2 constants/renderers are pinned byte-for-byte against `mngr_imbue_cloud.slices.qemu_slice` by a new drift-ratchet test.

- The transition supervisor (`stop_start.py`) dispatches every box-side operation on the row's stamped `box_generation`, and restore candidate selection is generation-aware: a gen-2 artifact never restores onto a gen-1 box, while a gen-1 artifact restoring onto a gen-2 box goes through the new conversion path -- rebuilt cidata with a placement-keyed instance-id (a controlled one-time cloud-init replay applies the new static /30), followed by the strict in-VM bookworm -> trixie dist-upgrade (detached, status-polled over SSH to the VM, supervisor-issued reboot, trixie + both-sshd-banners verification). A failed upgrade lands the row back on `stopped` with `attributes.guest_upgrade_failed` set and no bookworm-on-gen-2 fallback.

- Gen-2 restores regenerate `meta-data`/`network-config` per placement and reuse the artifact's own replay-safe `user-data`; a successful restore restamps the row's `box_generation` (column and attributes copy).

- A `draining` origin box no longer restarts a stopped workspace in place: the restore is forced onto a surviving box and the abandoned local VM is reaped; draining boxes are also excluded from restore candidates (the `ready` filter).

- Release teardown and the reconcile sweep dispatch on the row's/box's generation (gen-2 teardown via the idempotent systemd destroy script; reconcile lists gen-2 instance dirs).

- The lease and workspace responses gain an additive `box_generation` field (from the row's column), and the pre-tolerant strict client compat snapshot (`wire_models_minds_0_3_16.py`) is pruned early: effectively the whole client fleet runs 0.4.1+, whose tolerant wire models accept additive response fields.
