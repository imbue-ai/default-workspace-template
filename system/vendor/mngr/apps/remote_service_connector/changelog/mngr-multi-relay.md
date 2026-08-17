Multi-relay sharing, phase 1 (blueprint/multi-relay): the relay fleet becomes data instead of env config.

New `relays` table (+ `share_tunnel_logins`) with an admin API (`GET/POST /admin/relays`, `DELETE /admin/relays/{relay_id}`); the `SHARE_RELAY_ENDPOINTS` and `SHARE_DEFAULT_REGION` env vars are gone. Latency-unknown shares spread deterministically (hash of host id) over the regions with at least one active relay.

`POST /shares` and share status return a `relay_endpoints` list (relay_id + endpoint) instead of a single `relay_endpoint`; `GET /shares/relays` returns endpoint lists per region with no default region.

New relay-token-authenticated `GET /shares/assignment`: the in-workspace share gateway polls it for the relay set to tunnel to, so fleet changes never require touching workspaces.

frps plugin-auth paths gain a per-relay id (`POST /frps/auth/{secret}/{relay_id}`), making tunnel logins attributable per relay (surfaced in `GET /shares/{host_id}/status`).

New per-minute `relay_health_sweep` cron: probes each relay's `/healthz` and reconciles the region wildcard + relay DNS A-record sets (2 consecutive failures pull an IP, 1 success restores, the set is never emptied); transitions log at error level.

Relay registrations validate `ip_address` as a literal IPv4 (it becomes the region's DNS A-record answer); malformed registrations are rejected with a 422.
