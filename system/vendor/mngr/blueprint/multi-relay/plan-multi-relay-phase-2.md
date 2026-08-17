# Plan: Multi-relay sharing, Phase 2 — bucket-sharded k=2 + computed DNS

Companion to [plan-multi-relay-phase-1.md](./plan-multi-relay-phase-1.md). Phase 2 is triggered per region, not built now; its purpose here is to prove Phase 1's seams scale to N > 2 relays and millions of workspaces with **zero changes to clients or workspaces** — every delta below is connector- or relay-side.

## Overview

- Phase 1's full replication stops scaling past N = 2: every relay holds every tunnel, so per-relay state grows with total shares, not shares/N. Phase 2 shards: each share tunnels to exactly k = 2 owner relays, so per-relay state is ~2M/N and capacity grows linearly with the fleet.
- Trigger (per region): sustained relay bandwidth above ~50% of the instance flavor's capacity, or per-relay tunnel count approaching ~100k.
- Placement is an explicit, operator-legible table — 256 buckets keyed by the first hex byte of the host id, `bucket -> (primary relay, secondary relay)` per region — not a consistent-hash ring: the fleet is small and low-churn, every fleet change must be staged (tunnels must exist before routing points at them), and one table read by all consumers eliminates the ring-mirroring bug class.
- Visitor steering moves into DNS: each region's zone (`<region>.<content-domain>`) is NS-delegated to a small authoritative DNS service that *computes* answers — no per-share records, no stored routing state. No fronting router exists (frps cannot accept inbound PROXY protocol, so a router would destroy visitor-IP fidelity; and Phase-1 experiments showed the stale-DNS miss case is an instant clean TLS alert, not a hang).
- ACME DNS-01 keeps working across the delegation via computed CNAME indirection: the DNS service answers `_acme-challenge.<name>` with a CNAME to `<ws-domain>.acme.<content-domain>` (Cloudflare-hosted, outside the delegated labels); the connector's cert flow changes only the record name it writes.

## Expected behavior

- Nothing changes for users, clients, or workspaces: same domains, certs, grants, and sharing UX; the gateway keeps polling `GET /shares/assignment` exactly as in Phase 1 — only the returned relay list changes (2 owners instead of all).
- A visitor's DNS query returns the k healthy owner-relay IPs for that specific workspace (TTL 60–120s), so steady-state traffic goes only to relays holding the tunnel; a dead owner's IP disappears within the DNS service's health-probe interval, and browsers' connect-failure fallback covers TTL-stale caches (both owners are in every answer).
- Relay failure: identical story to Phase 1, scoped to the dead relay's buckets — the secondary already holds every affected tunnel; the connector later reassigns the dead relay's bucket slots at leisure.
- Rebalancing (add/remove a relay) is make-before-break and invisible: edit bucket rows secondary-first, gateways converge tunnels via their existing polling, DNS answers follow the table, old tunnels stop after the overlap window. A minimal-movement rebalance command moves only excess buckets.
- Stale-DNS worst case (both owners changed within one TTL, or cache older than the overlap window): an instant, clean TLS error that a reload after TTL expiry resolves — never a hang.
- Certificate issuance and renewal behave identically; the CA follows the challenge CNAME to Cloudflare, where the connector writes/deletes TXT records exactly as today.

## Changes

- **Connector — bucket map**: new `relay_buckets` table (`region`, `bucket` 0–255, `primary_relay_id`, `secondary_relay_id`); `GET /shares/assignment` and share creation resolve through it (Phase 1's "all relays in region" answer becomes a 2-row lookup); admin CLI gains `admin relays buckets list/set/rebalance` with a minimal-movement, secondary-first rebalance; frps `Login` enforcement flips from warn to reject for non-assigned relays.
- **Connector — cert flow**: the DNS-01 TXT write in `share_certs.py` targets `<ws-domain>.acme.<content-domain>` in the Cloudflare zone instead of `_acme-challenge.<ws-domain>` (name-length check for the longest region label; the delegated zone serves the CNAME pointing there).
- **New DNS service** (deployed on the relays themselves, 2–3 NS per region): plan of record is PowerDNS with the remote HTTP backend — a small stateless lookup service implements: workspace names → healthy owner IPs from the bucket table + its own health probes of peer relays; `_acme-challenge.*` → the computed CNAME; correct SOA/NS/NXDOMAIN. CoreDNS with a custom plugin is the fallback; final call via a one-day spike at kickoff. No DNSSEC (no DS record; legal unsigned delegation).
- **DNS cutover per region**: add NS records for `<region>.<content-domain>` in the Cloudflare zone (delegation), retire the static wildcard records; the Phase 1 health cron's DNS reconciliation retires in delegated regions (health filtering lives in the DNS service's answers); rollback is deleting the NS records (wildcards restorable from the relays table).
- **share_relay**: deploy/config-render for the DNS service on relay hosts (systemd unit, zone config); `provision`/`register` unchanged otherwise.
- **Out of scope, enabled by this design**: wake-on-visit for stopped workspaces (the DNS layer answering for a stopped share with a wake-service address is the natural future hook).

## Feasibility checks before Phase 1 ships (why Phase 1 can commit to this path)

- Assignment endpoint contract already carries per-relay ids and a server-controlled poll interval — bucket filtering is a data change, not a wire change.
- Gateways already converge both ways on assignment changes and cache the last answer — rebalancing needs no new workspace behavior.
- Per-relay login records already exist — they are the convergence signal rebalancing waits on.
- The relays table already holds region/endpoint/IP/health — the bucket table only references it.
- The one Phase-2-only external dependency is the NS delegation + DNS service; nothing in Phase 1 assumes Cloudflare remains authoritative for region labels (the health cron's DNS writes are the only coupling, and they retire per delegated region).

## Open questions (resolved at Phase-2 kickoff)

- PowerDNS-remote-backend vs CoreDNS plugin (one-day spike).
- DNS answer TTL final value (60–120s) and the DNS service's health-probe cadence.
- Whether the challenge CNAME target zone needs sharding at very high issuance volume (records are deleted post-issuance; steady state is small).
- `shares.last_tunnel_login_at` retirement in favor of the per-relay table.
