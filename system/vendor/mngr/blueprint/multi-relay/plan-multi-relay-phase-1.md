# Plan: Multi-relay sharing, Phase 1 — two relays per region, full replication, static DNS

Companion: [plan-multi-relay-phase-2.md](./plan-multi-relay-phase-2.md) (bucket-sharded k=2 + computed DNS). Phase 1 deliberately ships every client- and workspace-side seam Phase 2 needs, so Phase 2 is a purely server-side change.

## Overview

- Today each region has exactly one relay: one frps instance, one `SHARE_RELAY_ENDPOINTS` entry, one wildcard A record. A dead relay takes down every share in its region until an operator replaces it and repoints DNS.
- The core constraint (verified empirically on the pinned frp 0.70.1): a visitor's TLS connection must land on the exact frps process holding the workspace's tunnel — frps splices SNI to tunnel in process memory, and frp has no clustering. Plain DNS round-robin over independent relays therefore mis-routes.
- Phase 1 fix: every shared workspace's gateway runs one frpc per relay in its region (k = N = 2, full replication), so every relay holds every tunnel and the static wildcard can safely carry both relay IPs. Failover is browser-native: a dead relay's IP fails TCP connect and browsers fall back to the next A record.
- Verified frp facts this design rests on (see the committed harness, below): a same-domain claim on two frps servers is independent and clean; unknown SNI gets an instant fatal `unrecognized_name` TLS alert + FIN (never a hang); frps has no inbound PROXY protocol support (so no fronting router — it would destroy visitor-IP fidelity); a wedged relay's tunnels are evicted in ~35s (worst case one heartbeat timeout); frpc reconnects in ~2–7s and re-resolves DNS per dial.
- The relay fleet becomes data, not env config: a connector `relays` table (source of truth; replaces `SHARE_RELAY_ENDPOINTS` and `SHARE_DEFAULT_REGION`), self-registered by the provisioning flow, consumed by share creation, a new assignment endpoint, and a health cron that maintains the DNS record set.
- The workspace's relay endpoints become dynamic: `share.env` no longer carries any endpoint; the gateway fetches its assignment from the connector (relay-token auth), polls for changes, and converges its frpc set. This is the seam that makes Phase 2 client-transparent.
- No backwards compatibility is needed: the self-hosted sharing stack is not yet deployed, so wire shapes change cleanly (no legacy fields, no migration choreography for existing shares).

## Expected behavior

- Sharing UX is unchanged: enable/disable, grants, domains, certs, cookies, and the broker flow all work exactly as before.
- A visitor resolving `<label>.<ws-domain>` gets both relay IPs (60s TTL). All traffic works via either relay; load spreads by resolver choice.
- When one relay dies:
  - Existing connections spliced through it drop (inherent; reload recovers). New visits via the surviving relay work immediately — its tunnel already exists, no reconnect, no control-plane action.
  - Visitors who resolve to the dead IP hit TCP connect failure and browsers fall back to the other A record.
  - Within ~2 failed probes the health cron pulls the dead IP from DNS (never emptying the set below 1) and logs the transition at error level. Restore is symmetric on one healthy probe.
- When a relay is wedged (accepting TCP but not splicing): visitors routed to it hang until frp evicts the dead tunnels (~10–30s with the pinned heartbeats), then get instant clean TLS errors; the health cron pulls it if `/healthz` fails.
- The gateway keeps two tunnels up; one relay being down at share-enable no longer matters (`loginFailExit = false`; the frpc for the dead relay retries in the background).
- Share status: "live" if any relay has a recent tunnel login; per-relay login stamps are visible via `mngr imbue_cloud shares status` / ops queries, not in the end-user UI.
- Fleet changes never touch workspaces: the gateway polls its assignment (~60s and on frpc failure) and starts/stops per-relay frpc processes to converge; the last assignment is cached on disk so container restarts do not depend on the connector.
- A share request for a region with zero active relays fails with a clear 503-style error (share-eligible regions are those with ≥1 active relay).
- Latency-unknown shares (local workspaces without a usable `preferred_region`, unmapped datacenters) are spread deterministically by hash of host id over share-eligible regions; a share's region remains sticky once created.
- Dev/ci tiers run the same code path with one relay (k = N = 1); staging runs two relays per region like production.

## Implementation plan

### apps/remote_service_connector

- `migrations/025_relays.sql`:
  - `relays` table: `relay_id` (PK, random id), `region`, `tunnel_endpoint` (`host:port` frpc dials), `ip_address` (for DNS + healthz), `instance_name` (human-readable OVH name), `is_active` (registered/retired), `health` (`healthy` / `unhealthy`), `consecutive_probe_failures`, `created_at`, `updated_at`.
  - `share_tunnel_logins` table: `(host_id, user_id, relay_id)` PK, `last_login_at`. The `shares.last_tunnel_login_at` column stays as the coarse max for existing readers.
- `relays.py` (new): the relay inventory.
  - `RelayStore` protocol + Postgres implementation (list active by region, upsert, retire, record probe result with the 2-strikes-down / 1-to-restore transition rule).
  - Admin router (same `MINDS_ADMIN_KEY` guard as `/admin/accounts/*`): `GET /admin/relays`, `POST /admin/relays` (register/upsert by `relay_id`), `DELETE /admin/relays/{relay_id}` (retire).
  - Pure helpers: `share_eligible_regions()`, `pick_fallback_region(host_id, regions)` (deterministic hash spread).
- `relay_health.py` (new): the health sweep body, separated for unit tests.
  - Probe each active relay's `http://<ip>:8080/healthz` (short timeout); apply the transition rule; on any region whose healthy-IP set changed, reconcile the region wildcard + `relay.<region>` record sets in Cloudflare; floor of 1 (never remove the last IP, even if unhealthy); log every transition at error level (alerting later keys off these logs).
  - Cloudflare A-record-*set* reconciliation helper added next to the existing `cloudflare.py` client (the connector image cannot import `imbue.share_relay`).
- `shares.py`:
  - Delete `parse_relay_endpoint_map` / `share_relay_endpoint_map` / `SHARE_RELAY_ENDPOINTS` / `SHARE_DEFAULT_REGION`; region resolution reads the `relays` table; fallback becomes `pick_fallback_region`.
  - `POST /shares` returns `relay_endpoints: [{relay_id, endpoint}, ...]` (singular `relay_endpoint` field deleted).
  - `GET /shares/relays` returns `{relays: {region: [endpoint, ...]}}` (`default_region` field deleted).
  - New `GET /shares/assignment`: Bearer relay-token auth (same pattern as `POST /shares/cert` in `share_certs.py`); returns `{workspace_domain, relay_endpoints: [{relay_id, endpoint}], poll_seconds}` for the token's active share; this is the endpoint the in-workspace gateway polls.
  - frps plugin path becomes `POST /frps/auth/{plugin_secret}/{relay_id}`; `Login` upserts `share_tunnel_logins` for that relay (and the coarse share stamp); unknown/retired `relay_id` is rejected.
  - `GET /shares/{host_id}/status` gains `relays: [{relay_id, last_login_at}]`.
- `hosts.py` (server-side sharing primitive for pool hosts): `build_share_env_text` drops `SHARE_RELAY_ENDPOINT`; the enable-sharing flow stops resolving an endpoint entirely (the gateway fetches its own assignment).
- `web.py`: mount the relays admin router. `app.py`: register the `relay_health_sweep` function on a ~60s schedule alongside the existing crons.
- `.minds/template/sharing.sh` + per-env Vault entries: remove `SHARE_RELAY_ENDPOINTS` and `SHARE_DEFAULT_REGION` keys.
- Tests: `relays_test.py`, `relay_health_test.py`, updates to `shares_test.py`, `hosts_enable_sharing_test.py`, `testing.py`.

### apps/share_relay

- `primitives.py` / `data_types.py`: `RelayId` (random id primitive); `RelayConfiguration` gains `relay_id` (rendered into the plugin path).
- `config_render.py`: plugin `path` becomes `/frps/auth/<secret>/<relay_id>`.
- `provisioning.py` / `cli.py`:
  - `build_relay_instance_name(env, region, ordinal)` → `share-relay-<env>-<region>-<n>`; `provision` takes the ordinal.
  - New `register` / `deregister` commands calling the connector admin endpoints (`MINDS_ADMIN_KEY` from the environment) with relay_id, region, endpoint, IP; `provision`/`deploy` recipes end with `register`, `destroy` with `deregister`.
  - `dns` reconciles a record *set*: takes `--ip` repeatably, upserts all given A records at TTL 60 and deletes stale ones (today's converge-to-one behavior inverted). Normal operation defers DNS to the connector's health cron; the CLI command remains for bring-up and disaster recovery.
- `dns_records.py`: `reconcile_a_record_set(client, zone_id, name, ips)` replacing single-record upsert semantics; TTL 60.
- New `frp_verification/` (or `scripts/`): the committed manual harness from the design spikes — asserts on a downloaded pinned frp: unknown-SNI → fatal alert 112 + FIN; inbound PROXY v2 → rejected; same-domain claims on two servers both route with correct PROXY source; frozen-client eviction bound; duplicate-claim retry. Run manually on any frp version bump (documented in the README).
- justfile: `provision-share-relay` / `provision-dev-relay` gain the ordinal + registration step; `destroy-share-relay` deregisters.

### default-workspace-template (separate repo, one PR referencing this spec)

- `materials.py`: `SHARE_RELAY_ENDPOINT` removed from required keys and `ShareMaterials` (which keeps domain, token, connector/broker/chrome).
- New `assignment.py`: `fetch_assignment(connector_url, relay_token)` (httpx, Bearer token); disk cache at `data/.state/share_gateway/assignment.json` (last good answer wins when the connector is unreachable); `poll_seconds` honored from the response.
- `frpc_config.py`: render one config per assigned relay — per-instance proxy name, admin port `7401 + index`, and pinned `loginFailExit = false`, `transport.heartbeatInterval = 10`, `transport.heartbeatTimeout = 30`.
- `runner.py`: `ShareStack` holds `frpc_process_by_relay_id`; stack start fetches (or reads cached) assignment and starts one frpc per relay; the tick loop polls the assignment and converges both ways (start newly assigned, stop unassigned); a single frpc dying restarts only that frpc (no longer the whole stack); service-registry changes hot-reload every frpc via its own admin port; cert/caddy handling unchanged.
- Tests: `assignment_test.py`, updates to `frpc_config_test.py`, `materials_test.py`, `server_test.py` / runner coverage, and the dockerized integration harness gains a two-frps topology.

### apps/minds

- `desktop_client/share_materials_injection.py`: `build_share_env_text` drops `relay_endpoint`.
- `desktop_client/sharing_handler.py`: drop the "connector did not return relay coordinates" check (token + domain suffice); `_pick_preferred_relay_region` probes every endpoint of each region and scores the region by its minimum.
- `desktop_client/imbue_cloud_cli.py`: `ShareCliInfo` carries `relay_endpoints`; parsing updated.
- `envs/provisioning.py`: delete the per-env `sharing` override block (`SHARE_DEFAULT_REGION` / `SHARE_RELAY_ENDPOINTS`); dev relays self-register instead.
- `deployment_tests/`: new relay-failover test — SSH to one staging relay with the `relay-ssh` Vault key, `systemctl stop frps`, assert a shared workspace stays reachable through the survivor, restart, assert both relays regain tunnel logins. Soak checklist gains the manual two-IP browser-fallback verification (Chrome/Firefox/Safari) — the one browser-behavior assumption not machine-verified.
- Docs: `docs/staging-bringup.md` (two relays per region, ordinal + register step), `docs/host-pool-setup.md` / `docs/next_deploy.md` touchpoints.

### libs/mngr_imbue_cloud

- `data_types.py`: `ShareInfo.relay_endpoints` (list) replaces `relay_endpoint`; `ShareRelayMap` loses `default_region`; new `RelayAdminInfo`.
- `connector/client.py`: updated share methods; new admin relay methods (list/add/remove).
- `cli/shares.py`: output shape updates. New `cli/relays_admin.py` wired under `mngr imbue_cloud admin relays ...`.

### Changelogs

- One entry per touched project: `apps/remote_service_connector`, `apps/share_relay`, `apps/minds`, `libs/mngr_imbue_cloud` (+ the template's own changelog in its repo).

## Implementation phases

1. **Relay inventory.** Connector `relays` + `share_tunnel_logins` migrations, `relays.py` store + admin API, `mngr imbue_cloud admin relays` CLI, share_relay `register`/`deregister` + relay-id-in-plugin-path, shares.py switched from env vars to the table (still one relay per region). Result: current behavior, fleet as data, `SHARE_RELAY_ENDPOINTS` / `SHARE_DEFAULT_REGION` gone.
2. **Dynamic assignment.** `GET /shares/assignment`; template gateway fetches/polls/caches and renders frpc from it; `share.env` loses the endpoint (connector `hosts.py` + minds injection updated). Result: still one tunnel, but no endpoint is baked anywhere — the Phase 2 seam is live.
3. **Multi-tunnel.** Multi-frpc converge-both-ways in the gateway (per-instance admin ports, pinned frp tuning); `relay_endpoints` list on the wire; dev envs exercise N=1 through the same path. Result: a two-relay dev/staging region carries every share on both relays.
4. **DNS + health.** Record-set reconciliation at TTL 60 in share_relay CLI; connector health sweep maintaining the set (2-strikes / floor-of-1 / error-level transition logs); per-relay login stamps surfaced in `shares status`. Result: the failover story is complete.
5. **Fleet + verification.** Second relay provisioned per staging and production region; deployment failover test; frp verification harness committed; docs updated.

## Testing strategy

- **Unit** (per project): region resolution + hash-spread fallback against the table; assignment endpoint auth (valid/inactive/unknown token); frps-auth with relay ids (login recording, unknown relay rejected); health transition rule (2-strikes down, 1-up restore, floor-of-1, no-op churn); DNS set reconciliation (pure diff logic); frpc render for N endpoints (ports, pins, stable ordering); gateway converge logic (add/remove/restart-one); assignment cache fallback when the connector is unreachable; latency probe scoring with multi-endpoint regions.
- **Integration**: template's dockerized harness with two frps containers — full share flow with both tunnels up; stop one frps and assert continued reachability through the other plus single-frpc restart-not-whole-stack; assignment change converges the frpc set without dropping the surviving tunnel. Connector integration tests for the admin API + assignment endpoint against the test DB.
- **Acceptance / deployment**: the staging relay-failover deployment test (stop frps via SSH, assert reachability, restore, assert re-login on both relays); existing sharing deployment tests keep passing unchanged.
- **Manual (soak checklist)**: two-IP browser fallback in Chrome/Firefox/Safari against a staging share with one relay down; `provision → register → deploy → dns` runbook end-to-end for a new relay.
- **Edge cases**: region with zero active relays (clear error on share create); relay retired while shares are connected (gateway converges off it); connector down at gateway restart (cached assignment keeps the tunnel up); both relays unhealthy (DNS floor holds, errors logged); duplicate registration (upsert, not dup rows).

## Open questions

- Health sweep cadence: confirm the Modal scheduling floor for the sweep function (a ~60s period is assumed; if the effective floor is coarser, the 2-strike detection window stretches accordingly — still acceptable given browser-level fallback is the fast path).
- `poll_seconds` default for the assignment endpoint (spec assumes 60; server-controlled, so tunable post-ship without workspace changes).
- Whether `shares.last_tunnel_login_at` should eventually be dropped in favor of `MAX(share_tunnel_logins.last_login_at)` — kept in Phase 1 to avoid churning existing readers; revisit in Phase 2's migration.
- Exact healthz probe transport hardening (timeouts, treating TCP-refused vs HTTP-503 the same) — to be settled in review of `relay_health.py`.
