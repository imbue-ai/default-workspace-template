Variable machine sizing (specs/slice-fleet, phase 1). `minds-admin pool create` gains `--units N` (dev/testing bakes of odd-sized gen-2 machines; production pools stay uniform at the 8-unit default): gen-2 bakes size the carve from units (proportional vCPUs, 3.5GiB/unit data disk) and stamp `memory_units`/`disk_gb` on the inserted pool row.

`server register` and `server order` gain the units-valid guard: a gen-2 box whose disk cannot hold its RAM's full complement of default-size machines is refused (no storage add-on ordering). `server order` gains `--box-generation` (default 2). The pricing table gains a `UNITS_VALID` column, and `server list` shows gen-2 boxes' capacity as used/total units and disk (gen-1 keeps the slot display).

This branch also carries the earlier slice-fleet gen-2 phase 1-4 work; see the `mngr-slice-fleet-gen2-phase-*` and `mngr-variable-sizing` entries in this same PR.

Dev-canary fix landed in this same PR: box prep/setup scripts are copied to the box as files (scp under the pinned host key) instead of riding the ssh command line, which the gen-2 prep's 512 per-ordinal sudoers grants had pushed past the kernel's per-argument size limit. The gen-2 `server list` disk usage also now includes each machine's 32GiB boot disk, matching the on-box reserve accounting.
