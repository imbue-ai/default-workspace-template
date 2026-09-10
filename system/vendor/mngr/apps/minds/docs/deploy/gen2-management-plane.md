# Gen-2 management plane: box lockdown and operator WireGuard

Phase 3 of [specs/slice-fleet-gen2](../../../../specs/slice-fleet-gen2/spec.md). Gen-2 boxes stop answering management SSH (`:22`) from the open internet: the only allowed paths are the connector's Modal Proxy static egress IPs and the tier's operator WireGuard overlay. This doc is the operator runbook for that machinery.

## The pieces

| Piece | Where |
|---|---|
| Per-tier config (operator WireGuard public keys, Modal Proxy name + static IPs) | the `[management_plane]` table of `apps/minds/imbue/minds/config/envs/<tier>/deploy.toml` (committed; all values public) |
| Box WireGuard bring-up + `:22` lockdown | rendered into the gen-2 prep by `minds-admin server prep` / `setup` (`slices/management_plane.py`) |
| Box overlay address (`wireguard_address`) + public key (`wireguard_public_key`) | `bare_metal_servers` row, assigned/recorded at prep |
| Operator client config | `minds-admin wireguard config` |
| Fleet peer sync | `minds-admin wireguard sync-peers` |
| Connector egress via the proxy | `proxy=` on every connector function in `app.py`, threaded from `deploy.toml`'s `[management_plane.modal_proxy]` at `minds-admin env deploy` |

## Addressing plan

All tiers' overlays are disjoint carves of one reserved supernet, `10.64.0.0/10` (`MANAGEMENT_OVERLAY_CIDR_BY_TIER` in the minds config is the authoritative table, pinned by tests):

| Tier | Overlay | Operators | Boxes from | Reserved growth |
|---|---|---|---|---|
| production | `10.64.0.0/11` | `10.64.0.0/24` | `10.64.1.1` | — |
| staging | `10.96.0.0/16` | `10.96.0.0/24` | `10.96.1.1` | `10.96.0.0/13` |
| ci | `10.104.0.0/16` | `10.104.0.0/24` | `10.104.1.1` | `10.104.0.0/13` |
| dev | `10.112.0.0/16` | `10.112.0.0/24` | `10.112.1.1` | `10.112.0.0/13` |

Each tier is still its own isolated WireGuard network (own keys, own peers); the disjoint numbering exists for the *operator machine*, where several tiers' tunnels can now coexist without ambiguity. The supernet deliberately avoids the Tailscale/CGNAT range (in use on operator machines), the box-local `10.201.0.0/16` per-slice range, and Docker's pools. Each small tier sits at the base of its reserved /13, so widening it 8x later is a one-value config change followed by a re-prep/peer sync — no box changes address. A box whose stamped address falls outside its tier's current allocation is renumbered automatically at its next prep (the prep's own overlay-riding session may drop when `wg0` moves to the new address — just re-run; prep is idempotent).

## Operator transport (how tooling reaches a locked-down box)

Every box-management command resolves its dial automatically, trying in order:

1. **Userspace WireGuard tunnel** — when the [`onetun`](https://github.com/aramperes/onetun) binary is available (install the pinned, hash-verified release with `minds-admin wireguard install-onetun`; also discovered on PATH or via `MNGR_ONETUN_PATH`) and your operator private key is at `~/.mindsadmin/<tier>/wireguard.key` (the per-tier operator identity directory; `MINDS_ADMIN_IDENTITY_DIR` relocates the root), the tooling forwards a local port to the box's overlay `:22` entirely in userspace: **no root, no network interface, no local peer list** (the peer is built from the box's DB row per dial), verified end to end by reading the box sshd's banner. This is the intended steady-state path for operators, CI, and automation alike; a new box needs nothing on your machine.
2. **Kernel-route overlay dial** — the box's overlay address directly, when an interface-level tunnel (`wg-quick`, see below) reaches it.
3. **The public address** — correct for every box without a live lockdown (including a first-ever prep).

The dial is only for management SSH; the per-slice ports (22000+) are not locked down and stay on the public address, and host-key pinning is by key, so the dial is transparent to trust. For *interactive* box sessions, `minds-admin server ssh --server-id <id>` (optionally `-- <command>` for a one-off) rides the same resolved dial, so it too needs no root and no tunnel bring-up. A `wg-quick` kernel tunnel remains only for a bare `ssh debian@<overlay-address>` outside the tooling; the rendered operator config's `Address` is a `/32`, so only the per-box `/32` routes land on your machine and the tunnel coexists with anything else it runs.

## Management SSH identity (certificates, not keys)

A gen-2 box, every slice VM on it, and every workspace container inside those VMs authorize **no static management key**. Each layer's sshd trusts the tier's SSH certificate authority -- a Vault SSH secrets-engine mount per tier (`minds-dev-ssh`, `minds-ci-ssh`, `minds-staging-ssh`, `minds-production-ssh`; terraform in imbue-ai/vault) -- and maps certificate *principals* to accounts through an `AuthorizedPrincipalsFile`: `mngr-operator` (the box's `debian` bootstrap user, which has full sudo; root on the box has no principals file and accepts no certificate), `mngr-service` (the box's slice service user), `mngr-vm` (root in a VM), `mngr-container` (root in a workspace container). What authenticates is a short-lived certificate (imbue-ai/mngr-internal#850):

- **Operators**: any `minds-admin` command that dials a gen-2 box signs the key at `~/.mindsadmin/<tier>/ssh_id` through your own `vault login` (12h certificates, re-signed when under 6h remain; every principal) and writes the certificate beside it. OpenSSH, paramiko, pyinfra, scp, and rsync all pick the sibling `-cert.pub` up on their own, so `ssh -i ~/.mindsadmin/<tier>/ssh_id ...` works raw as well. Who may sign is the Vault role's allowlist: removing a person revokes them within 12h, and no host carries anything of theirs to clean up.
- **The connector**: one cron (`ssh_cert_refresh`, every two hours) is the only connector function with a Vault credential -- the tier's AppRole, whose only capability is signing the `connector` and `analytics` roles. It mints a fresh ed25519 key plus an 8h certificate per role and stores them in a Modal Dict; every other connector function reads its bundle from the Dict, so no request path touches Vault and a Vault outage is tolerated for the certificate's remaining life. `minds-admin env deploy` runs the function once after every connector deploy, so a fresh env is never waiting on the cron. The connector's certificate carries no `permit-pty` or forwarding extensions.
- **The analytics collector** reads its own Dict entry (VM + container principals only).

The per-tier operator identity directory `~/.mindsadmin/<tier>/` (relocate the root with `MINDS_ADMIN_IDENTITY_DIR`) holds both halves of an operator's tier identity: `wireguard.key` (the tunnel transport above) and `ssh_id` / `ssh_id-cert.pub`. Nothing in it is worth stealing for long.

The box's sshd logs every certificate login as `Accepted publickey ... ID operator:<who> (serial N) CA ...`; the box telemetry collector ([gen2-telemetry.md](gen2-telemetry.md); `journalctl -t mngr-box-telemetry` on the box, or the tier's `box_logs` stream) emits each as a `management_login` event and raises a `MANAGEMENT_SSH_ANOMALY` signal with reason `static_key_login` for any *static-key* login on a gen-2 box, which should never happen. `just server-audit` reports `authorized_key_count` (must be `0` on gen-2) and `is_trusted_ca_correct` (the pinned CA is exactly the tier's committed `[ssh_ca] public_key` in `deploy.toml`).

Gen-1 boxes keep authorizing the tier's static pool key until the cutover retires them; every tool dispatches on the box's recorded generation.

## Bringing a tier's management plane up

1. **Create the tier's Modal Proxy** in its Modal workspace (dashboard -> Proxies; the dev tier shares the single minds-dev proxy across all dev envs; Team plan allows one proxy per workspace with up to 5 static IPs). Note its name and static IPs.
2. **Commit the tier's `[management_plane]` table** in `envs/<tier>/deploy.toml`: proxy name + static IPs under `[management_plane.modal_proxy]`, and each operator's WireGuard public key + assigned address (inside the tier's operator `/24`, see the table above) as a `[[management_plane.wireguard.operators]]` block (the dev tier's `deploy.toml` is the worked example). Operators generate their own keypair locally (`mkdir -p -m 700 ~/.mindsadmin/<tier> && wg genkey | tee ~/.mindsadmin/<tier>/wireguard.key | wg pubkey`); only the public half is committed — keeping the private key at that path is what lights up the tooling's userspace-tunnel transport.
3. **Redeploy the connector** (`minds-admin env deploy ...`). The deploy threads the proxy name into `modal deploy`; every connector function (web app, transition supervisors, crons) then egresses from the proxy's static IPs. Verify with a lease/stop cycle against an existing box before locking anything down.
4. **Prep the gen-2 box(es)** (`minds-admin server prep --server-id ...`, or `setup` for a fresh box). Prep assigns the box's overlay address, generates its WireGuard keypair on-box (the private key never leaves it), brings up `wg0` with the committed operator peers, records the public key on the row, and — because `[management_plane.modal_proxy]` is configured — installs the `:22` lockdown. The prep's own SSH session survives (established connections are exempt); NEW laptop connections to the public address are dropped from that moment.
5. **Verify the transport**: with the key in place (and `onetun` installed via `minds-admin wireguard install-onetun`), every `minds-admin` box command now reaches the locked-down box automatically — no tunnel to bring up. Interactive sessions ride the same path via `minds-admin server ssh --server-id <id>`. If you want a raw `ssh debian@<wireguard_address>` instead, `minds-admin wireguard config --operator <name>` emits your wg-quick client config (one `[Peer]` per prepped gen-2 box, endpoint = its public address:51820, allowed IPs = its overlay /32); splice in your private key and `wg-quick up`.

Ordering matters: 3 before 4 (the connector must already egress from the allowlisted IPs when the first lockdown lands), and your operator entry must be committed before the prep that locks you out of the public address.

## Peer changes

Edit the committed operator list, then run `minds-admin wireguard sync-peers`. It re-renders each prepped gen-2 box's `wg0.conf` from the committed list and restarts the interface only where the config actually changed (an unchanged box never bounces live sessions). It resolves each box's dial automatically (the userspace tunnel, else a reachable overlay route, else the public address — see Operator transport above), so a post-lockdown sync just works. The box you are adding *yourself* to is the exception: your tunnel cannot reach it until its peers include you and its locked public `:22` drops you, so another allowed path (an existing operator, or the bounce host under Break-glass) must run that sync.

## Rollback

Remove (or comment out) the tier's `[management_plane.modal_proxy]` block and re-run `server prep` on each box: with no proxy IPs configured, prep converges the box back to open (removes the policy file and the live `mngr_mgmt` nftables table). WireGuard stays up either way — it is independent of the lockdown.

Mind where you run that re-prep from: `server prep` resolves its dial address the same way as every other box-management command (the overlay address when your WireGuard tunnel reaches the box, else the public address), so on a box whose lockdown is already live it works from your laptop whenever the userspace tunnel (or a kernel tunnel) reaches the box. With neither available, run it from a machine whose egress is allowlisted — the Modal-sandbox bounce host under Break-glass below. Before any lockdown has landed, the plain laptop path works as-is.

## Break-glass

When WireGuard is unusable and the connector path can't help:

- **OVH rescue console**: reboot the box into OVH's rescue environment from the OVH manager — it boots a fresh OS with its own SSH, outside the installed system's nftables entirely. Slices are down while in rescue.
- **Modal-sandbox bounce host**: start a Modal sandbox in the tier's workspace attached to the *same* Modal Proxy as the connector; its egress then comes from the allowlisted IPs, so it can SSH the box's `:22` like the connector does (with a certificate signed by the tier's CA -- the connector's Dict bundle or your operator identity; on a gen-1 box, the pool key). This needs no reboot and no downtime.

## Implementation notes (what the lockdown actually installs)

- Its own nftables table (`inet mngr_mgmt`), never the per-VM `mngr_slices` table the slice helper owns. The policy is scoped entirely to `tcp dport 22`: established/related first (so applying it never severs the applying session), then loopback, `wg0`, the proxy IPs, then a counting drop. The WireGuard port (51820/udp) and the public per-slice port range stay open via the chain's accept policy.
- Boot persistence rides `nftables.service` loading `/etc/nftables.d/mngr-management.nft`. Prep neutralizes the distro conf's `flush ruleset` line — a service restart running it would wipe the live per-VM slice rules out from under running VMs. That plumbing (the include line, the neutralized flush, `nftables.service` enabled) is shared with the slice DHCP server's udp/67 policy (`/etc/nftables.d/mngr-slice-dhcp.nft`, table `inet mngr_slice_dhcp`; see the "Networking on a gen-2 box" section of [host-pool-setup.md](./host-pool-setup.md)), so every gen-2 box has it, lockdown or not.
- The box's WireGuard private key is generated at prep and never rotated by re-runs; to rotate it, delete `/etc/wireguard/wg0.key` on the box and re-run prep (then re-run `wireguard config` for every operator, since the box's public key changed).
