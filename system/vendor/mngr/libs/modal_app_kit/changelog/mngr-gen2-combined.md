Integration branch for the imbue_cloud slice-fleet generation 2 program (raw qemu slices with routed-tap networking on Debian 13 boxes, replacing lima), merged together with `main` and the three pre-cutover fix PRs: imbue-ai/mngr-internal#857 (SSH certificates from Vault, #850), #855 (DHCP placement, #849) and #856 (the upstream artifact mirror, #851). The per-change details live in this directory's constituent entries, listed below.

Deploy conventions for the gen-2 connector: the Modal Proxy egress threading and the mounted `gen2_scripts` subpackage.

Constituent entries: `new-fleet-base.md`, `new-fleet-phase-3.md`, `mngr-variable-sizing.md`, `mngr-slice-fleet-gen2-phase-3.md`, `mngr-new-fleet-testing.md`, `mngr-finish-new-fleet-canary-testing.md`

Gen-2 small follow-ups: docstring-only -- `read_modal_proxy` describes the proxy name as coming from the `[management_plane]` table of the tier's `deploy.toml` (the separate `management_plane.toml` was merged into it).
