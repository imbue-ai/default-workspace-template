Integration branch for the imbue_cloud slice-fleet generation 2 program (mngr-internal `mngr/gen2-combined`: the gen-2 stack plus `main` plus mngr-internal #857, #855 and #856). The template-side changes it carries, each detailed in its own entry here:

- Remote (imbue_cloud) workspaces run their container under gVisor (`runsc`) from the bake, with a "Sandboxed runtime" section in `AGENTS.md` and the `.mngr/settings.toml` comment/rename fixes (`new-fleet-runsc-prototype.md`).

- host_backup keeps no btrfs snapshot between backup ticks and drops the `max_local_snapshots` setting (`new-fleet-phase-2.md`).

- The desktop Lima guest image pins point at imbue's artifact mirror instead of `cloud.debian.org` (`mngr-mirror-upstream-artifacts.md`, the companion of mngr-internal #856).
