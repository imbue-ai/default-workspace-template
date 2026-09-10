Gen-2 boxes serve their slices' addresses by DHCP (imbue-ai/mngr-internal#849).

- `minds-admin server prep` / `setup` install `dnsmasq-base` and three plugin-rendered DHCP prep artifacts on every gen-2 box: `/etc/mngr/slice-dhcp.conf` (DHCP only, bound to the `mslice*` taps, one single-address range per ordinal), `mngr-slice-dhcp.service` (dnsmasq in the foreground on exactly that config, as the unprivileged `mngr-dhcp` user) and the udp/67 policy `/etc/nftables.d/mngr-slice-dhcp.nft` (loaded with `nft -f` before the server is started). All three are content-converged like the unit, helper and sudoers; the config is syntax-checked before it can replace the live one, and the server is restarted only when its config or unit changed. All three join the box telemetry collector's prep-artifact integrity manifest, so tampering with the address a guest is handed, or with who may reach the server, raises `PREP_ARTIFACT_DRIFT`.

- The prep also creates the `mngr-dhcp` system user the DHCP unit runs as (owner of the lease directory). The `nftables.service` boot-persistence plumbing (the `/etc/nftables.d` include, the neutralized distro `flush ruleset`, the enabled service) is a section shared by the `:22` lockdown and the DHCP policy, so every gen-2 prep runs it, lockdown or not.

- `minds-admin cutover migrate` reserves the transplanted VM with the same placement-free cidata as a fresh carve (stable instance-id, DHCP network-config, the harvested VM trust material in the user-data) instead of a placement-keyed instance-id.

- Re-prep every gen-2 box from this version before any workspace restores onto it: a slice carved or restored by this code gets no address until the box runs the DHCP server. Running slices are unaffected by the re-prep. Gen-2 slices carved before this change carry a static netplan for their original ordinal and cannot restore onto a different ordinal or box; only dev-tier VMs predate it (re-bake them, see `apps/minds/docs/deploy/next_deploy.md`).

- The `prep_box` docstring now gives the pre-created per-slice user count as 512 (`GEN2_MAX_SLICE_COUNT`); it said 64.
