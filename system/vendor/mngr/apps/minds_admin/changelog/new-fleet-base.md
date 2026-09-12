`new-fleet-base` is the integration base for the in-progress slice-fleet generation-2 program (specs/slice-fleet-gen2 and specs/slice-fleet): the squash of the formerly stacked PRs #571, #573, #574, #581, #609, and #614. It is deployed only to dev canaries and must not be deployed or merged as-is; follow-up PRs stack on it and the whole program lands on `main` as one change.

For this project it carries:

- Gen-2 box lifecycle: `server register` / `server order` gain `--box-generation` and `--uplink-mbps` plus the units-valid disk guard; `server setup` reinstalls gen-2 boxes as `debian13_64` with the md-mirrored ext4 root + fill-remaining XFS storage layout; `server prep` / `setup` dispatch on the box's generation, and the gen-2 prep installs the raw-qemu stack, the 512 pre-created per-slice users, the plugin-rendered template unit / root helper / scoped sudoers (content-converged), the staged trixie guest image with the os-release-derived docker pin, the swapfile and transfer tooling, the management WireGuard bring-up, the `:22` lockdown (when the tier names a Modal Proxy), and the box telemetry collector with its prep-artifact hash manifest. Root scripts are shipped to the box by scp (the 512-grant sudoers overflowed the ssh argv).

- `server drain` (the fleet-turnover primitive), `server ssh` (interactive management SSH over the resolved dial), the `wireguard` command group (`config`, `sync-peers`, `install-onetun` pinning onetun 0.3.10 by sha256), and the shared box-management dial resolver in `slices/box_access.py` (userspace onetun tunnel, then kernel-route overlay, then the public address) threaded through every operator-side box SSH.

- Variable machine sizing: `pool create --units N`, units-based carve sizing stamping `memory_units` / `disk_gb` on inserted rows, the `UNITS_VALID` pricing column, and unit-based capacity in `server list`.

The detailed per-phase history is in this directory's `mngr-design-network-observation`, `mngr-slice-fleet-gen2-phase-*`, `mngr-variable-sizing`, `mngr-new-fleet-testing`, `mngr-finish-new-fleet-canary-testing`, `mngr-slice-fleet-canary-followups`, and `mngr-onetun-install-and-canary` entries.
