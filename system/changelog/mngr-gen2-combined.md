Integration branch for the imbue_cloud slice-fleet generation 2 program (mngr-internal `mngr/gen2-combined`: the gen-2 stack plus `main` plus mngr-internal #857, #855 and #856). The template-side changes it carries, each detailed in its own entry here:

- Remote (imbue_cloud) workspaces run their container under gVisor (`runsc`) from the bake, with a "Sandboxed runtime" section in `AGENTS.md` and the `.mngr/settings.toml` comment/rename fixes (`new-fleet-runsc-prototype.md`).

- host_backup keeps no btrfs snapshot between backup ticks and drops the `max_local_snapshots` setting (`new-fleet-phase-2.md`).

- The desktop Lima guest image pins point at imbue's artifact mirror instead of `cloud.debian.org` (`mngr-mirror-upstream-artifacts.md`, the companion of mngr-internal #856).

- The in-container owner-exec daemon pin (`system/scripts/install_owner_exec.sh`) moves from v0.2.1 to v0.2.2, which never authorizes an `authorized_keys` line carrying options (`command=`, `restrict`, `from=`, `cert-authority`); v0.2.1 stripped the options and granted such keys unrestricted exec. The monorepo's VM-install pin moves in lockstep (mngr-internal #870).
