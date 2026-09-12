Integration branch for the imbue_cloud slice-fleet generation 2 program (raw qemu slices with routed-tap networking on Debian 13 boxes, replacing lima), merged together with `main` and the three pre-cutover fix PRs: imbue-ai/mngr-internal#857 (SSH certificates from Vault, #850), #855 (DHCP placement, #849) and #856 (the upstream artifact mirror, #851). The per-change details live in this directory's constituent entries, listed below.

Box telemetry and alerting for the gen-2 fleet (per-slice network signals, prep-artifact integrity, the management-login signals) and the mirror-sourced collector install.

Constituent entries: `new-fleet-base.md`, `new-fleet-runsc-prototype.md`, `new-fleet-phase-4-impl.md`, `mngr-variable-sizing.md`, `mngr-slice-fleet-gen2-phase-4.md`, `mngr-new-fleet-testing.md`, `mngr-lima-rename.md`, `mngr-mirror-upstream-artifacts.md`
