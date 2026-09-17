Integration branch for the imbue_cloud slice-fleet generation 2 program (raw qemu slices with routed-tap networking on Debian 13 boxes, replacing lima), merged together with `main` and the three pre-cutover fix PRs: imbue-ai/mngr-internal#857 (SSH certificates from Vault, #850), #855 (DHCP placement, #849) and #856 (the upstream artifact mirror, #851). The per-change details live in this directory's constituent entries, listed below.

The collection loop hops into gen-2 workspaces with the connector-refreshed `analytics` SSH certificate (pool key on gen-1 only), and the box dashboards gain the gen-2 per-slice throughput and box-metrics sources.

Constituent entries: `new-fleet-base.md`, `mngr-new-fleet-testing.md`, `mngr-variable-sizing.md`, `mngr-slice-fleet-gen2-phase-4.md`, `mngr-ssh-authority-in-vault.md`
