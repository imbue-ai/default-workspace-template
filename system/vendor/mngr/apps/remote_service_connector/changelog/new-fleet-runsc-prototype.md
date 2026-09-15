The connector's drift-pinned mirror of `SLICE_BOOT_DISK_GIB` follows the plugin: 32 -> 20 GiB (the restore-reserve budget guard, the eviction planner, and the restore df guard's boot-disk fallback all size from it).

- Migration 038 lets `pool_hosts.agent_id` be NULL: a slice's row now exists as `baking` (no agent yet) from before its carve until its bake finishes.

- The drift-pinned gen-2 mirror renders `MNGR_SLICE_MEMORY_MIB` as the guest's boot RAM (`units x 1024 - GUEST_RAM_HOLDBACK_MIB`) like the plugin.

- The drift-pinned gen-2 mirror follows the new disk sizing (`GEN2_BOOT_DISK_GIB` 10, data disk 16 + 3.5 GiB/unit, default 44 GiB) and the quota-aware grow oneshot; `DEFAULT_MACHINE_DATA_DISK_GB` is 44.
